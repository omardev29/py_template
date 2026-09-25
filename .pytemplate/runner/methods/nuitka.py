"""nuitka: executable built with Nuitka (CPython only). It also compiles your dependencies to C.

Much slower builds than PyInstaller, in exchange for a native executable. With the
mypyc backend, your core modules are already compiled by mypyc (Nuitka includes the
.pyd files as-is) and Nuitka compiles the rest. Nuitka runs through `uv run --with nuitka==...`
(it does not go into uv.lock or the development environment).
"""

from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

from .. import envs, mypyc, proc, ui, upx
from ..cmd_build import BuildRequest, dist_path
from ..config import Config
from ..project import BUILD, IS_WINDOWS, ROOT
from ..ui import DeployError

# Nuitka is not in uv.lock (`uv run --with`), so it is pinned here to keep builds reproducible:
# the latest release on PyPI in September 2026. Bump it deliberately.
NUITKA = "nuitka==4.2.2"


def _flet_client_archive(cfg: Config) -> Path:
    """Return the Flet desktop client archive of the locked flet-desktop (downloaded once).

    Same file and URL as flet_desktop's own first-start download (flet-windows.zip,
    flet-macos.tar.gz or the glibc-matched Linux tarball), cached in .build/flet-client/.
    """
    query = "import flet_desktop, flet_desktop.version as v; print(flet_desktop.get_artifact_filename(), v.version)"
    out = envs.uv(envs.tool_env(cfg), ["run", "--locked", "python", "-c", query], capture=True, echo=False).stdout.split()
    if len(out) != 2:
        raise DeployError(f"could not ask flet_desktop for its client archive: {' '.join(out)!r}")
    name, version = out
    archive = BUILD / "flet-client" / version / name
    if archive.is_file():
        return archive
    url = f"https://github.com/flet-dev/flet/releases/download/v{version}/{name}"
    ui.info(f"  downloading the Flet client for Nuitka: {url}")
    if proc.DRY_RUN:
        return archive
    archive.parent.mkdir(parents=True, exist_ok=True)
    partial = archive.with_suffix(archive.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=300) as r, partial.open("wb") as f:  # noqa: S310 (fixed https URL)
            shutil.copyfileobj(r, f)
    except OSError as e:
        partial.unlink(missing_ok=True)
        raise DeployError(f"cannot download the Flet client {url}: {e}", 3) from None
    partial.replace(archive)
    return archive


def build(req: BuildRequest) -> Path:
    cfg = req.cfg
    stage = BUILD / "nuitka-stage" / req.backend
    if req.compiled:
        mypyc.exe_stage(cfg, req.app_dir, stage)
    else:
        if stage.exists():
            shutil.rmtree(stage)
        shutil.copytree(req.app_dir, stage, ignore=shutil.ignore_patterns("__pycache__"))
    work = BUILD / "nuitka" / req.backend
    if work.exists():
        shutil.rmtree(work)
    onefile = req.onefile if req.onefile is not None else cfg.deploy.nuitka.mode == "onefile"

    argv: list[str | Path] = [
        "python", "-m", "nuitka", stage / "main.py",
        f"--mode={'onefile' if onefile else 'standalone'}",
        f"--output-dir={work}",
        f"--output-filename={cfg.app.name}{'.exe' if IS_WINDOWS else ''}",
        "--assume-yes-for-downloads",
        f"--include-package={cfg.pkg}",
    ]
    if req.compiled:
        argv += [f"--include-module={m}" for m in mypyc.hidden_imports(cfg, stage)]
    if cfg.deploy.optimize >= 1:
        argv.append("--python-flag=no_asserts")
    if cfg.deploy.optimize >= 2:
        argv.append("--python-flag=no_docstrings")
    assets = cfg.app.assets
    if assets and (stage / assets).is_dir():
        argv.append(f"--include-data-dir={stage / assets}={assets}")
    if cfg.app.gui and IS_WINDOWS:
        argv.append("--windows-console-mode=disable")
    if cfg.deploy.exe.icon and IS_WINDOWS:
        argv.append(f"--windows-icon-from-ico={ROOT / cfg.deploy.exe.icon}")
    argv += [f"--nofollow-import-to={m}" for m in cfg.deploy.exclude_modules]
    if upx.active(cfg):
        # Nuitka's plugin packs each binary with --best --lzma (deploy.upx.level does not apply)
        argv += ["--plugin-enable=upx", f"--upx-binary={upx.find(cfg)}"]
    if cfg.app.preset == "flet":
        # flet loads its controls lazily (module __getattr__ + importlib), which Nuitka cannot
        # follow; and the flet-desktop wheel has no Flutter client: bundle the release archive
        # where flet_desktop looks for one (flet_desktop/app/), as `flet pack` does
        archive = _flet_client_archive(cfg)
        argv += [
            "--include-package=flet",
            "--include-package=flet_desktop",
            f"--include-data-files={archive}=flet_desktop/app/{archive.name}",
        ]
    argv += cfg.deploy.nuitka.extra_args + req.extra

    ui.info("  Nuitka compiles everything to C: the first build takes several minutes")
    envs.uv(envs.tool_env(cfg), ["run", "--locked", "--with", NUITKA, *argv], cwd=stage)

    out = dist_path(req)
    if out.exists():
        shutil.rmtree(out)
    produced = sorted(work.iterdir()) if work.is_dir() else []
    if onefile:
        exe = next((p for p in produced if p.is_file() and p.name.startswith(cfg.app.name)), None)
        if exe is None:
            raise DeployError(f"nuitka finished without producing {cfg.app.name}* in {work}")
        out.mkdir(parents=True)
        shutil.move(str(exe), str(out / exe.name))
        return out / exe.name
    dist_dir = next((p for p in produced if p.is_dir() and p.name.endswith(".dist")), None)
    if dist_dir is None:
        raise DeployError(f"nuitka finished without producing a *.dist folder in {work}")
    shutil.move(str(dist_dir), str(out))
    return out
