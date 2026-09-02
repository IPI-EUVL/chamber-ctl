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

## Passive Siglent observer

The Siglent comparison path uses two process-computer sidecars. Start
`euv-acquisition-siglent` first to own the VISA instrument and ports
`11762`/`11763`, then start `chamber-siglent-recorder` before exposure PREINIT.
The recorder subscribes read-only to exposure and laser timing state, attaches
source-qualified artifacts to the run, and publishes display-only `cur_dose`
and `cur_time` values from the exact bound calibration revision. The exposure
GUI shows those values beside the Red Pitaya totals. Siglent sequence-mode
updates arrive after each completed hardware sequence rather than after every
trigger. Interlock and stop decisions remain owned by the Red Pitaya acquisition
controller even when another source supplies the default post-run dose graph.

Configure acquisition sources from the direct exposure or batch editor and mark
exactly one as **Primary**. New runs store the bindings and primary key in the
scalar, versioned JSON tags `source_calibrations` and `primary_source`.
Each binding selects an immutable profile revision for one exact `(source kind,
source ID)` pair. They are not exposure settings, and list order, process start
order, and DDS discovery order have no meaning.

The source editor lists connected acquisition sources advertised through DDS,
along with the deployed Red Pitaya and any source already bound in the current
plan. Use **Other source...** only for offline commissioning. Its calibration
dropdown includes every stored immutable revision; **New...** creates revision
one in the same dataset and selects it immediately.

The deployed Red Pitaya key is `red_pitaya/red-pitaya`. A Siglent recorder uses
`siglent/<--source-id value>`. Both publish `Configured source: kind/id` as an
informational subsystem status item; Siglent observer subsystem UUIDs are also
derived deterministically from that exact key. Two processes claiming the same
logical observer key therefore cannot become separate sources accidentally.

Every completed source keeps its own analysis products. The primary Red Pitaya
uses its native HDF5 analysis. A primary Siglent uses its source-qualified
`captured` product for default dose and graph tags while retaining the exact
`legacy_compensated` product for comparison. The scalar `active_dose_product`
tag records the exact source, algorithm, and analysis resource selected. Old
runs without these tags retain their historical Red Pitaya/default behavior.

The recorder uses a binding only when both its source kind and source ID match
the recorder's configured identity exactly, then loads the exact profile UUID
and revision from the calibration repository. A run tag takes precedence over
a historical embedded-settings binding and the recorder CLI fallback. Configure
the fallback for runs that omit a binding for this recorder:

```powershell
chamber-siglent-recorder `
	--source-id "SDS2HBAX900425" `
	--calibration-profile-id "<profile-uuid>" `
	--calibration-revision 1 `
	--capture-host 127.0.0.1 `
	--dds-host 127.0.0.1
```

The profile revision must already exist in the same experiment dataset used by
the chamber controller. If no matching run or historical binding and no fallback
are present, the recorder logs a warning and deliberately skips the exposure.

The legacy `DummyOscilloscope` and `euv-acquisition-sim` do not implement the
Siglent sequence-batch protocol. For a local observer rehearsal, run the
dedicated service alongside the authoritative simulator:

```powershell
euv-acquisition-siglent-sim `
	--spool C:\temp\euv-siglent-sim-spool

chamber-siglent-recorder `
	--source-id "siglent-simulator" `
	--calibration-profile-id "<profile-uuid>" `
	--calibration-revision 1 `
	--capture-host 127.0.0.1 `
	--dds-host 127.0.0.1
```

The simulator and physical Siglent service share ports `11762`/`11763`, so only
one may run at a time. Both expose the same recorder-facing contract.