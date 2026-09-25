"""pyz: un solo archivo zipapp que ejecuta cualquier CPython o PyPy compatible.

    miapp.pyz
      __main__.py             arranque (extrae a una caché la primera vez)
      _pyz.json               build_id, versión mínima, destinos
      common/app/             tu código en .py (sirve en cualquier intérprete)
      common/lib/             dependencias puras (si ninguna es nativa)
      targets/<clave>/lib/    dependencias para esa plataforma (si hay nativas)
      targets/<clave>/app/    los paquetes compilados por mypyc (solo la clave del host)

Claves: cp314-windows-x86_64, cp314-linux-x86_64, pp311-windows-x86_64...
Extra con [deploy.pyz] targets o --target. mypyc no compila para otros sistemas: allí
se usa el .py (más lento, mismo resultado).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipapp
from pathlib import Path

from .. import ui
from ..cmd_build import BuildRequest, dist_path
from ..config import Config
from ..project import BUILD, EXT_SUFFIXES, IS_WINDOWS, TEMPLATES, rel
from ..ui import DeployError
from . import common


def _build_id(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(path.relative_to(root).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _wrapper_cmd(cfg: Config, backend: str, pyz_name: str) -> str:
    if backend == "pypy":
        order = ["pypy3", "pypy", "py", "python"]
    else:
        order = [f"py -{cfg.python.cpython}", "python3", "python", "pypy3"]
    major, minor = cfg.min_python.split(".")
    probe = f'-c "import sys; sys.exit(sys.version_info[:2] < ({major}, {minor}))"'
    lines = ["@echo off", "setlocal", 'set "PYTHONUTF8=1"']
    # Se prueba cada intérprete de verdad (que exista Y cumpla la versión mínima): el
    # lanzador `py` puede estar instalado sin ningún Python registrado
    for i, cmd in enumerate(order):
        lines.append(f"{cmd} {probe} >nul 2>nul && goto run{i}")
    lines += [f"echo {cfg.app.name}: hace falta Python o PyPy en el PATH 1>&2", "exit /b 9009"]
    for i, cmd in enumerate(order):
        lines += [f":run{i}", f'{cmd} "%~dp0{pyz_name}" %*', "exit /b %ERRORLEVEL%"]
    return "\r\n".join(lines) + "\r\n"


def build(req: BuildRequest) -> Path:
    cfg = req.cfg
    keys = [*cfg.deploy.pyz.targets, *req.targets]
    targets = common.targets_for(cfg, req.backend, keys)
    host = targets[0]
    work = BUILD / "pyz" / req.backend
    root = work / "root"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    # Código puro (sin .pyd): funciona en cualquier intérprete >= versión mínima
    common.copy_app(req.app_dir, root / "common" / "app", extensions=False)

    requirements = common.export_requirements(cfg)
    sites: dict[str, Path] = {}
    for t in targets:
        ui.info(f"  dependencias para {t.key}")
        sites[t.key] = common.install_deps(cfg, req.backend, t, work / "site" / t.key, requirements)
    native = any(common.has_native(p) for p in sites.values())
    if native:
        for key, site in sites.items():
            shutil.copytree(site, root / "targets" / key / "lib")
    else:
        shutil.copytree(sites[host.key], root / "common" / "lib")

    if req.compiled:
        # Paquetes compilados COMPLETOS (con __init__.py): un overlay parcial sería un
        # namespace package y Python cargaría los .py del zip en su lugar
        overlay = root / "targets" / host.key / "app"
        common.copy_app(req.app_dir, overlay, extensions=True)

    target_keys = sorted(p.name for p in (root / "targets").iterdir()) if (root / "targets").is_dir() else []
    pure = not native
    min_python = [int(x) for x in cfg.min_python.split(".")]
    info = {
        "name": cfg.app.name,
        "build_id": _build_id(root),
        "min_python": min_python,
        "targets": target_keys,
        "pure": pure,
        "backend": req.backend,
    }
    (root / "_pyz.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    shutil.copy2(TEMPLATES / "pyz" / "__main__.py", root / "__main__.py")

    out_dir = dist_path(req)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    pyz = out_dir / f"{cfg.app.name}.pyz"
    zipapp.create_archive(root, pyz, interpreter="/usr/bin/env python3", compressed=True)
    (out_dir / f"{cfg.app.name}.cmd").write_text(_wrapper_cmd(cfg, req.backend, pyz.name), encoding="ascii", newline="")
    if not IS_WINDOWS:
        pyz.chmod(0o755)

    where = ", ".join(target_keys) if target_keys else "cualquier plataforma"
    if req.compiled:
        ui.info(f"  compilado (mypyc) para {host.key}; en el resto se usa el .py")
    if pure:
        ui.info(f"  puro: funciona con CPython o PyPy >= {cfg.min_python} en cualquier sistema")
    else:
        ui.info(f"  binarios para: {where}")
    ui.info(f"  ejecuta: python {rel(pyz)}   (en Windows también {rel(out_dir / (cfg.app.name + '.cmd'))})")
    if any(p.name.endswith(EXT_SUFFIXES) for p in (root / "common").rglob("*")):
        raise DeployError("bug: hay extensiones compiladas en common/ del pyz")
    return pyz


def merge(parts: list[Path], out: Path) -> Path:
    """Une varios .pyz del mismo proyecto (uno por SO, p. ej. de la CI) en uno multiplataforma."""
    import tempfile
    import zipfile

    if len(parts) < 2:
        raise DeployError("pyz-merge necesita al menos dos .pyz")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        infos: list[dict[str, object]] = []
        for i, part in enumerate(parts):
            with zipfile.ZipFile(part) as archive:
                info = json.loads(archive.read("_pyz.json"))
                infos.append(info)
                for member in archive.namelist():
                    if member.endswith("/") or member == "_pyz.json":
                        continue
                    # common/ y __main__.py del primero; targets/ de todos
                    if i > 0 and not member.startswith("targets/"):
                        continue
                    target = root / member
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(member))
        names = {str(i["name"]) for i in infos}
        if len(names) != 1:
            raise DeployError(f"pyz-merge: son de apps distintas: {', '.join(sorted(names))}")
        targets = sorted({str(t) for i in infos for t in i["targets"]})  # type: ignore[attr-defined]
        merged = {
            **infos[0],
            "targets": targets,
            "pure": all(bool(i["pure"]) for i in infos),
            "build_id": _build_id(root),
        }
        (root / "_pyz.json").write_text(json.dumps(merged, indent=2), encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(root, out, interpreter="/usr/bin/env python3", compressed=True)
    ui.ok(f"{rel(out)}: binarios para {', '.join(targets)}")
    return out
