"""Arranque del .pyz (generado por ./deploy build ... --method pyz). Solo biblioteca estándar.

Python no puede importar extensiones (.pyd/.so) desde dentro de un zip, así que la
primera vez se extrae el contenido a una caché versionada por build y se ejecuta de
ahí. Si hay binarios para este intérprete y sistema (_pyz.json "targets"), se usan;
si no, se usa la versión Python pura (si la app lo permite).
"""

import json
import os
import platform
import runpy
import shutil
import site
import sys
import tempfile
import zipfile
from pathlib import Path

ARCH = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}
OS = {"win32": "windows", "linux": "linux", "darwin": "macos"}


def _key() -> str:
    impl = {"cpython": "cp", "pypy": "pp"}.get(sys.implementation.name, sys.implementation.name)
    arch = ARCH.get(platform.machine().lower(), platform.machine().lower())
    return f"{impl}{sys.version_info[0]}{sys.version_info[1]}-{OS.get(sys.platform, sys.platform)}-{arch}"


def _cache_root(name: str) -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / name / "pyz"


def _extract(archive: zipfile.ZipFile, prefixes: list[str], dest: Path) -> None:
    """Extrae de forma atómica (carpeta temporal + rename): seguro con arranques simultáneos."""
    if (dest / ".complete").is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=dest.parent))
    try:
        for prefix in prefixes:
            for member in archive.namelist():
                if member.startswith(prefix) and not member.endswith("/"):
                    target = tmp / member[len(prefix) :]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
        (tmp / ".complete").write_text("ok")
        try:
            os.replace(tmp, dest)
        except OSError:
            if not (dest / ".complete").is_file():
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _prune_old(root: Path, keep: str) -> None:
    builds = sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.stat().st_mtime)
    for old in builds[:-3]:
        if old.name != keep:
            shutil.rmtree(old, ignore_errors=True)


def main() -> None:
    pyz = Path(__file__).resolve().parent
    with zipfile.ZipFile(pyz) as archive:
        info = json.loads(archive.read("_pyz.json"))
        need = tuple(info["min_python"])
        if sys.version_info[:2] < need:
            sys.exit(f"{info['name']}: necesita Python {need[0]}.{need[1]} o superior (tienes {platform.python_version()})")
        key = _key()
        if key in info["targets"]:
            flavour, prefixes = key, ["common/", f"targets/{key}/"]
        elif info["pure"]:
            flavour, prefixes = "pure", ["common/"]
        else:
            sys.exit(
                f"{info['name']}: no hay binarios para este intérprete ({key}).\n"
                f"  Disponibles: {', '.join(info['targets'])}"
            )
        root = _cache_root(info["name"])
        dest = root / info["build_id"] / flavour
        _extract(archive, prefixes, dest)
    _prune_old(root, info["build_id"])

    app = dest / "app"
    sys.path.insert(0, str(app))
    if (dest / "lib").is_dir():
        site.addsitedir(str(dest / "lib"))
    os.environ.setdefault("PYTEMPLATE_ASSETS", str(app / "assets"))
    sys.argv[0] = str(app / "main.py")
    runpy.run_path(str(app / "main.py"), run_name="__main__")


main()
