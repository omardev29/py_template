#!/usr/bin/env pwsh
# Launcher for ./deploy in PowerShell 7 and Windows PowerShell 5.1.
# It only finds uv and forwards the arguments: all the logic is in .pytemplate/deploy.py.
# ASCII file without a BOM on purpose (xonsh uses the shebang on the first line).
# If the execution policy blocks it: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# or use .\deploy.cmd.

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
    [Console]::Error.WriteLine('deploy: uv not found (https://docs.astral.sh/uv/).')
    $interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
    if ($interactive) {
        $answer = Read-Host 'Install it now with the official installer? [y/N]'
        if ($answer -match '^(y|yes)$') {
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
        [Console]::Error.WriteLine('Install it with: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"')
        exit 127
    }
}

$env:PYTEMPLATE_CALLER_CWD = (Get-Location).Path
& $uv.Source run --quiet --script (Join-Path $PSScriptRoot '.pytemplate/deploy.py') @args
exit $LASTEXITCODE
