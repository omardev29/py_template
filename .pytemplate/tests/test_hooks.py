"""Tests for runner/hooks.py: the git pre-commit hook (script, install/uninstall/status, foreign
hooks, other projects of the same repository, core.hooksPath and husky, ignored projects, the git
environment of hooks, the staged-file mapping and which checks run on which content).

Every repository is a throwaway `git init` in tmp_path; tests that need git skip without it.
Git never reads the machine's or the user's configuration here (`_isolated_git`: a global
core.hooksPath, commit.gpgsign or init.templateDir would change what the tests see). The real
ruff and uv commands the hook builds run against this project's .venv in
test_real_ruff_accepts_the_hook_arguments (skipped without it).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_apply, cmd_dev, config, envs, hooks, lintc, mypyc, proc, render  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import IS_WINDOWS, ROOT, TEMPLATE  # noqa: E402
from runner.ui import PytError  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
GUARD = TEMPLATE / "tests" / "test_no_spanish.py"
# The variables of `_isolated_git` that the tests' own git commands keep
ISOLATION = ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CEILING_DIRECTORIES")


def make(data: dict[str, object] | None = None) -> Config:
    cfg: Config = config._build(Config, data or {}, "")
    return cfg


def git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m[1]), int(m[2])) if m else (0, 0)


def git_env() -> dict[str, str]:
    """The environment of the tests' own git commands: `_isolated_git`'s isolation, no other GIT_*."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in ISOLATION}
    env.setdefault("GIT_CONFIG_NOSYSTEM", "1")  # no core.autocrlf / core.hooksPath from the machine
    env.setdefault("GIT_CONFIG_GLOBAL", os.devnull)  # nor from ~/.gitconfig or the XDG one
    return env


def git(
    cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.autocrlf=false", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=check, env=env or git_env(),
        timeout=timeout,
    )


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The runner's git calls and the tests' own: no system config, no global config (a missing
    file reads as empty), no discovery above tmp_path, no repository variables."""
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))


def make_repo(tmp_path: Path, sub: str = "") -> tuple[Path, Path]:
    """Return (top, project): a git repository with the project in `sub`."""
    top = tmp_path / "repo"
    top.mkdir()
    git(top, "init", "-q")
    project = top / sub if sub else top
    project.mkdir(parents=True, exist_ok=True)
    return top, project


def find(project: Path, top: Path | None = None, environ: dict[str, str] | None = None) -> hooks.Repo:
    return hooks.find_repo(project, environ=environ or {}, cwd=top or project)


# --- the script ------------------------------------------------------------------------------------


def test_hook_script_is_ascii_lf_and_marked() -> None:
    text = hooks.hook_script("./pyt")
    assert text.isascii()
    assert "\r" not in text
    assert text.startswith("#!/bin/sh\n")
    assert hooks.MARKER in text
    assert "./pyt hooks uninstall" in text
    assert "git commit --no-verify" in text
    assert "_pt_launcher='./pyt'" in text
    assert "exec " not in text  # the script outlives the launcher: it explains a launcher failure
    assert '\nsh "$_pt_launcher" hooks run\n_pt_rc=$?\n' in text
    assert text.rstrip().endswith('exit "$_pt_rc"')
    assert hooks.LOCAL in text
    # as the pre-commit.local of another project's hook it must not run pre-commit.local (itself)
    assert f'[ "${{0##*/}}" != {hooks.LOCAL} ]' in text


@pytest.mark.parametrize(
    "launcher",
    ["./pyt", "./apps/my app/pyt", "./it's/pyt", "./caf\u00e9 50%/x\\y/pyt", "./\u65e5\u672c/'q'/pyt"],
)
def test_launcher_of_reads_back_every_launcher(launcher: str) -> None:
    assert hooks.launcher_of(hooks.hook_script(launcher)) == launcher


def test_launcher_of_rejects_what_it_does_not_know() -> None:
    assert hooks.launcher_of("#!/bin/sh\necho hi\n") is None
    assert hooks.launcher_of("_pt_launcher=./pyt\n") is None  # unquoted: never written by us
    assert hooks.launcher_of("_pt_launcher=\"$(printf '\\377')\"\n") is None  # not UTF-8


def test_hook_script_passes_shellcheck(tmp_path: Path) -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    files = []
    for i, launcher in enumerate(["./pyt", "./caf\u00e9 50%/it's/pyt"]):
        f = tmp_path / f"hook{i}.sh"
        f.write_bytes(hooks.hook_script(launcher).encode("ascii"))
        files.append(str(f))
    r = subprocess.run([shellcheck, "-s", "sh", *files], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_sh_literal() -> None:
    assert hooks.sh_literal("./a b/pyt") == "'./a b/pyt'"
    assert hooks.sh_literal("it's") == "'it'\\''s'"
    quoted = hooks.sh_literal("./caf\u00e9/pyt")
    assert quoted.isascii()
    assert quoted == "\"$(printf './caf\\303\\251/pyt')\""
    assert hooks.sh_literal("50%\\x\u00e9").isascii()


def posix_sh() -> str | None:
    if not IS_WINDOWS:
        return "/bin/sh" if Path("/bin/sh").is_file() else None
    from runner import shells

    for sh in shells.discover(distros=lambda wsl: []):
        if sh.name in ("git-sh", "msys2-dash", "git-dash"):
            return sh.argv[0]
    return None


def test_sh_literal_round_trip_in_sh() -> None:
    sh = posix_sh()
    if sh is None:
        pytest.skip("no POSIX sh")
    text = "./caf\u00e9 50%/it's \\n/pyt"
    r = subprocess.run([sh, "-c", f"printf '%s' {hooks.sh_literal(text)}"], capture_output=True, check=True)
    assert r.stdout.decode("utf-8") == text


@needs_git
def test_launcher_path_is_relative_to_the_top(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path, "apps/my app")
    repo = find(project, top)
    assert repo.prefix == "apps/my app"
    assert repo.launcher == "./apps/my app/pyt"
    assert "_pt_launcher='./apps/my app/pyt'" in hooks.hook_script(repo.launcher)
    assert hooks.run_line(repo) == "[ ! -f './apps/my app/pyt' ] || sh './apps/my app/pyt' hooks run || exit $?"
    assert find(top).launcher == "./pyt"
    assert hooks.run_line(find(top)) == "[ ! -f ./pyt ] || sh ./pyt hooks run || exit $?"


@needs_git
@pytest.mark.skipif(IS_WINDOWS, reason="a symlink stands for another spelling of the folder (Windows needs a privilege)")
def test_a_project_spelled_otherwise_than_git_spells_it_is_found(tmp_path: Path) -> None:
    """On macOS (case-insensitive APFS) ROOT keeps the case the user typed (`cd ~/projects/myapp`
    for MyApp: Path.resolve keeps it there), while git's top has the case on disk: the prefix,
    computed from the two texts, put the project outside its own work tree ("../myapp"), and
    every hook command failed. A symlink gives the same folder another spelling here. The prefix
    now comes from git (--show-prefix), and the file system says it names the project's folder."""
    top, project = make_repo(tmp_path, "apps/p")
    typed = tmp_path / "typed"
    typed.symlink_to(project, target_is_directory=True)
    repo = find(typed, top)
    assert repo.prefix == "apps/p" and repo.launcher == "./apps/p/pyt"
    (project / "src").mkdir()
    (project / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(top, "add", "-A")
    assert hooks.staged_files(repo) == ["src/a.py"]


@needs_git
def test_staged_paths_fold_case_where_git_says_the_disk_does(tmp_path: Path) -> None:
    """core.ignorecase (git sets it on macOS's default APFS, and on Windows): a staged path spelled
    otherwise than the project's prefix (Apps/P/x.py for apps/p) was dropped on macOS, where
    project_paths compared case-sensitively, and `hooks run` said there was nothing to check."""
    top, project = make_repo(tmp_path, "apps/p")
    source = tmp_path / "x.py"
    source.write_text("x = 1\n", encoding="utf-8")
    blob = git(top, "hash-object", "-w", str(source)).stdout.strip()
    git(top, "update-index", "--add", "--cacheinfo", f"100644,{blob},Apps/P/x.py")
    git(top, "config", "core.ignorecase", "true")
    repo = find(project, top)
    assert hooks.staged_files(repo) == ["x.py"] and repo.ignore_case
    if not IS_WINDOWS:  # a case-sensitive disk: Apps/P is another folder
        git(top, "config", "core.ignorecase", "false")
        assert hooks.staged_files(find(project, top)) == []


def test_not_a_git_work_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("git") is None:
        with pytest.raises(hooks.NotInGit) as e:
            find(tmp_path)
        assert e.value.code == 3
        return
    # git's messages are read in English whatever the user's locale (the runner sets LC_ALL=C)
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANGUAGE", "de")
    with pytest.raises(hooks.NotInGit, match="not inside a git work tree") as e:
        find(tmp_path)
    assert e.value.code == 2


def test_find_repo_reports_other_git_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """dubious ownership (a repository of another user: other drives on Windows, /mnt/c in WSL,
    network shares) is git's own error, never "not inside a git work tree (git init first)"."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    said = "fatal: detected dubious ownership in repository at '/x'\nTo add an exception for this directory, call:"

    def dubious(args: Sequence[str], cwd: Path, env: object, **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["git", *args], 128, "", said)

    monkeypatch.setattr(hooks, "_git", dubious)
    with pytest.raises(PytError) as e:
        find(tmp_path)
    assert not isinstance(e.value, hooks.NotInGit)
    assert "dubious ownership" in str(e.value) and "git init" not in str(e.value)
    hooks.ensure_installed(make(), tmp_path)  # setup says why the hook was not installed
    assert "dubious ownership" in capsys.readouterr().err
    lines: list[tuple[bool | None, str, str]] = []
    hooks.doctor(make(), lambda passed, label, hint="": lines.append((passed, label, hint)), tmp_path)
    assert len(lines) == 1 and lines[0][0] is None and "dubious ownership" in lines[0][2]
    with pytest.raises(PytError, match="dubious ownership"):
        hooks.show_status(make(), tmp_path)


# --- install / uninstall / status --------------------------------------------------------------------


@needs_git
def test_install_update_uninstall(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    repo = find(project)
    target = repo.default_dir / hooks.HOOK
    assert not repo.custom_hooks_path
    assert hooks.classify(target, repo) == "missing"
    assert "installed" in hooks.install(repo)
    data = target.read_bytes()
    assert data == hooks.hook_script("./pyt").encode("ascii")
    if not IS_WINDOWS:
        assert target.stat().st_mode & 0o777 == 0o755
    assert hooks.classify(target, repo) == "installed"
    assert "already installed" in hooks.install(repo)
    target.write_text(f"#!/bin/sh\n# {hooks.MARKER}\nexec sh ./old/pyt hooks run\n", encoding="utf-8")
    assert hooks.classify(target, repo) == "outdated"
    assert "updated" in hooks.install(repo)
    assert target.read_bytes() == data
    assert "removed" in hooks.uninstall(repo)
    assert not target.exists()
    assert hooks.uninstall(repo) == "no pytemplate pre-commit hook installed"


@needs_git
def test_install_creates_a_missing_hooks_dir(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    shutil.rmtree(top / ".git" / "hooks")
    repo = find(project)
    hooks.install(repo)
    assert (top / ".git" / "hooks" / hooks.HOOK).is_file()


@needs_git
def test_foreign_hook_is_preserved_and_chained(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    repo = find(project)
    target = repo.default_dir / hooks.HOOK
    local = repo.default_dir / hooks.LOCAL
    foreign = b"#!/bin/sh\necho mine\n"
    target.write_bytes(foreign)
    assert hooks.classify(target, repo) == "foreign"
    with pytest.raises(PytError, match="--force"):
        hooks.install(repo)
    assert target.read_bytes() == foreign
    msg = hooks.install(repo, force=True)
    assert hooks.LOCAL in msg
    assert local.read_bytes() == foreign
    assert hooks.classify(target, repo) == "installed"
    passed, label, _ = hooks._status_line(make(), repo)
    assert passed is True and hooks.LOCAL in label
    assert "restored" in hooks.uninstall(repo)
    assert target.read_bytes() == foreign
    assert not local.exists()
    # both files there already: never overwrite either
    local.write_bytes(b"#!/bin/sh\necho older\n")
    with pytest.raises(PytError, match="merge them by hand"):
        hooks.install(repo, force=True)
    assert target.read_bytes() == foreign


@needs_git
def test_a_hook_that_already_calls_hooks_run(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    repo = find(project)
    target = repo.default_dir / hooks.HOOK
    target.write_text("#!/bin/sh\nnpm test || exit 1\nsh ./pyt hooks run\n", encoding="utf-8")
    assert hooks.classify(target, repo) == "calls"
    assert "already runs" in hooks.install(repo)
    assert "left alone" in hooks.uninstall(repo)
    assert target.is_file()


@needs_git
@pytest.mark.skipif(IS_WINDOWS, reason="symlinks need Developer Mode on Windows")
def test_symlinked_hook_is_never_written_through(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A team symlinks .git/hooks/pre-commit to a tracked script; on a branch without it the link
    dangles. Neither setup nor install may write through it (an untracked file in the work tree,
    a checkout that then refuses to switch branches)."""
    top, project = make_repo(tmp_path)
    repo = find(project)
    target = repo.default_dir / hooks.HOOK
    local = repo.default_dir / hooks.LOCAL
    (top / "tools" / "hooks").mkdir(parents=True)
    link = "../../tools/hooks/pre-commit"
    target.symlink_to(link)
    assert hooks.classify(target, repo) == "foreign"
    hooks.ensure_installed(make(), project)  # the ./pyt setup path leaves it alone
    assert "another tool's hook" in capsys.readouterr().err
    with pytest.raises(PytError, match="--force"):
        hooks.install(repo)
    assert list((top / "tools" / "hooks").iterdir()) == []
    assert target.is_symlink() and os.readlink(target) == link
    hooks.install(repo, force=True)
    assert list((top / "tools" / "hooks").iterdir()) == []
    assert not target.is_symlink() and hooks.classify(target, repo) == "installed"
    assert local.is_symlink() and os.readlink(local) == link  # moved as a link
    assert "restored" in hooks.uninstall(repo)
    assert target.is_symlink() and os.readlink(target) == link and not os.path.lexists(local)
    # a dangling pre-commit.local is somebody's too: never overwritten
    (repo.default_dir / hooks.LOCAL).symlink_to("../../nowhere")
    target.unlink()
    target.write_bytes(b"#!/bin/sh\necho mine\n")
    with pytest.raises(PytError, match="merge them by hand"):
        hooks.install(repo, force=True)
    (repo.default_dir / hooks.LOCAL).unlink()
    target.unlink()
    target.symlink_to(link)
    # the link's folder missing too: an error with the --force hint, no traceback, nothing written
    shutil.rmtree(top / "tools")
    with pytest.raises(PytError, match="--force"):
        hooks.install(repo)
    # a live link to a script that runs `hooks run` (even one with the MARKER) is never rewritten
    (top / "tools" / "hooks").mkdir(parents=True)
    shared = top / "tools" / "hooks" / "pre-commit"
    shared.write_text(f"#!/bin/sh\n# {hooks.MARKER}\nexec sh ./pyt hooks run\n", encoding="utf-8")
    before = shared.read_bytes()
    assert hooks.classify(target, repo) == "calls"
    hooks.install(repo)
    hooks.ensure_installed(make(), project)
    assert shared.read_bytes() == before and target.is_symlink()
    # ...nor one that runs another launcher's checks: not this project's, and still not written through
    shared.write_text(f"#!/bin/sh\n# {hooks.MARKER}\nexec sh ./old/pyt hooks run\n", encoding="utf-8")
    before = shared.read_bytes()
    assert hooks.classify(target, repo) == "foreign"
    with pytest.raises(PytError, match="--force"):
        hooks.install(repo)
    hooks.ensure_installed(make(), project)
    assert shared.read_bytes() == before and target.is_symlink()


@needs_git
def test_core_hooks_path_is_respected(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path, "proj")
    git(top, "config", "core.hooksPath", "hk")
    repo = find(project, top)
    assert repo.custom_hooks_path
    assert repo.hooks_dir == Path(os.path.normpath(top / "hk"))
    with pytest.raises(PytError) as e:
        hooks.install(repo, force=True)
    assert "core.hooksPath = 'hk'" in str(e.value)
    assert "sh ./proj/pyt hooks run || exit $?" in str(e.value)
    assert not (repo.default_dir / hooks.HOOK).exists()
    assert not (top / "hk").exists()
    passed, label, hint = hooks._status_line(make(), repo)
    assert passed is None and "core.hooksPath" in label and "sh ./proj/pyt hooks run" in hint
    (top / "hk").mkdir()
    (top / "hk" / hooks.HOOK).write_text("#!/bin/sh\nsh ./proj/pyt hooks run || exit $?\n", encoding="utf-8")
    assert "already runs" in hooks.install(repo)
    assert hooks._status_line(make(), repo)[0] is True
    # core.hooksPath naming the default folder is not a custom one
    git(top, "config", "core.hooksPath", ".git/hooks")
    assert not find(project, top).custom_hooks_path


@needs_git
def test_core_hooks_path_husky_layout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """husky 9: core.hooksPath=.husky/_, whose generated pre-commit sources `h`, which runs the
    user's .husky/pre-commit. That file is the one to check and to name in the hint."""
    top, project = make_repo(tmp_path)
    git(top, "config", "core.hooksPath", ".husky/_")
    husky = top / ".husky"
    (husky / "_").mkdir(parents=True)
    (husky / "_" / "h").write_text('#!/usr/bin/env sh\nn=$(basename "$0")\nsh -e "$(dirname "$(dirname "$0")")/$n" "$@"\n', encoding="utf-8")
    (husky / "_" / hooks.HOOK).write_text('#!/usr/bin/env sh\n. "$(dirname "$0")/h"\n', encoding="utf-8")
    repo = find(project)
    assert repo.custom_hooks_path
    passed, label, hint = hooks._status_line(make(), repo)
    assert passed is None and ".husky/pre-commit" in label + hint and ".husky/_/pre-commit" not in label + hint
    assert "husky runs it" in hint
    hooks.ensure_installed(make(), project)
    assert "core.hooksPath is set" in capsys.readouterr().err
    (husky / hooks.HOOK).write_text("npm test\nsh ./pyt hooks run || exit $?\n", encoding="utf-8")
    assert hooks._status_line(make(), repo)[0] is True
    assert "already runs" in hooks.install(repo)
    hooks.ensure_installed(make(), project)  # the checks run: nothing to say
    assert capsys.readouterr().err == ""
    # without husky's `h`, .husky/_ is a plain hooks folder: its own pre-commit is the one
    (husky / "_" / "h").unlink()
    assert hooks._status_line(make(), repo)[0] is None


@needs_git
def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    top, project = make_repo(tmp_path)
    repo = find(project)
    target = repo.default_dir / hooks.HOOK
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert "would be installed" in hooks.install(repo)
    assert not target.exists()
    monkeypatch.setattr(proc, "DRY_RUN", False)
    hooks.install(repo)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert "would remove" in hooks.uninstall(repo)
    assert target.is_file()


@needs_git
def test_ensure_installed(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    top, project = make_repo(tmp_path)
    target = top / ".git" / "hooks" / hooks.HOOK
    hooks.ensure_installed(make({"hooks": {"pre_commit": False}}), project)
    assert not target.exists()
    hooks.ensure_installed(make(), project)
    assert hooks.MARKER in target.read_text(encoding="utf-8")
    assert "installed" in capsys.readouterr().err
    hooks.ensure_installed(make(), project)  # already there: silent
    assert capsys.readouterr().err == ""
    # this project's hook from an older template version: updated in place
    target.write_text(f"#!/bin/sh\n# {hooks.MARKER}\nexec sh ./pyt hooks run\n", encoding="utf-8")
    hooks.ensure_installed(make(), project)
    assert target.read_bytes() == hooks.hook_script("./pyt").encode("ascii")
    assert "updated" in capsys.readouterr().err
    target.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    hooks.ensure_installed(make(), project)
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert "another tool's hook" in capsys.readouterr().err
    target.unlink()

    def broken(repo: hooks.Repo, *, force: bool = False) -> str:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(hooks, "install", broken)
    hooks.ensure_installed(make(), project)  # never fails setup: one warning
    assert "not installed" in capsys.readouterr().err and not target.exists()
    outside = tmp_path / "plain"
    outside.mkdir()
    hooks.ensure_installed(make(), outside)  # not a git work tree: nothing, no error
    assert capsys.readouterr().err == ""


@needs_git
def test_ensure_installed_skips_an_ignored_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A project unzipped inside a repository that ignores it (a dotfiles repo with `*`): its
    commits never contain the project, so no hook (unless forced)."""
    top, project = make_repo(tmp_path, "code/p")
    (top / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    (project / "pyt").write_text("#!/bin/sh\n", encoding="utf-8")
    target = top / ".git" / "hooks" / hooks.HOOK
    repo = find(project, top)
    assert repo.ignored()
    hooks.ensure_installed(make(), project)
    assert not target.exists()
    assert "ignores this project" in capsys.readouterr().err
    with pytest.raises(PytError, match="ignores this project"):
        hooks.install(repo)
    passed, label, hint = hooks._status_line(make(), repo)
    assert passed is None and "ignores this project" in label and "git init" in hint
    # another project's hook there (the enclosing repository's own): still "ignored" is the news
    (top / "pyt").write_text("#!/bin/sh\n", encoding="utf-8")
    target.write_bytes(hooks.hook_script("./pyt").encode("ascii"))
    assert hooks.classify(target, repo) == "other"
    hooks.ensure_installed(make(), project)
    err = capsys.readouterr().err
    assert "ignores this project" in err and "another project" not in err
    assert "ignores this project" in hooks._status_line(make(), repo)[1]
    target.unlink()
    (top / "pyt").unlink()
    assert "installed" in hooks.install(repo, force=True)
    assert hooks._status_line(make(), repo)[0] is True
    hooks.ensure_installed(make(), project)  # installed on purpose: nothing to say
    assert capsys.readouterr().err == ""
    # tracked files are never "ignored", whatever the patterns say; nor is a repository's own top
    git(top, "add", "-f", "code/p/pyt")
    assert not find(project, top).ignored()
    (top / "pyt").write_text("#!/bin/sh\n", encoding="utf-8")
    assert not find(top).ignored()


@needs_git
def test_doctor_lines(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    lines: list[tuple[bool | None, str, str]] = []

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        lines.append((passed, label, hint))

    hooks.doctor(make(), check, project)
    assert lines[-1][0] is None and "not installed" in lines[-1][1] and lines[-1][2].startswith("./pyt hooks install")
    hooks.install(find(project))
    hooks.doctor(make(), check, project)
    assert lines[-1][0] is True
    target = top / ".git" / "hooks" / hooks.HOOK
    target.write_text(f"#!/bin/sh\n# {hooks.MARKER}\nexec sh ./pyt hooks run\n", encoding="utf-8")
    hooks.doctor(make(), check, project)  # an older template version's hook: setup updates it
    assert lines[-1][0] is None and "outdated" in lines[-1][1] and lines[-1][2] == "./pyt hooks install"
    target.write_text("#!/bin/sh\nnpm test\nsh ./pyt hooks run || exit $?\n", encoding="utf-8")
    hooks.doctor(make(), check, project)
    assert lines[-1][0] is True and "runs ./pyt hooks run" in lines[-1][1]
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hooks.doctor(make(), check, project)
    assert lines[-1][0] is None and "another tool" in lines[-1][1]
    count = len(lines)
    outside = tmp_path / "plain"
    outside.mkdir()
    hooks.doctor(make(), check, outside)
    assert len(lines) == count


@needs_git
def test_doctor_notes_a_generated_ci_that_github_never_runs(tmp_path: Path) -> None:
    """A project moved or cloned into a bigger repository (new warns only when it creates one
    there): GitHub reads <top>/.github/workflows only, so the project's generated ci.yml never runs."""
    lines: list[tuple[bool | None, str, str]] = []

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        lines.append((passed, label, hint))

    top, project = make_repo(tmp_path, "apps/p")
    hooks.doctor(make(), check, project)
    assert not [ln for ln in lines if "ci.yml" in ln[1]]  # no generated CI (no templates/ci.yml)
    (project / ".github" / "workflows").mkdir(parents=True)
    (project / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    hooks.doctor(make(), check, project)
    [note] = [ln for ln in lines if "ci.yml" in ln[1]]
    assert note[0] is None  # a note, never a problem: the setup is supported
    assert "apps/p/.github/workflows/ci.yml" in note[1] and "never runs" in note[1]
    assert "working-directory" in note[2] and "apps/p" in note[2]
    lines.clear()
    # A repository that ignores the project never holds it: no workflow of it would run the
    # project's steps, and the hook line says to give it a repository of its own
    (top / ".gitignore").write_text("apps/\n", encoding="utf-8")
    hooks.doctor(make(), check, project)
    assert not [ln for ln in lines if "ci.yml" in ln[1]], lines
    assert "ignores this project" in lines[-1][1] and "git init the project" in lines[-1][2]
    lines.clear()
    (tmp_path / "alone").mkdir()
    _, own = make_repo(tmp_path / "alone")  # the project is the repository: its CI runs
    (own / ".github" / "workflows").mkdir(parents=True)
    (own / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    hooks.doctor(make(), check, own)
    assert not [ln for ln in lines if "ci.yml" in ln[1]]


FAKE_LAUNCHER = """#!/bin/sh
if [ -f .topmark ]; then _w=top; else _w=elsewhere; fi
printf '%s\\n' "launcher $* from $_w" >> "$PT_HOOK_LOG"
exit "${PT_HOOK_EXIT:-0}"
"""
NAMED_LAUNCHER = """#!/bin/sh
printf '%s\\n' "$0 $*" >> "$PT_HOOK_LOG"
exit 0
"""
LOCAL_HOOK = """#!/bin/sh
printf '%s\\n' "local hook" >> "$PT_HOOK_LOG"
exit "${PT_LOCAL_EXIT:-0}"
"""


@needs_git
def test_second_project_does_not_steal_the_hook(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two pytemplate projects in one repository (apps/p and apps/q): setup in q must not replace
    p's hook (and back again on p's next setup); --force chains both, each runs once."""
    top, p = make_repo(tmp_path, "apps/p")
    q = top / "apps" / "q"
    q.mkdir(parents=True)
    for project in (p, q):
        (project / "pyt").write_bytes(NAMED_LAUNCHER.encode("ascii"))
    rp, rq = find(p, top), find(q, top)
    target = rp.default_dir / hooks.HOOK
    local = rp.default_dir / hooks.LOCAL
    assert "installed" in hooks.install(rp)
    p_hook = target.read_bytes()
    assert hooks.classify(target, rp) == "installed"
    assert hooks.classify(target, rq) == "other"
    hooks.ensure_installed(make(), q)  # q's ./pyt setup
    assert target.read_bytes() == p_hook
    assert "another project" in capsys.readouterr().err
    with pytest.raises(PytError, match="--force"):
        hooks.install(rq)
    assert "left alone" in hooks.uninstall(rq) and target.read_bytes() == p_hook
    passed, label, hint = hooks._status_line(make(), rq)
    assert passed is None and "./apps/p/pyt" in label and "--force" in hint
    # --force: q's hook runs a fresh copy of p's first (which never chains itself)
    hooks.install(rq, force=True)
    assert hooks.classify(target, rq) == "installed"
    assert local.read_bytes() == hooks.hook_script("./apps/p/pyt").encode("ascii")
    assert hooks._status_line(make(), rp)[0] is True  # p's checks still run, from pre-commit.local
    capsys.readouterr()
    hooks.ensure_installed(make(), p)  # p's setup: nothing to change, nothing to say
    assert capsys.readouterr().err == "" and hooks.classify(target, rq) == "installed"
    log = tmp_path / "hook.log"
    env = dict(git_env(), PT_HOOK_LOG=log.as_posix())
    (p / "a.txt").write_text("a\n", encoding="utf-8")
    git(top, "add", "-A", env=env)
    assert git(top, "commit", "-q", "-m", "one", env=env, check=False, timeout=120).returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["./apps/p/pyt hooks run", "./apps/q/pyt hooks run"]
    # uninstall in q gives p its hook back
    assert "restored" in hooks.uninstall(rq)
    assert target.read_bytes() == p_hook and not local.exists()
    # a hook whose project is gone is stale, not another project's: replaced without --force
    shutil.rmtree(p)
    assert hooks.classify(target, rq) == "outdated"
    assert "updated" in hooks.install(rq)


def _commit_log(top: Path, tmp_path: Path, name: str) -> list[str]:
    """Commit a new file through the hooks; return what the launchers logged."""
    log = tmp_path / "hook.log"
    log.unlink(missing_ok=True)
    env = dict(git_env(), PT_HOOK_LOG=log.as_posix())
    (top / name).write_text("x\n", encoding="utf-8")
    git(top, "add", "-A", env=env)
    assert git(top, "commit", "-q", "-m", name, env=env, check=False, timeout=120).returncode == 0
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@needs_git
def test_a_chained_hook_stays_this_projects(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """q's `install --force` chains p's hook as pre-commit.local: that copy is still p's. p's
    uninstall (and apply with hooks.pre_commit = false) removes it; once q's hook goes, p's
    install drops the copy instead of running p's checks twice."""
    top, p = make_repo(tmp_path, "apps/p")
    q = top / "apps" / "q"
    q.mkdir(parents=True)
    for project in (p, q):
        (project / "pyt").write_bytes(NAMED_LAUNCHER.encode("ascii"))
    rp, rq = find(p, top), find(q, top)
    target, local = rp.default_dir / hooks.HOOK, rp.default_dir / hooks.LOCAL
    hooks.install(rp)
    hooks.install(rq, force=True)
    q_hook = target.read_bytes()
    assert (hooks.hook_state(rp), hooks.own_local(rp)) == ("chained", True)
    assert (hooks.hook_state(rq), hooks.own_local(rq)) == ("installed", False)
    # p's checks already run: install says so, with or without --force (never "merge them by hand")
    for force in (False, True):
        assert "already runs this project's checks" in hooks.install(rp, force=force)
    assert target.read_bytes() == q_hook and hooks.own_local(rp)
    # p's uninstall removes p's copy and leaves q's hook alone
    msg = hooks.uninstall(rp)
    assert "removed" in msg and hooks.LOCAL in msg and "left alone" in msg
    assert not local.exists() and target.read_bytes() == q_hook
    assert _commit_log(top, tmp_path, "one.txt") == ["./apps/q/pyt hooks run"]
    # chained again, then q goes away: p's install replaces q's stale hook and drops the copy
    hooks.uninstall(rq)
    hooks.install(rp)
    hooks.install(rq, force=True)
    assert hooks.hook_state(rp) == "chained"
    shutil.rmtree(q)
    assert hooks.classify(target, rp) == "outdated"
    capsys.readouterr()
    hooks.ensure_installed(make(), p)
    assert "a copy of this project's hook" in capsys.readouterr().err
    assert target.read_bytes() == hooks.hook_script("./apps/p/pyt").encode("ascii") and not local.exists()
    assert _commit_log(top, tmp_path, "two.txt") == ["./apps/p/pyt hooks run"]  # once, not twice
    # a project left in that state by an older runner: status says so, install and setup repair it
    local.write_bytes(hooks.hook_script("./apps/p/pyt").encode("ascii"))
    passed, label, hint = hooks._status_line(make(), rp)
    assert passed is None and "run twice" in label and hint == "./pyt hooks install"
    hooks.ensure_installed(make(), p)
    assert not local.exists() and hooks._status_line(make(), rp)[0] is True
    # uninstall never restores this project's own copy as pre-commit (it would run with pre_commit = false)
    local.write_bytes(hooks.hook_script("./apps/p/pyt").encode("ascii"))
    assert "a copy of this project's hook" in hooks.uninstall(rp)
    assert not target.exists() and not local.exists()


@needs_git
@pytest.mark.parametrize("sub", ["", "apps/p"])
@pytest.mark.parametrize("old_launcher_left", [False, True])
def test_a_hook_that_calls_the_launcher_by_its_old_name_is_updated(tmp_path: Path, capsys: pytest.CaptureFixture[str], sub: str, old_launcher_left: bool) -> None:
    """The hook of a project made before the launchers were renamed calls `deploy`: it is this
    project's outdated hook, whether that file is gone or still next to `pyt` (it was taken for
    another project's, left alone, and the checks were skipped), and setup rewrites it."""
    top, project = make_repo(tmp_path, sub)
    (project / "pyt").write_bytes(NAMED_LAUNCHER.encode("ascii"))
    if old_launcher_left:
        (project / "deploy").write_bytes(NAMED_LAUNCHER.encode("ascii"))
    repo = find(project, top)
    target = repo.default_dir / hooks.HOOK
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(hooks.hook_script(f"./{sub}/deploy" if sub else "./deploy").encode("ascii"))
    assert hooks.hook_state(repo) == "outdated"
    passed, label, hint = hooks._status_line(make(), repo)
    assert passed is None and "outdated" in label and hint == "./pyt hooks install"
    capsys.readouterr()
    hooks.ensure_installed(make(), project)  # ./pyt setup
    assert "pre-commit hook updated" in capsys.readouterr().err
    assert target.read_bytes() == hooks.hook_script(repo.launcher).encode("ascii")
    assert _commit_log(top, tmp_path, "one.txt") == [f"{repo.launcher} hooks run"]


@needs_git
def test_a_chained_copy_that_calls_the_launcher_by_its_old_name_is_updated(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """p's hook, chained after q's by a runner from before the rename, calls ./apps/p/deploy: it
    is still p's own copy, reported outdated (its checks are skipped), and p's setup brings it
    up to date in place: q's hook stays, and each project's checks run once."""
    top, p = make_repo(tmp_path, "apps/p")
    q = top / "apps" / "q"
    q.mkdir(parents=True)
    for project in (p, q):
        (project / "pyt").write_bytes(NAMED_LAUNCHER.encode("ascii"))
    rp, rq = find(p, top), find(q, top)
    target, local = rp.default_dir / hooks.HOOK, rp.default_dir / hooks.LOCAL
    hooks.install(rq)
    q_hook = target.read_bytes()
    old_copy = hooks.hook_script("./apps/p/deploy").encode("ascii")
    local.write_bytes(old_copy)
    if not IS_WINDOWS:
        local.chmod(0o755)
    assert (hooks.hook_state(rp), hooks.own_local(rp), hooks.hook_state(rq)) == ("chained", True, "installed")
    passed, label, hint = hooks._status_line(make(), rp)
    assert passed is None and "outdated" in label and hint == "./pyt hooks install"
    assert _commit_log(top, tmp_path, "one.txt") == ["./apps/q/pyt hooks run"]  # p's checks skipped
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert "would be updated" in hooks.install(rp)
    assert local.read_bytes() == old_copy
    monkeypatch.setattr(proc, "DRY_RUN", False)
    capsys.readouterr()
    hooks.ensure_installed(make(), p)  # p's ./pyt setup
    assert "pre-commit hook updated" in capsys.readouterr().err
    assert local.read_bytes() == hooks.hook_script("./apps/p/pyt").encode("ascii") and target.read_bytes() == q_hook
    assert hooks._status_line(make(), rp)[0] is True
    assert "already runs this project's checks" in hooks.install(rp)
    assert _commit_log(top, tmp_path, "two.txt") == ["./apps/p/pyt hooks run", "./apps/q/pyt hooks run"]


@needs_git
def test_another_projects_hook_by_the_old_launcher_name_is_left_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """In a monorepo, p was made before the launchers were renamed (its hook calls
    ./apps/p/deploy) and upgraded in place (./apps/p/pyt now): q's setup took that hook for a
    gone project's and replaced it, and p's then said its checks were another project's. q
    leaves it alone; p brings it up to date itself."""
    top, p = make_repo(tmp_path, "apps/p")
    q = top / "apps" / "q"
    q.mkdir(parents=True)
    for project in (p, q):
        (project / "pyt").write_bytes(NAMED_LAUNCHER.encode("ascii"))
        (project / ".pytemplate").mkdir()
        (project / ".pytemplate" / "pyt.py").write_bytes(b"")
    rp, rq = find(p, top), find(q, top)
    target = rp.default_dir / hooks.HOOK
    target.parent.mkdir(parents=True, exist_ok=True)
    old = hooks.hook_script("./apps/p/deploy").encode("ascii")
    target.write_bytes(old)
    assert (hooks.hook_state(rq), hooks.hook_state(rp)) == ("other", "outdated")
    capsys.readouterr()
    hooks.ensure_installed(make(), q)  # q's ./pyt setup
    assert target.read_bytes() == old and "pre-commit hook updated" not in capsys.readouterr().err
    hooks.ensure_installed(make(), p)  # p's
    assert target.read_bytes() == hooks.hook_script("./apps/p/pyt").encode("ascii")
    (p / ".pytemplate" / "pyt.py").unlink()  # a project that is gone: its stale hook may be replaced
    (p / "pyt").unlink()
    assert hooks.hook_state(rq) != "other"


@needs_git
def test_a_third_project_is_not_told_to_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """pre-commit and pre-commit.local both taken: install --force would fail ("merge them by
    hand"), so no message suggests it."""
    top, p = make_repo(tmp_path, "apps/p")
    others = [top / "apps" / n for n in ("q", "r")]
    for project in (p, *others):
        project.mkdir(parents=True, exist_ok=True)
        (project / "pyt").write_bytes(NAMED_LAUNCHER.encode("ascii"))
    rp, rq, rr = find(p, top), *(find(o, top) for o in others)
    hooks.install(rq)
    hooks.install(rr, force=True)  # r on top, q's copy as pre-commit.local
    passed, label, hint = hooks._status_line(make(), rp)
    assert passed is None and "./apps/r/pyt" in label
    assert "exists as well" in hint and "merge the two by hand" in hint
    with pytest.raises(PytError, match="exists as well") as e:
        hooks.install(rp)
    assert "keeps it as" not in str(e.value)
    with pytest.raises(PytError, match="merge them by hand"):
        hooks.install(rp, force=True)
    hooks.ensure_installed(make(), p)
    err = capsys.readouterr().err
    assert "left alone" in err and "is taken too" in err and "--force runs both" not in err


@needs_git
def test_calls_means_this_projects_launcher_on_a_live_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A hook pytemplate does not manage runs this project's checks only with a line that is not
    a comment and names this project's launcher: another project's line, or a commented-out one,
    leaves the checks out (status, doctor and install say so)."""
    top, a = make_repo(tmp_path, "apps/a")
    b = top / "apps" / "b"
    b.mkdir(parents=True)
    for project in (a, b):
        (project / "pyt").write_bytes(NAMED_LAUNCHER.encode("ascii"))
    git(top, "config", "core.hooksPath", "hk")
    (top / "hk").mkdir()
    shared = top / "hk" / hooks.HOOK
    ra = find(a, top)
    not_ours = [
        "sh ./apps/b/pyt hooks run || exit $?",
        "# sh ./apps/a/pyt hooks run || exit $?\nexit 0",
        "  # TODO: sh ./apps/a/pyt hooks run",
        "sh ./apps/a/pyt hooks runner",
        "sh ./apps/a/mypyt hooks run",
        "sh ./pyt hooks run",  # the top's launcher: not this project's
        "cd apps/b && ./pyt hooks run",  # b's, reached through a cd
        "cd apps/a && ../b/pyt hooks run",
        "(cd apps/a)\n./pyt hooks run",  # the cd stayed in its subshell
        "(cd apps/a)\n(./pyt hooks run)",  # ...and the next subshell starts at the top again
        "sh ./apps/a/pyt -q status hooks run",  # not the hooks command
        "echo 'sh ./apps/a/pyt hooks run'",  # a string, not a call
        "sh ./apps/a/pyt hooks run-all",
        # a mention of the launcher in another command's words: that program runs, not ./pyt
        "npx lint-staged\necho Tip: also run ./apps/a/pyt hooks run",
        "printf '%s\\n' ./apps/a/pyt hooks run",
        "echo -n sh ./apps/a/pyt hooks run",
        "git notes add -m ./apps/a/pyt hooks run",
    ]
    for body in not_ours:
        shared.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        assert hooks.classify(shared, ra) == "foreign", body
        passed, label, hint = hooks._status_line(make(), ra)
        assert passed is None and "not in" in label and "sh ./apps/a/pyt hooks run || exit $?" in hint, body
        with pytest.raises(PytError, match="core.hooksPath"):
            hooks.install(ra)
    ours = [
        hooks.run_line(ra),
        "npm test && sh ./apps/a/pyt hooks run",
        'sh "./apps/a/pyt" hooks run',
        "sh apps/a/pyt hooks run",
        f'sh "{(a / "pyt").as_posix()}" hooks run',
        'sh "$(git rev-parse --show-toplevel)/apps/a/pyt" hooks run',  # cannot tell: counts
        "sh ./apps/b/pyt hooks run || exit $?\nsh ./apps/a/pyt hooks run || exit $?",
        # global options go before the command (./pyt -q hooks run)
        "sh ./apps/a/pyt -q hooks run || exit $?",
        "sh ./apps/a/pyt --verbose --no-render hooks run",
        # a word made of an expansion and a path is one word, and it cannot be resolved: counts
        'sh "$(git rev-parse --show-toplevel)"/apps/a/pyt hooks run',
        'sh "$ROOT"/apps/a/pyt hooks run',
        "sh $(git rev-parse --show-toplevel)/apps/a/pyt hooks run",
        "sh `git rev-parse --show-toplevel`/apps/a/pyt hooks run",
        # a relative launcher after a cd: resolved from there (git runs hooks from the top)
        "cd apps/a && ./pyt hooks run",
        "cd apps\ncd a\nsh pyt hooks run",
        "cd ./apps/b && cd ../a && sh ./pyt hooks run",
        '(cd apps/b && ./pyt hooks run)\n./apps/a/pyt hooks run',  # a subshell's cd stays there
        'cd "$(dirname "$0")/../apps/a" && ./pyt hooks run',  # cannot be resolved: counts
        "{ cd apps/a; ./pyt hooks run; }",  # a group is no subshell: its cd holds
        "if cd apps/a; then sh ./pyt -q hooks run; fi",
        "exec sh ./apps/a/pyt hooks run",
        "sh ./apps/a/pyt hooks run; status=$?",
        # what may run the launcher: a shell with its options, env, assignments, keywords
        "/bin/bash -e ./apps/a/pyt hooks run",
        "env PT_X=1 sh ./apps/a/pyt hooks run",
        "PT_X=1 ./apps/a/pyt hooks run",
        "! ./apps/a/pyt hooks run && exit 1",
        "command sh ./apps/a/pyt hooks run",
    ]
    for body in ours:
        shared.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        assert hooks.classify(shared, ra) == "calls", body
        assert hooks._status_line(make(), ra)[0] is True, body
        assert "already runs" in hooks.install(ra)
    # the same rule in the default hooks folder: another project's line is somebody else's hook
    git(top, "config", "--unset", "core.hooksPath")
    ra = find(a, top)
    target = ra.default_dir / hooks.HOOK
    target.write_text("#!/bin/sh\nsh ./apps/b/pyt hooks run || exit $?\n", encoding="utf-8")
    if not IS_WINDOWS:
        target.chmod(0o755)  # a hook of the user's is executable
    assert hooks.classify(target, ra) == "foreign"
    hooks.install(ra, force=True)  # chained: both run
    assert hooks.classify(target, ra) == "installed" and (ra.default_dir / hooks.LOCAL).is_file()
    assert _commit_log(top, tmp_path, "c.txt") == ["./apps/b/pyt hooks run", "./apps/a/pyt hooks run"]


@needs_git
def test_a_global_hooks_path_line_skips_the_repositories_without_the_launcher(tmp_path: Path) -> None:
    """A core.hooksPath shared by every repository (a global one): the advised line runs in each
    of them, and unguarded (`sh ./pyt hooks run || exit $?`) it failed every commit of the
    others ("cannot open ./pyt"). It skips a checkout without the launcher, and still runs
    this project's checks."""
    ghooks = tmp_path / "ghooks"
    ghooks.mkdir()
    Path(os.environ["GIT_CONFIG_GLOBAL"]).write_text(f"[core]\n\thooksPath = {ghooks.as_posix()}\n", encoding="utf-8")
    top, project = make_repo(tmp_path)
    (project / "pyt").write_bytes(NAMED_LAUNCHER.encode("ascii"))
    repo = find(project, top)
    assert repo.custom_hooks_path
    passed, _, hint = hooks._status_line(make(), repo)
    assert passed is None and hooks.run_line(repo) in hint and "other repositories run it too" in hint, hint
    hook = ghooks / hooks.HOOK
    hook.write_bytes(f"#!/bin/sh\n{hooks.run_line(repo)}\n".encode("ascii"))
    if not IS_WINDOWS:
        hook.chmod(0o755)
    assert hooks.classify(hook, repo) == "calls"
    assert _commit_log(top, tmp_path, "a.txt") == ["./pyt hooks run"]  # this project's checks run
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    assert _commit_log(other, tmp_path, "b.txt") == []  # no ./pyt there: skipped, and the commit is made


@needs_git
def test_calls_with_a_project_folder_that_needs_quotes(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path, "my app")
    (project / "pyt").write_bytes(NAMED_LAUNCHER.encode("ascii"))
    git(top, "config", "core.hooksPath", "hk")
    (top / "hk").mkdir()
    shared = top / "hk" / hooks.HOOK
    repo = find(project, top)
    line = hooks.run_line(repo)
    assert line == "[ ! -f './my app/pyt' ] || sh './my app/pyt' hooks run || exit $?"
    shared.write_text(f"#!/bin/sh\n{line}\n", encoding="utf-8")
    assert hooks.classify(shared, repo) == "calls"
    shared.write_text('#!/bin/sh\nsh "./my app/pyt" hooks run\n', encoding="utf-8")
    assert hooks.classify(shared, repo) == "calls"


def test_cmd_hooks_rejects_bad_arguments() -> None:
    with pytest.raises(PytError, match="unknown subcommand"):
        hooks.cmd_hooks(make(), ["instal"])
    with pytest.raises(PytError, match="unrecognized arguments"):
        hooks.cmd_hooks(make(), ["install", "--forse"])
    with pytest.raises(PytError, match="unrecognized arguments"):
        hooks.cmd_hooks(make(), ["run", "src"])


@needs_git
def test_cmd_hooks_routes_every_subcommand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    top, project = make_repo(tmp_path)
    monkeypatch.setattr(hooks, "ROOT", project)
    monkeypatch.chdir(project)
    target = top / ".git" / "hooks" / hooks.HOOK
    assert hooks.cmd_hooks(make(), []) == 0  # status by default
    assert "not installed" in capsys.readouterr().err
    assert hooks.cmd_hooks(make(), ["install"]) == 0 and target.is_file()
    assert hooks.cmd_hooks(make(), ["status"]) == 0
    assert "hook installed" in capsys.readouterr().err
    seen: list[hooks.Repo] = []

    def fake_run(cfg: Config, repo: hooks.Repo) -> int:
        seen.append(repo)
        return 7

    monkeypatch.setattr(hooks, "run", fake_run)
    assert hooks.cmd_hooks(make(), ["run"]) == 7 and seen[0].project == project
    assert hooks.cmd_hooks(make(), ["install", "--force"]) == 0
    assert hooks.cmd_hooks(make(), ["uninstall"]) == 0 and not target.exists()
    # core.hooksPath with pytemplate's old hook still in .git/hooks: status says it is inactive
    hooks.install(find(project))
    git(top, "config", "core.hooksPath", "hk")
    capsys.readouterr()
    assert hooks.cmd_hooks(make(), ["status"]) == 0
    assert "inactive (core.hooksPath)" in capsys.readouterr().err
    # outside git: status explains and exits 0; install is an error
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.setattr(hooks, "ROOT", outside)
    assert hooks.cmd_hooks(make(), ["status"]) == 0
    assert "no git hook" in capsys.readouterr().err
    with pytest.raises(hooks.NotInGit):
        hooks.cmd_hooks(make(), ["install"])


@needs_git
def test_cmd_hooks_names_a_hook_file_it_cannot_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hooks folder the user may not write (another user's checkout, a read-only mount, chattr
    +i): `hooks install` and `hooks uninstall` ended in an internal-error traceback."""
    top, project = make_repo(tmp_path)
    monkeypatch.setattr(hooks, "ROOT", project)
    monkeypatch.chdir(project)
    target = top / ".git" / "hooks" / hooks.HOOK
    write = hooks._write_hook

    def refused(path: Path, text: str) -> None:
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(hooks, "_write_hook", refused)
    with pytest.raises(PytError, match=r"hooks install: cannot change .*pre-commit: Operation not permitted") as e:
        hooks.cmd_hooks(make(), ["install"])
    assert e.value.code == 2 and not target.exists()
    monkeypatch.setattr(hooks, "_write_hook", write)
    assert hooks.cmd_hooks(make(), ["install"]) == 0 and target.is_file()
    unlink = Path.unlink

    def refuse_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == hooks.HOOK:
            raise PermissionError(1, "Operation not permitted", str(self))
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_unlink)
    with pytest.raises(PytError, match=r"hooks uninstall: cannot change .*pre-commit: Operation not permitted"):
        hooks.cmd_hooks(make(), ["uninstall"])
    assert target.is_file()


@needs_git
def test_install_in_a_linked_worktree(tmp_path: Path) -> None:
    """A linked worktree shares the main repository's hooks folder: install writes there and
    git runs the hook from the worktree's top."""
    top, project = make_repo(tmp_path)
    (project / "pyt").write_bytes(FAKE_LAUNCHER.encode("ascii"))
    (top / ".git" / "info" / "exclude").write_text(".topmark\n", encoding="utf-8")
    git(top, "add", "-A")
    git(top, "commit", "-q", "--no-verify", "-m", "base")
    wt = tmp_path / "wt"
    git(top, "worktree", "add", "-q", str(wt))
    repo = find(wt)
    assert repo.default_dir == Path(os.path.normpath(top / ".git" / "hooks"))
    assert repo.top == Path(os.path.normpath(wt)) or os.path.samefile(repo.top, wt)
    hooks.install(repo)
    assert (top / ".git" / "hooks" / hooks.HOOK).is_file()
    assert hooks.classify(top / ".git" / "hooks" / hooks.HOOK, find(project)) == "installed"  # same launcher
    (wt / ".topmark").write_text("", encoding="utf-8")
    log = tmp_path / "hook.log"
    env = dict(git_env(), PT_HOOK_LOG=log.as_posix())
    (wt / "a.txt").write_text("a\n", encoding="utf-8")
    git(wt, "add", "a.txt", env=env)
    assert git(wt, "commit", "-q", "-m", "wt", env=env, check=False).returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["launcher hooks run from top"]


@needs_git
def test_the_user_git_config_does_not_reach_the_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's ~/.gitconfig (global core.hooksPath, unusable commit signing, a template
    folder with its own pre-commit) must not change what these tests see."""
    if git_version() < (2, 32):
        pytest.skip("GIT_CONFIG_GLOBAL needs git 2.32")
    home = tmp_path / "home"
    (home / "tpl" / "hooks").mkdir(parents=True)
    (home / "tpl" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hostile = (
        f"[core]\n\thooksPath = {(home / 'global-hooks').as_posix()}\n[commit]\n\tgpgsign = true\n"
        f"[gpg]\n\tprogram = {(home / 'no-gpg').as_posix()}\n[init]\n\ttemplateDir = {(home / 'tpl').as_posix()}\n"
        "[diff]\n\trelative = true\n"
    )
    (home / ".gitconfig").write_text(hostile, encoding="utf-8")
    (home / "xdg" / "git").mkdir(parents=True)
    (home / "xdg" / "git" / "config").write_text(hostile, encoding="utf-8")
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    top, project = make_repo(tmp_path)
    assert not (top / ".git" / "hooks" / hooks.HOOK).exists()
    repo = find(project)
    assert not repo.custom_hooks_path
    assert "installed" in hooks.install(repo)
    (top / "a.txt").write_text("a\n", encoding="utf-8")
    git(top, "add", "a.txt")
    git(top, "commit", "-q", "--no-verify", "-m", "one")


# --- the environment git gives hooks, and the staged files ------------------------------------------


def test_git_env_pins_relative_paths(tmp_path: Path) -> None:
    top = tmp_path / "top"
    env = hooks.git_env({"GIT_INDEX_FILE": ".git/index", "GIT_AUTHOR_NAME": "x"}, top)
    assert env == {"GIT_INDEX_FILE": ".git/index"}  # without GIT_DIR git reads it from the top
    absolute = str(tmp_path / "wt" / "index")
    env = hooks.git_env({"GIT_DIR": ".git", "GIT_INDEX_FILE": absolute}, top)
    assert env["GIT_DIR"] == os.path.normpath(top / ".git")
    assert env["GIT_INDEX_FILE"] == absolute
    assert env["GIT_WORK_TREE"] == str(top)  # GIT_DIR alone means: the cwd is the top
    assert hooks.git_env({"GIT_DIR": ".git", "GIT_WORK_TREE": "w"}, top)["GIT_WORK_TREE"] == os.path.normpath(top / "w")
    assert hooks.git_env({}, top) == {}


def test_a_hook_that_cds_into_the_project_reads_the_real_index(tmp_path: Path) -> None:
    """README's monorepo form, `cd apps/a && sh ./pyt hooks run`: git hands the hook
    GIT_INDEX_FILE=.git/index (relative to the top) and it was joined to apps/a, a missing
    index. The staged files read as none (a first commit passed unchecked) or all deleted."""
    top, project = make_repo(tmp_path, "apps/a")
    (project / "x.py").write_text("x = 1\n", encoding="utf-8")
    git(top, "add", "apps/a/x.py")
    repo = find(project, top=project, environ={"GIT_INDEX_FILE": ".git/index"})  # the hook's cwd after its cd
    assert hooks.staged_files(repo) == ["x.py"]
    assert hooks.staged_files(repo, "D") == []


@needs_git
@pytest.mark.parametrize("kind", ["worktree", "separate-git-dir"])
def test_a_hook_that_cds_into_the_project_finds_the_top_of_a_checkout_whose_git_is_a_file(tmp_path: Path, kind: str) -> None:
    """The same hook in a linked worktree or a --separate-git-dir clone: git hands the hook an
    absolute GIT_DIR (and GIT_INDEX_FILE) without GIT_WORK_TREE, which makes the cwd, apps/a
    after the cd, the top of the work tree. The staged files read as none ("ruff: no staged
    Python file") and every committed file of the project as untracked, so every commit was
    refused with "generated files staged"."""
    if kind == "worktree":
        top, project = make_repo(tmp_path, "apps/a")
    else:
        top, project = tmp_path / "repo", tmp_path / "repo" / "apps" / "a"
        git(tmp_path, "init", "-q", "--separate-git-dir", str(tmp_path / "gitdir"), str(top))
        project.mkdir(parents=True)
    (project / "kept.json").write_text("{}\n", encoding="utf-8")
    git(top, "add", "-A")
    git(top, "commit", "-q", "--no-verify", "-m", "base")
    if kind == "worktree":
        top = tmp_path / "wt"
        git(tmp_path / "repo", "worktree", "add", "-q", str(top))
        project = top / "apps" / "a"
    (project / "x.py").write_text("x = 1\n", encoding="utf-8")
    git(top, "add", "apps/a/x.py")
    gitdir = git(top, "rev-parse", "--absolute-git-dir").stdout.strip()
    exported = {"GIT_DIR": gitdir, "GIT_INDEX_FILE": os.path.join(gitdir, "index"), "GIT_PREFIX": ""}
    repo = find(project, top=project, environ=exported)  # the hook's cwd after its cd
    assert os.path.samefile(repo.top, top) and repo.prefix == "apps/a"
    assert hooks.staged_files(repo) == ["x.py"]
    assert hooks.unstaged_files(repo, ["kept.json", "x.py"]) == []
    # where git runs the hook (the top: no cd), the same variables keep working
    repo = find(project, top=top, environ=exported)
    assert os.path.samefile(repo.top, top) and repo.prefix == "apps/a" and hooks.staged_files(repo) == ["x.py"]


def test_git_calls_are_pinned_against_user_config(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv: Sequence[str], **kw: object) -> subprocess.CompletedProcess[str]:
        env = kw["env"]
        assert isinstance(env, dict)
        seen.append(([str(a) for a in argv], env))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(proc, "run", fake_run)
    monkeypatch.setenv("GIT_DIR", "/elsewhere")  # a hook's variable: only repo.env may set it
    hooks._git(["status"], ROOT, {"GIT_INDEX_FILE": "/x/index"})
    argv, env = seen[0]
    assert argv[:4] == ["git", "-c", "diff.relative=false", "--literal-pathspecs"] and argv[4:] == ["status"]
    assert env["LC_ALL"] == "C" and env["GIT_INDEX_FILE"] == "/x/index" and "GIT_DIR" not in env


def test_project_paths() -> None:
    names = ["a.py", "apps/p/src/x.py", "apps/p/pyt", "apps/pp/y.py", "apps/p", "", "Apps/P/z.py"]
    assert hooks.project_paths("", names, ignore_case=False) == ["a.py", "apps/p/src/x.py", "apps/p/pyt", "apps/pp/y.py", "apps/p", "Apps/P/z.py"]
    assert hooks.project_paths("apps/p", names, ignore_case=False) == ["src/x.py", "pyt"]
    assert hooks.project_paths("apps/p/", names, ignore_case=True) == ["src/x.py", "pyt", "z.py"]


def test_python_files() -> None:
    staged = ["src/a.py", "src/pkg/b.pyi", "tests/test_c.py", "tools/d.py", "e.py", "src/f.txt", ".pytemplate/runner/g.py"]
    assert hooks.python_files(staged, ["src", "tests"]) == ["src/a.py", "src/pkg/b.pyi", "tests/test_c.py"]
    assert hooks.python_files(staged, ["src"]) == ["src/a.py", "src/pkg/b.pyi"]


def test_batches() -> None:
    files = [f"src/{i:03}.py" for i in range(100)]
    batches = list(hooks._batches(files, limit=100))
    assert [f for b in batches for f in b] == files
    assert all(sum(len(f) + 3 for f in b) <= 100 for b in batches)


@needs_git
def test_staged_files_from_a_subfolder_project(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path, "apps/p")
    for rel in ("outside.txt", "apps/p/src/a.py", "apps/p/src/gone.py", "apps/p/old.txt"):
        (top / rel).parent.mkdir(parents=True, exist_ok=True)
        (top / rel).write_text("x = 1\n", encoding="utf-8")
    git(top, "add", "-A")
    git(top, "commit", "-q", "--no-verify", "-m", "one")
    (top / "apps/p/src/a.py").write_text("x = 2\n", encoding="utf-8")
    (top / "apps/p/new.py").write_text("y = 1\n", encoding="utf-8")
    (top / "outside.txt").write_text("changed\n", encoding="utf-8")
    git(top, "rm", "-q", "apps/p/src/gone.py")
    git(top, "mv", "apps/p/old.txt", "apps/p/renamed.txt")
    git(top, "add", "-A")
    repo = find(project, top)
    assert sorted(hooks.staged_files(repo)) == ["new.py", "renamed.txt", "src/a.py"]
    assert sorted(hooks.staged_files(repo, "D")) == ["old.txt", "src/gone.py"]  # a rename is also a deletion
    # as a hook sees it: a relative GIT_INDEX_FILE (relative to the top), GIT_DIR without GIT_WORK_TREE
    for environ in ({"GIT_INDEX_FILE": ".git/index"}, {"GIT_DIR": ".git", "GIT_INDEX_FILE": ".git/index"}):
        hooked = find(project, top, environ)
        assert sorted(hooks.staged_files(hooked)) == ["new.py", "renamed.txt", "src/a.py"]
        assert hooked.launcher == "./apps/p/pyt"


@needs_git
@pytest.mark.parametrize("how", ["repo-config", "GIT_CONFIG_PARAMETERS"])
def test_staged_and_unstaged_files_ignore_diff_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    """diff.relative=true makes `git diff` print paths relative to the cwd (the project folder):
    the hook then saw no staged file at all and checked nothing."""
    top, project = make_repo(tmp_path, "apps/p")
    (project / "gen.json").write_text("{}\n", encoding="utf-8")
    git(top, "add", "apps/p/gen.json")
    git(top, "commit", "-q", "--no-verify", "-m", "one")
    (project / "src").mkdir()
    (project / "src/a.py").write_text("x = 1\n", encoding="utf-8")
    (top / "outside.txt").write_text("x\n", encoding="utf-8")
    git(top, "add", "apps/p/src/a.py", "outside.txt")
    (project / "gen.json").write_text('{"a": 1}\n', encoding="utf-8")
    if how == "repo-config":
        git(top, "config", "diff.relative", "true")
    else:
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'diff.relative'='true'")
    repo = find(project, top)
    assert hooks.staged_files(repo) == ["src/a.py"]
    assert hooks.unstaged_files(repo, ["gen.json"]) == ["gen.json"]
    assert hooks.worktree_changes(repo) == {"gen.json"}


@needs_git
def test_staged_files_in_the_first_commit(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(top, "add", "a.py")
    assert hooks.staged_files(find(project)) == ["a.py"]
    assert hooks.staged_files(find(project), "D") == []


@needs_git
def test_unstaged_files(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path, "p")
    for name in ("gen.json", "clean.json", "ignored.json"):
        (project / name).write_text("{}\n", encoding="utf-8")
    (top / ".gitignore").write_text("p/ignored.json\n", encoding="utf-8")
    git(top, "add", "p/gen.json", "p/clean.json", ".gitignore")
    git(top, "commit", "-q", "--no-verify", "-m", "one")
    (project / "gen.json").write_text('{"a": 1}\n', encoding="utf-8")
    (project / "new.json").write_text("{}\n", encoding="utf-8")
    repo = find(project, top)
    paths = ["gen.json", "clean.json", "ignored.json", "new.json", "missing.json"]
    assert hooks.unstaged_files(repo, paths) == ["gen.json", "new.json"]
    git(top, "add", "p/gen.json")
    assert hooks.unstaged_files(repo, paths) == ["new.json"]
    assert hooks.unstaged_files(repo, []) == []


@needs_git
def test_staged_blob_is_the_staged_version_as_checked_out(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path, "p")
    (top / ".gitattributes").write_text("*.cmd text eol=crlf\n", encoding="utf-8")
    (project / "x.py").write_bytes(b"x = 1\n")
    (project / "pyt.cmd").write_bytes(b"@echo off\r\n")
    if not IS_WINDOWS:  # no ":" in Windows file names
        (project / "1:odd.py").write_bytes(b"odd = 1\n")
    git(top, "add", "-A")
    (project / "x.py").write_bytes(b"x = 2\n")  # unstaged
    repo = find(project, top)
    assert hooks.staged_blob(repo, "x.py") == b"x = 1\n"
    assert hooks.staged_blob(repo, "pyt.cmd") == b"@echo off\r\n"  # the index stores LF
    if not IS_WINDOWS:
        assert hooks.staged_blob(repo, "1:odd.py") == b"odd = 1\n"  # not "stage 1 of odd.py"
    assert hooks.staged_blob(repo, "missing.py") is None
    assert hooks.worktree_changes(repo) == {"x.py"}


# --- which checks run on which files ---------------------------------------------------------------


class Tools:
    """Records the tool calls of `hooks.checks` (ruff by path and on stdin, uv lock --check) and
    fakes the renderer."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.ruff_calls: list[tuple[list[str], list[str]]] = []
        self.staged_calls: list[tuple[str, str, bytes]] = []  # (ruff subcommand, path, content)
        self.ruff_code = 0
        self.ruff_output = ""
        self.lock_code = 0
        self.generated: dict[str, str] = {"gen.json": "{}\n"}
        self.render_result: tuple[list[str], list[str]] = ([], [])
        self.pyproject_outdated = False
        self.compiled: list[Path] = []
        self.findings: list[str] = []
        self.linted: list[Path] = []
        cfg_file = tmp_path / "ruff-profile.toml"
        monkeypatch.setattr(hooks, "ruff", self._ruff)
        monkeypatch.setattr(hooks, "ruff_staged", self._ruff_staged)
        monkeypatch.setattr(hooks, "uv_lock_check", lambda cfg: (self.lock_code, "uv.lock needs to be updated"))
        monkeypatch.setattr(hooks, "_profile_file", lambda cfg, profile, kind: cfg_file)
        monkeypatch.setattr(render, "outputs", lambda cfg: dict(self.generated))
        monkeypatch.setattr(render, "apply", lambda cfg, check=False: self.render_result)
        monkeypatch.setattr(render, "pyproject_outdated", lambda cfg: self.pyproject_outdated)
        # cmd_apply.pending reads the real project (src/<pkg>/, pyproject.toml), not the throwaway
        # repository: test_apply covers what check_lock gets from it
        monkeypatch.setattr(cmd_apply, "pending", lambda cfg, hook=True: [])
        monkeypatch.setattr(mypyc, "compiled_sources", lambda cfg: list(self.compiled))
        monkeypatch.setattr(lintc, "lint", self._lint)
        monkeypatch.setattr(lintc, "describe", lambda files: ", ".join(p.stem for p in files))

    def _ruff(self, cfg: Config, args: Sequence[str | Path], files: Sequence[str]) -> tuple[int, str]:
        self.ruff_calls.append(([str(a) for a in args], list(files)))
        return self.ruff_code, self.ruff_output

    def _ruff_staged(self, cfg: Config, args: Sequence[str | Path], path: str, data: bytes) -> tuple[int, str]:
        self.staged_calls.append((str(args[0]), path, data))
        return self.ruff_code, self.ruff_output

    def _lint(self, cfg: Config, files: list[Path]) -> list[FakeFinding]:
        self.linted += files
        return [FakeFinding(text) for text in self.findings]


class FakeFinding:
    """A lintc.Finding the hook prints as `text`; `note` findings never block."""

    def __init__(self, text: str, note: bool = False) -> None:
        self.text, self.note = text, note

    def __str__(self) -> str:
        return self.text


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Tools:
    return Tools(monkeypatch, tmp_path)


def staged_project(tmp_path: Path, files: dict[str, bytes], *, commit: Sequence[str] = ()) -> tuple[hooks.Repo, list[str]]:
    """A project in a subfolder with `files` written, `commit` committed first, the rest staged."""
    top, project = make_repo(tmp_path, "proj")
    for rel, data in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_bytes(data)
    if commit:
        git(project, "add", *commit)
        git(project, "commit", "-q", "--no-verify", "-m", "base")
    git(project, "add", "-A")
    repo = find(project, top)
    return repo, hooks.staged_files(repo)


def results(cfg: Config, repo: hooks.Repo, staged: list[str], *, template_repo: bool = False) -> dict[str, hooks.Result]:
    out: dict[str, hooks.Result] = {}
    deleted = hooks.staged_files(repo, "D")
    for r in hooks.checks(cfg, repo, staged, code_dirs=["src", "tests"], template_repo=template_repo, deleted=deleted):
        out[r.label.split(":")[0].split(" (")[0]] = r
    return out


def failures(res: dict[str, hooks.Result]) -> dict[str, hooks.Result]:
    return {k: r for k, r in res.items() if r.passed is False}


@needs_git
def test_checks_ruff_only_on_staged_python_files(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {
        "src/pkg/a.py": b"x = 1\n", "src/pkg/b.pyi": b"y: int\n", "tests/test_a.py": b"def test(): pass\n",
        "tools/other.py": b"z = 1\n", "README.md": b"# r\n", "gen.json": b"{}\n",
    })
    res = results(make(), repo, staged)
    assert [files for _, files in tools.ruff_calls] == [["src/pkg/a.py", "src/pkg/b.pyi", "tests/test_a.py"]] * 2
    assert tools.staged_calls == []  # nothing has unstaged changes: every file by path
    check_args, format_args = (args for args, _ in tools.ruff_calls)
    assert check_args[:1] == ["check"] and "--force-exclude" in check_args and "--exit-zero" not in check_args
    assert format_args[:2] == ["format", "--check"]
    assert res["ruff check"].passed is True and "profile 'off'" in res["ruff check"].label
    assert res["ruff format"].passed is True
    assert res["generated files up to date"].passed is True
    assert res["generated files staged"].passed is True
    assert res["pyproject.toml and uv.lock up to date"].passed is True
    assert res["config files staged together"].passed is True  # gen.json is in the commit, nothing unstaged
    assert res["mypyc rules"].passed is None
    assert res["launchers"].passed is None
    assert "language guard" not in res


@needs_git
def test_checks_skip_ruff_without_python_files(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"README.md": b"# r\n"})
    res = results(make(), repo, staged)
    assert tools.ruff_calls == [] and tools.staged_calls == []
    assert res["ruff"].passed is None
    assert res["config files"].passed is None  # none of them in this commit


@needs_git
def test_checks_report_ruff_failures_and_exit_zero(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"src/a.py": b"print(y)\n"})
    tools.ruff_code, tools.ruff_output = 1, "src/a.py:1:7: F821 Undefined name `y`\nFound 1 error."
    res = results(make(), repo, staged)
    assert res["ruff check"].passed is False and "F821" in res["ruff check"].output
    assert res["ruff format"].passed is False and "./pyt fmt" in res["ruff format"].hint
    tools.ruff_calls.clear()
    tools.ruff_code = 0
    res = results(make({"typing": {"relaxed": "warn"}}), repo, staged)
    assert "--exit-zero" in tools.ruff_calls[0][0]
    assert res["ruff check"].passed is True and "warnings only" in res["ruff check"].label


@needs_git
@pytest.mark.parametrize("code", [2, 127, -9])
def test_ruff_that_cannot_run_is_not_a_format_failure(tmp_path: Path, tools: Tools, code: int) -> None:
    """uv could not start ruff (a stale lock with --locked, no uv, a crash): that is not a
    formatting problem, and ./pyt fmt would not fix it."""
    repo, staged = staged_project(tmp_path, {"src/a.py": b"x = 1\n"})
    tools.ruff_code, tools.ruff_output = code, "error: Failed to spawn: `ruff`"
    res = results(make(), repo, staged)
    for key in ("ruff check", "ruff format"):
        assert res[key].passed is False and "could not run ruff" in res[key].label
        assert "Failed to spawn" in res[key].output and "./pyt" not in res[key].hint


def test_ruff_uses_the_lock_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """--frozen, not --locked: a stale uv.lock is the lock check's finding, not a reason for ruff
    not to run (it made one lock problem show as three failures)."""
    calls: list[list[str]] = []

    def fake_uv(env: envs.PyEnv, args: Sequence[str | Path], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in args])
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(envs, "uv", fake_uv)
    assert hooks.ruff(make(), ["check", "--force-exclude"], ["src/a.py"]) == (0, "")
    assert calls == [["run", "--quiet", "--frozen", "ruff", "check", "--force-exclude", "src/a.py"]]
    # a batch killed by a signal is never hidden by a later clean batch
    codes = iter([-9, *[0] * 20])

    def killed_then_ok(env: envs.PyEnv, args: Sequence[str | Path], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), next(codes), "", "")

    monkeypatch.setattr(envs, "uv", killed_then_ok)
    many = [f"src/{i:04}_{'x' * 40}.py" for i in range(400)]  # more than one batch
    assert hooks.ruff(make(), ["check"], many)[0] == 2


@needs_git
def test_partially_staged_python_file_is_checked_as_staged(tmp_path: Path, tools: Tools) -> None:
    """`./pyt fmt` after a failed commit, then `git commit` again without `git add`: the
    working tree is formatted but the commit is not. The hook must check what is committed
    (and not block good staged content for bad unstaged edits)."""
    repo, staged = staged_project(tmp_path, {"src/a.py": b"x=1\n", "src/b.py": b"y = 2\n"})
    (repo.project / "src/a.py").write_bytes(b"x = 1\n")  # formatted in the working tree only
    res = results(make(), repo, staged)
    assert [files for _, files in tools.ruff_calls] == [["src/b.py"]] * 2
    assert tools.staged_calls == [("check", "src/a.py", b"x=1\n"), ("format", "src/a.py", b"x=1\n")]
    assert "1 with unstaged changes, checked as staged" in res["ruff check"].label
    tools.ruff_code = 1
    res = results(make(), repo, staged)
    assert res["ruff format"].passed is False and "git add -p" in res["ruff format"].hint
    assert "Would reformat: src/a.py" in res["ruff format"].output  # ruff says nothing for stdin


@needs_git
def test_staged_python_file_missing_from_the_working_tree(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"src/a.py": b"x = 1\n", "src/b.py": b"y = 2\n"})
    (repo.project / "src/a.py").unlink()  # staged, then deleted without `git rm`: git still commits it
    assert "src/a.py" in hooks.staged_files(repo)
    res = results(make(), repo, staged)
    assert [files for _, files in tools.ruff_calls] == [["src/b.py"]] * 2  # ruff never sees a missing path
    assert tools.staged_calls == []
    missing = res["staged files missing from the working tree"]
    assert missing.passed is False and "src/a.py" in missing.label and "src/b.py" not in missing.label
    assert "git restore src/a.py" in missing.hint and "git rm --cached src/a.py" in missing.hint
    (repo.project / "src/b.py").unlink()
    tools.ruff_calls.clear()
    res = results(make(), repo, staged)
    assert tools.ruff_calls == [] and res["ruff"].passed is None
    assert res["staged files missing from the working tree"].passed is False


@needs_git
def test_checks_generated_files(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"gen.json": b"{}\n", "src/a.py": b"x = 1\n"}, commit=["gen.json"])
    (repo.project / "gen.json").write_bytes(b'{"new": 1}\n')  # regenerated after the staging
    staged = hooks.staged_files(repo)
    res = results(make(), repo, staged)
    assert res["generated files staged"].passed is False
    assert "git add gen.json" in res["generated files staged"].hint
    tools.render_result = (["gen.json"], [".mypy.ini"])
    res = results(make(), repo, staged)
    assert res["generated files up to date"].passed is False
    assert "./pyt render" in res["generated files up to date"].hint and ".mypy.ini" in res["generated files up to date"].hint
    # render rewrites state.json with them: the line names it, or following it blocks the next commit
    assert "./pyt render, then git add gen.json .pytemplate/state.json" in res["generated files up to date"].hint


def test_quiet_keeps_what_a_failed_check_says(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`./pyt -q hooks run` (a form README accepts: the global options may come first) printed
    "[XX] ruff format: files need formatting" without the file nor the fix: -q hides progress,
    never the answer (CLAUDE.md 5.3)."""
    from runner import ui

    monkeypatch.setattr(ui, "QUIET", True)
    hooks._print(hooks.Result(False, "ruff format: files need formatting", "./pyt fmt, then git add src/p/extra.py", output="Would reformat: src/p/extra.py"))
    hooks._print(hooks.Result(True, "ruff", output="All checks passed!"))
    err = capsys.readouterr().err
    assert "files need formatting" in err and "Would reformat: src/p/extra.py" in err and "./pyt fmt, then git add src/p/extra.py" in err, err
    assert "All checks passed!" not in err  # a passed check's output stays progress


def test_the_hook_reports_preset_options_that_are_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A [preset.*] edit that is not applied yet (flet's version leaves no trace in the managed
    block) blocks the commit: check_lock asks cmd_apply.pending. CLAUDE.md 15.2 said it showed only
    in doctor: a maintainer reading it learned the opposite of what the hook does."""
    monkeypatch.setattr(render, "pyproject_outdated", lambda cfg: False)
    monkeypatch.setattr(hooks, "uv_lock_check", lambda cfg: (0, ""))
    asked: list[bool] = []

    def pending(cfg: Config, hook: bool = True) -> list[tuple[str, str]]:
        asked.append(hook)
        return [("[preset.flet] is not applied to pyproject.toml (add flet==1.0.0)", "./pyt apply")]

    monkeypatch.setattr(cmd_apply, "pending", pending)
    result = hooks.check_lock(make())
    assert result.passed is False and "[preset.flet] is not applied" in result.hint and asked == [False]
    guide = ROOT / "CLAUDE.md"
    if guide.is_file():
        text = " ".join(guide.read_text(encoding="utf-8").split())
        assert "shows only in `doctor`" not in text
        assert "`doctor` and the pre-commit hook report it (`cmd_apply.pending`" in text


@needs_git
def test_checks_lock(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(
        tmp_path, {"pyproject.toml": b"[project]\n", "uv.lock": b"v1\n"}, commit=["pyproject.toml", "uv.lock"]
    )
    (repo.project / "pyproject.toml").write_bytes(b"[project]\nname = 'x'\n")
    git(repo.project, "add", "pyproject.toml")
    (repo.project / "uv.lock").write_bytes(b"v2\n")  # re-locked but not staged
    staged = hooks.staged_files(repo)
    assert staged == ["pyproject.toml"]
    res = results(make(), repo, staged)
    assert res["config files staged together"].passed is False
    assert "git add uv.lock" in res["config files staged together"].hint
    git(repo.project, "add", "uv.lock")
    tools.lock_code = 1
    tools.pyproject_outdated = True
    res = results(make(), repo, hooks.staged_files(repo))
    hint = res["pyproject.toml and uv.lock"].hint
    assert "does not match pytemplate.toml" in hint and "uv.lock needs to be updated" in hint and "git add" not in hint
    assert res["config files staged together"].passed is True


@needs_git
def test_config_lock_and_generated_files_are_committed_together(tmp_path: Path, tools: Tools) -> None:
    """uv.lock staged without pyproject.toml (HEAD then fails `uv run --locked`), generated files
    without pytemplate.toml (HEAD fails `render --check`): blocked; and the hints never lead to
    such a commit."""
    base = {"pytemplate.toml": b"a = 1\n", "pyproject.toml": b"[project]\n", "uv.lock": b"v1\n", "gen.json": b"{}\n", "src/a.py": b"x = 1\n"}
    repo, _ = staged_project(tmp_path, base, commit=list(base))
    p = repo.project

    def reset() -> None:
        git(p, "reset", "-q")
        git(p, "checkout", "-q", "--", ".")

    # 1. `./pyt add six` rewrote both, only uv.lock staged
    (p / "pyproject.toml").write_bytes(b"[project]\ndependencies = ['six']\n")
    (p / "uv.lock").write_bytes(b"v2\n")
    git(p, "add", "uv.lock")
    res = failures(results(make(), repo, hooks.staged_files(repo)))
    assert "git add pyproject.toml" in res["config files staged together"].hint
    reset()
    # 2. pytemplate.toml edited and rendered, only the generated file staged
    (p / "pytemplate.toml").write_bytes(b"a = 2\n")
    (p / "gen.json").write_bytes(b'{"a": 2}\n')
    git(p, "add", "gen.json")
    res = failures(results(make(), repo, hooks.staged_files(repo)))
    assert "git add pytemplate.toml" in res["config files staged together"].hint
    # 3. the same edits unstaged, an unrelated file staged: still blocked (conservative), and the
    #    hint names the source too, so following it cannot produce case 2
    git(p, "reset", "-q")
    (p / "src/a.py").write_bytes(b"x = 2\n")
    git(p, "add", "src/a.py")
    res = results(make(), repo, hooks.staged_files(repo))
    assert res["config files"].passed is None
    hint = res["generated files staged"].hint
    assert res["generated files staged"].passed is False and "git add pytemplate.toml gen.json" in hint
    reset()
    # 4. pytemplate.toml staged, the pyproject.toml it rewrote not
    (p / "pytemplate.toml").write_bytes(b"a = 3\n")
    (p / "pyproject.toml").write_bytes(b"[project]\nrequires-python = '>=3.11'\n")
    git(p, "add", "pytemplate.toml")
    res = failures(results(make(), repo, hooks.staged_files(repo)))
    assert "git add pyproject.toml" in res["config files staged together"].hint
    # 5. everything staged: nothing to complain about
    (p / "uv.lock").write_bytes(b"v3\n")
    (p / "gen.json").write_bytes(b'{"a": 3}\n')
    git(p, "add", "-A")
    assert failures(results(make(), repo, hooks.staged_files(repo))) == {}
    git(p, "commit", "-q", "--no-verify", "-m", "all")
    # 6. `git rm --cached uv.lock`: gone from the commit, still on disk
    git(p, "rm", "-q", "--cached", "uv.lock")
    res = failures(results(make(), repo, hooks.staged_files(repo)))
    assert "deleted in the commit but still in the working tree: uv.lock" in res["config files staged together"].hint


@needs_git
def test_run_checks_a_commit_that_only_deletes_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A commit that only deletes files (a generated file, uv.lock) still runs the project-wide
    checks: `git rm .mypy.ini uv.lock` alone must not print 'nothing to check'."""
    repo, _ = staged_project(tmp_path, {"gen.json": b"{}\n", "uv.lock": b"v\n", "src/a.py": b"x = 1\n"}, commit=["gen.json", "uv.lock", "src/a.py"])
    seen: list[tuple[list[str], list[str]]] = []

    def fake_checks(cfg: Config, repo: hooks.Repo, staged: Sequence[str], **kw: object) -> Iterator[hooks.Result]:
        deleted = kw["deleted"]
        assert isinstance(deleted, list)
        seen.append((list(staged), sorted(deleted)))
        yield hooks.Result(False, "generated files up to date", "outdated: gen.json")

    monkeypatch.setattr(hooks, "checks", fake_checks)
    git(repo.project, "rm", "-q", "gen.json", "uv.lock", "src/a.py")
    assert hooks.run(make(), repo) == 1
    err = capsys.readouterr().err
    assert "nothing to check" not in err and "3 staged files, 3 deleted" in err
    assert seen == [([], ["gen.json", "src/a.py", "uv.lock"])]  # per-file checks never get a deleted path
    git(repo.project, "reset", "-q")  # nothing staged at all: still nothing to check
    assert hooks.run(make(), repo) == 0
    assert "nothing to check" in capsys.readouterr().err


def test_uv_error_message() -> None:
    out = (
        "Resolved 25 packages in 20ms\n"
        "error: The lockfile at `uv.lock` needs to be updated, but `--check` was\n"
        "       provided.\n\nhint: To update the lockfile, run `uv lock`.\n"
    )
    assert envs.uv_error(out) == "error: The lockfile at `uv.lock` needs to be updated, but `--check` was provided."
    assert envs.uv_error("one\nlast line\n\n") == "last line"
    assert envs.uv_error("") == ""
    # uv's graphical report (uv 0.8 to 0.11 print no `error:`): the last line was "cache." of the hint
    report = (
        "Using CPython 3.14.7\n"
        "  \u00d7 No solution found when resolving dependencies:\n"
        "  \u2570\u2500\u25b6 Because six was not found in the cache and wsapp depends\n"
        "      on six>=1.16, we can conclude that wsapp's requirements are\n"
        "      unsatisfiable.\n"
        "\n"
        "      hint: Packages were unavailable because the network was disabled. When\n"
        "      the network is disabled, registry packages may only be read from the\n"
        "      cache.\n"
    )
    assert envs.uv_error(report) == (
        "No solution found when resolving dependencies: Because six was not found in the cache and wsapp depends "
        "on six>=1.16, we can conclude that wsapp's requirements are unsatisfiable."
    )


@needs_git
def test_checks_mypyc_rules_only_on_staged_compiled_modules(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"src/pkg/core/a.py": b"x = 1\n", "src/pkg/core/b.py": b"y = 1\n"}, commit=["src/pkg/core/b.py"])
    tools.compiled = [repo.project / "src/pkg/core/a.py", repo.project / "src/pkg/core/b.py"]
    tools.findings = ["src/pkg/core/a.py:1: nested class 'X': mypyc does not support it"]
    res = results(make(), repo, staged)  # cpython active: profile 'off' -> warnings
    assert tools.linted == [repo.project / "src/pkg/core/a.py"]
    assert res["mypyc rules"].passed is True and res["mypyc rules"].warnings == tools.findings
    res = results(make({"backend": {"active": "mypyc"}}), repo, staged)
    assert res["mypyc rules"].passed is False and res["mypyc rules"].errors == tools.findings
    res = results(make({"backend": {"supported": ["cpython"]}}), repo, staged)
    assert res["mypyc rules"].passed is None


@needs_git
def test_checks_launchers(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"pyt": b"#!/bin/sh\r\necho hi\r\n", "pyt.cmd": b"@echo off\r\n"})
    res = results(make(), repo, staged)
    launchers = next(r for k, r in res.items() if k.startswith("launchers"))
    assert launchers.passed is False
    assert "pyt: CRLF line endings" in launchers.label
    assert "git mode 100644" in launchers.label  # staged without the exec bit
    assert "git update-index --chmod=+x pyt" in launchers.hint
    (repo.project / "pyt").write_bytes(b"#!/bin/sh\necho hi\n")
    git(repo.project, "add", "--chmod=+x", "pyt")
    res = results(make(), repo, hooks.staged_files(repo))
    assert res["launchers"].passed is True


@needs_git
def test_launchers_are_checked_as_staged(tmp_path: Path, tools: Tools) -> None:
    """The commit holds the staged launcher: a fixed working tree must not hide a broken staged
    file (and a broken working tree must not block a good staged one)."""
    repo, staged = staged_project(tmp_path, {"pyt.cmd": b"@echo off\n"})  # LF: breaks cmd's labels
    (repo.project / "pyt.cmd").write_bytes(b"@echo off\r\n")  # fixed, not staged
    res = results(make(), repo, staged)
    launchers = next(r for k, r in res.items() if k.startswith("launchers"))
    assert launchers.passed is False and "not CRLF" in launchers.label
    git(repo.project, "add", "pyt.cmd")
    (repo.project / "pyt.cmd").write_bytes(b"@echo off\n")  # broken again, not staged
    res = results(make(), repo, hooks.staged_files(repo))
    assert res["launchers"].passed is True


@needs_git
def test_checks_language_guard_in_the_template_repo(tmp_path: Path, tools: Tools) -> None:
    spanish = "# no se encuentra el archivo\n".encode()  # lang: allow
    repo, staged = staged_project(tmp_path, {"notes.md": spanish, "ok.md": b"# fine\n", "uv.lock": spanish, "logo.png": spanish})
    res = results(make(), repo, staged, template_repo=True)
    guard = res["language guard"]
    assert guard.passed is False
    assert guard.output.startswith("notes.md:1:")
    assert "uv.lock" not in guard.output and "logo.png" not in guard.output
    # the staged version counts: fixing the working tree without `git add` does not pass
    (repo.project / "notes.md").write_bytes(b"# translated\n")
    assert results(make(), repo, staged, template_repo=True)["language guard"].passed is False


def test_language_guard_loads_without_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    real_find_spec = importlib.util.find_spec
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "pytest" else real_find_spec(name, *a))
    guard = hooks.load_language_guard(GUARD)
    assert "pytest" not in sys.modules
    assert guard.offending_lines("no se encuentra el archivo")  # lang: allow
    assert not guard.offending_lines("plain English")


@needs_git
def test_run_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    repo, staged = staged_project(tmp_path, {"a.txt": b"a\n"})
    outcome: list[hooks.Result] = []

    def fake_checks(cfg: Config, repo: hooks.Repo, staged: Sequence[str], **_: object) -> Iterator[hooks.Result]:
        yield from outcome

    monkeypatch.setattr(hooks, "checks", fake_checks)
    for key in hooks.GIT_REPO_VARS:
        monkeypatch.setenv(key, "x")  # run() drops them for the tools; monkeypatch restores them
    outcome[:] = [hooks.Result(True, "one"), hooks.Result(None, "two")]
    assert hooks.run(make(), repo) == 0
    assert "all checks passed" in capsys.readouterr().err
    assert not any(key in os.environ for key in hooks.GIT_REPO_VARS)
    outcome[:] = [hooks.Result(False, "bad", "how to fix", output="tool says no")]
    assert hooks.run(make(), repo) == 1
    err = capsys.readouterr().err
    assert err.index("tool says no") < err.index("how to fix") < err.index("1 check failed")
    assert "git commit --no-verify" in err
    git(repo.project, "reset", "-q")
    assert hooks.run(make(), repo) == 0
    assert "nothing to check" in capsys.readouterr().err


# --- the real tools ----------------------------------------------------------------------------------


def _venv_ruff() -> Path:
    return ROOT / ".venv" / ("Scripts/ruff.exe" if IS_WINDOWS else "bin/ruff")


def test_real_ruff_accepts_the_hook_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ruff and uv command lines the hook builds, run for real in this project's .venv: a
    ruff or uv release that renamed a flag would otherwise block every commit of every project
    with no failing test."""
    if not _venv_ruff().is_file():
        pytest.skip("no ruff in .venv (./pyt setup)")
    try:
        proc.find_uv()
    except PytError:
        pytest.skip("uv not found")
    monkeypatch.setattr(cmd_dev, "BUILD", tmp_path / "build")  # _profile_file writes its ruff config there
    cfg = make()
    good, bad, undefined = (tmp_path / name for name in ("good.py", "bad.py", "undefined.py"))
    good.write_text("x = 1\n", encoding="utf-8")
    bad.write_text("x=1\n", encoding="utf-8")
    undefined.write_text("print(y)\n", encoding="utf-8")

    def run(files: list[Path], as_staged: dict[str, bytes] | None = None) -> dict[str, hooks.Result]:
        return {r.label.split(":")[0]: r for r in hooks.check_ruff(cfg, [str(f) for f in files], as_staged)}

    res = run([good])
    assert res["ruff check"].passed is True and res["ruff format"].passed is True, (res["ruff check"].output, res["ruff format"].output)
    res = run([bad])
    assert res["ruff check"].passed is True and res["ruff format"].passed is False
    assert "bad.py" in res["ruff format"].output and "could not run" not in res["ruff format"].label
    res = run([undefined])
    assert res["ruff check"].passed is False and "F821" in res["ruff check"].output
    # the staged version of a file whose working tree differs: stdin, reported under its path
    res = run([good], {str(good): b"x=1\n"})
    assert res["ruff check"].passed is True and res["ruff format"].passed is False
    assert "its staged version" in res["ruff format"].output
    res = run([good], {str(good): b"print(y)\n"})
    assert res["ruff check"].passed is False and "F821" in res["ruff check"].output and "good.py" in res["ruff check"].output
    # and `uv lock --check` of this project (offline: the lock is resolved from the cache)
    monkeypatch.setenv("UV_OFFLINE", "1")
    code, out = hooks.uv_lock_check(cfg)
    assert code == 0, out


def test_real_ruff_format_of_a_staged_syntax_error_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ruff format --check` exits 2 for a file it cannot parse, as for a ruff that did not run:
    the hook said "could not run ruff (the output says why)" for an ordinary syntax error, which
    pointed at uv or the environment (and lost the ./pyt fmt hint of the other files)."""
    if not _venv_ruff().is_file():
        pytest.skip("no ruff in .venv (./pyt setup)")
    try:
        proc.find_uv()
    except PytError:
        pytest.skip("uv not found")
    monkeypatch.setattr(cmd_dev, "BUILD", tmp_path / "build")
    cfg = make()
    syn, ugly = tmp_path / "syn.py", tmp_path / "ugly.py"
    syn.write_text("def f(:\n    pass\n", encoding="utf-8")
    ugly.write_text("x=1\n", encoding="utf-8")

    def run(files: list[Path], as_staged: dict[str, bytes] | None = None) -> hooks.Result:
        return next(r for r in hooks.check_ruff(cfg, [str(f) for f in files], as_staged) if r.label.startswith("ruff format"))

    for fmt in (run([syn]), run([ugly], {str(ugly): b"def f(:\n    pass\n"})):  # by path, and staged on stdin
        assert fmt.passed is False and fmt.label == "ruff format: a staged file does not parse", (fmt.label, fmt.output)
        assert "./pyt fmt" not in fmt.hint
    fmt = run([syn, ugly])
    assert fmt.label == "ruff format: a staged file does not parse" and "./pyt fmt" in fmt.hint


def test_real_ruff_runs_in_a_project_folder_named_like_a_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ruff expands $NAME in its --config argument and in the paths of that file (CLAUDE.md
    15.1): with absolute paths, every commit that staged a Python file of a project in a folder
    such as app$v2 was blocked with "could not run ruff". The real ruff of .venv, started in the
    project as the hook starts it (uv's part faked: `uv run --frozen ruff ARGS` is ruff ARGS)."""
    ruff = _venv_ruff()
    if not ruff.is_file():
        pytest.skip("no ruff in .venv (./pyt setup)")
    root = tmp_path / "app$v2"
    (root / "src" / "pkg").mkdir(parents=True)
    source = root / "src" / "pkg" / "a.py"
    source.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(render, "ROOT", root)
    monkeypatch.setattr(cmd_dev, "ROOT", root)
    monkeypatch.setattr(cmd_dev, "BUILD", root / ".build")
    monkeypatch.setattr(hooks, "ROOT", root)
    monkeypatch.delenv("v2", raising=False)

    def tail(argv: Sequence[object]) -> list[str]:
        args = [str(a) for a in argv]
        return [str(ruff), *args[args.index("ruff") + 1 :]]

    def uv(_env: envs.PyEnv, argv: Sequence[object], **_kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(tail(argv), cwd=root, capture_output=True, text=True, timeout=120, check=False)

    def run_bytes(argv: Sequence[str], *, cwd: Path, env: object, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(tail(argv), cwd=cwd, input=data, capture_output=True, timeout=120, check=False)

    monkeypatch.setattr(envs, "uv", uv)
    monkeypatch.setattr(hooks, "_run_bytes", run_bytes)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    cfg = make()

    def run(as_staged: dict[str, bytes] | None = None) -> dict[str, hooks.Result]:
        return {r.label.split(":")[0]: r for r in hooks.check_ruff(cfg, ["src/pkg/a.py"], as_staged)}

    res = run()
    assert res["ruff check"].passed is True and res["ruff format"].passed is True, (res["ruff check"].output, res["ruff format"].output)
    source.write_text("print(y)\n", encoding="utf-8")
    res = run()
    assert res["ruff check"].passed is False and "F821" in res["ruff check"].output
    res = run({"src/pkg/a.py": b"print(z)\n"})  # the staged version, on stdin
    assert res["ruff check"].passed is False and "F821" in res["ruff check"].output


NAME_DISPATCH_HOOK = """{shebang}
. "$(dirname "$0")/helper.sh"
case $(basename "$0") in
    pre-commit) check_it ;;
    *) exit 0 ;;
esac
"""
HELPER = """check_it() {
    printf '%s\\n' "user check as $(basename "$0")" >> "$PT_HOOK_LOG"
    exit "${PT_LOCAL_EXIT:-0}"
}
"""


@needs_git
@pytest.mark.parametrize("shebang", ["#!/bin/sh", "#!/bin/sh -e", "#!/usr/bin/env bash"])
def test_a_kept_hook_runs_under_its_own_name(tmp_path: Path, shebang: str) -> None:
    """husky v4 and yorkie pick their job from `basename "$0"` and source their helpers from
    `dirname "$0"`: kept as pre-commit.local and run by that name, the user's blocking check
    silently checked nothing while pytemplate said it ran first."""
    if "bash" in shebang and shutil.which("bash") is None:
        pytest.skip("no bash")
    top, project = make_repo(tmp_path)
    (project / "pyt").write_bytes(FAKE_LAUNCHER.encode("ascii"))
    (top / ".topmark").write_text("", encoding="utf-8")
    (top / ".git" / "info" / "exclude").write_text(".topmark\n", encoding="utf-8")
    log = tmp_path / "hook.log"
    env = dict(git_env(), PT_HOOK_LOG=log.as_posix())
    hooks_dir = top / ".git" / "hooks"
    (hooks_dir / hooks.HOOK).write_bytes(NAME_DISPATCH_HOOK.format(shebang=shebang).encode("ascii"))
    (hooks_dir / "helper.sh").write_bytes(HELPER.encode("ascii"))
    if not IS_WINDOWS:
        (hooks_dir / hooks.HOOK).chmod(0o755)
    hooks.install(find(project, top), force=True)

    def commit(name: str, **extra: str) -> subprocess.CompletedProcess[str]:
        (project / name).write_text(name, encoding="utf-8")
        git(top, "add", "-A", env=env)
        return git(top, "commit", "-q", "-m", name, env={**env, **extra}, check=False, timeout=120)

    assert commit("one.txt").returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["user check as pre-commit", "launcher hooks run from top"]
    log.unlink()
    assert commit("two.txt", PT_LOCAL_EXIT="1").returncode != 0  # the user's check still blocks
    assert log.read_text(encoding="utf-8").splitlines() == ["user check as pre-commit"]


@needs_git
def test_force_leaves_a_non_shell_hook_that_reads_its_name(tmp_path: Path) -> None:
    """overcommit's Ruby hook picks its job from $0: it cannot be sourced as pre-commit, and
    run as pre-commit.local it would check nothing. --force leaves it alone and says what line
    to add to it instead."""
    top, project = make_repo(tmp_path)
    target = top / ".git" / "hooks" / hooks.HOOK
    text = "#!/usr/bin/env ruby\nhook_type = File.basename($0)\nexit 0\n"
    target.write_text(text, encoding="utf-8")
    with pytest.raises(PytError, match=r"reads its own name .*Add this line to it instead:\n  \[ ! -f \./pyt \] \|\| sh \./pyt hooks run \|\| exit \$\?"):
        hooks.install(find(project, top), force=True)
    assert target.read_text(encoding="utf-8") == text and not (target.parent / hooks.LOCAL).exists()


def test_interpreter_reads_the_hash_bang_line_as_the_hook_does() -> None:
    assert hooks.interpreter("#!/bin/sh\n") == "sh"
    assert hooks.interpreter("#! /bin/bash -e\r\n") == "bash"
    assert hooks.interpreter("#!/usr/bin/env python3\n") == "python3"
    assert hooks.interpreter("#!/usr/bin/env -S ruby -w\n") == "-S"  # as the hook script: run by its path
    assert hooks.interpreter("echo no hash bang\n") == "sh"
    assert not hooks.reads_its_name('#!/bin/sh\ncase $(basename "$0") in *) ;; esac\n')  # sourced: $0 is right
    assert hooks.reads_its_name("#!/usr/bin/env node\nconst h = process.argv[1]\n")
    assert not hooks.reads_its_name("#!/usr/bin/env python3\nprint('checks')\n")
    # compiled (a NUL byte in the first 64 bytes): no interpreter, and its bytes say nothing
    elf = "\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00" + "argv[0] $0" * 10
    assert hooks.interpreter(elf) == "" and not hooks.reads_its_name(elf)
    assert hooks.interpreter("MZ\x90\x00\x03\x00") == ""


COMPILED_HOOK = r"""#include <stdio.h>
#include <stdlib.h>
int main(void) {
    const char *log = getenv("PT_HOOK_LOG"), *code = getenv("PT_LOCAL_EXIT");
    FILE *f = log ? fopen(log, "a") : NULL;
    if (f) { fputs("compiled hook\n", f); fclose(f); }
    return code ? atoi(code) : 0;
}
"""


@needs_git
@pytest.mark.skipif(IS_WINDOWS, reason="a compiled hook of POSIX (git for Windows runs a PE hook by the same rule)")
@pytest.mark.parametrize("how", ["file", "symlink"])
def test_a_compiled_hook_kept_by_force_runs_first(tmp_path: Path, how: str) -> None:
    """A compiled hook (ELF, Mach-O), or a symlink to one, has no #! line: the hook script
    sourced it as a shell script, so after install --force every commit failed with a shell
    syntax error, and neither the kept hook nor the checks ran. It runs as git ran it."""
    compiler = next((c for c in ("cc", "gcc", "clang") if shutil.which(c)), None)
    if compiler is None:
        pytest.skip("no C compiler")
    source = tmp_path / "hook.c"
    source.write_text(COMPILED_HOOK, encoding="utf-8")
    program = tmp_path / "compiled-hook"
    subprocess.run([compiler, "-o", str(program), str(source)], check=True, capture_output=True, timeout=120)
    top, project = make_repo(tmp_path)
    (project / "pyt").write_bytes(FAKE_LAUNCHER.encode("ascii"))
    (top / ".topmark").write_text("", encoding="utf-8")
    (top / ".git" / "info" / "exclude").write_text(".topmark\n", encoding="utf-8")
    log = tmp_path / "hook.log"
    env = dict(git_env(), PT_HOOK_LOG=log.as_posix())
    target = top / ".git" / "hooks" / hooks.HOOK
    if how == "file":
        shutil.copy2(program, target)
    else:
        target.symlink_to(program)
    assert "runs first" in hooks.install(find(project, top), force=True)

    def commit(name: str, **extra: str) -> subprocess.CompletedProcess[str]:
        (project / name).write_text(name, encoding="utf-8")
        git(top, "add", "-A", env=env)
        return git(top, "commit", "-q", "-m", name, env={**env, **extra}, check=False, timeout=120)

    r = commit("one.txt")
    assert r.returncode == 0, r.stderr
    assert log.read_text(encoding="utf-8").splitlines() == ["compiled hook", "launcher hooks run from top"]
    log.unlink()
    assert commit("two.txt", PT_LOCAL_EXIT="1").returncode != 0  # the kept hook still blocks
    assert log.read_text(encoding="utf-8").splitlines() == ["compiled hook"]
    assert hooks.interpreter(hooks._read(target.parent / hooks.LOCAL)) == ""  # as the hook script tells it


@needs_git
@pytest.mark.skipif(IS_WINDOWS, reason="the x bit is POSIX's")
def test_a_kept_hook_without_its_x_bit_is_never_said_to_run(tmp_path: Path) -> None:
    """The hook script runs pre-commit.local only with its x bit, as git runs no hook without
    one: install --force and the status said a kept hook without it ran first, and it never did
    (git's own warning about it was gone too)."""
    top, project = make_repo(tmp_path)
    repo = find(project)
    target = repo.default_dir / hooks.HOOK
    target.write_bytes(b"#!/bin/sh\necho mine\nexit 1\n")
    target.chmod(0o644)
    msg = hooks.install(repo, force=True)
    assert "runs first" not in msg and "is not executable" in msg and "chmod +x .git/hooks/pre-commit.local" in msg
    passed, label, _ = hooks._status_line(make(), repo)
    assert passed is True and "first" not in label.split("chmod")[0] and "is not executable" in label
    (repo.default_dir / hooks.LOCAL).chmod(0o755)
    passed, label, _ = hooks._status_line(make(), repo)
    assert passed is True and f"(runs {hooks.LOCAL} first)" in label


@needs_git
@pytest.mark.parametrize("sub", ["", "apps/my app", "caf\u00e9"])
def test_git_runs_the_hook(tmp_path: Path, sub: str) -> None:
    """git runs the installed hook from the top: it calls `sh <launcher> hooks run`, chains a
    kept foreign hook first, blocks the commit on failure, explains a launcher that could not
    run the checks, and --no-verify skips it."""
    top, project = make_repo(tmp_path, sub)
    (project / "pyt").write_bytes(FAKE_LAUNCHER.encode("ascii"))  # no exec bit needed: the hook uses sh
    (top / ".topmark").write_text("", encoding="utf-8")
    (top / ".git" / "info" / "exclude").write_text(".topmark\n", encoding="utf-8")
    log = tmp_path / "hook.log"
    env = dict(git_env(), PT_HOOK_LOG=log.as_posix())
    hooks_dir = top / ".git" / "hooks"
    (hooks_dir / hooks.HOOK).write_bytes(LOCAL_HOOK.encode("ascii"))
    if not IS_WINDOWS:
        (hooks_dir / hooks.HOOK).chmod(0o755)
    hooks.install(find(project, top), force=True)

    def commit(name: str, *flags: str, **extra: str) -> subprocess.CompletedProcess[str]:
        (project / name).write_text(name, encoding="utf-8")
        git(top, "add", "-A", env=env)
        return git(top, "commit", "-q", "-m", name, *flags, env={**env, **extra}, check=False, timeout=120)

    assert commit("one.txt").returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["local hook", "launcher hooks run from top"]
    log.unlink()
    r = commit("two.txt", PT_HOOK_EXIT="1")  # checks failed: the runner already said why
    assert r.returncode != 0 and "pytemplate pre-commit" not in r.stderr
    assert log.read_text(encoding="utf-8").splitlines() == ["local hook", "launcher hooks run from top"]
    log.unlink()
    for code in ("2", "127"):  # a broken pytemplate.toml, uv not found from a GUI client...
        r = commit(f"x{code}.txt", PT_HOOK_EXIT=code)
        assert r.returncode != 0
        assert f"could not check this commit (exit code {code})" in r.stderr and "git commit --no-verify" in r.stderr
        log.unlink()
    assert commit("three.txt", PT_LOCAL_EXIT="1").returncode != 0
    assert log.read_text(encoding="utf-8").splitlines() == ["local hook"]  # stops before the checks
    log.unlink()
    assert commit("four.txt", "--no-verify", PT_HOOK_EXIT="1").returncode == 0
    assert not log.exists()
    (project / "pyt").unlink()  # a checkout without the launcher: the checks are skipped
    (hooks_dir / hooks.LOCAL).unlink()
    assert commit("five.txt").returncode == 0
    assert git(top, "log", "--format=%s", env=env).stdout.split() == ["five.txt", "four.txt", "one.txt"]
