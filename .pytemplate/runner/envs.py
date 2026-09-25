"""Entornos de Python por backend y cómo llamar a uv dentro de cada uno.

- cpython / mypyc -> .venv       (CPython gestionado por uv; aquí corren TODAS las herramientas)
- pypy            -> .venv-pypy  (PyPy fijado exactamente)
- python.jit      -> .venv-jit   (un python.org 3.14 del sistema: los CPython de uv para
                                  Windows no traen el JIT)

uv necesita UV_PROJECT_ENVIRONMENT y UV_PYTHON *juntos*: con solo uno de los dos
recrea el entorno con el intérprete equivocado sin avisar.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .config import Config
from .project import ENV_SUFFIX, IS_WINDOWS, ROOT, venv_python
from .ui import DeployError


@dataclass(frozen=True)
class PyEnv:
    key: str  # cpython | pypy | jit
    dir: Path
    request: str  # valor de UV_PYTHON
    preference: str  # valor de UV_PYTHON_PREFERENCE

    @property
    def python(self) -> Path:
        return venv_python(self.dir)


def cpython_env(cfg: Config) -> PyEnv:
    return PyEnv("cpython", ROOT / f".venv{ENV_SUFFIX}", cfg.python.cpython, "only-managed")


def pypy_env(cfg: Config) -> PyEnv:
    return PyEnv("pypy", ROOT / f".venv-pypy{ENV_SUFFIX}", cfg.python.pypy, "only-managed")


def jit_env(cfg: Config) -> PyEnv:
    return PyEnv("jit", ROOT / f".venv-jit{ENV_SUFFIX}", find_jit_interpreter(cfg), "only-system")


def tool_env(cfg: Config) -> PyEnv:
    """Entorno de las herramientas (mypy, ruff, mypyc, PyInstaller...): siempre CPython."""
    return cpython_env(cfg)


def runtime_env(cfg: Config, backend: str) -> PyEnv:
    """Entorno en el que se EJECUTA la app o los tests de un backend."""
    if backend == "pypy":
        return pypy_env(cfg)
    return jit_env(cfg) if cfg.python.jit else cpython_env(cfg)


def ensure_supported(cfg: Config, backend: str) -> None:
    if not cfg.supports(backend):
        raise DeployError(
            f"el backend '{backend}' no está en backend.supported {cfg.backend.supported}.\n"
            f"  Actívalo con: ./deploy mode --supports +{backend}"
            + ("  (baja la sintaxis a Python 3.11 y re-bloquea uv.lock)" if backend == "pypy" else "")
        )


def env_vars(env: PyEnv, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    e = proc.base_env()
    e["UV_PROJECT_ENVIRONMENT"] = str(env.dir)
    e["UV_PYTHON"] = env.request
    e["UV_PYTHON_PREFERENCE"] = env.preference
    # PYTHON_JIT se lee por su primer carácter: "false" lo ACTIVARÍA. Siempre "0" o "1".
    e["PYTHON_JIT"] = "1" if env.key == "jit" else "0"
    if extra:
        e.update(extra)
    return e


def uv(
    env: PyEnv,
    args: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
    echo: bool = True,
) -> subprocess.CompletedProcess[str]:
    return proc.run(
        [proc.find_uv(), *args],
        cwd=cwd,
        env=env_vars(env, extra_env),
        check=check,
        capture=capture,
        echo=echo,
    )


def uv_run(
    env: PyEnv,
    args: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
    check: bool = True,
    groups: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """`uv run --locked ...` en el entorno dado (sincroniza solo si hace falta)."""
    group_args = [a for g in groups for a in ("--group", g)]
    return uv(env, ["run", "--locked", *group_args, *args], cwd=cwd, extra_env=extra_env, check=check)


def sync(env: PyEnv, *, groups: Sequence[str] = ()) -> None:
    group_args = [a for g in groups for a in ("--group", g)]
    uv(env, ["sync", "--locked", *group_args])


def interpreter_info(python: str | Path) -> dict[str, object]:
    """Datos del intérprete (impl, versión, JIT) sin importar nada del proyecto."""
    code = (
        "import json,sys,sysconfig;"
        "j=getattr(sys,'_jit',None);"
        "print(json.dumps({'impl':sys.implementation.name,'version':'%d.%d.%d'%sys.version_info[:3],"
        "'executable':sys.executable,'base_prefix':sys.base_prefix,"
        "'jit':bool(j and j.is_available()),"
        "'gil_disabled':bool(sysconfig.get_config_var('Py_GIL_DISABLED'))}))"
    )
    out = proc.output([str(python), "-I", "-c", code])
    data: dict[str, object] = json.loads(out)
    return data


def find_jit_interpreter(cfg: Config) -> str:
    """Busca un CPython del sistema (python.org) con el JIT disponible."""
    if cfg.python.jit_interpreter:
        return cfg.python.jit_interpreter
    candidates: list[str] = []
    env = proc.base_env()
    env["UV_PYTHON_PREFERENCE"] = "only-system"
    try:
        found = proc.output([proc.find_uv(), "python", "find", cfg.python.cpython], env=env)
        candidates.append(found)
    except DeployError:
        pass
    if IS_WINDOWS:
        try:
            candidates.append(proc.output(["py", f"-{cfg.python.cpython}", "-c", "import sys;print(sys.executable)"]))
        except DeployError:
            pass
    for c in candidates:
        try:
            if interpreter_info(c).get("jit"):
                return c
        except (DeployError, json.JSONDecodeError):
            continue
    raise DeployError(
        f"python.jit = true pero no hay un CPython {cfg.python.cpython} del sistema con JIT.\n"
        "  Los CPython que descarga uv para Windows no lo traen. Instala uno de python.org, fijo:\n"
        f"    py install {cfg.python.cpython}      (gestor oficial de Python para Windows)\n"
        f"    scoop install versions/python{cfg.python.cpython.replace('.', '')}\n"
        "  o indica la ruta en python.jit_interpreter.",
        3,
    )
