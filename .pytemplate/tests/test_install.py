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
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_install, presets, proc  # noqa: E402
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
    (clone / "notes.txt").write_text("untracked\n", encoding="utf-8")
    r = pyt(clone, box, "install")
    assert r.returncode == 0, r.stderr
    assert "pyt is installed" in r.stderr and "not copied (not tracked by git): notes.txt" in r.stderr, r.stderr
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
    (clone / "docs").mkdir()
    (clone / "docs" / "manual.md").symlink_to(Path("..") / "README.md")
    (clone / "docs" / "gone.md").symlink_to("missing.md")
    git(clone, "add", "docs")
    git(clone, "commit", "-q", "-m", "links")
    assert pyt(clone, box, "install").returncode == 0
    for name, target in (("manual.md", "../README.md"), ("gone.md", "missing.md")):
        link = box.snapshot / "docs" / name
        assert link.is_symlink() and os.readlink(link) == target, name


@needs_git
@needs_uv
def test_install_again_swaps_the_whole_copy(clone: Path, box: Box) -> None:
    (clone / "extra.txt").write_text("one\n", encoding="utf-8")
    git(clone, "add", "extra.txt")
    git(clone, "commit", "-q", "-m", "extra")
    assert pyt(clone, box, "install").returncode == 0
    first = _record(box.snapshot)
    assert (box.snapshot / "extra.txt").is_file()
    (box.snapshot / "stray.txt").write_text("written by hand\n", encoding="utf-8")
    git(clone, "rm", "-q", "extra.txt")
    git(clone, "commit", "-q", "-m", "no extra")
    (clone / "README.md").write_bytes(b"# py_template\n\nChanged, not committed.\n")
    r = pyt(clone, box, "install")
    assert r.returncode == 0, r.stderr
    assert f"it replaces the one of commit {first['commit'][:7]}" in r.stderr, r.stderr
    got = _files(box.snapshot)
    assert "extra.txt" not in got and "stray.txt" not in got  # the old copy went whole
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
    (clone / "extra.txt").write_text("x\n", encoding="utf-8")
    git(clone, "add", "extra.txt")
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
        (self.source / "a.txt").write_text("new a\n", encoding="utf-8")
        (self.source / "sub" / "b.txt").write_text("new b\n", encoding="utf-8")
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


def test_a_complete_install_replaces_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    swap = Swap(tmp_path, monkeypatch, fresh=False)
    cmd_install.install(swap.plan())
    assert _files(swap.snapshot) == {"a.txt": b"new a\n", "sub/b.txt": b"new b\n", cmd_install.RECORD: b'{\n  "schema": 1,\n  "commit": "new"\n}\n'}
    assert _files(swap.bin) == {"pyt": NEW_LAUNCHER, "pyt.cmd": NEW_LAUNCHER}
    assert sorted(p.name for p in swap.snapshot.parent.iterdir()) == ["template"]


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
    assert any("differ from the installed template's" in n for n in d.notes()), d.lines
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
