"""Shared pytest configuration for every backend.

With `./deploy test mypyc` the tests run against the COMPILED modules: this checks that
the .pyd/.so files were really loaded instead of the .py files (otherwise the tests would
pass without exercising the binary, which is where runtime TypeErrors show up).
"""

import importlib
import os

import pytest

BACKEND = os.environ.get("PYTEMPLATE_BACKEND", "cpython")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "interpreted_only: uses monkeypatch/mocks on compiled code (skipped with mypyc)"
    )
    for name in filter(None, os.environ.get("PYTEMPLATE_COMPILED", "").split(",")):
        path = importlib.import_module(name).__file__ or ""
        if not path.endswith((".pyd", ".so")):
            raise pytest.UsageError(f"{name} was loaded from {path}, not from the mypyc binary")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if BACKEND != "mypyc":
        return
    skip = pytest.mark.skip(reason="monkeypatch/mocks do not work on compiled code")
    for item in items:
        if "interpreted_only" in item.keywords:
            item.add_marker(skip)
