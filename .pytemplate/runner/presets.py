"""Presets (script, raylib, flet): code skeleton + dependencies + initial config.

Each preset lives in .pytemplate/presets/<name>/:
  preset.toml      description, dependencies, extra [tool.uv] keys and extra pyproject tables
  files/           skeleton copied to the root. `__pkg__` in a path is replaced with the app
                   package, and `{{name}}`/`{{pkg}}` in text with the name and the package.
  constraints.txt  optional: the versions the template was tested with for every package the
                   preset adds to the template's own uv.lock (see `constraints`).

`./deploy new` copies the template (`copy_template`) and runs `init` in the copy: `init` is its
internal step (and how the template maintainer regenerates the template root).
"""

from __future__ import annotations

import keyword
import os
import re
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
from .config import APP_NAME, BACKENDS, NAME_RULE
from .project import BUILD, PRESETS, PYPROJECT, ROOT, TEMPLATE, rel
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
LOCK = ROOT / "uv.lock"
CONSTRAINTS = "constraints.txt"

# src/<pkg>/ may not be one of the project's own folders (the preset's src/ entries, such as
# src/main.py, are checked too: see check_name_free)
RESERVED_PACKAGES = {
    "tests": "tests/ (a package too: the imports would clash)",
    "typings": "typings/ (.ruff.toml excludes it: ruff would skip the app)",
    "build": "build/ (.gitignore excludes it at any depth: the app would never reach git)",
    "dist": "dist/ (.gitignore excludes it at any depth: the app would never reach git)",
    "assets": "src/assets/ (the data folder bundled with the app: app.assets)",
}
# Windows reserves these names (any case, any extension) for devices: src/aux/ cannot be created
# there, and git cannot check out a repository that holds it
WINDOWS_DEVICES = frozenset({"con", "prn", "aux", "nul", *(f"{d}{i}" for d in ("com", "lpt") for i in range(10))})


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
    that cannot be read, is not TOML or holds an unknown key or a wrong type is a DeployError
    naming it: render (the managed [tool.uv] block) and new read it on every run."""
    path = PRESETS / name / "preset.toml"
    if not path.is_file():
        raise DeployError(f"unknown preset '{name}' (available: {', '.join(available())})")
    where = rel(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as e:
        raise DeployError(f"{where} is not UTF-8 text (byte {e.start}): save it as UTF-8") from None
    except OSError as e:
        raise DeployError(f"cannot read {where}: {e.strerror or e}") from None
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"{where} is not valid TOML: {e}") from None
    for key, value in data.items():
        kind = PRESET_KEYS.get(key)
        if kind is None:
            raise DeployError(f"{where}: unknown key '{key}' (known: {', '.join(PRESET_KEYS)})")
        if not _holds(value, kind):
            raise DeployError(f"{where}: '{key}' must be {kind}")
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


# --- names -------------------------------------------------------------------------------------


def name_from_folder(folder: str) -> str:
    """The app name `./deploy new DIR` derives from the folder name: accents dropped (NFKD ->
    ASCII, so an accented e becomes e), every run of other characters -> '-' (no '--'), and no
    '-' or '_' at either end (uv refuses a name that does not end with a letter or digit)."""
    ascii_name = unicodedata.normalize("NFKD", folder).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9_-]+", "-", ascii_name)).strip("-_")


def _norm_name(req: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else req


def _read_text(path: Path, what: str) -> str:
    """A text file that uv reads too, such as pyproject.toml: a BOM is dropped, as uv does."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise DeployError(f"{what} not found: {path}") from None
    except UnicodeDecodeError as e:
        raise DeployError(f"{what} is not UTF-8 text (byte {e.start}): save it as UTF-8") from None
    except OSError as e:
        raise DeployError(f"cannot read {what}: {e.strerror or e}") from None


def _read_toml(path: Path, what: str) -> dict[str, Any]:
    try:
        return tomllib.loads(_read_text(path, what))
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"{what} is not valid TOML: {e}") from None


def read_pyproject() -> dict[str, Any]:
    """pyproject.toml, parsed (a BOM is fine); a DeployError that names the problem otherwise."""
    return _read_toml(PYPROJECT, "pyproject.toml")


def _names(items: Any) -> set[str]:
    return {_norm_name(r) for r in items if isinstance(r, str)} if isinstance(items, list) else set()


def _declared(group: str | None) -> set[str]:
    """The requirement names of [project] dependencies (group None) or of a dependency group."""
    data = _read_toml(PYPROJECT, "pyproject.toml")
    table = data.get("project") if group is None else data.get("dependency-groups")
    if not isinstance(table, dict):
        return set()
    return _names(table.get("dependencies" if group is None else group))


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
    only the ones they need (dependencies of dependencies included, markers ignored: uv refuses
    a project that depends on itself on every platform)."""
    graph = _lock_graph(lock)
    return set(graph) if roots is None else _closure(graph, roots)


def constraints_path(preset: str) -> Path:
    return PRESETS / preset / CONSTRAINTS


def constraints(preset: str) -> dict[str, str]:
    """{normalized name: version} from PRESETS/<preset>/constraints.txt ({} without the file).

    The file pins, one `name==version` per line, every package a project of this preset locks
    that the template's own uv.lock does not (the preset's dependencies and what they pull in),
    at the versions the template was tested with. `init` hands the ones the project does not
    lock yet to `uv add --constraints`: a one-off, nothing is written into pyproject.toml, and
    `./deploy lock --upgrade` moves on later. How to regenerate it: CLAUDE.md section 11.
    """
    path = constraints_path(preset)
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise DeployError(f"{rel(path)} is not UTF-8 text (byte {e.start}): regenerate it (CLAUDE.md section 11)") from None
    except OSError as e:
        raise DeployError(f"cannot read {rel(path)}: {e.strerror or e}") from None
    pins: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([A-Za-z0-9][A-Za-z0-9._+!-]*)", line)
        if m is None:
            raise DeployError(f"{rel(path)}:{number}: expected name==version, found {line!r}")
        pins[_norm_name(m.group(1))] = m.group(2)
    return pins


def constraints_text(preset: str, project_lock: Path, template_lock: Path | None = None) -> str:
    """The constraints.txt of `preset`, made from the uv.lock of a project just created with it
    (without constraints): every package it locks that the template's uv.lock does not. A
    package locked at two versions (a fork by platform) cannot be pinned and is left out."""
    base = locked_names(lock=template_lock)
    versions: dict[str, set[str]] = {}
    for entry in _lock_entries(project_lock):
        name = _norm_name(entry["name"])
        if not _is_project(entry) and name not in base:
            versions.setdefault(name, set()).add(str(entry.get("version", "")))
    lines = [
        f"# Versions the template was tested with for the packages the {preset} preset adds to the",
        "# template's uv.lock. `./deploy new` hands them to `uv add --constraints` (a one-off:",
        "# pyproject.toml keeps plain bounds). Regenerate it, never edit it: CLAUDE.md section 11.",
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
    dependencies, and what they pull in (from uv.lock and the preset's constraints)."""
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
        # The preset brings packages uv.lock does not have: only their names are known, not what
        # they need, which may be any package already locked (flet-cli -> rich -> mdurl)
        names |= set(graph)
    return names


def check_name_free(cfg: Config | None, preset: str, name: str) -> None:
    """Reject an app name that would break the project: one uv refuses (APP_NAME), a package
    src/<pkg>/ that is a Python keyword, a standard library module (of any supported Python), a
    backend name, a Windows device name, one of the project's own folders or files, or a
    package the project depends on, directly or not
    (uv refuses a project that depends on itself, and src/<pkg>/ would shadow the library).
    new, init, their dry runs and rename (which keeps the first line) call it."""
    pkg = name.replace("-", "_").lower()
    hint = "\n  Choose another name with --name NAME"
    if not APP_NAME.fullmatch(name):
        raise DeployError(f"'{name}' is not a valid app name: it may only contain {NAME_RULE}.{hint}")
    if keyword.iskeyword(pkg):
        raise DeployError(f"the package '{pkg}' would be a Python keyword (`import {pkg}` is a syntax error).{hint}")
    if shadows_stdlib(pkg):  # every Python the project can run on, not only the runner's
        raise DeployError(f"src/{pkg}/ would shadow the standard library module '{pkg}'.{hint}")
    if pkg in BACKENDS:  # src/mypyc/ would shadow mypy's compiler; `./deploy run pypy` reads a backend
        raise DeployError(f"'{name}' is the name of a backend ({', '.join(BACKENDS)}).{hint}")
    if pkg in WINDOWS_DEVICES:
        raise DeployError(
            f"src/{pkg}/ cannot exist on Windows: '{pkg}' is a reserved device name there (CON, PRN, "
            f"AUX, NUL, COM0-9, LPT0-9) and a repository holding it cannot be checked out.{hint}"
        )
    taken = {**_skeleton_src_names(preset), **RESERVED_PACKAGES}
    if pkg in taken:
        raise DeployError(f"src/{pkg}/ would collide with the project's own {taken[pkg]}.{hint}")
    clash = _norm_name(name)
    if clash in _dependency_names(cfg, preset):
        raise DeployError(
            f"the app name '{name}' is also the name of a dependency of the '{preset}' preset "
            f"({clash}, direct or indirect): uv would refuse the project and src/{pkg}/ would "
            f"shadow the library.{hint}"
        )


# --- pyproject ---------------------------------------------------------------------------------


# A [project] header, any table header (the end of [project]), and a `name = "..."` line: a
# trailing \r is fine (rename passes CRLF text)
_PROJECT_HEADER = re.compile(r"[ \t]*\[[ \t]*project[ \t]*\][ \t\r]*(?:#.*)?")
_TABLE_HEADER = re.compile(r"[ \t]*\[\[?[^\[\]\n]+\]\]?[ \t\r]*(?:#.*)?")
def _string_key(key: str) -> re.Pattern[str]:
    """A `key = "..."` (or '...') line, the key bare or quoted; group 1 is everything before the value."""
    k = re.escape(key)
    return re.compile(rf"""([ \t]*(?:{k}|"{k}"|'{k}')[ \t]*=[ \t]*)(?:"(?:[^"\\\n]|\\.)*"|'[^'\n]*')""")


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
    """The lines of the preset markers (None: no preset block); DeployError when they are damaged."""
    begins = [i for i, ln in enumerate(lines) if ln.strip() == EXTRA_BEGIN]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == EXTRA_END]
    if not begins and not ends:
        return None
    if len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0]:
        return begins[0], ends[0]
    order = " (the closing one comes first)" if len(begins) == len(ends) == 1 else ""
    raise DeployError(
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


# --- names and option-driven dependencies (./deploy apply, ./deploy rename) ------------------------

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
    }
)


def shadows_stdlib(pkg: str) -> bool:
    """Whether src/<pkg>/ would shadow a standard-library module on some supported Python."""
    return pkg in sys.stdlib_module_names or pkg in STDLIB_OTHER_VERSIONS


def set_project_name(text: str, name: str) -> str:
    """`text` (a pyproject.toml) with [project] name = `name` (_set_project_name). DeployError when
    the result does not say so, so no caller reports a change it did not make (apply, rename)."""
    new = _set_project_name(text, name)
    if project_name(new) != name:
        raise DeployError(f'could not set [project] name = "{name}" in pyproject.toml: edit that line by hand and try again')
    return new


def project_name(text: str) -> str | None:
    """Return [project] name of a pyproject.toml text (None: missing, not a string or not TOML)."""
    try:
        value = tomllib.loads(text.lstrip("\ufeff")).get("project", {}).get("name")
    except (tomllib.TOMLDecodeError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def default_options(preset: str) -> dict[str, Any]:
    """Return the [options] defaults of a preset (what `init` applies)."""
    return dict(load(preset).get("options", {}))


def option_dependencies(preset: str, opts: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return the preset requirements that use an option ({version}, {package}), formatted with
    `opts`: (dependencies, dev group). pytemplate.toml [preset.<name>] keeps them in sync; the
    plain ones (rich, types-cffi) belong to the project after `init` (./deploy add/remove)."""
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
                raise DeployError(f"preset {preset}: cannot format {template!r} with [preset.{preset}] ({e!r})") from None
        out.append(reqs)
    return out[0], out[1]


def _contains(data: Any, part: Any) -> bool:
    """Whether every key of `part` is in `data` with the same value (tables compared deeply)."""
    if isinstance(part, dict):
        return isinstance(data, dict) and all(k in data and _contains(data[k], v) for k, v in part.items())
    return bool(data == part)


def pyproject_after_init(cfg: Config, preset: str, name: str) -> str:
    """pyproject.toml as `init` writes it for the new configuration `cfg`: the old preset's tables
    out, the project name and the managed parts in, then the new preset's tables. DeployError when
    the result would not be valid TOML (a table of the preset also defined outside the markers)
    or would not hold all of them; damaged markers are a DeployError too."""
    from . import render

    text = _read_text(PYPROJECT, "pyproject.toml").replace("\r\n", "\n")
    extra = extra_tables(preset, name)
    try:
        wanted = tomllib.loads(extra)
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"{rel(PRESETS / preset / 'preset.toml')}: the pyproject tables are not valid TOML ({e})") from None
    hint = f"fix the '{EXTRA_BEGIN}' / '{EXTRA_END}' and '# >>> pytemplate' markers of pyproject.toml and try again"
    try:
        managed = render.pyproject_expected(cfg, _set_extra_tables(_set_project_name(text, name), ""))
        text = _set_extra_tables(managed, extra)
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise DeployError(
            f"pyproject.toml would not be valid TOML with the tables of the '{preset}' preset ({e}).\n"
            f"  One of them is probably defined outside the markers: {hint}"
        ) from None
    project = data.get("project")
    if not isinstance(project, dict) or project.get("name") != name:
        raise DeployError(f"pyproject.toml: the [project] table has no name = \"...\" line to set to '{name}': add one")
    if not _contains(data, wanted) or render.pyproject_expected(cfg, text) != text:
        raise DeployError(f"pyproject.toml: the tables of the '{preset}' preset or the managed [tool.uv] block did not land: {hint}")
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


def _dropped(old: list[str], new: list[str], declared: set[str]) -> list[str]:
    """The old preset's requirements that init removes: not in the new preset, still declared."""
    keep = {_norm_name(r) for r in new}
    return sorted((r for r in old if _norm_name(r) not in keep and _norm_name(r) in declared), key=_norm_name)


def plan_init(cfg: Config, preset: str, name: str | None, *, force: bool) -> InitPlan:
    """Every check `init` makes, in memory: nothing is written (the --dry-run of init prints it)."""
    from . import config, render

    new_name = name or cfg.app.name
    check_name_free(cfg, preset, new_name)  # the format too
    target = load(preset)
    if not force and not pristine(cfg):
        raise DeployError(
            "src/, tests/ or typings/ have changes compared to the skeleton of the current preset "
            f"('{cfg.app.preset}'). init would replace them.\n  If you are sure: ./deploy __init {preset} --force"
        )
    files = skeleton(preset, new_name)
    where = f"preset {preset}: files/pytemplate.toml"
    if "pytemplate.toml" not in files:
        raise DeployError(f"{where} is missing")
    try:
        data = tomllib.loads(files["pytemplate.toml"].decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise DeployError(f"{where} is not valid TOML: {e}") from None
    new_cfg: Config = config._build(config.Config, data, "")
    config.validate(new_cfg)
    if (new_cfg.app.name, new_cfg.app.preset) != (new_name, preset):
        raise DeployError(f"{where}: [app] must say name = \"{{{{name}}}}\" and preset = \"{preset}\"")
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
        drop=_dropped(old_deps, new_deps, _declared(None)),
        drop_dev=_dropped(old_dev, new_dev, _declared("dev")),
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
                    path.write_bytes(data)
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
    PYPROJECT.write_text(plan.pyproject, encoding="utf-8", newline="\n")
    uv = proc.find_uv()
    env = envs.env_vars(envs.tool_env(plan.cfg))
    # --frozen: only pyproject.toml changes; the next resolution sees every removal at once
    if plan.drop:
        proc.run([uv, "remove", "--frozen", *(_norm_name(r) for r in plan.drop)], env=env)
    if plan.drop_dev:
        proc.run([uv, "remove", "--frozen", "--dev", *(_norm_name(r) for r in plan.drop_dev)], env=env)
    pins: list[str] = []
    if plan.pins:  # the versions the template was tested with, for this resolution only
        path = BUILD / "init" / CONSTRAINTS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{n}=={v}\n" for n, v in sorted(plan.pins.items())), encoding="utf-8", newline="\n")
        pins = ["--constraints", str(path)]
    if plan.add:
        proc.run([uv, "add", "--no-sync", *pins, *plan.add], env=env)
    if plan.add_dev:
        proc.run([uv, "add", "--no-sync", "--dev", *pins, *plan.add_dev], env=env)
    proc.run([uv, "lock"], env=env)


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
                raise DeployError(
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
        for script in ("deploy", "deploy.ps1"):
            p = ROOT / script
            if p.exists():
                p.chmod(p.stat().st_mode | 0o111)


def init(cfg: Config, preset: str, name: str | None, *, force: bool) -> None:
    """Convert the project to `preset`: the internal step of `./deploy new`.

    Every check runs first (plan_init). Then pyproject.toml and uv.lock change (uv: the only
    step that needs the network), then src/, tests/, typings/ and pytemplate.toml are swapped.
    When a step fails, every file is put back as it was and the error is raised.
    """
    from . import render

    plan = plan_init(cfg, preset, name, force=force)
    ui.step(f"preset {preset} ({plan.description}) as '{plan.name}'")
    undo = _Undo()
    try:
        _swap_dependencies(plan, undo)
        _swap_files(plan, undo)
    except BaseException as e:
        left = undo.rollback()
        if left:
            ui.error(f"init failed and could not put back: {', '.join(left)}")
        else:
            ui.info("init failed: every file is back as it was")
        if isinstance(e, OSError):  # a full disk, permissions, a name the file system refuses
            where = f" {rel(e.filename)}" if isinstance(e.filename, str) and e.filename else ""
            raise DeployError(f"init could not write{where}: {e.strerror or e}") from None
        raise
    undo.discard()
    render.apply(plan.cfg, force=True)
    ui.ok(f"preset '{preset}' done. Next step: ./deploy setup && ./deploy run")


# --- new -----------------------------------------------------------------------------------------

# Never copied by `new`: history, builds, caches and the marker of the template repository itself
SKIP_ANYWHERE = frozenset(
    {".git", ".build", "dist", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".flet", "template-repo"}
)
# PyInstaller/Flet leftovers, Claude Code state (settings, agent worktrees), and the page and
# license of the program this is: a project made with `new` is another program (TEMPLATE_DOCS)
SKIP_AT_ROOT = frozenset({"build", ".claude", "README.md", "LICENSE"})
# Where a project keeps the template repository's README (the manual of ./deploy, of the
# version it was made from) and LICENSE (the notice the MIT license asks for, for the copied
# runner). A project copies them on like any tracked file when it runs `new` itself.
TEMPLATE_DOCS = {"README.md": ".pytemplate/README.md", "LICENSE": ".pytemplate/LICENSE"}
TEMPLATE_URL = "https://github.com/omardev29/py_template"


def _skipped(rel_path: str) -> bool:
    """Whether `new` leaves this path (relative to ROOT, '/'-separated) out of the copy."""
    parts = rel_path.split("/")
    if any(p in SKIP_ANYWHERE or p.startswith(".venv") for p in parts) or parts[0] in SKIP_AT_ROOT:
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


def _git_files(*args: str) -> list[str] | None:
    """`git ls-files -z ARGS` in ROOT (paths relative to it); None without git or a work tree.
    Any other git failure (dubious ownership, a broken repository) is said out loud: the copy then
    takes every file, untracked ones included."""
    git = shutil.which("git")
    if git is None:
        return None
    env = {**_git_env(), "LC_ALL": "C"}  # git's messages in English: "not a git repository"
    r = proc.run([git, "ls-files", "-z", *args], cwd=ROOT, env=env, capture=True, check=False, echo=False)
    if r.returncode != 0:
        reason = (r.stderr or r.stdout or "").strip()
        if "not a git repository" not in reason:
            first = reason.splitlines()[0] if reason else f"exit code {r.returncode}"
            ui.warn(f"git ls-files failed in {ROOT} ({first}): the copy includes files git does not track")
        return None
    return [p for p in r.stdout.split("\0") if p]


def copy_template(dest: Path) -> None:
    """Copy the template to `dest`, without history, environments, builds, caches or the
    template repository's own files (_skipped).

    In a git work tree only what git tracks is copied (with its working-tree content):
    untracked and ignored files (.env secrets, .idea/, htmlcov/, *.spec...) stay behind, and the
    untracked ones are listed. Without git, or when git does not track the template (a copy
    inside another repository), every file but the _skipped ones is copied.
    """
    if dest.exists() and any(dest.iterdir()):
        raise DeployError(f"{dest} already exists and is not empty")
    tracked = _git_files("--cached")
    if tracked is None or ".pytemplate/deploy.py" not in tracked:
        if tracked is not None:
            ui.info("  git does not track this project's files (never committed?): copying every file")
        shutil.copytree(ROOT, dest, ignore=_ignore, dirs_exist_ok=True)
        return
    dest.mkdir(parents=True, exist_ok=True)
    for rel_path in tracked:
        if _skipped(rel_path):
            continue
        src = ROOT / rel_path
        if src.is_dir():  # a submodule
            shutil.copytree(src, dest / rel_path, ignore=_ignore, dirs_exist_ok=True)
        elif src.exists():  # a tracked file deleted in the working tree is not copied
            (dest / rel_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel_path)
    untracked = [p for p in _git_files("--others", "--exclude-standard") or [] if not _skipped(p)]
    if untracked:
        more = f" and {len(untracked) - 5} more" if len(untracked) > 5 else ""
        ui.info(f"  not copied (not tracked by git): {', '.join(untracked[:5])}{more}")


def _outermost_missing(path: Path) -> Path | None:
    """The outermost folder that creating `path` creates (None: it already exists)."""
    top: Path | None = None
    while not os.path.lexists(path) and path.parent != path:
        top, path = path, path.parent
    return top


def _git_init(dest: Path) -> None:
    """A git repository on branch main (the branch the generated CI runs on), with deploy and
    deploy.ps1 executable. Nothing when dest is already inside a work tree (a monorepo)."""
    git = shutil.which("git")
    if git is None:
        ui.info("  git not found: the project is not a git repository (later: git init -b main)")
        return
    env = _git_env()
    inside = proc.run(
        [git, "rev-parse", "--is-inside-work-tree"], cwd=dest.parent, env=env, capture=True, check=False, echo=False
    )
    if (inside.returncode == 0 and inside.stdout.strip() == "true") or (dest / ".git").exists():
        return
    r = proc.run([git, "init", "--quiet", "-b", "main"], cwd=dest, env=env, capture=True, check=False)
    if r.returncode != 0:  # git < 2.28 has no -b: a plain init, then HEAD -> main
        r = proc.run([git, "init", "--quiet"], cwd=dest, env=env, capture=True, check=False)
        if r.returncode == 0:
            proc.run([git, "symbolic-ref", "HEAD", "refs/heads/main"], cwd=dest, env=env, check=False, echo=False)
    if r.returncode != 0:
        ui.warn(f"git init failed ({(r.stderr or r.stdout).strip()}): the project is not a git repository")
        return
    proc.run([git, "add", "--chmod=+x", "deploy", "deploy.ps1"], cwd=dest, env=env, check=False)


def project_readme(name: str, preset: str, *, manual: bool) -> str:
    """The README.md of a project made with `new` (the template's own is its .pytemplate/README.md)."""
    description = str(load(preset).get("description", "")).rstrip(".")
    where = (
        "The manual of `./deploy` and `pytemplate.toml` is `.pytemplate/README.md`: the py_template\n"
        "README of the version this project was made from."
        if manual
        else f"The manual of `./deploy` and `pytemplate.toml` is the py_template README: {TEMPLATE_URL}"
    )
    return (
        f"# {name}\n\n{description}. Made from [py_template]({TEMPLATE_URL}).\n\n"
        "## Getting started\n\n"
        "```sh\n"
        "./deploy setup    # interpreters, environments, uv.lock, git hook, editor configs\n"
        "./deploy run      # run the app with the active backend (pytemplate.toml)\n"
        "./deploy test     # pytest\n"
        "./deploy build    # package the app into dist/\n"
        "```\n\n"
        f"`./deploy help` lists every command. {where}\n"
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
        description = str(load(preset).get("description", ""))
        pyproject.write_text(_set_project_string(text, "description", description), encoding="utf-8", newline="\n")


def new(dest: Path, preset: str, name: str | None) -> None:
    """`./deploy new`: copy the template to `dest` and run `init` in the copy.

    When anything fails (a name uv refuses, no network, Ctrl+C...), what this call created is
    removed: the folder and the parents it had to create, or only its content when it existed
    (empty). A folder with content is refused before anything is written.
    """
    dest = dest.resolve()
    if dest == ROOT or ROOT in dest.parents:
        raise DeployError("new: the destination folder cannot be inside this template")
    app_name = name or name_from_folder(dest.name)
    if not APP_NAME.fullmatch(app_name):
        raise DeployError(f"'{app_name}' is not a valid app name: it may only contain {NAME_RULE}.\n  Choose one with --name NAME")
    load(preset)
    if os.path.lexists(dest) and not dest.is_dir():
        raise DeployError(f"{dest} exists and is not a folder")
    if dest.is_dir() and any(dest.iterdir()):
        raise DeployError(f"{dest} already exists and is not empty")
    top = _outermost_missing(dest)
    ui.step(f"new project in {dest}")
    try:
        copy_template(dest)
        _make_own(dest, preset, app_name)
        deploy_py = dest / ".pytemplate" / "deploy.py"
        proc.run([proc.find_uv(), "run", "--quiet", "--script", deploy_py, "__init", preset, "--name", app_name, "--force"], cwd=dest)
    except BaseException as e:
        if top is not None:
            left = [] if _remove(top) else [str(top)]
        else:
            left = [str(child) for child in dest.iterdir() if not _remove(child)]
        note = (
            f"the half-made project in {dest} was removed"
            if not left
            else f"could not delete everything the failed copy wrote ({', '.join(left[:3])}): delete {dest} by hand"
        )
        if not isinstance(e, Exception):  # Ctrl+C: cleaned up, stop as asked
            ui.error(note)
            raise
        raise DeployError(f"{e}\n  {note}", e.code if isinstance(e, DeployError) else 1) from e
    _git_init(dest)
    ui.ok(f"project created. cd {dest} && ./deploy setup")
