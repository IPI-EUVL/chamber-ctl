[CmdletBinding()]
param(
    [switch]$Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$natName = "EUVL-Instrument-NAT"
$internalPrefix = "10.11.13.0/24"
$processComputerAddress = "10.194.210.21"
$pitayaExternalPort = 2222
$pitayaInternalAddress = "10.11.13.50"
$sampleStageExternalPort = 2223
$sampleStageInternalAddress = "10.11.13.225"
$sshPort = 22
$firewallRuleName = "EUVL-Sample-Stage-SSH-Relay-2223"
$firewallDisplayName = "EUVL Sample Stage SSH Relay (TCP 2223)"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated Windows PowerShell session on the process computer."
}

$processAddress = @(
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object IPAddress -eq $processComputerAddress
)
if ($processAddress.Count -eq 0) {
    throw "This host does not own $processComputerAddress; run the relay script on the process computer."
}

$nat = Get-NetNat -Name $natName -ErrorAction Stop
if ($nat.InternalIPInterfaceAddressPrefix -ne $internalPrefix -or -not $nat.Active) {
    throw "NAT '$natName' is not the expected active $internalPrefix instrument NAT."
}

$mappings = @(Get-NetNatStaticMapping -NatName $natName -ErrorAction Stop)
$pitayaMapping = @(
    $mappings | Where-Object {
        $_.Protocol -eq "TCP" -and
        $_.ExternalIPAddress -eq "0.0.0.0" -and
        $_.ExternalPort -eq $pitayaExternalPort -and
        $_.InternalIPAddress -eq $pitayaInternalAddress -and
        $_.InternalPort -eq $sshPort
    }
)
if ($pitayaMapping.Count -ne 1) {
    throw "The existing Pitaya mapping must remain exactly 0.0.0.0:2222 -> 10.11.13.50:22/TCP."
}

$sampleStageMappings = @(
    $mappings | Where-Object {
        $_.Protocol -eq "TCP" -and $_.ExternalPort -eq $sampleStageExternalPort
    }
)
$exactSampleStageMapping = @(
    $sampleStageMappings | Where-Object {
        $_.ExternalIPAddress -eq "0.0.0.0" -and
        $_.InternalIPAddress -eq $sampleStageInternalAddress -and
        $_.InternalPort -eq $sshPort
    }
)

if ($sampleStageMappings.Count -ne $exactSampleStageMapping.Count) {
    throw "TCP port 2223 already has a conflicting WinNAT mapping; no changes were made."
}
if ($exactSampleStageMapping.Count -gt 1) {
    throw "Multiple sample-stage mappings exist on TCP port 2223; no changes were made."
}

if ($Remove) {
    if ($exactSampleStageMapping.Count -eq 1) {
        $exactSampleStageMapping[0] |
            Remove-NetNatStaticMapping -Confirm:$false -ErrorAction Stop
        Write-Output "Removed sample-stage WinNAT mapping."
    } else {
        Write-Output "Sample-stage WinNAT mapping is already absent."
    }

    $firewallRule = Get-NetFirewallRule -Name $firewallRuleName -ErrorAction SilentlyContinue
    if ($null -ne $firewallRule) {
        $firewallRule | Remove-NetFirewallRule -ErrorAction Stop
        Write-Output "Removed sample-stage firewall rule."
    } else {
        Write-Output "Sample-stage firewall rule is already absent."
    }
} else {
    $mappingCreated = $false
    try {
        if ($exactSampleStageMapping.Count -eq 0) {
            Add-NetNatStaticMapping `
                -NatName $natName `
                -Protocol TCP `
                -ExternalIPAddress "0.0.0.0" `
                -ExternalPort $sampleStageExternalPort `
                -InternalIPAddress $sampleStageInternalAddress `
                -InternalPort $sshPort `
                -ErrorAction Stop | Out-Null
            $mappingCreated = $true
            Write-Output "Added sample-stage WinNAT mapping."
        } else {
            Write-Output "Sample-stage WinNAT mapping already matches."
        }

        $firewallRule = Get-NetFirewallRule -Name $firewallRuleName -ErrorAction SilentlyContinue
        if ($null -eq $firewallRule) {
            New-NetFirewallRule `
                -Name $firewallRuleName `
                -DisplayName $firewallDisplayName `
                -Direction Inbound `
                -Action Allow `
                -Enabled True `
                -Protocol TCP `
                -LocalPort $sampleStageExternalPort `
                -RemoteAddress Any `
                -ErrorAction Stop | Out-Null
            Write-Output "Added sample-stage firewall rule."
        } else {
            $portFilter = $firewallRule | Get-NetFirewallPortFilter
            $addressFilter = $firewallRule | Get-NetFirewallAddressFilter
            if (
                $firewallRule.Direction -ne "Inbound" -or
                $firewallRule.Action -ne "Allow" -or
                $firewallRule.Enabled -ne "True" -or
                $portFilter.Protocol -ne "TCP" -or
                [string]$portFilter.LocalPort -ne [string]$sampleStageExternalPort -or
                [string]$addressFilter.RemoteAddress -ne "Any"
            ) {
                throw "Firewall rule '$firewallRuleName' exists with conflicting settings."
            }
            Write-Output "Sample-stage firewall rule already matches."
        }
    } catch {
        if ($mappingCreated) {
            Get-NetNatStaticMapping -NatName $natName -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.Protocol -eq "TCP" -and
                    $_.ExternalIPAddress -eq "0.0.0.0" -and
                    $_.ExternalPort -eq $sampleStageExternalPort -and
                    $_.InternalIPAddress -eq $sampleStageInternalAddress -and
                    $_.InternalPort -eq $sshPort
                } |
                Remove-NetNatStaticMapping -Confirm:$false -ErrorAction SilentlyContinue
        }
        throw
    }
}

Get-NetNatStaticMapping -NatName $natName -ErrorAction Stop |
    Sort-Object ExternalPort |
    Format-Table Protocol, ExternalIPAddress, ExternalPort, InternalIPAddress, InternalPort
