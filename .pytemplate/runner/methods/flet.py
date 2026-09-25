"""flet: `flet build` (Flutter) -> native desktop, mobile or web app.

- Desktop (windows/macos/linux): embeds CPython 3.14, so the core compiled
  by mypyc (.pyd/.so cp314) works. It can only be built for the host OS.
- Mobile and web (apk, aab, ipa, web): they cannot load custom extensions, so your
  code is packaged as .py (interpreted), even if the backend is mypyc.
- `flet build` ignores uv.lock: the project it builds carries the EXACT versions
  exported from uv.lock. It downloads the Flutter SDK the first time (~1 GB).
- On Windows it needs Visual Studio (C++) and Developer Mode turned on.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path
from typing import Any

from .. import envs, mypyc, proc, render, ui
from ..cmd_build import BuildRequest, dist_path
from ..config import Config
from ..project import BUILD, IS_WINDOWS, PYPROJECT, SRC, host_os
from ..ui import DeployError

MOBILE_WEB = {"apk", "aab", "ipa", "ios-simulator", "web"}


def _developer_mode() -> bool:
    try:
        import winreg

        path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            value, _ = winreg.QueryValueEx(key, "AllowDevelopmentWithoutDevLicense")
            return bool(value)
    except OSError:
        return False


def _pinned_requirements(cfg_tool: envs.PyEnv) -> list[str]:
    out = envs.uv(
        cfg_tool,
        ["export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes", "--no-header", "--no-annotate", "--format", "requirements.txt"],
        capture=True,
        echo=False,
    ).stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.startswith("#")]


def build_pyproject(cfg: Config, data: dict[str, Any], pins: list[str]) -> str:
    """Return the pyproject.toml that `flet build` reads: the exact versions of uv.lock plus the
    project's whole [tool.flet] (parsed, so no other table of pyproject.toml leaks into it)."""
    project = data["project"]
    tool_flet = data.get("tool", {}).get("flet") or {"app": {"path": "src"}}
    lines = [
        "[project]",
        f"name = {json.dumps(project['name'])}",
        f"version = {json.dumps(project['version'])}",
        f'requires-python = ">={cfg.python.cpython}"',
        "dependencies = [",
        *[f"    {json.dumps(p)}," for p in pins],
        "]",
        "",
        "[tool.flet]",
        render.to_toml(tool_flet, "tool.flet"),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def build(req: BuildRequest) -> Path:
    cfg = req.cfg
    if cfg.app.preset != "flet":
        raise DeployError("--method flet is for the flet preset (pytemplate.toml app.preset)")
    target = cfg.deploy.flet.target
    if target == "host":
        target = host_os()
    if IS_WINDOWS and target == "windows" and not _developer_mode():
        raise DeployError(
            "flet build on Windows needs Developer Mode (Flutter uses symlinks):\n"
            "  Settings > System > For developers > Developer Mode. Meanwhile, use\n"
            "  `./deploy build` (flet pack), which does not need it.",
            3,
        )

    app_dir = req.app_dir
    if req.compiled and target in MOBILE_WEB:
        ui.warn(f"{target}: compiled extensions are not supported; packaging the .py code (interpreted)")
        app_dir = BUILD / "payload" / "flet-src"
        mypyc.sync_tree(SRC, app_dir)

    # Persistent stage: `flet build` keeps its Flutter cache in <stage>/build
    work = BUILD / "flet-build" / req.backend
    work.mkdir(parents=True, exist_ok=True)
    mypyc.sync_tree(app_dir, work / "src")
    # sync_tree keeps extensions: drop those of a previous build (a desktop build's .pyd must
    # not reach a mobile/web one), then copy this payload's binaries
    for stale in mypyc.extension_files(work / "src"):
        stale.unlink()
    for ext in mypyc.extension_files(app_dir):
        target_ext = work / "src" / ext.relative_to(app_dir)
        target_ext.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ext, target_ext)

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    text = build_pyproject(cfg, data, _pinned_requirements(envs.tool_env(cfg)))
    (work / "pyproject.toml").write_text(text, encoding="utf-8", newline="\n")

    out = dist_path(req, f"-{target}")
    argv: list[str | Path] = ["flet", "build", target, work, "--yes", "--output", out]
    argv += cfg.deploy.flet.extra_args + req.extra
    envs.uv_run(envs.tool_env(cfg), argv, cwd=work)  # FLET_* variables pass through (base_env copies os.environ)
    if not out.exists() and not proc.DRY_RUN:
        raise DeployError("flet build finished without producing the output")
    return out
