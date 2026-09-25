"""nvim [doctor|trust|extras|bootstrap|sync]: Neovim/LazyVim integration helper.

The project side needs no install step: `.lazy.lua` (a static copy of
.pytemplate/templates/nvim/lazy.lua) loads the local plugin .pytemplate/nvim/ when Neovim
starts inside the project and the file is trusted. This command checks that setup and does
the one-time steps for the user (trust, LazyVim extras, starter config, plugin sync).
"""

from __future__ import annotations

from collections.abc import Callable

from .config import Config
from .ui import DeployError

Check = Callable[[bool | None, str, str], None]


def cmd_nvim(cfg: Config, args: list[str]) -> int:
    """nvim [doctor|trust|extras|bootstrap|sync]"""
    raise DeployError("nvim: not implemented yet")


def doctor(check: Check) -> None:
    """One-line Neovim/LazyVim summary for ./deploy doctor (no output when Neovim is absent)."""
