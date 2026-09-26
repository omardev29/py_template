"""Exit codes of the selftest harnesses the template's CI trusts: plain `./deploy selftest`
(cli.cmd_selftest), `selftest --shells` (shells.selftest) and `selftest --nvim`
(nvimtest.selftest). A harness that returned 0 after a FAIL would keep every workflow green.

No shell, Neovim, pytest or mypy runs here: the probes, the smoke runs and the uv calls are
faked; the result collection, the tables and the exit logic are the real ones. (`--e2e`:
test_e2e_plan.py.)"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cli, cmd_nvim, config, e2e, envs, nvimtest, shells  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import TEMPLATE  # noqa: E402
from runner.shells import Result, Shell  # noqa: E402
from runner.ui import DeployError  # noqa: E402


PREPARE_BASE = nvimtest.prepare_base  # the real one: the nvim_run fixture fakes it


def make() -> Config:
    cfg: Config = config._build(Config, {}, "")
    config.validate(cfg)
    return cfg


# --- plain ./deploy selftest: pytest, then mypy --strict ----------------------------------------------


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
    assert pt_argv[:5] == ["python", "-m", "pytest", "-q", "-p"] and pt_argv[-3:] == [str(TEMPLATE / "tests"), "-k", "x"]
    assert my_argv[:3] == ["mypy", "--strict", "--no-incremental"] and "--python-version" in my_argv
    assert my_argv[my_argv.index("--python-version") + 1] == "3.11"  # the runner's floor
    assert my_argv[-2:] == [str(TEMPLATE / "runner"), str(TEMPLATE / "deploy.py")]


@pytest.mark.parametrize("suite", ["--shells", "--nvim", "--e2e"])
@pytest.mark.parametrize("code", [0, 1, 3])
def test_a_suite_returns_its_own_exit_code(monkeypatch: pytest.MonkeyPatch, suite: str, code: int) -> None:
    got: list[list[str]] = []

    def fake(cfg: Config, args: list[str]) -> int:
        got.append(args)
        return code

    for module in (shells, nvimtest, e2e):
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


def test_shells_refuse_what_cannot_run(probes: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(DeployError, match="not found here: fish") as e:
        shells.selftest(make(), ["fish"])
    assert e.value.code == 2
    with pytest.raises(DeployError, match="no .pytemplate/deploy.py there") as e:
        shells.selftest(make(), ["--project", str(tmp_path)])
    assert e.value.code == 2
    monkeypatch.setattr(shells, "discover", lambda *a, **k: [])
    with pytest.raises(DeployError, match="no shell found") as e:
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


@pytest.mark.parametrize("missing", ["nvim", "git"])
def test_nvim_missing_tool_skips_or_fails_with_require(nvim_run: dict[str, Any], missing: str) -> None:
    del nvim_run["which"][missing]
    assert nvimtest.selftest(make(), ["script", *nvim_run["args"]]) == 0  # a developer machine: SKIP
    with pytest.raises(DeployError, match=f"{missing} not found") as e:
        nvimtest.selftest(make(), ["script", "--require", *nvim_run["args"]])  # CI: fail loudly
    assert e.value.code == 3 and nvim_run["ran"] == []


def test_nvim_older_than_lazyvims_minimum_skips_or_fails_with_require(nvim_run: dict[str, Any]) -> None:
    major, minor, patch = cmd_nvim.MIN_LAZYVIM
    nvim_run["version"] = (major, minor, patch - 1) if patch else (major, minor - 1, 99)
    assert nvimtest.selftest(make(), ["script", *nvim_run["args"]]) == 0
    with pytest.raises(DeployError, match="older than LazyVim's minimum") as e:
        nvimtest.selftest(make(), ["script", "--require", *nvim_run["args"]])
    assert e.value.code == 3 and nvim_run["ran"] == []
    nvim_run["version"] = cmd_nvim.MIN_LAZYVIM  # the minimum itself runs
    assert nvimtest.selftest(make(), ["script", "--require", *nvim_run["args"]]) == 0 and nvim_run["ran"] == ["script"]


def test_nvim_unknown_preset_is_a_usage_error(nvim_run: dict[str, Any]) -> None:
    with pytest.raises(DeployError, match="unknown preset") as e:
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
    with pytest.raises(DeployError, match="clone the LazyVim starter: exit code 128") as e:
        nvimtest.selftest(make(), ["script", *nvim_run["args"]])
    assert e.value.code == 1 and nvim_run["ran"] == []


def test_nvim_dir_that_is_a_file_is_a_usage_error(nvim_run: dict[str, Any], tmp_path: Path) -> None:
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(DeployError, match="is not a folder") as e:
        nvimtest.selftest(make(), ["script", "--dir", str(afile)])
    assert e.value.code == 2 and nvim_run["ran"] == []
