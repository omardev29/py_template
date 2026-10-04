"""exe: executable with PyInstaller (CPython and mypyc; PyInstaller does not support PyPy).

With Flet, `flet pack` is used (PyInstaller + the bundled Flutter client): with
plain PyInstaller the app would download a ~40 MB client on first startup.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .. import envs, mypyc, ui, upx
from ..cmd_build import BuildRequest, dist_path
from ..config import Config
from ..project import BUILD, IS_MACOS, IS_WINDOWS, ROOT, TOOLS
from .common import DOWNLOAD_RETRIES, copy_tree, refuse_a_globbed_folder, remove_output


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
        mypyc.remove_tree(dest)
    copy_tree(req.app_dir, dest, ignore=shutil.ignore_patterns("__pycache__"), copy_function=mypyc.copy_writable)
    return dest


def _hidden(req: BuildRequest, stage: Path) -> list[str]:
    hidden = mypyc.hidden_imports(req.cfg, stage) if req.compiled else []
    hidden += req.cfg.deploy.exe.hidden_imports
    return sorted(set(hidden))


def _data_args(req: BuildRequest, stage: Path, spec_dir: Path) -> list[str]:
    """--add-data for the assets, the source relative to `spec_dir`, the folder of the .spec file
    PyInstaller writes and reads a relative data source from (--specpath; flet pack: its cwd):
    PyInstaller splits SOURCE:DEST at ':' and at os.pathsep, and the absolute source of a project
    in a folder named with ';' (legal on Windows, where uv works in it) gave it two separators:
    "Wrong syntax, should be --add-data=SOURCE:DEST"."""
    assets = req.cfg.app.assets
    if not (assets and (stage / assets).is_dir()):
        return []
    try:
        source = os.path.relpath(stage / assets, spec_dir)
    except ValueError:  # another drive (Windows): only the absolute path names it
        source = str(stage / assets)
    return ["--add-data", f"{source}:{assets}"]


def _icon_args(req: BuildRequest) -> list[str]:
    icon = req.cfg.deploy.exe.icon
    return ["--icon", str(ROOT / icon)] if icon else []


def size_args(cfg: Config) -> tuple[list[str], dict[str, str]]:
    """Return PyInstaller's size options (UPX, excluded modules, strip) and the env they need.

    UPX goes through PyInstaller's own step (--upx-dir): it packs each collected binary before
    bundling (also in onefile mode), skips Control Flow Guard DLLs, and reads the level from
    the UPX environment variable (it always adds --lzma). Windows only: PyInstaller's
    configure.get_config turns UPX off on every other OS (packed .so files crash when loaded),
    so elsewhere nothing is downloaded and the build says the exe is not packed.
    """
    args: list[str] = []
    env: dict[str, str] = {}
    if IS_WINDOWS and upx.active(cfg):
        args.append(f"--upx-dir={upx.find(cfg).parent}")
        args += [f"--upx-exclude={p}" for p in upx.excludes(cfg)]
        env["UPX"] = upx.env_value(cfg)
    else:
        if cfg.deploy.upx.enabled and not IS_WINDOWS:  # (on Windows upx.active already said why)
            reason = upx.unsupported_reason() or "PyInstaller packs with UPX only on Windows"
            ui.warn(f"deploy.upx: {reason}: this exe is not UPX-packed")
        args.append("--noupx")
    args += [f"--exclude-module={m}" for m in cfg.deploy.exclude_modules]
    if cfg.deploy.exe.strip and not IS_WINDOWS:
        args.append("--strip")
    return args, env


def check_options(cfg: Config) -> None:
    """What the build refuses before any work (cmd_build calls this before the checks, also in
    --dry-run): a project folder whose path holds '[', '*' or '?'. PyInstaller lists the hook
    scripts of each hook folder (its own, pyinstaller-hooks-contrib's, flet_cli's: all in .venv)
    with glob.glob(os.path.join(hook_dir, 'hook-*.py')), and under `games [2026]` it found none:
    the executable lacked what the hooks collect (rich's Unicode tables, Flet's client and icons)
    while the build said done, and flet pack said its client was inside."""
    refuse_a_globbed_folder(
        "exe",
        "PyInstaller would find none of its hooks in .venv, and the executable would lack what they "
        "collect (rich's Unicode tables, Flet's client and icons) while the build says done",
    )


def build(req: BuildRequest) -> Path:
    check_options(req.cfg)
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
    argv += size + _data_args(req, stage, work) + _icon_args(req) + cfg.deploy.exe.extra_args + req.extra
    remove_output(out)
    envs.uv_run(envs.tool_env(cfg), argv, extra_env=size_env)
    result = out / (cfg.app.name + (".exe" if IS_WINDOWS else "")) if onefile else out / cfg.app.name
    return result if result.exists() else out


def _flet_pack(req: BuildRequest) -> Path:
    """Run `flet pack` from its own folder: it removes <cwd>/build and the distpath without asking (-y).

    The distpath is the final output folder: flet pack writes the Linux desktop entry after
    PyInstaller with an absolute Exec built from it, which a later move would leave pointing at
    nothing.
    """
    cfg = req.cfg
    stage = _stage(req)
    work = BUILD / "flet-pack" / req.backend
    if work.exists():
        mypyc.remove_tree(work)
    work.mkdir(parents=True)
    out = dist_path(req)
    remove_output(out)  # flet pack's own -y removal ignores errors: a running app is refused here
    # flet pack refuses --onedir on macOS ("not supported"): there it always builds a .app bundle
    onedir = not _onefile(req) and not IS_MACOS
    argv: list[str | Path] = [
        "flet", "pack", stage / "main.py",
        "-y",
        "--name", cfg.app.name,
        "--distpath", out,
        "--product-name", cfg.app.name,
    ]
    if onedir:
        argv.append("--onedir")
    if _console(req):
        # flet pack always passes --noconsole unless --debug-console has a (truthy) value
        argv.append("--debug-console=true")
    for h in _hidden(req, stage):
        argv += ["--hidden-import", h]
    argv += _data_args(req, stage, work)  # flet pack writes the spec in its cwd, work
    if cfg.deploy.exe.icon:
        argv += ["--icon", str(ROOT / cfg.deploy.exe.icon)]
    size, size_env = size_args(cfg)
    # --clean as in the plain PyInstaller build: PyInstaller's global binary cache is keyed
    # without the UPX level, so it would hand back binaries packed at another deploy.upx.level
    argv += ["--pyinstaller-build-args=--clean", f"--pyinstaller-build-args=--optimize={cfg.deploy.optimize}"]
    # UTF-8 as in development, like the plain PyInstaller build (one argv item: flet pack
    # hands each --pyinstaller-build-args value to PyInstaller unchanged)
    argv.append("--pyinstaller-build-args=--python-option=X utf8")
    argv += [f"--pyinstaller-build-args={a}" for a in size]
    if onedir and IS_WINDOWS:
        # A flat folder only on Windows (<name>.exe): on Linux the executable dist/<name>/<name>
        # would be a FILE where the package folder dist/<name>/<pkg>/ (mypyc extensions, data)
        # must go when app.name == pkg, the default. Linux keeps PyInstaller's _internal/.
        argv.append("--pyinstaller-build-args=--contents-directory=.")
    argv += cfg.deploy.exe.extra_args + req.extra
    # flet pack bundles the Flet client from flet_desktop's cache, which the first build on a
    # machine fills with one download from GitHub and no retry: a transient HTTP 500 failed the
    # build (15.1). Fetched first with flet_desktop's own code, in flet pack's folder and
    # environment, trying a transient failure again; nothing is downloaded when it is cached.
    pauses = [f"{p:g}" for p in DOWNLOAD_RETRIES]
    envs.uv_run(envs.tool_env(cfg), ["python", TOOLS / "flet_client.py", *pauses], cwd=work)
    envs.uv_run(envs.tool_env(cfg), argv, cwd=work, extra_env=size_env)
    ui.info("Flet: the Flutter client is inside the executable (it is not downloaded on first startup)")
    return out
