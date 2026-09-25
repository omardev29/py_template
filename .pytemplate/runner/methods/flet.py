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

import os
import shutil
import tomllib
from pathlib import Path

from .. import envs, mypyc, proc, ui
from ..cmd_build import BuildRequest, dist_path
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
    for ext in mypyc.extension_files(app_dir):
        target_ext = work / "src" / ext.relative_to(app_dir)
        target_ext.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ext, target_ext)

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    pins = _pinned_requirements(envs.tool_env(cfg))
    text = PYPROJECT.read_text(encoding="utf-8")
    tool_flet = text[text.index("[tool.flet]"):] if "[tool.flet]" in text else '[tool.flet.app]\npath = "src"\n'
    tool_flet = tool_flet.split("# <<< pytemplate-preset")[0]
    lines = [
        "[project]",
        f'name = "{project["name"]}"',
        f'version = "{project["version"]}"',
        f'requires-python = ">={cfg.python.cpython}"',
        "dependencies = [",
        *[f'    "{p}",' for p in pins],
        "]",
        "",
        tool_flet.strip(),
    ]
    (work / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    out = dist_path(req, f"-{target}")
    argv: list[str | Path] = ["flet", "build", target, work, "--yes", "--output", out]
    argv += cfg.deploy.flet.extra_args + req.extra
    env = dict(os.environ)
    envs.uv_run(envs.tool_env(cfg), argv, cwd=work, extra_env={k: v for k, v in env.items() if k.startswith("FLET_")})
    if not out.exists() and not proc.DRY_RUN:
        raise DeployError("flet build finished without producing the output")
    return out
