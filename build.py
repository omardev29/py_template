"""Build de producción: mypy --strict -> mypyc -> PyInstaller.

Uso:
    uv run build.py            # un solo ejecutable  -> dist/miapp(.exe)
    uv run build.py --onedir   # una carpeta          -> dist/miapp/  (arranca más rápido)

Hay que ejecutarlo en cada SO/arquitectura destino: ni mypyc ni PyInstaller
compilan de forma cruzada.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "miapp"  # nombre del ejecutable final
ENTRY = "main.py"  # lanzador en src/: NO se compila (tiene que ejecutarse como __main__)
COMPILE = ["app.py"]  # módulos de src/ que mypyc compila a extensiones C

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
BUILD = ROOT / "build"
STAGE = BUILD / "stage"
EXT_SUFFIXES = (".so", ".pyd")


def run(*cmd: str, cwd: Path = ROOT) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def imports_of(path: Path) -> set[str]:
    """Módulos que importa un archivo.

    PyInstaller descubre dependencias leyendo bytecode; dentro de un .so compilado
    por mypyc no ve nada, así que se los pasamos como --hidden-import.
    """
    found: set[str] = set()
    # En bytes: así ast respeta el BOM que añade Windows PowerShell 5.1 (utf-8 con BOM)
    for node in ast.walk(ast.parse(path.read_bytes())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def main() -> int:
    onefile = "--onedir" not in sys.argv

    # 1) Chequeo estático con la config de pyproject.toml (la misma que usa VS Code).
    #    mypyc hace cumplir los tipos en runtime (TypeError): mejor que mypy esté limpio.
    run(sys.executable, "-m", "mypy")

    # 2) Compilar en una copia de src/: si el .so quedara junto a app.py, Python
    #    importaría ese binario en lugar de tu código editado (un .so obsoleto).
    shutil.rmtree(BUILD, ignore_errors=True)
    shutil.rmtree(ROOT / "dist", ignore_errors=True)
    shutil.copytree(SRC, STAGE, ignore=shutil.ignore_patterns("__pycache__", "*.so", "*.pyd"))
    run(sys.executable, "-m", "mypyc", *COMPILE, cwd=STAGE)

    # 3) Quitar el .py de lo compilado para que PyInstaller solo pueda meter el
    #    binario, y declarar a mano lo que ese binario importa.
    hidden: set[str] = set()
    for name in COMPILE:
        hidden |= imports_of(STAGE / name)
        (STAGE / name).unlink()
    for ext in STAGE.iterdir():
        if ext.name.endswith(EXT_SUFFIXES):
            hidden.add(ext.name.split(".")[0])  # app, y <hash>__mypyc si hay 2+ módulos

    # 4) Empaquetar intérprete + dependencias + extensiones compiladas.
    hidden_args = [arg for mod in sorted(hidden) for arg in ("--hidden-import", mod)]
    run(
        sys.executable, "-m", "PyInstaller", ENTRY,
        "--name", NAME,
        "--onefile" if onefile else "--onedir",
        "--noconfirm", "--clean",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD),
        *hidden_args,
        cwd=STAGE,
    )
    print(f"\nListo -> {ROOT / 'dist'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
