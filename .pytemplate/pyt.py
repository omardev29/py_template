# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Entry point of ./pyt (the launchers run it with `uv run --script`).

Uses only the standard library: uv runs it in an isolated environment, without touching
the project's .venv. All the logic lives in the `runner/` package next to this file.
"""

import sys

# A UV_PYTHON (the launchers clear it; `uv run` by hand does not) or a python.cpython below
# 3.11 makes uv start an older Python, which would crash on tomllib and blame the runner.
if sys.version_info < (3, 11):
    sys.stderr.write(
        f"error: the ./pyt runner needs Python 3.11 or newer, but uv started Python {sys.version.split()[0]}"
        f" ({sys.executable}). Unset UV_PYTHON (or point it at 3.11+) and check python.cpython in pytemplate.toml.\n"
    )
    raise SystemExit(3)

import os  # noqa: E402
from pathlib import Path  # noqa: E402

# UTF-8 output even when the console/pipe uses cp1252 (paths and tool output may be non-ASCII)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8", errors="replace")

_here = Path(__file__).resolve().parent


def _user_cache() -> str | None:
    """The user's cache folder (%LOCALAPPDATA%, $XDG_CACHE_HOME or ~/.cache), None without one."""
    name = "LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME"
    base = os.environ.get(name, "")
    if not os.path.isabs(base):  # a relative value is ignored, as the XDG spec says
        home = os.path.expanduser("~")
        base = os.path.join(home, "AppData", "Local") if os.name == "nt" else os.path.join(home, ".cache")
    return base if os.path.isabs(base) else None


# Global mode (runner/project.py detect_global: the installed template, outside any project):
# nothing is written into the installed template, not even the bytecode cache of the runner's
# modules (__pycache__ next to them): it goes to the user's cache folder, or nowhere without one.
if os.environ.get("PYTEMPLATE_GLOBAL") == "1" or (_here / "installed.json").is_file():
    _cache = _user_cache()
    if _cache is None:
        sys.dont_write_bytecode = True
    else:
        sys.pycache_prefix = os.path.join(_cache, "pytemplate", "pycache")

sys.path.insert(0, str(_here))

from runner.cli import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
