# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Entry point of ./deploy (the launchers run it with `uv run --script`).

Uses only the standard library: uv runs it in an isolated environment, without touching
the project's .venv. All the logic lives in the `runner/` package next to this file.
"""

import sys

# A UV_PYTHON (the launchers clear it; `uv run` by hand does not) or a python.cpython below
# 3.11 makes uv start an older Python, which would crash on tomllib and blame the runner.
if sys.version_info < (3, 11):
    sys.stderr.write(
        f"error: the ./deploy runner needs Python 3.11 or newer, but uv started Python {sys.version.split()[0]}"
        f" ({sys.executable}). Unset UV_PYTHON (or point it at 3.11+) and check python.cpython in pytemplate.toml.\n"
    )
    raise SystemExit(3)

from pathlib import Path  # noqa: E402

# UTF-8 output even when the console/pipe uses cp1252 (paths and tool output may be non-ASCII)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner.cli import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
