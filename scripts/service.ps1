<#
.SYNOPSIS
    start | stop | restart | status for the aevoraex-scheduler service.

.DESCRIPTION
    Controlling a Windows service needs administrator rights; add -Elevate from a normal
    PowerShell to get a UAC prompt. 'stop' is graceful: the service gets up to 60 s to finish
    in-flight writes (ledger rows are closed; claimed detail rows are recovered on next start).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)][ValidateSet('start', 'stop', 'restart', 'status')][string]$Action,
    [string]$ServiceName = 'aevoraex-scheduler',
    [switch]$Elevate
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if ($Action -ne 'status' -and -not (Test-Admin)) {
    if ($Elevate) {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", $Action,
            '-ServiceName', "`"$ServiceName`"")
        Get-Service -Name $ServiceName | Format-Table Name, Status, StartType -AutoSize
        return
    }
    throw 'Administrator rights are required. Re-run elevated, or add -Elevate.'
}

switch ($Action) {
    'status'  { Get-Service -Name $ServiceName | Format-Table Name, Status, StartType -AutoSize }
    'start'   { Start-Service -Name $ServiceName; Get-Service -Name $ServiceName | Format-Table Name, Status -AutoSize }
    'stop'    { Stop-Service -Name $ServiceName -Force; Get-Service -Name $ServiceName | Format-Table Name, Status -AutoSize }
    'restart' { Restart-Service -Name $ServiceName -Force; Get-Service -Name $ServiceName | Format-Table Name, Status -AutoSize }
}
