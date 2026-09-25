"""Running child processes with an explicit, reproducible environment."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import ui
from .project import IS_WINDOWS, ROOT, rel
from .ui import DeployError

DRY_RUN = False


class CommandFailed(DeployError):
    def __init__(self, argv: Sequence[str], code: int) -> None:
        super().__init__(f"failed (exit code {code}): {show(argv)}", code)


def find_uv() -> str:
    """Find uv: it exports its own path in $UV to the processes it starts with `uv run`."""
    uv = os.environ.get("UV")
    if uv and Path(uv).is_file():
        return uv
    found = shutil.which("uv")
    if not found:
        raise DeployError("uv not found in PATH", 3)
    return found


def vs_installer_dir() -> Path | None:
    root = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    if not root:
        return None
    path = Path(root) / "Microsoft Visual Studio" / "Installer"
    return path if (path / "vswhere.exe").is_file() else None


def base_env() -> dict[str, str]:
    """Return the base environment for child processes.

    - No VIRTUAL_ENV and no bin/ of this runner's isolated environment (`uv run --script`
      exports them and they would confuse the project's `uv`).
    - PYTHONUTF8=1: mypy/mypyc open files with the locale encoding (cp1252 on Windows).
    - On Windows, the Visual Studio installer in PATH: VS 2026's vcvarsall.bat calls
      vswhere.exe without a path and, if that fails, setuptools cannot find the compiler.
    """
    env = dict(os.environ)
    for key in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    if sys.prefix != sys.base_prefix:
        own = os.path.normcase(str(Path(sys.prefix) / ("Scripts" if IS_WINDOWS else "bin")))
        parts = [p for p in parts if os.path.normcase(p) != own]
    if IS_WINDOWS:
        vs = vs_installer_dir()
        if vs and os.path.normcase(str(vs)) not in {os.path.normcase(p) for p in parts}:
            parts.append(str(vs))
    env["PATH"] = os.pathsep.join(parts)
    env["PYTHONUTF8"] = "1"
    return env


def show(argv: Sequence[str | Path]) -> str:
    out: list[str] = []
    for i, a in enumerate(argv):
        s = str(a)
        if i == 0 and os.path.isabs(s):
            s = Path(s).stem  # uv.exe -> uv
        elif os.path.isabs(s):
            try:
                s = Path(s).relative_to(ROOT).as_posix()
            except ValueError:
                pass
        out.append(s if s and not any(c in s for c in " \t\"'&|<>^;") else shlex.quote(s))
    return " ".join(out)


def run(
    argv: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
    echo: bool = True,
) -> subprocess.CompletedProcess[str]:
    args = [str(a) for a in argv]
    where = f"   (in {rel(cwd)})" if cwd is not None and cwd.resolve() != ROOT else ""
    if echo:
        ui.command(show(args) + where)
    else:
        ui.detail("$ " + show(args) + where)
    if DRY_RUN and echo:
        return subprocess.CompletedProcess(args, 0, "", "")
    try:
        proc = subprocess.run(
            args,
            cwd=cwd or ROOT,
            env=dict(env) if env is not None else base_env(),
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        raise DeployError(f"program not found: {args[0]}", 3) from None
    if check and proc.returncode != 0:
        if capture and proc.stderr:
            ui.info(proc.stderr.rstrip())
        raise CommandFailed(args, proc.returncode)
    return proc


def output(argv: Sequence[str | Path], *, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> str:
    """Run without echo and return stdout (raises CommandFailed on failure)."""
    return run(argv, env=env, cwd=cwd, capture=True, echo=False).stdout.strip()
