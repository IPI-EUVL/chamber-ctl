import tkinter as tk
from tkinter import ttk
import os
import traceback
import uuid
from collections.abc import Iterable
from queue import Empty, Queue
import time

from chamber_ctl import ECS_IP
from chamber_ctl.data.calibration import SourceKey
from chamber_ctl.gui.experiments_gui import ExperimentsGUI
from chamber_ctl.gui.exposure_controller import ExposureControllerGUI
from chamber_ctl.gui.acquisition import AcquisitionGUI
from chamber_ctl.gui.batch_controller import BatchControllerGUI
from chamber_ctl.gui.laser_sync import LaserSyncTestGUI
from chamber_ctl.gui.settings_presets import SettingsPresetsGUI
from chamber_ctl.gui.sample_motion_gui import SampleMotionDDSClient, SampleStageControl
from chamber_ctl.gui.target_motion import TargetControlGUI
from chamber_ctl.interfaces import target_controller_interface
from chamber_ctl.subsystems import uuids as subsystem_uuids
from ipi_ecs.gui.lifecycle_gui import LifecycleGUI
from ipi_ecs.core import daemon
from ipi_ecs.dds import client, subsystem as dds_subsystem


DEFAULT_DATA_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
DEFAULT_EXPERIMENT_NAME = "exposure"
DEFAULT_LIFECYCLE_UUID = uuid.uuid3(uuid.NAMESPACE_OID, "Lifecycle Manager")
ACQUISITION_WORKSPACE_TABS = ("Exposure", "Capture Diagnostics")
CONFIGURED_SOURCE_STATUS_PREFIX = "Configured source: "


def configured_sources_from_status_rows(rows: Iterable[dict]) -> tuple[SourceKey, ...]:
	sources: set[SourceKey] = set()
	for row in rows:
		if not isinstance(row, dict) or row.get("connected") is not True:
			continue
		for item in row.get("status_items", ()):
			if item.get_code() != 10:
				continue
			message = item.get_message()
			if not isinstance(message, str) or not message.startswith(CONFIGURED_SOURCE_STATUS_PREFIX):
				continue
			kind, separator, source_id = message.removeprefix(CONFIGURED_SOURCE_STATUS_PREFIX).partition("/")
			if not separator:
				continue
			try:
				sources.add(SourceKey(kind.strip(), source_id.strip()))
			except ValueError:
				continue
	return tuple(sorted(sources))


def _known_subsystems_from_uuids() -> list[tuple[str, uuid.UUID]]:
	known: list[tuple[str, uuid.UUID]] = []
	for name, value in subsystem_uuids.__dict__.items():
		if not name.startswith("UUID_"):
			continue
		if not isinstance(value, uuid.UUID):
			continue
		label = name.removeprefix("UUID_").replace("_", " ").title()
		known.append((label, value))
	return known


class SubsystemStatusMonitor:
	def __init__(self, known_subsystems: list[tuple[str, uuid.UUID]], dds_ip: str = ECS_IP):
		self.__known_subsystems = known_subsystems
		self.__dds_ip = dds_ip

		self.__out_queue: Queue = Queue()
		self.__cmd_queue: Queue = Queue()

		self.__did_config = False
		self.__client: client.DDSClient | None = None
		self.__subsystem_handle: client._RegisteredSubsystemHandle | None = None

		self.__event_consumer = None
		self.__E_SYSTEM_UPDATE = None

		self.__next_periodic_emit = 0.0

		c_uuid = uuid.uuid4()
		s_uuid = uuid.uuid4()
		self.__client = client.DDSClient(c_uuid, ip=dds_ip)

		def _on_ready():
			if self.__did_config:
				return

			self.__did_config = True
			sh = self.__client.register_subsystem(f"__central_status_{s_uuid}", s_uuid, temporary=True)
			self.__on_got_subsystem(sh)

		self.__client.when_ready().then(_on_ready)

		import mt_events

		self.__event_consumer = mt_events.EventConsumer()
		self.__E_SYSTEM_UPDATE = self.__client.on_remote_system_update().bind(self.__event_consumer)

		self.__daemon = daemon.Daemon()
		self.__daemon.add(self.__thread)
		self.__daemon.start()

	def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
		self.__subsystem_handle = handle
		self.__emit_snapshot()

	def __await_awaiter(self, awaiter_handle, timeout_s: float = 0.75):
		import mt_events

		done = {"ok": False, "value": None, "has_result": False}
		consumer = mt_events.EventConsumer()
		evt = mt_events.Event()

		def _ok(value=None):
			done["ok"] = True
			done["value"] = value
			done["has_result"] = True
			evt.call()

		def _err(*_args, **_kwargs):
			done["ok"] = False
			done["value"] = None
			done["has_result"] = True
			evt.call()

		awaiter_handle.then(_ok).catch(_err)
		bound = evt.bind(consumer)

		start = time.time()
		while time.time() - start < timeout_s:
			try:
				e = consumer.get(timeout=0.1)
				if e == bound and done["has_result"]:
					return done
			except Empty:
				pass

		return {"ok": False, "value": None, "has_result": False}

	def __await_status(self, remote, timeout_s: float = 0.75):
		ret = remote.get_status()
		if ret is None:
			return None

		result = self.__await_awaiter(ret, timeout_s=timeout_s)
		if not result["ok"]:
			return None

		return result["value"]

	@staticmethod
	def __severity_text(sev: int) -> str:
		if sev == dds_subsystem.StatusItem.STATE_ALARM:
			return "ALARM"
		if sev == dds_subsystem.StatusItem.STATE_WARN:
			return "WARN"
		return "INFO"

	@staticmethod
	def __status_summary(connected: bool, status_items: list[dds_subsystem.StatusItem]) -> str:
		for item in status_items:
			if item.get_code() == 0:
				message = item.get_message()
				if message is not None and message.strip() != "":
					return message

		has_alarm = any(item.get_severity() == dds_subsystem.StatusItem.STATE_ALARM for item in status_items)
		has_warn = any(item.get_severity() == dds_subsystem.StatusItem.STATE_WARN for item in status_items)

		if has_alarm:
			return "ALARM"
		if has_warn:
			return "WARN"
		if connected:
			return "Connected"
		return "Disconnected"

	def __build_snapshot(self) -> list[dict]:
		by_uuid: dict[uuid.UUID, dict] = {}

		if self.__subsystem_handle is not None:
			for remote, state_from_all in self.__subsystem_handle.get_all():
				info = remote.get_info()
				s_uuid = info.get_uuid()

				state = state_from_all
				fresh_state = self.__await_status(remote)
				if fresh_state is not None:
					state = fresh_state

				status_items = list(state.get_status_items())
				by_uuid[s_uuid] = {
					"uuid": s_uuid,
					"name": info.get_name(),
					"connected": state.get_status() == dds_subsystem.SubsystemStatus.STATE_ALIVE,
					"status_items": status_items,
				}

		rows: list[dict] = []
		known_uuids: set[uuid.UUID] = set()
		for default_name, s_uuid in self.__known_subsystems:
			known_uuids.add(s_uuid)
			live = by_uuid.get(s_uuid)
			if live is None:
				status_items = []
				connected = False
				name = default_name
			else:
				status_items = live["status_items"]
				connected = bool(live["connected"])
				name = live["name"] or default_name

			has_warn = any(item.get_severity() == dds_subsystem.StatusItem.STATE_WARN for item in status_items)
			has_error = any(item.get_severity() == dds_subsystem.StatusItem.STATE_ALARM for item in status_items)

			rows.append(
				{
					"uuid": s_uuid,
					"name": name,
					"connected": connected,
					"status_items": status_items,
					"has_warn": has_warn,
					"has_error": has_error,
					"status_text": self.__status_summary(connected, status_items),
				}
			)

		for s_uuid, live in sorted(
			(
				(key, value)
				for key, value in by_uuid.items()
				if key not in known_uuids and configured_sources_from_status_rows((value,))
			),
			key=lambda item: (str(item[1]["name"]).casefold(), str(item[0])),
		):
			status_items = live["status_items"]
			rows.append(
				{
					"uuid": s_uuid,
					"name": live["name"],
					"connected": bool(live["connected"]),
					"status_items": status_items,
					"has_warn": any(
						item.get_severity() == dds_subsystem.StatusItem.STATE_WARN
						for item in status_items
					),
					"has_error": any(
						item.get_severity() == dds_subsystem.StatusItem.STATE_ALARM
						for item in status_items
					),
					"status_text": self.__status_summary(bool(live["connected"]), status_items),
				}
			)

		return rows

	def __emit_snapshot(self):
		rows = self.__build_snapshot()
		self.__out_queue.put(("snapshot", rows))

	def __thread(self, stop_flag: daemon.StopFlag):
		while stop_flag.run():
			try:
				while True:
					cmd = self.__cmd_queue.get_nowait()
					if cmd == "refresh":
						self.__emit_snapshot()
			except Empty:
				pass

			if self.__subsystem_handle is not None and time.time() >= self.__next_periodic_emit:
				self.__next_periodic_emit = time.time() + 1.5
				self.__emit_snapshot()

			if self.__subsystem_handle is None:
				time.sleep(0.05)
				continue

			try:
				e = self.__event_consumer.get(timeout=0.2)
				if e == self.__E_SYSTEM_UPDATE:
					self.__emit_snapshot()
			except Empty:
				pass

	def request_refresh(self):
		self.__cmd_queue.put("refresh")

	def pop_messages(self) -> list:
		msgs = []
		try:
			while True:
				msgs.append(self.__out_queue.get_nowait())
		except Empty:
			pass
		return msgs

	def close(self):
		if self.__client is not None:
			self.__client.close()
		self.__daemon.stop()


class CentralGUI:
	def __init__(self, root: tk.Tk):
		self.__root = root
		self.__components: list[tuple[str, object, str]] = []
		self.__known_subsystems = _known_subsystems_from_uuids()
		self.__status_rows: list[dict] = []
		self.__status_monitor: SubsystemStatusMonitor | None = None
		self.__status_update_job = None
		self.__status_text_var = tk.StringVar(value="Connecting subsystem monitor...")

		self.__root.title("EUVL Central Control")
		self.__root.geometry("1600x980")
		self.__root.minsize(1200, 760)

		layout = ttk.PanedWindow(self.__root, orient=tk.HORIZONTAL)
		layout.pack(fill=tk.BOTH, expand=True)

		self.__left_panel = ttk.Frame(layout)
		self.__right_panel = ttk.Frame(layout)
		layout.add(self.__left_panel, weight=1)
		layout.add(self.__right_panel, weight=3)

		self.__build_subsystem_status_panel(self.__left_panel)

		self.__notebook = ttk.Notebook(self.__right_panel)
		self.__notebook.pack(fill=tk.BOTH, expand=True)

		self.__start_subsystem_monitor()
		self.__build_tabs()
		self.__root.protocol("WM_DELETE_WINDOW", self.on_close)

	def __register_component(self, tab_name: str, component: object, close_method: str):
		self.__components.append((tab_name, component, close_method))

	def __add_error_content(self, parent, title: str, exc: Exception):
		outer = ttk.Frame(parent, padding=12)
		outer.pack(fill=tk.BOTH, expand=True)
		ttk.Label(outer, text=f"Failed to initialize {title} tab", foreground="red").pack(anchor=tk.W)
		ttk.Label(outer, text=str(exc)).pack(anchor=tk.W, pady=(4, 8))

		txt = tk.Text(outer, wrap=tk.WORD, height=24)
		txt.pack(fill=tk.BOTH, expand=True)
		txt.insert("1.0", traceback.format_exc())
		txt.config(state=tk.DISABLED)

	def __build_tabs(self):
		self.__build_experiments_tab()
		self.__build_acquisition_tab()
		self.__build_batch_tab()
		self.__build_settings_presets_tab()
		self.__build_laser_sync_tab()
		self.__build_sample_motion_tab()
		self.__build_target_motion_tab()
		self.__build_lifecycle_tab()

	def __build_subsystem_status_panel(self, parent):
		outer = ttk.LabelFrame(parent, text="Subsystem Status", padding=6)
		outer.pack(fill=tk.BOTH, expand=True, padx=(6, 3), pady=6)

		bar = ttk.Frame(outer)
		bar.pack(fill=tk.X, pady=(0, 4))
		self.__status_refresh_btn = ttk.Button(bar, text="Refresh", command=self.__on_refresh_subsystem_status)
		self.__status_refresh_btn.pack(side=tk.LEFT)

		canvas_frame = ttk.Frame(outer)
		canvas_frame.pack(fill=tk.BOTH, expand=True)

		self.__status_canvas = tk.Canvas(canvas_frame, background="#f3f3f3", highlightthickness=0)
		self.__status_vsb = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.__status_canvas.yview)
		self.__status_canvas.configure(yscrollcommand=self.__status_vsb.set)

		self.__status_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
		self.__status_vsb.pack(side=tk.RIGHT, fill=tk.Y)
		self.__status_canvas.bind("<Configure>", self.__on_status_canvas_configure)
		self.__status_canvas.bind("<Enter>", self.__on_status_canvas_enter)
		self.__status_canvas.bind("<Leave>", self.__on_status_canvas_leave)

		status_lbl = ttk.Label(outer, textvariable=self.__status_text_var, anchor=tk.W)
		status_lbl.pack(fill=tk.X, pady=(4, 0))

		self.__rebuild_subsystem_status_cards()

	def __on_status_canvas_configure(self, event):
		_ = event
		self.__rebuild_subsystem_status_cards()

	def __on_status_canvas_enter(self, _event=None):
		self.__root.bind_all("<MouseWheel>", self.__on_status_canvas_mousewheel)
		self.__root.bind_all("<Button-4>", self.__on_status_canvas_mousewheel)
		self.__root.bind_all("<Button-5>", self.__on_status_canvas_mousewheel)

	def __on_status_canvas_leave(self, _event=None):
		self.__root.unbind_all("<MouseWheel>")
		self.__root.unbind_all("<Button-4>")
		self.__root.unbind_all("<Button-5>")

	def __on_status_canvas_mousewheel(self, event):
		if getattr(event, "num", None) == 4:
			self.__status_canvas.yview_scroll(-1, "units")
			return
		if getattr(event, "num", None) == 5:
			self.__status_canvas.yview_scroll(1, "units")
			return

		delta = getattr(event, "delta", 0)
		if delta == 0:
			return
		steps = int(-delta / 120)
		if steps == 0:
			steps = -1 if delta > 0 else 1
		self.__status_canvas.yview_scroll(steps, "units")

	def __draw_rounded_rect(self, x1: float, y1: float, x2: float, y2: float, radius: float, fill: str, outline: str, width: int = 1):
		r = max(1.0, min(radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
		points = [
			x1 + r,
			y1,
			x2 - r,
			y1,
			x2,
			y1,
			x2,
			y1 + r,
			x2,
			y2 - r,
			x2,
			y2,
			x2 - r,
			y2,
			x1 + r,
			y2,
			x1,
			y2,
			x1,
			y2 - r,
			x1,
			y1 + r,
			x1,
			y1,
		]
		return self.__status_canvas.create_polygon(points, smooth=True, splinesteps=20, fill=fill, outline=outline, width=width)

	@staticmethod
	def __severity_text(sev: int) -> str:
		if sev == dds_subsystem.StatusItem.STATE_ALARM:
			return "ALARM"
		if sev == dds_subsystem.StatusItem.STATE_WARN:
			return "WARN"
		return "INFO"

	@staticmethod
	def __status_card_palette(row: dict) -> tuple[str, str, str]:
		if row.get("has_error", False):
			return ("#ffd8d8", "#b66a6a", "error")
		if row.get("has_warn", False):
			return ("#fff4cc", "#c9a73a", "warn")
		if row.get("connected", False):
			return ("#ddf4e1", "#73b687", "ok")
		return ("#ffcccc", "#bf5a5a", "disconnected")

	def __draw_status_icon(self, x1: float, y1: float, x2: float, y2: float, icon_kind: str):
		cx = (x1 + x2) / 2.0
		cy = (y1 + y2) / 2.0
		size = min(x2 - x1, y2 - y1)

		if icon_kind == "ok":
			self.__status_canvas.create_text(
				cx,
				cy,
				text="OK",
				font=("Segoe UI", max(18, int(size * 0.45)), "bold"),
				fill="#10d62f",
				anchor="center",
			)
			return

		if icon_kind == "disconnected":
			r = size * 0.42
			self.__status_canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#ff5a00", outline="#ff5a00", width=2)
			bar_h = max(8, size * 0.16)
			self.__status_canvas.create_rectangle(
				cx - r * 0.62,
				cy - bar_h / 2,
				cx + r * 0.62,
				cy + bar_h / 2,
				fill="#ffffff",
				outline="#ffffff",
			)
			return

		# Triangle icons for warning/error.
		h = size * 0.78
		w = size * 0.80
		p1 = (cx, cy - h / 2)
		p2 = (cx - w / 2, cy + h / 2)
		p3 = (cx + w / 2, cy + h / 2)

		if icon_kind == "warn":
			fill_color = "#f0d000"
			outline_color = "#202020"
		else:
			fill_color = "#e11b22"
			outline_color = "#b01218"

		self.__status_canvas.create_polygon(
			p1[0],
			p1[1],
			p2[0],
			p2[1],
			p3[0],
			p3[1],
			fill=fill_color,
			outline=outline_color,
			width=3,
		)
		self.__status_canvas.create_line(cx, cy - h * 0.15, cx, cy + h * 0.20, fill="#ffffff" if icon_kind == "error" else "#101010", width=max(4, int(size * 0.10)), capstyle=tk.ROUND)
		self.__status_canvas.create_oval(
			cx - size * 0.05,
			cy + h * 0.26,
			cx + size * 0.05,
			cy + h * 0.36,
			fill="#ffffff" if icon_kind == "error" else "#101010",
			outline="#ffffff" if icon_kind == "error" else "#101010",
		)

	def __primary_status_text(self, row: dict) -> str:
		for item in row.get("status_items", []):
			if item.get_code() == 0:
				message = item.get_message()
				if message is not None and message.strip() != "":
					return message
		return "CONNECTED" if row.get("connected", False) else "DISCONNECTED"

	@staticmethod
	def __line_height(font_size: int) -> int:
		return font_size + 8

	@staticmethod
	def __estimated_wrapped_lines(text: str, width_px: int, avg_char_px: int = 7) -> int:
		if text is None or text == "":
			return 1
		max_chars = max(12, width_px // max(1, avg_char_px))
		lines = 0
		for paragraph in str(text).splitlines() or [str(text)]:
			p = paragraph.strip()
			if p == "":
				lines += 1
				continue
			lines += max(1, (len(p) + max_chars - 1) // max_chars)
		return max(1, lines)

	def __rebuild_subsystem_status_cards(self):
		self.__status_canvas.delete("all")

		canvas_width = max(self.__status_canvas.winfo_width(), 260)
		card_margin_x = 10
		card_spacing = 10
		icon_col_w = 64
		card_x1 = card_margin_x
		card_x2 = canvas_width - card_margin_x
		card_width = max(220, card_x2 - card_x1)
		content_x = card_x1 + icon_col_w + 12
		content_w = max(120, card_width - icon_col_w - 24)
		y_cursor = 10

		for row in self.__status_rows:
			bg, border, icon_kind = self.__status_card_palette(row)

			title_font = ("Segoe UI", 12, "bold")
			primary_font = ("Segoe UI", 11, "bold")
			item_font = ("Segoe UI", 10)

			primary = self.__primary_status_text(row)
			status_items = [item for item in row.get("status_items", []) if item.get_code() != 0]

			title_lines = self.__estimated_wrapped_lines(row.get("name", ""), content_w, avg_char_px=7)
			primary_lines = self.__estimated_wrapped_lines(primary, content_w, avg_char_px=7)

			if len(status_items) == 0:
				items_lines = 1
			else:
				items_lines = 0
				for item in status_items:
					sev_text = self.__severity_text(item.get_severity())
					msg = item.get_message() or ""
					line = f"• {item.get_code()}: [{sev_text}] {msg}"
					items_lines += self.__estimated_wrapped_lines(line, content_w, avg_char_px=7)

			card_h = 16
			card_h += title_lines * self.__line_height(12)
			card_h += primary_lines * self.__line_height(11)
			card_h += items_lines * self.__line_height(10)
			card_h += 18
			card_h = max(card_h, 98)

			card_y1 = y_cursor
			card_y2 = y_cursor + card_h

			self.__draw_rounded_rect(card_x1, card_y1, card_x2, card_y2, radius=18, fill=bg, outline=border, width=2)
			icon_x1 = card_x1 + 8
			icon_y1 = card_y1 + 10
			icon_x2 = card_x1 + icon_col_w - 4
			icon_y2 = min(card_y2 - 10, icon_y1 + 70)
			self.__draw_status_icon(icon_x1, icon_y1, icon_x2, icon_y2, icon_kind)

			title_id = self.__status_canvas.create_text(
				content_x,
				card_y1 + 10,
				text=row["name"],
				font=title_font,
				fill="#000000",
				anchor="nw",
				width=content_w,
			)
			title_bbox = self.__status_canvas.bbox(title_id)
			text_y = (title_bbox[3] if title_bbox is not None else card_y1 + 30) + 4

			primary_id = self.__status_canvas.create_text(
				content_x,
				text_y,
				text=primary,
				font=primary_font,
				fill="#000000",
				anchor="nw",
				width=content_w,
			)
			primary_bbox = self.__status_canvas.bbox(primary_id)
			text_y = (primary_bbox[3] if primary_bbox is not None else text_y + 20) + 6

			if len(status_items) == 0:
				item_id = self.__status_canvas.create_text(
					content_x,
					text_y,
					text="• (no active status items)",
					font=item_font,
					fill="#000000",
					anchor="nw",
					width=content_w,
				)
				item_bbox = self.__status_canvas.bbox(item_id)
				text_y = (item_bbox[3] if item_bbox is not None else text_y + 18) + 4
			else:
				for item in status_items:
					sev_text = self.__severity_text(item.get_severity())
					msg = item.get_message() or ""
					line = f"• {item.get_code()}: [{sev_text}] {msg}"
					item_id = self.__status_canvas.create_text(
						content_x,
						text_y,
						text=line,
						font=item_font,
						fill="#000000",
						anchor="nw",
						width=content_w,
					)
					item_bbox = self.__status_canvas.bbox(item_id)
					text_y = (item_bbox[3] if item_bbox is not None else text_y + 18) + 3

			y_cursor = card_y2 + card_spacing

		self.__status_canvas.configure(scrollregion=(0, 0, canvas_width, y_cursor + 6))

	def __on_refresh_subsystem_status(self):
		if self.__status_monitor is not None:
			self.__status_monitor.request_refresh()
			self.__status_text_var.set("Refreshing subsystem statuses...")

	def __start_subsystem_monitor(self):
		try:
			self.__status_monitor = SubsystemStatusMonitor(self.__known_subsystems)
			self.__register_component("Subsystem Status Monitor", self.__status_monitor, "close")
			self.__status_monitor.request_refresh()
			self.__status_updater()
		except Exception as exc:
			self.__status_monitor = None
			self.__status_text_var.set(f"Failed to start subsystem monitor: {exc}")

	def __status_updater(self):
		if self.__status_monitor is not None:
			for msg in self.__status_monitor.pop_messages():
				kind = msg[0]
				if kind == "snapshot":
					_, rows = msg
					self.__status_rows = rows
					offline_count = len([r for r in rows if not r["connected"]])
					self.__status_text_var.set(f"Tracking {len(rows)} subsystem(s); {offline_count} offline.")
					self.__rebuild_subsystem_status_cards()

		self.__status_update_job = self.__root.after(200, self.__status_updater)

	def __build_experiments_tab(self):
		tab = ttk.Frame(self.__notebook)
		self.__notebook.add(tab, text="Experiments")
		db_path = os.path.join(DEFAULT_DATA_PATH, "library.sqlite3")
		if not os.path.isfile(db_path):
			outer = ttk.Frame(tab, padding=12)
			outer.pack(fill=tk.BOTH, expand=True)
			ttk.Label(
				outer,
				text="Experiments tab is disabled because the local database file was not found.",
				foreground="red",
			).pack(anchor=tk.W)
			ttk.Label(outer, text=f"Expected: {db_path}").pack(anchor=tk.W, pady=(4, 0))
			return
		try:
			comp = ExperimentsGUI(tab, DEFAULT_DATA_PATH, DEFAULT_EXPERIMENT_NAME, own_window=False)
			self.__register_component("Experiments", comp, "on_close")
		except Exception as exc:
			self.__add_error_content(tab, "Experiments", exc)

	def __build_acquisition_tab(self):
		tab = ttk.Frame(self.__notebook)
		self.__notebook.add(tab, text="Acquisition")
		workspace = ttk.Notebook(tab)
		workspace.pack(fill=tk.BOTH, expand=True)

		exposure_tab = ttk.Frame(workspace)
		workspace.add(exposure_tab, text=ACQUISITION_WORKSPACE_TABS[0])
		try:
			comp = ExposureControllerGUI(
				exposure_tab,
				own_window=False,
				source_options_provider=self.__configured_sources,
			)
			self.__register_component("Acquisition / Exposure", comp, "close")
		except Exception as exc:
			self.__add_error_content(exposure_tab, "Exposure", exc)

		diagnostics_tab = ttk.Frame(workspace)
		workspace.add(diagnostics_tab, text=ACQUISITION_WORKSPACE_TABS[1])
		try:
			comp = AcquisitionGUI(diagnostics_tab, own_window=False, data_path=DEFAULT_DATA_PATH)
			self.__register_component("Acquisition / Capture Diagnostics", comp, "close")
		except Exception as exc:
			self.__add_error_content(diagnostics_tab, "Capture Diagnostics", exc)

	def __build_batch_tab(self):
		tab = ttk.Frame(self.__notebook)
		self.__notebook.add(tab, text="Batch Controller")
		try:
			comp = BatchControllerGUI(
				tab,
				DEFAULT_DATA_PATH,
				own_window=False,
				source_options_provider=self.__configured_sources,
			)
			self.__register_component("Batch Controller", comp, "close")
		except Exception as exc:
			self.__add_error_content(tab, "Batch Controller", exc)

	def __configured_sources(self) -> tuple[SourceKey, ...]:
		return configured_sources_from_status_rows(self.__status_rows)

	def __build_settings_presets_tab(self):
		tab = ttk.Frame(self.__notebook)
		self.__notebook.add(tab, text="Settings Presets")
		try:
			comp = SettingsPresetsGUI(tab, DEFAULT_DATA_PATH, own_window=False)
			self.__register_component("Settings Presets", comp, "close")
		except Exception as exc:
			self.__add_error_content(tab, "Settings Presets", exc)

	def __build_laser_sync_tab(self):
		tab = ttk.Frame(self.__notebook)
		self.__notebook.add(tab, text="Laser Sync")
		try:
			comp = LaserSyncTestGUI(tab, own_window=False)
			self.__register_component("Laser Sync", comp, "close")
		except Exception as exc:
			self.__add_error_content(tab, "Laser Sync", exc)

	def __build_sample_motion_tab(self):
		tab = ttk.Frame(self.__notebook)
		self.__notebook.add(tab, text="Sample Motion")
		try:
			ctl = SampleMotionDDSClient()
			comp = SampleStageControl(tab, ctl, own_window=False)
			self.__register_component("Sample Motion", comp, "cleanup")
		except Exception as exc:
			self.__add_error_content(tab, "Sample Motion", exc)

	def __build_target_motion_tab(self):
		tab = ttk.Frame(self.__notebook)
		self.__notebook.add(tab, text="Target Motion")
		try:
			itf = target_controller_interface.TargetClient()
			comp = TargetControlGUI(tab, itf, own_window=False)
			self.__register_component("Target Motion GUI", comp, "close")
			self.__register_component("Target Motion Interface", itf, "close")
		except Exception as exc:
			self.__add_error_content(tab, "Target Motion", exc)

	def __build_lifecycle_tab(self):
		tab = ttk.Frame(self.__notebook)
		self.__notebook.add(tab, text="Lifecycle")
		try:
			comp = LifecycleGUI(tab, DEFAULT_LIFECYCLE_UUID, own_window=False, dds_ip=ECS_IP)
			self.__register_component("Lifecycle", comp, "on_close")
		except Exception as exc:
			self.__add_error_content(tab, "Lifecycle", exc)

	def on_close(self):
		self.__on_status_canvas_leave()

		if self.__status_update_job is not None:
			try:
				self.__root.after_cancel(self.__status_update_job)
			except Exception:
				pass
			self.__status_update_job = None

		for tab_name, component, close_method in reversed(self.__components):
			method = getattr(component, close_method, None)
			if method is None:
				continue
			try:
				method()
			except Exception as exc:
				print(f"Failed closing {tab_name}: {exc}")

		self.__root.destroy()


def main():
	root = tk.Tk()
	CentralGUI(root)
	root.mainloop()


if __name__ == "__main__":
	main()
