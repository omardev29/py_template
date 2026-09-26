"""Tests of the mypyc area: the PyPy 3.11 precheck, lintc, imports, the mypyc stage
(mypyc.py + tools/mypyc_build.py), the compile-module sections of the generated type-checker
configs and the wheel method.

Most tests run in-process with `proc.run` / `envs.uv` replaced by fakes. The few that need the
real tools (mypy, mypyc and a C compiler in .venv) skip cleanly without them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_mode, config, envs, proc, ui  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import ROOT, venv_python  # noqa: E402
from runner.ui import DeployError  # noqa: E402

TOOL_PYTHON = venv_python(ROOT / ".venv")
needs_venv = pytest.mark.skipif(not TOOL_PYTHON.is_file(), reason="needs .venv (./deploy setup)")


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


def _has_mypyc() -> bool:
    try:
        import mypyc.build  # noqa: F401  (selftest runs pytest in .venv, which has mypy)
    except ImportError:
        return False
    return True


def _has_c_compiler() -> bool:
    if os.name == "nt":
        return shutil.which("cl") is not None or proc.vs_installer_dir() is not None
    cc = (sysconfig.get_config_var("CC") or "cc").split()[0]
    return shutil.which(cc) is not None or shutil.which("cc") is not None


needs_compiler = pytest.mark.skipif(
    not (_has_mypyc() and _has_c_compiler()), reason="needs mypyc (run through ./deploy selftest) and a C compiler"
)


@pytest.fixture(autouse=True)
def _quiet_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """No leftovers from other tests: real runs, normal verbosity."""
    monkeypatch.setattr(proc, "DRY_RUN", False)
    monkeypatch.setattr(ui, "VERBOSE", False)
    monkeypatch.setattr(ui, "QUIET", False)


def _done(argv: list[str], code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


# --- 1. the PyPy precheck (cmd_mode._precheck_py311) ------------------------------------------------


class FakeTools:
    """Replaces envs.sync / envs.uv in cmd_mode: records the calls, answers per tool."""

    def __init__(self, *, ruff: int = 0, mypy: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.ruff = ruff
        self.mypy = mypy or {}

    def sync(self, env: envs.PyEnv, **_kw: Any) -> None:
        self.calls.append(["sync", str(env.dir)])

    def uv(self, _env: envs.PyEnv, args: list[Any], **_kw: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in args]
        self.calls.append(argv)
        if "ruff" in argv:
            return _done(argv, self.ruff)
        version = argv[argv.index("--python-version") + 1]
        code, out = self.mypy.get(version, (0, ""))
        return _done(argv, code, out)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeTools:
        monkeypatch.setattr(cmd_mode.envs, "sync", self.sync)
        monkeypatch.setattr(cmd_mode.envs, "uv", self.uv)
        return self


@pytest.fixture
def fake_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tools environment whose interpreter 'exists' (the dry-run precheck needs one)."""
    venv = tmp_path / "venv"
    python = venv_python(venv)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    real = envs.tool_env

    def tool_env(cfg: Config) -> envs.PyEnv:
        env = real(cfg)
        return envs.PyEnv(env.key, venv, env.request, env.preference)

    monkeypatch.setattr(cmd_mode.envs, "tool_env", tool_env)
    return venv


def test_precheck_syncs_first_and_never_reads_the_project_mypy_ini(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = FakeTools().install(monkeypatch)
    cfg = make({})
    cmd_mode._precheck_py311(cfg)
    assert tools.calls[0] == ["sync", str(envs.tool_env(cfg).dir)]  # before any tool runs
    assert len([c for c in tools.calls if c[0] == "sync"]) == 1
    runs = tools.calls[1:]
    assert [c[:3] for c in runs] == [["run", "--locked", "--no-sync"]] * 3  # ruff, mypy 3.11, mypy cpython
    mypy = [c for c in runs if "mypy" in c]
    assert [c[c.index("--python-version") + 1] for c in mypy] == ["3.11", cfg.python.cpython]
    for argv in mypy:
        # an empty --config-file: the root .mypy.ini (ignore_errors = True under "off") is never read
        assert "--config-file=" in argv and "--check-untyped-defs" in argv
        assert not any(a.endswith((".ini", ".toml")) for a in argv)


def test_precheck_dry_run_never_syncs(monkeypatch: pytest.MonkeyPatch, fake_venv: Path) -> None:
    tools = FakeTools().install(monkeypatch)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    cmd_mode._precheck_py311(make({}))
    assert tools.calls and all(c[0] != "sync" for c in tools.calls)
    assert all(c[:3] == ["run", "--locked", "--no-sync"] for c in tools.calls)


def test_precheck_dry_run_without_venv_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tools = FakeTools().install(monkeypatch)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    monkeypatch.setattr(cmd_mode.envs, "tool_env", lambda cfg: envs.PyEnv("cpython", tmp_path / "none", "3.14", "only-managed"))
    cmd_mode._precheck_py311(make({}))
    assert tools.calls == []


@pytest.mark.parametrize("dry", [False, True])
def test_precheck_ruff_that_cannot_run_is_not_a_syntax_error(monkeypatch: pytest.MonkeyPatch, fake_venv: Path, dry: bool) -> None:
    FakeTools(ruff=2).install(monkeypatch)
    monkeypatch.setattr(proc, "DRY_RUN", dry)
    with pytest.raises(DeployError, match=r"could not run ruff .*exit code 2") as err:
        cmd_mode._precheck_py311(make({}))
    assert "syntax" not in str(err.value)


def test_precheck_ruff_findings_are_syntax_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = FakeTools(ruff=1).install(monkeypatch)
    with pytest.raises(DeployError, match="syntax that does not exist in Python 3.11"):
        cmd_mode._precheck_py311(make({}))
    assert not [c for c in tools.calls if "mypy" in c]  # stops at the syntax step


@pytest.mark.parametrize("version", ["3.11", "3.14"])
def test_precheck_mypy_that_aborts_never_passes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], version: str) -> None:
    same = 'tests/__init__.py: error: Duplicate module named "tests"'
    FakeTools(mypy={version: (2, same)}).install(monkeypatch)
    with pytest.raises(DeployError, match=f"mypy could not check the code as Python {version} .exit code 2"):
        cmd_mode._precheck_py311(make({}))
    assert "Duplicate module" in capsys.readouterr().err  # the reason is shown


def test_precheck_reports_only_the_errors_new_at_311(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    both = "src/myapp/a.py:3: error: Incompatible types in assignment"
    new = 'src/myapp/b.py:1: error: Module "typing" has no attribute "override"  [attr-defined]'
    FakeTools(mypy={"3.11": (1, f"{both}\n{new}\n"), "3.14": (1, both + "\n")}).install(monkeypatch)
    with pytest.raises(DeployError, match="APIs that do not exist in Python 3.11"):
        cmd_mode._precheck_py311(make({}))
    err = capsys.readouterr().err
    assert f"error: {new}" in err and "Incompatible types" not in err
    # the same errors at both versions: nothing new, the precheck passes
    FakeTools(mypy={"3.11": (1, both), "3.14": (1, both)}).install(monkeypatch)
    cmd_mode._precheck_py311(make({}))
    assert "ok the code is valid on Python 3.11" in capsys.readouterr().err


NEW_APIS = (
    "import itertools\n"
    "from typing import override\n\n\n"
    "class Base:\n    def f(self) -> int:\n        return 1\n\n\n"
    "class Child(Base):\n    @override\n    def f(self) -> int:\n        return 2\n\n\n"
    "def pairs(xs):\n    return list(itertools.batched(xs, 2))\n"  # unannotated: needs --check-untyped-defs
)


@needs_venv
def test_precheck_real_mypy_catches_new_apis_with_the_off_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The project's own .mypy.ini is the "off" profile (ignore_errors = True): it must not matter
    assert "ignore_errors = True" in (ROOT / ".mypy.ini").read_text(encoding="utf-8")
    code = tmp_path / "code"
    code.mkdir()
    (code / "newapi.py").write_text(NEW_APIS, encoding="utf-8")
    monkeypatch.setattr(cmd_mode, "code_dirs", lambda: [str(code)])
    monkeypatch.setattr(proc, "DRY_RUN", True)  # uv run --no-sync: .venv is never touched
    with pytest.raises(DeployError, match="APIs that do not exist in Python 3.11"):
        cmd_mode._precheck_py311(make({}))
    err = capsys.readouterr().err
    assert 'Module "typing" has no attribute "override"' in err
    assert err.count('has no attribute "batched"') == 1  # inside an unannotated function
    (code / "newapi.py").write_text(
        NEW_APIS.replace("from typing import override", "from typing_extensions import override").replace(
            "itertools.batched(xs, 2)", "zip(xs, xs)"
        ),
        encoding="utf-8",
    )
    cmd_mode._precheck_py311(make({}))
    assert "ok the code is valid on Python 3.11" in capsys.readouterr().err
