"""nuitka: ejecutable con Nuitka (solo CPython). Compila a C también tus dependencias.

Builds mucho más lentos que PyInstaller, a cambio de un ejecutable nativo. Con el
backend mypyc, tus módulos del núcleo ya van compilados por mypyc (Nuitka incluye los
.pyd tal cual) y Nuitka compila el resto. Nuitka se usa con `uv run --with nuitka`
(no entra en uv.lock ni en el entorno de desarrollo).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import envs, mypyc, ui
from ..cmd_build import BuildRequest, dist_path
from ..project import BUILD, IS_WINDOWS, ROOT


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
    argv += cfg.deploy.nuitka.extra_args + req.extra

    ui.info("  Nuitka compila todo a C: la primera vez tarda varios minutos")
    envs.uv(envs.tool_env(cfg), ["run", "--locked", "--with", "nuitka", *argv], cwd=stage)

    out = dist_path(req)
    if out.exists():
        shutil.rmtree(out)
    if onefile:
        out.mkdir(parents=True)
        exe = next(p for p in work.iterdir() if p.is_file() and p.name.startswith(cfg.app.name))
        shutil.move(str(exe), str(out / exe.name))
        return out / exe.name
    dist_dir = next(p for p in work.iterdir() if p.is_dir() and p.name.endswith(".dist"))
    shutil.move(str(dist_dir), str(out))
    return out
