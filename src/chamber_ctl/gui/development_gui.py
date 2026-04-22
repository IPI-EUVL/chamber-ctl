import argparse
import json
import queue
import threading
import time
import tkinter as tk
import uuid
import os
from tkinter import ttk, messagebox

import ipi_ecs.core.tcp as tcp
import ipi_ecs.dds.client as client
import ipi_ecs.dds.magics as magics
import ipi_ecs.dds.subsystem as subsystem
import ipi_ecs.dds.types as types
from ipi_ecs.cli.captive_cli import wait_for
from ipi_ecs.core import daemon
from ipi_ecs.logging.client import LogClient

from chamber_ctl.subsystems import uuids


def _fmt_timestamp(ts) -> str:
	if ts is None:
		return ""
	try:
		return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
	except (TypeError, ValueError):
		return str(ts)
	
ECS_IP = os.environ.get("ECS_HOST", "127.0.0.1")


class DevelopmentMetricsDDSClient:
	def __init__(self):
		self.__run = True
		self.__connected = False
		self.__did_config = False

		self.__query_event = None
		self.__read_event = None
		self.__save_event = None

		self.__ui_queue = queue.Queue()
		self.__cmd_queue = queue.Queue()

		c_uuid = uuid.uuid4()
		self.__logger_sock = tcp.TCPClientSocket()
		self.__logger_sock.connect((ECS_IP, 11751))
		self.__logger_sock.start()

		self.__logger = LogClient(self.__logger_sock, origin_uuid=c_uuid)
		self.__client = client.DDSClient(c_uuid, logger=self.__logger, ip=ECS_IP)
		self.__client.when_ready().then(self.__on_ready)

		self.__daemon = daemon.Daemon(exception_handler=self.__on_exception)
		self.__daemon.add(self.__worker)
		self.__daemon.start()

	def __on_exception(self, e: Exception):
		self.__ui_queue.put(("error", f"Worker exception: {e}"))

	def __on_ready(self, _=None):
		if self.__did_config:
			return

		self.__did_config = True
		handle = self.__client.register_subsystem("__development_metrics_gui", uuid.uuid4(), temporary=True)
		self.__on_got_subsystem(handle)

	def __on_got_subsystem(self, handle: client._RegisteredSubsystemHandle):
		self.__query_event = handle.add_event_provider(b"query_exposures")
		self.__query_event.set_types(types.ByteTypeSpecifier(), types.ByteTypeSpecifier())

		self.__read_event = handle.add_event_provider(b"read_exposure")
		self.__read_event.set_types(types.ByteTypeSpecifier(), types.ByteTypeSpecifier())

		self.__save_event = handle.add_event_provider(b"save_measurements")
		self.__save_event.set_types(types.ByteTypeSpecifier(), types.ByteTypeSpecifier())

		self.__connected = True
		self.__ui_queue.put(("connected", None))

	@staticmethod
	def __to_json_bytes(payload: dict) -> bytes:
		return json.dumps(payload).encode("utf-8")

	@staticmethod
	def __decode_event_bytes(value) -> dict:
		if value is None:
			return {"ok": False, "error": "No response."}

		if isinstance(value, bytes):
			raw = value
			if raw.startswith(magics.OP_OK):
				raw = raw[len(magics.OP_OK):]
			if raw.startswith(magics.OP_IN_PROGRESS):
				raw = raw[len(magics.OP_IN_PROGRESS):]
			text = raw.decode("utf-8", errors="replace").strip()
		else:
			text = str(value).strip()

		if not text:
			return {"ok": False, "error": "Empty response."}

		try:
			obj = json.loads(text)
		except json.JSONDecodeError as e:
			return {"ok": False, "error": f"Invalid JSON response: {e}"}

		if not isinstance(obj, dict):
			return {"ok": False, "error": "Invalid response format."}
		return obj

	def __call_event_json(self, event_provider, payload: dict, timeout: float = 10.0) -> dict:
		if event_provider is None:
			return {"ok": False, "error": "Not connected to development metrics subsystem."}

		evt_handle = event_provider.call(self.__to_json_bytes(payload), [uuids.UUID_DEVELOPMENT_METRICS_CONTROLLER])
		if evt_handle is None:
			return {"ok": False, "error": "Failed to send event request."}

		begin = time.monotonic()
		while evt_handle.is_in_progress() and (time.monotonic() - begin) < timeout:
			time.sleep(0.05)

		if evt_handle.is_in_progress():
			return {"ok": False, "error": f"Timed out after {timeout:.1f}s."}

		state = evt_handle.get_state(uuids.UUID_DEVELOPMENT_METRICS_CONTROLLER)
		result = evt_handle.get_result(uuids.UUID_DEVELOPMENT_METRICS_CONTROLLER)
		decoded = self.__decode_event_bytes(result)

		if state != client.EVENT_OK and decoded.get("ok", False):
			decoded["ok"] = False
			decoded["error"] = decoded.get("error") or f"Event failed with state {state}."

		return decoded

	def query_exposures(self, date_str: str):
		self.__cmd_queue.put(("query", date_str))

	def read_exposure(self, exposure_uuid: str):
		self.__cmd_queue.put(("read", exposure_uuid))

	def save_measurements(self, exposure_uuid: str, measurements: list[dict]):
		self.__cmd_queue.put(("save", exposure_uuid, measurements))

	def __worker(self, stop_flag: daemon.StopFlag):
		while stop_flag.run() and self.__run:
			try:
				item = self.__cmd_queue.get(timeout=0.1)
			except queue.Empty:
				continue

			cmd = item[0]
			try:
				if cmd == "query":
					_cmd, date_str = item
					resp = self.__call_event_json(self.__query_event, {"date": date_str}, timeout=20.0)
					self.__ui_queue.put(("query_result", resp))

				elif cmd == "read":
					_cmd, exposure_uuid = item
					resp = self.__call_event_json(self.__read_event, {"exposure_uuid": exposure_uuid}, timeout=10.0)
					self.__ui_queue.put(("read_result", exposure_uuid, resp))

				elif cmd == "save":
					_cmd, exposure_uuid, measurements = item
					payload = {"exposure_uuid": exposure_uuid, "measurements": measurements}
					resp = self.__call_event_json(self.__save_event, payload, timeout=15.0)
					self.__ui_queue.put(("save_result", exposure_uuid, resp))
			except Exception as e:
				self.__ui_queue.put(("error", f"{cmd} failed: {e}"))

	def drain_ui_messages(self) -> list[tuple]:
		out = []
		while True:
			try:
				out.append(self.__ui_queue.get_nowait())
			except queue.Empty:
				return out

	def ok(self) -> bool:
		return self.__run and self.__client.ok() and self.__daemon.is_ok()

	def close(self):
		self.__run = False
		self.__daemon.stop()
		self.__client.close()
		self.__logger_sock.close()


class DevelopmentMetricsGUI:
	EXPOSURE_COLS = (
		"created",
		"uuid",
		"name",
		"description",
		"recorded_dose",
		"recorded_runtime",
		"target_dose",
		"target_time",
	)

	def __init__(self, root: tk.Tk):
		self.__root = root
		self.__closed = False
		root.title("Development Metrics Remote")
		root.geometry("1200x760")
		root.minsize(980, 580)

		self.__client = DevelopmentMetricsDDSClient()
		self.__exposure_by_iid = {}
		self.__selected_exposure_uuid = None
		self.__measurements = []

		self.__status_var = tk.StringVar(value="Connecting...")

		self.__build_ui()
		self.__set_date_today()
		self.__schedule_tick()

		self.__root.protocol("WM_DELETE_WINDOW", self.on_close)

	def __build_ui(self):
		main = ttk.Frame(self.__root, padding=8)
		main.pack(fill=tk.BOTH, expand=True)

		top = ttk.LabelFrame(main, text="Query Exposures", padding=8)
		top.pack(fill=tk.X)

		ttk.Label(top, text="Date (YYYY-MM-DD):").pack(side=tk.LEFT)
		self.__date_var = tk.StringVar()
		self.__date_entry = ttk.Entry(top, textvariable=self.__date_var, width=14)
		self.__date_entry.pack(side=tk.LEFT, padx=(6, 6))
		ttk.Button(top, text="Today", command=self.__set_date_today).pack(side=tk.LEFT, padx=(0, 6))
		ttk.Button(top, text="Refresh", command=self.__on_refresh).pack(side=tk.LEFT)

		paned = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
		paned.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

		left = ttk.Frame(paned)
		paned.add(left, weight=3)

		exposure_lf = ttk.LabelFrame(left, text="Exposures", padding=6)
		exposure_lf.pack(fill=tk.BOTH, expand=True)

		tree_frame = ttk.Frame(exposure_lf)
		tree_frame.pack(fill=tk.BOTH, expand=True)
		tree_frame.rowconfigure(0, weight=1)
		tree_frame.columnconfigure(0, weight=1)

		ysb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL)
		xsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL)

		self.__exposure_tree = ttk.Treeview(
			tree_frame,
			columns=self.EXPOSURE_COLS,
			show="headings",
			yscrollcommand=ysb.set,
			xscrollcommand=xsb.set,
			selectmode="browse",
		)
		ysb.config(command=self.__exposure_tree.yview)
		xsb.config(command=self.__exposure_tree.xview)

		self.__exposure_tree.heading("created", text="Created")
		self.__exposure_tree.heading("uuid", text="UUID")
		self.__exposure_tree.heading("name", text="Name")
		self.__exposure_tree.heading("description", text="Description")
		self.__exposure_tree.heading("recorded_dose", text="Recorded Dose")
		self.__exposure_tree.heading("recorded_runtime", text="Recorded Time")
		self.__exposure_tree.heading("target_dose", text="Target Dose")
		self.__exposure_tree.heading("target_time", text="Target Time")

		self.__exposure_tree.column("created", width=150, minwidth=110)
		self.__exposure_tree.column("uuid", width=100, minwidth=90)
		self.__exposure_tree.column("name", width=120, minwidth=90)
		self.__exposure_tree.column("description", width=240, minwidth=120)
		self.__exposure_tree.column("recorded_dose", width=100, minwidth=90)
		self.__exposure_tree.column("recorded_runtime", width=110, minwidth=90)
		self.__exposure_tree.column("target_dose", width=100, minwidth=90)
		self.__exposure_tree.column("target_time", width=100, minwidth=90)

		self.__exposure_tree.grid(row=0, column=0, sticky=tk.NSEW)
		ysb.grid(row=0, column=1, sticky=tk.NS)
		xsb.grid(row=1, column=0, sticky=tk.EW)

		self.__exposure_tree.bind("<<TreeviewSelect>>", self.__on_exposure_selected)

		right = ttk.Frame(paned)
		paned.add(right, weight=2)

		entry_lf = ttk.LabelFrame(right, text="Add Measurement", padding=8)
		entry_lf.pack(fill=tk.X)

		self.__spot_var = tk.StringVar(value="exposed")
		ttk.Radiobutton(entry_lf, text="Exposed", variable=self.__spot_var, value="exposed").grid(row=0, column=0, sticky=tk.W, padx=2, pady=2)
		ttk.Radiobutton(entry_lf, text="Blank", variable=self.__spot_var, value="blank").grid(row=0, column=1, sticky=tk.W, padx=2, pady=2)

		ttk.Label(entry_lf, text="Thickness (nm):").grid(row=1, column=0, sticky=tk.W, padx=2, pady=2)
		self.__thickness_var = tk.StringVar()
		self.__thickness_entry = ttk.Entry(entry_lf, textvariable=self.__thickness_var, width=16)
		self.__thickness_entry.grid(row=1, column=1, sticky=tk.W, padx=2, pady=2)

		ttk.Label(entry_lf, text="Goodness Of Fit:").grid(row=2, column=0, sticky=tk.W, padx=2, pady=2)
		self.__gof_var = tk.StringVar()
		self.__gof_entry = ttk.Entry(entry_lf, textvariable=self.__gof_var, width=16)
		self.__gof_entry.grid(row=2, column=1, sticky=tk.W, padx=2, pady=2)
		self.__gof_entry.bind("<Return>", self.__on_add_measurement_enter)

		ttk.Button(entry_lf, text="Add Measurement", command=self.__on_add_measurement).grid(
			row=3, column=0, columnspan=2, sticky=tk.W, padx=2, pady=(6, 2)
		)

		meas_lf = ttk.LabelFrame(right, text="Measurements", padding=8)
		meas_lf.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

		m_tree_frame = ttk.Frame(meas_lf)
		m_tree_frame.pack(fill=tk.BOTH, expand=True)
		m_tree_frame.rowconfigure(0, weight=1)
		m_tree_frame.columnconfigure(0, weight=1)

		m_ysb = ttk.Scrollbar(m_tree_frame, orient=tk.VERTICAL)

		self.__meas_tree = ttk.Treeview(
			m_tree_frame,
			columns=("spot", "thickness", "gof"),
			show="headings",
			yscrollcommand=m_ysb.set,
			selectmode="browse",
			height=10,
		)
		m_ysb.config(command=self.__meas_tree.yview)

		self.__meas_tree.heading("spot", text="Spot")
		self.__meas_tree.heading("thickness", text="Thickness (nm)")
		self.__meas_tree.heading("gof", text="Goodness Of Fit")
		self.__meas_tree.column("spot", width=100, minwidth=70)
		self.__meas_tree.column("thickness", width=120, minwidth=90)
		self.__meas_tree.column("gof", width=120, minwidth=90)

		self.__meas_tree.grid(row=0, column=0, sticky=tk.NSEW)
		m_ysb.grid(row=0, column=1, sticky=tk.NS)

		btns = ttk.Frame(meas_lf)
		btns.pack(fill=tk.X, pady=(6, 0))
		ttk.Button(btns, text="Remove Selected", command=self.__on_remove_measurement).pack(side=tk.LEFT)
		ttk.Button(btns, text="Clear", command=self.__clear_measurements).pack(side=tk.LEFT, padx=(6, 0))
		ttk.Button(btns, text="Save", command=self.__on_save).pack(side=tk.RIGHT)

		status = ttk.Label(self.__root, textvariable=self.__status_var, anchor=tk.W, relief=tk.SUNKEN, padding=(6, 3))
		status.pack(side=tk.BOTTOM, fill=tk.X)

	def __set_status(self, text: str):
		self.__status_var.set(text)

	def __set_date_today(self):
		self.__date_var.set(time.strftime("%Y-%m-%d"))

	@staticmethod
	def __as_num_text(value) -> str:
		if value is None:
			return ""
		try:
			return f"{float(value):.6g}"
		except (TypeError, ValueError):
			return str(value)

	def __populate_exposure_tree(self, exposures: list[dict]):
		self.__exposure_tree.delete(*self.__exposure_tree.get_children())
		self.__exposure_by_iid.clear()

		for exposure in exposures:
			uuid_text = str(exposure.get("uuid", ""))
			short_uuid = uuid_text[-8:] if uuid_text else ""
			iid = self.__exposure_tree.insert(
				"",
				tk.END,
				values=(
					_fmt_timestamp(exposure.get("created_at")),
					short_uuid,
					exposure.get("name", ""),
					exposure.get("description", ""),
					self.__as_num_text(exposure.get("recorded_dose")),
					self.__as_num_text(exposure.get("recorded_runtime")),
					self.__as_num_text(exposure.get("target_dose")),
					self.__as_num_text(exposure.get("target_time")),
				),
			)
			self.__exposure_by_iid[iid] = exposure

	def __render_measurements(self):
		self.__meas_tree.delete(*self.__meas_tree.get_children())
		for m in self.__measurements:
			self.__meas_tree.insert(
				"",
				tk.END,
				values=(m["spot_type"], f"{float(m['thickness_nm']):.10g}", f"{float(m['goodness_of_fit']):.10g}"),
			)

	def __clear_measurements(self):
		self.__measurements = []
		self.__render_measurements()

	def __on_refresh(self):
		date_str = self.__date_var.get().strip()
		if not date_str:
			messagebox.showerror("Invalid Date", "Please enter a date in YYYY-MM-DD format.")
			return
		try:
			time.strptime(date_str, "%Y-%m-%d")
		except ValueError:
			messagebox.showerror("Invalid Date", f"Invalid date '{date_str}'. Use YYYY-MM-DD.")
			return

		self.__set_status(f"Querying exposures for {date_str}...")
		self.__client.query_exposures(date_str)

	def __on_exposure_selected(self, _event=None):
		sel = self.__exposure_tree.selection()
		if not sel:
			self.__selected_exposure_uuid = None
			self.__clear_measurements()
			return

		exposure = self.__exposure_by_iid.get(sel[0])
		if exposure is None:
			return

		self.__selected_exposure_uuid = str(exposure.get("uuid", ""))
		if not self.__selected_exposure_uuid:
			return

		self.__clear_measurements()
		self.__set_status(f"Reading measurements for ...{self.__selected_exposure_uuid[-8:]}")
		self.__client.read_exposure(self.__selected_exposure_uuid)

	def __on_add_measurement_enter(self, _event=None):
		self.__on_add_measurement()

	def __on_add_measurement(self):
		spot = self.__spot_var.get().strip().lower()
		if spot not in ("exposed", "blank"):
			messagebox.showerror("Invalid Input", "Spot type must be exposed or blank.")
			return

		try:
			thickness = float(self.__thickness_var.get().strip())
		except ValueError:
			messagebox.showerror("Invalid Input", "Thickness must be a number.")
			return

		try:
			gof = float(self.__gof_var.get().strip())
		except ValueError:
			messagebox.showerror("Invalid Input", "Goodness of fit must be a number.")
			return

		self.__measurements.append({"spot_type": spot, "thickness_nm": thickness, "goodness_of_fit": gof})
		self.__render_measurements()

		self.__thickness_var.set("")
		self.__gof_var.set("")
		self.__thickness_entry.focus_set()

	def __on_remove_measurement(self):
		sel = self.__meas_tree.selection()
		if not sel:
			return
		idx = self.__meas_tree.index(sel[0])
		if 0 <= idx < len(self.__measurements):
			self.__measurements.pop(idx)
			self.__render_measurements()

	def __on_save(self):
		if not self.__selected_exposure_uuid:
			messagebox.showinfo("Save Measurements", "Select an exposure first.")
			return

		self.__set_status(f"Saving {len(self.__measurements)} measurements to ...{self.__selected_exposure_uuid[-8:]}")
		self.__client.save_measurements(self.__selected_exposure_uuid, list(self.__measurements))

	def __handle_query_result(self, response: dict):
		if not response.get("ok", False):
			self.__set_status(f"Query failed: {response.get('error', 'unknown error')}")
			messagebox.showerror("Query Exposures", response.get("error", "Unknown error."))
			return

		exposures = response.get("exposures", [])
		if not isinstance(exposures, list):
			exposures = []

		self.__populate_exposure_tree(exposures)
		self.__selected_exposure_uuid = None
		self.__clear_measurements()
		self.__set_status(f"Found {len(exposures)} exposure(s) for {response.get('date', '')}.")

	def __handle_read_result(self, exposure_uuid: str, response: dict):
		if not response.get("ok", False):
			self.__set_status(f"Read failed for ...{exposure_uuid[-8:]}: {response.get('error', 'unknown error')}")
			messagebox.showerror("Read Exposure", response.get("error", "Unknown error."))
			return

		# Avoid applying stale async responses after selection changed.
		if exposure_uuid != self.__selected_exposure_uuid:
			return

		rows = response.get("measurements", [])
		if not isinstance(rows, list):
			rows = []

		normalized = []
		for row in rows:
			if not isinstance(row, dict):
				continue
			try:
				spot = str(row.get("spot_type", "")).strip().lower()
				if spot not in ("exposed", "blank"):
					continue
				normalized.append(
					{
						"spot_type": spot,
						"thickness_nm": float(row.get("thickness_nm")),
						"goodness_of_fit": float(row.get("goodness_of_fit")),
					}
				)
			except (TypeError, ValueError):
				continue

		self.__measurements = normalized
		self.__render_measurements()
		self.__set_status(f"Loaded {len(normalized)} measurement(s) for ...{exposure_uuid[-8:]}")

	def __handle_save_result(self, exposure_uuid: str, response: dict):
		if not response.get("ok", False):
			self.__set_status(f"Save failed for ...{exposure_uuid[-8:]}: {response.get('error', 'unknown error')}")
			messagebox.showerror("Save Measurements", response.get("error", "Unknown error."))
			return

		self.__set_status(
			f"Saved {int(response.get('saved_count', 0))} measurement(s) for ...{exposure_uuid[-8:]}"
		)

	def __schedule_tick(self):
		for msg in self.__client.drain_ui_messages():
			m_type = msg[0]

			if m_type == "connected":
				self.__set_status("Connected. Select date and click Refresh.")
			elif m_type == "query_result":
				_t, response = msg
				self.__handle_query_result(response)
			elif m_type == "read_result":
				_t, exposure_uuid, response = msg
				self.__handle_read_result(exposure_uuid, response)
			elif m_type == "save_result":
				_t, exposure_uuid, response = msg
				self.__handle_save_result(exposure_uuid, response)
			elif m_type == "error":
				_t, err = msg
				self.__set_status(err)

		if self.__client.ok():
			self.__root.after(100, self.__schedule_tick)
		else:
			self.__set_status("Disconnected from DDS client.")

	def on_close(self):
		if self.__closed:
			return
		self.__closed = True

		self.__client.close()
		if self.__root.winfo_exists():
			self.__root.destroy()


def main(_args: argparse.Namespace = None):
	root = tk.Tk()
	app = DevelopmentMetricsGUI(root)

	try:
		root.mainloop()
	except KeyboardInterrupt:
		pass
	finally:
		app.on_close()

	return 0


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Development metrics remote GUI")
	args = parser.parse_args()
	raise SystemExit(main(args))

