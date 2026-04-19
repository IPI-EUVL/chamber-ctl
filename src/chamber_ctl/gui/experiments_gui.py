import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import os
import time
import threading
from queue import Queue, Empty
import plotly.graph_objects as go

from ipi_ecs.core import daemon
from ipi_ecs.subsystems.experiment_controller import ExperimentReader, RunRecord
from chamber_ctl.subsystems.oscilloscope import DataReader, calculate_dose_of_experiment, calculate_doses_of_segments
from chamber_ctl.subsystems.development_metrics import DevelopmentMetrics
from ipi_ecs.util.export_experiment import export_experiment_data


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
            tag_filters["zr_filter"] = zr

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
    _COLS = (
        "name",
        "description",
        "sample",
        "target_dose",
        "date",
        "uuid",
        "dose",
        "runtime",
        "effective_dose_rate",
        "avg_exposed_thickness",
        "avg_blank_thickness",
        "status",
    )

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
            selectmode="extended",
        )
        vsb.config(command=self.__tree.yview)
        hsb.config(command=self.__tree.xview)

        self.__tree.heading("name", text="Name")
        self.__tree.heading("description", text="Description")
        self.__tree.heading("sample", text="Sample")
        self.__tree.heading("target_dose", text="Target Dose")
        self.__tree.heading("date", text="Created")
        self.__tree.heading("uuid", text="UUID")
        self.__tree.heading("dose", text="Dose")
        self.__tree.heading("runtime", text="Runtime")
        self.__tree.heading("effective_dose_rate", text="Eff. Dose Rate")
        self.__tree.heading("avg_exposed_thickness", text="Avg Exposed (nm)")
        self.__tree.heading("avg_blank_thickness", text="Avg Blank (nm)")
        self.__tree.heading("status", text="Status")

        self.__tree.column("name", width=140, minwidth=70)
        self.__tree.column("description", width=200, minwidth=80)
        self.__tree.column("sample", width=130, minwidth=80)
        self.__tree.column("target_dose", width=110, minwidth=80)
        self.__tree.column("date", width=150, minwidth=100)
        self.__tree.column("uuid", width=80, minwidth=60)
        self.__tree.column("dose", width=110, minwidth=80)
        self.__tree.column("runtime", width=110, minwidth=80)
        self.__tree.column("effective_dose_rate", width=130, minwidth=90)
        self.__tree.column("avg_exposed_thickness", width=130, minwidth=90)
        self.__tree.column("avg_blank_thickness", width=130, minwidth=90)
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
            runtime = self.__runtime_display(record)
            sample = self.__sample_display(record)
            target_dose = self.__target_dose_display(record)
            effective_dose_rate = self.__effective_dose_rate_display(record)
            avg_exposed_thickness = self.__avg_exposed_thickness_display(record)
            avg_blank_thickness = self.__avg_blank_thickness_display(record)
            name = record.get_name() or ""
            desc = record.get_description() or ""
            if len(desc) > 60:
                desc = desc[:57] + "..."
            iid = self.__tree.insert(
                "",
                tk.END,
                values=(
                    name,
                    desc,
                    sample,
                    target_dose,
                    date_str,
                    uuid_str,
                    dose,
                    runtime,
                    effective_dose_rate,
                    avg_exposed_thickness,
                    avg_blank_thickness,
                    status,
                ),
            )
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
                runtime = self.__runtime_display(record)
                sample = self.__sample_display(record)
                target_dose = self.__target_dose_display(record)
                effective_dose_rate = self.__effective_dose_rate_display(record)
                avg_exposed_thickness = self.__avg_exposed_thickness_display(record)
                avg_blank_thickness = self.__avg_blank_thickness_display(record)
                name = record.get_name() or ""
                desc = record.get_description() or ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                self.__tree.item(
                    iid,
                    values=(
                        name,
                        desc,
                        sample,
                        target_dose,
                        date_str,
                        uuid_str,
                        dose,
                        runtime,
                        effective_dose_rate,
                        avg_exposed_thickness,
                        avg_blank_thickness,
                        status,
                    ),
                )
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

    @staticmethod
    def __runtime_display(record: RunRecord) -> str:
        tags = record.get_tags() or {}
        runtime = tags.get("runtime")
        if runtime is None:
            return "None"
        try:
            return f"{float(runtime):.6g}"
        except (TypeError, ValueError):
            return str(runtime)

    @staticmethod
    def __sample_display(record: RunRecord) -> str:
        tags = record.get_tags() or {}
        sample = tags.get("sample")
        if sample is None:
            return "None"
        return str(sample)

    @staticmethod
    def __target_dose_display(record: RunRecord) -> str:
        tags = record.get_tags() or {}
        target_dose = tags.get("target_dose")
        if target_dose is None:
            return "None"
        try:
            return f"{float(target_dose):.6g}"
        except (TypeError, ValueError):
            return str(target_dose)

    @staticmethod
    def __effective_dose_rate_display(record: RunRecord) -> str:
        tags = record.get_tags() or {}
        dose = tags.get("dose")
        runtime = tags.get("runtime")
        if dose is None or runtime is None:
            return "None"
        try:
            runtime_f = float(runtime)
            if runtime_f == 0.0:
                return "None"
            return f"{float(dose) / runtime_f:.6g}"
        except (TypeError, ValueError):
            return "None"

    @staticmethod
    def __avg_exposed_thickness_display(record: RunRecord) -> str:
        tags = record.get_tags() or {}
        return ResultsFrame.__format_numeric_tag(tags.get("avg_exposed_area_thickness_nm"))

    @staticmethod
    def __avg_blank_thickness_display(record: RunRecord) -> str:
        tags = record.get_tags() or {}
        return ResultsFrame.__format_numeric_tag(tags.get("avg_blank_area_thickness_nm"))

    @staticmethod
    def __format_numeric_tag(value) -> str:
        if value is None:
            return "None"
        try:
            return f"{float(value):.6g}"
        except (TypeError, ValueError):
            return str(value)

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

    def get_selected_records(self) -> list:
        selected = []
        for iid in self.__tree.selection():
            rec = self.__records.get(iid)
            if rec is not None:
                selected.append(rec)
        return selected

    def __on_select(self, _event):
        sel = self.__tree.selection()
        record = self.__records.get(sel[0]) if sel else None
        self.__on_selection_changed(record)


class DetailFrame(ttk.LabelFrame):
    def __init__(self, parent, reader: ExperimentReaderThread, on_saved, on_read_metrics, on_save_metrics):
        super().__init__(parent, text="Experiment Detail", padding=8)
        self.__reader = reader
        self.__on_saved = on_saved
        self.__on_read_metrics = on_read_metrics
        self.__on_save_metrics = on_save_metrics
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

        btn_frame = ttk.Frame(edit_frame)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=5)
        self.__save_btn = ttk.Button(btn_frame, text="Save", command=self.__do_save, state=tk.DISABLED)
        self.__save_btn.pack(side=tk.LEFT, padx=3)
        self.__copy_id_btn = ttk.Button(
            btn_frame,
            text="Copy Experiment ID",
            command=self.__copy_experiment_id,
            state=tk.DISABLED,
        )
        self.__copy_id_btn.pack(side=tk.LEFT, padx=3)

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

        self.__metrics_lf = ttk.LabelFrame(self.__scroll_frame, text="Development Metrics", padding=5)
        self.__metrics_lf.pack(fill=tk.X, padx=2, pady=4)
        self.__metrics_lf.columnconfigure(1, weight=1)

        ttk.Label(self.__metrics_lf, text="Exposed Thickness (nm):").grid(row=0, column=0, sticky=tk.W, padx=2, pady=2)
        self.__exposed_var = tk.StringVar()
        self.__exposed_entry = ttk.Entry(self.__metrics_lf, textvariable=self.__exposed_var)
        self.__exposed_entry.grid(row=0, column=1, sticky=tk.EW, padx=2, pady=2)

        ttk.Label(self.__metrics_lf, text="Blank Thickness (nm):").grid(row=1, column=0, sticky=tk.W, padx=2, pady=2)
        self.__blank_var = tk.StringVar()
        self.__blank_entry = ttk.Entry(self.__metrics_lf, textvariable=self.__blank_var)
        self.__blank_entry.grid(row=1, column=1, sticky=tk.EW, padx=2, pady=2)

        ttk.Label(self.__metrics_lf, text="Goodness Of Fit:").grid(row=2, column=0, sticky=tk.W, padx=2, pady=2)
        self.__gof_var = tk.StringVar()
        self.__gof_entry = ttk.Entry(self.__metrics_lf, textvariable=self.__gof_var)
        self.__gof_entry.grid(row=2, column=1, sticky=tk.EW, padx=2, pady=2)

        ttk.Label(self.__metrics_lf, text="Comma-separated floats.", foreground="gray").grid(
            row=3, column=0, columnspan=2, sticky=tk.W, padx=2, pady=(0, 4)
        )

        self.__metrics_btn_frame = ttk.Frame(self.__metrics_lf)
        self.__metrics_btn_frame.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(2, 0))
        self.__read_metrics_btn = ttk.Button(
            self.__metrics_btn_frame,
            text="Read Metrics",
            command=self.__read_metrics,
            state=tk.DISABLED,
        )
        self.__read_metrics_btn.pack(side=tk.LEFT, padx=2)
        self.__save_metrics_btn = ttk.Button(
            self.__metrics_btn_frame,
            text="Save Metrics",
            command=self.__save_metrics,
            state=tk.DISABLED,
        )
        self.__save_metrics_btn.pack(side=tk.LEFT, padx=2)

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
            self.__copy_id_btn.config(state=tk.DISABLED)
            self.__set_metrics_state(False)
            self.__clear_metrics_fields()
            self.__clear_frame(self.__settings_lf)
            self.__tags_tree.delete(*self.__tags_tree.get_children())
            self.__clear_frame(self.__meta_lf)
            return

        self.__placeholder.place_forget()
        self.__name_entry.config(state=tk.NORMAL)
        self.__desc_entry.config(state=tk.NORMAL)
        self.__save_btn.config(state=tk.NORMAL)
        self.__copy_id_btn.config(state=tk.NORMAL)
        self.__set_metrics_state(True)

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

    def set_metrics_values(self, data: dict):
        exposed = data.get("exposed_area_thickness_nm", [])
        blank = data.get("blank_area_thickness_nm", [])
        gof = data.get("goodness_of_fit", [])

        self.__exposed_var.set(self.__format_float_list(exposed))
        self.__blank_var.set(self.__format_float_list(blank))
        self.__gof_var.set(self.__format_float_list(gof))

    def __clear_metrics_fields(self):
        self.__exposed_var.set("")
        self.__blank_var.set("")
        self.__gof_var.set("")

    def __set_metrics_state(self, enabled: bool):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.__exposed_entry.config(state=state)
        self.__blank_entry.config(state=state)
        self.__gof_entry.config(state=state)
        self.__read_metrics_btn.config(state=state)
        self.__save_metrics_btn.config(state=state)

    @staticmethod
    def __format_float_list(values) -> str:
        out = []
        for value in values or []:
            try:
                out.append(f"{float(value):.10g}")
            except (TypeError, ValueError):
                out.append(str(value))
        return ", ".join(out)

    @staticmethod
    def __parse_float_list(text: str, field_name: str) -> list[float]:
        stripped = text.strip()
        if not stripped:
            return []

        values = []
        for raw in stripped.split(","):
            token = raw.strip()
            if not token:
                continue
            try:
                values.append(float(token))
            except ValueError:
                raise ValueError(f"Invalid float in {field_name}: '{token}'")
        return values

    def __read_metrics(self):
        if self.__record is None:
            return
        self.__on_read_metrics(self.__record)

    def __save_metrics(self):
        if self.__record is None:
            return

        try:
            exposed = self.__parse_float_list(self.__exposed_var.get(), "Exposed Thickness")
            blank = self.__parse_float_list(self.__blank_var.get(), "Blank Thickness")
            gof = self.__parse_float_list(self.__gof_var.get(), "Goodness Of Fit")
        except ValueError as e:
            messagebox.showerror("Invalid Metrics Input", str(e))
            return

        expected_gof_count = len(exposed) + len(blank)
        if len(gof) != expected_gof_count:
            messagebox.showerror(
                "Invalid Metrics Input",
                (
                    "Goodness-of-fit list must contain exposed values first, followed by blank values. "
                    f"Expected {expected_gof_count} total GoF values "
                    f"({len(exposed)} exposed + {len(blank)} blank), got {len(gof)}."
                ),
            )
            return

        self.__on_save_metrics(self.__record, exposed, blank, gof)

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

    def __copy_experiment_id(self):
        if self.__record is None:
            return
        run_uuid = self.__record.get_state().get_uuid()
        uuid_text = str(run_uuid)
        self.clipboard_clear()
        self.clipboard_append(uuid_text)
        self.update_idletasks()

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
    def __init__(self, root, data_path: str, exp_name: str, own_window: bool = True):
        self.root = root
        self.__own_window = own_window
        if self.__own_window and hasattr(root, "title"):
            root.title("Experiments Browser")
        if self.__own_window and hasattr(root, "geometry"):
            root.geometry("1200x700")
        if self.__own_window and hasattr(root, "minsize"):
            root.minsize(800, 500)

        self.__data_path = data_path
        self.__exp_name = exp_name
        self.__reader = ExperimentReaderThread(data_path, exp_name)
        self.__dev_metrics = DevelopmentMetrics(self.__data_path)
        self.__pending_query: Queue = None
        self.__dose_recalc_queue: Queue = Queue()
        self.__dose_recalc_cancel = threading.Event()
        self.__dose_recalc_worker = None

        self.__metrics_queue: Queue = Queue()
        self.__metrics_read_worker = None
        self.__metrics_save_worker = None

        self.__dose_dialog = None
        self.__dose_progressbar = None
        self.__dose_progress_var = tk.DoubleVar(value=0.0)
        self.__dose_status_var = tk.StringVar(value="")
        self.__dose_info_var = tk.StringVar(value="")
        self.__dose_cancel_btn = None

        self.__export_queue: Queue = Queue()
        self.__export_worker = None

        self.__plot_queue: Queue = Queue()
        self.__plot_worker = None

        self.__loading_dialog = None
        self.__loading_progressbar = None
        self.__loading_progress_var = tk.DoubleVar(value=0.0)

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
            text="Export Experiments To...",
            command=self.__on_export_selected_experiments,
        ).pack(side=tk.LEFT, padx=2)
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
        ttk.Button(
            self.__actions_frame,
            text="Combined Dose/Runtime (Selected)",
            command=self.__on_combined_selected_dose_runtime,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            self.__actions_frame,
            text="Plot Dose Graph (Selected)",
            command=self.__on_plot_selected_dose_graph,
        ).pack(side=tk.LEFT, padx=2)

        self.__results_frame = ResultsFrame(left, on_selection_changed=self.__on_selection_changed)
        self.__results_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        right = ttk.Frame(paned)
        paned.add(right, weight=3)

        self.__detail_frame = DetailFrame(
            right,
            reader=self.__reader,
            on_saved=self.__on_record_saved,
            on_read_metrics=self.__on_read_metrics,
            on_save_metrics=self.__on_save_metrics,
        )
        self.__detail_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        if self.__own_window and hasattr(root, "protocol"):
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

    def __show_loading_dialog(self, message: str, determinate: bool = False, maximum: int = 1):
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
        mode = "determinate" if determinate else "indeterminate"
        self.__loading_progress_var.set(0.0)
        self.__loading_progressbar = ttk.Progressbar(
            frame,
            mode=mode,
            variable=self.__loading_progress_var,
            maximum=max(maximum, 1),
        )
        self.__loading_progressbar.pack(fill=tk.X)
        if not determinate:
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
        self.__loading_progress_var.set(0.0)

    def __on_selection_changed(self, record: RunRecord):
        self.__detail_frame.load_record(record)
        if record is None:
            return

        # Clear stale values immediately, then try loading metrics for the new selection.
        self.__detail_frame.set_metrics_values({})
        self.__on_read_metrics(record, notify_if_busy=False)

    def __on_record_saved(self, record: RunRecord):
        self.__results_frame.refresh_record(record)

    def __on_read_metrics(self, record: RunRecord, notify_if_busy: bool = True):
        if self.__metrics_read_worker is not None and self.__metrics_read_worker.is_alive():
            if notify_if_busy:
                messagebox.showinfo("Read Metrics", "A metrics read operation is already in progress.")
            return

        run_uuid = record.get_state().get_uuid()
        self.__status_var.set(f"Reading development metrics ...{str(run_uuid)[-8:]}")

        self.__metrics_read_worker = threading.Thread(
            target=self.__read_metrics_thread,
            args=(record,),
            daemon=True,
        )
        self.__metrics_read_worker.start()

    def __read_metrics_thread(self, record: RunRecord):
        run_uuid = record.get_state().get_uuid()
        try:
            data = self.__dev_metrics.read_ellipsometry_data(run_uuid)
            self.__metrics_queue.put(("read_ok", record, data))
        except Exception as e:
            self.__metrics_queue.put(("read_err", record, e))

    @staticmethod
    def __is_missing_metrics_file_error(err: Exception) -> bool:
        if isinstance(err, FileNotFoundError):
            return True

        msg = str(err).lower()
        has_missing_hint = ("no such file" in msg) or ("not found" in msg) or ("does not exist" in msg)
        mentions_metrics_file = "ellipsometry.json" in msg
        return has_missing_hint and mentions_metrics_file

    def __on_save_metrics(self, record: RunRecord, exposed: list[float], blank: list[float], gof: list[float]):
        if self.__metrics_save_worker is not None and self.__metrics_save_worker.is_alive():
            messagebox.showinfo("Save Metrics", "A metrics save operation is already in progress.")
            return

        run_uuid = record.get_state().get_uuid()
        self.__status_var.set(f"Saving development metrics ...{str(run_uuid)[-8:]}")

        self.__metrics_save_worker = threading.Thread(
            target=self.__save_metrics_thread,
            args=(record, exposed, blank, gof),
            daemon=True,
        )
        self.__metrics_save_worker.start()

    def __save_metrics_thread(self, record: RunRecord, exposed: list[float], blank: list[float], gof: list[float]):
        run_uuid = record.get_state().get_uuid()
        try:
            self.__dev_metrics.save_ellipsometry_data(run_uuid, exposed, blank, gof)
            self.__metrics_queue.put(("save_ok", record))
        except Exception as e:
            self.__metrics_queue.put(("save_err", record, str(e)))

    def __on_export_selected_experiments(self):
        records = self.__results_frame.get_selected_records()
        if not records:
            messagebox.showinfo("Export Experiments", "Select one or more experiments from the list first.")
            return

        if self.__export_worker is not None and self.__export_worker.is_alive():
            messagebox.showinfo("Export Experiments", "An export is already in progress.")
            return

        target_dir = filedialog.askdirectory(title="Export Experiments To")
        if not target_dir:
            return

        overwrite = messagebox.askyesno(
            "Overwrite Existing?",
            "Overwrite existing experiment folders if they already exist?",
        )

        run_ids = [record.get_state().get_uuid() for record in records]
        self.__show_loading_dialog(
            f"Exporting {len(run_ids)} experiment(s)...",
            determinate=True,
            maximum=len(run_ids),
        )
        self.__status_var.set(f"Exporting {len(run_ids)} experiment(s)...")

        self.__export_worker = threading.Thread(
            target=self.__export_selected_thread,
            args=(run_ids, target_dir, overwrite),
            daemon=True,
        )
        self.__export_worker.start()

    def __export_selected_thread(self, run_ids: list, target_dir: str, overwrite: bool):
        reader = ExperimentReader(self.__data_path, self.__exp_name)
        errors = []
        total = len(run_ids)

        for idx, run_uuid in enumerate(run_ids, start=1):
            try:
                export_experiment_data(run_uuid, target_dir, reader, overwrite)
                self.__export_queue.put(("progress", idx, total, run_uuid))
            except Exception as e:
                errors.append((run_uuid, str(e)))
                self.__export_queue.put(("error", idx, total, run_uuid, str(e)))

        self.__export_queue.put(("done", total, errors))

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

    def __on_combined_selected_dose_runtime(self):
        records = self.__results_frame.get_selected_records()
        if not records:
            messagebox.showinfo("Combined Dose/Runtime", "Select one or more experiments from the list first.")
            return

        dose_total = 0.0
        runtime_total = 0.0
        dose_count = 0
        runtime_count = 0
        invalid_dose = 0
        invalid_runtime = 0

        for record in records:
            tags = record.get_tags() or {}

            dose_val = tags.get("dose")
            if dose_val is not None:
                try:
                    dose_total += float(dose_val)
                    dose_count += 1
                except (TypeError, ValueError):
                    invalid_dose += 1

            runtime_val = tags.get("runtime")
            if runtime_val is not None:
                try:
                    runtime_total += float(runtime_val)
                    runtime_count += 1
                except (TypeError, ValueError):
                    invalid_runtime += 1

        selected_count = len(records)
        self.__status_var.set(
            f"Combined selected ({selected_count}): dose={dose_total:.6g} mJ/cm2, runtime={runtime_total:.6g} s"
        )

        message = (
            f"Selected experiments: {selected_count}\n"
            f"\n"
            f"Total dose: {dose_total:.6g} mJ/cm2 (from {dose_count} tag(s))\n"
            f"Total runtime: {runtime_total:.6g} s (from {runtime_count} tag(s))"
        )
        if invalid_dose or invalid_runtime:
            message += (
                f"\n\nIgnored invalid values — dose: {invalid_dose}, runtime: {invalid_runtime}."
            )

        messagebox.showinfo("Combined Dose/Runtime", message)

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

    def __on_plot_selected_dose_graph(self):
        records = self.__results_frame.get_selected_records()
        if not records:
            messagebox.showinfo("Plot Dose Graph", "Select one or more experiments from the list first.")
            return

        if self.__plot_worker is not None and self.__plot_worker.is_alive():
            messagebox.showinfo("Plot Dose Graph", "A graph is already being prepared.")
            return

        run_ids = [record.get_state().get_uuid() for record in records]
        self.__show_loading_dialog(
            f"Preparing dose graph for {len(run_ids)} experiment(s)...",
            determinate=True,
            maximum=len(run_ids),
        )
        self.__status_var.set(f"Preparing dose graph for {len(run_ids)} experiment(s)...")

        self.__plot_worker = threading.Thread(
            target=self.__plot_selected_thread,
            args=(run_ids,),
            daemon=True,
        )
        self.__plot_worker.start()

    def __plot_selected_thread(self, run_ids: list):
        data_reader = DataReader(self.__data_path)
        exp_reader = ExperimentReader(self.__data_path, self.__exp_name)
        traces = []
        errors = []
        total = len(run_ids)

        for idx, run_uuid in enumerate(run_ids, start=1):
            try:
                run = exp_reader.locate_run_by_uuid(run_uuid)
                name = f"{run.get_name()}:{run.get_description()}"

                doses, times = calculate_doses_of_segments(run_uuid, data_reader)
                abs_doses = []
                abs_times = []
                running_total = 0.0
                running_time = 0.0

                for dose, run_time in zip(doses, times):
                    running_total += float(dose)
                    running_time += float(run_time)
                    abs_doses.append(running_total)
                    abs_times.append(running_time)

                traces.append((name, abs_times, abs_doses))
                self.__plot_queue.put(("progress", idx, total, run_uuid))
            except Exception as e:
                errors.append((run_uuid, str(e)))
                self.__plot_queue.put(("error", idx, total, run_uuid, str(e)))

        self.__plot_queue.put(("done", traces, errors, total))

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

        try:
            while True:
                msg = self.__export_queue.get_nowait()
                m_type = msg[0]

                if m_type == "progress":
                    _t, done, total, run_uuid = msg
                    self.__loading_progress_var.set(done)
                    self.__status_var.set(f"Exported {done}/{total} experiments...")

                elif m_type == "error":
                    _t, done, total, run_uuid, err = msg
                    self.__loading_progress_var.set(done)
                    self.__status_var.set(f"Export {done}/{total}: failed ...{str(run_uuid)[-8:]} ({err})")

                elif m_type == "done":
                    _t, total, errors = msg
                    self.__hide_loading_dialog()
                    if errors:
                        self.__status_var.set(f"Export complete with errors: {total - len(errors)}/{total} succeeded.")
                        first_uuid, first_err = errors[0]
                        messagebox.showwarning(
                            "Export Complete",
                            f"Exported {total - len(errors)}/{total} experiments. "
                            f"First error (...{str(first_uuid)[-8:]}): {first_err}",
                        )
                    else:
                        self.__status_var.set(f"Export complete: {total}/{total} succeeded.")
                        messagebox.showinfo("Export Complete", f"Exported {total} experiment(s).")
        except Empty:
            pass

        try:
            while True:
                msg = self.__plot_queue.get_nowait()
                m_type = msg[0]

                if m_type == "progress":
                    _t, done, total, run_uuid = msg
                    self.__loading_progress_var.set(done)
                    self.__status_var.set(f"Preparing graph {done}/{total} (...{str(run_uuid)[-8:]})")

                elif m_type == "error":
                    _t, done, total, run_uuid, err = msg
                    self.__loading_progress_var.set(done)
                    self.__status_var.set(f"Graph {done}/{total}: failed ...{str(run_uuid)[-8:]} ({err})")

                elif m_type == "done":
                    _t, traces, errors, total = msg
                    self.__hide_loading_dialog()

                    if traces:
                        fig = go.Figure()
                        for name, times, doses in traces:
                            fig.add_trace(go.Scatter(x=times, y=doses, mode="lines", name=name))
                        fig.update_layout(
                            title="Dose vs Time",
                            xaxis_title="Time (s)",
                            yaxis_title="Dose (mJ/cm2)",
                        )
                        fig.show()

                    if errors:
                        success = len(traces)
                        self.__status_var.set(
                            f"Graph ready with errors: {success}/{total} experiments plotted."
                        )
                        first_uuid, first_err = errors[0]
                        messagebox.showwarning(
                            "Plot Dose Graph",
                            f"Plotted {success}/{total} experiment(s). "
                            f"First error (...{str(first_uuid)[-8:]}): {first_err}",
                        )
                    else:
                        self.__status_var.set(f"Graph ready: {len(traces)}/{total} experiments plotted.")

        except Empty:
            pass

        try:
            while True:
                msg = self.__metrics_queue.get_nowait()
                m_type = msg[0]

                if m_type == "read_ok":
                    _t, record, data = msg
                    if self.__detail_frame.is_showing_record(record):
                        self.__detail_frame.set_metrics_values(data)
                    self.__status_var.set(f"Development metrics loaded for ...{str(record.get_state().get_uuid())[-8:]}")

                elif m_type == "read_err":
                    _t, record, err = msg
                    if self.__is_missing_metrics_file_error(err):
                        if self.__detail_frame.is_showing_record(record):
                            self.__detail_frame.set_metrics_values({})
                        self.__status_var.set(f"No development metrics for ...{str(record.get_state().get_uuid())[-8:]}")
                    else:
                        self.__status_var.set(f"Read metrics failed for ...{str(record.get_state().get_uuid())[-8:]}")
                        messagebox.showerror("Read Metrics Failed", str(err))

                elif m_type == "save_ok":
                    _t, record = msg
                    self.__status_var.set(f"Development metrics saved for ...{str(record.get_state().get_uuid())[-8:]}")
                    if self.__detail_frame.is_showing_record(record):
                        self.__detail_frame.load_record(record)
                    self.__results_frame.refresh_record(record)
                    messagebox.showinfo("Save Metrics", "Development metrics saved.")

                elif m_type == "save_err":
                    _t, record, err = msg
                    self.__status_var.set(f"Save metrics failed for ...{str(record.get_state().get_uuid())[-8:]}")
                    messagebox.showerror("Save Metrics Failed", err)

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
        self.__dev_metrics.close()
        if self.__own_window and hasattr(self.root, "destroy"):
            self.root.destroy()


if __name__ == "__main__":
    _DATA_PATH = os.path.join(os.environ["EUVL_PATH"], "datasets")
    root = tk.Tk()
    app = ExperimentsGUI(root, _DATA_PATH, "exposure")
    root.mainloop()
