"""./deploy: Justfile-style runner, no extra binaries (only uv).

    ./deploy [-v|-q] [--dry-run] [--no-render] COMMAND [args...]

Global options go BEFORE the command; everything after it belongs to the command
(and for `run`/`test`, to your app or to pytest).
"""

from __future__ import annotations

import importlib
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from . import proc, ui
from .ui import DeployError


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
    "setup": Command("cmd_env", "cmd_setup", "Install interpreters and backend environments, lock deps and generate configs", group="Environment"),
    "doctor": Command("cmd_env", "cmd_doctor", "Check uv, compiler, PyPy, JIT, shells and generated files", group="Environment"),
    "sync": Command("cmd_env", "cmd_sync", "Run uv sync --locked on one or all environments", "[cpython|pypy|mypyc|all]", group="Environment"),
    "lock": Command("cmd_env", "cmd_lock", "Apply the managed parts of pyproject and re-lock uv.lock", "[--upgrade] [--upgrade-package PKG]", group="Environment"),
    "add": Command("cmd_env", "cmd_add", "Add dependencies (uv add)", "PKG... [--dev|--group G] [--cpython-only]", group="Environment"),
    "remove": Command("cmd_env", "cmd_remove", "Remove dependencies (uv remove)", "PKG... [--dev|--group G]", group="Environment"),
    "clean": Command("cmd_env", "cmd_clean", "Remove .build/ and dist/ (and the environments with --envs)", "[--envs]", render=False, group="Environment"),
    # mode and template
    "mode": Command("cmd_mode", "cmd_mode", "Show or change the mode (backend, supported, typing, JIT, editor)", "[BACKEND] [--supports +pypy|-pypy] [--typing off|warn|strict|auto] [--jit on|off] [--editor pylance|basedpyright]", group="Mode"),
    "render": Command("cmd_mode", "cmd_render", "Regenerate .mypy.ini, pyrightconfig.json, .ruff.toml and .vscode/", "[--check] [--diff] [--force]", render=False, group="Mode"),
    "init": Command("cmd_mode", "cmd_init", "Convert this project to a preset (script, raylib, flet)", "PRESET [--name NAME] [--force]", group="Mode"),
    "new": Command("cmd_mode", "cmd_new", "Create a new project from this template", "DIR [--preset P] [--name NAME]", render=False, group="Mode"),
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
    "shell-setup": Command("shells", "cmd_shell_setup", "Print a `deploy` function/alias for your shell (works from any subfolder)", "[xonsh|pwsh|powershell|bash|zsh|niubash|msys2|fish|nu]", render=False, group="Other"),
    "nvim": Command("cmd_nvim", "cmd_nvim", "Neovim/LazyVim integration: check it, trust .lazy.lua, enable extras, sync plugins", "[doctor|trust|extras|bootstrap|sync]", group="Other"),
    "selftest": Command("cli", "cmd_selftest", "Run the runner's own tests and mypy --strict (.pytemplate)", "[--shells|--nvim|--e2e] [args...]", render=False, group="Other"),
    "help": Command("cli", "cmd_help", "Show this help (or a command's help)", "[COMMAND]", render=False, group="Other"),
}

EXAMPLES = """\
Examples:
  ./deploy setup                 # first time: interpreters, environments and configs
  ./deploy run                   # run with the active backend (pytemplate.toml)
  ./deploy run mypyc --verbose   # compile with mypyc and run (--verbose goes to your app)
  ./deploy test all              # pytest on every supported backend
  ./deploy mode mypyc            # change the active backend (strict typing)
  ./deploy mode --supports +pypy # add PyPy (3.11 syntax)
  ./deploy build mypyc           # exe with PyInstaller (mypyc's default method)
  ./deploy build pypy            # portable folder with PyPy bundled
  ./deploy build cpython --method pyz
  ./deploy nvim doctor           # check the LazyVim integration (./deploy nvim trust once)"""


def cmd_help(cfg: object, args: list[str]) -> int:
    if args and args[0] in COMMANDS:
        c = COMMANDS[args[0]]
        print(f"./deploy {args[0]} {c.usage}".rstrip())
        print(f"  {c.summary}")
        return 0
    print("./deploy [-v|-q] [--dry-run] [--no-render] COMMAND [args...]\n")
    groups: dict[str, list[str]] = {}
    for name, c in COMMANDS.items():
        groups.setdefault(c.group, []).append(name)
    for group, names in groups.items():
        print(f"{group}:")
        for name in names:
            print(f"  {name:<12} {COMMANDS[name].summary}")
        print()
    try:
        from . import config

        loaded = config.load(set(COMMANDS))
        if loaded.tasks:
            print("Custom tasks (pytemplate.toml [tasks]):")
            for name, task in loaded.tasks.items():
                print(f"  {name:<12} {task.help or ' '.join(task.cmd)}")
            print()
    except DeployError:
        pass
    print("BACKEND = cpython | pypy | mypyc (default: backend.active from pytemplate.toml)\n")
    print(EXAMPLES)
    return 0


def cmd_tasks(cfg: object, args: list[str]) -> int:
    from . import tasks
    from .config import Config

    assert isinstance(cfg, Config)
    tasks.list_tasks(cfg)
    return 0


def cmd_selftest(cfg: object, args: list[str]) -> int:
    from . import e2e, envs, nvimtest, shells
    from .config import Config
    from .project import TEMPLATE

    assert isinstance(cfg, Config)
    suites: dict[str, Callable[[Config, list[str]], int]] = {
        "--shells": shells.selftest,  # every launcher through every installed shell
        "--nvim": nvimtest.selftest,  # the LazyVim integration in an isolated LazyVim
        "--e2e": e2e.selftest,  # ./deploy new + setup/check/test/build per preset
    }
    if args and args[0] in suites:
        return suites[args[0]](cfg, args[1:])
    tool = envs.tool_env(cfg)
    code = envs.uv_run(tool, ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", TEMPLATE / "tests", *args], check=False).returncode
    typed = envs.uv_run(
        tool,
        ["mypy", "--strict", "--no-incremental", "--python-version", "3.11", "--config-file", TEMPLATE / "tests" / "mypy-runner.ini", TEMPLATE / "runner", TEMPLATE / "deploy.py"],
        check=False,
    ).returncode
    return code or typed


def _parse_globals(argv: list[str]) -> list[str]:
    rest = list(argv)
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
        elif flag in ("-h", "--help"):
            return ["help"]
        else:
            raise DeployError(f"unknown global option: {flag}  (command options go AFTER the command)")
    return rest


_OPTS: dict[str, bool] = {"no_render": False}


def dispatch(argv: list[str]) -> int:
    from . import config, render, tasks

    if not argv or argv[0] == "help":
        return cmd_help(None, argv[1:])
    name, args = argv[0], argv[1:]
    cfg = config.load(set(COMMANDS))
    command = COMMANDS.get(name)
    if command is None:
        if name in cfg.tasks:
            if not _OPTS["no_render"]:
                render.auto(cfg)
            return tasks.run_task(cfg, name, args, dispatch)
        raise DeployError(f"unknown command: {name}  (./deploy help)")
    if command.render and not _OPTS["no_render"]:
        render.auto(cfg)
    module = importlib.import_module(f"{__package__}.{command.module}")
    func: Callable[[object, list[str]], int] = getattr(module, command.func)
    return func(cfg, args)


def main(argv: list[str]) -> int:
    if argv[:1] == ["__probe"]:  # launcher self-test target: no config, no render, not in help
        from . import shells

        return shells.probe(argv[1:])
    try:
        return dispatch(_parse_globals(argv))
    except DeployError as e:
        ui.error(str(e))
        return e.code
    except KeyboardInterrupt:
        ui.error("interrupted")
        return 130
    except SystemExit as e:  # argparse
        return e.code if isinstance(e.code, int) else 2
    except Exception:
        traceback.print_exc()
        ui.error("internal runner error (the traceback above is a bug in .pytemplate/runner)")
        return 1
    finally:
        sys.stdout.flush()
