<#
.SYNOPSIS
    Make the laptop safe to leave running as the always-on engine host.

.DESCRIPTION
    Default is REPORT ONLY: it prints the current AC power settings. Add -Apply to change them.
    Applied to the ACTIVE power scheme, on AC power only (battery behaviour is left alone):
      - never sleep, never hibernate, never spin down the disk
      - closing the lid does nothing
      - USB selective suspend, PCIe link-state power management and Wi-Fi power saving off
      - screen turns off after -MonitorMinutes (default 10); the screen is not needed
    Optional (-Apply required, administrator required):
      -DisableHibernate   powercfg /hibernate off (also frees the multi-GB hiberfil.sys on C:)
      -ActiveHours        Windows Update active hours 06:00-00:00 so updates avoid restarting mid-run

    Windows can still reboot for updates; the service is set to start automatically (delayed) and
    recovers its own queue on start, so a reboot costs freshness, never data.
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [int]$MonitorMinutes = 10,
    [switch]$DisableHibernate,
    [switch]$ActiveHours,
    [switch]$Elevate
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if ($Apply -and $Elevate -and -not (Test-Admin)) {
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    foreach ($name in $PSBoundParameters.Keys) {
        if ($name -eq 'Elevate') { continue }
        $value = $PSBoundParameters[$name]
        if ($value -is [switch]) { if ($value.IsPresent) { $arguments += "-$name" } }
        else { $arguments += "-$name"; $arguments += "`"$value`"" }
    }
    Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait
    return
}

# GUIDs: subgroup / setting
$Settings = @(
    @{ Name = 'Sleep after (s, 0 = never)';        Sub = 'SUB_SLEEP';   Id = 'STANDBYIDLE';    Want = 0 },
    @{ Name = 'Hibernate after (s, 0 = never)';      Sub = 'SUB_SLEEP';   Id = 'HIBERNATEIDLE';  Want = 0 },
    @{ Name = 'Disk off after (s, 0 = never)';       Sub = 'SUB_DISK';    Id = 'DISKIDLE';       Want = 0 },
    @{ Name = 'Lid close action (0 = do nothing)';   Sub = '4f971e89-eebd-4455-a8de-9e59040e7347'; Id = '5ca83367-6e45-459f-a27b-476b1d01c936'; Want = 0 },
    @{ Name = 'USB selective suspend (0 = off)';     Sub = '2a737441-1930-4402-8d77-b2bebba308a3'; Id = '48e6b7a6-50f5-4782-a5d4-53bb8f07e226'; Want = 0 },
    @{ Name = 'PCIe link-state PM (0 = off)';        Sub = '501a4d13-42af-4429-9fd1-a8218c268e20'; Id = 'ee12f906-d277-404b-b6da-e5fa1a576df5'; Want = 0 },
    @{ Name = 'Wi-Fi power saving (0 = max perf)';   Sub = '19cbb8fa-5279-450e-9fac-8a3d5fedd0c1'; Id = '12bbebe6-58d6-4636-95bb-3217ef867c1a'; Want = 0 }
)

function Get-AcValue([string]$Sub, [string]$Id) {
    $lines = & powercfg /query SCHEME_CURRENT $Sub $Id 2>$null
    foreach ($line in $lines) {
        if ($line -match 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)') {
            return [Convert]::ToInt64($Matches[1], 16)
        }
    }
    return $null
}

# STANDBYIDLE / DISKIDLE etc. report seconds; normalise "never" to 0 for the display only.
Write-Host ((& powercfg /getactivescheme) -join ' ')
Write-Host ''
$rows = foreach ($s in $Settings) {
    $current = Get-AcValue $s.Sub $s.Id
    [pscustomobject]@{
        Setting = $s.Name
        Current = if ($null -eq $current) { 'n/a (not supported on this machine)' } else { $current }
        Wanted  = $s.Want
    }
}
$rows | Format-Table -AutoSize | Out-String | Write-Host

if (-not $Apply) {
    Write-Host 'Report only. Re-run with -Apply to change these AC settings.'
    return
}

foreach ($s in $Settings) {
    if ($null -eq (Get-AcValue $s.Sub $s.Id)) {
        # Some OEM images hide settings (the lid action is commonly hidden). Try to unhide once.
        if (Test-Admin) { & powercfg -attributes $s.Sub $s.Id -ATTRIB_HIDE | Out-Null }
        if ($null -eq (Get-AcValue $s.Sub $s.Id)) {
            Write-Warning "Not available on this machine, skipped: $($s.Name)"
            continue
        }
    }
    & powercfg /setacvalueindex SCHEME_CURRENT $s.Sub $s.Id $s.Want | Out-Null
}
& powercfg /change monitor-timeout-ac $MonitorMinutes | Out-Null
& powercfg /change standby-timeout-ac 0 | Out-Null
& powercfg /change hibernate-timeout-ac 0 | Out-Null
& powercfg /setactive SCHEME_CURRENT | Out-Null
Write-Host 'AC power settings applied to the active scheme.'

if ($DisableHibernate) {
    if (-not (Test-Admin)) { throw 'Run from an elevated PowerShell to use -DisableHibernate.' }
    & powercfg /hibernate off
    Write-Host 'Hibernation (and hiberfil.sys) disabled.'
}

if ($ActiveHours) {
    if (-not (Test-Admin)) { throw 'Run from an elevated PowerShell to use -ActiveHours.' }
    $key = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
    New-ItemProperty -Path $key -Name 'ActiveHoursStart' -Value 6 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $key -Name 'ActiveHoursEnd' -Value 0 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $key -Name 'SmartActiveHoursState' -Value 2 -PropertyType DWord -Force | Out-Null
    Write-Host 'Windows Update active hours set to 06:00-00:00.'
}

Write-Host ''
Write-Host 'Re-run without -Apply to verify. Also keep the laptop plugged in and ventilated.'
