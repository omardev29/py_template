@echo off
rem ./pyt launcher for cmd.exe and for every Windows program that can only
rem start PATHEXT files: xonsh, nushell, Python's subprocess, VS Code
rem "process" tasks. It finds the project root and uv and hands every
rem argument to .pytemplate\pyt.py, where all the logic lives. Outside any
rem project it runs the copy of the template that pyt install made, in its
rem global mode (PYTEMPLATE_GLOBAL=1).
rem pytemplate-launcher: pyt install copies this file into uv's tool bin folder and pyt uninstall removes it.
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
rem   * Nothing follows the argument list on the uv line: an argument with an
rem     odd number of double quotes swallows the rest of that line. The exit
rem     code is taken on the next line (a bare "exit /b" gives cmd /c callers
rem     0, whatever uv returned).
rem   * cmd reads a batch file one line at a time, opening it again by name:
rem     PYTEMPLATE_LAUNCHER_FILE names this file for the runner, whose pyt
rem     uninstall then leaves in its place a file that deletes itself on the
rem     line cmd reads next, and whose pyt install never replaces it.
rem   * Ctrl+C: cmd asks "Terminate batch job (Y/N)?" once uv has exited.
rem   * A UNC current folder is not supported (cmd.exe replaces it).
rem Exit codes: 2 = no project found, 127 = no uv, anything else = the runner's.
setlocal EnableExtensions DisableDelayedExpansion

rem --- project root: this file's folder, else walk up from the current one
rem (the folder of this file is wrong when cmd found it through PATH from a
rem quoted name). A project made before the launchers were renamed holds
rem .pytemplate\deploy.py instead (pyt.py first when a folder has both).
set "PT_GLOBAL="
set "PT_ROOT=%~dp0"
set "PT_ENTRY=pyt.py"
if exist "%PT_ROOT%.pytemplate\pyt.py" goto :find_uv
set "PT_ENTRY=deploy.py"
if exist "%PT_ROOT%.pytemplate\deploy.py" goto :find_uv
for %%I in ("%CD%\x") do set "PT_ROOT=%%~dpI"
rem A drive root is never the project found this way: any user may create
rem folders there, so its .pytemplate\pyt.py could be anybody's.
:walk_up
for %%I in ("%PT_ROOT%.") do set "PT_PARENT=%%~dpI"
if /i "%PT_PARENT%"=="%PT_ROOT%" goto :installed
set "PT_ENTRY=pyt.py"
if exist "%PT_ROOT%.pytemplate\pyt.py" goto :find_uv
set "PT_ENTRY=deploy.py"
if exist "%PT_ROOT%.pytemplate\deploy.py" goto :find_uv
set "PT_ROOT=%PT_PARENT%"
goto :walk_up

:installed
rem No project: the copy of the template that pyt install made, in its
rem global mode (pyt new...): LOCALAPPDATA\pytemplate\template.
set "PT_ENTRY=pyt.py"
rem Neither variable set: no folder is named (never one below the current
rem drive root, where any user may create folders).
if not defined LOCALAPPDATA if not defined USERPROFILE goto :no_root
set "PT_ROOT=%LOCALAPPDATA%"
if not defined LOCALAPPDATA set "PT_ROOT=%USERPROFILE%\AppData\Local"
set "PT_ROOT=%PT_ROOT%\pytemplate\template\"
if not exist "%PT_ROOT%.pytemplate\pyt.py" goto :no_root
set "PT_GLOBAL=1"

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
set "PYTEMPLATE_LAUNCHER_FILE=%~f0"
rem The installed template runs in its global mode; a project's runner never
rem does (an empty value removes the variable).
set "PYTEMPLATE_GLOBAL=%PT_GLOBAL%"
rem The Python the runner starts on. While the project has an environment,
rem the one uv reads from .python-version (python.cpython, which that
rem environment was made with), as always: --python= with no value is no
rem request, and only-managed keeps a system Python out. Else any CPython
rem 3.11 or newer that uv finds, a system one too; the runner moves the
rem commands that need python.cpython onto it itself.
set "PT_PY=>=3.11"
set "PT_PREF=managed"
if exist "%PT_ROOT%.venv\Scripts\python.exe" set "PT_PY="
if exist "%PT_ROOT%.venv\Scripts\python.exe" set "PT_PREF=only-managed"
rem The runner starts on that Python, in the caller's folder: a UV_PYTHON of
rem the caller must not choose another one, a UV_MANAGED_PYTHON or
rem UV_NO_MANAGED_PYTHON must not stop uv (it refuses them next to
rem --python-preference), a PYTHONHOME or PYTHONPATH must not break it, a
rem UV_WORKING_DIR must not move it (setlocal keeps this local).
set "UV_PYTHON="
set "UV_MANAGED_PYTHON="
set "UV_NO_MANAGED_PYTHON="
set "PYTHONHOME="
set "PYTHONPATH="
set "UV_WORKING_DIR="
rem cmd expands the whole line before running it: the helper variables are
rem cleared for the runner while uv still gets their values.
set "PT_ROOT=" & set "PT_UV=" & set "PT_ENTRY=" & set "PT_GLOBAL=" & set "PT_PY=" & set "PT_PREF=" & "%PT_UV%" run --quiet "--python=%PT_PY%" --python-preference %PT_PREF% --script "%PT_ROOT%.pytemplate\%PT_ENTRY%" %*
exit /b %ERRORLEVEL%

:no_root
>&2 echo pyt: no .pytemplate\pyt.py next to this launcher, in the current folder or in any parent folder.
>&2 echo To run pyt outside a project, install it: .\pyt install in a clone of the template, https://github.com/omardev29/py_template
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
