"""rename NEW_NAME [--force]: rename the app and its Python package everywhere.

Renames src/<pkg>/ and rewrites every reference to the old name/package in src/, tests/,
pytemplate.toml and pyproject.toml, then re-locks uv.lock and regenerates the generated files.
"""

from __future__ import annotations

from .config import Config
from .ui import DeployError


def cmd_rename(cfg: Config, args: list[str]) -> int:
    """rename NEW_NAME [--force]"""
    raise DeployError("rename: not implemented yet")
