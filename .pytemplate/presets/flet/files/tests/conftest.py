"""Configuración de pytest compartida por todos los backends.

Con `./deploy test mypyc` los tests corren contra los módulos COMPILADOS: aquí se
comprueba que de verdad se cargaron los .pyd/.so y no los .py (si no, los tests
pasarían sin probar el binario, que es donde aparecen los TypeError de runtime).
"""

import importlib
import os

import pytest

BACKEND = os.environ.get("PYTEMPLATE_BACKEND", "cpython")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "interpreted_only: usa monkeypatch/mocks sobre código compilado (se omite con mypyc)"
    )
    for name in filter(None, os.environ.get("PYTEMPLATE_COMPILED", "").split(",")):
        path = importlib.import_module(name).__file__ or ""
        if not path.endswith((".pyd", ".so")):
            raise pytest.UsageError(f"{name} se cargó desde {path}, no desde el binario de mypyc")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if BACKEND != "mypyc":
        return
    skip = pytest.mark.skip(reason="monkeypatch/mocks no funcionan sobre código compilado")
    for item in items:
        if "interpreted_only" in item.keywords:
            item.add_marker(skip)
