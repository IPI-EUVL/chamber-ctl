[CmdletBinding()]
param(
    [string]$Alias = "euvl-sample-stage",
    [string]$HostName = "10.194.210.21",
    [int]$Port = 2223,
    [string]$User = "euvl",
    [string]$IdentityFile = (Join-Path $HOME ".ssh\id_ed25519_euvl_sample_stage"),
    [string]$ConfigPath = (Join-Path $HOME ".ssh\config")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Alias -notmatch '^[A-Za-z0-9_.-]+$') {
    throw "SSH alias contains unsupported characters: $Alias"
}

$sshDirectory = Split-Path $IdentityFile -Parent
if ((Split-Path $ConfigPath -Parent) -ne $sshDirectory) {
    throw "IdentityFile and ConfigPath must use the same SSH directory."
}
New-Item -ItemType Directory -Path $sshDirectory -Force | Out-Null

$publicKeyPath = "$IdentityFile.pub"
$privateExists = Test-Path $IdentityFile -PathType Leaf
$publicExists = Test-Path $publicKeyPath -PathType Leaf
if ($privateExists -xor $publicExists) {
    throw "Only one half of the dedicated sample-stage key pair exists; no files were changed."
}
if (-not $privateExists) {
    & ssh-keygen.exe -q -t ed25519 -f $IdentityFile -N '""' -C $Alias
    if ($LASTEXITCODE -ne 0) {
        throw "ssh-keygen failed with exit code $LASTEXITCODE."
    }
    Write-Output "Created dedicated SSH key: $IdentityFile"
} else {
    Write-Output "Dedicated SSH key already exists: $IdentityFile"
}

$identityConfigPath = "~/.ssh/" + [IO.Path]::GetFileName($IdentityFile)
$requiredDirectives = [ordered]@{
    HostName = $HostName
    Port = [string]$Port
    User = $User
    IdentityFile = $identityConfigPath
    IdentitiesOnly = "yes"
}
$configText = if (Test-Path $ConfigPath -PathType Leaf) {
    [IO.File]::ReadAllText($ConfigPath)
} else {
    ""
}
$lines = @($configText -split "`r?`n")
$matchingHostIndexes = @()
for ($index = 0; $index -lt $lines.Count; $index++) {
    if ($lines[$index] -match '^\s*Host\s+(.+?)\s*$') {
        $hostAliases = @($Matches[1] -split '\s+')
        if ($hostAliases -contains $Alias) {
            $matchingHostIndexes += $index
        }
    }
}

if ($matchingHostIndexes.Count -gt 1) {
    throw "SSH alias '$Alias' appears in multiple Host blocks; no config changes were made."
}
if ($matchingHostIndexes.Count -eq 1) {
    $start = $matchingHostIndexes[0]
    if ($lines[$start].Trim() -ne "Host $Alias") {
        throw "SSH alias '$Alias' shares a Host block with other aliases; no config changes were made."
    }
    $end = $lines.Count
    for ($index = $start + 1; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match '^\s*Host\s+') {
            $end = $index
            break
        }
    }

    $actualDirectives = @{}
    for ($index = $start + 1; $index -lt $end; $index++) {
        if ($lines[$index] -match '^\s*(\S+)\s+(.+?)\s*$') {
            $actualDirectives[$Matches[1]] = $Matches[2]
        }
    }
    foreach ($directive in $requiredDirectives.GetEnumerator()) {
        if ($actualDirectives[$directive.Key] -ne $directive.Value) {
            throw "SSH alias '$Alias' has a conflicting $($directive.Key) directive."
        }
    }
    Write-Output "SSH alias '$Alias' already matches."
} else {
    $block = @(
        "Host $Alias",
        "  HostName $HostName",
        "  Port $Port",
        "  User $User",
        "  IdentityFile $identityConfigPath",
        "  IdentitiesOnly yes"
    ) -join "`n"
    $prefix = if ($configText.Length -eq 0) { "" } else { $configText.TrimEnd() + "`n`n" }
    [IO.File]::WriteAllText(
        $ConfigPath,
        $prefix + $block + "`n",
        [Text.UTF8Encoding]::new($false)
    )
    Write-Output "Added SSH alias '$Alias' to $ConfigPath"
}

Write-Output "Authorize this public key for '$User' on the sample-stage Pi:"
Get-Content $publicKeyPath
