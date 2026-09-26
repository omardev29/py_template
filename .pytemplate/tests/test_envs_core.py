"""Tests for runner/envs.py and runner/cmd_env.py: the uv environment contract (CLAUDE.md section
7), the oldest uv the project supports, clean, the sync/add/remove/lock command lines,
ensure_lock, the exec bits of the launchers, the C compiler checks, and doctor's lines and exit
code.

Fast and hermetic: uv, the interpreters and the other doctor steps are faked. Real processes
only where a test says so: git in a throwaway repository (isolated from the user's
configuration) and this Python for interpreter_info.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_apply, cmd_env, cmd_nvim, config, envs, hooks, project, proc, render, shells  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import ENV_SUFFIX, IS_WINDOWS, PRESETS, ROOT, venv_python  # noqa: E402
from runner.ui import DeployError  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
PYPY = {"backend": {"active": "cpython", "supported": ["cpython", "pypy", "mypyc"]}}


def make(data: dict[str, Any] | None = None) -> Config:
    cfg: Config = config._build(Config, data or {}, "")
    config.validate(cfg)
    return cfg


def done(argv: Sequence[Any], code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([str(a) for a in argv], code, out, err)


class Calls:
    """Fakes proc.run (and find_uv) under envs: records every argv with its keyword arguments."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, version: str = "uv 0.12.19 (x86_64-unknown-linux-gnu)") -> None:
        self.argvs: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.version = version
        monkeypatch.setattr(proc, "run", self._run)
        monkeypatch.setattr(proc, "find_uv", lambda: "uv")
        monkeypatch.setattr(envs, "_uv_ok", set())

    def _run(self, argv: Sequence[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        if args[1:] == ["--version"]:
            return done(args, 0, self.version + "\n")
        self.argvs.append(args)
        self.kwargs.append(kw)
        return done(args)


# --- the environment contract (section 7) ------------------------------------------------------------

POLLUTED = {
    "VIRTUAL_ENV": "/wrong/venv",
    "UV_PROJECT_ENVIRONMENT": "/wrong/env",
    "UV_PYTHON": "/wrong/python",
    "UV_PYTHON_PREFERENCE": "system",
    "PYTHONHOME": "/wrong/home",
    "PYTHONPATH": "/wrong/path",
}


@pytest.mark.parametrize("which", ["cpython", "pypy", "tool"])
def test_env_vars_pin_the_environment_and_the_interpreter_together(which: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """UV_PROJECT_ENVIRONMENT and UV_PYTHON always together (with only one uv silently recreates
    the environment with the wrong interpreter), only-managed, whatever the caller exported."""
    for key, value in POLLUTED.items():
        monkeypatch.setenv(key, value)
    cfg = make(PYPY)
    env = {"cpython": envs.cpython_env, "pypy": envs.pypy_env, "tool": envs.tool_env}[which](cfg)
    e = envs.env_vars(env)
    assert e["UV_PROJECT_ENVIRONMENT"] == str(env.dir) and Path(e["UV_PROJECT_ENVIRONMENT"]).is_absolute()
    assert e["UV_PYTHON"] == (cfg.python.pypy if which == "pypy" else cfg.python.cpython)
    assert e["UV_PYTHON_PREFERENCE"] == "only-managed"
    assert e["PYTHONUTF8"] == "1"
    for key in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
        assert key not in e
    assert envs.env_vars(env, {"FLET_WEB": "1"})["FLET_WEB"] == "1"  # extra variables go on top


@pytest.mark.parametrize("suffix", ["", "-wsl"])
def test_runtime_and_tool_environments(suffix: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envs, "ENV_SUFFIX", suffix)
    cfg = make(PYPY)
    assert envs.runtime_env(cfg, "cpython").dir == ROOT / f".venv{suffix}"
    assert envs.runtime_env(cfg, "mypyc").dir == ROOT / f".venv{suffix}"  # mypyc runs on the .venv CPython
    assert envs.runtime_env(cfg, "pypy").dir == ROOT / f".venv-pypy{suffix}"
    tool = envs.tool_env(cfg)
    assert tool.dir == ROOT / f".venv{suffix}" and tool.key == "cpython" and tool.request == cfg.python.cpython
    assert envs.pypy_env(cfg).request == cfg.python.pypy
    assert envs.cpython_env(cfg).python == venv_python(ROOT / f".venv{suffix}")


def test_uv_run_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envs, "ROOT", tmp_path)
    (tmp_path / ".venv").mkdir()
    calls = Calls(monkeypatch)
    env = envs.cpython_env(make())
    envs.uv_run(env, ["ruff", "--version"])
    envs.uv_run(env, ["x"], groups=["docs", "lint"])
    stage = tmp_path / "stage"  # a work dir with its own pyproject.toml (the flet build stage)
    stage.mkdir()
    envs.uv_run(env, ["x"], cwd=stage)
    envs.uv_run(env, ["x"], cwd=tmp_path)  # the root itself: no --project
    assert calls.argvs == [
        ["uv", "run", "--locked", "ruff", "--version"],
        ["uv", "run", "--locked", "--group", "docs", "--group", "lint", "x"],
        ["uv", "run", "--locked", "--project", str(tmp_path), "x"],
        ["uv", "run", "--locked", "x"],
    ]
    for kw in calls.kwargs:
        assert kw["env"]["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / f".venv{ENV_SUFFIX}")
        assert kw["env"]["UV_PYTHON"] == "3.14"


def test_sync_installs_every_dependency_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`./deploy add --group docs mkdocs` installs mkdocs; an exact `uv sync` of the default
    groups removed it again on the next sync/setup, and a fresh clone never got it."""
    monkeypatch.setattr(envs, "ROOT", tmp_path)
    (tmp_path / ".venv").mkdir()
    calls = Calls(monkeypatch)
    envs.sync(envs.cpython_env(make()))
    assert calls.argvs == [["uv", "sync", "--locked", "--all-groups"]]


def test_a_polluted_uv_environment_still_selects_the_project_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real uv, offline, in this project's .venv: an exported UV_PROJECT_ENVIRONMENT, UV_PYTHON
    or VIRTUAL_ENV (another project, an activated venv) never reaches uv."""
    tool = envs.tool_env(make())
    if not tool.python.is_file():
        pytest.skip("no .venv (./deploy setup)")
    try:
        proc.find_uv()
    except DeployError:
        pytest.skip("uv not found")
    for key, value in {**POLLUTED, "UV_PYTHON": "3.99", "UV_OFFLINE": "1"}.items():
        monkeypatch.setenv(key, value)
    code = "import sys; print(sys.prefix)"
    r = envs.uv(tool, ["run", "--frozen", "python", "-c", code], capture=True, check=False, echo=False)
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip()).resolve() == tool.dir.resolve()
    r = envs.uv(tool, ["lock", "--check"], capture=True, check=False, echo=False)
    assert r.returncode == 0, r.stderr


def test_interpreter_info_of_this_python() -> None:
    info = envs.interpreter_info(sys.executable)
    assert info["impl"] == sys.implementation.name
    assert info["version"] == "%d.%d.%d" % sys.version_info[:3]
    assert info["platform"] == sysconfig.get_platform()  # what setuptools picks the MSVC tools by


# --- the oldest uv ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("uv 0.12.19 (x86_64-unknown-linux-gnu)", (0, 12, 19)),
        ("uv 0.8.17", (0, 8, 17)),
        ("uv 0.10.12 (Homebrew 2026-03-01)", (0, 10, 12)),
        ("uv 1.2.3-dev+abc", (1, 2, 3)),
        ("", None),
        ("uvx 0.1.2", None),
        ("error: something", None),
    ],
)
def test_uv_version(text: str, version: tuple[int, int, int] | None) -> None:
    assert envs.uv_version(text) == version


@pytest.mark.parametrize(
    ("text", "too_old"),
    [("uv 0.8.17", True), ("uv 0.10.11", True), ("uv 0.9.0", True), ("uv 0.10.12", False), ("uv 0.11.0", False), ("uv 1.0.0", False), ("garbage", False)],
)
def test_uv_problem(text: str, too_old: bool) -> None:
    problem = envs.uv_problem(text)
    assert (problem is not None) is too_old
    if problem:
        assert envs.MIN_UV in problem


def test_min_uv_matches_the_pinned_interpreters() -> None:
    """MIN_UV was measured for these pins (uv 0.10.12 is the first that downloads PyPy 3.11.15;
    CPython 3.14 final needs 0.9.0): a new pin needs a new measurement and maybe a new MIN_UV."""
    assert envs.uv_version(f"uv {envs.MIN_UV}") == (0, 10, 12)
    pins = {(make().python.cpython, make().python.pypy)}
    for preset in PRESETS.iterdir():
        toml_file = preset / "files" / "pytemplate.toml"
        if toml_file.is_file():
            python = tomllib.loads(toml_file.read_text(encoding="utf-8"))["python"]
            pins.add((python["cpython"], python["pypy"]))
    assert pins == {("3.14", "pypy@3.11.15")}


def test_an_old_uv_is_refused_before_it_creates_an_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envs, "ROOT", tmp_path)  # no .venv there yet
    calls = Calls(monkeypatch, version="uv 0.8.17")
    env = envs.cpython_env(make())
    with pytest.raises(DeployError) as e:
        envs.sync(env)
    assert e.value.code == 3
    assert "uv 0.8.17 is too old" in str(e.value) and envs.MIN_UV in str(e.value) and "uv self update" in str(e.value)
    assert calls.argvs == []  # uv never got to install a CPython prerelease
    # an existing environment is used as it is: no version question
    env.dir.mkdir()
    envs.sync(env)
    assert calls.argvs == [["uv", "sync", "--locked", "--all-groups"]]
    # a new enough uv is asked once per process; an unreadable version is not refused
    env.dir.rmdir()
    asked: list[str] = []
    real_run = proc.run

    def counting(argv: Sequence[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        if [str(a) for a in argv][1:] == ["--version"]:
            asked.append(str(argv[0]))
        return real_run(argv, **kw)

    monkeypatch.setattr(proc, "run", counting)
    calls.version = "uv 0.12.19"
    envs.sync(env)
    envs.sync(env)
    assert asked == ["uv"]
    monkeypatch.setattr(envs, "_uv_ok", set())
    calls.version = "uv, but not as we know it"
    envs.sync(env)


# --- sync / add / remove / lock ------------------------------------------------------------------------


def names(found: list[envs.PyEnv]) -> list[str]:
    return [e.dir.name.removesuffix(ENV_SUFFIX) if ENV_SUFFIX else e.dir.name for e in found]


def test_sync_targets() -> None:
    cfg, pp = make(), make(PYPY)
    assert names(cmd_env._envs_for(cfg, "all")) == [".venv"]
    assert names(cmd_env._envs_for(pp, "all")) == [".venv", ".venv-pypy"]
    assert names(cmd_env._envs_for(pp, "cpython")) == names(cmd_env._envs_for(pp, "mypyc")) == [".venv"]
    assert names(cmd_env._envs_for(pp, "pypy")) == [".venv-pypy"]
    with pytest.raises(DeployError, match=r"--supports \+pypy") as e:
        cmd_env._envs_for(cfg, "pypy")
    assert e.value.code == 2
    with pytest.raises(DeployError, match="unknown target 'jython'"):
        cmd_env._envs_for(cfg, "jython")


def test_cmd_sync_syncs_the_chosen_environments(monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[str] = []
    monkeypatch.setattr(envs, "sync", lambda env: synced.append(env.key))
    assert cmd_env.cmd_sync(make(PYPY), []) == 0
    assert cmd_env.cmd_sync(make(PYPY), ["pypy"]) == 0
    assert synced == ["cpython", "pypy", "pypy"]


def fake_uv(monkeypatch: pytest.MonkeyPatch, code_for: dict[tuple[str, ...], int] | None = None) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(env: envs.PyEnv, args: Sequence[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in args])
        return done(args, (code_for or {}).get(tuple(calls[-1]), 0))

    monkeypatch.setattr(envs, "uv", run)
    return calls


def test_add_remove_argv(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = fake_uv(monkeypatch)
    cfg, pp = make(), make(PYPY)
    assert cmd_env.cmd_add(cfg, ["x", "--cpython-only"]) == 0
    assert calls[-1] == ["add", "--marker", "implementation_name == 'cpython'", "x"]
    cmd_env.cmd_add(cfg, ["--dev", "x", "y"])
    assert calls[-1] == ["add", "--dev", "x", "y"]
    cmd_env.cmd_add(cfg, ["--group", "docs", "x"])
    assert calls[-1] == ["add", "--group", "docs", "x"]
    cmd_env.cmd_remove(cfg, ["--group", "docs", "x"])
    assert calls[-1] == ["remove", "--group", "docs", "x"]
    cmd_env.cmd_remove(cfg, ["x"])
    assert calls[-1] == ["remove", "x"]
    count = len(calls)
    for bad in (["--dev", "--group", "g", "x"], [], ["--dev"]):
        with pytest.raises(SystemExit) as e:
            cmd_env.cmd_add(cfg, bad)
        assert e.value.code == 2
    with pytest.raises(SystemExit):
        cmd_env.cmd_remove(cfg, ["--cpython-only", "x"])  # an `add` flag
    assert len(calls) == count  # nothing ran for the rejected ones
    capsys.readouterr()
    cmd_env.cmd_add(cfg, ["x"])
    assert "PyPy" not in capsys.readouterr().err
    cmd_env.cmd_add(pp, ["x"])
    assert "--cpython-only" in capsys.readouterr().err
    cmd_env.cmd_add(pp, ["x", "--cpython-only"])
    cmd_env.cmd_remove(pp, ["x"])
    assert "PyPy" not in capsys.readouterr().err


def test_cmd_lock_applies_pyproject_and_forwards_its_arguments(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = fake_uv(monkeypatch)
    written: list[bool] = []
    monkeypatch.setattr(render, "write_pyproject", lambda cfg: written.append(True) or True)
    monkeypatch.setattr(render, "apply", lambda cfg, **kw: ([], []))
    assert cmd_env.cmd_lock(make(), ["--upgrade-package", "rich"]) == 0
    assert written == [True] and calls == [["lock", "--upgrade-package", "rich"]]
    assert "pyproject.toml: updated the parts managed by pytemplate" in capsys.readouterr().err


def test_ensure_lock_dry_run_announces_the_relock(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """--dry-run writes no pyproject.toml, so `uv lock --check` read the old one, passed, and the
    dry run hid the `uv lock` the real setup makes."""
    monkeypatch.setattr(proc, "DRY_RUN", True)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    monkeypatch.setattr(envs, "require_min_uv", lambda uv: None)
    monkeypatch.setattr(render, "write_pyproject", lambda cfg: True)

    def no_process(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("no process may start under --dry-run")

    monkeypatch.setattr(subprocess, "run", no_process)
    cmd_env.ensure_lock(make())
    err = capsys.readouterr().err
    assert "pyproject.toml: would update" in err
    assert "$ uv lock" in err.replace("$ uv lock --check", "")


@pytest.mark.parametrize(("pyproject_changed", "check_rc", "relocks"), [(False, 0, False), (False, 1, True), (True, 0, False), (True, 1, True)])
def test_ensure_lock_relocks_only_when_the_check_fails(
    monkeypatch: pytest.MonkeyPatch, pyproject_changed: bool, check_rc: int, relocks: bool
) -> None:
    monkeypatch.setattr(proc, "DRY_RUN", False)
    monkeypatch.setattr(render, "write_pyproject", lambda cfg: pyproject_changed)
    calls = fake_uv(monkeypatch, {("lock", "--check"): check_rc})
    cmd_env.ensure_lock(make())
    assert calls[0] == ["lock", "--check"]
    assert (["lock"] in calls) is relocks


# --- clean ---------------------------------------------------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    for d in (".build/x", "dist/app", ".venv/bin", ".venv-pypy/bin", ".venv-wsl/bin", ".venv-pypy-wsl/bin", "src/pkg"):
        (root / d).mkdir(parents=True)
    (root / ".venvrc").write_text("keep\n", encoding="utf-8")  # a FILE named like an env
    (root / "src/pkg/__init__.py").write_text("", encoding="utf-8")
    (root / ".build/x/f.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sibling").mkdir()
    monkeypatch.setattr(cmd_env, "ROOT", root)
    monkeypatch.setattr(cmd_env, "BUILD", root / ".build")
    monkeypatch.setattr(cmd_env, "DIST", root / "dist")
    monkeypatch.setattr(cmd_env, "ENV_SUFFIX", "")
    monkeypatch.setattr(proc, "DRY_RUN", False)
    return root


def test_clean_targets(tree: Path) -> None:
    assert cmd_env.cmd_clean(make(), []) == 0
    assert not (tree / ".build").exists() and not (tree / "dist").exists()
    assert (tree / ".venv").is_dir() and (tree / ".venv-pypy").is_dir()
    assert cmd_env.cmd_clean(make(), ["--envs"]) == 0
    assert not (tree / ".venv").exists() and not (tree / ".venv-pypy").exists()
    assert (tree / ".venv-wsl").is_dir() and (tree / ".venv-pypy-wsl").is_dir()  # the WSL side's
    assert (tree / ".venvrc").read_text(encoding="utf-8") == "keep\n"
    assert (tree / "src/pkg/__init__.py").is_file() and (tree.parent / "sibling").is_dir()
    assert cmd_env.cmd_clean(make(), ["--envs"]) == 0  # idempotent: nothing left, still ok


def test_clean_envs_on_the_wsl_side(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In WSL on a Windows checkout (/mnt/...) the Windows .venv and .venv-pypy are not ours."""
    monkeypatch.setattr(cmd_env, "ENV_SUFFIX", "-wsl")
    assert cmd_env.cmd_clean(make(), ["--envs"]) == 0
    assert not (tree / ".venv-wsl").exists() and not (tree / ".venv-pypy-wsl").exists()
    assert (tree / ".venv").is_dir() and (tree / ".venv-pypy").is_dir()


def test_clean_dry_run_removes_nothing(tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert cmd_env.cmd_clean(make(), ["--envs"]) == 0
    for d in (".build", "dist", ".venv", ".venv-pypy"):
        assert (tree / d).is_dir()
    err = capsys.readouterr().err
    assert "would remove .build" in err and "would remove .venv-pypy" in err and "removing" not in err


def test_clean_reports_a_folder_it_could_not_remove(tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Windows: the editor's mypy/ruff server runs from .venv, so part of it cannot be deleted.
    clean said "removing .venv" and exited 0; the half-deleted .venv then broke every command."""
    real = shutil.rmtree

    def locked(path: Any, ignore_errors: bool = False, **kw: Any) -> None:
        if Path(path).name != ".venv":  # .venv keeps a file in use
            real(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(cmd_env.shutil, "rmtree", locked)
    assert cmd_env.cmd_clean(make(), ["--envs"]) == 1
    err = capsys.readouterr().err
    assert "error: could not remove .venv completely" in err and "Close" in err and "./deploy clean --envs" in err
    assert not (tree / ".venv-pypy").exists() and not (tree / "dist").exists()  # the others still go


def test_clean_retries_read_only_contents(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A first rmtree that leaves read-only files behind (Windows) is retried once, writable."""
    real = shutil.rmtree
    attempts: list[str] = []

    def flaky(path: Any, ignore_errors: bool = False, **kw: Any) -> None:
        attempts.append(Path(path).name)
        if attempts.count(Path(path).name) > 1:
            real(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(cmd_env.shutil, "rmtree", flaky)
    assert cmd_env.cmd_clean(make(), []) == 0
    assert attempts == [".build", ".build", "dist", "dist"] and not (tree / ".build").exists()


@pytest.mark.skipif(IS_WINDOWS, reason="symlinks need Developer Mode on Windows")
def test_clean_removes_links_never_their_targets(tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A .venv kept on another disk (a symlink), a symlinked .build, a dangling env link: only the
    links go (rmtree refused links, and ignore_errors hid it: clean said "removing" and kept all)."""
    ext = tmp_path / "ext-venv"
    (ext / "bin").mkdir(parents=True)
    (ext / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    os.symlink(ext, tree / ".venv-ext", target_is_directory=True)
    outer = tmp_path / "ext-build"
    outer.mkdir()
    (outer / "keep").write_text("k", encoding="utf-8")
    shutil.rmtree(tree / ".build")
    os.symlink(outer, tree / ".build", target_is_directory=True)
    os.symlink(tmp_path / "gone", tree / ".venv-gone", target_is_directory=True)
    (tmp_path / "file.txt").write_text("f", encoding="utf-8")
    os.symlink(tmp_path / "file.txt", tree / ".venv-file")  # a link to a file is not an environment
    assert cmd_env.cmd_clean(make(), ["--envs"]) == 0
    assert not os.path.lexists(tree / ".venv-ext") and (ext / "pyvenv.cfg").is_file()
    assert not os.path.lexists(tree / ".build") and (outer / "keep").is_file()
    assert not os.path.lexists(tree / ".venv-gone")
    assert os.path.lexists(tree / ".venv-file") and (tmp_path / "file.txt").is_file()
    assert "removing .venv-ext" in capsys.readouterr().err  # the link's name, not its target


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX modes and symlinks")
def test_make_writable_never_follows_links(tmp_path: Path) -> None:
    root = tmp_path / "t"
    (root / "ro-dir").mkdir(parents=True)
    (root / "ro-dir" / "f").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("o", encoding="utf-8")
    os.symlink(outside, root / "link")
    os.chmod(outside, 0o444)
    os.chmod(root / "ro-dir" / "f", 0o444)
    os.chmod(root / "ro-dir", 0o555)
    cmd_env._make_writable(root)
    assert stat.S_IMODE(os.stat(root / "ro-dir").st_mode) & 0o700 == 0o700
    assert stat.S_IMODE(os.stat(root / "ro-dir" / "f").st_mode) & stat.S_IWRITE
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o444
    os.chmod(outside, 0o644)


# --- the exec bits of the launchers -------------------------------------------------------------------


def isolate_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def launcher_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """deploy, deploy.ps1 and deploy.cmd committed without the exec bit (from Windows) and
    checked out without it: what setup repairs."""
    isolate_git(monkeypatch, tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    for name in ("deploy", "deploy.ps1", "deploy.cmd"):
        (root / name).write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(root / name, 0o644)
    git(root, "add", "--chmod=-x", "deploy", "deploy.ps1", "deploy.cmd")
    monkeypatch.setattr(proc, "ROOT", root)
    monkeypatch.setattr(cmd_env, "ROOT", root)
    monkeypatch.setattr(proc, "DRY_RUN", False)
    return root


def index_mode(root: Path, name: str) -> str:
    return git(root, "ls-files", "-s", name).split(" ", 1)[0]


@needs_git
def test_fix_exec_bit_repairs_the_index_and_the_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = launcher_repo(tmp_path, monkeypatch)
    cmd_env._fix_exec_bit()
    assert index_mode(root, "deploy") == index_mode(root, "deploy.ps1") == "100755"
    assert index_mode(root, "deploy.cmd") == "100644"
    if not IS_WINDOWS:
        assert os.access(root / "deploy", os.X_OK) and os.access(root / "deploy.ps1", os.X_OK)
        assert not os.access(root / "deploy.cmd", os.X_OK)
        # core.filemode=true: the next `git add` records the file's mode, which must be 755 now
        git(root, "add", "deploy", "deploy.ps1")
        assert index_mode(root, "deploy") == index_mode(root, "deploy.ps1") == "100755"
        assert git(root, "diff", "--name-only") == ""
    cmd_env._fix_exec_bit()  # idempotent
    assert index_mode(root, "deploy") == "100755"


@pytest.mark.skipif(IS_WINDOWS, reason="exec bits are POSIX")
def test_fix_exec_bit_outside_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy without git (an unzipped archive that dropped the modes): the files still get +x."""
    isolate_git(monkeypatch, tmp_path)
    root = tmp_path / "plain"
    root.mkdir()
    (root / "deploy").write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(root / "deploy", 0o644)
    monkeypatch.setattr(proc, "ROOT", root)
    monkeypatch.setattr(cmd_env, "ROOT", root)
    monkeypatch.setattr(proc, "DRY_RUN", False)
    cmd_env._fix_exec_bit()
    assert os.access(root / "deploy", os.X_OK)
    assert not (root / "deploy.ps1").exists()  # a missing launcher is not created


@needs_git
def test_fix_exec_bit_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = launcher_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    cmd_env._fix_exec_bit()
    assert index_mode(root, "deploy") == "100644"
    err = capsys.readouterr().err
    assert "$ git update-index --chmod=+x deploy" in err
    if not IS_WINDOWS:
        assert not os.access(root / "deploy", os.X_OK) and "$ chmod +x deploy" in err


# --- the C compiler ------------------------------------------------------------------------------------


def test_c_compiler_rejects_macos_xcode_shims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """/usr/bin/cc, gcc and clang exist on every Mac as xcrun shims, even without the developer
    tools (fresh Mac) or with a broken developer folder (after a macOS upgrade)."""
    monkeypatch.setattr(cmd_env, "IS_WINDOWS", False)
    monkeypatch.setattr(cmd_env, "IS_MACOS", True)
    monkeypatch.delenv("CC", raising=False)
    def which(name: str) -> str | None:  # every compiler name is a /usr/bin shim; "ccache gcc" is no program
        return None if " " in name else name if name.startswith("/") else f"/usr/bin/{name}"

    monkeypatch.setattr(cmd_env.shutil, "which", which)
    calls: list[list[str]] = []
    answer: dict[str, Any] = {"rc": 2, "out": ""}

    def fake_run(argv: Sequence[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in argv])
        if answer.get("missing"):
            raise DeployError("program not found: /usr/bin/xcode-select", 3)
        return done(argv, answer["rc"], answer["out"])

    monkeypatch.setattr(cmd_env.proc, "run", fake_run)
    found, where = cmd_env._c_compiler()  # fresh Mac
    assert found is False and "Xcode" in where and calls[0] == ["/usr/bin/xcode-select", "-p"]
    answer.update(rc=0, out=f"{tmp_path}\n")  # after an upgrade: the folder lost its clang
    found, where = cmd_env._c_compiler()
    assert found is False and "has no clang" in where
    # Xcode.app (the macOS runners' Xcode 26 has no Contents/Developer/usr/bin/xcrun): the
    # default toolchain's clang
    xcode = tmp_path / "Toolchains" / "XcodeDefault.xctoolchain" / "usr" / "bin"
    xcode.mkdir(parents=True)
    (xcode / "clang").write_text("", encoding="utf-8")
    assert cmd_env._c_compiler() == (True, "/usr/bin/cc")
    (xcode / "clang").unlink()
    (tmp_path / "usr" / "bin").mkdir(parents=True)  # the Command Line Tools layout
    (tmp_path / "usr" / "bin" / "clang").write_text("", encoding="utf-8")
    assert cmd_env._c_compiler() == (True, "/usr/bin/cc")  # working tools
    answer.update(missing=True)
    assert cmd_env._c_compiler()[0] is False  # no xcode-select at all
    calls.clear()
    monkeypatch.setenv("CC", "/opt/llvm/bin/clang")  # Homebrew llvm, zig cc: never the shim check
    assert cmd_env._c_compiler() == (True, "/opt/llvm/bin/clang") and calls == []
    monkeypatch.setenv("CC", "ccache gcc")  # not a path: the next candidate
    monkeypatch.setattr(cmd_env, "IS_MACOS", False)  # Linux: /usr/bin/cc is a real compiler
    assert cmd_env._c_compiler() == (True, "/usr/bin/cc") and calls == []


@pytest.mark.parametrize(("platform", "component"), [("win-amd64", "VC.Tools.x86.x64"), ("win-arm64", "VC.Tools.arm64"), ("win32", "VC.Tools.x86.x64")])
def test_msvc_component_follows_the_venv_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, component: str) -> None:
    """The .venv's platform decides (setuptools asks for the tools of the Python that runs mypyc),
    never the host: uv installs x86_64 CPython even on Windows on ARM."""
    monkeypatch.setattr(proc, "vs_installer_dir", lambda: tmp_path)
    monkeypatch.setattr(project, "host_arch", lambda: "aarch64")
    seen: list[list[str]] = []
    out = {"text": "C:\\VS\n"}

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return done(argv, 0, out["text"])

    monkeypatch.setattr(cmd_env.subprocess, "run", fake_run)
    assert cmd_env._msvc(platform) == (True, "C:\\VS")
    argv = seen[0]
    assert argv[argv.index("-requires") + 1] == f"Microsoft.VisualStudio.Component.{component}"
    assert "-prerelease" in argv  # a VS preview counts, as for setuptools
    out["text"] = ""
    found, where = cmd_env._msvc(platform)
    assert found is False and component in where and platform in where
    monkeypatch.setattr(proc, "vs_installer_dir", lambda: None)
    assert cmd_env._msvc(platform) == (False, "no Visual Studio / Build Tools (vswhere.exe not found)")


def test_msvc_component_matches_setuptools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The component doctor asks vswhere for is the one setuptools asks for when mypyc builds."""
    msvc = pytest.importorskip("setuptools._distutils.compilers.C.msvc")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.setattr(proc, "vs_installer_dir", lambda: tmp_path)
    for platform in ("win-amd64", "win-arm64"):
        theirs: list[str] = []
        ours: list[str] = []

        def check_output(argv: list[str], **kw: Any) -> bytes:
            theirs.append(argv[argv.index("-requires") + 1])
            raise OSError("no vswhere here")

        def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            ours.append(argv[argv.index("-requires") + 1])
            return done(argv, 0, "")

        monkeypatch.setattr(msvc, "get_platform", lambda p=platform: p)
        monkeypatch.setattr(msvc.subprocess, "check_output", check_output)
        msvc._find_vc2017()
        monkeypatch.setattr(cmd_env.subprocess, "run", fake_run)
        cmd_env._msvc(platform)
        assert theirs[0].lower() == ours[0].lower()


# --- doctor --------------------------------------------------------------------------------------------


class Doctor:
    """cmd_doctor with every environment, tool and later step faked; records the check lines."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.lines: list[tuple[bool | None, str, str]] = []
        self.reached: list[str] = []
        self.uv_version = "uv 0.12.19 (x86_64-unknown-linux-gnu)"
        self.render_result: tuple[list[str], list[str]] = ([], [])
        self.lock: BaseException | int = 0
        self.info: dict[str, dict[str, object] | BaseException] = {
            "cpython": {"impl": "cpython", "version": "3.14.7", "jit": False, "platform": "linux-x86_64"},
            "pypy": {"impl": "pypy", "version": "3.11.15", "jit": False, "platform": "linux-x86_64"},
        }
        self.compiler_platforms: list[str] = []
        self.cp = envs.PyEnv("cpython", tmp_path / ".venv", "3.14", "only-managed")
        self.pp = envs.PyEnv("pypy", tmp_path / ".venv-pypy", "pypy@3.11.15", "only-managed")
        for env in (self.cp, self.pp):
            env.python.parent.mkdir(parents=True)
            env.python.write_text("", encoding="utf-8")
        monkeypatch.setattr(cmd_env.ui, "check_line", lambda passed, label, hint="": self.lines.append((passed, label, hint)))
        monkeypatch.setattr(cmd_env.proc, "find_uv", lambda: "uv")
        monkeypatch.setattr(cmd_env.proc, "output", lambda argv, **kw: self.uv_version)
        monkeypatch.setattr(cmd_env.envs, "cpython_env", lambda cfg: self.cp)
        monkeypatch.setattr(cmd_env.envs, "pypy_env", lambda cfg: self.pp)
        monkeypatch.setattr(cmd_env.envs, "interpreter_info", self._info)
        monkeypatch.setattr(cmd_env.envs, "uv", self._uv)
        monkeypatch.setattr(cmd_env.render, "apply", lambda cfg, **kw: self.render_result)
        monkeypatch.setattr(cmd_env.render, "pyproject_outdated", lambda cfg: False)
        monkeypatch.setattr(cmd_env, "_c_compiler", self._compiler)
        monkeypatch.setattr(cmd_env, "_long_paths", lambda: True)
        # cmd_apply.doctor reads the real project (src/<pkg>/, pyproject.toml): in a project made
        # with ./deploy new it would compare that project with the Config the test built
        # (test_apply covers it)
        monkeypatch.setattr(cmd_apply, "doctor", lambda cfg, check: self.reached.append("apply"))
        monkeypatch.setattr(shells, "doctor", lambda check: self.reached.append("shells"))
        monkeypatch.setattr(hooks, "doctor", lambda cfg, check: self.reached.append("hooks"))
        monkeypatch.setattr(cmd_nvim, "doctor", lambda check: self.reached.append("nvim"))

    def _info(self, python: str | Path) -> dict[str, object]:
        key = "pypy" if Path(python) == self.pp.python else "cpython"
        value = self.info[key]
        if isinstance(value, BaseException):
            raise value
        return value

    def _uv(self, env: envs.PyEnv, args: Sequence[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(self.lock, BaseException):
            raise self.lock
        err = "error: The lockfile at `uv.lock` needs to be updated, but `--check` was\n       provided.\n" if self.lock else ""
        return done(args, self.lock, "", err)

    def _compiler(self, platform: str = "") -> tuple[bool, str]:
        self.compiler_platforms.append(platform)
        return True, "/usr/bin/cc"

    def problems(self) -> list[tuple[bool | None, str, str]]:
        return [line for line in self.lines if line[0] is False]

    def line(self, text: str) -> tuple[bool | None, str, str]:
        return next(line for line in self.lines if text in line[1])


@pytest.fixture
def doctor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Doctor:
    return Doctor(monkeypatch, tmp_path)


def test_doctor_all_good(doctor: Doctor, capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_env.cmd_doctor(make(PYPY), []) == 0
    assert doctor.problems() == [] and doctor.reached == ["apply", "shells", "hooks", "nvim"]
    assert "all good" in capsys.readouterr().err
    assert doctor.line("uv: uv 0.12.19")[0] is True
    assert doctor.compiler_platforms == ["linux-x86_64"]  # the .venv's platform picks the MSVC tools


def test_doctor_missing_environment(doctor: Doctor, capsys: pytest.CaptureFixture[str]) -> None:
    doctor.pp.python.unlink()
    assert cmd_env.cmd_doctor(make(PYPY), []) == 1
    assert [line[1] for line in doctor.problems()] == [f"environment {project.rel(doctor.pp.dir)} is missing (pypy@3.11.15)"]
    assert "error: 1 problem(s)" in capsys.readouterr().err
    assert cmd_env.cmd_doctor(make(), []) == 0  # PyPy not supported: its environment does not matter


@pytest.mark.parametrize("broken", ["cpython", "pypy"])
@pytest.mark.parametrize("failure", ["exit103", "permission", "garbage"])
def test_doctor_reports_a_broken_interpreter(doctor: Doctor, capsys: pytest.CaptureFixture[str], broken: str, failure: str) -> None:
    """Windows: the base Python of .venv was uninstalled; the venv launcher exits 103 ("No Python
    at ..."). doctor stopped there with exit code 103 and skipped every later check."""
    import json

    doctor.info[broken] = {
        "exit103": proc.CommandFailed(["python", "-I", "-c", "..."], 103),
        "permission": PermissionError(13, "Permission denied"),
        "garbage": json.JSONDecodeError("Expecting value", "No Python at 'C:\\x'", 0),
    }[failure]
    assert cmd_env.cmd_doctor(make(PYPY), []) == 1
    env = doctor.cp if broken == "cpython" else doctor.pp
    assert [line[1] for line in doctor.problems()] == [f"environment {project.rel(env.dir)} is broken (its Python does not start)"]
    assert "./deploy setup" in doctor.problems()[0][2] and "&&" not in doctor.problems()[0][2]  # PowerShell 5.1 has no &&
    assert doctor.reached == ["apply", "shells", "hooks", "nvim"]
    assert "error: 1 problem(s)" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("changed", "edited", "expected"),
    [([], [], 0), ([".mypy.ini"], [], 1), ([], [".ruff.toml"], 1), ([], [".ruff.toml", ".mypy.ini"], 2), ([".mypy.ini"], [".ruff.toml"], 2)],
)
def test_doctor_counts_each_generated_file_problem_once(doctor: Doctor, changed: list[str], edited: list[str], expected: int) -> None:
    doctor.render_result = (changed, edited)
    assert cmd_env.cmd_doctor(make(), []) == (1 if expected else 0)
    assert len(doctor.problems()) == expected
    for path in edited:
        passed, label, hint = doctor.line(f"{path} hand-edited")
        assert passed is False and "pytemplate.toml" in hint and "./deploy render --force" in hint
    if not changed:  # the "update with any command" hint is false for a hand-edited file
        assert all("any command" not in hint for _, _, hint in doctor.problems())
    else:
        assert ".mypy.ini" in doctor.line("generated files up to date")[2]


def test_doctor_flags_an_old_uv(doctor: Doctor, capsys: pytest.CaptureFixture[str]) -> None:
    doctor.uv_version = "uv 0.8.17"
    assert cmd_env.cmd_doctor(make(), []) == 1
    passed, label, hint = doctor.line("uv: uv 0.8.17")
    assert passed is False and envs.MIN_UV in hint and "uv self update" in hint
    doctor.lines.clear()
    doctor.uv_version = "uv (a build that prints something else)"
    assert cmd_env.cmd_doctor(make(), []) == 0
    assert doctor.line("version not recognised")[0] is None


def test_doctor_lock_line_says_what_uv_said(doctor: Doctor) -> None:
    doctor.lock = 1
    assert cmd_env.cmd_doctor(make(), []) == 1
    passed, label, hint = doctor.line("uv.lock up to date")
    assert passed is False and "needs to be updated, but `--check` was provided." in hint and "./deploy lock" in hint
    doctor.lines.clear()
    doctor.lock = DeployError("uv 0.8.17 is too old: this project needs uv 0.10.12 or newer", 3)
    assert cmd_env.cmd_doctor(make(), []) == 1  # reported, not a crash
    assert "too old" in doctor.line("uv lock --check did not run")[2]
    assert doctor.reached[-3:] == ["shells", "hooks", "nvim"]


def test_doctor_passes_the_venv_platform_to_the_compiler_check(doctor: Doctor) -> None:
    info = doctor.info["cpython"]
    assert isinstance(info, dict)
    info["platform"] = "win-arm64"
    cmd_env.cmd_doctor(make(), [])
    doctor.cp.python.unlink()  # no .venv yet: the check uses uv's default (x86_64 CPython)
    cmd_env.cmd_doctor(make(), [])
    assert doctor.compiler_platforms == ["win-arm64", ""]
