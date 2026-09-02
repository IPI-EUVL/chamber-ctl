[CmdletBinding()]
param(
    [string]$Target = "euvl-sample-stage",
    [string]$Python = "",
    [string]$ExpectedReleaseId = "",
    [switch]$RestartService,
    [switch]$ConfirmStageIdle
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$artifactRoot = Join-Path $projectRoot "vendor\cp313-aarch64"
$artifactManifest = Join-Path $artifactRoot "SHA256SUMS"
$serviceUnit = Join-Path $projectRoot "deploy\sample_stage.service"
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("sample-stage-deploy-" + [guid]::NewGuid().ToString("N"))
$wheelRoot = Join-Path $temporaryRoot "wheel"
$bundleRoot = Join-Path $temporaryRoot "bundle"
$bundleArchive = Join-Path $temporaryRoot "bundle.tar.gz"
$remoteToken = [guid]::NewGuid().ToString("N").Substring(0, 12)
$remoteArchive = "/tmp/sample-stage-$remoteToken.tar.gz"
$remoteStage = "/tmp/sample-stage-$remoteToken"
$remoteArchiveUploaded = $false

if ($Python.Length -eq 0) {
    $pythonCommand = "py.exe"
    $pythonPrefix = @("-3.13")
} else {
    $pythonCommand = $Python
    $pythonPrefix = @()
}

function Assert-LastExitCode {
    param([string]$Operation)

    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE."
    }
}

function Get-SingleFile {
    param(
        [string]$Path,
        [string]$Filter,
        [string]$Description
    )

    $matches = @(Get-ChildItem -Path $Path -Filter $Filter -File)
    if ($matches.Count -ne 1) {
        throw "Expected one $Description matching '$Filter' in '$Path'; found $($matches.Count)."
    }
    return $matches[0]
}

function Get-LowerSha256 {
    param([string]$Path)

    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-LowerTextSha256 {
    param([string]$Text)

    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(
        $Text.Replace("`r`n", "`n").Replace("`r", "`n")
    )
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha256.ComputeHash($bytes) | ForEach-Object {
            $_.ToString("x2")
        }) -join "")
    } finally {
        $sha256.Dispose()
    }
}

function Write-Utf8LfText {
    param(
        [string]$Path,
        [string]$Text
    )

    $normalized = $Text.Replace("`r`n", "`n").Replace("`r", "`n")
    [IO.File]::WriteAllText($Path, $normalized, [Text.UTF8Encoding]::new($false))
}

function Write-Utf8LfLines {
    param(
        [string]$Path,
        [string[]]$Lines
    )

    Write-Utf8LfText $Path (($Lines -join "`n") + "`n")
}

if ($Target -notmatch '^[A-Za-z0-9_.@-]+$') {
    throw "Target must be an SSH host or alias containing only letters, digits, '.', '_', '@', or '-'."
}
if ($ExpectedReleaseId.Length -gt 0 -and $ExpectedReleaseId -notmatch '^[A-Za-z0-9._+-]+$') {
    throw "ExpectedReleaseId contains unsupported characters: $ExpectedReleaseId"
}
if ($RestartService -and -not $ConfirmStageIdle) {
    throw "RestartService requires ConfirmStageIdle after physically verifying that the stage is idle."
}
if (-not (Test-Path $serviceUnit -PathType Leaf)) {
    throw "Systemd unit does not exist: $serviceUnit"
}
if (-not (Test-Path $artifactManifest -PathType Leaf)) {
    throw "Wheelhouse SHA256SUMS is missing. Build and return the target sysv-ipc wheel first."
}

& $pythonCommand @pythonPrefix -c "import sys; assert sys.version_info[:2] == (3, 13), sys.version"
Assert-LastExitCode "CPython 3.13 build interpreter check"

$artifactManifestLines = @(Get-Content $artifactManifest)
foreach ($line in $artifactManifestLines) {
    if ($line -notmatch '^(?<Hash>[0-9a-f]{64})  (?<Name>[^/\\]+)$') {
        throw "Invalid wheelhouse checksum line: $line"
    }
    $artifactPath = Join-Path $artifactRoot $Matches.Name
    if (-not (Test-Path $artifactPath -PathType Leaf)) {
        throw "Wheelhouse artifact is missing: $($Matches.Name)"
    }
    if ((Get-LowerSha256 $artifactPath) -ne $Matches.Hash) {
        throw "Wheelhouse checksum mismatch: $($Matches.Name)"
    }
}

$dependencyWheels = @(Get-ChildItem $artifactRoot -Filter "*.whl" -File)
if ($dependencyWheels.Count -ne 14) {
    throw "Expected 14 pinned dependency wheels; found $($dependencyWheels.Count)."
}
Get-SingleFile $artifactRoot "sysv_ipc-1.1.0-cp313-*-linux_aarch64.whl" "sysv-ipc target wheel" | Out-Null
if (@($dependencyWheels | Where-Object Name -like "pcf8574_io-*").Count -ne 0) {
    throw "The unused pcf8574-io package must not be bundled."
}

New-Item -ItemType Directory -Path $wheelRoot, $bundleRoot | Out-Null
try {
    Write-Output "Building the sample-stage application wheel with CPython 3.13..."
    $previousSourceDateEpoch = [Environment]::GetEnvironmentVariable(
        "SOURCE_DATE_EPOCH",
        "Process"
    )
    try {
        [Environment]::SetEnvironmentVariable(
            "SOURCE_DATE_EPOCH",
            "315532800",
            "Process"
        )
        & $pythonCommand @pythonPrefix -m pip wheel `
            --disable-pip-version-check `
            --no-cache-dir `
            --no-deps `
            --wheel-dir $wheelRoot `
            $projectRoot
        Assert-LastExitCode "Application wheel build"
    } finally {
        [Environment]::SetEnvironmentVariable(
            "SOURCE_DATE_EPOCH",
            $previousSourceDateEpoch,
            "Process"
        )
    }
    $applicationWheel = Get-SingleFile $wheelRoot "ipi_sample_stage_ctl-*.whl" "application wheel"
    if ($applicationWheel.Name -notmatch '^ipi_sample_stage_ctl-(?<Version>.+)-py3-none-any\.whl$') {
        throw "Unexpected application wheel name: $($applicationWheel.Name)"
    }

    $applicationVersion = $Matches.Version
    $applicationHash = Get-LowerSha256 $applicationWheel.FullName

    Copy-Item $applicationWheel.FullName $bundleRoot
    foreach ($wheel in $dependencyWheels) {
        Copy-Item $wheel.FullName $bundleRoot
    }
    Write-Utf8LfText `
        (Join-Path $bundleRoot "requirements.lock") `
        ([IO.File]::ReadAllText((Join-Path $artifactRoot "requirements.lock")))
    Write-Utf8LfText (Join-Path $bundleRoot "sample_stage.service") ([IO.File]::ReadAllText($serviceUnit))

    $wheelhouseManifestHash = Get-LowerSha256 $artifactManifest
    $releaseContentLines = @(
        Get-ChildItem $bundleRoot -File |
            Sort-Object Name |
            ForEach-Object { "$(Get-LowerSha256 $_.FullName)  $($_.Name)" }
    )
    $releaseContentLines += "$wheelhouseManifestHash  WHEELHOUSE-SHA256SUMS"
    $releaseContentHash = Get-LowerTextSha256 (($releaseContentLines -join "`n") + "`n")
    $releaseId = "$applicationVersion-$($releaseContentHash.Substring(0, 12))"
    if ($releaseId -notmatch '^[A-Za-z0-9._+-]+$') {
        throw "Generated release ID contains unsupported characters: $releaseId"
    }
    if ($ExpectedReleaseId.Length -gt 0 -and $releaseId -cne $ExpectedReleaseId) {
        throw "Built release '$releaseId' does not match expected release '$ExpectedReleaseId'."
    }

    $releaseMetadata = @(
        "release_id=$releaseId",
        "release_content_sha256=$releaseContentHash",
        "application_version=$applicationVersion",
        "application_sha256=$applicationHash",
        "wheelhouse_manifest_sha256=$wheelhouseManifestHash"
    )
    $releasePath = Join-Path $bundleRoot "RELEASE"
    Write-Utf8LfLines $releasePath $releaseMetadata

    $checksumLines = @(
        Get-ChildItem $bundleRoot -File |
            Sort-Object Name |
            ForEach-Object { "$(Get-LowerSha256 $_.FullName)  $($_.Name)" }
    )
    $checksumPath = Join-Path $bundleRoot "SHA256SUMS"
    Write-Utf8LfLines $checksumPath $checksumLines
    foreach ($linuxTextPath in @(
        $releasePath,
        $checksumPath,
        (Join-Path $bundleRoot "sample_stage.service"),
        (Join-Path $bundleRoot "requirements.lock")
    )) {
        if ([Array]::IndexOf([IO.File]::ReadAllBytes($linuxTextPath), [byte]13) -ge 0) {
            throw "Linux deployment text contains a carriage return: $linuxTextPath"
        }
    }
    $bundleManifestHash = Get-LowerSha256 $checksumPath

    Write-Output "Packaging release $releaseId..."
    & tar.exe -czf $bundleArchive -C $bundleRoot .
    Assert-LastExitCode "Deployment bundle creation"

    Write-Output "Checking passwordless SSH and sudo access to $Target..."
    & ssh.exe -o BatchMode=yes -o ConnectionAttempts=1 -o ConnectTimeout=10 $Target "sudo -n true"
    Assert-LastExitCode "SSH and sudo preflight"

    Write-Output "Uploading release $releaseId..."
    & scp.exe $bundleArchive "${Target}:$remoteArchive"
    Assert-LastExitCode "Deployment bundle upload"
    $remoteArchiveUploaded = $true

    $activation = if ($RestartService) { "restart" } else { "defer" }
    $remoteScript = @'
set -euo pipefail

archive=$1
stage=$2
release_id=$3
bundle_manifest_sha256=$4
activation=$5
application_root=/opt/sample-stage
releases_root=$application_root/releases
release_path=$releases_root/$release_id
current_link=$application_root/current
unit_name=sample_stage.service
unit_path=/etc/systemd/system/$unit_name
temporary_release=$releases_root/.$release_id.tmp.$$
preflight_runtime=

cleanup() {
    rm -rf -- "$stage" "$temporary_release"
    if [ -n "$preflight_runtime" ]; then
        rm -rf -- "$preflight_runtime"
    fi
    rm -f -- "$archive"
}
trap cleanup EXIT

install -d -m 0700 "$stage"
tar -xzf "$archive" -C "$stage"
cd "$stage"
printf '%s  %s\n' "$bundle_manifest_sha256" SHA256SUMS | sha256sum --check
sha256sum --check SHA256SUMS
chmod 0644 sample_stage.service

grep -Fqx 'User=euvl' sample_stage.service
grep -Fqx 'Group=euvl' sample_stage.service
grep -Fqx 'RuntimeDirectory=sample-stage' sample_stage.service
grep -Fqx 'RuntimeDirectoryMode=0750' sample_stage.service
grep -Fqx 'WorkingDirectory=/run/sample-stage' sample_stage.service
grep -Fqx 'Environment="PYTHONNOUSERSITE=1"' sample_stage.service
grep -Fqx 'Environment="PYTHONPATH=/opt/sample-stage/current/python"' sample_stage.service
grep -Fqx 'ExecStart=/usr/bin/python3 -m sample_stage_ctl' sample_stage.service
grep -Fqx 'Restart=on-failure' sample_stage.service
grep -Fqx 'KillSignal=SIGTERM' sample_stage.service
systemd-analyze verify "$stage/sample_stage.service"

test "$(uname -m)" = aarch64
/usr/bin/python3 -c 'import sys; assert sys.version_info[:2] == (3, 13), sys.version'
id euvl >/dev/null
dpkg-query -W -f='${Status}\n' python3-rpi-lgpio | grep -Fqx 'install ok installed'
dpkg-query -W -f='${Status}\n' python3-lgpio | grep -Fqx 'install ok installed'
runuser -u euvl -- test -r /dev/gpiomem
runuser -u euvl -- test -w /dev/gpiomem
runuser -u euvl -- test -r /dev/i2c-1
runuser -u euvl -- test -w /dev/i2c-1

install -d -m 0755 "$releases_root"
if [ -e "$release_path" ]; then
    if [ ! -f "$release_path/.bundle-sha256" ] || [ "$(cat "$release_path/.bundle-sha256")" != "$bundle_manifest_sha256" ]; then
        echo "Release path already exists with different contents: $release_path" >&2
        exit 1
    fi
    test -f "$release_path/sample_stage.service"
    echo "Reusing verified release $release_id."
else
    install -d -m 0755 "$temporary_release/python"
    preflight_runtime=$(mktemp -d /run/sample-stage-preflight.XXXXXX)
    chown euvl:euvl "$preflight_runtime"
    chmod 0750 "$preflight_runtime"
    application_wheel=$(find "$stage" -maxdepth 1 -type f -name 'ipi_sample_stage_ctl-*.whl' -print -quit)
    test -n "$application_wheel"
    mapfile -t dependency_wheels < <(find "$stage" -maxdepth 1 -type f -name '*.whl' ! -name 'ipi_sample_stage_ctl-*.whl' -print | sort)
    test "${#dependency_wheels[@]}" -eq 14
    /usr/bin/python3 -m pip install \
        --disable-pip-version-check \
        --no-index \
        --no-deps \
        --root-user-action=ignore \
        --target "$temporary_release/python" \
        "$application_wheel" \
        "${dependency_wheels[@]}"

    (
        cd "$preflight_runtime"
        RELEASE_PYTHON_ROOT="$temporary_release/python" \
        PYTHONNOUSERSITE=1 \
        PYTHONPATH="$temporary_release/python" \
        runuser -u euvl --preserve-environment -- /usr/bin/python3 - <<'PY'
import importlib.metadata
import pathlib
import platform
import site
import sys

import Adafruit_PureIO
import RPi.GPIO
import adafruit_blinka
import adafruit_bus_device
import adafruit_connection_manager
import adafruit_pcf8574
import adafruit_platformdetect
import adafruit_requests
import binhoHostAdapter
import board
import circuitpython_typing
import digitalio
import lgpio
import pyftdi
import serial
import sysv_ipc
import typing_extensions
import usb
from sample_stage_ctl import protocol

release_root = pathlib.Path(__import__("os").environ["RELEASE_PYTHON_ROOT"]).resolve()
assert sys.version_info[:2] == (3, 13), sys.version
assert platform.machine() == "aarch64", platform.machine()
assert site.ENABLE_USER_SITE is False

expected_versions = {
    "ipi-sample-stage-ctl": "0.1.0",
    "Adafruit-Blinka": "8.68.1",
    "Adafruit-PlatformDetect": "3.85.0",
    "Adafruit-PureIO": "1.1.11",
    "adafruit-circuitpython-busdevice": "5.2.14",
    "adafruit-circuitpython-connectionmanager": "3.1.6",
    "adafruit-circuitpython-pcf8574": "1.0.15",
    "adafruit-circuitpython-requests": "4.1.15",
    "adafruit-circuitpython-typing": "1.12.3",
    "binho-host-adapter": "0.1.6",
    "pyftdi": "0.57.1",
    "pyserial": "3.5",
    "pyusb": "1.3.1",
    "sysv-ipc": "1.1.0",
    "typing_extensions": "4.13.2",
}
for distribution, version in expected_versions.items():
    installed = importlib.metadata.distribution(distribution)
    assert installed.version == version, distribution
    assert pathlib.Path(installed.locate_file("")).resolve().is_relative_to(release_root), distribution

release_modules = (
    Adafruit_PureIO,
    adafruit_blinka,
    adafruit_bus_device,
    adafruit_connection_manager,
    adafruit_pcf8574,
    adafruit_platformdetect,
    adafruit_requests,
    binhoHostAdapter,
    board,
    circuitpython_typing,
    digitalio,
    pyftdi,
    serial,
    sysv_ipc,
    typing_extensions,
    usb,
)
for module in release_modules:
    assert pathlib.Path(module.__file__).resolve().is_relative_to(release_root), module.__name__

assert pathlib.Path(RPi.GPIO.__file__).resolve().is_relative_to("/usr/lib/python3/dist-packages")
assert pathlib.Path(lgpio.__file__).resolve().is_relative_to("/usr/lib/python3/dist-packages")
assert protocol.SERVER_INCOMING_HOST == "0.0.0.0"
assert protocol.SERVER_INCOMING_PORT == 11755
assert protocol.parse_datagram(b"7,HOME,0,T,50").arguments == (0, True, 50)
print("release_preflight_ok")
PY
    )

    rpi_gpio_path=$(cd "$preflight_runtime" && PYTHONNOUSERSITE=1 runuser -u euvl --preserve-environment -- /usr/bin/python3 -c 'import RPi.GPIO; print(RPi.GPIO.__file__)')
    lgpio_path=$(cd "$preflight_runtime" && PYTHONNOUSERSITE=1 runuser -u euvl --preserve-environment -- /usr/bin/python3 -c 'import lgpio; print(lgpio.__file__)')
    dpkg-query -S "$rpi_gpio_path" | grep -Fq 'python3-rpi-lgpio:'
    dpkg-query -S "$lgpio_path" | grep -Fq 'python3-lgpio:'

    printf '%s\n' "$bundle_manifest_sha256" > "$temporary_release/.bundle-sha256"
    cp "$stage/RELEASE" "$temporary_release/RELEASE"
    cp "$stage/requirements.lock" "$temporary_release/requirements.lock"
    cp "$stage/sample_stage.service" "$temporary_release/sample_stage.service"
    chown -R root:root "$temporary_release"
    find "$temporary_release" -type f -exec chmod 0444 {} +
    find "$temporary_release" -type d -exec chmod 0555 {} +
    mv "$temporary_release" "$release_path"
fi

if [ "$activation" = defer ]; then
    echo "STAGED_RELEASE=$release_id"
    echo "STAGED_PATH=$release_path"
    echo "The current link, systemd unit, enablement, and process were not changed."
    exit 0
fi

previous_release=
had_previous_current=false
if [ -e "$current_link" ] || [ -L "$current_link" ]; then
    if [ ! -L "$current_link" ]; then
        echo "Refusing to replace non-symlink path: $current_link" >&2
        exit 1
    fi
    previous_release=$(readlink -f "$current_link")
    test -d "$previous_release"
    had_previous_current=true
fi
previous_unit="$stage/$unit_name.previous"
had_previous_unit=false
if [ -f "$unit_path" ]; then
    cp -p "$unit_path" "$previous_unit"
    had_previous_unit=true
fi
was_enabled=false
if systemctl is-enabled --quiet "$unit_name"; then
    was_enabled=true
fi
was_active=false
if systemctl is-active --quiet "$unit_name"; then
    was_active=true
fi

temporary_link=$application_root/.current.$$.tmp
rollback_link=$application_root/.current.rollback.$$.tmp

restore_previous_installation() {
    local rollback_ok=true

    rm -f "$temporary_link" "$rollback_link"
    if [ "$was_active" = false ]; then
        systemctl stop "$unit_name" >/dev/null 2>&1 || true
    fi
    if [ "$was_enabled" = false ]; then
        systemctl disable "$unit_name" >/dev/null 2>&1 || true
    fi

    if [ "$had_previous_unit" = true ]; then
        if ! install -m 0644 "$previous_unit" "$unit_path"; then
            rollback_ok=false
        fi
    elif ! rm -f "$unit_path"; then
        rollback_ok=false
    fi

    if [ "$had_previous_current" = true ]; then
        if ! ln -s "$previous_release" "$rollback_link" || ! mv -Tf "$rollback_link" "$current_link"; then
            rollback_ok=false
        fi
    elif ! rm -f "$current_link"; then
        rollback_ok=false
    fi

    if ! systemctl daemon-reload; then
        rollback_ok=false
    fi
    if [ "$was_enabled" = true ]; then
        if ! systemctl enable "$unit_name" >/dev/null; then
            rollback_ok=false
        fi
    elif systemctl is-enabled --quiet "$unit_name"; then
        rollback_ok=false
    fi

    if [ "$was_active" = true ]; then
        if ! systemctl restart "$unit_name" || ! systemctl is-active --quiet "$unit_name"; then
            rollback_ok=false
        fi
    elif systemctl is-active --quiet "$unit_name"; then
        rollback_ok=false
    fi

    [ "$rollback_ok" = true ]
}

rollback_after_failure() {
    echo "Restoring the previous sample-stage installation." >&2
    if ! restore_previous_installation; then
        echo "Rollback was incomplete; manual recovery is required." >&2
    fi
    exit 1
}

installation_ok=true
if ! ln -s "$release_path" "$temporary_link"; then
    installation_ok=false
elif ! mv -Tf "$temporary_link" "$current_link"; then
    installation_ok=false
elif ! install -m 0644 "$stage/sample_stage.service" "$unit_path"; then
    installation_ok=false
elif ! systemctl daemon-reload; then
    installation_ok=false
elif ! systemctl enable "$unit_name" >/dev/null; then
    installation_ok=false
fi
if [ "$installation_ok" = false ]; then
    rollback_after_failure
fi

activation_ok=true
if ! systemctl restart "$unit_name"; then
    activation_ok=false
elif ! systemctl is-active --quiet "$unit_name"; then
    activation_ok=false
else
    sleep 3
    if ! systemctl is-active --quiet "$unit_name"; then
        activation_ok=false
    elif ! ss -H -lun | awk '$4 ~ /:11755$/ { found=1 } END { exit !found }'; then
        activation_ok=false
    fi
fi

if [ "$activation_ok" = false ]; then
    echo "The new release failed its activation checks." >&2
    systemctl status "$unit_name" --no-pager --full >&2 || true
    journalctl -u "$unit_name" -n 50 --no-pager >&2 || true
    rollback_after_failure
fi

echo "DEPLOYED_RELEASE=$release_id"
echo "PREVIOUS_RELEASE=${previous_release:-none}"
echo "CURRENT_RELEASE=$(readlink -f "$current_link")"
echo "SERVICE_STATE=$(systemctl is-active "$unit_name" 2>/dev/null || true)"
'@
    $encodedScript = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($remoteScript -replace "`r", "")))
    & ssh.exe $Target "printf '%s' '$encodedScript' | base64 -d | sudo -n bash -s -- '$remoteArchive' '$remoteStage' '$releaseId' '$bundleManifestHash' '$activation'"
    Assert-LastExitCode "Remote release installation"
    $remoteArchiveUploaded = $false

    if ($RestartService) {
        Write-Output "Release $releaseId deployed and activated successfully."
    } else {
        Write-Output "Release $releaseId staged successfully; live service state was not changed."
    }
}
finally {
    if ($remoteArchiveUploaded) {
        & ssh.exe -o BatchMode=yes -o ConnectTimeout=5 $Target "rm -f -- '$remoteArchive'; rm -rf -- '$remoteStage'" 2>$null
    }
    Remove-Item $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}
