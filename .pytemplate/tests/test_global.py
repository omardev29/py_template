"""Global mode (CLAUDE.md 5.2): the runner of the installed template, outside any project.

`pyt install` copies the template into a folder of the user's and puts the launchers on PATH;
when they find no project they run that copy's .pytemplate/pyt.py with PYTEMPLATE_GLOBAL=1
(project.GLOBAL). Only help, new, doctor, install and uninstall run then; any other command,
internal route and name exits 2 and says what to do; nothing is written into the installed
template (its bytecode cache included); `new` copies it whole and the copy's `__init` runs as a
project; doctor checks the machine only.

The in-process tests set project.GLOBAL; the real runs start the pyt.py of a fake installed
template (this project's files, as the install contract says) with PYTEMPLATE_GLOBAL=1.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_DIR))

from runner import cli, cmd_env, cmd_mode, cmd_nvim, config, e2e, envs, hooks, nvimtest, presets, proc, project, render, shells, ui  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import ROOT  # noqa: E402

IS_WINDOWS = os.name == "nt"
TEMPLATE_REPO = (ROOT / ".pytemplate" / "template-repo").is_file()
NEEDS = "needs a project: run it in a project folder (any subfolder works), or create one: pyt new DIR [--preset P]"
PROJECT_COMMANDS = sorted(set(cli.COMMANDS) - set(cli.GLOBAL_COMMANDS))


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


def fail(*_a: Any, **_kw: Any) -> Any:
    raise AssertionError("must not be called outside a project")


@pytest.fixture(autouse=True)
def _globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts without -v/-q/--dry-run/--no-render and without launcher variables."""
    monkeypatch.setattr(ui, "VERBOSE", False)
    monkeypatch.setattr(ui, "QUIET", False)
    monkeypatch.setattr(ui, "_COLOR", False)
    monkeypatch.setattr(proc, "DRY_RUN", False)
    monkeypatch.setitem(cli._OPTS, "no_render", False)
    for name in ("PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER", "PYTEMPLATE_GLOBAL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def outside(monkeypatch: pytest.MonkeyPatch) -> None:
    """Global mode in process: the installed template's pytemplate.toml is never read and nothing
    renders, unless a test says otherwise."""
    monkeypatch.setattr(project, "GLOBAL", True)
    monkeypatch.setattr(config, "load", fail)
    monkeypatch.setattr(render, "auto", fail)


# --- what makes the mode --------------------------------------------------------------------------


def test_global_mode_comes_from_the_launchers_or_the_install_record(tmp_path: Path) -> None:
    root = tmp_path / "template"
    (root / ".pytemplate").mkdir(parents=True)
    assert project.detect_global(root, {"PYTEMPLATE_GLOBAL": "1"})
    for value in ("", "0", "true", "yes", " 1"):
        assert not project.detect_global(root, {"PYTEMPLATE_GLOBAL": value}), value
    assert not project.detect_global(root, {})
    # The installed template is never a project, even reached without the launchers' variable (a
    # command typed inside its folder, its own launcher run by its path)
    (root / project.INSTALL_RECORD).write_text("{}\n", encoding="utf-8")
    assert project.detect_global(root, {})
    # the contract with `pyt install`, and pyt.py (which decides before the runner is imported)
    assert project.INSTALL_RECORD == ".pytemplate/installed.json"
    entry = (TEMPLATE_DIR / "pyt.py").read_text(encoding="utf-8")
    assert 'os.environ.get("PYTEMPLATE_GLOBAL") == "1" or (_here / "installed.json").is_file()' in entry


@pytest.mark.parametrize(("value", "expected"), [("1", "True"), ("0", "False"), (None, "False")])
def test_the_mode_is_read_when_the_runner_starts(value: str | None, expected: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTEMPLATE_GLOBAL"}
    if value is not None:
        env["PYTEMPLATE_GLOBAL"] = value
    code = "import sys; sys.path.insert(0, sys.argv[1]); from runner import project; print(project.GLOBAL)"
    r = subprocess.run([sys.executable, "-B", "-c", code, str(TEMPLATE_DIR)], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert r.stdout.strip() == expected, r.stderr


def test_no_child_process_inherits_the_global_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mode is this runner's own: the copy `new` makes runs its __init as a project, and no
    tool, launcher probe, e2e or Neovim step may take it on."""
    monkeypatch.setenv("PYTEMPLATE_GLOBAL", "1")
    for env in (
        proc.base_env(),
        presets._git_env(),
        shells.child_env(),
        e2e.scrub_env(os.environ),
        nvimtest.runner_env(os.environ),
        nvimtest.nvim_env(nvimtest.Layout(tmp_path), os.environ),
    ):
        assert not [k for k in env if k.upper() == "PYTEMPLATE_GLOBAL"]


def test_new_never_copies_the_install_record() -> None:
    assert presets._skipped(project.INSTALL_RECORD)
    assert not presets._skipped(".pytemplate/pyt.py") and not presets._skipped(".pytemplate/installed.json.bak")


# --- dispatch --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", PROJECT_COMMANDS)
@pytest.mark.usefixtures("outside")
def test_a_project_command_outside_a_project_says_how_to_get_one(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([name]) == 2
    assert f"error: `pyt {name}` {NEEDS}" in capsys.readouterr().err
    assert cli.main(["-v", "--dry-run", "--no-render", name, "x"]) == 2  # global options still work
    assert f"`pyt {name}` needs a project" in capsys.readouterr().err


@pytest.mark.usefixtures("outside")
def test_internal_routes_tasks_and_typos_outside_a_project(capsys: pytest.CaptureFixture[str]) -> None:
    """`__init` (new's step in the copy) needs a project; any other name is unknown there, the
    template's own [tasks] entries (ci) included: they are the installed template's, not the
    user's, and are never read."""
    assert cli.main(["__init", "script", "--force"]) == 2
    assert f"`pyt __init` {NEEDS}" in capsys.readouterr().err
    for argv in (["ci"], ["ci", "-h"], ["gen", "x"]):
        assert cli.main(argv) == 2
        err = capsys.readouterr().err
        assert f"unknown command: {argv[0]}" in err and "[tasks]" in err and "new | doctor" in err, err
    assert cli.main(["init"]) == 2
    assert "init is no longer a pyt command: create a project with pyt new DIR [--preset P]" in capsys.readouterr().err


@pytest.mark.usefixtures("outside")
def test_help_outside_a_project_lists_what_runs_there(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["help"], [], ["-h"], ["-q", "help"]):
        assert cli.main(argv) == 0
        out = capsys.readouterr().out
        here, rest = out.split("Every other command needs a project", 1)
        assert re.findall(r"(?m)^  (\S+) {2,}", here) == [n for n in cli.GLOBAL_COMMANDS if n in cli.COMMANDS]
        assert set(PROJECT_COMMANDS) <= set(rest.split()), rest
        assert "./pyt" not in out and "pyt new game --preset raylib" in out and "(their help: pyt help COMMAND)" in rest
        assert "Custom tasks" not in out  # the installed template's own


@pytest.mark.usefixtures("outside")
def test_help_of_a_command_outside_a_project_says_whether_it_needs_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["help", "run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("pyt run [BACKEND] [app args...]\n") and f"Needs a project: {NEEDS.split(': ', 1)[1]}" in out
    assert cli.main(["help", "new"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("pyt new DIR [--preset P] [--name NAME]\n") and "Needs a project" not in out
    assert cli.main(["help", "doctor"]) == 0
    assert cli.GLOBAL_SUMMARIES["doctor"] in capsys.readouterr().out
    # -h after a command is its help here too, also after run, test, lock and selftest (no app,
    # pytest or uv of a project to hand it to)
    for argv in (["run", "-h"], ["check", "--help"], ["-h", "test"], ["lock", "--help"], ["selftest", "-h"], ["new", "-h"]):
        assert cli.main(argv) == 0, argv
        out = capsys.readouterr().out
        assert out.startswith(f"pyt {argv[1] if argv[0] == '-h' else argv[0]} ")
        assert ("Needs a project" in out) == (argv[0] != "new"), out
    for name in ("ci", "__init", "gen"):
        assert cli.main(["help", name]) == 2
        assert f"unknown command: {name}" in capsys.readouterr().err
    assert cli.main(["help", "init"]) == 2


@pytest.mark.parametrize(("name", "module"), [("new", cmd_mode), ("doctor", cmd_env)])
def test_a_global_command_runs_with_the_templates_config_and_never_renders(name: str, module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project, "GLOBAL", True)
    cfg = make({})
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: cfg)
    monkeypatch.setattr(render, "auto", fail)
    seen: list[tuple[Config, list[str]]] = []
    monkeypatch.setattr(module, f"cmd_{name}", lambda c, args: seen.append((c, args)) or 0)
    assert cli.main(["-q", name, "x"]) == 0
    assert seen == [(cfg, ["x"])]


def test_install_and_uninstall_run_outside_a_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pyt install` and `pyt uninstall` put the launchers on PATH and take them off: they must
    run outside a project (whatever they do)."""
    assert {"install", "uninstall"} <= set(cli.GLOBAL_COMMANDS)
    monkeypatch.setattr(project, "GLOBAL", True)
    monkeypatch.setattr(config, "load", lambda *_a, **_kw: make({}))
    monkeypatch.setattr(render, "auto", fail)
    calls: list[list[str]] = []
    monkeypatch.setattr(cmd_env, "pt_fake_command", lambda _cfg, args: calls.append(args) or 0, raising=False)
    commands = dict(cli.COMMANDS)
    for name in ("install", "uninstall"):
        commands[name] = cli.Command("cmd_env", "pt_fake_command", f"fake {name}", render=True)
    monkeypatch.setattr(cli, "COMMANDS", commands)
    assert cli.main(["install", "a"]) == 0 and cli.main(["uninstall", "b"]) == 0
    assert calls == [["a"], ["b"]]


def test_the_probe_runs_outside_a_project(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(project, "GLOBAL", True)
    monkeypatch.setattr(config, "load", fail)
    assert cli.main(["__probe", "0", "0", "x"]) == 0
    assert capsys.readouterr().out.startswith("PTPROBE{")


# --- doctor --------------------------------------------------------------------------------------


def _doctor_lines(monkeypatch: pytest.MonkeyPatch) -> list[tuple[bool | None, str, str]]:
    lines: list[tuple[bool | None, str, str]] = []
    monkeypatch.setattr(ui, "check_line", lambda passed, label, hint="": lines.append((passed, label, hint)))
    return lines


@pytest.mark.usefixtures("outside")
def test_doctor_outside_a_project_checks_this_machine_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for target, name in ((cmd_env, "_project"), (render, "apply"), (hooks, "doctor"), (shells, "_check_launchers"), (envs, "tool_env"), (envs, "cpython_env")):
        monkeypatch.setattr(target, name, fail)
    monkeypatch.setattr(cmd_nvim, "find_nvim", lambda: None)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    monkeypatch.setattr(proc, "output", lambda argv, **_kw: f"uv {envs.MIN_UV}")
    monkeypatch.setattr(cmd_env, "_c_compiler", lambda platform, cc="": (False, "cc not found (the CC of the .venv Python: 'cc')"))
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: None)  # no git either
    monkeypatch.setenv("PYTEMPLATE_LAUNCHER", "sh")
    lines = _doctor_lines(monkeypatch)
    assert cmd_env.cmd_doctor(make({}), []) == 0  # what is missing here is a note: uv is there
    labels = [label for _, label, _ in lines]
    assert labels[0].startswith("outside a project: this machine only") and str(project.ROOT) in labels[0]
    assert f"uv: uv {envs.MIN_UV}" in labels and any(label.startswith("runner: Python ") for label in labels)
    notes = {label: passed for passed, label, _ in lines}
    assert notes["git not found: `new` makes no repository, and a project gets no pre-commit hook"] is None
    assert notes["C compiler for mypyc: cc not found (the CC of the .venv Python: 'cc')"] is None
    assert "this run was started by: sh" in labels
    machine = ("outside a project", "uv: ", "runner: ", "git", "C compiler for mypyc: ", "Windows long paths", "this run was started by: ")
    assert not [label for label in labels if not label.startswith(machine)], labels  # no project step


@pytest.mark.usefixtures("outside")
def test_doctor_outside_a_project_fails_only_on_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmd_nvim, "find_nvim", lambda: None)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    monkeypatch.setattr(proc, "output", lambda argv, **_kw: "uv 0.8.0")
    monkeypatch.setattr(cmd_env, "_c_compiler", lambda platform, cc="": (True, "/usr/bin/cc"))
    lines = _doctor_lines(monkeypatch)
    assert cmd_env.cmd_doctor(make({}), []) == 1
    assert [label for passed, label, _ in lines if passed is False] == ["uv: uv 0.8.0"]


@pytest.mark.parametrize("global_mode", [True, False])
def test_doctor_ends_with_the_same_steps_in_both_modes(global_mode: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """The last steps (Neovim, and whatever follows it) run in a project and outside one."""
    monkeypatch.setattr(project, "GLOBAL", global_mode)
    calls: list[str] = []
    monkeypatch.setattr(cmd_env, "_tools", lambda check: calls.append("tools"))
    monkeypatch.setattr(cmd_env, "_machine", lambda check: calls.append("machine"))
    monkeypatch.setattr(cmd_env, "_project", lambda cfg, check: calls.append("project"))
    monkeypatch.setattr(cmd_nvim, "doctor", lambda check: calls.append("neovim"))
    assert cmd_env.cmd_doctor(make({}), []) == 0
    assert calls == ["tools", "machine" if global_mode else "project", "neovim"]


@pytest.mark.parametrize("lazyvim", [True, False])
def test_the_neovim_line_outside_a_project_trusts_no_lazy_lua(lazyvim: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No project: no .lazy.lua to trust (the installed template's own is no user's)."""
    monkeypatch.setattr(project, "GLOBAL", True)
    nv = types.SimpleNamespace(version=(0, 12, 5), version_text="0.12.5", lazyvim_installed=lambda: lazyvim, trust_db=tmp_path / "trust")
    monkeypatch.setattr(cmd_nvim, "find_nvim", lambda: "nvim")
    monkeypatch.setattr(cmd_nvim, "query", lambda exe: nv)
    monkeypatch.setattr(cmd_nvim, "trust_status", fail)
    lines: list[tuple[bool | None, str, str]] = []
    cmd_nvim.doctor(lambda passed, label, hint: lines.append((passed, label, hint)))
    assert lines == [(True if lazyvim else None, f"Neovim 0.12.5, LazyVim {'yes' if lazyvim else 'no'}", "details: pyt nvim doctor in a project")]


# --- new -----------------------------------------------------------------------------------------


def test_new_outside_a_project_copies_every_file_without_asking_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The installed template has no .git and is a clean copy by construction (it may still lie
    in a repository of the user's: a home folder kept in git): git is never asked, and nothing is
    said about untracked or ignored files."""
    snap = tmp_path / "installed"
    (snap / ".pytemplate").mkdir(parents=True)
    for rel in (".pytemplate/pyt.py", ".pytemplate/template-repo", project.INSTALL_RECORD, "pytemplate.toml", "README.md", "src/app/x.py", "src/app/__pycache__/x.pyc"):
        (snap / rel).parent.mkdir(parents=True, exist_ok=True)
        (snap / rel).write_text(rel, encoding="utf-8")
    monkeypatch.setattr(project, "GLOBAL", True)
    monkeypatch.setattr(presets, "ROOT", snap)
    monkeypatch.setattr(presets, "_git_files", fail)
    dest = tmp_path / "demo"
    presets.copy_template(dest)
    copied = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert copied == [".pytemplate/pyt.py", "pytemplate.toml", "src/app/x.py"]
    assert capsys.readouterr().err == ""
    assert presets.copy_scope() == "every file"
    assert presets.source_name() == f"the installed template ({snap})"


# --- real runs of an installed template ------------------------------------------------------------


def _tree(root: Path) -> dict[str, str]:
    """Every file (sha256) and folder under root: a new __pycache__ counts too."""
    return {
        p.relative_to(root).as_posix(): "<dir>" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
    }


def _git(*args: str, cwd: Path) -> None:
    git = shutil.which("git")
    if git is not None:
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        subprocess.run([git, *args], cwd=cwd, env=env, capture_output=True, check=True, timeout=60)


def _installed_template(tmp_path: Path) -> Path:
    """What `pyt install` makes: this project's files (as `new` copies them), plus, from the
    template repository, its marker, README.md and LICENSE, and the install record. It lies in a
    home folder kept in git, which global mode never asks."""
    home = tmp_path / "home"
    home.mkdir()
    _git("init", "--quiet", cwd=home)
    snap = home / ".local" / "share" / "pytemplate" / "template"
    presets.copy_template(snap)
    if TEMPLATE_REPO:
        for rel in (".pytemplate/template-repo", "README.md", "LICENSE"):
            shutil.copy2(ROOT / rel, snap / rel)
    (snap / project.INSTALL_RECORD).write_text('{"from": "test_global"}\n', encoding="utf-8")
    return snap


def _global_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """A child runner started as the launchers start it outside a project: PYTEMPLATE_GLOBAL=1,
    the user's cache folder in tmp_path (the runner's bytecode cache goes there), no bytecode
    setting of the caller's (the redirect is what is tested)."""
    drop = ("VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON")
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("PYTEMPLATE_")}
    cache = str(tmp_path / "cache")
    env.update(PYTEMPLATE_GLOBAL="1", NO_COLOR="1", XDG_CACHE_HOME=cache, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(tmp_path / "no-gitconfig"))
    if IS_WINDOWS:
        env["LOCALAPPDATA"] = cache
    env.update(extra)
    return env


def _run(snap: Path, args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(snap / ".pytemplate" / "pyt.py"), *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False,
    )  # fmt: skip


def test_a_global_run_never_writes_into_the_installed_template(tmp_path: Path) -> None:
    snap = _installed_template(tmp_path)
    away = tmp_path / "away"
    away.mkdir()
    env = _global_env(tmp_path)
    before = _tree(snap)
    runs = [
        (["help"], 0, "Outside a project, pyt runs:"),
        (["help", "sync"], 0, "Needs a project"),
        (["doctor"], 0, "outside a project: this machine only"),
        (["--dry-run", "new", "demo"], 0, f"would copy the installed template ({snap.resolve()}) there (every file;"),
        (["new", str(snap / "inside")], 2, "cannot be inside the installed template"),
        (["check"], 2, f"`pyt check` {NEEDS}"),
        (["ci"], 2, "unknown command: ci"),
        (["run", "-h"], 0, "Needs a project"),
    ]
    for args, code, text in runs:
        r = _run(snap, args, away, env)
        assert r.returncode == code, (args, r.stdout, r.stderr)
        assert text in r.stdout + r.stderr, (args, r.stdout, r.stderr)
        assert _tree(snap) == before, f"pyt {' '.join(args)} wrote into the installed template"
    assert not (away / "demo").exists()
    assert list((tmp_path / "cache" / "pytemplate" / "pycache").rglob("*.pyc")), "the runner's bytecode cache went nowhere"


def test_the_installed_template_is_never_a_project(tmp_path: Path) -> None:
    """Typed inside the installed template's folder, the launchers find its pyt.py as a project's
    (no PYTEMPLATE_GLOBAL): its install record keeps it in global mode all the same."""
    snap = _installed_template(tmp_path)
    env = _global_env(tmp_path)
    del env["PYTEMPLATE_GLOBAL"]
    before = _tree(snap)
    r = _run(snap, ["sync"], snap, env)
    assert r.returncode == 2 and f"`pyt sync` {NEEDS}" in r.stderr, r.stdout + r.stderr
    r = _run(snap, ["help"], snap / ".pytemplate", env)
    assert r.returncode == 0 and "Outside a project, pyt runs:" in r.stdout, r.stdout + r.stderr
    assert _tree(snap) == before


@pytest.mark.skipif(IS_WINDOWS, reason="a #!/bin/sh stand-in for uv")
def test_new_outside_a_project_makes_a_project_of_the_installed_template(tmp_path: Path) -> None:
    """`pyt new DIR` outside a project: DIR is the caller's, the copy is the installed template's
    every file but what `new` leaves out (the install record, the template's marker, its README
    and LICENSE, which go to .pytemplate/), said without a word about git, and the copy's own
    runner runs `__init` as a project (no PYTEMPLATE_GLOBAL). uv is faked: no network."""
    snap = _installed_template(tmp_path)
    record = tmp_path / "uv-call"
    fake = tmp_path / "fake-uv"
    fake.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$PT_FAKE_UV.argv"\nenv > "$PT_FAKE_UV.env"\n', encoding="utf-8")
    fake.chmod(0o755)
    away = tmp_path / "away"
    away.mkdir()
    env = _global_env(tmp_path, UV=str(fake), PT_FAKE_UV=str(record), PYTEMPLATE_CALLER_CWD=str(away), PYTEMPLATE_LAUNCHER="sh")
    before = _tree(snap)
    r = _run(snap, ["new", "demo"], away, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _tree(snap) == before
    dest = away / "demo"
    argv = record.with_suffix(".argv").read_text(encoding="utf-8").splitlines()
    assert argv == ["run", "--quiet", "--script", str(dest / ".pytemplate" / "pyt.py"), "--no-render", "__init", "script", "--name", "demo", "--force"]
    assert not re.search(r"(?m)^PYTEMPLATE_GLOBAL=", record.with_suffix(".env").read_text(encoding="utf-8"))
    for text in ("copying every file", "not a git work tree", "not copied", "git does not track"):
        assert text not in r.stderr, r.stderr
    assert f"cd {dest}" in r.stderr and "./pyt setup" in r.stderr
    source = {rel for rel, digest in before.items() if digest != "<dir>"}
    copied = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file() and ".git" not in p.relative_to(dest).parts}
    manual = {".pytemplate/README.md", ".pytemplate/LICENSE"} if TEMPLATE_REPO else set()
    assert copied == {rel for rel in source if not presets._skipped(rel)} | {"README.md"} | manual
    for left_out in (project.INSTALL_RECORD, ".pytemplate/template-repo", ".pytemplate/deploy.py"):
        assert not (dest / left_out).exists(), left_out
    assert (dest / "README.md").read_text(encoding="utf-8").startswith("# demo\n")
    if TEMPLATE_REPO:
        assert (dest / ".pytemplate" / "README.md").read_bytes() == (snap / "README.md").read_bytes()
        assert (dest / ".pytemplate" / "LICENSE").read_bytes() == (snap / "LICENSE").read_bytes()
