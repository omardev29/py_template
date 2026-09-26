"""Python environments per backend, and how to call uv inside each one.

- cpython / mypyc -> .venv       (uv-managed CPython; ALL the tools run here)
- pypy            -> .venv-pypy  (PyPy pinned exactly)

uv needs UV_PROJECT_ENVIRONMENT and UV_PYTHON *together*: with only one of them it
silently recreates the environment with the wrong interpreter.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .config import Config
from .project import ENV_SUFFIX, ROOT, venv_python
from .ui import DeployError

# The oldest uv this project works with. What sets it (bump it when one of them moves):
# - pypy@3.11.15, the python.pypy pin of every preset: uv 0.10.12 is the first that can download
#   it (0.10.11 and older: "No download found for request: pypy-3.11.15-...");
# - CPython 3.14 final for python.cpython = "3.14": uv 0.9.0 (0.8.x installs 3.14.0rc2 without
#   a word, and 0.7.x an alpha);
# - `uv export --format requirements.txt` of the pyz/portable/wheel builds: uv 0.6.15.
MIN_UV = "0.10.12"
UV_UPDATE = (
    "uv self update   (installed with a package manager? brew upgrade uv | pipx upgrade uv | "
    "winget upgrade astral-sh.uv | scoop update uv)"
)
_uv_ok: set[str] = set()  # uv binaries already checked against MIN_UV in this process


def uv_version(text: str) -> tuple[int, int, int] | None:
    """Return the version in `uv --version` output ("uv 0.12.19 (x86_64-unknown-linux-gnu)")."""
    m = re.search(r"\buv (\d+)\.(\d+)\.(\d+)", text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def uv_problem(version_text: str) -> str | None:
    """Return why the uv that printed `version_text` is too old for this project, or None
    (also None when the version cannot be read: the uv call itself then says what is wrong)."""
    found = uv_version(version_text)
    minimum = uv_version(f"uv {MIN_UV}")
    if found is None or minimum is None or found >= minimum:
        return None
    return f"uv {'.'.join(map(str, found))} is too old: this project needs uv {MIN_UV} or newer"


def uv_error(out: str) -> str:
    """Return uv's `error:` message from its output (uv wraps it: indented continuation lines,
    joined here), else the last non-empty line ("" for no output)."""
    lines = out.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.lower().startswith("error")), None)
    if start is None:
        return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    parts = [lines[start].strip()]
    for ln in lines[start + 1 :]:
        if not ln.strip() or not ln[0].isspace():
            break
        parts.append(ln.strip())
    return " ".join(parts)


def require_min_uv(uv_path: str) -> None:
    """Raise DeployError(3) if `uv_path` is older than MIN_UV (asked once per process).

    Called before uv creates an environment: that is where an old uv first goes wrong (it
    cannot download the pinned PyPy, or it installs a CPython prerelease without a word).
    """
    if uv_path in _uv_ok:
        return
    try:
        text = proc.run([uv_path, "--version"], capture=True, check=False, echo=False).stdout
    except DeployError:
        return  # uv cannot even start: the real call reports it
    problem = uv_problem(text)
    if problem:
        raise DeployError(
            f"{problem} (older ones cannot install the interpreters it pins: CPython 3.14 final, "
            f"PyPy 3.11.15).\n  Update it: {UV_UPDATE}",
            3,
        )
    _uv_ok.add(uv_path)


@dataclass(frozen=True)
class PyEnv:
    key: str  # cpython | pypy
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


def tool_env(cfg: Config) -> PyEnv:
    """Return the tools environment (mypy, ruff, mypyc, PyInstaller...): always CPython."""
    return cpython_env(cfg)


def runtime_env(cfg: Config, backend: str) -> PyEnv:
    """Return the environment that RUNS the app or the tests of a backend."""
    if backend == "pypy":
        return pypy_env(cfg)
    return cpython_env(cfg)


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
    uv_path = proc.find_uv()
    if not env.dir.exists():  # uv is about to create it (and maybe download its interpreter)
        require_min_uv(uv_path)
    return proc.run(
        [uv_path, *args],
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
    """Run `uv run --locked ...` in the given environment (syncs only when needed).

    With a `cwd` other than the root, `--project` pins this project: uv looks for the project
    from the cwd upwards, and a work dir with its own pyproject.toml (the `flet build` stage)
    would otherwise become the project ("Unable to find lockfile at uv.lock").
    """
    group_args = [a for g in groups for a in ("--group", g)]
    project = ["--project", str(ROOT)] if cwd is not None and cwd.resolve() != ROOT.resolve() else []
    return uv(env, ["run", "--locked", *project, *group_args, *args], cwd=cwd, extra_env=extra_env, check=check)


def sync(env: PyEnv) -> None:
    """`uv sync --locked --all-groups`: the environment gets EVERY dependency group of
    pyproject.toml, not only uv's default ones (dev), so a package added with `./deploy add
    --group G` survives the next sync/setup and reaches a fresh clone. (`uv run` never removes
    packages: it syncs inexactly.)"""
    uv(env, ["sync", "--locked", "--all-groups"])


def interpreter_info(python: str | Path) -> dict[str, object]:
    """Return interpreter data (impl, version, platform, prefix) without importing anything from
    the project. `platform` is sysconfig.get_platform(): setuptools picks the MSVC tools by it;
    `cc` the C compiler command it runs without $CC (None on Windows)."""
    code = (
        "import json,sys,sysconfig;"
        "print(json.dumps({'impl':sys.implementation.name,'version':'%d.%d.%d'%sys.version_info[:3],"
        "'platform':sysconfig.get_platform(),'cc':sysconfig.get_config_var('CC'),"
        "'executable':sys.executable,'base_prefix':sys.base_prefix,"
        "'gil_disabled':bool(sysconfig.get_config_var('Py_GIL_DISABLED'))}))"
    )
    out = proc.output([str(python), "-I", "-c", code])
    data: dict[str, object] = json.loads(out)
    return data
