# Update only a clean main branch from its configured main upstream.
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\', '/')
Set-Location -LiteralPath $root

function Stop-Update([string]$Message) {
    Write-Host "Update stopped: $Message" -ForegroundColor Yellow
    exit 1
}
function Git-Text([string[]]$GitArgs) {
    $result = & git @GitArgs 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($result -join "`n").Trim()
}
function Assert-Safe {
    $status = Git-Text @('status', '--porcelain=v1', '--untracked-files=all')
    if ($null -eq $status) { Stop-Update 'Could not inspect the working tree.' }
    if ($status -ne '') { Stop-Update 'Working tree contains staged, unstaged, or untracked files. Preserve and resolve them before updating.' }
    $gitDir = Git-Text @('rev-parse', '--absolute-git-dir')
    if (-not $gitDir) { Stop-Update 'Could not locate the Git directory.' }
    foreach ($name in @('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG', 'rebase-merge', 'rebase-apply', 'sequencer', 'index.lock', 'shallow.lock')) {
        if (Test-Path -LiteralPath (Join-Path $gitDir $name)) { Stop-Update "A Git operation may be in progress ($name)." }
    }
    # Only inspect likely launchers; never print command lines, which may contain secrets.
    try {
        $processes = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'SpectralSweep.exe' OR Name = 'cmd.exe'" -ErrorAction Stop
        foreach ($process in $processes) {
            $line = [string]$process.CommandLine
            $exe = [string]$process.ExecutablePath
            if (($exe -and $exe.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) -or
                ($line.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and $line -match '(?i)(launch\.bat|main\.py|launch_app\.py)') -or
                ($line -match '(?i)(?:^|\s)["'']?(?:\.[\\/])?(?:main\.py|launch_app\.py|launch\.bat)(?:["'']|\s|$)')) {
                Stop-Update 'A possible application or launcher process is running. Close it and retry.'
            }
        }
    } catch { Stop-Update 'Could not inspect running processes. Close the application and retry with process inspection available.' }
}

try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Stop-Update 'Git was not found on PATH.' }
    $top = Git-Text @('rev-parse', '--show-toplevel')
    if (-not $top -or ([IO.Path]::GetFullPath($top).TrimEnd('\','/') -ine [IO.Path]::GetFullPath($root).TrimEnd('\','/'))) {
        Stop-Update 'This script must be located in the root of its Git repository.'
    }
    $branch = Git-Text @('symbolic-ref', '--quiet', '--short', 'HEAD')
    if ($branch -ne 'main') { Stop-Update 'The current branch must be main; no branch was changed.' }
    $remote = Git-Text @('config', '--get', 'branch.main.remote')
    $merge = Git-Text @('config', '--get', 'branch.main.merge')
    if (-not $remote -or $remote -eq '.' -or $merge -ne 'refs/heads/main') {
        Stop-Update 'Configure main to track a remote main branch, for example: git branch --set-upstream-to=<remote>/main main'
    }
    $upstream = Git-Text @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}')
    if (-not $upstream) { Stop-Update 'The configured upstream is unavailable. Fetch it and configure main to track <remote>/main.' }
    if ($remote -notmatch '^[A-Za-z0-9._/-]+$') { Stop-Update 'The configured remote name cannot be used safely.' }
    Assert-Safe
    $originalHead = Git-Text @('rev-parse', 'HEAD')
    if (-not $originalHead) { Stop-Update 'Could not inspect the current commit.' }
    Write-Host "Fetching configured remote: $remote"
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = (Get-Command git -CommandType Application | Select-Object -First 1).Source
    $start.Arguments = "fetch --quiet -- `"$remote`""
    $start.WorkingDirectory = $root
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
    $start.EnvironmentVariables['GCM_INTERACTIVE'] = 'Never'
    $process = [Diagnostics.Process]::Start($start)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(30000)) {
        # Git may spawn credential helpers or transport children. Terminate only
        # this fetch process tree, then fall back to killing the parent itself.
        try {
            $killer = New-Object System.Diagnostics.ProcessStartInfo
            $killer.FileName = Join-Path $env:SystemRoot 'System32\taskkill.exe'
            $killer.Arguments = "/PID $($process.Id) /T /F"
            $killer.UseShellExecute = $false
            $killer.CreateNoWindow = $true
            $killer.RedirectStandardOutput = $true
            $killer.RedirectStandardError = $true
            $killProcess = [Diagnostics.Process]::Start($killer)
            if (-not $killProcess.WaitForExit(5000)) { $killProcess.Kill() }
        } catch { }
        try { if (-not $process.HasExited) { $process.Kill() } } catch { }
        Stop-Update 'Fetch timed out after 30 seconds. No merge was attempted.'
    }
    if ($process.ExitCode -ne 0) { Stop-Update 'Fetch failed without interactive authentication. No merge was attempted.' }
    Assert-Safe
    if ((Git-Text @('symbolic-ref', '--quiet', '--short', 'HEAD')) -ne 'main' -or
        (Git-Text @('rev-parse', 'HEAD')) -ne $originalHead -or
        (Git-Text @('config', '--get', 'branch.main.remote')) -ne $remote -or
        (Git-Text @('config', '--get', 'branch.main.merge')) -ne 'refs/heads/main' -or
        (Git-Text @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}')) -ne $upstream) {
        Stop-Update 'Branch, commit, or upstream changed during fetch. No merge was attempted.'
    }
    $counts = Git-Text @('rev-list', '--left-right', '--count', 'HEAD...@{upstream}')
    if ($counts -notmatch '^(\d+)\s+(\d+)$') { Stop-Update 'Could not compare main with its upstream.' }
    $ahead = [int]$Matches[1]; $behind = [int]$Matches[2]
    if ($ahead -gt 0) { Stop-Update "Local main has $ahead unpushed commit(s) and is $behind commit(s) behind. Resolve the history manually." }
    if ($behind -eq 0) { Write-Host 'main is already up to date.'; exit 0 }
    Write-Host "Fast-forwarding main by $behind commit(s)..."
    & git merge --ff-only '@{upstream}'
    if ($LASTEXITCODE -ne 0) { Stop-Update 'Fast-forward failed. Inspect Git status before retrying.' }
    Write-Host 'Update completed.'
    exit 0
} catch {
    Stop-Update "Unexpected error: $($_.Exception.Message)"
}
