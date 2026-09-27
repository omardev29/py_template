"""Language guard: the template repository must stay English-only.

Runs only in the template repository (marker file .pytemplate/template-repo, which
`./pyt new` does not copy): projects created from the template may use any language.
To allow a line on purpose, put `lang: allow` anywhere on it (in a comment).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MARKER = ROOT / ".pytemplate" / "template-repo"
PRAGMA = "lang: allow"
ALLOWED_PATHS = frozenset({".pytemplate/tests/test_no_spanish.py", "uv.lock"})
BINARY_SUFFIXES = frozenset({".png", ".ico", ".jpg", ".gif", ".pyz", ".whl", ".zip", ".gz", ".pyd", ".so", ".dll", ".exe"})
SKIP_DIRS = frozenset({".git", ".build", "dist", "build", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".flet"})

# Escaped so this file stays ASCII: a e i o u with acute accent (lower and upper case), n with
# tilde, u with diaeresis, and the inverted question and exclamation marks.
ACCENTS = re.compile("[\u00e1\u00e9\u00ed\u00f3\u00fa\u00c1\u00c9\u00cd\u00d3\u00da\u00f1\u00d1\u00fc\u00dc\u00bf\u00a1]")
# Checked against the CPython stdlib and site-packages: none of these occur in English code or prose.
# Excluded on purpose (collide with English/code): del sin con para solo todos usa los es el la de en no se lo un y o a si
WORDS = (
    "que las por una pero como esta este estos estas cada desde hasta cuando donde porque sobre entre hay tiene "
    "puede hace falta archivo archivos carpeta entorno entornos lanzador lanzadores nucleo frontera compilado "
    "compilada compilados siempre nunca nada otro otra otros mismo misma aviso listo tipado perfil ruta rutas "
    "paquete nombre tarea tareas informe conejos iteraciones ejecuta ejecutar necesita usar tambien aqui logica "
    "proposito instalalo reenvia desconocida desconocido booleano entero texto tabla exacto choca anidada "
    "interrumpido borrando ejecutando actualizado actualizados herramientas proyecto resumen soportados activo "
    "codigo dependencias sistema mensaje fallo lento lenta rapido primera vez ahora antes despues luego ejemplo "
    "ejemplos opcional opciones comando comandos argumentos plantilla esqueleto destino origen compilar instalar "
    "comprobar ejecutable empaqueta empaquetar dentro conjunto enseguida miapp"
).split()
WORD_RE = re.compile(r"\b(?:" + "|".join(WORDS) + r")\b", re.IGNORECASE)

pytestmark = pytest.mark.skipif(not MARKER.is_file(), reason="language guard only runs in the template repository")


def _candidates() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout
        return sorted({p for p in out.decode("utf-8").split("\0") if p})
    except (OSError, subprocess.CalledProcessError):
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".venv")]
            found += [Path(dirpath, f).relative_to(ROOT).as_posix() for f in filenames]
        return sorted(found)


def offending_lines(text: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if PRAGMA in line:
            continue
        match = ACCENTS.search(line) or WORD_RE.search(line)
        if match:
            out.append((number, match.group(0), line.strip()))
    return out


def test_detector() -> None:
    assert offending_lines("no se encuentra el archivo")
    assert offending_lines("versi\u00f3n")
    assert not offending_lines("del x; y = sin(z); CON; para; solo mode; TODOs")
    assert not offending_lines("versi\u00f3n  # lang: allow")


def test_repository_is_english() -> None:
    problems: list[str] = []
    for rel in _candidates():
        if rel in ALLOWED_PATHS or Path(rel).suffix.lower() in BINARY_SUFFIXES:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        problems += [f"{rel}:{n}: {word!r}: {line[:100]}" for n, word, line in offending_lines(text)]
    assert not problems, "Spanish text found (translate it, or add 'lang: allow' to the line):\n" + "\n".join(problems[:200])
