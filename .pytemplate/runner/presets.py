"""Presets (script, raylib, flet): code skeleton + dependencies + initial config.

Each preset lives in .pytemplate/presets/<name>/:
  preset.toml   description, dependencies, extra [tool.uv] keys and extra pyproject tables
  files/        skeleton copied to the root. `__pkg__` in a path is replaced with the app
                package, and `{{name}}`/`{{pkg}}` in text with the name and the package.
"""

from __future__ import annotations

import keyword
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import proc, ui
from .project import PRESETS, PYPROJECT, ROOT
from .ui import DeployError

if TYPE_CHECKING:
    from .config import Config

# Files with these suffixes (or none, e.g. LICENSE) get token replacement and LF line endings,
# but only if they are really text (see _text_of); anything else is copied byte for byte
TEXT_SUFFIXES = {".py", ".pyi", ".toml", ".md", ".txt", ".json", ".cfg", ".ini", ".yml", ".yaml"}
EXTRA_BEGIN = "# >>> pytemplate-preset"
EXTRA_END = "# <<< pytemplate-preset"
# Folders owned by the preset: replaced as a whole when switching presets
OWNED_DIRS = ("src", "tests", "typings")


def available() -> list[str]:
    return sorted(p.name for p in PRESETS.iterdir() if (p / "preset.toml").is_file())


def load(name: str) -> dict[str, Any]:
    path = PRESETS / name / "preset.toml"
    if not path.is_file():
        raise DeployError(f"unknown preset '{name}' (available: {', '.join(available())})")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _fmt(value: Any, options: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format_map(options)
    if isinstance(value, list):
        return [_fmt(v, options) for v in value]
    return value


def options(cfg: Config) -> dict[str, Any]:
    """Return the preset options: the preset.toml defaults + the user's [preset.<name>]."""
    data = load(cfg.app.preset)
    merged: dict[str, Any] = dict(data.get("options", {}))
    merged.update(cfg.preset_options(cfg.app.preset))
    return merged


def uv_extras(cfg: Config) -> dict[str, Any]:
    """Return the extra keys of the managed [tool.uv] block (e.g. no-build-package for raylib)."""
    data = load(cfg.app.preset)
    opts = options(cfg)
    return {k: _fmt(v, opts) for k, v in data.get("uv", {}).items()}


def dependencies(cfg: Config, name: str | None = None) -> tuple[list[str], list[str]]:
    preset = name or cfg.app.preset
    data = load(preset)
    opts: dict[str, Any] = dict(data.get("options", {}))
    if preset == cfg.app.preset:
        opts.update(cfg.preset_options(preset))
    deps = [str(_fmt(d, opts)) for d in data.get("dependencies", [])]
    dev = [str(_fmt(d, opts)) for d in data.get("dev_dependencies", [])]
    return deps, dev


# --- skeleton files ----------------------------------------------------------------------------


def _text_of(path: Path, data: bytes) -> str | None:
    """Return the file as text, or None if it must be treated as binary."""
    if path.suffix not in TEXT_SUFFIXES and path.suffix != "":
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def skeleton(preset: str, name: str) -> dict[str, bytes]:
    """Return the preset files, already customized: relative path -> content."""
    pkg = name.replace("-", "_").lower()
    base = PRESETS / preset / "files"
    out: dict[str, bytes] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(base).as_posix().replace("__pkg__", pkg)
        data = path.read_bytes()
        text = _text_of(path, data)
        if text is not None:
            text = text.replace("{{name}}", name).replace("{{pkg}}", pkg)
            data = text.replace("\r\n", "\n").encode("utf-8")
        out[rel] = data
    return out


def _owned_files() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for d in OWNED_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and not any(p in path.parts for p in ("__pycache__", ".pytest_cache")):
                data = path.read_bytes()
                if _text_of(path, data) is not None:  # git may check text files out with CRLF
                    data = data.replace(b"\r\n", b"\n")
                out[path.relative_to(ROOT).as_posix()] = data
    return out


def pristine(cfg: Config) -> bool:
    """Return whether src/, tests/ and typings/ are exactly the current preset's skeleton (untouched)."""
    expected = {k: v for k, v in skeleton(cfg.app.preset, cfg.app.name).items() if k.split("/")[0] in OWNED_DIRS}
    return _owned_files() == expected


# --- pyproject ---------------------------------------------------------------------------------


def _norm_name(req: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else req


def _declared(group: str | None) -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    if group is None:
        reqs = data.get("project", {}).get("dependencies", [])
    else:
        reqs = data.get("dependency-groups", {}).get(group, [])
    return {_norm_name(r) for r in reqs if isinstance(r, str)}


def _dependency_names(cfg: Config | None, preset: str) -> set[str]:
    """Return the normalized names of every dependency the project has after `init preset`."""
    target = load(preset)
    opts: dict[str, Any] = dict(target.get("options", {}))
    if cfg is not None and preset == cfg.app.preset:
        opts.update(cfg.preset_options(preset))
    new = [str(_fmt(d, opts)) for d in (*target.get("dependencies", []), *target.get("dev_dependencies", []))]
    declared = _declared(None) | _declared("dev")
    if cfg is not None:  # the current preset's own dependencies are removed by init
        old_deps, old_dev = dependencies(cfg)
        declared -= {_norm_name(d) for d in (*old_deps, *old_dev)}
    return {_norm_name(d) for d in new} | declared


def check_name_free(cfg: Config | None, preset: str, name: str) -> None:
    """Reject an app name that is also a dependency's name (e.g. `flet`, `raylib`, `rich`).

    uv refuses a project that depends on itself ("self-dependencies are not permitted"), and
    src/<pkg>/ would shadow the library's own import.
    """
    pkg = name.replace("-", "_").lower()
    if keyword.iskeyword(pkg):
        raise DeployError(f"the package '{pkg}' would be a Python keyword (`import {pkg}` is a syntax error).\n  Choose another name with --name NAME")
    if pkg in sys.stdlib_module_names:
        raise DeployError(f"src/{pkg}/ would shadow the standard library module '{pkg}'.\n  Choose another name with --name NAME")
    clash = _norm_name(name)
    if clash in _dependency_names(cfg, preset):
        raise DeployError(
            f"the app name '{name}' is also the name of a dependency of the '{preset}' preset "
            f"({clash}): uv would refuse the project and src/{name.replace('-', '_').lower()}/ would "
            "shadow the library.\n  Choose another name with --name NAME"
        )


def _set_project_name(text: str, name: str) -> str:
    return re.sub(r'(?m)^(name\s*=\s*)"[^"]*"', lambda m: f'{m.group(1)}"{name}"', text, count=1)


def _set_extra_tables(text: str, extra: str) -> str:
    lines = text.rstrip("\n").splitlines()
    begin = next((i for i, ln in enumerate(lines) if ln.strip() == EXTRA_BEGIN), None)
    end = next((i for i, ln in enumerate(lines) if ln.strip() == EXTRA_END), None)
    if begin is not None and end is not None:
        del lines[begin : end + 1]
        while lines and not lines[-1].strip():
            lines.pop()
    if extra.strip():
        lines += ["", EXTRA_BEGIN, *extra.strip("\n").splitlines(), EXTRA_END]
    return "\n".join(lines) + "\n"


# --- init / new --------------------------------------------------------------------------------


def init(cfg: Config, preset: str, name: str | None, *, force: bool) -> None:
    from . import config, envs, render

    new_name = name or cfg.app.name
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", new_name):
        raise DeployError("the name may only contain letters, digits, '-' and '_' (and must start with a letter)")
    check_name_free(cfg, preset, new_name)
    target = load(preset)
    if not force and not pristine(cfg):
        raise DeployError(
            "src/, tests/ or typings/ have changes compared to the skeleton of the current preset "
            f"('{cfg.app.preset}'). init would replace them.\n  If you are sure: ./deploy init {preset} --force"
        )
    old_deps, old_dev = dependencies(cfg)

    ui.step(f"preset {preset} ({target.get('description', '')}) as '{new_name}'")
    for d in OWNED_DIRS:
        shutil.rmtree(ROOT / d, ignore_errors=True)
    for rel, data in skeleton(preset, new_name).items():
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ui.detail(f"  + {rel}")
    if os.name != "nt":
        for script in ("deploy", "deploy.ps1"):
            p = ROOT / script
            if p.exists():
                p.chmod(p.stat().st_mode | 0o111)

    text = PYPROJECT.read_text(encoding="utf-8").replace("\r\n", "\n")
    text = _set_project_name(text, new_name)
    text = _set_extra_tables(text, str(target.get("pyproject", "")).replace("{{name}}", new_name).replace("{{pkg}}", new_name.replace("-", "_").lower()))
    PYPROJECT.write_text(text, encoding="utf-8", newline="\n")

    new_cfg = config.load()
    render.write_pyproject(new_cfg)

    uv = proc.find_uv()
    new_deps, new_dev = dependencies(new_cfg, preset)
    keep = {_norm_name(d) for d in new_deps}
    keep_dev = {_norm_name(d) for d in new_dev}
    drop = [n for n in {_norm_name(d) for d in old_deps} if n not in keep and n in _declared(None)]
    drop_dev = [n for n in {_norm_name(d) for d in old_dev} if n not in keep_dev and n in _declared("dev")]
    env = envs.env_vars(envs.tool_env(new_cfg))
    if drop:
        proc.run([uv, "remove", "--no-sync", *drop], env=env)
    if drop_dev:
        proc.run([uv, "remove", "--no-sync", "--dev", *drop_dev], env=env)
    if new_deps:
        proc.run([uv, "add", "--no-sync", *new_deps], env=env)
    if new_dev:
        proc.run([uv, "add", "--no-sync", "--dev", *new_dev], env=env)
    proc.run([uv, "lock"], env=env)
    render.apply(new_cfg, force=True)
    ui.ok(f"preset '{preset}' done. Next step: ./deploy setup && ./deploy run")


def copy_template(dest: Path) -> None:
    """Copy the template (without environments, builds or history) to `dest`."""
    # template-repo: marker of the template repository itself (enables the language guard test)
    skip_names = {".git", ".build", "dist", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".flet", "template-repo"}

    here_root = ROOT.as_posix()
    workflows = (ROOT / ".github" / "workflows").as_posix()

    def ignore(directory: str, names: list[str]) -> set[str]:
        out = {n for n in names if n in skip_names or n.startswith(".venv")}
        here = Path(directory).as_posix()
        if here == here_root:
            # PyInstaller/Flet leftovers and Claude Code state (settings, agent worktrees)
            out |= {n for n in names if n in ("build", ".claude")}
        elif here == workflows:
            # CI of the template repository itself (template-*.yml), not of the new project
            out |= {n for n in names if n.startswith("template-")}
        return out

    if dest.exists() and any(dest.iterdir()):
        raise DeployError(f"{dest} already exists and is not empty")
    shutil.copytree(ROOT, dest, ignore=ignore, dirs_exist_ok=True)


def new(dest: Path, preset: str, name: str | None) -> None:
    dest = dest.resolve()
    app_name = name or re.sub(r"[^A-Za-z0-9_-]", "-", dest.name)
    load(preset)
    ui.step(f"new project in {dest}")
    copy_template(dest)
    proc.run(
        [proc.find_uv(), "run", "--quiet", "--script", dest / ".pytemplate" / "deploy.py", "init", preset, "--name", app_name, "--force"],
        cwd=dest,
    )
    # no nested repository when the destination is already inside one (a monorepo)
    inside = shutil.which("git") and proc.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=dest.parent, capture=True, check=False, echo=False
    ).returncode == 0
    if shutil.which("git") and not inside and not (dest / ".git").exists():
        proc.run(["git", "init", "--quiet"], cwd=dest, check=False)
        proc.run(["git", "add", "--chmod=+x", "deploy", "deploy.ps1"], cwd=dest, check=False)
    ui.ok(f"project created. cd {dest} && ./deploy setup")
