"""wheel: paquete instalable (`uv tool install`, pip) con un comando de consola.

- cpython / pypy: wheel puro (py3-none-any), vale para cualquier intérprete compatible.
- mypyc: wheel de plataforma (cp314-win_amd64...) con las extensiones compiladas por
  setuptools + mypycify; incluye también los .py (Python carga antes el binario).

El proyecto de build se genera en .build/wheel/<backend>/: tu pyproject.toml no
necesita [build-system] (así uv lo trata como app, no como paquete).
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

from .. import envs, mypyc, render, ui
from ..cmd_build import BuildRequest, dist_path
from ..config import Config
from ..project import BUILD, PYPROJECT, SRC, rel
from ..ui import DeployError


def _locked_version(package: str) -> str:
    lock = tomllib.loads((PYPROJECT.parent / "uv.lock").read_text(encoding="utf-8"))
    for pkg in lock.get("package", []):
        if pkg.get("name") == package:
            return str(pkg["version"])
    raise DeployError(f"{package} no está en uv.lock")


def _pyproject(cfg: Config, compiled: bool) -> str:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    entry = cfg.deploy.wheel.entry or f"{cfg.pkg}.app:main"
    requires = ["setuptools>=84"] + ([f"mypy=={_locked_version('mypy')}"] if compiled else [])
    lines = [
        "[build-system]",
        f"requires = {json.dumps(requires)}",
        'build-backend = "setuptools.build_meta"',
        "",
        "[project]",
        f"name = {json.dumps(project['name'])}",
        f"version = {json.dumps(project['version'])}",
        f"description = {json.dumps(project.get('description', ''), ensure_ascii=False)}",
        f"requires-python = {json.dumps(project.get('requires-python', '>=3.11'))}",
        f"dependencies = {json.dumps(project.get('dependencies', []))}",
        "",
        "[project.scripts]",
        f"{json.dumps(cfg.app.name)} = {json.dumps(entry)}",
        "",
        "[tool.setuptools.packages.find]",
        'where = ["src"]',
        "",
        "[tool.setuptools.package-data]",
        f'{json.dumps(cfg.pkg)} = ["assets/**/*"]',
    ]
    return "\n".join(lines) + "\n"


SETUP_PY = '''\
"""Generado por ./deploy build --method wheel: compila con mypyc los módulos de compile.modules."""
from mypyc.build import mypycify
from setuptools import setup

setup(
    ext_modules=mypycify(
        ["--config-file", "mypy.ini", *{files!r}],
        opt_level={opt!r},
        debug_level="0",
        strip_asserts={strip!r},
        group_name={group!r},
    )
)
'''


def build(req: BuildRequest) -> Path:
    cfg = req.cfg
    package = SRC / cfg.pkg
    if not package.is_dir():
        raise DeployError(f"wheel: no existe el paquete src/{cfg.pkg}/")
    work = BUILD / "wheel" / req.backend
    if work.exists():
        shutil.rmtree(work)
    (work / "src").mkdir(parents=True)
    shutil.copytree(package, work / "src" / cfg.pkg, ignore=shutil.ignore_patterns("__pycache__", "*.pyd", "*.so"))
    assets = cfg.app.assets
    if assets and (SRC / assets).is_dir():
        # En un wheel los assets viajan dentro del paquete (resources.py los busca ahí)
        shutil.copytree(SRC / assets, work / "src" / cfg.pkg / "assets")
    (work / "pyproject.toml").write_text(_pyproject(cfg, req.compiled), encoding="utf-8")
    if req.compiled:
        (work / "mypy.ini").write_text(render.mypy_ini(cfg, "mypyc", for_compile=True), encoding="utf-8")
        files = [f"src/{p.relative_to(SRC).as_posix()}" for p in mypyc.compiled_sources(cfg)]
        (work / "setup.py").write_text(
            SETUP_PY.format(
                files=files,
                opt=cfg.compile.opt_level,
                strip=cfg.deploy.optimize >= 1,
                group=mypyc.group_name(cfg),
            ),
            encoding="utf-8",
        )
    out = dist_path(req)
    if out.exists():
        shutil.rmtree(out)
    envs.uv(envs.tool_env(cfg), ["build", "--wheel", "--out-dir", out, work], extra_env={"VSLANG": "1033"})
    wheels = sorted(out.glob("*.whl"))
    if not wheels:
        raise DeployError("uv build no generó ningún wheel")
    ui.info(f"  instálalo con: uv tool install {rel(wheels[0])}   (comando: {cfg.app.name})")
    return wheels[0]
