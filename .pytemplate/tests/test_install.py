"""`./pyt install` and `./pyt uninstall` (runner/cmd_install.py), and the installed template the
launchers run outside any project.

The real runs start the runner of a throwaway clone of the template (a git repository with the
marker, README.md, LICENSE and a template workflow, committed) with this Python, and move every
folder install writes to into tmp_path: HOME, XDG_DATA_HOME, LOCALAPPDATA and uv's tool bin
folder (UV_TOOL_BIN_DIR). Nothing here touches the user's own installed pyt, PATH or data
folder. The all-or-nothing swap is tested in process, with a failure injected at every step.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cli, cmd_install, config, presets, proc  # noqa: E402
from runner.project import IS_WINDOWS, ROOT  # noqa: E402
from runner.ui import PytError  # noqa: E402

UV = os.environ.get("UV") or shutil.which("uv")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
needs_uv = pytest.mark.skipif(not UV, reason="uv not found")
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="modes and sh: Linux/macOS")
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="cmd, Windows PowerShell and pwsh on Windows")
DROP = ("UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "VIRTUAL_ENV", "UV_TOOL_DIR", "XDG_BIN_HOME")
SNAPSHOT_FILES_SKIP = (".git",)


# --- the sandbox -----------------------------------------------------------------------------------


class Box:
    """The folders one test's install writes to, all in tmp_path."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home, self.data, self.local, self.bin = root / "home", root / "data", root / "local", root / "bin"
        self.away = root / "away"  # a folder outside any project
        for folder in (self.home, self.away):
            folder.mkdir(parents=True, exist_ok=True)

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k.upper() not in DROP and not k.upper().startswith(("PYTEMPLATE_", "GIT_"))}
        env.update(
            HOME=str(self.home), XDG_DATA_HOME=str(self.data), LOCALAPPDATA=str(self.local), UV_TOOL_BIN_DIR=str(self.bin),
            GIT_CONFIG_GLOBAL=str(self.root / "no-gitconfig"), GIT_CONFIG_NOSYSTEM="1",
            NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
        )  # fmt: skip
        if UV:
            env["UV"] = UV
        env.update(extra)
        return env

    @property
    def snapshot(self) -> Path:
        """Where install puts the installed template for this environment."""
        found = cmd_install.snapshot_dir(self.env())
        assert found is not None
        return found

    def launchers(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(self.bin.iterdir())} if self.bin.is_dir() else {}


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path / "box")


def _git_env(tmp: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=str(tmp / "no-gitconfig"), GIT_CONFIG_NOSYSTEM="1", LC_ALL="C")
    return env


def git(cwd: Path, *args: str) -> str:
    who = ["-c", "user.name=pt", "-c", "user.email=pt@example.invalid", "-c", "commit.gpgsign=false"]
    r = subprocess.run(["git", *who, *args], cwd=cwd, env=_git_env(cwd), capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout


def _init_repo(root: Path) -> None:
    if subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, env=_git_env(root), capture_output=True, check=False).returncode != 0:
        git(root, "init", "-q")  # git < 2.28
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "template")


def _drop_tasks_named_like_builtins(toml: Path) -> None:
    """A project's [tasks] entry may have the name of a builtin added after the contract
    (install, uninstall: CLAUDE.md 5.2), and ./pyt install runs that task there. The template's
    own pytemplate.toml has none: a clone made from a project's files leaves them out, or its
    `install` ran the project's task (about 20 times, with whatever that task does)."""
    text = toml.read_bytes().decode("utf-8-sig")
    statements = config.scan(text)
    assert statements is not None, f"{toml} is not valid TOML"
    kept, start = [], 0
    for s in statements:  # a statement's text: from the end of the one before to its own end
        if not (len(s.path) > 1 and s.path[0] == "tasks" and s.path[1] in cli.COMMANDS):
            kept.append(text[start : s.end])
        start = s.end
    toml.write_bytes(("".join(kept) + text[start:]).encode("utf-8"))


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clone of the template: what copy_template copies (the tracked files, in any project
    made with ./pyt new too) plus the marker, README.md, LICENSE and a template workflow, in a
    repository of its own."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    dest = tmp_path_factory.mktemp("tpl") / "template"
    dest.mkdir()
    presets.copy_template(dest)
    _drop_tasks_named_like_builtins(dest / "pytemplate.toml")
    (dest / ".pytemplate" / "template-repo").write_text("", encoding="utf-8")
    (dest / "README.md").write_text("# py_template\n\nThe manual.\n", encoding="utf-8")
    (dest / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    workflow = dest / ".github" / "workflows" / "template-selftest.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text("name: template CI\n", encoding="utf-8")
    _init_repo(dest)
    return dest


@pytest.fixture
def clone(template: Path, tmp_path: Path) -> Path:
    """A clone of its own for a test that changes it."""
    dest = tmp_path / "clone"
    shutil.copytree(template, dest, symlinks=True)
    return dest


def pyt(root: Path, box: Box, *args: str, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess[str]:
    """The runner of `root`, as a launcher would start it (in the caller's folder)."""
    return subprocess.run(
        [sys.executable, "-B", str(root / ".pytemplate" / "pyt.py"), *args],
        cwd=cwd or root, env=box.env(**env), stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=300, check=False,
    )  # fmt: skip


def _files(root: Path) -> dict[str, bytes]:
    """Every file (and link, as `-> target`) below root, without .git and caches."""
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            path = Path(dirpath, d)
            if path.is_symlink():
                out[path.relative_to(root).as_posix()] = b"-> " + os.fsencode(os.readlink(path))
            elif d not in (".git", "__pycache__"):
                keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            path = Path(dirpath, name)
            rel = path.relative_to(root).as_posix()
            out[rel] = b"-> " + os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
    return out


def _tree(root: Path) -> dict[str, bytes | str]:
    """Every file (its bytes) and folder below root: an empty folder left behind shows too."""
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): "<dir>" if p.is_dir() and not p.is_symlink() else p.read_bytes() for p in sorted(root.rglob("*"))}


def _tracked(root: Path) -> set[str]:
    return {p for p in git(root, "-c", "core.quotePath=false", "ls-files").splitlines() if p}


def _free_name(clone: Path, stem: str, suffix: str = "") -> str:
    """A name the clone holds nothing at: the clone is the project the suite runs in, its files
    included. Fixed names failed ./pyt selftest in a project that has them: docs/ (mkdir said it
    exists) and a tracked notes.txt (written over, it was no untracked file)."""
    return next(name for i in range(1000) if not os.path.lexists(clone / (name := f"{stem}{i or ''}{suffix}")))


def _record(snapshot: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((snapshot / cmd_install.RECORD).read_text(encoding="utf-8"))
    return data


def _leftovers(box: Box) -> list[str]:
    """What an install may leave behind by mistake: folders next to the installed template and
    staged launchers in the bin folder."""
    parent = box.snapshot.parent
    found = [p.name for p in parent.iterdir() if p.name.startswith((cmd_install.NEW, cmd_install.OLD))] if parent.is_dir() else []
    found += [p.name for p in box.bin.iterdir() if cmd_install.STAGED.match(p.name)] if box.bin.is_dir() else []
    return found


# --- install --------------------------------------------------------------------------------------


@needs_git
@needs_uv
def test_install_copies_the_tracked_template_and_writes_the_launchers(clone: Path, box: Box) -> None:
    notes = _free_name(clone, "notes", ".txt")
    (clone / notes).write_text("untracked\n", encoding="utf-8")
    r = pyt(clone, box, "install")
    assert r.returncode == 0, r.stderr
    assert "pyt is installed" in r.stderr and f"not copied (not tracked by git): {notes}" in r.stderr, r.stderr
    snapshot = box.snapshot
    if not IS_WINDOWS:
        assert snapshot == box.data / "pytemplate" / "template"  # an absolute XDG_DATA_HOME
    else:
        assert snapshot == box.local / "pytemplate" / "template"
    # The tracked files with their working-tree content, the template's own CI left out, the
    # marker, README.md and LICENSE kept (pyt new from there makes what ./pyt new makes here)
    expected = {p for p in _tracked(clone) if not p.startswith(".github/workflows/template-")}
    got = _files(snapshot)
    assert set(got) == expected | {cmd_install.RECORD}, set(got) ^ (expected | {cmd_install.RECORD})
    source = _files(clone)
    assert all(got[p] == source[p] for p in expected)
    for kept in ("README.md", "LICENSE", ".pytemplate/template-repo"):
        assert kept in got
    record = _record(snapshot)
    assert record["schema"] == cmd_install.SCHEMA and record["commit"] == git(clone, "rev-parse", "HEAD").strip()
    assert record["dirty"] is False and os.path.samefile(record["source"], clone) and os.path.samefile(record["bin"], box.bin)
    assert record["launchers"] == list(cmd_install.LAUNCHERS)
    # The launchers, byte for byte, executable; nothing else in the bin folder
    assert box.launchers() == {name: (clone / name).read_bytes() for name in cmd_install.LAUNCHERS}
    if not IS_WINDOWS:
        assert (box.bin / "pyt").stat().st_mode & 0o777 == 0o755
    assert _leftovers(box) == []


@posix_only
@needs_git
@needs_uv
def test_install_keeps_tracked_links_as_links(clone: Path, box: Box) -> None:
    """A symbolic link git tracks is copied as the link (its target text), as `new` copies it:
    never the file it points to, and a dangling one too."""
    docs = _free_name(clone, "docs")
    (clone / docs).mkdir()
    (clone / docs / "manual.md").symlink_to(Path("..") / "README.md")
    (clone / docs / "gone.md").symlink_to("missing.md")
    git(clone, "add", docs)
    git(clone, "commit", "-q", "-m", "links")
    assert pyt(clone, box, "install").returncode == 0
    for name, target in (("manual.md", "../README.md"), ("gone.md", "missing.md")):
        link = box.snapshot / docs / name
        assert link.is_symlink() and os.readlink(link) == target, name


@needs_git
@needs_uv
def test_install_again_swaps_the_whole_copy(clone: Path, box: Box) -> None:
    extra, stray = _free_name(clone, "extra", ".txt"), _free_name(clone, "stray", ".txt")
    (clone / extra).write_text("one\n", encoding="utf-8")
    git(clone, "add", extra)
    git(clone, "commit", "-q", "-m", "extra")
    assert pyt(clone, box, "install").returncode == 0
    first = _record(box.snapshot)
    assert (box.snapshot / extra).is_file()
    (box.snapshot / stray).write_text("written by hand\n", encoding="utf-8")
    git(clone, "rm", "-q", extra)
    git(clone, "commit", "-q", "-m", "no extra")
    (clone / "README.md").write_bytes(b"# py_template\n\nChanged, not committed.\n")
    r = pyt(clone, box, "install")
    assert r.returncode == 0, r.stderr
    assert f"it replaces the one of commit {first['commit'][:7]}" in r.stderr, r.stderr
    got = _files(box.snapshot)
    assert extra not in got and stray not in got  # the old copy went whole
    assert got["README.md"] == b"# py_template\n\nChanged, not committed.\n"
    record = _record(box.snapshot)
    assert record["commit"] == git(clone, "rev-parse", "HEAD").strip() != first["commit"]
    assert record["dirty"] is True
    assert _leftovers(box) == [] and sorted(p.name for p in box.snapshot.parent.iterdir()) == ["template"]


@needs_git
@needs_uv
@pytest.mark.parametrize("kind", ["foreign file", "symlink", "project folder"])
def test_install_never_overwrites_what_it_did_not_write(clone: Path, box: Box, kind: str) -> None:
    box.bin.mkdir(parents=True)
    target = box.bin / "pyt"
    if kind == "foreign file":
        target.write_text("#!/bin/sh\necho another tool\n", encoding="utf-8")
    elif kind == "symlink":
        (box.root / "real").write_bytes((clone / "pyt").read_bytes())  # even one with the marker
        try:
            target.symlink_to(box.root / "real")
        except OSError as e:
            pytest.skip(f"cannot create a symlink here: {e}")
    else:
        (box.bin / ".pytemplate").mkdir()
    before = _files(box.root)
    r = pyt(clone, box, "install")
    assert r.returncode == 2, r.stderr
    if kind == "project folder":
        assert "is a project's folder" in r.stderr, r.stderr
    else:
        why = "pyt: not a pytemplate launcher" if kind == "foreign file" else "pyt: a symbolic link"
        assert "never overwrites a file it did not write" in r.stderr and why in r.stderr, r.stderr
    assert _files(box.root) == before and not box.data.exists() and not box.local.exists()


@needs_git
@needs_uv
def test_install_refuses_a_data_folder_it_did_not_make(clone: Path, box: Box) -> None:
    mine = box.snapshot / "mine.txt"
    mine.parent.mkdir(parents=True)
    mine.write_text("the user's\n", encoding="utf-8")
    before = _files(box.root)
    r = pyt(clone, box, "install")
    assert r.returncode == 2 and "pyt install did not make it" in r.stderr, r.stderr
    assert _files(box.root) == before and not box.bin.exists()


@needs_git
@needs_uv
def test_dry_runs_write_nothing(clone: Path, box: Box) -> None:
    r = pyt(clone, box, "--dry-run", "install")
    assert r.returncode == 0, r.stderr
    assert "(--dry-run) would copy" in r.stderr and str(box.snapshot) in r.stderr, r.stderr
    assert not box.data.exists() and not box.local.exists() and not box.bin.exists()
    assert pyt(clone, box, "install").returncode == 0
    before = _files(box.root)
    for args in (["--dry-run", "uninstall"], ["--dry-run", "install"]):
        r = pyt(clone, box, *args)
        assert r.returncode == 0, r.stderr
        assert _files(box.root) == before, f"{args} wrote files"
    assert f"would remove {box.snapshot} (the installed template)" in pyt(clone, box, "--dry-run", "uninstall").stderr


@needs_git
@needs_uv
def test_install_runs_only_in_a_clone_of_the_template(template: Path, box: Box, tmp_path: Path) -> None:
    project = tmp_path / "project"  # what ./pyt new copies: no template-repo marker
    project.mkdir()
    shutil.copytree(template, project, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "template-repo"))
    r = pyt(project, box, "install")
    assert r.returncode == 2 and "this is a project made with pyt new" in r.stderr and cmd_install.FROM_A_CLONE in r.stderr, r.stderr
    assert pyt(template, box, "install").returncode == 0
    before = _files(box.root)
    r = pyt(box.snapshot, box, "install", cwd=box.away)
    assert r.returncode == 2 and "this is the installed copy of the template" in r.stderr, r.stderr
    assert _files(box.root) == before
    # typed outside any project (a launcher ran the installed template): it says how, and never
    # "this is the installed copy" of a folder the user never named
    r = pyt(box.snapshot, box, "install", cwd=box.away, PYTEMPLATE_GLOBAL="1")
    assert r.returncode == 2 and "outside a project pyt install installs nothing" in r.stderr and cmd_install.FROM_A_CLONE in r.stderr, r.stderr
    assert _files(box.root) == before


@needs_git
@needs_uv
def test_install_into_another_bin_folder_removes_the_earlier_launchers(clone: Path, box: Box) -> None:
    """Installed with UV_TOOL_BIN_DIR=A, then again with B: A's launchers stayed (they ran the new
    copy, uninstall only looked in B, and doctor said there was no pyt). Now the second install
    removes them once its own are in place, and says so."""
    first, second = box.root / "bin-a", box.root / "bin-b"
    assert pyt(clone, box, "install", UV_TOOL_BIN_DIR=str(first)).returncode == 0
    assert sorted(p.name for p in first.iterdir()) == sorted(cmd_install.LAUNCHERS)
    dry = pyt(clone, box, "--dry-run", "install", UV_TOOL_BIN_DIR=str(second))
    assert dry.returncode == 0 and f"and remove {first / 'pyt'}" in dry.stderr and (first / "pyt").is_file(), dry.stderr
    r = pyt(clone, box, "install", UV_TOOL_BIN_DIR=str(second))
    assert r.returncode == 0, r.stderr
    assert f"the earlier install's launchers in {first}" in r.stderr and f"removed {first / 'pyt'}" in r.stderr, r.stderr
    assert list(first.iterdir()) == [] and sorted(p.name for p in second.iterdir()) == sorted(cmd_install.LAUNCHERS)
    assert _record(box.snapshot)["bin"] == str(second)
    r = pyt(clone, box, "uninstall", UV_TOOL_BIN_DIR=str(second))
    assert r.returncode == 0 and list(second.iterdir()) == [] and not box.snapshot.exists(), r.stderr


# --- make_plan in process -----------------------------------------------------------------------------


class Planner:
    """make_plan in process: the installed template and uv's bin folder in tmp_path, the clone's
    git answers faked, every launcher's bytes NEW_LAUNCHER."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp
        self.snapshot = tmp / "data" / "pytemplate" / "template"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        monkeypatch.setattr(cmd_install, "_refuse_elsewhere", lambda: None)
        monkeypatch.setattr(cmd_install, "snapshot_dir", lambda environ=None, windows=IS_WINDOWS: self.snapshot)
        monkeypatch.setattr(cmd_install, "bin_dir", lambda: self.bin)
        monkeypatch.setattr(cmd_install, "_launcher_bytes", lambda name: NEW_LAUNCHER)
        monkeypatch.setattr(cmd_install, "source_state", lambda: ("0123456789", False))
        monkeypatch.setattr(presets, "_tracked_template", lambda: (["a.txt"], "the files git tracks"))
        monkeypatch.setattr(presets, "_git_files", lambda *args: [])
        monkeypatch.delenv(cmd_install.LAUNCHER_FILE, raising=False)

    def installed(self, bin: Path) -> None:
        """An earlier install here, its record naming `bin`."""
        (self.snapshot / ".pytemplate").mkdir(parents=True)
        (self.snapshot / cmd_install.RECORD).write_text(json.dumps({"schema": 1, "bin": str(bin)}), encoding="utf-8")

    def refusal(self) -> str:
        before = _files(self.tmp)
        with pytest.raises(PytError) as e:
            cmd_install.make_plan()
        assert e.value.code == 2 and _files(self.tmp) == before  # before any write
        return str(e.value)


def test_a_clone_git_refuses_to_read_is_named_with_the_way_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A clone another user owns (a shared machine, a root-owned checkout): git refuses it
    (dubious ownership), and install warned that "the copy includes files git does not track"
    (it copies nothing), then said "here it cannot tell which (every file, ignored ones
    included: not a git work tree): use a git clone" of a git clone. Now one refusal says that
    git refuses the repository, with git's own command to let it read the folder."""
    tracked, files = presets._tracked_template, presets._git_files
    p = Planner(tmp_path, monkeypatch)
    monkeypatch.setattr(presets, "_tracked_template", tracked)
    monkeypatch.setattr(presets, "_git_files", files)
    monkeypatch.setattr(shutil, "which", lambda name: "git")
    stderr = (
        "fatal: detected dubious ownership in repository at '/x/clone'\n"
        "To add an exception for this directory, call:\n\n\tgit config --global --add safe.directory /x/clone\n"
    )
    monkeypatch.setattr(proc, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 128, "", stderr))
    message = p.refusal()
    assert "git refuses to list them here (fatal: detected dubious ownership in repository at '/x/clone')" in message
    assert message.endswith("let git read it: git config --global --add safe.directory /x/clone"), message
    assert "not a git work tree" not in message and "use a git clone" not in message
    assert "the copy includes" not in capsys.readouterr().err  # install copies nothing


@pytest.mark.parametrize("clone", [True, False], ids=["a clone", "no repository"])
def test_a_clone_with_no_git_on_path_is_told_to_put_git_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clone: bool) -> None:
    """A git clone typed in where git is not on PATH (a git GUI's own git: GitHub Desktop, Fork):
    install said "here it cannot tell which (every file, ignored ones included: git not found):
    use a git clone", to someone in a git clone. It says that git is not on PATH; a folder that
    is no repository still hears to use a git clone."""
    tracked, files = presets._tracked_template, presets._git_files
    p = Planner(tmp_path, monkeypatch)
    monkeypatch.setattr(presets, "_tracked_template", tracked)
    monkeypatch.setattr(presets, "_git_files", files)
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: None if name == "git" else real_which(name, *args, **kwargs))
    root = tmp_path / "clone"
    root.mkdir()
    if clone:
        (root / ".git").mkdir()
    elif any((d / ".git").exists() for d in root.parents):
        pytest.skip("a folder above tmp_path holds a .git")
    monkeypatch.setattr(cmd_install, "ROOT", root)
    message = p.refusal()
    if clone:
        assert f"git is not on PATH, and {root} is a git clone: install git, or put the git you have on PATH" in message, message
        assert "use a git clone" not in message
    else:
        assert "use a git clone" in message and "not on PATH" not in message, message


def test_every_refusal_comes_in_one_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A data folder install did not make and a file of the bin folder it did not write: the
    first run named one, and the next run the other."""
    p = Planner(tmp_path, monkeypatch)
    (p.snapshot / "mine").mkdir(parents=True)
    (p.bin / "pyt").write_text("#!/bin/sh\necho another tool\n", encoding="utf-8")
    message = p.refusal()
    assert "pyt install writes nothing, for 2 reasons:" in message, message
    assert f"{p.snapshot}: it holds no {cmd_install.RECORD} (pyt install did not make it)" in message
    assert "never overwrites a file it did not write" in message and "pyt: not a pytemplate launcher" in message


@pytest.mark.parametrize("which", ["bin", "data"], ids=["uv's tool bin folder", "the data folder"])
def test_a_folder_install_may_not_write_is_refused_before_the_first_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    """uv's tool bin folder (or the data folder) this user may not write: another user's, a
    read-only one, /usr/local/bin through UV_TOOL_BIN_DIR. install wrote the whole new copy of
    the template, failed on its first staged launcher and named that temporary file, which does
    not exist (`<bin>/.pyt-install-p81hkalh: Permission denied`). It is refused with the other
    refusals, before any write, naming the folder and the way out. The folder is refused as the
    kernel answers for a user who may not write it (os.access; root may write almost anywhere,
    and this test runs as root in the CI image's container too)."""
    p = Planner(tmp_path, monkeypatch)
    locked = p.bin if which == "bin" else tmp_path  # the data folder is made in tmp_path
    real = os.access

    def access(path: Any, mode: int, *args: Any, **kwargs: Any) -> bool:
        if mode & os.W_OK and Path(path) == locked:
            return False
        return real(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", access)
    message = p.refusal()
    if which == "bin":
        assert f"pyt install cannot write into uv's tool bin folder {p.bin} (this user may not write in it)" in message, message
        assert "set UV_TOOL_BIN_DIR to a folder of yours" in message
    else:
        where = p.snapshot.parent
        assert f"pyt install cannot write the installed template into {where} (this user may not create it in {tmp_path})" in message, message
        assert f"set another data folder ({cmd_install.data_home_names()})" in message


def test_a_linked_data_folder_is_refused_as_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A data folder that is a link to a real install was refused because it "holds no"
    installed.json, which it did hold: it is refused as the link it is."""
    p = Planner(tmp_path, monkeypatch)
    real = tmp_path / "elsewhere"
    (real / ".pytemplate").mkdir(parents=True)
    (real / cmd_install.RECORD).write_text("{}\n", encoding="utf-8")
    p.snapshot.parent.mkdir(parents=True)
    try:
        p.snapshot.symlink_to(real, target_is_directory=True)
    except OSError as e:
        pytest.skip(f"cannot create a symlink here: {e}")
    message = p.refusal()
    assert f"{p.snapshot}: it is a symbolic link to " in message and "never makes one, nor writes through one" in message, message
    assert "holds no" not in message


def test_the_earlier_launchers_are_only_marked_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What install removes from the bin folder of the earlier install: launchers with the MARKER,
    never another file there nor a link (even to a launcher)."""
    p = Planner(tmp_path, monkeypatch)
    earlier = tmp_path / "earlier-bin"
    earlier.mkdir()
    p.installed(earlier)
    (earlier / "pyt").write_bytes(OLD_LAUNCHER)
    (earlier / "pyt.cmd").write_text("@echo off\r\nrem another tool\r\n", encoding="utf-8")
    linked = True
    try:
        (earlier / "pyt.ps1").symlink_to(earlier / "pyt")
    except OSError:
        linked = False
    assert cmd_install.make_plan().earlier == [earlier / "pyt"]
    monkeypatch.setattr(cmd_install, "bin_dir", lambda: earlier)  # the same folder: nothing to remove
    (earlier / "pyt.cmd").unlink()
    if linked:
        (earlier / "pyt.ps1").unlink()
    assert cmd_install.make_plan().earlier == []


@pytest.mark.parametrize(
    ("pathext", "refused"),
    [(None, ["pyt.exe", "pyt.bat"]), (".COM;.EXE;.BAT;.CMD;.PY", ["pyt.exe", "pyt.bat"]), (".CMD;.EXE", [])],
    ids=["default PATHEXT", "PATHEXT with .PY", ".CMD first"],
)
def test_on_windows_a_pyt_that_runs_before_pyt_cmd_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pathext: str | None, refused: list[str]
) -> None:
    """cmd, xonsh, nushell and Python's subprocess take the first pyt<ext> of a folder in PATHEXT
    order, and .COM, .EXE and .BAT come before .CMD: another tool's pyt.exe in uv's bin folder
    (`uv tool install python-taint`) ran instead of the installed pyt.cmd, and nothing said so."""
    monkeypatch.setattr(cmd_install, "IS_WINDOWS", True)
    if pathext is None:
        monkeypatch.delenv("PATHEXT", raising=False)
    else:
        monkeypatch.setenv("PATHEXT", pathext)
    p = Planner(tmp_path, monkeypatch)
    for name in ("pyt.exe", "pyt.bat", "pyt.py"):
        (p.bin / name).write_bytes(b"MZ another tool\n")
    if not refused:
        assert cmd_install.make_plan().bin == p.bin
        return
    message = p.refusal()
    assert f"{', '.join(refused)} in {p.bin} would run instead of pyt.cmd" in message and "PATHEXT" in message, message
    assert "uv tool uninstall" in message and "pyt.py" not in message
    d = Doctor(tmp_path / "doctor", monkeypatch)
    for name in refused:
        (d.bin / name).write_bytes(b"MZ another tool\n")
    d.run()
    assert [n for n in d.notes() if "would run instead of pyt.cmd" in n] == [
        f"{name} in {d.bin} would run instead of pyt.cmd: cmd, xonsh, nushell and Python start the first pyt<ext> of a "
        f"folder in PATHEXT order, and it comes before .cmd | {cmd_install.SHADOW_FIX}"
        for name in refused
    ], d.lines


def test_on_windows_install_and_doctor_note_a_powershell_that_blocks_pyt_ps1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Windows PowerShell 5.1 is Restricted by default on client Windows: a bare `pyt` there runs
    the installed pyt.ps1 (PowerShell takes it before pyt.cmd), which does not start, and install
    gave no hint. Install and doctor's `pyt install` step now note each blocking policy."""
    monkeypatch.setattr(cmd_install, "IS_WINDOWS", True)
    monkeypatch.setattr(cmd_install, "LAUNCHERS", cmd_install.NAMES)
    policies = [("Windows PowerShell 5.1", "Desktop", "Restricted"), ("PowerShell 7", "Core", "RemoteSigned")]
    folder = tmp_path / "bin"
    folder.mkdir()
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setattr(cmd_install.shells, "registry_path_dirs", lambda env=None: [], raising=False)
    monkeypatch.setattr(cmd_install.shells, "_ps_policies", lambda: policies)
    note = ("Windows PowerShell 5.1: ExecutionPolicy = Restricted: a bare `pyt` there runs pyt.ps1, which this policy does not let run",
            "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned, or type pyt.cmd in that PowerShell")  # fmt: skip
    assert cmd_install.policy_notes() == [note]
    cmd_install._say_notes(folder)
    assert f"warning: {note[0]}: {note[1]}" in capsys.readouterr().err
    d = Doctor(tmp_path / "doctor", monkeypatch)
    monkeypatch.setattr(cmd_install.shells, "_ps_policies", lambda: policies)  # Doctor stubs it out
    d.install()
    d.run()
    assert f"{note[0]} | {note[1]}" in d.notes(), d.lines
    monkeypatch.setattr(cmd_install.shells, "_ps_policies", lambda: [("Windows PowerShell 5.1", "Desktop", "RemoteSigned")])
    assert cmd_install.policy_notes() == []


# --- uninstall ----------------------------------------------------------------------------------


@needs_git
@needs_uv
def test_uninstall_removes_only_what_install_wrote(template: Path, box: Box) -> None:
    assert pyt(template, box, "install").returncode == 0
    snapshot = box.snapshot
    (box.bin / "pyt").write_bytes(b"#!/bin/sh\necho another tool now\n")  # no longer ours
    (snapshot.parent / "notes").mkdir()  # something else in <data>/pytemplate
    r = pyt(template, box, "uninstall")
    assert r.returncode == 0, r.stderr
    assert f"removed {snapshot} (the installed template)" in r.stderr, r.stderr
    assert "pyt: not a pytemplate launcher" in r.stderr, r.stderr  # left, with the reason
    assert not snapshot.exists() and (snapshot.parent / "notes").is_dir()
    assert box.launchers() == {"pyt": b"#!/bin/sh\necho another tool now\n"}
    r = pyt(template, box, "uninstall")
    assert r.returncode == 0 and "pyt is not installed: nothing to remove" in r.stderr, r.stderr


@needs_git
@needs_uv
@pytest.mark.parametrize("where", ["a project", "the installed template"])
def test_uninstall_runs_from_any_project_and_from_the_installed_template(template: Path, box: Box, tmp_path: Path, where: str) -> None:
    assert pyt(template, box, "install").returncode == 0
    if where == "a project":
        runner = tmp_path / "project"
        shutil.copytree(template, runner, ignore=shutil.ignore_patterns(".git", "template-repo"))
    else:
        runner = box.snapshot  # it deletes the files it runs from
    r = pyt(runner, box, "uninstall", cwd=box.away)
    assert r.returncode == 0, r.stderr
    assert "pyt is uninstalled" in r.stderr, r.stderr
    assert not box.snapshot.parent.exists() and box.launchers() == {}


@needs_git
@needs_uv
def test_uninstall_leaves_a_folder_install_did_not_make(template: Path, box: Box) -> None:
    mine = box.snapshot / "mine.txt"
    mine.parent.mkdir(parents=True)
    mine.write_text("the user's\n", encoding="utf-8")
    r = pyt(template, box, "uninstall")
    assert r.returncode == 0, r.stderr
    assert f"left {box.snapshot}: it holds no {cmd_install.RECORD}" in r.stderr and "nothing to remove" in r.stderr, r.stderr
    assert mine.is_file()


@needs_git
@needs_uv
def test_leftovers_of_an_interrupted_install_are_cleaned_up(template: Path, box: Box) -> None:
    """A killed install leaves its new copy (.template-new-*), an old one moved aside
    (.template-old-*) or a staged launcher: the next install, and uninstall, delete them; a file
    that only looks like a staged launcher stays."""
    parent = box.snapshot.parent
    for name in (cmd_install.NEW + "x1", cmd_install.OLD + "x2"):
        (parent / name / "sub").mkdir(parents=True)
    box.bin.mkdir(parents=True)
    (box.bin / ".pyt-install-abc").write_bytes((template / "pyt").read_bytes())
    (box.bin / ".pyt-install-xyz").write_bytes(b"not a launcher\n")
    assert pyt(template, box, "install").returncode == 0
    assert _leftovers(box) == [".pyt-install-xyz"]
    (parent / (cmd_install.NEW + "x3")).mkdir()
    assert pyt(template, box, "uninstall").returncode == 0
    assert not parent.exists() and box.launchers() == {".pyt-install-xyz": b"not a launcher\n"}


NO_CFG: Any = None  # install and uninstall read nothing of the Config


def test_dry_runs_name_the_leftovers_the_real_runs_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`--dry-run uninstall` said "pyt is not installed: nothing to remove" where the real run then
    removed what an unfinished install left (and said "pyt is uninstalled"), and `--dry-run
    install` never named the leftovers install deletes first: each dry run names them now."""
    p = Planner(tmp_path, monkeypatch)
    old, new = p.snapshot.parent / (cmd_install.OLD + "deadbeef"), p.snapshot.parent / (cmd_install.NEW + "cafe")
    (old / "sub").mkdir(parents=True)
    new.mkdir()
    staged = p.bin / ".pyt-install-abc"
    staged.write_bytes(NEW_LAUNCHER)
    (p.bin / ".pyt-install-xyz").write_bytes(b"not a launcher\n")  # only looks like one: never removed
    before = _files(tmp_path)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert cmd_install.cmd_install(NO_CFG, []) == 0
    err = capsys.readouterr().err
    for path in (old, new, staged):
        assert f"would remove {path} (left by an unfinished install or uninstall)" in err, err
    assert ".pyt-install-xyz" not in err
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0
    err = capsys.readouterr().err
    for path in (old, new, staged):
        assert f"would remove {path} (left by an unfinished install or uninstall)" in err, err
    assert "nothing to remove" not in err and ".pyt-install-xyz" not in err
    assert _files(tmp_path) == before and old.is_dir() and new.is_dir()
    monkeypatch.setattr(proc, "DRY_RUN", False)
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0
    err = capsys.readouterr().err
    assert f"removed {old} (left by an unfinished install or uninstall)" in err, err
    assert "pyt is not installed: removed what an unfinished install or uninstall left" in err and "pyt is uninstalled" not in err
    assert not p.snapshot.parent.exists() and [f.name for f in p.bin.iterdir()] == [".pyt-install-xyz"]


class Installed:
    """An installed template (its record naming the bin folder; its runner: the ENTRY, a module
    and pytemplate.toml) and a launcher in tmp_path, for uninstall in process; a file named
    in-use.txt cannot be deleted while `stuck` is True (the one at the top, or `in_use`)."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch, in_use: str = "in-use.txt") -> None:
        self.snapshot = tmp / "data" / "pytemplate" / "template"
        self.bin = tmp / "bin"
        (self.snapshot / ".pytemplate" / "runner").mkdir(parents=True)
        (self.snapshot / "src").mkdir()
        for rel in (cmd_install.ENTRY, ".pytemplate/runner/cli.py", "pytemplate.toml", "src/a.py", in_use, "z.txt"):
            (self.snapshot / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.snapshot / rel).write_bytes(b"x\n")
        (self.snapshot / cmd_install.RECORD).write_text(json.dumps({"schema": 1, "bin": str(self.bin)}), encoding="utf-8")
        self.bin.mkdir()
        (self.bin / "pyt").write_bytes(NEW_LAUNCHER)
        monkeypatch.setattr(cmd_install, "snapshot_dir", lambda environ=None, windows=IS_WINDOWS: self.snapshot)
        monkeypatch.setattr(cmd_install, "bin_dir", lambda: self.bin)
        monkeypatch.delenv(cmd_install.LAUNCHER_FILE, raising=False)
        self.stuck = True
        real = os.unlink

        def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
            if self.stuck and os.path.basename(os.fsdecode(path)) == "in-use.txt":
                raise PermissionError(errno.EACCES, "Permission denied", os.fsdecode(path))
            real(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", unlink)
        monkeypatch.setattr(os, "remove", unlink)


@pytest.mark.parametrize("movable", [True, False], ids=["moved aside first", "deleted in place"])
def test_a_failed_uninstall_can_be_finished(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, movable: bool) -> None:
    """A file of the installed template that cannot be deleted (in use on Windows, chattr +i):
    rmtree deleted the record first and left the folder, which the next uninstall then called
    the user's own ("holds no installed.json") and install refused, although both said to run
    uninstall again. Now it moves aside first (a leftover the next uninstall or install deletes),
    or, where it cannot move (Windows: a terminal's folder inside it), goes in place with its
    record last: either way the next uninstall finishes the job."""
    inst = Installed(tmp_path, monkeypatch)
    if not movable:
        real = cmd_install._rename

        def rename(src: Path, dst: Path) -> None:
            if src == inst.snapshot:
                raise PermissionError(errno.EACCES, "Permission denied", str(src))
            real(src, dst)

        monkeypatch.setattr(cmd_install, "_rename", rename)
    with pytest.raises(PytError, match="could not remove") as e:
        cmd_install.cmd_uninstall(NO_CFG, [])
    # The launcher is gone: `pyt uninstall` would find no pyt to run
    assert e.value.code == 1 and "then run ./pyt uninstall in a project or in a clone of the template" in str(e.value)
    assert not (inst.bin / "pyt").exists()
    if movable:
        left = list(inst.snapshot.parent.iterdir())
        assert len(left) == 1 and left[0].name.startswith(cmd_install.OLD), left
        assert sorted(p.name for p in left[0].iterdir()) == ["in-use.txt"] and f"{left[0]}: in use" in str(e.value)
        assert not inst.snapshot.exists()  # install is free to write a new one there
    else:
        assert cmd_install.read_record(inst.snapshot) is not None and cmd_install.not_an_install(inst.snapshot) == ""
        runner = [".pytemplate", cmd_install.RECORD, cmd_install.ENTRY, ".pytemplate/runner", ".pytemplate/runner/cli.py", "pytemplate.toml"]
        assert sorted(p.relative_to(inst.snapshot).as_posix() for p in inst.snapshot.rglob("*")) == sorted([*runner, "in-use.txt"])
    inst.stuck = False  # the program that held the file has ended
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0
    assert not inst.snapshot.parent.exists()


def test_a_folder_that_cannot_go_keeps_the_record_of_the_installed_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: `pyt uninstall` typed in the installed template's own folder, the runner's current
    folder, which can neither move nor be deleted while every file in it can. The rounds deleted
    all but the record, then rmtree took the record and failed on the folder itself: an empty
    folder was left, which the next uninstall called the user's own ("nothing to remove") and
    install refused. The record goes back where the folder stays, and the next uninstall
    finishes. Simulated: the rename and the rmdir of that folder fail, nothing else."""
    inst = Installed(tmp_path, monkeypatch)
    inst.stuck = False
    held = [True]
    real_rename, real_rmdir = cmd_install._rename, os.rmdir

    def rename(src: Path, dst: Path) -> None:
        if held[0] and src == inst.snapshot:
            raise PermissionError(errno.EACCES, "Permission denied", str(src))
        real_rename(src, dst)

    def rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
        # rmtree removes the top folder by its full path (dir_fd None), the others by name
        if held[0] and kwargs.get("dir_fd") is None and os.path.abspath(os.fsdecode(path)) == str(inst.snapshot):
            raise PermissionError(errno.EACCES, "Permission denied", os.fsdecode(path))
        real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(cmd_install, "_rename", rename)
    monkeypatch.setattr(os, "rmdir", rmdir)
    with pytest.raises(PytError, match="could not remove") as e:
        cmd_install.cmd_uninstall(NO_CFG, [])
    assert f"{inst.snapshot}: in use or not writable" in str(e.value)
    assert str(e.value).endswith("then run ./pyt uninstall in a project or in a clone of the template")
    left = sorted(p.relative_to(inst.snapshot).as_posix() for p in inst.snapshot.rglob("*"))
    assert left == [".pytemplate", cmd_install.RECORD]
    assert cmd_install.not_an_install(inst.snapshot) == ""  # still pyt install's: install may replace it
    held[0] = False  # the terminal left the folder
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0
    assert not inst.snapshot.parent.exists()


def test_uninstall_leaves_a_linked_data_folder_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A data folder that is a link to an install elsewhere: uninstall neither deletes through the
    link nor removes it, and says why (install refuses it for the same reason)."""
    inst = Installed(tmp_path, monkeypatch)
    inst.stuck = False
    real = tmp_path / "elsewhere"
    inst.snapshot.rename(real)
    try:
        inst.snapshot.symlink_to(real, target_is_directory=True)
    except OSError as e:
        pytest.skip(f"cannot create a symlink here: {e}")
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0
    assert f"left {inst.snapshot}: it is a symbolic link to " in capsys.readouterr().err
    assert inst.snapshot.is_symlink() and (real / cmd_install.RECORD).is_file()


# --- the installed template in use ----------------------------------------------------------------


_COPY = "import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\nfrom runner import presets\npresets.copy_template(Path(sys.argv[2]))\n"


def _copy_with(root: Path, dest: Path, box: Box) -> dict[str, bytes]:
    """What `new` copies from `root` (presets.copy_template run by that runner)."""
    dest.mkdir()
    r = subprocess.run([sys.executable, "-B", "-c", _COPY, str(root / ".pytemplate"), str(dest)], env=box.env(), capture_output=True, text=True, timeout=300, check=False)
    assert r.returncode == 0, r.stderr
    return _files(dest)


@needs_git
@needs_uv
def test_the_installed_template_copies_what_the_clone_copies(template: Path, box: Box) -> None:
    """`pyt new` from the installed template makes the project `./pyt new` makes in the clone:
    the same files (its own files, never those of a git repository around the data folder: a
    dotfiles repository in HOME)."""
    assert pyt(template, box, "install").returncode == 0
    from_clone = _copy_with(template, box.root / "a", box)
    from_installed = _copy_with(box.snapshot, box.root / "b", box)
    assert from_installed == from_clone
    assert cmd_install.RECORD not in from_installed and ".pytemplate/template-repo" not in from_installed
    # A repository around the data folder that tracks one file of the installed template: git
    # there names only that file, and the copy must not follow it
    (box.root / "dotfile").write_text("x\n", encoding="utf-8")
    one = (box.snapshot / ".pytemplate" / "pyt.py").relative_to(box.root).as_posix()
    if subprocess.run(["git", "init", "-q"], cwd=box.root, env=_git_env(box.root), capture_output=True, check=False).returncode != 0:
        pytest.skip("git init failed")
    git(box.root, "add", "dotfile", one)
    git(box.root, "commit", "-q", "-m", "dotfiles")
    assert _copy_with(box.snapshot, box.root / "c", box) == from_clone


@needs_git
@needs_uv
def test_the_installed_template_knows_how_old_it_is(clone: Path, box: Box) -> None:
    """doctor in the clone says when the installed template is older than the clone's HEAD."""
    assert pyt(clone, box, "install").returncode == 0
    code = "import sys\nsys.path.insert(0, sys.argv[1])\nfrom runner import cmd_install as c\nprint(repr(c.age(c.read_record(c.snapshot_dir()))))\n"

    def age() -> str:
        r = subprocess.run([sys.executable, "-B", "-c", code, str(clone / ".pytemplate")], env=box.env(), cwd=clone, capture_output=True, text=True, timeout=120, check=False)
        assert r.returncode == 0, r.stderr
        return str(r.stdout.strip())

    assert age() == "''"
    extra = _free_name(clone, "extra", ".txt")
    (clone / extra).write_text("x\n", encoding="utf-8")
    git(clone, "add", extra)
    git(clone, "commit", "-q", "-m", "newer")
    assert "is older than this clone" in age()


@posix_only
@needs_git
@needs_uv
def test_the_installed_launcher_runs_the_installed_template_outside_projects(clone: Path, box: Box) -> None:
    """The installer and the launcher agree on where the installed template is: `pyt` of the bin
    folder runs it in global mode outside any project, and a project's own runner inside one."""
    assert pyt(clone, box, "install").returncode == 0
    env = box.env(**_uv_dirs())
    for cwd, root, mode in ((box.away, box.snapshot, "1"), (clone / "src" if (clone / "src").is_dir() else clone, clone, "")):
        r = subprocess.run(["/bin/sh", str(box.bin / "pyt"), "__probe", "5", "0", "a b"], cwd=cwd, env=env, capture_output=True, text=True, timeout=180, check=False)
        probe = _probe(r)
        assert r.returncode == 5 and probe["argv"] == ["a b"], (r.stdout, r.stderr)
        assert Path(str(probe["root"])) == root.resolve() and probe["global"] == mode, probe


def _probe(r: subprocess.CompletedProcess[str]) -> dict[str, object]:
    found = [json.loads(line[len("PTPROBE") :]) for line in r.stdout.splitlines() if line.startswith("PTPROBE")]
    assert found, (r.returncode, r.stdout, r.stderr)
    probe: dict[str, object] = found[0]
    return probe


def _uv_dirs() -> dict[str, str]:
    """uv's cache and Python folders as this environment resolves them: with HOME,
    XDG_DATA_HOME or LOCALAPPDATA moved, uv would start from empty ones (and download CPython)."""
    assert UV
    out: dict[str, str] = {}
    env = {k: v for k, v in os.environ.items() if k.upper() not in DROP}
    for key, args in (("UV_CACHE_DIR", ["cache", "dir"]), ("UV_PYTHON_INSTALL_DIR", ["python", "dir"])):
        r = subprocess.run([UV, *args], env=env, capture_output=True, text=True, timeout=60, check=False)
        lines = r.stdout.strip().splitlines()
        if r.returncode != 0 or not lines:
            pytest.skip(f"uv {' '.join(args)} failed: {r.stderr.strip()}")
        out[key] = lines[-1]
    return out


@windows_only
@needs_git
@needs_uv
def test_the_installed_pyt_runs_from_cmd_powershell_and_pwsh(clone: Path, box: Box) -> None:
    """On Windows install writes pyt.cmd, pyt.ps1 and the sh launcher `pyt`: typed as a bare
    `pyt`, cmd runs pyt.cmd (PATHEXT) and PowerShell 5.1 and 7 run pyt.ps1 (their command
    discovery takes a .ps1 before the PATHEXT files and the extensionless file of the same
    folder). Each runs the installed template outside any project. Then `pyt uninstall` through
    cmd deletes the pyt.cmd cmd is running: no "The batch file cannot be found."."""
    assert pyt(clone, box, "install").returncode == 0
    env = box.env(**_uv_dirs())
    path_key = next(k for k in env if k.upper() == "PATH")
    env[path_key] = str(box.bin) + os.pathsep + env[path_key]
    comspec = os.environ.get("ComSpec", "cmd.exe")
    ran = 0
    shells: list[tuple[str, list[str] | str, str]] = [("cmd", f'"{comspec}" /d /s /c "pyt __probe 5 0 x"', "cmd")]
    for name, prefix in (("powershell", "ps1:Desktop:"), ("pwsh", "ps1:Core:")):
        exe = shutil.which(name)
        if exe:
            shells.append((name, [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "pyt __probe 5 0 x; exit $LASTEXITCODE"], prefix))
    for name, argv, prefix in shells:
        r = subprocess.run(argv, cwd=box.away, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
        probe = _probe(r)
        assert r.returncode == 5, (name, r.stdout, r.stderr)
        assert os.path.samefile(str(probe["root"]), box.snapshot) and probe["global"] == "1", (name, probe)
        assert str(probe["launcher"]).startswith(prefix) and probe["argv"] == ["x"], (name, probe)
        ran += 1
    assert ran >= 1
    r = subprocess.run(f'"{comspec}" /d /s /c "pyt uninstall"', cwd=box.away, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
    assert r.returncode == 0 and "pyt is uninstalled" in r.stderr, (r.stdout, r.stderr)
    assert "cannot be found" not in r.stdout + r.stderr, (r.stdout, r.stderr)
    assert box.launchers() == {} and not box.snapshot.exists()


# --- the all-or-nothing swap, in process -------------------------------------------------------------


OLD_LAUNCHER = b"#!/bin/sh\n# pytemplate-launcher: an older one\n"
NEW_LAUNCHER = b"#!/bin/sh\n# pytemplate-launcher: the new one\n"


class Swap:
    """A source folder, an installed template with its record, and a launcher of an earlier
    install; `plan()` installs the source over them."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch, fresh: bool) -> None:
        self.source = tmp / "source"
        (self.source / "sub").mkdir(parents=True)
        (self.source / "a.txt").write_bytes(b"new a\n")  # bytes: write_text gives CRLF on Windows
        (self.source / "sub" / "b.txt").write_bytes(b"new b\n")
        monkeypatch.setattr(cmd_install, "ROOT", self.source)
        self.snapshot = tmp / "data" / "pytemplate" / "template"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        if not fresh:
            (self.snapshot / ".pytemplate").mkdir(parents=True)
            (self.snapshot / "old.txt").write_text("old\n", encoding="utf-8")
            (self.snapshot / cmd_install.RECORD).write_text('{"commit": "old"}\n', encoding="utf-8")
            (self.bin / "pyt").write_bytes(OLD_LAUNCHER)
        self.before = self.state()

    def plan(self) -> cmd_install.Plan:
        return cmd_install.Plan(
            snapshot=self.snapshot, bin=self.bin, files=["a.txt", "sub/b.txt"], record={"schema": 1, "commit": "new"},
            old=cmd_install.read_record(self.snapshot), launchers={"pyt": NEW_LAUNCHER, "pyt.cmd": NEW_LAUNCHER},
        )  # fmt: skip

    def state(self) -> dict[str, dict[str, bytes | str]]:
        top = self.snapshot.parent.parent
        return {"data": _tree(top), "bin": _tree(self.bin)}


def _fail_on(calls: list[int], when: int, error: BaseException, real: Callable[..., Any]) -> Callable[..., Any]:
    """`real`, except that its call number `when` (1-based) raises `error`."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == when:
            raise error
        return real(*args, **kwargs)

    return wrapper


FAILURES = {
    # step: (function of cmd_install or os to break, its call that fails)
    "copying the files": ("_copy_files", 1),
    "staging the second launcher": ("_stage", 2),
    "moving the old copy aside": ("_rename", 1),
    "moving the new copy in": ("_rename", 2),
    "replacing the second launcher": ("replace", 2),
}


@pytest.mark.parametrize("fresh", [False, True], ids=["over an install", "first install"])
@pytest.mark.parametrize("step", list(FAILURES))
@pytest.mark.parametrize("error", ["disk full", "Ctrl+C"])
def test_a_failed_install_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str, error: str, fresh: bool) -> None:
    swap = Swap(tmp_path, monkeypatch, fresh)
    if fresh and step == "moving the old copy aside":
        pytest.skip("a first install moves no old copy aside")
    name, when = FAILURES[step]
    if fresh and name == "_rename":
        when -= 1  # no old copy to move
    exc: BaseException = KeyboardInterrupt() if error == "Ctrl+C" else OSError(errno.ENOSPC, "No space left on device")
    calls: list[int] = []
    if name == "replace":
        monkeypatch.setattr(os, "replace", _fail_on(calls, when, exc, os.replace))
    else:
        monkeypatch.setattr(cmd_install, name, _fail_on(calls, when, exc, getattr(cmd_install, name)))
    if error == "Ctrl+C":
        with pytest.raises(KeyboardInterrupt):
            cmd_install.install(swap.plan())
    else:
        with pytest.raises(PytError, match="nothing was changed") as e:
            cmd_install.install(swap.plan())
        assert e.value.code == 1 and "No space left on device" in str(e.value)
    assert swap.state() == swap.before


def test_the_installed_template_names_local_sources_outside_the_clone_from_its_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clone that takes a library and a wheelhouse from folders next to it (`../mylib`,
    `find-links = ["../wheels"]`): the installed template, in the data folder, names them from
    there, as `new` names them from a new project (presets.rebase_local_sources). Copied as
    they were, they named folders of the data folder, and `pyt new` from the installed template
    stopped in __init's `uv add` and removed the project."""
    swap = Swap(tmp_path, monkeypatch, fresh=True)
    (swap.source / "pyproject.toml").write_bytes(b'[tool.uv]\nfind-links = ["../wheels"]\n\n[tool.uv.sources]\nmylib = { path = "../mylib" }\n')
    (swap.source / "uv.lock").write_bytes(b'version = 1\n\n[[package]]\nname = "mylib"\nversion = "0.1.0"\nsource = { directory = "../mylib" }\n')
    plan = swap.plan()
    plan.files += ["pyproject.toml", "uv.lock"]
    cmd_install.install(plan)
    pyproject = tomllib.loads((swap.snapshot / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((swap.snapshot / "uv.lock").read_text(encoding="utf-8"))
    names = {
        "../wheels": pyproject["tool"]["uv"]["find-links"][0],
        "../mylib": pyproject["tool"]["uv"]["sources"]["mylib"]["path"],
    }
    assert lock["package"][0]["source"]["directory"] == names["../mylib"]
    for original, there in names.items():
        assert there != original and os.path.normpath(swap.snapshot / there) == os.path.normpath(swap.source / original), there
    assert (swap.source / "pyproject.toml").read_bytes().count(b'"../mylib"') == 1  # the clone itself stays


def test_an_interrupt_right_after_the_swap_is_undone_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C between the rename of the new copy into place and the line that notes it: the
    undo sees that the new copy's own folder has gone and puts the old copy back."""
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    real = cmd_install._rename

    def rename(src: Path, dst: Path) -> None:
        real(src, dst)
        if dst == swap.snapshot and src.name.startswith(cmd_install.NEW):
            raise KeyboardInterrupt

    monkeypatch.setattr(cmd_install, "_rename", rename)
    with pytest.raises(KeyboardInterrupt):
        cmd_install.install(swap.plan())
    assert swap.state() == swap.before


def test_an_interrupt_right_after_a_launcher_is_replaced_is_undone_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C right after the first launcher was replaced: its old bytes come back (they are
    noted before the replace, not after it)."""
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    real = os.replace
    interrupted: list[str] = []

    def replace(src: str | Path, dst: str | Path) -> None:
        real(src, dst)
        if Path(dst) == swap.bin / "pyt" and not interrupted:
            interrupted.append(str(dst))
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(KeyboardInterrupt):
        cmd_install.install(swap.plan())
    assert interrupted and swap.state() == swap.before


@pytest.mark.usefixtures("default_signals")
def test_a_second_ctrl_c_waits_for_the_undo_of_the_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A Ctrl+C as the new copy moves in is undone; a second one during that undo (SIGINT keeps
    Python's own handler) raised KeyboardInterrupt inside it: the old copy stayed aside, the new one
    was left next to it, and "nothing was changed" never came. The undo goes on to its end first."""
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    real = cmd_install._rename
    calls: list[int] = []

    def rename(src: Path, dst: Path) -> None:
        calls.append(1)
        if len(calls) == 2:  # the new copy moves in: the first Ctrl+C
            raise KeyboardInterrupt
        if len(calls) == 3:  # the undo moves the old copy back: a second one
            signal.raise_signal(signal.SIGINT)
        real(src, dst)

    monkeypatch.setattr(cmd_install, "_rename", rename)
    with pytest.raises(KeyboardInterrupt):
        cmd_install.install(swap.plan())
    assert len(calls) == 3 and swap.state() == swap.before
    assert "nothing was changed" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # given back once the undo is done


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM and SIGHUP are POSIX signals")
@pytest.mark.parametrize("second", ["sigterm", "sighup"])
@pytest.mark.parametrize("first", ["ctrl+c", "a rename that fails"])
@pytest.mark.usefixtures("default_signals")
def test_a_termination_signal_waits_for_the_undo_of_the_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], first: str, second: str
) -> None:
    """A SIGTERM or SIGHUP during the undo of a Ctrl+C or a failed step (a closed terminal,
    timeout, docker stop) raised proc.Interrupted inside the undo: the old copy stayed aside, and
    "nothing was changed" never came. The undo goes on to its end first, and the signals get
    their handlers back."""
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    real = cmd_install._rename
    calls: list[int] = []
    stop = signal.SIGTERM if second == "sigterm" else signal.SIGHUP

    def rename(src: Path, dst: Path) -> None:
        calls.append(1)
        if len(calls) == 2:  # the new copy moves in: it fails, or a Ctrl+C
            if first == "ctrl+c":
                raise KeyboardInterrupt
            raise OSError(errno.EACCES, "Permission denied", str(dst))
        if len(calls) == 3:  # the undo moves the old copy back: a signal comes
            signal.raise_signal(stop)
        real(src, dst)

    monkeypatch.setattr(cmd_install, "_rename", rename)
    with pytest.raises((KeyboardInterrupt, PytError)) as e:  # proc.Interrupted escaped the undo
        cmd_install.install(swap.plan())
    assert isinstance(e.value, KeyboardInterrupt if first == "ctrl+c" else PytError), repr(e.value)
    assert len(calls) == 3 and swap.state() == swap.before
    told = capsys.readouterr().err if first == "ctrl+c" else str(e.value)
    assert "nothing was changed" in told, told
    for signum in (signal.SIGTERM, signal.SIGHUP):  # given back once the undo is done
        assert signal.getsignal(signum) is signal.SIG_DFL
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_a_complete_install_replaces_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    cmd_install.install(swap.plan())
    assert _files(swap.snapshot) == {"a.txt": b"new a\n", "sub/b.txt": b"new b\n", cmd_install.RECORD: b'{\n  "schema": 1,\n  "commit": "new"\n}\n'}
    assert _files(swap.bin) == {"pyt": NEW_LAUNCHER, "pyt.cmd": NEW_LAUNCHER}
    assert sorted(p.name for p in swap.snapshot.parent.iterdir()) == ["template"]


NEW_COPY = {"a.txt": b"new a\n", "sub/b.txt": b"new b\n", cmd_install.RECORD: b'{\n  "schema": 1,\n  "commit": "new"\n}\n'}


def _during_the_copy(monkeypatch: pytest.MonkeyPatch, step: Callable[[], None]) -> None:
    """`step` runs once the first file of the new copy is written (in the first copy only: a
    second install that `step` starts copies as usual), then the copy goes on."""
    real = cmd_install._copy_files
    ran: list[bool] = []

    def copy(files: Any, dest: Path) -> None:
        files = list(files)
        real(files[:1], dest)
        if not ran:
            ran.append(True)
            step()
        real(files[1:], dest)

    monkeypatch.setattr(cmd_install, "_copy_files", copy)


@pytest.mark.parametrize("second", ["install", "uninstall"])
def test_a_second_install_or_uninstall_waits_for_none_and_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second: str) -> None:
    """Two runs at once (two terminals, a script and a hand): each deletes what an unfinished run
    left next to the installed template, and the second deleted the copy the first was writing.
    The first then swapped in an installed template without the files it had copied, and said
    it was installed (or its undo deleted the installed template, saying nothing was changed).
    One run at a time: the second is refused before it changes anything, and the first ends whole."""
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    monkeypatch.setattr(cmd_install, "snapshot_dir", lambda environ=None, windows=IS_WINDOWS: swap.snapshot)
    monkeypatch.setattr(cmd_install, "bin_dir", lambda: swap.bin)
    monkeypatch.delenv(cmd_install.LAUNCHER_FILE, raising=False)
    refused: list[str] = []

    def run_the_second() -> None:
        with pytest.raises(PytError, match=rf"pyt {second}: another pyt install or uninstall is running \(") as e:
            if second == "install":
                cmd_install.install(swap.plan())
            else:
                cmd_install.cmd_uninstall(NO_CFG, [])
        refused.append(str(e.value))

    _during_the_copy(monkeypatch, run_the_second)
    cmd_install.install(swap.plan())
    assert refused and str(swap.snapshot.parent / cmd_install.LOCK_FILE) in refused[0], refused
    assert _files(swap.snapshot) == NEW_COPY  # whole: every file of the new copy
    assert _files(swap.bin) == {"pyt": NEW_LAUNCHER, "pyt.cmd": NEW_LAUNCHER}
    assert sorted(p.name for p in swap.snapshot.parent.iterdir()) == ["template"]  # no lock file left
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0  # the lock went with the run that held it
    assert not swap.snapshot.parent.exists()


@pytest.mark.parametrize("then", ["the copy goes on", "a copy fails"])
def test_an_install_whose_new_copy_another_run_deleted_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, then: str) -> None:
    """A run of a pyt older than the lock (an installed template's own runner) deletes the new
    copy as a leftover while install writes it: the copy made the folder again and swapped in an
    installed template without the files written before (exit 0), or, when a copy failed, the
    undo took the folder's absence for the swap and deleted the installed template that was
    there, saying "nothing was changed". The copy now stops, and the old installed template stays."""
    swap = Swap(tmp_path, monkeypatch, fresh=False)

    def an_older_pyt() -> None:
        cmd_install.remove_leftovers(swap.snapshot, [swap.bin])  # what such a run does first: no lock
        if then == "a copy fails":
            raise OSError(errno.EIO, "Input/output error", str(swap.snapshot.parent / "x"))

    _during_the_copy(monkeypatch, an_older_pyt)
    with pytest.raises(PytError, match="nothing was changed") as e:
        cmd_install.install(swap.plan())
    assert "could not put back" not in str(e.value)
    assert swap.state() == swap.before  # the old installed template and launchers, whole


def test_a_lock_file_a_killed_run_left_holds_nothing_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock is the OS's, on the open file (dropped when the process ends, however it ends):
    the file a killed run left blocks no later run, which deletes it as it ends."""
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    (swap.snapshot.parent / cmd_install.LOCK_FILE).write_bytes(b"")
    cmd_install.install(swap.plan())
    assert _files(swap.snapshot) == NEW_COPY
    assert sorted(p.name for p in swap.snapshot.parent.iterdir()) == ["template"]


def _refuse_every_lock(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """The lock call fails with `code`: flock on POSIX, msvcrt.locking on Windows."""

    def refuse(*_args: object) -> None:
        raise OSError(code, os.strerror(code))

    if sys.platform == "win32":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", refuse)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", refuse)


@pytest.mark.parametrize("code", [errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL])
def test_install_and_uninstall_run_on_a_file_system_that_takes_no_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """A data folder on a file system without locks (a cluster's Lustre without flock, some FUSE
    and network mounts) fails the lock call with ENOLCK, EOPNOTSUPP or ENOSYS (EINVAL, as the C
    runtime maps an unsupported lock on Windows): every install and uninstall said "another pyt
    install or uninstall is running" where none was, for good, as the harnesses did before
    (project.lock_refusal). Nothing can be guarded there: both run without the lock and leave no
    lock file. A lock another run holds is still refused."""
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    monkeypatch.setattr(cmd_install, "snapshot_dir", lambda environ=None, windows=IS_WINDOWS: swap.snapshot)
    monkeypatch.setattr(cmd_install, "bin_dir", lambda: swap.bin)
    monkeypatch.delenv(cmd_install.LAUNCHER_FILE, raising=False)
    _refuse_every_lock(monkeypatch, code)
    cmd_install.install(swap.plan())
    assert _files(swap.snapshot) == NEW_COPY
    assert sorted(p.name for p in swap.snapshot.parent.iterdir()) == ["template"]  # no lock file left
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0
    assert not swap.snapshot.parent.exists()
    for held in (errno.EWOULDBLOCK, errno.EACCES):  # flock's answer, msvcrt.locking's
        _refuse_every_lock(monkeypatch, held)
        with pytest.raises(PytError, match=r"pyt install: another pyt install or uninstall is running \("):
            cmd_install.install(swap.plan())
        assert not swap.snapshot.exists()  # refused before it copied anything


@posix_only
@pytest.mark.parametrize("fresh", [False, True], ids=["over an install", "first install"])
def test_the_installed_template_has_the_mode_of_a_plain_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh: bool) -> None:
    """The new copy's folder came from tempfile.mkdtemp (0700), and the installed template kept that
    mode: `pyt new` from it gave every project folder 0700 (copy_template copied the root's mode
    then). It gets what a plain mkdir gives: 0777 minus the umask."""
    swap = Swap(tmp_path, monkeypatch, fresh=fresh)
    old = os.umask(0o022)
    try:
        cmd_install.install(swap.plan())
    finally:
        os.umask(old)
    assert stat.S_IMODE(swap.snapshot.stat().st_mode) & 0o777 == 0o755


@posix_only
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
@pytest.mark.parametrize("step", list(FAILURES))
def test_a_termination_signal_during_install_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str, signame: str) -> None:
    """SIGTERM or SIGHUP (kill PID, a closed terminal, a supervisor) during the swap killed the
    runner at once and left a whole `.template-new-*` copy next to the installed template: while
    the swap runs they stop it like Ctrl+C (proc.Interrupted: cli.main then says `terminated`
    and exits 128 + N), so its undo runs. Sent from inside every step."""
    signum = getattr(signal, signame)
    if signal.getsignal(signum) is not signal.SIG_DFL:
        pytest.skip(f"{signame} is not at its default handler in this process (the runner leaves such a one alone)")
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    name, when = FAILURES[step]
    calls: list[int] = []

    def sending(real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == when:
                # At its default action the signal would end this test process itself
                assert signal.getsignal(signum) is not signal.SIG_DFL, f"the swap runs with {signame} at its default action"
                os.kill(os.getpid(), signum)  # its Python handler runs within this step
            return real(*args, **kwargs)

        return wrapper

    if name == "replace":
        monkeypatch.setattr(os, "replace", sending(os.replace))
    else:
        monkeypatch.setattr(cmd_install, name, sending(getattr(cmd_install, name)))
    with pytest.raises(proc.Interrupted) as e:
        cmd_install.install(swap.plan())
    assert e.value.signum == signum and e.value.code == 0
    assert swap.state() == swap.before
    assert signal.getsignal(signum) is signal.SIG_DFL  # given back once the undo is done


@pytest.mark.parametrize("locked", ["pyt", "pyt.cmd"], ids=["the first launcher", "the second launcher"])
def test_a_launcher_that_cannot_be_replaced_is_named_and_nothing_leaks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked: str) -> None:
    """A launcher the user may not replace (chattr +i, another user's file): the error named the
    staged temporary file instead of the launcher, the undo said "could not put back" a launcher
    that never changed, and it left a staged `.pyt-install-*` file behind."""
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    target = swap.bin / locked
    real = os.replace

    def replace(src: str | Path, dst: str | Path) -> None:
        if Path(dst) == target:
            raise PermissionError(errno.EPERM, "Operation not permitted", os.fsdecode(src), None, os.fsdecode(dst))
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(PytError) as e:
        cmd_install.install(swap.plan())
    message = str(e.value)
    assert f"pyt install failed: {target}: Operation not permitted" in message, message
    assert "nothing was changed" in message and "could not put back" not in message and ".pyt-install-" not in message, message
    assert swap.state() == swap.before  # the first launcher put back, no staged file left


@pytest.mark.parametrize("which", ["bin", "data"], ids=["uv's tool bin folder", "the data folder"])
def test_a_folder_that_refuses_the_new_files_is_named_not_their_temporary_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    """A folder that refuses the files install makes there although os.access says yes (Windows
    answers yes for every folder; sysfs and a root-squashed share refuse root): the error named
    the temporary name it tried, `<bin>/.pyt-install-p81hkalh` (mkstemp's own error, as it
    raises it) or `.template-new-...`, which never existed. It names the folder, with the way
    out, and changes nothing."""
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    if which == "bin":
        folder, fix = swap.bin, "set UV_TOOL_BIN_DIR to a folder of yours"
        real_mkstemp = tempfile.mkstemp

        def mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
            if kwargs.get("dir") is not None and Path(kwargs["dir"]) == folder:
                raise PermissionError(errno.EACCES, "Permission denied", os.path.join(kwargs["dir"], f"{kwargs.get('prefix', 'tmp')}p81hkalh"))
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    else:
        folder, fix = swap.snapshot.parent, f"set another data folder ({cmd_install.data_home_names()})"
        real_mkdir = os.mkdir

        def mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
            if Path(path).parent == folder and Path(path).name.startswith(cmd_install.NEW):
                raise PermissionError(errno.EACCES, "Permission denied", os.fspath(path))
            real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(os, "mkdir", mkdir)
    with pytest.raises(PytError) as e:
        cmd_install.install(swap.plan())
    message = str(e.value)
    assert e.value.code == 1 and f"pyt install cannot write into {folder}: Permission denied: {fix}" in message, message
    assert "nothing was changed" in message and ".pyt-install-" not in message and cmd_install.NEW not in message, message
    assert swap.state() == swap.before


# --- where things go --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "windows", "expected"),
    [
        ({"XDG_DATA_HOME": "/x/data", "HOME": "/h"}, False, "/x/data/pytemplate/template"),
        ({"XDG_DATA_HOME": "rel/data", "HOME": "/h"}, False, "/h/.local/share/pytemplate/template"),  # the XDG spec ignores it
        ({"XDG_DATA_HOME": "", "HOME": "/h"}, False, "/h/.local/share/pytemplate/template"),
        ({"HOME": "rel"}, False, None),  # a relative HOME would name a folder below the current one
        ({}, False, None),
        ({"LOCALAPPDATA": "C:/Users/u/AppData/Local", "USERPROFILE": "C:/Users/u"}, True, "C:/Users/u/AppData/Local/pytemplate/template"),
        ({"localappdata": "C:/L"}, True, "C:/L/pytemplate/template"),  # Windows variables have no case
        ({"USERPROFILE": "C:/Users/u", "XDG_DATA_HOME": "/x"}, True, "C:/Users/u/AppData/Local/pytemplate/template"),
        ({"HOME": "/h"}, True, None),
    ],
)
def test_snapshot_dir_follows_the_launchers_rules(environ: dict[str, str], windows: bool, expected: str | None) -> None:
    """The launchers look for the installed template in the same place (`_pt_installed` in pyt,
    the `$data` of pyt.ps1, `:installed` in pyt.cmd): test_launcher_sh and test_launcher_win run
    them against it."""
    found = cmd_install.snapshot_dir(environ, windows)
    assert (found.as_posix() if found is not None else None) == expected


def test_every_launcher_carries_the_marker() -> None:
    for name in cmd_install.NAMES:
        head = (ROOT / name).read_bytes()[:4096]
        assert cmd_install.MARKER.search(head), f"{name} has no `pytemplate-launcher` line in its first 4 KiB"


# --- the pyt.cmd cmd runs for this very run ---------------------------------------------------------


def _uv_line_end(data: bytes) -> int:
    """Where cmd reads on once the uv call of a pyt.cmd has returned: after the line with the
    argument list."""
    return data.index(b"\n", data.index(b"%*")) + 1


def test_self_deleting_puts_its_last_line_where_cmd_reads_on() -> None:
    """cmd reads a batch file one line at a time, opening it again by name at the byte where the
    last line ended: the stand-in uninstall writes in place of the pyt.cmd cmd runs holds, at the
    end of pyt.cmd's uv line, the line that ends the batch, deletes the file and exits with uv's
    code. Started on its own it says what it is (exit 1). ASCII, CRLF, cmd's line limit."""
    running = (ROOT / "pyt.cmd").read_bytes()
    data = cmd_install.self_deleting(running)
    assert data is not None and data.isascii()
    at = _uv_line_end(running)
    assert data[at:] == cmd_install.SELF_DELETE and data.index(cmd_install.SELF_DELETE) == at
    assert data.startswith(b"@echo off\r\n") and cmd_install.MARKER.search(data[:4096])
    lines = data.split(b"\r\n")
    assert lines[-1] == b"" and all(b"\n" not in line and len(line) <= 8191 for line in lines)
    assert b"exit /b 1\r\n" in data[:at] and b"%*" not in data


@pytest.mark.parametrize("extra", [0, 1, 2, 3, 4, 5, 6, 4000, 4004, 4005, 9000])
def test_self_deleting_fills_any_distance(extra: int) -> None:
    """Rem lines of 5 to 4000 bytes fill the distance; one of 1 to 4 bytes goes to the marker line."""
    head = len(cmd_install._LEFTOVER)
    running = b"x" * (head + extra - 4) + b"%*\r\n" + b"exit /b %ERRORLEVEL%\r\n"
    data = cmd_install.self_deleting(running)
    assert data is not None and data.index(cmd_install.SELF_DELETE) == head + extra == _uv_line_end(running)
    assert all(len(line) <= 4000 for line in data.split(b"\r\n"))


def test_self_deleting_needs_one_uv_line_it_can_reach() -> None:
    assert cmd_install.self_deleting(b"@echo off\r\nexit /b 0\r\n") is None  # no argument list
    assert cmd_install.self_deleting(b"x" * 400 + b"%*\r\n%*\r\n") is None  # two: which one?
    assert cmd_install.self_deleting(b"%*\r\nexit /b %ERRORLEVEL%\r\n") is None  # the rest does not fit before it


def test_run_by_cmd_needs_cmd_and_the_file_it_named(tmp_path: Path) -> None:
    launcher = tmp_path / "pyt.cmd"
    launcher.write_bytes(b"@echo off\r\n")
    other = tmp_path / "other.cmd"
    other.write_bytes(b"@echo off\r\n")
    assert cmd_install.run_by_cmd(launcher, {"PYTEMPLATE_LAUNCHER": "cmd", cmd_install.LAUNCHER_FILE: str(launcher)})
    for env in (
        {"PYTEMPLATE_LAUNCHER": "ps1:Core:7.6", cmd_install.LAUNCHER_FILE: str(launcher)},  # pyt.ps1 reads itself whole
        {cmd_install.LAUNCHER_FILE: str(launcher)},
        {"PYTEMPLATE_LAUNCHER": "cmd"},
        {"PYTEMPLATE_LAUNCHER": "cmd", cmd_install.LAUNCHER_FILE: str(other)},
        {"PYTEMPLATE_LAUNCHER": "cmd", cmd_install.LAUNCHER_FILE: str(tmp_path / "gone.cmd")},
    ):
        assert not cmd_install.run_by_cmd(launcher, env), env


# A pyt.cmd of an earlier install: the marker, and a uv line the stand-in reads on after
EARLIER_CMD = (
    b"@echo off\r\nrem pytemplate-launcher: an earlier pyt.cmd\r\n" + b"rem " + b"-" * 300 + b"\r\n"
    + b'"%PT_UV%" run --quiet --script "%PT_ROOT%.pytemplate\\pyt.py" %*\r\nexit /b %ERRORLEVEL%\r\n'
)  # fmt: skip


@needs_git
@needs_uv
def test_install_never_replaces_the_pyt_cmd_cmd_runs(clone: Path, box: Box) -> None:
    """cmd reads the pyt.cmd it runs again once this run ends, at the byte where its uv line
    ended: replaced by other bytes, it would run what the new file holds there. So install
    refuses, before any write, when that file is the launcher it would replace with other
    bytes, and says how to run it; the same bytes are no change."""
    assert pyt(clone, box, "install").returncode == 0
    target = box.bin / cmd_install.LAUNCHERS[0]  # the file cmd would run, as far as this test goes
    target.write_bytes(EARLIER_CMD)
    before = (box.launchers(), _files(box.snapshot))
    via_cmd = {"PYTEMPLATE_LAUNCHER": "cmd", cmd_install.LAUNCHER_FILE: str(target)}
    r = pyt(clone, box, "install", **via_cmd)
    assert r.returncode == 2 and f"cmd runs {target}, which pyt install would replace" in r.stderr, r.stderr
    assert "pyt.cmd install in" in r.stderr and (box.launchers(), _files(box.snapshot)) == before
    assert pyt(clone, box, "install", **{**via_cmd, "PYTEMPLATE_LAUNCHER": "ps1:Core:7.6"}).returncode == 0
    assert pyt(clone, box, "install", **{**via_cmd, cmd_install.LAUNCHER_FILE: str(target)}).returncode == 0  # same bytes now


@needs_git
@needs_uv
def test_uninstall_leaves_a_self_deleting_stand_in_for_the_pyt_cmd_cmd_runs(clone: Path, box: Box) -> None:
    """Deleted while cmd ran it, a pyt.cmd made cmd say "The batch file cannot be found." (exit 1)
    after a successful uninstall: uninstall handles it last and puts self_deleting in its place,
    which cmd then reads on in and deletes (test_launcher_win.test_cmd_goes_on_reading_what_uninstall_leaves)."""
    assert pyt(clone, box, "install").returncode == 0
    target = box.bin / cmd_install.LAUNCHERS[0]
    target.write_bytes(EARLIER_CMD)
    via_cmd = {"PYTEMPLATE_LAUNCHER": "cmd", cmd_install.LAUNCHER_FILE: str(target)}
    dry = pyt(clone, box, "--dry-run", "uninstall", **via_cmd)
    assert dry.returncode == 0 and f"would remove {target}" in dry.stderr and target.read_bytes() == EARLIER_CMD, dry.stderr
    r = pyt(clone, box, "uninstall", cwd=box.away, **via_cmd)
    assert r.returncode == 0 and f"removed {target} (cmd runs it: it deletes itself as this run ends)" in r.stderr, r.stderr
    assert not box.snapshot.exists() and target.read_bytes() == cmd_install.self_deleting(EARLIER_CMD)
    assert cmd_install.is_launcher(target)  # ours: a later install replaces it, uninstall removes it
    assert pyt(clone, box, "uninstall").returncode == 0 and not target.exists()


@pytest.mark.parametrize("dry", [False, True], ids=["run", "dry run"])
def test_uninstall_refuses_to_delete_the_installed_pyt_cmd_cmd_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry: bool) -> None:
    """cmd ran the installed template's own pyt.cmd (%LOCALAPPDATA%\\pytemplate\\template\\pyt.cmd
    uninstall, or `pyt uninstall` typed in that folder: cmd runs the current folder's pyt.cmd
    first): uninstall deleted it with the installed template, and once uv returned cmd said "The
    batch file cannot be found." and exit 1 after a successful uninstall. It refuses before
    removing anything, naming the launcher that works."""
    inst = Installed(tmp_path, monkeypatch)
    inst.stuck = False
    inner = inst.snapshot / "pyt.cmd"
    _run_by_cmd(monkeypatch, inner)
    before = _files(tmp_path)
    monkeypatch.setattr(proc, "DRY_RUN", dry)
    with pytest.raises(PytError, match="the installed template's own pyt.cmd") as e:
        cmd_install.cmd_uninstall(NO_CFG, [])
    assert e.value.code == 2 and f"cmd runs {inner}" in str(e.value) and "run pyt uninstall from another folder" in str(e.value)
    assert _files(tmp_path) == before
    monkeypatch.setenv(cmd_install.LAUNCHER_FILE, str(inst.bin / "pyt.cmd"))  # the bin folder's: handled by _retire
    (inst.bin / "pyt.cmd").write_bytes(EARLIER_CMD)
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0


def test_the_pyt_cmd_cmd_runs_goes_only_once_the_rest_is_gone(tmp_path: Path) -> None:
    """After a failure the pyt.cmd cmd runs stays whole only while `pyt uninstall` can run again
    from it (retry); otherwise it goes as after a success."""
    target = tmp_path / "pyt.cmd"
    target.write_bytes(EARLIER_CMD)
    removed: list[str] = []
    left: list[str] = []
    assert cmd_install._retire(target, removed, left, ["somewhere: in use or not writable"], retry=True)
    assert target.read_bytes() == EARLIER_CMD and not removed and "cmd runs it" in left[0]
    assert not cmd_install._retire(target, removed, left, ["somewhere: in use or not writable"], retry=False)
    assert target.read_bytes() == cmd_install.self_deleting(EARLIER_CMD) and "it deletes itself" in removed[0]
    target.write_bytes(b"@echo off\r\nrem pytemplate-launcher: no uv line\r\n")
    assert not cmd_install._retire(target, removed, left, [])
    assert not target.exists() and "may say it cannot find the batch file" in removed[1]


def test_run_by_cmd_tells_the_names_of_a_hard_link_apart(tmp_path: Path) -> None:
    """Two names of one file (a hard link): cmd reads on in the name it was given, so only that
    name is the file cmd runs; os.path.samefile said yes for both."""
    project, linked = tmp_path / "proj" / "pyt.cmd", tmp_path / "bin" / "pyt.cmd"
    for folder in (project.parent, linked.parent):
        folder.mkdir()
    project.write_bytes(EARLIER_CMD)
    try:
        os.link(project, linked)
    except OSError as e:
        pytest.skip(f"cannot make a hard link here: {e}")
    via = {"PYTEMPLATE_LAUNCHER": "cmd"}
    assert cmd_install.run_by_cmd(linked, {**via, cmd_install.LAUNCHER_FILE: str(linked)})
    assert cmd_install.run_by_cmd(project, {**via, cmd_install.LAUNCHER_FILE: str(project)})
    assert not cmd_install.run_by_cmd(linked, {**via, cmd_install.LAUNCHER_FILE: str(project)})
    assert not cmd_install.run_by_cmd(project, {**via, cmd_install.LAUNCHER_FILE: str(linked)})


@pytest.mark.parametrize("runs", ["project", "bin"], ids=["cmd runs the project's name", "cmd runs the bin folder's name"])
def test_uninstall_never_writes_through_a_hard_link_of_the_pyt_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: str) -> None:
    """The pyt.cmd of uv's tool bin folder was a hard link of a project's pyt.cmd (made by hand to
    put pyt on PATH): uninstall took it for the file cmd runs when cmd ran the project's own name,
    and wrote the self-deleting stand-in into their one inode: the project's tracked pyt.cmd held
    it, and cmd then deleted it (del "%~f0"). The bin folder's name goes, or, when cmd runs that
    very name, a new file takes it: the project's pyt.cmd keeps its bytes either way."""
    inst = Installed(tmp_path, monkeypatch)
    inst.stuck = False
    project = tmp_path / "proj" / "pyt.cmd"
    (project.parent / ".pytemplate").mkdir(parents=True)
    project.write_bytes(EARLIER_CMD)
    linked = inst.bin / "pyt.cmd"
    try:
        os.link(project, linked)
    except OSError as e:
        pytest.skip(f"cannot make a hard link here: {e}")
    _run_by_cmd(monkeypatch, project if runs == "project" else linked)
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0
    assert project.read_bytes() == EARLIER_CMD and os.stat(project).st_nlink == 1
    if runs == "project":
        assert not linked.exists()
    else:  # cmd reads on in the stand-in, a file of its own
        assert linked.read_bytes() == cmd_install.self_deleting(EARLIER_CMD) and not os.path.samefile(linked, project)
    assert [p.name for p in inst.bin.iterdir() if cmd_install.STAGED.match(p.name)] == []


def _run_by_cmd(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """This run, as pyt.cmd hands it over when cmd runs it (run_by_cmd)."""
    path.write_bytes(EARLIER_CMD)
    monkeypatch.setenv("PYTEMPLATE_LAUNCHER", "cmd")
    monkeypatch.setenv(cmd_install.LAUNCHER_FILE, str(path))


@pytest.mark.parametrize(
    ("stuck", "movable", "again"),
    [("in-use.txt", False, True), ("in-use.txt", True, False), (".pytemplate/tools/in-use.txt", False, False)],
    ids=["a file of the template, in place", "a file of the copy moved aside", "a file of its runner, in place"],
)
def test_the_pyt_cmd_kept_for_a_retry_can_run_uninstall_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stuck: str, movable: bool, again: bool) -> None:
    """A failed uninstall kept the pyt.cmd cmd runs "so that pyt uninstall can run again from it",
    after it had deleted the installed template's runner (moved aside, or every file but the
    record in place): typed again, that pyt.cmd found no .pytemplate/pyt.py and said "install
    it". Now the runner goes last in place, after everything else, and the pyt.cmd stays only
    while the runner is whole (Windows' usual failure: a terminal's folder inside the template,
    which cannot move then); otherwise it goes too, and the error names ./pyt uninstall in a
    project or in a clone, which finishes the job."""
    inst = Installed(tmp_path, monkeypatch, in_use=stuck)
    running = inst.bin / "pyt.cmd"
    _run_by_cmd(monkeypatch, running)
    if not movable:
        real = cmd_install._rename

        def rename(src: Path, dst: Path) -> None:
            if src == inst.snapshot:
                raise PermissionError(errno.EACCES, "Permission denied", str(src))
            real(src, dst)

        monkeypatch.setattr(cmd_install, "_rename", rename)
    with pytest.raises(PytError, match="could not remove") as e:
        cmd_install.cmd_uninstall(NO_CFG, [])
    assert not (inst.bin / "pyt").exists()
    if again:
        # What pyt.cmd runs is there: the ENTRY it looks for, the runner, the configuration
        for rel in (cmd_install.ENTRY, ".pytemplate/runner/cli.py", "pytemplate.toml", cmd_install.RECORD):
            assert (inst.snapshot / rel).is_file(), rel
        assert running.read_bytes() == EARLIER_CMD and str(e.value).endswith("then run pyt uninstall again"), str(e.value)
    else:
        assert not (inst.snapshot / cmd_install.ENTRY).exists()
        assert running.read_bytes() == cmd_install.self_deleting(EARLIER_CMD)
        assert str(e.value).endswith("then run ./pyt uninstall in a project or in a clone of the template"), str(e.value)
    inst.stuck = False  # the program that held the file has ended
    if not again:  # from a project, whose launcher is not the pyt.cmd uninstall left
        monkeypatch.delenv("PYTEMPLATE_LAUNCHER")
        monkeypatch.delenv(cmd_install.LAUNCHER_FILE)
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0
    assert not inst.snapshot.parent.exists()
    if again:  # from the pyt.cmd it kept, which then goes as that run ends
        assert list(inst.bin.iterdir()) == [running] and running.read_bytes() == cmd_install.self_deleting(EARLIER_CMD)
    else:  # its self-deleting stand-in is ours too
        assert list(inst.bin.iterdir()) == []


def test_a_launcher_that_cannot_be_removed_keeps_the_installed_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A launcher uninstall cannot remove (a bin folder that is not writable, chattr +i) stays on
    PATH: uninstall deleted the installed template anyway, and that launcher then said "install
    it" and could not run `pyt uninstall` again. Now the installed template stays while one of
    its launchers does: pyt keeps working, and `pyt uninstall` again finishes the job."""
    inst = Installed(tmp_path, monkeypatch)
    inst.stuck = False
    before = _files(inst.snapshot)
    launcher = inst.bin / "pyt"
    real = cmd_install._unlink
    blocked = [True]
    monkeypatch.setattr(cmd_install, "_unlink", lambda path: False if blocked[0] and path == launcher else real(path))
    with pytest.raises(PytError, match="could not remove") as e:
        cmd_install.cmd_uninstall(NO_CFG, [])
    assert f"{launcher}: in use or not writable" in str(e.value) and str(e.value).endswith("then run pyt uninstall again")
    assert f"left {inst.snapshot} (the installed template): the launchers it could not remove run it" in capsys.readouterr().err
    assert launcher.is_file() and _files(inst.snapshot) == before
    blocked[0] = False
    assert cmd_install.cmd_uninstall(NO_CFG, []) == 0
    assert not inst.snapshot.parent.exists() and not launcher.exists()


@posix_only
@needs_git
@needs_uv
def test_the_launcher_left_by_a_failed_uninstall_runs_it_again(clone: Path, box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real launcher: after an uninstall that could not remove it, `pyt uninstall` typed
    outside any project runs the installed template again (it said "install it", exit 2), and
    finishes the job."""
    assert pyt(clone, box, "install").returncode == 0
    for key, value in box.env().items():
        monkeypatch.setenv(key, value)
    for key in [k for k in os.environ if k.upper() in DROP or k.upper().startswith("PYTEMPLATE_")]:
        monkeypatch.delenv(key)
    launcher = box.bin / "pyt"
    real = cmd_install._unlink
    monkeypatch.setattr(cmd_install, "_unlink", lambda path: False if path == launcher else real(path))
    with pytest.raises(PytError, match="then run pyt uninstall again"):
        cmd_install.cmd_uninstall(NO_CFG, [])
    r = subprocess.run(["/bin/sh", str(launcher), "uninstall"], cwd=box.away, env=box.env(**_uv_dirs()), capture_output=True, text=True, timeout=180, check=False)
    assert r.returncode == 0 and "pyt is uninstalled" in r.stderr, (r.stdout, r.stderr)
    assert box.launchers() == {} and not box.snapshot.parent.exists()


def test_is_launcher_and_not_ours(tmp_path: Path) -> None:
    ours = {"sh": b"#!/bin/sh\n# pytemplate-launcher: x\n", "cmd": b"@echo off\r\nrem pytemplate-launcher: x\r\n"}
    for name, data in ours.items():
        (tmp_path / name).write_bytes(data)
        assert cmd_install.is_launcher(tmp_path / name) and cmd_install.not_ours(tmp_path / name) == ""
    (tmp_path / "late").write_bytes(b"#" * 5000 + b"\n# pytemplate-launcher: x\n")  # not in the header
    (tmp_path / "other").write_bytes(b"#!/bin/sh\necho pytemplate-launcher:\n")
    for name in ("late", "other"):
        assert not cmd_install.is_launcher(tmp_path / name) and "not a pytemplate launcher" in cmd_install.not_ours(tmp_path / name)
    assert not cmd_install.is_launcher(tmp_path / "missing")
    try:
        (tmp_path / "link").symlink_to(tmp_path / "sh")
    except OSError:
        return
    assert not cmd_install.is_launcher(tmp_path / "link") and "symbolic link" in cmd_install.not_ours(tmp_path / "link")


def test_on_path_takes_every_spelling_of_the_folder(tmp_path: Path) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    sep = os.pathsep
    assert cmd_install.on_path(folder, f"{tmp_path / 'x'}{sep}{folder}")
    assert cmd_install.on_path(folder, f"{folder}{os.sep}")
    assert not cmd_install.on_path(folder, f"{tmp_path}{sep}{sep}{tmp_path / 'binx'}")
    try:
        (tmp_path / "alias").symlink_to(folder, target_is_directory=True)
    except OSError:
        return
    assert cmd_install.on_path(folder, str(tmp_path / "alias"))


def test_path_state_reads_the_registry_path_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows a console keeps the PATH it started with: the bin folder only in the PATH
    stored in the registry (after `uv tool update-shell`) means "open a new terminal"."""
    folder = tmp_path / "bin"
    folder.mkdir()
    monkeypatch.setattr(cmd_install, "IS_WINDOWS", True)
    monkeypatch.setattr(cmd_install.shells, "registry_path_dirs", lambda env: [str(folder)])
    assert cmd_install.path_state(folder, {"Path": str(folder)}) == "on"  # Windows names have no case
    assert cmd_install.path_state(folder, {"PATH": str(tmp_path / "x")}) == "registry"
    monkeypatch.setenv("PATH", str(tmp_path / "x"))
    problem = cmd_install.path_problem(folder)
    assert problem is not None and "open a new terminal" in problem[1], problem
    monkeypatch.setattr(cmd_install.shells, "registry_path_dirs", lambda env: [])
    problem = cmd_install.path_problem(folder)
    assert problem is not None and cmd_install.UPDATE_SHELL in problem[1] and "keeps its old PATH" in problem[1], problem


def test_first_pyt_ignores_the_current_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first pyt on PATH (doctor's "another pyt comes first"): never a project's own pyt.cmd
    in the current folder, which shutil.which takes first on Windows."""
    first, second = tmp_path / "first", tmp_path / "second"
    for folder in (first, second):
        folder.mkdir()
    (second / "pyt").write_text("#!/bin/sh\n", encoding="utf-8")
    (second / "pyt").chmod(0o755)
    (second / "pyt.cmd").write_text("@echo off\r\n", encoding="utf-8")
    here = tmp_path / "here"
    here.mkdir()
    for name in ("pyt", "pyt.cmd", "pyt.ps1"):
        (here / name).write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(here)
    found = cmd_install.first_pyt(f"{first}{os.pathsep}{second}")
    assert found is not None and found.parent == second
    if not IS_WINDOWS:
        (first / "pyt").write_text("not executable\n", encoding="utf-8")
        found = cmd_install.first_pyt(f"{first}{os.pathsep}{second}")
        assert found is not None and found.parent == second


@pytest.mark.parametrize(("out", "code", "message"), [("rel/bin\n", 0, "relative path"), ("", 2, "cannot name its tool bin folder")])
def test_bin_dir_needs_an_absolute_folder(monkeypatch: pytest.MonkeyPatch, out: str, code: int, message: str) -> None:
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    monkeypatch.setattr(proc, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, code, out, "error: no tool folder\n"))
    with pytest.raises(PytError, match=message):
        cmd_install.bin_dir()


# --- doctor ------------------------------------------------------------------------------------------


class Doctor:
    """cmd_install.doctor with the installed template and uv's bin folder in tmp_path."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.snapshot, self.bin = tmp / "data" / "pytemplate" / "template", tmp / "bin"
        self.bin.mkdir(parents=True)
        self.lines: list[tuple[bool | None, str, str]] = []
        monkeypatch.setattr(cmd_install, "snapshot_dir", lambda environ=None, windows=IS_WINDOWS: self.snapshot)
        monkeypatch.setattr(cmd_install, "bin_dir", lambda: self.bin)
        monkeypatch.setattr(cmd_install, "age", lambda record: "")
        monkeypatch.setenv("PATH", str(self.bin))
        monkeypatch.setattr(cmd_install.shells, "registry_path_dirs", lambda env=None: [], raising=False)
        monkeypatch.setattr(cmd_install.shells, "_ps_policies", lambda: [])  # never this machine's PowerShells

    def install(self) -> None:
        (self.snapshot / ".pytemplate").mkdir(parents=True)
        (self.snapshot / cmd_install.RECORD).write_text('{"commit": "0123456789", "dirty": false}\n', encoding="utf-8")
        for name in cmd_install.LAUNCHERS:
            (self.snapshot / name).write_bytes(NEW_LAUNCHER)
            (self.bin / name).write_bytes(NEW_LAUNCHER)
            (self.bin / name).chmod(0o755)

    def run(self) -> list[tuple[bool | None, str, str]]:
        self.lines.clear()
        cmd_install.doctor(lambda passed, label, hint="": self.lines.append((passed, label, hint)))
        assert all(passed is not False for passed, _, _ in self.lines), self.lines  # notes only: pyt works without it
        return self.lines

    def notes(self) -> list[str]:
        return [f"{label} | {hint}" for passed, label, hint in self.lines if passed is None]


def test_doctor_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = Doctor(tmp_path, monkeypatch)
    d.run()
    assert len(d.lines) == 1 and "pyt is not installed" in d.notes()[0] and cmd_install.FROM_A_CLONE in d.notes()[0]
    d.install()
    lines = d.run()
    assert [passed for passed, _, _ in lines] == [True, True, True], lines
    assert "commit 0123456" in lines[0][1] and f"{d.bin} is on PATH" in lines[2][1]
    (d.bin / "pyt").write_bytes(OLD_LAUNCHER)  # an earlier install's launcher
    d.run()
    assert any(f"pyt in {d.bin} differs from the installed template's" in n for n in d.notes()), d.lines
    (d.bin / "pyt").write_text("#!/bin/sh\necho another tool\n", encoding="utf-8")
    d.run()
    assert any("is not a pytemplate launcher" in n for n in d.notes()) and any("no launcher pyt in" in n for n in d.notes()), d.lines
    (d.bin / "pyt").write_bytes(NEW_LAUNCHER)
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    d.run()
    assert any("is not on PATH" in n and cmd_install.UPDATE_SHELL in n for n in d.notes()), d.lines
    other = tmp_path / "other"
    other.mkdir()
    (other / "pyt").write_bytes(b"#!/bin/sh\n")
    (other / "pyt").chmod(0o755)
    monkeypatch.setenv("PATH", f"{other}{os.pathsep}{d.bin}")
    d.run()
    assert any("another pyt comes first on PATH" in n and str(other) in n for n in d.notes()), d.lines


def test_doctor_without_uv_is_a_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = Doctor(tmp_path, monkeypatch)

    def no_uv() -> Path:
        raise PytError("uv not found", 3)

    monkeypatch.setattr(cmd_install, "bin_dir", no_uv)
    d.run()
    assert any("uv not found" in n for n in d.notes()), d.lines


def test_doctor_without_a_data_folder_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HOME unset (and no absolute XDG_DATA_HOME): "no installed template in None"."""
    d = Doctor(tmp_path, monkeypatch)
    d.install()
    d.snapshot = None  # type: ignore[assignment]
    d.run()
    assert any(f"but no installed template (no user data folder: {cmd_install.data_home_names()} is not set)" in n for n in d.notes()), d.lines
    assert not any("None" in label for _, label, _ in d.lines), d.lines


def test_doctor_notes_the_launchers_left_in_the_recorded_bin_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv's tool bin folder moved since the install (UV_TOOL_BIN_DIR): its launchers are in the
    folder its record names, and doctor says so (and how they go) instead of only "no pyt"."""
    d = Doctor(tmp_path, monkeypatch)
    d.install()
    recorded = tmp_path / "old-bin"
    recorded.mkdir()
    (recorded / "pyt").write_bytes(NEW_LAUNCHER)
    (recorded / "pyt.cmd").write_text("@echo off\r\nrem another tool\r\n", encoding="utf-8")  # not ours: never named
    (d.snapshot / cmd_install.RECORD).write_text(json.dumps({"commit": "0123456789", "bin": str(recorded)}), encoding="utf-8")
    d.run()
    expected = f"pyt in {recorded}: the launchers of this install, but uv's tool bin folder is {d.bin} now"
    assert any(n.startswith(expected) and "pyt uninstall removes them" in n for n in d.notes()), d.lines
    (d.snapshot / cmd_install.RECORD).write_text(json.dumps({"commit": "0123456789", "bin": str(d.bin)}), encoding="utf-8")
    d.run()
    assert not any(str(recorded) in n for n in d.notes()), d.lines
