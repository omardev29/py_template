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
#   * Never write the name of PowerShell's automatic pipeline variable in this
#     file: pwsh -File (and the shebang route) would then read a redirected
#     stdin as text lines and change its bytes. It is read by name instead.
#   * pwsh -File deploy.ps1, and ./deploy.ps1 typed in bash or zsh, split every
#     argument that starts with - at its first colon before this script runs
#     (-X:v arrives as -X v): from POSIX shells use ./deploy.
# Blocked by the execution policy? Use .\deploy.cmd, or run once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# A copy extracted from a downloaded zip also needs: Unblock-File .\deploy.ps1
# ConstrainedLanguage mode (AppLocker/WDAC policies) cannot run it: use .\deploy.cmd.
# Exit codes: 2 = no project found, 127 = no uv, 126 = uv would not start (or
# ConstrainedLanguage mode), anything else = the runner's.

# The caller's strict mode and preferences reach this script's scope. Reset them
# here (this scope only): with 'Stop', PowerShell 7.3+ turns a runner exit code
# into an error and 5.1 turns redirected runner stderr (2>&1) into one. No
# progress records either (5.1 shows one while it loads modules the first time).
Set-StrictMode -Off
$ErrorActionPreference = 'Continue'
$PSNativeCommandUseErrorActionPreference = $false
$ProgressPreference = 'SilentlyContinue'

# ConstrainedLanguage mode blocks the .NET calls below (and [Console] too): one clear line
# through a cmdlet instead of a cascade of errors.
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    Write-Error -Category PermissionDenied -Message ('deploy: PowerShell runs deploy.ps1 in ' + $ExecutionContext.SessionState.LanguageMode + ' mode (an AppLocker/WDAC policy), which cannot start uv: use .\deploy.cmd')
    exit 126
}

$onWindows = $env:OS -eq 'Windows_NT'
$uvExe = if ($onWindows) { 'uv.exe' } else { 'uv' }

# A uv that can run. On Linux/macOS it also needs an x bit, like `test -x` in ./deploy
# (Get-Command and File.Exists accept a uv left without one by a broken download).
# GetUnixFileMode needs .NET 7 (PowerShell 7.3+): older versions skip the mode check.
function Test-Uv([string] $Path) {
    if (-not $Path -or -not [IO.File]::Exists($Path)) { return $false }
    if ($onWindows) { return $true }
    try { return ([int][IO.File]::GetUnixFileMode($Path) -band 73) -ne 0 } catch { return $true }
}

function Find-UvIn([object[]] $Dirs) {
    foreach ($d in $Dirs) {
        if (-not $d) { continue }
        try { $p = [IO.Path]::Combine(([string]$d).Trim().Trim('"'), $uvExe) } catch { continue }
        if (Test-Uv $p) { return $p }
    }
    return $null
}

function Find-Uv {
    if (Test-Uv $env:UV) { return $env:UV }
    # Only a real executable: a uv.cmd or uv.ps1 wrapper would parse the arguments again.
    $cmd = Get-Command uv -CommandType Application -All -ErrorAction Ignore |
        Where-Object { (-not $onWindows -or $_.Extension -eq '.exe') -and (Test-Uv $_.Path) } | Select-Object -First 1
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

# --- arguments: keep typed a,b lists and -X:v whole, pre-quote for legacy passing.
# PowerShell hands a script a typed list (cpython,mypyc) as one array, and a typed -X:v as two
# elements: '-X:' (marked with the parameter name) and v. Join them again, as PowerShell does
# for a native program (a list becomes one argument, its items joined with commas).
$argv = @(for ($i = 0; $i -lt $args.Count; $i++) {
    $a = $args[$i]
    if ($a -is [string] -and $a.EndsWith(':') -and $a.PSObject.Properties['<CommandParameterName>'] -and $i + 1 -lt $args.Count) {
        $i++
        $a + ((@($args[$i]) | ForEach-Object { [string]$_ }) -join ',')
    } elseif ($a -is [array]) {
        (@($a) | ForEach-Object { [string]$_ }) -join ','
    } else {
        [string]$a
    }
})
$v = $PSVersionTable.PSVersion
$legacy = $v.Major -lt 7 -or ($v.Major -eq 7 -and $v.Minor -lt 3)
if (-not $legacy) { $legacy = (Get-Variable PSNativeCommandArgumentPassing -ValueOnly -ErrorAction Ignore) -eq 'Legacy' }
# PowerShell 7.3+ takes any native argument equal to --% (quoted or splatted) for its
# stop-parsing token: it drops it, then splits and %VAR%-expands the rest. Pre-quoted by
# legacy passing (set in this script's scope only) it reaches uv intact.
if (-not $legacy -and $argv -contains '--%') { $PSNativeCommandArgumentPassing = 'Legacy'; $legacy = $true }
if ($legacy) {
    # Legacy passing wraps an argument in quotes once it sees a blank after an even
    # number of `"`. Windows PowerShell counts every `"`, so a quote is written `""`
    # (uv reads `""` inside quotes as one `"`); PowerShell 7 skips `\"` when counting.
    $quote = if ($PSVersionTable.PSEdition -eq 'Desktop') { '""' } else { '\"' }
    $argv = @(foreach ($a in $argv) { '"' + (($a -replace '(\\*)"', ('$1$1' + $quote)) -replace '(\\+)$', '$1$1') + '"' })
}

# Pipeline input ('x' | ./deploy.ps1 run) goes to uv's stdin, as with a direct native call;
# without it uv keeps this process's stdin. Read by name (see the header).
$fromPipe = [bool]$MyInvocation.ExpectingInput
if ($fromPipe) { $pipeIn = $ExecutionContext.SessionState.PSVariable.GetValue('input') }

# --- hand over, restoring the caller's environment afterwards
$loc = Get-Location
$names = 'PYTEMPLATE_CALLER_CWD', 'PYTEMPLATE_LAUNCHER', 'UV_PYTHON', 'PYTHONHOME', 'PYTHONPATH', 'UV_WORKING_DIR'
$saved = @{}
foreach ($n in $names) { $saved[$n] = [Environment]::GetEnvironmentVariable($n) }
$code = 1
try {
    $env:PYTEMPLATE_CALLER_CWD = if ($loc.Provider.Name -eq 'FileSystem') { $loc.ProviderPath } else { [Environment]::CurrentDirectory }
    $env:PYTEMPLATE_LAUNCHER = "ps1:$($PSVersionTable.PSEdition):$($v.Major).$($v.Minor)"
    # The runner runs on the project's Python (.python-version next to it), in the caller's
    # folder: a UV_PYTHON of the caller must not choose that Python, a PYTHONHOME or PYTHONPATH
    # must not break it, a UV_WORKING_DIR must not move it (its own tools never get them either).
    Remove-Item -LiteralPath Env:UV_PYTHON, Env:PYTHONHOME, Env:PYTHONPATH, Env:UV_WORKING_DIR -ErrorAction Ignore
    $entry = [IO.Path]::Combine($root, '.pytemplate', 'deploy.py')
    if ($PSVersionTable.PSEdition -eq 'Core') {
        # PowerShell 7 rewrites native arguments that are not quoted literals, splatted ones
        # included: it globs '*' (Linux/macOS) and expands '~', '~/x' ('~\x' on Windows). Run
        # the call rebuilt from single-quoted words so argv reaches uv untouched.
        $q = [Management.Automation.Language.CodeGeneration]
        $words = foreach ($a in @($uv, 'run', '--quiet', '--script', $entry) + $argv) { "'" + $q::EscapeSingleQuotedStringContent($a) + "'" }
        $call = '& ' + ($words -join ' ')
        if ($fromPipe) { $call = '$pipeIn | ' + $call }
        Invoke-Expression $call
    } elseif ($fromPipe) {
        $pipeIn | & $uv run --quiet --script $entry @argv
    } else {
        & $uv run --quiet --script $entry @argv
    }
    $code = $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("deploy: cannot run ${uv}: $_")
    $code = 126
} finally {
    # Not SetEnvironmentVariable($n, $null): PowerShell passes $null to a .NET string
    # parameter as '', and PowerShell 7 then keeps an empty variable.
    foreach ($n in $names) {
        if ($null -eq $saved[$n]) { Remove-Item -LiteralPath "Env:$n" -ErrorAction Ignore }
        else { [Environment]::SetEnvironmentVariable($n, $saved[$n]) }
    }
}
exit $code
