# Shared helpers for the AevoraeX operations scripts. Dot-source this file; do not run it.
# Windows PowerShell 5.1 compatible. Never prints secret values.

Set-StrictMode -Version Latest

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Read-DotEnv {
    # Returns a hashtable of KEY -> value from .env. Values are never echoed by callers.
    param([Parameter(Mandatory)][string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $index = $trimmed.IndexOf('=')
        if ($index -lt 1) { continue }
        $key = $trimmed.Substring(0, $index).Trim()
        $value = $trimmed.Substring($index + 1).Trim()
        if ($value.Length -ge 2 -and (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key.ToUpperInvariant()] = $value
    }
    return $values
}

function Get-EnvValue {
    param([hashtable]$Settings, [string]$Key, [string]$Default = "")
    if ($Settings.ContainsKey($Key) -and $Settings[$Key] -ne "") { return $Settings[$Key] }
    return $Default
}

function Write-JsonLog {
    # One JSON object per line; callers pass only non-sensitive fields.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][hashtable]$Fields)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    $record = [ordered]@{ ts = (Get-Date).ToUniversalTime().ToString('o') }
    foreach ($key in $Fields.Keys) { $record[$key] = $Fields[$key] }
    ($record | ConvertTo-Json -Compress) | Add-Content -LiteralPath $Path -Encoding UTF8
}

function Send-Ping {
    # Healthchecks.io ping that can never fail the calling script.
    param([string]$BaseUrl, [string]$Suffix = '')
    if ([string]::IsNullOrWhiteSpace($BaseUrl)) { return }
    try {
        Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + $Suffix) -Method Get -TimeoutSec 10 | Out-Null
    } catch {
        Write-Warning 'Healthchecks ping failed (ignored).'
    }
}
