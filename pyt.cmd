@echo off
rem ./pyt launcher for cmd.exe and for every Windows program that can only
rem start PATHEXT files: xonsh, nushell, Python's subprocess, VS Code
rem "process" tasks. It finds the project root and uv and hands every
rem argument to .pytemplate\pyt.py, where all the logic lives.
rem Rules (CLAUDE.md, "Launchers"):
rem   * ASCII only (cmd reads this file in the OEM code page) and CRLF endings
rem     (labels and goto misbehave with LF).
rem   * No ( ) blocks: a PATH holding "(x86)" closes them early. No delayed
rem     expansion: it would eat the "!" of paths and arguments.
rem   * No percent sign in comments: cmd expands them even on rem lines.
rem   * The argument list (percent star) only on the uv line: "call" would
rem     expand it a second time.
rem   * cmd parses the arguments itself: percent, "!", double quote, caret and
rem     unquoted ampersand, pipe or angle brackets do not survive. Programs that
rem     quote argv for CreateProcess (Python, xonsh) cannot protect them either:
rem     use ./pyt or pyt.ps1 for such values.
rem   * Ctrl+C: cmd asks "Terminate batch job (Y/N)?" once uv has exited.
rem   * A UNC current folder is not supported (cmd.exe replaces it).
rem Exit codes: 2 = no project found, 127 = no uv, anything else = the runner's.
setlocal EnableExtensions DisableDelayedExpansion

rem --- project root: this file's folder, else walk up from the current one
rem (the folder of this file is wrong when cmd found it through PATH from a
rem quoted name).
set "PT_ROOT=%~dp0"
if exist "%PT_ROOT%.pytemplate\pyt.py" goto :find_uv
for %%I in ("%CD%\x") do set "PT_ROOT=%%~dpI"
rem A drive root is never the project found this way: any user may create
rem folders there, so its .pytemplate\pyt.py could be anybody's.
:walk_up
for %%I in ("%PT_ROOT%.") do set "PT_PARENT=%%~dpI"
if /i "%PT_PARENT%"=="%PT_ROOT%" goto :no_root
if exist "%PT_ROOT%.pytemplate\pyt.py" goto :find_uv
set "PT_ROOT=%PT_PARENT%"
goto :walk_up

:find_uv
set "PT_PARENT="
set "PT_UV="
if defined UV if exist "%UV%" if not exist "%UV%\" set "PT_UV=%UV%"
if not defined PT_UV for %%I in (uv.exe) do set "PT_UV=%%~$PATH:I"
if not defined PT_UV call :uv_in_dirs
if not defined PT_UV call :uv_in_registry
if not defined PT_UV goto :no_uv

set "PYTEMPLATE_CALLER_CWD=%CD%"
set "PYTEMPLATE_LAUNCHER=cmd"
rem The runner runs on the project's Python (.python-version next to it), in
rem the caller's folder: a UV_PYTHON of the caller must not choose that Python,
rem a PYTHONHOME or PYTHONPATH must not break it, a UV_WORKING_DIR must not
rem move it (setlocal keeps this local).
set "UV_PYTHON="
set "PYTHONHOME="
set "PYTHONPATH="
set "UV_WORKING_DIR="
rem cmd expands the whole line before running it: the helper variables are
rem cleared for the runner while uv still gets their values.
set "PT_ROOT=" & set "PT_UV=" & "%PT_UV%" run --quiet --script "%PT_ROOT%.pytemplate\pyt.py" %*
exit /b %ERRORLEVEL%

:no_root
>&2 echo pyt: no .pytemplate\pyt.py next to this launcher, in the current folder or in any parent folder.
exit /b 2

:no_uv
>&2 echo pyt: uv not found (https://docs.astral.sh/uv/getting-started/installation/).
>&2 echo Install it with one of these, then open a new terminal:
>&2 echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
>&2 echo   winget install --id=astral-sh.uv -e
>&2 echo   scoop install main/uv
exit /b 127

rem --- helpers (each probe is a no-op once PT_UV is set) ----------------------

:uv_in_dirs
if defined UV_INSTALL_DIR call :probe "%UV_INSTALL_DIR%"
if defined UV_INSTALL_DIR call :probe "%UV_INSTALL_DIR%\bin"
if defined XDG_BIN_HOME call :probe "%XDG_BIN_HOME%"
if defined XDG_DATA_HOME call :probe "%XDG_DATA_HOME%\..\bin"
if defined USERPROFILE call :probe "%USERPROFILE%\.local\bin"
if defined CARGO_HOME call :probe "%CARGO_HOME%\bin"
if defined USERPROFILE call :probe "%USERPROFILE%\.cargo\bin"
if not defined LOCALAPPDATA goto :uv_in_dirs_pf
call :probe "%LOCALAPPDATA%\Microsoft\WinGet\Links"
for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*") do call :probe "%%~D"
:uv_in_dirs_pf
if not defined ProgramFiles goto :uv_in_dirs_scoop
call :probe "%ProgramFiles%\WinGet\Links"
for /d %%D in ("%ProgramFiles%\WinGet\Packages\astral-sh.uv_*") do call :probe "%%~D"
:uv_in_dirs_scoop
if defined SCOOP call :probe "%SCOOP%\shims"
if defined USERPROFILE call :probe "%USERPROFILE%\scoop\shims"
if defined SCOOP_GLOBAL call :probe "%SCOOP_GLOBAL%\shims"
if defined ProgramData call :probe "%ProgramData%\scoop\shims"
if defined ChocolateyInstall call :probe "%ChocolateyInstall%\bin"
if defined ProgramData call :probe "%ProgramData%\chocolatey\bin"
exit /b 0

:uv_in_registry
rem The PATH stored in the registry: a console opened before uv was installed
rem still has the old one. The value stays in a variable, never in call
rem arguments: a quoted entry ("C:\Program Files\x") would split it there.
set "PT_LIST="
for /f "skip=2 tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "PT_LIST=%%B"
call :uv_in_list
if defined PT_UV goto :uv_in_registry_done
set "PT_LIST="
for /f "skip=2 tokens=2,*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "PT_LIST=%%B"
call :uv_in_list
:uv_in_registry_done
set "PT_LIST="
exit /b 0

:uv_in_list
if not defined PT_LIST exit /b 0
rem Drop the quotes of quoted entries first (the list is split on the
rem semicolons into quoted words below), then "call set" expands the
rem variables of REG_EXPAND_SZ values.
set "PT_LIST=%PT_LIST:"=%"
call set "PT_LIST=%PT_LIST%"
for %%P in ("%PT_LIST:;=" "%") do call :probe "%%~P"
exit /b 0

:probe
if defined PT_UV exit /b 0
if "%~1"=="" exit /b 0
if exist "%~1\uv.exe" set "PT_UV=%~1\uv.exe"
exit /b 0
