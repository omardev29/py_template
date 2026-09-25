@echo off
rem Launcher for ./deploy in cmd and in xonsh on Windows (xonsh only runs
rem PATHEXT extensions). It only finds uv and forwards the arguments: all the
rem logic is in .pytemplate\deploy.py. No ( ) blocks on purpose: PATH
rem contains "(x86)", which would break them.
setlocal
where uv >nul 2>nul && goto run
if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
where uv >nul 2>nul && goto run
echo deploy: uv not found. Install it with one of: 1>&2
echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" 1>&2
echo   winget install astral-sh.uv 1>&2
echo   scoop install uv 1>&2
exit /b 127
:run
set "PYTEMPLATE_CALLER_CWD=%CD%"
uv run --quiet --script "%~dp0.pytemplate\deploy.py" %*
exit /b %ERRORLEVEL%
