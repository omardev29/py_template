"""Ejecución de procesos hijo con un entorno explícito y reproducible."""

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
        super().__init__(f"falló (código {code}): {show(argv)}", code)


def find_uv() -> str:
    """uv exporta su propia ruta en $UV a los procesos que lanza con `uv run`."""
    uv = os.environ.get("UV")
    if uv and Path(uv).is_file():
        return uv
    found = shutil.which("uv")
    if not found:
        raise DeployError("no se encuentra uv en el PATH", 3)
    return found


def vs_installer_dir() -> Path | None:
    root = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    if not root:
        return None
    path = Path(root) / "Microsoft Visual Studio" / "Installer"
    return path if (path / "vswhere.exe").is_file() else None


def base_env() -> dict[str, str]:
    """Entorno base para los hijos.

    - Sin VIRTUAL_ENV ni el bin/ del entorno aislado de este runner (`uv run --script`
      los exporta y confundirían al `uv` del proyecto).
    - PYTHONUTF8=1: mypy/mypyc abren archivos con la codificación local (cp1252 en Windows).
    - En Windows, el instalador de Visual Studio en el PATH: el vcvarsall.bat de VS 2026
      llama a vswhere.exe sin ruta y, si falla, setuptools no encuentra el compilador.
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
    where = f"   (en {rel(cwd)})" if cwd is not None and cwd.resolve() != ROOT else ""
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
        raise DeployError(f"no se encuentra el programa: {args[0]}", 3) from None
    if check and proc.returncode != 0:
        if capture and proc.stderr:
            ui.info(proc.stderr.rstrip())
        raise CommandFailed(args, proc.returncode)
    return proc


def output(argv: Sequence[str | Path], *, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> str:
    """Ejecuta sin eco y devuelve stdout (lanza CommandFailed si falla)."""
    return run(argv, env=env, cwd=cwd, capture=True, echo=False).stdout.strip()
