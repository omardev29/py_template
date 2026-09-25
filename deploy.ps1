#!/usr/bin/env pwsh
# Lanzador de ./deploy para PowerShell 7 y Windows PowerShell 5.1.
# Solo busca uv y reenvia los argumentos: toda la logica esta en .pytemplate/deploy.py.
# Archivo ASCII sin BOM a proposito (el shebang de la primera linea lo usa xonsh).
# Si la politica de ejecucion lo bloquea: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# o usa .\deploy.cmd.

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    $sep = [IO.Path]::PathSeparator
    $candidates = @($env:UV_INSTALL_DIR, $env:XDG_BIN_HOME, (Join-Path $HOME '.local/bin'), (Join-Path $HOME '.cargo/bin'))
    foreach ($d in $candidates) {
        if ($d -and (Test-Path -LiteralPath $d)) {
            $env:PATH = "$d$sep$env:PATH"
            $uv = Get-Command uv -ErrorAction SilentlyContinue
            if ($uv) { break }
        }
    }
}

if (-not $uv) {
    [Console]::Error.WriteLine('deploy: no se encuentra uv (https://docs.astral.sh/uv/).')
    $interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
    if ($interactive) {
        $answer = Read-Host 'Instalarlo ahora con el instalador oficial? [s/N]'
        if ($answer -match '^(s|si|y|yes)$') {
            if ($env:OS -eq 'Windows_NT') {
                powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
                $env:PATH = (Join-Path $HOME '.local/bin') + [IO.Path]::PathSeparator + $env:PATH
            } else {
                sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
                $env:PATH = (Join-Path $HOME '.local/bin') + [IO.Path]::PathSeparator + $env:PATH
            }
            $uv = Get-Command uv -ErrorAction SilentlyContinue
        }
    }
    if (-not $uv) {
        [Console]::Error.WriteLine('Instalalo con: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"')
        exit 127
    }
}

$env:PYTEMPLATE_CALLER_CWD = (Get-Location).Path
& $uv.Source run --quiet --script (Join-Path $PSScriptRoot '.pytemplate/deploy.py') @args
exit $LASTEXITCODE
