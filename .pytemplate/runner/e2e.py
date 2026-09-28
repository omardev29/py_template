"""selftest --e2e: end-to-end test of the template.

    ./pyt selftest --e2e [PRESET ...] [--backends B,..] [--methods M,..] [--quick | --full]
                            [--gui auto|on|off] [--keep] [--reuse] [--json] [--base DIR]

For each preset (default: script, raylib, flet) it creates <base>/<preset> with THIS
template's `./pyt new`, checks what `new` made (verify: what it must and must not copy, the
git repository, the pinned versions, the project's own README), then works in it the way a
user does: setup, the first commit (through the pre-commit hook setup installs), check, test,
run and build (every artifact in dist/ is checked, the cheap headless ones are run, a portable
folder from another path), plus, in the deeper modes, the runner's own tests, a rename there
and back, `mode` and `apply` changes. The runner goes through `uv run --script` with stdin
closed, in a clean environment whose git sees neither the repositories around the base nor the
user's git configuration (isolate_git).

- --quick:  one build per backend (its deploy.default method): what every push runs.
- default:  every (backend, method) pair that cmd_build.COMPAT allows except nuitka (slow),
            `./pyt selftest` in the project, a usage error, and a rename round trip.
- --full:   adds nuitka, a pypy round trip (`mode --supports +pypy`, test, and back) and a
            [preset.*] option edit applied with `./pyt apply` (another version: network).
- `flet build` (Flutter SDK + Windows Developer Mode) is SKIP unless both are detected.

Every step is one PASS/FAIL/SKIP row with its time; its output goes to
<base>/logs/<preset>/NN-step.log. A failed step fails its preset (a failed `new` or `setup`
skips the rest of that preset) and the next presets still run. A round trip must leave the
project's files as they were before it (project_state snapshots). The base dir is removed at
the end unless --keep or something failed. Exit codes: 0 everything passed, 1 a FAIL, 2 a usage
error (also --backends/--methods that select nothing to test), 130 interrupted (Ctrl+C, SIGTERM,
SIGHUP). The planning (plan, parse_args, scrub_env, selection_problem) is pure and unit-tested
in .pytemplate/tests/test_e2e_plan.py; the running parts (steps, logs, exit codes, signals)
in test_e2e_run.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import proc, ui
from .cmd_build import COMPAT
from .config import BACKENDS, METHODS, Config
from .project import CONFIG_FILE, ENV_SUFFIX, IS_WINDOWS, PRESETS, ROOT, base_lock, check_private_dir, host_arch, host_os, make_private_dir, scratch_name, user_path, venv_python
from .ui import PytError

DEFAULT_PRESETS = ("script", "raylib", "flet")
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
MARKER = ".pytemplate-e2e"  # in the base dir: only a dir carrying it is ever wiped
# Dropped from every child's environment: they would point the new project's uv at this
# runner's own environment or interpreter, or leak the outer launcher's state (PYTEMPLATE_*).
# Every GIT_* goes too (the GIT_DIR/GIT_INDEX_FILE of a hook the suite runs from, a user's
# GIT_CONFIG_*): isolate_git sets the ones the children get.
# (UV_MANAGED_PYTHON, UV_NO_MANAGED_PYTHON: uv refuses them next to the --python-preference of
# the launchers' uv call, which the launchers drop them for)
SCRUBBED = frozenset(
    {"VIRTUAL_ENV", "UV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON", "PYTHONHOME", "PYTHONPATH"}
)
SCRUBBED_PREFIXES = ("PYTEMPLATE_", "GIT_")

# Seconds per step kind (builds: per method). Generous: the first run downloads interpreters,
# PyPy and wheels; PyInstaller, portable runtime copies and mypyc compiles take minutes.
TIMEOUTS = {
    "new": 900,
    "verify": 120,
    "pristine": 300,
    "render": 300,
    "mode": 1800,
    "setup": 1800,
    "apply": 1800,
    "doctor": 300,
    "commit": 600,
    "fmt": 300,
    "stubs": 600,
    "check": 900,
    "test": 1200,
    "run": 600,
    "selftest": 2400,
    "usage": 120,
    "rename": 900,
    "smoke": 300,
}
BUILD_TIMEOUTS = {"exe": 1200, "portable": 1200, "pyz": 900, "wheel": 900, "nuitka": 3600, "flet": 3600}
# How each preset's app ends on its own, and the text its output must contain. A preset
# missing here runs with no arguments if it is a console app and is skipped if it is a GUI.
SMOKE: dict[str, tuple[tuple[str, ...], str]] = {
    "script": ((), "Primes"),
    "raylib": (("--frames", "5"), "frames in"),
}
# What the app prints when its core really is the mypyc binary (not the .py fallback)
COMPILED_MARK = {"script": "mypyc (compiled)", "raylib": "mypyc/"}
SMOKE_METHODS = ("exe", "portable", "pyz", "wheel", "nuitka")  # `flet build`: files only
# The test projects are named e2e-<preset>, except these, named like their package (e2escript,
# no hyphen: like most `./pyt new DIR` projects and the template's own myapp). Only then do
# an executable and the package folder of the same name meet in one folder (nuitka standalone,
# onedir builds: nuitka names its binary <app>.bin there); the hyphenated ones cover name !=
# package (rename's context rules). The rename round trip flips the shape (renamed_app) and
# builds and runs the wheel under the other name, so the script preset's artifacts run as both.
SAME_AS_PACKAGE = frozenset({"script"})
# Backends a preset cannot install on a host: (preset, "<os>-<arch>") -> {backend: reason}.
# The project is switched off them (`mode`) before setup. raylib 6.0.1.0 publishes PyPy wheels
# for x86_64 only (Windows, Linux, macOS Intel). render.ci_workflow leaves the same backends out
# of the generated CI matrix for the architecture of each hosted runner it uses (ubuntu-latest
# and windows-latest are x86_64, macos-latest is arm64): there only the macOS gap shows
# (test_e2e_plan.test_host_gaps_match_the_generated_ci_matrix).
HOST_GAPS: dict[tuple[str, str], dict[str, str]] = {
    ("raylib", "macos-aarch64"): {"pypy": "raylib publishes no PyPy wheels for macOS arm64"},
    ("raylib", "linux-aarch64"): {"pypy": "raylib publishes no PyPy wheels for Linux arm64"},
}
# --full: one [preset.<name>] edit per preset that has options, applied with `./pyt apply`
# (another package or version: the network). raylib_sdl ships the same wheels as raylib (PyPy
# on x86_64, CPython everywhere), so the host gaps stay the same.
OPTION_EDITS: dict[str, tuple[str, str]] = {"raylib": ("package", "raylib_sdl"), "flet": ("version", "1.0.0")}
# `new` never copies these into a project (checked at its root, next to .venv*). `__init`, the
# step `new` runs in the copy, writes its own scratch into .build/init (the pins it hands to uv).
NEVER_COPIED = (".build", "dist", "build", ".claude")
MADE_BY_INIT = {".build": frozenset({"init"})}
# What a round trip must restore: every file of the project but what .gitignore leaves out
# (environments, builds, caches, compiled extensions) and .git (commits change the index)
STATE_SKIP_DIRS = frozenset(
    {".git", ".build", "dist", "build", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".hypothesis", ".flet", ".claude"}
)
STATE_SKIP_SUFFIXES = (".pyc", ".pyo", ".so", ".pyd", ".spec")
GIT_IDENTITY = ("-c", "user.name=pytemplate e2e", "-c", "user.email=e2e@example.invalid")


# --- planning (pure: unit-tested) --------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    presets: tuple[str, ...] = DEFAULT_PRESETS
    backends: tuple[str, ...] = ()  # empty = every supported backend
    methods: tuple[str, ...] = ()  # empty = the default selection
    quick: bool = False
    full: bool = False
    gui: str = "auto"  # auto | on | off
    keep: bool = False
    reuse: bool = False
    as_json: bool = False
    base: str = ""  # as typed by the user ("" = default_base())


@dataclass(frozen=True)
class Host:
    os: str  # windows | linux | macos
    display: str = ""  # why a GUI window cannot open here ("" = it can)
    gui_wrap: tuple[str, ...] = ()  # argv prefix for GUI runs (xvfb-run on a headless Linux)
    flet_build: str = ""  # why `flet build` cannot run here ("" = it can)
    arch: str = ""  # x86_64 | aarch64 (uv names; "" = unknown)


@dataclass(frozen=True)
class PresetInfo:
    name: str
    supported: tuple[str, ...]
    defaults: Mapping[str, str]  # deploy.default: backend -> method
    gui: bool
    active: str = "cpython"  # backend.active of the preset's pytemplate.toml
    tasks: tuple[str, ...] = ()  # its [tasks] names

    @property
    def app(self) -> str:
        """App name of the test project (never the preset name: `flet` or `raylib` would shadow the library)."""
        return f"e2e{self.name}" if self.name in SAME_AS_PACKAGE else f"e2e-{self.name}"


@dataclass(frozen=True)
class Step:
    preset: str
    name: str  # row label, unique within the preset
    kind: str  # new | verify | pyt | commit | option | build | smoke | unsupported
    # ./pyt arguments (pyt, build); (table, key, value) for option; (message,) for commit
    args: tuple[str, ...] = ()
    timeout: float = 600
    skip: str = ""  # SKIP with this reason, never run
    required: bool = False  # if it fails, the rest of the preset is skipped
    after: str = ""  # a step that must PASS first (smoke -> its build)
    expect: tuple[str, ...] = ()  # text the app's stdout must contain
    expect_code: int = 0  # the exit code that passes (2: a usage error the runner must refuse)
    wrap: tuple[str, ...] = ()  # argv prefix (xvfb-run)
    backend: str = ""
    method: str = ""
    app: str = ""  # build, smoke: the app name the artifact carries ("" = the project's, PresetInfo.app)
    snapshot: str = ""  # record the project's files (project_state of `scope`) under this key first
    restores: str = ""  # after the step they must equal the snapshot of this key (a round trip)
    scope: str = ""  # folder of the project the snapshot covers ("" = all of it)


def _pyt(preset: str, name: str, args: tuple[str, ...], *, kind: str = "", **kw: Any) -> Step:
    return Step(preset, name, "pyt", args, timeout=TIMEOUTS.get(kind or args[0], 600), **kw)


def expected_output(preset: str, backend: str) -> tuple[str, ...]:
    text = SMOKE.get(preset, ((), ""))[1]
    if not text:
        return ()
    mark = COMPILED_MARK.get(preset, "")
    return (text, mark) if backend == "mypyc" and mark else (text,)


def renamed_app(app: str) -> str:
    """The other name of the rename round trip. It flips name == package (e2escript ->
    e2escript-2, e2e-raylib -> e2eraylib2), so rename's context rules run both ways."""
    if app == app.replace("-", "_").lower():
        return app + "-2"
    return app.replace("-", "").replace("_", "").lower() + "2"


def run_step(info: PresetInfo, backend: str, host: Host) -> Step:
    name = f"run {backend}"
    smoke = SMOKE.get(info.name)
    if smoke is None and info.gui:
        return Step(info.name, name, "pyt", skip="GUI app that does not close itself: `test` covers its core", backend=backend)
    args = smoke[0] if smoke else ()
    skip = host.display if info.gui else ""
    wrap = host.gui_wrap if info.gui and not skip else ()
    return _pyt(info.name, name, ("run", backend, *args), skip=skip, wrap=wrap, expect=expected_output(info.name, backend), backend=backend)


def build_methods(info: PresetInfo, backend: str, opts: Options, host: Host) -> list[tuple[str, str]]:
    """Return the (method, skip reason) pairs to build for a backend ("" = build it)."""
    candidates = [info.defaults.get(backend, "exe")] if opts.quick else list(METHODS)
    out: list[tuple[str, str]] = []
    for method in candidates:
        if COMPAT.get(method, {}).get(backend):
            continue  # unsupported by design (e.g. PyInstaller + PyPy): not a row
        if method == "flet" and info.name != "flet":
            continue
        if opts.methods and method not in opts.methods:
            continue
        reason = ""
        if method == "nuitka" and not opts.full and method not in opts.methods:
            reason = "slow (compiles everything to C): add --full or --methods nuitka"
        elif method == "flet" and host.flet_build:
            reason = host.flet_build
        out.append((method, reason))
    return out


def rename_round_trip(info: PresetInfo, targets: Sequence[str], wheel: str = "") -> list[Step]:
    """Default and --full: `rename` to another name (a dry run first: it writes nothing), test
    the renamed project, build and run its wheel under the new name (`wheel`: that backend;
    "" = no build), commit it through the hook, and rename it back: every file must be as
    before. rename refuses uncommitted changes, so it needs the first commit, and the way back
    the commit of the renamed project (a user commits a rename too)."""
    p, old = info.name, info.app
    new = renamed_app(old)
    head = f"rename {new}"
    dry = f"rename {new} (dry run)"
    commit = f"commit ({new})"
    steps = [
        _pyt(p, dry, ("--dry-run", "rename", new), kind="rename", snapshot=dry, restores=dry),
        _pyt(p, head, ("rename", new), after="first commit", snapshot=head),
        _pyt(p, f"render --check ({new})", ("render", "--check"), after=head),
        *[_pyt(p, f"test {t} ({new})", ("test", t), after=head, backend="" if t == "all" else t) for t in targets],
    ]
    if wheel:
        build = f"build {wheel} wheel ({new})"
        steps += [
            Step(p, build, "build", ("build", wheel, "--method", "wheel", "--no-check"), timeout=BUILD_TIMEOUTS["wheel"], after=head, backend=wheel, method="wheel", app=new),
            Step(p, f"smoke {wheel} wheel ({new})", "smoke", timeout=TIMEOUTS["smoke"], after=build, expect=expected_output(p, wheel), backend=wheel, method="wheel", app=new),
        ]
    return steps + [
        Step(p, commit, "commit", (f"Rename the app to {new}",), timeout=TIMEOUTS["commit"], after=head),
        _pyt(p, f"rename {old} (back)", ("rename", old), after=commit, restores=head),
    ]


def pypy_round_trip(info: PresetInfo) -> list[Step]:
    """--full: add PyPy, test it and remove it again (the reverse for presets that have it). The
    project must end as it was, the active backend included (`-pypy` moves it off PyPy)."""
    p = info.name
    has = "pypy" in info.supported
    first = ("mode", "--supports", "-pypy") if has else ("mode", "--supports", "+pypy")
    if not has:
        back: tuple[str, ...] = ("mode", "--supports", "-pypy")
    elif info.active == "pypy":
        back = ("mode", "pypy", "--supports", "+pypy")
    else:
        back = ("mode", "--supports", "+pypy")
    head = f"round trip: {' '.join(first)}"
    test = ("test", "all") if has else ("test", "pypy")
    return [
        _pyt(p, head, first, snapshot=head, backend="pypy"),
        _pyt(p, f"round trip: {' '.join(test)}", test, after=head, backend="pypy"),
        _pyt(p, f"round trip: {' '.join(back)}", back, after=head, restores=head, backend="pypy"),
        _pyt(p, "round trip: render --check", ("render", "--check"), after=head, backend="pypy"),
    ]


def option_edit(info: PresetInfo) -> list[Step]:
    """--full: edit an [preset.<name>] option (OPTION_EDITS), `./pyt apply` it (pyproject.toml
    and uv.lock must follow), then doctor must be clean and a second apply must change nothing."""
    edit = OPTION_EDITS.get(info.name)
    if edit is None:
        return []
    key, value = edit
    p = info.name
    head = f"[preset.{p}] {key} = {value}: apply"
    again = "apply again (no change)"
    return [
        Step(p, head, "option", (f"preset.{p}", key, value), timeout=TIMEOUTS["apply"]),
        _pyt(p, "doctor (option applied)", ("doctor",), after=head),
        _pyt(p, again, ("apply",), after=head, snapshot=again, restores=again),
    ]


def host_gaps(info: PresetInfo, host: Host) -> dict[str, str]:
    """Return the preset's backends this host cannot install, with the reason."""
    gaps = HOST_GAPS.get((info.name, f"{host.os}-{host.arch}"), {})
    return {b: why for b, why in gaps.items() if b in info.supported}


def plan(info: PresetInfo, opts: Options, host: Host) -> list[Step]:
    """Return every step (row) for one preset, in order."""
    p = info.name
    steps = [
        Step(p, "new", "new", timeout=TIMEOUTS["new"], required=True),
        Step(p, "verify copy", "verify", timeout=TIMEOUTS["verify"]),
        # `__init` refuses (without --force) a project whose src/, tests/ and typings/ are not
        # exactly the preset's skeleton with this name: its dry run checks what `new` left
        _pyt(p, "pristine skeleton", ("--dry-run", "__init", p, "--name", info.app), kind="pristine"),
        _pyt(p, "render --check", ("render", "--check")),
    ]
    gaps = host_gaps(info, host)
    keep = [b for b in info.supported if b not in gaps]
    if gaps:
        # `mode` BACKEND also moves the active backend off a gap (raylib's is pypy)
        steps.append(_pyt(p, f"mode --supports {','.join(keep)}", ("mode", keep[0], "--supports", ",".join(keep)), required=True))
    steps += [
        _pyt(p, "setup", ("setup",), required=True),
        _pyt(p, "doctor", ("doctor",)),
        _pyt(p, "fmt --check", ("fmt", "--check")),
        # `git add -A` + `git commit` through the hook setup installed: the user's first commit
        Step(p, "first commit", "commit", ("First commit",), timeout=TIMEOUTS["commit"]),
    ]
    if "stubs" in info.tasks:  # raylib: the generator must reproduce the shipped stub byte for byte
        steps.append(_pyt(p, "stubs (typings/ unchanged)", ("stubs",), snapshot="stubs", restores="stubs", scope="typings"))
    backends = [b for b in keep if not opts.backends or b in opts.backends]
    targets = backends if opts.backends else ["all"]
    for verb in ("check", "test"):
        steps += [_pyt(p, f"{verb} {t}", (verb, t), backend="" if t == "all" else t) for t in targets]
    steps += [run_step(info, b, host) for b in backends]
    steps += [Step(p, f"{b} (every step)", "pyt", skip=why, backend=b) for b, why in gaps.items() if not opts.backends or b in opts.backends]
    trip = opts.full and "pypy" not in gaps and (not opts.backends or "pypy" in opts.backends)
    for b in opts.backends:
        if b not in info.supported and not (trip and b == "pypy"):
            extra = " (--full adds it for a round trip)" if b == "pypy" and not opts.full else ""
            steps.append(Step(p, f"{b} (not supported)", "unsupported", skip=f"the {p} preset does not support {b}{extra}", backend=b))
    if not opts.quick and targets:
        steps.append(_pyt(p, "selftest in the project", ("selftest",)))
    wheel = ""  # the first backend whose wheel is built and run: the rename builds it again
    planned = 0
    for b in backends:
        for method, reason in build_methods(info, b, opts, host):
            build = f"build {b} {method}"
            planned += 1
            steps.append(
                Step(p, build, "build", ("build", b, "--method", method, "--no-check"), timeout=BUILD_TIMEOUTS.get(method, 1200), skip=reason, backend=b, method=method)
            )
            if not reason and not info.gui and method in SMOKE_METHODS:
                steps.append(Step(p, f"smoke {b} {method}", "smoke", timeout=TIMEOUTS["smoke"], after=build, expect=expected_output(p, b), backend=b, method=method))
                wheel = wheel or (b if method == "wheel" else "")
    if opts.methods and backends and not planned:
        why = f"--methods {','.join(opts.methods)} builds nothing with {', '.join(backends)} here"
        steps.append(Step(p, "build (none selected)", "unsupported", skip=why + (" (--quick: only deploy.default methods)" if opts.quick else "")))
    if not opts.quick and backends:
        # the runner refuses a typo (a method without --method) instead of building the default
        steps.append(_pyt(p, f"usage error: build {backends[0]} pyz", ("build", backends[0], "pyz"), kind="usage", expect_code=2))
    if not opts.quick and targets:
        steps += rename_round_trip(info, targets, wheel)
    if trip:
        steps += pypy_round_trip(info)
    if opts.full:
        steps += option_edit(info)
    return steps


def selection_problem(plans: Sequence[tuple[PresetInfo, list[Step]]], opts: Options) -> str:
    """Why --backends/--methods select nothing to test in the selected presets ("" = they do).

    A backend a host cannot install still counts: its rows SKIP with the reason. A backend a
    preset does not support is only a SKIP row there, and an error when no preset has it.
    """
    steps = [s for _, ss in plans for s in ss]
    names = ", ".join(info.name for info, _ in plans)
    if opts.backends and not any(s.backend in opts.backends and s.kind != "unsupported" for s in steps):
        have = "; ".join(f"{info.name}: {', '.join(info.supported)}" for info, _ in plans)
        extra = "; --full adds pypy for a round trip" if "pypy" in opts.backends and not opts.full else ""
        return f"--backends {','.join(opts.backends)} selects nothing to test in {names} (supported: {have}{extra})"
    if opts.methods and not any(s.kind == "build" for s in steps):
        if opts.quick:
            why = "--quick builds only each backend's deploy.default method: drop --quick"
        else:
            why = "cmd_build.COMPAT: exe and nuitka need cpython or mypyc; flet builds only the flet preset"
        return f"--methods {','.join(opts.methods)} selects no build in {names} ({why})"
    return ""


def _csv(raw: str, allowed: Sequence[str], flag: str) -> tuple[str, ...]:
    names = [n.strip() for n in raw.split(",") if n.strip()]
    bad = [n for n in names if n not in allowed]
    if bad:
        raise PytError(f"selftest --e2e {flag}: unknown {', '.join(bad)} (valid: {', '.join(allowed)})")
    return tuple(dict.fromkeys(names))


def parse_args(args: Sequence[str], available: Sequence[str]) -> Options:
    parser = argparse.ArgumentParser(prog="./pyt selftest --e2e", description="End-to-end test: ./pyt new + setup/check/test/run/build per preset.")
    parser.add_argument("presets", nargs="*", metavar="PRESET", help="presets to test, spaces or commas (default: script raylib flet)")
    parser.add_argument("--backends", default="", help="only these backends, e.g. cpython,mypyc")
    parser.add_argument("--methods", default="", help="only these build methods, e.g. exe,pyz (naming nuitka runs it without --full)")
    size = parser.add_mutually_exclusive_group()
    size.add_argument("--quick", action="store_true", help="one build per backend (its deploy.default method); no selftest or rename in the project")
    size.add_argument("--full", action="store_true", help="also nuitka, a `mode --supports +pypy` round trip and a [preset.*] option edit")
    parser.add_argument("--gui", choices=("auto", "on", "off"), default="auto", help="GUI runs (raylib: 5 frames); auto skips them with no display or on a Windows/macOS CI runner")
    parser.add_argument("--keep", action="store_true", help="keep the base dir even when everything passes")
    parser.add_argument("--reuse", action="store_true", help="reuse <base>/<preset> kept by an earlier run instead of recreating it")
    parser.add_argument("--json", action="store_true", help="print the results as JSON on stdout")
    parser.add_argument("--base", default="", help="base dir (default: %%TEMP%%\\pt\\e2e on Windows, $TMPDIR/pt-e2e-<uid> elsewhere)")
    ns = parser.parse_intermixed_args(list(args))  # `raylib --quick flet` works too
    names = [n for chunk in ns.presets for n in chunk.split(",") if n] or list(DEFAULT_PRESETS)
    unknown = [n for n in names if n not in available]
    if unknown:
        raise PytError(f"selftest --e2e: unknown preset {', '.join(unknown)} (available: {', '.join(available)})")
    return Options(
        presets=tuple(dict.fromkeys(names)),
        backends=_csv(ns.backends, BACKENDS, "--backends"),
        methods=_csv(ns.methods, METHODS, "--methods"),
        quick=ns.quick,
        full=ns.full,
        gui=ns.gui,
        keep=ns.keep,
        reuse=ns.reuse,
        as_json=ns.json,
        base=ns.base,
    )


def scrub_env(environ: Mapping[str, str], drop_dirs: Sequence[str] = ()) -> dict[str, str]:
    """Return a copy of `environ` without SCRUBBED, PYTEMPLATE_* and GIT_* (and `drop_dirs` removed from PATH)."""
    env = {k: v for k, v in environ.items() if k.upper() not in SCRUBBED and not k.upper().startswith(SCRUBBED_PREFIXES)}
    drop = {os.path.normcase(os.path.normpath(d)) for d in drop_dirs}
    for key in [k for k in env if k.upper() == "PATH"]:
        parts = [p for p in env[key].split(os.pathsep) if p and os.path.normcase(os.path.normpath(p)) not in drop]
        env[key] = os.pathsep.join(parts)
    return env


def isolate_git(env: dict[str, str], base: Path) -> None:
    """Keep every git the suite starts inside the base, away from the user's configuration.

    GIT_CEILING_DIRECTORIES is the base's parent, not the base (git never applies a ceiling to
    its own cwd, and `new` looks for an enclosing work tree from the base itself): `new` then
    runs `git init` in the project and `setup` installs the hook there, never in a repository
    around the base (the hook stayed behind, or replaced that repository's own).
    GIT_CONFIG_GLOBAL (a file that does not exist) and GIT_CONFIG_NOSYSTEM: a user's
    core.hooksPath, init.templateDir or commit.gpgsign would change what `setup` installs and
    what the first commit runs.
    """
    env["GIT_CEILING_DIRECTORIES"] = str(base.resolve().parent)
    env["GIT_CONFIG_GLOBAL"] = str(base / "no-global-gitconfig")
    env["GIT_CONFIG_NOSYSTEM"] = "1"


def find_artifact(dist: Path, app: str, backend: str, method: str) -> Path | None:
    """Return the non-empty output dir of a build: dist/<app>-<backend>-<method>[-<platform key>]."""
    stem = f"{app}-{backend}-{method}"
    candidates = [dist / stem]
    if method in ("portable", "flet"):  # portable with runtime = "system" has no -<key> suffix
        candidates = sorted(p for p in dist.glob(f"{stem}-*") if p.is_dir()) + candidates
    return next((c for c in candidates if c.is_dir() and any(c.iterdir())), None)


def smoke_target(artifact: Path, app: str, method: str, windows: bool) -> Path | None:
    """Return the file to run (exe, launcher, .pyz) or install (.whl) inside a build's output dir."""
    if method in ("exe", "nuitka"):
        exe = app + (".exe" if windows else "")
        # nuitka standalone on Linux/macOS names the binary <app>.bin when app == package: an
        # <app> file would take the place of the package folder next to it
        for name in (exe, exe + ".bin") if method == "nuitka" and not windows else (exe,):
            hits = sorted((p for p in artifact.rglob(name) if p.is_file()), key=lambda p: len(p.parts))
            if hits:
                return hits[0]
        return None
    names = {"portable": f"{app}.{'cmd' if windows else 'sh'}", "pyz": f"{app}.pyz"}
    if method in names:
        path = artifact / names[method]
        return path if path.is_file() else None
    if method == "wheel":
        return next(iter(sorted(artifact.glob("*.whl"))), None)
    return None


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*)")


def requirement(text: str) -> tuple[str, str]:
    """(normalized name, version specifier without spaces) of a PEP 508 requirement."""
    m = _REQUIREMENT.match(text)
    return (_norm(m.group(1)), re.sub(r"\s+", "", m.group(2))) if m else (text.strip().lower(), "")


def lock_versions(lock: Path) -> dict[str, set[str]] | None:
    """{normalized name: versions} of the packages of a uv.lock (None when it cannot be read)."""
    try:
        data = tomllib.loads(lock.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    out: dict[str, set[str]] = {}
    for entry in data.get("package", []):
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            out.setdefault(_norm(entry["name"]), set()).add(str(entry.get("version", "")))
    return out


def lock_problems(lock: Path, pins: Mapping[str, str], template_lock: Path | None = None) -> list[str]:
    """How a new project's uv.lock differs from the versions the template was tested with.

    A package the template's own uv.lock holds at one version keeps that version (`__init`
    re-locks the copied lock, and uv keeps what a lock has); every other pin of the preset
    (constraints.txt: every package a project of the preset locks; the ones the template's lock
    lacks are handed to `uv add --constraints`) must be locked at its version.
    """
    locked = lock_versions(lock)
    if locked is None:
        return [f"cannot read {lock.name}"]
    expected = {name: (version, "constraints.txt") for name, version in pins.items()}
    tested = (lock_versions(template_lock) if template_lock is not None else None) or {}
    for name, versions in tested.items():
        if len(versions) == 1 and name in locked:
            expected[name] = (next(iter(versions)), "the template's uv.lock")
    problems: list[str] = []
    for name, (version, source) in sorted(expected.items()):
        have = locked.get(name)
        if have is None:
            problems.append(f"uv.lock does not lock {name} ({source}: {version})")
        elif have != {version}:
            problems.append(f"uv.lock locks {name} {', '.join(sorted(have))}, {source} has {version}")
    return problems


def project_state(root: Path) -> dict[str, str]:
    """{path relative to root: sha256} of the files a round trip must restore (STATE_SKIP_*:
    no environments, builds, caches or .git). A missing root is empty."""
    state: dict[str, str] = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in STATE_SKIP_DIRS and not d.startswith(".venv"))
        for name in files:
            if name.endswith(STATE_SKIP_SUFFIXES):
                continue
            path = Path(folder, name)
            key = path.relative_to(root).as_posix()
            state[key] = "-> " + os.readlink(path) if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def state_changes(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """`~ path` (changed), `+ path` (new) and `- path` (gone) between two project_state results."""
    out = [f"~ {p}" for p in sorted(before.keys() & after.keys()) if before[p] != after[p]]
    out += [f"+ {p}" for p in sorted(after.keys() - before.keys())]
    return out + [f"- {p}" for p in sorted(before.keys() - after.keys())]


def docs_problems(project: Path, app: str, description: str, template: Path = ROOT) -> list[str]:
    """What makes a project made with `new` a program of its own (presets._make_own) and not the
    template: its own README.md (`# <app>`, never the template's myapp), `[project] description`
    = the preset's, no LICENSE at its root, and the template's README and LICENSE kept under
    .pytemplate/ (presets.TEMPLATE_DOCS) with the bytes of the `template` it was made from: its
    root files in the template repository, its own .pytemplate/ copies in a project."""
    from .presets import TEMPLATE_DOCS

    problems: list[str] = []
    try:
        readme = (project / "README.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        problems.append(f"README.md: {e}")
    else:
        if not readme.startswith(f"# {app}\n"):
            problems.append(f"README.md does not start with '# {app}'")
        if "myapp" in readme:
            problems.append("README.md names myapp, the template's own app")
    if os.path.lexists(project / "LICENSE"):
        problems.append("LICENSE in the project root: the template's belongs in .pytemplate/LICENSE")
    template_repo = (template / ".pytemplate" / "template-repo").is_file()
    for name, target in TEMPLATE_DOCS.items():
        source = template / (name if template_repo else target)
        if not source.is_file():
            continue
        try:
            same = (project / target).read_bytes() == source.read_bytes()
        except OSError:
            problems.append(f"no {target} (a copy of the template's {source.relative_to(template).as_posix()})")
            continue
        if not same:
            problems.append(f"{target} differs from the template's {source.relative_to(template).as_posix()}")
    try:
        data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8-sig"))
        have = data.get("project", {}).get("description")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, AttributeError) as e:
        problems.append(f"pyproject.toml: {e}")
    else:
        if have != description:
            problems.append(f"[project] description is {have!r}, not the preset's {description!r}")
    return problems


# --- host and project data -----------------------------------------------------------------------


def preset_info(name: str) -> PresetInfo:
    """Read the preset's own pytemplate.toml (the schema's defaults fill in the rest)."""
    from . import config

    data = tomllib.loads((PRESETS / name / "files" / CONFIG_FILE.name).read_text(encoding="utf-8"))
    cfg: Config = config._build(Config, data, "")
    return PresetInfo(name, tuple(cfg.backend.supported), dict(cfg.deploy.default), cfg.app.gui, cfg.backend.active, tuple(cfg.tasks))


def _home_flutter() -> Path | None:
    """The Flutter of ~/flutter (where flet build installs its own), or None. Path.home() raises
    for a UID without a passwd entry and no HOME (a container's --user 4242): no home, no Flutter."""
    try:
        home = Path.home()
    except RuntimeError:
        return None
    return next(iter(sorted(home.glob("flutter/*/bin/flutter*"))), None)


def flet_build_reason(os_name: str) -> str:
    flutter = shutil.which("flutter") or _home_flutter()
    if not flutter:
        return "needs the Flutter SDK (flet build installs ~3 GB): not in PATH or ~/flutter"
    if os_name == "windows":
        from .methods.flet import _developer_mode

        if not _developer_mode():
            return "needs Windows Developer Mode (Settings > System > For developers)"
    return ""


def detect_host(gui: str) -> Host:
    os_name = host_os()
    display = "--gui off" if gui == "off" else ""
    wrap: tuple[str, ...] = ()
    headless_linux = os_name == "linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if gui != "off" and headless_linux:
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            wrap = (xvfb, "-a", "-s", "-screen 0 1280x720x24")  # depth 24: GLX has no 8-bit visuals
        elif gui == "auto":
            display = "no display (DISPLAY unset) and no xvfb-run"
    elif gui == "auto" and os_name != "linux" and os.environ.get("CI"):
        display = "CI runner without an OpenGL 3.3 context (--gui on forces it)"
    return Host(os_name, display, wrap, flet_build_reason(os_name), host_arch())


def default_base() -> Path:
    """A SHORT path: LongPathsEnabled=0 breaks PyPy runtime copies and Flet client extraction.
    Per user on POSIX (/tmp is shared): `check_private_dir` refuses one another user made."""
    tmp = Path(tempfile.gettempdir())
    return tmp / "pt" / "e2e" if IS_WINDOWS else tmp / scratch_name("pt-e2e")


def child_env(base: Path) -> dict[str, str]:
    """This process's environment, scrubbed and git-isolated, without the runner's own bin/ on
    PATH but with uv's dir."""
    own = [str(Path(sys.prefix) / ("Scripts" if IS_WINDOWS else "bin"))] if sys.prefix != sys.base_prefix else []
    env = scrub_env(os.environ, own)
    uv_dir = str(Path(proc.find_uv()).parent)
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    if not shutil.which("uv", path=env.get(key, "")):
        env[key] = os.pathsep.join(p for p in (uv_dir, env.get(key, "")) if p)
    isolate_git(env, base)
    return env


def _git_top(env: Mapping[str, str]) -> str:
    git = shutil.which("git")
    if git is None:
        return ""
    try:
        r = subprocess.run([git, "rev-parse", "--show-toplevel"], cwd=ROOT, env=dict(env), stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def hidden_template_repository(env: Mapping[str, str]) -> str:
    """The template's repository when isolate_git hides it from the template itself (the base's
    parent lies between the template and its repository root: a base next to a template that
    is a subfolder of a bigger repository), else "". `new` would then copy the files git does
    not track too."""
    top = _git_top({k: v for k, v in env.items() if k != "GIT_CEILING_DIRECTORIES"})
    return top if top and not _git_top(env) else ""


# --- running steps --------------------------------------------------------------------------------


@dataclass
class Result:
    preset: str
    step: str
    status: str  # PASS | FAIL | SKIP
    seconds: float = 0.0
    detail: str = ""
    log: str = ""  # relative to the base dir


@dataclass
class Context:
    info: PresetInfo
    base: Path
    project: Path
    logs: Path
    work: Path  # scratch for smoke runs (wheel venvs, pyz cache, the moved portable folder)
    uv: str
    env: dict[str, str]
    opts: Options
    results: dict[str, str] = field(default_factory=dict)  # step name -> status
    states: dict[str, dict[str, str]] = field(default_factory=dict)  # snapshot key -> project_state


def rmtree(path: Path) -> None:
    """Remove a tree even with read-only files (.git objects) and paths over 260 characters. A
    symlink or a junction (a --base on another disk) goes as a link, never what it names."""
    from .cmd_env import _is_link  # imported here: cmd_env imports much this module never needs

    if _is_link(path):
        os.unlink(path)  # on Windows this also removes a directory symlink or a junction
        return
    if not path.exists():
        return
    from .methods.portable import long_path  # \\?\C:\... or, for a share, \\?\UNC\server\...

    target = long_path(path) if IS_WINDOWS else str(path)

    def retry(func: Callable[..., object], name: str, exc: object) -> None:
        error = exc[1] if isinstance(exc, tuple) else exc  # onerror's exc_info (3.11), onexc's exception
        if isinstance(error, BaseException) and os.path.islink(name):
            raise error  # chmod follows a link: it made the folder a symlinked base names 0o200
        os.chmod(name, stat.S_IWRITE)
        func(name)

    for attempt in range(5):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(target, onexc=retry)
            else:
                shutil.rmtree(target, onerror=retry)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(1 + attempt)  # a just-exited exe or an antivirus scan may still hold a file


def kill_tree(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)], stdin=subprocess.DEVNULL, capture_output=True, check=False)
    else:
        import signal

        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        child.kill()
    except OSError:
        pass
    child.wait()


def run_logged(argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path, timeout: float) -> tuple[int | None, str]:
    """Run `argv` (stdin closed): stderr goes to `log`, stdout to `log` and the returned text.

    Files, not pipes: a grandchild that outlives its parent cannot hang the wait. The exit
    code is None on a timeout; a timeout or an interrupt (Ctrl+C, SIGTERM through
    termination_as_interrupt) kills the whole process tree first.
    """
    out_path = log.with_suffix(".out")
    code: int | None = None
    data = b""
    try:
        with log.open("ab") as err, out_path.open("w+b") as out:
            err.write(f"$ {proc.show(list(argv))}\n  (in {cwd})\n".encode())
            err.flush()
            child = subprocess.Popen(list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=not IS_WINDOWS)
            end = "interrupted"
            try:
                code = child.wait(timeout=timeout)
                end = str(code)
            except subprocess.TimeoutExpired:
                kill_tree(child)
                end = "timeout"
            except BaseException:
                kill_tree(child)
                raise
            finally:
                out.seek(0)
                data = out.read()
                if data:
                    err.write(b"--- stdout ---\n" + data + (b"" if data.endswith(b"\n") else b"\n"))
                err.write(f"--- exit code: {end}\n\n".encode())
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass  # still held by a killed process's orphan: it goes with the base dir
    return code, data.decode("utf-8", errors="replace")


def call(argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path, timeout: float, expect: Sequence[str] = (), code_ok: int = 0) -> tuple[str, str]:
    code, stdout = run_logged(argv, cwd, env, log, timeout)
    if code is None:
        return FAIL, f"timeout after {timeout:.0f} s"
    if code != code_ok:
        return FAIL, f"exit code {code}" + (f", expected {code_ok}" if code_ok else "")
    missing = [e for e in expect if e not in stdout]
    if missing:
        return FAIL, f"exit code {code} but the output lacks {missing[0]!r}"
    return PASS, ""


def runner_argv(uv: str, root: Path, args: Sequence[str]) -> list[str]:
    """./pyt ARGS of the project at `root`, the way its launchers run it (proc.runner_argv)."""
    return proc.runner_argv(uv, root, args)


def _log_note(log: Path, text: str) -> None:
    with log.open("a", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip("\n") + "\n")


def _git(ctx: Context, log: Path, timeout: float, *args: str) -> tuple[int | None, str]:
    git = shutil.which("git") or "git"
    return run_logged([git, *args], ctx.project, ctx.env, log, timeout)


def _shown(items: Sequence[str], limit: int = 4) -> str:
    return ", ".join(items[:limit]) + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def do_new(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    if ctx.opts.reuse and (ctx.project / CONFIG_FILE.name).is_file():
        return SKIP, f"reusing {ctx.project} (--reuse)"
    rmtree(ctx.project)
    argv = runner_argv(ctx.uv, ROOT, ["new", str(ctx.project), "--preset", ctx.info.name, "--name", ctx.info.app])
    return call(argv, ROOT, ctx.env, log, step.timeout)


def do_verify(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    """What `new` must (not) copy, its git repository with ./pyt and pyt.ps1 executable,
    the versions it locked (lock_problems) and what makes the project its own (docs_problems)."""
    from . import presets

    if ctx.results.get("new") == SKIP:
        return SKIP, "--reuse: new did not run"
    p = ctx.project
    problems: list[str] = []
    notes: list[str] = []
    if (p / ".pytemplate" / "template-repo").exists():
        problems.append("copied .pytemplate/template-repo")
    problems += [f"copied .github/workflows/{w.name}" for w in sorted((p / ".github" / "workflows").glob("template-*"))]
    for name in sorted(os.listdir(p)):
        if name in NEVER_COPIED or name.startswith(".venv"):
            made = MADE_BY_INIT.get(name)
            if made is None or not (p / name).is_dir() or not set(os.listdir(p / name)) <= made:
                problems.append(f"copied {name}/")
    if not (p / "pyt").is_file():
        problems.append("no ./pyt launcher")
    git = shutil.which("git")
    if git is None:
        notes.append("git not found: the repository and the exec bits are not checked")
    elif not (p / ".git").exists():
        problems.append("no git repository (new runs git init)")
    else:
        r = subprocess.run([git, "ls-files", "-s", "--", "pyt", "pyt.ps1"], cwd=p, env=ctx.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=step.timeout)
        _log_note(log, f"git ls-files -s pyt pyt.ps1:\n{r.stdout.strip() or r.stderr.strip()}")
        modes = {line.split("\t", 1)[1]: line.split()[0] for line in r.stdout.splitlines() if "\t" in line}
        for launcher in ("pyt", "pyt.ps1"):
            mode = modes.get(launcher, "missing from the index")
            if mode != "100755":
                problems.append(f"git mode of {launcher} is {mode}, not 100755")
    problems += lock_problems(p / "uv.lock", presets.constraints(ctx.info.name), presets.LOCK)
    problems += docs_problems(p, ctx.info.app, str(presets.load(ctx.info.name).get("description", "")))
    _log_note(log, "\n".join(problems + notes) or "all checks passed")
    return (FAIL, "; ".join(problems)) if problems else (PASS, "; ".join(notes))


def do_commit(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    """`git add -A` and `git commit` (message: step.args[0]) through the pre-commit hook `setup`
    installed: the user's first commit, and later the commit of a renamed project. Nothing to
    commit fails (the step before changed nothing), except a first commit under --reuse."""
    from .hooks import HOOK, MARKER as HOOK_MARKER

    if shutil.which("git") is None:
        return SKIP, "git not found"
    if not (ctx.project / ".git").exists():
        return FAIL, "no git repository"
    code, out = _git(ctx, log, step.timeout, "rev-parse", "--git-path", "hooks")
    hook = ctx.project / out.strip() / HOOK
    try:
        installed = code == 0 and HOOK_MARKER in hook.read_text(encoding="utf-8", errors="replace")
    except OSError:
        installed = False
    if not installed:
        return FAIL, f"setup installed no pytemplate {HOOK} hook ({hook})"
    code, pending = _git(ctx, log, step.timeout, "status", "--porcelain")
    if code == 0 and not pending.strip():
        if ctx.results.get("new") == SKIP:
            return PASS, "nothing to commit (--reuse: committed by an earlier run)"
        return FAIL, "nothing to commit"
    commit = (*GIT_IDENTITY, "commit", "--quiet", "-m", step.args[0] if step.args else step.name)
    for label, args in (("git add -A", ("add", "-A")), ("git commit", commit)):
        code, _ = _git(ctx, log, step.timeout, *args)
        if code is None:
            return FAIL, f"{label}: timeout after {step.timeout:.0f} s"
        if code != 0:
            return FAIL, "the pre-commit hook or git refused the commit (see the log)" if label == "git commit" else f"{label}: exit code {code}"
    code, left = _git(ctx, log, step.timeout, "status", "--porcelain")
    if code != 0 or left.strip():
        return FAIL, f"not committed: {_shown(left.splitlines())}"
    return PASS, ""


def option_requirements(project: Path, preset: str) -> dict[str, str]:
    """{normalized name: version} of the preset requirements its options drive ({version},
    {package}), as the project's pytemplate.toml [preset.<name>] sets them."""
    from . import presets

    data = tomllib.loads((project / CONFIG_FILE.name).read_text(encoding="utf-8-sig"))
    table = data.get("preset", {}).get(preset, {})
    deps, dev = presets.option_dependencies(preset, {**presets.default_options(preset), **table})
    out: dict[str, str] = {}
    for req in deps + dev:
        name, spec = requirement(req)
        out[name] = spec.removeprefix("==")
    return out


def requirement_problems(project: Path, before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """How pyproject.toml and uv.lock fail to follow an option edit: `after` must be declared and
    locked at its versions, the names only `before` had must be gone from both."""
    try:
        data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return [f"cannot read pyproject.toml: {e}"]
    reqs = [*data.get("project", {}).get("dependencies", []), *data.get("dependency-groups", {}).get("dev", [])]
    declared = dict(requirement(r) for r in reqs if isinstance(r, str))
    locked = lock_versions(project / "uv.lock")
    if locked is None:
        return ["cannot read uv.lock"]
    problems: list[str] = []
    for name, version in sorted(after.items()):
        if declared.get(name) != f"=={version}":
            problems.append(f"pyproject.toml declares {name} {declared.get(name, 'nowhere')}, not =={version}")
        if locked.get(name) != {version}:
            problems.append(f"uv.lock locks {name} {', '.join(sorted(locked.get(name, set()))) or 'nowhere'}, not {version}")
    for name in sorted(before.keys() - after.keys()):
        if name in declared:
            problems.append(f"pyproject.toml still declares {name}")
        if name in locked:
            problems.append(f"uv.lock still locks {name}")
    return problems


def do_option(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    """Edit an [preset.<name>] option like a user (config.set_value keeps the layout), run
    `./pyt apply`, and check that pyproject.toml and uv.lock follow."""
    from . import config

    table, key, value = step.args
    path = ctx.project / CONFIG_FILE.name
    before = option_requirements(ctx.project, ctx.info.name)
    path.write_bytes(config.set_value(path.read_bytes().decode("utf-8"), table, key, value).encode("utf-8"))
    _log_note(log, f"{CONFIG_FILE.name}: [{table}] {key} = {config.toml_value(value)}")
    status, detail = call(runner_argv(ctx.uv, ctx.project, ("apply",)), ctx.project, ctx.env, log, step.timeout)
    if status != PASS:
        return status, detail
    after = option_requirements(ctx.project, ctx.info.name)
    problems = requirement_problems(ctx.project, before, after)
    _log_note(log, "\n".join(problems) or f"pyproject.toml and uv.lock follow: {', '.join(f'{n}=={v}' for n, v in sorted(after.items()))}")
    return (FAIL, "; ".join(problems)) if problems else (PASS, ", ".join(f"{n}=={v}" for n, v in sorted(after.items())))


def do_build(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    from .methods.common import tree_bytes

    status, detail = call(runner_argv(ctx.uv, ctx.project, step.args), ctx.project, ctx.env, log, step.timeout)
    if status != PASS:
        return status, detail
    app = step.app or ctx.info.app
    artifact = find_artifact(ctx.project / "dist", app, step.backend, step.method)
    if artifact is None:
        return FAIL, f"the build passed but dist/{app}-{step.backend}-{step.method}* is missing or empty"
    size = tree_bytes(artifact)  # a symlinked file (a runtime's bin/python3) once, like the build's own line
    shown = f"{size / 1_048_576:.1f} MB" if size >= 1_048_576 else f"{size / 1024:.0f} KB"
    return PASS, f"{artifact.relative_to(ctx.project).as_posix()} ({shown})"


def runtime_python(ctx: Context, backend: str) -> Path:
    """The interpreter of the e2e PROJECT's environment. The project lives in the base and its own
    runner may compute a different ENV_SUFFIX than this one's: on WSL with a Windows checkout the
    default base is on the Linux file system, so the project's runner made `.venv`, not
    `.venv-wsl` (and the reverse with `--base` on /mnt/c). Take whichever of the two suffixes
    (this runner's first) the project actually made, so the smoke runs use the project's own env."""
    stem = ".venv-pypy" if backend == "pypy" else ".venv"
    for suffix in dict.fromkeys((ENV_SUFFIX, "", "-wsl")):
        python = venv_python(ctx.project / f"{stem}{suffix}")
        if python.exists():
            return python
    return venv_python(ctx.project / f"{stem}{ENV_SUFFIX}")  # neither is there: name the expected one


def _move(src: Path, dst: Path) -> None:
    for attempt in range(3):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)  # Windows: an antivirus scan may still hold a file of the build


def do_smoke(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    """Run the artifact itself, outside the project's environments. A portable folder runs from
    another path while its build folder is gone (moved there and back): it must work wherever
    it is copied."""
    app = step.app or ctx.info.app
    artifact = find_artifact(ctx.project / "dist", app, step.backend, step.method)
    target = smoke_target(artifact, app, step.method, IS_WINDOWS) if artifact else None
    if artifact is None or target is None:
        return FAIL, f"nothing to run in dist/ for {step.method}"
    shown = target.relative_to(ctx.project).as_posix()
    if step.method != "portable":
        status, detail = _smoke(ctx, step, log, target)
        return (status, detail) if status != PASS else (PASS, shown)
    moved = ctx.work / "moved" / artifact.name
    rmtree(moved.parent)
    moved.parent.mkdir(parents=True)
    _move(artifact, moved)
    _log_note(log, f"moved {artifact} -> {moved} for the run")
    try:
        status, detail = _smoke(ctx, step, log, moved / target.relative_to(artifact))
    finally:
        _move(moved, artifact)
    return (status, detail) if status != PASS else (PASS, f"{shown} (run from another folder)")


def _smoke(ctx: Context, step: Step, log: Path, target: Path) -> tuple[str, str]:
    env = dict(ctx.env)
    commands: list[list[str]] = []
    if step.method in ("exe", "nuitka"):
        commands.append([str(target)])
    elif step.method == "portable":
        commands.append([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(target)] if IS_WINDOWS else [str(target)])
    elif step.method == "pyz":
        # -S: no site-packages, so the .pyz must bring its own dependencies. Its bootstrap
        # extracts to the user cache: point that at the scratch dir.
        cache = ctx.work / "cache"
        env.update({"LOCALAPPDATA": str(cache), "XDG_CACHE_HOME": str(cache)})
        if sys.platform == "darwin":
            env["HOME"] = str(ctx.work / "home")
        commands.append([str(runtime_python(ctx, step.backend)), "-S", str(target)])
    elif step.method == "wheel":
        venv = ctx.work / f"wheel-{step.backend}"
        rmtree(venv)
        script = venv_python(venv).parent / ((step.app or ctx.info.app) + (".exe" if IS_WINDOWS else ""))
        commands += [
            [ctx.uv, "venv", "--quiet", "--python", str(runtime_python(ctx, step.backend)), str(venv)],
            [ctx.uv, "pip", "install", "--quiet", "--python", str(venv_python(venv)), str(target)],
            [str(script)],
        ]
    for i, argv in enumerate(commands):
        last = i == len(commands) - 1
        status, detail = call(argv, ctx.work, env, log, step.timeout, step.expect if last else ())
        if status != PASS:
            return status, detail if last else f"{Path(argv[0]).stem} {argv[1]}: {detail}"
    return PASS, ""


def _execute(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    if step.kind == "new":
        return do_new(ctx, step, log)
    if step.kind == "verify":
        return do_verify(ctx, step, log)
    if step.kind == "commit":
        return do_commit(ctx, step, log)
    if step.kind == "option":
        return do_option(ctx, step, log)
    if step.kind == "build":
        return do_build(ctx, step, log)
    if step.kind == "smoke":
        return do_smoke(ctx, step, log)
    argv = [*step.wrap, *runner_argv(ctx.uv, ctx.project, step.args)]
    return call(argv, ctx.project, ctx.env, log, step.timeout, step.expect, step.expect_code)


def execute(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    """Run one step; with `snapshot`/`restores`, also record or compare the project's files."""
    root = ctx.project / step.scope if step.scope else ctx.project
    if step.snapshot:
        ctx.states[step.snapshot] = project_state(root)
    status, detail = _execute(ctx, step, log)
    if status != PASS or not step.restores:
        return status, detail
    before = ctx.states.get(step.restores)
    if before is None:
        return FAIL, f"bug: no snapshot '{step.restores}'"
    changes = state_changes(before, project_state(root))
    if not changes:
        return status, detail
    where = f"{step.scope}/" if step.scope else "the project"
    since = "" if step.restores == step.snapshot else f" since '{step.restores}'"
    _log_note(log, f"--- files of {where} that changed{since} (~ changed, + new, - gone):\n" + "\n".join(changes))
    return FAIL, f"{where} changed{since}: {_shown(changes)}"


def _tail(log: Path, lines: int = 30) -> list[str]:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.rstrip("\n").splitlines()[-lines:]


def run_preset(ctx: Context, steps: list[Step], into: list[Result] | None = None) -> list[Result]:
    """Run the steps of one preset; each Result is also appended to `into` as soon as it is known
    (an interrupted run keeps the rows it has, the running step as FAIL "interrupted")."""
    out: list[Result] = []

    def record(result: Result, step: Step) -> None:
        ctx.results[step.name] = result.status
        out.append(result)
        if into is not None:
            into.append(result)

    blocked = ""
    for i, step in enumerate(steps, 1):
        if step.skip:
            record(Result(step.preset, step.name, SKIP, detail=step.skip), step)
        elif blocked:
            record(Result(step.preset, step.name, SKIP, detail=f"'{blocked}' failed"), step)
        elif step.after and ctx.results.get(step.after) != PASS:
            record(Result(step.preset, step.name, SKIP, detail=f"needs '{step.after}' to pass"), step)
        else:
            log = ctx.logs / f"{i:02d}-{re.sub(r'[^a-z0-9]+', '-', step.name.lower()).strip('-')[:40]}.log"
            ui.step(f"e2e {step.preset}: {step.name}")
            t0 = time.perf_counter()
            try:
                status, detail = execute(ctx, step, log)
            except (OSError, subprocess.SubprocessError, PytError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
                status, detail = FAIL, f"{type(e).__name__}: {e}"
                _log_note(log, detail)
            except KeyboardInterrupt:
                record(Result(step.preset, step.name, FAIL, round(time.perf_counter() - t0, 1), "interrupted", log.relative_to(ctx.base).as_posix()), step)
                raise
            result = Result(step.preset, step.name, status, round(time.perf_counter() - t0, 1), detail, log.relative_to(ctx.base).as_posix())
            # A failure and where its log is are results, shown even with -q (ui.report)
            shown = ui.report if status == FAIL else ui.info
            shown(f"     {status} in {result.seconds:.1f} s" + (f": {detail}" if detail else "") + (f" ({step.preset}: {step.name})" if status == FAIL else ""))
            if status == FAIL:
                for line in _tail(log):
                    ui.report(f"     | {line}")
                ui.report(f"     full log: {log}")
            record(result, step)
            if status == FAIL and step.required:
                blocked = step.name
    return out


# --- entry point ----------------------------------------------------------------------------------


def _prepare_base(base: Path) -> None:
    if base.resolve() == ROOT or ROOT in base.resolve().parents:
        raise PytError("selftest --e2e: the base dir cannot be inside this template")
    if base.exists() and not base.is_dir():
        raise PytError(f"selftest --e2e: {base} is not a directory")
    check_private_dir(base, "--base")
    if base.is_dir() and any(base.iterdir()) and not (base / MARKER).is_file():
        raise PytError(f"selftest --e2e: {base} is not empty and was not made by selftest --e2e (no {MARKER}): pick another --base")
    try:  # a --base below a file, in a folder it may not write, on a read-only mount
        make_private_dir(base, "--base")
        (base / MARKER).write_text("Made by ./pyt selftest --e2e: safe to delete.\n", encoding="utf-8", newline="\n")
    except OSError as e:  # it was an internal-error traceback, exit 1 (nvimtest's --dir says it too)
        raise PytError(f"selftest --e2e: cannot create --base {base}: {e.strerror or e}") from None


def _cleanup(base: Path, presets: Sequence[str], *, remove_base: bool = True) -> None:
    """Remove what this run made (other presets kept by an earlier --keep stay), then the base if
    empty. `remove_base=False` leaves the base itself: selftest holds a lock file in it and removes
    the base only once the lock is released (`_remove_base`; its file cannot be deleted on Windows
    while held)."""
    try:
        for p in presets:
            for d in (base / p, base / "logs" / p, base / "work" / p):
                rmtree(d)
        for d in (base / "logs", base / "work"):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        if remove_base and {x.name for x in base.iterdir()} <= {MARKER}:
            (base / MARKER).unlink(missing_ok=True)  # through a link too: that folder is the user's
            rmtree(base)  # a symlinked base: only the link goes
    except OSError as e:
        ui.warn(f"could not remove {base}: {e}")


def _remove_base(base: Path) -> None:
    """Remove the base and its lock file once the lock is released, but only when nothing else is
    left (an earlier --keep may have left another preset's projects)."""
    try:
        if base.is_dir() and {x.name for x in base.iterdir()} <= {MARKER, "lock"}:
            rmtree(base)  # unlinks MARKER and lock too; a symlinked base: only the link goes
    except OSError as e:
        ui.warn(f"could not remove {base}: {e}")


def _print_table(results: list[Result], seconds: float) -> None:
    """The table is the answer: shown even with -q (ui.report), like selftest --shells'."""
    ui.step("e2e results")
    width = max([len(r.step) for r in results] + [4])
    ui.report(f"  {'preset':<8} {'step':<{width}}  result     time  detail")
    for r in results:
        time_text = f"{r.seconds:.1f}s" if r.status != SKIP else "-"
        ui.report(f"  {r.preset:<8} {r.step:<{width}}  {r.status:<6} {time_text:>8}  {r.detail}".rstrip())
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, FAIL, SKIP)}
    minutes, secs = divmod(int(seconds), 60)
    ui.report(f"  total: {counts[PASS]} PASS, {counts[FAIL]} FAIL, {counts[SKIP]} SKIP in {minutes}m{secs:02d}s")


@contextmanager
def termination_as_interrupt() -> Iterator[None]:
    """POSIX, main thread: SIGTERM and SIGHUP raise KeyboardInterrupt while the suite runs, like
    Ctrl+C, then the handlers go back to what they were.

    Steps run in their own session (start_new_session), so a signal sent to the suite's process
    group (`timeout`, a closed terminal, uv forwarding it) never reaches them: without this the
    runner died at once and the running step (a build) went on as an orphan, writing into the
    base dir. The first signal also ignores the next ones, so the tree kill and the report run.
    """
    saved: list[tuple[int, Any]] = []
    if sys.platform != "win32":
        import signal

        watched = (signal.SIGTERM, signal.SIGHUP)

        def interrupt(signum: int, frame: object) -> None:
            for sig in watched:
                signal.signal(sig, signal.SIG_IGN)
            raise KeyboardInterrupt

        if threading.current_thread() is threading.main_thread():  # signal.signal works only there
            for sig in watched:
                saved.append((sig, signal.signal(sig, interrupt)))
    try:
        yield
    finally:
        if sys.platform != "win32":
            import signal

            for signum, handler in saved:
                signal.signal(signum, signal.SIG_DFL if handler is None else handler)


def selftest(cfg: Config, args: list[str]) -> int:
    """selftest --e2e [PRESET ...] [--backends ..] [--methods ..] [--quick|--full] [--gui ..] [--keep] [--reuse] [--json] [--base DIR]"""
    from . import presets

    opts = parse_args(args, presets.available())
    base = user_path(opts.base) if opts.base else default_base()
    host = detect_host(opts.gui)
    plans = [(info, plan(info, opts, host)) for info in (preset_info(name) for name in opts.presets)]
    problem = selection_problem(plans, opts)
    if problem:  # before anything is created
        raise PytError(f"selftest --e2e: {problem}")
    env = child_env(base)
    hidden = hidden_template_repository(env)
    if hidden:
        raise PytError(
            f"selftest --e2e: the base {base} is next to this template inside its git repository {hidden}: "
            "git in the base must not see a repository around it (./pyt new would skip git init), which would "
            "hide the template's own. Pick a --base outside that repository"
        )
    _prepare_base(base)
    # One run at a time per base: a second run's do_new/rmtree would delete the projects and logs
    # this one is building. _prepare_base above is non-destructive (mkdir + marker), so a second
    # run is refused here. _cleanup removes only this run's projects under the lock; the base and
    # its lock file go after it is released (_remove_base: the file cannot be deleted on Windows
    # while held).
    with base_lock(base, "selftest --e2e"):
        uv = proc.find_uv()
        ui.step(f"selftest --e2e: {', '.join(opts.presets)} in {base}" + (" (--quick)" if opts.quick else " (--full)" if opts.full else ""))
        results: list[Result] = []
        interrupted = False
        t0 = time.perf_counter()
        with termination_as_interrupt():
            try:
                for info, steps in plans:
                    ctx = Context(info, base, base / info.name, base / "logs" / info.name, base / "work" / info.name, uv, env, opts)
                    for d in (ctx.logs, ctx.work):
                        rmtree(d)
                        d.mkdir(parents=True)
                    run_preset(ctx, steps, results)
            except KeyboardInterrupt:
                interrupted = True
        seconds = time.perf_counter() - t0
        failed = interrupted or any(r.status == FAIL for r in results)
        if results:
            _print_table(results, seconds)
        kept = failed or opts.keep
        if kept:
            ui.report(f"kept for inspection: {base}  (logs in {base / 'logs'})")
        else:
            _cleanup(base, opts.presets, remove_base=False)
        if opts.as_json:
            report = {
                "ok": not failed,
                "interrupted": interrupted,
                "base": str(base),
                "kept": kept,
                "seconds": round(seconds, 1),
                "host": asdict(host),
                "options": asdict(opts),
                "results": [asdict(r) for r in results],
            }
            print(json.dumps(report, indent=2))
        if interrupted:
            ui.error("interrupted")
            code = 130
        elif failed:
            ui.error("selftest --e2e: some steps failed (table above)")
            code = 1
        else:
            ui.ok("selftest --e2e: everything passed")
            code = 0
    if not kept:  # the lock is released and its fd closed: remove the base and its lock file
        _remove_base(base)
    return code
