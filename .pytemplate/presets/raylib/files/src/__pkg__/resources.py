"""Rutas de assets que funcionan igual en desarrollo, stage de mypyc, pyz, portable, wheel y PyInstaller.

Úsalo siempre DENTRO de funciones: en un módulo compilado, `__file__` a nivel de
módulo no funciona (mypyc#700). Este módulo es de frontera (no se compila).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def assets_dir() -> Path:
    env = os.environ.get("PYTEMPLATE_ASSETS")  # lo fijan los lanzadores de portable/pyz
    if env:
        return Path(env)
    frozen = getattr(sys, "_MEIPASS", None)  # PyInstaller
    if isinstance(frozen, str):
        return Path(frozen) / "assets"
    here = Path(__file__).resolve().parent
    packaged = here / "assets"  # instalado como wheel: los assets van dentro del paquete
    return packaged if packaged.is_dir() else here.parent / "assets"  # src/assets


def asset(*parts: str) -> Path:
    return assets_dir().joinpath(*parts)
