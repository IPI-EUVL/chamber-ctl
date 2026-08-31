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