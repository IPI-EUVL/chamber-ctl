from __future__ import annotations

import tkinter as tk
import uuid
from collections.abc import Mapping
from tkinter import messagebox, ttk

from chamber_ctl.data.calibration import (
    SourceCalibrationBinding,
    normalize_source_calibration_bindings,
)


CalibrationOptions = Mapping[str, tuple[str, int]]


def source_calibration_from_selection(
    source_kind: str,
    source_id: str,
    calibration_label: str,
    calibration_options: CalibrationOptions,
) -> SourceCalibrationBinding:
    calibration = calibration_options.get(calibration_label)
    if calibration is None:
        raise ValueError("Select a calibration profile.")
    profile_id, revision = calibration
    return SourceCalibrationBinding(
        source_kind.strip(),
        source_id.strip(),
        uuid.UUID(profile_id),
        revision,
    )


def source_calibration_summary(bindings: object) -> str:
    normalized = normalize_source_calibration_bindings(bindings)
    if not normalized:
        return "None"
    return ", ".join(
        f"{binding.source_kind}/{binding.source_id}: {binding.profile_id} r{binding.revision}"
        for binding in sorted(normalized, key=lambda item: item.source_key)
    )


class SourceCalibrationDialog:
    def __init__(
        self,
        parent,
        bindings: object,
        calibration_options: CalibrationOptions,
    ) -> None:
        self.result: tuple[SourceCalibrationBinding, ...] | None = None
        self._bindings = list(normalize_source_calibration_bindings(bindings))
        self._calibration_options = dict(calibration_options)
        self._profile_labels = dict(self._calibration_options)

        owner = parent.winfo_toplevel()
        self.window = tk.Toplevel(owner)
        self.window.title("Source Calibrations")
        self.window.transient(owner)
        self.window.resizable(True, False)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)

        body = ttk.Frame(self.window, padding=10)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        self._tree = ttk.Treeview(
            body,
            columns=("kind", "source", "calibration"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        for column, label, width in (
            ("kind", "Source kind", 110),
            ("source", "Source ID", 180),
            ("calibration", "Calibration", 300),
        ):
            self._tree.heading(column, text=label)
            self._tree.column(column, width=width, anchor=tk.W)
        self._tree.grid(row=0, column=0, columnspan=4, sticky=tk.EW, pady=(0, 8))
        self._tree.bind("<<TreeviewSelect>>", self._load_selected)

        self._source_kind = tk.StringVar(value="siglent")
        self._source_id = tk.StringVar()
        self._profile = tk.StringVar()
        ttk.Label(body, text="Source kind").grid(row=1, column=0, sticky=tk.W, padx=(0, 6))
        ttk.Entry(body, textvariable=self._source_kind, width=16).grid(row=1, column=1, sticky=tk.EW, padx=(0, 10))
        ttk.Label(body, text="Source ID").grid(row=1, column=2, sticky=tk.W, padx=(0, 6))
        ttk.Entry(body, textvariable=self._source_id, width=24).grid(row=1, column=3, sticky=tk.EW)
        ttk.Label(body, text="Calibration").grid(row=2, column=0, sticky=tk.W, pady=(6, 0), padx=(0, 6))
        self._profile_combo = ttk.Combobox(
            body,
            textvariable=self._profile,
            values=tuple(self._profile_labels),
            state="readonly",
        )
        self._profile_combo.grid(row=2, column=1, columnspan=3, sticky=tk.EW, pady=(6, 0))

        row_buttons = ttk.Frame(body)
        row_buttons.grid(row=3, column=0, columnspan=4, sticky=tk.EW, pady=(8, 0))
        ttk.Button(row_buttons, text="Add or update", command=self._upsert).pack(side=tk.LEFT)
        ttk.Button(row_buttons, text="Remove", command=self._remove).pack(side=tk.LEFT, padx=(6, 0))

        dialog_buttons = ttk.Frame(body)
        dialog_buttons.grid(row=4, column=0, columnspan=4, sticky=tk.E, pady=(12, 0))
        ttk.Button(dialog_buttons, text="Cancel", command=self._cancel).pack(side=tk.LEFT)
        ttk.Button(dialog_buttons, text="Save", command=self._save).pack(side=tk.LEFT, padx=(6, 0))

        self._refresh_tree()
        self.window.grab_set()

    def show(self) -> tuple[SourceCalibrationBinding, ...] | None:
        self.window.wait_window()
        return self.result

    def _label_for(self, binding: SourceCalibrationBinding) -> str:
        expected = (str(binding.profile_id), binding.revision)
        for label, value in self._profile_labels.items():
            if value == expected:
                return label
        label = f"Unavailable r{binding.revision} | {binding.profile_id}"
        self._profile_labels[label] = expected
        self._profile_combo.config(values=tuple(self._profile_labels))
        return label

    def _refresh_tree(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        for binding in sorted(self._bindings, key=lambda item: item.source_key):
            self._tree.insert(
                "",
                tk.END,
                values=(binding.source_kind, binding.source_id, self._label_for(binding)),
            )

    def _load_selected(self, _event=None) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        values = self._tree.item(selection[0], "values")
        self._source_kind.set(values[0])
        self._source_id.set(values[1])
        self._profile.set(values[2])

    def _upsert(self) -> None:
        try:
            binding = source_calibration_from_selection(
                self._source_kind.get(),
                self._source_id.get(),
                self._profile.get(),
                self._profile_labels,
            )
        except ValueError as exc:
            messagebox.showerror("Source Calibrations", str(exc), parent=self.window)
            return
        self._bindings = [item for item in self._bindings if item.source_key != binding.source_key]
        self._bindings.append(binding)
        self._refresh_tree()

    def _remove(self) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        kind, source_id, _profile = self._tree.item(selection[0], "values")
        self._bindings = [
            binding
            for binding in self._bindings
            if (binding.source_kind, binding.source_id) != (kind, source_id)
        ]
        self._refresh_tree()

    def _save(self) -> None:
        self.result = normalize_source_calibration_bindings(self._bindings)
        self.window.destroy()

    def _cancel(self) -> None:
        self.window.destroy()


def edit_source_calibrations(
    parent,
    bindings: object,
    calibration_options: CalibrationOptions,
) -> tuple[SourceCalibrationBinding, ...] | None:
    return SourceCalibrationDialog(parent, bindings, calibration_options).show()
