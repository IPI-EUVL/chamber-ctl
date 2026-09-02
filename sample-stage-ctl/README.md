# Sample stage controller

`ipi-sample-stage-ctl` packages the Raspberry Pi sample-stage daemon as an
immutable, checksummed release. It preserves the existing GPIO assignments,
speed limits, PCF8574 limit input, and UDP wire contract while applying
trapezoidal acceleration to motor motion. The service listens on
`0.0.0.0:11755`; the chamber controller continues to use its existing `11756`
endpoint.

## Target

- Raspberry Pi 4 running Debian 13.1 aarch64
- `/usr/bin/python3` at CPython 3.13
- Service account `euvl`, with `gpio` and `i2c` group membership
- Read/write access to `/dev/gpiomem` and `/dev/i2c-1`
- Debian packages `python3-rpi-lgpio` and `python3-lgpio`
- Noninteractive sudo for deployment

The release does not replace the Debian GPIO bindings. All PyPI dependencies
are pinned, bundled, checksummed, installed with `--no-index --no-deps`, and
loaded from the release directory. Systemd supplies writable runtime storage
at `/run/sample-stage` for the `lgpio` notification pipe while release files
remain read-only.

## Development

From this directory:

```powershell
python -m pytest -q
python -m ruff check src tests
py -3.13 -m pip wheel --no-deps --wheel-dir dist .
```

Hardware imports occur only when the runtime is composed, so tests do not
initialize GPIO, I2C, motors, or the UDP listener.

## Motion profile

Both axes accelerate and decelerate symmetrically on every move. Long moves
reach the existing velocity limit and short moves use a triangular profile:

| Axis | Maximum velocity | Acceleration |
| --- | ---: | ---: |
| Rotation | 400 steps/s | 1,000 steps/s^2 |
| Linear | 20,000 steps/s | 20,000 steps/s^2 |

Rotation homing uses the same acceleration while respecting the requested
homing speed. When the Hall-effect switch triggers, the controller records the
crossing, decelerates past the nonmechanical sensor, and rebases the coast-down
so motion finishes at logical position zero. At the maximum rotation speed,
the configured stopping distance is 80 steps, or 18 degrees.

## Connection loss

The chamber-side stage client remains alive when UDP send or receive operations
fail. It publishes a `Sample stage offline` alarm with the connection detail,
uses safe startup values until the first valid status packet, and reconnects
automatically. Active operations fail promptly, and commands interrupted by a
connection loss are discarded rather than replayed after reconnect. Reissue a
motion command only after the offline alarm clears.

## One-time network setup

On the process computer, run Windows PowerShell as Administrator:

```powershell
.\scripts\setup_sample_stage_relay.ps1
```

The script refuses to run unless the host owns `10.194.210.21`, validates the
existing Pitaya relay at `2222 -> 10.11.13.50:22`, and adds only
`2223 -> 10.11.13.225:22` plus its inbound firewall rule. Remove only the
sample-stage mapping and rule with:

```powershell
.\scripts\setup_sample_stage_relay.ps1 -Remove
```

On the deployment workstation, create the dedicated key and SSH alias:

```powershell
.\scripts\setup_sample_stage_ssh.ps1
```

Authorize the printed public key for `euvl` on the Pi, then verify both key
authentication and noninteractive sudo:

```powershell
ssh -o BatchMode=yes euvl-sample-stage "sudo -n true"
```

## Rebuild the native wheelhouse

This is a maintainer-only regeneration procedure, not a normal deployment
step. The repository already contains the required checksummed CPython 3.13
aarch64 `sysv-ipc` wheel, so do not run this section before deploying the
current release.

Fetch the universal wheels and the verified `sysv-ipc` source archive on the
workstation:

```powershell
.\scripts\fetch_sample_stage_wheelhouse.ps1
```

Copy these two files to the Pi without replacing any system packages:

- `scripts/build_sysv_ipc_wheel.sh`
- `vendor/cp313-aarch64/sysv_ipc-1.1.0.tar.gz`

Build on the Pi with explicit input and output paths:

```bash
bash build_sysv_ipc_wheel.sh ./sysv_ipc-1.1.0.tar.gz .
```

Copy the emitted `sysv_ipc-1.1.0-cp313-*-linux_aarch64.whl` back into
`vendor/cp313-aarch64`, then rerun the fetcher. A complete wheelhouse contains
14 wheels and `SHA256SUMS`; deployment refuses to proceed without both.

## First migration

Service operations are user-controlled. Copilot must not run the commands in
this section. Before deleting the live legacy installation, preserve any
external backup required for recovery. Physically confirm that the stage is
idle, then run on the Pi:

```bash
sudo systemctl stop sample_stage.service
sudo systemctl disable sample_stage.service
sudo rm -f /etc/systemd/system/sample_stage.service
sudo systemctl daemon-reload
rm -rf /home/euvl/sample_stage_ctl
```

Confirm that no legacy process remains before deploying:

```bash
pgrep -af sample_stage_ctl && exit 1 || true
```

## Deploy

The default operation uploads, verifies, and installs an immutable release for
inspection. It does not change `/opt/sample-stage/current`, the installed
systemd unit, enablement, or process state:

```powershell
.\scripts\deploy_sample_stage.ps1
```

The release ID combines the application version with a deterministic hash of
every bundled wheel, the normalized lock file, the systemd unit, and the
wheelhouse manifest. A fixed source-date epoch makes unchanged application
wheels byte-identical. Existing release directories are never overwritten.
The installer checks bundle hashes, target architecture and Python, Debian GPIO
provenance, device permissions, imports, package versions, protocol constants,
and the systemd unit before reporting `STAGED_RELEASE` and `STAGED_PATH`.

Record the printed release ID and inspect that exact release without operating
the service:

```powershell
$releaseId = '<staged-release-id>'
ssh euvl-sample-stage "cat /opt/sample-stage/releases/$releaseId/RELEASE; cat /opt/sample-stage/releases/$releaseId/sample_stage.service"
```

After physically confirming that the stage is idle, run a fresh guarded
deployment and require it to match the inspected release:

```powershell
.\scripts\deploy_sample_stage.ps1 -ExpectedReleaseId $releaseId -RestartService -ConfirmStageIdle
```

This command atomically updates `current`, installs and enables the unit, and
restarts the service as one transaction. Activation must leave the unit active
and listening on UDP `11755` after the settling interval. On failure, the
deployer restores the previous unit, `current` link, enabled state, and active
state. A first-install failure restores the pre-command state with no selected
release or installed unit. Immutable release directories, including each
matching unit, remain for audit and later rollback. The deployer never deletes
a legacy installation from the Pi.

Review startup or failure logs with:

```powershell
ssh euvl-sample-stage "journalctl -u sample_stage.service -n 100 --no-pager"
```

## Manual rollback

Automatic rollback runs when guarded activation fails. To select an older
immutable release manually, first physically confirm that the stage is idle.
Then run these commands on the Pi, replacing `<release-id>` with a directory
listed under `/opt/sample-stage/releases`:

```bash
release_id='<release-id>'
release="/opt/sample-stage/releases/$release_id"
rollback_link="/opt/sample-stage/.current.manual.$$"
sudo test -f "$release/sample_stage.service"
sudo install -m 0644 "$release/sample_stage.service" /etc/systemd/system/sample_stage.service
sudo ln -s "$release" "$rollback_link"
sudo mv -Tf "$rollback_link" /opt/sample-stage/current
sudo systemctl daemon-reload
sudo systemctl restart sample_stage.service
sudo systemctl is-active sample_stage.service
```

## Physical acceptance

Keep the stage mechanically clear and observe it throughout acceptance:

1. Confirm the service remains active, UDP `11755` is listening, and the
	chamber reports the stage online with its driver disabled before commands.
2. Home rotation through the existing PCF8574 limit input and confirm the
	reported home position, smooth coast-down past the Hall sensor, and sequence
	acknowledgement.
3. Move rotation and linear axes in both directions using the existing chamber
	controls; confirm direction, position, status, existing maximum speeds, and
	smooth starts and stops.
4. Let motion remain idle for more than 10 seconds and confirm the driver
	disables.
5. While physically idle, stop and start the service manually; confirm the
	driver disables immediately on stop and startup produces no movement.
6. Review the journal for worker, GPIO, I2C, socket, or shutdown errors.

Remove the process-computer relay only when remote access is intentionally
being retired:

```powershell
.\scripts\setup_sample_stage_relay.ps1 -Remove
```