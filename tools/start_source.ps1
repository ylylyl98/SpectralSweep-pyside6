[CmdletBinding()]
param(
    [switch]$Mock,
    [switch]$WatchStyles,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArguments = @()
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
try {
    Set-Location -LiteralPath $projectRoot
    # Dependency-only and isolated preview commands retain their existing behavior.
    $maintenance = @($AppArguments | Where-Object { $_ -match '^--(install|update-deps|preview.*|smoke-test|help)$' }).Count -gt 0
    if (-not $maintenance) {
        & $powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'create_source_shortcut.ps1')
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'Desktop shortcut could not be saved. Continuing startup.' -ForegroundColor Yellow
        }
        Write-Host 'SpectralSweep (Source) - checking main for updates...'
        & $powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'update_source.ps1')
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'Not updated. Starting the existing local version.' -ForegroundColor Yellow
        }
    }
    Write-Host 'Starting SpectralSweep from:' $projectRoot
    $forwardArguments = @($AppArguments)
    if ($Mock) { $forwardArguments += '--mock' }
    if ($WatchStyles) { $forwardArguments += '--watch-styles' }
    # This child uses launch.bat's existing venv/bootstrap path without recursing.
    $env:SPECTRAL_SOURCE_STARTING = '1'
    & (Join-Path $projectRoot 'launch.bat') @forwardArguments
    exit $LASTEXITCODE
} catch {
    Write-Host "Could not start SpectralSweep: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}
