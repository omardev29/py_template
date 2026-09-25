"""Arranque de la carpeta portable (generado por ./deploy build ... --method portable).

Estructura:  runtime/ (intérprete)  lib/ (dependencias)  app/ (tu código)  boot.py
"""

import os
import runpy
import site
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "app"

sys.path.insert(0, str(APP))
site.addsitedir(str(HERE / "lib"))  # también procesa los .pth de las dependencias
os.environ.setdefault("PYTEMPLATE_ASSETS", str(APP / "assets"))
sys.argv[0] = str(APP / "main.py")
runpy.run_path(str(APP / "main.py"), run_name="__main__")
