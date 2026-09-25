"""Project paths and platform detection."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / ".pytemplate"
TEMPLATES = TEMPLATE / "templates"
PRESETS = TEMPLATE / "presets"
TOOLS = TEMPLATE / "tools"
SRC = ROOT / "src"
TESTS = ROOT / "tests"
DIST = ROOT / "dist"
CONFIG_FILE = ROOT / "pytemplate.toml"
PYPROJECT = ROOT / "pyproject.toml"
STATE_FILE = TEMPLATE / "state.json"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
# WSL on a Windows checkout (/mnt/c/...): separate environments and builds, so the
# Windows .venv is not turned into a Linux one.
IS_WSL = (
    sys.platform == "linux"
    and "WSL_DISTRO_NAME" in os.environ
    and str(ROOT).startswith("/mnt/")
)
ENV_SUFFIX = "-wsl" if IS_WSL else ""
BUILD = ROOT / ".build" / "wsl" if IS_WSL else ROOT / ".build"

EXT_SUFFIXES = (".pyd", ".so")


def venv_python(env_dir: Path) -> Path:
    """Return the interpreter inside a virtual environment (Scripts/ on Windows, bin/ elsewhere)."""
    if IS_WINDOWS:
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def host_os() -> str:
    """windows | linux | macos"""
    if IS_WINDOWS:
        return "windows"
    return "macos" if IS_MACOS else "linux"


def host_arch() -> str:
    """x86_64 | aarch64 | x86 (uv names)."""
    machine = platform.machine().lower()
    return {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "x86": "x86",
        "i386": "x86",
        "i686": "x86",
    }.get(machine, machine)


def rel(path: Path | str) -> str:
    """Return a path relative to the project root for display (or the absolute one if outside)."""
    p = Path(path)
    try:
        return p.resolve().relative_to(ROOT).as_posix() or "."
    except ValueError:
        return str(p)


def code_dirs() -> list[str]:
    """Return the code folders that ruff/mypy check (those that exist)."""
    return [d for d in ("src", "tests") if (ROOT / d).is_dir()]
