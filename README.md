# IPI Chamber Control

Process-computer control software for the IPI EUV chamber. The package provides
the chamber subsystems, command-line tools, and the central Tk GUI.

## Acquisition Diagnostics

The central GUI's **Acquisition** tab displays bounded pulse-window previews
published by the EUV Acquisition Controller. It supports idle-only continuous
and one-shot diagnostic captures, manual flush and stop, and remote simulator
fault controls when the connected acquisition source advertises them.

The same operations are available through DDS from the command line:

```powershell
chamber-ctl acquisition test-start
chamber-ctl acquisition test-flush
chamber-ctl acquisition test-stop
chamber-ctl acquisition test-one-shot
chamber-ctl acquisition simulator-set pll_locked off
chamber-ctl acquisition simulator-restore
```

The chamber controller remains the sole Red Pitaya control client. HDF5
artifacts stay off DDS: experiment snapshots are persisted before their bounded
preview is published, while diagnostic snapshots are previewed, acknowledged,
purged, and deleted. Automatic cleanup is permitted only for sessions whose
persisted capture purpose is `diagnostic`.

Acquisition diagnostics require `ipi-euv-acquisition>=0.1.4` on the Red Pitaya
and process computer. The GUI does not start, stop, deploy, or otherwise manage
the acquisition service.

### Capture integrity

Capture Diagnostics includes a live five-second capture-integrity view. Samples
move from right to left and show capture rate, estimated lost captures per
second, and individual inferred gaps. The rolling window can be set to one, two,
or three seconds; two seconds is the default. Gap markers identify whether the
interval crossed an HDF5 snapshot boundary.

These values are timestamp-inferred estimates, not physical trigger-counter
measurements. The expected capture rate is one half of the configured chopper
frequency. Recorded trigger-disabled intervals split the analysis into separate
segments and are not counted as losses. The data model also accepts future
trigger ordinals and counter epochs so counter hardware can replace inference
without changing the surrounding UI contract.

DDS carries only bounded live telemetry, capped at 60 KiB. Dense failure windows
retain aggregate loss totals but may omit older on-screen gap markers; the GUI
reports the omission count. Full post-exposure cadence data is transported as
the HDF5 experiment resource rather than as a DDS blob. Cadence telemetry is
best effort and a publication failure does not stop an active capture.

Completed native exposures receive a strict `euv_capture_cadence.h5` resource.
Its interactive Plotly view supports zoom, pan, and the same one-, two-, and
three-second windows. Open it with **Open Last Exposure** in Capture Diagnostics
or **Capture Integrity (Selected)** for one selected row in Experiments.