"""VS Code: .vscode/settings.json, extensions.json, launch.json and tasks.json.

Tasks are `"type": "process"` (never through the user's shell): `/bin/sh <root>/deploy ...`
by default and `deploy.cmd` in the `windows` block, so one tasks.json works on every OS.
"""

from __future__ import annotations

import json
from typing import Any

from .. import render
from ..config import Config
from ..project import TEMPLATES


def settings(cfg: Config, profile: str) -> dict[str, Any]:
    base: dict[str, Any] = json.loads((TEMPLATES / "vscode" / "settings.json").read_text("utf-8"))
    base.update(render.load_profile(profile).get("vscode", {}))
    base.update(cfg.vscode.settings)
    return base


def extensions(cfg: Config) -> dict[str, Any]:
    checker = "detachhead.basedpyright" if cfg.typing.editor == "basedpyright" else "ms-python.vscode-pylance"
    recs = [
        "ms-python.python",
        checker,
        "ms-python.debugpy",
        "ms-python.mypy-type-checker",
        "charliermarsh.ruff",
        "tamasfe.even-better-toml",
    ]
    data: dict[str, Any] = {"recommendations": recs}
    if cfg.typing.editor == "basedpyright":
        data["unwantedRecommendations"] = ["ms-python.vscode-pylance"]
    return data


def launch(cfg: Config) -> dict[str, Any]:
    main = {
        "name": "src/main.py (CPython, interpreted)",
        "type": "debugpy",
        "request": "launch",
        "program": "${workspaceFolder}/src/main.py",
        "cwd": "${workspaceFolder}",
        "console": "integratedTerminal",
        "justMyCode": True,
    }
    configs: list[dict[str, Any]] = [main]
    if cfg.pypy_enabled:
        configs.append(
            {
                **main,
                "name": "src/main.py (PyPy, experimental: the debugger is unreliable on PyPy)",
                "python": "${workspaceFolder}/.venv-pypy/bin/python",
                "windows": {"python": "${workspaceFolder}/.venv-pypy/Scripts/python.exe"},
            }
        )
    configs.append(
        {
            "name": "Tests (pytest)",
            "type": "debugpy",
            "request": "launch",
            "module": "pytest",
            "cwd": "${workspaceFolder}",
            "console": "integratedTerminal",
            "justMyCode": False,
        }
    )
    return {"version": "0.2.0", "configurations": configs}


def tasks(cfg: Config) -> dict[str, Any]:
    def task(label: str, args: list[str], group: dict[str, Any] | None = None) -> dict[str, Any]:
        t: dict[str, Any] = {
            "label": f"deploy: {label}",
            "type": "process",
            "command": "${workspaceFolder}/deploy",
            "windows": {"command": "${workspaceFolder}\\deploy.cmd"},
            "args": args,
            "options": {"cwd": "${workspaceFolder}"},
            "problemMatcher": [],
        }
        if group:
            t["group"] = group
        return t

    out = [
        task("run", ["run"]),
        task("test", ["test"], {"kind": "test", "isDefault": True}),
        task("check", ["check"]),
        task("build", ["build"], {"kind": "build", "isDefault": True}),
    ]
    for b in cfg.backend.supported:
        if b != cfg.backend.active:
            out.append(task(f"run {b}", ["run", b]))
    if cfg.supports("mypyc"):
        out.append(task("report (mypyc)", ["report", "--open"]))
    return {"version": "2.0.0", "tasks": out}


def outputs(cfg: Config, profile: str) -> dict[str, str]:
    """Return the generated VS Code files: {path relative to the root: content}."""
    return {
        ".vscode/settings.json": render.jsonc(settings(cfg, profile)),
        ".vscode/extensions.json": render.jsonc(extensions(cfg)),
        ".vscode/launch.json": render.jsonc(launch(cfg)),
        ".vscode/tasks.json": render.jsonc(tasks(cfg)),
    }
