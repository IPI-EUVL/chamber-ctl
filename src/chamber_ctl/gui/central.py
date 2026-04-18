import tkinter as tk
from tkinter import ttk
import os
import traceback
import uuid

from chamber_ctl.gui.experiments_gui import ExperimentsGUI
from chamber_ctl.gui.exposure_controller import ExposureControllerGUI
from chamber_ctl.gui.laser_sync import LaserSyncTestGUI
from chamber_ctl.gui.settings_presets import SettingsPresetsGUI
from chamber_ctl.gui.sample_motion_gui import SampleMotionDDSClient, SampleStageControl
from chamber_ctl.gui.target_motion import TargetControlGUI
from chamber_ctl.interfaces import target_controller_interface
from ipi_ecs.gui.lifecycle_gui import LifecycleGUI


DEFAULT_DATA_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
DEFAULT_EXPERIMENT_NAME = "exposure"
DEFAULT_LIFECYCLE_UUID = uuid.uuid3(uuid.NAMESPACE_OID, "Lifecycle Manager")


class CentralGUI:
	def __init__(self, root: tk.Tk):
		self.__root = root
		self.__components: list[tuple[str, object, str]] = []

		self.__root.title("EUVL Central Control")
		self.__root.geometry("1600x980")
		self.__root.minsize(1200, 760)

		self.__notebook = ttk.Notebook(self.__root)
		self.__notebook.pack(fill=tk.BOTH, expand=True)

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
		self.__build_exposure_tab()
		self.__build_settings_presets_tab()
		self.__build_laser_sync_tab()
		self.__build_sample_motion_tab()
		self.__build_target_motion_tab()
		self.__build_lifecycle_tab()

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

	def __build_exposure_tab(self):
		tab = ttk.Frame(self.__notebook)
		self.__notebook.add(tab, text="Exposure Controller")
		try:
			comp = ExposureControllerGUI(tab, own_window=False)
			self.__register_component("Exposure Controller", comp, "close")
		except Exception as exc:
			self.__add_error_content(tab, "Exposure Controller", exc)

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
			comp = LifecycleGUI(tab, DEFAULT_LIFECYCLE_UUID, own_window=False)
			self.__register_component("Lifecycle", comp, "on_close")
		except Exception as exc:
			self.__add_error_content(tab, "Lifecycle", exc)

	def on_close(self):
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
