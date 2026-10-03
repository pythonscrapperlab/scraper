<#
.SYNOPSIS
    Nightly local pg_dump of aevorex_db (custom format), verified, with retention.

.DESCRIPTION
    - Reads DB_* settings and HEALTHCHECKS_BACKUP_URL from .env (values are never printed).
    - Writes <Destination>\aevorex_db_YYYYMMDD_HHMMSS.dump via a .partial file, verifies it with
      pg_restore --list, then renames it. A failed dump never replaces a good one.
    - Keeps the newest -Keep dumps named aevorex_db_*.dump; other files (the old manual dumps in
      backups\) are never touched.
    - Refuses to start unless the destination drive has room for twice the last dump plus 1 GiB.
    - -DryRun prints what would happen and writes nothing.

    This is a same-machine backup. It protects against a bad migration or a deleted table, NOT
    against losing the laptop; see docs/runbook.md for copying dumps off the machine.
#>
[CmdletBinding()]
param(
    [string]$Destination,
    [int]$Keep = 0,
    [string]$PgBin,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$root = Get-RepoRoot
$envFile = Read-DotEnv (Join-Path $root '.env')
$logPath = Join-Path $root 'logs\backup.jsonl'
$hcUrl = Get-EnvValue $envFile 'HEALTHCHECKS_BACKUP_URL'

if (-not $Destination) { $Destination = Get-EnvValue $envFile 'BACKUP_DIR' 'D:\aevorex-backups' }
if ($Keep -le 0) { $Keep = [int](Get-EnvValue $envFile 'BACKUP_KEEP' '7') }

function Find-PgTool([string]$Name) {
    if ($PgBin) { return (Join-Path $PgBin $Name) }
    $found = Get-ChildItem 'C:\Program Files\PostgreSQL\*\bin' -Filter $Name -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Cannot find $Name. Pass -PgBin 'C:\Program Files\PostgreSQL\17\bin'."
}

$started = Get-Date
$stamp = $started.ToString('yyyyMMdd_HHmmss')
$partial = Join-Path $Destination "aevorex_db_$stamp.dump.partial"
$final = Join-Path $Destination "aevorex_db_$stamp.dump"
$status = 'failed'
$errorClass = ''
$bytes = 0
$deleted = 0

try {
    $pgDump = Find-PgTool 'pg_dump.exe'
    $pgRestore = Find-PgTool 'pg_restore.exe'
    $dbHost = Get-EnvValue $envFile 'DB_HOST' 'localhost'
    $dbPort = Get-EnvValue $envFile 'DB_PORT' '5432'
    $dbName = Get-EnvValue $envFile 'DB_NAME' 'aevorex_db'
    $dbUser = Get-EnvValue $envFile 'DB_USER' 'aevorex'
    $dbPassword = Get-EnvValue $envFile 'DB_PASSWORD'
    if (-not $dbPassword) { throw 'DB_PASSWORD is not set in .env' }

    if (-not (Test-Path -LiteralPath $Destination)) {
        if ($DryRun) { Write-Host "[dry-run] would create $Destination" }
        else { New-Item -ItemType Directory -Force $Destination | Out-Null }
    }

    $existing = @()
    if (Test-Path -LiteralPath $Destination) {
        $existing = @(Get-ChildItem -LiteralPath $Destination -Filter 'aevorex_db_*.dump' |
            Sort-Object LastWriteTime -Descending)
    }
    $lastSize = 0
    if ($existing.Count -gt 0) { $lastSize = $existing[0].Length }
    $driveLetter = (Split-Path -Qualifier $Destination).TrimEnd(':')
    $free = (Get-PSDrive -Name $driveLetter).Free
    $needed = [Math]::Max(2 * $lastSize, 0) + 1GB
    Write-Host ("Destination {0}: {1:N1} GiB free, {2:N1} GiB required" -f $Destination, ($free / 1GB), ($needed / 1GB))
    if ($free -lt $needed) { throw 'InsufficientDiskSpace' }

    if ($DryRun) {
        Write-Host "[dry-run] would run: $pgDump -Fc -Z 6 -h $dbHost -p $dbPort -U $dbUser -d $dbName -f $final"
        Write-Host "[dry-run] would keep newest $Keep dumps; currently $($existing.Count) present"
        $status = 'dry-run'
        return
    }

    Send-Ping $hcUrl '/start'
    $env:PGPASSWORD = $dbPassword
    try {
        & $pgDump -Fc -Z 6 -h $dbHost -p $dbPort -U $dbUser -d $dbName -f $partial
        if ($LASTEXITCODE -ne 0) { throw "pg_dump exited with $LASTEXITCODE" }
        & $pgRestore --list $partial | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "pg_restore --list exited with $LASTEXITCODE" }
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
    Move-Item -LiteralPath $partial -Destination $final
    $bytes = (Get-Item -LiteralPath $final).Length

    $dumps = @(Get-ChildItem -LiteralPath $Destination -Filter 'aevorex_db_*.dump' |
        Sort-Object LastWriteTime -Descending)
    foreach ($old in ($dumps | Select-Object -Skip $Keep)) {
        Remove-Item -LiteralPath $old.FullName -Force
        $deleted++
    }
    $status = 'ok'
    Write-Host ("Backup ok: {0} ({1:N1} MiB); removed {2} old dump(s)" -f $final, ($bytes / 1MB), $deleted)
} catch {
    $errorClass = $_.Exception.GetType().Name
    if ($_.Exception.Message -eq 'InsufficientDiskSpace') { $errorClass = 'InsufficientDiskSpace' }
    # Message text can include paths or server output; log the class only.
    Write-Error "Backup failed ($errorClass)"
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue }
} finally {
    if ($status -ne 'dry-run') {
        Write-JsonLog $logPath @{
            event = 'backup'; status = $status; error_class = $errorClass; bytes = $bytes
            seconds = [int]((Get-Date) - $started).TotalSeconds; kept = $Keep; deleted = $deleted
        }
        if ($status -eq 'ok') { Send-Ping $hcUrl '' } else { Send-Ping $hcUrl '/fail' }
    }
}
if ($status -eq 'failed') { exit 1 }
