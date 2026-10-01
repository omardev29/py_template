"""Mode and template commands: mode, render, new, and the internal init step (./pyt __init).

Under --dry-run each of them prints what it would do and writes nothing: no pytemplate.toml,
pyproject.toml, uv.lock or generated file, no environment synced, no project copied.
Read-only checks (the Python 3.11 precheck, `uv lock --check`) still run.
"""

from __future__ import annotations

import argparse
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from . import config, envs, presets, proc, render, ui
from .config import BACKENDS, Config
from .project import CONFIG_FILE, PYPROJECT, ROOT, code_dirs, native_path, rel, user_path, write_whole
from .ui import PytError

_DRY = "(--dry-run: nothing is written)"


def _describe(cfg: Config, title: str = "current mode") -> None:
    ui.step(title)
    ui.report(f"  app            {cfg.app.name}  (preset {cfg.app.preset}, package src/{cfg.pkg}/)")
    ui.report(f"  active backend {cfg.backend.active}")
    ui.report(f"  supported      {', '.join(cfg.backend.supported)}  (Python {cfg.min_python}+ syntax)")
    for b in cfg.backend.supported:
        ui.report(f"  {'typing ' + b:<14} {cfg.profile_for(b)}")
    ui.report(f"  editor         {cfg.typing.editor}")
    if cfg.supports("mypyc"):  # a project without it compiles nothing (the line said it did)
        ui.report(f"  mypyc compiles {', '.join(cfg.compile.modules)}")


def _parse(parser: argparse.ArgumentParser, args: list[str]) -> argparse.Namespace:
    """parse_args, but an unknown argument is a clear PytError instead of argparse's exit.

    An option the parser does not know is refused by name BEFORE parsing: argparse bound the
    value after it to a positional (`mode --typ strict`: "argument backend: invalid choice:
    'strict'", never a word about --typ). So is an option given twice, whose last value argparse
    keeps without a word (`new DIR --preset raylib --preset flet` made a flet project)."""
    options = args[: args.index("--")] if "--" in args else args
    known = parser._option_string_actions
    unknown = [a for a in options if a.startswith("-") and a != "-" and a.split("=", 1)[0] not in known]
    names = [a.split("=", 1)[0] for a in options if a.startswith("--") and a.split("=", 1)[0] in known]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated and not unknown:
        raise PytError(f"{parser.prog}: {', '.join(repeated)} given more than once; give each option once")
    ns = argparse.Namespace()
    if not unknown:
        ns, unknown = parser.parse_known_args(args)
    if unknown:
        raise PytError(f"{parser.prog}: unknown argument(s): {' '.join(unknown)}  ({parser.prog} -h lists the options)")
    return ns


def _builtins() -> set[str]:
    """The builtin commands, as dispatch validates with them: a [vscode] button of a retired
    command (config.RETIRED_COMMANDS) is left out only then, and mode rendered it into
    tasks.json, which render --check and the pre-commit hook then refused."""
    from .cli import COMMANDS  # lazy: cli imports this module through its COMMANDS table

    return set(COMMANDS)


def _config_from_text(text: str, where: str) -> Config:
    """Validate a pytemplate.toml text in memory (what config.load does with the file)."""
    try:
        data = tomllib.loads(text)
    except config.TOML_ERRORS as e:
        raise PytError(f"{where}: not valid TOML: {config.toml_error(e)}") from None
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg, _builtins())
    return cfg


# --- mode ----------------------------------------------------------------------------------------


_NEEDS_VALUE = "mode --supports needs a value: +pypy, -pypy or a list such as cpython,mypyc"


def _supports_after(cfg: Config, spec: str, backend: str | None = None) -> list[str]:
    """backend.supported after `--supports SPEC`: +name/-name changes, or the full list.

    `backend` (the BACKEND argument of `mode`) is added as `mode BACKEND` alone would, unless
    SPEC removes it or leaves it out of a full list: that contradiction is an error.
    """
    tokens = [t.strip() for t in spec.split(",") if t.strip()]  # "+pypy," and " +pypy" are fine
    if not tokens:
        raise PytError(_NEEDS_VALUE)
    signed = [t[:1] in ("+", "-") for t in tokens]
    if any(signed) and not all(signed):
        raise PytError(
            f"mode --supports {spec}: mixes changes (+name, -name) with plain names; give every "
            "change its sign (+pypy,-mypyc) or the full list (cpython,pypy,mypyc)"
        )
    known = " | ".join(BACKENDS)
    if all(signed):
        signs: dict[str, str] = {}
        for token in tokens:
            sign, name = token[0], token[1:].strip()
            if name not in BACKENDS:
                raise PytError(f"mode --supports: unknown backend '{name}' in '{token}' ({known})")
            if signs.setdefault(name, sign) != sign:
                raise PytError(f"mode --supports {spec}: {name} is both added and removed")
        dropped = {n for n, s in signs.items() if s == "-"}
        wanted = {*cfg.backend.supported, *(n for n, s in signs.items() if s == "+")} - dropped
    else:
        for name in tokens:
            if name not in BACKENDS:
                raise PytError(f"mode --supports: unknown backend '{name}' ({known})")
        dropped = set(BACKENDS) - set(tokens)
        wanted = set(tokens)
    if backend:
        if backend in dropped:
            how = "removes it" if all(signed) else "leaves it out of the list"
            raise PytError(
                f"mode {backend} --supports {spec}: {backend} would be the active backend, but --supports {how}"
            )
        wanted.add(backend)
    out = [b for b in BACKENDS if b in wanted]
    if not out:
        raise PytError(f"mode --supports {spec}: at least one backend must stay supported")
    return out


# mypy flags of the 3.11 API check: never the project's .mypy.ini (the default typing profile
# "off" sets ignore_errors = True there, which hid every error), and the bodies of unannotated
# functions are checked too. Only the errors that appear as 3.11 and not as python.cpython count.
# Modules are named from src (MYPYPATH, _precheck_mypypath) and the project folder, as .mypy.ini
# names them (explicit_package_bases): from the nearest folder with an __init__.py, an app package
# without one was "found twice" (`app` and `<pkg>.app`), mypy stopped and PyPy could not be enabled
PRECHECK_MYPY_FLAGS = (
    "--config-file=",  # an empty value: no config file at all
    "--check-untyped-defs",
    "--no-incremental",
    "--ignore-missing-imports",
    "--follow-imports", "silent",
    "--no-error-summary",
    "--hide-error-context",
    "--explicit-package-bases",
)


def _precheck_mypypath() -> str:
    """The MYPYPATH of the precheck's mypy: .mypy.ini's mypy_path (src, and typings/ when it holds
    stubs), relative to the project folder mypy runs in (an absolute path would split at a ':' of
    the project folder's path on POSIX)."""
    return os.pathsep.join(["src", "typings"] if render.typings_dir() else ["src"])


def precheck_key(line: str) -> tuple[str, str]:
    """A mypy error line (`path:line: error: message  [code]`) by what no typeshed wording
    changes: its place and its error code ("" for an error without one)."""
    where = line.split(": error:", 1)[0]
    code = re.search(r"\[([A-Za-z0-9_-]+)\]\s*$", line)
    return where, code.group(1) if code else ""


def _precheck_py311(cfg: Config) -> None:
    """Check that the code is valid on the pinned PyPy's Python (python.pypy: 3.11 by default,
    hence the name) before adding PyPy support.

    Two checks, both independent of the typing profile: ruff's syntax rules for that version,
    then the mypy errors that appear only when checking as it (an API it lacks, e.g.
    typing.override on 3.11). A tool that cannot run (a stale uv.lock, a failed install, mypy
    aborting) is reported as such: it is never blamed on the code and never passes silently.

    The tools environment is synced first; both tools then run with `uv run --no-sync`, so a
    failure of uv shows up in uv's own step. Under --dry-run nothing is synced: the checks run
    read-only, and are skipped when the environment does not exist yet.
    """
    version = cfg.pypy_minor
    ui.step(f"checking that the code is valid on Python {version} (required by PyPy)")
    tool = envs.tool_env(cfg)
    dry = proc.DRY_RUN
    if dry and not tool.python.is_file():
        ui.info(f"  (--dry-run) skipped: {rel(tool.dir)} does not exist yet (./pyt setup), and creating it is a side effect")
        return
    if dry:
        ui.info(f"  (--dry-run) running the read-only checks: ruff and mypy as Python {version}, with uv run --no-sync")
    else:
        envs.sync(tool)  # a stale uv.lock or a failed install fails HERE, with uv's own message
    run = ["run", "--locked", "--no-sync"]
    # Only the folders that hold Python files, as .mypy.ini's `files` (render._holds_python):
    # tests/ left with only __pycache__ (the tests removed with `git rm`) stopped mypy with "There
    # are no .py[i] files in directory 'tests'", while ./pyt check passed
    dirs = [d for d in code_dirs() if render._holds_python(ROOT / d)]
    if not dirs:  # ruff without a path would check the whole project
        ui.ok(f"no Python code in src/ or tests/: nothing to check for Python {version}")
        return
    # 1) syntax: ruff reports syntax that does not exist in the target version as an error
    #    (invalid-syntax). No lint rule besides E9 (an io-error): F632 `x is "a"` or an undefined
    #    name (F63, F82) is the same on every version, and blocked PyPy as "syntax" under the warn
    #    profile, whose check passes them
    r = envs.uv(
        tool,
        [*run, "ruff", "check", "--no-cache", "--isolated", "--target-version", "py" + version.replace(".", ""), "--select", "E9", *dirs],
        check=False,
        echo=not dry,  # proc.run skips echoed commands under --dry-run
    )
    if r.returncode == 1:  # ruff: 1 = findings
        raise PytError(f"the code uses syntax that does not exist in Python {version} (see above); fix it before enabling PyPy")
    if r.returncode != 0:  # 2 = ruff (or uv starting it) failed: nothing was checked
        raise PytError(f"could not run ruff for the Python {version} check (exit code {r.returncode}, see above)")

    # 2) APIs: mypy errors that appear ONLY when checking as that version (e.g. typing.override)
    def mypy_errors(version: str) -> dict[tuple[str, str], str]:
        """The error lines, by place and code (precheck_key): typeshed words the same error
        differently for each version (int(str | None) lists SupportsTrunc as 3.11 only)."""
        argv = [
            *run, "mypy", *PRECHECK_MYPY_FLAGS,
            "--python-version", version, "--python-executable", str(tool.python), *dirs,
        ]
        r = envs.uv(tool, argv, extra_env={"MYPYPATH": _precheck_mypypath()}, capture=True, check=False, echo=False)
        if r.returncode not in (0, 1):  # 2 = mypy (or uv starting it) aborted: nothing was checked
            ui.report(((r.stdout or "") + (r.stderr or "")).rstrip())  # why: shown even with -q
            raise PytError(f"mypy could not check the code as Python {version} (exit code {r.returncode}, see above)")
        errors: dict[tuple[str, str], str] = {}
        for ln in (r.stdout or "").splitlines():
            if ": error:" in ln:
                errors.setdefault(precheck_key(ln.strip()), ln.strip())
        return errors

    at_version = mypy_errors(version)
    at_cpython = mypy_errors(cfg.python.cpython)
    new = sorted(line for key, line in at_version.items() if key not in at_cpython)
    if new:
        for line in new:  # mypy's own lines (`path:line: error: ...`): never a second prefix
            ui.report(line)  # what was asked for: shown with -q too
        raise PytError(
            f"the code uses APIs that do not exist in Python {version} (above). Fix it before enabling PyPy "
            "(e.g. typing.override -> typing_extensions.override)"
        )
    ui.ok(f"the code is valid on Python {version}")


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _restore(before: dict[Path, bytes | None]) -> list[str]:
    """Put back the bytes (or the absence) of each file; return the names of those that changed."""
    restored: list[str] = []
    for path, data in before.items():
        if _read_bytes(path) == data:
            continue
        if data is None:
            path.unlink(missing_ok=True)
        else:
            write_whole(path, data)
        restored.append(path.name)
    return restored


def _current(cfg: Config, table: str, key: str) -> Any:
    return getattr(getattr(cfg, table), key)


def _leftover_envs(cfg: Config, new_cfg: Config) -> None:
    """One-line hint for the environments a mode change leaves unused (never deleted here)."""
    left: list[Path] = []
    if cfg.pypy_enabled and not new_cfg.pypy_enabled:
        left.append(envs.pypy_env(new_cfg).dir)
    names = [rel(d) for d in left if d.is_dir()]
    if names:
        ui.info(
            f"note: {', '.join(names)} {'is' if len(names) == 1 else 'are'} no longer used: "
            "./pyt clean --envs removes the .venv* environments (./pyt setup recreates the ones in use)"
        )


def _plan_mode(cfg: Config, new_cfg: Config, changes: list[tuple[str, str, object]], syncs: list[envs.PyEnv]) -> None:
    """--dry-run: print what `mode` would change, without writing or syncing anything."""
    ui.step(f"mode {_DRY}")
    effective = [(t, k, v) for t, k, v in changes if _current(cfg, t, k) != v]
    if not effective:
        ui.info("  pytemplate.toml  unchanged")
    for table, key, value in effective:
        ui.info(
            f"  pytemplate.toml  [{table}] {key} = {config.toml_value(value)}"
            f"   (now {config.toml_value(_current(cfg, table, key))})"
        )
    pyproject = render.pyproject_outdated(new_cfg)
    ui.info(
        "  pyproject.toml   "
        + ("would rewrite the managed parts (requires-python, [tool.uv] block)" if pyproject else "unchanged")
    )
    if pyproject:
        lock = "would re-lock (uv lock)"
    elif new_cfg.backend.supported != cfg.backend.supported:
        r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], check=False, capture=True, echo=False)
        lock = "up to date (uv lock --check)" if r.returncode == 0 else "would re-lock (uv lock)"
    else:
        lock = "unchanged"
    if lock.startswith("would re-lock"):
        # The real run's ensure_lock refuses this re-lock under the user's UV_FROZEN or UV_LOCKED
        # (`uv lock` writes nothing then): the plan says so, where it promised the re-lock
        from .cmd_env import _refuse_a_frozen_lock

        _refuse_a_frozen_lock()
    ui.info(f"  uv.lock          {lock}")
    changed, edited = render.apply(new_cfg)  # --dry-run: only compares
    ui.info("  generated files  " + (f"would update {', '.join(changed)}" if changed else "unchanged"))
    if edited:
        ui.info(f"                   hand-edited, left untouched: {', '.join(edited)}")
    for env in syncs:
        ui.info(f"  environment      would sync {rel(env.dir)} ({env.request})")


def cmd_mode(cfg: Config, args: list[str]) -> int:
    """mode [BACKEND] [--supports +pypy|-pypy|a,b] [--typing off|warn|strict|auto] [--editor pylance|basedpyright]"""
    # allow_abbrev=False: `--typ` is an unknown argument, never a silent alias of --typing
    parser = argparse.ArgumentParser(prog="./pyt mode", allow_abbrev=False)
    parser.add_argument("backend", nargs="?", choices=BACKENDS)
    parser.add_argument("--supports", help="+pypy, -pypy or a full list (cpython,mypyc)")
    parser.add_argument("--typing", choices=("auto", "off", "warn", "strict", "mypyc"))
    parser.add_argument("--editor", choices=config.EDITORS)
    # `--supports -pypy`: argparse would take "-pypy" for an option; join it as --supports=-pypy
    fixed: list[str] = []
    it = iter(args)
    for a in it:
        if a == "--supports":
            spec = next(it, "")
            if spec.startswith("--"):  # `--supports --typing strict`: the value is missing
                raise PytError(_NEEDS_VALUE)
            fixed.append(f"--supports={spec}")
        else:
            fixed.append(a)
    ns = _parse(parser, fixed)  # an option given twice is refused there, as for new, render, __init
    if ns.supports is not None and not ns.supports.strip():
        raise PytError("mode --supports needs a value: +pypy, -pypy or a list such as cpython,mypyc")
    if not any((ns.backend, ns.supports, ns.typing, ns.editor)):
        _describe(cfg)
        return 0

    changes: list[tuple[str, str, object]] = []
    supported = _supports_after(cfg, ns.supports, ns.backend) if ns.supports else list(cfg.backend.supported)
    if ns.backend and ns.backend not in supported:  # `mode pypy` alone also adds PyPy support
        supported = [b for b in BACKENDS if b in {*supported, ns.backend}]
    if supported != cfg.backend.supported:
        changes.append(("backend", "supported", supported))
    active = ns.backend or cfg.backend.active
    dropped_active = active not in supported  # `--supports -cpython` while cpython is active
    if dropped_active:
        active = supported[0]
    if active != cfg.backend.active:
        changes.append(("backend", "active", active))
    if ns.typing:
        if ns.typing in ("off", "warn", "strict"):
            changes += [("typing", "profile", "auto"), ("typing", "relaxed", ns.typing)]
        else:
            changes.append(("typing", "profile", ns.typing))
    if ns.editor:
        changes.append(("typing", "editor", ns.editor))
    # Only real changes: a value that is already set is not rewritten (its spelling stays)
    changes = [(t, k, v) for t, k, v in changes if _current(cfg, t, k) != v]

    # The new configuration, validated in memory BEFORE anything is written
    text = config.read_text()
    for table, key, value in changes:
        text = config.set_value(text, table, key, value)
    planned = _config_from_text(text, "mode: the new pytemplate.toml")
    # The managed parts of pyproject.toml must be rewritable for it (damaged markers, a managed
    # key repeated outside them...): refused here, before pytemplate.toml changes
    render.check_pyproject(planned)
    if dropped_active:
        ui.info(f"note: {cfg.backend.active} is no longer supported: the active backend becomes {active}")

    # PyPy is new when uv.lock does not resolve for it yet (render.gains_pypy), not only when this
    # command adds it: after a hand edit of backend.supported, pytemplate.toml already lists it, and
    # mode locked PyPy in without the Python 3.11 check and without .venv-pypy. The check runs in
    # ensure_lock, once the re-lock is done (--dry-run: here, read-only).
    adding_pypy = planned.pypy_enabled and (not cfg.pypy_enabled or render.gains_pypy(planned))
    syncs: list[envs.PyEnv] = []
    if adding_pypy:
        syncs.append(envs.pypy_env(planned))
        if proc.DRY_RUN:
            _precheck_py311(cfg)

    if proc.DRY_RUN:
        _plan_mode(cfg, planned, changes, syncs)
        _leftover_envs(cfg, planned)
        _describe(planned, "mode after the change (not applied: --dry-run)")
        return 0

    # Nothing half-applied: when the re-lock or a new environment fails (no solution for the new
    # interpreter, no network, Ctrl+C), the three files get their old bytes back. The generated
    # files are rendered only after that, so they never describe a mode that did not happen.
    before = {path: _read_bytes(path) for path in (CONFIG_FILE, PYPROJECT, PYPROJECT.with_name("uv.lock"))}
    try:
        config.update_file(changes)
        new_cfg = config.load(_builtins())
        heavy = supported != cfg.backend.supported
        if heavy or render.pyproject_outdated(new_cfg):
            from .cmd_env import ensure_lock

            ensure_lock(new_cfg)
        for env in syncs:
            envs.sync(env)
    except BaseException as e:
        restored = _restore(before)
        done = f"{', '.join(restored)} restored: the mode did not change" if restored else "the mode did not change"
        if not isinstance(e, PytError):
            ui.warn(done)
            raise
        raise PytError(f"{e}\n  {done}; fix the problem above and run the command again", e.code) from None
    changed, edited = render.apply(new_cfg)
    if changed:
        ui.info(f"render: updated {', '.join(changed)}")
    if edited:  # they still describe the old mode (the dry run names them too)
        ui.warn(f"not overwriting hand-edited generated files: {', '.join(edited)} (./pyt render --force)")
    _leftover_envs(cfg, new_cfg)
    _describe(new_cfg)
    return 0


# --- render --------------------------------------------------------------------------------------


def cmd_render(cfg: Config, args: list[str]) -> int:
    """render [--check] [--diff] [--force]: regenerate the configuration files."""
    parser = argparse.ArgumentParser(prog="./pyt render")
    parser.add_argument("--check", action="store_true", help="only report outdated files (exit code 1)")
    parser.add_argument("--diff", action="store_true", help="show how hand-edited files differ")
    parser.add_argument("--force", action="store_true", help="also overwrite hand-edited generated files")
    ns = _parse(parser, args)
    changed, edited = render.apply(cfg, force=ns.force, check=ns.check, show_diff=ns.diff)
    prefix = "outdated: " if ns.check else "would update: " if proc.DRY_RUN else "updated: "
    for path in changed:
        ui.report(prefix + path)  # the answer to --check: shown even with -q
    for path in edited:
        ui.warn(f"hand-edited (left untouched without --force): {path}")
    if render.pyproject_outdated(cfg):
        ui.warn("pyproject.toml does not match pytemplate.toml: ./pyt apply")
        if ns.check:
            return 1
    if ns.check and (changed or edited):
        return 1
    if not changed and not edited:
        ui.ok("generated files up to date")
    return 0


# --- init / new ----------------------------------------------------------------------------------


def _owned_now() -> dict[str, bytes]:
    """The files of src/, tests/ and typings/ (what init replaces), CRLF-normalized."""
    out: dict[str, bytes] = {}
    for d in presets.OWNED_DIRS:
        base = ROOT / d
        if base.is_dir():
            for path in base.rglob("*"):
                if path.is_file() and not any(p in path.parts for p in ("__pycache__", ".pytest_cache", ".hypothesis")):
                    out[path.relative_to(ROOT).as_posix()] = path.read_bytes().replace(b"\r\n", b"\n")
    return out


def _plan_init(cfg: Config, preset: str, name: str | None, *, force: bool) -> None:
    """--dry-run: print what `init` would do (presets.plan_init: the same checks, no writes)."""
    plan = presets.plan_init(cfg, preset, name, force=force)
    owned = _owned_now()
    ui.step(f"init {preset} ({plan.description}) as '{plan.name}' {_DRY}")
    marks: list[str] = []
    same = 0
    for path in sorted({*plan.files, *owned}):
        target_file = ROOT / path
        if path not in plan.files:
            marks.append(f"    - {path}")
        elif not target_file.is_file():
            marks.append(f"    + {path}")
        elif target_file.read_bytes().replace(b"\r\n", b"\n") != plan.files[path]:
            marks.append(f"    ~ {path}")
        else:
            same += 1
    ui.info(f"  files (- deleted, + new, ~ replaced; {same} identical):")
    for line in marks:
        ui.info(line)
    for label, drop, add in (("dependencies", plan.drop, plan.add), ("dev group", plan.drop_dev, plan.add_dev)):
        ui.info(f"  {label + ':':<14} remove {', '.join(drop) or '-'}; add {', '.join(add) or '-'}")
    if plan.pins:
        source = rel(presets.constraints_path(preset))
        ui.info(f"  {'versions:':<14} {len(plan.pins)} packages new to uv.lock at the versions the template tested ({source})")
    ui.info(f"  {PYPROJECT.name}: name = \"{plan.name}\", the preset's extra tables and the managed [tool.uv] block")
    ui.info("  uv.lock: re-locked (uv lock); generated files: re-rendered with --force")


def cmd_init(cfg: Config, args: list[str]) -> int:
    """__init PRESET [--name NAME] [--force]: convert this project to the preset (internal: ./pyt new)."""
    parser = argparse.ArgumentParser(prog="./pyt __init")
    parser.add_argument("preset", choices=presets.available())
    parser.add_argument("--name")
    parser.add_argument("--force", action="store_true")
    ns = _parse(parser, args)
    if proc.DRY_RUN:
        _plan_init(cfg, ns.preset, ns.name, force=ns.force)
        return 0
    presets.init(cfg, ns.preset, ns.name, force=ns.force)
    return 0


def _work_tree_top(folder: Path) -> Path | None:
    """The top of the git work tree `folder` would be in (its nearest existing parent is asked:
    new creates the folder), or None: no work tree there, one that ignores `folder` (the project
    gets a repository of its own there: presets.ignored_by_work_tree), or no git."""
    git = proc.find_program("git")
    if git is None:
        return None
    probe = folder
    while not probe.is_dir() and probe != probe.parent:
        probe = probe.parent
    r = proc.run([git, "rev-parse", "--show-toplevel"], cwd=probe, env=presets._git_env(), capture=True, check=False, echo=False)
    top = r.stdout.strip() if r.returncode == 0 else ""
    if not top or presets.ignored_by_work_tree(git, folder, probe):
        return None
    return Path(native_path(top))  # MSYS2's own git prints /c/...


def _monorepo_note(dest: Path, top: Path) -> None:
    """A project inside a bigger repository: new runs no git init there, and GitHub reads workflows
    only from the repository's own .github/workflows, so the generated CI never runs as it is."""
    sub = Path(os.path.relpath(dest.resolve(), top.resolve())).as_posix()
    ui.warn(
        f"{dest} is inside the git work tree of {top}: GitHub runs only {top.name}/.github/workflows/*.yml,\n"
        f"  so the project's generated .github/workflows/ci.yml does not run from {sub}/. For CI, add a\n"
        f"  workflow to the repository that runs its steps in {sub} (defaults.run.working-directory) and\n"
        f"  takes the artifacts from {sub}/dist/"
    )


def cmd_new(cfg: Config, args: list[str]) -> int:
    """new DIR [--preset P] [--name NAME]: copy the template to a new project."""
    from .cli import _prog  # `pyt` outside a project (global mode), where `new` runs too

    parser = argparse.ArgumentParser(prog=f"{_prog()} new")
    parser.add_argument("dest")
    parser.add_argument("--preset", default="script", choices=presets.available())
    parser.add_argument("--name")
    ns = _parse(parser, args)
    dest = user_path(ns.dest)
    # realpath, never Path.resolve: on Python 3.11 and 3.12 (new runs on any 3.11+) resolve raises
    # RuntimeError for a link loop, an internal error; check_destination names it below
    resolved = Path(os.path.realpath(dest))
    if resolved == ROOT or ROOT in resolved.parents:
        raise PytError(f"new: the destination folder cannot be inside {presets.source_name()}")
    presets.check_destination(dest, "new: ")
    # Checked here, before copying (and under --dry-run): a copy whose `init` fails is removed.
    # An empty --name (a script's "$NAME" with NAME unset) is no name: it took the folder's
    if ns.name == "":
        raise PytError("new: --name is empty: give the app a name, or leave --name out to name it after the folder")
    name = presets.name_from_folder(resolved.name) if ns.name is None else ns.name
    if not config.APP_NAME.fullmatch(name):
        raise PytError(
            f"new: '{name}' is not a valid app name (it may only contain {config.NAME_RULE}). "
            "Choose one with --name NAME"
        )
    presets.check_name_free(cfg, ns.preset, name)
    # Every new project is locked anew (its own name, the preset's requirements: __init runs `uv
    # add` and `uv lock`), which the user's UV_FROZEN or UV_LOCKED forbid. Refused here, before
    # the copy and in the dry run too: uv's own error came after the copy, blaming a `--no-sync`
    # the user never typed (or saying to run `uv lock`), and the dry run promised success.
    from .cmd_env import _refuse_a_frozen_lock  # imported where used: cmd_env imports much more

    _refuse_a_frozen_lock("a new project is locked anew")
    top = _work_tree_top(dest)
    # The new project's python.cpython: its lock needs it (uv lock), and its __init runs on it.
    # Asked before the copy: where uv cannot install it (Android/Termux), nothing is written.
    version = presets.preset_python(ns.preset)
    if proc.DRY_RUN:
        found = envs.find_cpython(version)
        if found is None and envs.cpython_downloads(version) is False:
            raise PytError(envs.no_download_problem(version), 3)
        ui.step(f"new project in {dest} {_DRY}")
        ui.info(f"  preset  {ns.preset}")
        ui.info(f"  name    {name}  (package src/{name.replace('-', '_').lower()}/)")
        ui.info(f"  python  CPython {version}: {found if found is not None else f'not installed, uv would install it (uv python install {version})'}")
        # what __init pins in the copy: the packages its uv.lock (this one) does not have yet
        locked = presets.locked_names()
        pins = [n for n in presets.constraints(ns.preset) if n not in locked]
        if pins:
            ui.info(f"  pins    {len(pins)} packages new to uv.lock at the versions the template tested (constraints.txt of the preset)")
        if top is not None:
            git = f"(inside the git work tree of {top}: no git init)"
        elif proc.find_program("git") is None:
            git = "(git not found: no git init)"
        else:
            git = "and `git init -b main`"
        ui.info(
            f"  would copy {presets.source_name()} there ({presets.copy_scope()}; no .git, environments, builds or "
            f"caches), run `./pyt __init {ns.preset} --name {name} --force` in it {git}"
        )
        if top is not None:
            _monorepo_note(dest, top)
        return 0
    presets.new(dest, ns.preset, name, python=envs.ensure_python(version))
    if top is not None:
        _monorepo_note(dest, top)
    return 0
