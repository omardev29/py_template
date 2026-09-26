"""apply [--force] and setup [--force]: bring the whole project in line with pytemplate.toml.

pytemplate.toml is the single source of truth. Most keys take effect on the next ./deploy
command (the generated files are re-rendered before each one), but some need work that only a
command can do: renaming src/<pkg>/, changing dependencies, re-locking, syncing environments,
installing or removing the git hook. `apply` does all of it, and only what is needed: a second
run changes nothing and runs no uv add/remove/lock. `setup` is the same operation under its
first-time name (a fresh clone): one implementation, `apply()`.

Steps (every check and refusal happens before the first write, in make_plan; --dry-run
prints the plan and stops):
  1. What the project really is ("applied"): the app name whose package is in src/, the preset,
     the option-driven requirements (flet==V, raylib's {package}=={version}) applied last time.
     The record `applied` in .pytemplate/state.json says what the last apply/rename wrote; it
     counts only when its name is app.name or pyproject.toml [project] name (a new project
     copies the template's record: ignored), and the project itself (pyproject.toml, src/)
     confirms or replaces it, so a missing or stale record is harmless.
  2. app.preset changed by hand: refused (exit 2). A preset decides src/, tests/, the
     dependencies and pyproject.toml: it cannot be switched in place (./deploy new DIR --preset P).
     The managed pyproject parts must be rewritable (render.check_pyproject).
  3. PyPy newly supported (tool.uv environments has no PyPy yet): the Python 3.11 precheck of
     `mode --supports +pypy` (it runs `uv run --locked`, so before anything changes the lock).
  4. app.name changed by hand: the rename flow (rename.plan/apply_plan) from the applied name,
     refused on a dirty git tree without --force (pytemplate.toml and the generated files do not
     count). When only pyproject.toml [project] name differs, only that line changes.
  5. [preset.<name>] options: `uv remove --frozen` / `uv add --frozen` of the option-driven
     requirements (dev group too; --frozen because flet-cli==V pins flet==V, so a resolving add
     of one group alone has no solution), then cmd_env.ensure_lock (managed pyproject parts and
     one `uv lock`). If they fail, pyproject.toml is restored: nothing half-applied.
  6. `uv sync --locked --all-groups` of every supported backend's environment, the exec bit of
     the launchers, the git hook (installed when hooks.pre_commit, pytemplate's own hook removed
     when false), render.apply, ruff tidy-up of renamed files, the record.
  7. A note for every unused .venv* (never deleted), warnings for references that do not exist
     (src/<pkg>/, compile.modules, app.assets, deploy.exe.icon, deploy.upx.path), a summary.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cmd_env, envs, hooks, presets, proc, render, rename, ui
from .cmd_dev import only_flags
from .config import Config
from .project import PYPROJECT, ROOT, STATE_FILE, rel
from .ui import DeployError

Check = Callable[[bool | None, str, str], None]

RECORD_KEY = "applied"  # top-level key of .pytemplate/state.json
_REQ = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)")
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


@dataclass
class Project:
    """What pyproject.toml says (read once)."""

    data: dict[str, Any]
    name: str | None  # [project] name
    deps: dict[str, str]  # normalized name -> requirement ([project] dependencies)
    dev: dict[str, str]  # the same for [dependency-groups] dev

    @property
    def pypy_locked(self) -> bool:
        """Whether uv.lock already resolves for PyPy (tool.uv environments, the managed block)."""
        tool = self.data.get("tool")
        uv = tool.get("uv") if isinstance(tool, dict) else None
        found = uv.get("environments", []) if isinstance(uv, dict) else []
        return any("implementation_name == 'pypy'" in e for e in found if isinstance(e, str)) if isinstance(found, list) else False


def _requirements(value: object) -> dict[str, str]:
    reqs = value if isinstance(value, list) else []
    return {req_key(r)[0]: r for r in reqs if isinstance(r, str)}


def read_project(path: Path | None = None) -> Project:
    """Parse pyproject.toml (a BOM is tolerated); DeployError when it is missing or not TOML."""
    path = path or ROOT / PYPROJECT.name
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise DeployError(f"pyproject.toml cannot be read: {e.strerror or e}") from None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise DeployError(f"pyproject.toml is not valid TOML: {e}") from None
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    groups = data.get("dependency-groups") if isinstance(data.get("dependency-groups"), dict) else {}
    name = project.get("name") if isinstance(project, dict) else None
    return Project(
        data=data,
        name=name if isinstance(name, str) else None,
        deps=_requirements(project.get("dependencies") if isinstance(project, dict) else None),
        dev=_requirements(groups.get("dev") if isinstance(groups, dict) else None),
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
            parts.append(f"add {', '.join(self.add + [f'{r} (dev)' for r in self.add_dev])}")
        return "; ".join(parts) or "none"


# --- the record of what was applied ------------------------------------------------------------------


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def load_record(path: Path | None = None) -> dict[str, Any] | None:
    """Return the `applied` record of .pytemplate/state.json (None: missing or malformed)."""
    path = path or _state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    record = data.get(RECORD_KEY) if isinstance(data, dict) else None
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
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get(RECORD_KEY) == record:
        return False
    if proc.DRY_RUN:
        return True
    data[RECORD_KEY] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return True


def record_of(cfg: Config) -> dict[str, Any]:
    """The record of a project that matches `cfg` (what apply writes at the end)."""
    deps, dev = presets.option_dependencies(cfg.app.preset, presets.options(cfg))
    return {"name": cfg.app.name, "preset": cfg.app.preset, "dependencies": deps, "dev": dev}


def rename_record(name: str, record: dict[str, Any] | None) -> None:
    """After `./deploy rename`: the record read before it keeps its preset and requirements
    (rename applies neither), with the new name. No record: nothing to update."""
    if record is not None and record["name"] != name:
        save_record({**record, "name": name})


def _state_file() -> Path:
    return ROOT / STATE_FILE.relative_to(STATE_FILE.parents[1])


# --- what the project really is ----------------------------------------------------------------------


@dataclass
class Applied:
    """The pytemplate.toml the project was last brought in line with."""

    preset: str
    dependencies: list[str]  # option-driven requirements applied last time
    dev: list[str]
    renamed_from: str | None  # app.name changed by hand: the name whose package is still in src/
    record: dict[str, Any] | None  # the record read from state.json, if any


def _src() -> Path:
    return ROOT / "src"


def _old_name(cfg: Config, candidates: list[str | None]) -> str | None:
    """The app name to rename from after a hand edit of app.name: the first candidate (the
    record, pyproject.toml [project] name) whose package is in src/, when app.name's own package
    is not (or is the very same folder: MyApp for myapp, or my-app for my_app)."""
    here = rename.package_dir(_src(), rename.package_of(cfg.app.name))
    for old in candidates:
        if not old or old == cfg.app.name or not _APP_NAME.fullmatch(old):
            continue
        folder = rename.package_dir(_src(), rename.package_of(old))
        if folder is not None and (here is None or rename.same_file(folder, here)):
            return old
    return None


def trusted_record(cfg: Config, project_name: str | None) -> dict[str, Any] | None:
    """The `applied` record, when it describes this project: its name is app.name or pyproject.toml
    [project] name (a hand edit changes only one of them). Anything else is foreign, e.g. the
    template's own record in a project that `./deploy new` just made: ignored."""
    record = load_record()
    if record is None or record["name"] not in (cfg.app.name, project_name):
        return None
    return record


def _project_name() -> str | None:
    try:
        return read_project().name
    except DeployError:
        return None


def project_record(cfg: Config) -> dict[str, Any] | None:
    """trusted_record(), reading pyproject.toml [project] name itself (rename: before it moves)."""
    return trusted_record(cfg, _project_name())


def applied_name(cfg: Config) -> str | None:
    """The name the project really has when app.name was changed by hand (None: no such case)."""
    project_name = _project_name()
    record = trusted_record(cfg, project_name)
    return _old_name(cfg, [record["name"] if record else None, project_name])


def _option_names(preset: str, opts: dict[str, Any]) -> set[str]:
    deps, dev = presets.option_dependencies(preset, opts)
    return {req_key(r)[0] for r in (*deps, *dev)}


def _applied_preset(cfg: Config, project: Project, record: dict[str, Any] | None) -> str:
    """The preset the project was made with: the one whose option-driven dependencies pyproject
    declares (flet, raylib), else the record's, else app.preset (a preset without such
    dependencies, like script, leaves no other trace)."""
    available = presets.available()
    declared = set(project.deps) | set(project.dev)
    recorded = record["preset"] if record and record["preset"] in available else None

    def present(preset: str) -> bool:
        names = _option_names(preset, presets.default_options(preset))
        if preset == cfg.app.preset:
            names |= _option_names(preset, presets.options(cfg))
        if record is not None and preset == recorded:
            names |= {req_key(r)[0] for r in (*record["dependencies"], *record["dev"])}
        return bool(names & declared)

    signed = [p for p in available if _option_names(p, presets.default_options(p))]
    if cfg.app.preset in signed and present(cfg.app.preset):
        return cfg.app.preset
    others = [p for p in signed if p != cfg.app.preset and present(p)]
    if others:
        return recorded if recorded in others else others[0]
    if cfg.app.preset not in signed or recorded == cfg.app.preset:
        return cfg.app.preset  # script, or its dependencies were removed by hand (apply adds them back)
    unsigned = [p for p in available if p not in signed]
    if recorded in unsigned:
        return str(recorded)
    return unsigned[0] if len(unsigned) == 1 else cfg.app.preset


def applied_state(cfg: Config, project: Project) -> Applied:
    record = trusted_record(cfg, project.name)
    preset = _applied_preset(cfg, project, record)
    if record is not None and record["preset"] == preset:
        deps, dev = list(record["dependencies"]), list(record["dev"])
    else:  # no record, or a foreign one: what `init` applies (the preset.toml defaults)
        deps, dev = presets.option_dependencies(preset, presets.default_options(preset))
    renamed_from = _old_name(cfg, [record["name"] if record else None, project.name])
    return Applied(preset, deps, dev, renamed_from, record)


def dependency_changes(cfg: Config, applied: Applied, project: Project) -> DepChanges:
    """Compare [preset.<name>] with what pyproject.toml declares (by name and version, never
    verbatim: uv writes raylib_sdl as raylib-sdl). A requirement the options no longer produce is
    removed only when the last applied options produced it."""
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
        for requirement in want:
            name, spec = req_key(requirement)
            if name not in declared or req_key(declared[name])[1] != spec:
                add.append(requirement)
    return changes


def preset_message(cfg: Config, applied: str) -> str:
    return (
        f"app.preset was changed from '{applied}' to '{cfg.app.preset}' by hand: a project cannot switch presets in place\n"
        f"  (the preset decides src/, tests/, the dependencies and pyproject.toml). Put back app.preset = \"{applied}\" in\n"
        f"  pytemplate.toml; to use the {cfg.app.preset} preset, create a new project and move your code there:\n"
        f"    ./deploy new DIR --preset {cfg.app.preset}"
    )


def missing_package(cfg: Config) -> tuple[str, str] | None:
    """(problem, hint) when src/<pkg>/ does not exist and no package of an older name was found."""
    src = _src()
    if rename.package_dir(src, cfg.pkg) is not None:
        return None
    here = sorted(p.name for p in src.iterdir() if p.is_dir() and (p / "__init__.py").is_file()) if src.is_dir() else []
    found = f"; src/ has {', '.join(p + '/' for p in here)}" if here else ""
    return (
        f"src/{cfg.pkg}/ does not exist (app.name = '{cfg.app.name}'{found}): run, test and build need the app package",
        "if app.name was changed by hand, put the old name back and run ./deploy rename NEW_NAME",
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
        raise DeployError(f"pyproject.toml cannot be read: {e.strerror or e}") from None


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
        raise DeployError(preset_message(cfg, applied.preset))
    render.check_pyproject(cfg)  # broken markers, a managed key outside them...: before any change
    plan = Plan(cfg, project, applied, dependency_changes(cfg, applied, project))
    plan.pypy_new = cfg.pypy_enabled and not project.pypy_locked
    if applied.renamed_from is not None:
        rename.check_new_name(cfg, cfg.app.name, who="app.name", retry="another app.name in pytemplate.toml, then ./deploy apply")
        plan.generated = render.outputs(cfg)
        plan.rename_plan = rename.plan(ROOT, applied.renamed_from, cfg.app.name, generated=plan.generated)
        plan.new_cfg = rename.validate_config(plan.rename_plan.config.new)
    elif project.name != cfg.app.name and rename.package_dir(_src(), cfg.pkg) is not None:
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
    message = rename.dirty_tree_message(changes, "renaming the app", f"./deploy {command} --force")
    if message:
        if not proc.DRY_RUN:
            raise DeployError(message)
        ui.warn(message)


# --- hook, references ------------------------------------------------------------------------------


OURS = ("installed", "outdated")  # hooks.classify: pytemplate's hook of this project


def _repo() -> hooks.Repo | str | None:
    """The git repository of the project: None outside git, git's refusal (dubious ownership, a
    broken .git...) as one line."""
    try:
        return hooks.find_repo(ROOT)
    except hooks.NotInGit:
        return None
    except DeployError as e:
        return " ".join(line.strip() for line in str(e).splitlines())


def _hook_state(cfg: Config) -> str | None:
    """classify() of the pre-commit hook in the default hooks folder (None: not in git, or git
    refuses the repository)."""
    repo = _repo()
    return hooks.classify(repo.default_dir / hooks.HOOK, repo) if isinstance(repo, hooks.Repo) else None


# What is in the hooks folder when apply leaves it alone (summary and --dry-run)
_LEFT_ALONE = {
    "foreign": "another tool's hook: left alone (./deploy hooks install --force chains both)",
    "calls": "a hook that runs ./deploy hooks run: left alone",
    "other": "another project's hook of this repository: left alone (./deploy hooks install --force runs both)",
}


def _apply_hook(cfg: Config) -> str:
    """Install the hook when hooks.pre_commit (hooks.ensure_installed: never fails), remove
    pytemplate's own when false (another tool's is never touched). Return the summary line."""
    repo = _repo()
    if repo is None:
        return "not a git work tree: nothing to do"
    if isinstance(repo, str):
        ui.warn(f"git pre-commit hook not checked: {repo}")
        return "not checked: git refuses the repository (see above)"
    target = repo.default_dir / hooks.HOOK
    before = hooks.classify(target, repo)
    if cfg.hooks.pre_commit:
        hooks.ensure_installed(cfg, ROOT)
    elif before in OURS:
        try:
            ui.ok(hooks.uninstall(repo))
        except OSError as e:
            ui.warn(f"could not remove the git pre-commit hook: {e} (./deploy hooks uninstall)")
            return "not removed (see above)"
    after = hooks.classify(target, repo)
    if after == "installed" and before != "installed":
        return "updated" if before == "outdated" else "installed"
    if before in OURS and after not in OURS:
        return "removed (hooks.pre_commit = false)"
    if after in _LEFT_ALONE:
        return _LEFT_ALONE[after]
    if repo.custom_hooks_path and cfg.hooks.pre_commit:
        return "core.hooksPath is set: nothing installed (./deploy hooks status says what to add)"
    if after == "missing":
        return "not installed (hooks.pre_commit = false)" if not cfg.hooks.pre_commit else "not installed (see above)"
    return "already installed" if after == "installed" else "unchanged"


def _hook_plan(cfg: Config) -> str:
    """--dry-run: what _apply_hook would do."""
    repo = _repo()
    if repo is None:
        return "not a git work tree: nothing to do"
    if isinstance(repo, str):
        return f"not checked: git refuses the repository ({repo})"
    state = hooks.classify(repo.default_dir / hooks.HOOK, repo)
    if not cfg.hooks.pre_commit:
        if state in OURS:
            return "would remove pytemplate's pre-commit hook (hooks.pre_commit = false)"
        return _LEFT_ALONE.get(state, "not installed (hooks.pre_commit = false)")
    if repo.custom_hooks_path:
        return "core.hooksPath is set: nothing installed (./deploy hooks status says what to add)"
    if state in _LEFT_ALONE:
        return _LEFT_ALONE[state]
    if state == "installed":
        return "installed"
    if state == "missing" and repo.ignored():
        return "not installed: the enclosing git repository ignores this project (./deploy hooks status)"
    return "would update the pre-commit hook" if state == "outdated" else "would install the pre-commit hook"


def _module_exists(src: Path, module: str) -> bool:
    base = module.replace(".", "/")
    return (src / base).is_dir() or (src / f"{base}.py").is_file()


def reference_problems(cfg: Config, *, package: bool = True) -> list[str]:
    """Paths pytemplate.toml refers to that do not exist (each makes some command fail later)."""
    src = _src()
    out: list[str] = []
    missing = missing_package(cfg) if package else None
    if missing is not None:
        out.append(f"{missing[0]}\n  {missing[1]}")
    if cfg.supports("mypyc"):
        modules = [m for m in cfg.compile.modules if not _module_exists(src, m)]
        if modules:
            out.append(f"compile.modules: {', '.join(modules)} not found in src/ (mypyc builds and `test mypyc` will fail)")
    if cfg.app.assets and not (src / cfg.app.assets).is_dir():
        out.append(f"app.assets = '{cfg.app.assets}': src/{cfg.app.assets}/ does not exist (nothing is packaged with the app)")
    if cfg.deploy.exe.icon and not (ROOT / cfg.deploy.exe.icon).is_file():
        out.append(f"deploy.exe.icon = '{cfg.deploy.exe.icon}' does not exist (relative to the project root): exe and nuitka builds fail")
    if cfg.deploy.upx.path:
        upx = Path(cfg.deploy.upx.path).expanduser()
        if not (upx if upx.is_absolute() else ROOT / upx).is_file():
            out.append(f"deploy.upx.path = '{cfg.deploy.upx.path}' does not exist: builds with UPX fail")
    return out


# --- doctor --------------------------------------------------------------------------------------


def pending(cfg: Config) -> list[tuple[str, str]]:
    """(problem, hint) for each pytemplate.toml change that is not applied yet (for doctor)."""
    try:
        project = read_project()
        applied = applied_state(cfg, project)
    except DeployError as e:
        return [(str(e).splitlines()[0], "fix it, then ./deploy apply")]
    if applied.preset != cfg.app.preset:
        return [
            (
                f"app.preset = '{cfg.app.preset}' but the project was made with the '{applied.preset}' preset",
                f"put back app.preset = \"{applied.preset}\" (a preset cannot change in place: ./deploy new DIR --preset {cfg.app.preset})",
            )
        ]
    out: list[tuple[str, str]] = []
    if applied.renamed_from is not None:
        old = applied.renamed_from
        out.append((f"app.name = '{cfg.app.name}' is not applied: the package is still src/{rename.package_of(old)}/", f"./deploy apply  (renames '{old}' -> '{cfg.app.name}')"))
    elif (missing := missing_package(cfg)) is not None:
        out.append(missing)
    elif project.name != cfg.app.name:
        out.append((f"pyproject.toml [project] name = '{project.name}', but app.name = '{cfg.app.name}'", "./deploy apply"))
    try:
        changes = dependency_changes(cfg, applied, project)
    except DeployError as e:
        changes = DepChanges()
        out.append((str(e).splitlines()[0], f"fix [preset.{cfg.app.preset}] in pytemplate.toml"))
    if changes:
        out.append((f"[preset.{cfg.app.preset}] is not applied to pyproject.toml ({changes.describe()})", "./deploy apply"))
    if not cfg.hooks.pre_commit and _hook_state(cfg) in ("installed", "outdated"):
        out.append(("hooks.pre_commit = false, but pytemplate's git pre-commit hook is installed", "./deploy apply  (or ./deploy hooks uninstall)"))
    return out


def doctor(cfg: Config, check: Check) -> None:
    """The ./deploy doctor lines about pytemplate.toml changes that are not applied yet."""
    problems = pending(cfg)
    if not problems:
        check(True, "pytemplate.toml applied (app.name, app.preset, [preset.*], hooks.pre_commit)", "")
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




def _print_plan(plan: Plan, command: str, force: bool) -> None:
    """--dry-run: what apply would do, without writing, syncing or locking anything."""
    cfg = plan.new_cfg or plan.cfg
    ui.step(f"{command} {_DRY}")
    _dirty(plan, command, force)
    if plan.pypy_new:
        cmd_mode_precheck(plan.cfg)  # read-only under --dry-run
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
    if parts:
        lock = "would re-lock (uv lock)"
    else:
        r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], check=False, capture=True, echo=False)
        lock = "up to date (uv lock --check)" if r.returncode == 0 else "would re-lock (uv lock)"
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
    for problem in reference_problems(cfg, package=plan.rename_plan is None):
        ui.warn(problem)


def cmd_mode_precheck(cfg: Config) -> None:
    """The Python 3.11 check of `mode --supports +pypy` (read-only under --dry-run)."""
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
            "./deploy clean --envs removes the .venv* environments (./deploy apply recreates the ones in use)"
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
    if plan.pypy_new:
        cmd_mode_precheck(cfg)  # before anything changes the lock (it runs `uv run --locked`)
    clean: rename.Tidy | None = None
    if plan.rename_plan is not None and plan.new_cfg is not None:
        n = plan.rename_plan.names
        rename.report(plan.rename_plan, dry=False)
        clean = rename.tidy_before(cfg, plan.rename_plan)
        rename.apply_plan(ROOT, plan.rename_plan)
        cfg = plan.new_cfg  # the renamed pytemplate.toml, validated before anything was written
        summary.append(("app.name", f"renamed '{n.old_name}' -> '{n.new_name}' (src/{cfg.pkg}/)"))
    elif plan.name_text is not None:
        (ROOT / PYPROJECT.name).write_text(plan.name_text, encoding="utf-8", newline="\n")
        summary.append(("app.name", f"pyproject.toml [project] name = \"{cfg.app.name}\""))

    pyproject_before = _read_bytes(ROOT / PYPROJECT.name)
    lock_before = _read_bytes(ROOT / "uv.lock")
    try:
        _edit_dependencies(cfg, plan.deps)
        cmd_env.ensure_lock(cfg)
    except BaseException as e:  # restore pyproject.toml: nothing half-applied (a failed uv lock leaves uv.lock alone)
        restored = pyproject_before is not None and _read_bytes(ROOT / PYPROJECT.name) != pyproject_before
        if restored and pyproject_before is not None:
            (ROOT / PYPROJECT.name).write_bytes(pyproject_before)
        if not isinstance(e, DeployError):
            raise
        notes = ["the app is already renamed" if plan.rename_plan is not None else "", "pyproject.toml was restored" if restored else ""]
        done = "; ".join(x for x in notes if x)
        raise DeployError(f"{e}\n  {done + ': ' if done else ''}fix the problem above and run ./deploy {command} again", e.code) from None
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
        ui.warn(f"not overwriting hand-edited generated files: {', '.join(edited)} (./deploy render --force)")
    if plan.rename_plan is not None:
        rename.tidy_after(cfg, plan.rename_plan, clean)
    save_record(record_of(cfg))
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
        ui.ok("done. Try: ./deploy run  |  ./deploy test  |  ./deploy doctor")
    else:
        ui.ok("pytemplate.toml applied")
    return 0


def cmd_apply(cfg: Config, args: list[str]) -> int:
    """apply [--force]: bring the project in line with pytemplate.toml (see the module docstring)."""
    return apply(cfg, args, command="apply")
