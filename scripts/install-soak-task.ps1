<#
.SYNOPSIS
    Registers the daily soak-metrics task (one docs/progress.md row per day).
.DESCRIPTION
    Runs `main.py scheduler soak-daily --label <Label>` every day at <At> local time. The task
    only reads (local runs ledger, scheduler log, database sizes) and writes one row. Run from
    an elevated prompt to register it as SYSTEM; otherwise it registers for the current user.
    Re-running replaces the task. Remove with: Unregister-ScheduledTask -TaskName <name> -Confirm:$false
.PARAMETER Label
    Soak label; also names logs/soak/<Label>.json. Default e5-7d.
.PARAMETER At
    Daily run time, local, HH:mm. Default 23:55 (so each row covers the local day just ended).
#>
[CmdletBinding()]
param(
    [string]$Label = 'e5-7d',
    [string]$At = '23:55'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$root = Get-RepoRoot
$python = Join-Path $root 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Python not found at $python" }
$taskName = "aevoraex-soak-daily-$Label"

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "main.py scheduler soak-daily --label $Label" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

if (Test-Admin) {
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
} else {
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Limited
    Write-Warning 'Not elevated: the task runs only while this user is logged on.'
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "AevoraeX soak metrics row ($Label)" -Force | Out-Null
Write-Host "Registered '$taskName' daily at $At."
