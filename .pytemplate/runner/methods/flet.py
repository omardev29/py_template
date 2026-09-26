"""flet: `flet build` (Flutter) -> native desktop, mobile or web app.

- Desktop (windows/macos/linux): embeds the CPython minor of python.cpython (the generated
  pyproject pins requires-python = "==X.Y.*": flet build bundles the HIGHEST Python its manifest
  has for the specifier, and fails when it has none for that minor), so the core compiled by
  mypyc (.pyd/.so for that minor) works. It can only be built for the host OS.
- Mobile and web (apk, aab, ipa, web): they cannot load custom extensions, so your
  code is packaged as .py (interpreted), even if the backend is mypyc.
- `flet build` ignores uv.lock: the project it builds carries the EXACT versions
  exported from uv.lock. It installs the Flutter SDK Flet pins the first time (~3 GB in
  ~/flutter).
- On Windows it needs Visual Studio (C++) and Developer Mode turned on.
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

from .. import envs, mypyc, proc, render, ui, upx
from ..cmd_build import BuildRequest, dist_path
from ..config import Config, toml_value
from ..project import BUILD, IS_WINDOWS, PYPROJECT, SRC, host_os
from ..ui import DeployError

MOBILE_WEB = {"apk", "aab", "ipa", "ios-simulator", "web"}
STAGE_APP = "src"  # build() stages the app in <work>/src: [tool.flet.app] path must point there


def _developer_mode() -> bool:
    if sys.platform == "win32":  # not an early return: see cmd_env._long_paths
        import winreg

        path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                value, _ = winreg.QueryValueEx(key, "AllowDevelopmentWithoutDevLicense")
                return bool(value)
        except OSError:
            pass
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
    project's whole [tool.flet] (parsed, so no other table of pyproject.toml leaks into it).

    [tool.flet.app] path is always the staged app folder (flet looks for <work>/<path>/<module>.py
    and aborts, after installing Flutter, when it is elsewhere); every other key is kept.
    requires-python pins the minor of uv.lock and the mypyc build: with ">=3.13" flet bundled
    its newest Python (3.14), which silently ignored the cp313 extensions. [project] description
    is kept (flet puts it in the app's metadata). With [deploy.flet] cleanup = false,
    [tool.flet.cleanup] app and packages default to false: flet cleans the packages unless told
    not to, and its --cleanup-* flags have no negative form.
    """
    project = data["project"]
    tool_flet = copy.deepcopy(data.get("tool", {}).get("flet") or {})
    app = tool_flet.setdefault("app", {})
    if not isinstance(app, dict):
        raise DeployError("pyproject.toml: [tool.flet] app must be a table ([tool.flet.app])")
    if app.get("path", STAGE_APP) != STAGE_APP:
        ui.warn(f"[tool.flet.app] path = {app['path']!r} is ignored: ./deploy build stages the app in {STAGE_APP}/")
    app["path"] = STAGE_APP
    if not cfg.deploy.flet.cleanup:
        cleanup = tool_flet.setdefault("cleanup", {})
        if not isinstance(cleanup, dict):
            raise DeployError("pyproject.toml: [tool.flet] cleanup must be a table ([tool.flet.cleanup])")
        cleanup.setdefault("app", False)
        cleanup.setdefault("packages", False)
    description = project.get("description")
    lines = [
        "[project]",
        f"name = {json.dumps(project['name'])}",
        f"version = {json.dumps(project['version'])}",
        *([f"description = {toml_value(description)}"] if isinstance(description, str) else []),
        f'requires-python = "=={cfg.python.cpython}.*"',
        "dependencies = [",
        *[f"    {json.dumps(p)}," for p in pins],
        "]",
        "",
        "[tool.flet]",
        render.to_toml(tool_flet, "tool.flet"),
    ]
    return "\n".join(lines).rstrip("\n") + "\n"


def build_target(cfg: Config) -> str:
    """The `flet build` target: [deploy.flet] target, with "host" as this OS."""
    target = cfg.deploy.flet.target
    return host_os() if target == "host" else target


def check_options(cfg: Config) -> None:
    """Refuse a flet build that cannot work, before any work: cmd_build calls this before the
    checks and the payload (also in --dry-run), build() again."""
    if cfg.app.preset != "flet":
        raise DeployError("--method flet is for the flet preset (pytemplate.toml app.preset)")
    if IS_WINDOWS and build_target(cfg) == "windows" and not _developer_mode():
        raise DeployError(
            "flet build on Windows needs Developer Mode (Flutter uses symlinks):\n"
            "  Settings > System > For developers > Developer Mode. Meanwhile, use\n"
            "  `./deploy build` (flet pack), which does not need it.",
            3,
        )


def build(req: BuildRequest) -> Path:
    cfg = req.cfg
    check_options(cfg)
    target = build_target(cfg)

    app_dir = req.app_dir
    if req.compiled and target in MOBILE_WEB:
        ui.warn(f"{target}: compiled extensions are not supported; packaging the .py code (interpreted)")
        app_dir = BUILD / "payload" / "flet-src"
        mypyc.sync_tree(SRC, app_dir)

    # Persistent stage: `flet build` keeps its Flutter cache in <stage>/build
    work = BUILD / "flet-build" / req.backend
    work.mkdir(parents=True, exist_ok=True)
    mypyc.sync_tree(app_dir, work / STAGE_APP)
    # sync_tree keeps extensions: drop those of a previous build (a desktop build's .pyd must
    # not reach a mobile/web one), then copy this payload's binaries
    for stale in mypyc.extension_files(work / STAGE_APP):
        stale.unlink()
    for ext in mypyc.extension_files(app_dir):
        target_ext = work / STAGE_APP / ext.relative_to(app_dir)
        target_ext.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ext, target_ext)

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8-sig"))  # an editor or PS 5.1 may add a BOM
    text = build_pyproject(cfg, data, _pinned_requirements(envs.tool_env(cfg)))
    (work / "pyproject.toml").write_text(text, encoding="utf-8", newline="\n")

    out = dist_path(req, f"-{target}")
    argv: list[str | Path] = ["flet", "build", target, work, "--yes", "--output", out]
    if cfg.deploy.flet.cleanup:
        argv += ["--cleanup-app", "--cleanup-packages"]
    if cfg.deploy.flet.exclude:
        argv += ["--exclude", *cfg.deploy.flet.exclude]
    argv += cfg.deploy.flet.extra_args + req.extra
    envs.uv_run(envs.tool_env(cfg), argv, cwd=work)  # FLET_* variables pass through (base_env copies os.environ)
    if not out.exists() and not proc.DRY_RUN:
        raise DeployError("flet build finished without producing the output")
    if target not in MOBILE_WEB and upx.active(cfg):
        upx.pack_tree(cfg, out)  # most Flutter/CPython DLLs are Control Flow Guard: skipped
    return out
