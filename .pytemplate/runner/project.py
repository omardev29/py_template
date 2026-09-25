"""Project paths and platform detection."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
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


# --- paths typed by the user ------------------------------------------------------------------

_DRIVE_MOUNT = re.compile(r"/(?:cygdrive/)?([A-Za-z])(/.*)?")


def native_path(raw: str) -> str:
    """Map a path typed in a POSIX shell on Windows to a Windows path.

    MSYS2/Git Bash rewrite such arguments themselves before starting a native program, but
    Cygwin, niubash, busybox-w32 and MSYS2 with MSYS2_ARG_CONV_EXCL=* do not, so the runner
    can get /c/x, /cygdrive/c/x, C:/x or a path inside the MSYS root. Elsewhere: unchanged.
    """
    if not IS_WINDOWS:
        return raw
    if re.match(r"[A-Za-z]:[\\/]", raw):
        return os.path.normpath(raw)
    if not raw.startswith("/") or raw.startswith("//"):
        return raw
    m = _DRIVE_MOUNT.fullmatch(raw)
    if m:
        return m[1].upper() + ":" + (m[2] or "/").replace("/", "\\")
    launcher = os.environ.get("PYTEMPLATE_LAUNCHER", "")
    cygpath = shutil.which("cygpath")
    if launcher.endswith((":msys", ":cygwin")) and cygpath:
        try:
            out = subprocess.run(
                [cygpath, "-w", raw], capture_output=True, text=True, timeout=10, check=True
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return raw
        if re.match(r"[A-Za-z]:\\", out):
            return out
    return raw


def caller_cwd() -> Path:
    """Return the directory ./deploy was typed in.

    PYTEMPLATE_CALLER_CWD is trusted only while it still names the process cwd: niubash
    sessions keep stale exports, and `uv run --script` never changes the cwd.
    """
    cwd = Path.cwd()
    raw = os.environ.get("PYTEMPLATE_CALLER_CWD", "")
    if raw:
        p = Path(native_path(raw))
        try:
            if os.path.samefile(p, cwd):
                return p
        except OSError:
            pass
    return cwd


def user_path(raw: str) -> Path:
    """Return a path argument typed by the user, resolved against the caller's cwd."""
    p = Path(native_path(raw)).expanduser()
    return p if p.is_absolute() else caller_cwd() / p
