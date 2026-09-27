"""Python environments per backend, and how to call uv inside each one.

- cpython / mypyc -> .venv       (uv-managed CPython; ALL the tools run here)
- pypy            -> .venv-pypy  (PyPy pinned exactly)

uv needs UV_PROJECT_ENVIRONMENT and UV_PYTHON *together*: with only one of them it
silently recreates the environment with the wrong interpreter.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import proc, ui
from .config import Config
from .project import ENV_SUFFIX, ROOT, venv_python
from .ui import PytError

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


# uv's graphical report (no `error:` prefix before uv 0.12): "  x message" (U+00D7), then
# "  `-> cause" lines drawn with box characters (U+2570 U+2500 U+25B6, U+251C, U+2502)
_REPORT_MARK = "\u00d7"
_REPORT_DRAWING = "\u00d7\u2570\u2500\u25b6\u251c\u2502 "


def uv_error(out: str) -> str:
    """Return uv's `error:` message from its output (uv wraps it: indented continuation lines,
    joined here), or its graphical report (the lines up to the first blank one: the hints that
    follow are left out), else the last non-empty line ("" for no output)."""
    lines = out.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.lower().startswith("error")), None)
    if start is None:
        report = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith(_REPORT_MARK)), None)
        if report is None:
            return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
        parts = []
        for ln in lines[report:]:
            if not ln.strip():
                break
            parts.append(ln.strip().lstrip(_REPORT_DRAWING))
        return " ".join(parts)
    parts = [lines[start].strip()]
    for ln in lines[start + 1 :]:
        if not ln.strip() or not ln[0].isspace():
            break
        parts.append(ln.strip())
    return " ".join(parts)


def require_min_uv(uv_path: str) -> None:
    """Raise PytError(3) if `uv_path` is older than MIN_UV (asked once per process).

    Called before uv creates an environment: that is where an old uv first goes wrong (it
    cannot download the pinned PyPy, or it installs a CPython prerelease without a word).
    """
    if uv_path in _uv_ok:
        return
    try:
        text = proc.run([uv_path, "--version"], capture=True, check=False, echo=False).stdout
    except PytError:
        return  # uv cannot even start: the real call reports it
    problem = uv_problem(text)
    if problem:
        raise PytError(
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
        raise PytError(
            f"backend '{backend}' is not in backend.supported {cfg.backend.supported}.\n"
            f"  Enable it with: ./pyt mode --supports +{backend}"
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
    quiet: bool = True,
) -> subprocess.CompletedProcess[str]:
    """`quiet=False`: a uv command the user drives with their own arguments (`./pyt lock
    ARGS`, the `uv add|remove` of add/remove) keeps its output under -q."""
    uv_path = proc.find_uv()
    if not env.dir.exists():  # uv is about to create it (and maybe download its interpreter)
        require_min_uv(uv_path)
    # -q: uv's own progress (Resolved, Installed, Checked...) is progress too, and uv --quiet
    # hides it; it never touches the output of what `uv run` starts. It also hides uv's
    # warnings and change summaries (uv has no level that keeps them): the commands whose
    # warnings (an extra the package lacks) and summaries (`lock --upgrade`, `lock --dry-run`)
    # answer the user's own arguments pass quiet=False. Errors always print. A captured query
    # keeps uv's full output: the runner reads it. A call without a command line (echo=False)
    # that is not captured is a preparation step whose progress reaches the terminal: quiet too.
    quiet_flag = ["--quiet"] if quiet and ui.QUIET and not capture else []
    return proc.run(
        [uv_path, *quiet_flag, *args],
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
    pyproject.toml, not only uv's default ones (dev), so a package added with `./pyt add
    --group G` survives the next sync/setup and reaches a fresh clone. (`uv run` never removes
    packages: it syncs inexactly.) Minus the groups uv cannot install there (`left_out`, one
    `--no-group` each, with a note): with them uv refused the whole sync."""
    skipped = left_out(env)
    for group, why in skipped:
        ui.info(f"{env.dir.name}: dependency group '{group}' not installed ({why})")
    uv(env, ["sync", "--locked", "--all-groups", *[a for group, _ in skipped for a in ("--no-group", group)]])


def _norm_name(name: str) -> str:
    """A package or dependency group name as uv compares it (PEP 503 / PEP 735)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def left_out(env: PyEnv) -> list[tuple[str, str]]:
    """Return the dependency groups `uv sync --all-groups` cannot install in ENV, each with why
    (uv refuses the whole sync for one of them, while `uv run --locked`, the default groups only,
    works): a group `[tool.uv] conflicts` pairs with another group of the project (a cpu/gpu
    split: --all-groups enables both), and a group whose `[tool.uv.dependency-groups]`
    requires-python excludes ENV's Python (a group that needs 3.12 next to PyPy 3.11). The
    default groups always stay: `uv run` needs them too. A pyproject.toml that cannot be read
    leaves nothing out (uv then says what is wrong with it)."""
    try:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    groups = data.get("dependency-groups")
    tool = data.get("tool")
    uv_table = tool.get("uv") if isinstance(tool, dict) else None
    if not isinstance(groups, dict) or not isinstance(uv_table, dict):
        return []
    defaults = uv_table.get("default-groups", ["dev"])
    if not isinstance(defaults, list):  # "all": every group is a default one
        return []
    default = {_norm_name(g) for g in defaults if isinstance(g, str)}
    names = [_norm_name(g) for g in groups if _norm_name(g) not in default]
    project_table = data.get("project")
    project_name = project_table.get("name") if isinstance(project_table, dict) else None
    own = _norm_name(project_name) if isinstance(project_name, str) else None
    why: dict[str, str] = {}
    conflicts = uv_table.get("conflicts")
    for pairing in conflicts if isinstance(conflicts, list) else []:
        members: list[str] = []  # the set's groups of this project (extras: --all-groups enables none)
        for item in pairing if isinstance(pairing, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("group"), str):
                continue
            package = item.get("package")
            if package is None or (isinstance(package, str) and _norm_name(package) == own):
                members.append(_norm_name(item["group"]))
        for name in members:
            others = sorted({m for m in members if m != name})
            if others and name in names and name not in why:
                why[name] = "[tool.uv] conflicts pairs it with " + ", ".join(f"'{o}'" for o in others)
    version = _request_version(env.request)
    settings = uv_table.get("dependency-groups")
    for group, table in settings.items() if isinstance(settings, dict) and version else []:
        spec = table.get("requires-python") if isinstance(table, dict) else None
        name = _norm_name(group)
        if isinstance(spec, str) and version and name in names and name not in why and _excludes(spec, version):
            why[name] = f"its requires-python '{spec}' excludes Python {'.'.join(map(str, version))}"
    return [(name, why[name]) for name in dict.fromkeys(names) if name in why]


def _request_version(request: str) -> tuple[int, ...] | None:
    """The Python version of a UV_PYTHON request: (3, 14) for python.cpython "3.14" (uv picks the
    patch release), (3, 11, 15) for python.pypy "pypy@3.11.15" (exact)."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?$", request)
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


_CLAUSE = re.compile(r"(~=|==|!=|<=|>=|<|>)\s*(\d+(?:\.\d+)*)(\.\*)?")
_LAST_PATCH = 1 << 20  # beyond every real patch release


def _pad(version: tuple[int, ...], width: int) -> tuple[int, ...]:
    return version + (0,) * (width - len(version))


def _holds(op: str, spec: tuple[int, ...], star: bool, version: tuple[int, ...]) -> bool:
    """One PEP 440 clause on a final release (uv installs no other interpreter for a request)."""
    if star:  # ==3.12.* / !=3.12.*: a prefix match
        same = _pad(version, len(spec))[: len(spec)] == spec
        return same if op == "==" else not same
    width = max(len(version), len(spec))
    a, b = _pad(version, width), _pad(spec, width)
    if op == "~=":  # ~=3.12.1 means >=3.12.1, ==3.12.*
        prefix = spec[:-1]
        return a >= b and _pad(version, len(prefix))[: len(prefix)] == prefix
    return {"==": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]


def _excludes(spec: str, version: tuple[int, ...]) -> bool:
    """Whether the requires-python `spec` rules out `version`: X.Y.Z, or X.Y, which stands for
    every patch release of that minor (only those it rules out all). False for what this
    subset of PEP 440 cannot read (a pre-release, `===`...): uv decides then."""
    clauses: list[tuple[str, tuple[int, ...], bool]] = []
    for part in spec.split(","):
        m = _CLAUSE.fullmatch(part.strip())
        if m is None:
            return False
        number = tuple(int(x) for x in m[2].split("."))
        if (m[3] and m[1] not in ("==", "!=")) or (m[1] == "~=" and len(number) < 2):
            return False
        clauses.append((m[1], number, bool(m[3])))
    if len(version) > 2:
        candidates = [version]
    else:  # a clause changes its answer over the patch releases only next to the ones it names
        patches = {0, _LAST_PATCH}
        for _, number, _ in clauses:
            if number[:2] == version and len(number) > 2:
                patches |= {max(number[2] - 1, 0), number[2], number[2] + 1}
        candidates = [(*version, p) for p in sorted(patches)]
    return not any(all(_holds(op, number, star, v) for op, number, star in clauses) for v in candidates)


def _python_env(version: str) -> PyEnv:
    """The uv calls about the CPython of python.cpython (find, list, install) run like the ones
    of the project's environment: only uv-managed Pythons."""
    return PyEnv("cpython", ROOT / f".venv{ENV_SUFFIX}", version, "only-managed")


def find_cpython(version: str) -> Path | None:
    """The uv-managed CPython `version` (python.cpython) itself, never the interpreter of a
    virtual environment made from it (--system: .venv is not it), or None when uv has none."""
    r = uv(_python_env(version), ["python", "find", "--system", version], check=False, capture=True, echo=False)
    lines = (r.stdout or "").strip().splitlines()
    return Path(lines[-1].strip()) if r.returncode == 0 and lines else None


def cpython_downloads(request: str, *, everywhere: bool = False) -> bool | None:
    """Whether uv can download a CPython for `request` (a version such as "3.14", or "cpython"
    for any) for this platform, or with `everywhere` for any platform; None when uv cannot say.
    uv knows its downloads without the network."""
    args = ["python", "list", request, "--only-downloads", *(["--all-platforms"] if everywhere else [])]
    r = uv(_python_env(request), args, check=False, capture=True, echo=False)
    if r.returncode != 0:
        return None
    return bool((r.stdout or "").strip())


def this_platform() -> str:
    """This machine as the messages name it: `Android (linux aarch64)` in Termux."""
    machine = f"{sys.platform} {platform.machine() or 'unknown'}"
    return f"Android ({machine})" if hasattr(sys, "getandroidapilevel") else machine


# What runs where (README, "Where pyt runs"): the launchers start the runner on any CPython 3.11
# or newer, and only these commands stay there; the others, and `new`'s lock, need python.cpython.
RUNS_ON_ANY_PYTHON = "help, doctor, install and uninstall"


def no_download_problem(version: str) -> str:
    """Why uv cannot give python.cpython `version` on this machine, when it has no download of it
    here: a version no platform has (a typo), one this platform lacks, or a platform without any
    uv-managed CPython (Android/Termux, the BSDs)."""
    head = f'python.cpython = "{version}": '
    if cpython_downloads(version, everywhere=True) is False:
        return (
            head + f"uv knows no CPython {version} for any platform: fix python.cpython in pytemplate.toml\n"
            "  (uv python list --only-downloads lists the ones uv can install here)"
        )
    if cpython_downloads("cpython"):
        return (
            head + f"uv has no CPython {version} for this platform ({this_platform()}).\n"
            "  The project's commands run on the CPython of python.cpython, which uv installs: set it to a\n"
            "  version uv has here (uv python list --only-downloads), or work on the project on another\n"
            f"  machine. Here pyt runs {RUNS_ON_ANY_PYTHON} only"
        )
    return (
        head + f"uv installs no CPython on this platform ({this_platform()}).\n"
        "  The project's commands run on the CPython of python.cpython, which uv installs: here pyt\n"
        f"  runs {RUNS_ON_ANY_PYTHON} only, on any Python 3.11 or newer.\n"
        '  Work on the project on Windows, macOS or Linux (README: "Where pyt runs")'
    )


def ensure_python(version: str) -> Path:
    """The uv-managed CPython `version` (python.cpython), installed when missing: the project's
    commands run on it (cli._restart), `new` locks the new project with it, and render asks for
    it before .python-version names it (uv run by hand and the editors follow that file). When
    uv cannot give it, PytError(3) says why and what to do: nothing to download for this
    platform or version (no_download_problem), or an install that failed (offline, downloads
    turned off). The install is not an echoed command, so a --dry-run gets the Python it needs."""
    found = find_cpython(version)
    if found is not None:
        return found
    if cpython_downloads(version) is False:
        raise PytError(no_download_problem(version), 3)
    ui.info(f"CPython {version} (python.cpython) is not installed: uv installs it (once, about 30 MB)")
    # --no-bin --no-registry: nothing outside uv's own folder (no python3.X in ~/.local/bin, no Windows
    # registry entry), as when uv downloads an interpreter itself
    r = uv(_python_env(version), ["python", "install", "--no-bin", "--no-registry", version], check=False, echo=False)
    found = find_cpython(version) if r.returncode == 0 else None
    if found is None:
        raise PytError(
            f'python.cpython = "{version}": uv could not install CPython {version} (uv says why above).\n'
            "  The project's commands run on it, and uv downloads it once: check the network (a proxy needs\n"
            f"  HTTPS_PROXY), or install it by hand: uv python install {version}. With UV_PYTHON_DOWNLOADS=never\n"
            '  (or python-downloads = "never" in a uv.toml) uv installs no Python at all: allow "manual"',
            3,
        )
    return found


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
