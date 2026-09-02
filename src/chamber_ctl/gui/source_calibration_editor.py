from __future__ import annotations

import tkinter as tk
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from tkinter import messagebox, ttk

from chamber_ctl.data.calibration import (
    CalibrationProfile,
    SourceConfiguration,
    SourceCalibrationBinding,
    SourceKey,
    normalize_source_calibration_bindings,
)
from chamber_ctl.gui.calibration_editor import create_calibration_profile
from euv_acquisition.source_identity import RED_PITAYA_SOURCE_ID, RED_PITAYA_SOURCE_KIND


CalibrationOptions = Mapping[str, tuple[str, int]]
OTHER_SOURCE_OPTION = "Other source..."


def available_source_keys(
    bindings: object,
    discovered_sources: Iterable[SourceKey] = (),
) -> tuple[SourceKey, ...]:
    sources = {SourceKey(RED_PITAYA_SOURCE_KIND, RED_PITAYA_SOURCE_ID)}
    sources.update(binding.source_key for binding in normalize_source_calibration_bindings(bindings))
    for source in discovered_sources:
        if not isinstance(source, SourceKey):
            raise ValueError("Source options must be source keys.")
        sources.add(source)
    return tuple(sorted(sources))


def source_option_label(source: SourceKey) -> str:
    return f"{source.source_kind}/{source.source_id}"


def calibration_option(profile: CalibrationProfile) -> tuple[str, tuple[str, int]]:
    return (
        f"{profile.name} r{profile.revision} | {profile.profile_id}",
        (str(profile.profile_id), profile.revision),
    )


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


def source_calibration_summary(
    bindings: object,
    primary_source: SourceKey | None = None,
) -> str:
    normalized = normalize_source_calibration_bindings(bindings)
    if not normalized:
        return "None"
    return ", ".join(
        f"{'Primary: ' if binding.source_key == primary_source else ''}"
        f"{binding.source_kind}/{binding.source_id}: {binding.profile_id} r{binding.revision}"
        for binding in sorted(normalized, key=lambda item: item.source_key)
    )


class SourceCalibrationDialog:
    def __init__(
        self,
        parent,
        bindings: object,
        calibration_options: CalibrationOptions,
        primary_source: SourceKey | None = None,
        *,
        data_path: str | Path | None = None,
        source_options: Iterable[SourceKey] = (),
        on_calibration_created: Callable[[CalibrationProfile], None] | None = None,
    ) -> None:
        self.result: SourceConfiguration | None = None
        self._bindings = list(normalize_source_calibration_bindings(bindings))
        self._primary_source = primary_source
        self._calibration_options = dict(calibration_options)
        self._profile_labels = dict(self._calibration_options)
        self._data_path = data_path
        self._on_calibration_created = on_calibration_created
        sources = available_source_keys(self._bindings, source_options)
        self._source_keys_by_label = {source_option_label(source): source for source in sources}

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
            columns=("primary", "kind", "source", "calibration"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        for column, label, width in (
            ("primary", "Primary", 65),
            ("kind", "Source kind", 110),
            ("source", "Source ID", 180),
            ("calibration", "Calibration", 300),
        ):
            self._tree.heading(column, text=label)
            self._tree.column(column, width=width, anchor=tk.W)
        self._tree.grid(row=0, column=0, columnspan=4, sticky=tk.EW, pady=(0, 8))
        self._tree.bind("<<TreeviewSelect>>", self._load_selected)

        default_source = SourceKey(RED_PITAYA_SOURCE_KIND, RED_PITAYA_SOURCE_ID)
        self._source_choice = tk.StringVar(value=source_option_label(default_source))
        self._source_kind = tk.StringVar()
        self._source_id = tk.StringVar()
        self._profile = tk.StringVar()
        self._is_primary = tk.BooleanVar(value=primary_source is None)
        ttk.Label(body, text="Source").grid(row=1, column=0, sticky=tk.W, padx=(0, 6))
        self._source_combo = ttk.Combobox(
            body,
            textvariable=self._source_choice,
            values=(*self._source_keys_by_label, OTHER_SOURCE_OPTION),
            state="readonly",
        )
        self._source_combo.grid(row=1, column=1, columnspan=3, sticky=tk.EW)
        self._source_combo.bind("<<ComboboxSelected>>", self._source_changed)

        self._manual_source = ttk.Frame(body)
        self._manual_source.grid(row=2, column=1, columnspan=3, sticky=tk.EW, pady=(6, 0))
        self._manual_source.columnconfigure(1, weight=1)
        self._manual_source.columnconfigure(3, weight=1)
        ttk.Label(self._manual_source, text="Kind").grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        ttk.Entry(self._manual_source, textvariable=self._source_kind, width=16).grid(
            row=0,
            column=1,
            sticky=tk.EW,
            padx=(0, 10),
        )
        ttk.Label(self._manual_source, text="ID").grid(row=0, column=2, sticky=tk.W, padx=(0, 6))
        ttk.Entry(self._manual_source, textvariable=self._source_id, width=24).grid(
            row=0,
            column=3,
            sticky=tk.EW,
        )
        self._manual_source.grid_remove()

        ttk.Label(body, text="Calibration").grid(row=3, column=0, sticky=tk.W, pady=(6, 0), padx=(0, 6))
        self._profile_combo = ttk.Combobox(
            body,
            textvariable=self._profile,
            values=tuple(self._profile_labels),
            state="readonly",
        )
        self._profile_combo.grid(row=3, column=1, columnspan=2, sticky=tk.EW, pady=(6, 0))
        if self._data_path is not None:
            ttk.Button(body, text="New...", command=self._create_calibration).grid(
                row=3,
                column=3,
                sticky=tk.E,
                padx=(6, 0),
                pady=(6, 0),
            )
        ttk.Checkbutton(
            body,
            text="Use this source for default analysis",
            variable=self._is_primary,
        ).grid(row=4, column=1, columnspan=3, sticky=tk.W, pady=(6, 0))

        row_buttons = ttk.Frame(body)
        row_buttons.grid(row=5, column=0, columnspan=4, sticky=tk.EW, pady=(8, 0))
        ttk.Button(row_buttons, text="Add or update", command=self._upsert).pack(side=tk.LEFT)
        ttk.Button(row_buttons, text="Remove", command=self._remove).pack(side=tk.LEFT, padx=(6, 0))

        dialog_buttons = ttk.Frame(body)
        dialog_buttons.grid(row=6, column=0, columnspan=4, sticky=tk.E, pady=(12, 0))
        ttk.Button(dialog_buttons, text="Cancel", command=self._cancel).pack(side=tk.LEFT)
        ttk.Button(dialog_buttons, text="Save", command=self._save).pack(side=tk.LEFT, padx=(6, 0))

        self._refresh_tree()
        self.window.grab_set()

    def show(self) -> SourceConfiguration | None:
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
                values=(
                    "Yes" if binding.source_key == self._primary_source else "",
                    binding.source_kind,
                    binding.source_id,
                    self._label_for(binding),
                ),
            )

    def _load_selected(self, _event=None) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        values = self._tree.item(selection[0], "values")
        self._is_primary.set(values[0] == "Yes")
        self._select_source(SourceKey(values[1], values[2]))
        self._profile.set(values[3])

    def _select_source(self, source: SourceKey) -> None:
        label = source_option_label(source)
        self._source_keys_by_label[label] = source
        self._source_combo.config(values=(*self._source_keys_by_label, OTHER_SOURCE_OPTION))
        self._source_choice.set(label)
        self._manual_source.grid_remove()

    def _source_changed(self, _event=None) -> None:
        if self._source_choice.get() == OTHER_SOURCE_OPTION:
            self._manual_source.grid()
        else:
            self._manual_source.grid_remove()

    def _selected_source(self) -> SourceKey:
        if self._source_choice.get() == OTHER_SOURCE_OPTION:
            return SourceKey(self._source_kind.get().strip(), self._source_id.get().strip())
        source = self._source_keys_by_label.get(self._source_choice.get())
        if source is None:
            raise ValueError("Select a source.")
        return source

    def _create_calibration(self) -> None:
        if self._data_path is None:
            return
        profile = create_calibration_profile(self.window, self._data_path)
        if self.window.winfo_exists():
            self.window.grab_set()
        if profile is None:
            return
        label, value = calibration_option(profile)
        self._profile_labels[label] = value
        self._profile_combo.config(values=tuple(self._profile_labels))
        self._profile.set(label)
        if self._on_calibration_created is not None:
            self._on_calibration_created(profile)

    def _upsert(self) -> None:
        try:
            source = self._selected_source()
            binding = source_calibration_from_selection(
                source.source_kind,
                source.source_id,
                self._profile.get(),
                self._profile_labels,
            )
        except ValueError as exc:
            messagebox.showerror("Source Calibrations", str(exc), parent=self.window)
            return
        self._bindings = [item for item in self._bindings if item.source_key != binding.source_key]
        self._bindings.append(binding)
        if self._is_primary.get():
            self._primary_source = binding.source_key
        elif self._primary_source == binding.source_key:
            self._primary_source = None
        self._refresh_tree()

    def _remove(self) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        _primary, kind, source_id, _profile = self._tree.item(selection[0], "values")
        self._bindings = [
            binding
            for binding in self._bindings
            if (binding.source_kind, binding.source_id) != (kind, source_id)
        ]
        if self._primary_source == SourceKey(kind, source_id):
            self._primary_source = None
        self._refresh_tree()

    def _save(self) -> None:
        if self._primary_source is None:
            messagebox.showerror(
                "Source Calibrations",
                "Select one source for default analysis.",
                parent=self.window,
            )
            return
        try:
            self.result = SourceConfiguration(tuple(self._bindings), self._primary_source)
        except ValueError as exc:
            messagebox.showerror("Source Calibrations", str(exc), parent=self.window)
            return
        self.window.destroy()

    def _cancel(self) -> None:
        self.window.destroy()


def edit_source_calibrations(
    parent,
    bindings: object,
    calibration_options: CalibrationOptions,
    primary_source: SourceKey | None = None,
    *,
    data_path: str | Path | None = None,
    source_options: Iterable[SourceKey] = (),
    on_calibration_created: Callable[[CalibrationProfile], None] | None = None,
) -> SourceConfiguration | None:
    return SourceCalibrationDialog(
        parent,
        bindings,
        calibration_options,
        primary_source,
        data_path=data_path,
        source_options=source_options,
        on_calibration_created=on_calibration_created,
    ).show()
