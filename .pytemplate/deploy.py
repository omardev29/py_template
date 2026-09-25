# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Punto de entrada de ./deploy (lo ejecutan los lanzadores con `uv run --script`).

Solo usa la biblioteca estándar: uv lo ejecuta en un entorno aislado, sin tocar el
.venv del proyecto. Toda la lógica está en el paquete `runner/` junto a este archivo.
"""

import sys
from pathlib import Path

# Salida en UTF-8 aunque la consola/tubería use cp1252 (acentos y ñ del runner)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner.cli import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
