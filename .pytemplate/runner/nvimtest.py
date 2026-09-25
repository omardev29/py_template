"""selftest --nvim: headless smoke test of the LazyVim integration in an isolated LazyVim.

Installs the LazyVim starter under a throwaway XDG_* tree (never the user's config), trusts
each scratch project's .lazy.lua through Neovim's API, syncs the plugins and runs
.pytemplate/nvim/tests/smoke.lua in a project made from each preset.
"""

from __future__ import annotations

from .config import Config
from .ui import DeployError


def selftest(cfg: Config, args: list[str]) -> int:
    """selftest --nvim [PRESET,...] [--keep]"""
    raise DeployError("selftest --nvim: not implemented yet")
