"""Neovim (LazyVim): the project-local spec .lazy.lua and the data file .pytemplate/editor.json.

.lazy.lua is a verbatim copy of .pytemplate/templates/nvim/lazy.lua and never depends on the
mode: Neovim trusts it by the hash of its bytes (vim.secure), so any change would ask the user
to trust it again. Everything that depends on pytemplate.toml goes to editor.json, which is
data only (the Lua side never runs anything named in it).
"""

from __future__ import annotations

import json
from typing import Any

from .. import render
from ..config import Config
from ..project import TEMPLATES

LAZY_TEMPLATE = TEMPLATES / "nvim" / "lazy.lua"
EDITOR_JSON = ".pytemplate/editor.json"
SCHEMA = 1


def env_dirs(cfg: Config) -> dict[str, str]:
    """Return the environment of each role, relative to the root (without the WSL -wsl suffix)."""
    runtime = ".venv-jit" if cfg.python.jit else ".venv"
    return {"tools": ".venv", "cpython": runtime, "mypyc": runtime, "pypy": ".venv-pypy"}


def editor_data(cfg: Config, profile: str) -> dict[str, Any]:
    """Return the content of .pytemplate/editor.json (schema 1)."""
    data = render.load_profile(profile)
    severity = data.get("vscode", {}).get("mypy-type-checker.severity", {"error": "Error", "note": "Information"})
    return {
        "schema": SCHEMA,
        "generated": render.HEADER,
        "name": cfg.app.name,
        "pkg": cfg.pkg,
        "preset": cfg.app.preset,
        "gui": cfg.app.gui,
        "min_python": cfg.min_python,
        "backend": {"active": cfg.backend.active, "supported": list(cfg.backend.supported)},
        "typing": {
            "profile": profile,
            "editor": cfg.typing.editor,
            "mypy": not data.get("skip_mypy", False),
            "mypy_severity": severity,
        },
        "envs": env_dirs(cfg),
        "mypyc_stage": ".build/mypyc-dev/stage",
        "tasks": [
            {"name": name, "help": task.help, "background": task.background} for name, task in cfg.tasks.items()
        ],
    }


def outputs(cfg: Config, profile: str) -> dict[str, str]:
    """Return the generated Neovim files: {path relative to the root: content}."""
    files = {EDITOR_JSON: json.dumps(editor_data(cfg, profile), indent=2, ensure_ascii=False) + "\n"}
    if LAZY_TEMPLATE.is_file():
        files[".lazy.lua"] = LAZY_TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n")
    return files
