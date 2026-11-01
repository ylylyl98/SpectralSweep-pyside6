param([Parameter(Mandatory=$true)][string]$Request)
$ErrorActionPreference = 'Stop'
$dir = [System.IO.Path]::GetDirectoryName($Request)
$log = Join-Path $dir 'install.log'
$result = Join-Path $dir 'result.json'
$req = $null
$armed = $false
function Write-Result([string]$status, [string]$detail) {
    @{ status = $status; detail = $detail; log = $log } | ConvertTo-Json -Compress | Set-Content -LiteralPath $result -Encoding UTF8
}
function Show-Failure([string]$detail) {
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show("$detail`n`nLog: $log", 'SpectralSweep update', 'OK', 'Error') | Out-Null
    } catch {}
}
function Has-Marker([string]$name) {
    $path = Join-Path $dir $name
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    return ((Get-Content -LiteralPath $path -Raw -Encoding UTF8).Trim() -ceq $req.token)
}
function Get-Sha256([string]$path) {
    $stream = [System.IO.File]::OpenRead($path)
    try {
        $hasher = [System.Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '') }
        finally { $hasher.Dispose() }
    } finally { $stream.Dispose() }
}
# Windows CommandLineToArgvW quoting, including trailing backslashes and quotes.
function Quote-Argument([string]$value) {
    $escaped = [regex]::Replace($value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}
try {
    $req = Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
    if (!$req.token -or !$req.parent_pid -or !$req.install_dir -or !$req.setup_file -or $req.sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'Invalid update request.' }
    if ((Get-Sha256 $req.setup_file) -ine $req.sha256) { throw 'Installer SHA-256 verification failed.' }
    Set-Content -LiteralPath (Join-Path $dir 'ready') -Value $req.token -Encoding UTF8 -NoNewline
    $deadline = [DateTime]::UtcNow.AddSeconds(600)
    while ($true) {
        if (Has-Marker 'cancel') { Write-Result 'cancelled' 'Window close was cancelled.'; exit 0 }
        if (Has-Marker 'arm') { $armed = $true; break }
        if ([DateTime]::UtcNow -ge $deadline) { Write-Result 'cancelled' 'Installation was not confirmed.'; exit 0 }
        Start-Sleep -Milliseconds 100
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while (Get-Process -Id ([int]$req.parent_pid) -ErrorAction SilentlyContinue) {
        if ([DateTime]::UtcNow -ge $deadline) { throw 'The application did not exit in time.' }
        Start-Sleep -Milliseconds 250
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while (Get-Process -Name 'SpectralSweep','SpectralSweep-worker' -ErrorAction SilentlyContinue) {
        if ([DateTime]::UtcNow -ge $deadline) { throw 'Another SpectralSweep process is still running.' }
        Start-Sleep -Milliseconds 250
    }
    if ((Get-Sha256 $req.setup_file) -ine $req.sha256) { throw 'Installer SHA-256 verification failed.' }
    $arguments = @('/SILENT', '/SP-', '/NORESTART', '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS', ('/DIR=' + (Quote-Argument $req.install_dir)), ('/LOG=' + (Quote-Argument $log)))
    $setup = Start-Process -FilePath $req.setup_file -ArgumentList $arguments -Wait -PassThru
    if ($setup.ExitCode -ne 0) { throw "Installer failed with exit code $($setup.ExitCode)." }
    $target = Join-Path $req.install_dir 'SpectralSweep.exe'
    if (!(Test-Path -LiteralPath $target -PathType Leaf)) { throw 'Installed application was not found after setup.' }
    $restart = @($req.restart_args | ForEach-Object { Quote-Argument ([string]$_) })
    if ($restart.Count -gt 0) { Start-Process -FilePath $target -ArgumentList $restart | Out-Null }
    else { Start-Process -FilePath $target | Out-Null }
    Write-Result 'success' 'Installation completed.'
} catch {
    $detail = $_.Exception.Message
    try { Write-Result 'failed' $detail } catch {}
    if ($armed) { Show-Failure $detail }
    exit 1
}
