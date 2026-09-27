"""Asset paths that work the same in dev, mypyc stage, pyz, portable, PyInstaller and flet build.

This is a boundary module (it is not compiled).
"""

from __future__ import annotations

import sys
from pathlib import Path


def assets_dir() -> Path:
    frozen = getattr(sys, "_MEIPASS", None)  # PyInstaller: this executable's own bundle
    if isinstance(frozen, str):
        return Path(frozen) / "assets"
    here = Path(__file__).resolve().parent
    packaged = here / "assets"  # installed as a wheel: the assets ship inside the package
    # Else next to the package: src/assets, the app/ folder of a portable build or a pyz. Never
    # PYTEMPLATE_ASSETS first: an app started by another one inherits that app's folder
    return packaged if packaged.is_dir() else here.parent / "assets"


def asset(*parts: str) -> Path:
    return assets_dir().joinpath(*parts)
