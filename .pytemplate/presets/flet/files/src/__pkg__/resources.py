"""Asset paths that work the same in dev, mypyc stage, pyz, portable, PyInstaller and flet build.

This is a boundary module (it is not compiled).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def assets_dir() -> Path:
    env = os.environ.get("PYTEMPLATE_ASSETS")  # set by the portable/pyz launchers
    if env:
        return Path(env)
    frozen = getattr(sys, "_MEIPASS", None)  # PyInstaller
    if isinstance(frozen, str):
        return Path(frozen) / "assets"
    here = Path(__file__).resolve().parent
    packaged = here / "assets"  # installed as a wheel: the assets ship inside the package
    return packaged if packaged.is_dir() else here.parent / "assets"  # src/assets


def asset(*parts: str) -> Path:
    return assets_dir().joinpath(*parts)
