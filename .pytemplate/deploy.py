# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Entry point of ./deploy (the launchers run it with `uv run --script`).

Uses only the standard library: uv runs it in an isolated environment, without touching
the project's .venv. All the logic lives in the `runner/` package next to this file.
"""

import sys
from pathlib import Path

# UTF-8 output even when the console/pipe uses cp1252 (paths and tool output may be non-ASCII)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner.cli import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
