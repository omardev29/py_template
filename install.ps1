# pytemplate-installer: installs the `pyt` command in one line, without keeping a clone:
#
#   irm https://raw.githubusercontent.com/omardev29/py_template/main/install.ps1 | iex
#
# It clones the template (its newest commit only) into a temporary folder, runs the clone's
# `pyt install` (the template is copied into your data folder and the launchers into uv's tool
# bin folder: README, "Install pyt"), then deletes the clone. Run it again to update pyt; `pyt
# uninstall` removes it. Needs git and uv. $env:PYT_REPO and $env:PYT_REF pick another
# repository or branch (a fork, a tag). Linux, macOS: install.sh.
#
# It runs inside the caller's session (iex): everything lives in a function, it never calls
# `exit` (that would close the window) and it leaves no variable behind.

function Install-PytFromGitHub {
    $ErrorActionPreference = 'Continue'
    $repo = if ($env:PYT_REPO) { $env:PYT_REPO } else { 'https://github.com/omardev29/py_template.git' }
    $ref = if ($env:PYT_REF) { $env:PYT_REF } else { 'main' }
    $git = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $git) {
        [Console]::Error.WriteLine('pyt installer: git is needed: winget install --id Git.Git -e (or https://git-scm.com/downloads), then run this again')
        return 3
    }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('pyt-install-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    $null = New-Item -ItemType Directory -Path $tmp
    $before = $env:PYTEMPLATE_INSTALL_SOURCE
    try {
        [Console]::Error.WriteLine("pyt installer: cloning $repo ($ref)")
        $clone = Join-Path $tmp 'py_template'
        & $git.Source clone --quiet --depth 1 --branch $ref $repo $clone | Out-Host
        if ($LASTEXITCODE -ne 0) {
            [Console]::Error.WriteLine("pyt installer: git could not clone $repo ($ref)")
            return 1
        }
        # The install record names where pyt came from: the clone is gone once this ends
        $env:PYTEMPLATE_INSTALL_SOURCE = "$repo ($ref)"
        if ($env:OS -eq 'Windows_NT') {
            # pyt.cmd, not pyt.ps1: an execution policy that blocks scripts (Windows PowerShell's
            # default on client Windows) blocks a .ps1 file, never this text run by iex
            & (Join-Path $clone 'pyt.cmd') install | Out-Host
        } else {
            & /bin/sh (Join-Path $clone 'pyt') install | Out-Host
        }
        $code = $LASTEXITCODE
        if ($code -eq 127) {
            [Console]::Error.WriteLine('pyt installer: uv is needed (how to install it is above): install it, then run this again')
        }
        return $code
    } finally {
        if ($null -eq $before) { Remove-Item Env:PYTEMPLATE_INSTALL_SOURCE -ErrorAction SilentlyContinue } else { $env:PYTEMPLATE_INSTALL_SOURCE = $before }
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$global:LASTEXITCODE = Install-PytFromGitHub
Remove-Item Function:Install-PytFromGitHub
