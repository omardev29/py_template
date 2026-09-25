"""hooks [install|uninstall|run|status]: the git pre-commit hook of the project.

The hook is a tiny sh script in the repository's hooks directory that runs
`./deploy hooks run`: fast checks of the STAGED files (ruff, ruff format, generated files and
uv.lock up to date, mypyc rules, launchers). `./deploy setup` installs it when
`hooks.pre_commit` is true in pytemplate.toml (the default).
"""

from __future__ import annotations

from collections.abc import Callable

from .config import Config
from .ui import DeployError

Check = Callable[[bool | None, str, str], None]


def cmd_hooks(cfg: Config, args: list[str]) -> int:
    """hooks [install|uninstall|run|status]"""
    raise DeployError("hooks: not implemented yet")


def ensure_installed(cfg: Config) -> None:
    """Called by ./deploy setup: install the hook if hooks.pre_commit and it is missing."""


def doctor(cfg: Config, check: Check) -> None:
    """One line for ./deploy doctor: whether the hook is installed."""
