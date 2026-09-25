@echo off
rem Lanzador de ./deploy para cmd y para xonsh en Windows (xonsh solo ejecuta
rem extensiones de PATHEXT). Solo busca uv y reenvia los argumentos: toda la
rem logica esta en .pytemplate\deploy.py. Sin bloques ( ) a proposito: PATH
rem contiene "(x86)" y los romperia.
setlocal
where uv >nul 2>nul && goto run
if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
where uv >nul 2>nul && goto run
echo deploy: no se encuentra uv. Instalalo con uno de: 1>&2
echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" 1>&2
echo   winget install astral-sh.uv 1>&2
echo   scoop install uv 1>&2
exit /b 127
:run
set "PYTEMPLATE_CALLER_CWD=%CD%"
uv run --quiet --script "%~dp0.pytemplate\deploy.py" %*
exit /b %ERRORLEVEL%
