"""Presets (script, raylib, flet): esqueleto de código + dependencias + config inicial.

Cada preset vive en .pytemplate/presets/<nombre>/:
  preset.toml   descripción, dependencias, claves extra de [tool.uv] y tablas extra de pyproject
  files/        esqueleto que se copia a la raíz. `__pkg__` en una ruta se sustituye por el
                paquete de la app, y en los textos `{{name}}`/`{{pkg}}` por nombre y paquete.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import proc, ui
from .project import CONFIG_FILE, PRESETS, PYPROJECT, ROOT
from .ui import DeployError

if TYPE_CHECKING:
    from .config import Config

TEXT_SUFFIXES = {".py", ".pyi", ".toml", ".md", ".txt", ".json", ".cfg", ".ini", ".yml", ".yaml", ""}
EXTRA_BEGIN = "# >>> pytemplate-preset"
EXTRA_END = "# <<< pytemplate-preset"
# Carpetas que pertenecen al preset: se reemplazan enteras al cambiar de preset
OWNED_DIRS = ("src", "tests", "typings")


def available() -> list[str]:
    return sorted(p.name for p in PRESETS.iterdir() if (p / "preset.toml").is_file())


def load(name: str) -> dict[str, Any]:
    path = PRESETS / name / "preset.toml"
    if not path.is_file():
        raise DeployError(f"no existe el preset '{name}' (disponibles: {', '.join(available())})")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _fmt(value: Any, options: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format_map(options)
    if isinstance(value, list):
        return [_fmt(v, options) for v in value]
    return value


def options(cfg: Config) -> dict[str, Any]:
    """Opciones del preset: los valores por defecto de preset.toml + [preset.<nombre>] del usuario."""
    data = load(cfg.app.preset)
    merged: dict[str, Any] = dict(data.get("options", {}))
    merged.update(cfg.preset_options(cfg.app.preset))
    return merged


def uv_extras(cfg: Config) -> dict[str, Any]:
    """Claves extra del bloque gestionado de [tool.uv] (p. ej. no-build-package para raylib)."""
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


# --- archivos del esqueleto --------------------------------------------------------------------


def skeleton(preset: str, name: str) -> dict[str, bytes]:
    """Archivos del preset ya personalizados: ruta relativa -> contenido."""
    pkg = name.replace("-", "_").lower()
    base = PRESETS / preset / "files"
    out: dict[str, bytes] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(base).as_posix().replace("__pkg__", pkg)
        data = path.read_bytes()
        if path.suffix in TEXT_SUFFIXES:
            text = data.decode("utf-8").replace("{{name}}", name).replace("{{pkg}}", pkg)
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
                out[path.relative_to(ROOT).as_posix()] = path.read_bytes().replace(b"\r\n", b"\n")
    return out


def pristine(cfg: Config) -> bool:
    """¿src/, tests/ y typings/ son exactamente el esqueleto del preset actual (sin tocar)?"""
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
        raise DeployError("el nombre solo admite letras, números, '-' y '_' (y empieza por letra)")
    target = load(preset)
    if not force and not pristine(cfg):
        raise DeployError(
            "src/, tests/ o typings/ tienen cambios respecto al esqueleto del preset actual "
            f"('{cfg.app.preset}'). init los reemplazaría.\n  Si estás seguro: ./deploy init {preset} --force"
        )
    old_deps, old_dev = dependencies(cfg)

    ui.step(f"preset {preset} ({target.get('description', '')}) como '{new_name}'")
    for d in OWNED_DIRS:
        shutil.rmtree(ROOT / d, ignore_errors=True)
    for rel, data in skeleton(preset, new_name).items():
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ui.detail(f"  + {rel}")
    if os.name != "nt":
        for script in ("deploy",):
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
    ui.ok(f"preset '{preset}' listo. Siguiente paso: ./deploy setup && ./deploy run")


def copy_template(dest: Path) -> None:
    """Copia la plantilla (sin entornos, builds ni historial) a `dest`."""
    skip_names = {".git", ".build", "dist", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".flet"}

    def ignore(directory: str, names: list[str]) -> set[str]:
        return {n for n in names if n in skip_names or n.startswith(".venv")}

    if dest.exists() and any(dest.iterdir()):
        raise DeployError(f"{dest} ya existe y no está vacío")
    shutil.copytree(ROOT, dest, ignore=ignore, dirs_exist_ok=True)


def new(dest: Path, preset: str, name: str | None) -> None:
    dest = dest.resolve()
    app_name = name or re.sub(r"[^A-Za-z0-9_-]", "-", dest.name)
    load(preset)
    ui.step(f"nuevo proyecto en {dest}")
    copy_template(dest)
    proc.run(
        [proc.find_uv(), "run", "--quiet", "--script", dest / ".pytemplate" / "deploy.py", "init", preset, "--name", app_name, "--force"],
        cwd=dest,
    )
    if shutil.which("git") and not (dest / ".git").exists():
        proc.run(["git", "init", "--quiet"], cwd=dest, check=False)
        proc.run(["git", "add", "--chmod=+x", "deploy"], cwd=dest, check=False)
    ui.ok(f"proyecto creado. cd {dest} && ./deploy setup")


def config_text(preset: str, name: str) -> str | None:
    data = skeleton(preset, name).get(CONFIG_FILE.name)
    return data.decode("utf-8") if data else None
