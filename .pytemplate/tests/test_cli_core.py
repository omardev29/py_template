"""The runner's core: cli (global options, dispatch, help, exit codes), proc (child processes,
--dry-run, the environment, Ctrl+C, signals), tasks ([tasks]) and cmd_dev (check, test, lint).

Most tests run in-process with fakes; the few that start processes use `sys.executable` and a
scrubbed environment, never uv unless marked, and never touch this project's files.
"""

from __future__ import annotations

import errno
import importlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_DIR))

from runner import cli, cmd_dev, cmd_env, cmd_mode, config, e2e, envs, lintc, mutation, mypyc, nvimtest, presets, proc, project, render, shells, tasks, ui  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import BUILD, DIST, ROOT, SRC  # noqa: E402
from runner.ui import PytError  # noqa: E402

PYT_PY = TEMPLATE_DIR / "pyt.py"
TEMPLATE_REPO = (TEMPLATE_DIR / "template-repo").is_file()  # the shipped content is pinned here only
IS_WINDOWS = os.name == "nt"
posix = pytest.mark.skipif(IS_WINDOWS, reason="POSIX signals, pipes and exec bits")
needs_uv = pytest.mark.skipif(shutil.which("uv") is None and not os.environ.get("UV"), reason="uv not found")


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


def own(data: dict[str, Any]) -> Config:
    """make() with this project's app name and compile.modules: code that reads src/ (the
    compiled modules) finds them in any project made with ./pyt new, not only in myapp."""
    real = config.load(set())
    app = {"name": real.app.name, **data.get("app", {})}
    return make({**data, "app": app, "compile": {"modules": list(real.compile.modules), **data.get("compile", {})}})


def unchecked(data: dict[str, Any]) -> Config:
    """A Config that never went through config.validate (the runner must still not crash)."""
    cfg: Config = config._build(Config, data, "")
    return cfg


# where the user's uv keeps its cache and its Pythons: theirs, not the uv run's. Dropped, a child
# in a home of its own (the workers of selftest --mutation) downloaded CPython, or offline failed
UV_FOLDERS = ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR")


def child_env() -> dict[str, str]:
    """The environment of a child ./pyt: nothing of the uv run that started pytest."""
    drop = ("UV", "VIRTUAL_ENV", "PYTHONUNBUFFERED", "PYTHONPATH", "PYTHONHOME")
    env = {k: v for k, v in os.environ.items() if k not in drop and (k in UV_FOLDERS or not k.startswith(("PYTEMPLATE_", "UV_")))}
    env.update(NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def test_child_env_keeps_where_uv_keeps_its_cache_and_pythons(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (("UV_CACHE_DIR", "/c"), ("UV_PYTHON_INSTALL_DIR", "/p"), ("UV_PROJECT_ENVIRONMENT", "/v"), ("UV_PYTHON", "3.14")):
        monkeypatch.setenv(name, value)
    env = child_env()
    assert env["UV_CACHE_DIR"] == "/c" and env["UV_PYTHON_INSTALL_DIR"] == "/p"
    assert "UV_PROJECT_ENVIRONMENT" not in env and "UV_PYTHON" not in env


def fail(*_a: Any, **_kw: Any) -> Any:
    raise AssertionError("must not be called")


@pytest.fixture(autouse=True)
def _globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts without -v/-q/--dry-run/--no-render and without launcher variables."""
    monkeypatch.setattr(ui, "VERBOSE", False)
    monkeypatch.setattr(ui, "QUIET", False)
    monkeypatch.setattr(ui, "_COLOR", False)
    monkeypatch.setattr(proc, "DRY_RUN", False)
    monkeypatch.setitem(cli._OPTS, "no_render", False)
    for name in ("PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any process start is a test failure (the check under test must come first)."""
    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)


def completed(argv: Any, code: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, out, "")


# === 1. global options ============================================================================


def test_global_options_only_before_the_command() -> None:
    rest = cli._parse_globals(["-v", "-q", "--no-render", "run", "-v", "--dry-run", "-q"])
    assert rest == ["run", "-v", "--dry-run", "-q"]  # after the command they belong to the app
    assert ui.VERBOSE and ui.QUIET and cli._OPTS["no_render"]
    assert not proc.DRY_RUN


@pytest.mark.parametrize("flag", ["--method", "-x", "--verbose=1", "-vq", "-"])
def test_an_unknown_global_option_is_a_usage_error(flag: str) -> None:
    with pytest.raises(PytError, match="AFTER the command") as e:
        cli._parse_globals([flag, "build"])
    assert e.value.code == 2


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["-h"], ["help"]),
        (["--help"], ["help"]),
        (["-h", "run"], ["help", "run"]),
        (["-v", "--help", "check"], ["help", "check"]),
        (["--help", "-q", "ci"], ["help", "ci"]),
    ],
)
def test_the_help_option_keeps_the_command(argv: list[str], expected: list[str]) -> None:
    assert cli._parse_globals(argv) == expected


def test_a_dry_run_is_never_quiet() -> None:
    # Its output is the plan the user asked for: -q would hide all of it
    assert cli._parse_globals(["-q", "--dry-run", "mode", "mypyc"]) == ["mode", "mypyc"]
    assert proc.DRY_RUN and not ui.QUIET


def test_quiet_without_a_dry_run() -> None:
    assert cli._parse_globals(["-q", "tasks"]) == ["tasks"]
    assert ui.QUIET and not proc.DRY_RUN


# === 2. main: every outcome has its exit code =====================================================


@pytest.mark.parametrize(
    ("raised", "code", "stderr"),
    [
        (PytError("bad config"), 2, "error: bad config"),
        (PytError("no uv", 3), 3, "error: no uv"),
        (proc.CommandFailed(["uv", "sync"], 7), 7, "error: failed (exit code 7): uv sync"),
        (proc.CommandFailed(["x"], -9), 137, "failed (exit code 137)"),
        (KeyboardInterrupt(), 130, "error: interrupted"),
        (proc.Interrupted(3), 3, "error: interrupted"),  # the child's own code after its cleanup
        (proc.Interrupted(0), 130, "error: interrupted"),  # an interrupted command never reports success
        (proc.Interrupted(130), 130, "error: interrupted"),
        (proc.Interrupted(proc.STATUS_CONTROL_C_EXIT), 130, "error: interrupted"),
        (proc.Interrupted(0, signal.SIGTERM), 143, "error: terminated (SIGTERM)"),  # passed on, the child exited 0
        (proc.Interrupted(4, signal.SIGTERM), 4, "error: terminated (SIGTERM)"),
        (proc.Interrupted(143, signal.SIGTERM), 143, "error: terminated (SIGTERM)"),
        (proc.Interrupted(0, 1), 129, "error: terminated ("),  # SIGHUP (POSIX only)
        (SystemExit(0), 0, ""),  # argparse -h
        (SystemExit(2), 2, ""),  # argparse usage error
        (SystemExit(None), 0, ""),
        (SystemExit("stopped"), 1, "error: stopped"),
        (RuntimeError("a bug"), 1, "internal runner error"),
    ],
)
def test_main_maps_every_outcome_to_its_exit_code(
    raised: BaseException, code: int, stderr: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dispatch(_argv: list[str]) -> int:
        raise raised

    monkeypatch.setattr(cli, "dispatch", dispatch)
    assert cli.main(["whatever"]) == code
    err = capsys.readouterr().err
    assert stderr in err
    assert ("Traceback" in err) == isinstance(raised, RuntimeError)


@pytest.mark.parametrize(
    ("filename", "code", "stderr"),
    [
        (BUILD / "cfg" / "ruff-off.toml", 2, "error: cannot write .build/cfg/ruff-off.toml: Permission denied"),
        (BUILD / "mypyc-dev" / "stage" / "main.py", 2, "error: cannot write .build/mypyc-dev/stage/main.py"),
        (DIST / "app-cpython-pyz", 2, "error: cannot write dist/app-cpython-pyz"),
        (ROOT / "src" / "main.py", 1, "internal runner error"),  # not a scratch folder: a bug to see
    ],
)
def test_a_scratch_folder_another_user_left_is_a_clear_error(
    filename: Path, code: int, stderr: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`sudo ./pyt check` leaves .build/cfg owned by root: the next `./pyt check` crashed
    with a PermissionError traceback and "internal runner error"."""

    def dispatch(_argv: list[str]) -> int:
        raise PermissionError(13, "Permission denied", str(filename))

    monkeypatch.setattr(cli, "dispatch", dispatch)
    assert cli.main(["check"]) == code
    err = capsys.readouterr().err
    assert stderr in err
    assert ("Traceback" in err) is (code == 1)
    if code == 2:
        assert "./pyt clean" in err and "sudo" in err


@posix
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root writes into a read-only folder")
def test_check_into_an_unwritable_build_folder_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    build = tmp_path / ".build"
    (build / "cfg").mkdir(parents=True)
    (build / "cfg").chmod(0o555)
    monkeypatch.setattr(cmd_dev, "BUILD", build)
    monkeypatch.setattr(cli, "BUILD", build)
    monkeypatch.setattr(cli, "dispatch", lambda _argv: cmd_dev._profile_file(make({}), "off", "ruff") and 0)
    try:
        assert cli.main(["check"]) == 2
    finally:
        (build / "cfg").chmod(0o755)
    assert "error: cannot write" in capsys.readouterr().err


@pytest.mark.parametrize(("returned", "code"), [(0, 0), (1, 1), (5, 5), (-9, 137), (-15, 143)])
def test_main_returns_the_commands_code(returned: int, code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # A negative code (a signal death reported by a path that bypassed proc.run) would become
    # 256 - N as the process exit status: main reports 128 + N like sh and uv
    monkeypatch.setattr(cli, "dispatch", lambda _argv: returned)
    assert cli.main(["x"]) == code


def test_main_parses_the_global_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "dispatch", lambda argv: seen.append(argv) or 0)
    assert cli.main(["-q", "--no-render", "run", "-q"]) == 0
    assert seen == [["run", "-q"]] and ui.QUIET and cli._OPTS["no_render"]
    assert cli.main(["--bogus", "run"]) == 2


def test_probe_needs_no_config(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(config, "load", fail)
    assert cli.main(["__probe", "5", "0", "a b", "--dry-run"]) == 5
    line = capsys.readouterr().out.strip()
    assert line.startswith("PTPROBE{")
    assert json.loads(line.removeprefix("PTPROBE"))["argv"] == ["a b", "--dry-run"]
    assert not proc.DRY_RUN  # __probe runs before the global options are parsed


# === 3. dispatch ===================================================================================

# Builtins that never render first (CLAUDE.md 5.2). A new command with render=False fails here
# until it is added (and documented).
# apply/setup render themselves at the end (a refused apply writes nothing); rename renders only
# after its checks, never with a hand-edited app.name before the dirty-tree check; the internal
# __init renders with --force at its end (rendering the copy first, `new` warned about the source
# project's hand-edited .vscode/settings.json: "use ./pyt render --force")
NEVER_RENDER = {"clean", "render", "new", "install", "uninstall", "pyz-merge", "tasks", "selftest", "help", "hooks", "setup", "apply", "rename", "__init"}


def test_commands_that_never_render() -> None:
    every = {**cli.COMMANDS, **cli.INTERNAL}
    assert {n for n, c in every.items() if not c.render} == NEVER_RENDER & set(every)


def test_render_runs_before_builtins_and_tasks_unless_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({"tasks": {"t": {"cmd": ["tool"]}}})
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: cfg)
    rendered: list[Config] = []
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(render, "auto", rendered.append)
    monkeypatch.setattr(cmd_dev, "cmd_lint", lambda _cfg, args: calls.append(("lint", args)) or 0)
    monkeypatch.setattr(cmd_env, "cmd_clean", lambda _cfg, args: calls.append(("clean", args)) or 0)
    monkeypatch.setattr(tasks, "run_task", lambda _cfg, name, args, _dispatch: calls.append((name, args)) or 0)
    assert cli.dispatch(["lint", "--fix"]) == 0
    assert rendered == [cfg]
    assert cli.dispatch(["t", "x"]) == 0
    assert rendered == [cfg, cfg]
    assert cli.dispatch(["clean"]) == 0  # render=False
    assert rendered == [cfg, cfg]
    monkeypatch.setitem(cli._OPTS, "no_render", True)
    assert cli.dispatch(["lint"]) == 0
    assert cli.dispatch(["t"]) == 0
    assert rendered == [cfg, cfg]
    assert calls == [("lint", ["--fix"]), ("t", ["x"]), ("clean", []), ("lint", []), ("t", [])]


def test_a_projects_task_keeps_a_name_a_later_builtin_took(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 1.11: a [tasks.install] of a project made before `pyt install` existed stopped every
    command. There the task runs for `./pyt install` (-h goes to its program), `help install`
    describes it and says where the builtin runs; a builtin of the contract keeps its help."""
    cfg = make({"tasks": {"install": {"cmd": ["tool"]}}})
    config.validate(cfg, set(cli.COMMANDS))
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: cfg)
    monkeypatch.setattr(render, "auto", lambda _cfg: None)
    ran: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(tasks, "run_task", lambda _cfg, name, args, _dispatch: ran.append((name, args)) or 0)
    assert cli.dispatch(["install", "-h"]) == 0 and cli.dispatch(["install"]) == 0
    assert ran == [("install", ["-h"]), ("install", [])]
    assert cli.cmd_help(None, ["install"]) == 0
    out = capsys.readouterr().out
    assert "./pyt install [args...]" in out and "the built-in `pyt install` runs outside the project" in out
    assert cli.cmd_help(None, []) == 0 and "runs instead of the built-in command here" in capsys.readouterr().out
    assert cli.dispatch(["uninstall", "-h"]) == 0 and "pyt uninstall" in capsys.readouterr().out  # no such task: the builtin's help
    assert ran == [("install", ["-h"]), ("install", [])]
    with pytest.raises(PytError, match=r"shell-setup is no longer a ./pyt command: `pyt install` puts") as e:
        cli.dispatch(["shell-setup"])
    assert e.value.code == 2


def test_a_retired_command_in_deps_is_skipped_with_a_warning(capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 1.11: a deps entry naming a builtin removed since the first contract version is left
    out with a warning; it stopped the task."""
    config._WARNED.clear()
    cfg = make({"tasks": {"ci": {"deps": ["shell-setup bash", "check"]}}})
    seen: list[list[str]] = []
    assert tasks.run_task(cfg, "ci", [], lambda argv: seen.append(argv) or 0) == 0
    assert seen == [["check"]]
    assert "task 'ci': deps entry 'shell-setup bash' names shell-setup, which is no longer a ./pyt command" in capsys.readouterr().err


def test_an_unknown_command_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: make({"tasks": {"ci": {"deps": ["check"]}}}))
    with pytest.raises(PytError, match="unknown command: sycn") as e:
        cli.dispatch(["sycn"])
    assert e.value.code == 2


def test_help_needs_no_valid_config(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(*_a: Any, **_kw: Any) -> Config:
        raise PytError("pytemplate.toml is not valid TOML: x")

    monkeypatch.setattr(config, "load", broken)
    assert cli.dispatch([]) == 0  # the full help, without the [tasks] block
    assert "Development:" in capsys.readouterr().out
    assert cli.dispatch(["check", "--help"]) == 0
    assert capsys.readouterr().out.startswith("./pyt check ")
    with pytest.raises(PytError, match="not valid TOML"):  # it cannot tell whether 'ci' is a task
        cli.cmd_help(None, ["ci"])


# === 4. help ========================================================================================


def _broken(*_a: Any, **_kw: Any) -> Config:
    raise PytError("pytemplate.toml is not valid TOML: x")


@pytest.mark.parametrize("name", sorted(cli.COMMANDS))
def test_help_for_every_command(name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # a builtin's help needs no config; one added after the contract looks for a [tasks] entry of
    # its name first (it keeps the name in its project), and a config that does not load is none
    monkeypatch.setattr(config, "load", fail if name in config.CONTRACT_COMMANDS else _broken)
    assert cli.cmd_help(None, [name]) == 0
    c = cli.COMMANDS[name]
    assert capsys.readouterr().out.splitlines()[:2] == [f"./pyt {name} {c.usage}".rstrip(), f"  {c.summary}"]


def _help_cases() -> list[tuple[str, list[str]]]:
    cases = []
    for name in sorted(set(cli.COMMANDS) - cli.HELP_PASSES_THROUGH):
        for args in (["-h"], ["--help"], ["x", "--help"]):
            if name != "help" or args[0] != "x":  # `help x --help` is the help of x
                cases.append((name, args))
    return cases


@pytest.mark.parametrize(("name", "args"), _help_cases())
def test_the_help_option_after_a_command_shows_its_help(
    name: str, args: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "load", fail if name in config.CONTRACT_COMMANDS else _broken)  # nothing runs, not even the config load
    assert cli.main([name, *args]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"./pyt {name} {cli.COMMANDS[name].usage}".rstrip()


@pytest.mark.parametrize("name", sorted(cli.HELP_PASSES_THROUGH))
def test_the_help_option_goes_on_to_the_app_pytest_or_uv(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: make({}))
    monkeypatch.setitem(cli._OPTS, "no_render", True)
    c = cli.COMMANDS[name]
    seen: list[list[str]] = []
    monkeypatch.setattr(importlib.import_module(f"runner.{c.module}"), c.func, lambda _cfg, args: seen.append(args) or 0)
    assert cli.dispatch([name, "--help"]) == 0
    assert seen == [["--help"]]


def test_the_help_option_after_a_task(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make({"tasks": {"ci": {"deps": ["check all", "test all"]}, "gen": {"cmd": ["tool"]}}})
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: cfg)
    monkeypatch.setattr(render, "auto", lambda _cfg: None)
    ran: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(tasks, "run_task", lambda _cfg, name, args, _dispatch: ran.append((name, args)) or 0)
    assert cli.dispatch(["ci", "--help"]) == 0  # deps only: nothing to pass -h on to
    assert capsys.readouterr().out.startswith("./pyt ci\n")
    assert ran == []
    assert cli.dispatch(["gen", "-h"]) == 0  # a task with a cmd passes it on to its program
    assert ran == [("gen", ["-h"])]


def test_help_for_a_task(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make(
        {
            "tasks": {
                "ci": {"deps": ["check all", "test all"]},
                "gen": {"help": "Generate the assets", "cmd": ["python", "gen.py", "{backend}", "a b"], "env": {"SEED": "42"}, "cwd": "src", "uv": False},
                "dev": {"cmd": ["flet", "run"], "backend": "cpython", "background": True},
            }
        }
    )
    assert cli.cmd_help(cfg, ["ci"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "./pyt ci"
    assert "check all, test all" in out and "no arguments" in out
    assert "Development:" not in out  # not the full help
    assert cli.cmd_help(cfg, ["gen"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[:2] == ["./pyt gen [args...]", "  Generate the assets"]
    for text in ("python gen.py {backend} 'a b'", "(uv = false)", "cwd      src", "env      SEED=42"):
        assert text in out
    assert cli.cmd_help(cfg, ["dev"]) == 0
    out = capsys.readouterr().out
    assert "uv run in the cpython environment" in out and "background" in out


def test_the_full_help_describes_every_task(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make({"tasks": {"ci": {"deps": ["check all", "test all"]}, "gen": {"cmd": ["tool", "x"]}, "doc": {"cmd": ["y"], "help": "Docs"}}})
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: cfg)
    assert cli.cmd_help(None, []) == 0
    out = capsys.readouterr().out
    assert "  ci           deps: check all, test all\n" in out  # deps-only: never an empty description
    assert "  gen          tool x\n" in out
    assert "  doc          Docs\n" in out


@pytest.mark.parametrize(
    ("args", "message"),
    [(["sycn"], "unknown command: sycn"), (["--bogus"], "unknown command: --bogus"), (["run", "x"], "unrecognized arguments: x")],
)
def test_help_never_ignores_a_typo(args: list[str], message: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: make({"tasks": {"ci": {"deps": ["check"]}}}))
    with pytest.raises(PytError, match=re.escape(message)) as e:
        cli.cmd_help(None, args)
    assert e.value.code == 2


def test_help_init_gives_the_hint_of_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """`./pyt help init` said only "unknown command", while `./pyt init` names the way out;
    a [tasks] entry named init is described instead."""
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: make({}))
    with pytest.raises(PytError, match=re.escape("./pyt new DIR --preset P")) as e:
        cli.cmd_help(None, ["init"])
    assert e.value.code == 2 and "unknown command" not in str(e.value)
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: make({"tasks": {"init": {"cmd": ["python", "-c", "pass"]}}}))
    assert cli.cmd_help(None, ["init"]) == 0


def test_help_of_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_help(None, ["-h"]) == 0
    assert capsys.readouterr().out.startswith("./pyt help [COMMAND]\n")


# === 5. every command rejects what it does not understand ============================================

# Every command that does not forward its arguments (cli.FORWARDS), with the smallest valid
# arguments and whether one more positional is legitimate (add/remove take several packages).
# A new command fails test_every_command_is_classified until it is added here.
MINIMAL: dict[str, tuple[list[str], bool]] = {
    "setup": ([], False),
    "apply": ([], False),
    "doctor": ([], False),
    "sync": (["cpython"], False),
    "add": (["somepkg"], True),
    "remove": (["somepkg"], True),
    "clean": ([], False),
    "hooks": (["status"], False),
    "mode": (["cpython"], False),
    "render": ([], False),
    "init": (["script"], False),
    "rename": (["othername"], False),
    "new": (["somewhere"], False),
    "install": ([], False),
    "uninstall": ([], False),
    "check": (["cpython"], False),
    "lint": ([], False),
    "fmt": ([], False),
    "report": ([], False),
    "compile": ([], False),
    "pyz-merge": (["a.pyz", "b.pyz", "--out", "c.pyz"], False),
    "tasks": ([], False),
    "nvim": (["doctor"], False),
    "help": (["run"], False),
}


def test_every_command_is_classified() -> None:
    missing = sorted(set(cli.COMMANDS) - cli.FORWARDS - set(MINIMAL))
    assert not missing, f"add {missing} to MINIMAL (it must reject unknown arguments) or to cli.FORWARDS"
    assert cli.HELP_PASSES_THROUGH <= cli.FORWARDS


def _rejection_cases() -> list[tuple[str, str]]:
    cases = []
    for name in sorted(set(cli.COMMANDS) - cli.FORWARDS):
        cases.append((name, "--pt-bogus-flag"))
        if name in MINIMAL and not MINIMAL[name][1]:
            cases.append((name, "pt-bogus-positional"))
    return cases


@pytest.mark.parametrize(("name", "bogus"), _rejection_cases())
@pytest.mark.usefixtures("no_processes")
def test_every_command_rejects_an_unknown_argument(name: str, bogus: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    args, _ = MINIMAL.get(name, ([], False))
    monkeypatch.setattr(proc, "DRY_RUN", True)  # in case the check is broken: nothing is written
    monkeypatch.setitem(cli._OPTS, "no_render", True)
    load = config.load

    def builtin_only(builtin_commands: set[str] | None = None) -> Config:
        """The project's config without a [tasks] entry named like the command: one may have the
        name of a builtin added after the contract (install, uninstall: CLAUDE.md 5.2), and
        dispatch runs that task there, with any argument. The builtin is what this test checks."""
        cfg = load(builtin_commands)
        cfg.tasks.pop(name, None)
        return cfg

    monkeypatch.setattr(config, "load", builtin_only)
    code = cli.main([name, *args, bogus])
    err = capsys.readouterr().err
    assert code == 2, err
    assert "Traceback" not in err


@pytest.mark.parametrize("suite", ["--shells", "--nvim", "--e2e", "--mutation"])
@pytest.mark.usefixtures("no_processes")
def test_the_selftest_suites_reject_an_unknown_option(suite: str) -> None:
    assert cli.main(["selftest", suite, "--pt-bogus-flag"]) == 2


@pytest.mark.parametrize("suite", ["--shells", "--nvim", "--e2e", "--mutation"])
@pytest.mark.usefixtures("no_processes")
def test_a_dry_run_never_starts_a_selftest_suite(suite: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (shells, nvimtest, e2e, mutation):
        monkeypatch.setattr(module, "selftest", fail)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    with pytest.raises(PytError, match="has no --dry-run") as e:
        cli.cmd_selftest(make({}), [suite, "script"])
    assert e.value.code == 2


def test_the_tasks_command_takes_no_arguments() -> None:
    with pytest.raises(PytError, match="tasks: unrecognized arguments: ci") as e:
        cli.cmd_tasks(make({}), ["ci"])
    assert e.value.code == 2


# === 6. proc.run ====================================================================================


def test_a_dry_run_skips_only_the_echoed_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "DRY_RUN", True)
    marker = tmp_path / "ran"
    code = f"open({str(marker)!r}, 'w').close()"
    r = proc.run([sys.executable, "-c", code])  # echoed: `uv sync`, `uv add`...: skipped
    assert (r.returncode, marker.exists()) == (0, False)
    proc.run([sys.executable, "-c", code], echo=False)  # a query (`uv lock --check`): it runs
    assert marker.exists()
    assert proc.output([sys.executable, "-c", "print(42)"]) == "42"


def test_run_returns_the_childs_exit_code(tmp_path: Path) -> None:
    exit7 = [sys.executable, "-c", "raise SystemExit(7)"]
    assert proc.run(exit7, check=False, echo=False, cwd=tmp_path).returncode == 7
    with pytest.raises(proc.CommandFailed) as e:
        proc.run(exit7, echo=False, cwd=tmp_path)
    assert e.value.code == 7 and "exit code 7" in str(e.value)


def test_a_failed_query_shows_its_stderr_even_with_quiet(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(ui, "QUIET", True)
    with pytest.raises(proc.CommandFailed):
        proc.output([sys.executable, "-c", "import sys; sys.stderr.write('why it failed'); raise SystemExit(1)"])
    assert "why it failed" in capsys.readouterr().err


@pytest.mark.parametrize(("raw", "code"), [(0, 0), (1, 1), (255, 255), (-2, 130), (-9, 137), (-15, 143)])
def test_exit_code_follows_the_shell_convention(raw: int, code: int) -> None:
    assert proc.exit_code(raw) == code
    if raw:
        assert proc.CommandFailed(["x"], raw).code == code  # also for callers that bypass proc.run


@posix
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGKILL", "SIGUSR1"])
def test_a_signal_death_is_128_plus_n(sig: str, tmp_path: Path) -> None:
    num = int(getattr(signal, sig))
    die = [sys.executable, "-c", f"import os; os.kill(os.getpid(), {num})"]
    assert proc.run(die, check=False, echo=False, cwd=tmp_path).returncode == 128 + num
    with pytest.raises(proc.CommandFailed) as e:
        proc.run(die, echo=False, cwd=tmp_path)
    assert e.value.code == 128 + num and f"exit code {128 + num}" in str(e.value)


def test_a_missing_program_is_a_missing_requirement(tmp_path: Path) -> None:
    with pytest.raises(PytError, match="program not found") as e:
        proc.run([str(tmp_path / "no-such-program")], echo=False)
    assert e.value.code == 3


@posix
def test_a_program_that_cannot_start_is_a_clear_error(tmp_path: Path) -> None:
    noexec = tmp_path / "noexec.sh"
    noexec.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    noexec.chmod(0o644)
    noshebang = tmp_path / "noshebang"
    noshebang.write_bytes(b"\x7fnot-a-binary\n")
    noshebang.chmod(0o755)
    folder = tmp_path / "folder"
    folder.mkdir()
    for program in (noexec, noshebang, folder):
        with pytest.raises(PytError) as e:
            proc.run([str(program)], echo=False)
        assert e.value.code == 2
        assert "cannot run" in str(e.value) and program.name in str(e.value)


@posix
def test_a_script_whose_interpreter_is_missing_names_it(tmp_path: Path) -> None:
    """exec reports a missing #! interpreter as ENOENT: `program not found: tools/bad.sh` sent
    the user looking for a script that is right there."""
    tools = tmp_path / "tools"
    tools.mkdir()
    bad = tools / "bad.sh"
    bad.write_text("#!/nonexistent/interp -x\necho hi\n", encoding="utf-8")
    bad.chmod(0o755)
    for argv, cwd in (([str(bad)], None), (["tools/bad.sh"], tmp_path)):  # absolute, and relative to cwd
        with pytest.raises(PytError) as e:
            proc.run(argv, cwd=cwd, echo=False)
        assert e.value.code == 3
        assert "the interpreter of its #! line was not found: /nonexistent/interp" in str(e.value)
    env = proc.base_env()
    env["PATH"] = f"{tools}{os.pathsep}{env['PATH']}"
    with pytest.raises(PytError, match=r"cannot run bad\.sh: the interpreter of its #! line was not found"):
        proc.run(["bad.sh"], env=env, echo=False)  # found on the child's PATH
    with pytest.raises(PytError, match="program not found: no-such-tool"):
        proc.run(["no-such-tool"], env=env, echo=False)


@posix
def test_a_script_with_windows_line_endings_says_so(tmp_path: Path) -> None:
    """A script checked out with CRLF (a Windows checkout used from WSL): exec looks for the
    interpreter "/bin/sh\\r", and the message named /bin/sh, which exists."""
    (tmp_path / "tools").mkdir()
    gen = tmp_path / "tools" / "gen"
    gen.write_bytes(b"#!/bin/sh\r\necho generated\r\n")
    gen.chmod(0o755)
    with pytest.raises(PytError) as e:
        proc.run(["tools/gen"], cwd=tmp_path, echo=False)
    assert e.value.code == 3
    assert "carriage return (Windows line endings)" in str(e.value) and "LF line endings" in str(e.value), str(e.value)
    assert "was not found: /bin/sh" not in str(e.value)


@posix
@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_the_crlf_hint_names_a_gitattributes_line_git_applies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A task's program reaches proc.run absolute (run_task anchors tools/crlf to the task's
    cwd), and the hint said to add `/home/.../tools/crlf text eol=lf` to .gitattributes, a pattern
    git never matches (patterns are relative to their .gitattributes). It names the file
    relative to the project; git applies the line it suggests. The real proc.run: the script
    fails to start."""
    monkeypatch.setattr(project, "ROOT", tmp_path)
    monkeypatch.setattr(tasks, "ROOT", tmp_path)
    (tmp_path / "tools").mkdir()
    script = tmp_path / "tools" / "crlf"
    script.write_bytes(b"#!/bin/sh\r\necho crlf\r\n")
    script.chmod(0o755)
    cfg = make({"tasks": {"t5": {"cmd": ["tools/crlf"], "uv": False}}})
    with pytest.raises(PytError) as e:
        tasks.run_task(cfg, "t5", [], lambda argv: 0)
    message = str(e.value)
    assert e.value.code == 3 and message.startswith("cannot run tools/crlf: its #! line ends with a carriage return"), message
    line = re.search(r"a line such as `([^`]+)` in \.gitattributes", message)
    assert line is not None and line.group(1) == "tools/crlf text eol=lf", message
    git = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "no-gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=git, check=True)
    (tmp_path / ".gitattributes").write_text(line.group(1) + "\n", encoding="utf-8")
    attrs = subprocess.run(["git", "check-attr", "eol", "--", "tools/crlf"], cwd=tmp_path, env=git, capture_output=True, text=True, check=True).stdout
    assert attrs.strip() == "tools/crlf: eol: lf", attrs


@pytest.mark.parametrize(("name", "message"), [("missing", "folder not found"), ("a-file", "not a folder")])
def test_a_bad_working_folder_is_named(name: str, message: str, tmp_path: Path) -> None:
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    with pytest.raises(PytError) as e:
        proc.run([sys.executable, "-V"], cwd=tmp_path / name, echo=False)
    assert e.value.code == 2
    assert message in str(e.value) and name in str(e.value)
    assert "program not found" not in str(e.value)


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
def test_a_working_folder_it_cannot_enter_is_named(windows: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A working folder the child cannot enter (not searchable: another user's, mode 000): the
    message blamed the program, "cannot run ls: Permission denied  (is it executable? a script
    needs a #! line)". Popen raises what CPython raises there: on POSIX an OSError whose filename
    is the cwd (the child's chdir failed), on Windows ERROR_DIRECTORY (267)."""
    locked = tmp_path / "locked"
    locked.mkdir()

    class DirectoryError(NotADirectoryError):
        winerror = 267  # ERROR_DIRECTORY (Windows sets winerror; POSIX has no such attribute)

    def popen(args: list[str], **kwargs: Any) -> Any:
        if windows:
            raise DirectoryError(errno.ENOTDIR, "The directory name is invalid")
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), kwargs["cwd"])

    monkeypatch.setattr(proc, "IS_WINDOWS", windows)
    monkeypatch.setattr(proc.subprocess, "Popen", popen)
    # the lookup of a bare name on Windows: test_a_bare_program_never_runs_from_the_callers_folder_on_windows
    monkeypatch.setattr(proc, "program", lambda name: name)
    with pytest.raises(PytError) as e:
        proc.run(["ls"], cwd=locked, echo=False)
    assert e.value.code == 2
    assert str(e.value).startswith(f"cannot enter the working folder {proc.rel(locked)}: "), str(e.value)
    assert str(e.value).endswith("(the working folder of ls)") and "#! line" not in str(e.value)


@posix
def test_a_working_folder_it_cannot_enter_is_named_for_real(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        if os.access(locked, os.X_OK):
            pytest.skip("this user enters any folder (root)")
        with pytest.raises(PytError, match=re.escape(f"cannot enter the working folder {proc.rel(locked)}: Permission denied")):
            proc.run([sys.executable, "-V"], cwd=locked, echo=False)
    finally:
        locked.chmod(0o755)


def test_a_file_windows_cannot_start_gets_the_windows_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `uv = false` task whose program is a .sh or .py: CreateProcess says WinError 193, and
    the hint asked for an exec bit or a #! line, which change nothing there."""

    def popen(args: list[str], **kwargs: Any) -> Any:
        raise OSError(errno.ENOEXEC, "%1 is not a valid Win32 application")

    monkeypatch.setattr(proc, "IS_WINDOWS", True)
    monkeypatch.setattr(proc.subprocess, "Popen", popen)
    with pytest.raises(PytError) as e:
        proc.run(["tools\\gen.sh"], echo=False)
    assert e.value.code == 2 and "#! line" not in str(e.value)
    assert str(e.value) == f"cannot run tools\\gen.sh: %1 is not a valid Win32 application  ({proc.WINDOWS_START_HINT})"


def test_a_dry_run_does_not_need_the_working_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A previous step would create it: a dry run only prints the command
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert proc.run([sys.executable, "-V"], cwd=tmp_path / "later").returncode == 0


def test_run_restores_the_sigint_handler(tmp_path: Path) -> None:
    before = signal.getsignal(signal.SIGINT)
    proc.run([sys.executable, "-c", "pass"], echo=False, cwd=tmp_path)
    assert signal.getsignal(signal.SIGINT) is before
    with pytest.raises(PytError):
        proc.run([str(tmp_path / "missing")], echo=False)
    assert signal.getsignal(signal.SIGINT) is before


def test_run_works_outside_the_main_thread(tmp_path: Path) -> None:
    # signal.signal() only works in the main thread: other threads leave SIGINT alone
    codes: list[int] = []
    worker = threading.Thread(target=lambda: codes.append(proc.run([sys.executable, "-c", "raise SystemExit(4)"], check=False, echo=False, cwd=tmp_path).returncode))
    worker.start()
    worker.join(60)
    assert codes == [4]


def test_find_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "uv"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("UV", str(fake))
    assert proc.find_uv() == str(fake)
    monkeypatch.setenv("UV", str(tmp_path))  # a folder is not uv
    elsewhere = str(tmp_path / "elsewhere" / "uv")  # absolute on every OS (a drive on Windows)
    monkeypatch.setattr(proc.shutil, "which", lambda _name: elsewhere)
    assert proc.find_uv() == elsewhere
    monkeypatch.delenv("UV")
    monkeypatch.setattr(proc.shutil, "which", lambda _name: None)
    with pytest.raises(PytError, match="uv not found") as e:
        proc.find_uv()
    assert e.value.code == 3


# --- programs by name, never from the caller's folder (Windows) ---------------------------------


def _windows_which(cmd: str, mode: int = os.F_OK | os.X_OK, path: str | None = None) -> str | None:
    """CPython's shutil.which on Windows (3.11; 3.12+ without NoDefaultCurrentDirectoryInExePath):
    the current folder first, then PATH, each with the extensions of PATHEXT; it returns the
    relative .\\git.bat it finds in the current folder."""
    value = os.environ.get("PATH", "") if path is None else path
    if not value:  # PATH='' finds nothing, the current folder neither
        return None
    entries = value.split(os.pathsep)
    exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    names = [cmd] if any(cmd.lower().endswith(e.lower()) for e in exts) else [cmd + e for e in exts]
    for folder in [os.curdir, *entries]:
        for name in names:
            if os.path.isfile(os.path.join(folder, name)):
                return os.path.join(folder, name)
    return None


@pytest.fixture
def windows_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Windows simulated for proc (its flag, PATHEXT, CPython's shutil.which there), the runner in
    a caller's folder `here` where another user left a git.bat, git.exe and nvim.cmd, and the real
    programs in `bin`, on PATH. Returns `bin`."""
    here, bindir = tmp_path / "here", tmp_path / "bin"
    for folder, names in ((here, ("git.bat", "git.exe", "nvim.cmd", "pwsh.exe", "only-here.exe")), (bindir, ("git.exe", "nvim.cmd", "pwsh.exe"))):
        folder.mkdir()
        for name in names:
            (folder / name).write_bytes(b"planted" if folder == here else b"real")
    monkeypatch.chdir(here)
    monkeypatch.setattr(proc, "IS_WINDOWS", True)
    monkeypatch.setenv("PATHEXT", ".com;.exe;.bat;.cmd")  # lower case: Linux file names are case-sensitive
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "here-too", str(bindir)]))  # empty and relative entries name the current folder too
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.delenv("windir", raising=False)
    monkeypatch.setattr(shutil, "which", _windows_which)
    return bindir


def test_find_program_never_takes_the_callers_folder_on_windows(windows_caller: Path) -> None:
    """shutil.which searches the current folder first on Windows, and the runner's is the
    caller's (the launchers never cd): `pyt doctor` or `pyt new` typed in a folder where another
    user left a git.bat (any authenticated user may write into a folder made under C:\\) ran it
    as the caller. The lookup takes PATH's absolute entries alone, and answers an absolute path."""
    bindir = windows_caller
    assert os.path.dirname(shutil.which("git") or "") == os.curdir  # the scenario: CPython's own answer, the planted one
    assert proc.find_program("git") == str(bindir / "git.exe")
    assert proc.find_program("nvim", path=os.environ["PATH"]) == str(bindir / "nvim.cmd")  # cmd_nvim.which passes PATH
    assert proc.find_program("only-here") is None  # found only in the caller's folder: not found
    assert proc.find_program("git", path="") is None  # an empty PATH holds nothing, as for shutil.which


def test_find_program_keeps_an_answer_that_names_no_folder_below_the_current_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an answer in the current folder or below it (shutil.which's .\\git.bat, an empty or
    relative PATH entry's) is searched again on Windows; a rooted one is PATH's own and stays,
    such as the /usr/bin/xvfb-run tests give e2e on every OS."""
    below = proc._below_the_current_folder
    for found in (os.path.join(os.curdir, "git.bat"), "git.exe", os.path.join("bin", "git.exe")):
        assert below(found), found
    for found in ("/usr/bin/xvfb-run", "\\bin\\git.exe"):
        assert not below(found), found
    if sys.platform == "win32":  # drives exist only there
        assert not below("C:\\x\\git.exe") and not below("\\\\server\\share\\git.exe") and below("C:bin\\git.exe")
    monkeypatch.setattr(proc, "IS_WINDOWS", True)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/xvfb-run")
    assert proc.find_program("xvfb-run") == "/usr/bin/xvfb-run"


def test_doctors_nvim_and_powershell_never_come_from_the_callers_folder_on_windows(windows_caller: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`pyt doctor` (global mode too) starts nvim (its Neovim step, cmd_nvim.which) and both
    PowerShells (the execution policies, shells._ps_policies): an nvim.cmd or pwsh.exe left in
    the folder it was typed in ran instead."""
    from runner import cmd_nvim

    bindir = windows_caller
    assert cmd_nvim.which("nvim") == str(bindir / "nvim.cmd")
    ran: list[str] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        ran.append(argv[0])
        return subprocess.CompletedProcess(argv, 0, "RemoteSigned\n", "")

    monkeypatch.setattr(shells.subprocess, "run", run)
    assert shells._ps_policies() == [("PowerShell 7", "Core", "RemoteSigned")]  # no powershell.exe on PATH
    assert ran == [str(bindir / "pwsh.exe")]


def test_a_bare_program_never_runs_from_the_callers_folder_on_windows(windows_caller: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess hands CreateProcess a bare name (lpApplicationName = NULL), and CreateProcess
    looks in the folder of the runner's python.exe and the runner's current folder (the caller's)
    before the system folders and PATH: `proc.run(["git", ...])` (setup's exec-bit fix, the hook,
    rename) ran a git.exe left there. proc.run hands it the program of the system folders or PATH,
    and a name found in neither never reaches CreateProcess."""
    bindir = windows_caller
    started: list[list[str]] = []

    class Started(Exception):
        pass

    def popen(args: list[str], **kwargs: Any) -> Any:
        started.append(list(args))
        raise Started

    monkeypatch.setattr(proc.subprocess, "Popen", popen)
    with pytest.raises(Started):
        proc.run(["git", "rev-parse"], cwd=tmp_path, echo=False)
    assert started[-1] == [str(bindir / "git.exe"), "rev-parse"]
    with pytest.raises(PytError, match=r"^program not found: only-here$") as e:
        proc.run(["only-here"], cwd=tmp_path, echo=False)
    assert e.value.code == 3 and len(started) == 1  # never handed to CreateProcess bare
    with pytest.raises(PytError, match="program not found: nvim"):
        proc.run(["nvim"], cwd=tmp_path, echo=False)  # CreateProcess takes nvim.exe only: nvim.cmd is no candidate
    # the system folders come first, as for CreateProcess; a name with a folder is no search
    system32 = tmp_path / "Windows" / "System32"
    system32.mkdir(parents=True)
    (system32 / "git.exe").write_bytes(b"system")
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    with pytest.raises(Started):
        proc.run(["git"], cwd=tmp_path, echo=False)
    assert started[-1] == [str(system32 / "git.exe")]
    with pytest.raises(Started):
        proc.run(["tools\\gen.exe"], cwd=tmp_path, echo=False)
    assert started[-1] == ["tools\\gen.exe"]
    # taskkill (the harnesses' tree kill) is the system folders' too
    killed: list[list[str]] = []
    (system32 / "taskkill.exe").write_bytes(b"system")
    (Path.cwd() / "taskkill.exe").write_bytes(b"planted")
    monkeypatch.setattr(proc.subprocess, "run", lambda argv, **kw: killed.append(list(argv)))
    proc.taskkill(42)
    assert killed == [[str(system32 / "taskkill.exe"), "/F", "/T", "/PID", "42"]]


@pytest.mark.parametrize(
    "where",
    ["new: the work tree", "new: git ls-files", "new: git init", "install: git", "doctor: git (global)", "doctor: launcher modes", "setup: exec bits", "hook: find_repo", "rename: git status"],
)
def test_the_runners_git_never_comes_from_the_callers_folder_on_windows(windows_caller: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    """The commands of A2-01 (`pyt new`, `pyt doctor`, setup, install, the hook, rename), each
    with a git.exe and a git.bat another user left in the folder they were typed in: every git
    they start is PATH's (the first one is caught before it runs)."""
    from runner import cmd_install, hooks, rename

    started: list[str] = []

    class Started(Exception):
        pass

    def popen(args: list[str], **kwargs: Any) -> Any:
        started.append(str(args[0]))
        raise Started

    monkeypatch.setattr(proc.subprocess, "Popen", popen)
    calls = {
        "new: the work tree": lambda: cmd_mode._work_tree_top(tmp_path / "game"),
        "new: git ls-files": lambda: presets._git_files("--cached"),
        "new: git init": lambda: presets._git_init(tmp_path / "game"),
        "install: git": lambda: cmd_install._git("rev-parse", "HEAD"),
        "doctor: git (global)": lambda: cmd_env._machine(lambda passed, label, hint="": None),
        "doctor: launcher modes": lambda: shells._git_modes(["pyt"]),
        "setup: exec bits": lambda: cmd_env._fix_exec_bit(),
        "hook: find_repo": lambda: hooks.find_repo(tmp_path, environ={}, cwd=tmp_path),
        "rename: git status": lambda: rename.git_changes(tmp_path),
    }
    with pytest.raises(Started):
        calls[where]()
    assert started == [str(windows_caller / "git.exe")]


def test_the_runner_finds_programs_only_through_proc() -> None:
    """Every lookup of a program by name goes through proc.find_program (never shutil.which,
    which searches the caller's folder first on Windows), and no process starts from a literal
    bare name past proc.run's own lookup (taskkill, git: proc.program, proc.taskkill)."""
    found: list[str] = []
    for path in sorted((TEMPLATE_DIR / "runner").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(TEMPLATE_DIR).as_posix()
        if name != "runner/proc.py":
            found += [f"{name}: shutil.which" for _ in re.finditer(r"\bshutil\.which\(", text)]
        found += [f"{name}: {m.group(0)}" for m in re.finditer(r"subprocess\.(?:run|Popen|call|check_call|check_output)\(\s*\[\s*[\"']", text)]
    assert found == []


def test_show_is_for_display() -> None:
    args = ["tool", "a b", "", "it's", 'q"uote', "x;y", "a&b", "--flag=1"]
    if not IS_WINDOWS:
        assert shlex.split(proc.show(args)) == args
    assert proc.show([ROOT / "tools" / "gen.sh", ROOT / "src", "x"]) == "tools/gen.sh src x"
    outside = Path(ROOT.anchor) / "pt-nowhere" / "bin" / "uv"
    assert proc.show([outside, "run"]) == "uv run"


# --- the environment of every child ---------------------------------------------------------------


@pytest.mark.parametrize("name", [*proc.UV_SELECTION, "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"])
def test_base_env_drops_what_would_move_uv_or_python(name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("FLET_X", "kept")
    monkeypatch.setenv("UV_INDEX_URL", "https://example.invalid/simple")  # the user's resolution settings stay
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    env = proc.base_env()
    assert name not in env
    assert env["PYTHONUTF8"] == "1"
    assert (env["FLET_X"], env["UV_INDEX_URL"], env["UV_CACHE_DIR"]) == ("kept", "https://example.invalid/simple", str(tmp_path / "cache"))
    venv = envs.cpython_env(make({}))
    uv_env = envs.env_vars(venv)
    expected = {"UV_PROJECT_ENVIRONMENT": str(venv.dir), "UV_PYTHON": venv.request}
    assert uv_env.get(name) == expected.get(name)  # set by the runner, or absent
    assert uv_env["UV_PYTHON_PREFERENCE"] == venv.preference


def test_the_selection_variables_include_the_known_conflicts() -> None:
    assert {"UV_PROJECT", "UV_NO_PROJECT", "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON"} <= set(proc.UV_SELECTION)
    assert "UV_INDEX_URL" not in proc.UV_SELECTION and "UV_CACHE_DIR" not in proc.UV_SELECTION


def test_a_user_uv_no_group_never_reaches_uv() -> None:
    """UV_NO_GROUP=dev (left over from a production or Docker setup) wins over `--all-groups`:
    `./pyt sync` uninstalled mypy, ruff and pytest, `check` ran PATH-wide ones and `test` found
    no pytest. It moves the groups like UV_NO_DEV, so the runner drops it too."""
    assert {"UV_NO_DEV", "UV_NO_DEFAULT_GROUPS", "UV_NO_GROUP"} <= set(proc.UV_SELECTION)


def test_base_env_removes_the_runners_own_bin_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    own = tmp_path / "script-env"
    bin_dir = own / ("Scripts" if IS_WINDOWS else "bin")
    monkeypatch.setattr(sys, "prefix", str(own))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), str(tmp_path / "a"), str(bin_dir), str(tmp_path / "b")]))
    parts = proc.base_env()["PATH"].split(os.pathsep)
    assert str(bin_dir) not in parts
    assert parts[:2] == [str(tmp_path / "a"), str(tmp_path / "b")]


@needs_uv
@pytest.mark.parametrize("name", ["UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON", "UV_PROJECT", "UV_NO_PROJECT", "UV_WORKING_DIR", "UV_ISOLATED"])
def test_uv_runs_in_the_projects_environment_whatever_the_user_exported(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Measured with uv 0.12: UV_(NO_)MANAGED_PYTHON -> exit 2 next to UV_PYTHON_PREFERENCE;
    # UV_PROJECT/UV_NO_PROJECT/UV_WORKING_DIR -> `--locked has no effect`, outside the project;
    # UV_ISOLATED -> a throwaway environment instead of .venv
    tool = envs.tool_env(config.load())
    if not tool.python.exists():
        pytest.skip(f"{tool.dir} does not exist (./pyt setup)")
    monkeypatch.setenv(name, str(tmp_path) if name in ("UV_PROJECT", "UV_WORKING_DIR") else "1")
    probe = "import os, sys; print(sys.prefix); print(os.getcwd())"
    r = envs.uv(tool, ["run", "--locked", "python", "-c", probe], check=False, capture=True, echo=False)
    assert r.returncode == 0, r.stderr
    prefix, cwd = r.stdout.splitlines()[:2]
    assert Path(prefix).resolve() == tool.dir.resolve()
    assert Path(cwd).resolve() == ROOT.resolve()
    assert "has no effect" not in r.stderr


# === 7. Ctrl+C: the child is waited for (POSIX; the Windows console never interrupted the wait) ====

CHILD = """\
import signal, sys, time
mode, marker = sys.argv[1], sys.argv[2]
if mode == "second":
    open(marker + ".second", "w").close()
    raise SystemExit(0)
got = []
if mode.startswith("trap"):
    signal.signal(signal.SIGINT, lambda *_: got.append(1))
if mode.startswith("term"):  # the SIGTERM/SIGHUP the runner passes on (a supervisor, kill PID)
    signal.signal(signal.SIGTERM, lambda *_: got.append(1))
    signal.signal(signal.SIGHUP, lambda *_: got.append(1))
print("CHILD-" + "READY", flush=True)
deadline = time.monotonic() + 30
while not got and time.monotonic() < deadline:
    time.sleep(0.02)  # mode "default": KeyboardInterrupt ends it here (death by SIGINT)
time.sleep(0.6)  # the cleanup: longer than the 0.25 s subprocess.run used to wait before SIGKILL
open(marker, "w").close()
raise SystemExit(int(mode[4:] or 0))
"""

DRIVER = """\
import sys
sys.path.insert(0, sys.argv[1])
from runner import cli, proc
child, marker, modes = sys.argv[2], sys.argv[3], sys.argv[4:]

def dispatch(argv):
    code = 0
    for mode in modes:  # the steps of one command (check: ruff, then mypy)
        code = proc.run([sys.executable, child, mode, marker], check=False).returncode
    return code

cli.dispatch = dispatch
raise SystemExit(cli.main(["x"]))
"""


def _terminal_signals() -> None:
    """In the driver before exec: the signals as a terminal session has them. A suite started
    in the background (`./pyt selftest &` in a script, nohup) ignores SIGINT (SIGHUP), the
    runner rightly keeps an inherited SIG_IGN, and these tests are about the terminal case."""
    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, signal.SIG_DFL)


def _interrupt(tmp_path: Path, *modes: str, sig: int = signal.SIGINT, group: bool = True) -> tuple[int, bool, str]:
    """Run DRIVER in its own session, press Ctrl+C (SIGINT to the process group, as a terminal
    does) once the child is ready; return (exit code, marker written before the exit, log).
    `sig`/`group`: another signal, sent to the driver alone (kill PID) when `group` is False."""
    child = tmp_path / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    marker = tmp_path / "marker"
    log = tmp_path / "log.txt"
    with log.open("wb") as out:
        p = subprocess.Popen(
            [sys.executable, "-c", DRIVER, str(TEMPLATE_DIR), str(child), str(marker), *modes],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=child_env(),
            start_new_session=True,
            preexec_fn=_terminal_signals,  # noqa: PLW1509 (no threads here)
        )
    try:
        deadline = time.monotonic() + 60
        while "CHILD-READY" not in log.read_text(encoding="utf-8", errors="replace"):
            if p.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"the child never got ready: {log.read_text(encoding='utf-8', errors='replace')}")
            time.sleep(0.02)
        if group:
            os.killpg(p.pid, sig)
        else:
            os.kill(p.pid, sig)
        code = p.wait(timeout=60)
        written = marker.exists()  # at the moment the driver returned
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
    return code, written, log.read_text(encoding="utf-8", errors="replace")


@posix
def test_ctrl_c_waits_for_a_child_that_cleans_up(tmp_path: Path) -> None:
    code, written, log = _interrupt(tmp_path, "trap")
    assert written, f"the child was killed before its cleanup: {log}"
    assert code == 130  # it exited 0, but the command was interrupted: never a success
    assert "error: interrupted" in log


@posix
def test_ctrl_c_reports_the_childs_own_exit_code(tmp_path: Path) -> None:
    code, written, log = _interrupt(tmp_path, "trap3")
    assert (code, written) == (3, True), log


@posix
def test_ctrl_c_still_exits_130_when_the_child_dies_of_it(tmp_path: Path) -> None:
    code, written, log = _interrupt(tmp_path, "default")
    assert (code, written) == (130, False), log
    assert "error: interrupted" in log


@posix
def test_ctrl_c_stops_the_remaining_steps(tmp_path: Path) -> None:
    code, written, log = _interrupt(tmp_path, "trap", "second")
    assert (code, written) == (130, True), log
    assert not (tmp_path / "marker.second").exists(), "a step ran after Ctrl+C"


@posix
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP] if hasattr(signal, "SIGHUP") else [])
def test_a_sigterm_to_the_runner_is_passed_on_and_waited_for(tmp_path: Path, sig: int) -> None:
    """kill PID, a supervisor, docker stop, Popen.terminate(): the signal reaches the runner
    alone. Its default action killed the runner at once (exit 143) and left uv and the app
    running as orphans that never got the signal."""
    code, written, log = _interrupt(tmp_path, "term", "second", sig=sig, group=False)
    assert written, f"the child did not get the signal, or was not waited for: {log}"
    assert code == 128 + sig, log  # the child exited 0 after its cleanup: never a success
    assert f"error: terminated ({signal.Signals(sig).name})" in log
    assert not (tmp_path / "marker.second").exists(), "a step ran after the SIGTERM"


@posix
def test_a_sigterm_passed_on_reports_the_childs_own_code(tmp_path: Path) -> None:
    code, written, log = _interrupt(tmp_path, "term5", sig=signal.SIGTERM, group=False)
    assert (code, written) == (5, True), log


@posix
def test_a_child_that_dies_of_the_sigterm_passed_on_leaves_no_orphan(tmp_path: Path) -> None:
    code, written, log = _interrupt(tmp_path, "default", sig=signal.SIGTERM, group=False)
    assert (code, written) == (143, False), log  # the child died of it: 128 + 15


@posix
@pytest.mark.usefixtures("default_signals")
def test_an_ignored_sigterm_is_passed_on_to_the_child() -> None:
    # nohup and supervisors that ignore SIGHUP/SIGTERM: the children keep inheriting SIG_IGN
    code = (
        "import signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); sys.path.insert(0, sys.argv[1]); "
        "from runner import proc; "
        "probe = 'import signal; print(signal.getsignal(signal.SIGTERM) == signal.SIG_IGN, signal.getsignal(signal.SIGHUP) == signal.SIG_DFL)'; "
        "print(proc.run([sys.executable, '-c', probe], capture=True, echo=False).stdout.strip()); "
        "print(signal.getsignal(signal.SIGTERM) == signal.SIG_IGN, signal.getsignal(signal.SIGHUP) == signal.SIG_DFL)"
    )
    r = subprocess.run([sys.executable, "-c", code, str(TEMPLATE_DIR)], capture_output=True, text=True, env=child_env(), timeout=60, check=False)
    assert r.stdout.split() == ["True", "True", "True", "True"], r.stderr  # the child's, then the runner's afterwards


@posix
def test_an_ignored_sigint_is_passed_on_to_the_child() -> None:
    # A runner started with SIGINT ignored (a background job of a script) must not give its
    # children a default SIGINT: a Ctrl+C meant for the foreground job would kill them
    code = (
        "import signal, sys; signal.signal(signal.SIGINT, signal.SIG_IGN); sys.path.insert(0, sys.argv[1]); "
        "from runner import proc; "
        "probe = 'import signal; print(signal.getsignal(signal.SIGINT) == signal.SIG_IGN)'; "
        "print(proc.run([sys.executable, '-c', probe], capture=True, echo=False).stdout.strip())"
    )
    r = subprocess.run([sys.executable, "-c", code, str(TEMPLATE_DIR)], capture_output=True, text=True, env=child_env(), timeout=60, check=False)
    assert r.stdout.strip() == "True", r.stderr


# === 8. a closed stdout (./pyt help | head -1) ==================================================


@posix
@pytest.mark.parametrize("unbuffered", [False, True])
@pytest.mark.parametrize("args", [["help"], ["help", "build"]])
def test_a_closed_stdout_is_not_a_runner_bug(args: list[str], unbuffered: bool) -> None:
    env = child_env()
    if unbuffered:
        env["PYTHONUNBUFFERED"] = "1"
    read_end, write_end = os.pipe()
    os.close(read_end)  # no reader: every write to stdout fails with EPIPE
    try:
        r = subprocess.run(
            [sys.executable, "-B", str(PYT_PY), *args],
            stdin=subprocess.DEVNULL,
            stdout=write_end,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    finally:
        os.close(write_end)
    for text in ("Traceback", "internal runner error", "Exception ignored"):
        assert text not in r.stderr, r.stderr
    assert r.returncode == 141  # 128 + SIGPIPE, what a shell pipeline reports


class _NoRoom(io.StringIO):
    """A stream on a full disk: every write fails with ENOSPC."""

    def write(self, text: str) -> int:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


@pytest.mark.parametrize("args", [["help"], ["help", "build"]])
def test_a_write_that_finds_no_room_is_one_error_line(args: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`./pyt help > /dev/full` (or a full disk under `./pyt help >> notes.txt`) printed a
    traceback and called it a bug in the runner."""
    monkeypatch.setattr(sys, "stdout", _NoRoom())
    assert cli.main(args) == 1
    err = capsys.readouterr().err
    assert "a write failed: " + os.strerror(errno.ENOSPC) in err and "internal runner error" not in err, err
    monkeypatch.setattr(sys, "stderr", _NoRoom())  # no room for the error line either: exit 1 quietly
    assert cli.main(args) == 1


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="no /dev/full here")
@pytest.mark.parametrize("args", [["help"], ["help", "build"]])
def test_output_to_dev_full_is_no_runner_bug(args: list[str]) -> None:
    with open("/dev/full", "wb") as full:
        r = subprocess.run(
            [sys.executable, "-B", str(PYT_PY), *args], stdin=subprocess.DEVNULL, stdout=full, stderr=subprocess.PIPE,
            env=child_env(), text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
        )  # fmt: skip
    for text in ("Traceback", "internal runner error", "Exception ignored"):
        assert text not in r.stderr, r.stderr
    assert r.returncode == 1 and "a write failed" in r.stderr, r.stderr


# === 9. [tasks] =====================================================================================


class Recorder:
    """Fakes for tasks.envs.uv_run and tasks.proc.run, and a dispatcher: they record the argv
    (and uv's environment key and the keyword arguments) and return a code per program name."""

    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.codes = codes or {}
        self.runs: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.dispatched: list[list[str]] = []

    def uv_run(self, env: envs.PyEnv, argv: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.runs.append([f"uv:{env.key}", *map(str, argv)])
        self.kwargs.append(kw)
        return completed(argv, self.codes.get(str(argv[0]), 0))

    def run(self, argv: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.runs.append([str(a) for a in argv])
        self.kwargs.append(kw)
        return completed(argv, self.codes.get(Path(str(argv[0])).name, 0))

    def dispatch(self, argv: list[str]) -> int:
        self.dispatched.append(argv)
        return self.codes.get(argv[0], 0)


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder()
    monkeypatch.setattr(tasks.envs, "uv_run", r.uv_run)
    monkeypatch.setattr(tasks.proc, "run", r.run)
    # The programs recorded here exist nowhere, and on Windows run_task refuses a bare name the
    # task's PATH lacks before it reaches proc.run: POSIX rules, unless a test of that lookup
    # sets tasks.IS_WINDOWS itself.
    monkeypatch.setattr(tasks, "IS_WINDOWS", False)
    return r


def test_deps_run_in_order_then_the_cmd_with_the_extra_arguments(rec: Recorder) -> None:
    cfg = make({"tasks": {"gen": {"cmd": ["tool", "{backend}"], "deps": ["check all", "other --x"], "uv": False}, "other": {"cmd": ["o"], "uv": False}}})
    assert tasks.run_task(cfg, "gen", ["--y", "{root}", "a b"], rec.dispatch) == 0
    assert rec.dispatched == [["check", "all"]]
    assert rec.runs == [["o", "--x"], ["tool", "cpython", "--y", "{root}", "a b"]]  # extra args are never formatted


def test_a_backslash_in_a_dep_is_a_plain_character(rec: Recorder) -> None:
    """A Windows path in a deps entry reaches the command as typed (POSIX shlex turned
    C:\\data\\in.txt into C:datain.txt); quotes still group words, as for [vscode] buttons and
    the Neovim plugin's :Pyt."""
    deps = [r"run cpython C:\data\in.txt", r'run cpython "C:\My Data\x" a\b', r"other 'say \"hi\"'"]
    cfg = make({"tasks": {"t": {"deps": deps}, "other": {"cmd": ["o"], "uv": False}}})
    assert tasks.run_task(cfg, "t", [], rec.dispatch) == 0
    assert rec.dispatched == [["run", "cpython", r"C:\data\in.txt"], ["run", "cpython", r"C:\My Data\x", "a\\b"]]
    assert rec.runs == [["o", r"say \"hi\""]]


def test_the_first_failing_dep_stops_the_task_with_its_code(rec: Recorder) -> None:
    cfg = make({"tasks": {"t": {"cmd": ["tool"], "deps": ["check", "other", "test"], "uv": False}, "other": {"cmd": ["o"], "uv": False}}})
    rec.codes.update(check=0, o=4)
    assert tasks.run_task(cfg, "t", [], rec.dispatch) == 4
    assert rec.dispatched == [["check"]] and rec.runs == [["o"]]  # neither `test` nor the cmd ran
    rec.codes.update(check=7)
    assert tasks.run_task(cfg, "t", [], rec.dispatch) == 7


def test_the_cmds_exit_code_is_the_tasks(rec: Recorder) -> None:
    cfg = make({"tasks": {"t": {"cmd": ["tool"]}, "u": {"cmd": ["tool"], "uv": False}}})
    rec.codes["tool"] = 3
    assert tasks.run_task(cfg, "t", [], rec.dispatch) == 3
    assert tasks.run_task(cfg, "u", [], rec.dispatch) == 3


@pytest.mark.parametrize(
    ("spec", "start", "cycle"),
    [
        ({"a": {"deps": ["b"]}, "b": {"deps": ["a"]}}, "a", "a -> b -> a"),
        ({"a": {"deps": ["a"]}}, "a", "a -> a"),
        ({"a": {"deps": ["b"]}, "b": {"deps": ["c"]}, "c": {"deps": ["b"]}}, "a", "a -> b -> c -> b"),
    ],
)
def test_task_cycles_are_reported(spec: dict[str, Any], start: str, cycle: str, rec: Recorder) -> None:
    with pytest.raises(PytError, match=f"task cycle: {re.escape(cycle)}$") as e:
        tasks.run_task(make({"tasks": spec}), start, [], rec.dispatch)
    assert e.value.code == 2


def test_a_deps_only_task_rejects_arguments_before_anything_runs(rec: Recorder) -> None:
    cfg = make({"tasks": {"ci": {"deps": ["check all", "test all"]}, "outer": {"deps": ["check", "ci --help"]}}})
    for extra in (["--help"], ["mypyc"], ["a b"]):
        with pytest.raises(PytError, match="only runs its deps .check all, test all. and takes no arguments") as e:
            tasks.run_task(cfg, "ci", extra, rec.dispatch)
        assert e.value.code == 2 and shlex.join(extra) in str(e.value)
    assert rec.dispatched == []
    with pytest.raises(PytError, match="takes no arguments: --help"):
        tasks.run_task(cfg, "outer", [], rec.dispatch)  # a deps entry that passes arguments to it
    assert rec.dispatched == [["check"]]
    rec.dispatched.clear()
    assert tasks.run_task(cfg, "ci", [], rec.dispatch) == 0
    assert rec.dispatched == [["check", "all"], ["test", "all"]]


def test_a_shared_dependency_runs_once_per_invocation(rec: Recorder) -> None:
    cfg = make(
        {
            "tasks": {
                "a": {"deps": ["b", "c", "check all"], "cmd": ["a"], "uv": False},
                "b": {"deps": ["d", "check all"], "cmd": ["b"], "uv": False},
                "c": {"deps": ["d", "check all", "check"], "cmd": ["c"], "uv": False},
                "d": {"cmd": ["d"], "uv": False},
            }
        }
    )
    assert tasks.run_task(cfg, "a", [], rec.dispatch) == 0
    assert rec.runs == [["d"], ["b"], ["c"], ["a"]]
    assert rec.dispatched == [["check", "all"], ["check"]]  # `check` differs from `check all`
    tasks.run_task(cfg, "a", [], rec.dispatch)  # a new invocation runs them again
    assert rec.runs[4:] == [["d"], ["b"], ["c"], ["a"]]


def test_every_placeholder(rec: Recorder) -> None:
    cfg = make(
        {
            "app": {"name": "My-App"},
            "tasks": {"t": {"cmd": ["{root}", "{src}", "{build}", "{dist}", "{backend}", "{name}", "{pkg}", "{{x}}", "a{{b}}c"], "env": {"OUT": "{build}/x"}, "cwd": "{src}", "uv": False}},
        }
    )
    tasks.run_task(cfg, "t", [], rec.dispatch)
    assert rec.runs == [[str(ROOT), str(SRC), str(BUILD), str(DIST), "cpython", "My-App", "my_app", "{x}", "a{b}c"]]
    assert rec.kwargs[0]["cwd"] == SRC
    assert rec.kwargs[0]["env"]["OUT"] == f"{BUILD}/x"


def test_literal_braces_are_written_doubled(rec: Recorder) -> None:
    cfg = make({"tasks": {"t": {"cmd": ["python", "-c", "d = {{}}; print({{'a': 1}})"], "uv": False}}})
    tasks.run_task(cfg, "t", [], rec.dispatch)
    assert rec.runs == [["python", "-c", "d = {}; print({'a': 1})"]]


BAD_BRACES = ["{}", "{", "}", "x{0}", "{name!r}", "{name:>9}", "{name.upper}", "{name[0]}", "{name[a]}", "{root"]


def _bad_task(where: str, bad: str) -> dict[str, Any]:
    spec: dict[str, Any] = {"cmd": ["echo", "ok"], "uv": False}
    if where == "cmd":
        spec["cmd"] = ["python", "-c", f"print({bad})"]
    elif where == "env":
        spec["env"] = {"A": bad}
    else:
        spec["cwd"] = bad
    return spec


@pytest.mark.parametrize("where", ["cmd", "env", "cwd"])
@pytest.mark.parametrize("bad", BAD_BRACES)
def test_bad_braces_are_config_errors(where: str, bad: str) -> None:
    with pytest.raises(PytError, match=rf"tasks\.t\.{where}") as e:
        make({"tasks": {"t": _bad_task(where, bad)}})
    assert e.value.code == 2 and "{{ and }}" in str(e.value)


@pytest.mark.parametrize("where", ["cmd", "env", "cwd"])
@pytest.mark.parametrize("bad", BAD_BRACES)
def test_bad_braces_never_crash_a_task(where: str, bad: str, rec: Recorder) -> None:
    cfg = unchecked({"tasks": {"t": _bad_task(where, bad)}})  # a Config that skipped validate
    with pytest.raises(PytError, match="task 't'") as e:
        tasks.run_task(cfg, "t", [], rec.dispatch)
    assert e.value.code == 2 and "{{ and }}" in str(e.value)
    assert rec.runs == []


def test_an_unknown_placeholder_is_reported_when_the_task_runs(rec: Recorder) -> None:
    cfg = make({"tasks": {"t": {"cmd": ["{nope}"], "uv": False}, "ok": {"cmd": ["x"]}}})  # it loads
    with pytest.raises(PytError, match="unknown placeholder 'nope'") as e:
        tasks.run_task(cfg, "t", [], rec.dispatch)
    assert e.value.code == 2 and "{python}" in str(e.value)


@pytest.mark.parametrize(
    ("bad", "where"),
    [
        ({"cmd": ["echo", "{roots}"]}, "badph"),
        ({"cmd": ["echo"], "env": {"X": "{srcs}"}}, "badph"),
        ({"cmd": ["echo"], "cwd": "{bulid}"}, "badph"),
        ({"cmd": ["echo"], "deps": ["gen", "check all", "inner"]}, "inner"),  # a task the deps reach
    ],
)
def test_an_unknown_placeholder_is_refused_before_any_dep_runs(rec: Recorder, bad: dict[str, Any], where: str) -> None:
    """A typo in a placeholder of a task with deps (`ci` with check all and test all) was reported
    only after every dep had run, which can take minutes."""
    cfg = make({"tasks": {
        "badph": {"deps": ["gen", "check all"], "uv": False, **bad},
        "gen": {"cmd": ["g"], "uv": False},
        "inner": {"cmd": ["i", "{pkgs}"], "uv": False},
    }})  # fmt: skip
    with pytest.raises(PytError, match=f"task '{where}': unknown placeholder") as e:
        tasks.run_task(cfg, "badph", [], rec.dispatch)
    assert e.value.code == 2
    assert rec.runs == [] and rec.dispatched == []  # neither gen nor check all ran


@pytest.mark.parametrize(
    ("task", "message"),
    [
        ("typo", r"task 'typo': deps entry 'tset all': unknown command: tset  \(./pyt help"),
        ("inner-typo", r"task 'inner': deps entry 'chek': unknown command: chek"),  # a task the deps reach
        ("old", r"task 'old': deps entry 'init script': init is no longer a ./pyt command"),
        ("bench", r"backend 'pypy' is not in backend.supported"),  # uv = true runs in PyPy's environment
        ("bench-python", r"backend 'pypy' is not in backend.supported"),  # {python} names its interpreter
    ],
)
def test_a_deps_entry_or_backend_that_cannot_run_is_refused_before_any_dep_runs(rec: Recorder, task: str, message: str) -> None:
    """A deps entry that names no command (`tset all`) or a task backend the project does not
    support stopped the task only once the deps before it had run (`fmt --check`, `test all`:
    minutes), like a placeholder typo did."""
    cfg = make({"backend": {"supported": ["cpython", "mypyc"]}, "tasks": {
        "typo": {"deps": ["fmt --check", "tset all"]},
        "inner-typo": {"deps": ["fmt --check", "inner"]},
        "inner": {"deps": ["chek"]},
        "old": {"deps": ["fmt --check", "init script"]},
        "bench": {"deps": ["fmt --check"], "cmd": ["python", "-c", "print('bench')"], "backend": "pypy"},
        "bench-python": {"deps": ["fmt --check"], "cmd": ["{python}", "-c", "pass"], "uv": False, "backend": "pypy"},
    }})  # fmt: skip
    with pytest.raises(PytError, match=message) as e:
        tasks.run_task(cfg, task, [], rec.dispatch)
    assert e.value.code == 2
    assert rec.runs == [] and rec.dispatched == []  # fmt --check never ran


def test_a_task_on_an_unsupported_backend_runs_when_it_needs_no_environment(rec: Recorder) -> None:
    """A uv = false task without {python} runs no interpreter of its backend: its deps and cmd run."""
    cfg = make({"backend": {"supported": ["cpython"]}, "tasks": {"t": {"deps": ["fmt --check", "help"], "cmd": ["tool"], "uv": False, "backend": "pypy"}}})
    assert tasks.run_task(cfg, "t", [], rec.dispatch) == 0
    assert rec.dispatched == [["fmt", "--check"], ["help"]] and rec.runs == [["tool"]]


@pytest.mark.parametrize("key", ["A=B", "", "1X", "A B", "A-B", chr(0xE9)])
def test_task_env_names_are_validated(key: str) -> None:
    with pytest.raises(PytError, match=r"tasks\.t\.env'?: invalid environment variable name") as e:
        make({"tasks": {"t": {"cmd": ["x"], "env": {key: "v"}}}})
    assert e.value.code == 2


@pytest.mark.parametrize("cmd", [[""], ["  ", "x"]])
def test_a_task_program_cannot_be_empty(cmd: list[str]) -> None:
    with pytest.raises(PytError, match=r"tasks\.t\.cmd: the program"):
        make({"tasks": {"t": {"cmd": cmd}}})


@pytest.mark.parametrize(("dep", "message"), [("run 'x", "No closing quotation"), ("", "empty deps entry"), ("   ", "empty deps entry")])
def test_a_bad_deps_entry_is_reported_before_anything_runs(dep: str, message: str, rec: Recorder) -> None:
    # Loads (vscode.scan renders such a task), fails when the task runs: before its first dep
    cfg = make({"tasks": {"t": {"cmd": ["x"], "deps": ["check", dep]}}})
    with pytest.raises(PytError, match=message) as e:
        tasks.run_task(cfg, "t", [], rec.dispatch)
    assert e.value.code == 2 and "task 't'" in str(e.value)
    assert rec.dispatched == [] and rec.runs == []


@pytest.mark.parametrize("uv", [True, False])
@pytest.mark.parametrize("cwd", ["no-such-dir-xyz", "pyproject.toml"])
def test_a_task_cwd_must_be_a_folder(cwd: str, uv: bool, rec: Recorder) -> None:
    cfg = make({"tasks": {"t": {"cmd": ["python", "-V"], "cwd": cwd, "uv": uv}}})
    with pytest.raises(PytError) as e:
        tasks.run_task(cfg, "t", [], rec.dispatch)
    assert e.value.code == 2
    assert cwd in str(e.value) and "program not found" not in str(e.value)
    assert rec.runs == []


def test_a_dry_run_does_not_check_the_task_cwd(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    # A dep would create it, and a dry run skips the deps
    monkeypatch.setattr(proc, "DRY_RUN", True)
    cfg = make({"tasks": {"t": {"cmd": ["tool"], "cwd": "later", "deps": ["build"]}}})
    assert tasks.run_task(cfg, "t", [], rec.dispatch) == 0
    assert rec.runs == [["uv:cpython", "tool"]]


def test_a_task_backend_must_be_supported_where_its_environment_is_used(rec: Recorder) -> None:
    cfg = make(
        {
            "backend": {"active": "cpython", "supported": ["cpython", "mypyc"]},
            "tasks": {
                "uvrun": {"cmd": ["python", "-V"], "backend": "pypy"},
                "needs": {"cmd": ["{python}", "-V"], "backend": "pypy", "uv": False},
                "label": {"cmd": ["tool", "{backend}"], "backend": "pypy", "uv": False},
                "venv": {"cmd": ["python", "-V"], "backend": "mypyc"},
            },
        }
    )
    for name in ("uvrun", "needs"):
        with pytest.raises(PytError, match=r"mode --supports \+pypy") as e:
            tasks.run_task(cfg, name, [], rec.dispatch)
        assert e.value.code == 2
    assert rec.runs == []
    # No environment needed: {backend} is a label; mypyc tasks run interpreted in .venv
    assert tasks.run_task(cfg, "label", [], rec.dispatch) == 0
    assert tasks.run_task(cfg, "venv", [], rec.dispatch) == 0
    assert rec.runs == [["tool", "pypy"], ["uv:cpython", "python", "-V"]]


def test_a_uv_task_runs_in_its_backends_environment(rec: Recorder) -> None:
    cfg = make({"backend": {"supported": ["cpython", "pypy", "mypyc"]}, "tasks": {"t": {"cmd": ["python", "x.py"], "backend": "pypy", "cwd": "src", "env": {"A": "1"}}}})
    tasks.run_task(cfg, "t", ["y"], rec.dispatch)
    assert rec.runs == [["uv:pypy", "python", "x.py", "y"]]
    assert rec.kwargs[0] == {"cwd": SRC, "extra_env": {"A": "1"}, "check": False}  # cwd != ROOT: uv_run adds --project


def test_a_plain_task_gets_a_clean_environment(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/elsewhere")
    monkeypatch.setenv("UV_NO_SYNC", "1")
    cfg = make({"tasks": {"t": {"cmd": ["tool"], "uv": False, "env": {"SEED": "42", "PYTHONUTF8": "0"}}}})
    tasks.run_task(cfg, "t", [], rec.dispatch)
    env = rec.kwargs[0]["env"]
    assert env["SEED"] == "42" and env["PYTHONUTF8"] == "0"  # the task's env wins
    assert not {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_NO_SYNC"} & set(env)


def test_a_plain_task_returns_its_programs_exit_code() -> None:
    cfg = make({"tasks": {"t": {"cmd": [sys.executable, "-c", "raise SystemExit(7)"], "uv": False}}})
    assert tasks.run_task(cfg, "t", [], fail) == 7


@posix
def test_a_plain_task_killed_by_a_signal_is_128_plus_n() -> None:
    cfg = make({"tasks": {"t": {"cmd": [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"], "uv": False}}})
    assert tasks.run_task(cfg, "t", [], fail) == 137


def test_python_placeholder_of_a_missing_environment_syncs_it(rec: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh clone, clean --envs, git clean -fdx: uv = false never goes through `uv run`
    venv = envs.PyEnv("cpython", tmp_path / ".venv", "3.14", "only-managed")
    monkeypatch.setattr(envs, "cpython_env", lambda _cfg: venv)
    synced: list[envs.PyEnv] = []
    monkeypatch.setattr(envs, "sync", lambda env, **_kw: synced.append(env))
    cfg = make({"tasks": {"pyv": {"cmd": ["{python}", "-V"], "uv": False}, "plain": {"cmd": ["tool"], "uv": False}}})
    assert tasks.run_task(cfg, "pyv", ["x"], rec.dispatch) == 0
    assert synced == [venv]
    assert rec.runs == [[str(venv.python), "-V", "x"]]
    tasks.run_task(cfg, "plain", [], rec.dispatch)
    assert synced == [venv]  # no {python}: nothing to sync
    venv.python.parent.mkdir(parents=True)
    venv.python.write_text("", encoding="utf-8")
    tasks.run_task(cfg, "pyv", [], rec.dispatch)
    assert synced == [venv]  # it exists now


def test_a_relative_program_runs_from_the_task_cwd(rec: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows (CreateProcess) looks for a relative program in the runner's cwd, the caller's folder
    work = tmp_path / "work"
    (work / "tools").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    spellings = ["tools/x", "./tools/x", *(["tools\\x"] if IS_WINDOWS else [])]
    specs = {f"t{i}": {"cmd": [s, "arg"], "cwd": str(work), "uv": False} for i, s in enumerate(spellings)}
    specs["bare"] = {"cmd": ["tool"], "cwd": str(work), "uv": False}
    specs["abs"] = {"cmd": ["{root}/tools/x"], "uv": False}
    cfg = make({"tasks": specs})
    monkeypatch.chdir(elsewhere)
    for i in range(len(spellings)):
        rec.runs.clear()
        rec.kwargs.clear()
        tasks.run_task(cfg, f"t{i}", ["extra"], rec.dispatch)
        assert Path(rec.runs[0][0]).resolve() == (work / "tools" / "x").resolve()
        assert rec.runs[0][1:] == ["arg", "extra"] and rec.kwargs[0]["cwd"] == work
    rec.runs.clear()
    tasks.run_task(cfg, "bare", [], rec.dispatch)
    tasks.run_task(cfg, "abs", [], rec.dispatch)
    assert rec.runs == [["tool"], [f"{ROOT}/tools/x"]]  # a bare name keeps the OS search


def _batch(folder: Path, name: str) -> Path:
    """A stand-in .cmd file (never run: the recorder takes its place)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("@echo off\r\n", encoding="ascii")
    return folder / name


def test_a_bare_program_is_found_with_pathext_on_windows(rec: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: CreateProcess only tries `npm.exe` for a bare `npm`, so a task running npm, yarn
    or mvn (.cmd files) failed with "program not found: npm" there and worked elsewhere. The
    search is the task's PATH alone: shutil.which looked in the current folder first, the
    caller's (the launchers never cd), and a same-named npm.cmd there ran instead. A name the
    task's PATH lacks went to CreateProcess bare, which looks in the runner's own folder, the
    caller's folder and the runner's PATH first: an npm.exe there ran. It is not found now."""
    npm = _batch(tmp_path / "nodejs", "npm.cmd")
    _batch(tmp_path / "caller", "npm.cmd")  # the folder ./pyt web was typed in: never searched
    (tmp_path / "caller" / "npm.exe").write_bytes(b"MZ")  # what CreateProcess finds there for a bare npm
    monkeypatch.chdir(tmp_path / "caller")
    monkeypatch.setattr(tasks, "IS_WINDOWS", True)
    monkeypatch.setenv("PATHEXT", ".com;.exe;.bat;.cmd")  # lower case: Linux file names are case-sensitive
    work = tmp_path / "work"
    tool = _batch(work / "bin", "tool.bat")
    script = tmp_path / "scripts" / "gen.py"
    script.parent.mkdir()
    script.write_text("print('gen')\n", encoding="utf-8")
    cfg = make({"tasks": {
        "web": {"cmd": ["npm", "run", "build"], "uv": False, "env": {"PATH": str(npm.parent)}},
        "missing": {"cmd": ["npm"], "uv": False, "env": {"PATH": str(tmp_path / "empty")}},
        "rel": {"cmd": ["tools/x.cmd"], "uv": False},
        "named": {"cmd": ["npm.cmd"], "uv": False, "env": {"PATH": str(npm.parent)}},
        "relpath": {"cmd": ["tool"], "uv": False, "env": {"PATH": "bin"}, "cwd": str(work)},
        "script": {"cmd": ["gen.py"], "uv": False, "env": {"PATH": str(script.parent)}},
    }})
    tasks.run_task(cfg, "web", ["--prod"], rec.dispatch)
    assert rec.runs[-1] == [str(npm), "run", "build", "--prod"]  # the task's own PATH, never the caller's folder
    ran = len(rec.runs)
    with pytest.raises(PytError, match=r"^program not found: npm  \(looked for on the task's PATH") as e:
        tasks.run_task(cfg, "missing", [], rec.dispatch)
    assert e.value.code == 3 and len(rec.runs) == ran  # never handed to CreateProcess bare
    monkeypatch.setattr(tasks.proc, "DRY_RUN", True)
    tasks.run_task(cfg, "missing", [], rec.dispatch)
    assert rec.runs[-1] == ["npm"]  # a dry run shows it (a dep may make the program)
    monkeypatch.setattr(tasks.proc, "DRY_RUN", False)
    tasks.run_task(cfg, "script", [], rec.dispatch)
    assert rec.runs[-1] == [str(script)]  # an extension outside PATHEXT: as it is (CreateProcess then says it cannot start it)
    tasks.run_task(cfg, "rel", [], rec.dispatch)
    assert Path(rec.runs[-1][0]) == ROOT / "tools" / "x.cmd"  # a path is no PATH lookup
    tasks.run_task(cfg, "named", [], rec.dispatch)
    assert rec.runs[-1] == [str(npm)]  # a name with its extension, as it is
    tasks.run_task(cfg, "relpath", [], rec.dispatch)
    assert rec.runs[-1] == [str(tool)]  # a relative PATH entry is the task cwd's, as on POSIX
    monkeypatch.setattr(tasks, "IS_WINDOWS", False)
    tasks.run_task(cfg, "web", [], rec.dispatch)
    assert rec.runs[-1][0] == "npm"  # POSIX: execvp searches PATH itself


@pytest.mark.parametrize(
    ("arg", "refused"),
    [
        ("react@^18", "^"), ("a&b", "&"), ("x|y", "|"), ("<in", "<"), ("out>", ">"),  # unquoted: operators
        ("%PATH%", "%"), ("50%", "%"), ('say "hi"', '"'), ("a\nb", "\n"),  # quoted or not
        ("a & b", None), ("x ^ y", None), ("", None), ("--prod", None), ("C:\\a b\\", None),  # list2cmdline quotes these
    ],
)
def test_a_batch_file_gets_only_arguments_cmd_passes_unchanged(
    rec: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arg: str, refused: str | None
) -> None:
    """Windows runs a .cmd/.bat through cmd.exe, which re-parses the command line list2cmdline
    builds: `./pyt web react@^18` installed react@18 (an unquoted ^ is cmd's escape), `a&b`
    ran `b`, and %VAR% expands even inside quotes. Such an argument is refused (exit 2) instead
    of reaching the program changed; one that list2cmdline quotes (a space) is passed."""
    monkeypatch.setattr(tasks, "IS_WINDOWS", True)
    monkeypatch.setenv("PATHEXT", ".com;.exe;.bat;.cmd")
    _batch(tmp_path, "npm.cmd")
    cfg = make({"tasks": {"web": {"cmd": ["npm", "install"], "uv": False, "env": {"PATH": str(tmp_path)}}, "bat": {"cmd": ["tools/build.BAT"], "uv": False}}})
    for task in ("web", "bat"):
        before = len(rec.runs)
        if refused is None:
            tasks.run_task(cfg, task, [arg], rec.dispatch)
            assert rec.runs[-1][-1] == arg
            continue
        with pytest.raises(PytError) as e:
            tasks.run_task(cfg, task, [arg], rec.dispatch)
        assert e.value.code == 2 and len(rec.runs) == before  # nothing ran
        assert f"{refused!r}" in str(e.value) and "cmd.exe" in str(e.value)
    monkeypatch.setattr(tasks, "IS_WINDOWS", False)  # POSIX: no cmd.exe in between
    tasks.run_task(cfg, "web", [arg], rec.dispatch)
    assert rec.runs[-1][-1] == arg


@pytest.mark.parametrize(("folder", "refused"), [("R&D", "&"), ("a^b", "^"), ("50%", "%"), ("R & D", None)])
def test_a_batch_file_whose_path_cmd_would_change_is_refused(
    rec: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str, refused: str | None
) -> None:
    """The batch file's own path reaches cmd.exe on the same line as its arguments, and
    list2cmdline quotes it only for a blank: C:\\Users\\R&D\\AppData\\Roaming\\npm\\eslint.cmd ran
    as `C:\\Users\\R` and a second command. Only the arguments were checked."""
    monkeypatch.setattr(tasks, "IS_WINDOWS", True)
    program = tmp_path / folder / "eslint.cmd"
    cfg = make({"tasks": {"lint": {"cmd": [str(program), "src"], "uv": False}}})
    if refused is None:
        tasks.run_task(cfg, "lint", [], rec.dispatch)
        assert rec.runs[-1] == [str(program), "src"]
        return
    before = len(rec.runs)
    with pytest.raises(PytError) as e:
        tasks.run_task(cfg, "lint", [], rec.dispatch)
    assert e.value.code == 2 and len(rec.runs) == before  # nothing ran
    assert f"its path {str(program)!r} ({refused!r})" in str(e.value) and "Move it to a folder" in str(e.value), str(e.value)


def test_the_task_list_is_shown_with_quiet(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(ui, "QUIET", True)
    assert cli.cmd_tasks(make({"tasks": {"ci": {"deps": ["check all"]}, "gen": {"cmd": ["g"], "help": "Generate"}}}), []) == 0
    err = capsys.readouterr().err
    assert "  ci             deps: check all\n" in err and "  gen            Generate\n" in err
    assert "==>" not in err  # the header is progress
    tasks.list_tasks(make({}))
    assert "No custom tasks" in capsys.readouterr().err


def test_the_mode_display_and_render_lists_are_shown_with_quiet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-q` hides progress only: `mode` (its display) and `render --check` answer the question."""
    monkeypatch.setattr(ui, "QUIET", True)
    cfg = make({"backend": {"supported": ["cpython", "mypyc"]}})
    assert cmd_mode.cmd_mode(cfg, []) == 0
    err = capsys.readouterr().err
    assert "  active backend cpython" in err and "  typing mypyc" in err and "==>" not in err
    monkeypatch.setattr(render, "apply", lambda *a, **k: (["x.json"], []))
    monkeypatch.setattr(render, "pyproject_outdated", lambda c: False)
    assert cmd_mode.cmd_render(cfg, ["--check"]) == 1
    assert "outdated: x.json" in capsys.readouterr().err


# === 10. cmd_dev ====================================================================================


@pytest.mark.parametrize(
    ("args", "allow_all", "expected"),
    [
        ([], False, ("cpython", [])),
        (["mypyc", "x"], False, ("mypyc", ["x"])),
        (["pypy"], False, ("pypy", [])),
        (["all"], False, ("cpython", ["all"])),
        (["all", "-k", "x"], True, ("all", ["-k", "x"])),
        (["-k", "pypy"], True, ("cpython", ["-k", "pypy"])),
        (["cpython", "mypyc"], False, ("cpython", ["mypyc"])),  # an app argument after the backend
    ],
)
def test_split_backend(args: list[str], allow_all: bool, expected: tuple[str, list[str]]) -> None:
    assert cmd_dev.split_backend(make({}), args, allow_all=allow_all) == expected


class FakeChecks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.failing: set[str] = set()

    def run_checks(self, _cfg: Config, backend: str, *, rules: bool = True) -> bool:
        self.calls.append((backend, rules))
        return backend not in self.failing


@pytest.fixture
def checks(monkeypatch: pytest.MonkeyPatch) -> FakeChecks:
    fake = FakeChecks()
    monkeypatch.setattr(cmd_dev, "run_checks", fake.run_checks)
    return fake


def test_check_all_runs_each_profile_once_and_the_mypyc_rules_once(checks: FakeChecks, capsys: pytest.CaptureFixture[str]) -> None:
    three = {"backend": {"supported": ["cpython", "pypy", "mypyc"]}}
    assert cmd_dev.cmd_check(make(three), ["all"]) == 0
    assert checks.calls == [("cpython", False), ("mypyc", True)]  # cpython and pypy share 'off'
    assert "ok check: no errors" in capsys.readouterr().err
    checks.calls.clear()
    checks.failing.add("cpython")
    assert cmd_dev.cmd_check(make(three), ["all"]) == 1
    assert checks.calls == [("cpython", False), ("mypyc", True)]  # a failure does not skip the rest
    assert "error: check found errors" in capsys.readouterr().err
    checks.calls.clear()
    assert cmd_dev.cmd_check(make({**three, "typing": {"profile": "mypyc"}}), ["all"]) == 1
    assert checks.calls == [("cpython", True)]  # one profile for every backend: one run, with the rules
    checks.calls.clear()
    checks.failing.clear()
    assert cmd_dev.cmd_check(make({}), []) == 0
    assert checks.calls == [("cpython", True)]


def _profile_flags(name: str) -> tuple[bool, bool, bool]:
    """(blocking, ruff's exit_zero, skip_mypy) of the typing profile `name` as the project has it,
    read as cmd_dev.run_checks reads them: README lets a project edit
    .pytemplate/templates/typing/<profile>.toml, and the tests that pinned the shipped values
    failed in a project that had (`exit_zero = false` in warn.toml: 5 of them)."""
    data = render.load_profile(name)
    return bool(data.get("blocking", False)), bool(data.get("ruff", {}).get("exit_zero")), bool(data.get("skip_mypy"))


# The shipped profiles' flags, pinned in the template repository
SHIPPED_PROFILE_FLAGS = {"off": (False, False, True), "warn": (False, True, False), "strict": (True, False, False), "mypyc": (True, False, False)}


@pytest.mark.parametrize("name", sorted(SHIPPED_PROFILE_FLAGS))
def test_the_shipped_typing_profiles_keep_their_flags(name: str) -> None:
    if not TEMPLATE_REPO:
        pytest.skip("the shipped profiles: a project may edit its own (README)")
    assert _profile_flags(name) == SHIPPED_PROFILE_FLAGS[name]


def test_a_dry_run_of_check_names_only_what_it_skipped(checks: FakeChecks, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The dry-run line of `check` was fixed text: it said the mypyc rules had run in a project
    that does not support mypyc (they run only then), and named basedpyright with the pylance
    editor and mypy under a profile that skips it."""

    def line(*tools: str) -> str:
        return "(--dry-run) check: " + (f"{', '.join(tools[:-1])} and {tools[-1]} were" if len(tools) > 1 else f"{tools[0]} was") + " not run"

    def mypy(*profiles: str) -> tuple[str, ...]:  # mypy is skipped where every profile skips it
        return () if all(_profile_flags(p)[2] for p in profiles) else ("mypy",)

    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert cmd_dev.cmd_check(make({"backend": {"supported": ["cpython"]}}), []) == 0  # profile off: no mypy (shipped)
    assert line("ruff", *mypy("off")) + "\n" in capsys.readouterr().err
    strict = {"typing": {"relaxed": "strict"}}
    assert cmd_dev.cmd_check(make({**strict, "backend": {"supported": ["cpython", "pypy"]}}), ["all"]) == 0
    assert line("ruff", *mypy("strict")) + "\n" in capsys.readouterr().err
    both = {"backend": {"supported": ["cpython", "mypyc"]}, "typing": {"editor": "basedpyright"}}
    assert cmd_dev.cmd_check(make(both), ["all"]) == 0
    assert line("ruff", *mypy("off", "mypyc"), "basedpyright") + " (the mypyc rules were)\n" in capsys.readouterr().err
    if TEMPLATE_REPO:  # the shipped profiles: off skips mypy, the others run it
        assert (mypy("off"), mypy("strict"), mypy("off", "mypyc")) == ((), ("mypy",), ("mypy",))
        assert line("ruff", "mypy", "basedpyright") == "(--dry-run) check: ruff, mypy and basedpyright were not run"


def test_check_rejects_extra_arguments_and_unsupported_backends(checks: FakeChecks) -> None:
    for args, message in ((["all", "extra"], "unrecognized arguments: extra"), (["foo"], "unrecognized arguments: foo"), (["pypy"], "not in backend.supported")):
        with pytest.raises(PytError, match=message) as e:
            cmd_dev.cmd_check(make({}), args)
        assert e.value.code == 2
    assert checks.calls == []


class FakeTools:
    """envs.uv_run / envs.uv fakes for run_checks: exit codes per tool, argv recorded."""

    def __init__(self, ruff: int = 0, mypy: int = 0, basedpyright: int = 0) -> None:
        self.codes = {"ruff": ruff, "mypy": mypy, "basedpyright": basedpyright}
        self.calls: list[list[str]] = []

    def uv_run(self, _env: envs.PyEnv, argv: list[Any], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(a) for a in argv])
        return completed(argv, self.codes[str(argv[0])])

    def uv(self, _env: envs.PyEnv, argv: list[Any], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(a) for a in argv])
        return completed(argv, self.codes["basedpyright"] if "basedpyright" in map(str, argv) else 0)

    def tool(self, name: str) -> list[str] | None:
        return next((c for c in self.calls if name in c[:8]), None)


@pytest.fixture
def isolated_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(cmd_dev, "BUILD", tmp_path)  # .build/cfg/* goes to tmp_path
    monkeypatch.setattr(mypyc, "compiled_sources", lambda _cfg: [])
    monkeypatch.setattr(lintc, "lint", lambda _cfg, _files: [])
    yield


def _checks_with(monkeypatch: pytest.MonkeyPatch, cfg: Config, backend: str = "cpython", **codes: int) -> tuple[bool, FakeTools]:
    fake = FakeTools(**codes)
    monkeypatch.setattr(envs, "uv_run", fake.uv_run)
    monkeypatch.setattr(envs, "uv", fake.uv)
    return cmd_dev.run_checks(cfg, backend), fake


@pytest.mark.parametrize(
    ("relaxed", "ruff", "mypy", "passed"),
    [
        ("off", 0, 0, True),
        ("off", 1, 0, False),  # ruff always blocks (warn only turns it into --exit-zero)
        ("warn", 0, 1, True),  # type errors are warnings
        ("warn", 0, 2, False),  # mypy itself failed (exit 2): never a warning
        ("warn", 1, 0, False),
        ("strict", 0, 1, False),
        ("strict", 0, 0, True),
    ],
)
@pytest.mark.usefixtures("isolated_checks")
def test_run_checks_blocking_matrix(relaxed: str, ruff: int, mypy: int, passed: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`passed` is the shipped profiles' answer; the project's own profile decides (README: a
    project may edit it): ruff's failure always blocks, mypy's exit 1 only under a blocking one."""
    blocking, exit_zero, skip_mypy = _profile_flags(relaxed)
    expected = ruff == 0 and (skip_mypy or mypy == 0 or (mypy == 1 and not blocking))
    if TEMPLATE_REPO:
        assert expected is passed
    ok, fake = _checks_with(monkeypatch, make({"typing": {"relaxed": relaxed}}), ruff=ruff, mypy=mypy)
    assert ok is expected
    ruff_argv = fake.tool("ruff")
    assert ruff_argv is not None and ("--exit-zero" in ruff_argv) == exit_zero  # shipped: warn only
    assert (fake.tool("mypy") is None) == skip_mypy  # shipped: the off profile skips mypy
    if not blocking and not skip_mypy and mypy == 1:
        assert "non-blocking" in capsys.readouterr().err


@pytest.mark.usefixtures("isolated_checks")
def test_run_checks_passes_the_3_11_target_with_pypy(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({"backend": {"supported": ["cpython", "pypy", "mypyc"]}, "typing": {"relaxed": "strict"}})
    _ok, fake = _checks_with(monkeypatch, cfg)
    mypy = fake.tool("mypy")
    assert mypy is not None
    i = mypy.index("--python-version")
    assert mypy[i : i + 4] == ["--python-version", "3.11", "--python-executable", str(envs.tool_env(cfg).python)]


@pytest.mark.parametrize(("backend", "passed"), [("mypyc", False), ("cpython", True)])
@pytest.mark.usefixtures("isolated_checks")
def test_the_mypyc_rules_block_only_under_the_mypyc_profile(backend: str, passed: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    finding = lintc.Finding(SRC / "myapp" / "core" / "x.py", 3, "a nested class")
    monkeypatch.setattr(lintc, "lint", lambda _cfg, _files: [finding])
    ok, _fake = _checks_with(monkeypatch, make({}), backend=backend)
    assert ok is passed
    err = capsys.readouterr().err
    assert (f"error: {finding}" in err) == (backend == "mypyc") and (f"warning: {finding}" in err) == (backend != "mypyc")


@pytest.mark.parametrize(("relaxed", "passed"), [("strict", False), ("warn", True)])
@pytest.mark.usefixtures("isolated_checks")
def test_basedpyright_runs_with_every_pin(relaxed: str, passed: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({"typing": {"editor": "basedpyright", "relaxed": relaxed}})
    blocking = _profile_flags(relaxed)[0]  # `passed`: the shipped profile's answer
    if TEMPLATE_REPO:
        assert passed is not blocking
    ok, fake = _checks_with(monkeypatch, cfg, basedpyright=1)
    assert ok is not blocking  # a failure only blocks under a blocking profile
    (argv,) = [c for c in fake.calls if "basedpyright" in c]
    withs = [argv[i + 1] for i, a in enumerate(argv) if a == "--with"]
    assert withs == [cmd_dev.BASEDPYRIGHT, cmd_dev.BASEDPYRIGHT_NODE]
    assert argv[argv.index("--project") + 1] == str(tmp_path / "cfg" / f"pyright-{relaxed}.json")


@pytest.mark.parametrize(
    ("relaxed", "ready", "code", "passed", "message"),
    [
        ("warn", 0, 0, True, ""),
        ("warn", 0, 1, True, "warning: basedpyright: type warnings (profile 'warn', non-blocking)"),
        ("strict", 0, 1, False, ""),
        # uv could not install the pins (offline, cold cache): uv exits 1 too, never "findings"
        ("warn", 1, 1, False, "error: basedpyright could not run: uv could not install basedpyright=="),
        ("strict", 2, 0, False, "error: basedpyright could not run"),
        ("warn", 0, 2, False, "error: basedpyright stopped (exit code 2)"),  # a fatal error
        ("warn", 0, 3, False, "error: basedpyright stopped (exit code 3)"),  # its config
    ],
)
@pytest.mark.usefixtures("isolated_checks")
def test_basedpyright_that_cannot_run_always_fails(
    relaxed: str, ready: int, code: int, passed: bool, message: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline with a cold cache, `uv run --with basedpyright==...` failed (exit 1: No solution
    found), and under the non-blocking `warn` profile check said `ok check: no errors`. Findings
    (exit 1 once the pins are installed) block as the project's own profile says (`passed` and
    `message`: the shipped profile's answer)."""
    cfg = make({"typing": {"editor": "basedpyright", "relaxed": relaxed}})
    if not ready and code == 1:
        blocking = _profile_flags(relaxed)[0]
        found = (not blocking, "" if blocking else f"warning: basedpyright: type warnings (profile '{relaxed}', non-blocking)")
        if TEMPLATE_REPO:
            assert found == (passed, message)
        passed, message = found
    calls: list[list[str]] = []

    def uv(_env: envs.PyEnv, argv: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        calls.append(args)
        if args[-3:] == ["python", "-c", ""]:
            # Not captured: on a cold cache this step downloads the pins (tens of MB), and uv's
            # progress, or its error, reaches the terminal itself
            assert not kw.get("capture") and kw.get("echo") is False
            if ready:
                print("error: No solution found when resolving --with dependencies", file=sys.stderr)
            return subprocess.CompletedProcess(args, ready)
        return completed(args, code)

    monkeypatch.setattr(envs, "uv_run", lambda _env, argv, **_kw: completed(argv))
    monkeypatch.setattr(envs, "uv", uv)
    assert cmd_dev.run_checks(cfg, "cpython") is passed
    err = capsys.readouterr().err
    assert message in err
    assert [c[-3:] == ["python", "-c", ""] for c in calls] == ([True] if ready else [True, False])
    assert all(c[:6] == ["run", "--locked", "--with", cmd_dev.BASEDPYRIGHT, "--with", cmd_dev.BASEDPYRIGHT_NODE] for c in calls)
    if ready:
        assert "No solution found" in err  # uv's own reason


def test_tools_are_pinned_exactly() -> None:
    assert re.fullmatch(r"basedpyright==\d+\.\d+\.\d+", cmd_dev.BASEDPYRIGHT)
    assert re.fullmatch(r"nodejs-wheel-binaries==\d+\.\d+\.\d+", cmd_dev.BASEDPYRIGHT_NODE)


@pytest.mark.usefixtures("isolated_checks")
def test_profile_files_are_the_rendered_configs(tmp_path: Path) -> None:
    cfg = make({})
    mypy_ini = cmd_dev._profile_file(cfg, "strict", "mypy")
    ruff_toml = cmd_dev._profile_file(cfg, "warn", "ruff")
    assert mypy_ini == tmp_path / "cfg" / "mypy-strict.ini"
    assert mypy_ini.read_text(encoding="utf-8") == render.mypy_ini(cfg, "strict")
    assert ruff_toml == tmp_path / "cfg" / "ruff-warn.toml"
    assert ruff_toml.read_text(encoding="utf-8") == render.to_toml(render.ruff_config(cfg, "warn", relative_to=ROOT)) + "\n"


def _venv_ruff() -> Path:
    return ROOT / ".venv" / ("Scripts/ruff.exe" if sys.platform == "win32" else "bin/ruff")


# The shipped `off` typing profile, for the tests about ruff itself (its config paths, the folders it
# checks) whatever this project's off.toml says: a project may edit its profiles (README), and in one
# whose `off` reports ruff's findings without blocking (exit_zero) they failed (A9-02).
SHIPPED_OFF: dict[str, Any] = {
    "description": "No type checking: only syntax errors and undefined names",
    "blocking": False,
    "skip_mypy": True,
    "mypy": {"ignore_errors": True},
    "pyright": {"typeCheckingMode": "off"},
    "ruff": {"select": ["E9", "F63", "F7", "F82"], "ignore": [], "exit_zero": False},
}


def _shipped_off(monkeypatch: pytest.MonkeyPatch) -> None:
    if TEMPLATE_REPO:  # the copy here follows the template's own file
        assert {k: v for k, v in render.load_profile("off").items() if k in SHIPPED_OFF} == SHIPPED_OFF
    real = render.load_profile
    monkeypatch.setattr(render, "load_profile", lambda name: json.loads(json.dumps(SHIPPED_OFF)) if name == "off" else real(name))


@pytest.mark.parametrize("folder", ["app$v2", "a${b}"])
def test_check_runs_ruff_in_a_project_folder_named_like_a_variable(folder: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ruff expands $NAME and ${NAME} in its --config argument and in the paths of that file
    (CLAUDE.md 15.1): with absolute paths, `check` (and the checks of `build`) failed in every
    project folder named like app$v2 ("does not point to a configuration file", "environment
    variable not found"). The real ruff of .venv runs as the runner starts it: in the project."""
    ruff = _venv_ruff()
    if not ruff.is_file():
        pytest.skip("no ruff in .venv (./pyt setup)")
    root = tmp_path / folder
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    source = root / "src" / "pkg" / "a.py"
    source.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(render, "ROOT", root)
    monkeypatch.setattr(cmd_dev, "ROOT", root)
    monkeypatch.setattr(cmd_dev, "BUILD", root / ".build")
    monkeypatch.setattr(project, "ROOT", root)  # code_dirs: the folders ruff checks
    for name in ("v2", "b"):
        monkeypatch.delenv(name, raising=False)
    outputs: list[str] = []

    def uv_run(_env: envs.PyEnv, argv: list[Any], **_kw: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        if args[0] != "ruff":
            return completed(args)  # mypy: not the point here
        r = subprocess.run([str(ruff), *args[1:]], cwd=root, capture_output=True, text=True, timeout=120, check=False)
        outputs.append(r.stdout + r.stderr)
        return r

    monkeypatch.setattr(envs, "uv_run", uv_run)
    _shipped_off(monkeypatch)
    cfg = make({"typing": {"relaxed": "off"}})
    assert cmd_dev.run_checks(cfg, "cpython", rules=False) is True, outputs
    source.write_text("print(undefined_name)\n", encoding="utf-8")
    assert cmd_dev.run_checks(cfg, "cpython", rules=False) is False
    assert "F821" in outputs[-1], outputs[-1]


def test_check_lint_and_fmt_see_every_folder_below_src_and_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ruff read the excludes of the generated configs (the project's typings and dist, its own
    default venv, _build, node_modules, __pypackages__...) as folder names at ANY depth: `check`
    (the .build/cfg copy), `lint` and `fmt` (the editors' .ruff.toml) passed a syntax error in a
    subpackage or a test folder of such a name. The real ruff of .venv, started in the project."""
    ruff = _venv_ruff()
    if not ruff.is_file():
        pytest.skip("no ruff in .venv (./pyt setup)")
    root = tmp_path / "p"
    broken = [*(f"src/pkg/{d}/__init__.py" for d in ("venv", "typings", "dist", "_build", "node_modules", "__pypackages__")), "tests/typings/test_x.py", "tests/venv/test_y.py"]
    for rel in ("src/pkg/__init__.py", *broken):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("def broken(:\n    pass\n" if rel in broken else "", encoding="utf-8")
    monkeypatch.setattr(render, "ROOT", root)
    monkeypatch.setattr(cmd_dev, "ROOT", root)
    monkeypatch.setattr(cmd_dev, "BUILD", root / ".build")
    monkeypatch.setattr(project, "ROOT", root)  # code_dirs: the folders ruff checks
    _shipped_off(monkeypatch)
    cfg = make({"typing": {"relaxed": "off"}})
    (root / ".ruff.toml").write_text(render.to_toml(render.ruff_config(cfg, "off")) + "\n", encoding="utf-8")  # as render writes it
    outputs: list[str] = []

    def uv_run(_env: envs.PyEnv, argv: list[Any], **_kw: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        if args[0] != "ruff":
            return completed(args)  # mypy: skipped by the off profile anyway
        r = subprocess.run([str(ruff), *args[1:]], cwd=root, capture_output=True, text=True, timeout=120, check=False)
        outputs.append(r.stdout + r.stderr)
        return r

    monkeypatch.setattr(envs, "uv_run", uv_run)
    assert cmd_dev.run_checks(cfg, "cpython", rules=False) is False, outputs
    assert cmd_dev.cmd_lint(cfg, []) != 0, outputs[-1]
    assert cmd_dev.cmd_fmt(cfg, ["--check"]) != 0, outputs[-1]
    for command, out in zip(("check", "lint", "fmt --check"), outputs, strict=True):
        missed = [rel for rel in broken if rel not in out.replace("\\", "/")]
        assert not missed, f"./pyt {command} skipped {missed}:\n{out}"


@pytest.mark.parametrize(("relaxed", "exit_zero"), [("warn", True), ("strict", False), ("off", False)])
def test_lint_honours_the_profiles_exit_zero(relaxed: str, exit_zero: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """`exit_zero`: the shipped profile's; the project's own decides (README: it may edit it)."""
    if TEMPLATE_REPO:
        assert _profile_flags(relaxed)[1] is exit_zero
    calls: list[list[str]] = []
    monkeypatch.setattr(envs, "uv_run", lambda _env, argv, **_kw: calls.append([str(a) for a in argv]) or completed(argv))
    assert cmd_dev.cmd_lint(make({"typing": {"relaxed": relaxed}}), ["--fix"]) == 0
    assert calls[0][:3] == ["ruff", "check", "--fix"]
    assert ("--exit-zero" in calls[0]) == _profile_flags(relaxed)[1]


class FakeTests:
    """envs.uv_run and mypyc.build fakes for test/run: pytest exits with codes[backend]."""

    def __init__(self, stage: Path, codes: dict[str, int] | None = None) -> None:
        self.stage = stage
        self.codes = codes or {}
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def uv_run(self, env: envs.PyEnv, argv: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        extra = kw.get("extra_env") or {}
        backend = extra.get("PYTEMPLATE_BACKEND", env.key)
        self.calls.append((env.key, [str(a) for a in argv], dict(extra)))
        return completed(argv, self.codes.get(backend, 0))

    def build(self, _cfg: Config, _profile: str, **_kw: Any) -> Path:
        if isinstance(self.codes.get("build"), int):
            raise PytError("mypyc failed (exit code 1)", 1)
        return self.stage


@pytest.fixture
def fake_tests(monkeypatch: pytest.MonkeyPatch) -> FakeTests:
    fake = FakeTests(ROOT / ".build" / "pt-stage")
    monkeypatch.setattr(envs, "uv_run", fake.uv_run)
    monkeypatch.setattr(mypyc, "build", fake.build)
    return fake


def test_test_argv_and_the_compiled_proof(fake_tests: FakeTests) -> None:
    cfg = own({"backend": {"supported": ["cpython", "pypy", "mypyc"]}})
    assert cmd_dev.test_backend(cfg, "mypyc", ["-k", "x"]) == 0
    key, argv, extra = fake_tests.calls[-1]
    assert key == "cpython" and argv == ["python", "-m", "pytest", "-o", "pythonpath=.build/pt-stage", "-k", "x"]
    assert extra["PYTEMPLATE_BACKEND"] == "mypyc"
    assert extra["PYTEMPLATE_COMPILED"] == ",".join(mypyc.compiled_modules(cfg)) != ""  # conftest's proof
    assert cmd_dev.test_backend(cfg, "cpython", []) == 0
    assert fake_tests.calls[-1] == ("cpython", ["python", "-m", "pytest"], {"PYTEMPLATE_BACKEND": "cpython"})
    assert cmd_dev.test_backend(cfg, "pypy", ["-q"]) == 0
    assert fake_tests.calls[-1] == ("pypy", ["python", "-m", "pytest", "-q"], {"PYTEMPLATE_BACKEND": "pypy"})


def test_the_mypyc_tests_keep_the_projects_other_pythonpath_entries(fake_tests: FakeTests, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring of stage_pythonpath: with the project's `pythonpath = ["src", "tests/helpers"]`
    the mypyc run gets the stage in place of src AND tests/helpers (a plain `pythonpath=<stage>`
    dropped it: the tests that import a helper failed to collect under mypyc only). The project's
    own ["src"] cannot tell the two apart."""
    monkeypatch.setattr(cmd_dev, "pytest_pythonpath", lambda root=ROOT: ["src", "tests/helpers"])
    cfg = own({"backend": {"supported": ["cpython", "mypyc"]}})
    assert cmd_dev.test_backend(cfg, "mypyc", []) == 0
    assert fake_tests.calls[-1][1] == ["python", "-m", "pytest", "-o", "pythonpath=.build/pt-stage tests/helpers"]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        # the template's own: src becomes the stage
        ({"pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n'}, ["STAGE"]),
        # the user's extra entries stay (-o replaces the whole setting)
        ({"pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["tests/helpers", "./src", "my libs"]\n'}, ["tests/helpers", "STAGE", "my libs"]),
        ({"pyproject.toml": '[tool.pytest.ini_options]\npythonpath = "src tests/helpers"\n'}, ["STAGE", "tests/helpers"]),
        ({"pyproject.toml": '[tool.pytest]\npythonpath = ["src", "tests/helpers"]\n'}, ["STAGE", "tests/helpers"]),  # pytest 9 native
        # no src entry: the stage goes first
        ({"pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["lib"]\n'}, ["STAGE", "lib"]),
        ({"pyproject.toml": "[project]\nname = 'x'\n"}, ["STAGE"]),
        ({}, ["STAGE"]),
        # the other files pytest reads, in pytest's order
        ({"pytest.ini": "[pytest]\npythonpath = src\n    tests/helpers\n", "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["x"]\n'}, ["STAGE", "tests/helpers"]),
        ({"pytest.ini": "", "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src", "x"]\n'}, ["STAGE"]),  # an empty pytest.ini wins
        ({"pytest.toml": '[pytest]\npythonpath = ["src", "h"]\n', "pytest.ini": "[pytest]\npythonpath = x\n"}, ["STAGE", "h"]),
        ({"tox.ini": "[pytest]\npythonpath = src h\n"}, ["STAGE", "h"]),
        ({"setup.cfg": "[tool:pytest]\npythonpath = src h\n", "tox.ini": "[tox]\n"}, ["STAGE", "h"]),
        ({"pyproject.toml": "not toml ["}, ["STAGE"]),
    ],
)
def test_mypyc_tests_keep_the_other_pythonpath_entries(tmp_path: Path, files: dict[str, str], expected: list[str]) -> None:
    """`test mypyc` passed `-o pythonpath=<stage>`, which replaces the WHOLE setting: a test
    importing a helper from an extra entry (tests/helpers) passed on cpython and failed to
    collect on mypyc."""
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    stage = ROOT / ".build" / "pt-stage"
    value = cmd_dev.stage_pythonpath(stage, root=tmp_path, src=tmp_path / "src")
    assert shlex.split(value) == [".build/pt-stage" if e == "STAGE" else e for e in expected]


def test_pytest_reads_the_mypyc_pythonpath_like_the_project_setting(tmp_path: Path) -> None:
    """The real pytest: the compiled package of the stage wins over src/, and a helper of an
    extra pythonpath entry (with a space in its folder) still imports."""
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["src", "tests/my helpers"]\n', encoding="utf-8")
    for folder, where in (("src", "src"), ("stage", "stage")):
        (tmp_path / folder / "pkg").mkdir(parents=True)
        (tmp_path / folder / "pkg" / "__init__.py").write_text(f"WHERE = {where!r}\n", encoding="utf-8")
    (tmp_path / "tests" / "my helpers").mkdir(parents=True)
    (tmp_path / "tests" / "my helpers" / "myhelp.py").write_text("VALUE = 3\n", encoding="utf-8")
    (tmp_path / "tests" / "test_x.py").write_text(
        "import pkg\nfrom myhelp import VALUE\n\ndef test_it():\n    assert (pkg.WHERE, VALUE) == ('stage', 3)\n", encoding="utf-8"
    )
    value = cmd_dev.stage_pythonpath(tmp_path / "stage", root=tmp_path, src=tmp_path / "src")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", f"pythonpath={value}", "tests"],
        cwd=tmp_path,
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_test_returns_pytests_code_for_one_backend(fake_tests: FakeTests, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = own({})
    fake_tests.codes.update(cpython=5)  # no tests collected
    assert cmd_dev.cmd_test(cfg, ["-k", "nothing"]) == 5
    fake_tests.codes.update(cpython=4)  # a pytest usage error
    assert cmd_dev.cmd_test(cfg, ["cpython", "--bogus"]) == 4
    assert cmd_dev.cmd_test(cfg, ["mypyc"]) == 0
    assert "test summary" not in capsys.readouterr().err  # no summary for one backend
    fake_tests.codes.update(cpython=0, mypyc=5)
    assert cmd_dev.cmd_test(cfg, ["all"]) == 1  # `all` stays 0 or 1
    err = capsys.readouterr().err
    assert "[ok] cpython" in err and "[XX] mypyc" in err and "exit code 5" in err
    fake_tests.codes.update(mypyc=0)
    assert cmd_dev.cmd_test(cfg, ["all"]) == 0


def test_test_all_goes_on_after_a_backend_that_cannot_build(fake_tests: FakeTests, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make({"backend": {"active": "cpython", "supported": ["mypyc", "cpython"]}})
    fake_tests.codes["build"] = 1  # the mypyc build raises PytError
    assert cmd_dev.cmd_test(cfg, ["all"]) == 1
    assert [c[2]["PYTEMPLATE_BACKEND"] for c in fake_tests.calls] == ["cpython"]  # cpython still ran
    err = capsys.readouterr().err
    assert "error: test mypyc: mypyc failed" in err
    assert "[XX] mypyc" in err and "[ok] cpython" in err
    with pytest.raises(PytError, match="mypyc failed"):  # one backend: the error is the answer
        cmd_dev.cmd_test(cfg, ["mypyc"])


def test_run_returns_the_apps_code(fake_tests: FakeTests) -> None:
    cfg = make({})
    fake_tests.codes["cpython"] = 7
    assert cmd_dev.cmd_run(cfg, ["--frames", "3"]) == 7
    assert fake_tests.calls[-1][1] == ["python", str(SRC / "main.py"), "--frames", "3"]
    assert cmd_dev.cmd_run(cfg, ["mypyc", "x"]) == 7
    assert fake_tests.calls[-1][1] == ["python", str(fake_tests.stage / "main.py"), "x"]
    with pytest.raises(PytError, match="not in backend.supported"):
        cmd_dev.cmd_run(cfg, ["pypy"])


@pytest.mark.usefixtures("fake_tests")
def test_report_needs_mypyc_and_never_opens_a_browser_in_a_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PytError, match="report comes from mypyc"):
        cmd_dev.cmd_report(make({"backend": {"supported": ["cpython"]}}), [])
    monkeypatch.setattr(cmd_dev.webbrowser, "open", fail)
    monkeypatch.setattr(cmd_dev, "_profile_file", lambda _cfg, _profile, _kind: Path("mypy.ini"))
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert cmd_dev.cmd_report(make({}), ["--open"]) == 0


@pytest.mark.parametrize(("mypy", "code", "line"), [(0, 0, "ok Any expressions per module"), (1, 0, "ok Any expressions per module"), (2, 1, "error: mypy stopped (exit code 2)")])
def test_report_follows_mypys_exit_code(
    fake_tests: FakeTests, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mypy: int, code: int, line: str
) -> None:
    """A syntax error stops mypy (exit 2) before its reports: report said `ok Any expressions per
    module` and exited 0 with an empty any-exprs.txt. Type errors (exit 1) still write them."""
    monkeypatch.setattr(proc, "DRY_RUN", False)
    monkeypatch.setattr(cmd_dev, "_profile_file", lambda _cfg, _profile, _kind: Path("mypy.ini"))
    fake_tests.codes["cpython"] = mypy  # the mypy run of the report
    assert cmd_dev.cmd_report(make({}), []) == code
    err = capsys.readouterr().err
    assert line in err
    assert ("ok Any expressions" in err) is (code == 0)


def test_a_dry_run_reports_no_success_for_what_it_skipped(
    fake_tests: FakeTests, checks: FakeChecks, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run skips compile, pytest, ruff, mypy: `ok compiled stage`, `ok mypyc report`,
    `ok Any expressions`, `ok check: no errors` and a test summary of [ok] lines were printed."""
    monkeypatch.setattr(proc, "DRY_RUN", True)
    monkeypatch.setattr(cmd_dev, "_profile_file", lambda _cfg, _profile, _kind: Path("mypy.ini"))
    cfg = own({"backend": {"supported": ["cpython", "mypyc"]}})  # test mypyc reads the compiled modules in src/
    assert cmd_dev.cmd_compile(cfg, []) == 0
    assert cmd_dev.cmd_report(cfg, []) == 0
    assert cmd_dev.cmd_test(cfg, ["all"]) == 0
    assert cmd_dev.cmd_check(cfg, ["all"]) == 0
    err = capsys.readouterr().err
    assert "ok " not in err and "[ok]" not in err and "test summary" not in err
    assert "(--dry-run) would compile the stage" in err and "(--dry-run) would write the mypyc report" in err
    assert "(--dry-run) would write the Any reports" in err and "(--dry-run) check: ruff and mypy were not run (the mypyc rules were)" in err


def test_a_dry_run_of_test_all_still_fails_when_a_backend_fails_its_checks(
    fake_tests: FakeTests, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dry run makes the real checks (CLAUDE.md 5.4): a `compile.exclude` naming nothing
    stops `test mypyc` with a PytError there too. `--dry-run test all` printed that error and
    still exited 0 (it returned before looking at the results); the failed backend is listed
    and the exit code is 1, while no [ok] row claims a test that did not run."""
    monkeypatch.setattr(proc, "DRY_RUN", True)
    fake_tests.codes["build"] = 1  # the mypyc step raises PytError
    cfg = make({"backend": {"supported": ["cpython", "mypyc"]}})
    assert cmd_dev.cmd_test(cfg, ["all"]) == 1
    err = capsys.readouterr().err
    assert "error: test mypyc: mypyc failed" in err and "[XX] mypyc" in err
    assert "[ok]" not in err and "cpython" not in err.split("test summary", 1)[1]


# === 11. end to end: the exit codes cross pyt.py (a throwaway copy, no uv needed) ==============


@pytest.fixture(scope="module")
def tasks_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the project with the tasks the tests below run. Every one of them is defined
    here: the project's own [tasks] belong to its user (the preset's deps-only `ci`, which a test
    ran, may be renamed, deleted or given a cmd)."""
    dest = tmp_path_factory.mktemp("cli")
    presets.copy_template(dest)
    py = json.dumps(sys.executable)
    extra = f"""
[tasks.exit7]
cmd = [{py}, "-c", "raise SystemExit(7)"]
uv = false

[tasks.depsonly]
deps = ["exit7"]

[tasks.depfail]
deps = ["exit7"]
cmd = [{py}, "-c", "print('SHOULD-NOT-RUN')"]
uv = false

[tasks.cyc-a]
deps = ["cyc-b"]

[tasks.cyc-b]
deps = ["cyc-a"]

[tasks.typo]
cmd = ["{{nope}}"]
uv = false

[tasks.badcwd]
cmd = [{py}, "-V"]
cwd = "no-such-dir"
uv = false

[tasks.pypyt]
cmd = ["python", "-V"]
backend = "pypy"

[tasks.killed]
cmd = [{py}, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
uv = false
"""
    toml = dest / "pytemplate.toml"
    text = toml.read_text(encoding="utf-8")
    if "pypy" in tomllib.loads(text)["backend"]["supported"]:  # a raylib project: `pypyt` needs PyPy unsupported
        text = config.set_value(config.set_value(text, "backend", "active", "cpython"), "backend", "supported", ["cpython", "mypyc"])
    toml.write_text(text + extra, encoding="utf-8")
    return dest


def _pyt(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(root / ".pytemplate" / "pyt.py"), *args],
        cwd=root,
        env=child_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


@pytest.mark.parametrize(
    ("args", "code", "stderr"),
    [
        (["exit7"], 7, ""),
        (["depfail"], 7, ""),
        (["cyc-a"], 2, "task cycle: cyc-a -> cyc-b -> cyc-a"),
        (["typo"], 2, "unknown placeholder 'nope'"),
        (["badcwd"], 2, "no-such-dir"),
        (["pypyt"], 2, "mode --supports +pypy"),
        (["depsonly", "--no-such"], 2, "takes no arguments"),
        pytest.param(["killed"], 137, "", marks=posix),
    ],
)
def test_task_exit_codes_cross_pyt_py(tasks_project: Path, args: list[str], code: int, stderr: str) -> None:
    r = _pyt(tasks_project, "--no-render", *args)
    assert r.returncode == code, r.stderr
    assert stderr in r.stderr
    assert "SHOULD-NOT-RUN" not in r.stdout and "Traceback" not in r.stderr


def test_quiet_keeps_what_was_asked_for(tasks_project: Path) -> None:
    r = _pyt(tasks_project, "-q", "--no-render", "tasks")
    assert r.returncode == 0 and "  exit7" in r.stderr and "==>" not in r.stderr
    r = _pyt(tasks_project, "-q", "--no-render", "--dry-run", "cyc-b", "--x")  # the plan's error still shows
    assert r.returncode == 2 and "takes no arguments" in r.stderr


uv_on_path = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")


@uv_on_path
def test_quiet_does_not_hide_a_dry_runs_plan(tasks_project: Path) -> None:
    r = _pyt(tasks_project, "-q", "--no-render", "--dry-run", "sync", "cpython")
    assert r.returncode == 0, r.stderr
    assert "$ uv sync --locked" in r.stderr
    assert not (tasks_project / ".venv").exists()


@uv_on_path
def test_a_dry_run_of_sync_and_add_changes_nothing(tasks_project: Path) -> None:
    files = {name: (tasks_project / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    for args in (["sync", "cpython"], ["add", "requests"], ["remove", "rich"]):
        r = _pyt(tasks_project, "--no-render", "--dry-run", *args)
        assert r.returncode == 0, r.stderr
        assert f"$ uv {args[0]}" in r.stderr
    assert {name: (tasks_project / name).read_bytes() for name in files} == files
    assert not (tasks_project / ".venv").exists()


@posix
@uv_on_path
@pytest.mark.parametrize(("task", "code", "stderr"), [("exit7", 7, ""), ("killed", 137, ""), ("depsonly --x", 2, "takes no arguments")])
def test_task_exit_codes_cross_the_sh_launcher(tasks_project: Path, task: str, code: int, stderr: str) -> None:
    # pyt -> uv run --script -> pyt.py: nothing on the way may change the code
    r = subprocess.run(
        ["sh", str(tasks_project / "pyt"), "--no-render", *task.split()],
        cwd=tasks_project,
        env=child_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert r.returncode == code, r.stderr
    assert stderr in r.stderr  # an unknown command exits 2 as well


# === 12. invariants of other modules that the runner's error paths depend on =======================


def test_update_file_never_writes_broken_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # config.set_value is line-based: a multi-line array plus set_value gave broken TOML
    path = tmp_path / "pytemplate.toml"
    original = '[backend]\nactive = "cpython"\nsupported = [\n  "cpython",\n  "mypyc",\n]\n'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    try:
        config.update_file([("backend", "supported", ["cpython", "pypy"])])
    except Exception:  # refusing is fine; writing a broken file is not
        assert path.read_text(encoding="utf-8") == original
    import tomllib

    tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("preset", "name"), [("script", "json"), ("script", "class"), ("script", "rich"), ("flet", "Flet")])
def test_names_that_would_break_the_project(preset: str, name: str) -> None:
    with pytest.raises(PytError) as e:
        presets.check_name_free(None, preset, name)
    assert e.value.code == 2 and "--name" in str(e.value)
    presets.check_name_free(None, preset, "my-app")


def test_librt_is_forbidden_only_while_pypy_is_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = tmp_path / "fast.py"
    module.write_text("import librt\nfrom librt.base64 import b64encode\n", encoding="utf-8")
    with_pypy = lintc.lint_file(make({"backend": {"supported": ["cpython", "pypy", "mypyc"]}}), module)
    assert len([f for f in with_pypy if "librt" in f.message]) == 2
    # Without PyPy it only has to be a runtime dependency (mypy installs it in the dev group only)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndependencies = ["librt>=0.15"]\n', encoding="utf-8")
    monkeypatch.setattr(lintc, "PYPROJECT", pyproject)
    assert not [f for f in lintc.lint_file(make({}), module) if "librt" in f.message]


# --- the Python the commands run on: cli._restart (CLAUDE.md 5.2) --------------------------------


def _toml(monkeypatch: pytest.MonkeyPatch, text: str | Exception) -> None:
    """pytemplate.toml as _python_needed reads it (config.read_text)."""

    def read() -> str:
        if isinstance(text, Exception):
            raise text
        return text

    monkeypatch.setattr(config, "read_text", read)


PROJECT_TOML = '[python]\ncpython = "3.14"\n\n[tasks.ci]\ncmd = ["x"]\n\n[tasks.install]\ncmd = ["y"]\n'


@pytest.mark.parametrize(
    ("line", "needs"),
    [
        ([], None),
        (["help", "run"], None),
        (["doctor"], None),
        (["new", "d"], None),
        (["install"], "3.14"),  # the project's [tasks] entry named install: a task
        (["uninstall"], None),
        (["__init", "script"], None),
        (["check", "-h"], None),  # ./pyt help check
        (["run", "-h"], "3.14"),  # -h goes to the app
        (["run"], "3.14"),
        (["tasks"], "3.14"),
        (["render", "--check"], "3.14"),
        (["hooks", "run"], "3.14"),
        (["ci"], "3.14"),  # a [tasks] entry
        (["bogus"], None),  # dispatch says it is no command, on any Python
        (["init"], None),
        (["shell-setup"], None),
    ],
)
def test_the_commands_that_need_python_cpython(monkeypatch: pytest.MonkeyPatch, line: list[str], needs: str | None) -> None:
    """help, doctor, new, install, uninstall (the builtins) and __init run on the Python the
    launchers started the runner on; every other command and task runs on python.cpython."""
    monkeypatch.setattr(cli.project, "GLOBAL", False)
    _toml(monkeypatch, PROJECT_TOML)
    assert cli._python_needed(line) == needs


@pytest.mark.parametrize(
    ("text", "needs"),
    [
        ('[python]\ncpython = "3.13"\n', "3.13"),
        ("", "3.14"),  # the field's default
        ('[python]\npypy = "pypy@3.11.15"\n', "3.14"),
        ('[python]\ncpython = "3.x"\n', None),  # config.validate names it
        ('[python]\ncpython = "3.10"\n', None),
        ('[python]\ncpython = 3.14\n', None),
        ("python = 3\n", "3.14"),
        ("[python\n", None),  # not TOML: dispatch says so
        (PytError("pytemplate.toml is not UTF-8"), None),
        (OSError("gone"), None),
    ],
)
def test_the_python_cpython_a_command_runs_on(monkeypatch: pytest.MonkeyPatch, text: str | Exception, needs: str | None) -> None:
    monkeypatch.setattr(cli.project, "GLOBAL", False)
    _toml(monkeypatch, text)
    assert cli._python_needed(["run"]) == needs


def test_global_mode_never_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside a project only help, doctor, new, install and uninstall run: never a restart."""
    monkeypatch.setattr(cli.project, "GLOBAL", True)
    _toml(monkeypatch, PROJECT_TOML)
    assert cli._python_needed(["run"]) is None and cli._python_needed(["new", "x"]) is None


def test_started_by_uv_is_the_virtual_environment_of_this_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`uv run --script` starts the runner in a virtual environment of its own and names it in
    VIRTUAL_ENV. A runner started on a Python by hand, by a test, or by _restart (runner_env drops
    VIRTUAL_ENV) is not: it runs where it was started, and never restarts twice."""
    monkeypatch.setattr(cli.sys, "prefix", str(tmp_path / "env"))
    monkeypatch.setattr(cli.sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "env"))
    assert cli._started_by_uv()
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "another"))
    assert not cli._started_by_uv()
    monkeypatch.delenv("VIRTUAL_ENV")
    assert not cli._started_by_uv()
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "env"))
    monkeypatch.setattr(cli.sys, "base_prefix", str(tmp_path / "env"))  # a base interpreter
    assert not cli._started_by_uv()


class Restart:
    """cli._restart with uv's start faked: python.cpython 3.99 (never this Python's), and the
    second runner recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.python = tmp_path / "uv" / "cpython-3.99" / "bin" / "python3.99"
        self.calls: list[tuple[list[str], Path | None, dict[str, str]]] = []
        self.result: int | BaseException = 7
        monkeypatch.setattr(cli, "_started_by_uv", lambda: True)
        monkeypatch.setattr(cli, "_python_needed", lambda rest: "3.99")
        monkeypatch.setattr(envs, "ensure_python", lambda version: self.python)
        monkeypatch.setattr(proc, "run", self._run)
        own_bin = str(Path(sys.prefix) / ("Scripts" if IS_WINDOWS else "bin"))
        monkeypatch.setenv("PATH", os.pathsep.join([own_bin, str(tmp_path / "tools")]))
        monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
        monkeypatch.setenv("PYTEMPLATE_LAUNCHER", "sh")

    def _run(self, argv: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        assert kw["echo"] is False and kw["check"] is False  # not skipped by --dry-run: it runs the dry run
        self.calls.append(([str(a) for a in argv], kw.get("cwd"), dict(kw["env"])))
        if isinstance(self.result, BaseException):
            raise self.result
        return subprocess.CompletedProcess(argv, self.result, None, None)


def test_a_command_moves_onto_python_cpython(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The launchers started the runner on another Python: the command runs in a second runner
    on python.cpython, with the same arguments (global options too), in the same folder, and its
    exit code is this run's. That runner gets the launcher's variables but no VIRTUAL_ENV (not
    started by uv: no second restart) and no bin/ of this runner's environment on PATH."""
    box = Restart(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys, "dont_write_bytecode", False)
    assert cli._restart(["-v", "run", "a b"], ["run", "a b"]) == 7
    ((argv, cwd, env),) = box.calls
    assert argv == [str(box.python), "-s", str(TEMPLATE_DIR / "pyt.py"), "-v", "run", "a b"]
    assert cwd == Path.cwd()
    assert "VIRTUAL_ENV" not in env and env["PATH"] == str(tmp_path / "tools") and env["PYTEMPLATE_LAUNCHER"] == "sh"
    monkeypatch.setattr(cli.sys, "dont_write_bytecode", True)  # python -B: the second runner writes no bytecode either
    cli._restart(["run"], ["run"])
    assert box.calls[-1][0][:3] == [str(box.python), "-s", "-B"]


@pytest.mark.parametrize(("code", "expected"), [(130, 130), (143, 143), (proc.STATUS_CONTROL_C_EXIT, 130), (0, 0)])
def test_a_restarted_command_that_was_interrupted_reports_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], code: int, expected: int) -> None:
    """Ctrl+C (or a SIGTERM passed on) reached the second runner too, which said so: this one only
    passes its exit code on (no second `error: interrupted`)."""
    box = Restart(monkeypatch, tmp_path)
    box.result = proc.Interrupted(code)
    assert cli._restart(["run"], ["run"]) == expected
    assert capsys.readouterr().err == ""


def test_no_restart_on_python_cpython_nor_for_what_runs_anywhere(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    box = Restart(monkeypatch, tmp_path)
    monkeypatch.setattr(project, "launcher_python", lambda root: ("", "only-managed"))  # the project has .venv
    monkeypatch.setattr(cli, "_python_needed", lambda rest: "%d.%d" % sys.version_info[:2])  # this very Python
    assert cli._restart(["run"], ["run"]) is None
    monkeypatch.setattr(cli, "_python_needed", lambda rest: None)  # help, doctor, new...
    assert cli._restart(["help"], ["help"]) is None
    monkeypatch.setattr(cli, "_python_needed", lambda rest: "3.99")
    monkeypatch.setattr(cli, "_started_by_uv", lambda: False)  # by hand, a test, or already restarted
    assert cli._restart(["run"], ["run"]) is None
    assert box.calls == []


def test_a_python_of_the_right_minor_is_not_python_cpython_unless_uv_manages_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With the project's environment the launchers asked uv for its own CPython (only-managed):
    python.cpython's minor is enough. Without it they let uv take a system Python too, so one of
    that minor (Fedora's or Homebrew's 3.14, Termux's 3.13 for a python.cpython of "3.13") only
    runs the command when uv names it as its own; else the command moves onto uv's, or stops where
    uv has none (ensure_python)."""
    box = Restart(monkeypatch, tmp_path)
    this = "%d.%d" % sys.version_info[:2]
    monkeypatch.setattr(cli, "_python_needed", lambda rest: this)
    asked: list[str] = []

    def found(path: Path | None) -> None:
        monkeypatch.setattr(envs, "find_cpython", lambda version: asked.append(version) or path)

    found(None)
    monkeypatch.setattr(project, "launcher_python", lambda root: ("", "only-managed"))
    assert cli._restart(["run"], ["run"]) is None and asked == [] and box.calls == []
    monkeypatch.setattr(project, "launcher_python", lambda root: (">=3.11", "managed"))
    own = Path(sys.base_prefix) / ("python.exe" if IS_WINDOWS else f"bin/python{this}")
    found(own)  # uv's own CPython of that minor is the one this runner runs on
    assert cli._restart(["run"], ["run"]) is None and asked == [this] and box.calls == []
    (tmp_path / "system" / "bin").mkdir(parents=True)
    found(tmp_path / "system" / "bin" / f"python{this}")  # uv's is another interpreter
    assert cli._restart(["run"], ["run"]) == 7 and box.calls[-1][0][0] == str(box.python)
    found(None)  # uv has none of that minor (Termux): ensure_python installs it or says why
    assert cli._restart(["run"], ["run"]) == 7 and len(box.calls) == 2


def test_python_cpython_uv_cannot_install_stops_the_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Android/Termux: uv installs no CPython there. The command stops with exit 3 and why,
    before anything of it ran."""
    Restart(monkeypatch, tmp_path)

    def unavailable(version: str) -> Path:
        raise PytError(f'python.cpython = "{version}": uv installs no CPython on this platform (Android (linux aarch64)).', 3)

    monkeypatch.setattr(envs, "ensure_python", unavailable)
    monkeypatch.setattr(cli, "dispatch", fail)
    assert cli.main(["run"], entry=True) == 3
    assert 'python.cpython = "3.99": uv installs no CPython' in capsys.readouterr().err


def test_only_the_entry_point_restarts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """.pytemplate/pyt.py runs main(entry=True); a caller in this process (these tests) runs the
    command on its own Python."""
    box = Restart(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "dispatch", lambda rest: 5)
    assert cli.main(["run"]) == 5 and box.calls == []
    assert cli.main(["run"], entry=True) == 7 and len(box.calls) == 1
    assert "entry=True" in PYT_PY.read_text(encoding="utf-8")


def _another_cpython() -> str:
    """A uv-managed or system CPython 3.11+ other than python.cpython, for uv's --python."""
    pinned = config.load(set()).python.cpython
    uv = os.environ.get("UV") or shutil.which("uv")
    if uv is None:
        pytest.skip("uv not found")
    for minor in range(11, 20):
        version = f"3.{minor}"
        if version == pinned:
            continue
        r = subprocess.run([uv, "python", "find", "--system", version], env=child_env(), capture_output=True, text=True, timeout=60, check=False)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[-1]
    pytest.skip(f"no CPython 3.11+ but python.cpython {pinned} here")


@needs_uv
def test_a_runner_started_on_another_python_moves_project_commands(tmp_path: Path) -> None:
    """For real: the launchers let uv start the runner on any CPython 3.11+ (here another one than
    python.cpython) while the project has no environment. `help` runs there; `tasks` runs in a
    second runner on python.cpython, which -v shows, and its output and exit code are this run's.
    In a throwaway copy: uv makes a script's cached environment again, in the same folder, for
    another Python, and this checkout's is the one `./pyt selftest` runs in (Windows cannot
    delete it)."""
    other = _another_cpython()
    uv = os.environ.get("UV") or shutil.which("uv")
    assert uv
    presets.copy_template(tmp_path)
    start = [uv, "run", "--quiet", f"--python={other}", "--python-preference", "managed", "--script", str(tmp_path / ".pytemplate" / "pyt.py")]
    env = child_env()
    helped = subprocess.run([*start, "-v", "help"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300, check=False)
    assert helped.returncode == 0 and "Development:" in helped.stdout, helped.stderr
    assert "pyt.py -v help" not in helped.stderr  # no second runner
    listed = subprocess.run([*start, "-v", "tasks"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300, check=False)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert re.search(r"\$ python\S* -s .*pyt\.py -v tasks", listed.stderr), listed.stderr  # Windows: python.exe
    pinned = config.load(set()).python.cpython
    assert f"python{pinned}" in listed.stderr or IS_WINDOWS, listed.stderr
    code = subprocess.run([*start, "bogus-command"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300, check=False)
    assert code.returncode == 2 and "unknown command: bogus-command" in code.stderr, code.stderr
