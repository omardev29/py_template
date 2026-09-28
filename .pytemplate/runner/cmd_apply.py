"""apply [--force] and setup [--force]: bring the whole project in line with pytemplate.toml.

pytemplate.toml is the single source of truth. Most keys take effect on the next ./pyt
command (the generated files are re-rendered before each one), but some need work that only a
command can do: renaming src/<pkg>/, changing dependencies, re-locking, syncing environments,
installing or removing the git hook. `apply` does all of it, and only what is needed: a second
run changes nothing and runs no uv add/remove/lock. `setup` is the same operation under its
first-time name (a fresh clone): one implementation, `apply()`.

Steps (every check and refusal happens before the first write, in make_plan; --dry-run
prints the plan and stops):
  1. What the project really is ("applied"): the app name whose package is in src/, the preset,
     the option-driven requirements (flet==V, raylib's {package}=={version}) applied last time.
     The record `applied` in .pytemplate/state.json says what the last apply, rename or
     `./pyt new` wrote (new writes the project's own, never the copied one); it counts only
     when its name is app.name or pyproject.toml [project] name, or its own package is in src/
     (both lines edited by hand: trusted_record). Without one (lost to a merge
     conflict) pyproject.toml stands in: the preset's traces, and the options the managed
     [tool.uv] block was last written with.
  2. app.preset changed by hand: refused (exit 2). A preset decides src/, tests/, the
     dependencies and pyproject.toml: it cannot be switched in place (./pyt new DIR --preset P).
     The managed pyproject parts must be rewritable (render.check_pyproject).
  3. app.name changed by hand: the rename flow (rename.plan/apply_plan) from the applied name,
     refused on a dirty git tree without --force (pytemplate.toml and the generated files do not
     count), then the record follows the new name. When only pyproject.toml [project] name
     differs, only that line changes; an app.name that names another package of src/ is refused.
  4. [preset.<name>] options: `uv remove --frozen` / `uv add --frozen` of the option-driven
     requirements (dev group too; --frozen because flet-cli==V pins flet==V, so a resolving add
     of one group alone has no solution), then cmd_env.ensure_lock (managed pyproject parts and
     one `uv lock`).
  5. PyPy newly supported (tool.uv environments had no PyPy): the PyPy precheck (pypy_minor) of
     `mode --supports +pypy`. It syncs the tools environment with this configuration, so it runs
     once pyproject.toml and uv.lock follow it. If 4 or 5 fail, pyproject.toml and uv.lock get
     their old bytes back: nothing half-applied. Then the record (the steps below can still fail).
  6. `uv sync --locked --all-groups` of every supported backend's environment, the exec bit of
     the launchers, the git hook (installed when hooks.pre_commit, pytemplate's own hook removed
     when false), render.apply, ruff tidy-up of renamed files.
  7. A note for every unused .venv* (never deleted), warnings for references that do not exist
     (src/<pkg>/, compile.modules, app.assets, deploy.exe.icon, deploy.upx.path), a summary.
"""

from __future__ import annotations

import json
import os
import re
import string
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cmd_env, envs, hooks, presets, proc, render, rename, ui
from .cmd_dev import only_flags
from .config import Config, import_path
from .project import PYPROJECT, ROOT, STATE_FILE, rel, write_whole
from .ui import PytError

Check = Callable[[bool | None, str, str], None]

RECORD_KEY = "applied"  # top-level key of .pytemplate/state.json
_REQ = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)")
# An OLD name (the record, pyproject.toml) may predate config.APP_NAME, which also wants a
# letter or digit at the end: it is still recognised, so apply can rename away from it
_APP_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_DRY = "(--dry-run: nothing is written)"


# --- requirements ----------------------------------------------------------------------------------


def req_key(requirement: str) -> tuple[str, str]:
    """(normalized name, version specifier without spaces) of a PEP 508 requirement.

    Extras and markers are ignored: `Flet == 1.0.1; sys_platform != 'x'` is ("flet", "==1.0.1").
    uv writes names normalized (raylib_sdl -> raylib-sdl), so names are compared that way.
    """
    m = _REQ.match(requirement)
    if m is None:
        return requirement.strip().lower(), ""
    return re.sub(r"[-_.]+", "-", m.group(1)).lower(), re.sub(r"\s+", "", m.group(2))


def req_marker(requirement: str) -> str:
    """The environment marker of a PEP 508 requirement ("" without one): `sys_platform != 'x'`."""
    m = _REQ.match(requirement)
    rest = requirement[m.end() :].strip() if m is not None else ""
    return rest[1:].strip() if rest.startswith(";") else ""


def _with_marker(requirement: str, marker: str) -> str:
    return f"{requirement}; {marker}" if marker else requirement


@dataclass
class Project:
    """What pyproject.toml says (read once)."""

    data: dict[str, Any]
    name: str | None  # [project] name
    deps: dict[str, str]  # normalized name -> requirement ([project] dependencies)
    dev: dict[str, str]  # the same for [dependency-groups] dev
    # [tool.uv] between the pytemplate markers (render.managed_values): the project's own keys
    # outside them never count as what the block was written with
    block: dict[str, Any] = field(default_factory=dict)

    @property
    def pypy_locked(self) -> bool:
        """Whether uv.lock already resolves for PyPy (tool.uv environments, the managed block)."""
        return render.resolves_pypy(self.data)


def _requirements(value: object) -> dict[str, str]:
    reqs = value if isinstance(value, list) else []
    return {req_key(r)[0]: r for r in reqs if isinstance(r, str)}


def read_project(path: Path | None = None) -> Project:
    """Parse pyproject.toml (a BOM is tolerated); PytError when it is missing or not TOML."""
    path = path or ROOT / PYPROJECT.name
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = tomllib.loads(text)
    except OSError as e:
        raise PytError(f"pyproject.toml cannot be read: {e.strerror or e}") from None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PytError(f"pyproject.toml is not valid TOML: {e}") from None
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    groups = data.get("dependency-groups") if isinstance(data.get("dependency-groups"), dict) else {}
    name = project.get("name") if isinstance(project, dict) else None
    return Project(
        data=data,
        name=name if isinstance(name, str) else None,
        deps=_requirements(project.get("dependencies") if isinstance(project, dict) else None),
        dev=_requirements(groups.get("dev") if isinstance(groups, dict) else None),
        block=render.managed_values(text),
    )


@dataclass
class DepChanges:
    """What `uv remove/add --frozen` must do so pyproject.toml matches [preset.<name>]."""

    remove: list[str] = field(default_factory=list)
    remove_dev: list[str] = field(default_factory=list)
    add: list[str] = field(default_factory=list)
    add_dev: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.remove or self.remove_dev or self.add or self.add_dev)

    def describe(self) -> str:
        parts = [f"remove {', '.join(self.remove + [f'{n} (dev)' for n in self.remove_dev])}"] if self.remove or self.remove_dev else []
        if self.add or self.add_dev:
            shown = [f'"{r}"' if ";" in r else r for r in self.add] + [f'"{r}" (dev)' if ";" in r else f"{r} (dev)" for r in self.add_dev]
            parts.append(f"add {', '.join(shown)}")
        return "; ".join(parts) or "none"


# --- the record of what was applied ------------------------------------------------------------------


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def load_record(path: Path | None = None) -> dict[str, Any] | None:
    """Return the `applied` record of .pytemplate/state.json (None: missing or malformed). Read
    like render reads the file: outside git's conflict markers the record survives a merge."""
    record = render._read_state(path or _state_file()).get(RECORD_KEY)
    if not isinstance(record, dict):
        return None
    name, preset = record.get("name"), record.get("preset")
    deps, dev = record.get("dependencies", []), record.get("dev", [])
    if not (isinstance(name, str) and isinstance(preset, str) and _str_list(deps) and _str_list(dev)):
        return None
    return {"name": name, "preset": preset, "dependencies": list(deps), "dev": list(dev)}


def save_record(record: dict[str, Any], path: Path | None = None) -> bool:
    """Store the record in .pytemplate/state.json, keeping every other key (the hashes of the
    generated files). Return whether it changed; nothing is written under --dry-run."""
    path = path or _state_file()
    data = render._read_state(path)  # a conflicted file: what both sides agree on, no hashes
    if data.get(RECORD_KEY) == record:
        return False
    if proc.DRY_RUN:
        return True
    data[RECORD_KEY] = record
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_whole(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError as e:  # read-only, owned by another user, a folder in the way...
        raise PytError(f"cannot write {rel(path)}: {e.strerror or e}") from None
    return True


def record_of(cfg: Config) -> dict[str, Any]:
    """The record of a project that matches `cfg` (what apply writes at the end)."""
    deps, dev = presets.option_dependencies(cfg.app.preset, presets.options(cfg))
    return {"name": cfg.app.name, "preset": cfg.app.preset, "dependencies": deps, "dev": dev}


def rename_record(name: str, record: dict[str, Any] | None) -> None:
    """After `./pyt rename`: the record read before it keeps its preset and requirements
    (rename applies neither), with the new name. No record: nothing to update."""
    if record is not None and record["name"] != name:
        save_record({**record, "name": name})


def _state_file() -> Path:
    return state_file(ROOT)


def state_file(root: Path) -> Path:
    """The state.json of the project at `root` (presets.init writes a new project's record there)."""
    return root / STATE_FILE.relative_to(STATE_FILE.parents[1])


# --- what the project really is ----------------------------------------------------------------------


@dataclass
class Applied:
    """The pytemplate.toml the project was last brought in line with."""

    preset: str
    dependencies: list[str]  # option-driven requirements applied last time
    dev: list[str]
    renamed_from: str | None  # app.name changed by hand: the name whose package is still in src/
    record: dict[str, Any] | None  # the record read from state.json, if any
    guessed: bool = False  # no record and no trace of any preset: `preset` is the traceless one


def _src() -> Path:
    return ROOT / "src"


def _old_name(cfg: Config, record_name: str | None, project_name: str | None = None) -> str | None:
    """The app name to rename from after a hand edit of app.name: the record's name, else
    pyproject.toml [project] name, whose package is in src/, when app.name's own package is not
    (or is the very same folder: MyApp for myapp, or my-app for my_app). A record named like
    app.name says app.name was not edited: only [project] name was (in case or -/_ at most, so
    its package is app.name's folder), and apply puts that line back: None. It took pyproject's
    name for the real one and planned a rename that rewrote the user's prose."""
    if record_name is not None and record_name == cfg.app.name:
        return None
    here = rename.package_dir(_src(), rename.package_of(cfg.app.name))
    for old in (record_name, project_name):
        if not old or old == cfg.app.name or not _APP_NAME.fullmatch(old):
            continue
        folder = rename.package_dir(_src(), rename.package_of(old))
        if folder is not None and (here is None or rename.same_file(folder, here)):
            return old
    return None


def _other_package(cfg: Config, record: dict[str, Any] | None, project_name: str | None) -> str | None:
    """The name the project really has when its package is in src/ next to app.name's own, as
    ANOTHER folder: app.name was set by hand to the name of another package of the project
    (src/helpers/), which is not the app. The record's name says what the app is; without a
    record, pyproject.toml [project] name does (then either line may be the edited one). A record
    named like app.name: app.name was not edited, only pyproject.toml was (apply puts it back)."""
    here = rename.package_dir(_src(), rename.package_of(cfg.app.name))
    old = record["name"] if record is not None else project_name
    if here is None or not old or old == cfg.app.name or not _APP_NAME.fullmatch(old):
        return None
    folder = rename.package_dir(_src(), rename.package_of(old))
    return old if folder is not None and not rename.same_file(folder, here) else None


def _pyproject_edited_too(cfg: Config) -> str:
    """Without a record, the pyproject.toml [project] name may be the line edited by hand."""
    return f"or, if pyproject.toml [project] name is the line edited by hand, put back name = \"{cfg.app.name}\" there"


def _onto_another_package(cfg: Config, old: str, record: dict[str, Any] | None) -> str:
    text = (
        f"app.name: src/{cfg.pkg}/ already exists and is not the app's package (the app is '{old}', in "
        f"src/{rename.package_of(old)}/): the app is not renamed onto another package.\n"
        f"  Put back app.name = \"{old}\" in pytemplate.toml, or move or delete src/{cfg.pkg}/ first, then ./pyt apply"
    )
    return text if record is not None else f"{text}\n  ({_pyproject_edited_too(cfg)})"


def trusted_record(cfg: Config, project_name: str | None) -> dict[str, Any] | None:
    """The `applied` record, when it describes this project: its name is app.name or pyproject.toml
    [project] name (a hand edit of one of them), or its own package is in src/: both lines were
    edited by hand, to a new name (apply skipped the rename, recorded the new name and lost the
    real one) or onto another package of src/ (src/helpers/: apply said "applied", recorded
    helpers and a later rename moved that package; _other_package refuses it). Anything else is
    foreign, e.g. the template's own record in a copy of it (no src/myapp/ there): ignored."""
    record = load_record()
    if record is None:
        return None
    name = record["name"]
    if name in (cfg.app.name, project_name):
        return record
    if _APP_NAME.fullmatch(name) and rename.package_dir(_src(), rename.package_of(name)) is not None:
        return record
    return None


def _project_name() -> str | None:
    try:
        return read_project().name
    except PytError:
        return None


def project_record(cfg: Config) -> dict[str, Any] | None:
    """trusted_record(), reading pyproject.toml [project] name itself (rename: before it moves)."""
    return trusted_record(cfg, _project_name())


def applied_name(cfg: Config) -> str | None:
    """The name the project really has when app.name was changed by hand (None: no such case)."""
    project_name = _project_name()
    record = trusted_record(cfg, project_name)
    return _old_name(cfg, record["name"] if record else None, project_name)


def _option_names(preset: str, opts: dict[str, Any]) -> set[str]:
    deps, dev = presets.option_dependencies(preset, opts)
    return {req_key(r)[0] for r in (*deps, *dev)}


def _table_paths(data: dict[str, Any], path: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """The tables of `data` that hold a value of their own ([tool.flet], not the bare [tool])."""
    out: set[tuple[str, ...]] = set()
    for key, value in data.items():
        if isinstance(value, dict):
            out |= _table_paths(value, (*path, key))
        elif path:
            out.add(path)
    return out


def _has_table(data: dict[str, Any], path: tuple[str, ...]) -> bool:
    node: Any = data
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return isinstance(node, dict)


def _marks(preset: str) -> set[tuple[str, ...]]:
    """What only `preset` writes into pyproject.toml besides its dependencies, and only when the
    project is made (`./pyt new`): its extra tables (flet's [tool.flet])."""
    try:
        extra = tomllib.loads(presets.extra_tables(preset, "x"))  # only the table paths count
    except tomllib.TOMLDecodeError:
        extra = {}
    return _table_paths(extra)


def _unformat(template: str, text: str) -> dict[str, str] | None:
    """The values of the {fields} of `template` that format it into `text` (the reverse of
    str.format), or None: no match, or a field that cannot be read back (a conversion, a format
    spec, a positional or dotted name)."""
    pattern: list[str] = []
    seen: set[str] = set()
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError:
        return None
    for literal, name, spec, conversion in parts:
        pattern.append(re.escape(literal))
        if name is None:
            continue
        if spec or conversion or not name.isidentifier():
            return None
        pattern.append(f"(?P={name})" if name in seen else f"(?P<{name}>.+?)")
        seen.add(name)
    m = re.fullmatch("".join(pattern), text, re.DOTALL)
    return None if m is None else {k: v for k, v in m.groupdict().items() if v is not None}


def _block_options(preset: str, project: Project) -> dict[str, str]:
    """The [preset.<name>] options the managed [tool.uv] block was last written with, read back
    from the values of the preset's own keys there (raylib's no-build-package = ["{package}"]: the
    package of the last apply, or lock). Only the block counts (Project.block, between the
    markers): the project's own list outside them (render._adopted) is no preset's, and a script
    project's no-build-package = ["six"] once read as raylib's package, a hand switch to refuse.
    Empty: the block holds no key of the preset (it follows app.preset), or a value its template
    does not give."""
    uv = project.block
    found: dict[str, str] = {}
    for key, template in presets.load(preset).get("uv", {}).items():
        value = uv.get(key)
        if isinstance(template, str) and isinstance(value, str):
            pairs = [(template, value)]
        elif isinstance(template, list) and isinstance(value, list) and len(template) == len(value):
            pairs = list(zip(template, value, strict=True))
        else:
            return {}
        for want, got in pairs:
            options = _unformat(want, got) if isinstance(want, str) and isinstance(got, str) else None
            if options is None or any(found.setdefault(k, v) != v for k, v in options.items()):
                return {}
    return found


def _traced(cfg: Config, project: Project) -> list[str]:
    """The presets whose traces pyproject.toml holds: an option-driven requirement (by name: any
    version; with the default options, app.preset's current ones, and those the managed block was
    last written with) or an extra table. The managed [tool.uv] keys themselves are no trace:
    render.managed_block writes them from app.preset, so `./pyt lock` after a hand edit of
    app.preset writes the NEW preset's keys."""
    declared = set(project.deps) | set(project.dev)

    def present(preset: str) -> bool:
        names = _option_names(preset, presets.default_options(preset))
        if preset == cfg.app.preset:
            names |= _option_names(preset, presets.options(cfg))
        block = _block_options(preset, project)
        if block:
            names |= _option_names(preset, {**presets.default_options(preset), **block})
        return bool(names & declared or any(_has_table(project.data, t) for t in _marks(preset)))

    return [p for p in presets.available() if present(p)]


def _infer_preset(cfg: Config, project: Project, record: dict[str, Any] | None) -> tuple[str, bool]:
    """(the preset the project was made with, whether that is a guess).

    The trusted record decides: the last apply, rename or `./pyt new` wrote it, so an app.preset
    that differs from it was changed by hand, whatever pyproject.toml holds (a script project may
    depend on raylib or flet). Without one (it was lost: a state.json merge conflict whose sides
    disagree on it, a deleted file), pyproject.toml's traces (_traced): app.preset when it shows
    them, else a preset that does (a hand switch), else, with no trace of any preset, app.preset
    when it leaves none (script), else the preset that leaves none (a guess: its dependencies may
    also have been replaced by hand)."""
    available = presets.available()
    if record is not None and record["preset"] in available:
        return str(record["preset"]), False
    traced = _traced(cfg, project)
    if cfg.app.preset in traced:
        return cfg.app.preset, False
    if traced:
        return traced[0], False
    traceless = [p for p in available if not (_option_names(p, presets.default_options(p)) or _marks(p))]
    if cfg.app.preset in traceless or len(traceless) != 1:
        return cfg.app.preset, False
    return traceless[0], True


def _applied_preset(cfg: Config, project: Project, record: dict[str, Any] | None) -> str:
    """The preset the project was made with (_infer_preset)."""
    return _infer_preset(cfg, project, record)[0]


def applied_state(cfg: Config, project: Project) -> Applied:
    record = trusted_record(cfg, project.name)
    preset, guessed = _infer_preset(cfg, project, record)
    if record is not None and record["preset"] == preset:
        deps, dev = list(record["dependencies"]), list(record["dev"])
    else:  # no record: the options the managed block was last written with, else init's defaults
        options = {**presets.default_options(preset), **_block_options(preset, project)}
        deps, dev = presets.option_dependencies(preset, options)
    renamed_from = _old_name(cfg, record["name"] if record else None, project.name)
    return Applied(preset, deps, dev, renamed_from, record, guessed)


def dependency_changes(cfg: Config, applied: Applied, project: Project) -> DepChanges:
    """Compare [preset.<name>] with what pyproject.toml declares (by name and version, never
    verbatim: uv writes raylib_sdl as raylib-sdl). A requirement the options no longer produce is
    removed only when the last applied options produced it.

    A declared requirement keeps its marker: `uv add --frozen` replaces a requirement only with
    one of the same marker (and keeps its extras), and appended a second, unmarked pin next to
    `flet-desktop==1.0.1; sys_platform != 'emscripten'` (no solution: the change could never be
    applied). A package switch carries the marker of the requirement the same preset entry
    produced before (`{package}=={version}`: raylib -> raylib_sdl)."""
    want_deps, want_dev = presets.option_dependencies(cfg.app.preset, presets.options(cfg))
    changes = DepChanges()
    groups = (
        (want_deps, applied.dependencies, project.deps, changes.remove, changes.add),
        (want_dev, applied.dev, project.dev, changes.remove_dev, changes.add_dev),
    )
    for want, old, declared, remove, add in groups:
        keep = {req_key(r)[0] for r in want}
        for requirement in old:
            name = req_key(requirement)[0]
            if name not in keep and name in declared and name not in remove:
                remove.append(name)
        for i, requirement in enumerate(want):
            name, spec = req_key(requirement)
            if name in declared:
                if req_key(declared[name])[1] != spec:
                    add.append(_with_marker(requirement, req_marker(declared[name])))
                continue
            before = req_key(old[i])[0] if len(old) == len(want) else None  # the same preset entry, last time
            add.append(_with_marker(requirement, req_marker(declared[before]) if before in declared else ""))
    return changes


def _restore_hint(cfg: Config) -> str:
    """How to keep app.preset when its requirements were replaced by hand (a guessed preset)."""
    try:
        deps, dev = presets.option_dependencies(cfg.app.preset, presets.options(cfg))
    except PytError:  # [preset.<name>] cannot format them: pending reports that on its own line
        deps, dev = [], []
    adds = [f"./pyt add {' '.join(deps)}"] if deps else []
    adds += [f"./pyt add --dev {' '.join(dev)}"] if dev else []
    restore = f"restore them ({' and '.join(adds)}) or " if adds else ""
    return f"{restore}set [preset.{cfg.app.preset}] in pytemplate.toml to the ones pyproject.toml declares"


def preset_message(cfg: Config, applied: str, *, guessed: bool = False) -> str:
    text = (
        f"app.preset was changed from '{applied}' to '{cfg.app.preset}' by hand: a project cannot switch presets in place\n"
        f"  (the preset decides src/, tests/, the dependencies and pyproject.toml). Put back app.preset = \"{applied}\" in\n"
        f"  pytemplate.toml; to use the {cfg.app.preset} preset, create a new project and move your code there:\n"
        f"    ./pyt new DIR --preset {cfg.app.preset}"
    )
    if guessed:
        text += (
            f"\n  ('{applied}' is a guess: there is no record of the last apply, and pyproject.toml holds no trace of the "
            f"{cfg.app.preset} preset.\n  If the project was made with {cfg.app.preset} and its requirements were "
            f"replaced by hand, {_restore_hint(cfg)} instead)"
        )
    return text


def missing_package(cfg: Config) -> tuple[str, str] | None:
    """(problem, hint) when src/<pkg>/ does not exist and no package of an older name was found."""
    src = _src()
    if rename.package_dir(src, cfg.pkg) is not None:
        return None
    try:  # os.path: an entry it cannot stat (a link into a folder the user may not enter) is no
        # package; Python 3.11's Path.is_dir raised for it, and doctor ended in an internal error
        entries = list(src.iterdir()) if os.path.isdir(src) else []
    except OSError:
        entries = []
    here = sorted(p.name for p in entries if os.path.isdir(p) and os.path.isfile(p / "__init__.py"))
    found = f"; src/ has {', '.join(p + '/' for p in here)}" if here else ""
    return (
        f"src/{cfg.pkg}/ does not exist (app.name = '{cfg.app.name}'{found}): run, test and build need the app package",
        "if app.name was changed by hand, put the old name back and run ./pyt rename NEW_NAME",
    )


def _project_name_text(cfg: Config, project: Project) -> str:
    """app.name and src/<pkg>/ agree, pyproject.toml [project] name does not: the new pyproject.toml
    text with that line fixed (and the name in the preset block, such as [tool.flet] product).
    Planned before anything is written: a [project] table it cannot edit is a refusal."""
    old = project.name
    if old is not None and _APP_NAME.fullmatch(old):
        edit = rename._plan_pyproject(ROOT, rename.Names(old, cfg.app.name))
        if edit is not None:
            return edit.new
    try:
        text = (ROOT / PYPROJECT.name).read_text(encoding="utf-8-sig")
        return presets.set_project_name(text, cfg.app.name)
    except OSError as e:
        raise PytError(f"pyproject.toml cannot be read: {e.strerror or e}") from None


# --- the plan ------------------------------------------------------------------------------------


@dataclass
class Plan:
    cfg: Config
    project: Project
    applied: Applied
    deps: DepChanges
    rename_plan: rename.Plan | None = None
    new_cfg: Config | None = None  # the configuration after the rename (compile.modules...)
    name_text: str | None = None  # only pyproject.toml [project] name differs: its new text
    pypy_new: bool = False
    generated: dict[str, str] = field(default_factory=dict)


def make_plan(cfg: Config) -> Plan:
    """Everything apply would do, computed without writing: every refusal happens here."""
    project = read_project()
    applied = applied_state(cfg, project)
    if applied.preset != cfg.app.preset:
        raise PytError(preset_message(cfg, applied.preset, guessed=applied.guessed))
    render.check_pyproject(cfg)  # broken markers, a managed key outside them...: before any change
    plan = Plan(cfg, project, applied, dependency_changes(cfg, applied, project))
    plan.pypy_new = cfg.pypy_enabled and not project.pypy_locked
    if applied.renamed_from is not None:
        rename.check_new_name(cfg, cfg.app.name, who="app.name", retry="another app.name in pytemplate.toml, then ./pyt apply")
        plan.generated = render.outputs(cfg)
        plan.rename_plan = rename.plan(ROOT, applied.renamed_from, cfg.app.name, generated=plan.generated)
        plan.new_cfg = rename.validate_config(plan.rename_plan.config.new)
    elif rename.package_dir(_src(), cfg.pkg) is not None:
        # app.name onto another package of src/, whatever [project] name says (both lines may have
        # been edited to it): `rename` refuses the same, src/<new>/ exists
        other = _other_package(cfg, applied.record, project.name)
        if other is not None:
            raise PytError(_onto_another_package(cfg, other, applied.record))
        if project.name != cfg.app.name:
            rename.check_new_name(cfg, cfg.app.name, who="app.name", retry="another app.name in pytemplate.toml, then ./pyt apply")
            plan.name_text = _project_name_text(cfg, project)
    return plan


def _dirty(plan: Plan, command: str, force: bool) -> None:
    """Refuse (or, under --dry-run, warn about) uncommitted changes before a rename."""
    if plan.rename_plan is None or force:
        return
    changes = rename.git_changes(ROOT)
    if isinstance(changes, list):
        ignore = rename.derived_paths(plan.generated or render.outputs(plan.cfg)) | {"pytemplate.toml"}
        changes = [p for p in changes if p not in ignore]
    message = rename.dirty_tree_message(changes, "renaming the app", f"./pyt {command} --force")
    if message:
        if not proc.DRY_RUN:
            raise PytError(message)
        ui.warn(message)


# --- hook, references ------------------------------------------------------------------------------


# hooks.hook_state: pytemplate's hook of this project ("chained": as the pre-commit.local that
# another project's hook runs first)
OURS = ("installed", "outdated", "chained")


def _repo() -> hooks.Repo | str | None:
    """The git repository of the project: None outside git, why the hook cannot be read as one
    line: git's refusal (dubious ownership, a broken .git...), or hooks.NO_GIT when git is not on
    PATH in a project that has a .git (in or above it: hooks.git_missing_here)."""
    try:
        return hooks.find_repo(ROOT)
    except hooks.NotInGit:
        return hooks.NO_GIT if hooks.git_missing_here(ROOT) else None
    except PytError as e:
        return " ".join(line.strip() for line in str(e).splitlines())


def _unchecked(repo: str) -> str:
    """Why the hook was not checked (a _repo() string), for the summary line."""
    return hooks.NO_GIT if repo == hooks.NO_GIT else "git refuses the repository"


def _hook_state(cfg: Config) -> str | None:
    """hooks.hook_state() of the default hooks folder (None: not in git, or git refuses the
    repository)."""
    repo = _repo()
    return hooks.hook_state(repo) if isinstance(repo, hooks.Repo) else None


# the other project's hook runs pre-commit.local (this project's copy) first (hooks.hook_script)
_CHAINED = "runs this project's checks from pre-commit.local, which another project's hook runs first"


def _left_alone(state: str, repo: hooks.Repo) -> str | None:
    """What is in the hooks folder when apply leaves it alone (summary and --dry-run)."""
    if state == "foreign":
        return f"another tool's hook: left alone ({hooks.chain_advice(repo)})"
    if state == "calls":
        return "a hook that runs ./pyt hooks run: left alone"
    if state == "other":
        return f"another project's hook of this repository: left alone ({hooks.chain_advice(repo)})"
    return None


def _hooks_path_summary(repo: hooks.Repo) -> str:
    """hooks.pre_commit with core.hooksPath set: nothing is installed; does that hook run the checks?"""
    runner = hooks.hooks_path_runner(repo)
    if runner is not None:
        return f"core.hooksPath is set: {runner} runs ./pyt hooks run"
    return "core.hooksPath is set: nothing installed (./pyt hooks status says what to add)"


def _apply_hook(cfg: Config) -> str:
    """Install the hook when hooks.pre_commit (hooks.ensure_installed: never fails), remove
    pytemplate's own when false (another tool's is never touched). Return the summary line."""
    repo = _repo()
    if repo is None:
        return "not a git work tree: nothing to do"
    if isinstance(repo, str):
        again = " (put it on PATH and run ./pyt apply again)" if repo == hooks.NO_GIT else ""
        ui.warn(f"git pre-commit hook not checked: {repo}{again}")
        return f"not checked: {_unchecked(repo)} (see above)"
    before, copy_before = hooks.hook_state(repo), hooks.own_local(repo)
    ours = before in OURS or (copy_before and before == "missing")  # what hooks.uninstall removes
    if cfg.hooks.pre_commit:
        hooks.ensure_installed(cfg, ROOT)
    elif ours:
        try:
            ui.ok(hooks.uninstall(repo))
        except OSError as e:
            ui.warn(f"could not remove the git pre-commit hook: {e} (./pyt hooks uninstall)")
            return "not removed (see above)"
    after, copy_after = hooks.hook_state(repo), hooks.own_local(repo)
    dropped = " (and removed pre-commit.local, a copy of this project's hook)" if copy_before and not copy_after else ""
    if after == "installed" and (before != "installed" or dropped):
        return ("updated" if before in ("outdated", "installed") else "installed") + dropped
    if ours and not cfg.hooks.pre_commit and after not in OURS and not copy_after:
        return "removed (hooks.pre_commit = false)"
    if repo.custom_hooks_path and cfg.hooks.pre_commit:
        return _hooks_path_summary(repo)
    if after == "chained":
        return _CHAINED
    left = _left_alone(after, repo)
    if left is not None:
        return left
    if after == "missing":
        return "not installed (hooks.pre_commit = false)" if not cfg.hooks.pre_commit else "not installed (see above)"
    return "already installed" if after == "installed" else "unchanged"


def _hook_plan(cfg: Config) -> str:
    """--dry-run: what _apply_hook would do."""
    repo = _repo()
    if repo is None:
        return "not a git work tree: nothing to do"
    if isinstance(repo, str):
        return f"not checked: {hooks.NO_GIT}" if repo == hooks.NO_GIT else f"not checked: git refuses the repository ({repo})"
    state, copy = hooks.hook_state(repo), hooks.own_local(repo)
    if not cfg.hooks.pre_commit:
        if state == "chained":
            return "would remove pre-commit.local, this project's checks that another project's hook runs first (hooks.pre_commit = false)"
        if state in OURS or (copy and state == "missing"):
            return "would remove pytemplate's pre-commit hook (hooks.pre_commit = false)"
        return _left_alone(state, repo) or "not installed (hooks.pre_commit = false)"
    if repo.custom_hooks_path:
        return _hooks_path_summary(repo)
    if state == "chained":
        return _CHAINED
    left = _left_alone(state, repo)
    if left is not None:
        return left
    if state == "installed":
        return "would remove pre-commit.local, a copy of this project's hook (the checks run twice)" if copy else "installed"
    if state == "missing" and not copy and repo.ignored():
        return "not installed: the enclosing git repository ignores this project (./pyt hooks status)"
    return "would update the pre-commit hook" if state == "outdated" else "would install the pre-commit hook"


def reference_problems(cfg: Config, *, package: bool = True, move: tuple[str, str] | None = None) -> list[str]:
    """Paths pytemplate.toml refers to that do not exist (each makes some command fail later).
    `move` (rename.Plan.move: src/<old>, src/<new>): a rename that is planned, not done (--dry-run):
    a path under src/<new>/ is looked for where it is now, under src/<old>/."""
    src = _src()

    def now(path: Path) -> Path:
        if move is None:
            return path
        try:
            return ROOT / move[0] / path.relative_to(ROOT / move[1])
        except ValueError:
            return path

    def module_exists(module: str) -> bool:  # what mypyc.compiled_sources finds a module in
        path = import_path(now(src / module.replace(".", "/")))
        return path.is_file() or render._holds_python(path)

    out: list[str] = []
    missing = missing_package(cfg) if package else None
    if missing is not None:
        out.append(f"{missing[0]}\n  {missing[1]}")
    if cfg.supports("mypyc"):
        modules = [m for m in cfg.compile.modules if not module_exists(m)]
        if modules:
            out.append(f"compile.modules: {', '.join(modules)} not found in src/ (mypyc builds and `test mypyc` will fail)")
    if cfg.app.assets and not now(src / cfg.app.assets).is_dir():
        out.append(f"app.assets = '{cfg.app.assets}': src/{cfg.app.assets}/ does not exist (nothing is packaged with the app)")
    if cfg.deploy.exe.icon and not now(ROOT / cfg.deploy.exe.icon).is_file():
        out.append(f"deploy.exe.icon = '{cfg.deploy.exe.icon}' does not exist (relative to the project root): exe and nuitka builds fail")
    if cfg.deploy.upx.path:
        try:
            upx: Path | None = Path(cfg.deploy.upx.path).expanduser()
        except RuntimeError:  # a ~user of another machine (a shared pytemplate.toml), or no home folder
            upx = None
        if upx is None or not (upx if upx.is_absolute() else now(ROOT / upx)).is_file():
            out.append(f"deploy.upx.path = '{cfg.deploy.upx.path}' does not exist: builds with UPX fail")
    return out


# --- doctor --------------------------------------------------------------------------------------


def pending(cfg: Config, *, hook: bool = True) -> list[tuple[str, str]]:
    """(problem, hint) for each pytemplate.toml change that is not applied yet (for doctor).
    `hook=False` skips the git hook (a git process): a quick check before every command."""
    try:
        project = read_project()
        applied = applied_state(cfg, project)
    except PytError as e:
        return [(str(e).splitlines()[0], "fix it, then ./pyt apply")]
    if applied.preset != cfg.app.preset:
        put_back = f"put back app.preset = \"{applied.preset}\" (a preset cannot change in place: ./pyt new DIR --preset {cfg.app.preset})"
        if applied.guessed:
            return [
                (
                    f"app.preset = '{cfg.app.preset}', but pyproject.toml holds no trace of that preset (and there is no record of the last apply)",
                    f"{put_back}; or, if its requirements were replaced by hand, {_restore_hint(cfg)}",
                )
            ]
        return [(f"app.preset = '{cfg.app.preset}' but the project was made with the '{applied.preset}' preset", put_back)]
    out: list[tuple[str, str]] = []
    if applied.renamed_from is not None:
        old = applied.renamed_from
        out.append((f"app.name = '{cfg.app.name}' is not applied: the package is still src/{rename.package_of(old)}/", f"./pyt apply  (renames '{old}' -> '{cfg.app.name}')"))
    elif (missing := missing_package(cfg)) is not None:
        out.append(missing)
    elif other := _other_package(cfg, applied.record, project.name):  # [project] name edited to it too, or not
        hint = f"put back app.name = \"{other}\" (or move src/{cfg.pkg}/ away, then ./pyt apply)"
        out.append(
            (
                f"app.name = '{cfg.app.name}' names src/{cfg.pkg}/, another package: the app is '{other}' (src/{rename.package_of(other)}/)",
                hint if applied.record is not None else f"{hint}; {_pyproject_edited_too(cfg)}",
            )
        )
    elif project.name != cfg.app.name:
        out.append((f"pyproject.toml [project] name = '{project.name}', but app.name = '{cfg.app.name}'", "./pyt apply"))
    try:
        changes = dependency_changes(cfg, applied, project)
    except PytError as e:
        changes = DepChanges()
        out.append((str(e).splitlines()[0], f"fix [preset.{cfg.app.preset}] in pytemplate.toml"))
    if changes:
        out.append((f"[preset.{cfg.app.preset}] is not applied to pyproject.toml ({changes.describe()})", "./pyt apply"))
    if hook and not cfg.hooks.pre_commit and _hook_state(cfg) in OURS:
        out.append(("hooks.pre_commit = false, but pytemplate's git pre-commit hook is installed", "./pyt apply  (or ./pyt hooks uninstall)"))
    return out


def doctor(cfg: Config, check: Check) -> None:
    """The ./pyt doctor lines about pytemplate.toml changes that are not applied yet."""
    problems = pending(cfg)
    if not problems:
        # hooks.pre_commit only where git could read the hook (the "git hook" line says why not)
        read = not isinstance(_repo(), str)
        check(True, f"pytemplate.toml applied (app.name, app.preset, [preset.*]{', hooks.pre_commit' if read else ''})", "")
    for label, hint in problems:
        check(False, label, hint)
    for problem in reference_problems(cfg, package=False):
        check(None, problem, "fix pytemplate.toml or add the file")


# --- the command ---------------------------------------------------------------------------------


def _edit_dependencies(cfg: Config, changes: DepChanges) -> None:
    """`uv remove/add --frozen`: they only edit pyproject.toml; ensure_lock then locks once."""
    tool = envs.tool_env(cfg)
    if changes.remove:
        envs.uv(tool, ["remove", "--frozen", *changes.remove])
    if changes.remove_dev:
        envs.uv(tool, ["remove", "--frozen", "--dev", *changes.remove_dev])
    if changes.add:
        envs.uv(tool, ["add", "--frozen", *changes.add])
    if changes.add_dev:
        envs.uv(tool, ["add", "--frozen", "--dev", *changes.add_dev])


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _restore(path: Path, before: bytes | None) -> bool:
    """Give `path` its old bytes back; return whether it had changed. None (it could not be read
    before): left as it is, never deleted (a re-lock makes a stale uv.lock right again)."""
    if before is None or _read_bytes(path) == before:
        return False
    try:
        write_whole(path, before)
    except OSError as e:
        ui.warn(f"could not restore {path.name}: {e.strerror or e}")
        return False
    return True


def _print_plan(plan: Plan, command: str, force: bool) -> None:
    """--dry-run: what apply would do, without writing, syncing or locking anything."""
    cfg = plan.new_cfg or plan.cfg
    ui.step(f"{command} {_DRY}")
    _dirty(plan, command, force)
    # uv.lock against the pyproject.toml of today (read-only; UV_PYTHON is this configuration's)
    fresh = envs.uv(envs.tool_env(cfg), ["lock", "--check"], check=False, capture=True, echo=False).returncode == 0
    if plan.pypy_new and fresh:
        cmd_mode_precheck(plan.cfg)  # read-only under --dry-run
    elif plan.pypy_new:  # its `uv run --locked` would fail on the lock alone: apply runs it after the re-lock
        ui.info(f"  (--dry-run) the Python {plan.cfg.pypy_minor} check (PyPy is new) is not run: uv.lock does not match yet; apply runs it after the re-lock")
    rows: list[tuple[str, str]] = []
    if plan.rename_plan is not None:
        n = plan.rename_plan.names
        rows.append(("app.name", f"would rename '{n.old_name}' -> '{n.new_name}' (details below)"))
    elif plan.name_text is not None:
        rows.append(("app.name", f"would set pyproject.toml [project] name = \"{cfg.app.name}\" (now '{plan.project.name}')"))
    else:
        rows.append(("app.name", f"'{cfg.app.name}' (src/{cfg.pkg}/): unchanged"))
    rows.append(("app.preset", f"{cfg.app.preset}: unchanged"))
    rows.append((f"[preset.{cfg.app.preset}]", f"would {plan.deps.describe()}" if plan.deps else "dependencies unchanged"))
    parts = [
        text
        for text, due in (
            ("the managed parts", render.pyproject_outdated(cfg)),
            ("[project] name", plan.name_text is not None or plan.rename_plan is not None),
            ("dependencies", bool(plan.deps)),
        )
        if due
    ]
    rows.append(("pyproject.toml", f"would rewrite {', '.join(parts)}" if parts else "unchanged"))
    # uv.lock holds the normalized project name: p -> P, or my_app -> My-App, changes no lock
    renamed = (plan.name_text is not None or plan.rename_plan is not None) and req_key(plan.project.name or "")[0] != req_key(cfg.app.name)[0]
    if render.pyproject_outdated(cfg) or plan.deps or renamed or not fresh:
        lock = "would re-lock (uv lock)"
    else:
        lock = "up to date (uv lock --check)"
    rows.append(("uv.lock", lock))
    rows.append(("environments", "would sync " + ", ".join(f"{rel(e.dir)} ({e.request})" for e in cmd_env._envs_for(cfg, "all"))))
    rows.append(("git hook", _hook_plan(cfg)))
    changed, edited = render.apply(cfg)  # --dry-run: compares only
    rows.append(("generated files", f"would update {', '.join(changed)}" if changed else "unchanged"))
    if edited:
        rows.append(("", f"hand-edited, left untouched: {', '.join(edited)}"))
    for label, text in rows:
        ui.info(f"  {label:<16} {text}")
    if plan.rename_plan is not None:
        rename.report(plan.rename_plan, dry=True)
    _leftover_note(cfg)
    move = plan.rename_plan.move if plan.rename_plan is not None else None
    for problem in reference_problems(cfg, package=plan.rename_plan is None, move=move):
        ui.warn(problem)


def cmd_mode_precheck(cfg: Config) -> None:
    """The PyPy precheck of `mode --supports +pypy`: the Python of python.pypy (3.11 by default;
    Config.pypy_minor). Read-only under --dry-run."""
    from .cmd_mode import _precheck_py311

    _precheck_py311(cfg)


def unused_envs(cfg: Config) -> list[Path]:
    """The .venv* environments of this side that the configuration does not use (.venv-pypy
    once PyPy left backend.supported, the .venv-jit of an older template...): never deleted."""
    used = {env.dir for env in cmd_env._envs_for(cfg, "all")}
    return [d for d in cmd_env._env_dirs() if d not in used and not any(rename.same_file(d, u) for u in used)]


def _leftover_note(cfg: Config) -> None:
    names = [rel(d) for d in unused_envs(cfg)]
    if names:
        ui.info(
            f"note: {', '.join(names)} {'is' if len(names) == 1 else 'are'} not used by this configuration: "
            "./pyt clean --envs removes the .venv* environments (./pyt apply recreates the ones in use)"
        )


def apply(cfg: Config, args: list[str], *, command: str = "apply") -> int:
    """The whole operation (`apply` and `setup`): see the module docstring."""
    force = "--force" in only_flags(command, args, ("--force",))
    plan = make_plan(cfg)
    if proc.DRY_RUN:
        _print_plan(plan, command, force)
        return 0

    ui.step(command)
    summary: list[tuple[str, str]] = []
    _dirty(plan, command, force)
    clean: rename.Tidy | None = None
    if plan.rename_plan is not None and plan.new_cfg is not None:
        n = plan.rename_plan.names
        rename.report(plan.rename_plan, dry=False)
        clean = rename.tidy_before(cfg, plan.rename_plan)
        rename.apply_plan(ROOT, plan.rename_plan)
        cfg = plan.new_cfg  # the renamed pytemplate.toml, validated before anything was written
        # the record follows the rename at once: a later failure must not leave it naming the old app
        save_record({"name": cfg.app.name, "preset": plan.applied.preset, "dependencies": plan.applied.dependencies, "dev": plan.applied.dev})
        summary.append(("app.name", f"renamed '{n.old_name}' -> '{n.new_name}' (src/{cfg.pkg}/)"))
    elif plan.name_text is not None:
        try:
            write_whole(ROOT / PYPROJECT.name, plan.name_text.encode("utf-8"))
        except OSError as e:
            raise PytError(f"cannot write pyproject.toml: {e.strerror or e}") from None
        summary.append(("app.name", f"pyproject.toml [project] name = \"{cfg.app.name}\""))
    try:
        return _finish(cfg, plan, command, summary, clean)
    except KeyboardInterrupt:  # Ctrl+C, or SIGTERM/SIGHUP passed on to uv (proc.Interrupted)
        if plan.rename_plan is not None:  # as `./pyt rename` says it: only "terminated" was printed
            ui.warn(f"the app is already renamed: run ./pyt {command} to finish (uv.lock, the environments and the generated files)")
        raise


def _finish(cfg: Config, plan: Plan, command: str, summary: list[tuple[str, str]], clean: rename.Tidy | None) -> int:
    """apply once the app is renamed (or its [project] name line fixed): the dependencies and the
    lock (put back when they fail), the record, the environments, the hook, the generated files,
    the notes and the summary."""
    pyproject_before = _read_bytes(ROOT / PYPROJECT.name)
    lock_before = _read_bytes(ROOT / "uv.lock")
    try:
        _edit_dependencies(cfg, plan.deps)
        # With PyPy new (plan.pypy_new) ensure_lock runs the PyPy precheck once the re-lock is
        # done: it syncs the tools environment (`uv sync --locked`) with THIS configuration (a
        # python.cpython change in the same edit made uv refuse the old lock); when it fails,
        # pyproject.toml and uv.lock get their old bytes back below.
        cmd_env.ensure_lock(cfg)
    except BaseException as e:  # restore pyproject.toml and uv.lock: nothing half-applied
        restored = [
            name
            for name, before in ((PYPROJECT.name, pyproject_before), ("uv.lock", lock_before))
            if _restore(ROOT / name, before)
        ]
        if not isinstance(e, PytError):
            raise
        notes = ["the app is already renamed" if plan.rename_plan is not None else "", f"{' and '.join(restored)} {'was' if len(restored) == 1 else 'were'} restored" if restored else ""]
        done = "; ".join(x for x in notes if x)
        raise PytError(f"{e}\n  {done + ': ' if done else ''}fix the problem above and run ./pyt {command} again", e.code) from None
    # pyproject.toml and uv.lock now follow [preset.*]: record it before the steps that can still
    # fail (a sync, the hook, render), or the next apply would not know what to remove
    save_record(record_of(cfg))
    if plan.deps:
        summary.append((f"[preset.{cfg.app.preset}]", plan.deps.describe()))
    if _read_bytes(ROOT / PYPROJECT.name) != pyproject_before and not plan.deps:
        summary.append(("pyproject.toml", "managed parts updated"))
    if _read_bytes(ROOT / "uv.lock") != lock_before:
        summary.append(("uv.lock", "re-locked"))

    synced: list[str] = []
    for env in cmd_env._envs_for(cfg, "all"):
        ui.step(f"environment {env.key}: {rel(env.dir)} ({env.request})")
        envs.sync(env)
        synced.append(rel(env.dir))
    summary.append(("environments", "synced " + ", ".join(synced)))
    cmd_env._fix_exec_bit()
    summary.append(("git hook", _apply_hook(cfg)))
    changed, edited = render.apply(cfg)
    if changed:
        ui.info(f"render: updated {', '.join(changed)}")
        summary.append(("generated files", f"updated {', '.join(changed)}"))
    if edited:
        ui.warn(f"not overwriting hand-edited generated files: {', '.join(edited)} (./pyt render --force)")
    if plan.rename_plan is not None:
        rename.tidy_after(cfg, plan.rename_plan, clean)
    _leftover_note(cfg)
    problems = reference_problems(cfg)
    for problem in problems:
        ui.warn(problem)

    ui.step("summary")
    for label, text in summary:
        ui.info(f"  {label:<16} {text}")
    if problems:
        ui.info(f"  {'warnings':<16} {len(problems)} (above)")
    if command == "setup":
        ui.ok("done. Try: ./pyt run  |  ./pyt test  |  ./pyt doctor")
    else:
        ui.ok("pytemplate.toml applied")
    return 0


def cmd_apply(cfg: Config, args: list[str]) -> int:
    """apply [--force]: bring the project in line with pytemplate.toml (see the module docstring)."""
    return apply(cfg, args, command="apply")
