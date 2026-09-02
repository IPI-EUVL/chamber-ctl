from __future__ import annotations

import time
import tkinter as tk
import uuid
from collections.abc import Mapping
from pathlib import Path
from tkinter import messagebox, ttk

from chamber_ctl.data.calibration import CalibrationProfile, CalibrationRepository


DEFAULT_ALGORITHM_VERSION = "dose-v1-native-integral"


def calibration_profile_from_fields(
    fields: Mapping[str, str],
    *,
    profile_id: uuid.UUID | None = None,
    created_at: float | None = None,
) -> CalibrationProfile:
    def text(name: str, label: str) -> str:
        value = fields.get(name, "")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required.")
        return value.strip()

    def number(name: str, label: str, *, default: float | None = None) -> float:
        raw = fields.get(name, "")
        if isinstance(raw, str) and not raw.strip() and default is not None:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a number.") from exc

    try:
        signal_polarity = int(fields.get("signal_polarity", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("Signal polarity must be -1 or 1.") from exc

    return CalibrationProfile(
        profile_id=uuid.uuid4() if profile_id is None else profile_id,
        revision=1,
        name=text("name", "Name"),
        created_at=time.time() if created_at is None else created_at,
        algorithm_version=text("algorithm_version", "Algorithm version"),
        signal_polarity=signal_polarity,
        load_resistance_ohms=number("load_resistance_ohms", "Load resistance"),
        photodiode_responsivity_a_per_w=number(
            "photodiode_responsivity_a_per_w",
            "Photodiode responsivity",
        ),
        illuminated_area_cm2=number("illuminated_area_cm2", "Illuminated area"),
        multiplicative_correction=number(
            "multiplicative_correction",
            "Multiplicative correction",
            default=1.0,
        ),
        additive_pulse_dose_mj_cm2=number(
            "additive_pulse_dose_mj_cm2",
            "Additive pulse dose",
            default=0.0,
        ),
        provenance=fields.get("provenance", "").strip(),
        notes=fields.get("notes", "").strip(),
    )


class CalibrationProfileDialog:
    def __init__(self, parent, data_path: str | Path) -> None:
        self.result: CalibrationProfile | None = None
        self._data_path = data_path

        owner = parent.winfo_toplevel()
        self.window = tk.Toplevel(owner)
        self.window.title("Create Calibration")
        self.window.transient(owner)
        self.window.resizable(True, False)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)

        body = ttk.Frame(self.window, padding=10)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        defaults = {
            "name": "",
            "algorithm_version": DEFAULT_ALGORITHM_VERSION,
            "signal_polarity": "",
            "load_resistance_ohms": "",
            "photodiode_responsivity_a_per_w": "",
            "illuminated_area_cm2": "",
            "multiplicative_correction": "1.0",
            "additive_pulse_dose_mj_cm2": "0.0",
            "provenance": "",
            "notes": "",
        }
        self._fields = {name: tk.StringVar(value=value) for name, value in defaults.items()}

        rows = (
            ("Name", "name", None),
            ("Algorithm version", "algorithm_version", None),
            ("Signal polarity", "signal_polarity", ("-1", "1")),
            ("Load resistance (ohms)", "load_resistance_ohms", None),
            ("Responsivity (A/W)", "photodiode_responsivity_a_per_w", None),
            ("Illuminated area (cm2)", "illuminated_area_cm2", None),
            ("Multiplicative correction", "multiplicative_correction", None),
            ("Additive pulse dose (mJ/cm2)", "additive_pulse_dose_mj_cm2", None),
            ("Provenance", "provenance", None),
            ("Notes", "notes", None),
        )
        for row, (label, name, options) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=3)
            if options is None:
                widget = ttk.Entry(body, textvariable=self._fields[name], width=48)
            else:
                widget = ttk.Combobox(
                    body,
                    textvariable=self._fields[name],
                    values=options,
                    state="readonly",
                    width=12,
                )
            widget.grid(row=row, column=1, sticky=tk.EW, pady=3)

        buttons = ttk.Frame(body)
        buttons.grid(row=len(rows), column=0, columnspan=2, sticky=tk.E, pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Create", command=self._create).pack(side=tk.LEFT, padx=(6, 0))

        self.window.grab_set()

    def show(self) -> CalibrationProfile | None:
        self.window.wait_window()
        return self.result

    def _create(self) -> None:
        try:
            profile = calibration_profile_from_fields(
                {name: value.get() for name, value in self._fields.items()}
            )
            repository = CalibrationRepository(self._data_path)
            try:
                self.result = repository.create(profile)
            finally:
                repository.close()
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("Create Calibration", str(exc), parent=self.window)
            return
        self.window.destroy()

    def _cancel(self) -> None:
        self.window.destroy()


def create_calibration_profile(parent, data_path: str | Path) -> CalibrationProfile | None:
    return CalibrationProfileDialog(parent, data_path).show()