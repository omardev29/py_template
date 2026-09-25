"""exe: ejecutable con PyInstaller (CPython y mypyc; PyInstaller no soporta PyPy).

En Flet se usa `flet pack` (PyInstaller + el cliente Flutter empaquetado): con
PyInstaller a secas la app descargaría ~40 MB de cliente en el primer arranque.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import envs, mypyc, ui
from ..cmd_build import BuildRequest, dist_path
from ..project import BUILD, IS_MACOS, ROOT


def _console(req: BuildRequest) -> bool:
    mode = req.cfg.deploy.exe.console
    return (not req.cfg.app.gui) if mode == "auto" else mode == "yes"


def _onefile(req: BuildRequest) -> bool:
    return req.onefile if req.onefile is not None else req.cfg.deploy.exe.mode == "onefile"


def _stage(req: BuildRequest) -> Path:
    """Carpeta que ve PyInstaller: en mypyc, sin los .py de lo compilado (solo el binario)."""
    dest = BUILD / "exe-stage" / req.backend
    if req.compiled:
        return mypyc.exe_stage(req.cfg, req.app_dir, dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(req.app_dir, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def _hidden(req: BuildRequest, stage: Path) -> list[str]:
    hidden = mypyc.hidden_imports(req.cfg, stage) if req.compiled else []
    hidden += req.cfg.deploy.exe.hidden_imports
    return sorted(set(hidden))


def _data_args(req: BuildRequest, stage: Path) -> list[str]:
    assets = req.cfg.app.assets
    if assets and (stage / assets).is_dir():
        return ["--add-data", f"{stage / assets}:{assets}"]
    return []


def _icon_args(req: BuildRequest) -> list[str]:
    icon = req.cfg.deploy.exe.icon
    return ["--icon", str(ROOT / icon)] if icon else []


def build(req: BuildRequest) -> Path:
    if req.cfg.app.preset == "flet":
        return _flet_pack(req)
    cfg = req.cfg
    stage = _stage(req)
    out = dist_path(req)
    work = BUILD / "pyinstaller" / req.backend
    onefile = _onefile(req)
    argv: list[str | Path] = [
        "python", "-m", "PyInstaller", stage / "main.py",
        "--name", cfg.app.name,
        "--onefile" if onefile else "--onedir",
        "--noconfirm", "--clean",
        "--distpath", out,
        "--workpath", work,
        "--specpath", work,
        "--paths", stage,
        "--optimize", str(cfg.deploy.optimize),
        "--noupx",
        # UTF-8 como en desarrollo (el runner exporta PYTHONUTF8=1)
        "--python-option", "X utf8",
    ]
    if not ui.VERBOSE:
        argv.append("--log-level=WARN")
    if not _console(req):
        argv.append("--noconsole")
    for h in _hidden(req, stage):
        argv += ["--hidden-import", h]
    argv += _data_args(req, stage) + _icon_args(req) + cfg.deploy.exe.extra_args + req.extra
    if out.exists():
        shutil.rmtree(out)
    envs.uv_run(envs.tool_env(cfg), argv)
    result = out / (cfg.app.name + (".exe" if not IS_MACOS and _is_windows() else "")) if onefile else out / cfg.app.name
    return result if result.exists() else out


def _is_windows() -> bool:
    import os

    return os.name == "nt"


def _flet_pack(req: BuildRequest) -> Path:
    """`flet pack` desde una carpeta propia: borra <cwd>/build y el distpath sin preguntar (-y)."""
    cfg = req.cfg
    stage = _stage(req)
    work = BUILD / "flet-pack" / req.backend
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    onefile = _onefile(req)
    argv: list[str | Path] = [
        "flet", "pack", stage / "main.py",
        "-y",
        "--name", cfg.app.name,
        "--distpath", work / "dist",
        "--product-name", cfg.app.name,
    ]
    if not onefile:
        argv.append("--onedir")
    for h in _hidden(req, stage):
        argv += ["--hidden-import", h]
    assets = cfg.app.assets
    if assets and (stage / assets).is_dir():
        argv += ["--add-data", f"{stage / assets}:{assets}"]
    if cfg.deploy.exe.icon:
        argv += ["--icon", str(ROOT / cfg.deploy.exe.icon)]
    argv += [f"--pyinstaller-build-args=--optimize={cfg.deploy.optimize}", "--pyinstaller-build-args=--noupx"]
    if not onefile:
        argv.append("--pyinstaller-build-args=--contents-directory=.")
    argv += cfg.deploy.exe.extra_args + req.extra
    envs.uv_run(envs.tool_env(cfg), argv, cwd=work)
    out = dist_path(req)
    if out.exists():
        shutil.rmtree(out)
    shutil.move(str(work / "dist"), str(out))
    ui.info("Flet: el cliente Flutter va dentro del ejecutable (no se descarga en el primer arranque)")
    return out
