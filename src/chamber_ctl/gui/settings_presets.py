import os
import tkinter as tk
from tkinter import messagebox, ttk

from chamber_ctl.subsystems.settings_presets import SettingsPresets


class PresetListFrame(ttk.LabelFrame):
	def __init__(self, parent, title: str):
		super().__init__(parent, text=title, padding=8)
		self.__build()

	def __build(self):
		self.columnconfigure(0, weight=1)
		self.rowconfigure(0, weight=1)

		list_container = ttk.Frame(self)
		list_container.grid(row=0, column=0, columnspan=2, sticky=tk.NSEW)
		list_container.rowconfigure(0, weight=1)
		list_container.columnconfigure(0, weight=1)

		self.__listbox = tk.Listbox(list_container, selectmode=tk.BROWSE, height=12)
		self.__listbox.grid(row=0, column=0, sticky=tk.NSEW)

		ysb = ttk.Scrollbar(list_container, orient=tk.VERTICAL, command=self.__listbox.yview)
		ysb.grid(row=0, column=1, sticky=tk.NS)
		self.__listbox.config(yscrollcommand=ysb.set)

		self.__entry_var = tk.StringVar()
		entry = ttk.Entry(self, textvariable=self.__entry_var)
		entry.grid(row=1, column=0, sticky=tk.EW, padx=(0, 4), pady=(8, 4))
		entry.bind("<Return>", lambda _e: self.__add_or_update())

		ttk.Button(self, text="Add/Update", command=self.__add_or_update).grid(
			row=1,
			column=1,
			sticky=tk.EW,
			pady=(8, 4),
		)

		btns = ttk.Frame(self)
		btns.grid(row=2, column=0, columnspan=2, sticky=tk.W)
		ttk.Button(btns, text="Use Selected", command=self.__use_selected).pack(side=tk.LEFT, padx=(0, 4))
		ttk.Button(btns, text="Delete Selected", command=self.__delete_selected).pack(side=tk.LEFT, padx=4)

		self.__listbox.bind("<<ListboxSelect>>", lambda _e: self.__use_selected())

	def __add_or_update(self):
		value = self.__entry_var.get().strip()
		if not value:
			return

		idx = self.__listbox.curselection()
		if idx:
			self.__listbox.delete(idx[0])
			self.__listbox.insert(idx[0], value)
			self.__listbox.selection_set(idx[0])
		else:
			self.__listbox.insert(tk.END, value)

		self.__entry_var.set("")

	def __delete_selected(self):
		idx = self.__listbox.curselection()
		if idx:
			self.__listbox.delete(idx[0])

	def __use_selected(self):
		idx = self.__listbox.curselection()
		if not idx:
			return

		value = self.__listbox.get(idx[0])
		self.__entry_var.set(value)

	def set_items(self, items: list[str]):
		self.__listbox.delete(0, tk.END)
		for item in items:
			self.__listbox.insert(tk.END, str(item))
		self.__entry_var.set("")

	def get_items(self) -> list[str]:
		out = []
		for item in self.__listbox.get(0, tk.END):
			text = str(item).strip()
			if text:
				out.append(text)
		return out


class SettingsPresetsGUI:
	def __init__(self, root, data_path: str | None = None, own_window: bool = True):
		self.__root = root
		self.__own_window = own_window

		if data_path is None:
			data_path = os.path.join(os.environ["EUVL_PATH"], "datasets")

		self.__presets = SettingsPresets(data_path)
		self.__status_var = tk.StringVar(value="Ready.")

		if self.__own_window and hasattr(root, "title"):
			root.title("Settings Presets")
		if self.__own_window and hasattr(root, "geometry"):
			root.geometry("980x430")
		if self.__own_window and hasattr(root, "minsize"):
			root.minsize(760, 360)

		self.__build()
		self.__load_all()

		if self.__own_window and hasattr(root, "protocol"):
			root.protocol("WM_DELETE_WINDOW", self.close)

	def __build(self):
		outer = ttk.Frame(self.__root, padding=8)
		outer.pack(fill=tk.BOTH, expand=True)

		top = ttk.Frame(outer)
		top.pack(fill=tk.X, pady=(0, 6))

		ttk.Button(top, text="Reload", command=self.__load_all).pack(side=tk.LEFT, padx=(0, 4))
		ttk.Button(top, text="Save", command=self.__save_all).pack(side=tk.LEFT, padx=4)

		self.__sample_types = PresetListFrame(outer, "Sample Types")
		self.__sample_types.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))

		self.__zr_filters = PresetListFrame(outer, "ZR Filters")
		self.__zr_filters.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)

		self.__operators = PresetListFrame(outer, "Operators")
		self.__operators.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))

		ttk.Label(
			self.__root,
			textvariable=self.__status_var,
			anchor=tk.W,
			relief=tk.SUNKEN,
			padding=(4, 2),
		).pack(side=tk.BOTTOM, fill=tk.X)

	def __load_all(self):
		try:
			sample_types = self.__presets.read_sample_types()
			zr_filters = self.__presets.read_zr_filters()
			operators = self.__presets.read_operators()

			self.__sample_types.set_items(sample_types)
			self.__zr_filters.set_items(zr_filters)
			self.__operators.set_items(operators)

			self.__status_var.set(
				f"Loaded presets: {len(sample_types)} sample type(s), "
				f"{len(zr_filters)} ZR filter(s), {len(operators)} operator(s)."
			)
		except Exception as e:
			self.__status_var.set("Failed loading presets.")
			messagebox.showerror("Settings Presets", f"Failed to load presets:\n{e}")

	def __save_all(self):
		try:
			sample_types = self.__sample_types.get_items()
			zr_filters = self.__zr_filters.get_items()
			operators = self.__operators.get_items()

			self.__presets.save_sample_types(sample_types)
			self.__presets.save_zr_filters(zr_filters)
			self.__presets.save_operators(operators)

			self.__status_var.set(
				f"Saved presets: {len(sample_types)} sample type(s), "
				f"{len(zr_filters)} ZR filter(s), {len(operators)} operator(s)."
			)
			messagebox.showinfo("Settings Presets", "Presets saved.")
		except Exception as e:
			self.__status_var.set("Failed saving presets.")
			messagebox.showerror("Settings Presets", f"Failed to save presets:\n{e}")

	def close(self):
		try:
			self.__presets.close()
		finally:
			if self.__own_window and hasattr(self.__root, "destroy"):
				self.__root.destroy()


def main():
	root = tk.Tk()
	SettingsPresetsGUI(root)
	root.mainloop()


if __name__ == "__main__":
	main()
