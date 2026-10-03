<#
.SYNOPSIS
    Remove the aevoraex-scheduler service and the nightly backup task.

.DESCRIPTION
    Stops the service (waiting for the graceful-stop window), removes it, and unregisters the
    backup task. Logs, backups, .env and the database are never touched.
    Needs an elevated PowerShell (or -Elevate). -DryRun prints the plan only.
#>
[CmdletBinding()]
param(
    [string]$ServiceName = 'aevoraex-scheduler',
    [string]$Nssm,
    [string]$BackupTaskName = 'aevoraex-backup',
    [switch]$KeepBackupTask,
    [switch]$DryRun,
    [switch]$Elevate
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$root = Get-RepoRoot

if (-not $DryRun -and -not (Test-Admin)) {
    if ($Elevate) {
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
    throw 'Administrator rights are required. Re-run from an elevated PowerShell, or add -Elevate.'
}

function Invoke-Step([string]$Description, [scriptblock]$Action) {
    if ($DryRun) { Write-Host "[dry-run] $Description"; return }
    Write-Host $Description
    & $Action
}

$nssmExe = $Nssm
if (-not $nssmExe) {
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) { $nssmExe = $cmd.Source }
}
if (-not $nssmExe) {
    $packages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    $hit = Get-ChildItem $packages -Recurse -Filter nssm.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match 'win64' } | Select-Object -First 1
    if ($hit) { $nssmExe = $hit.FullName }
}

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    if (-not $nssmExe) { throw 'nssm.exe not found; pass -Nssm <path>.' }
    Invoke-Step "Stopping $ServiceName" { & $nssmExe stop $ServiceName | Out-Null }
    Invoke-Step "Removing $ServiceName" { & $nssmExe remove $ServiceName confirm | Out-Null }
} else {
    Write-Host "$ServiceName is not installed."
}

if (-not $KeepBackupTask) {
    if (Get-ScheduledTask -TaskName $BackupTaskName -ErrorAction SilentlyContinue) {
        Invoke-Step "Unregistering task $BackupTaskName" {
            Unregister-ScheduledTask -TaskName $BackupTaskName -Confirm:$false
        }
    } else {
        Write-Host "Task $BackupTaskName is not registered."
    }
}
Write-Host 'Done. Logs, backups, .env and the database were left untouched.'
