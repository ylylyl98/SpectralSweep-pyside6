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
    $destinations = if ($DestinationDirectory) {
        @($DestinationDirectory)
    } else {
        @([Environment]::GetFolderPath('DesktopDirectory'), [Environment]::GetFolderPath('Programs'))
    }
    Add-Type -Path (Join-Path $PSScriptRoot 'windows_shortcut.cs')
    $target = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $launcher + '"'
    $appId = [SpectralSweep.SourceShortcut]::ApplicationId($projectRoot)
    $failed = $false
    foreach ($directory in $destinations) {
        try {
            if (-not $directory -or -not (Test-Path -LiteralPath $directory -PathType Container)) {
                throw "Shortcut destination does not exist: $directory"
            }
            $shortcutPath = Join-Path $directory 'SpectralSweep (Source).lnk'
            [SpectralSweep.SourceShortcut]::Save($shortcutPath, $target, $arguments, $projectRoot, $icon, $appId)
            if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
                throw "Windows did not save the shortcut: $shortcutPath"
            }
            Write-Host "Created: $shortcutPath"
        } catch {
            Write-Warning "Could not save shortcut: $($_.Exception.Message)"
            $failed = $true
        }
    }
    if ($failed) { exit 1 }
    Write-Host 'Double-click SpectralSweep (Source) to check for updates and start the app.'
    Write-Host 'To pin it, right-click SpectralSweep (Source) in Start or right-click the running app on the taskbar.'
    Write-Host 'Run launch.bat again if you move the project folder.'
    exit 0
} catch {
    Write-Host "Could not create shortcut: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
