"""./pyt: Justfile-style runner, no extra binaries (only uv).

    ./pyt [-v|-q] [--dry-run] [--no-render] COMMAND [args...]

Global options go BEFORE the command; everything after it belongs to the command
(and for `run`/`test`, to your app or to pytest). Outside a project (global mode: the `pyt`
that `pyt install` puts on PATH, run from a folder of no project) only GLOBAL_COMMANDS run.
"""

from __future__ import annotations

import errno
import importlib
import os
import re
import signal
import sys
import textwrap
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import proc, project, ui
from .project import BUILD, DIST, rel
from .ui import PytError

if TYPE_CHECKING:
    from .config import TaskConfig


@dataclass(frozen=True)
class Command:
    module: str
    func: str
    summary: str
    usage: str = ""
    render: bool = True  # regenerate configs before running it
    group: str = ""


COMMANDS: dict[str, Command] = {
    # environment
    "setup": Command("cmd_env", "cmd_setup", "First time on a clone: the same as apply (interpreters, environments, uv.lock, hook)", "[--force]", render=False, group="Environment"),
    "apply": Command("cmd_apply", "cmd_apply", "Apply every pytemplate.toml change (rename, dependencies, uv.lock, envs, hook, configs)", "[--force]", render=False, group="Environment"),
    "doctor": Command("cmd_env", "cmd_doctor", "Check uv, compiler, PyPy, shells and generated files", group="Environment"),
    "sync": Command("cmd_env", "cmd_sync", "Run uv sync --locked on one or all environments", "[cpython|pypy|mypyc|all]", group="Environment"),
    "lock": Command("cmd_env", "cmd_lock", "Apply the managed parts of pyproject and re-lock uv.lock", "[--upgrade] [--upgrade-package PKG]", group="Environment"),
    "add": Command("cmd_env", "cmd_add", "Add dependencies (uv add)", "PKG... [--dev|--group G] [--cpython-only]", group="Environment"),
    "remove": Command("cmd_env", "cmd_remove", "Remove dependencies (uv remove)", "PKG... [--dev|--group G]", group="Environment"),
    "clean": Command("cmd_env", "cmd_clean", "Remove .build/ and dist/ (and the environments with --envs)", "[--envs]", render=False, group="Environment"),
    "hooks": Command("hooks", "cmd_hooks", "Install or remove the git pre-commit hook, or run its checks on the staged files", "[install [--force]|uninstall|run|status]", render=False, group="Environment"),
    # mode and template
    "mode": Command("cmd_mode", "cmd_mode", "Show or change the mode (backend, supported, typing, editor)", "[BACKEND] [--supports +B|-B|B,B...] [--typing auto|off|warn|strict|mypyc] [--editor pylance|basedpyright]", group="Mode"),
    "render": Command("cmd_mode", "cmd_render", "Regenerate the files derived from pytemplate.toml (.python-version, .mypy.ini, .ruff.toml, pyrightconfig.json, .vscode/, .lazy.lua, editor.json, ci.yml)", "[--check] [--diff] [--force]", render=False, group="Mode"),
    "rename": Command("rename", "cmd_rename", "Rename the app: src/<pkg>, imports, pytemplate.toml, pyproject.toml, uv.lock", "NEW_NAME [--force]", render=False, group="Mode"),
    "new": Command("cmd_mode", "cmd_new", "Create a new project from this template", "DIR [--preset P] [--name NAME]", render=False, group="Mode"),
    "install": Command("cmd_install", "cmd_install", "Install the `pyt` command for use in any folder (run it in a clone of the template)", render=False, group="Mode"),
    "uninstall": Command("cmd_install", "cmd_uninstall", "Remove the `pyt` command and the copy of the template that install made", render=False, group="Mode"),
    # development
    "run": Command("cmd_dev", "cmd_run", "Run the app (mypyc: compile first)", "[BACKEND] [app args...]", group="Development"),
    "check": Command("cmd_dev", "cmd_check", "Run ruff + mypy with the backend's typing profile + mypyc rules", "[BACKEND|all]", group="Development"),
    "lint": Command("cmd_dev", "cmd_lint", "ruff check", "[--fix]", group="Development"),
    "fmt": Command("cmd_dev", "cmd_fmt", "ruff format", "[--check]", group="Development"),
    "test": Command("cmd_dev", "cmd_test", "Run pytest on one backend (mypyc: against the .pyd files) or on all of them", "[BACKEND|all] [pytest args...]", group="Development"),
    "report": Command("cmd_dev", "cmd_report", "Generate the mypyc HTML report of slow lines + mypy's Any reports", "[--open] [--no-mypy]", group="Development"),
    "compile": Command("cmd_dev", "cmd_compile", "Compile the mypyc stage without running it (for debuggers and editors)", "[--release]", group="Development"),
    # distribution
    "build": Command("cmd_build", "cmd_build", "Compile and package into dist/", "[BACKEND] [--method exe|portable|pyz|wheel|nuitka|flet] [--onefile|--onedir] [--target KEY]... [--no-check]", group="Distribution"),
    "pyz-merge": Command("cmd_build", "cmd_pyz_merge", "Merge the .pyz files of each OS (e.g. from CI) into a cross-platform one", "A.pyz B.pyz... --out C.pyz", render=False, group="Distribution"),
    # other
    "tasks": Command("cli", "cmd_tasks", "List the custom tasks in pytemplate.toml [tasks]", render=False, group="Other"),
    "nvim": Command("cmd_nvim", "cmd_nvim", "Neovim/LazyVim integration: check it, trust .lazy.lua, enable extras, sync plugins", "[doctor|trust|extras|bootstrap|sync]", group="Other"),
    "selftest": Command("cli", "cmd_selftest", "Run the runner's own tests and mypy --strict (.pytemplate)", "[--shells|--nvim|--e2e|--mutation] [args...]", render=False, group="Other"),
    "help": Command("cli", "cmd_help", "Show this help (or a command's help)", "[COMMAND]", render=False, group="Other"),
}

# Internal routes: dispatched like COMMANDS but never listed (help, editor.json, the editors'
# task lists and their completion read COMMANDS only). Task names cannot start with "_".
INTERNAL: dict[str, Command] = {
    # `./pyt new` runs it in the fresh copy; the template maintainer regenerates the template
    # root with it (./pyt __init script --name myapp --force). Users pick a preset with `new`.
    # render=False: it renders with --force at its end, and rendering the copied configuration
    # first only warned about the source project's hand-edited files inside the output of `new`.
    "__init": Command("cmd_mode", "cmd_init", "Replace src/, tests/, typings/ and pytemplate.toml with a preset's skeleton", "PRESET [--name NAME] [--force]", render=False),
}

EXAMPLES = """\
Examples:
  ./pyt setup                 # first time: interpreters, environments and configs
  ./pyt apply                 # after editing pytemplate.toml: apply every change
  ./pyt run                   # run with the active backend (pytemplate.toml)
  ./pyt run mypyc --verbose   # compile with mypyc and run (--verbose goes to your app)
  ./pyt test all              # pytest on every supported backend
  ./pyt mode mypyc            # change the active backend (strict typing)
  ./pyt mode --supports +pypy # add PyPy (3.11 syntax)
  ./pyt build mypyc           # exe with PyInstaller (mypyc's default method)
  ./pyt build pypy            # portable folder with PyPy bundled
  ./pyt build cpython --method pyz
  ./pyt nvim doctor           # check the LazyVim integration (./pyt nvim trust once)"""

# Global mode (project.GLOBAL: the launchers found no project and run the installed template):
# the commands that run there, in the order `pyt help` lists them. install and uninstall put the
# launchers on PATH and take them off again. Every other command, internal route and name (a
# project's [tasks] entry) exits 2: it needs a project (_outside_a_project).
GLOBAL_COMMANDS = ("new", "doctor", "install", "uninstall", "help")
# What a global command does outside a project, where it differs from its summary (install runs
# only in a clone of the template: outside a project it installs nothing and says so)
GLOBAL_SUMMARIES = {
    "doctor": "Check this machine: uv, the runner's Python, git, the C compiler of mypyc, Neovim, the installed pyt",
    "install": "Say how to install or update pyt: install runs in a clone of the template (from here it installs nothing)",
}
NEEDS_A_PROJECT = "run it in a project folder (any subfolder works), or create one: pyt new DIR [--preset P]"


def _prog() -> str:
    """How the user started this run: `pyt` (on PATH) outside a project, `./pyt` in one."""
    return "pyt" if project.GLOBAL else "./pyt"


INIT_OUTSIDE = "init is no longer a pyt command: create a project with pyt new DIR [--preset P]"


def _outside_a_project(name: str) -> str:
    """The error of `pyt NAME` in global mode, for a NAME that is not a GLOBAL_COMMANDS one."""
    from . import config

    if name == "init":
        return INIT_OUTSIDE
    if name in config.RETIRED_COMMANDS:
        return _retired(name)
    if name in COMMANDS or name in INTERNAL:
        return f"`pyt {name}` needs a project: {NEEDS_A_PROJECT}"
    return _unknown_outside(name)


def _retired(name: str) -> str:
    """The error of a command removed since the pytemplate.toml contract began (rule 1.11)."""
    from . import config

    return f"{name} is no longer a {_prog()} command: {config.RETIRED_COMMANDS[name]}"


def _unknown_outside(name: str) -> str:
    """A name that is no command: a [tasks] entry of some project (never looked up outside one:
    the installed template's own tasks are not the user's), or a typo."""
    here = " | ".join(n for n in GLOBAL_COMMANDS if n in COMMANDS)
    return f"unknown command: {name}  (outside a project pyt runs {here}; a project's [tasks] entries run in its folder, any subfolder works)"


def _help_outside_a_project() -> int:
    """`pyt help` in global mode: what runs here, and the commands that need a project."""
    from . import presets

    print("pyt [-v|-q] [--dry-run] COMMAND [args...]   (outside a project)\n")
    print("Outside a project, pyt runs:")
    for name in (n for n in GLOBAL_COMMANDS if n in COMMANDS):
        print(f"  {name:<12} {GLOBAL_SUMMARIES.get(name, COMMANDS[name].summary)}")
    print()
    print("Every other command needs a project: run pyt in a project folder (any subfolder works),")
    print("or create one first (their help: pyt help COMMAND):")
    others = " ".join(n for n in COMMANDS if n not in GLOBAL_COMMANDS)
    print(textwrap.fill(others, width=92, initial_indent="  ", subsequent_indent="  "))
    print()
    print("Examples:")
    print("  pyt new game                   # a new project in ./game (the script preset)")
    print(f"  pyt new game --preset raylib   # another preset: {' | '.join(presets.available())}")
    print("  pyt doctor                     # what this machine has for pytemplate projects")
    return 0


# Commands whose extra arguments belong to someone else: `run` -> the app, `test` -> pytest,
# `lock` -> `uv lock`, `selftest` -> pytest (or its --shells/--nvim/--e2e suite), `build` -> the
# packager (unknown flags only). Every other command rejects an unknown argument (exit 2).
FORWARDS = frozenset({"run", "test", "lock", "selftest", "build"})
# `-h`/`--help` after these goes to the app, pytest, uv or the suite; after any other command it
# shows `./pyt help COMMAND` (also after a [tasks] entry that only has deps).
HELP_PASSES_THROUGH = frozenset({"run", "test", "lock", "selftest"})
HELP_FLAGS = ("-h", "--help")
# pytest options that only print (plain `selftest` then skips its mypy step)
SELFTEST_INFO_FLAGS = (*HELP_FLAGS, "--version", "-V")


def _asks_help(args: list[str]) -> bool:
    return any(a in HELP_FLAGS for a in args)


def _print_task(name: str, task: TaskConfig) -> None:
    from . import tasks

    print(f"./pyt {name}{' [args...]' if task.cmd else ''}")
    print(f"  {tasks.describe(task)}")
    if task.cmd:
        print(f"  cmd      {proc.show(task.cmd)}   (extra arguments are appended)")
    if task.deps:
        print(f"  deps     {', '.join(task.deps)}" + ("" if task.cmd else "   (it only runs its deps: no arguments)"))
    if task.cmd:
        where = f"the {task.backend} environment" if task.backend else "the active backend's environment"
        print(f"  runs     {'with uv run in ' + where if task.uv else 'the program as-is (uv = false)'}")
    if task.cwd:
        print(f"  cwd      {task.cwd}")
    for key, value in task.env.items():
        print(f"  env      {key}={value}")
    if task.background:
        print("  background: a long-running server (editors start it without waiting)")


def _task_named_like(cfg: object, name: str) -> TaskConfig | None:
    """The project's [tasks] entry named like a builtin added after the contract (config.
    CONTRACT_COMMANDS), which keeps the name in its project; None otherwise, outside a project,
    and when pytemplate.toml does not load (the builtin's help then)."""
    from . import config

    if name not in COMMANDS or name in config.CONTRACT_COMMANDS or project.GLOBAL:
        return None
    try:
        loaded = cfg if isinstance(cfg, config.Config) else config.load(set(COMMANDS))
    except PytError:
        return None
    return loaded.tasks.get(name)


# `./pyt init` and `./pyt help init` (unless a [tasks] entry took the name)
INIT_REMOVED = "init is no longer a ./pyt command. To start from another preset: ./pyt new DIR --preset P"


def cmd_help(cfg: object, args: list[str]) -> int:
    """help [COMMAND]: every command and task, or one of them. Outside a project (global mode)
    the commands that run there; a project command's help then says it needs a project."""
    from . import config

    names = [a for a in args if a not in HELP_FLAGS] or (["help"] if args else [])  # help -h: this one
    if len(names) > 1:
        raise PytError(f"help: unrecognized arguments: {' '.join(names[1:])}  ({_prog()} help [COMMAND])")
    if names:
        name = names[0]
        shadowing = _task_named_like(cfg, name)
        if shadowing is not None:  # a builtin added after the contract: this project's task keeps the name
            _print_task(name, shadowing)
            print(f"  (this project's [tasks] entry: the built-in `pyt {name}` runs outside the project: {COMMANDS[name].summary})")
            return 0
        if name in COMMANDS:
            c = COMMANDS[name]
            print(f"{_prog()} {name} {c.usage}".rstrip())
            print(f"  {GLOBAL_SUMMARIES.get(name, c.summary) if project.GLOBAL else c.summary}")
            if project.GLOBAL and name not in GLOBAL_COMMANDS:
                print(f"  Needs a project: {NEEDS_A_PROJECT}")
            return 0
        if project.GLOBAL:  # the installed template's own [tasks] are not the user's: never read
            if name in config.RETIRED_COMMANDS:  # what replaced it, as typing it says
                raise PytError(_retired(name))
            raise PytError(INIT_OUTSIDE if name == "init" else _unknown_outside(name))
        # Not a builtin: a [tasks] entry, a typo, or a pytemplate.toml that does not load (that
        # error is the answer then: it says why the task is unknown)
        loaded = cfg if isinstance(cfg, config.Config) else config.load(set(COMMANDS))
        if name in loaded.tasks:
            _print_task(name, loaded.tasks[name])
            return 0
        if name == "init":
            raise PytError(INIT_REMOVED)
        if name in config.RETIRED_COMMANDS:  # what replaced it, as typing it says (dispatch)
            raise PytError(_retired(name))
        raise PytError(f"unknown command: {name}  (./pyt help lists the commands and tasks)")
    if project.GLOBAL:
        return _help_outside_a_project()
    print("./pyt [-v|-q] [--dry-run] [--no-render] COMMAND [args...]\n")
    groups: dict[str, list[str]] = {}
    for name, c in COMMANDS.items():
        groups.setdefault(c.group, []).append(name)
    for group, names in groups.items():
        print(f"{group}:")
        for name in names:
            print(f"  {name:<12} {COMMANDS[name].summary}")
        print()
    try:
        from . import tasks

        loaded = config.load(set(COMMANDS))
        if loaded.tasks:
            print("Custom tasks (pytemplate.toml [tasks]):")
            for name, task in loaded.tasks.items():
                note = "  (runs instead of the built-in command here)" if name in COMMANDS else ""
                print(f"  {name:<12} {tasks.describe(task)}{note}")
            print()
    except PytError as e:  # help still prints (stdout); say why the tasks are missing (stderr)
        ui.warn(f"{e}\n  (so the custom tasks of pytemplate.toml are not listed)")
    print("BACKEND = cpython | pypy | mypyc (default: backend.active from pytemplate.toml)\n")
    print(EXAMPLES)
    return 0


def cmd_tasks(cfg: object, args: list[str]) -> int:
    from . import tasks
    from .config import Config

    assert isinstance(cfg, Config)
    if args:
        raise PytError(f"tasks: unrecognized arguments: {' '.join(args)}  (it takes no arguments)")
    tasks.list_tasks(cfg)
    return 0


def cmd_selftest(cfg: object, args: list[str]) -> int:
    from . import e2e, envs, mutation, nvimtest, shells
    from .config import Config
    from .project import ROOT, TEMPLATE

    assert isinstance(cfg, Config)
    suites: dict[str, Callable[[Config, list[str]], int]] = {
        "--shells": shells.selftest,  # every launcher through every installed shell
        "--nvim": nvimtest.selftest,  # the LazyVim integration in an isolated LazyVim
        "--e2e": e2e.selftest,  # ./pyt new + setup/check/test/build per preset
        "--mutation": mutation.selftest,  # Cosmic Ray's mutants of the runner against its tests
    }
    if args and args[0] in suites:
        if proc.DRY_RUN:
            # The suites start their shells, Neovim and ./pyt runs themselves (not through
            # proc.run), in scratch folders: nothing of them can be skipped and still mean anything
            raise PytError(
                f"selftest {args[0]} has no --dry-run: it runs real shells, projects and builds, "
                "only in its own scratch folders; run it without --dry-run"
            )
        return suites[args[0]](cfg, args[1:])
    tool = envs.tool_env(cfg)
    # The suite's own settings (-c): pytest looked for them itself and read the project's (its
    # pyproject.toml, pytest.ini, tox.ini or setup.cfg, and its root conftest.py), so a coverage
    # gate, python_files or a plugin the project gives its app's tests failed the runner's.
    # --rootdir=. (the project folder, the cwd) keeps the test ids: .pytemplate/tests/test_x.py::y
    suite: list[str | Path] = ["-c", TEMPLATE / "tests" / "pytest.ini", "--rootdir=.", TEMPLATE / "tests"]
    code = envs.uv_run(tool, ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *suite, *args], cwd=ROOT, check=False).returncode
    if any(a in SELFTEST_INFO_FLAGS for a in args):
        return code  # pytest printed its help or version: no mypy of the whole runner after it
    typed = envs.uv_run(
        tool,
        ["mypy", "--strict", "--no-incremental", "--python-version", "3.11", "--config-file", TEMPLATE / "tests" / "mypy-runner.ini", TEMPLATE / "runner", TEMPLATE / "pyt.py"],
        check=False,
    ).returncode
    return code or typed


def _parse_globals(argv: list[str]) -> list[str]:
    """Consume the global options (only BEFORE the command) and return the command line.

    `-h`/`--help` turns it into `help [COMMAND]`. `-q` does not apply to a dry run: its
    output is the plan it was asked for.
    """
    rest = list(argv)
    wants_help = False
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in ("-v", "--verbose"):
            ui.VERBOSE = True
        elif flag in ("-q", "--quiet"):
            ui.QUIET = True
        elif flag == "--dry-run":
            proc.DRY_RUN = True
        elif flag == "--no-render":
            _OPTS["no_render"] = True
        elif flag in HELP_FLAGS:
            wants_help = True
        else:
            raise PytError(f"unknown global option: {flag}  (command options go AFTER the command)")
    if proc.DRY_RUN:
        ui.QUIET = False
    return ["help", *rest] if wants_help else rest


_OPTS: dict[str, bool] = {"no_render": False}


def dispatch(argv: list[str]) -> int:
    from . import config, render, tasks

    if not argv or argv[0] == "help":
        return cmd_help(None, argv[1:])
    name, args = argv[0], argv[1:]
    if project.GLOBAL:
        return _dispatch_outside_a_project(name, args)
    command = COMMANDS.get(name) or INTERNAL.get(name)
    if name in COMMANDS and name not in HELP_PASSES_THROUGH and _asks_help(args) and _task_named_like(None, name) is None:
        return cmd_help(None, [name])  # the same as `./pyt help NAME` (a builtin of the contract needs no config)
    cfg = config.load(set(COMMANDS))
    if name in cfg.tasks:
        command = None  # a builtin added after the contract (install): the project's own task had the name first
    if command is None:
        task = cfg.tasks.get(name)
        if task is None:
            if name == "init":  # no longer public (it is INTERNAL["__init"]): the preset is chosen by `new`
                raise PytError(INIT_REMOVED)
            if name in config.RETIRED_COMMANDS:
                raise PytError(_retired(name))
            raise PytError(f"unknown command: {name}  (./pyt help)")
        if not task.cmd and _asks_help(args):
            return cmd_help(cfg, [name])  # a deps-only task has no program to pass -h on to
        if not _OPTS["no_render"]:
            render.auto(cfg)
        return tasks.run_task(cfg, name, args, dispatch)
    if command.render and not _OPTS["no_render"]:
        render.auto(cfg)
    module = importlib.import_module(f"{__package__}.{command.module}")
    func: Callable[[object, list[str]], int] = getattr(module, command.func)
    return func(cfg, args)


def _dispatch_outside_a_project(name: str, args: list[str]) -> int:
    """Global mode: only GLOBAL_COMMANDS run, never after render.auto (nothing is written into the
    installed template) and never a [tasks] entry (the installed template's own are the template's,
    not the user's). `-h` after any command is its help, which says whether it needs a project
    (the app, pytest or uv that `run`, `test` or `lock` would hand it to are a project's too)."""
    from . import config

    if name in COMMANDS and _asks_help(args):
        return cmd_help(None, [name])
    if name not in COMMANDS or name not in GLOBAL_COMMANDS:
        raise PytError(_outside_a_project(name))
    try:
        cfg = config.load(set(COMMANDS))  # the template's own: `new` checks names against its lock
    except PytError as e:
        raise PytError(f"the installed template in {project.ROOT}: {e}", e.code) from None
    command = COMMANDS[name]
    module = importlib.import_module(f"{__package__}.{command.module}")
    func: Callable[[object, list[str]], int] = getattr(module, command.func)
    return func(cfg, args)


def _output_closed() -> int:
    """The reader of our output went away (`./pyt help | head -1`, `less` quit early): exit
    quietly with the SIGPIPE code of a shell pipeline. stdout and stderr now point at the null
    device, so the interpreter's final flush of what is still buffered cannot fail again.

    Windows reports a closed pipe as OSError EINVAL, not BrokenPipeError: not handled there.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, stream.fileno())
            finally:
                os.close(null)
        except (OSError, ValueError):  # no file descriptor (a replaced stream)
            pass
    return 1 if sys.platform == "win32" else 141


# errno of a write that found no room: a full disk, /dev/full, a quota, a file size limit
NO_ROOM = frozenset(n for n in (getattr(errno, name, None) for name in ("ENOSPC", "EDQUOT", "EFBIG")) if isinstance(n, int))


def _no_room(e: OSError) -> str:
    """The error line for a write that found no room (`./pyt help > /dev/full`, a full disk
    under a command): no runner bug. A write to an open file (stdout too) names no file."""
    if e.filename is not None:
        return f"cannot write {e.filename}: {e.strerror or e}"
    return f"a write failed: {e.strerror or e} (the disk, or the file the output goes to, is full)"


def _system_exit_code(e: SystemExit) -> int:
    """What Python itself would exit with: None -> 0, an int as-is, a message -> printed, 1."""
    if e.code is None:
        return 0
    if isinstance(e.code, int):
        return e.code
    ui.error(str(e.code))
    return 1


def _scratch_denied(e: PermissionError) -> str | None:
    """The file under .build/ or dist/ that `e` could not write, or None. Those folders only
    hold what ./pyt writes again, and one left behind by another user (`sudo ./pyt ...`)
    is no runner bug: every write there (tool configs, the mypyc stage, the build work dirs)
    ends here as one clear error instead of a traceback."""
    for name in (e.filename, e.filename2):
        if not name:
            continue
        try:
            path = Path(os.fsdecode(name)).resolve()
        except (OSError, ValueError):
            continue
        if any(path.is_relative_to(d.resolve()) for d in (BUILD, DIST)):
            return rel(path)
    return None


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:  # not a signal of this OS
        return f"signal {signum}"


# The commands that run on any Python 3.11 or newer, the one the launchers started the runner on
# (project.launcher_python): they read no code of the project's and need no environment of it.
# Every other command, and a [tasks] entry (install and uninstall may be one), runs on
# python.cpython (_restart). `new` needs python.cpython too, for the new project's lock: it asks
# uv for it itself (envs.ensure_python), and its __init step then runs on it.
RUNS_ON_ANY_PYTHON = frozenset({"help", "doctor", "new", "install", "uninstall", "__init"})


def _started_by_uv() -> bool:
    """Whether this runner runs in the virtual environment `uv run --script` made for it (the
    launchers' start; VIRTUAL_ENV names it). A runner started on a given Python by hand, a test
    harness or _restart (runner_env drops VIRTUAL_ENV) runs there: no second restart, ever."""
    venv = os.environ.get("VIRTUAL_ENV", "")
    if not venv or sys.prefix == sys.base_prefix:
        return False
    return os.path.normcase(os.path.realpath(venv)) == os.path.normcase(os.path.realpath(sys.prefix))


def _python_needed(rest: list[str]) -> str | None:
    """The python.cpython the command line `rest` (after the global options) must run on, or None
    when this Python will do: help, doctor, new... (RUNS_ON_ANY_PYTHON), a builtin's -h, a name
    that is no command (dispatch says so), global mode, and a pytemplate.toml that does not read
    or names no valid python.cpython (dispatch reports it, on any Python). Read without
    config.load: its warnings would print twice, here and in the restarted runner."""
    import tomllib

    from . import config

    if project.GLOBAL or not rest:
        return None
    name, args = rest[0], rest[1:]
    try:
        data = tomllib.loads(config.read_text())
    except (PytError, OSError, *config.TOML_ERRORS):
        return None
    tasks = data.get("tasks")
    if not (isinstance(tasks, dict) and name in tasks):
        if name not in COMMANDS and name not in INTERNAL or name in RUNS_ON_ANY_PYTHON:
            return None
        if name in COMMANDS and name not in HELP_PASSES_THROUGH and _asks_help(args):
            return None  # `./pyt help NAME`
    table = data.get("python")
    version = table.get("cpython", config.PythonConfig.cpython) if isinstance(table, dict) else config.PythonConfig.cpython
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", version):
        return None
    if tuple(int(part) for part in version.split(".")) < (3, 11):
        return None  # config.validate refuses it
    return version


def _runs_on_python_cpython(version: str) -> bool:
    """Whether this runner runs on python.cpython `version`: the CPython uv manages, never another
    one of that minor. With the project's environment the launchers asked uv for its own
    (only-managed: project.launcher_python); without it they let uv take a system Python too, and
    uv is asked which interpreter is its own (Fedora's or Homebrew's 3.14, Termux's 3.13 for a
    python.cpython of "3.13", are not)."""
    if sys.implementation.name != "cpython" or sys.version_info[:2] != tuple(int(part) for part in version.split(".")):
        return False
    if project.launcher_python(project.ROOT)[1] == "only-managed":
        return True
    from . import envs

    found = envs.find_cpython(version)
    if found is None:
        return False
    prefix = found.parent if project.IS_WINDOWS else found.parent.parent  # python.exe / bin/python3.X
    try:
        return os.path.samefile(prefix, sys.base_prefix)
    except OSError:
        return False


def _restart(argv: list[str], rest: list[str]) -> int | None:
    """Run the command on python.cpython when the launchers started this runner on another
    Python (_runs_on_python_cpython; project.launcher_python: the project has no environment yet,
    so any CPython 3.11+; or python.cpython was edited and .python-version not rendered yet) and
    the command needs it (_python_needed): a second runner process on it, with the same
    arguments, in the same folder, whose exit code is this one's. uv installs that CPython first
    when it is missing, or PytError(3) says why it cannot (Android/Termux:
    envs.no_download_problem). None: this Python runs the command."""
    if not _started_by_uv():
        return None
    version = _python_needed(rest)
    if version is None or _runs_on_python_cpython(version):
        return None
    from . import envs

    python = envs.ensure_python(version)
    flags = ["-s", *(["-B"] if sys.dont_write_bytecode else [])]  # no user site: none in uv's environment either
    try:
        done = proc.run([python, *flags, project.TEMPLATE / "pyt.py", *argv], cwd=Path.cwd(), env=proc.runner_env(), check=False, echo=False)
    except proc.Interrupted as e:  # the other runner had the Ctrl+C (or the signal passed on) too, and said so
        return 128 + int(signal.SIGINT) if e.code == proc.STATUS_CONTROL_C_EXIT else e.code
    return done.returncode


def _main(argv: list[str], entry: bool = False) -> int:
    try:
        rest = _parse_globals(argv)
        restarted = _restart(argv, rest) if entry else None
        if restarted is not None:
            return restarted
        code = proc.exit_code(dispatch(rest))
        sys.stdout.flush()  # inside the try: a closed pipe is reported by main, not as a bug
        return code
    except BrokenPipeError:
        raise
    except PytError as e:
        ui.error(str(e))
        return e.code
    except KeyboardInterrupt as e:
        # proc.Interrupted: the child had the Ctrl+C too (or got the SIGTERM/SIGHUP passed on) and
        # exited with its own code. Never 0: the command did not finish its remaining steps.
        code, signum = (e.code, e.signum) if isinstance(e, proc.Interrupted) else (0, int(signal.SIGINT))
        ui.error("interrupted" if signum == signal.SIGINT else f"terminated ({_signal_name(signum)})")
        return 128 + signum if code in (0, proc.STATUS_CONTROL_C_EXIT) else code
    except SystemExit as e:  # argparse: -h (0) and usage errors (2)
        return _system_exit_code(e)
    except Exception as e:
        if isinstance(e, OSError) and e.errno in NO_ROOM:
            ui.error(_no_room(e))
            return 1
        scratch = _scratch_denied(e) if isinstance(e, PermissionError) else None
        if scratch is not None:
            ui.error(
                f"cannot write {scratch}: {e.strerror if isinstance(e, OSError) else e}.\n"
                "  .build/ and dist/ only hold what ./pyt writes again: ./pyt clean removes them\n"
                "  (if another user created them, e.g. `sudo ./pyt ...`, remove them as that user)"
            )
            return 2
        traceback.print_exc()
        ui.error("internal runner error (the traceback above is a bug in .pytemplate/runner)")
        return 1


def main(argv: list[str], *, entry: bool = False) -> int:
    """Run one ./pyt command line; return the exit code (section 5.3 of CLAUDE.md). `entry`: the
    command line of .pytemplate/pyt.py, whose runner may move the command onto python.cpython
    (_restart); a caller in this process (the tests) runs it on this Python."""
    if argv[:1] == ["__probe"]:  # launcher self-test target: no config, no render, not in help
        from . import shells

        return shells.probe(argv[1:])
    try:
        return _main(argv, entry)
    except BrokenPipeError:  # stdout or stderr closed, even while an error was being reported
        return _output_closed()
    except OSError as e:  # no room for the error line either (stderr on a full disk too)
        if e.errno not in NO_ROOM:
            raise
        _output_closed()
        return 1
    finally:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            _output_closed()
