[CmdletBinding()]
param(
    [string]$DestinationDirectory
)

$ErrorActionPreference = 'Stop'
try {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $launcher = Join-Path $PSScriptRoot 'start_source.ps1'
    $icon = Join-Path $projectRoot 'assets\icons\spectralsweep.ico'
    foreach ($required in @($launcher, $icon, (Join-Path $projectRoot 'launch.bat'))) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Project file is missing: $required. Extract or clone the complete project first."
        }
    }
    if (-not $DestinationDirectory) {
        $DestinationDirectory = [Environment]::GetFolderPath('DesktopDirectory')
    }
    if (-not (Test-Path -LiteralPath $DestinationDirectory -PathType Container)) {
        throw "Shortcut destination does not exist: $DestinationDirectory"
    }
    $shortcutPath = Join-Path $DestinationDirectory 'SpectralSweep (Source).lnk'
    Add-Type -Path (Join-Path $PSScriptRoot 'windows_shortcut.cs')
    $target = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $launcher + '"'
    [SpectralSweep.SourceShortcut]::Save($shortcutPath, $target, $arguments, $projectRoot, $icon)
    if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        throw 'Windows did not save the shortcut.'
    }
    Write-Host "Created: $shortcutPath"
    Write-Host 'Double-click SpectralSweep (Source) to check for updates and start the app.'
    Write-Host 'For one-click access, start the app, then right-click its taskbar icon and choose Pin to taskbar.'
    Write-Host 'Run launch.bat again if you move the project folder.'
    exit 0
} catch {
    Write-Host "Could not create shortcut: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
