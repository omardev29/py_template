"""The runner's core: cli (global options, dispatch, help, exit codes), proc (child processes,
--dry-run, the environment, Ctrl+C, signals), tasks ([tasks]) and cmd_dev (check, test, lint).

Most tests run in-process with fakes; the few that start processes use `sys.executable` and a
scrubbed environment, never uv unless marked, and never touch this project's files.
"""

from __future__ import annotations

import importlib
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
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_DIR))

from runner import cli, cmd_dev, cmd_env, config, e2e, envs, lintc, mypyc, nvimtest, presets, proc, render, shells, tasks, ui  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import BUILD, DIST, ROOT, SRC  # noqa: E402
from runner.ui import DeployError  # noqa: E402

DEPLOY_PY = TEMPLATE_DIR / "deploy.py"
IS_WINDOWS = os.name == "nt"
posix = pytest.mark.skipif(IS_WINDOWS, reason="POSIX signals, pipes and exec bits")
needs_uv = pytest.mark.skipif(shutil.which("uv") is None and not os.environ.get("UV"), reason="uv not found")


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


def unchecked(data: dict[str, Any]) -> Config:
    """A Config that never went through config.validate (the runner must still not crash)."""
    cfg: Config = config._build(Config, data, "")
    return cfg


def child_env() -> dict[str, str]:
    """The environment of a child ./deploy: nothing of the uv run that started pytest."""
    drop = ("UV", "VIRTUAL_ENV", "PYTHONUNBUFFERED", "PYTHONPATH", "PYTHONHOME")
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith(("PYTEMPLATE_", "UV_"))}
    env.update(NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1")
    return env


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
    with pytest.raises(DeployError, match="AFTER the command") as e:
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
        (DeployError("bad config"), 2, "error: bad config"),
        (DeployError("no uv", 3), 3, "error: no uv"),
        (proc.CommandFailed(["uv", "sync"], 7), 7, "error: failed (exit code 7): uv sync"),
        (proc.CommandFailed(["x"], -9), 137, "failed (exit code 137)"),
        (KeyboardInterrupt(), 130, "error: interrupted"),
        (proc.Interrupted(3), 3, "error: interrupted"),  # the child's own code after its cleanup
        (proc.Interrupted(0), 130, "error: interrupted"),  # an interrupted command never reports success
        (proc.Interrupted(130), 130, "error: interrupted"),
        (proc.Interrupted(proc.STATUS_CONTROL_C_EXIT), 130, "error: interrupted"),
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
NEVER_RENDER = {"clean", "render", "new", "pyz-merge", "tasks", "shell-setup", "selftest", "help", "hooks"}


def test_commands_that_never_render() -> None:
    assert {n for n, c in cli.COMMANDS.items() if not c.render} == NEVER_RENDER & set(cli.COMMANDS)


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


def test_an_unknown_command_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: make({"tasks": {"ci": {"deps": ["check"]}}}))
    with pytest.raises(DeployError, match="unknown command: sycn") as e:
        cli.dispatch(["sycn"])
    assert e.value.code == 2


def test_help_needs_no_valid_config(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(*_a: Any, **_kw: Any) -> Config:
        raise DeployError("pytemplate.toml is not valid TOML: x")

    monkeypatch.setattr(config, "load", broken)
    assert cli.dispatch([]) == 0  # the full help, without the [tasks] block
    assert "Development:" in capsys.readouterr().out
    assert cli.dispatch(["check", "--help"]) == 0
    assert capsys.readouterr().out.startswith("./deploy check ")
    with pytest.raises(DeployError, match="not valid TOML"):  # it cannot tell whether 'ci' is a task
        cli.cmd_help(None, ["ci"])


# === 4. help ========================================================================================


@pytest.mark.parametrize("name", sorted(cli.COMMANDS))
def test_help_for_every_command(name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(config, "load", fail)  # a builtin's help needs no config
    assert cli.cmd_help(None, [name]) == 0
    c = cli.COMMANDS[name]
    assert capsys.readouterr().out.splitlines()[:2] == [f"./deploy {name} {c.usage}".rstrip(), f"  {c.summary}"]


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
    monkeypatch.setattr(config, "load", fail)  # nothing runs, not even the config load
    assert cli.main([name, *args]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"./deploy {name} {cli.COMMANDS[name].usage}".rstrip()


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
    assert capsys.readouterr().out.startswith("./deploy ci\n")
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
    assert out.splitlines()[0] == "./deploy ci"
    assert "check all, test all" in out and "no arguments" in out
    assert "Development:" not in out  # not the full help
    assert cli.cmd_help(cfg, ["gen"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[:2] == ["./deploy gen [args...]", "  Generate the assets"]
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
    with pytest.raises(DeployError, match=re.escape(message)) as e:
        cli.cmd_help(None, args)
    assert e.value.code == 2


def test_help_of_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_help(None, ["-h"]) == 0
    assert capsys.readouterr().out.startswith("./deploy help [COMMAND]\n")


# === 5. every command rejects what it does not understand ============================================

# Every command that does not forward its arguments (cli.FORWARDS), with the smallest valid
# arguments and whether one more positional is legitimate (add/remove take several packages).
# A new command fails test_every_command_is_classified until it is added here.
MINIMAL: dict[str, tuple[list[str], bool]] = {
    "setup": ([], False),
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
    "check": (["cpython"], False),
    "lint": ([], False),
    "fmt": ([], False),
    "report": ([], False),
    "compile": ([], False),
    "pyz-merge": (["a.pyz", "b.pyz", "--out", "c.pyz"], False),
    "tasks": ([], False),
    "shell-setup": (["bash"], False),
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
    code = cli.main([name, *args, bogus])
    err = capsys.readouterr().err
    assert code == 2, err
    assert "Traceback" not in err


@pytest.mark.parametrize("suite", ["--shells", "--nvim", "--e2e"])
@pytest.mark.usefixtures("no_processes")
def test_the_selftest_suites_reject_an_unknown_option(suite: str) -> None:
    assert cli.main(["selftest", suite, "--pt-bogus-flag"]) == 2


@pytest.mark.parametrize("suite", ["--shells", "--nvim", "--e2e"])
@pytest.mark.usefixtures("no_processes")
def test_a_dry_run_never_starts_a_selftest_suite(suite: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (shells, nvimtest, e2e):
        monkeypatch.setattr(module, "selftest", fail)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    with pytest.raises(DeployError, match="has no --dry-run") as e:
        cli.cmd_selftest(make({}), [suite, "script"])
    assert e.value.code == 2


def test_the_tasks_command_takes_no_arguments() -> None:
    with pytest.raises(DeployError, match="tasks: unrecognized arguments: ci") as e:
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
    with pytest.raises(DeployError, match="program not found") as e:
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
        with pytest.raises(DeployError) as e:
            proc.run([str(program)], echo=False)
        assert e.value.code == 2
        assert "cannot run" in str(e.value) and program.name in str(e.value)


@pytest.mark.parametrize(("name", "message"), [("missing", "folder not found"), ("a-file", "not a folder")])
def test_a_bad_working_folder_is_named(name: str, message: str, tmp_path: Path) -> None:
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    with pytest.raises(DeployError) as e:
        proc.run([sys.executable, "-V"], cwd=tmp_path / name, echo=False)
    assert e.value.code == 2
    assert message in str(e.value) and name in str(e.value)
    assert "program not found" not in str(e.value)


def test_a_dry_run_does_not_need_the_working_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A previous step would create it: a dry run only prints the command
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert proc.run([sys.executable, "-V"], cwd=tmp_path / "later").returncode == 0


def test_run_restores_the_sigint_handler(tmp_path: Path) -> None:
    before = signal.getsignal(signal.SIGINT)
    proc.run([sys.executable, "-c", "pass"], echo=False, cwd=tmp_path)
    assert signal.getsignal(signal.SIGINT) is before
    with pytest.raises(DeployError):
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
    monkeypatch.setattr(proc.shutil, "which", lambda _name: "/elsewhere/uv")
    assert proc.find_uv() == "/elsewhere/uv"
    monkeypatch.delenv("UV")
    monkeypatch.setattr(proc.shutil, "which", lambda _name: None)
    with pytest.raises(DeployError, match="uv not found") as e:
        proc.find_uv()
    assert e.value.code == 3


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
        pytest.skip(f"{tool.dir} does not exist (./deploy setup)")
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


def _interrupt(tmp_path: Path, *modes: str) -> tuple[int, bool, str]:
    """Run DRIVER in its own session, press Ctrl+C (SIGINT to the process group, as a terminal
    does) once the child is ready; return (exit code, marker written before the exit, log)."""
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
        )
    try:
        deadline = time.monotonic() + 60
        while "CHILD-READY" not in log.read_text(encoding="utf-8", errors="replace"):
            if p.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"the child never got ready: {log.read_text(encoding='utf-8', errors='replace')}")
            time.sleep(0.02)
        os.killpg(p.pid, signal.SIGINT)
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


# === 8. a closed stdout (./deploy help | head -1) ==================================================


@posix
@pytest.mark.parametrize("unbuffered", [False, True])
@pytest.mark.parametrize("args", [["help"], ["help", "build"], ["shell-setup", "bash"]])
def test_a_closed_stdout_is_not_a_runner_bug(args: list[str], unbuffered: bool) -> None:
    env = child_env()
    if unbuffered:
        env["PYTHONUNBUFFERED"] = "1"
    read_end, write_end = os.pipe()
    os.close(read_end)  # no reader: every write to stdout fails with EPIPE
    try:
        r = subprocess.run(
            [sys.executable, "-B", str(DEPLOY_PY), *args],
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
    return r


def test_deps_run_in_order_then_the_cmd_with_the_extra_arguments(rec: Recorder) -> None:
    cfg = make({"tasks": {"gen": {"cmd": ["tool", "{backend}"], "deps": ["check all", "other --x"], "uv": False}, "other": {"cmd": ["o"], "uv": False}}})
    assert tasks.run_task(cfg, "gen", ["--y", "{root}", "a b"], rec.dispatch) == 0
    assert rec.dispatched == [["check", "all"]]
    assert rec.runs == [["o", "--x"], ["tool", "cpython", "--y", "{root}", "a b"]]  # extra args are never formatted


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
    with pytest.raises(DeployError, match=f"task cycle: {re.escape(cycle)}$") as e:
        tasks.run_task(make({"tasks": spec}), start, [], rec.dispatch)
    assert e.value.code == 2


def test_a_deps_only_task_rejects_arguments_before_anything_runs(rec: Recorder) -> None:
    cfg = make({"tasks": {"ci": {"deps": ["check all", "test all"]}, "outer": {"deps": ["check", "ci --help"]}}})
    for extra in (["--help"], ["mypyc"], ["a b"]):
        with pytest.raises(DeployError, match="only runs its deps .check all, test all. and takes no arguments") as e:
            tasks.run_task(cfg, "ci", extra, rec.dispatch)
        assert e.value.code == 2 and shlex.join(extra) in str(e.value)
    assert rec.dispatched == []
    with pytest.raises(DeployError, match="takes no arguments: --help"):
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
    with pytest.raises(DeployError, match=rf"tasks\.t\.{where}") as e:
        make({"tasks": {"t": _bad_task(where, bad)}})
    assert e.value.code == 2 and "{{ and }}" in str(e.value)


@pytest.mark.parametrize("where", ["cmd", "env", "cwd"])
@pytest.mark.parametrize("bad", BAD_BRACES)
def test_bad_braces_never_crash_a_task(where: str, bad: str, rec: Recorder) -> None:
    cfg = unchecked({"tasks": {"t": _bad_task(where, bad)}})  # a Config that skipped validate
    with pytest.raises(DeployError, match="task 't'") as e:
        tasks.run_task(cfg, "t", [], rec.dispatch)
    assert e.value.code == 2 and "{{ and }}" in str(e.value)
    assert rec.runs == []


def test_an_unknown_placeholder_is_reported_when_the_task_runs(rec: Recorder) -> None:
    cfg = make({"tasks": {"t": {"cmd": ["{nope}"], "uv": False}, "ok": {"cmd": ["x"]}}})  # it loads
    with pytest.raises(DeployError, match="unknown placeholder 'nope'") as e:
        tasks.run_task(cfg, "t", [], rec.dispatch)
    assert e.value.code == 2 and "{python}" in str(e.value)


@pytest.mark.parametrize("key", ["A=B", "", "1X", "A B", "A-B", chr(0xE9)])
def test_task_env_names_are_validated(key: str) -> None:
    with pytest.raises(DeployError, match=r"tasks\.t\.env: invalid environment variable name") as e:
        make({"tasks": {"t": {"cmd": ["x"], "env": {key: "v"}}}})
    assert e.value.code == 2


@pytest.mark.parametrize("cmd", [[""], ["  ", "x"]])
def test_a_task_program_cannot_be_empty(cmd: list[str]) -> None:
    with pytest.raises(DeployError, match=r"tasks\.t\.cmd: the program"):
        make({"tasks": {"t": {"cmd": cmd}}})


@pytest.mark.parametrize(("dep", "message"), [("run 'x", "No closing quotation"), ("", "empty deps entry"), ("   ", "empty deps entry")])
def test_a_bad_deps_entry_is_reported_before_anything_runs(dep: str, message: str, rec: Recorder) -> None:
    # Loads (vscode.scan renders such a task), fails when the task runs: before its first dep
    cfg = make({"tasks": {"t": {"cmd": ["x"], "deps": ["check", dep]}}})
    with pytest.raises(DeployError, match=message) as e:
        tasks.run_task(cfg, "t", [], rec.dispatch)
    assert e.value.code == 2 and "task 't'" in str(e.value)
    assert rec.dispatched == [] and rec.runs == []


@pytest.mark.parametrize("uv", [True, False])
@pytest.mark.parametrize("cwd", ["no-such-dir-xyz", "pyproject.toml"])
def test_a_task_cwd_must_be_a_folder(cwd: str, uv: bool, rec: Recorder) -> None:
    cfg = make({"tasks": {"t": {"cmd": ["python", "-V"], "cwd": cwd, "uv": uv}}})
    with pytest.raises(DeployError) as e:
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
        with pytest.raises(DeployError, match=r"mode --supports \+pypy") as e:
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


def test_the_task_list_is_shown_with_quiet(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(ui, "QUIET", True)
    assert cli.cmd_tasks(make({"tasks": {"ci": {"deps": ["check all"]}, "gen": {"cmd": ["g"], "help": "Generate"}}}), []) == 0
    err = capsys.readouterr().err
    assert "  ci             deps: check all\n" in err and "  gen            Generate\n" in err
    assert "==>" not in err  # the header is progress
    tasks.list_tasks(make({}))
    assert "No custom tasks" in capsys.readouterr().err


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


def test_check_rejects_extra_arguments_and_unsupported_backends(checks: FakeChecks) -> None:
    for args, message in ((["all", "extra"], "unrecognized arguments: extra"), (["foo"], "unrecognized arguments: foo"), (["pypy"], "not in backend.supported")):
        with pytest.raises(DeployError, match=message) as e:
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
    ok, fake = _checks_with(monkeypatch, make({"typing": {"relaxed": relaxed}}), ruff=ruff, mypy=mypy)
    assert ok is passed
    ruff_argv = fake.tool("ruff")
    assert ruff_argv is not None and ("--exit-zero" in ruff_argv) == (relaxed == "warn")
    assert (fake.tool("mypy") is None) == (relaxed == "off")  # the off profile skips mypy
    if relaxed == "warn" and mypy == 1:
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
    ok, fake = _checks_with(monkeypatch, cfg, basedpyright=1)
    assert ok is passed  # a failure only blocks under a blocking profile
    (argv,) = [c for c in fake.calls if "basedpyright" in c]
    withs = [argv[i + 1] for i, a in enumerate(argv) if a == "--with"]
    assert withs == [cmd_dev.BASEDPYRIGHT, cmd_dev.BASEDPYRIGHT_NODE]
    assert argv[argv.index("--project") + 1] == str(tmp_path / "cfg" / f"pyright-{relaxed}.json")


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
    assert ruff_toml.read_text(encoding="utf-8") == render.to_toml(render.ruff_config(cfg, "warn", absolute=True)) + "\n"


@pytest.mark.parametrize(("relaxed", "exit_zero"), [("warn", True), ("strict", False), ("off", False)])
def test_lint_honours_the_profiles_exit_zero(relaxed: str, exit_zero: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(envs, "uv_run", lambda _env, argv, **_kw: calls.append([str(a) for a in argv]) or completed(argv))
    assert cmd_dev.cmd_lint(make({"typing": {"relaxed": relaxed}}), ["--fix"]) == 0
    assert calls[0][:3] == ["ruff", "check", "--fix"]
    assert ("--exit-zero" in calls[0]) == exit_zero


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
            raise DeployError("mypyc failed (exit code 1)", 1)
        return self.stage


@pytest.fixture
def fake_tests(monkeypatch: pytest.MonkeyPatch) -> FakeTests:
    fake = FakeTests(ROOT / ".build" / "pt-stage")
    monkeypatch.setattr(envs, "uv_run", fake.uv_run)
    monkeypatch.setattr(mypyc, "build", fake.build)
    return fake


def test_test_argv_and_the_compiled_proof(fake_tests: FakeTests) -> None:
    cfg = make({"backend": {"supported": ["cpython", "pypy", "mypyc"]}})
    assert cmd_dev.test_backend(cfg, "mypyc", ["-k", "x"]) == 0
    key, argv, extra = fake_tests.calls[-1]
    assert key == "cpython" and argv == ["python", "-m", "pytest", "-o", "pythonpath=.build/pt-stage", "-k", "x"]
    assert extra["PYTEMPLATE_BACKEND"] == "mypyc"
    assert extra["PYTEMPLATE_COMPILED"] == ",".join(mypyc.compiled_modules(cfg)) != ""  # conftest's proof
    assert cmd_dev.test_backend(cfg, "cpython", []) == 0
    assert fake_tests.calls[-1] == ("cpython", ["python", "-m", "pytest"], {"PYTEMPLATE_BACKEND": "cpython"})
    assert cmd_dev.test_backend(cfg, "pypy", ["-q"]) == 0
    assert fake_tests.calls[-1] == ("pypy", ["python", "-m", "pytest", "-q"], {"PYTEMPLATE_BACKEND": "pypy"})


def test_test_returns_pytests_code_for_one_backend(fake_tests: FakeTests, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make({})
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
    fake_tests.codes["build"] = 1  # the mypyc build raises DeployError
    assert cmd_dev.cmd_test(cfg, ["all"]) == 1
    assert [c[2]["PYTEMPLATE_BACKEND"] for c in fake_tests.calls] == ["cpython"]  # cpython still ran
    err = capsys.readouterr().err
    assert "error: test mypyc: mypyc failed" in err
    assert "[XX] mypyc" in err and "[ok] cpython" in err
    with pytest.raises(DeployError, match="mypyc failed"):  # one backend: the error is the answer
        cmd_dev.cmd_test(cfg, ["mypyc"])


def test_run_returns_the_apps_code(fake_tests: FakeTests) -> None:
    cfg = make({})
    fake_tests.codes["cpython"] = 7
    assert cmd_dev.cmd_run(cfg, ["--frames", "3"]) == 7
    assert fake_tests.calls[-1][1] == ["python", str(SRC / "main.py"), "--frames", "3"]
    assert cmd_dev.cmd_run(cfg, ["mypyc", "x"]) == 7
    assert fake_tests.calls[-1][1] == ["python", str(fake_tests.stage / "main.py"), "x"]
    with pytest.raises(DeployError, match="not in backend.supported"):
        cmd_dev.cmd_run(cfg, ["pypy"])


@pytest.mark.usefixtures("fake_tests")
def test_report_needs_mypyc_and_never_opens_a_browser_in_a_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(DeployError, match="report comes from mypyc"):
        cmd_dev.cmd_report(make({"backend": {"supported": ["cpython"]}}), [])
    monkeypatch.setattr(cmd_dev.webbrowser, "open", fail)
    monkeypatch.setattr(cmd_dev, "_profile_file", lambda _cfg, _profile, _kind: Path("mypy.ini"))
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert cmd_dev.cmd_report(make({}), ["--open"]) == 0


# === 11. end to end: the exit codes cross deploy.py (a throwaway copy, no uv needed) ==============


@pytest.fixture(scope="module")
def tasks_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("cli")
    presets.copy_template(dest)
    py = json.dumps(sys.executable)
    extra = f"""
[tasks.exit7]
cmd = [{py}, "-c", "raise SystemExit(7)"]
uv = false

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
    toml.write_text(toml.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return dest


def _deploy(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(root / ".pytemplate" / "deploy.py"), *args],
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
        (["ci", "--no-such"], 2, "takes no arguments"),
        pytest.param(["killed"], 137, "", marks=posix),
    ],
)
def test_task_exit_codes_cross_deploy_py(tasks_project: Path, args: list[str], code: int, stderr: str) -> None:
    r = _deploy(tasks_project, "--no-render", *args)
    assert r.returncode == code, r.stderr
    assert stderr in r.stderr
    assert "SHOULD-NOT-RUN" not in r.stdout and "Traceback" not in r.stderr


def test_quiet_keeps_what_was_asked_for(tasks_project: Path) -> None:
    r = _deploy(tasks_project, "-q", "--no-render", "tasks")
    assert r.returncode == 0 and "  exit7" in r.stderr and "==>" not in r.stderr
    r = _deploy(tasks_project, "-q", "--no-render", "--dry-run", "cyc-b", "--x")  # the plan's error still shows
    assert r.returncode == 2 and "takes no arguments" in r.stderr


uv_on_path = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")


@uv_on_path
def test_quiet_does_not_hide_a_dry_runs_plan(tasks_project: Path) -> None:
    r = _deploy(tasks_project, "-q", "--no-render", "--dry-run", "sync", "cpython")
    assert r.returncode == 0, r.stderr
    assert "$ uv sync --locked" in r.stderr
    assert not (tasks_project / ".venv").exists()


@uv_on_path
def test_a_dry_run_of_sync_and_add_changes_nothing(tasks_project: Path) -> None:
    files = {name: (tasks_project / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    for args in (["sync", "cpython"], ["add", "requests"], ["remove", "rich"]):
        r = _deploy(tasks_project, "--no-render", "--dry-run", *args)
        assert r.returncode == 0, r.stderr
        assert f"$ uv {args[0]}" in r.stderr
    assert {name: (tasks_project / name).read_bytes() for name in files} == files
    assert not (tasks_project / ".venv").exists()


@posix
@uv_on_path
@pytest.mark.parametrize(("task", "code"), [("exit7", 7), ("killed", 137), ("ci --x", 2)])
def test_task_exit_codes_cross_the_sh_launcher(tasks_project: Path, task: str, code: int) -> None:
    # deploy -> uv run --script -> deploy.py: nothing on the way may change the code
    r = subprocess.run(
        ["sh", str(tasks_project / "deploy"), "--no-render", *task.split()],
        cwd=tasks_project,
        env=child_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert r.returncode == code, r.stderr


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
    with pytest.raises(DeployError) as e:
        presets.check_name_free(None, preset, name)
    assert e.value.code == 2 and "--name" in str(e.value)
    presets.check_name_free(None, preset, "my-app")


def test_librt_is_forbidden_only_while_pypy_is_supported(tmp_path: Path) -> None:
    module = tmp_path / "fast.py"
    module.write_text("import librt\nfrom librt.base64 import b64encode\n", encoding="utf-8")
    with_pypy = lintc.lint_file(make({"backend": {"supported": ["cpython", "pypy", "mypyc"]}}), module)
    assert len([f for f in with_pypy if "librt" in f.message]) == 2
    assert not [f for f in lintc.lint_file(make({}), module) if "librt" in f.message]
