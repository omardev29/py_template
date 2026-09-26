"""Neovim (LazyVim): the project-local spec .lazy.lua and the data file .pytemplate/editor.json.

.lazy.lua is a verbatim copy of .pytemplate/templates/nvim/lazy.lua and never depends on the
mode: Neovim trusts it by the hash of its bytes (vim.secure), so any change would ask the user
to trust it again. Everything that depends on pytemplate.toml goes to editor.json, which is
data only: the plugin in .pytemplate/nvim/ validates every value it reads (whitelists and
patterns) and never runs a program named in it. No absolute or machine-specific paths.
"""

from __future__ import annotations

import json
from typing import Any

from .. import render
from ..config import METHODS, Config
from ..project import TEMPLATES, rel
from ..ui import DeployError

LAZY_TEMPLATE = TEMPLATES / "nvim" / "lazy.lua"
LAZY_LUA = ".lazy.lua"
EDITOR_JSON = ".pytemplate/editor.json"
SCHEMA = 1
DEFAULT_SEVERITY = {"error": "Error", "note": "Information"}
BOM = "\ufeff"  # an editor may add one: it would change the trusted hash


def env_dirs() -> dict[str, str]:
    """Return the environment of each role, relative to the root (without the WSL -wsl suffix)."""
    return {"tools": ".venv", "cpython": ".venv", "mypyc": ".venv", "pypy": ".venv-pypy"}


def commands() -> list[dict[str, str]]:
    """Return the ./deploy commands (name, usage, summary, group) for the editor's task list."""
    from ..cli import COMMANDS

    return [{"name": n, "usage": c.usage, "summary": c.summary, "group": c.group} for n, c in COMMANDS.items()]


def editor_data(cfg: Config, profile: str) -> dict[str, Any]:
    """Return the content of .pytemplate/editor.json (schema 1)."""
    from ..cmd_dev import BASEDPYRIGHT  # lazily, like commands(): render imports this module

    data = render.load_profile(profile)
    severity = data.get("vscode", {}).get("mypy-type-checker.severity", DEFAULT_SEVERITY)
    return {
        "schema": SCHEMA,
        "generated": render.HEADER,
        "name": cfg.app.name,
        "pkg": cfg.pkg,
        "preset": cfg.app.preset,
        "gui": cfg.app.gui,
        "min_python": cfg.min_python,
        "pypy_enabled": cfg.pypy_enabled,
        "backend": {"active": cfg.backend.active, "supported": list(cfg.backend.supported)},
        "typing": {
            "profile": profile,
            "editor": cfg.typing.editor,
            "mypy": not data.get("skip_mypy", False),
            "mypy_severity": severity,
            # Same as render.mypy_cli_args: with PyPy supported mypy checks the 3.11 syntax
            "python_version": cfg.min_python if cfg.pypy_enabled else None,
            # the plugin's uvx language server runs the basedpyright ./deploy check pins
            "basedpyright": BASEDPYRIGHT,
        },
        "envs": env_dirs(),
        "mypyc_stage": ".build/mypyc-dev/stage",
        "tasks": [
            {"name": name, "help": task.help, "background": task.background} for name, task in cfg.tasks.items()
        ],
        "commands": commands(),
        "build": {"methods": list(METHODS), "default": dict(cfg.deploy.default)},
    }


def outputs(cfg: Config, profile: str) -> dict[str, str]:
    """Return the generated Neovim files: {path relative to the root: content}."""
    files = {EDITOR_JSON: json.dumps(editor_data(cfg, profile), indent=2, ensure_ascii=True) + "\n"}
    if LAZY_TEMPLATE.is_file():
        files[LAZY_LUA] = lazy_lua()
    return files


def lazy_lua() -> str:
    """Return .lazy.lua: the template without a BOM, with LF (the bytes Neovim trusts)."""
    try:
        text = LAZY_TEMPLATE.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise DeployError(f"{rel(LAZY_TEMPLATE)} is not UTF-8 text: save it as UTF-8") from None
    except OSError as e:
        raise DeployError(f"cannot read {rel(LAZY_TEMPLATE)}: {e.strerror or e}") from None
    return text.lstrip(BOM).replace("\r\n", "\n")
