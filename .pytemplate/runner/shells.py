"""Shells: shell-setup snippets, the hidden `__probe` command, doctor checks and `selftest --shells`.

The launchers (deploy, deploy.cmd, deploy.ps1) only find the project root and uv, export
PYTEMPLATE_CALLER_CWD and PYTEMPLATE_LAUNCHER, and hand every argument to
`uv run --quiet --script .pytemplate/deploy.py`. `__probe` is the target the launcher tests
call through each shell to check that argv, the exit code, the cwd and stdin arrive intact.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable

from .config import Config
from .project import IS_WINDOWS, IS_WSL, ROOT, caller_cwd
from .ui import DeployError

Check = Callable[[bool | None, str, str], None]


# --- __probe (launcher self-test target, not listed in help) -----------------------------------


def probe(argv: list[str]) -> int:
    """__probe EXIT STDIN(0|1) ARGS...: print one PTPROBE{json} line and exit with EXIT."""
    code = int(argv[0]) if argv and argv[0].lstrip("-").isdigit() else 0
    read_stdin = len(argv) > 1 and argv[1] == "1"
    line = sys.stdin.readline().rstrip("\r\n") if read_stdin else None
    data = {
        "argv": argv[2:],
        "cwd": os.getcwd(),
        "caller_cwd_raw": os.environ.get("PYTEMPLATE_CALLER_CWD"),
        "caller_cwd": str(caller_cwd()),
        "launcher": os.environ.get("PYTEMPLATE_LAUNCHER"),
        "stdin_tty": sys.stdin.isatty() if sys.stdin else None,
        "stdin": line,
        "root": str(ROOT),
    }
    print("PTPROBE" + json.dumps(data, ensure_ascii=True), flush=True)
    return code


# --- doctor --------------------------------------------------------------------------------------


def _ps_policy() -> str:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return ""
    r = subprocess.run([exe, "-NoProfile", "-Command", "Get-ExecutionPolicy"], capture_output=True, text=True, check=False)
    return r.stdout.strip()


def doctor(check: Check) -> None:
    """Launcher and shell checks for ./deploy doctor (it prints its own step headers)."""
    from . import ui

    ui.step("launchers")
    launcher = (ROOT / "deploy").read_bytes() if (ROOT / "deploy").is_file() else b""
    check(launcher.startswith(b"#!/bin/sh") and b"\r\n" not in launcher, "launcher ./deploy: #!/bin/sh with LF line endings", "")

    ui.step("shell")
    if IS_WINDOWS:
        bash = shutil.which("bash") or ""
        if "system32" in bash.lower():
            check(
                None,
                f"`bash` points to WSL ({bash})",
                "On Windows use ./deploy from xonsh, pwsh or cmd; in WSL the runner uses separate -wsl environments",
            )
        policy = _ps_policy()
        if policy:
            check(
                policy not in ("Restricted", "AllSigned"),
                f"PowerShell: ExecutionPolicy = {policy}",
                "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   (or use .\\deploy.cmd)",
            )
    if IS_WSL:
        check(None, "WSL on /mnt: .venv*-wsl environments and .build/wsl kept separate from Windows", "")


# --- shell-setup ---------------------------------------------------------------------------------


XONSH_SNIPPET = r'''
# --- ./deploy without "./" in any project made from the template (paste into ~/.xonshrc) ---
from pathlib import Path as _DeployPath

def _deploy_find():
    for d in (_DeployPath.cwd(), *_DeployPath.cwd().parents):
        if (d / ".pytemplate" / "deploy.py").is_file():
            return d / ".pytemplate" / "deploy.py"
    return None

def _deploy_complete(context):
    words = "setup doctor mode render sync lock add remove init new run check lint fmt test report build clean tasks shell-setup help cpython pypy mypyc all".split()
    return {w for w in words if w.startswith(context.prefix)} if hasattr(context, "prefix") else set(words)

@aliases.register("deploy")
@aliases.return_command
def _deploy(args):
    script = _deploy_find()
    if script is None:
        return ["echo", "deploy: no .pytemplate/deploy.py in this directory or its parents"]
    return ["uv", "run", "--quiet", "--script", str(script), *args]
'''

PWSH_SNIPPET = r"""
# --- ./deploy without ".\" in any project made from the template (paste into $PROFILE) ---
function deploy {
    $d = Get-Item -LiteralPath (Get-Location)
    while ($d -and -not (Test-Path (Join-Path $d.FullName '.pytemplate/deploy.py'))) { $d = $d.Parent }
    if (-not $d) { Write-Error 'deploy: no .pytemplate/deploy.py here or in any parent directory'; return }
    uv run --quiet --script (Join-Path $d.FullName '.pytemplate/deploy.py') @args
}
"""

BASH_SNIPPET = r"""
# --- ./deploy without "./" in any project made from the template (paste into ~/.bashrc or ~/.zshrc) ---
deploy() {
    d=$PWD
    while [ "$d" != "/" ] && [ ! -f "$d/.pytemplate/deploy.py" ]; do d=$(dirname "$d"); done
    if [ ! -f "$d/.pytemplate/deploy.py" ]; then echo "deploy: no .pytemplate/deploy.py found" >&2; return 1; fi
    uv run --quiet --script "$d/.pytemplate/deploy.py" "$@"
}
"""


def cmd_shell_setup(cfg: Config, args: list[str]) -> int:
    """shell-setup xonsh|pwsh|bash|zsh: print a `deploy` alias to use it without ./"""
    shell = args[0] if args else "xonsh"
    snippets = {"xonsh": XONSH_SNIPPET, "pwsh": PWSH_SNIPPET, "powershell": PWSH_SNIPPET, "bash": BASH_SNIPPET, "zsh": BASH_SNIPPET}
    if shell not in snippets:
        raise DeployError(f"shell-setup: unknown shell '{shell}' (xonsh | pwsh | bash | zsh)")
    print(snippets[shell].strip("\n"))
    return 0


# --- selftest --shells ---------------------------------------------------------------------------


def selftest(cfg: Config, args: list[str]) -> int:
    """selftest --shells [NAME,...] [--list] [--json] [--keep]: run ./deploy __probe through every shell."""
    raise DeployError("selftest --shells: not implemented yet")
