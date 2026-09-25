"""Python environments per backend, and how to call uv inside each one.

- cpython / mypyc -> .venv       (uv-managed CPython; ALL the tools run here)
- pypy            -> .venv-pypy  (PyPy pinned exactly)
- python.jit      -> .venv-jit   (a system python.org 3.14: uv's CPython builds for
                                  Windows do not ship the JIT)

uv needs UV_PROJECT_ENVIRONMENT and UV_PYTHON *together*: with only one of them it
silently recreates the environment with the wrong interpreter.
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
    request: str  # value of UV_PYTHON
    preference: str  # value of UV_PYTHON_PREFERENCE

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
    """Return the tools environment (mypy, ruff, mypyc, PyInstaller...): always CPython."""
    return cpython_env(cfg)


def runtime_env(cfg: Config, backend: str) -> PyEnv:
    """Return the environment that RUNS the app or the tests of a backend."""
    if backend == "pypy":
        return pypy_env(cfg)
    return jit_env(cfg) if cfg.python.jit else cpython_env(cfg)


def ensure_supported(cfg: Config, backend: str) -> None:
    if not cfg.supports(backend):
        raise DeployError(
            f"backend '{backend}' is not in backend.supported {cfg.backend.supported}.\n"
            f"  Enable it with: ./deploy mode --supports +{backend}"
            + ("  (lowers the syntax to Python 3.11 and re-locks uv.lock)" if backend == "pypy" else "")
        )


def env_vars(env: PyEnv, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    e = proc.base_env()
    e["UV_PROJECT_ENVIRONMENT"] = str(env.dir)
    e["UV_PYTHON"] = env.request
    e["UV_PYTHON_PREFERENCE"] = env.preference
    # PYTHON_JIT is read by its first character: "false" would ENABLE it. Always "0" or "1".
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
    """Run `uv run --locked ...` in the given environment (syncs only when needed)."""
    group_args = [a for g in groups for a in ("--group", g)]
    return uv(env, ["run", "--locked", *group_args, *args], cwd=cwd, extra_env=extra_env, check=check)


def sync(env: PyEnv, *, groups: Sequence[str] = ()) -> None:
    group_args = [a for g in groups for a in ("--group", g)]
    uv(env, ["sync", "--locked", *group_args])


def interpreter_info(python: str | Path) -> dict[str, object]:
    """Return interpreter data (impl, version, JIT) without importing anything from the project."""
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
    """Find a system CPython (python.org) with the JIT available."""
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
        f"python.jit = true but there is no system CPython {cfg.python.cpython} with the JIT.\n"
        "  The CPython builds uv downloads for Windows do not ship it. Install a pinned one from python.org:\n"
        f"    py install {cfg.python.cpython}      (official Python install manager for Windows)\n"
        f"    scoop install versions/python{cfg.python.cpython.replace('.', '')}\n"
        "  or set its path in python.jit_interpreter.",
        3,
    )
