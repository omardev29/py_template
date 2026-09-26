"""hooks [install [--force]|uninstall|run|status]: the git pre-commit hook of the project.

A native git hook (no pre-commit framework: only uv is needed). `install` writes a small sh
script, `pre-commit`, into the directory git runs hooks from (`git rev-parse --git-path
hooks`, which also handles linked worktrees). The script runs `sh <launcher> hooks run`, with
the POSIX launcher `deploy` given relative to the top of the work tree: git runs pre-commit
hooks from there, whatever client makes the commit (Git Bash, cmd, PowerShell, xonsh, VS Code,
lazygit...), and on Windows it runs them with Git's own sh.exe. `sh` is explicit so the exec
bit of `deploy` does not matter. When the launcher cannot run the checks (exit code > 1: uv not
found from a GUI client, a broken pytemplate.toml, a runner without `hooks run`), the script
says so and how to commit anyway. `./deploy setup` installs the hook when `hooks.pre_commit`
is true in pytemplate.toml (the default).

- A pre-commit hook that is not pytemplate's (no MARKER), or a symlink, is never overwritten:
  `install` fails, `install --force` keeps it as `pre-commit.local` and the new hook runs it
  first; `uninstall` removes only pytemplate's hook and puts the old one back.
- Another pytemplate project of the same repository (a monorepo with apps/p and apps/q) is
  "other": setup leaves its hook alone; `install --force` chains it the same way (a fresh copy
  of its hook becomes `pre-commit.local`, which never chains itself), so both checks run. That
  copy stays p's own ("chained"): `uninstall` in p (and apply with hooks.pre_commit = false)
  deletes it, never restores it, and `install` in p drops it once p's hook is back in
  `pre-commit` (q's went away), so p's checks never run twice.
- A hook pytemplate does not manage "calls" the checks only with a line that is not a comment
  and runs THIS project's launcher with `hooks run` (another project's line does not count).
- A project the enclosing repository ignores (`git check-ignore deploy`) gets no hook unless
  forced: that repository's commits never contain it.
- With `core.hooksPath` set (husky, a shared hooks folder...) git ignores `.git/hooks`: the
  hook is not installed there; `install` and `status` print the line to add to that setup
  (husky 9, core.hooksPath=.husky/_: to `.husky/pre-commit`).
- Linked worktrees share one hooks directory: the hook calls the launcher at the same
  relative path in every worktree, and a checkout without it skips the checks. A checkout
  whose runner predates `hooks run` (an old branch) fails the hook: commit there with
  `git commit --no-verify`.

`hooks run` checks what the commit contains (`git diff --cached`, deletions included), fast,
so no mypy (that stays in `./deploy check`, the editors and CI):
  1. ruff check (the active backend's typing profile, like `check`) and ruff format --check
     on the staged .py/.pyi files under src/ and tests/. A file with unstaged changes is
     checked in its STAGED version (fed to ruff on stdin), and a staged file deleted from the
     working tree is reported;
  2. the generated files are up to date (`render --check`) and none has unstaged changes;
  3. pyproject.toml matches pytemplate.toml and uv.lock is up to date (`uv lock --check`);
  4. pytemplate.toml, pyproject.toml, uv.lock and the generated files are committed together:
     once one of them is in the commit, none of the three config files may keep unstaged
     changes (uv.lock without its pyproject.toml, generated files without pytemplate.toml);
  5. the mypyc rules (lintc) on staged compiled modules: errors only under the mypyc profile;
  6. the static launcher checks of `doctor` on staged launchers;
  7. [template repo] the language guard (test_no_spanish.offending_lines) on staged files.

Limits: the project-wide checks (2, 3) and the mypyc rules read the working tree, so they are
conservative: outdated or unstaged generated files block every commit until they are rendered
and staged (or stashed). `git commit --no-verify` skips the hook.

Git runs hooks from the top of the work tree and may export repository variables relative to
it (GIT_INDEX_FILE=.git/index) or GIT_DIR without GIT_WORK_TREE (linked worktrees: "the cwd is
the top"). The project may live in a subfolder, so `git_env` pins them as absolute paths for
the git calls made here, and `run` removes them from the environment of every other tool
(uv may run git for git dependencies: it must not see the hook's repository).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
import types
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import envs, lintc, mypyc, proc, render, ui
from .cmd_dev import _profile_file, only_flags
from .config import Config
from .project import IS_WINDOWS, ROOT, STATE_FILE, TEMPLATE, native_path
from .ui import DeployError

Check = Callable[[bool | None, str, str], None]

HOOK = "pre-commit"
LOCAL = "pre-commit.local"  # a foreign hook moved aside by `install --force`; ours runs it first
MARKER = "pytemplate pre-commit hook"
LAUNCHERS = ("deploy", "deploy.cmd", "deploy.ps1")
PY_SUFFIXES = (".py", ".pyi")
USAGE = "install [--force] | uninstall | run | status"
# Repository variables git exports to hooks, possibly relative to the top of the work tree
GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")
# ...and the ones removed from the environment of the tools `run` starts
GIT_REPO_VARS = (*GIT_LOCATION_VARS, "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_NAMESPACE")
ARG_LIMIT = 8000  # characters of file arguments per tool call (Windows command lines: 32767)
# The sources of the generated files and of the lock: committed together with them
CONFIG_FILES = ("pytemplate.toml", "pyproject.toml", "uv.lock")
# The staged changes the checks look at: added, copied, modified, renamed, type-changed
STAGED = "ACMRT"


class NotInGit(DeployError):
    """git is missing, or the project is not inside a git work tree: the hook does not apply
    (setup and doctor stay silent). Any other git failure is a plain DeployError."""


# --- the repository ------------------------------------------------------------------------------


def git_env(environ: Mapping[str, str], cwd: Path) -> dict[str, str]:
    """Return git's repository variables made absolute (relative ones are relative to `cwd`).

    GIT_DIR without GIT_WORK_TREE means "cwd is the top of the work tree", so GIT_WORK_TREE is
    pinned to `cwd` then: git calls from another folder (the project root) stay correct.
    """
    out: dict[str, str] = {}
    for key in GIT_LOCATION_VARS:
        value = native_path(environ.get(key, ""))
        if value:
            out[key] = value if os.path.isabs(value) else os.path.normpath(os.path.join(cwd, value))
    if "GIT_DIR" in out and "GIT_WORK_TREE" not in out:
        out["GIT_WORK_TREE"] = str(cwd)
    return out


def _git_process_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment of the git calls made here: `env` (git_env) is the only source of the
    repository variables; LC_ALL=C keeps git's messages English (find_repo reads them)."""
    base = {k: v for k, v in proc.base_env().items() if k not in GIT_REPO_VARS}
    return {**base, "LC_ALL": "C", **env}


# `diff.relative=true` (git 2.28+; any config file or GIT_CONFIG_PARAMETERS) makes `git diff`
# print paths relative to the project folder, which project_paths() would drop: a command-line
# -c wins over all of them, and older git ignores the unknown key.
GIT_CONFIG = ("-c", "diff.relative=false")


def _git(args: Sequence[str], cwd: Path, env: Mapping[str, str], *, literal: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git quietly; `env` (git_env) is the only source of the repository variables.
    `literal`: pathspecs are plain paths (a file named `*.py`); check-ignore refuses the option."""
    argv = ["git", *GIT_CONFIG, *(["--literal-pathspecs"] if literal else []), *args]
    return proc.run(argv, cwd=cwd, env=_git_process_env(env), capture=True, check=False, echo=False)


def _run_bytes(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run quietly with raw bytes in and out, like proc.run(echo=False).

    proc.run is text-only (its universal newlines would turn the CRLF the launcher checks look
    for into LF) and has no stdin; the staged content of a file, and ruff reading it on stdin,
    need both."""
    ui.detail("$ " + proc.show(argv))
    try:
        return subprocess.run(list(argv), cwd=cwd, env=dict(env), input=data, capture_output=True, check=False)
    except OSError as e:
        raise DeployError(f"cannot run {argv[0]}: {e}", 3) from None


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _within(path: Path, parent: Path) -> str | None:
    """Return `path` relative to `parent` as a POSIX string ("" for the same folder), or None."""
    rel = os.path.relpath(os.path.normpath(path), os.path.normpath(parent)) if _drive(path) == _drive(parent) else ".."
    if rel == os.curdir:
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


def _drive(path: Path) -> str:
    return os.path.splitdrive(os.path.normcase(str(path)))[0]


@dataclass(frozen=True)
class Repo:
    project: Path  # the project root (where ./deploy is)
    top: Path  # top of the git work tree
    hooks_dir: Path  # where git runs hooks from (core.hooksPath, or <common dir>/hooks)
    default_dir: Path  # <common dir>/hooks: where `install` writes
    prefix: str  # the project relative to `top`, POSIX ("" = the top itself)
    env: dict[str, str] = field(default_factory=dict)  # git_env() for every git call

    @property
    def custom_hooks_path(self) -> bool:
        """Whether core.hooksPath sends git elsewhere (git then ignores the default dir)."""
        return not _same(self.hooks_dir, self.default_dir)

    @property
    def launcher(self) -> str:
        """The POSIX launcher relative to the top of the work tree, as the hook calls it."""
        return f"./{self.prefix}/deploy" if self.prefix else "./deploy"

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _git(args, self.project, self.env)

    def hooks_path_value(self) -> str:
        return self.git("config", "--get", "core.hooksPath").stdout.strip()

    def ignored(self) -> bool:
        """Whether an enclosing repository ignores the project (never for its own top): its
        commits then never contain the project, and the hook would only slow them down."""
        if not self.prefix:
            return False
        # exit 0: ignored and untracked (a tracked file is never reported); 1: not; 128: an error
        return _git(["check-ignore", "-q", "deploy"], self.project, self.env, literal=False).returncode == 0


def find_repo(project: Path = ROOT, environ: Mapping[str, str] | None = None, cwd: Path | None = None) -> Repo:
    """Return the git repository of `project`. NotInGit when git is missing (3) or the project
    is not inside a git work tree (2); DeployError with git's own message for any other git
    failure (dubious ownership, a broken .git...)."""
    if shutil.which("git") is None:
        raise NotInGit("git not found in PATH", 3)
    env = git_env(os.environ if environ is None else environ, cwd or Path(os.getcwd()))
    r = _git(["rev-parse", "--show-toplevel", "--git-common-dir", "--git-path", "hooks"], project, env)
    lines = [native_path(ln) for ln in r.stdout.splitlines()]  # MSYS2's own git prints /c/...
    if r.returncode != 0 and "not a git repository" not in r.stderr:
        said = r.stderr.strip().splitlines() or [f"exit code {r.returncode}"]
        raise DeployError(f"git cannot use the repository of {project}:\n" + "\n".join(f"  {ln}" for ln in said), 2)
    if r.returncode != 0 or len(lines) != 3:
        raise NotInGit(f"{project} is not inside a git work tree (git init first)", 2)
    top = Path(lines[0])
    prefix = _within(project, top)
    if prefix is None:
        raise DeployError(f"the project {project} is not inside its git work tree {top}", 2)
    return Repo(
        project=project,
        top=top,
        hooks_dir=Path(os.path.normpath(project / lines[2])),  # relative ones are relative to the cwd
        default_dir=Path(os.path.normpath(project / lines[1] / "hooks")),
        prefix=prefix,
        env=env,
    )


# --- the hook script -----------------------------------------------------------------------------


def sh_literal(text: str) -> str:
    """Quote `text` for sh with ASCII only: '...' or, for non-ASCII, "$(printf '\\ooo...')"."""
    if text.isascii():
        return "'" + text.replace("'", "'\\''") + "'"
    fmt = "".join(chr(b) if 32 <= b < 127 and chr(b) not in "%\\'" else f"\\{b:03o}" for b in text.encode("utf-8"))
    return f"\"$(printf '{fmt}')\""


_LAUNCHER_LINE = re.compile(r"^_pt_launcher=(.*)$", re.MULTILINE)
_PRINTF_WORD = re.compile(r"\"\$\(printf '([^']*)'\)\"")


def launcher_of(text: str) -> str | None:
    """Return the launcher a pytemplate hook script calls (the reverse of sh_literal), or None."""
    m = _LAUNCHER_LINE.search(text)
    if not m:
        return None
    word = m.group(1).strip()
    if len(word) >= 2 and word[0] == word[-1] == "'":
        return word[1:-1].replace("'\\''", "'")
    p = _PRINTF_WORD.fullmatch(word)
    if p is None:
        return None
    raw = re.sub(r"\\([0-7]{3})", lambda o: chr(int(o.group(1), 8)), p.group(1))
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None


def hook_script(launcher: str) -> str:
    """Return the pre-commit hook: pure ASCII, LF, runs `sh <launcher> hooks run` from the top."""
    lines = [
        "#!/bin/sh",
        f"# {MARKER}: written by ./deploy hooks install (it rewrites this file: do not edit)",
        "# Runs `./deploy hooks run`: fast checks of the staged files (ruff, ruff format,",
        "# generated files, uv.lock, mypyc rules, launchers). mypy runs in ./deploy check.",
        "#   remove it:    ./deploy hooks uninstall",
        "#   skip it once: git commit --no-verify",
        f"# A hook that was here before is kept as {LOCAL} and runs first (unless this file is",
        f"# itself the {LOCAL} of another project's hook).",
        "case $0 in */*) _pt_dir=${0%/*} ;; *) _pt_dir=. ;; esac",
        f'if [ "${{0##*/}}" != {LOCAL} ] && [ -x "$_pt_dir/{LOCAL}" ]; then',
        f'    "$_pt_dir/{LOCAL}" "$@" || exit $?',
        "fi",
        f"_pt_launcher={sh_literal(launcher)}",
        'if [ ! -f "$_pt_launcher" ]; then',
        "    printf '%s\\n' \"pytemplate pre-commit: $_pt_launcher not found in this checkout: checks skipped\" >&2",
        "    exit 0",
        "fi",
        'sh "$_pt_launcher" hooks run',
        "_pt_rc=$?",
        'if [ "$_pt_rc" -gt 1 ]; then',
        "    printf '%s\\n' \"pytemplate pre-commit: $_pt_launcher could not check this commit (exit code $_pt_rc).\" \\",
        "        '  Commit without the checks: git commit --no-verify   Remove the hook: ./deploy hooks uninstall' >&2",
        "fi",
        'exit "$_pt_rc"',
    ]
    return "\n".join(lines) + "\n"


def run_line(repo: Repo) -> str:
    """The line to add to a hook that pytemplate does not manage (core.hooksPath)."""
    word = repo.launcher if re.fullmatch(r"[A-Za-z0-9_./-]+", repo.launcher) else sh_literal(repo.launcher)
    return f"sh {word} hooks run || exit $?"


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return ""


def _other_project(repo: Repo, launcher: str) -> bool:
    """Whether `launcher` (relative to the top, as a hook calls it) is another live project's."""
    if launcher == repo.launcher:
        return False
    path = repo.top / launcher
    if not path.is_file():
        return False  # a project that is gone (moved, renamed): its stale hook may be replaced
    try:
        return not os.path.samefile(path, repo.project / "deploy")
    except OSError:
        return True


def _is_this_launcher(word: str, repo: Repo) -> bool:
    """Whether `word` (a launcher as a hook line spells it: relative to the top of the work tree,
    where git runs hooks, or absolute) is this project's launcher. A word with a shell expansion
    ("$ROOT/deploy", husky's "$(dirname ...)") cannot be resolved: it counts as this project's."""
    if any(c in word for c in "$`") or word.startswith("~"):
        return True
    path = Path(native_path(word))
    if path.name not in LAUNCHERS:
        return False
    folder = (path if path.is_absolute() else repo.top / path).parent
    try:
        return os.path.samefile(folder, repo.project)
    except OSError:
        return _same(folder, repo.project)


# A launcher called with `hooks run`: `sh ./apps/p/deploy hooks run`, `sh 'my app/deploy' hooks run`...
_RUN_CALL = re.compile(r"""(?:"([^"]+)"|'([^']+)'|([^\s"';&|<>()]+))\s+hooks\s+run(?![\w-])""")


def runs_checks(text: str, repo: Repo) -> bool:
    """Whether a hook script runs THIS project's checks: pytemplate's own hook for this launcher,
    or a line that is not a comment and calls this project's launcher with `hooks run` (a line
    of another project of the repository, or a commented-out one, does not count)."""
    if MARKER in text and (launcher := launcher_of(text)) is not None:
        return _is_this_launcher(launcher, repo)
    for line in text.splitlines():
        code = line.strip()
        if not code or code.startswith("#"):
            continue
        if any(_is_this_launcher(m.group(1) or m.group(2) or m.group(3), repo) for m in _RUN_CALL.finditer(code)):
            return True
    return False


def classify(path: Path, repo: Repo) -> str:
    """missing | installed | outdated (this project's, other content) | other (another
    pytemplate project of this repository: its launcher exists) | calls (another hook that runs
    this project's `hooks run`) | foreign.

    A symlink is never pytemplate's (install writes a regular file): writing through it,
    dangling or not, would create or change its target, often a file of the work tree."""
    if path.is_symlink():
        return "calls" if runs_checks(_read(path), repo) else "foreign"
    if not path.is_file():
        return "missing"
    text = _read(path)
    if MARKER in text:
        if text == hook_script(repo.launcher):
            return "installed"
        other = launcher_of(text)
        return "other" if other is not None and _other_project(repo, other) else "outdated"
    return "calls" if runs_checks(text, repo) else "foreign"


def own_local(repo: Repo) -> bool:
    """Whether pre-commit.local is this project's own hook: the copy another project's
    `install --force` chained after its hook (a regular file whose launcher is this one)."""
    local = repo.default_dir / LOCAL
    if local.is_symlink() or not local.is_file():
        return False
    text = _read(local)
    launcher = launcher_of(text) if MARKER in text else None
    return launcher is not None and _is_this_launcher(launcher, repo)


def hook_state(repo: Repo) -> str:
    """classify() of pre-commit in the default hooks folder, or "chained": that is another
    project's hook, and it runs this project's own from pre-commit.local."""
    state = classify(repo.default_dir / HOOK, repo)
    return "chained" if state == "other" and own_local(repo) else state


def hooks_path_runner(repo: Repo) -> str | None:
    """With core.hooksPath: the hook git runs there (as shown to the user) when it already runs
    this project's checks, else None."""
    hook = _hooks_path_file(repo)
    return _show(hook, repo) if classify(hook, repo) in ("installed", "calls") else None


def _show(path: Path, repo: Repo) -> str:
    rel = _within(path, repo.project)
    return rel if rel else path.as_posix()


def _hooks_path_file(repo: Repo) -> Path:
    """The pre-commit script of a core.hooksPath setup that should run pytemplate's line.
    husky 9 points core.hooksPath at .husky/_, whose generated pre-commit (it sources `h`) runs
    the user's .husky/pre-commit: that one is the file to check and to edit."""
    husky = repo.hooks_dir.name == "_" and any((repo.hooks_dir / name).is_file() for name in ("h", "husky.sh"))
    return repo.hooks_dir.parent / HOOK if husky else repo.hooks_dir / HOOK


def _hooks_path_hint(repo: Repo) -> str:
    target = _hooks_path_file(repo)
    runs = "husky runs it" if target.parent != repo.hooks_dir else "a sh script; git runs it from the top of the work tree"
    return (
        f"Add this line to {_show(target, repo)} ({runs}),\n"
        f"or run it from the tool that manages that folder:\n    {run_line(repo)}"
    )


def _ignored_message(repo: Repo) -> str:
    return (
        f"the git repository at {repo.top} ignores this project (git check-ignore deploy): its commits "
        "never contain it. git init the project to give it its own repository"
    )


def _write_hook(path: Path, text: str) -> None:
    path.write_bytes(text.encode("ascii"))  # bytes: LF on every OS
    if not IS_WINDOWS:
        path.chmod(0o755)


def install(repo: Repo, *, force: bool = False) -> str:
    """Install or update the hook; return the message to print (DeployError if it cannot)."""
    if repo.custom_hooks_path:
        hook = _hooks_path_file(repo)
        state = classify(hook, repo)
        if state in ("calls", "installed"):
            return f"{_show(hook, repo)} already runs ./deploy hooks run (core.hooksPath)"
        raise DeployError(
            f"core.hooksPath = {repo.hooks_path_value()!r}: git runs the hooks in {_show(repo.hooks_dir, repo)}, "
            "not in the default folder, so pytemplate does not install its hook there.\n"
            + "\n".join(f"  {line}" for line in _hooks_path_hint(repo).splitlines())
        )
    target = repo.default_dir / HOOK
    local = repo.default_dir / LOCAL
    script = hook_script(repo.launcher)
    state = classify(target, repo)
    if state == "missing" and not force and repo.ignored():
        raise DeployError(f"{_ignored_message(repo)} (or: ./deploy hooks install --force)")
    other = launcher_of(_read(target)) if state == "other" else None
    if other is not None and own_local(repo):
        return f"{_show(target, repo)} ({other}) already runs this project's checks from {_show(local, repo)}"
    # this project's own copy as pre-commit.local (its chain's first hook went away): with this
    # project's hook back in pre-commit, it would run the checks twice
    drop = state in ("missing", "outdated", "installed") and own_local(repo)
    moved = False
    if state in ("foreign", "calls", "other"):
        if state == "calls" and not force:
            return f"{_show(target, repo)} already runs ./deploy hooks run (not pytemplate's file: left alone)"
        if not force:
            if other is not None:
                raise DeployError(
                    f"{_show(target, repo)} runs the checks of another project of this repository ({other}): left alone.\n"
                    f"  {chain_hint(repo)}"
                )
            raise DeployError(f"{_show(target, repo)} already exists and is not pytemplate's hook: left alone.\n  {chain_hint(repo)}")
        if os.path.lexists(local):  # lexists: a dangling link there is somebody's too
            raise DeployError(f"both {_show(target, repo)} and {_show(local, repo)} exist: merge them by hand, then ./deploy hooks install")
        moved = True
    elif state == "installed" and not drop:
        return f"pre-commit hook already installed: {_show(target, repo)}"
    dry = proc.DRY_RUN
    if not dry:
        repo.default_dir.mkdir(parents=True, exist_ok=True)
        if moved:
            os.replace(target, local)  # a symlink moves as a link: its target is never touched
            if other is not None:
                # A current copy of the other project's hook: as pre-commit.local it must not run
                # pre-commit.local (itself), which hooks written before that guard would do.
                _write_hook(local, hook_script(other))
        if drop:
            local.unlink()
        _write_hook(target, script)
    verb = {"missing": "installed", "outdated": "updated", "installed": "already installed"}.get(state, "installed")
    msg = f"pre-commit hook {'would be ' + verb if dry and state != 'installed' else verb}: {_show(target, repo)} -> sh {repo.launcher} hooks run"
    if moved:
        msg += f"\n  the previous hook {'would be' if dry else 'was'} kept as {_show(local, repo)} and runs first"
    elif drop:
        msg += f"\n  {'would remove' if dry else 'removed'} {_show(local, repo)}: a copy of this project's hook (the checks would run twice)"
    elif local.is_file():
        msg += f"\n  it runs {_show(local, repo)} first"
    return msg


def uninstall(repo: Repo) -> str:
    """Remove this project's hook (never another one) and restore the hook it had moved aside.
    This project's hook chained as pre-commit.local after another project's is removed too
    (never restored: it would run twice, or run with hooks.pre_commit = false)."""
    target = repo.default_dir / HOOK
    local = repo.default_dir / LOCAL
    state = classify(target, repo)
    dry = proc.DRY_RUN
    own_copy = own_local(repo)
    if state == "other":
        other = launcher_of(_read(target))
        if own_copy:
            if not dry:
                local.unlink()
            return f"{'would remove' if dry else 'removed'} {_show(local, repo)} (this project's checks); {_show(target, repo)} runs those of {other}: left alone"
        return f"{_show(target, repo)} runs the checks of another project ({other}): left alone"
    if state in ("foreign", "calls"):
        return f"{_show(target, repo)} is not pytemplate's hook: left alone"
    parts: list[str] = []
    if state in ("installed", "outdated"):
        if not dry:
            target.unlink()
        parts.append(f"{'would remove' if dry else 'removed'} {_show(target, repo)}")
    if own_copy:
        if not dry:
            local.unlink()
        parts.append(f"{'would remove' if dry else 'removed'} {_show(local, repo)} (a copy of this project's hook)")
    elif local.is_file() or local.is_symlink():  # a link moved aside by --force comes back as a link
        if not dry:
            os.replace(local, target)
        parts.append(f"{'would restore' if dry else 'restored'} the previous hook ({_show(local, repo)} -> {HOOK})")
    return "; ".join(parts) if parts else "no pytemplate pre-commit hook installed"


# --- status, setup, doctor -------------------------------------------------------------------------


def _status_line(cfg: Config, repo: Repo) -> tuple[bool | None, str, str]:
    """Return (passed, label, hint) describing the hook, for `status` and `doctor`."""
    if repo.custom_hooks_path:
        hook = _hooks_path_file(repo)
        state = classify(hook, repo)
        where = _show(hook, repo)
        if state in ("installed", "calls"):
            return True, f"git pre-commit hook: {where} runs ./deploy hooks run (core.hooksPath)", ""
        return None, f"git pre-commit hook: core.hooksPath = {repo.hooks_path_value()!r}, pytemplate's checks are not in {where}", _hooks_path_hint(repo)
    target = repo.default_dir / HOOK
    local = repo.default_dir / LOCAL
    state = classify(target, repo)
    chained = local.is_file()
    if state == "installed" and own_local(repo):
        return None, f"git pre-commit hook installed, but {LOCAL} is a copy of it: the checks run twice", "./deploy hooks install"
    if state == "installed":
        extra = f" (runs {LOCAL} first)" if chained else ""
        return True, f"git pre-commit hook installed: {_show(target, repo)} -> sh {repo.launcher} hooks run{extra}", ""
    if state == "outdated":
        return None, "git pre-commit hook outdated (another launcher path or template version)", "./deploy hooks install"
    if state == "calls":
        return True, f"git pre-commit hook: {_show(target, repo)} runs ./deploy hooks run", ""
    other = launcher_of(_read(target)) if state == "other" else None
    if other is not None and own_local(repo):
        return True, f"git pre-commit hook: {_show(target, repo)} runs this project's checks ({LOCAL}), then those of {other}", ""
    if repo.ignored():  # missing, foreign or another project's: none of ours, and this is why
        return None, f"git pre-commit hook not installed: the repository at {repo.top} ignores this project", (
            "git init the project to give it its own repository (or ./deploy hooks install --force)"
        )
    if other is not None:
        return None, f"git pre-commit hook: {_show(target, repo)} runs the checks of another project ({other}), not this one's", chain_hint(repo)
    if state == "foreign":
        return None, f"git pre-commit hook: {_show(target, repo)} is another tool's hook", chain_hint(repo)
    off = "" if cfg.hooks.pre_commit else "   (hooks.pre_commit = false: ./deploy setup does not install it)"
    return None, "git pre-commit hook not installed", "./deploy hooks install" + off


def chain_hint(repo: Repo) -> str:
    """How to add this project's checks to a pre-commit hook that is not its own."""
    local = repo.default_dir / LOCAL
    if os.path.lexists(local):  # install --force would refuse: it chains one hook only
        return f"{_show(local, repo)} exists as well, so ./deploy hooks install --force cannot chain it: merge the two by hand, then ./deploy hooks install --force"
    return f"./deploy hooks install --force keeps it as {LOCAL} (it runs first) and adds this project's checks"


def chain_advice(repo: Repo) -> str:
    """chain_hint in a few words, for the one-line messages of apply and setup."""
    if os.path.lexists(repo.default_dir / LOCAL):
        return f"{LOCAL} is taken too: ./deploy hooks status says what to do"
    return "./deploy hooks install --force runs both"


def show_status(cfg: Config, project: Path = ROOT) -> int:
    try:
        repo = find_repo(project)
    except NotInGit as e:
        ui.info(f"hooks: {e} (no git hook)")
        return 0
    ui.step(f"git hooks: {_show(repo.hooks_dir, repo)}")
    passed, label, hint = _status_line(cfg, repo)
    ui.check_line(passed, label, hint)
    if repo.custom_hooks_path and classify(repo.default_dir / HOOK, repo) in ("installed", "outdated"):
        ui.check_line(None, f"{_show(repo.default_dir / HOOK, repo)} is pytemplate's but inactive (core.hooksPath)", "./deploy hooks uninstall removes it")
    return 0


def ensure_installed(cfg: Config, project: Path = ROOT) -> None:
    """Called by ./deploy setup: install the hook if hooks.pre_commit and it is missing (or
    update this project's own). Prints at most one line and never fails setup."""
    if not cfg.hooks.pre_commit:
        return
    try:
        repo = find_repo(project)
    except NotInGit:
        return  # no git, or not a git work tree: nothing to do
    except DeployError as e:  # git refuses the repository (dubious ownership...): say why
        ui.warn(f"git pre-commit hook not installed: {e}")
        return
    try:
        if repo.custom_hooks_path:
            if classify(_hooks_path_file(repo), repo) not in ("installed", "calls"):
                ui.info("git pre-commit hook: core.hooksPath is set, not installed (./deploy hooks status says what to add)")
            return
        target = repo.default_dir / HOOK
        state = classify(target, repo)
        own_copy = own_local(repo)
        if state in ("missing", "foreign", "other") and not own_copy and repo.ignored():  # nothing of ours there: why
            ui.info(f"git pre-commit hook: not installed: {_ignored_message(repo)} (or: ./deploy hooks install --force)")
        elif state in ("missing", "outdated") or (state == "installed" and own_copy):
            lines = install(repo).splitlines()
            ui.ok("\n".join(lines if own_copy else lines[:1]))  # the removed copy is news
        elif state == "foreign":
            ui.info(f"git pre-commit hook: another tool's hook is installed, left alone ({chain_advice(repo)})")
        elif state == "other" and not own_copy:
            ui.info(
                f"git pre-commit hook: it runs the checks of another project of this repository "
                f"({launcher_of(_read(target))}), left alone ({chain_advice(repo)})"
            )
    except (DeployError, OSError) as e:
        ui.warn(f"git pre-commit hook not installed: {e}")


def doctor(cfg: Config, check: Check, project: Path = ROOT) -> None:
    """One line for ./deploy doctor: whether the hook is installed (nothing outside git)."""
    try:
        repo = find_repo(project)
    except NotInGit:
        return
    except DeployError as e:  # git refuses the repository (dubious ownership...): show why
        ui.step("git hook")
        first, _, rest = str(e).partition("\n")
        check(None, first, rest)
        return
    ui.step("git hook")
    check(*_status_line(cfg, repo))
    ci = ".github/workflows/ci.yml"
    if repo.prefix and (project / ci).is_file():  # new warns only when it creates a project there
        check(
            None,
            f"generated CI: {repo.prefix}/{ci} never runs (GitHub reads {repo.top.name}/.github/workflows only)",
            f"For CI, add a workflow to the repository that runs its steps in {repo.prefix} "
            f"(defaults.run.working-directory) and takes the artifacts from {repo.prefix}/dist/",
        )


# --- run: the checks -------------------------------------------------------------------------------


@dataclass
class Result:
    passed: bool | None  # None: not applicable
    label: str
    hint: str = ""
    output: str = ""  # tool output printed under the line
    warnings: list[str] = field(default_factory=list)  # printed with ui.warn
    errors: list[str] = field(default_factory=list)  # printed with ui.error


def project_paths(prefix: str, names: Iterable[str], *, ignore_case: bool = IS_WINDOWS) -> list[str]:
    """Map paths relative to the top of the work tree to paths relative to the project
    (`prefix` = the project relative to the top); paths outside the project are dropped."""
    head = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def fold(s: str) -> str:
        return s.casefold() if ignore_case else s

    out: list[str] = []
    for name in names:
        if not name:
            continue
        if not head:
            out.append(name)
        elif fold(name[: len(head)]) == fold(head) and len(name) > len(head):
            out.append(name[len(head) :])
    return out


def staged_files(repo: Repo, diff_filter: str = STAGED) -> list[str]:
    """Return the staged files, relative to the project: the ones whose new content is in the
    commit by default (what the per-file checks read); diff_filter="D": the staged deletions.
    --no-renames: a rename is its deletion plus its addition, so both paths are seen."""
    r = repo.git("diff", "--cached", "--name-only", "--no-renames", f"--diff-filter={diff_filter}", "-z")
    if r.returncode != 0:
        raise DeployError(f"git diff --cached failed: {r.stderr.strip()}")
    return project_paths(repo.prefix, r.stdout.split("\0"))


def unstaged_files(repo: Repo, paths: Sequence[str]) -> list[str]:
    """Return which of `paths` (relative to the project) have unstaged changes or are untracked
    (and not ignored): a commit made now would miss them."""
    if not paths:
        return []
    diff = repo.git("diff", "--name-only", "-z", "--", *paths)
    others = repo.git("ls-files", "--others", "--exclude-standard", "--full-name", "-z", "--", *paths)
    names = diff.stdout.split("\0") + others.stdout.split("\0")
    return sorted(set(project_paths(repo.prefix, names)))


def worktree_changes(repo: Repo) -> set[str]:
    """Return the project files whose working tree differs from the index (unstaged edits and
    files deleted from the working tree), relative to the project."""
    r = repo.git("diff", "--name-only", "--no-renames", "-z")
    return set(project_paths(repo.prefix, r.stdout.split("\0")))


def staged_blob(repo: Repo, path: str) -> bytes | None:
    """Return the staged content of `path` (relative to the project) as a checkout would write
    it (`--filters`: CRLF for `eol=crlf` files, whatever the index stores), or None.
    `:0:` (stage 0) keeps a path like `1:x.py` from reading as a stage number."""
    spec = ":0:" + (f"{repo.prefix}/{path}" if repo.prefix else path)
    try:
        r = _run_bytes(["git", *GIT_CONFIG, "cat-file", "--filters", spec], cwd=repo.project, env=_git_process_env(repo.env))
    except DeployError:
        return None
    return r.stdout if r.returncode == 0 else None


def python_files(staged: Sequence[str], dirs: Sequence[str]) -> list[str]:
    """Return the staged .py/.pyi files under the code dirs (src/, tests/)."""
    return [p for p in staged if p.endswith(PY_SUFFIXES) and "/" in p and p.split("/", 1)[0] in dirs]


def _batches(files: Sequence[str], limit: int = ARG_LIMIT) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for f in files:
        if batch and size + len(f) + 3 > limit:
            yield batch
            batch, size = [], 0
        batch.append(f)
        size += len(f) + 3
    if batch:
        yield batch


# `uv run --frozen`: the lock as it is. A stale uv.lock is the lock check's finding; with
# --locked, uv would refuse to start ruff and that would read as ruff findings too.
RUFF = ("run", "--quiet", "--frozen", "ruff")


def ruff(cfg: Config, args: Sequence[str | Path], files: Sequence[str]) -> tuple[int, str]:
    """Run `ruff ARGS FILES` in the tools environment (in batches); return (exit code, output).
    --quiet: uv's own notes (creating .venv, installing) are not ruff's findings."""
    code = 0
    out: list[str] = []
    for batch in _batches(files):
        try:
            r = envs.uv(envs.tool_env(cfg), [*RUFF, *args, *batch], capture=True, check=False, echo=False)
        except DeployError as e:  # uv missing or too old
            return e.code, str(e)
        code = max(code, r.returncode if r.returncode in (0, 1) else 2)  # 2: did not run (uv, a crash, a signal)
        out += [t.strip() for t in (r.stdout, r.stderr) if t.strip()]
    return code, "\n".join(out)


def ruff_staged(cfg: Config, args: Sequence[str | Path], path: str, data: bytes) -> tuple[int, str]:
    """Run `ruff ARGS` on the staged content of one file: fed on stdin, configured and
    reported as `path` (--stdin-filename), so per-file rules and excludes still apply."""
    env = envs.tool_env(cfg)
    try:
        uv_path = proc.find_uv()
        if not env.dir.exists():
            envs.require_min_uv(uv_path)
        argv = [uv_path, *RUFF, *(str(a) for a in args), "--stdin-filename", path, "-"]
        r = _run_bytes(argv, cwd=ROOT, env=envs.env_vars(env), data=data)
    except DeployError as e:
        return e.code, str(e)
    out = [t.decode("utf-8", errors="replace").strip() for t in (r.stdout, r.stderr)]
    return r.returncode, "\n".join(t for t in out if t)


def uv_lock_check(cfg: Config) -> tuple[int, str]:
    try:
        r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], capture=True, check=False, echo=False)
    except DeployError as e:  # uv missing or too old
        return e.code, str(e)
    return r.returncode, (r.stderr.strip() or r.stdout.strip())


def _run_ruff(cfg: Config, args: Sequence[str | Path], whole: Sequence[str], as_staged: Mapping[str, bytes]) -> tuple[int, str]:
    """ruff on the files read from the working tree plus the ones fed as staged; the exit
    code is 0, 1 (findings) or 2 (ruff did not run: uv or ruff failed, or a signal)."""
    code = 0
    outs: list[str] = []
    runs: list[tuple[int, str]] = [ruff(cfg, args, whole)] if whole else []
    for path, data in as_staged.items():
        c, o = ruff_staged(cfg, args, path, data)
        if c == 1 and not o and args and args[0] == "format":
            o = f"Would reformat: {path} (its staged version)"  # ruff prints nothing for stdin
        runs.append((c, o))
    for c, o in runs:
        code = max(code, c if c in (0, 1) else 2)
        if o:
            outs.append(o)
    return code, "\n".join(outs)


def check_ruff(cfg: Config, files: Sequence[str], as_staged: Mapping[str, bytes] | None = None) -> Iterator[Result]:
    """ruff check and ruff format --check on the staged Python files `files`: the ones in
    `as_staged` (path -> staged content: they have unstaged changes) through stdin, the others
    by path (their working tree is what is staged)."""
    if not files:
        yield Result(None, "ruff: no staged Python file")
        return
    staged_ = {p: b for p, b in (as_staged or {}).items() if p in files}
    whole = [p for p in files if p not in staged_]
    profile = cfg.profile_for()
    data = render.load_profile(profile)
    exit_zero = bool(data.get("ruff", {}).get("exit_zero"))
    config_file = _profile_file(cfg, profile, "ruff")
    n = f"{len(files)} file{'s' if len(files) != 1 else ''}"
    note = f"; {len(staged_)} with unstaged changes, checked as staged" if staged_ else ""
    partial_hint = "\n(a file with unstaged changes was checked as staged: stage only the fix, e.g. git add -p)" if staged_ else ""
    common: list[str | Path] = ["--config", config_file, "--force-exclude", "--output-format", "concise"]
    code, out = _run_ruff(cfg, ["check", *common, *(["--exit-zero"] if exit_zero else [])], whole, staged_)
    if code > 1:
        yield Result(False, "ruff check: could not run ruff (the output says why)", output=out)
    elif code == 0 and exit_zero and "Found " in out:
        yield Result(True, f"ruff check: {n}, warnings only (profile '{profile}'{note})", output=out)
    elif code == 0:
        yield Result(True, f"ruff check: {n} (profile '{profile}'{note})")
    else:
        yield Result(False, f"ruff check: {n} (profile '{profile}'{note})", "./deploy lint --fix fixes some; then git add" + partial_hint, output=out)
    code, out = _run_ruff(cfg, ["format", "--check", *common], whole, staged_)
    if code > 1:
        yield Result(False, "ruff format: could not run ruff (the output says why)", output=out)
    elif code == 0:
        yield Result(True, f"ruff format: {n} formatted" + (f" ({note[2:]})" if note else ""))
    else:
        yield Result(False, "ruff format: files need formatting", "./deploy fmt, then git add" + partial_hint, output=out)


def check_generated(cfg: Config, generated: Sequence[str], dirty: set[str]) -> Iterator[Result]:
    """The generated files match their sources, and none has unstaged changes. Conservative:
    this blocks even a commit that touches none of them, because the generator also reads the
    runner and the templates. The hints name the config files with unstaged changes too, so
    following them never commits generated files without their source."""
    changed, edited = render.apply(cfg, check=True)
    sources = [p for p in CONFIG_FILES if p in dirty]
    if changed or edited:
        hints = []
        if changed:
            hints.append(f"outdated: {', '.join(changed)}\n./deploy render, then git add {' '.join([*sources, *changed])}")
        if edited:
            hints.append(f"hand-edited: {', '.join(edited)}\nchange pytemplate.toml or .pytemplate/templates (./deploy render --diff), or ./deploy render --force")
        yield Result(False, "generated files up to date", "\n".join(hints))
    else:
        yield Result(True, "generated files up to date")
    missed = [p for p in generated if p in dirty]
    if missed:
        also = f" (their source {', '.join(sources)} too)" if sources else ""
        yield Result(False, "generated files staged", f"unstaged: {', '.join(missed)}{also}\ngit add {' '.join([*sources, *missed])}")
    else:
        yield Result(True, "generated files staged")


def check_lock(cfg: Config) -> Result:
    """pyproject.toml matches pytemplate.toml and uv.lock matches pyproject.toml (working tree).

    "Matches" includes what only ./deploy apply brings in line (a hand-edited app.name, the
    [preset.*] options, a hand-edited app.preset): cmd_apply.pending, without its git check."""
    from . import cmd_apply  # lazy: cmd_apply imports this module

    hints: list[str] = []
    if render.pyproject_outdated(cfg):
        hints.append("pyproject.toml does not match pytemplate.toml: ./deploy apply")
    hints += [f"{problem}: {hint}" for problem, hint in cmd_apply.pending(cfg, hook=False)]
    code, out = uv_lock_check(cfg)
    if code != 0:
        hints.append(f"uv lock --check: {envs.uv_error(out) or f'exit code {code}'}\n./deploy lock")
    if hints:
        return Result(False, "pyproject.toml and uv.lock", "\n".join(hints))
    return Result(True, "pyproject.toml and uv.lock up to date")


def check_together(group: Sequence[str], touched: set[str], deleted: set[str], dirty: set[str]) -> Result:
    """Once any file of `group` (the config files and the generated ones) is in the commit, the
    config files are committed together: none may keep unstaged changes, or HEAD pairs a new
    file with an old one (uv.lock without its pyproject.toml fails `uv run --locked`; generated
    files without pytemplate.toml fail `render --check`). Unstaged generated files are
    check_generated's finding."""
    in_commit = [p for p in group if p in touched]
    if not in_commit:
        return Result(None, "config files: not in this commit")
    missed = [p for p in CONFIG_FILES if p in dirty]
    if not missed:
        return Result(True, "config files staged together")
    hints = [f"in the commit: {', '.join(in_commit)}"]
    kept = [p for p in missed if p in deleted]  # `git rm --cached`: gone from the commit, still on disk
    if kept:
        hints.append(f"deleted in the commit but still in the working tree: {', '.join(kept)}\ngit add {' '.join(kept)} (or delete them)")
    rest = [p for p in missed if p not in deleted]
    if rest:
        hints.append(f"unstaged changes: {', '.join(rest)} (they are committed together)\ngit add {' '.join(rest)}")
    return Result(False, f"config files staged together: {', '.join(missed)} not staged", "\n".join(hints))


def check_mypyc(cfg: Config, project: Path, staged: set[str]) -> Result:
    if not cfg.supports("mypyc"):
        return Result(None, "mypyc rules: mypyc is not in backend.supported")
    try:
        sources = mypyc.compiled_sources(cfg)
    except DeployError as e:
        return Result(False, "mypyc rules", str(e))
    files = [p for p in sources if _within(p, project) in staged]
    if not files:
        return Result(None, "mypyc rules: no staged compiled module")
    findings = [str(f) for f in lintc.lint(cfg, files)]
    strict = cfg.profile_for() == "mypyc"
    if not findings:
        return Result(True, f"mypyc rules: {lintc.describe(files)}")
    if strict:
        return Result(False, f"mypyc rules: {len(findings)} problem(s)", "fix them, or move the code to a boundary module", errors=findings)
    return Result(True, f"mypyc rules: {len(findings)} warning(s) (non-blocking with profile '{cfg.profile_for()}')", warnings=findings)


def _index_modes(repo: Repo, names: Sequence[str]) -> dict[str, str]:
    r = repo.git("ls-files", "-s", "--", *names)
    modes: dict[str, str] = {}
    for line in r.stdout.splitlines():
        meta, _, path = line.partition("\t")
        if meta and path:
            modes[path.rsplit("/", 1)[-1]] = meta.split()[0]
    return modes


def _worktree_bytes(project: Path) -> Callable[[str], bytes | None]:
    def read(rel_path: str) -> bytes | None:
        try:
            return (project / rel_path).read_bytes()
        except OSError:
            return None

    return read


def check_launchers(repo: Repo, staged: set[str], content: Callable[[str], bytes | None] | None = None) -> Result:
    """The doctor's static launcher checks on the staged launchers (`content`: what the commit
    contains, the working tree by default)."""
    names = [n for n in LAUNCHERS if n in staged]
    if not names:
        return Result(None, "launchers: none staged")
    from .shells import launcher_problems  # the doctor's static checks (a big module: only here)

    read = content or _worktree_bytes(repo.project)
    modes = _index_modes(repo, names)
    problems: list[str] = []
    fixes: list[str] = []
    for name in names:
        data = read(name)
        if data is None:
            continue
        for problem, fix in launcher_problems(name, data, modes.get(name) if name == "deploy" else None):
            problems.append(f"{name}: {problem}")
            fixes.append(fix)
    if problems:
        return Result(False, "launchers: " + "; ".join(problems), "\n".join(dict.fromkeys(fixes)))
    return Result(True, f"launchers: {', '.join(names)}")


class _Anything:
    """Stands in for pytest while the language guard module is imported without it."""

    def __getattr__(self, name: str) -> _Anything:
        return self

    def __call__(self, *args: object, **kwargs: object) -> _Anything:
        return self


def load_language_guard(path: Path) -> types.ModuleType:
    """Import .pytemplate/tests/test_no_spanish.py (it imports pytest, which the runner lacks)."""
    spec = importlib.util.spec_from_file_location("_pytemplate_language_guard", path)
    if spec is None or spec.loader is None:
        raise DeployError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    shim = "pytest" not in sys.modules and importlib.util.find_spec("pytest") is None
    if shim:
        fake = types.ModuleType("pytest")
        setattr(fake, "mark", _Anything())  # pytest.mark.skipif(...) at module level
        sys.modules["pytest"] = fake
    try:
        spec.loader.exec_module(module)
    finally:
        if shim:
            sys.modules.pop("pytest", None)
    return module


def check_language(project: Path, staged: Sequence[str], guard_file: Path, content: Callable[[str], bytes | None] | None = None) -> Result:
    guard = load_language_guard(guard_file)
    offending: Callable[[str], list[tuple[int, str, str]]] = guard.offending_lines
    allowed: frozenset[str] = guard.ALLOWED_PATHS
    binary: frozenset[str] = guard.BINARY_SUFFIXES
    read = content or _worktree_bytes(project)
    found: list[str] = []
    checked = 0
    for rel_path in staged:
        if rel_path in allowed or Path(rel_path).suffix.lower() in binary:
            continue
        data = read(rel_path)
        if data is None:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        checked += 1
        found += [f"{rel_path}:{n}: {word!r}: {line[:100]}" for n, word, line in offending(text)]
    if found:
        return Result(False, f"language guard: {len(found)} line(s) not in English", "translate them, or add `lang: allow` to the line", output="\n".join(found[:50]))
    return Result(True, f"language guard: {checked} file(s) in English")


def checks(
    cfg: Config,
    repo: Repo,
    staged: Sequence[str],
    *,
    code_dirs: Sequence[str],
    template_repo: bool,
    deleted: Sequence[str] = (),
) -> Iterator[Result]:
    """Yield the result of every check, in order (the caller prints them as they come).
    `staged`: the files whose new content is in the commit; `deleted`: the staged deletions."""
    staged_set = set(staged)
    # staged files whose working tree differs (unstaged edits, deleted on disk): read the index
    partial = worktree_changes(repo) & staged_set
    worktree = _worktree_bytes(repo.project)

    def content(rel_path: str) -> bytes | None:
        """What the commit contains: the working tree, unless the file has unstaged changes."""
        return staged_blob(repo, rel_path) if rel_path in partial else worktree(rel_path)

    py = python_files(staged, code_dirs)
    missing = [p for p in py if not (repo.project / p).is_file()]
    present = [p for p in py if p not in missing]
    as_staged: dict[str, bytes] = {}
    for p in present:
        if p in partial and (blob := staged_blob(repo, p)) is not None:
            as_staged[p] = blob
    yield from check_ruff(cfg, present, as_staged)
    if missing:
        # git commits the staged version of a file deleted from the disk: almost always an accident
        yield Result(
            False,
            f"staged files missing from the working tree: {', '.join(missing)}",
            f"keep them: git restore {' '.join(missing)}\ndrop them from the commit: git rm --cached {' '.join(missing)}",
        )
    generated = sorted({*render.outputs(cfg), STATE_FILE.relative_to(ROOT).as_posix()})
    group = [*CONFIG_FILES, *generated]
    dirty = set(unstaged_files(repo, group))
    yield from check_generated(cfg, generated, dirty)
    yield check_lock(cfg)
    yield check_together(group, staged_set | set(deleted), set(deleted), dirty)
    yield check_mypyc(cfg, repo.project, staged_set)
    yield check_launchers(repo, staged_set, content)
    if template_repo:
        yield check_language(repo.project, staged, TEMPLATE / "tests" / "test_no_spanish.py", content)


def _print(result: Result) -> None:
    """The check line, then the tool output, then how to fix it."""
    ui.check_line(result.passed, result.label)
    for line in result.output.splitlines():
        ui.info(f"         {line}".rstrip())
    for w in result.warnings:
        ui.warn(w)
    for e in result.errors:
        ui.error(e)
    if result.passed is not True:
        for line in result.hint.splitlines():
            ui.info(f"         {line}")


def run(cfg: Config, repo: Repo) -> int:
    """Check the staged files; return 1 if any check failed."""
    start = time.perf_counter()
    for key in GIT_REPO_VARS:  # uv and ruff must not see the hook's repository (repo.env keeps them for git)
        os.environ.pop(key, None)
    staged = staged_files(repo)
    deleted = staged_files(repo, "D")
    # A commit that only deletes files (a generated file, uv.lock) still gets the project-wide checks
    if not staged and not deleted:
        ui.info("pre-commit: no staged file in this project: nothing to check")
        return 0
    n = len(staged) + len(deleted)
    gone = f", {len(deleted)} deleted" if deleted else ""
    ui.step(f"pre-commit: checking {n} staged file{'s' if n != 1 else ''}{gone}")
    dirs = [d for d in ("src", "tests") if (repo.project / d).is_dir()]
    failed = 0
    template_repo = (TEMPLATE / "template-repo").is_file()
    for result in checks(cfg, repo, staged, code_dirs=dirs, template_repo=template_repo, deleted=deleted):
        _print(result)
        failed += result.passed is False
    seconds = time.perf_counter() - start
    if failed:
        ui.error(
            f"pre-commit: {failed} check{'s' if failed != 1 else ''} failed ({seconds:.1f} s). Fix, `git add` and commit again\n"
            "  (skip the hook once: git commit --no-verify)"
        )
        return 1
    ui.ok(f"pre-commit: all checks passed ({seconds:.1f} s)")
    return 0


# --- the command ---------------------------------------------------------------------------------


def cmd_hooks(cfg: Config, args: list[str]) -> int:
    """hooks [install [--force]|uninstall|run|status]"""
    sub, rest = (args[0], args[1:]) if args else ("status", [])
    allowed: dict[str, tuple[str, ...]] = {"install": ("--force",), "uninstall": (), "run": (), "status": ()}
    if sub not in allowed:
        raise DeployError(f"hooks: unknown subcommand {sub!r}  ({USAGE})")
    flags = only_flags(f"hooks {sub}", rest, allowed[sub])
    if sub == "status":
        return show_status(cfg, ROOT)  # explicit: a default argument is bound at import time
    repo = find_repo(ROOT)
    if sub == "run":
        return run(cfg, repo)
    if sub == "install":
        ui.ok(install(repo, force="--force" in flags))
    else:
        ui.info(uninstall(repo))
    return 0
