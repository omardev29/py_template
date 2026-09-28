"""flet: `flet build` (Flutter) -> native desktop, mobile or web app.

- Desktop (windows/macos/linux): embeds the CPython minor of python.cpython (the generated
  pyproject pins requires-python = "==X.Y.*": flet build bundles the HIGHEST Python its manifest
  has for the specifier, and fails when it has none for that minor), so the core compiled by
  mypyc (.pyd/.so for that minor) works. It can only be built for the host OS.
- Mobile and web (apk, aab, ipa, web): they cannot load custom extensions, so your
  code is packaged as .py (interpreted), even if the backend is mypyc.
- `flet build` ignores uv.lock: the project it builds carries the EXACT versions
  exported from uv.lock, but for mobile and web targets a package without a pure wheel (msgpack)
  keeps only the project's own bounds: flet build takes their binaries from Flet's own index,
  which may not hold uv.lock's release (common.unpin_binaries). flet-desktop is left out (no
  flet build app starts that client), and for mobile and web targets the platform markers are
  written the way flet build's pip reads them (target_markers). It installs the Flutter SDK Flet
  pins the first time (~3 GB in ~/flutter).
- On Windows it needs Visual Studio (C++) and Developer Mode turned on.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from .. import envs, mypyc, proc, render, ui, upx
from ..cmd_build import BuildRequest, dist_path
from ..config import Config, toml_value
from ..project import BUILD, IS_WINDOWS, PYPROJECT, SRC, host_os
from ..ui import PytError
from . import common

MOBILE_WEB = {"apk", "aab", "ipa", "ios-simulator", "web"}
STAGE_APP = "src"  # build() stages the app in <work>/src: [tool.flet.app] path must point there
DESKTOP_CLIENT = "flet-desktop"  # the client `flet run` and `flet pack` start: no flet build app does
# sys.platform values as platform.system() names the same platform: what target_markers writes
PLATFORM_SYSTEM = {"android": "Android", "darwin": "Darwin", "emscripten": "Emscripten", "ios": "iOS", "linux": "Linux", "win32": "Windows"}
_SYS_PLATFORM_MARKER = re.compile(r"\bsys_platform\s*(==|!=)\s*(['\"])([^'\"]*)\2")
_OS_NAME_MARKER = re.compile(r"\bos_name\s*(==|!=)\s*(['\"])(nt|posix)\2")


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
    """The locked runtime requirements, for the build project's [project] dependencies.

    --no-editable, and a local library as `name @ file:///absolute/path` (common.direct_reference):
    exported editable it was `-e ./libs/x ; <markers>`, no PEP 508 requirement (pip refused it),
    and a relative path would point into the build stage, not the project. Without
    `DESKTOP_CLIENT` and what only it needs (`--prune`: what the app requires itself stays): a
    flet build app runs embedded in its own Flutter host (Pyodide on the web), which never starts
    the client of `flet run` and `flet pack`; with rich and pygments it was 3.1 of the 5.6 MB of
    a web build's app.zip. No dependency group (--no-default-groups, as common.export_requirements:
    --no-dev kept a group [tool.uv] default-groups names, a lint group's ruff in an apk build).
    """
    out = envs.uv(
        cfg_tool,
        ["export", "--frozen", "--no-default-groups", "--no-editable", "--no-emit-project", "--prune", DESKTOP_CLIENT, "--no-hashes", "--no-header", "--no-annotate", "--format", "requirements.txt"],
        capture=True,
        echo=False,
    ).stdout
    return [common.direct_reference(ln.strip()) for ln in out.splitlines() if ln.strip() and not ln.startswith("#")]


def target_markers(pins: list[str]) -> list[str]:
    """For a mobile or web target: the `sys_platform` and `os_name` markers of the pins as
    `platform_system` ones, which mean the same on the target (every mobile and web target is
    POSIX: `os_name == 'nt'` is Windows).

    serious_python runs flet build's pip on the build machine with only `platform.system()` faked
    for the target (bin/sitecustomize.dart), and uv writes a `platform_system` marker as a
    `sys_platform` one (flet's `platform_system != "Emscripten"`, a user's `platform_system ==
    "Android"`): pip read the build machine's sys_platform and os_name. Every web app got httpx and
    its tree, which flet leaves out in the browser; an app's Android-only requirement was left out
    of the .apk, and a Linux-only one went in (a native one failed the build: no Android wheel).
    A value platform.system() has no fixed name for (`cygwin`, `freebsd14`) stays (CLAUDE.md 15.1).
    """

    def system(m: re.Match[str]) -> str:
        name = PLATFORM_SYSTEM.get(m[3])
        return f"platform_system {m[1]} '{name}'" if name else m[0]

    def windows(m: re.Match[str]) -> str:
        same = (m[1] == "==") == (m[3] == "nt")  # os_name == 'nt' <=> platform_system == 'Windows'
        return f"platform_system {'==' if same else '!='} 'Windows'"

    out = []
    for pin in pins:
        requirement, marked, marker = pin.partition(" ;")
        if marked:
            marker = _OS_NAME_MARKER.sub(windows, _SYS_PLATFORM_MARKER.sub(system, marker))
        out.append(requirement + marked + marker)
    return out


_LOWER_BOUND = re.compile(r">=|~=|==|>(?!=)")


def relaxed_message(target: str, relaxed: list[tuple[str, str]]) -> str:
    """What `common.unpin_binaries` did for a mobile or web target, and where pip looks instead:
    Flet's own index (pypi.flet.dev), and for the web first the packages of the Pyodide release
    flet build uses (serious_python serves them to pip). A lower bound the project keeps may be
    newer than they hold: `./pyt add numpy` writes `numpy>=<the newest release on PyPI>`."""
    source = "the packages of its Pyodide release and Flet's own index, pypi.flet.dev" if target == "web" else "Flet's own index, pypi.flet.dev"
    shown = ", ".join(f"{old} -> {new}" for old, new in relaxed)
    bounded = [new for _, new in relaxed if _LOWER_BOUND.search(new)]
    hint = (
        f"; a lower bound may be newer than it holds ({', '.join(bounded)}: `./pyt add` writes the newest release on PyPI as one):"
        " if pip finds no release, lower it in pyproject.toml, then ./pyt lock"
        if bounded
        else ""
    )
    return f"{target}: flet build takes this target's binary packages from {source}, which may not hold uv.lock's versions: {shown} (pip picks a release that fits every package's bounds{hint})"


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
        raise PytError("pyproject.toml: [tool.flet] app must be a table ([tool.flet.app])")
    if app.get("path", STAGE_APP) != STAGE_APP:
        ui.warn(f"[tool.flet.app] path = {app['path']!r} is ignored: ./pyt build stages the app in {STAGE_APP}/")
    app["path"] = STAGE_APP
    if not cfg.deploy.flet.cleanup:
        cleanup = tool_flet.setdefault("cleanup", {})
        if not isinstance(cleanup, dict):
            raise PytError("pyproject.toml: [tool.flet] cleanup must be a table ([tool.flet.cleanup])")
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


def plan_lines(cfg: Config) -> list[str]:
    """What `./pyt --dry-run build B --method flet` says about the build project: its target
    and, for a mobile or web one, the markers target_markers writes and the pins
    common.unpin_binaries relaxes (the warning of a real build). Reads uv.lock (uv export)."""
    target = build_target(cfg)
    lines = [f"flet build {target}"]
    if target not in MOBILE_WEB:
        return lines
    pins = _pinned_requirements(envs.tool_env(cfg))
    marked = target_markers(pins)
    changed = [new for old, new in zip(pins, marked, strict=True) if old != new]
    if changed:
        lines.append(f"{target}: markers written as platform_system ones (what flet build's pip reads): {'; '.join(changed)}")
    _, relaxed = common.unpin_binaries(marked)
    if relaxed:
        lines.append(relaxed_message(target, relaxed))
    return lines


def build_target(cfg: Config) -> str:
    """The `flet build` target: [deploy.flet] target, with "host" as this OS."""
    target = cfg.deploy.flet.target
    return host_os() if target == "host" else target


def check_options(cfg: Config) -> None:
    """Refuse a flet build that cannot work, before any work: cmd_build calls this before the
    checks and the payload (also in --dry-run), build() again."""
    if cfg.app.preset != "flet":
        raise PytError("--method flet is for the flet preset (pytemplate.toml app.preset)")
    if IS_WINDOWS and build_target(cfg) == "windows" and not _developer_mode():
        raise PytError(
            "flet build on Windows needs Developer Mode (Flutter uses symlinks):\n"
            "  Settings > System > For developers > Developer Mode. Meanwhile, use\n"
            "  `./pyt build` (flet pack), which does not need it.",
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
        mypyc.make_writable(stale)  # a read-only copy an older ./pyt made: Windows deletes none
        stale.unlink()
    for ext in mypyc.extension_files(app_dir):
        target_ext = work / STAGE_APP / ext.relative_to(app_dir)
        target_ext.parent.mkdir(parents=True, exist_ok=True)
        mypyc.copy_writable(str(ext), str(target_ext))

    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8-sig"))  # an editor or PS 5.1 may add a BOM
    pins = _pinned_requirements(envs.tool_env(cfg))
    if target in MOBILE_WEB:
        pins = target_markers(pins)
        pins, relaxed = common.unpin_binaries(pins)
        if relaxed:
            ui.warn(relaxed_message(target, relaxed))
    text = build_pyproject(cfg, data, pins)
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
        raise PytError("flet build finished without producing the output")
    if target not in MOBILE_WEB and upx.active(cfg):
        upx.pack_tree(cfg, out)  # most Flutter/CPython DLLs are Control Flow Guard: skipped
    return out
