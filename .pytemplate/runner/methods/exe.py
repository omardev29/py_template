"""exe: executable with PyInstaller (CPython and mypyc; PyInstaller does not support PyPy).

With Flet, `flet pack` is used (PyInstaller + the bundled Flutter client): with
plain PyInstaller the app would download a ~40 MB client on first startup.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import envs, mypyc, ui, upx
from ..cmd_build import BuildRequest, dist_path
from ..config import Config
from ..project import BUILD, IS_MACOS, IS_WINDOWS, ROOT


def _console(req: BuildRequest) -> bool:
    mode = req.cfg.deploy.exe.console
    return (not req.cfg.app.gui) if mode == "auto" else mode == "yes"


def _onefile(req: BuildRequest) -> bool:
    return req.onefile if req.onefile is not None else req.cfg.deploy.exe.mode == "onefile"


def _stage(req: BuildRequest) -> Path:
    """Return the folder PyInstaller sees: with mypyc, without the .py of compiled modules (only the binary)."""
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


def size_args(cfg: Config) -> tuple[list[str], dict[str, str]]:
    """Return PyInstaller's size options (UPX, excluded modules, strip) and the env they need.

    UPX goes through PyInstaller's own step (--upx-dir): it packs each collected binary before
    bundling (also in onefile mode), skips Control Flow Guard DLLs, and reads the level from
    the UPX environment variable (it always adds --lzma).
    """
    args: list[str] = []
    env: dict[str, str] = {}
    if upx.active(cfg):
        args.append(f"--upx-dir={upx.find(cfg).parent}")
        args += [f"--upx-exclude={p}" for p in upx.excludes(cfg)]
        env["UPX"] = upx.env_value(cfg)
    else:
        args.append("--noupx")
    args += [f"--exclude-module={m}" for m in cfg.deploy.exclude_modules]
    if cfg.deploy.exe.strip and not IS_WINDOWS:
        args.append("--strip")
    return args, env


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
        # UTF-8 as in development (the runner exports PYTHONUTF8=1)
        "--python-option", "X utf8",
    ]
    if not ui.VERBOSE:
        argv.append("--log-level=WARN")
    if not _console(req):
        argv.append("--noconsole")
    for h in _hidden(req, stage):
        argv += ["--hidden-import", h]
    size, size_env = size_args(cfg)
    argv += size + _data_args(req, stage) + _icon_args(req) + cfg.deploy.exe.extra_args + req.extra
    if out.exists():
        shutil.rmtree(out)
    envs.uv_run(envs.tool_env(cfg), argv, extra_env=size_env)
    result = out / (cfg.app.name + (".exe" if IS_WINDOWS else "")) if onefile else out / cfg.app.name
    return result if result.exists() else out


def _flet_pack(req: BuildRequest) -> Path:
    """Run `flet pack` from its own folder: it removes <cwd>/build and the distpath without asking (-y)."""
    cfg = req.cfg
    stage = _stage(req)
    work = BUILD / "flet-pack" / req.backend
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    # flet pack refuses --onedir on macOS ("not supported"): there it always builds a .app bundle
    onedir = not _onefile(req) and not IS_MACOS
    argv: list[str | Path] = [
        "flet", "pack", stage / "main.py",
        "-y",
        "--name", cfg.app.name,
        "--distpath", work / "dist",
        "--product-name", cfg.app.name,
    ]
    if onedir:
        argv.append("--onedir")
    for h in _hidden(req, stage):
        argv += ["--hidden-import", h]
    assets = cfg.app.assets
    if assets and (stage / assets).is_dir():
        argv += ["--add-data", f"{stage / assets}:{assets}"]
    if cfg.deploy.exe.icon:
        argv += ["--icon", str(ROOT / cfg.deploy.exe.icon)]
    size, size_env = size_args(cfg)
    argv += [f"--pyinstaller-build-args=--optimize={cfg.deploy.optimize}"]
    argv += [f"--pyinstaller-build-args={a}" for a in size]
    if onedir:
        argv.append("--pyinstaller-build-args=--contents-directory=.")
    argv += cfg.deploy.exe.extra_args + req.extra
    envs.uv_run(envs.tool_env(cfg), argv, cwd=work, extra_env=size_env)
    out = dist_path(req)
    if out.exists():
        shutil.rmtree(out)
    shutil.move(str(work / "dist"), str(out))
    ui.info("Flet: the Flutter client is inside the executable (it is not downloaded on first startup)")
    return out
