"""Loading, validation and editing of pytemplate.toml.

The schema is defined with dataclasses: each field with its default value and its type. An
unknown key or a value of the wrong type is an error (exit 2) that shows the full path of the
key, so a typo is never silently ignored. The file must be UTF-8 (TOML requires it): another
encoding is a config error that says how to save it again, never a traceback.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import json
import math
import re
import tomllib
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import proc
from .project import CONFIG_FILE, PRESETS, SRC, rel
from .ui import DeployError

SCHEMA = 1  # the pytemplate.toml layout this runner reads (`schema = 1`)
BACKENDS = ("cpython", "pypy", "mypyc")
# app.name: PEP 508 (and uv) wants a letter or digit at both ends; the package src/<pkg>/ (the
# name in snake_case) must start with a letter. new, __init and rename check it too (presets)
APP_NAME = re.compile(r"[A-Za-z](?:[A-Za-z0-9_-]*[A-Za-z0-9])?")
NAME_RULE = "letters, digits, '-' and '_', starting with a letter and ending with a letter or digit"
PROFILES = ("mypyc", "strict", "warn", "off")
METHODS = ("exe", "portable", "pyz", "wheel", "nuitka", "flet")
EDITORS = ("pylance", "basedpyright")
# [deploy] default: the build method of each backend; a backend left out of the table keeps its own
DEFAULT_METHODS = {"cpython": "exe", "mypyc": "exe", "pypy": "portable"}

_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


@dataclass
class AppConfig:
    name: str = "myapp"  # executable/dist name; the package is its snake_case version
    preset: str = "script"  # script | raylib | flet
    gui: bool = False  # True: exe without a console, launchers use pythonw/pypyw
    assets: str = "assets"  # "assets": src/assets/ is bundled with the app; "" = none (no other name)


@dataclass
class BackendConfig:
    active: str = "cpython"
    supported: list[str] = field(default_factory=lambda: ["cpython", "mypyc"])


@dataclass
class PythonConfig:
    cpython: str = "3.14"
    pypy: str = "pypy@3.11.15"


@dataclass
class TypingConfig:
    profile: str = "auto"  # auto | mypyc | strict | warn | off
    relaxed: str = "off"  # what "auto" means with the cpython/pypy backends
    editor: str = "pylance"  # pylance | basedpyright
    mypy_overrides: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CompileConfig:
    modules: list[str] = field(default_factory=lambda: ["myapp.core"])
    exclude: list[str] = field(default_factory=list)
    forbid_imports: list[str] = field(default_factory=list)
    # True: every mypyc build (run, test, compile, build) also writes the annotated HTML report
    # of slow lines to .build/reports/mypyc-annotate.html (./deploy report does it on demand)
    annotate: bool = False
    opt_level: str = "3"
    # Linux gcc/clang: -fno-semantic-interposition (as CPython itself is built), so the compiler
    # may inline calls between compiled functions (tools/mypyc_build.py, extra_cflags)
    no_semantic_interposition: bool = True
    multi_file: bool = False
    separate: bool = False
    strict_dunder_typing: bool = False


@dataclass
class ExeConfig:
    mode: str = "onefile"  # onefile | onedir
    console: str = "auto"  # auto (= not app.gui) | yes | no
    icon: str = ""
    hidden_imports: list[str] = field(default_factory=list)
    strip: bool = False  # Linux/macOS: strip the symbol tables of the bundled binaries (smaller)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class PortableConfig:
    runtime: str = "bundled"  # bundled (includes the interpreter) | system (uses the target's own)
    prune: bool = True
    archive: bool = True
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class PyzConfig:
    targets: list[str] = field(default_factory=lambda: ["host"])


@dataclass
class WheelConfig:
    entry: str = ""  # "package.module:function" for [project.scripts]; empty = <pkg>.app:main


@dataclass
class NuitkaConfig:
    mode: str = "standalone"  # standalone | onefile
    # --lto: auto | yes | no. Nuitka's auto is "yes" with uv's CPython (gcc/clang, MSVC) until
    # more than 250 modules are compiled (the flet preset compiles ~800: no LTO there)
    lto: str = "auto"
    pgo: bool = False  # --pgo-c: profile-guided C optimization (Nuitka runs the app once while building)
    pgo_args: list[str] = field(default_factory=list)  # the app's arguments for that profiling run
    extra_args: list[str] = field(default_factory=list)


@dataclass
class FletBuildConfig:
    target: str = "host"  # host | windows | macos | linux | apk | aab | ipa | web
    cleanup: bool = True  # --cleanup-app --cleanup-packages: drop tests, docs... from the bundle
    exclude: list[str] = field(default_factory=list)  # app files/folders left out (--exclude)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class UpxConfig:
    enabled: bool = False  # UPX-pack the binaries of the exe (Windows only), nuitka, portable and flet methods
    level: str = "best"  # 1..9 | best | brute | ultra-brute
    lzma: bool = True
    exclude: list[str] = field(default_factory=list)  # file-name globs never packed
    # explicit upx executable, absolute or relative to the project root (default: PATH, then a pinned download)
    path: str = ""


@dataclass
class DeployConfig:
    optimize: int = 1
    default: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_METHODS))
    exe: ExeConfig = field(default_factory=ExeConfig)
    portable: PortableConfig = field(default_factory=PortableConfig)
    pyz: PyzConfig = field(default_factory=PyzConfig)
    wheel: WheelConfig = field(default_factory=WheelConfig)
    nuitka: NuitkaConfig = field(default_factory=NuitkaConfig)
    flet: FletBuildConfig = field(default_factory=FletBuildConfig)
    upx: UpxConfig = field(default_factory=UpxConfig)
    # Modules left out of the exe and nuitka builds even if something imports them (size)
    exclude_modules: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # A partial table (`default = { cpython = "pyz" }`) keeps the method of the other
        # backends: without this, pypy fell back to "exe", which PyInstaller cannot build
        self.default = {**DEFAULT_METHODS, **self.default}


@dataclass
class TaskConfig:
    cmd: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    backend: str = ""  # environment it runs in (empty = active backend)
    uv: bool = True  # True: runs with `uv run` inside the backend's environment
    cwd: str = ""
    help: str = ""
    background: bool = False  # long-running (dev server, hot reload): editors start it without waiting


@dataclass
class HooksConfig:
    pre_commit: bool = True  # ./deploy setup installs the git pre-commit hook (./deploy hooks run)


@dataclass
class VSCodeConfig:
    settings: dict[str, Any] = field(default_factory=dict)  # merged into .vscode/settings.json
    # Status bar buttons (VS Code extension actboy168.tasks): "<command> [args]" or a [tasks] name
    buttons: list[str] = field(default_factory=lambda: ["run", "test", "check", "build"])


@dataclass
class Config:
    schema: int = SCHEMA
    app: AppConfig = field(default_factory=AppConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    python: PythonConfig = field(default_factory=PythonConfig)
    typing: TypingConfig = field(default_factory=TypingConfig)
    compile: CompileConfig = field(default_factory=CompileConfig)
    deploy: DeployConfig = field(default_factory=DeployConfig)
    tasks: dict[str, TaskConfig] = field(default_factory=dict)
    preset: dict[str, dict[str, Any]] = field(default_factory=dict)
    vscode: VSCodeConfig = field(default_factory=VSCodeConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)

    # --- derived values ----------------------------------------------------------------

    @property
    def pkg(self) -> str:
        """Name of the app's Python package (src/<pkg>/)."""
        return self.app.name.replace("-", "_").lower()

    def supports(self, backend: str) -> bool:
        return backend in self.backend.supported

    @property
    def pypy_enabled(self) -> bool:
        return self.supports("pypy")

    @property
    def pypy_minor(self) -> str:
        """Python version of the pinned PyPy: "pypy@3.11.15" -> "3.11"."""
        m = re.fullmatch(r"pypy@([0-9]+\.[0-9]+)\.[0-9]+", self.python.pypy)
        return m.group(1) if m else "3.11"

    @property
    def min_python(self) -> str:
        """Oldest Python the code must run on: the lowest minor of the interpreters in use.

        CPython (python.cpython) always counts: the tools run on it and mypyc compiles for it.
        PyPy (python.pypy) counts while it is supported, usually lowering it to 3.11.
        """
        minors = [self.python.cpython, *([self.pypy_minor] if self.pypy_enabled else [])]
        return min(minors, key=_version_key)

    def profile_for(self, backend: str | None = None) -> str:
        """Return the effective typing profile for a backend (the active one by default)."""
        b = backend or self.backend.active
        if b == "mypyc":
            return "mypyc"
        return self.typing.relaxed if self.typing.profile == "auto" else self.typing.profile

    def preset_options(self, name: str) -> dict[str, Any]:
        return self.preset.get(name, {})


def _version_key(version: str) -> tuple[int, ...]:
    """Sort key of a version: numbers, never strings ("3.9" < "3.11" < "3.100")."""
    return tuple(int(n) for n in re.findall(r"[0-9]+", version))


# --- loading -----------------------------------------------------------------------------

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_TYPE_NAMES: dict[Any, str] = {bool: "boolean", int: "integer", str: "string"}


def _join(where: str, key: str) -> str:
    """The dotted path of `key` inside `where`, quoting keys that are not bare TOML keys."""
    part = key if _BARE_KEY.fullmatch(key) else json.dumps(key)
    return f"{where}.{part}" if where else part


def _type_name(value: Any) -> str:
    if isinstance(value, datetime.datetime):
        return "date-time"
    return {
        bool: "boolean",
        int: "integer",
        float: "float",
        str: "string",
        list: "list",
        dict: "table",
        datetime.date: "date",
        datetime.time: "time",
    }.get(type(value), type(value).__name__)


_HINTS: dict[type[Any], dict[str, Any]] = {}


def _fields(cls: type[Any]) -> dict[str, Any]:
    """Field name -> resolved type (e.g. list[str]) of a schema dataclass."""
    if cls not in _HINTS:
        _HINTS[cls] = typing.get_type_hints(cls)
    return _HINTS[cls]


def _check_type(value: Any, hint: Any, where: str) -> None:
    """Raise DeployError unless `value` (from tomllib) has the schema type `hint`, recursively."""
    if hint is Any:  # free-form: [vscode] settings, [preset.<p>] options, mypy override options
        _check_free(value, where)
        return
    origin = typing.get_origin(hint)
    if origin is list or origin is dict:
        if not isinstance(value, origin):
            wanted = "list" if origin is list else "table"
            raise DeployError(f"pytemplate.toml: '{where}' must be of type {wanted}, not {_type_name(value)}")
        item = typing.get_args(hint)[-1]
        if isinstance(value, list):
            for i, v in enumerate(value):
                _check_type(v, item, f"{where}[{i}]")
        else:
            for k, v in value.items():
                _check_type(v, item, _join(where, k))
        return
    if not isinstance(value, hint) or (hint is not bool and isinstance(value, bool)):
        name = _TYPE_NAMES.get(hint, getattr(hint, "__name__", str(hint)))
        raise DeployError(f"pytemplate.toml: '{where}' must be of type {name}, not {_type_name(value)}")
    if isinstance(value, str) and "\0" in value:
        raise DeployError(f"pytemplate.toml: '{where}' contains a NUL character (\\u0000)")


def _check_free(value: Any, where: str) -> None:
    """A free-form value: strings, finite numbers, booleans, lists and tables only.

    They end up in JSON (.vscode/settings.json), .mypy.ini or dependency strings, which cannot
    hold a TOML date/time, nan/inf or a NUL character.
    """
    if isinstance(value, str):
        if "\0" in value:
            raise DeployError(f"pytemplate.toml: '{where}' contains a NUL character (\\u0000)")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise DeployError(f"pytemplate.toml: '{where}' = {value}: only finite numbers are allowed here")
    elif isinstance(value, (datetime.date, datetime.time)):
        raise DeployError(
            f"pytemplate.toml: '{where}' is a TOML {_type_name(value)}, which the generated files "
            "cannot hold: write it as a string (quoted)"
        )
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_free(v, f"{where}[{i}]")
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_free(v, _join(where, k))


def _check_env_names(env: dict[str, str], where: str) -> None:
    """Names no process environment can hold (subprocess would raise ValueError)."""
    for name in env:
        if not name or "=" in name or "\0" in name:
            raise DeployError(
                f"pytemplate.toml: '{where}': invalid environment variable name {name!r} "
                "(it cannot be empty or contain '=')"
            )


def _check_schema(schema: Any) -> None:
    if isinstance(schema, int) and not isinstance(schema, bool) and schema != SCHEMA:
        raise DeployError(
            f"pytemplate.toml: schema = {schema} is not supported by this runner, which reads "
            f"schema = {SCHEMA}: the file comes from a different version of the template"
        )


def _build(cls: type[Any], data: Any, where: str) -> Any:
    if not isinstance(data, dict):
        raise DeployError(f"pytemplate.toml: '{where}' must be a table")
    if cls is Config:
        _check_schema(data.get("schema", SCHEMA))  # before a newer layout's unknown keys
    hints = _fields(cls)
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        path = _join(where, key)
        hint = hints.get(key)
        if hint is None:
            valid = ", ".join(sorted(hints))
            raise DeployError(f"pytemplate.toml: unknown key '{path}' (valid: {valid})")
        if isinstance(hint, type) and dataclasses.is_dataclass(hint):
            kwargs[key] = _build(hint, value, path)
        elif key == "tasks" and cls is Config:
            kwargs[key] = {
                name: _build(TaskConfig, spec, _join("tasks", name)) for name, spec in _table(value, path).items()
            }
        else:
            _check_type(value, hint, path)
            if key == "env":  # tasks.X.env, deploy.portable.env
                _check_env_names(value, path)
            kwargs[key] = value
    return cls(**kwargs)


def _table(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeployError(f"pytemplate.toml: '{where}' must be a table")
    return value


def _one_of(value: str, allowed: tuple[str, ...], where: str) -> None:
    if value not in allowed:
        raise DeployError(f"pytemplate.toml: '{where}' = {value!r} is not valid ({' | '.join(allowed)})")


def _presets() -> list[str]:
    """Return the presets of this template (like presets.available(), which would be an import cycle)."""
    if not PRESETS.is_dir():
        return []
    return sorted(p.name for p in PRESETS.iterdir() if (p / "preset.toml").is_file())


def _within(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _validate_compile(cfg: Config) -> None:
    """[compile]: the relations between the dotted names, once validate() checked each of them
    (whether they exist is checked when mypyc runs: mypyc.compiled_sources)."""
    modules = cfg.compile.modules
    for i, m in enumerate(modules):
        for other in modules[:i]:
            if m == other:
                raise DeployError(f"pytemplate.toml: compile.modules lists {m!r} twice")
            if _within(m, other) or _within(other, m):
                inner, outer = (m, other) if _within(m, other) else (other, m)
                raise DeployError(
                    f"pytemplate.toml: compile.modules: {inner!r} is inside {outer!r}, which already compiles it "
                    f"(mypyc would see the module twice): keep only {outer!r}"
                )
    for ex in cfg.compile.exclude:
        if ex in modules:
            raise DeployError(
                f"pytemplate.toml: compile.exclude: {ex!r} is a whole compile.modules entry: remove it from compile.modules instead"
            )
        if not any(ex.startswith(m + ".") for m in modules):
            raise DeployError(
                f"pytemplate.toml: compile.exclude: {ex!r} is not inside compile.modules {modules}: "
                "list modules or subpackages of those packages"
            )


# --- [tasks] -------------------------------------------------------------------------------------

TASK_PLACEHOLDERS = ("root", "src", "build", "dist", "backend", "name", "pkg", "python")
BRACES_HINT = "write a literal brace doubled: {{ and }}"


def task_format_error(text: str) -> str | None:
    """Why `text` (a [tasks] cmd item, env value or cwd) is not a valid template, or None.

    Only the syntax, which str.format_map would otherwise turn into a traceback: placeholders
    are bare names ({root}; never {}, {0}, {root.x}, {root!r}, {root:>9}), literal braces are
    doubled. An unknown name ({nope}) is reported when the task runs.
    """
    import string

    try:
        parts = list(string.Formatter().parse(text))
    except ValueError as e:  # a lone '{' or '}'
        return f"{e} ({BRACES_HINT})"
    for _literal, field_name, spec, conversion in parts:
        if field_name is None:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name) or spec or conversion:
            shown = "{" + field_name + (f"!{conversion}" if conversion else "") + (f":{spec}" if spec else "") + "}"
            known = " ".join("{" + p + "}" for p in TASK_PLACEHOLDERS)
            return f"{shown} is not a placeholder ({known}; {BRACES_HINT})"
    return None


def _validate_task(name: str, task: TaskConfig, builtin_commands: set[str] | None) -> None:
    """The static rules of a [tasks] entry. Its deps (quoting, the command they name) and its
    placeholder names are checked when the task runs (tasks.run_task), which is what the
    editor renderers expect (vscode.scan renders a task whose deps do not parse)."""
    where = f"pytemplate.toml: tasks.{name}"
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        raise DeployError(f"pytemplate.toml: invalid task name: {name!r}")
    if builtin_commands and name in builtin_commands:
        raise DeployError(f"pytemplate.toml: task '{name}' clashes with the built-in command ./deploy {name}")
    if not task.cmd and not task.deps:
        raise DeployError(f"pytemplate.toml: task '{name}' needs 'cmd' or 'deps'")
    if task.backend:
        _one_of(task.backend, BACKENDS, f"tasks.{name}.backend")
    if task.cmd and not task.cmd[0].strip():
        raise DeployError(f"{where}.cmd: the program (its first item) is empty")
    values = [(f"{where}.cmd", item) for item in task.cmd] + [(f"{where}.env.{k}", v) for k, v in task.env.items()]
    if task.cwd:
        values.append((f"{where}.cwd", task.cwd))
    for key_path, value in values:
        problem = task_format_error(value)
        if problem:
            raise DeployError(f"{key_path}: {value!r}: {problem}")
    for key in task.env:
        # They become environment variables of the task's process ('=' or '' would crash it)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise DeployError(f"{where}.env: invalid environment variable name {key!r}")


def validate(cfg: Config, builtin_commands: set[str] | None = None) -> None:
    if not APP_NAME.fullmatch(cfg.app.name):
        raise DeployError(f"pytemplate.toml: 'app.name' only allows {NAME_RULE}")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", cfg.app.preset) or not (PRESETS / cfg.app.preset / "preset.toml").is_file():
        raise DeployError(
            f"pytemplate.toml: app.preset = {cfg.app.preset!r} is not a preset of this template "
            f"(available: {', '.join(_presets()) or 'none'}). To start from another preset: ./deploy new DIR --preset P"
        )

    _check_schema(cfg.schema)
    if cfg.app.assets not in ("", "assets"):
        raise DeployError(
            f"pytemplate.toml: app.assets = {cfg.app.assets!r} is not supported: use \"assets\" "
            "(bundle src/assets/ with the app) or \"\" (no assets folder). The runtime lookup "
            "(resources.assets_dir) and the portable/pyz bootstraps only know src/assets/"
        )
    for b in cfg.backend.supported:
        _one_of(b, BACKENDS, "backend.supported")
    if not cfg.backend.supported:
        raise DeployError("pytemplate.toml: 'backend.supported' cannot be empty")
    twice = sorted({b for b in cfg.backend.supported if cfg.backend.supported.count(b) > 1})
    if twice:
        raise DeployError(f"pytemplate.toml: 'backend.supported' lists {', '.join(twice)} more than once")
    _one_of(cfg.backend.active, BACKENDS, "backend.active")
    if cfg.backend.active not in cfg.backend.supported:
        raise DeployError(
            f"pytemplate.toml: backend.active = {cfg.backend.active!r} is not in backend.supported "
            f"{cfg.backend.supported}. Use: ./deploy mode {cfg.backend.active} --supports +{cfg.backend.active}"
        )
    # [0-9], never \d: \d also matches other scripts' digits ("\u0663.\u0661\u0664")
    if not re.fullmatch(r"[0-9]+\.[0-9]+", cfg.python.cpython):
        raise DeployError("pytemplate.toml: 'python.cpython' must be a minor version, e.g. \"3.14\"")
    if not re.fullmatch(r"pypy@[0-9]+\.[0-9]+\.[0-9]+", cfg.python.pypy):
        raise DeployError(
            f"pytemplate.toml: 'python.pypy' must be an exact version, e.g. \"{PythonConfig.pypy}\": "
            "a loose request picks the newest PyPy, and a new PyPy can change the extension ABI "
            "(7.3 -> 8.0: pp73 -> pp80) that your dependencies must publish wheels for"
        )
    _one_of(cfg.typing.profile, ("auto", *PROFILES), "typing.profile")
    _one_of(cfg.typing.relaxed, ("off", "warn", "strict"), "typing.relaxed")
    _one_of(cfg.typing.editor, EDITORS, "typing.editor")
    if cfg.backend.active == "mypyc" and cfg.typing.profile in ("warn", "off"):
        raise DeployError(
            "pytemplate.toml: with the mypyc backend, typing cannot be 'warn' or 'off' "
            "(mypyc aborts on any mypy error). Use typing.profile = \"auto\"."
        )
    for m in [*cfg.compile.modules, *cfg.compile.exclude, *cfg.compile.forbid_imports]:
        if not _DOTTED.fullmatch(m):  # fullmatch: `$` alone accepts a trailing newline
            raise DeployError(f"pytemplate.toml: invalid module in [compile]: {m!r}")
    for i, override in enumerate(cfg.typing.mypy_overrides):
        _check_override(override, f"typing.mypy_overrides[{i}]")
    _one_of(cfg.compile.opt_level, ("0", "1", "2", "3"), "compile.opt_level")
    _validate_compile(cfg)
    if cfg.deploy.optimize not in (0, 1, 2):
        raise DeployError("pytemplate.toml: 'deploy.optimize' must be 0, 1 or 2")
    for backend, method in cfg.deploy.default.items():
        _one_of(backend, BACKENDS, "deploy.default")
        _one_of(method, METHODS, f"deploy.default.{backend}")
    _check_default_methods(cfg)
    _one_of(cfg.deploy.exe.mode, ("onefile", "onedir"), "deploy.exe.mode")
    _one_of(cfg.deploy.exe.console, ("auto", "yes", "no"), "deploy.exe.console")
    _one_of(cfg.deploy.portable.runtime, ("bundled", "system"), "deploy.portable.runtime")
    for key in cfg.deploy.portable.env:
        # They become `set "K=v"` / `export K=v` lines of the .cmd/.sh launchers
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise DeployError(f"pytemplate.toml: deploy.portable.env: invalid environment variable name {key!r}")
    _one_of(cfg.deploy.nuitka.mode, ("standalone", "onefile"), "deploy.nuitka.mode")
    _check_nuitka(cfg)
    _one_of(cfg.deploy.upx.level, ("1", "2", "3", "4", "5", "6", "7", "8", "9", "best", "brute", "ultra-brute"), "deploy.upx.level")
    for where, names in (("deploy.exclude_modules", cfg.deploy.exclude_modules), ("deploy.exe.hidden_imports", cfg.deploy.exe.hidden_imports)):
        for m in names:
            if not _DOTTED.fullmatch(m):
                raise DeployError(f"pytemplate.toml: invalid module in {where}: {m!r}")
    _check_preset_tables(cfg)
    for name, task in cfg.tasks.items():
        _validate_task(name, task, builtin_commands)
    if builtin_commands:
        for button in cfg.vscode.buttons:
            first = button.split()[0] if button.split() else ""
            if first not in builtin_commands and first not in cfg.tasks:
                raise DeployError(
                    f"pytemplate.toml: vscode.buttons: {button!r} is neither a ./deploy command nor a [tasks] name"
                )


# A [[typing.mypy_overrides]] table becomes a `[mypy-<module>,...]` section of .mypy.ini
_MODULE_PATTERN = re.compile(r"(\*|[A-Za-z_][A-Za-z0-9_]*)(\.(\*|[A-Za-z_][A-Za-z0-9_]*))*")
_OPTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def _check_override(override: dict[str, Any], where: str) -> None:
    if "module" not in override:
        raise DeployError(f"pytemplate.toml: '{where}' needs 'module' (each [[typing.mypy_overrides]] names its modules)")
    if "strict" in override:
        raise DeployError(
            "pytemplate.toml: do not put 'strict' in typing.mypy_overrides "
            "(mypy would apply it to ALL modules); use specific options instead"
        )
    names = override["module"] if isinstance(override["module"], list) else [override["module"]]
    if not names:
        raise DeployError(f"pytemplate.toml: '{where}.module' is empty: name a module or a pattern")
    for name in names:
        if not isinstance(name, str) or not _MODULE_PATTERN.fullmatch(name.replace("{pkg}", "pkg")):
            raise DeployError(
                f"pytemplate.toml: '{where}.module': {name!r} is not a module name or pattern "
                "(e.g. \"raylib\", \"raylib.*\", \"{pkg}.ui.*\")"
            )
    for key, value in override.items():
        if key == "module":
            continue
        if not _OPTION_NAME.fullmatch(key):
            raise DeployError(f"pytemplate.toml: '{where}': {key!r} is not a mypy option name")
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, (list, dict)) or (isinstance(item, str) and ("\n" in item or "\r" in item)):
                raise DeployError(
                    f"pytemplate.toml: '{_join(where, key)}' must be a boolean, a number, a one-line "
                    "string or a list of them (it becomes one line of .mypy.ini)"
                )


def _check_nuitka(cfg: Config) -> None:
    """[deploy.nuitka] lto and pgo. The PGO rules the config alone decides are checked here; the
    backend and the build machine (mypyc, macOS) are checked by nuitka.check_options at build time."""
    nuitka = cfg.deploy.nuitka
    _one_of(nuitka.lto, ("auto", "yes", "no"), "deploy.nuitka.lto")
    if nuitka.pgo_args and not nuitka.pgo:
        raise DeployError(
            "pytemplate.toml: deploy.nuitka.pgo_args is set but deploy.nuitka.pgo is false: they are the "
            "app's arguments for the PGO profiling run (set pgo = true, or remove pgo_args)"
        )
    if nuitka.pgo and cfg.app.gui:
        raise DeployError(
            "pytemplate.toml: deploy.nuitka.pgo needs app.gui = false: Nuitka's profiling run starts the "
            "app while building, and the build waits, with no timeout, until its window is closed"
        )
    if nuitka.pgo and cfg.app.assets:
        raise DeployError(
            f"pytemplate.toml: deploy.nuitka.pgo needs app.assets = \"\": Nuitka's profiling run starts the "
            f"app before its data files are in place, so reading src/{cfg.app.assets}/ fails (FileNotFoundError)"
        )


def _check_default_methods(cfg: Config) -> None:
    """deploy.default: each method must be able to package its backend."""
    from .cmd_build import COMPAT  # imported here: cmd_build imports this module

    for backend, method in cfg.deploy.default.items():
        reason = COMPAT.get(method, {}).get(backend)
        if reason:
            raise DeployError(f"pytemplate.toml: deploy.default.{backend} = {method!r} cannot package {backend}: {reason}")
        if method == "flet" and cfg.app.preset != "flet":
            raise DeployError(
                f"pytemplate.toml: deploy.default.{backend} = 'flet': the flet method (flet build) "
                f"is only for the flet preset (app.preset is {cfg.app.preset!r})"
            )


def _check_preset_tables(cfg: Config) -> None:
    """[preset.<p>]: a preset of this template, and only the [options] its preset.toml declares."""
    for name, options in cfg.preset.items():
        table = _join("preset", name)
        path = PRESETS / name / "preset.toml"
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or not path.is_file():
            raise DeployError(
                f"pytemplate.toml: [{table}]: {name!r} is not a preset of this template "
                f"(available: {', '.join(_presets()) or 'none'})"
            )
        declared = _preset_options(path)
        for key, value in options.items():
            if key not in declared:
                valid = ", ".join(sorted(declared)) or f"none, the {name} preset has no options"
                raise DeployError(f"pytemplate.toml: unknown key '{_join(table, key)}' (valid: {valid})")
            if type(value) is not type(declared[key]):
                raise DeployError(
                    f"pytemplate.toml: '{_join(table, key)}' must be of type "
                    f"{_type_name(declared[key])}, not {_type_name(value)}"
                )


def _preset_options(path: Path) -> dict[str, Any]:
    """The [options] of a preset.toml, read with tomllib (importing presets.py would be a cycle)."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))  # a BOM is fine, as in presets.load
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise DeployError(f"{rel(path)} cannot be read: {e}") from None
    options = data.get("options", {})
    return options if isinstance(options, dict) else {}


# --- reading pytemplate.toml ---------------------------------------------------------------------

_UTF8_BOM = b"\xef\xbb\xbf"
_WIDE_BOMS = ((b"\x00\x00\xfe\xff", "UTF-32"), (b"\xff\xfe\x00\x00", "UTF-32"), (b"\xfe\xff", "UTF-16"), (b"\xff\xfe", "UTF-16"))
_SAVE_AS_UTF8 = (
    "save it as UTF-8 (TOML files are UTF-8). Windows PowerShell 5.1 writes UTF-16 with '>' and "
    "Out-File and ANSI with Set-Content: add -Encoding utf8"
)


def _decode(raw: bytes, name: str) -> str:
    """UTF-8 bytes (with or without a BOM) as text; anything else is a DeployError with the fix."""
    for bom, kind in _WIDE_BOMS:
        if raw.startswith(bom):
            raise DeployError(f"{name} is {kind} text, not UTF-8: {_SAVE_AS_UTF8}")
    body = raw.removeprefix(_UTF8_BOM)  # by hand: the line numbers below count from the text
    if b"\0" in body:  # never in TOML: ASCII text saved as UTF-16 without a BOM
        line = body.count(b"\n", 0, body.index(b"\0")) + 1
        raise DeployError(f"{name} has NUL bytes (line {line}), like UTF-16 text without a BOM: {_SAVE_AS_UTF8}")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as e:
        line = body.count(b"\n", 0, e.start) + 1
        raise DeployError(
            f"{name} is not UTF-8 (byte 0x{body[e.start]:02X} on line {line}; ANSI/cp1252?): {_SAVE_AS_UTF8}"
        ) from None


def _read() -> tuple[str, bool]:
    """pytemplate.toml as text (line endings kept) and whether it starts with a UTF-8 BOM."""
    try:
        raw = CONFIG_FILE.read_bytes()
    except OSError as e:
        raise DeployError(f"cannot read {CONFIG_FILE.name}: {e.strerror or e}") from None
    return _decode(raw, CONFIG_FILE.name), raw.startswith(_UTF8_BOM)


def read_text() -> str:
    """pytemplate.toml as text: UTF-8 with or without a BOM (dropped); line endings are kept."""
    return _read()[0]


def load(builtin_commands: set[str] | None = None) -> Config:
    if not CONFIG_FILE.is_file():
        raise DeployError(f"{CONFIG_FILE.name} not found in the project root")
    try:
        data = tomllib.loads(read_text())
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"pytemplate.toml is not valid TOML: {e}") from None
    cfg: Config = _build(Config, data, "")
    validate(cfg, builtin_commands)
    return cfg


def compiled_paths(cfg: Config) -> list[str]:
    """Return the paths (relative to src/) of the modules/packages in compile.modules.

    The one Python imports: a folder with __init__.py (a package) wins over <name>.py, and
    <name>.py wins over a folder without __init__.py (a package turned into a module leaves its
    __pycache__ folder behind); a folder alone is a namespace package.
    """
    out: list[str] = []
    for m in cfg.compile.modules:
        base = m.replace(".", "/")
        folder = SRC / base
        package = (folder / "__init__.py").is_file() or not (SRC / f"{base}.py").is_file()
        out.append(base if package and folder.is_dir() else base + ".py")
    return out


# --- editing pytemplate.toml while keeping comments --------------------------------------------


def toml_value(value: Any) -> str:
    """A bool, int, str or list of them as a one-line TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if any("\ud800" <= c <= "\udfff" for c in value):
            raise DeployError(f"{value!r} cannot be written to TOML: it holds a lone surrogate (not valid Unicode)")
        # JSON escapes '"', '\' and U+0000-U+001F the way TOML does; TOML also forbids a raw DEL
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(value)


class _ScanError(Exception):
    """The scanner met something it does not understand: set_value refuses to edit."""


@dataclass
class _Stmt:
    kind: str  # "table" ([a.b]), "array" ([[a.b]]) or "key" (a key = value statement)
    path: tuple[str, ...]  # the header, or the table + the (dotted) key
    value: tuple[int, int]  # "key": the span of the value, which may cover several lines
    end: int  # just past the statement's line ending (or the end of the text)
    in_array: bool = False  # "key" statements under a [[header]]


def _skip_blank(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _line_end(text: str, i: int) -> int:
    """Index just past the rest of the line at i: blanks, an optional comment, the line ending."""
    i = _skip_blank(text, i)
    if text.startswith("#", i):
        nl = text.find("\n", i)
        return len(text) if nl < 0 else nl + 1
    if i >= len(text):
        return i
    if text.startswith("\n", i):
        return i + 1
    if text.startswith("\r\n", i):
        return i + 2
    raise _ScanError(i)


def _string_end(text: str, i: int) -> int:
    """Index just past the string that starts at i (basic, literal, or their multi-line forms)."""
    q = text[i]
    if text.startswith(q * 3, i):
        j = i + 3
        while j < len(text):
            if q == '"' and text[j] == "\\":
                j += 2
            elif text.startswith(q * 3, j):
                k = j + 3
                while k < min(j + 5, len(text)) and text[k] == q:  # """a"""" ends with a quote
                    k += 1
                return k
            else:
                j += 1
        raise _ScanError(i)
    j = i + 1
    while j < len(text) and text[j] not in (q, "\n"):
        j += 2 if q == '"' and text[j] == "\\" else 1
    if j >= len(text) or text[j] != q:
        raise _ScanError(i)
    return j + 1


def _value_end(text: str, i: int) -> int:
    """Index just past the value that starts at i (an array or inline table may span lines)."""
    if text.startswith(('"', "'"), i):
        return _string_end(text, i)
    if text.startswith(("[", "{"), i):
        depth, j = 0, i
        while j < len(text):
            c = text[j]
            if c in "\"'":
                j = _string_end(text, j)
                continue
            if c == "#":  # a comment inside a multi-line array
                nl = text.find("\n", j)
                j = len(text) if nl < 0 else nl
                continue
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        raise _ScanError(i)
    j = i  # a number, boolean or date: up to a comment or the end of the line
    while j < len(text) and text[j] not in "#\n":
        j += 1
    while j > i and text[j - 1] in " \t\r":
        j -= 1
    if j == i:
        raise _ScanError(i)
    return j


def _key(text: str, i: int) -> tuple[tuple[str, ...], int]:
    """A (dotted, maybe quoted) key at i: its parts and the index after it and its blanks."""
    parts: list[str] = []
    while True:
        i = _skip_blank(text, i)
        if text.startswith(('"', "'"), i):
            end = _string_end(text, i)
            parts.append(str(tomllib.loads("k = " + text[i:end])["k"]))
            i = end
        else:
            m = _BARE_KEY.match(text, i)
            if not m:
                raise _ScanError(i)
            parts.append(m.group())
            i = m.end()
        i = _skip_blank(text, i)
        if not text.startswith(".", i):
            return tuple(parts), i
        i += 1


def _statements(text: str) -> list[_Stmt]:
    """The top-level statements of a TOML text with their offsets.

    Strings and multi-line arrays are skipped as a whole, so a `[table]` or `key = ...` line
    inside a multi-line string or array is never taken for a real one.
    """
    out: list[_Stmt] = []
    table: tuple[str, ...] = ()
    in_array = False
    i = 0
    while i < len(text):
        i = _skip_blank(text, i)
        if i >= len(text):
            break
        if text[i] in "\r\n#":
            i = _line_end(text, i)
            continue
        if text[i] == "[":
            in_array = text.startswith("[[", i)
            table, i = _key(text, i + (2 if in_array else 1))
            close = "]]" if in_array else "]"
            if not text.startswith(close, i):
                raise _ScanError(i)
            i = _line_end(text, i + len(close))
            out.append(_Stmt("array" if in_array else "table", table, (i, i), i))
            continue
        key, i = _key(text, i)
        if not text.startswith("=", i):
            raise _ScanError(i)
        start = _skip_blank(text, i + 1)
        stop = _value_end(text, start)
        i = _line_end(text, stop)
        out.append(_Stmt("key", table + key, (start, stop), i, in_array))
    return out


def scan(text: str) -> list[_Stmt] | None:
    """The top-level statements of a TOML text (table headers, array-of-tables headers and keys,
    with their offsets), or None when the scanner cannot read it (not valid TOML). render reads
    pyproject.toml with it: a line of a multi-line string is never taken for a header or a key."""
    try:
        return _statements(text)
    except _ScanError:
        return None


def _edited(text: str, path: tuple[str, ...], rendered: str) -> str:
    """`text` with the value at `path` replaced by `rendered` (or the key/table added)."""
    stmts = _statements(text)
    m = re.search(r"\r?\n", text)
    eol = m.group() if m else "\n"
    hit = next((s for s in stmts if s.kind == "key" and not s.in_array and s.path == path), None)
    if hit:  # only the value changes: a comment after it and the line ending stay
        start, stop = hit.value
        return text[:start] + rendered + text[stop:]
    line = f"{path[-1]} = {rendered}{eol}"
    header = next((n for n, s in enumerate(stmts) if s.kind == "table" and s.path == path[:-1]), None)
    if header is None:  # a new table at the end, after a blank line
        body = text if not text or text.endswith("\n") else text + eol
        gap = eol if body.strip() and not body.endswith(eol * 2) else ""
        return f"{body}{gap}[{'.'.join(path[:-1])}]{eol}{line}"
    at = stmts[header].end  # after the table's last key: comments before the next header stay there
    for s in stmts[header + 1 :]:
        if s.kind != "key":
            break
        at = s.end
    head = text[:at]
    if head and not head.endswith("\n"):  # the last line of the file had no line ending
        head += eol
    return head + line + text[at:]


def set_value(text: str, table: str, key: str, value: Any) -> str:
    """Set `key = value` in `[table]` of a pytemplate.toml text and return the new text.

    Only the value changes: the comment after it, the other lines, the layout and the line
    endings (LF or CRLF) stay. The old value may span several lines (taplo, the TOML formatter,
    expands long arrays): all of it is replaced. A missing key is added after the table's last
    key, a missing table at the end. The result is parsed again: an edit that would change
    anything but this key (an unusual layout) is refused with a DeployError, never written.
    """
    path = (*table.split("."), key)
    rendered = toml_value(value)
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"pytemplate.toml is not valid TOML: {e}") from None
    try:
        new: str | None = _edited(text, path, rendered)
    except _ScanError:
        new = None
    if new is None or not _only_changed(before, new, path, value):
        raise DeployError(
            f"pytemplate.toml: could not set {'.'.join(path)} automatically (unusual layout): "
            f"set it by hand to {key} = {rendered} in [{table}]"
        )
    return new


def _only_changed(before: dict[str, Any], text: str, path: tuple[str, ...], value: Any) -> bool:
    """Whether `text` parses as `before` with only `path` set to `value`."""
    try:
        after = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    expected = copy.deepcopy(before)
    node: Any = expected
    for part in path[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            return False
    node[path[-1]] = value
    return bool(after == expected)


def update_file(changes: list[tuple[str, str, Any]]) -> None:
    """Apply `changes` (table, key, value) to pytemplate.toml, keeping comments and layout.

    Every edit is checked in memory first (set_value), so a failing one writes nothing and the
    file is never left as broken TOML. A UTF-8 BOM and the line endings are kept. Nothing is
    written when nothing changes or under --dry-run.
    """
    old, bom = _read()
    new = old
    for table, key, value in changes:
        new = set_value(new, table, key, value)
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:  # set_value checks each edit; this guards the sum
        raise DeployError(f"pytemplate.toml: the change would break the file ({e}); nothing was written") from None
    if new != old and not proc.DRY_RUN:
        CONFIG_FILE.write_text(("\ufeff" if bom else "") + new, encoding="utf-8", newline="\n")
