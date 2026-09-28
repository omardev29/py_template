#!/usr/bin/env pwsh
# ./pyt launcher for PowerShell 7+ (any OS) and Windows PowerShell 5.1.
# pytemplate-launcher: `pyt install` copies this file into uv's tool bin folder and `pyt uninstall` removes it.
# Finds the project root and uv and hands every argument to
# .pytemplate/pyt.py, where all the logic lives. Outside any project it runs the copy of the
# template that `pyt install` made, in its global mode (PYTEMPLATE_GLOBAL=1). Rules
# (CLAUDE.md, "Launchers"):
#   * ASCII only, LF endings, no BOM: xonsh and Unix kernels read the shebang.
#   * No param() block: it would turn -v, -h, -q into PowerShell parameters.
#   * Never touch $env:PATH and restore every variable set here: a .ps1 runs
#     inside the caller's session, so anything it changes stays there.
#   * PowerShell < 7.3 (and $PSNativeCommandArgumentPassing = 'Legacy') drops
#     empty arguments and mangles embedded quotes: arguments are pre-quoted.
#   * PowerShell itself removes a bare -- before any script sees it (5.1 and
#     7.x alike): quote it ('--') or use .\pyt.cmd.
#   * Never write the name of PowerShell's automatic pipeline variable in this
#     file: pwsh -File (and the shebang route) would then read a redirected
#     stdin as text lines and change its bytes. It is read by name instead.
#   * pwsh -File pyt.ps1, and ./pyt.ps1 typed in bash or zsh, split every
#     argument that starts with - at its first colon before this script runs
#     (-X:v arrives as -X v): from POSIX shells use ./pyt.
# Blocked by the execution policy? Use .\pyt.cmd, or run once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# A copy extracted from a downloaded zip also needs: Unblock-File .\pyt.ps1
# ConstrainedLanguage mode (AppLocker/WDAC policies) cannot run it: use .\pyt.cmd.
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
    Write-Error -Category PermissionDenied -Message ('pyt: PowerShell runs pyt.ps1 in ' + $ExecutionContext.SessionState.LanguageMode + ' mode (an AppLocker/WDAC policy), which cannot start uv: use .\pyt.cmd')
    exit 126
}

$onWindows = $env:OS -eq 'Windows_NT'
$uvExe = if ($onWindows) { 'uv.exe' } else { 'uv' }

# A uv that can run. On Linux/macOS it also needs an x bit, like `test -x` in ./pyt
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

# The runner a folder holds: .pytemplate/pyt.py, else .pytemplate/deploy.py (a project made
# before the launchers were renamed); $null when it holds neither.
function Get-Entry([string] $Dir) {
    if (-not $Dir) { return $null }
    foreach ($name in 'pyt.py', 'deploy.py') {
        $p = [IO.Path]::Combine($Dir, '.pytemplate', $name)
        if ([IO.File]::Exists($p)) { return $p }
    }
    return $null
}

# Whether another user owns this runner (POSIX: anyone may create /tmp/.pytemplate/pyt.py; on
# Windows, whose owners are not read here, a drive root, where any user may create folders).
# Its folder counts too, where the runner's package is imported from: a hard link keeps the
# owner of the file it links (on macOS any user may link a pyt.py of yours into a folder of theirs).
function Test-Foreign([string] $Entry, [bool] $Top) {
    if ($onWindows) { return $Top }
    & /bin/sh -c 'set -f; IFS=; [ -O "${1%/*}" ] && [ -O "$1" ]' sh $Entry
    return $LASTEXITCODE -ne 0
}

# --- project root: this file's folder, else walk up from the current location. A symlink to
# this file (~/bin/mypyt.ps1 -> proj/pyt.ps1) is followed to the launcher it names.
$root = $PSScriptRoot
$self = $PSCommandPath
for ($hops = 0; $self -and $hops -lt 40; $hops++) {
    $link = try {
        if ([IO.File]::GetAttributes($self) -band [IO.FileAttributes]::ReparsePoint) {
            $item = Get-Item -LiteralPath $self -Force -ErrorAction Stop
            if ($item.LinkType -eq 'SymbolicLink') { @($item.Target)[0] }
        }
    } catch { $null }
    if (-not $link) { break }
    $dir = [IO.Path]::GetDirectoryName($self)
    if (-not $onWindows -and -not [IO.Path]::IsPathRooted($link)) {
        # Relative to the PHYSICAL folder of the link (.NET folds '..' as text, the kernel does not).
        $dir = & /bin/sh -c 'CDPATH= cd -P -- "$1" 2>/dev/null && pwd -P' sh $dir
    }
    $self = [IO.Path]::Combine([string]$dir, $link)
    $root = [IO.Path]::GetDirectoryName($self)
}
$entry = Get-Entry $root
$globalMode = $false
if (-not $entry) {
    $root = $null
    $loc = Get-Location
    $dir = if ($loc.Provider.Name -eq 'FileSystem') { $loc.ProviderPath } else { [Environment]::CurrentDirectory }
    $other = $null
    $walked = $dir
    while ($dir) {
        $parent = [IO.Path]::GetDirectoryName($dir)
        $top = -not $parent -or $parent -eq $dir
        $candidate = Get-Entry $dir
        if ($candidate) {
            # Its code is not run when another user owns it (Test-Foreign)
            if (Test-Foreign $candidate $top) { $other = $candidate } else { $root = $dir; $entry = $candidate }
            break
        }
        if ($top) {
            # The location is logical: from a folder reached through a symlink into a project
            # (~/game-src -> ~/code/game/src) no logical parent holds it. Walk up again from the
            # physical folder, where the kernel is (POSIX only).
            $dir = $null
            if (-not $onWindows -and $walked) {
                $physical = (& /bin/sh -c 'CDPATH= cd -P -- "$1" 2>/dev/null && pwd -P' sh $walked) -join "`n"
                if ($physical -and $physical -ne $walked) { $dir = $physical }
                $walked = $null
            }
            continue
        }
        $dir = $parent
    }
    if (-not $root -and -not $other) {
        # No project: the copy of the template that `pyt install` made (the runner's
        # cmd_install.snapshot_dir), in its global mode, under the same ownership rule.
        $data = if ($onWindows) {
            if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } elseif ($env:USERPROFILE) { [IO.Path]::Combine($env:USERPROFILE, 'AppData', 'Local') }
        } elseif ($env:XDG_DATA_HOME -and $env:XDG_DATA_HOME.StartsWith('/')) {
            $env:XDG_DATA_HOME
        } elseif ($env:HOME -and $env:HOME.StartsWith('/')) {
            [IO.Path]::Combine($env:HOME, '.local', 'share')
        }
        if ($data) {
            $snapshot = [IO.Path]::Combine($data, 'pytemplate', 'template')
            $candidate = [IO.Path]::Combine($snapshot, '.pytemplate', 'pyt.py')
            if ([IO.File]::Exists($candidate)) {
                if (Test-Foreign $candidate $false) { $other = $candidate } else { $root = $snapshot; $entry = $candidate; $globalMode = $true }
            }
        }
    }
    if ($other) {
        $own = [IO.Path]::GetFileNameWithoutExtension($other) + '.ps1'
        [Console]::Error.WriteLine("pyt: $other is not yours (another user owns it or its folder, or it is at a drive root): not run. If you trust it, run $([IO.Path]::Combine([IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($other)), $own)) yourself.")
        exit 2
    }
    if (-not $root) {
        [Console]::Error.WriteLine('pyt: no .pytemplate/pyt.py next to this launcher, in the current folder or in any parent folder.')
        [Console]::Error.WriteLine('To run pyt outside a project, install it: ./pyt install in a clone of the template (https://github.com/omardev29/py_template).')
        exit 2
    }
}

# --- uv
$uv = Find-Uv
if (-not $uv) {
    [Console]::Error.WriteLine('pyt: uv not found (https://docs.astral.sh/uv/getting-started/installation/).')
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

# --- arguments: pass them as PowerShell does to a native program, pre-quote for legacy passing.
# A script gets a typed list (cpython,mypyc) and an array value ($files) alike, as one array; a
# native program gets the list as ONE argument, its items joined with commas, and the value's
# items as separate arguments. Only the text of the call tells them apart: Get-Typed reads it
# (a list is an ArrayLiteralAst) and returns, per argument that frame $K of $Stack received,
# $true when it was typed as a list, else $false; a splatted @args is read where that caller
# was called, and any other splatted variable is one 'gap' of unknown length (its items are
# values, as for a native call). $null when the text cannot be read or two gaps make the words
# not line up: then an array counts as a typed list. A typed -X:v arrives as two
# elements, '-X:' (marked with the parameter name) and v.
function Get-Typed([object[]] $Stack, [int] $K) {
    try {
        $flags = @()
        $at = [Management.Automation.InvocationInfo].GetProperty('ScriptPosition', [Reflection.BindingFlags]'NonPublic,Instance').GetValue($Stack[$K].InvocationInfo)
        $cmd = [Management.Automation.Language.Parser]::ParseInput($at.Text, [ref]$null, [ref]$null).Find({ param($n) $n -is [Management.Automation.Language.CommandAst] }, $false)
        if (-not $cmd) { return $null }
        $els = $cmd.CommandElements
        for ($j = 1; $j -lt $els.Count; $j++) {
            $el = $els[$j]
            if ($el -is [Management.Automation.Language.CommandParameterAst]) {
                # -X:v gives two elements; a bare -- none (PowerShell drops it)
                if ($null -ne $el.Argument) { $flags += $false, ($el.Argument -is [Management.Automation.Language.ArrayLiteralAst]) }
                elseif ($el.ParameterName -ne '-') { $flags += $false }
            } elseif ($el -is [Management.Automation.Language.VariableExpressionAst] -and $el.Splatted) {
                $up = if ($el.VariablePath.UserPath -eq 'args' -and $K + 1 -lt $Stack.Count) { Get-Typed $Stack ($K + 1) }
                if ($null -eq $up) { $up = 'gap' }
                $flags += $up
            } else {
                $flags += $el -is [Management.Automation.Language.ArrayLiteralAst]
            }
        }
        return ,$flags
    } catch {
        return $null
    }
}
$typed = $null
foreach ($a in $args) { if ($a -is [array]) { $typed = Get-Typed @(Get-PSCallStack) 0; break } }
if ($null -ne $typed) {
    # A gap takes the arguments the others leave, and its arrays pass their items one by one,
    # as a native call splats them; with two gaps nothing lines up.
    $gaps = @(foreach ($t in $typed) { if ($t -is [string]) { $t } }).Count
    $fill = $args.Count - $typed.Count + 1
    if ($gaps -eq 1 -and $fill -ge 0) {
        $typed = @(foreach ($t in $typed) { if ($t -is [string]) { for ($n = 0; $n -lt $fill; $n++) { $false } } else { $t } })
    }
    if ($typed.Count -ne $args.Count -or $gaps -gt 1) { $typed = $null }
}
# A native program never gets a $null argument (an unset $env:X, an optional variable), the
# $null items of an array, nor a -X: whose value is $null; an empty string it does get.
$argv = @(for ($i = 0; $i -lt $args.Count; $i++) {
    $a = $args[$i]
    if ($null -eq $a) { continue }
    if ($a -is [string] -and $a.EndsWith(':') -and $a.PSObject.Properties['<CommandParameterName>'] -and $i + 1 -lt $args.Count) {
        $i++
        if ($null -eq $args[$i]) { continue }
        $a + ((@($args[$i]) | Where-Object { $null -ne $_ } | ForEach-Object { [string]$_ }) -join ',')
    } elseif ($a -is [array] -and ($null -eq $typed -or $typed[$i])) {
        (@($a) | ForEach-Object { [string]$_ }) -join ','
    } else {
        # (a List[string]'s null item is an empty string for a native program too)
        foreach ($x in @($a)) { if ($null -ne $x -or $a -isnot [array]) { [string]$x } }
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

# Pipeline input ('x' | ./pyt.ps1 run) goes to uv's stdin, as with a direct native call;
# without it uv keeps this process's stdin. Read by name (see the header).
$fromPipe = [bool]$MyInvocation.ExpectingInput
if ($fromPipe) { $pipeIn = $ExecutionContext.SessionState.PSVariable.GetValue('input') }

# The Python the runner starts on. While the project has an environment, the one uv reads from
# .python-version (python.cpython, which that environment was made with), as always: --python=
# with no value is no request, and only-managed keeps a system Python out (uv makes its cached
# environment of the runner again when one built it). Else any CPython 3.11 or newer that uv
# finds, a system one too (uv has none to download on Android or the BSDs); the runner moves the
# commands that need python.cpython onto it itself. On Linux/macOS the environment's python is
# a link to its base Python, which must exist too (.NET's Exists takes a dangling link for a
# file: resolved first; without ResolveLinkTarget, PowerShell < 7.2, the link counts).
$python = '>=3.11'
$preference = 'managed'
$venvs = if ($onWindows) { , '.venv\Scripts\python.exe' } else { '.venv-wsl/bin/python', '.venv/bin/python' }
foreach ($p in $venvs) {
    $f = [IO.FileInfo]::new([IO.Path]::Combine($root, $p))
    try { if ($f.LinkTarget) { $f = $f.ResolveLinkTarget($true) } } catch { $f = $null }
    if ($f -and $f.Exists) { $python = ''; $preference = 'only-managed'; break }
}

# --- hand over, restoring the caller's environment afterwards
$loc = Get-Location
$names = 'PYTEMPLATE_CALLER_CWD', 'PYTEMPLATE_LAUNCHER', 'PYTEMPLATE_GLOBAL', 'UV_PYTHON', 'UV_MANAGED_PYTHON', 'UV_NO_MANAGED_PYTHON', 'PYTHONHOME', 'PYTHONPATH', 'UV_WORKING_DIR'
$saved = @{}
foreach ($n in $names) { $saved[$n] = [Environment]::GetEnvironmentVariable($n) }
$code = 1
try {
    $env:PYTEMPLATE_CALLER_CWD = if ($loc.Provider.Name -eq 'FileSystem') { $loc.ProviderPath } else { [Environment]::CurrentDirectory }
    $env:PYTEMPLATE_LAUNCHER = "ps1:$($PSVersionTable.PSEdition):$($v.Major).$($v.Minor)"
    # The installed template runs in its global mode; a project's runner never does.
    if ($globalMode) { $env:PYTEMPLATE_GLOBAL = '1' } else { Remove-Item -LiteralPath Env:PYTEMPLATE_GLOBAL -ErrorAction Ignore }
    # The runner starts on $python, in the caller's folder: a UV_PYTHON of the caller must not
    # choose another one, a UV_MANAGED_PYTHON or UV_NO_MANAGED_PYTHON must not stop uv (it refuses
    # them next to --python-preference), a PYTHONHOME or PYTHONPATH must not break it, a
    # UV_WORKING_DIR must not move it (its own tools never get them either).
    Remove-Item -LiteralPath Env:UV_PYTHON, Env:UV_MANAGED_PYTHON, Env:UV_NO_MANAGED_PYTHON, Env:PYTHONHOME, Env:PYTHONPATH, Env:UV_WORKING_DIR -ErrorAction Ignore
    if ($PSVersionTable.PSEdition -eq 'Core') {
        # PowerShell 7 rewrites native arguments that are not quoted literals, splatted ones
        # included: it globs '*' (Linux/macOS) and expands '~', '~/x' ('~\x' on Windows). Run
        # the call rebuilt from single-quoted words so argv reaches uv untouched.
        $q = [Management.Automation.Language.CodeGeneration]
        $words = foreach ($a in @($uv, 'run', '--quiet', "--python=$python", '--python-preference', $preference, '--script', $entry) + $argv) { "'" + $q::EscapeSingleQuotedStringContent($a) + "'" }
        $call = '& ' + ($words -join ' ')
        if ($fromPipe) { $call = '$pipeIn | ' + $call }
        Invoke-Expression $call
    } elseif ($fromPipe) {
        $pipeIn | & $uv run --quiet "--python=$python" --python-preference $preference --script $entry @argv
    } else {
        & $uv run --quiet "--python=$python" --python-preference $preference --script $entry @argv
    }
    $code = $LASTEXITCODE
} catch {
    # The innermost exception's message on one line: the outer one (and the error record) add
    # the position of the generated Invoke-Expression call (At line:1 char:1, + & '...').
    $e = $_.Exception
    while ($e.InnerException) { $e = $e.InnerException }
    [Console]::Error.WriteLine("pyt: cannot run ${uv}: " + ($e.Message -replace '\s*[\r\n]+\s*', ' '))
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
