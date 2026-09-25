"""selftest --e2e: end-to-end test of the template.

Creates a project from each preset with ./deploy new (in a short temporary path), then runs
setup, check all, test all and a build for each supported backend and compatible method.
"""

from __future__ import annotations

from .config import Config
from .ui import DeployError


def selftest(cfg: Config, args: list[str]) -> int:
    """selftest --e2e [PRESET,...] [--keep] [--quick]"""
    raise DeployError("selftest --e2e: not implemented yet")
