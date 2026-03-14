import tkinter as tk
from tkinter import ttk, messagebox
import os
import time
import threading
from queue import Queue, Empty

from ipi_ecs.core import daemon
from ipi_ecs.subsystems.experiment_controller import ExperimentReader, RunRecord
from chamber_ctl.subsystems.oscilloscope import DataReader, calculate_dose_of_experiment


def _fmt_timestamp(ts) -> str:
    if ts is None:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return str(ts)


class ExperimentReaderThread:
    """Thread-safe wrapper around ExperimentReader via a queue pair."""

    def __init__(self, data_path: str, exp_name: str):
        self.__data_path = data_path
        print(f"ExperimentReaderThread: data_path={data_path}, exp_name={exp_name}")
        self.__exp_name = exp_name
        self.__reader: ExperimentReader = None
        self.__in_queue: Queue = Queue()
        self.__daemon = daemon.Daemon()
        self.__daemon.add(self.__lib_thread)
        self.__daemon.start()

    def __lib_thread(self, stop_flag: daemon.StopFlag):
        self.__reader = ExperimentReader(self.__data_path, self.__exp_name)
        while stop_flag.run():
            try:
                fn, result_queue = self.__in_queue.get(timeout=0.1)
                try:
                    result_queue.put(("ok", fn()))
                except Exception as e:
                    result_queue.put(("err", e))
            except Empty:
                pass

    def __enqueue(self, fn) -> Queue:
        rq: Queue = Queue()
        self.__in_queue.put((fn, rq))
        return rq

    def __enqueue_sync(self, fn):
        status, result = self.__enqueue(fn).get()
        if status == "err":
            raise result

    def query_async(self, query: dict) -> Queue:
        q_copy = dict(query)
        q_tags = q_copy.pop("tags", {})
        return self.__enqueue(lambda: self.__reader.list_runs(q_tags=q_tags, q_args=q_copy))

    def list_all_async(self) -> Queue:
        return self.__enqueue(lambda: self.__reader.list_runs())

    def set_name(self, record: RunRecord, name: str):
        self.__enqueue_sync(lambda: record.set_name(name))

    def set_description(self, record: RunRecord, description: str):
        self.__enqueue_sync(lambda: record.set_description(description))

    def add_tag(self, record: RunRecord, key: str, value):
        self.__enqueue_sync(lambda: record.add_tag(key, value))

    def ok(self) -> bool:
        return self.__daemon.is_ok()

    def close(self):
        self.__daemon.stop()


class QueryFrame(ttk.LabelFrame):
    def __init__(self, parent, on_search, on_list_all):
        super().__init__(parent, text="Query / Filter", padding=8)
        self.__on_search = on_search
        self.__on_list_all = on_list_all
        self.__build()

    def __build(self):
        r = 0
        ttk.Label(self, text="Name:").grid(row=r, column=0, sticky=tk.W, padx=2, pady=2)
        self.__name_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.__name_var, width=18).grid(row=r, column=1, sticky=tk.EW, padx=2, pady=2)

        ttk.Label(self, text="Date (YYYY-MM-DD):").grid(row=r, column=2, sticky=tk.W, padx=(10, 2), pady=2)
        self.__date_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.__date_var, width=12).grid(row=r, column=3, sticky=tk.EW, padx=2, pady=2)
        ttk.Button(self, text="Today", command=self.__set_today).grid(row=r, column=4, padx=2, pady=2)

        r += 1
        ttk.Label(self, text="Min Dose:").grid(row=r, column=0, sticky=tk.W, padx=2, pady=2)
        self.__min_dose_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.__min_dose_var, width=10).grid(row=r, column=1, sticky=tk.EW, padx=2, pady=2)

        ttk.Label(self, text="Min Target Dose:").grid(row=r, column=2, sticky=tk.W, padx=(10, 2), pady=2)
        self.__min_target_dose_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.__min_target_dose_var, width=10).grid(row=r, column=3, sticky=tk.EW, padx=2, pady=2)

        r += 1
        ttk.Label(self, text="Min Time (s):").grid(row=r, column=0, sticky=tk.W, padx=2, pady=2)
        self.__min_time_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.__min_time_var, width=10).grid(row=r, column=1, sticky=tk.EW, padx=2, pady=2)

        ttk.Label(self, text="Max Time (s):").grid(row=r, column=2, sticky=tk.W, padx=(10, 2), pady=2)
        self.__max_time_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.__max_time_var, width=10).grid(row=r, column=3, sticky=tk.EW, padx=2, pady=2)

        r += 1
        ttk.Label(self, text="ZR:").grid(row=r, column=0, sticky=tk.W, padx=2, pady=2)
        self.__zr_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.__zr_var, width=12).grid(row=r, column=1, sticky=tk.EW, padx=2, pady=2)

        ttk.Label(self, text="Sample:").grid(row=r, column=2, sticky=tk.W, padx=(10, 2), pady=2)
        self.__sample_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.__sample_var, width=12).grid(row=r, column=3, sticky=tk.EW, padx=2, pady=2)

        r += 1
        ttk.Label(self, text="Operator:").grid(row=r, column=0, sticky=tk.W, padx=2, pady=2)
        self.__operator_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.__operator_var, width=12).grid(row=r, column=1, sticky=tk.EW, padx=2, pady=2)

        r += 1
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=r, column=0, columnspan=5, sticky=tk.W, pady=(6, 2))
        ttk.Button(btn_frame, text="Search", command=self.__do_search).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="List All", command=self.__on_list_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="Clear", command=self.__clear_fields).pack(side=tk.LEFT, padx=2)

        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)

    def __set_today(self):
        self.__date_var.set(time.strftime("%Y-%m-%d"))

    def __clear_fields(self):
        for var in (self.__name_var, self.__date_var, self.__min_dose_var,
                    self.__min_target_dose_var, self.__min_time_var, self.__max_time_var,
                    self.__zr_var, self.__sample_var, self.__operator_var):
            var.set("")

    def __do_search(self):
        query = {}

        name = self.__name_var.get().strip()
        if name:
            query["name"] = name

        date = self.__date_var.get().strip()
        if date:
            try:
                ts_min = time.mktime(time.strptime(date, "%Y-%m-%d"))
                query["created_min"] = ts_min
                query["created_max"] = ts_min + 86400.0
            except ValueError:
                messagebox.showerror("Invalid Input", f"Invalid date: '{date}'. Use YYYY-MM-DD.")
                return

        tag_filters = {}

        min_dose = self.__min_dose_var.get().strip()
        if min_dose:
            try:
                tag_filters["dose"] = {"min": float(min_dose), "max": 1e9}
            except ValueError:
                messagebox.showerror("Invalid Input", f"Invalid dose value: '{min_dose}'.")
                return

        min_target_dose = self.__min_target_dose_var.get().strip()
        if min_target_dose:
            try:
                tag_filters["target_dose"] = {"min": float(min_target_dose), "max": 1e9}
            except ValueError:
                messagebox.showerror("Invalid Input", f"Invalid target dose: '{min_target_dose}'.")
                return

        time_range = {}
        min_time = self.__min_time_var.get().strip()
        if min_time:
            try:
                time_range["min"] = float(min_time)
            except ValueError:
                messagebox.showerror("Invalid Input", f"Invalid min time: '{min_time}'.")
                return

        max_time = self.__max_time_var.get().strip()
        if max_time:
            try:
                time_range["max"] = float(max_time)
            except ValueError:
                messagebox.showerror("Invalid Input", f"Invalid max time: '{max_time}'.")
                return

        if time_range:
            tag_filters["state_time"] = time_range

        zr = self.__zr_var.get().strip()
        if zr:
            tag_filters["zr"] = zr

        sample = self.__sample_var.get().strip()
        if sample:
            tag_filters["sample"] = sample

        operator = self.__operator_var.get().strip()
        if operator:
            tag_filters["operator"] = operator

        if tag_filters:
            query["tags"] = tag_filters

        self.__on_search(query)


class ResultsFrame(ttk.LabelFrame):
    _COLS = ("name", "description", "date", "uuid", "dose", "status")

    def __init__(self, parent, on_selection_changed):
        super().__init__(parent, text="Results", padding=5)
        self.__on_selection_changed = on_selection_changed
        self.__records: dict = {}
        self.__build()

    def __build(self):
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL)

        self.__tree = ttk.Treeview(
            tree_frame,
            columns=self._COLS,
            show="headings",
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
            selectmode="browse",
        )
        vsb.config(command=self.__tree.yview)
        hsb.config(command=self.__tree.xview)

        self.__tree.heading("name", text="Name")
        self.__tree.heading("description", text="Description")
        self.__tree.heading("date", text="Created")
        self.__tree.heading("uuid", text="UUID")
        self.__tree.heading("dose", text="Dose")
        self.__tree.heading("status", text="Status")

        self.__tree.column("name", width=140, minwidth=70)
        self.__tree.column("description", width=200, minwidth=80)
        self.__tree.column("date", width=150, minwidth=100)
        self.__tree.column("uuid", width=80, minwidth=60)
        self.__tree.column("dose", width=110, minwidth=80)
        self.__tree.column("status", width=80, minwidth=60)

        self.__tree.grid(row=0, column=0, sticky=tk.NSEW)
        vsb.grid(row=0, column=1, sticky=tk.NS)
        hsb.grid(row=1, column=0, sticky=tk.EW)

        self.__tree.bind("<<TreeviewSelect>>", self.__on_select)

        self.__count_label = ttk.Label(self, text="No results.")
        self.__count_label.pack(side=tk.BOTTOM, anchor=tk.W, pady=(4, 0))

    def populate(self, records: list):
        self.__tree.delete(*self.__tree.get_children())
        self.__records.clear()

        for record in records:
            meta = record.get_metadata() or {}
            end_meta = record.get_end_metadata()
            date_str = _fmt_timestamp(meta.get("created_at"))
            uuid_str = str(record.get_state().get_uuid())[-8:]
            status = (end_meta or {}).get("status", "Active")
            dose = self.__dose_display(record)
            name = record.get_name() or ""
            desc = record.get_description() or ""
            if len(desc) > 60:
                desc = desc[:57] + "..."
            iid = self.__tree.insert("", tk.END, values=(name, desc, date_str, uuid_str, dose, status))
            self.__records[iid] = record

        n = len(records)
        self.__count_label.config(text=f"{n} result{'s' if n != 1 else ''}.")

    def refresh_record(self, record: RunRecord):
        for iid, rec in self.__records.items():
            if rec is record:
                meta = record.get_metadata() or {}
                end_meta = record.get_end_metadata()
                date_str = _fmt_timestamp(meta.get("created_at"))
                uuid_str = str(record.get_state().get_uuid())[-8:]
                status = (end_meta or {}).get("status", "Active")
                dose = self.__dose_display(record)
                name = record.get_name() or ""
                desc = record.get_description() or ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                self.__tree.item(iid, values=(name, desc, date_str, uuid_str, dose, status))
                break

    @staticmethod
    def __dose_display(record: RunRecord) -> str:
        tags = record.get_tags() or {}
        dose = tags.get("dose")
        if dose is None:
            return "None"
        try:
            return f"{float(dose):.6g}"
        except (TypeError, ValueError):
            return str(dose)

    def get_records(self) -> list:
        records = []
        for iid in self.__tree.get_children():
            rec = self.__records.get(iid)
            if rec is not None:
                records.append(rec)
        return records

    def get_selected_record(self):
        sel = self.__tree.selection()
        if not sel:
            return None
        return self.__records.get(sel[0])

    def __on_select(self, _event):
        sel = self.__tree.selection()
        record = self.__records.get(sel[0]) if sel else None
        self.__on_selection_changed(record)


class DetailFrame(ttk.LabelFrame):
    def __init__(self, parent, reader: ExperimentReaderThread, on_saved):
        super().__init__(parent, text="Experiment Detail", padding=8)
        self.__reader = reader
        self.__on_saved = on_saved
        self.__record: RunRecord = None
        self.__build()

    def __build(self):
        edit_frame = ttk.Frame(self)
        edit_frame.pack(fill=tk.X, pady=(0, 4))
        edit_frame.columnconfigure(1, weight=1)

        ttk.Label(edit_frame, text="Name:").grid(row=0, column=0, sticky=tk.W, padx=2, pady=2)
        self.__name_var = tk.StringVar()
        self.__name_entry = ttk.Entry(edit_frame, textvariable=self.__name_var, state=tk.DISABLED)
        self.__name_entry.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)

        ttk.Label(edit_frame, text="Description:").grid(row=1, column=0, sticky=tk.W, padx=2, pady=2)
        self.__desc_var = tk.StringVar()
        self.__desc_entry = ttk.Entry(edit_frame, textvariable=self.__desc_var, state=tk.DISABLED)
        self.__desc_entry.grid(row=1, column=1, sticky=tk.EW, padx=2, pady=2)

        self.__save_btn = ttk.Button(edit_frame, text="Save", command=self.__do_save, state=tk.DISABLED)
        self.__save_btn.grid(row=2, column=0, columnspan=2, pady=5)

        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 4))

        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(container, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.__scroll_frame = ttk.Frame(canvas)
        frame_id = canvas.create_window((0, 0), window=self.__scroll_frame, anchor=tk.NW)

        self.__scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(frame_id, width=e.width))

        self.__settings_lf = ttk.LabelFrame(self.__scroll_frame, text="Settings", padding=5)
        self.__settings_lf.pack(fill=tk.X, padx=2, pady=4)
        self.__settings_lf.columnconfigure(0, minsize=120)
        self.__settings_lf.columnconfigure(1, weight=1)

        self.__tags_lf = ttk.LabelFrame(self.__scroll_frame, text="Tags", padding=5)
        self.__tags_lf.pack(fill=tk.X, padx=2, pady=4)
        self.__tags_lf.columnconfigure(0, minsize=120)
        self.__tags_lf.columnconfigure(1, weight=1)

        self.__tags_tree = ttk.Treeview(
            self.__tags_lf,
            columns=("key", "value"),
            show="headings",
            height=8,
            selectmode="browse",
        )
        self.__tags_tree.heading("key", text="Key")
        self.__tags_tree.heading("value", text="Value")
        self.__tags_tree.column("key", width=140, minwidth=80)
        self.__tags_tree.column("value", width=200, minwidth=80)
        self.__tags_tree.grid(row=0, column=0, columnspan=2, sticky=tk.NSEW, padx=2, pady=(0, 4))
        self.__tags_lf.rowconfigure(0, weight=1)

        self.__tag_buttons = ttk.Frame(self.__tags_lf)
        self.__tag_buttons.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(0, 2))
        ttk.Button(self.__tag_buttons, text="Add Tag", command=self.__add_tag_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(self.__tag_buttons, text="Edit Selected", command=self.__edit_selected_tag).pack(side=tk.LEFT, padx=2)
        self.__tags_tree.bind("<Double-1>", lambda _e: self.__edit_selected_tag())

        self.__meta_lf = ttk.LabelFrame(self.__scroll_frame, text="Metadata", padding=5)
        self.__meta_lf.pack(fill=tk.X, padx=2, pady=4)
        self.__meta_lf.columnconfigure(0, minsize=120)
        self.__meta_lf.columnconfigure(1, weight=1)

        self.__placeholder = ttk.Label(
            self, text="Select an experiment to view details.",
            font=("Arial", 11), foreground="gray"
        )
        self.__placeholder.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    @staticmethod
    def __clear_frame(frame: ttk.Frame):
        for child in frame.winfo_children():
            child.destroy()

    @staticmethod
    def __populate_kv_frame(frame: ttk.Frame, items: dict, empty_text="(none)"):
        DetailFrame.__clear_frame(frame)
        if not items:
            ttk.Label(frame, text=empty_text, foreground="gray").grid(row=0, column=0, sticky=tk.W)
            return
        for i, (k, v) in enumerate(items.items()):
            ttk.Label(frame, text=f"{k}:", font=("TkDefaultFont", 9, "bold")).grid(
                row=i, column=0, sticky=tk.W, padx=2, pady=1)
            ttk.Label(frame, text=str(v)).grid(row=i, column=1, sticky=tk.W, padx=2, pady=1)

    def load_record(self, record: RunRecord):
        self.__record = record

        if record is None:
            self.__placeholder.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
            self.__name_entry.config(state=tk.DISABLED)
            self.__desc_entry.config(state=tk.DISABLED)
            self.__save_btn.config(state=tk.DISABLED)
            self.__clear_frame(self.__settings_lf)
            self.__tags_tree.delete(*self.__tags_tree.get_children())
            self.__clear_frame(self.__meta_lf)
            return

        self.__placeholder.place_forget()
        self.__name_entry.config(state=tk.NORMAL)
        self.__desc_entry.config(state=tk.NORMAL)
        self.__save_btn.config(state=tk.NORMAL)

        self.__name_var.set(record.get_name() or "")
        self.__desc_var.set(record.get_description() or "")

        state = record.get_state()
        config = state.get_settings().get_dict() if state else {}
        self.__populate_kv_frame(self.__settings_lf, config)

        self.__tags_tree.delete(*self.__tags_tree.get_children())
        tags = record.get_tags() or {}
        for k, v in tags.items():
            self.__tags_tree.insert("", tk.END, values=(str(k), str(v)))

        meta = record.get_metadata() or {}
        end_meta = record.get_end_metadata()
        meta_items = {"Created": _fmt_timestamp(meta.get("created_at"))}
        if end_meta:
            meta_items["Status"] = end_meta.get("status", "?")
            meta_items["Ended"] = _fmt_timestamp(end_meta.get("end_time"))
            meta_items["Reason"] = end_meta.get("end_reason", "")
        else:
            meta_items["Status"] = "Active"
        meta_items["Version"] = meta.get("version", "?")
        self.__populate_kv_frame(self.__meta_lf, meta_items)

    def is_showing_record(self, record: RunRecord) -> bool:
        return self.__record is record

    def __do_save(self):
        if self.__record is None:
            return
        name = self.__name_var.get().strip()
        desc = self.__desc_var.get().strip()
        try:
            self.__reader.set_name(self.__record, name)
            self.__reader.set_description(self.__record, desc)
            self.__on_saved(self.__record)
        except Exception as e:
            messagebox.showerror("Save Failed", str(e))

    def __add_tag_dialog(self):
        self.__tag_dialog(title="Add Or Update Tag")

    def __edit_selected_tag(self):
        if self.__record is None:
            return
        sel = self.__tags_tree.selection()
        if not sel:
            messagebox.showinfo("Edit Tag", "Select a tag to edit first.")
            return
        key, value = self.__tags_tree.item(sel[0], "values")
        self.__tag_dialog(title="Edit Tag", key=key, value=value)

    def __tag_dialog(self, title: str, key: str = "", value: str = ""):
        if self.__record is None:
            return

        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Key:").grid(row=0, column=0, sticky=tk.W, padx=2, pady=4)
        key_entry = ttk.Entry(frame, width=22)
        key_entry.grid(row=0, column=1, padx=2, pady=4)
        key_entry.insert(0, key)

        ttk.Label(frame, text="Value:").grid(row=1, column=0, sticky=tk.W, padx=2, pady=4)
        value_entry = ttk.Entry(frame, width=22)
        value_entry.grid(row=1, column=1, padx=2, pady=4)
        value_entry.insert(0, value)

        def _on_ok():
            key = key_entry.get().strip()
            value_str = value_entry.get().strip()
            if not key:
                messagebox.showerror("Invalid Input", "Key cannot be empty.", parent=dialog)
                return
            try:
                val = int(value_str)
            except ValueError:
                try:
                    val = float(value_str)
                except ValueError:
                    val = value_str
            try:
                self.__reader.add_tag(self.__record, key, val)
            except Exception as e:
                messagebox.showerror("Error", str(e), parent=dialog)
                return
            dialog.destroy()
            self.load_record(self.__record)
            self.__on_saved(self.__record)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(btn_frame, text="OK", command=_on_ok).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

        key_entry.focus_set()
        dialog.bind("<Return>", lambda e: _on_ok())


class ExperimentsGUI:
    def __init__(self, root: tk.Tk, data_path: str, exp_name: str):
        self.root = root
        root.title("Experiments Browser")
        root.geometry("1200x700")
        root.minsize(800, 500)

        self.__data_path = data_path
        self.__reader = ExperimentReaderThread(data_path, exp_name)
        self.__pending_query: Queue = None
        self.__dose_recalc_queue: Queue = Queue()
        self.__dose_recalc_cancel = threading.Event()
        self.__dose_recalc_worker = None

        self.__dose_dialog = None
        self.__dose_progressbar = None
        self.__dose_progress_var = tk.DoubleVar(value=0.0)
        self.__dose_status_var = tk.StringVar(value="")
        self.__dose_info_var = tk.StringVar(value="")
        self.__dose_cancel_btn = None

        self.__loading_dialog = None
        self.__loading_progressbar = None

        self.__status_var = tk.StringVar(value="Ready.")
        ttk.Label(root, textvariable=self.__status_var, anchor=tk.W, relief=tk.SUNKEN, padding=(4, 2)).pack(
            side=tk.BOTTOM, fill=tk.X)

        paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        left = ttk.Frame(paned)
        paned.add(left, weight=2)

        self.__query_frame = QueryFrame(left, on_search=self.__on_search, on_list_all=self.__on_list_all)
        self.__query_frame.pack(fill=tk.X, padx=2, pady=(2, 4))

        self.__actions_frame = ttk.Frame(left)
        self.__actions_frame.pack(fill=tk.X, padx=2, pady=(0, 4))
        ttk.Button(
            self.__actions_frame,
            text="Recalculate Selected Dose",
            command=self.__on_recalculate_selected_dose,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            self.__actions_frame,
            text="Recalculate Doses (Listed)",
            command=self.__on_recalculate_listed_doses,
        ).pack(side=tk.LEFT, padx=2)

        self.__results_frame = ResultsFrame(left, on_selection_changed=self.__on_selection_changed)
        self.__results_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        right = ttk.Frame(paned)
        paned.add(right, weight=3)

        self.__detail_frame = DetailFrame(right, reader=self.__reader, on_saved=self.__on_record_saved)
        self.__detail_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.__updater()

    def __on_search(self, query: dict):
        self.__pending_query = self.__reader.query_async(query)
        self.__show_loading_dialog("Searching experiments...")
        self.__status_var.set("Searching...")

    def __on_list_all(self):
        self.__pending_query = self.__reader.list_all_async()
        self.__show_loading_dialog("Loading all experiments...")
        self.__status_var.set("Loading all experiments...")

    def __show_loading_dialog(self, message: str):
        if self.__loading_dialog is not None and self.__loading_dialog.winfo_exists():
            self.__hide_loading_dialog()

        dialog = tk.Toplevel(self.root)
        dialog.title("Please Wait")
        dialog.geometry("320x90")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text=message).pack(anchor=tk.W, pady=(0, 6))
        self.__loading_progressbar = ttk.Progressbar(frame, mode="indeterminate")
        self.__loading_progressbar.pack(fill=tk.X)
        self.__loading_progressbar.start(10)

        dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        self.__loading_dialog = dialog

    def __hide_loading_dialog(self):
        if self.__loading_progressbar is not None:
            self.__loading_progressbar.stop()
        if self.__loading_dialog is not None and self.__loading_dialog.winfo_exists():
            self.__loading_dialog.grab_release()
            self.__loading_dialog.destroy()
        self.__loading_dialog = None
        self.__loading_progressbar = None

    def __on_selection_changed(self, record: RunRecord):
        self.__detail_frame.load_record(record)

    def __on_record_saved(self, record: RunRecord):
        self.__results_frame.refresh_record(record)

    def __on_recalculate_listed_doses(self):
        records = self.__results_frame.get_records()
        if not records:
            messagebox.showinfo("Recalculate Doses", "No listed experiments to recalculate.")
            return

        self.__start_dose_recalc(records)

    def __on_recalculate_selected_dose(self):
        record = self.__results_frame.get_selected_record()
        if record is None:
            messagebox.showinfo("Recalculate Dose", "Select an experiment from the list first.")
            return

        self.__start_dose_recalc([record])

    def __start_dose_recalc(self, records: list[RunRecord]):

        if self.__dose_recalc_worker is not None and self.__dose_recalc_worker.is_alive():
            messagebox.showinfo("Recalculate Doses", "Dose recalculation is already running.")
            return

        self.__open_dose_progress_dialog(len(records))

        self.__dose_recalc_cancel.clear()
        self.__dose_recalc_worker = threading.Thread(
            target=self.__dose_recalc_thread,
            args=(records,),
            daemon=True,
        )
        self.__dose_recalc_worker.start()

    def __open_dose_progress_dialog(self, total_count: int):
        if self.__dose_dialog is not None and self.__dose_dialog.winfo_exists():
            self.__dose_dialog.destroy()

        dialog = tk.Toplevel(self.root)
        dialog.title("Recalculate Doses")
        dialog.geometry("520x170")
        dialog.resizable(False, False)
        dialog.transient(self.root)

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        self.__dose_progress_var.set(0.0)
        self.__dose_status_var.set(f"Calculated: 0 | Remaining: {total_count}")
        self.__dose_info_var.set("Starting...")

        ttk.Label(frame, textvariable=self.__dose_status_var).pack(anchor=tk.W, pady=(0, 6))
        self.__dose_progressbar = ttk.Progressbar(
            frame,
            mode="determinate",
            maximum=max(total_count, 1),
            variable=self.__dose_progress_var,
        )
        self.__dose_progressbar.pack(fill=tk.X)
        ttk.Label(frame, textvariable=self.__dose_info_var).pack(anchor=tk.W, pady=(8, 0))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        self.__dose_cancel_btn = ttk.Button(btn_frame, text="Cancel", command=self.__cancel_dose_recalc)
        self.__dose_cancel_btn.pack(side=tk.RIGHT)

        dialog.protocol("WM_DELETE_WINDOW", self.__cancel_dose_recalc)
        self.__dose_dialog = dialog

    def __cancel_dose_recalc(self):
        self.__dose_recalc_cancel.set()
        self.__dose_status_var.set("Cancel requested...")
        if self.__dose_cancel_btn is not None and self.__dose_cancel_btn.winfo_exists():
            self.__dose_cancel_btn.config(state=tk.DISABLED)

    def __dose_recalc_thread(self, records: list[RunRecord]):
        total = len(records)
        processed = 0
        errors = 0

        # Keep database access thread-affine: instantiate readers in this worker thread.
        data_reader = DataReader(self.__data_path)

        for i, record in enumerate(records, start=1):
            if self.__dose_recalc_cancel.is_set():
                break

            run_uuid = record.get_state().get_uuid()
            try:
                dose, runtime = calculate_dose_of_experiment(run_uuid, data_reader)
                self.__reader.add_tag(record, "dose", float(dose))
                self.__reader.add_tag(record, "runtime", float(runtime))
                processed += 1
                self.__dose_recalc_queue.put(("progress", i, total, record, float(dose), float(runtime)))
            except Exception as e:
                errors += 1
                self.__dose_recalc_queue.put(("error", i, total, record, str(e)))

        canceled = self.__dose_recalc_cancel.is_set()
        self.__dose_recalc_queue.put(("done", processed, total, errors, canceled))

    def __updater(self):
        if self.__pending_query is not None:
            try:
                status, result = self.__pending_query.get_nowait()
                self.__pending_query = None
                self.__hide_loading_dialog()
                if status == "ok":
                    self.__results_frame.populate(result)
                    n = len(result)
                    self.__status_var.set(f"Found {n} result{'s' if n != 1 else ''}.")
                else:
                    self.__status_var.set(f"Error: {result}")
                    messagebox.showerror("Query Error", str(result))
            except Empty:
                pass

        try:
            while True:
                msg = self.__dose_recalc_queue.get_nowait()
                m_type = msg[0]

                if m_type == "progress":
                    _t, done, total, record, dose, runtime = msg
                    self.__dose_progress_var.set(done)
                    self.__dose_status_var.set(f"Calculated: {done} | Remaining: {max(total - done, 0)}")
                    self.__dose_info_var.set(
                        f"...{str(record.get_state().get_uuid())[-8:]}  dose={dose:.6g} mJ/cm2  runtime={runtime:.6g} s"
                    )
                    self.__results_frame.refresh_record(record)
                    if self.__detail_frame.is_showing_record(record):
                        self.__detail_frame.load_record(record)

                elif m_type == "error":
                    _t, done, total, record, err = msg
                    self.__dose_progress_var.set(done)
                    self.__dose_status_var.set(f"Calculated: {done} | Remaining: {max(total - done, 0)}")
                    self.__dose_info_var.set(f"Failed ...{str(record.get_state().get_uuid())[-8:]}: {err}")

                elif m_type == "done":
                    _t, processed, total, errors, canceled = msg
                    remaining = max(total - processed, 0)
                    if canceled:
                        self.__status_var.set(
                            f"Dose recalculation canceled. Completed {processed}, remaining {remaining}, errors {errors}."
                        )
                        self.__dose_status_var.set(
                            f"Canceled. Calculated: {processed} | Remaining: {remaining} | Errors: {errors}"
                        )
                    else:
                        self.__status_var.set(f"Dose recalculation done. Calculated {processed}/{total}, errors {errors}.")
                        self.__dose_status_var.set(
                            f"Done. Calculated: {processed} | Remaining: {remaining} | Errors: {errors}"
                        )

                    if self.__dose_cancel_btn is not None and self.__dose_cancel_btn.winfo_exists():
                        self.__dose_cancel_btn.config(text="Close", state=tk.NORMAL)
                        self.__dose_cancel_btn.configure(command=self.__close_dose_dialog)
        except Empty:
            pass

        self.root.after(200, self.__updater)

    def __close_dose_dialog(self):
        if self.__dose_dialog is not None and self.__dose_dialog.winfo_exists():
            self.__dose_dialog.destroy()
        self.__dose_dialog = None

    def on_close(self):
        self.__hide_loading_dialog()
        self.__reader.close()
        self.root.destroy()


if __name__ == "__main__":
    _DATA_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
    root = tk.Tk()
    app = ExperimentsGUI(root, _DATA_PATH, "exposure")
    root.mainloop()
