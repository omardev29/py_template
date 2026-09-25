#!/usr/bin/env pwsh
# ./deploy launcher for PowerShell 7+ (any OS) and Windows PowerShell 5.1.
# Finds the project root and uv and hands every argument to
# .pytemplate/deploy.py, where all the logic lives. Rules (CLAUDE.md, "Launchers"):
#   * ASCII only, LF endings, no BOM: xonsh and Unix kernels read the shebang.
#   * No param() block: it would turn -v, -h, -q into PowerShell parameters.
#   * Never touch $env:PATH and restore every variable set here: a .ps1 runs
#     inside the caller's session, so anything it changes stays there.
#   * PowerShell < 7.3 (and $PSNativeCommandArgumentPassing = 'Legacy') drops
#     empty arguments and mangles embedded quotes: arguments are pre-quoted.
#   * PowerShell itself removes a bare -- before any script sees it (5.1 and
#     7.x alike): quote it ('--') or use .\deploy.cmd.
# Blocked by the execution policy? Use .\deploy.cmd, or run once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# A copy extracted from a downloaded zip also needs: Unblock-File .\deploy.ps1
# Exit codes: 2 = no project found, 127 = no uv, 126 = uv would not start,
# anything else = the runner's.

# The caller's strict mode and preferences reach this script's scope. Reset them
# here (this scope only): with 'Stop', PowerShell 7.3+ turns a runner exit code
# into an error and 5.1 turns redirected runner stderr (2>&1) into one. No
# progress records either (5.1 shows one while it loads modules the first time).
Set-StrictMode -Off
$ErrorActionPreference = 'Continue'
$PSNativeCommandUseErrorActionPreference = $false
$ProgressPreference = 'SilentlyContinue'

$onWindows = $env:OS -eq 'Windows_NT'
$uvExe = if ($onWindows) { 'uv.exe' } else { 'uv' }

function Find-UvIn([object[]] $Dirs) {
    foreach ($d in $Dirs) {
        if (-not $d) { continue }
        try { $p = [IO.Path]::Combine(([string]$d).Trim().Trim('"'), $uvExe) } catch { continue }
        if ([IO.File]::Exists($p)) { return $p }
    }
    return $null
}

function Find-Uv {
    if ($env:UV -and [IO.File]::Exists($env:UV)) { return $env:UV }
    # Only a real executable: a uv.cmd or uv.ps1 wrapper would parse the arguments again.
    $cmd = Get-Command uv -CommandType Application -ErrorAction Ignore |
        Where-Object { -not $onWindows -or $_.Extension -eq '.exe' } | Select-Object -First 1
    if ($cmd) { return $cmd.Path }
    $h = if ($onWindows -and $env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    $dirs = @(
        $env:UV_INSTALL_DIR
        $(if ($env:UV_INSTALL_DIR) { [IO.Path]::Combine($env:UV_INSTALL_DIR, 'bin') })
        $env:XDG_BIN_HOME
        $(if ($env:XDG_DATA_HOME) { [IO.Path]::Combine($env:XDG_DATA_HOME, '..', 'bin') })
        $(if ($h) { [IO.Path]::Combine($h, '.local', 'bin') })
        $(if ($env:CARGO_HOME) { [IO.Path]::Combine($env:CARGO_HOME, 'bin') })
        $(if ($h) { [IO.Path]::Combine($h, '.cargo', 'bin') })
    )
    if ($onWindows) {
        $lad = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } elseif ($h) { [IO.Path]::Combine($h, 'AppData', 'Local') }
        if ($lad) {
            $dirs += [IO.Path]::Combine($lad, 'Microsoft', 'WinGet', 'Links')
            $dirs += @(Get-ChildItem -LiteralPath ([IO.Path]::Combine($lad, 'Microsoft', 'WinGet', 'Packages')) -Filter 'astral-sh.uv_*' -Directory -ErrorAction Ignore | ForEach-Object FullName)
        }
        if ($env:ProgramFiles) {
            $dirs += [IO.Path]::Combine($env:ProgramFiles, 'WinGet', 'Links')
            $dirs += @(Get-ChildItem -LiteralPath ([IO.Path]::Combine($env:ProgramFiles, 'WinGet', 'Packages')) -Filter 'astral-sh.uv_*' -Directory -ErrorAction Ignore | ForEach-Object FullName)
        }
        if ($env:SCOOP) { $dirs += [IO.Path]::Combine($env:SCOOP, 'shims') }
        if ($h) { $dirs += [IO.Path]::Combine($h, 'scoop', 'shims') }
        if ($env:SCOOP_GLOBAL) { $dirs += [IO.Path]::Combine($env:SCOOP_GLOBAL, 'shims') }
        if ($env:ProgramData) { $dirs += [IO.Path]::Combine($env:ProgramData, 'scoop', 'shims') }
        if ($env:ChocolateyInstall) { $dirs += [IO.Path]::Combine($env:ChocolateyInstall, 'bin') }
        if ($env:ProgramData) { $dirs += [IO.Path]::Combine($env:ProgramData, 'chocolatey', 'bin') }
        # The PATH stored in the registry (a console opened before uv was installed
        # still has the old one). GetEnvironmentVariable expands %VARS% itself.
        $dirs += @([Environment]::GetEnvironmentVariable('Path', 'User') -split ';')
        $dirs += @([Environment]::GetEnvironmentVariable('Path', 'Machine') -split ';')
    } else {
        $dirs += '/opt/homebrew/bin', '/usr/local/bin', '/home/linuxbrew/.linuxbrew/bin'
        if ($h) { $dirs += [IO.Path]::Combine($h, '.nix-profile', 'bin') }
    }
    return Find-UvIn $dirs
}

# --- project root: this file's folder, else walk up from the current location.
$root = $PSScriptRoot
if (-not ($root -and [IO.File]::Exists([IO.Path]::Combine($root, '.pytemplate', 'deploy.py')))) {
    $root = $null
    $loc = Get-Location
    $dir = if ($loc.Provider.Name -eq 'FileSystem') { $loc.ProviderPath } else { [Environment]::CurrentDirectory }
    while ($dir) {
        if ([IO.File]::Exists([IO.Path]::Combine($dir, '.pytemplate', 'deploy.py'))) { $root = $dir; break }
        $parent = [IO.Path]::GetDirectoryName($dir)
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }
    if (-not $root) {
        [Console]::Error.WriteLine('deploy: no .pytemplate/deploy.py next to this launcher, in the current folder or in any parent folder.')
        exit 2
    }
}

# --- uv
$uv = Find-Uv
if (-not $uv) {
    [Console]::Error.WriteLine('deploy: uv not found (https://docs.astral.sh/uv/getting-started/installation/).')
    $interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and -not $env:CI
    if ($interactive) {
        $answer = try { Read-Host 'Install it now with the official installer? [y/N]' } catch { '' }
        if ($answer -match '^(y|yes)$') {
            if ($onWindows) {
                & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
            } else {
                sh -c 'if command -v curl >/dev/null 2>&1; then curl -LsSf https://astral.sh/uv/install.sh | sh; else wget -qO- https://astral.sh/uv/install.sh | sh; fi'
            }
            $uv = Find-Uv
        }
    }
    if (-not $uv) {
        $hints = if ($onWindows) {
            '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"', '  winget install --id=astral-sh.uv -e', '  scoop install main/uv'
        } else {
            '  curl -LsSf https://astral.sh/uv/install.sh | sh', '  brew install uv', '  pipx install uv'
        }
        [Console]::Error.WriteLine('Install it with one of these, then open a new terminal:')
        foreach ($line in $hints) { [Console]::Error.WriteLine($line) }
        exit 127
    }
}

# --- arguments: flatten `a,b` arrays; pre-quote for legacy native passing
$argv = @(foreach ($a in $args) { foreach ($x in @($a)) { [string]$x } })
$v = $PSVersionTable.PSVersion
$legacy = $v.Major -lt 7 -or ($v.Major -eq 7 -and $v.Minor -lt 3)
if (-not $legacy) { $legacy = (Get-Variable PSNativeCommandArgumentPassing -ValueOnly -ErrorAction Ignore) -eq 'Legacy' }
if ($legacy) {
    # Legacy passing wraps an argument in quotes once it sees a blank after an even
    # number of `"`. Windows PowerShell counts every `"`, so a quote is written `""`
    # (uv reads `""` inside quotes as one `"`); PowerShell 7 skips `\"` when counting.
    $quote = if ($PSVersionTable.PSEdition -eq 'Desktop') { '""' } else { '\"' }
    $argv = @(foreach ($a in $argv) { '"' + (($a -replace '(\\*)"', ('$1$1' + $quote)) -replace '(\\+)$', '$1$1') + '"' })
}

# --- hand over, restoring the caller's environment afterwards
$loc = Get-Location
$names = 'PYTEMPLATE_CALLER_CWD', 'PYTEMPLATE_LAUNCHER'
$saved = @{}
foreach ($n in $names) { $saved[$n] = [Environment]::GetEnvironmentVariable($n) }
$code = 1
try {
    $env:PYTEMPLATE_CALLER_CWD = if ($loc.Provider.Name -eq 'FileSystem') { $loc.ProviderPath } else { [Environment]::CurrentDirectory }
    $env:PYTEMPLATE_LAUNCHER = "ps1:$($PSVersionTable.PSEdition):$($v.Major).$($v.Minor)"
    # Linux/macOS: PowerShell globs native arguments that come from a variable ('*' would
    # reach uv as the file list) unless the current location is outside the FileSystem
    # provider; uv still starts in the caller's folder (the FileSystem location)
    if ($IsLinux -or $IsMacOS) { Push-Location -LiteralPath 'Function:\' -StackName pytemplate }
    & $uv run --quiet --script ([IO.Path]::Combine($root, '.pytemplate', 'deploy.py')) @argv
    $code = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("deploy: cannot run ${uv}: $_")
    $code = 126
} finally {
    if ($IsLinux -or $IsMacOS) { Pop-Location -StackName pytemplate -ErrorAction Ignore }
    # Not SetEnvironmentVariable($n, $null): PowerShell passes $null to a .NET string
    # parameter as '', and PowerShell 7 then keeps an empty variable.
    foreach ($n in $names) {
        if ($null -eq $saved[$n]) { Remove-Item -LiteralPath "Env:$n" -ErrorAction Ignore }
        else { [Environment]::SetEnvironmentVariable($n, $saved[$n]) }
    }
}
exit $code
