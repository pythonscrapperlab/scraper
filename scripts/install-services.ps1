<#
.SYNOPSIS
    Install (or update) the aevoraex-scheduler Windows service with NSSM, plus the nightly
    backup scheduled task.

.DESCRIPTION
    The service runs:  venv\Scripts\python.exe main.py scheduler run   (working dir = repo root)
    - starts automatically (delayed) after PostgreSQL; restarts on any exit after 15 s
    - stops with Ctrl+C and waits up to 60 s so in-flight work can record itself
    - stdout/stderr go to logs\service-*.log (rotated); the application writes JSON logs to
      logs\scheduler.jsonl (daily rotation, no PII)
    - a failed preflight (config invalid, DB unreachable) aborts BEFORE anything is installed

    Needs an elevated PowerShell. From a normal one, use -Elevate to get a UAC prompt.
    -DryRun prints every change without making any. Re-running updates in place (idempotent).

    NSSM is not bundled. Install it once with:  winget install NSSM.NSSM
    (or pass -Nssm 'C:\path\to\nssm.exe').
#>
[CmdletBinding()]
param(
    [string]$ServiceName = 'aevoraex-scheduler',
    [string]$Nssm,
    [string]$BackupTime = '04:30',
    [string]$BackupTaskName = 'aevoraex-backup',
    [switch]$SkipBackupTask,
    [switch]$Start,
    [switch]$ApplyPower,
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

$transcript = Join-Path $root 'logs\install-services.log'
New-Item -ItemType Directory -Force (Split-Path -Parent $transcript) | Out-Null
try { Start-Transcript -Path $transcript -Force | Out-Null } catch { }

function Invoke-Step([string]$Description, [scriptblock]$Action) {
    if ($DryRun) { Write-Host "[dry-run] $Description"; return }
    Write-Host $Description
    & $Action
}

function Find-Nssm {
    if ($Nssm) { return $Nssm }
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $local = Join-Path $root 'tools\nssm.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    $packages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (Test-Path -LiteralPath $packages) {
        $hit = Get-ChildItem $packages -Recurse -Filter nssm.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match 'win64' } | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    throw 'nssm.exe not found. Run: winget install NSSM.NSSM   (or pass -Nssm <path>)'
}

# ---- preflight ----------------------------------------------------------------------------
$python = Join-Path $root 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Missing $python. Create the venv first (setup.ps1)." }
if (-not (Test-Path -LiteralPath (Join-Path $root '.env'))) { throw 'Missing .env (copy .env.example).' }
$nssmExe = Find-Nssm
Write-Host "Using NSSM: $nssmExe"

Write-Host 'Preflight: validating scheduler configuration and schedule (executes nothing)...'
Push-Location $root
try {
    & $python main.py scheduler preview --offline --hours 6 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Preflight failed: python main.py scheduler preview --offline' }
} finally { Pop-Location }

$postgres = Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue | Select-Object -First 1
$dependency = if ($postgres) { $postgres.Name } else { '' }
if (-not $dependency) { Write-Warning 'No local PostgreSQL service found; the dependency is not set.' }

# ---- service ------------------------------------------------------------------------------
$logs = Join-Path $root 'logs'
$exists = [bool](Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)

if ($exists) {
    Invoke-Step "Stopping existing $ServiceName" { & $nssmExe stop $ServiceName | Out-Null }
    Invoke-Step 'Updating program path and arguments' {
        & $nssmExe set $ServiceName Application $python | Out-Null
        & $nssmExe set $ServiceName AppParameters 'main.py scheduler run' | Out-Null
    }
} else {
    Invoke-Step "Installing $ServiceName" {
        & $nssmExe install $ServiceName $python 'main.py scheduler run' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'nssm install failed' }
    }
}

$settings = [ordered]@{
    AppDirectory         = $root
    DisplayName          = 'AevoraeX engine scheduler'
    Description          = 'Drives Redfin freshness checks, refreshes, analysis and Supabase publishing.'
    Start                = 'SERVICE_DELAYED_AUTO_START'
    AppStdout            = (Join-Path $logs 'service-stdout.log')
    AppStderr            = (Join-Path $logs 'service-stderr.log')
    AppRotateFiles       = 1
    AppRotateOnline      = 1
    AppRotateBytes       = 10485760
    AppRotateSeconds     = 86400
    AppExit              = 'Default Restart'
    AppRestartDelay      = 15000
    AppThrottle          = 10000
    AppStopMethodConsole = 60000
    AppStopMethodWindow  = 5000
    AppStopMethodThreads = 5000
    AppEnvironmentExtra  = 'PYTHONUNBUFFERED=1'
}
if ($dependency) { $settings['DependOnService'] = $dependency }

foreach ($name in $settings.Keys) {
    $value = $settings[$name]
    Invoke-Step "  nssm set $name" {
        if ($name -eq 'AppExit') { & $nssmExe set $ServiceName AppExit Default Restart | Out-Null }
        else { & $nssmExe set $ServiceName $name $value | Out-Null }
        if ($LASTEXITCODE -ne 0) { throw "nssm set $name failed" }
    }
}
Invoke-Step 'Configuring service recovery (restart after 1 min, three times, daily reset)' {
    & sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
}
Invoke-Step 'Creating logs directory' { New-Item -ItemType Directory -Force $logs | Out-Null }

# ---- nightly backup task ------------------------------------------------------------------
if (-not $SkipBackupTask) {
    Invoke-Step "Registering scheduled task '$BackupTaskName' daily at $BackupTime (SYSTEM)" {
        $script = Join-Path $root 'scripts\backup-local.ps1'
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $root
        $trigger = New-ScheduledTaskTrigger -Daily -At $BackupTime
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
            -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $BackupTaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $taskSettings -Force | Out-Null
    }
}

if ($ApplyPower) {
    Invoke-Step 'Applying always-on power settings (scripts\power.ps1 -Apply)' {
        & (Join-Path $PSScriptRoot 'power.ps1') -Apply
    }
}

if ($DryRun) {
    Write-Host 'Dry run complete; nothing was changed.'
} elseif ($Start) {
    Invoke-Step "Starting $ServiceName" {
        & $nssmExe start $ServiceName | Out-Null
        Start-Sleep -Seconds 5
        Get-Service $ServiceName | Format-Table Name, Status, StartType -AutoSize | Out-String | Write-Host
    }
} else {
    Write-Host "Installed. Start it with:  nssm start $ServiceName   (or re-run with -Start)"
}

try { Stop-Transcript | Out-Null } catch { }
