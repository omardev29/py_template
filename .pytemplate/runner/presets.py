"""Presets (script, raylib, flet): code skeleton + dependencies + initial config.

Each preset lives in .pytemplate/presets/<name>/:
  preset.toml      description, dependencies, extra [tool.uv] keys and extra pyproject tables
  files/           skeleton copied to the root. `__pkg__` in a path is replaced with the app
                   package, and `{{name}}`/`{{pkg}}` in text with the name and the package.
  constraints.txt  optional: the versions the template was tested with for every package a
                   project of the preset locks, its whole tested tree (see `constraints`).

`./pyt new` copies the template (`copy_template`) and runs `init` in the copy: `init` is its
internal step (and how the template maintainer regenerates the template root).
"""

from __future__ import annotations

import csv
import keyword
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import tomllib
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import proc, ui
from .config import APP_NAME, BACKENDS, NAME_RULE, TOML_ERRORS, toml_error
from .project import BUILD, INSTALL_RECORD, PRESETS, PYPROJECT, ROOT, TEMPLATE, rel, write_whole
from .ui import PytError

if TYPE_CHECKING:
    from .config import Config

# Files with these suffixes (or none, e.g. LICENSE) get token replacement and LF line endings,
# but only if they are really text (see _text_of); anything else is copied byte for byte
TEXT_SUFFIXES = {".py", ".pyi", ".toml", ".md", ".txt", ".json", ".cfg", ".ini", ".yml", ".yaml"}
EXTRA_BEGIN = "# >>> pytemplate-preset"
EXTRA_END = "# <<< pytemplate-preset"
# Folders owned by the preset: replaced as a whole when switching presets
OWNED_DIRS = ("src", "tests", "typings")
LOCK = ROOT / "uv.lock"
CONSTRAINTS = "constraints.txt"

# src/<pkg>/ may not be one of the project's own folders (the preset's src/ entries, such as
# src/main.py, are checked too: see check_name_free)
RESERVED_PACKAGES = {
    "src": "src/ (paths relative to the root and to src/ would read the same: the problem matchers, rename)",
    "tests": "tests/ (a package too: the imports would clash)",
    "typings": "typings/ (the stubs at the root: paths relative to the root and to src/ would read the same)",
    "build": "build/ (.gitignore excludes it at any depth: the app would never reach git)",
    "dist": "dist/ (.gitignore excludes it at any depth: the app would never reach git)",
    "assets": "src/assets/ (the data folder bundled with the app: app.assets)",
}
# Windows reserves these names (any case, any extension) for devices: src/aux/ cannot be created
# there, and git cannot check out a repository that holds it
WINDOWS_DEVICES = frozenset({"con", "prn", "aux", "nul", *(f"{d}{i}" for d in ("com", "lpt") for i in range(10))})
# The interpreters the generated Windows launchers call by bare name (pyz._wrapper_cmd,
# portable.cmd_launcher, common.windowed): cmd.exe looks in the current folder first, so an app
# called python started its own python.cmd again and again instead of Python
INTERPRETER_COMMANDS = frozenset({"py", "pyw", "python", "python3", "pythonw", "pypy", "pypy3", "pypyw"})
# The launcher's name (./pyt) and the command `pyt install` puts on PATH: the app's own `pyt`
# (the console script of its wheel) would take that place, and every ./pyt of its code and docs
# reads like the app's name (rename rewrote them)
LAUNCHER_NAMES = frozenset({"pyt"})
# The top-level modules a pinned package (constraints.txt of any preset) installs under another
# name than its own (normalized, '_' for '-'), read from the wheels' RECORD files; names that
# cannot be an app package (_pytest, _yaml, cffi-stubs...) are left out. src/<pkg>/ with such a
# name shadows the library: a project named py failed ./pyt test at once (pytest imports its
# `py` shim), one named markdown-it broke rich.markdown. Compared in lower case (PIL and src/pil/
# merge on a case-insensitive file system). test_import_names_follow_the_installed_packages
# checks it against what .venv installs.
IMPORT_NAMES: dict[str, tuple[str, ...]] = {
    "markdown-it-py": ("markdown_it",),
    "mypy": ("mypyc",),
    "pefile": ("ordlookup", "peutils"),
    "pillow": ("PIL",),
    "pytest": ("py",),
    "python-dateutil": ("dateutil",),
    "python-slugify": ("slugify",),
    "pywin32-ctypes": ("win32ctypes",),
    "pyyaml": ("yaml",),
    "raylib": ("pyray",),
}


def available() -> list[str]:
    return sorted(p.name for p in PRESETS.iterdir() if (p / "preset.toml").is_file())


# The keys of preset.toml and what each holds
PRESET_KEYS: dict[str, str] = {
    "description": "a string",
    "dependencies": "a list of strings",
    "dev_dependencies": "a list of strings",
    "options": "a table",
    "uv": "a table",
    "pyproject": "a string",
}


def _holds(value: Any, kind: str) -> bool:
    if kind == "a list of strings":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return isinstance(value, dict if kind == "a table" else str)


def load(name: str) -> dict[str, Any]:
    """preset.toml of `name`, read like every file the template ships (a BOM is fine). A file
    that cannot be read, is not TOML or holds an unknown key or a wrong type is a PytError
    naming it: render (the managed [tool.uv] block) and new read it on every run."""
    path = PRESETS / name / "preset.toml"
    if not path.is_file():
        raise PytError(f"unknown preset '{name}' (available: {', '.join(available())})")
    where = rel(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as e:
        raise PytError(f"{where} is not UTF-8 text (byte {e.start}): save it as UTF-8") from None
    except OSError as e:
        raise PytError(f"cannot read {where}: {e.strerror or e}") from None
    except tomllib.TOMLDecodeError as e:
        raise PytError(f"{where} is not valid TOML: {e}") from None
    for key, value in data.items():
        kind = PRESET_KEYS.get(key)
        if kind is None:
            raise PytError(f"{where}: unknown key '{key}' (known: {', '.join(PRESET_KEYS)})")
        if not _holds(value, kind):
            raise PytError(f"{where}: '{key}' must be {kind}")
    return data


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
        rel_path = path.relative_to(base).as_posix().replace("__pkg__", pkg)
        data = path.read_bytes()
        text = _text_of(path, data)
        if text is not None:
            text = text.replace("{{name}}", name).replace("{{pkg}}", pkg)
            data = text.replace("\r\n", "\n").encode("utf-8")
        out[rel_path] = data
    return out


def _owned_files() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for d in OWNED_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and not any(p in path.parts for p in ("__pycache__", ".pytest_cache", ".hypothesis")):
                data = path.read_bytes()
                if _text_of(path, data) is not None:  # git may check text files out with CRLF
                    data = data.replace(b"\r\n", b"\n")
                out[path.relative_to(ROOT).as_posix()] = data
    return out


def pristine(cfg: Config) -> bool:
    """Return whether src/, tests/ and typings/ are exactly the current preset's skeleton (untouched)."""
    expected = {k: v for k, v in skeleton(cfg.app.preset, cfg.app.name).items() if k.split("/")[0] in OWNED_DIRS}
    return _owned_files() == expected


# --- names -------------------------------------------------------------------------------------


def name_from_folder(folder: str) -> str:
    """The app name `./pyt new DIR` derives from the folder name: accents dropped (NFKD, then
    the combining marks: an accented e becomes e), every run of other characters, letters
    without an ASCII form included (a sharp s, an o with stroke), -> '-' (no '--'), and no '-'
    or '_' at either end (uv refuses a name that does not end with a letter or digit)."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", folder) if not unicodedata.combining(c))
    return re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9_-]+", "-", plain)).strip("-_")


def _norm_name(req: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else req


def _read_text(path: Path, what: str) -> str:
    """A text file that uv reads too, such as pyproject.toml: a BOM is dropped, as uv does."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise PytError(f"{what} not found: {path}") from None
    except UnicodeDecodeError as e:
        raise PytError(f"{what} is not UTF-8 text (byte {e.start}): save it as UTF-8") from None
    except OSError as e:
        raise PytError(f"cannot read {what}: {e.strerror or e}") from None


def _read_toml(path: Path, what: str) -> dict[str, Any]:
    try:
        return tomllib.loads(_read_text(path, what))
    except TOML_ERRORS as e:  # an integer of 5000 digits, arrays nested a thousand deep: no TOMLDecodeError
        raise PytError(f"{what} is not valid TOML: {toml_error(e)}") from None


def read_pyproject() -> dict[str, Any]:
    """pyproject.toml, parsed (a BOM is fine); a PytError that names the problem otherwise."""
    return _read_toml(PYPROJECT, "pyproject.toml")


def _names(items: Any) -> set[str]:
    return {_norm_name(r) for r in items if isinstance(r, str)} if isinstance(items, list) else set()


def _declared_requirements(group: str | None) -> list[str]:
    """The requirements of [project] dependencies (group None) or of a dependency group."""
    data = _read_toml(PYPROJECT, "pyproject.toml")
    table = data.get("project") if group is None else data.get("dependency-groups")
    items = table.get("dependencies" if group is None else group) if isinstance(table, dict) else None
    return [r for r in items if isinstance(r, str)] if isinstance(items, list) else []


def _declared(group: str | None) -> set[str]:
    """The requirement names of [project] dependencies (group None) or of a dependency group."""
    return _names(_declared_requirements(group))


def _declared_anywhere() -> set[str]:
    """Every requirement name of pyproject.toml: dependencies, extras and every group."""
    data = _read_toml(PYPROJECT, "pyproject.toml")
    project = data.get("project")
    if not isinstance(project, dict):
        project = {}
    out = _names(project.get("dependencies"))
    for table in (project.get("optional-dependencies"), data.get("dependency-groups")):
        if isinstance(table, dict):
            for items in table.values():
                out |= _names(items)
    return out


def _lock_entries(lock: Path | None = None) -> list[dict[str, Any]]:
    """The [[package]] tables of a uv.lock ([] when it is missing or unreadable: uv says why)."""
    try:
        data = tomllib.loads((lock or LOCK).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    entries = data.get("package")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)]


def _is_project(entry: dict[str, Any]) -> bool:
    """The project's own entry in uv.lock (source virtual or editable ".")."""
    source = entry.get("source")
    return isinstance(source, dict) and "." in (source.get("virtual"), source.get("editable"))


def _requires(entry: dict[str, Any]) -> set[str]:
    """The names a uv.lock entry depends on: every extra, group and marker included."""
    items: list[Any] = list(entry.get("dependencies") or [])
    for table in ("optional-dependencies", "dev-dependencies"):
        groups = entry.get(table)
        if isinstance(groups, dict):
            for group in groups.values():
                items += group if isinstance(group, list) else []
    return {_norm_name(i["name"]) for i in items if isinstance(i, dict) and isinstance(i.get("name"), str)}


def _lock_graph(lock: Path | None = None) -> dict[str, set[str]]:
    """{package: the packages it needs} from uv.lock (normalized names, without the project)."""
    graph: dict[str, set[str]] = {}
    for entry in _lock_entries(lock):
        if not _is_project(entry):
            graph.setdefault(_norm_name(entry["name"]), set()).update(_requires(entry))
    return graph


def _self_dependents(name: str, lock: Path | None = None) -> list[str]:
    """The packages of uv.lock that depend on a package called `name`, when that package is
    the project itself: uv (0.12) resolves such a dependency to the project (markdown-it-py's
    mdurl -> a project named mdurl) instead of refusing it, and the library is then missing."""
    wanted = _norm_name(name)
    entries = _lock_entries(lock)
    if not any(_is_project(e) and _norm_name(e["name"]) == wanted for e in entries):
        return []
    return sorted({e["name"] for e in entries if not _is_project(e) and wanted in _requires(e)})


def _closure(graph: dict[str, set[str]], roots: set[str]) -> set[str]:
    seen: set[str] = set()
    todo = [r for r in roots if r in graph]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo += [c for c in graph[name] if c in graph and c not in seen]
    return seen


def locked_names(roots: set[str] | None = None, lock: Path | None = None) -> set[str]:
    """Normalized names of the packages in uv.lock, the project itself excluded. With `roots`,
    only the ones they need (dependencies of dependencies included, markers ignored: uv resolves
    the project's own name the same way on every platform, refusing the project or taking it
    for the dependency)."""
    graph = _lock_graph(lock)
    return set(graph) if roots is None else _closure(graph, roots)


def constraints_path(preset: str) -> Path:
    return PRESETS / preset / CONSTRAINTS


def constraints(preset: str) -> dict[str, str]:
    """{normalized name: version} from PRESETS/<preset>/constraints.txt ({} without the file).

    The file pins, one `name==version` per line, every package a project of this preset locks
    (the template's own packages it keeps, the preset's dependencies and what they pull in), at
    the versions the template was tested with. `init` hands the ones the project does not lock
    yet to `uv add --constraints`: a one-off, nothing is written into pyproject.toml, and
    `./pyt lock --upgrade` moves on later. The name check reads it too: from a project whose
    uv.lock lacks the preset's tree, it is the only place that names that tree (a project named
    mdurl made from a raylib project got markdown-it-py's mdurl resolved to itself). How to
    regenerate it: CLAUDE.md section 11.
    """
    path = constraints_path(preset)
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise PytError(f"{rel(path)} is not UTF-8 text (byte {e.start}): regenerate it (CLAUDE.md section 11)") from None
    except OSError as e:
        raise PytError(f"cannot read {rel(path)}: {e.strerror or e}") from None
    pins: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([A-Za-z0-9][A-Za-z0-9._+!-]*)", line)
        if m is None:
            raise PytError(f"{rel(path)}:{number}: expected name==version, found {line!r}")
        pins[_norm_name(m.group(1))] = m.group(2)
    return pins


def constraints_text(preset: str, project_lock: Path) -> str:
    """The constraints.txt of `preset`, made from the uv.lock of a project just created with it
    from the template: every package it locks (the project itself excluded). A package locked
    at two versions (a fork by platform) cannot be pinned and is left out (init's check of the
    resolved uv.lock, `_self_dependents`, still covers its name)."""
    versions: dict[str, set[str]] = {}
    for entry in _lock_entries(project_lock):
        if not _is_project(entry):
            versions.setdefault(_norm_name(entry["name"]), set()).add(str(entry.get("version", "")))
    lines = [
        f"# Versions the template was tested with for every package a project of the {preset} preset",
        "# locks. `./pyt new` hands the ones the source project does not lock yet to `uv add",
        "# --constraints` (a one-off: pyproject.toml keeps plain bounds), and the name check refuses",
        "# them all. Regenerate it, never edit it: CLAUDE.md section 11.",
        *(f"{name}=={next(iter(v))}" for name, v in sorted(versions.items()) if len(v) == 1 and "" not in v),
    ]
    return "\n".join(lines) + "\n"


def _skeleton_src_names(preset: str) -> dict[str, str]:
    """The entries of the preset's src/ besides the package: a package with the same name would
    shadow them (`import main` finds a package before main.py) or merge with them."""
    base = PRESETS / preset / "files" / "src"
    if not base.is_dir():
        return {}
    return {
        (p.stem if p.is_file() else p.name).lower(): f"src/{p.name}" + ("" if p.is_file() else "/")
        for p in base.iterdir()
        if p.name != "__pkg__"
    }


def _dependency_names(cfg: Config | None, preset: str) -> set[str]:
    """Normalized names of every package the project has after `init preset`: what pyproject
    declares (without the current preset's own dependencies, which init removes), the preset's
    dependencies, and what they pull in (from uv.lock and the preset's constraints, which name
    the preset's whole tested tree even when uv.lock lacks it)."""
    target = load(preset)
    opts: dict[str, Any] = dict(target.get("options", {}))
    if cfg is not None and preset == cfg.app.preset:
        opts.update(cfg.preset_options(preset))
    new = {_norm_name(str(_fmt(d, opts))) for d in (*target.get("dependencies", []), *target.get("dev_dependencies", []))}
    kept = _declared_anywhere()
    if cfg is not None:
        old_deps, old_dev = dependencies(cfg)
        kept -= {_norm_name(d) for d in (*old_deps, *old_dev)} - new
    kept |= new
    pins = set(constraints(preset))
    graph = _lock_graph()
    names = kept | _closure(graph, kept) | pins
    if pins - set(graph):
        # The preset brings packages uv.lock does not have: the pins name what they needed when
        # the template was tested, but uv may resolve them against the versions this lock holds,
        # which may need any package already locked (conservative)
        names |= set(graph)
    return names


def _record_modules(info: Path) -> set[str]:
    """The top-level modules a distribution installed (its `*.dist-info` folder): the first part
    of every path of its RECORD (a package folder, `mod.py`, `mod.<abi>.so`), without the
    metadata, the scripts outside site-packages and anything that is no identifier."""
    try:
        rows = list(csv.reader((info / "RECORD").read_text(encoding="utf-8").splitlines()))
    except (OSError, UnicodeDecodeError, csv.Error):
        return set()
    out: set[str] = set()
    for row in rows:
        parts = row[0].replace("\\", "/").split("/") if row else []
        if not parts or parts[0].endswith((".dist-info", ".data")):
            continue
        head = parts[0] if len(parts) > 1 else parts[0].split(".")[0]
        if len(parts) == 1 and not parts[0].endswith((".py", ".so", ".pyd")):
            continue  # a .pth file and the like
        if head.isidentifier() and head != "__pycache__":
            out.add(head)
    return out


def _installed_import_names() -> dict[str, set[str]]:
    """The top-level modules of every distribution installed in an environment of the project
    (`.venv*`, either layout), by normalized distribution name: what a dependency the user
    added installs under another name (beautifulsoup4's bs4), which IMPORT_NAMES cannot know.
    Empty without an environment (a fresh clone, the copy `new` makes: its source checked it)."""
    out: dict[str, set[str]] = {}
    for env in sorted(p for p in ROOT.glob(".venv*") if p.is_dir()):
        for site in (*env.glob("lib/*/site-packages"), env / "Lib" / "site-packages"):
            for info in sorted(site.glob("*.dist-info")) if site.is_dir() else ():
                dist = _norm_name(info.name[: -len(".dist-info")].rsplit("-", 1)[0])
                out.setdefault(dist, set()).update(_record_modules(info))
    return out


def check_name_free(cfg: Config | None, preset: str, name: str) -> None:
    """Reject an app name that would break the project: one uv refuses (APP_NAME), a package
    src/<pkg>/ that is a Python keyword, a standard library module (of any supported Python), a
    backend name, a Windows device name, one of the project's own folders or files, a package
    the project depends on, directly or not (uv refuses the project, or resolves a dependency
    of a dependency to the project itself when its version fits, section 15.1 of CLAUDE.md; and
    src/<pkg>/ would shadow the library), or a module such a package installs under another
    name (IMPORT_NAMES for the presets' pins: pytest's py, raylib's pyray; the environments of
    the project for the rest: beautifulsoup4's bs4 once `./pyt add` installed it).
    new, init, their dry runs and rename (which keeps the first line) call it."""
    pkg = name.replace("-", "_").lower()
    hint = "\n  Choose another name with --name NAME"
    if not APP_NAME.fullmatch(name):
        raise PytError(f"'{name}' is not a valid app name: it may only contain {NAME_RULE}.{hint}")
    if keyword.iskeyword(pkg):
        raise PytError(f"the package '{pkg}' would be a Python keyword (`import {pkg}` is a syntax error).{hint}")
    if shadows_stdlib(pkg):  # every Python the project can run on, not only the runner's
        raise PytError(f"src/{pkg}/ would shadow the standard library module '{pkg}'.{hint}")
    if pkg in BACKENDS:  # src/mypyc/ would shadow mypy's compiler; `./pyt run pypy` reads a backend
        raise PytError(f"'{name}' is the name of a backend ({', '.join(BACKENDS)}).{hint}")
    if pkg in WINDOWS_DEVICES:
        raise PytError(
            f"src/{pkg}/ cannot exist on Windows: '{pkg}' is a reserved device name there (CON, PRN, "
            f"AUX, NUL, COM0-9, LPT0-9) and a repository holding it cannot be checked out.{hint}"
        )
    if name.lower() in LAUNCHER_NAMES:
        raise PytError(
            f"'{name}' is the name of the ./pyt launcher and of the `pyt` command that pyt install puts on PATH: "
            f"the app's own `{name}` command would take its place.{hint}"
        )
    if name.lower() in INTERPRETER_COMMANDS:
        raise PytError(
            f"'{name}' is the name of a Python command: the Windows launchers of the pyz and portable "
            f"builds ({name}.cmd) call it by name, and cmd.exe would start {name}.cmd itself again and again.{hint}"
        )
    taken = {**_skeleton_src_names(preset), **RESERVED_PACKAGES}
    if pkg in taken:
        raise PytError(f"src/{pkg}/ would collide with the project's own {taken[pkg]}.{hint}")
    clash = _norm_name(name)
    names = _dependency_names(cfg, preset)
    if clash in names:
        raise PytError(
            f"the app name '{name}' is also the name of a dependency of the '{preset}' preset "
            f"({clash}, direct or indirect): uv would refuse the project or resolve that dependency "
            f"to the project itself, and src/{pkg}/ would shadow the library.{hint}"
        )
    installed = _installed_import_names()
    for dist in sorted(names & (IMPORT_NAMES.keys() | installed.keys())):
        modules = sorted({*IMPORT_NAMES.get(dist, ()), *installed.get(dist, ())})
        module = next((m for m in modules if m.lower() == pkg), None)
        if module is not None:
            raise PytError(
                f"src/{pkg}/ would shadow the module '{module}' of {dist}, a dependency of the "
                f"'{preset}' preset (direct or indirect): `import {module}` would find the app.{hint}"
            )


# --- pyproject ---------------------------------------------------------------------------------


# A [project] header, any table header (the end of [project]), and a `name = "..."` line: a
# trailing \r is fine (rename passes CRLF text)
_PROJECT_HEADER = re.compile(r"[ \t]*\[[ \t]*project[ \t]*\][ \t\r]*(?:#.*)?")
_TABLE_HEADER = re.compile(r"[ \t]*\[\[?[^\[\]\n]+\]\]?[ \t\r]*(?:#.*)?")
def _string_key(key: str) -> re.Pattern[str]:
    """A `key = "..."` (or '...') line, the key bare or quoted; group 1 is everything before the
    value. A multi-line string never matches (its opening quotes are not an empty string)."""
    k = re.escape(key)
    return re.compile(rf"""([ \t]*(?:{k}|"{k}"|'{k}')[ \t]*=[ \t]*)(?:"(?:[^"\\\n]|\\.)*"(?!")|'[^'\n]*'(?!'))""")


def _set_project_string(text: str, key: str, value: str) -> str:
    """`text` with the string `key` of its [project] table set to `value` (a one-line TOML
    string); the same key of any other table (an index's `name`, a tool's) is left alone.
    Unchanged without one: callers that need the key check the result."""
    from .config import toml_value

    pattern = _string_key(key)
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if _PROJECT_HEADER.fullmatch(ln)), len(lines))
    for i in range(start + 1, len(lines)):
        if _TABLE_HEADER.fullmatch(lines[i]):
            break
        m = pattern.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}{toml_value(value)}{lines[i][m.end():]}"
            break
    return "\n".join(lines)


def _set_project_name(text: str, name: str) -> str:
    """`text` with the name of its [project] table set to `name` (_set_project_string).
    Unchanged without one: pyproject_after_init checks the result."""
    return _set_project_string(text, "name", name)


def _extra_bounds(lines: list[str]) -> tuple[int, int] | None:
    """The lines of the preset markers (None: no preset block); PytError when they are damaged."""
    begins = [i for i, ln in enumerate(lines) if ln.strip() == EXTRA_BEGIN]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == EXTRA_END]
    if not begins and not ends:
        return None
    if len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0]:
        return begins[0], ends[0]
    order = " (the closing one comes first)" if len(begins) == len(ends) == 1 else ""
    raise PytError(
        f"pyproject.toml: the preset's tables sit between one '{EXTRA_BEGIN}' line and one "
        f"'{EXTRA_END}' line; found {len(begins)} and {len(ends)}{order}.\n"
        "  Restore the markers around those tables (or delete the markers with the tables), then try again"
    )


def _set_extra_tables(text: str, extra: str) -> str:
    """`text` (LF) with the preset block replaced by `extra` (dropped when `extra` is empty)."""
    lines = text.rstrip("\n").split("\n") if text.strip() else []
    bounds = _extra_bounds(lines)
    if bounds is not None:
        del lines[bounds[0] : bounds[1] + 1]
        while lines and not lines[-1].strip():
            lines.pop()
    if extra.strip():
        lines += ["", EXTRA_BEGIN, *extra.strip("\n").split("\n"), EXTRA_END]
    return "\n".join(lines) + "\n"


def extra_tables(preset: str, name: str) -> str:
    """The preset's extra pyproject.toml tables, with {{name}} and {{pkg}} filled in."""
    pkg = name.replace("-", "_").lower()
    return str(load(preset).get("pyproject", "")).replace("{{name}}", name).replace("{{pkg}}", pkg)


# --- names and option-driven dependencies (./pyt apply, ./pyt rename) ------------------------

# Top-level standard-library modules of only SOME of the Pythons a project can run on (PyPy 3.11
# ... the newest CPython). sys.stdlib_module_names only knows the runner's own version: the
# runner runs on python.cpython, but the app may also run on PyPy 3.11, and python.cpython can move.
STDLIB_OTHER_VERSIONS = frozenset(
    {
        "annotationlib", "compression", "profiling",  # new in 3.14 / 3.15
        "aifc", "asynchat", "asyncore", "audioop", "cgi", "cgitb", "chunk", "crypt", "distutils",
        "imghdr", "imp", "lib2to3", "mailcap", "msilib", "nis", "nntplib", "ossaudiodev", "pipes",
        "smtpd", "sndhdr", "spwd", "sunau", "telnetlib", "uu", "xdrlib",  # removed in 3.12 / 3.13 (PyPy 3.11 has them)
        "sre_compile", "sre_constants", "sre_parse",  # removed in 3.15
        # PyPy 3.11's own sys.stdlib_module_names and built-ins (a built-in is found before
        # sys.path: an app named pypyjit could not import itself there)
        "cpyext", "ctypes_support", "future_builtins", "greenlet", "identity_dict", "pypyjit",
        "stackless", "tputil",
    }
)


def shadows_stdlib(pkg: str) -> bool:
    """Whether src/<pkg>/ would shadow a standard-library module on some supported Python."""
    return pkg in sys.stdlib_module_names or pkg in STDLIB_OTHER_VERSIONS


def set_project_name(text: str, name: str) -> str:
    """`text` (a pyproject.toml) with [project] name = `name` (_set_project_name). PytError when
    the result does not say so, so no caller reports a change it did not make (apply, rename). A
    text that is no valid TOML is said to be so (it was "edit that line by hand")."""
    try:
        data = tomllib.loads(text.lstrip("\ufeff"))
    except TOML_ERRORS as e:
        raise PytError(f'could not set [project] name = "{name}": pyproject.toml is not valid TOML: {toml_error(e)}') from None
    new = _set_project_name(text, name)
    if project_name(new) != name:
        table = data.get("project")
        if not isinstance(table, dict):  # no line to edit: say what to add ("edit that line" named none)
            raise PytError(f'could not set [project] name = "{name}": pyproject.toml has no [project] table: add one, with name = "{name}"')
        if "name" not in table:
            raise PytError(f'could not set [project] name = "{name}": pyproject.toml has no [project] name: add name = "{name}" to that table')
        raise PytError(f'could not set [project] name = "{name}" in pyproject.toml: edit that line by hand and try again')
    return new


def project_name(text: str) -> str | None:
    """Return [project] name of a pyproject.toml text (None: missing, not a string or not TOML)."""
    try:
        value = tomllib.loads(text.lstrip("\ufeff")).get("project", {}).get("name")
    except (*TOML_ERRORS, AttributeError):
        return None
    return value if isinstance(value, str) else None


def default_options(preset: str) -> dict[str, Any]:
    """Return the [options] defaults of a preset (what `init` applies)."""
    return dict(load(preset).get("options", {}))


def option_dependencies(preset: str, opts: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return the preset requirements that use an option ({version}, {package}), formatted with
    `opts`: (dependencies, dev group). pytemplate.toml [preset.<name>] keeps them in sync; the
    plain ones (rich, types-cffi) belong to the project after `init` (./pyt add/remove)."""
    data = load(preset)
    out: list[list[str]] = []
    for key in ("dependencies", "dev_dependencies"):
        reqs: list[str] = []
        for template in data.get(key, []):
            if "{" not in str(template):
                continue
            try:
                reqs.append(str(template).format_map(opts))
            except (KeyError, IndexError, ValueError) as e:
                raise PytError(f"preset {preset}: cannot format {template!r} with [preset.{preset}] ({e!r})") from None
        out.append(reqs)
    return out[0], out[1]


def _contains(data: Any, part: Any) -> bool:
    """Whether every key of `part` is in `data` with the same value (tables compared deeply)."""
    if isinstance(part, dict):
        return isinstance(data, dict) and all(k in data and _contains(data[k], v) for k, v in part.items())
    return bool(data == part)


def pyproject_after_init(cfg: Config, preset: str, name: str) -> str:
    """pyproject.toml as `init` writes it for the new configuration `cfg`: the old preset's tables
    out, the project name and the managed parts in, then the new preset's tables. PytError when
    the result would not be valid TOML (a table of the preset also defined outside the markers)
    or would not hold all of them; damaged markers are a PytError too."""
    from . import render

    text = _read_text(PYPROJECT, "pyproject.toml").replace("\r\n", "\n")
    extra = extra_tables(preset, name)
    try:
        wanted = tomllib.loads(extra)
    except tomllib.TOMLDecodeError as e:
        raise PytError(f"{rel(PRESETS / preset / 'preset.toml')}: the pyproject tables are not valid TOML ({e})") from None
    hint = f"fix the '{EXTRA_BEGIN}' / '{EXTRA_END}' and '# >>> pytemplate' markers of pyproject.toml and try again"
    try:
        managed = render.pyproject_expected(cfg, _set_extra_tables(_set_project_name(text, name), ""))
        text = _set_extra_tables(managed, extra)
        data = tomllib.loads(text)
    except TOML_ERRORS as e:
        raise PytError(
            f"pyproject.toml would not be valid TOML with the tables of the '{preset}' preset ({toml_error(e)}).\n"
            f"  One of them is probably defined outside the markers: {hint}"
        ) from None
    project = data.get("project")
    if not isinstance(project, dict) or project.get("name") != name:
        raise PytError(f"pyproject.toml: the [project] table has no name = \"...\" line to set to '{name}': add one")
    if not _contains(data, wanted) or render.pyproject_expected(cfg, text) != text:
        raise PytError(f"pyproject.toml: the tables of the '{preset}' preset or the managed [tool.uv] block did not land: {hint}")
    return text


# --- init ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InitPlan:
    """What `init` does, worked out and checked before anything is written."""

    preset: str
    name: str
    description: str
    files: dict[str, bytes]  # the skeleton (pytemplate.toml included), relative to ROOT
    cfg: Config  # the new configuration, validated
    pyproject: str  # pyproject.toml with the new name, preset tables and managed parts
    drop: list[str]  # the old preset's requirements that uv removes
    drop_dev: list[str]
    add: list[str]  # the new preset's requirements that uv adds
    add_dev: list[str]
    pins: dict[str, str]  # tested versions of the packages uv.lock does not have yet (constraints)


def _requirement_key(req: str) -> str:
    """A requirement as uv compares it: the name normalized, no blanks."""
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)(.*)", req, re.S)
    return _norm_name(m.group(1)) + re.sub(r"\s+", "", m.group(2)) if m else req.strip()


def _dropped(old: list[str], new: list[str], added: list[str], declared: list[str]) -> list[str]:
    """The requirements of one group (`declared`: what pyproject.toml lists there) that init
    removes before the new preset's are added: the old preset's that the new one does not have,
    and every one the new preset adds (`added`, both groups) in another form. A pin left in
    place breaks the resolving `uv add`: flet-cli==1.0.0 in the dev group (a flet project with
    [preset.flet] version = "1.0.0") pins flet==1.0.0, so adding flet==1.0.1 had no solution."""
    keep = {_norm_name(r) for r in new}
    gone = {_norm_name(r) for r in old} - keep
    again = {_norm_name(r): _requirement_key(r) for r in added}
    return sorted(
        (r for r in declared if _norm_name(r) in gone or again.get(_norm_name(r), _requirement_key(r)) != _requirement_key(r)),
        key=_norm_name,
    )


def plan_init(cfg: Config, preset: str, name: str | None, *, force: bool) -> InitPlan:
    """Every check `init` makes, in memory: nothing is written (the --dry-run of init prints it)."""
    from . import config, render

    new_name = name or cfg.app.name
    check_name_free(cfg, preset, new_name)  # the format too
    target = load(preset)
    if not force and not pristine(cfg):
        raise PytError(
            "src/, tests/ or typings/ have changes compared to the skeleton of the current preset "
            f"('{cfg.app.preset}'). init would replace them.\n  If you are sure: ./pyt __init {preset} --force"
        )
    files = skeleton(preset, new_name)
    where = f"preset {preset}: files/pytemplate.toml"
    if "pytemplate.toml" not in files:
        raise PytError(f"{where} is missing")
    try:
        data = tomllib.loads(files["pytemplate.toml"].decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PytError(f"{where} is not valid TOML: {e}") from None
    new_cfg: Config = config._build(config.Config, data, "")
    config.validate(new_cfg)
    if (new_cfg.app.name, new_cfg.app.preset) != (new_name, preset):
        raise PytError(f"{where}: [app] must say name = \"{{{{name}}}}\" and preset = \"{preset}\"")
    # The managed parts of pyproject.toml can be rewritten for the new configuration (broken
    # markers, a managed key repeated outside them...): refused here, before anything changes
    render.check_pyproject(new_cfg)
    text = pyproject_after_init(new_cfg, preset, new_name)
    old_deps, old_dev = dependencies(cfg)
    new_deps, new_dev = dependencies(new_cfg, preset)
    locked = locked_names()
    return InitPlan(
        preset=preset,
        name=new_name,
        description=str(target.get("description", "")),
        files=files,
        cfg=new_cfg,
        pyproject=text,
        drop=_dropped(old_deps, new_deps, [*new_deps, *new_dev], _declared_requirements(None)),
        drop_dev=_dropped(old_dev, new_dev, [*new_deps, *new_dev], _declared_requirements("dev")),
        add=new_deps,
        add_dev=new_dev,
        pins={n: v for n, v in constraints(preset).items() if n not in locked},
    )


def _remove(path: Path) -> bool:
    """Delete a file, link or folder, read-only entries included; return whether it is gone."""

    def retry(func: Callable[..., object], name: str, *_: object) -> None:
        if func not in (os.unlink, os.rmdir, os.remove):
            return  # what is left is checked below
        try:
            os.chmod(name, stat.S_IMODE(os.lstat(name).st_mode) | stat.S_IWRITE)
            func(name)
        except OSError:
            pass

    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except OSError:
            retry(os.unlink, str(path))
    elif path.is_dir():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)
    return not os.path.lexists(path)


class _Undo:
    """What init changed so far, to put back when a later step fails."""

    def __init__(self) -> None:
        self.saved: dict[Path, bytes | None] = {}  # file -> its bytes before (None: it did not exist)
        self.aside: Path | None = None  # where the old src/, tests/ and typings/ were moved
        self.existed: set[str] = set()  # owned folders that existed before
        self.moved: list[str] = []  # owned folders moved aside

    def save(self, path: Path) -> None:
        if path not in self.saved:
            self.saved[path] = path.read_bytes() if path.is_file() else None

    def rollback(self) -> list[str]:
        """Put every file and folder back; return what could not be (normally nothing)."""
        left: list[str] = []
        if self.aside is not None:
            for d in OWNED_DIRS:
                if d not in self.moved and d in self.existed:
                    continue  # never touched (the move stopped before it)
                if not _remove(ROOT / d):
                    left.append(f"{d}/ (partly written)")
                elif d in self.moved:
                    try:
                        (self.aside / d).rename(ROOT / d)
                    except OSError:
                        left.append(f"{d}/ (the original is in {rel(self.aside / d)})")
            if not left:
                _remove(self.aside)
        for path, data in self.saved.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    write_whole(path, data)
            except OSError:
                left.append(rel(path))
        return left

    def discard(self) -> None:
        """Success: delete the old folders moved aside."""
        if self.aside is not None and not _remove(self.aside):
            ui.warn(f"init could not delete {rel(self.aside)} (the old src/, tests/, typings/): delete it by hand")


def _swap_dependencies(plan: InitPlan, undo: _Undo) -> None:
    """pyproject.toml, then the dependency swap in uv.lock (uv: the only step that needs the network)."""
    from . import envs

    undo.save(PYPROJECT)
    undo.save(LOCK)
    write_whole(PYPROJECT, plan.pyproject.encode("utf-8"))
    uv = proc.find_uv()
    q = ["--quiet"] if ui.QUIET else []  # -q: uv's progress too (its errors still show)
    env = envs.env_vars(envs.tool_env(plan.cfg))
    # --frozen: only pyproject.toml changes; the next resolution sees every removal at once
    if plan.drop:
        proc.run([uv, "remove", *q, "--frozen", *sorted({_norm_name(r) for r in plan.drop})], env=env)
    if plan.drop_dev:
        proc.run([uv, "remove", *q, "--frozen", "--dev", *sorted({_norm_name(r) for r in plan.drop_dev})], env=env)
    pins: list[str] = []
    if plan.pins:  # the versions the template was tested with, for this resolution only
        path = BUILD / "init" / CONSTRAINTS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{n}=={v}\n" for n, v in sorted(plan.pins.items())), encoding="utf-8", newline="\n")
        # Relative to the root, uv's working folder: uv splits a --constraints value at every
        # space (astral-sh/uv#12639), and the project's own folder may hold one
        pins = ["--constraints", path.relative_to(ROOT).as_posix()]
    if plan.add:
        proc.run([uv, "add", *q, "--no-sync", *pins, *plan.add], cwd=ROOT, env=env)
    if plan.add_dev:
        proc.run([uv, "add", *q, "--no-sync", "--dev", *pins, *plan.add_dev], cwd=ROOT, env=env)
    proc.run([uv, "lock", *q], env=env)
    # The name check knows the tested tree (constraints.txt); this catches what it cannot know
    needs = _self_dependents(plan.name)
    if needs:
        raise PytError(
            f"the app name '{plan.name}' is also the name of a package the '{plan.preset}' preset needs "
            f"({', '.join(needs)} depend{'s' if len(needs) == 1 else ''} on {_norm_name(plan.name)}): uv "
            "resolved it to the project itself, so the library would be missing.\n"
            "  Choose another name with --name NAME"
        )


def _swap_files(plan: InitPlan, undo: _Undo) -> None:
    """src/, tests/ and typings/ become the skeleton, and pytemplate.toml the preset's. The old
    folders are first moved aside (a locked file makes that fail before anything changed)."""
    undo.existed = {d for d in OWNED_DIRS if os.path.lexists(ROOT / d)}
    undo.aside = Path(tempfile.mkdtemp(prefix=".pytemplate-init-", dir=ROOT))
    for d in OWNED_DIRS:
        if d in undo.existed:
            try:
                (ROOT / d).rename(undo.aside / d)
            except OSError as e:
                raise PytError(
                    f"init cannot move {d}/ aside ({e.strerror or e}): close the programs that use it "
                    "(an editor, a terminal in it, OneDrive, an antivirus) and try again"
                ) from None
            undo.moved.append(d)
    for rel_path, data in plan.files.items():
        path = ROOT / rel_path
        if rel_path.split("/")[0] not in OWNED_DIRS:
            undo.save(path)  # pytemplate.toml
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ui.detail(f"  + {rel_path}")
    if os.name != "nt":
        for script in ("pyt", "pyt.ps1"):
            p = ROOT / script
            if p.exists():
                p.chmod(p.stat().st_mode | 0o111)


def _record(plan: InitPlan, undo: _Undo) -> None:
    """The project's own `applied` record (cmd_apply): what init wrote is what `./pyt apply`
    finds applied. The record copied from the template, or from the project `new` ran in, must
    never stand for the new project: one named like it would trust it, and apply took the copied
    preset for the project's own."""
    from . import cmd_apply  # it imports this module

    state = cmd_apply.state_file(ROOT)
    undo.save(state)
    cmd_apply.save_record(cmd_apply.record_of(plan.cfg), state)


def init(cfg: Config, preset: str, name: str | None, *, force: bool) -> None:
    """Convert the project to `preset`: the internal step of `./pyt new`.

    Every check runs first (plan_init). Then pyproject.toml and uv.lock change (uv: the only
    step that needs the network), then src/, tests/, typings/ and pytemplate.toml are swapped,
    and the project's own `applied` record is written (_record). When a step fails, every file
    is put back as it was and the error is raised.
    """
    from . import render

    plan = plan_init(cfg, preset, name, force=force)
    ui.step(f"preset {preset} ({plan.description}) as '{plan.name}'")
    undo = _Undo()
    try:
        _swap_dependencies(plan, undo)
        _swap_files(plan, undo)
        _record(plan, undo)
    except BaseException as e:
        left = undo.rollback()
        if left:
            ui.error(f"init failed and could not put back: {', '.join(left)}")
        else:
            ui.info("init failed: every file is back as it was")
        if isinstance(e, OSError):  # a full disk, permissions, a name the file system refuses
            where = f" {rel(e.filename)}" if isinstance(e.filename, str) and e.filename else ""
            raise PytError(f"init could not write{where}: {e.strerror or e}") from None
        raise
    undo.discard()
    render.apply(plan.cfg, force=True)
    ui.ok(f"preset '{preset}' done")  # `new` says what comes next, from the right folder


# --- new -----------------------------------------------------------------------------------------

# Never copied by `new`: history, builds, caches and the marker of the template repository itself
SKIP_ANYWHERE = frozenset(
    {".git", ".build", "dist", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".hypothesis", ".flet", "template-repo"}
)
# PyInstaller/Flet leftovers, Claude Code state (settings, agent worktrees), and the page and
# license of the program this is: a project made with `new` is another program (TEMPLATE_DOCS)
SKIP_AT_ROOT = frozenset({"build", ".claude", "README.md", "LICENSE"})
# Where a project keeps the template repository's README (the manual of ./pyt, of the
# version it was made from) and LICENSE (the notice the MIT license asks for, for the copied
# runner). A project copies them on like any tracked file when it runs `new` itself.
TEMPLATE_DOCS = {"README.md": ".pytemplate/README.md", "LICENSE": ".pytemplate/LICENSE"}
TEMPLATE_URL = "https://github.com/omardev29/py_template"


def _skipped(rel_path: str) -> bool:
    """Whether `new` leaves this path (relative to ROOT, '/'-separated) out of the copy."""
    parts = rel_path.split("/")
    if any(p in SKIP_ANYWHERE or p.startswith(".venv") for p in parts) or parts[0] in SKIP_AT_ROOT:
        return True
    if rel_path == INSTALL_RECORD:  # the record of `pyt install` in its copy of the template
        return True
    # CI of the template repository itself (template-*.yml), not of the new project
    return len(parts) >= 3 and parts[:2] == [".github", "workflows"] and parts[2].startswith("template-")


def _ignore(directory: str, names: list[str]) -> set[str]:
    here = Path(directory).relative_to(ROOT).as_posix()
    prefix = "" if here == "." else here + "/"
    return {n for n in names if _skipped(prefix + n)}


def _git_env() -> dict[str, str]:
    """proc.base_env() without what points git at another repository (a hook's variables)."""
    env = proc.base_env()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"):
        env.pop(key, None)
    return env


_GIT_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _git_path(line: str) -> str:
    """A path as git prints it with core.quotePath (a name holding a control character, a quote,
    a backslash or a byte above 0x7f is written in double quotes with C escapes, octal for the
    bytes) as the file system names it: os.fsdecode keeps the bytes of a name that is not UTF-8
    (a Latin-1 `caf\\351.txt` from an old archive), which text decoding turned into U+FFFD."""
    if not (len(line) >= 2 and line.startswith('"') and line.endswith('"')):
        return line
    raw = bytearray()
    i = 1
    while i < len(line) - 1:
        if line[i] != "\\":
            raw += line[i].encode("utf-8")
            i += 1
        elif line[i + 1] in _GIT_ESCAPES:
            raw.append(_GIT_ESCAPES[line[i + 1]])
            i += 2
        else:
            raw.append(int(line[i + 1 : i + 4], 8))
            i += 4
    return os.fsdecode(bytes(raw))


# What git said when the last _git_files call found a repository in ROOT that git refuses to
# read (dubious ownership: a clone that another user owns; a broken repository), and the line
# of git's message that says how to let it ("" for none): the callers name them (new warns, as
# its copy then takes every file; install refuses). Both "" otherwise.
_git_refused = ""
_git_refused_fix = ""


def _git_files(*args: str) -> list[str] | None:
    """`git ls-files ARGS` in ROOT (paths relative to it, _git_path); None without git, a work
    tree, or when git refuses the repository (_git_refused then says why)."""
    global _git_refused, _git_refused_fix
    _git_refused = _git_refused_fix = ""
    git = shutil.which("git")
    if git is None:
        return None
    env = {**_git_env(), "LC_ALL": "C"}  # git's messages in English: "not a git repository"
    # One quoted ASCII line per path, whatever core.quotePath says: the output is read as text
    argv = [git, "-c", "core.quotePath=true", "ls-files", *args]
    r = proc.run(argv, cwd=ROOT, env=env, capture=True, check=False, echo=False)
    if r.returncode != 0:
        reason = (r.stderr or r.stdout or "").strip()
        if "not a git repository" not in reason:
            _git_refused = reason.splitlines()[0] if reason else f"exit code {r.returncode}"
            _git_refused_fix = next((ln.strip() for ln in reason.splitlines() if "safe.directory" in ln and ln.strip().startswith("git ")), "")
        return None
    return [_git_path(line) for line in r.stdout.split("\n") if line]


def git_refusal_fix() -> str:
    """How to let git read the repository in ROOT, after it refused it (_git_refused): install
    said "not a git work tree ... use a git clone" for a clone that another user owns."""
    if "dubious ownership" in _git_refused:
        return "run pyt as the folder's owner, or let git read it: " + (_git_refused_fix or f"git config --global --add safe.directory {ROOT}")
    return "repair the repository, or use a fresh git clone"


def _installed() -> bool:
    """Whether `new` copies the installed template (global mode: project.GLOBAL)."""
    from . import project  # the name `project` is a local variable elsewhere in this module

    return project.GLOBAL


def source_name() -> str:
    """What `new` copies, for its messages."""
    return f"the installed template ({ROOT})" if _installed() else "this template"


def _tracked_template() -> tuple[list[str] | None, str]:
    """The files copy_template copies: git's tracked files, or None for every file (ignored ones
    included), with how it picks them (the real run and the dry run of `new` say it alike).
    The installed template (global mode) is a clean copy of a template's tracked files made by
    `pyt install`, with no .git: every file, without asking a git (the folder may even lie in
    a repository of the user's, such as a home folder kept in git)."""
    global _git_refused, _git_refused_fix
    _git_refused = _git_refused_fix = ""  # what an earlier call left (a stand-in _git_files sets nothing)
    if _installed():
        return None, "every file"
    tracked = _git_files("--cached")
    if tracked is not None and ".pytemplate/pyt.py" in tracked:
        return tracked, "the files git tracks"
    if tracked is not None:
        return None, "every file, ignored ones included: git does not track this project's files (never committed?)"
    if _git_refused:
        return None, "every file, ignored ones included: git refuses to read this repository"
    return None, "every file, ignored ones included: " + ("git not found" if shutil.which("git") is None else "not a git work tree")


def _warn_refused(verb: str) -> None:
    """new's warning when git refused the repository: the copy takes every file, secrets (.env)
    and untracked ones included."""
    if _git_refused:
        ui.warn(f"git ls-files failed in {ROOT} ({_git_refused}): the copy {verb} files git does not track\n  {git_refusal_fix()}")


def copy_scope() -> str:
    """What copy_template would copy from here (`new --dry-run`)."""
    how = _tracked_template()[1]
    _warn_refused("would include")
    return how


def _copy_entry(src: Path, target: Path, rel_path: str, errors: list[tuple[str, str, str]]) -> None:
    """One entry of the template into the new project: a link as the link (_copy_link), a folder
    with what it holds (copytree: a folder of ROOT, or a submodule), a file with copy2. What
    fails goes to `errors` as (source, target, why), the tuples shutil.Error holds."""
    try:
        if src.is_symlink():
            _copy_link(src, target, rel_path)
        elif src.is_dir():
            shutil.copytree(src, target, symlinks=True, ignore=_ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target)
    except shutil.Error as e:
        found = e.args[0] if e.args else None
        if isinstance(found, list):
            errors.extend((str(t[0]), str(t[1]), str(t[2])) for t in found if isinstance(t, tuple) and len(t) == 3)
        else:
            errors.append((str(src), str(target), str(e)))
    except OSError as e:
        errors.append((str(src), str(target), str(e)))


def _raise_copy_errors(dest: Path, errors: list[tuple[str, str, str]]) -> None:
    """A copy that failed, as lines a person reads: shutil.Error printed its list of tuples."""
    if errors:
        lines = [re.sub(r"^\[(?:Errno|WinError) -?\d+\] ", "", why) for _src, _target, why in errors[:5]]
        more = f"\n  and {len(errors) - 5} more" if len(errors) > 5 else ""
        raise PytError(f"could not copy the template into {dest}:\n  " + "\n  ".join(lines) + more, 1)


def copy_template(dest: Path) -> None:
    """Copy the template to `dest`, without history, environments, builds, caches or the
    template repository's own files (_skipped).

    In a git work tree only what git tracks is copied (with its working-tree content):
    untracked and ignored files (.env secrets, .idea/, htmlcov/, *.spec...) stay behind, and the
    untracked ones are listed. Without git, or when git does not track the template (a copy
    inside another repository), every file but the _skipped ones is copied, and so from the
    installed template (global mode), without a word about it. A symbolic link is
    copied as a link, as git tracks it (a link to a folder is not the folder's content, and a
    dangling one is still a tracked file). A copy that fails is a PytError (exit 1) naming what
    failed.
    """
    if dest.exists() and any(dest.iterdir()):
        raise PytError(f"{dest} already exists and is not empty")
    tracked, how = _tracked_template()
    # `dest` itself only ever gets mkdir: its mode, owner and times stay those of the folder it
    # is (or of a new one). copytree(ROOT, dest) also copied ROOT's own mode and times onto it
    # (copystat): from the installed template, whose folder was 0700, every project folder became
    # 0700, an empty folder given to `new` lost its mode (a setgid 2775 became 700), and one the
    # user does not own refused the chmod, which failed `new` with a list of tuples.
    dest.mkdir(parents=True, exist_ok=True)
    errors: list[tuple[str, str, str]] = []
    if tracked is None:
        if _git_refused:
            _warn_refused("includes")
        elif not _installed():  # a clean copy by construction: nothing to say about it
            ui.info(f"  copying {how}")
        names = sorted(os.listdir(ROOT))
        left_out = _ignore(str(ROOT), names)
        for name in names:
            if name not in left_out:
                _copy_entry(ROOT / name, dest / name, name, errors)
        _raise_copy_errors(dest, errors)
        return
    deleted: list[str] = []
    for rel_path in tracked:
        if _skipped(rel_path):
            continue
        src = ROOT / rel_path
        if os.path.lexists(src):  # git tracks a link itself (mode 120000); a folder: a submodule
            (dest / rel_path).parent.mkdir(parents=True, exist_ok=True)
            _copy_entry(src, dest / rel_path, rel_path, errors)
        else:  # the working tree is what is copied: a tracked file deleted there stays out
            deleted.append(rel_path)
    _raise_copy_errors(dest, errors)
    untracked = [p for p in _git_files("--others", "--exclude-standard") or [] if not _skipped(p)]
    for what, paths in (("deleted in the working tree", deleted), ("not tracked by git", untracked)):
        if paths:
            more = f" and {len(paths) - 5} more" if len(paths) > 5 else ""
            ui.info(f"  not copied ({what}): {', '.join(paths[:5])}{more}")


def _copy_link(src: Path, target: Path, rel_path: str, command: str = "new") -> None:
    """A symbolic link copied as the link (its target text). Where no link can be made (Windows
    without the symlink privilege) what it points to is copied instead, with a warning naming
    `command` (`new`, or `install` for the installed template)."""
    try:
        target.symlink_to(os.readlink(src), target_is_directory=src.is_dir())
        return
    except OSError as e:
        reason = e.strerror or str(e)
    if src.is_dir():
        shutil.copytree(src, target, symlinks=True, ignore=_ignore, dirs_exist_ok=True)
    elif src.exists():
        shutil.copy2(src, target)
    else:
        ui.warn(f"{command}: could not copy the link {rel_path} ({reason}); it points nowhere: left out")
        return
    ui.warn(f"{command}: could not copy the link {rel_path} ({reason}): copied what it points to")


def _outermost_missing(path: Path) -> Path | None:
    """The outermost folder that creating `path` creates (None: it already exists)."""
    top: Path | None = None
    while not os.path.lexists(path) and path.parent != path:
        top, path = path, path.parent
    return top


def ignored_by_work_tree(git: str, folder: Path, cwd: Path) -> bool:
    """Whether the git work tree around `folder` ignores it (`git check-ignore <folder>/pyt`, as
    hooks asks): a home folder kept in git with `*` in its .gitignore, a monorepo that ignores its
    apps/. A project made there is never part of that repository, so it gets one of its own.
    `folder` need not exist yet (git matches the path); `cwd`, an existing folder inside that
    work tree, is where git runs (exit 0: ignored; 1: not; 128: no work tree, or an error)."""
    path = Path(os.path.relpath(folder / "pyt", cwd)).as_posix()
    r = proc.run([git, "check-ignore", "-q", "--", path], cwd=cwd, env=_git_env(), capture=True, check=False, echo=False)
    return r.returncode == 0


def _git_init(dest: Path) -> None:
    """A git repository on branch main (the branch the generated CI runs on), with pyt and
    pyt.ps1 executable. Inside a work tree (a monorepo) no repository; only where that one
    has core.filemode = false (Git for Windows) the two launchers are staged executable: a
    later `git add` would record them as 100644, and the pre-commit hook refuses that. A work
    tree that ignores the project (ignored_by_work_tree) does not count: the project got no
    repository at all there, and setup then said to `git init` it."""
    git = shutil.which("git")
    if git is None:
        ui.info("  git not found: the project is not a git repository (later: git init -b main)")
        return
    env = _git_env()
    inside = proc.run(
        [git, "rev-parse", "--is-inside-work-tree"], cwd=dest.parent, env=env, capture=True, check=False, echo=False
    )
    if inside.returncode == 0 and inside.stdout.strip() == "true" and not ignored_by_work_tree(git, dest, dest.parent):
        # --bool: git's own reading of the value (off, no and 0 are false too); every git has it
        filemode = proc.run([git, "config", "--bool", "--get", "core.filemode"], cwd=dest, env=env, capture=True, check=False, echo=False)
        if filemode.stdout.strip() == "false":
            proc.run([git, "add", "--chmod=+x", "--", "pyt", "pyt.ps1"], cwd=dest, env=env, capture=True, check=False)
        return
    if (dest / ".git").exists():
        return
    r = proc.run([git, "init", "--quiet", "-b", "main"], cwd=dest, env=env, capture=True, check=False)
    if r.returncode != 0:  # git < 2.28 has no -b: a plain init, then HEAD -> main
        r = proc.run([git, "init", "--quiet"], cwd=dest, env=env, capture=True, check=False)
        if r.returncode == 0:
            proc.run([git, "symbolic-ref", "HEAD", "refs/heads/main"], cwd=dest, env=env, check=False, echo=False)
    if r.returncode != 0:
        ui.warn(f"git init failed ({(r.stderr or r.stdout).strip()}): the project is not a git repository")
        return
    proc.run([git, "add", "--chmod=+x", "pyt", "pyt.ps1"], cwd=dest, env=env, check=False)


def project_readme(name: str, preset: str, *, manual: bool) -> str:
    """The README.md of a project made with `new` (the template's own is its .pytemplate/README.md)."""
    description = str(load(preset).get("description", "")).rstrip(".")
    where = (
        "The manual of `./pyt` and `pytemplate.toml` is `.pytemplate/README.md`: the py_template\n"
        "README of the version this project was made from."
        if manual
        else f"The manual of `./pyt` and `pytemplate.toml` is the py_template README: {TEMPLATE_URL}"
    )
    return (
        f"# {name}\n\n{description}. Made from [py_template]({TEMPLATE_URL}).\n\n"
        "## Getting started\n\n"
        "```sh\n"
        "./pyt setup    # interpreters, environments, uv.lock, git hook, editor configs\n"
        "./pyt run      # run the app with the active backend (pytemplate.toml)\n"
        "./pyt test     # pytest\n"
        "./pyt build    # package the app into dist/\n"
        "```\n\n"
        f"`./pyt help` lists every command. {where}\n"
    )


def _make_own(dest: Path, preset: str, name: str) -> None:
    """What makes the copy a project of its own, before `__init` runs in it: its README.md and
    [project] description, and (from the template repository) the template's README and LICENSE
    under .pytemplate/ (TEMPLATE_DOCS)."""
    if (TEMPLATE / "template-repo").is_file():
        for src_name, target in TEMPLATE_DOCS.items():
            if (ROOT / src_name).is_file():
                (dest / target).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / src_name, dest / target)
    manual = (dest / TEMPLATE_DOCS["README.md"]).is_file()
    (dest / "README.md").write_text(project_readme(name, preset, manual=manual), encoding="utf-8", newline="\n")
    pyproject = dest / PYPROJECT.name
    if pyproject.is_file():
        text = _read_text(pyproject, "pyproject.toml of the copy").replace("\r\n", "\n")
        pyproject.write_text(_set_description(text, str(load(preset).get("description", ""))), encoding="utf-8", newline="\n")


def _set_description(text: str, description: str) -> str:
    """`text` (a pyproject.toml) with [project] description = `description`, whatever form the
    old value has (a multi-line string too) and added when missing (config.set_value checks the
    result). Unchanged, with a warning, when the file has no [project] table or an unusual
    layout: the description is not worth failing `new` for (init checks the rest)."""
    from . import config

    try:
        if not isinstance(tomllib.loads(text).get("project"), dict):
            return text
    except TOML_ERRORS:
        return text  # init says what is wrong with it
    try:
        return config.set_value(text, "project", "description", description)
    except PytError:
        ui.warn("new: could not set [project] description in the copy's pyproject.toml: set it by hand")
        return text


def preset_python(preset: str) -> str:
    """python.cpython of a new project of `preset`: its skeleton's pytemplate.toml says it (the
    field's default without one). `new` makes sure uv has it (the new project's lock needs it)."""
    from . import config

    data = _read_toml(PRESETS / preset / "files" / "pytemplate.toml", f"preset {preset}: files/pytemplate.toml")
    table = data.get("python")
    version = table.get("cpython", config.PythonConfig.cpython) if isinstance(table, dict) else config.PythonConfig.cpython
    return str(version)


def check_destination(dest: Path, prefix: str = "") -> None:
    """Refuse, before anything is written, a destination `new` cannot use: one it may not look
    into (below a folder it may not enter, a name too long: Path.exists raised that
    PermissionError on Python 3.11-3.13, an internal-error traceback, and 3.14 read the folder as
    missing, so git's start in the folder above said "cannot run git"), something that is not a
    folder, a folder with content, one it cannot list (a traceback too), or a path below a file
    (the copy failed with a bare `[Errno 20] Not a directory`, and new said it had removed a
    project it never made). `prefix` starts each message."""
    try:
        info: os.stat_result | None = os.stat(dest)
    except (FileNotFoundError, NotADirectoryError):
        info = None  # missing, or below a file (the parents say which)
    except OSError as e:
        raise PytError(f"{prefix}cannot access {dest}: {e.strerror or e}") from None
    if info is None:
        if os.path.lexists(dest):  # a symbolic link whose target is gone
            raise PytError(f"{prefix}{dest} exists and is not a folder")
        parent = dest.parent
        while not os.path.lexists(parent) and parent.parent != parent:
            parent = parent.parent
        if not parent.is_dir():
            raise PytError(f"{prefix}{parent} is not a folder: {dest} cannot be made in it")
        return
    if not stat.S_ISDIR(info.st_mode):
        raise PytError(f"{prefix}{dest} exists and is not a folder")
    try:
        empty = next(iter(dest.iterdir()), None) is None
    except OSError as e:
        raise PytError(f"{prefix}cannot read the folder {dest}: {e.strerror or e}") from None
    if not empty:
        raise PytError(f"{prefix}{dest} already exists and is not empty")


def new(dest: Path, preset: str, name: str | None, python: Path | None = None) -> None:
    """`./pyt new`: copy the template to `dest` and run `init` in the copy, on `python` (the
    preset's python.cpython, which the caller made sure uv has: envs.ensure_python), else on the
    Python the launchers would pick there.

    When anything fails (a name uv refuses, no network, Ctrl+C...), what this call created is
    removed: the folder and the parents it had to create, or only its content when it existed
    (empty). A folder with content is refused before anything is written.
    """
    dest = dest.resolve()
    if dest == ROOT or ROOT in dest.parents:
        raise PytError(f"new: the destination folder cannot be inside {source_name()}")
    app_name = name or name_from_folder(dest.name)
    if not APP_NAME.fullmatch(app_name):
        raise PytError(f"'{app_name}' is not a valid app name: it may only contain {NAME_RULE}.\n  Choose one with --name NAME")
    load(preset)
    check_destination(dest)
    top = _outermost_missing(dest)
    ui.step(f"new project in {dest}")
    try:
        copy_template(dest)
        _make_own(dest, preset, app_name)
        loud = ["-q"] if ui.QUIET else ["-v"] if ui.VERBOSE else []  # the copy's runner, as quiet as this one
        # --no-render: init renders every generated file itself (force=True); render.auto first
        # warned about the source's hand-edited ones, which the new project never had
        init = ["--no-render", "__init", preset, "--name", app_name, "--force"]
        proc.run(proc.runner_argv(proc.find_uv(), dest, [*loud, *init], python=python), cwd=dest)
    except BaseException as e:
        if top is not None:
            made = os.path.lexists(top)
            left = [] if _remove(top) else [str(top)]
        else:
            children = list(dest.iterdir())
            made = bool(children)
            left = [str(child) for child in children if not _remove(child)]
        note = (
            "nothing was written"  # it said it had removed a project it never made
            if not made
            else f"the half-made project in {dest} was removed"
            if not left
            else f"could not delete everything the failed copy wrote ({', '.join(left[:3])}): delete {dest} by hand"
        )
        if not isinstance(e, Exception):  # Ctrl+C: cleaned up, stop as asked
            ui.error(note)
            raise
        what = f"{e.filename}: {e.strerror or e}" if isinstance(e, OSError) and e.filename else str(e)
        raise PytError(f"{what}\n  {note}", e.code if isinstance(e, PytError) else 1) from e
    _git_init(dest)
    ui.ok(f"project created in {dest}. Next:")
    for line in next_steps(dest):
        ui.info(f"  {line}")


def next_steps(dest: Path) -> list[str]:
    """The commands to type after `new`, each on its own line (cmd and Windows PowerShell 5.1
    have no `&&`): `cd` to the project, quoted for the calling shell, then the launcher that shell
    runs. The shell: PowerShell from the launcher (PYTEMPLATE_LAUNCHER ps1:), then xonsh and
    nushell, which export XONSH_VERSION and NU_VERSION to what they start (niubash's own launcher
    value sh:niubash wins over an inherited one), then cmd's syntax behind pyt.cmd (cmd itself, a
    Python subprocess, a VS Code task), else a POSIX shell's. xonsh and nushell on Windows start
    pyt.cmd too: `./pyt.cmd` there."""
    launcher = os.environ.get("PYTEMPLATE_LAUNCHER", "")
    path = str(dest)
    if launcher.startswith("ps1"):
        # PowerShell reads the typographic single quotes as quotes too: each is doubled. Its cd
        # (Set-Location -Path) reads [ ] * ? and ` as a wildcard pattern: -LiteralPath then
        literal = "-LiteralPath " if re.search(r"[\[\]*?`]", path) else ""
        return [f"cd {literal}'" + re.sub("['\u2018-\u201b]", lambda m: m.group() * 2, path) + "'", "./pyt setup"]
    cmd = launcher.startswith("cmd")
    niubash = launcher.startswith("sh:niubash")
    setup = "./pyt.cmd setup" if cmd else "./pyt setup"
    if not niubash and os.environ.get("XONSH_VERSION"):
        # xonsh reads a quoted argument as a Python string, but expands $NAME in it: @(...) is a
        # Python expression, passed as it is
        return [f"cd @({path!r})" if "$" in path else f"cd {path!r}", setup]
    if not niubash and os.environ.get("NU_VERSION"):
        # nushell: a single-quoted string is raw, a double-quoted one has the escapes \\ and \"
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return [f"cd '{path}'" if "'" not in path else f'cd "{escaped}"', setup]
    if cmd:
        return [f"cd /d {cmd_path(path)}", r".\pyt setup"]
    return [f"cd {shlex.quote(path)}", "./pyt setup"]


def cmd_path(path: str) -> str:
    """`path` as cmd reads it on a typed line: in double quotes (a Windows path never holds one),
    each `%` outside them as `^%`. cmd expands %NAME% of a typed line inside quotes too (the hint
    for a folder `a%OS%b` led to `aWindows_NTb`), but leaves one whose name is no variable: the
    name between two `%` then ends in `^` (or starts with a quote), and the caret, outside the
    quotes, is dropped after that expansion. cd strips the quotes."""
    return "^%".join(f'"{part}"' if part else "" for part in path.split("%"))
