"""Exit codes of the selftest harnesses the template's CI trusts: plain `./pyt selftest`
(cli.cmd_selftest), `selftest --shells` (shells.selftest) and `selftest --nvim`
(nvimtest.selftest). A harness that returned 0 after a FAIL would keep every workflow green.

No shell, Neovim, pytest or mypy runs here: the probes, the smoke runs and the uv calls are
faked; the result collection, the tables and the exit logic are the real ones. (`--e2e`:
test_e2e_plan.py.)"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cli, cmd_nvim, config, e2e, envs, mutation, nvimtest, project, shells  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import TEMPLATE  # noqa: E402
from runner.shells import Result, Shell  # noqa: E402
from runner.ui import PytError  # noqa: E402


PREPARE_BASE = nvimtest.prepare_base  # the real one: the nvim_run fixture fakes it


def make() -> Config:
    cfg: Config = config._build(Config, {}, "")
    config.validate(cfg)
    return cfg


# --- plain ./pyt selftest: pytest, then mypy --strict ----------------------------------------------


class FakeUvRun:
    """envs.uv_run stand-in: records each call and answers with the next exit code."""

    def __init__(self, *codes: int) -> None:
        self.codes = list(codes)
        self.calls: list[tuple[str, list[str], bool]] = []

    def __call__(self, env: envs.PyEnv, argv: list[Any], *, check: bool = True, **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((env.key, [str(a) for a in argv], check))
        return subprocess.CompletedProcess(argv, self.codes.pop(0), "", "")


@pytest.mark.parametrize(
    ("pytest_code", "mypy_code", "expected"),
    [(0, 0, 0), (1, 0, 1), (0, 1, 1), (1, 1, 1), (5, 0, 5), (2, 1, 2)],  # pytest's own code first (5: nothing collected)
)
def test_plain_selftest_fails_when_pytest_or_mypy_fails(monkeypatch: pytest.MonkeyPatch, pytest_code: int, mypy_code: int, expected: int) -> None:
    fake = FakeUvRun(pytest_code, mypy_code)
    monkeypatch.setattr(envs, "uv_run", fake)
    assert cli.cmd_selftest(make(), ["-k", "x"]) == expected
    (pt_env, pt_argv, pt_check), (my_env, my_argv, my_check) = fake.calls  # mypy runs even after a pytest failure
    assert pt_env == my_env == envs.tool_env(make()).key and not pt_check and not my_check
    assert pt_argv[:5] == ["python", "-m", "pytest", "-q", "-p"] and pt_argv[-3:] == [str(Path(".pytemplate", "tests")), "-k", "x"]
    assert my_argv[:3] == ["mypy", "--strict", "--no-incremental"] and "--python-version" in my_argv
    assert my_argv[my_argv.index("--python-version") + 1] == "3.11"  # the runner's floor
    assert my_argv[-2:] == [str(TEMPLATE / "runner"), str(TEMPLATE / "pyt.py")]


@pytest.mark.parametrize("folder", ["project", "Projects [2026]"])
def test_plain_selftest_runs_the_suite_with_its_own_pytest_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str) -> None:
    """pytest looked for its settings itself and read the project's, which are its app's tests':
    a coverage gate in addopts (here an unknown option, exit 4: this .venv has no pytest-cov;
    with it "total of 0 is less than fail-under"), python_files (no runner test collected: exit
    5) and its root conftest.py all reached the runner's suite. The suite has its own
    (.pytemplate/tests/pytest.ini, -c), and the test ids stay .pytemplate/tests/..., which CI
    deselects by (template-selftest's uv-floor job). The real pytest of .venv runs the suite's
    real settings here; only uv run and mypy are left out. A project folder with a '[' (pytest
    9 reads one in a collection argument as a parametrization): the absolute tests folder
    stopped the suite, "path cannot contain [] parametrization", exit 4."""
    project_dir = tmp_path / folder
    tests = project_dir / ".pytemplate" / "tests"
    tests.mkdir(parents=True)
    for name in ("pytest.ini", "conftest.py"):
        shutil.copyfile(TEMPLATE / "tests" / name, tests / name)
    (tests / "test_tiny.py").write_text("def test_ok() -> None:\n    pass\n\n\ndef test_other() -> None:\n    pass\n", encoding="utf-8")
    (project_dir / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["-ra", "--cov=src", "--cov-fail-under=50"]\npython_files = ["*_check.py"]\n',
        encoding="utf-8",
    )
    (project_dir / "conftest.py").write_text('raise RuntimeError("the project conftest reached the suite")\n', encoding="utf-8")
    monkeypatch.setattr(project, "ROOT", project_dir)
    monkeypatch.setattr(project, "TEMPLATE", project_dir / ".pytemplate")
    ran: list[subprocess.CompletedProcess[str]] = []

    def uv_run(env: envs.PyEnv, argv: list[Any], *, cwd: Path | None = None, check: bool = True, **_: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        if args[:3] != ["python", "-m", "pytest"]:
            return subprocess.CompletedProcess(args, 0, "", "")  # mypy of the runner: not this test's
        r = subprocess.run([sys.executable, *args[1:]], cwd=cwd, capture_output=True, text=True, timeout=300, check=False)
        ran.append(r)
        return r

    monkeypatch.setattr(envs, "uv_run", uv_run)
    assert cli.cmd_selftest(make(), ["-rA", "--deselect", ".pytemplate/tests/test_tiny.py::test_other"]) == 0, ran[0].stdout + ran[0].stderr
    assert "PASSED .pytemplate/tests/test_tiny.py::test_ok" in ran[0].stdout
    assert "1 passed, 1 deselected" in ran[0].stdout, ran[0].stdout


@pytest.mark.parametrize("flag", ["-h", "--help", "--version", "-V"])
def test_plain_selftest_help_runs_no_mypy(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """`./pyt selftest --help` printed pytest's help, then ran mypy --strict of the whole runner
    (seconds, more on Windows) and took mypy's exit code."""
    fake = FakeUvRun(0)
    monkeypatch.setattr(envs, "uv_run", fake)
    assert cli.cmd_selftest(make(), [flag]) == 0
    assert [argv[:3] for _, argv, _ in fake.calls] == [["python", "-m", "pytest"]]


@pytest.mark.parametrize("suite", ["--shells", "--nvim", "--e2e", "--mutation"])
@pytest.mark.parametrize("code", [0, 1, 3])
def test_a_suite_returns_its_own_exit_code(monkeypatch: pytest.MonkeyPatch, suite: str, code: int) -> None:
    got: list[list[str]] = []

    def fake(cfg: Config, args: list[str]) -> int:
        got.append(args)
        return code

    for module in (shells, nvimtest, e2e, mutation):
        monkeypatch.setattr(module, "selftest", fake)
    monkeypatch.setattr(envs, "uv_run", FakeUvRun())  # pytest must not start
    assert cli.cmd_selftest(make(), [suite, "a", "--b"]) == code
    assert got == [["a", "--b"]]


# --- selftest --shells ---------------------------------------------------------------------------------

SHELLS = [Shell("sh", "posix", ("/bin/sh",)), Shell("dash", "posix", ("/bin/dash",))]


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """Two fake shells; run_test answers each (shell, test) with the status set here (default
    pass), or raises OSError for the status "raise". run_shell, _run_all and the rest are real."""
    status: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(shells, "discover", lambda *a, **k: list(SHELLS))

    def run_test(ctx: shells.Context, sh: Shell, test: str) -> Result:
        want = status.get((sh.name, test), "pass")
        if want == "raise":
            raise OSError("the shell vanished")
        return Result(sh.name, test, want, 1, "detail" if want != "pass" else "", "sh")

    monkeypatch.setattr(shells, "run_test", run_test)
    return status


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({}, 0),
        ({("dash", "T2"): "fail"}, 1),
        ({("sh", "T4"): "skip", ("dash", "T7"): "n/a"}, 0),  # skips and n/a are not failures
        ({("sh", "T1"): "skip", ("dash", "T6"): "fail"}, 1),
        ({("dash", "T3"): "raise"}, 1),  # a probe that raises is a FAIL, never a crash
    ],
)
def test_shells_exit_code_follows_the_failures(probes: dict[tuple[str, str], str], statuses: dict[tuple[str, str], str], expected: int) -> None:
    probes.update(statuses)
    assert shells.selftest(make(), ["--jobs", "2"]) == expected


def test_shells_json_report_counts_what_ran(probes: dict[tuple[str, str], str], capsys: pytest.CaptureFixture[str]) -> None:
    probes[("sh", "T5")] = "fail"
    probes[("dash", "T4")] = "skip"
    assert shells.selftest(make(), ["--json", "--tests", "T4,T5"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"] == {"pass": 2, "fail": 1, "skip": 1}
    assert {(r["shell"], r["test"], r["status"]) for r in report["results"]} == {
        ("sh", "T4", "pass"), ("sh", "T5", "fail"), ("dash", "T4", "skip"), ("dash", "T5", "pass"),
    }  # fmt: skip


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_shells_help_is_the_suites_help(
    probes: dict[tuple[str, str], str], flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`./pyt selftest --shells -h` said `unknown option -h` (exit 2), while -h after --nvim,
    --e2e and --mutation prints that suite's help: its usage on stdout, exit 0, nothing probed."""
    monkeypatch.setattr(shells, "run_test", lambda *a: pytest.fail("-h must not probe"))
    assert cli.cmd_selftest(make(), ["--shells", "dash", flag, "--bogus"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: {shells.SELFTEST_USAGE}\n"), out
    assert "--timeout S" in out and "T7 hints" in out, out


def test_shells_list_runs_nothing(probes: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shells, "run_test", lambda *a: pytest.fail("--list must not probe"))
    assert shells.selftest(make(), ["--list"]) == 0


def test_quiet_keeps_what_selftest_shells_was_asked_for(
    probes: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """-q hides progress, never the answer (CLAUDE.md 5.3): the --list rows, the result table and
    which shell failed which test, and why."""
    from runner import ui

    monkeypatch.setattr(ui, "QUIET", True)
    assert shells.selftest(make(), ["--list"]) == 0
    listed = capsys.readouterr().err
    assert "/bin/sh" in listed and "/bin/dash" in listed, listed
    probes[("dash", "T2")] = "fail"
    assert shells.selftest(make(), ["--tests", "T1,T2"]) == 1
    err = capsys.readouterr().err
    assert "T1 argv" in err and "failures:" in err and "dash T2 exit: detail" in err, err
    assert "dash: FAIL T2" not in err  # the per-shell progress lines stay hidden


def test_quiet_keeps_where_the_kept_scratch_files_are(
    probes: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--keep was asked for, and the folder has a random name: `-q selftest --shells --keep` kept it
    without saying where."""
    import tempfile

    from runner import ui

    monkeypatch.setattr(ui, "QUIET", True)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert shells.selftest(make(), ["--keep", "--tests", "T2"]) == 0
    kept = list(tmp_path.glob("pts-*"))
    err = capsys.readouterr().err
    assert len(kept) == 1 and f"scratch files kept in {kept[0]}" in err, err


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM/SIGHUP and process groups are POSIX")
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_shells_termination_signal_kills_the_probe_and_removes_the_scratch_folder(tmp_path: Path, signame: str) -> None:
    """`kill <pid>`, a closed terminal: the runner died at once, left its pts-* scratch folder
    in the temp folder, and a probe (in a session of its own) went on. Now the run stops like
    Ctrl+C: the probe's tree is killed, the folder goes, `error: interrupted`, exit 130."""
    import signal

    pidfile = tmp_path / "probe.pid"
    temp = tmp_path / "temp"
    temp.mkdir()
    step = f"import os, pathlib, time; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(120)"
    script = "\n".join(
        [
            "import signal, sys, tempfile",
            "for sig in (signal.SIGTERM, signal.SIGHUP):  # a terminal session's (nohup'ed, SIGHUP stays ignored)",
            "    signal.signal(sig, signal.SIG_DFL)",
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})",
            "from runner import cli, shells",
            f"tempfile.tempdir = {str(temp)!r}",
            "shells.discover = lambda *a, **k: [shells.Shell('sh', 'posix', ('/bin/sh',))]",
            "def run_test(ctx, sh, test):",
            f"    shells.spawn(ctx, [sys.executable, '-c', {step!r}], cwd=ctx.tmp, env=ctx.env, tag=test)",
            "    return shells.Result(sh.name, test, 'pass', 1, '', 'sh')",
            "shells.run_test = run_test",
            "sys.exit(cli.main(['selftest', '--shells', 'sh', '--tests', 'T1']))",
        ]
    )
    log = tmp_path / "runner.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEMPLATE_")}
    with log.open("wb") as out:
        runner = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env)
    child = 0
    try:
        deadline = time.monotonic() + 60
        while not (pidfile.is_file() and pidfile.read_text()):
            assert runner.poll() is None, log.read_text()
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        child = int(pidfile.read_text())
        runner.send_signal(getattr(signal, signame))
        assert runner.wait(timeout=60) == 130, log.read_text()
        assert "error: interrupted" in log.read_text(), log.read_text()
        assert _gone(child), "the probe outlived the runner"
        assert list(temp.iterdir()) == [], "the scratch folder was left behind"
    finally:
        if runner.poll() is None:
            runner.kill()
        if child and not _gone(child, within=0):
            os.kill(child, signal.SIGKILL)


def test_a_probe_that_starts_after_an_interrupt_is_never_left_running(probes: dict[tuple[str, str], str], tmp_path: Path) -> None:
    """Once the suite is interrupted, a worker thread that goes on to its shell's next test starts
    no probe: it was never killed, and ran until its timeout after the runner had stopped."""
    ctx = shells.Context(project=tmp_path, sub=tmp_path, tmp=tmp_path, away=tmp_path, timeout=60, env=dict(os.environ))
    (tmp_path / "out").mkdir()
    ctx.stopped.set()
    run = shells.spawn(ctx, [sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path, env=os.environ, tag="late")
    assert run.rc is None and "interrupted" in run.error and not ctx.running


def test_shells_refuse_what_cannot_run(probes: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(PytError, match="not found here: fish") as e:
        shells.selftest(make(), ["fish"])
    assert e.value.code == 2
    with pytest.raises(PytError, match="no .pytemplate/pyt.py there") as e:
        shells.selftest(make(), ["--project", str(tmp_path)])
    assert e.value.code == 2
    monkeypatch.setattr(shells, "discover", lambda *a, **k: [])
    with pytest.raises(PytError, match="no shell found") as e:
        shells.selftest(make(), [])
    assert e.value.code == 2


# --- selftest --nvim -----------------------------------------------------------------------------------


def _nvim(tmp_path: Path, version: tuple[int, int, int]) -> cmd_nvim.Nvim:
    x = tmp_path / "w" / "x"
    return cmd_nvim.Nvim("nvim", version, x / "config", x / "data", x / "state", x / "cache")


def _row(preset: str, ok: bool) -> nvimtest.Row:
    if ok:
        return nvimtest.Row(preset, smoke=nvimtest.Smoke(passed=["a", "b"], expected=2), code=0)
    return nvimtest.Row(preset, smoke=nvimtest.Smoke(passed=["a"], failed=[("b", "boom")], expected=2), code=1)


@pytest.fixture
def nvim_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """A selftest --nvim whose Neovim, base install and smoke runs are faked (`--dir` in tmp)."""
    state: dict[str, Any] = {"which": {"nvim": "/x/nvim", "git": "/x/git"}, "version": (0, 12, 5), "ok": {}, "ran": []}
    monkeypatch.setattr(cmd_nvim, "which", lambda name: state["which"].get(name))
    monkeypatch.setattr(cmd_nvim, "query", lambda exe=None, *, env=None: _nvim(tmp_path, state["version"]))
    monkeypatch.setattr(nvimtest, "uv_dirs", lambda env: {})
    monkeypatch.setattr(nvimtest, "prepare_base", lambda layout, exe, env, *, fresh: (_nvim(tmp_path, state["version"]), None))

    def run_preset(preset: str, layout: nvimtest.Layout, nv: cmd_nvim.Nvim, **_: Any) -> nvimtest.Row:
        state["ran"].append(preset)
        return _row(preset, state["ok"].get(preset, True))

    monkeypatch.setattr(nvimtest, "run_preset", run_preset)
    monkeypatch.setattr(nvimtest, "record_pins", lambda layout, nv: "the pins")
    state["args"] = ["--dir", str(tmp_path / "w")]
    return state


@pytest.mark.parametrize(("failed", "expected"), [((), 0), (("raylib",), 1), (("script", "flet"), 1)])
def test_nvim_exit_code_follows_the_presets(nvim_run: dict[str, Any], failed: tuple[str, ...], expected: int) -> None:
    nvim_run["ok"] = {p: False for p in failed}
    assert nvimtest.selftest(make(), ["script,raylib,flet", *nvim_run["args"]]) == expected
    assert nvim_run["ran"] == ["script", "raylib", "flet"]  # a failed preset does not stop the others


def test_quiet_keeps_what_selftest_nvim_was_asked_for(
    nvim_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """-q hides progress, never the answer (CLAUDE.md 5.3): `-q selftest --nvim` printed only
    `error: script: FAIL b`, without the table, the reason, the SKIP lines or where the logs are."""
    from runner import ui

    monkeypatch.setattr(ui, "QUIET", True)
    row = nvimtest.Row("script", smoke=nvimtest.Smoke(passed=["a"], failed=[("b", "the reason it failed")], skipped=["c (no network)"], expected=3), code=1)
    monkeypatch.setattr(nvimtest, "run_preset", lambda preset, layout, nv, **_: row)
    assert nvimtest.selftest(make(), ["script", *nvim_run["args"]]) == 1
    err = capsys.readouterr().err
    assert "  script   FAIL" in err and "    the reason it failed" in err and "script: SKIP c (no network)" in err, err
    assert "isolated LazyVim: reused (cached)" in err and "pinned to: the pins" in err and "logs: " in err, err
    assert "preset script" not in err  # the progress lines stay hidden


@pytest.mark.parametrize("missing", ["nvim", "git"])
def test_nvim_missing_tool_skips_or_fails_with_require(nvim_run: dict[str, Any], missing: str) -> None:
    del nvim_run["which"][missing]
    assert nvimtest.selftest(make(), ["script", *nvim_run["args"]]) == 0  # a developer machine: SKIP
    with pytest.raises(PytError, match=f"{missing} not found") as e:
        nvimtest.selftest(make(), ["script", "--require", *nvim_run["args"]])  # CI: fail loudly
    assert e.value.code == 3 and nvim_run["ran"] == []


def test_nvim_older_than_lazyvims_minimum_skips_or_fails_with_require(nvim_run: dict[str, Any]) -> None:
    major, minor, patch = cmd_nvim.MIN_LAZYVIM
    nvim_run["version"] = (major, minor, patch - 1) if patch else (major, minor - 1, 99)
    assert nvimtest.selftest(make(), ["script", *nvim_run["args"]]) == 0
    with pytest.raises(PytError, match="older than LazyVim's minimum") as e:
        nvimtest.selftest(make(), ["script", "--require", *nvim_run["args"]])
    assert e.value.code == 3 and nvim_run["ran"] == []
    nvim_run["version"] = cmd_nvim.MIN_LAZYVIM  # the minimum itself runs
    assert nvimtest.selftest(make(), ["script", "--require", *nvim_run["args"]]) == 0 and nvim_run["ran"] == ["script"]


def test_nvim_unknown_preset_is_a_usage_error(nvim_run: dict[str, Any]) -> None:
    with pytest.raises(PytError, match="unknown preset") as e:
        nvimtest.selftest(make(), ["script,nosuch", *nvim_run["args"]])
    assert e.value.code == 2 and nvim_run["ran"] == []


def test_nvim_base_that_cannot_be_installed_fails_the_suite(nvim_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """No network, a git failure, a timeout: exit 1 (a FAIL of the suite), never 2 (usage)."""
    monkeypatch.setattr(nvimtest, "prepare_base", PREPARE_BASE)

    def run_logged(argv: list[str], *, cwd: Path, env: dict[str, str], log: Path, timeout: float) -> int:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("fatal: unable to access 'https://github.com/LazyVim/starter/'\n", encoding="utf-8")
        return 128

    monkeypatch.setattr(nvimtest, "_run_logged", run_logged)
    with pytest.raises(PytError, match="clone the LazyVim starter: exit code 128") as e:
        nvimtest.selftest(make(), ["script", *nvim_run["args"]])
    assert e.value.code == 1 and nvim_run["ran"] == []


def test_nvim_dir_that_is_a_file_is_a_usage_error(nvim_run: dict[str, Any], tmp_path: Path) -> None:
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(PytError, match="is not a folder") as e:
        nvimtest.selftest(make(), ["script", "--dir", str(afile)])
    assert e.value.code == 2 and nvim_run["ran"] == []


def test_nvim_pyt_steps_never_touch_a_repository_around_the_dir(nvim_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A --dir inside the user's git work tree with core.filemode = false (Git for Windows; a
    default %TEMP% under a dotfiles repository): the harness's `./pyt new` saw that repository,
    skipped git init and staged the scratch project's launchers there (`git add --chmod=+x`); the
    project was deleted afterwards and the user's next commit recorded two files of a folder that
    no longer existed. The step here runs the real presets._git_init in a child with the
    environment the harness gives its ./pyt steps."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("needs git")
    genv = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "no-gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run([git, "init", "-q"], cwd=outer, env=genv, check=True)
    subprocess.run([git, "config", "core.filemode", "false"], cwd=outer, env=genv, check=True)
    seen: dict[str, dict[str, str]] = {}

    def run_preset(preset: str, layout: nvimtest.Layout, nv: cmd_nvim.Nvim, *, renv: dict[str, str], venv: dict[str, str], **_: Any) -> nvimtest.Row:
        proj = layout.projects / preset
        proj.mkdir(parents=True)
        for name in ("pyt", "pyt.ps1"):
            (proj / name).write_text("#!/bin/sh\n", encoding="utf-8")
        code = "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from runner import presets; presets._git_init(Path(sys.argv[2]))"
        subprocess.run([sys.executable, "-c", code, str(TEMPLATE), str(proj)], env=renv, stdin=subprocess.DEVNULL, capture_output=True, check=True)
        seen.update(renv=renv, venv=venv)
        return _row(preset, True)

    monkeypatch.setattr(nvimtest, "run_preset", run_preset)
    assert nvimtest.selftest(make(), ["script", "--keep", "--dir", str(outer / "nvt")]) == 0
    staged = subprocess.run([git, "diff", "--cached", "--name-only"], cwd=outer, env=genv, capture_output=True, text=True, check=True).stdout
    assert staged == "", f"the scratch project's files were staged in the repository around --dir: {staged}"
    assert (outer / "nvt" / "p" / "script" / ".git").is_dir()  # the project's own repository instead
    # Neovim keeps the user's git configuration (lazy.nvim clones the plugins with it): the user's
    # own GIT_CONFIG_GLOBAL, else a <dir>/gitconfig that includes it (nvim_env moves XDG_CONFIG_HOME,
    # so git would otherwise miss $XDG_CONFIG_HOME/git/config: nvimtest.user_git_config)
    user_gcg = os.environ.get("GIT_CONFIG_GLOBAL")
    gcg = seen["venv"].get("GIT_CONFIG_GLOBAL")
    if user_gcg is not None:
        assert gcg == user_gcg
    elif gcg is not None:
        assert Path(gcg) == outer / "nvt" / "gitconfig"
    assert seen["renv"]["GIT_CONFIG_NOSYSTEM"] == "1"


def test_nvim_dir_git_cannot_be_kept_inside_is_refused(nvim_run: dict[str, Any], tmp_path: Path) -> None:
    """README promises the scratch projects git repositories of their own even with --dir inside
    one of the user's: below a folder named with os.pathsep (`a:b`) the git ceiling could not
    hold it, and `new` skipped git init there. Refused before anything is created."""
    d = tmp_path / f"a{os.pathsep}b" / "w"
    with pytest.raises(PytError, match="GIT_CEILING_DIRECTORIES") as e:
        nvimtest.selftest(make(), ["script", "--dir", str(d)])
    assert e.value.code == 2 and nvim_run["ran"] == [] and not d.parent.exists()


def test_nvim_dir_neovim_cannot_put_on_its_runtimepath_is_refused(nvim_run: dict[str, Any], tmp_path: Path) -> None:
    """spec.lua switches the ./pyt integration off in a project whose path Neovim cannot put on
    its runtimepath, as `nvim trust` refuses one: a --dir named with `[` installed the isolated
    LazyVim, made and synced every project, then failed every smoke check with "is .lazy.lua
    trusted?". Refused before anything is made."""
    d = tmp_path / "rtp[2]" / "w"
    with pytest.raises(PytError, match="holds `\\[`, which Neovim cannot put on its 'runtimepath'") as e:
        nvimtest.selftest(make(), ["script", "--dir", str(d)])
    assert e.value.code == 2 and nvim_run["ran"] == [] and not d.parent.exists()


def test_nvim_dir_that_would_hide_the_templates_repository_is_refused(nvim_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A --dir next to a template that is a subfolder of a bigger repository: git there must not
    see a repository around --dir, which hides the template's own (new would copy its untracked
    files). Refused before anything is created, as for selftest --e2e."""
    asked: list[str] = []

    def hidden(env: dict[str, str]) -> str:
        asked.append(env["GIT_CEILING_DIRECTORIES"])
        return "/big"

    monkeypatch.setattr(e2e, "hidden_template_repository", hidden)
    with pytest.raises(PytError, match="inside its git repository /big") as e:
        nvimtest.selftest(make(), ["script", *nvim_run["args"]])
    assert e.value.code == 2 and nvim_run["ran"] == [] and not (tmp_path / "w").exists()
    assert asked == [str((tmp_path / "w").resolve().parent)]


def _gone(pid: int, within: float = 10.0) -> bool:
    """True once `pid` runs no more (a zombie waiting for its parent counts as gone)."""
    deadline = time.monotonic() + within
    while True:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        stat = Path(f"/proc/{pid}/stat")
        try:
            if stat.is_file() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except OSError:
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM/SIGHUP and process groups are POSIX")
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_nvim_termination_signal_kills_the_running_step(tmp_path: Path, signame: str) -> None:
    """`timeout 30m ./pyt selftest --nvim`, a closed terminal, `kill <pid>`: the runner died at
    once and the running step (a headless Neovim with its git, Mason and uv jobs), in a session of
    its own, went on as an orphan writing into --dir. Now the run stops like Ctrl+C: the step's
    tree is killed, `error: interrupted`, exit 130."""
    import signal

    pidfile = tmp_path / "step.pid"
    step = f"import os, pathlib, time; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(120)"
    x = tmp_path / "w" / "x"
    script = "\n".join(
        [
            "import os, signal, sys",
            "from pathlib import Path",
            "for sig in (signal.SIGTERM, signal.SIGHUP):  # a terminal session's (nohup'ed, SIGHUP stays ignored)",
            "    signal.signal(sig, signal.SIG_DFL)",
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})",
            "from runner import cli, cmd_nvim, nvimtest",
            f"x = Path({str(x)!r})",
            "nv = cmd_nvim.Nvim('nvim', (0, 12, 5), x / 'config', x / 'data', x / 'state', x / 'cache')",
            "cmd_nvim.which = lambda name: '/x/' + name",
            "cmd_nvim.query = lambda exe=None, *, env=None: nv",
            "nvimtest.uv_dirs = lambda env: {}",
            "nvimtest.prepare_base = lambda layout, exe, env, *, fresh: (nv, None)",
            "def run_preset(preset, layout, nv, **_):",
            f"    nvimtest._run_logged([sys.executable, '-c', {step!r}], cwd=layout.base, env=os.environ, log=layout.logs / 'step.log', timeout=300)",
            "    return nvimtest.Row(preset)",
            "nvimtest.run_preset = run_preset",
            f"sys.exit(cli.main(['selftest', '--nvim', 'script', '--dir', {str(tmp_path / 'w')!r}]))",
        ]
    )
    log = tmp_path / "runner.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEMPLATE_")}
    with log.open("wb") as out:
        runner = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env)
    child = 0
    try:
        deadline = time.monotonic() + 60
        while not (pidfile.is_file() and pidfile.read_text()):
            assert runner.poll() is None, log.read_text()
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        child = int(pidfile.read_text())
        runner.send_signal(getattr(signal, signame))
        assert runner.wait(timeout=60) == 130, log.read_text()
        text = log.read_text()
        assert "error: interrupted" in text and f"logs of the interrupted run: {tmp_path / 'w' / 'logs'}" in text, text
        assert _gone(child), "the step outlived the runner"
    finally:
        if runner.poll() is None:
            runner.kill()
        if child and not _gone(child, within=0):
            os.kill(child, signal.SIGKILL)


SIGNAL_TESTS = [
    "test_cli_core.py::test_an_ignored_sigterm_is_passed_on_to_the_child",
    "test_rename.py::test_an_interrupted_rename_is_undone",
    "test_rename.py::test_a_second_ctrl_c_waits_for_the_undo",
    "test_install.py::test_a_second_ctrl_c_waits_for_the_undo_of_the_swap",
    "test_mutation.py::test_a_test_run_that_a_keyboard_interrupt_ends_is_a_kill",
    "test_mutation.py::test_run_all_goes_on_as_an_interrupt",
]


@pytest.mark.skipif(sys.platform == "win32", reason="a job's ignored SIGINT and nohup's SIGHUP are POSIX")
def test_the_signal_tests_pass_in_a_selftest_started_with_the_signals_ignored(tmp_path: Path) -> None:
    """./pyt selftest started as a background job of a script (a POSIX shell starts it with
    SIGINT ignored) or under nohup (SIGHUP ignored): every test inherited SIG_IGN, and the tests
    that send themselves a signal or read what a child inherits failed (4 of them, in every
    project) or skipped. They set the handling they need (conftest.default_signals) and pass."""
    start = (
        "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "signal.signal(signal.SIGHUP, signal.SIG_IGN); os.execv(sys.executable, [sys.executable, *sys.argv[1:]])"
    )
    drop = ("UV", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON")
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("PYTEMPLATE_")}
    r = subprocess.run(
        [sys.executable, "-c", start, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", ".pytemplate/tests/pytest.ini",
         "--rootdir=.", "--basetemp", str(tmp_path / "t"), *(f".pytemplate/tests/{node}" for node in SIGNAL_TESTS)],
        cwd=project.ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, check=False,
    )  # fmt: skip
    assert r.returncode == 0 and " passed" in r.stdout and "skipped" not in r.stdout, r.stdout[-6000:] + r.stderr[-2000:]
