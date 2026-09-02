[CmdletBinding()]
param(
    [string]$Python = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$vendorRoot = Join-Path $projectRoot "vendor\cp313-aarch64"
$lockPath = Join-Path $vendorRoot "requirements.lock"
$checksumPath = Join-Path $vendorRoot "SHA256SUMS"
$sysvSourceName = "sysv_ipc-1.1.0.tar.gz"
$sysvSourceHash = "0f063cbd36ec232032e425769ebc871f195a7d183b9af32f9901589ea7129ac3"
$sysvWheelPattern = "sysv_ipc-1.1.0-cp313-*-linux_aarch64.whl"

$universalRequirements = @(
    "Adafruit-Blinka==8.68.1",
    "Adafruit-PlatformDetect==3.85.0",
    "Adafruit-PureIO==1.1.11",
    "adafruit-circuitpython-busdevice==5.2.14",
    "adafruit-circuitpython-connectionmanager==3.1.6",
    "adafruit-circuitpython-pcf8574==1.0.15",
    "adafruit-circuitpython-requests==4.1.15",
    "adafruit-circuitpython-typing==1.12.3",
    "binho-host-adapter==0.1.6",
    "pyftdi==0.57.1",
    "pyserial==3.5",
    "pyusb==1.3.1",
    "typing_extensions==4.13.2"
)

function Assert-LastExitCode {
    param([string]$Operation)

    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE."
    }
}

function Get-LowerSha256 {
    param([string]$Path)

    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

$expectedLock = @($universalRequirements + "sysv-ipc==1.1.0" | Sort-Object)
$actualLock = @(
    Get-Content $lockPath |
        Where-Object { $_.Trim().Length -gt 0 } |
        Sort-Object
)
if (Compare-Object $expectedLock $actualLock) {
    throw "requirements.lock does not match the fetcher's exact dependency set."
}

foreach ($requirement in $universalRequirements) {
    Write-Output "Fetching $requirement..."
    & $Python -m pip download `
        --disable-pip-version-check `
        --no-cache-dir `
        --no-deps `
        --only-binary=:all: `
        --dest $vendorRoot `
        $requirement
    Assert-LastExitCode "Wheel download for $requirement"
}

Write-Output "Fetching sysv-ipc==1.1.0 source..."
& $Python -m pip download `
    --disable-pip-version-check `
    --no-build-isolation `
    --no-cache-dir `
    --no-deps `
    --no-binary=:all: `
    --dest $vendorRoot `
    "sysv-ipc==1.1.0"
Assert-LastExitCode "Source download for sysv-ipc==1.1.0"

$sysvSource = Join-Path $vendorRoot $sysvSourceName
if ((Get-LowerSha256 $sysvSource) -ne $sysvSourceHash) {
    throw "sysv-ipc source archive does not match the published 1.1.0 SHA-256."
}

$wheels = @(Get-ChildItem $vendorRoot -Filter "*.whl" -File)
$universalWheels = @($wheels | Where-Object Name -like "*-none-any.whl")
if ($universalWheels.Count -ne $universalRequirements.Count) {
    throw "Expected $($universalRequirements.Count) universal wheels; found $($universalWheels.Count)."
}

$sysvWheels = @(Get-ChildItem $vendorRoot -Filter $sysvWheelPattern -File)
if ($sysvWheels.Count -eq 0) {
    Remove-Item $checksumPath -ErrorAction SilentlyContinue
    Write-Warning "Target-built sysv-ipc wheel is still missing; SHA256SUMS was not generated."
    exit 0
}
if ($sysvWheels.Count -ne 1 -or $wheels.Count -ne ($universalRequirements.Count + 1)) {
    throw "Wheelhouse must contain exactly one wheel per locked dependency."
}

$checksumLines = @(
    Get-ChildItem $vendorRoot -File |
        Where-Object Name -notin @("SHA256SUMS", "README.md") |
        Sort-Object Name |
        ForEach-Object { "$(Get-LowerSha256 $_.FullName)  $($_.Name)" }
)
[IO.File]::WriteAllText(
    $checksumPath,
    ($checksumLines -join "`n") + "`n",
    [Text.UTF8Encoding]::new($false)
)
Write-Output "Wheelhouse is complete and checksummed."
