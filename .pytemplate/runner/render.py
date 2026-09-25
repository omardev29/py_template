"""Genera los archivos de configuración a partir de pytemplate.toml + .pytemplate/templates.

Archivos generados (se commitean; NO se editan a mano):
  .python-version, .mypy.ini, .ruff.toml, pyrightconfig.json, .vscode/*.json,
  .github/workflows/ci.yml

.pytemplate/state.json guarda el hash (normalizado a LF) de lo último que escribimos:
si un archivo generado difiere de ese hash, alguien lo editó a mano y no se pisa sin
--force. Además, pyproject.toml tiene dos partes gestionadas (requires-python y un
bloque de [tool.uv] entre marcas) que solo se reescriben con `mode`/`lock`, porque
cambiarlas obliga a re-bloquear uv.lock.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from . import presets, ui
from .config import Config, compiled_paths
from .project import PYPROJECT, ROOT, SRC, STATE_FILE, TEMPLATES
from .ui import DeployError

HEADER = "GENERADO por ./deploy a partir de pytemplate.toml y .pytemplate/templates: no lo edites a mano"
MARK_BEGIN = "# >>> pytemplate"
MARK_END = "# <<< pytemplate"


# --- perfiles ----------------------------------------------------------------------------------


def load_profile(name: str) -> dict[str, Any]:
    path = TEMPLATES / "typing" / f"{name}.toml"
    if not path.is_file():
        raise DeployError(f"no existe el perfil de tipado {path}")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def typings_dir() -> Path | None:
    """Stubs propios del proyecto (p. ej. los de raylib corregidos por el preset)."""
    path = ROOT / "typings"
    return path if path.is_dir() else None


def compiled_patterns(cfg: Config) -> list[str]:
    return [f"{m}.*" for m in cfg.compile.modules]


# --- mypy ----------------------------------------------------------------------------------------


def _ini_value(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _ini_section(name: str, options: dict[str, Any]) -> list[str]:
    lines = [f"[{name}]"]
    lines += [f"{k} = {_ini_value(v)}" for k, v in options.items()]
    return [*lines, ""]


def mypy_ini(cfg: Config, profile: str, *, for_compile: bool = False) -> str:
    """Config de mypy. `for_compile`: la que usa mypyc desde el stage (rutas absolutas)."""
    data = load_profile(profile)
    head: dict[str, Any] = {}
    typings = typings_dir()
    if for_compile:
        if typings:
            head["mypy_path"] = typings.as_posix()
    else:
        head["mypy_path"] = ["src", "typings"] if typings else "src"
        head["files"] = ["src", "tests"] if (ROOT / "tests").is_dir() else "src"
    lines = [f"# {HEADER}", f"# Perfil de tipado: {profile} ({data.get('description', '')})", ""]
    lines += _ini_section("mypy", {**head, **data.get("mypy", {})})
    compiled = data.get("mypy_compiled")
    if compiled:
        for pattern in compiled_patterns(cfg):
            lines += _ini_section(f"mypy-{pattern}", compiled)
    for override in cfg.typing.mypy_overrides:
        modules = override["module"]
        names = modules if isinstance(modules, list) else [modules]
        opts = {k: v for k, v in override.items() if k != "module"}
        section = ",".join(n.replace("{pkg}", cfg.pkg) for n in names)
        lines += _ini_section(f"mypy-{section}", opts)
    return "\n".join(lines).rstrip("\n") + "\n"


def mypy_cli_args(cfg: Config, python: Path) -> list[str]:
    """Con PyPy soportado, el código debe ser válido en 3.11: mypy lo comprueba así."""
    if not cfg.pypy_enabled:
        return []
    return ["--python-version", cfg.min_python, "--python-executable", str(python)]


# --- pyright / basedpyright ----------------------------------------------------------------------


def pyright_config(cfg: Config, profile: str, *, absolute: bool = False) -> dict[str, Any]:
    """`absolute`: para una copia fuera de la raíz (pyright resuelve rutas desde el archivo)."""
    data = load_profile(profile)

    def path(p: str) -> str:
        return (ROOT / p).as_posix() if absolute else p

    include = [path("src"), path("tests")] if (ROOT / "tests").is_dir() else [path("src")]
    conf: dict[str, Any] = {
        "include": include,
        "exclude": ["**/node_modules", "**/__pycache__", "**/.*", path("dist"), path("build")],
        "extraPaths": [path("src")],
        "pythonVersion": cfg.min_python,
        "venvPath": path("."),
        "venv": ".venv",
    }
    if typings_dir():
        conf["stubPath"] = path("typings")
    conf.update(data.get("pyright", {}))
    paths = [path(f"src/{p}") for p in compiled_paths(cfg)]
    if data.get("pyright_compiled", {}).get("strict"):
        conf["strict"] = paths
    based = data.get("basedpyright_compiled")
    if cfg.typing.editor == "basedpyright" and based:
        conf["executionEnvironments"] = [
            {"root": p, "extraPaths": [path("src")], **based} for p in paths if (ROOT / p).is_dir()
        ]
    return conf


# --- ruff ----------------------------------------------------------------------------------------


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_scalar(v)}" for k, v in value.items()) + " }"
    raise TypeError(value)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def to_toml(data: dict[str, Any], prefix: str = "") -> str:
    """Serializador TOML mínimo (tablas anidadas, listas y valores simples)."""
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict) or not v}
    tables = {k: v for k, v in data.items() if k not in scalars}
    out: list[str] = []
    for k, v in scalars.items():
        out.append(f"{_toml_key(k)} = {_toml_scalar(v)}")
    for k, v in tables.items():
        name = f"{prefix}.{_toml_key(k)}" if prefix else _toml_key(k)
        out.append("")
        out.append(f"[{name}]")
        body = to_toml(v, name)
        if body:
            out.append(body)
    return "\n".join(out).strip("\n")


def ruff_config(cfg: Config, profile: str, *, absolute: bool = False) -> dict[str, Any]:
    """`absolute`: para copias fuera de la raíz (ruff resuelve rutas desde el propio archivo)."""
    data = load_profile(profile).get("ruff", {})

    def path(p: str) -> str:
        return (ROOT / p).as_posix() if absolute else p

    select = list(data.get("select", []))
    lint: dict[str, Any] = {"select": select, "ignore": list(data.get("ignore", []))}
    if "ANN" in select:
        lint["per-file-ignores"] = {"tests/**" : ["ANN"]}
        lint["flake8-annotations"] = {"mypy-init-return": True}
    return {
        "target-version": "py" + cfg.min_python.replace(".", ""),
        "line-length": 100,
        "src": [path("src"), path("tests")],
        "extend-exclude": [path(p) for p in (".build", "dist", ".pytemplate", "typings")],
        "lint": lint,
        "format": {"docstring-code-format": True},
    }


# --- VS Code -------------------------------------------------------------------------------------


def _json(data: Any) -> str:
    return f"// {HEADER}\n" + json.dumps(data, indent=4, ensure_ascii=False) + "\n"


def vscode_settings(cfg: Config, profile: str) -> dict[str, Any]:
    base: dict[str, Any] = json.loads((TEMPLATES / "vscode" / "settings.json").read_text("utf-8"))
    base.update(load_profile(profile).get("vscode", {}))
    base.update(cfg.vscode.get("settings", {}))
    return base


def vscode_extensions(cfg: Config) -> dict[str, Any]:
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


def vscode_launch(cfg: Config) -> dict[str, Any]:
    main = {
        "name": "src/main.py (CPython, interpretado)",
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
                "name": "src/main.py (PyPy, experimental: el depurador en PyPy no es fiable)",
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


def vscode_tasks(cfg: Config) -> dict[str, Any]:
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

    tasks = [
        task("run", ["run"]),
        task("test", ["test"], {"kind": "test", "isDefault": True}),
        task("check", ["check"]),
        task("build", ["build"], {"kind": "build", "isDefault": True}),
    ]
    for b in cfg.backend.supported:
        if b != cfg.backend.active:
            tasks.append(task(f"run {b}", ["run", b]))
    if cfg.supports("mypyc"):
        tasks.append(task("report (mypyc)", ["report", "--open"]))
    return {"version": "2.0.0", "tasks": tasks}


# --- CI ------------------------------------------------------------------------------------------


def ci_workflow(cfg: Config) -> str:
    text = (TEMPLATES / "ci.yml").read_text(encoding="utf-8")
    builds = [b for b in ("mypyc", "cpython") if cfg.supports(b)]
    matrix: list[str] = []
    for os_name in ("ubuntu-latest", "windows-latest", "macos-latest"):
        backends = list(cfg.backend.supported)
        if os_name.startswith("macos") and cfg.app.preset == "raylib" and "pypy" in backends:
            backends.remove("pypy")  # raylib no publica wheels de PyPy para macOS arm64
        matrix += [f"          - os: {os_name}", f'            backends: "{" ".join(backends)}"']
    linux = ""
    if cfg.app.preset == "raylib":
        linux = (
            "      - name: Librerías de sistema para raylib (Linux)\n"
            "        if: runner.os == 'Linux'\n"
            "        run: sudo apt-get update && sudo apt-get install -y libgl1 libx11-6 libxrandr2 libxinerama1 libxcursor1 libxi6"
        )
    out = (
        text.replace("__HEADER__", HEADER)
        .replace("__MATRIX__", "\n".join(matrix))
        .replace("__LINUX_DEPS__\n", linux + "\n" if linux else "")
        .replace("__NAME__", cfg.app.name)
        .replace("__BUILD_BACKEND__", builds[0] if builds else cfg.backend.active)
    )
    return out


# --- conjunto de salidas ------------------------------------------------------------------------


def outputs(cfg: Config) -> dict[str, str]:
    profile = cfg.profile_for()
    files = {
        ".python-version": cfg.python.cpython + "\n",
        ".mypy.ini": mypy_ini(cfg, profile),
        ".ruff.toml": f"# {HEADER}\n# Perfil de tipado: {profile}\n\n" + to_toml(ruff_config(cfg, profile)) + "\n",
        "pyrightconfig.json": _json(pyright_config(cfg, profile)),
        ".vscode/settings.json": _json(vscode_settings(cfg, profile)),
        ".vscode/extensions.json": _json(vscode_extensions(cfg)),
        ".vscode/launch.json": _json(vscode_launch(cfg)),
        ".vscode/tasks.json": _json(vscode_tasks(cfg)),
    }
    if (TEMPLATES / "ci.yml").is_file():
        files[".github/workflows/ci.yml"] = ci_workflow(cfg)
    return files


def _norm(text: str) -> str:
    return text.lstrip("﻿").replace("\r\n", "\n")


def _digest(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def _load_state() -> dict[str, str]:
    if not STATE_FILE.is_file():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        files: dict[str, str] = data.get("files", {})
        return files
    except (json.JSONDecodeError, AttributeError):
        return {}


def _save_state(files: dict[str, str]) -> None:
    body = {"comment": "Hashes de los archivos generados por ./deploy (detecta ediciones a mano)", "files": dict(sorted(files.items()))}
    STATE_FILE.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def apply(cfg: Config, *, force: bool = False, check: bool = False, show_diff: bool = False) -> tuple[list[str], list[str]]:
    """Escribe los archivos desactualizados. Devuelve (cambiados, editados_a_mano)."""
    state = _load_state()
    changed: list[str] = []
    edited: list[str] = []
    new_state = dict(state)
    for path, content in outputs(cfg).items():
        target = ROOT / path
        new_hash = _digest(content)
        if target.is_file():
            current = target.read_text(encoding="utf-8", errors="replace")
            current_hash = _digest(current)
            if current_hash == new_hash:
                new_state[path] = new_hash
                continue
            recorded = state.get(path)
            if recorded is not None and recorded != current_hash and not force:
                edited.append(path)
                if show_diff:
                    diff = difflib.unified_diff(
                        content.splitlines(), _norm(current).splitlines(), f"{path} (generado)", f"{path} (actual)", lineterm=""
                    )
                    ui.info("\n".join(diff))
                continue
        changed.append(path)
        if not check:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            new_state[path] = new_hash
    if not check and new_state != state:
        _save_state(new_state)
    return changed, edited


def auto(cfg: Config, *, force: bool = False) -> None:
    """Render silencioso previo a cada comando: una línea si algo cambió."""
    changed, edited = apply(cfg, force=force)
    if changed:
        ui.info(f"render: actualizado {', '.join(changed)}")
    if edited:
        ui.warn(
            f"no se sobrescriben archivos generados editados a mano: {', '.join(edited)}\n"
            "  Cambia pytemplate.toml o .pytemplate/templates en su lugar, o usa ./deploy render --force"
        )
    if pyproject_outdated(cfg):
        ui.warn(
            "pyproject.toml no coincide con pytemplate.toml (backend.supported / python / preset).\n"
            "  Aplícalo y re-bloquea con: ./deploy lock"
        )


# --- pyproject.toml (partes gestionadas) ----------------------------------------------------------


def managed_block(cfg: Config) -> str:
    def minor_range(version: str) -> str:
        major, minor = version.split(".")
        return f"python_full_version >= '{version}' and python_full_version < '{major}.{int(minor) + 1}'"

    lines = [
        f"{MARK_BEGIN}: generado desde pytemplate.toml por ./deploy; no editar hasta la marca de cierre",
        "# uv.lock solo resuelve para la versión menor fijada (p. ej. sin la rama de 3.15, donde",
        "# librerías como raylib aún no tienen wheels). Cambiarla: python.cpython + ./deploy lock",
        "environments = [",
        f"    \"implementation_name == 'cpython' and {minor_range(cfg.python.cpython)}\",",
    ]
    if cfg.pypy_enabled:
        lines.append(f"    \"implementation_name == 'pypy' and {minor_range(cfg.min_python)}\",")
    lines.append("]")
    if cfg.pypy_enabled:
        lines += [
            "# PyPy trae cffi integrado; uv no lo ve e intentaría compilar el de PyPI",
            "override-dependencies = [\"cffi>=1.15.1; implementation_name == 'cpython'\"]",
        ]
    for key, value in presets.uv_extras(cfg).items():
        lines.append(f"{key} = {_toml_scalar(value)}")
    lines.append(f'python-preference = "only-managed"  {MARK_END}')
    return "\n".join(lines)


def pyproject_expected(cfg: Config, text: str) -> str:
    text = re.sub(
        r'(?m)^(requires-python\s*=\s*)"[^"]*"',
        lambda m: f'{m.group(1)}">={cfg.min_python}"',
        text,
        count=1,
    )
    block = managed_block(cfg)
    lines = text.splitlines()
    begin = next((i for i, ln in enumerate(lines) if ln.strip().startswith(MARK_BEGIN)), None)
    end = next((i for i, ln in enumerate(lines) if MARK_END in ln), None)
    if begin is not None and end is not None and end >= begin:
        lines[begin : end + 1] = block.splitlines()
    else:
        header = next((i for i, ln in enumerate(lines) if ln.strip() == "[tool.uv]"), None)
        if header is None:
            lines += ["", "[tool.uv]", *block.splitlines()]
        else:
            lines[header + 1 : header + 1] = block.splitlines()
    return "\n".join(lines) + "\n"


def pyproject_outdated(cfg: Config) -> bool:
    text = PYPROJECT.read_text(encoding="utf-8")
    return _norm(text) != pyproject_expected(cfg, _norm(text))


def write_pyproject(cfg: Config) -> bool:
    text = _norm(PYPROJECT.read_text(encoding="utf-8"))
    new = pyproject_expected(cfg, text)
    if new == text:
        return False
    tomllib.loads(new)
    PYPROJECT.write_text(new, encoding="utf-8", newline="\n")
    return True


def src_exists() -> bool:
    return SRC.is_dir()
