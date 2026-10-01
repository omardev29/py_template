"""hooks [install [--force]|uninstall|run|status]: the git pre-commit hook of the project.

A native git hook (no pre-commit framework: only uv is needed). `install` writes a small sh
script, `pre-commit`, into the directory git runs hooks from (`git rev-parse --git-path
hooks`, which also handles linked worktrees). The script runs `sh <launcher> hooks run`, with
the POSIX launcher `pyt` given relative to the top of the work tree: git runs pre-commit
hooks from there, whatever client makes the commit (Git Bash, cmd, PowerShell, xonsh, VS Code,
lazygit...), and on Windows it runs them with Git's own sh.exe. `sh` is explicit so the exec
bit of `pyt` does not matter. When the launcher cannot run the checks (exit code > 1: uv not
found from a GUI client, a broken pytemplate.toml, a runner without `hooks run`), the script
says so and how to commit anyway. `./pyt setup` installs the hook when `hooks.pre_commit`
is true in pytemplate.toml (the default).

- A pre-commit hook that is not pytemplate's (not what hook_script writes, is_ours: a comment
  that names pytemplate's hook does not make it so), or a symlink, is never overwritten:
  `install` fails, `install --force` keeps it as `pre-commit.local` and the new hook runs it
  first; `uninstall` removes only pytemplate's hook and puts the old one back.
- Another pytemplate project of the same repository (a monorepo with apps/p and apps/q) is
  "other": setup leaves its hook alone; `install --force` chains it the same way (a fresh copy
  of its hook becomes `pre-commit.local`, which never chains itself), so both checks run. That
  copy stays p's own ("chained"): `uninstall` in p (and apply with hooks.pre_commit = false)
  deletes it, never restores it, and `install` in p drops it once p's hook is back in
  `pre-commit` (q's went away), so p's checks never run twice.
- A hook pytemplate does not manage "calls" the checks only with a command that is not a comment
  and runs THIS project's launcher with `hooks run` (runs_checks: global options may come
  between, a relative launcher is read from the top or the folder a `cd` moved to, a launcher
  built from an expansion cannot be resolved and counts; another project's command does not).
- A project the enclosing repository ignores (`git check-ignore pyt`) gets no hook unless
  forced: that repository's commits never contain it.
- With `core.hooksPath` set (husky, a shared hooks folder...) git ignores `.git/hooks`: the
  hook is not installed there; `install` and `status` print the line to add to that setup
  (husky 9, core.hooksPath=.husky/_: to `.husky/pre-commit`), unless the hook there runs the
  checks already: pytemplate's hook of an older version that calls this launcher is "stale"
  (hooks_path_state), reported as outdated, never as missing. The same for a `.git/hooks` that
  is a link or junction to another folder (a team's tracked `.githooks`): nothing is written
  through it, and `uninstall` removes only a hook of pytemplate's there that git does not track,
  in a folder of this work tree (linked_hands_off).
- Linked worktrees share one hooks directory: the hook calls the launcher at the same
  relative path in every worktree, and a checkout without it skips the checks. A checkout
  whose runner predates `hooks run` (an old branch) fails the hook: commit there with
  `git commit --no-verify`.

`hooks run` checks what the commit contains (`git diff --cached`, deletions included), fast,
so no mypy (that stays in `./pyt check`, the editors and CI):
  1. ruff check (the active backend's typing profile, like `check`) and ruff format --check
     on the staged .py/.pyi/.ipynb files under src/ and tests/. A file with unstaged changes is
     checked in its STAGED version (fed to ruff on stdin), and a staged file deleted from the
     working tree is reported;
  2. the generated files are up to date (`render --check`) and none has unstaged changes (an
     untracked one that git ignores, or changes a skip-worktree or assume-unchanged flag hides,
     count too: the commit needs them whatever git is told to overlook);
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
the git calls made here (and `_found_from` lets git find a GIT_DIR from the project folder
itself, whose top a user's hook that ran `cd` first no longer is), and `run` removes them from
the environment of every other tool (uv may run git for git dependencies: it must not see the
hook's repository).
"""

from __future__ import annotations

import functools
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import time
import types
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import envs, lintc, mypyc, proc, render, ui
from .cmd_dev import _profile_file, config_arg, only_flags
from .config import Config
from .project import IS_WINDOWS, ROOT, STATE_FILE, TEMPLATE, native_path
from .ui import PytError

Check = Callable[[bool | None, str, str], None]

HOOK = "pre-commit"
LOCAL = "pre-commit.local"  # a foreign hook moved aside by `install --force`; ours runs it first
MARKER = "pytemplate pre-commit hook"
# The second line of every hook hook_script has written (`./deploy hooks install` before the
# launchers were renamed). With the `#!/bin/sh` line before it and a `_pt_launcher=` line
# launcher_of reads, it makes a hook pytemplate's (is_ours): the phrase alone does not, since a
# hook of the user's that calls the checks names the tool in a comment.
HEADER = f"# {MARKER}: written by ./pyt hooks install (it rewrites this file: do not edit)"
_OURS = re.compile(rf"#!/bin/sh\n# {MARKER}: written by \./(?:pyt|deploy) hooks install \(it rewrites this file: do not edit\)\n")
LAUNCHERS = ("pyt", "pyt.cmd", "pyt.ps1")
# The files ruff checks and formats: notebooks too, as `./pyt check`, `lint`, `fmt` and CI do
PY_SUFFIXES = (".py", ".pyi", ".ipynb")
USAGE = "install [--force] | uninstall | run | status"
# Repository variables git exports to hooks, possibly relative to the top of the work tree
GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")
# ...and the ones removed from the environment of the tools `run` starts
GIT_REPO_VARS = (*GIT_LOCATION_VARS, "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_NAMESPACE")
# The user's pathspec settings, never passed to the git calls made here: they pass
# --literal-pathspecs (a file named `*.py` is a path), which git refuses next to
# GIT_GLOB_PATHSPECS or GIT_ICASE_PATHSPECS (exit 128), and check-ignore refuses all four
PATHSPEC_VARS = ("GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS", "GIT_LITERAL_PATHSPECS")
ARG_LIMIT = 8000  # characters of file arguments per tool call (Windows command lines: 32767)
# The sources of the generated files and of the lock: committed together with them
CONFIG_FILES = ("pytemplate.toml", "pyproject.toml", "uv.lock")
# The staged changes the checks look at: added, copied, modified, renamed, type-changed
STAGED = "ACMRT"


class NotInGit(PytError):
    """git is missing, or the project is not inside a git work tree: the hook does not apply
    (setup and doctor stay silent). Any other git failure is a plain PytError."""


NO_GIT = "git not found in PATH"  # NotInGit's message (code 3) when git is missing


def git_missing_here(project: Path = ROOT) -> bool:
    """git is not on PATH, yet a `.git` in the project or a folder above it says it lives in a
    repository: the hook can be neither checked nor installed nor removed (GitHub Desktop, Fork
    and SourceTree bring a git of their own, often not on PATH). apply said "not a git work
    tree: nothing to do" and doctor called hooks.pre_commit applied."""
    return proc.find_program("git") is None and any((d / ".git").exists() for d in (project, *project.parents))


# --- the repository ------------------------------------------------------------------------------


def git_env(environ: Mapping[str, str], cwd: Path) -> dict[str, str]:
    """Return git's repository variables, made absolute where git reads them from `cwd`.

    Without GIT_DIR git finds the repository itself and reads a relative GIT_INDEX_FILE (or
    object directory) from the top of the work tree, wherever it runs: those stay as they are.
    git hands a plain commit's hook GIT_INDEX_FILE=.git/index, and a user's hook that runs `cd
    apps/a && ./pyt hooks run` joined it to apps/a: every git call read a missing, empty
    index, and a first commit passed unchecked. GIT_DIR (a linked worktree) is read from the
    cwd, and GIT_DIR without GIT_WORK_TREE means "cwd is the top of the work tree": then the
    relative values are joined to `cwd` and GIT_WORK_TREE is pinned to it, so git calls from
    another folder (the project root) stay correct.
    """
    out: dict[str, str] = {}
    from_cwd = bool(native_path(environ.get("GIT_DIR", "")))
    for key in GIT_LOCATION_VARS:
        value = native_path(environ.get(key, ""))
        if value:
            out[key] = value if os.path.isabs(value) or not from_cwd else os.path.normpath(os.path.join(cwd, value))
    if "GIT_DIR" in out and "GIT_WORK_TREE" not in out:
        out["GIT_WORK_TREE"] = str(cwd)
    return out


def _git_process_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment of the git calls made here: `env` (git_env) is the only source of the
    repository variables; LC_ALL=C keeps git's messages English (find_repo reads them). The
    user's pathspec settings go (PATHSPEC_VARS): with GIT_ICASE_PATHSPECS=1 every call with a
    pathspec failed, and the checks it fed passed a commit that left files behind."""
    base = {k: v for k, v in proc.base_env().items() if k not in GIT_REPO_VARS and k not in PATHSPEC_VARS}
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
        # Windows: a bare name, never from the folder the commit was made in (proc.program)
        return subprocess.run([proc.program(argv[0]), *argv[1:]], cwd=cwd, env=dict(env), input=data, capture_output=True, check=False)
    except OSError as e:
        raise PytError(f"cannot run {argv[0]}: {e}", 3) from None


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _same_folder(a: Path, b: Path) -> bool:
    """Whether `a` and `b` are one folder, however each is spelled (case on a case-insensitive
    disk, a symlink on the way): the file system decides, never the text."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return _same(a, b)


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
    project: Path  # the project root (where ./pyt is)
    top: Path  # top of the git work tree
    hooks_dir: Path  # where git runs hooks from (core.hooksPath, or <common dir>/hooks, or where it links to)
    default_dir: Path  # <common dir>/hooks: where `install` writes
    prefix: str  # the project relative to `top`, POSIX ("" = the top itself)
    env: dict[str, str] = field(default_factory=dict)  # git_env() for every git call
    hooks_link: bool = False  # default_dir is a link or junction to hooks_dir (_link_target)

    @property
    def custom_hooks_path(self) -> bool:
        """Whether git runs the hooks from a folder pytemplate writes nothing into: core.hooksPath
        sends git elsewhere (git then ignores the default dir), or the default dir is a link to
        another folder (hooks_link: a team's tracked folder, `ln -s ../.githooks .git/hooks`, or
        one other repositories share), where install --force changed a tracked file."""
        return not _same(self.hooks_dir, self.default_dir)

    @property
    def launcher(self) -> str:
        """The POSIX launcher relative to the top of the work tree, as the hook calls it."""
        return f"./{self.prefix}/pyt" if self.prefix else "./pyt"

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
        return _git(["check-ignore", "-q", "pyt"], self.project, self.env, literal=False).returncode == 0

    @functools.cached_property
    def ignore_case(self) -> bool:
        """Whether paths of the work tree match whatever their case: always on Windows, and where
        git says the file system folds case (core.ignorecase, which git sets on macOS's default
        APFS). project_paths compared case-sensitively there, and a staged path spelled otherwise
        than the prefix was dropped: the checks were skipped without a word."""
        if IS_WINDOWS:
            return True
        return self.git("config", "--bool", "core.ignorecase").stdout.strip() == "true"


def _found_from(project: Path, env: dict[str, str], environ: Mapping[str, str]) -> dict[str, str]:
    """`env` (git_env), or git's own discovery from `project` when that finds the repository git
    exported as GIT_DIR without GIT_WORK_TREE. git's rule then makes the cwd the top of the work
    tree: right where git runs a hook (the top), wrong after the `cd` of a hook of the user's
    (`cd apps/a && ./pyt hooks run`, README) in a checkout whose .git is a file (a linked
    worktree, a submodule, a --separate-git-dir clone, where git exports an absolute GIT_DIR):
    the project folder became the top, its staged files read as none and every generated file as
    untracked, so every commit was refused. From the project folder git finds that very
    repository through the .git file, and the real top; the index git exported stays."""
    if "GIT_DIR" not in env or native_path(environ.get("GIT_WORK_TREE", "")):
        return env
    found = {k: v for k, v in env.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    r = _git(["rev-parse", "--absolute-git-dir"], project, found)
    where = native_path(r.stdout.strip()) if r.returncode == 0 else ""
    return found if where and _same_folder(Path(where), Path(env["GIT_DIR"])) else env


def find_repo(project: Path = ROOT, environ: Mapping[str, str] | None = None, cwd: Path | None = None) -> Repo:
    """Return the git repository of `project`. NotInGit when git is missing (3) or the project
    is not inside a git work tree (2); PytError with git's own message for any other git
    failure (dubious ownership, a broken .git...)."""
    if proc.find_program("git") is None:
        raise NotInGit(NO_GIT, 3)
    source = os.environ if environ is None else environ
    env = _found_from(project, git_env(source, cwd or Path(os.getcwd())), source)
    r = _git(["rev-parse", "--show-toplevel", "--git-common-dir", "--git-path", "hooks", "--show-prefix"], project, env)
    lines = r.stdout.splitlines()
    if r.returncode != 0 and "not a git repository" not in r.stderr:
        said = r.stderr.strip().splitlines() or [f"exit code {r.returncode}"]
        raise PytError(f"git cannot use the repository of {project}:\n" + "\n".join(f"  {ln}" for ln in said), 2)
    if r.returncode != 0 or len(lines) not in (3, 4):  # the prefix line is empty at the top
        raise NotInGit(f"{project} is not inside a git work tree (git init first)", 2)
    top = Path(native_path(lines[0]))  # MSYS2's own git prints /c/...
    # The prefix as git spells it (the folders' case on disk), checked to name the project's folder:
    # computed from ROOT (the spelling typed, which Path.resolve keeps on macOS) against git's top,
    # a cwd typed in another case on a case-insensitive disk put the project outside its own work
    # tree, and every hook command failed.
    prefix = lines[3].strip("/") if len(lines) == 4 else ""
    if not _same_folder(top / prefix, project):
        raise PytError(f"the project {project} is not inside its git work tree {top}", 2)
    lines = [native_path(ln) for ln in lines[:3]]
    hooks_dir = Path(os.path.normpath(project / lines[2]))  # relative ones are relative to the cwd
    default_dir = Path(os.path.normpath(project / lines[1] / "hooks"))
    # git names the default folder even when it is a link (a symlink, a junction): git runs the
    # hooks through it, and pytemplate writes into it as into core.hooksPath's folder: never
    linked = _link_target(default_dir) if _same(hooks_dir, default_dir) else None
    return Repo(
        project=project,
        top=top,
        hooks_dir=linked or hooks_dir,
        default_dir=default_dir,
        prefix=prefix,
        env=env,
        hooks_link=linked is not None,
    )


def _link_target(folder: Path) -> Path | None:
    """The folder that `folder`, a symbolic link or a junction, leads to (a missing one too), or
    None for a plain folder or none at all. realpath resolves both on every OS and resolves the
    folders above it the same way on both sides, so only a link AT `folder` tells them apart."""
    real = Path(os.path.realpath(folder))
    return None if _same(real, Path(os.path.realpath(folder.parent)) / folder.name) else real


# --- the hook script -----------------------------------------------------------------------------


def sh_literal(text: str) -> str:
    """Quote `text` for sh with ASCII only: '...' or, for non-ASCII, "$(printf '\\ooo...')"."""
    if text.isascii():
        return "'" + text.replace("'", "'\\''") + "'"
    fmt = "".join(chr(b) if 32 <= b < 127 and chr(b) not in "%\\'" else f"\\{b:03o}" for b in text.encode("utf-8"))
    return f"\"$(printf '{fmt}')\""


def shell_word(text: str) -> str:
    """`text` as one word of a sh script, ASCII only (sh_literal), as it is when nothing in it
    is special (run_line)."""
    return text if re.fullmatch(r"[A-Za-z0-9_./-]+", text) else sh_literal(text)


def pasteable(text: str) -> str:
    """`text` as one word a user can paste: as it is when plain; in double quotes when sh,
    PowerShell and cmd read nothing in them (no $, backtick, backslash, double quote, ! or %);
    else in sh's single quotes, which PowerShell reads alike (shlex.quote). Unquoted, the hints
    for a project in apps/R&D split there: `cd apps/R&D` ran `cd apps/R` in the background, then
    a command `D`."""
    if re.fullmatch(r"[A-Za-z0-9_./-]+", text):
        return text
    if not re.search(r'[$`\\"!%]', text):
        return f'"{text}"'
    return shlex.quote(text)


def script_word(text: str) -> str:
    """pasteable(text), in ASCII for the hook script: a non-ASCII path as sh_literal's
    `"$(printf ...)"`, which a POSIX shell pastes as the path."""
    return pasteable(text) if text.isascii() else sh_literal(text)


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


def is_ours(text: str) -> bool:
    """Whether a hook's text (LF, as _read gives it) is one hook_script wrote, this version's or an
    earlier one's: its first two lines and a `_pt_launcher=` line launcher_of reads. A file that
    only held MARKER (a hook of the user's whose comment names pytemplate's hook) was taken for
    this project's outdated hook: setup and apply replaced it and uninstall deleted it, no copy
    kept, and the checks it ran besides ours (a secrets scan) were gone."""
    return _OURS.match(text) is not None and launcher_of(text) is not None


# The shells a kept hook is sourced by, with $0 = <hooks>/pre-commit: a hook that picks its job
# from its own name (husky v4, yorkie: `basename "$0"`) or finds its helpers next to it ran as
# pre-commit.local and silently checked nothing. zsh is left out: it sets $0 to a sourced file.
# A kept hook without a #! line is a shell script when it is text (git runs it with sh), and a
# compiled program when its first 64 bytes hold a NUL byte (ELF, Mach-O, PE: git executes it):
# that one is executed too, never sourced (every commit failed with a shell syntax error).
# A kept pytemplate hook (another project's, chained by --force) is executed as well: it must run
# as pre-commit.local, which never chains itself. It is told from the others by what is_ours
# reads, as grep patterns (the header line; a `_pt_launcher=` line), never by MARKER alone: a
# husky v4 hook whose comment named pytemplate's hook ran as pre-commit.local and checked nothing.
SHELLS = ("sh", "bash", "dash", "ash", "ksh", "mksh", "yash")
_OURS_GREP = "'^" + HEADER.replace("./pyt", "\\./[a-z]*") + "$'"
CHAIN_LINES = (
    f'    _pt_local="$_pt_dir/{LOCAL}"',
    "    _pt_line=",
    '    IFS= read -r _pt_line < "$_pt_local" || :',
    "    case $_pt_line in *\"$(printf '\\r')\") _pt_line=${_pt_line%?} ;; esac",
    "    case $_pt_line in '#!'*) _pt_line=${_pt_line#??} ;; *) _pt_line=/bin/sh ;; esac",
    "    case $_pt_line in /bin/sh) od -An -tx1 -N 64 \"$_pt_local\" 2>/dev/null | grep -q ' 00' && _pt_line= ;; esac",
    '    _pt_line=${_pt_line#"${_pt_line%%[! ]*}"}',
    "    _pt_interp=${_pt_line%% *}",
    '    _pt_args=${_pt_line#"$_pt_interp"}',
    '    _pt_args=${_pt_args#"${_pt_args%%[! ]*}"}',
    "    _pt_split=",
    '    if [ "${_pt_interp##*/}" = env ]; then',
    # env's options (-S, -i, -u NAME...) and NAME=VALUE words come before the program it runs
    # (interpreter reads them alike); -S splits the rest of the line into words, as env does
    "        _pt_interp=",
    '        while [ -z "$_pt_interp" ] && [ -n "$_pt_args" ]; do',
    "            _pt_word=${_pt_args%% *}",
    '            _pt_args=${_pt_args#"$_pt_word"}',
    '            _pt_args=${_pt_args#"${_pt_args%%[! ]*}"}',
    "            case $_pt_word in",
    '                -u|-C|--unset|--chdir) _pt_args=${_pt_args#"${_pt_args%% *}"}; _pt_args=${_pt_args#"${_pt_args%%[! ]*}"} ;;',
    "                -S|--split-string) _pt_split=1 ;;",
    '                -S*|--split-string=*) _pt_split=1; _pt_args="${_pt_word#*[S=]} $_pt_args" ;;',
    "                -*|*=*) ;;",
    "                *) _pt_interp=$_pt_word ;;",
    "            esac",
    "        done",
    "    fi",
    '    _pt_args=${_pt_args%"${_pt_args##*[! ]}"}',
    f"    if grep -q {_OURS_GREP} \"$_pt_local\" 2>/dev/null && grep -q '^_pt_launcher=' \"$_pt_local\" 2>/dev/null; then",
    '        "$_pt_local" "$@" || exit $?',
    "    else",
    "        case ${_pt_interp##*/} in",
    f"            {'|'.join(SHELLS)})",
    '                if [ -z "$_pt_split" ]; then',
    '                    _PT_HOOK=$_pt_local "$_pt_interp" ${_pt_args:+"$_pt_args"} -c ". \\"\\$_PT_HOOK\\"" "$_pt_dir/pre-commit" "$@" || exit $?',
    "                else",
    # each word an argument of its own, before -c, and the hook's own arguments last
    "                    (",
    "                        _pt_n=$#",
    '                        while [ -n "$_pt_args" ]; do',
    "                            _pt_word=${_pt_args%% *}",
    '                            _pt_args=${_pt_args#"$_pt_word"}',
    '                            _pt_args=${_pt_args#"${_pt_args%%[! ]*}"}',
    '                            set -- "$@" "$_pt_word"',
    "                        done",
    '                        set -- "$@" -c ". \\"\\$_PT_HOOK\\"" "$_pt_dir/pre-commit"',
    '                        while [ "$_pt_n" -gt 0 ]; do',
    '                            set -- "$@" "$1"',
    "                            shift",
    "                            _pt_n=$((_pt_n - 1))",
    "                        done",
    '                        _PT_HOOK=$_pt_local "$_pt_interp" "$@"',
    "                    ) || exit $?",
    "                fi ;;",
    '            *) "$_pt_local" "$@" || exit $? ;;',
    "        esac",
    "    fi",
)
# env's options that take the next word as their value (env -u NAME, -C DIR)
_ENV_VALUE_OPTIONS = ("-u", "-C", "--unset", "--chdir")
_NAME_READS = re.compile(r"\$0\b|\$\{0\}|argv\[0\]|__FILE__|\$PROGRAM_NAME|process\.argv")


def _after_env(words: list[str]) -> list[str]:
    """The words of a #! line from the program env runs: env's options (-S, -i, -u NAME, -C DIR,
    `-Sbash`...) and NAME=VALUE words come first (`#!/usr/bin/env -S bash -e` named '-S')."""
    rest = list(words)
    while rest:
        word = rest.pop(0)
        if word in _ENV_VALUE_OPTIONS:
            rest = rest[1:]
        elif word.startswith(("--split-string=", "-S")) and word not in ("-S", "--split-string"):
            return [word.split("=", 1)[1] if word.startswith("--") else word[2:], *rest]
        elif not word.startswith("-") and "=" not in word:
            return [word, *rest]
    return []


def interpreter(text: str) -> str:
    """The program a hook's #! line names (after env and its options, as the hook script reads
    it); sh without one, and "" for a compiled hook (a NUL byte in its first 64 bytes: it runs
    by itself)."""
    first = text.split("\n", 1)[0].rstrip("\r")
    if not first.startswith("#!"):
        return "" if "\0" in text[:64] else "sh"
    words = first[2:].split()
    if words and words[0].rsplit("/", 1)[-1] == "env":
        words = _after_env(words[1:])
    return words[0].rsplit("/", 1)[-1] if words else "sh"


def reads_its_name(text: str) -> bool:
    """Whether a hook script that is not a shell script reads its own name: kept as
    pre-commit.local it runs under that name (only a shell script can be sourced as pre-commit).
    A compiled hook's bytes cannot say: it runs as pre-commit.local, as git would run it."""
    shell = interpreter(text)
    return shell != "" and shell not in SHELLS and _NAME_READS.search(text) is not None


def hook_script(launcher: str) -> str:
    """Return the pre-commit hook: pure ASCII, LF, runs `sh <launcher> hooks run` from the top."""
    lines = [
        "#!/bin/sh",
        HEADER,
        "# Runs `./pyt hooks run`: fast checks of the staged files (ruff, ruff format,",
        "# generated files, uv.lock, mypyc rules, launchers). mypy runs in ./pyt check.",
        "#   remove it:    ./pyt hooks uninstall",
        "#   skip it once: git commit --no-verify",
        f"# A hook that was here before is kept as {LOCAL} and runs first (unless this file is",
        f"# itself the {LOCAL} of another project's hook).",
        "case $0 in */*) _pt_dir=${0%/*} ;; *) _pt_dir=. ;; esac",
        f'if [ "${{0##*/}}" != {LOCAL} ] && [ -x "$_pt_dir/{LOCAL}" ]; then',
        *CHAIN_LINES,
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
        # the launcher itself, from the top where git runs hooks (a project in a subfolder has no
        # ./pyt there), as a word the user can paste (script_word); each `$` ends its quoted piece
        # (`$''`, the same text), so shellcheck sees no expansion in single quotes (SC2016)
        "        " + sh_literal(f"  Commit without the checks: git commit --no-verify   Remove the hook: sh {script_word(launcher)} hooks uninstall").replace("$", "$''") + " >&2",
        "fi",
        'exit "$_pt_rc"',
    ]
    return "\n".join(lines) + "\n"


def run_line(repo: Repo) -> str:
    """The line to add to a hook that pytemplate does not manage (core.hooksPath). It skips a
    checkout without the launcher, as pytemplate's own hook does (another branch): the unguarded
    `sh ./pyt hooks run || exit $?` in a global hooks folder failed every commit of every
    other repository ("cannot open ./pyt")."""
    word = shell_word(repo.launcher)
    return f"[ ! -f {word} ] || sh {word} hooks run || exit $?"


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return ""


def _in_this_project(launcher: str, repo: Repo) -> bool:
    """Whether the launcher a pytemplate hook calls (relative to the top, as the hook calls it) is
    in this project's folder, whatever its file name: a hook written before the launchers were
    renamed calls `deploy`, and it is this project's (outdated) hook, never another project's."""
    folder = (repo.top / launcher).parent  # samefile follows `..` as the kernel does
    try:
        return os.path.samefile(folder, repo.project)
    except OSError:
        return _same(folder, repo.project)


def _other_project(repo: Repo, launcher: str) -> bool:
    """Whether `launcher` (relative to the top, as a hook calls it) is another live project's:
    the file is there, or its folder still holds a runner (a project upgraded in place since the
    launchers were renamed: its hook calls `deploy`, and that project brings it up to date)."""
    if launcher == repo.launcher:
        return False
    target = repo.top / launcher
    runner = target.parent / ".pytemplate"
    if not target.is_file() and not any((runner / entry).is_file() for entry in ("pyt.py", "deploy.py")):
        return False  # a project that is gone (moved, renamed): its stale hook may be replaced
    return not _in_this_project(launcher, repo)


def _unresolved(word: str) -> bool:
    """A word with a shell expansion ("$ROOT/pyt", husky's "$(dirname ...)", `...`, ~)."""
    return any(c in word for c in "$`") or word.startswith("~")


def _is_this_launcher(word: str, repo: Repo, cwd: Path | None) -> bool:
    """Whether `word` (a launcher as a hook spells it: absolute, or relative to `cwd`, the top of
    the work tree where git runs hooks unless a `cd` moved it) is this project's launcher. A word
    with a shell expansion, or a relative one where the folder is not known (cwd None: a `cd`
    into an expansion), cannot be resolved: it counts as this project's."""
    if _unresolved(word):
        return True
    path = Path(native_path(word))
    if path.name not in LAUNCHERS:
        return False
    if not path.is_absolute():
        if cwd is None:
            return True
        path = cwd / path
    folder = path.parent  # samefile follows `..` as the kernel does
    try:
        return os.path.samefile(folder, repo.project)
    except OSError:
        return _same(folder, repo.project)


def _quoted_end(text: str, i: int) -> int:
    """The index after the `"` that closes a double-quoted string whose content starts at i."""
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == '"':
            return i + 1
        elif text.startswith("$(", i):
            i = _substitution_end(text, i + 2)
        elif text[i] == "`":
            i = _backquote_end(text, i + 1)
        else:
            i += 1
    return len(text)


def _substitution_end(text: str, i: int) -> int:
    """The index after the `)` that closes a `$(` whose content starts at i (quotes and nested
    brackets inside it included)."""
    depth = 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            end = text.find("'", i + 1)
            i = len(text) if end < 0 else end + 1
            continue
        if c == '"':
            i = _quoted_end(text, i + 1)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _backquote_end(text: str, i: int) -> int:
    """The index after the backquote that closes a `...` substitution whose content starts at i."""
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == "`":
            return i + 1
        else:
            i += 1
    return len(text)


def _shell_commands(text: str) -> list[tuple[tuple[int, ...], list[str]]]:
    """The simple commands of a sh script in order, each with the subshells it runs in (the
    numbers of their `(`, outermost first) and its words: the quotes removed, the expansions kept
    as written ($X, $(...), `...`). A command ends at ; & | < > and a newline, `(` and `)` open
    and close a subshell, comments are dropped. Enough to find the calls of a hook, not a sh
    parser (no here-documents, aliases or functions)."""
    commands: list[tuple[tuple[int, ...], list[str]]] = []
    words: list[str] = []
    word: list[str] = []
    started = False
    subshells: list[int] = []
    opened, i, n = 0, 0, len(text)

    def close_word() -> None:
        nonlocal started
        if started:
            words.append("".join(word))
        word.clear()
        started = False

    def close_command() -> None:
        close_word()
        if words:
            commands.append((tuple(subshells), list(words)))
        words.clear()

    while i < n:
        c = text[i]
        if c in " \t\r":
            close_word()
            i += 1
        elif c == "\\":
            if not text.startswith("\\\n", i):  # a backslash-newline continues the line
                word.append(text[i + 1 : i + 2])
                started = True
            i += 2
        elif c == "#" and not started:
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif c in ";&|<>\n()":
            close_command()
            if c == "(":
                opened += 1
                subshells.append(opened)
            elif c == ")" and subshells:
                subshells.pop()
            i += 1
        elif c == "'":
            end = text.find("'", i + 1)
            end = n if end < 0 else end
            word.append(text[i + 1 : end])
            started = True
            i = end + 1
        elif c == '"':
            end = _quoted_end(text, i + 1)
            inner = text[i + 1 : end - 1] if text[end - 1] == '"' and end - 1 > i else text[i + 1 : end]
            word.append(re.sub(r'\\([$`"\\])', r"\1", inner))
            started = True
            i = end
        elif text.startswith("$(", i) or c == "`":
            end = _substitution_end(text, i + 2) if c == "$" else _backquote_end(text, i + 1)
            word.append(text[i:end])
            started = True
            i = end
        else:
            word.append(c)
            started = True
            i += 1
    close_command()
    return commands


# ./pyt's global options, which come before the command (`sh ./pyt -q hooks run`)
_GLOBAL_OPTIONS = frozenset({"-v", "--verbose", "-q", "--quiet", "--no-render", "--dry-run"})
# Words that may come before a `cd` in the same command: `{ cd x; ...; }`, `if cd x; then ...`
_BEFORE_CD = frozenset({"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "builtin", "command"})


def _cd(words: list[str], cwd: Path | None) -> Path | None:
    """The folder after `cd ARGS` run from `cwd` (None: it cannot be known)."""
    args = [w for w in words[1:] if not (w.startswith("-") and w != "-")]  # cd -P DIR
    if len(args) != 1 or cwd is None or args[0] == "-" or _unresolved(args[0]):
        return None
    target = Path(native_path(args[0]))
    return Path(os.path.normpath(target if target.is_absolute() else cwd / target))


# Programs that run the command after them (`exec ./pyt hooks run`, `env X=1 ./pyt ...`)
_RUNNERS = frozenset({"exec", "env", "time", "nohup"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _before_the_program(word: str) -> bool:
    """Whether `word` may come before the program a command runs: a keyword (`if`, `!`), a
    shell or a program that runs it (`sh`, `/bin/bash`, `exec`, `env`), one of their options,
    or a variable assignment. Any other word is the program (`echo`, `printf`)."""
    name = word.rsplit("/", 1)[-1]
    return word in _BEFORE_CD or name in _RUNNERS or name in (*SHELLS, "zsh") or word.startswith("-") or _ASSIGNMENT.match(word) is not None


def _calls_launcher(words: list[str], repo: Repo, cwd: Path | None) -> bool:
    """Whether a command calls this project's launcher with `hooks run` (`sh ./pyt hooks run`,
    `./pyt -q hooks run`: the launcher's global options come before the command). The launcher
    must be the program the command runs: `echo Tip: also run ./pyt hooks run` only mentions
    it, and counted as running the checks."""
    for i in range(1, len(words) - 1):
        if words[i : i + 2] != ["hooks", "run"]:
            continue
        j = i - 1
        while j > 0 and words[j] in _GLOBAL_OPTIONS:
            j -= 1
        if words[j] not in _GLOBAL_OPTIONS and _is_this_launcher(words[j], repo, cwd) and all(map(_before_the_program, words[:j])):
            return True
    return False


def runs_checks(text: str, repo: Repo) -> bool:
    """Whether a hook script runs THIS project's checks: pytemplate's own hook for this launcher,
    or a command that is not a comment and calls this project's launcher with `hooks run`
    (_calls_launcher). A relative launcher is resolved from the top of the work tree, where git
    runs hooks, or from the folder a `cd` before it moved to (a `cd` in a subshell stays there);
    a launcher, or a `cd` folder, that holds a shell expansion cannot be resolved and counts.
    Another project's launcher, a commented-out line or a quoted string does not."""
    launcher = launcher_of(text) if is_ours(text) else None
    if launcher is not None:
        return _is_this_launcher(launcher, repo, repo.top)
    folders: dict[tuple[int, ...], Path | None] = {(): repo.top}  # the current folder of each (sub)shell
    for subshells, words in _shell_commands(text):
        if subshells not in folders:  # a subshell starts in the folder of the shell around it
            outer = subshells[:-1]
            while outer not in folders:
                outer = outer[:-1]
            folders[subshells] = folders[outer]
        start = next((k for k, w in enumerate(words) if w not in _BEFORE_CD), len(words))
        if words[start : start + 1] == ["cd"]:
            folders[subshells] = _cd(words[start:], folders[subshells])
        elif _calls_launcher(words, repo, folders[subshells]):
            return True
    return False


def classify(path: Path, repo: Repo) -> str:
    """missing | installed | outdated (this project's, other content) | other (another
    pytemplate project of this repository: its launcher exists) | calls (another hook that runs
    this project's `hooks run`) | foreign.

    A symlink is never pytemplate's (install writes a regular file): writing through it,
    dangling or not, would create or change its target, often a file of the work tree. Nor is a
    file hook_script did not write (is_ours), whatever its comments say."""
    if path.is_symlink():
        return "calls" if runs_checks(_read(path), repo) else "foreign"
    if not path.is_file():
        return "missing"
    text = _read(path)
    if is_ours(text):
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
    launcher = launcher_of(text) if is_ours(text) else None
    return launcher is not None and _in_this_project(launcher, repo)


def _own_local_outdated(repo: Repo) -> bool:
    """Whether this project's own pre-commit.local (own_local) is not its current hook: written
    by an older runner, or calling the launcher by its old name, `deploy`."""
    return _read(repo.default_dir / LOCAL) != hook_script(repo.launcher)


def hook_state(repo: Repo) -> str:
    """classify() of pre-commit in the default hooks folder, or "chained": that is another
    project's hook, and it runs this project's own from pre-commit.local."""
    state = classify(repo.default_dir / HOOK, repo)
    return "chained" if state == "other" and own_local(repo) else state


def active_skipped(repo: Repo) -> tuple[str, str] | None:
    """git_skips() of the hook git runs: core.hooksPath's (or a linked folder's) pre-commit
    script, else the default folder's pre-commit."""
    return git_skips(_hooks_path_file(repo) if repo.custom_hooks_path else repo.default_dir / HOOK, repo)


def hooks_path_state(repo: Repo) -> str:
    """classify() of the hook git runs in a custom hooks folder (core.hooksPath, a linked
    folder), or "stale": pytemplate's hook of an older version that calls this project's
    launcher. pytemplate writes nothing there, and such a hook (a team copied it into its
    folder, then took a newer template) runs the checks all the same: read as "outdated", status,
    install and setup said the checks were not in it and told to add the run line, which then
    ran them twice."""
    hook = _hooks_path_file(repo)
    state = classify(hook, repo)
    return "stale" if state == "outdated" and runs_checks(_read(hook), repo) else state


# The states of a custom folder's hook (hooks_path_state) that run this project's checks
RUNS_CHECKS = ("installed", "calls", "stale")


def stale_hint(repo: Repo) -> str:
    """How to bring a "stale" hook (hooks_path_state) up to date: pytemplate writes nothing in
    that folder, so the user (or the tool that manages it) does. The pre-commit.local next to it,
    which it runs first, must keep running."""
    local = _hooks_path_file(repo).parent / LOCAL
    keep = f", after a line that runs {_show(local, repo)} (it runs that first now)" if os.path.lexists(local) else ""
    return f"it still runs the checks; to bring it up to date, replace it with this line{keep} (pytemplate writes nothing in {_show(repo.hooks_dir, repo)}):\n{run_line(repo)}"


def hooks_path_runner(repo: Repo) -> str | None:
    """With core.hooksPath: the hook git runs there (as shown to the user) when it already runs
    this project's checks, else None."""
    return _show(_hooks_path_file(repo), repo) if hooks_path_state(repo) in RUNS_CHECKS else None


def _show(path: Path, repo: Repo) -> str:
    rel = _within(path, repo.project)
    return rel if rel else path.as_posix()


# What makes a file executable for Git for Windows' sh (the MSYS2 runtime on its noacl mounts,
# Cygwin's has_exec_chars): its first two bytes. git itself runs a hook there that starts with #!
WINDOWS_EXEC_MAGIC = (b"#!", b"MZ", b":\n")


def _not_run(hook: Path, shown: str) -> str | None:
    """None when the hook script runs the kept hook `hook` (shown as `shown`) first, else why
    not: it runs it only with its x bit (`[ -x ]`), as git runs no hook without one (install
    and status said it ran first, while git's own warning about it was gone). On Windows the
    file's first bytes are its x bit for Git's sh (WINDOWS_EXEC_MAGIC), where os.access says X_OK
    for every file: a kept hook without a #! line was said to run first, and was skipped."""
    if IS_WINDOWS:
        try:
            with open(hook, "rb") as f:
                magic = f.read(2)
        except OSError:
            magic = b""
        if magic in WINDOWS_EXEC_MAGIC:
            return None
        return f"{shown} has no #! line, so neither git nor this hook runs it: start it with #!/bin/sh to run it first"
    if os.access(hook, os.X_OK):
        return None
    return f"{shown} is not executable, so neither git nor this hook runs it: chmod +x {shown} to run it first"


def git_skips(hook: Path, repo: Repo) -> tuple[str, str] | None:
    """Why git does not run `hook`, the pre-commit of the folder git runs hooks from, and the fix,
    or None. git runs no hook without its x bit: it only prints a hint, and the commit goes
    through unchecked (a copy, an archive or a backup tool that drops modes). status and doctor
    said "[ok] installed" and install, setup and apply "already installed", while every commit
    went unchecked. Never on Windows, where git's access() ignores X_OK (compat/mingw.c) and runs
    the hook whatever its mode; nor for husky's .husky/pre-commit, which husky runs through sh."""
    if not (_same(hook.parent, repo.hooks_dir) or _same(hook.parent, repo.default_dir)):
        return None  # husky 9's .husky/pre-commit (_hooks_path_file)
    if IS_WINDOWS or not os.path.lexists(hook) or os.access(hook, os.X_OK):
        return None
    shown = _show(hook, repo)
    return f"{shown} is not executable", f"chmod +x {shown}"


def _hooks_path_file(repo: Repo) -> Path:
    """The pre-commit script of a core.hooksPath setup that should run pytemplate's line.
    husky 9 points core.hooksPath at .husky/_, whose generated pre-commit (it sources `h`) runs
    the user's .husky/pre-commit: that one is the file to check and to edit."""
    husky = repo.hooks_dir.name == "_" and any((repo.hooks_dir / name).is_file() for name in ("h", "husky.sh"))
    return repo.hooks_dir.parent / HOOK if husky else repo.hooks_dir / HOOK


def elsewhere(repo: Repo, *, value: bool = False) -> str:
    """Why pytemplate installs no hook where git runs them (custom_hooks_path), for the messages:
    the default folder is a link (hooks_link), or core.hooksPath is set (`value`: and to what)."""
    if repo.hooks_link:
        return f"{_show(repo.default_dir, repo)} is a link to {_show(repo.hooks_dir, repo)}"
    return f"core.hooksPath = {repo.hooks_path_value()!r}" if value else "core.hooksPath is set"


def _elsewhere_short(repo: Repo) -> str:
    return "a linked hooks folder" if repo.hooks_link else "core.hooksPath"


def linked_hands_off(repo: Repo) -> str | None:
    """Why uninstall, and apply with hooks.pre_commit = false, leave the hooks of a linked hooks
    folder (Repo.hooks_link) as they are, or None. The folder is outside this repository's work
    tree: other repositories may run its hooks; or git tracks its pre-commit or pre-commit.local:
    a team's shared folder, whose hooks are the team's, pytemplate's own script too (shared on
    purpose, or written there by an install older than the rule that leaves such a folder alone:
    either way only a commit of the team's may change it). uninstall deleted the tracked
    pre-commit and renamed the tracked pre-commit.local over it, and doctor advised it. None for a
    folder that is no link, or for one of this work tree whose hooks git does not track: what such
    an older install wrote there, which uninstall still removes."""
    if not repo.hooks_link:
        return None
    present = [p for p in (repo.hooks_dir / HOOK, repo.hooks_dir / LOCAL) if os.path.lexists(p)]
    top = Path(os.path.realpath(repo.top))
    inside = _within(Path(os.path.realpath(repo.hooks_dir)), top)
    if inside is None:
        return f"{_show(repo.hooks_dir, repo)} is outside this repository, and other repositories may run its hooks"
    names = [f"{inside}/{p.name}" if inside else p.name for p in present]
    if not names:
        return None
    r = _git(["ls-files", "-z", "--", *names], top, repo.env)
    if r.returncode != 0:  # read as "tracked": never a guess that deletes a file of the team's
        return f"git cannot tell whether it tracks {', '.join(names)}: {r.stderr.strip() or f'exit code {r.returncode}'}"
    tracked = [n for n in r.stdout.split("\0") if n]
    if tracked:
        return f"git tracks {', '.join(tracked)}: the hooks of everyone who uses that folder"
    return None


def linked_left_alone(repo: Repo, why: str) -> str:
    """What uninstall says when it leaves pytemplate's hook in a linked hooks folder alone
    (linked_hands_off gave `why`)."""
    return (
        f"pytemplate's hook in {_show(repo.hooks_dir, repo)} ({elsewhere(repo)}): left alone, since {why}. "
        "If it should go, change that folder by hand (for a tracked file, in a commit)"
    )


def _hooks_path_hint(repo: Repo) -> str:
    target = _hooks_path_file(repo)
    runs = "husky runs it" if target.parent != repo.hooks_dir else "a sh script; git runs it from the top of the work tree"
    inside = any(_same_folder(folder, repo.top) for folder in (repo.hooks_dir, *repo.hooks_dir.parents))
    shared = "" if inside else "\n(that folder is outside this repository: other repositories run it too, and the line skips those without the launcher)"
    return (
        f"Add this line to {_show(target, repo)} ({runs}),\n"
        f"or run it from the tool that manages that folder:\n    {run_line(repo)}{shared}"
    )


def _ignored_message(repo: Repo) -> str:
    return (
        f"the git repository at {repo.top} ignores this project (git check-ignore pyt): its commits "
        "never contain it. git init the project to give it its own repository"
    )


def _write_hook(path: Path, text: str) -> None:
    path.write_bytes(text.encode("ascii"))  # bytes: LF on every OS
    if not IS_WINDOWS:
        path.chmod(0o755)


def install(repo: Repo, *, force: bool = False) -> str:
    """Install or update the hook; return the message to print (PytError if it cannot)."""
    if repo.custom_hooks_path:
        hook = _hooks_path_file(repo)
        state = hooks_path_state(repo)
        skipped = git_skips(hook, repo) if state in RUNS_CHECKS else None
        if skipped is not None:  # pytemplate writes nothing in that folder: the fix is the user's
            raise PytError(f"{_show(hook, repo)} runs ./pyt hooks run ({_elsewhere_short(repo)}), but git skips it: {skipped[0]}.\n  {skipped[1]}")
        if state == "stale":
            hint = "\n".join(f"  {line}" for line in stale_hint(repo).splitlines())
            return f"{_show(hook, repo)} already runs ./pyt hooks run ({_elsewhere_short(repo)}), but it is pytemplate's hook of an older version:\n{hint}"
        if state in ("calls", "installed"):
            return f"{_show(hook, repo)} already runs ./pyt hooks run ({_elsewhere_short(repo)})"
        where = "a folder pytemplate never writes into" if repo.hooks_link else "not in the default folder"
        raise PytError(
            f"{elsewhere(repo, value=True)}: git runs the hooks in {_show(repo.hooks_dir, repo)}, "
            f"{where}, so pytemplate does not install its hook there.\n"
            + "\n".join(f"  {line}" for line in _hooks_path_hint(repo).splitlines())
        )
    target = repo.default_dir / HOOK
    local = repo.default_dir / LOCAL
    script = hook_script(repo.launcher)
    state = classify(target, repo)
    if state == "missing" and not force and repo.ignored():
        raise PytError(f"{_ignored_message(repo)} (or: ./pyt hooks install --force)")
    other = launcher_of(_read(target)) if state == "other" else None
    skipped = git_skips(target, repo) if state in ("calls", "other") else None  # not pytemplate's file to change
    if other is not None and own_local(repo):
        skip_note = f"\n  but git skips {_show(target, repo)}: {skipped[0]} ({skipped[1]})" if skipped is not None else ""
        if not _own_local_outdated(repo):
            if skip_note:  # the other project's file: its fix is the user's
                raise PytError(f"{_show(target, repo)} ({other}) would run this project's checks from {_show(local, repo)},{skip_note}")
            return f"{_show(target, repo)} ({other}) already runs this project's checks from {_show(local, repo)}"
        if not proc.DRY_RUN:  # the other project's hook runs it: brought up to date in place
            _write_hook(local, script)
        verb = "would be updated" if proc.DRY_RUN else "updated"
        return f"pre-commit hook {verb}: {_show(local, repo)} -> sh {pasteable(repo.launcher)} hooks run (run first by {_show(target, repo)}, the hook of {other}){skip_note}"
    # this project's own copy as pre-commit.local (its chain's first hook went away): with this
    # project's hook back in pre-commit, it would run the checks twice
    drop = state in ("missing", "outdated", "installed") and own_local(repo)
    moved = False
    if state in ("foreign", "calls", "other"):
        if state == "calls" and not force:
            if skipped is not None:
                raise PytError(f"{_show(target, repo)} runs ./pyt hooks run (not pytemplate's file: left alone), but git skips it: {skipped[0]}.\n  {skipped[1]}")
            return f"{_show(target, repo)} already runs ./pyt hooks run (not pytemplate's file: left alone)"
        if not force:
            if other is not None:
                raise PytError(
                    f"{_show(target, repo)} runs the checks of another project of this repository ({other}): left alone.\n"
                    f"  {chain_hint(repo)}"
                )
            raise PytError(f"{_show(target, repo)} already exists and is not pytemplate's hook: left alone.\n  {chain_hint(repo)}")
        if os.path.lexists(local):  # lexists: a dangling link there is somebody's too
            raise PytError(f"both {_show(target, repo)} and {_show(local, repo)} exist: merge them by hand, then ./pyt hooks install")
        text = _read(target)
        if state == "foreign" and reads_its_name(text):
            raise PytError(
                f"{_show(target, repo)} is a {interpreter(text)} script that reads its own name ($0): kept as {LOCAL} "
                f"it would not run its checks, so it was left alone. Add this line to it instead:\n  {run_line(repo)}"
            )
        moved = True
    elif state == "installed" and not drop:
        skips = git_skips(target, repo)
        if skips is None:
            return f"pre-commit hook already installed: {_show(target, repo)}"
        if proc.DRY_RUN:
            return f"pre-commit hook would be made executable again: {_show(target, repo)} (git skips it: {skips[0]})"
        target.chmod(0o755)  # the mode _write_hook gives it (POSIX only: git_skips)
        return f"pre-commit hook made executable again: {_show(target, repo)} (git skipped it: {skips[0]})"
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
    msg = f"pre-commit hook {'would be ' + verb if dry and state != 'installed' else verb}: {_show(target, repo)} -> sh {pasteable(repo.launcher)} hooks run"
    if moved:
        idle = _not_run(target if dry else local, _show(local, repo))  # a dry run moved nothing
        msg += f"\n  the previous hook {'would be' if dry else 'was'} kept as {_show(local, repo)}" + (f"; {idle}" if idle else " and runs first")
    elif drop:
        msg += f"\n  {'would remove' if dry else 'removed'} {_show(local, repo)}: a copy of this project's hook (the checks would run twice)"
    elif local.is_file():
        msg += f"\n  {_not_run(local, _show(local, repo)) or f'it runs {_show(local, repo)} first'}"
    return msg


def uninstall(repo: Repo) -> str:
    """Remove this project's hook (never another one) and restore the hook it had moved aside.
    This project's hook chained as pre-commit.local after another project's is removed too
    (never restored: it would run twice, or run with hooks.pre_commit = false). In a linked hooks
    folder only where linked_hands_off allows it: never a hook git tracks there."""
    target = repo.default_dir / HOOK
    local = repo.default_dir / LOCAL
    state = classify(target, repo)
    dry = proc.DRY_RUN
    own_copy = own_local(repo)
    if state in ("installed", "outdated") or own_copy:
        why = linked_hands_off(repo)
        if why is not None:
            return linked_left_alone(repo, why)
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
        state = hooks_path_state(repo)
        where = _show(hook, repo)
        skipped = git_skips(hook, repo) if state in RUNS_CHECKS else None
        if skipped is not None:
            return None, f"git pre-commit hook: {where} runs ./pyt hooks run ({_elsewhere_short(repo)}), but git skips it: {skipped[0]}", skipped[1]
        if state == "stale":
            return None, f"git pre-commit hook outdated: {where} runs ./pyt hooks run ({_elsewhere_short(repo)}), as pytemplate's hook of an older version", stale_hint(repo)
        if state in ("installed", "calls"):
            return True, f"git pre-commit hook: {where} runs ./pyt hooks run ({_elsewhere_short(repo)})", ""
        return None, f"git pre-commit hook: {elsewhere(repo, value=True)}, pytemplate's checks are not in {where}", _hooks_path_hint(repo)
    target = repo.default_dir / HOOK
    local = repo.default_dir / LOCAL
    state = classify(target, repo)
    chained = local.is_file()
    skipped = git_skips(target, repo) if state in ("installed", "calls", "other") else None
    if state == "installed" and own_local(repo):
        return None, f"git pre-commit hook installed, but {LOCAL} is a copy of it: the checks run twice", "./pyt hooks install"
    if state == "installed" and skipped is not None:
        return None, f"git pre-commit hook installed, but git skips it: {skipped[0]}", f"./pyt hooks install  (or {skipped[1]})"
    if state == "installed":
        idle = _not_run(local, _show(local, repo)) if chained else None
        extra = f" ({idle})" if idle else f" (runs {LOCAL} first)" if chained else ""
        return True, f"git pre-commit hook installed: {_show(target, repo)} -> sh {pasteable(repo.launcher)} hooks run{extra}", ""
    if state == "outdated":
        return None, "git pre-commit hook outdated (another launcher path or template version)", "./pyt hooks install"
    if state == "calls" and skipped is not None:
        return None, f"git pre-commit hook: {_show(target, repo)} runs ./pyt hooks run, but git skips it: {skipped[0]}", skipped[1]
    if state == "calls":
        return True, f"git pre-commit hook: {_show(target, repo)} runs ./pyt hooks run", ""
    other = launcher_of(_read(target)) if state == "other" else None
    if other is not None and own_local(repo):
        if _own_local_outdated(repo):
            return None, f"git pre-commit hook outdated: {LOCAL}, this project's hook that {_show(target, repo)} ({other}) runs first", "./pyt hooks install"
        if skipped is not None:
            return None, f"git pre-commit hook: {_show(target, repo)} ({other}) would run this project's checks ({LOCAL}), but git skips it: {skipped[0]}", skipped[1]
        return True, f"git pre-commit hook: {_show(target, repo)} runs this project's checks ({LOCAL}), then those of {other}", ""
    if repo.ignored():  # missing, foreign or another project's: none of ours, and this is why
        return None, f"git pre-commit hook not installed: the repository at {repo.top} ignores this project", (
            "git init the project to give it its own repository (or ./pyt hooks install --force)"
        )
    if other is not None:
        return None, f"git pre-commit hook: {_show(target, repo)} runs the checks of another project ({other}), not this one's", chain_hint(repo)
    if state == "foreign":
        return None, f"git pre-commit hook: {_show(target, repo)} is another tool's hook", chain_hint(repo)
    off = "" if cfg.hooks.pre_commit else "   (hooks.pre_commit = false: ./pyt setup does not install it)"
    return None, "git pre-commit hook not installed", "./pyt hooks install" + off


def chain_hint(repo: Repo) -> str:
    """How to add this project's checks to a pre-commit hook that is not its own."""
    local = repo.default_dir / LOCAL
    if os.path.lexists(local):  # install --force would refuse: it chains one hook only
        return f"{_show(local, repo)} exists as well, so ./pyt hooks install --force cannot chain it: merge the two by hand, then ./pyt hooks install --force"
    return f"./pyt hooks install --force keeps it as {LOCAL} (it runs first) and adds this project's checks"


def chain_advice(repo: Repo) -> str:
    """chain_hint in a few words, for the one-line messages of apply and setup."""
    if os.path.lexists(repo.default_dir / LOCAL):
        return f"{LOCAL} is taken too: ./pyt hooks status says what to do"
    return "./pyt hooks install --force runs both"


def show_status(cfg: Config, project: Path = ROOT) -> int:
    try:
        repo = find_repo(project)
    except NotInGit as e:  # the answer, as the status line is in a repository: -q never hides it
        ui.report(f"hooks: {e} (no git hook)")
        return 0
    ui.step(f"git hooks: {_show(repo.hooks_dir, repo)}")
    passed, label, hint = _status_line(cfg, repo)
    ui.check_line(passed, label, hint)
    if repo.custom_hooks_path and not repo.hooks_link and classify(repo.default_dir / HOOK, repo) in ("installed", "outdated"):
        ui.check_line(None, f"{_show(repo.default_dir / HOOK, repo)} is pytemplate's but inactive (core.hooksPath)", "./pyt hooks uninstall removes it")
    return 0


def ensure_installed(cfg: Config, project: Path = ROOT) -> None:
    """Called by ./pyt setup: install the hook if hooks.pre_commit and it is missing (or
    update this project's own). Prints at most one line and never fails setup."""
    if not cfg.hooks.pre_commit:
        return
    try:
        repo = find_repo(project)
    except NotInGit:
        return  # no git, or not a git work tree: nothing to do
    except PytError as e:  # git refuses the repository (dubious ownership...): say why
        ui.warn(f"git pre-commit hook not installed: {e}")
        return
    try:
        if repo.custom_hooks_path:
            hook = _hooks_path_file(repo)
            state = hooks_path_state(repo)
            skipped = git_skips(hook, repo) if state in RUNS_CHECKS else None
            if skipped is not None:  # a folder pytemplate never writes into: the fix is the user's
                ui.warn(f"git pre-commit hook: {_show(hook, repo)} runs ./pyt hooks run, but git skips it: {skipped[0]} ({skipped[1]})")
            elif state == "stale":
                ui.info(f"git pre-commit hook: {_show(hook, repo)} runs ./pyt hooks run, as pytemplate's hook of an older version (./pyt hooks status says how to update it)")
            elif state not in RUNS_CHECKS:
                ui.info(f"git pre-commit hook: {elsewhere(repo)}, not installed (./pyt hooks status says what to add)")
            return
        target = repo.default_dir / HOOK
        state = classify(target, repo)
        own_copy = own_local(repo)
        skipped = git_skips(target, repo) if state in ("installed", "calls", "other") else None
        if state in ("missing", "foreign", "other") and not own_copy and repo.ignored():  # nothing of ours there: why
            ui.info(f"git pre-commit hook: not installed: {_ignored_message(repo)} (or: ./pyt hooks install --force)")
        elif (
            state in ("missing", "outdated")
            or (state == "installed" and (own_copy or skipped is not None))  # a copy to drop, or the x bit to give back
            or (state == "other" and own_copy and _own_local_outdated(repo))
        ):
            lines = install(repo).splitlines()
            ui.ok("\n".join(lines if own_copy else lines[:1]))  # the removed copy is news
        elif skipped is not None and (state == "calls" or own_copy):  # not pytemplate's file to change
            ui.warn(f"git pre-commit hook: {_show(target, repo)} would run this project's checks, but git skips it: {skipped[0]} ({skipped[1]})")
        elif state == "foreign":
            ui.info(f"git pre-commit hook: another tool's hook is installed, left alone ({chain_advice(repo)})")
        elif state == "other" and not own_copy:
            ui.info(
                f"git pre-commit hook: it runs the checks of another project of this repository "
                f"({launcher_of(_read(target))}), left alone ({chain_advice(repo)})"
            )
    except (PytError, OSError) as e:
        ui.warn(f"git pre-commit hook not installed: {e}")


def doctor(cfg: Config, check: Check, project: Path = ROOT) -> None:
    """One line for ./pyt doctor: whether the hook is installed (nothing outside git)."""
    try:
        repo = find_repo(project)
    except NotInGit:
        if git_missing_here(project):
            ui.step("git hook")
            check(None, f"{NO_GIT}: the pre-commit hook of this repository is not checked", "put git on PATH (a git GUI's own git is often not on it)")
        return
    except PytError as e:  # git refuses the repository (dubious ownership...): show why
        ui.step("git hook")
        first, _, rest = str(e).partition("\n")
        check(None, first, rest)
        return
    ui.step("git hook")
    check(*_status_line(cfg, repo))
    ci = ".github/workflows/ci.yml"
    # new warns only when it creates a project there. An ignored project is in no commit of that
    # repository: the line above says to give it one of its own (git init), not to add a workflow
    if repo.prefix and not repo.ignored() and (project / ci).is_file():
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


def _git_output(repo: Repo, *args: str) -> str:
    """git's output for a call a check relies on, its paths as the file system names them
    (os.fsdecode: a name that is not UTF-8 keeps its bytes, which the checks then open and hand
    to git and ruff; read as UTF-8 text it became U+FFFD, and a staged file whose name is
    Latin-1 was "missing from the working tree"). A call that failed read as an empty answer (no
    file with unstaged changes, no untracked file, no index mode) and the checks it fed passed
    a commit they had to block: it stops the hook with git's own message (the hook script then
    blocks the commit and says how to commit without the checks)."""
    argv = ["git", *GIT_CONFIG, "--literal-pathspecs", *args]
    r = _run_bytes(argv, cwd=repo.project, env=_git_process_env(repo.env))
    if r.returncode != 0:
        said = r.stderr.decode("utf-8", errors="replace").strip() or f"exit code {r.returncode}"
        raise PytError(f"git {' '.join(args[:2])} failed: {said}")
    return os.fsdecode(r.stdout)


def staged_files(repo: Repo, diff_filter: str = STAGED) -> list[str]:
    """Return the staged files, relative to the project: the ones whose new content is in the
    commit by default (what the per-file checks read); diff_filter="D": the staged deletions.
    --no-renames: a rename is its deletion plus its addition, so both paths are seen."""
    out = _git_output(repo, "diff", "--cached", "--name-only", "--no-renames", f"--diff-filter={diff_filter}", "-z")
    return project_paths(repo.prefix, out.split("\0"), ignore_case=repo.ignore_case)


# The index flags that hide a file's changes from `git diff` and `git add`, by the tag `git
# ls-files -v` gives the entry: S skip-worktree (a sparse checkout, or the trick that keeps local
# edits of a tracked file out of `git status`), a lower-case tag assume-unchanged, s both
HIDING_FLAGS = ("skip-worktree", "assume-unchanged")


def _hiding_flags(tag: str) -> tuple[str, ...]:
    return tuple(flag for flag, on in zip(HIDING_FLAGS, (tag in ("S", "s"), tag.islower()), strict=True) if on)


@dataclass(frozen=True)
class Staging:
    """What a commit made now would miss of some paths (relative to the project)."""

    dirty: frozenset[str]  # unstaged changes, untracked (ignored or not), changes an index flag hides
    flags: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # tracked path -> its HIDING_FLAGS


def staging(repo: Repo, paths: Sequence[str]) -> Staging:
    """Which of `paths` (relative to the project) a commit made now would miss, and the index flags
    that hide the changes of any of them. The paths are files the commit needs (the generated
    ones, pytemplate.toml, pyproject.toml, uv.lock: CI renders and syncs from them), so neither
    an ignore rule nor an index flag makes one committed: `--exclude-standard` left out an
    untracked one that a .gitignore or the user's core.excludesFile (a global `.vscode/` or
    `.python-version` rule) ignores, `git diff` never shows the changes of a skip-worktree or
    assume-unchanged entry, and the hook passed a commit CI then failed on every push."""
    if not paths:
        return Staging(frozenset())
    names = _git_output(repo, "diff", "--name-only", "-z", "--", *paths).split("\0")
    index = _git_output(repo, "ls-files", "--others", "--stage", "-v", "-z", "--full-name", "--", *paths)
    flags: dict[str, tuple[str, ...]] = {}
    blobs: dict[str, str] = {}
    for record in index.split("\0"):
        if record.startswith("? "):  # untracked, whatever an ignore rule says
            names.append(record[2:])
            continue
        meta, _, name = record.partition("\t")
        fields = meta.split()  # tag, mode, blob, stage
        hidden = _hiding_flags(fields[0]) if len(fields) == 4 else ()
        for rel in project_paths(repo.prefix, [name], ignore_case=repo.ignore_case) if hidden else ():
            flags[rel], blobs[rel] = hidden, fields[2]
    # git diff never compares a flagged entry with its file: the file's blob tells (the path's
    # attributes applied, as `git add` would store it); a skip-worktree file missing from the
    # disk (a sparse checkout) changes nothing in the commit
    present = [p for p in blobs if (repo.project / p).is_file()]
    if present:
        hashes = _git_output(repo, "hash-object", "--", *present).split()
        head = f"{repo.prefix}/" if repo.prefix else ""
        names += [head + p for p, blob in zip(present, hashes, strict=False) if blob != blobs[p]]
    return Staging(frozenset(project_paths(repo.prefix, names, ignore_case=repo.ignore_case)), flags)


def unstaged_files(repo: Repo, paths: Sequence[str]) -> list[str]:
    """Return which of `paths` (relative to the project) a commit made now would miss (staging)."""
    return sorted(staging(repo, paths).dirty)


def add_command(repo: Repo, flags: Mapping[str, tuple[str, ...]], paths: Sequence[str]) -> list[str]:
    """The lines of a hint that stage `paths` (relative to the project; `flags`: Staging.flags):
    `git add`, `git add -f` for an untracked path git ignores (git add refuses it otherwise), and
    first a `git update-index` that clears each flag hiding a path's changes (git add refuses a
    skip-worktree path, and stages nothing of an assume-unchanged one), then why. A plain `git add
    a b` when nothing is ignored or flagged (asked only for a hint: the check already failed)."""
    r = _git(["check-ignore", "--", *paths], repo.project, repo.env, literal=False) if paths else None
    ignored = set(r.stdout.splitlines()) if r is not None and r.returncode == 0 else set()  # 1: none
    lines: list[str] = []
    for flag in HIDING_FLAGS:
        marked = [p for p in paths if flag in flags.get(p, ())]
        if marked:
            lines.append(f"git update-index --no-{flag} {' '.join(marked)}")
    plain = [p for p in paths if p not in ignored]
    forced = [p for p in paths if p in ignored]
    if plain:
        lines.append(f"git add {' '.join(plain)}")
    if forced:
        lines.append(f"git add -f {' '.join(forced)}")
        lines.append("(git ignores them, by a .gitignore or core.excludesFile, but the commit needs them: CI and every clone render and sync from them)")
    if any(p in flags for p in paths):
        lines.append("(an index flag hides their changes from git status and git add: update-index clears it)")
    return lines


def _plain_add(paths: Sequence[str]) -> list[str]:
    return [f"git add {' '.join(paths)}"]


def worktree_changes(repo: Repo) -> set[str]:
    """Return the project files whose working tree differs from the index (unstaged edits and
    files deleted from the working tree), relative to the project. Never a submodule: its own
    checkout is no content of this commit, and git cat-file cannot read a gitlink (staged_blob
    stopped the hook on a staged submodule that had moved on)."""
    out = _git_output(repo, "diff", "--name-only", "--no-renames", "--ignore-submodules=all", "-z")
    return set(project_paths(repo.prefix, out.split("\0"), ignore_case=repo.ignore_case))


def staged_blob(repo: Repo, path: str) -> bytes:
    """Return the staged content of `path` (relative to the project) as a checkout would write
    it (`--filters`: CRLF for `eol=crlf` files, whatever the index stores). A path the index does
    not hold, or git failing, is a PytError with git's message: None read as "nothing to check",
    and the launcher and language checks passed a staged file they never read.
    `:0:` (stage 0) keeps a path like `1:x.py` from reading as a stage number."""
    spec = ":0:" + (f"{repo.prefix}/{path}" if repo.prefix else path)
    r = _run_bytes(["git", *GIT_CONFIG, "cat-file", "--filters", spec], cwd=repo.project, env=_git_process_env(repo.env))
    if r.returncode != 0:
        said = r.stderr.decode("utf-8", errors="replace").strip() or f"exit code {r.returncode}"
        raise PytError(f"git cat-file {spec} failed: {said}")
    return r.stdout


def python_files(staged: Sequence[str], dirs: Sequence[str]) -> list[str]:
    """Return the staged Python files and notebooks (PY_SUFFIXES) under the code dirs (src/,
    tests/): ruff reads a notebook by path and on stdin alike (--stdin-filename x.ipynb)."""
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
        except PytError as e:  # uv missing or too old
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
    except PytError as e:
        return e.code, str(e)
    out = [t.decode("utf-8", errors="replace").strip() for t in (r.stdout, r.stderr)]
    return r.returncode, "\n".join(t for t in out if t)


def uv_lock_check(cfg: Config) -> tuple[int, str]:
    try:
        r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], capture=True, check=False, echo=False)
    except PytError as e:  # uv missing or too old
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


# What `ruff format --check` prints for a file it cannot parse, by path (concise) or on stdin; its
# exit code is then 2, like a ruff that did not run, which the hook called "could not run ruff"
_UNPARSABLE = re.compile(r": invalid-syntax: |^error: Failed to parse ", re.MULTILINE)


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
    common: list[str | Path] = ["--config", config_arg(config_file), "--force-exclude", "--output-format", "concise"]
    code, out = _run_ruff(cfg, ["check", *common, *(["--exit-zero"] if exit_zero else [])], whole, staged_)
    if code > 1:
        yield Result(False, "ruff check: could not run ruff (the output says why)", output=out)
    elif code == 0 and exit_zero and "Found " in out:
        yield Result(True, f"ruff check: {n}, warnings only (profile '{profile}'{note})", output=out)
    elif code == 0:
        yield Result(True, f"ruff check: {n} (profile '{profile}'{note})")
    else:
        yield Result(False, f"ruff check: {n} (profile '{profile}'{note})", "./pyt lint --fix fixes some; then git add" + partial_hint, output=out)
    code, out = _run_ruff(cfg, ["format", "--check", *common], whole, staged_)
    if code > 1 and _UNPARSABLE.search(out):  # ruff format exits 2 for a file it cannot parse
        also = ", then ./pyt fmt" if "would be reformatted" in out or "Would reformat" in out else ""
        yield Result(False, "ruff format: a staged file does not parse", f"fix the syntax error ruff check shows{also}; then git add" + partial_hint, output=out)
    elif code > 1:
        yield Result(False, "ruff format: could not run ruff (the output says why)", output=out)
    elif code == 0:
        yield Result(True, f"ruff format: {n} formatted" + (f" ({note[2:]})" if note else ""))
    else:
        yield Result(False, "ruff format: files need formatting", "./pyt fmt, then git add" + partial_hint, output=out)


def check_generated(cfg: Config, generated: Sequence[str], dirty: set[str], add: Callable[[Sequence[str]], list[str]] = _plain_add) -> Iterator[Result]:
    """The generated files match their sources, and none has unstaged changes. Conservative:
    this blocks even a commit that touches none of them, because the generator also reads the
    runner and the templates. The hints name the config files with unstaged changes too, so
    following them never commits generated files without their source. `add`: the command lines
    that stage some paths (add_command)."""
    changed, edited = render.apply(cfg, check=True)
    sources = [p for p in CONFIG_FILES if p in dirty]
    if changed or edited:
        hints = []
        if changed:
            # render records their hashes in state.json too: left out of the line, the next commit
            # stopped at "generated files staged: unstaged: .pytemplate/state.json"
            state = STATE_FILE.relative_to(ROOT).as_posix()
            first, *rest = add([*sources, *changed, state])
            hints.append("\n".join([f"outdated: {', '.join(changed)}", f"./pyt render, then {first}", *rest]))
        if edited:
            hints.append(f"hand-edited: {', '.join(edited)}\nchange pytemplate.toml or .pytemplate/templates (./pyt render --diff), or ./pyt render --force")
        yield Result(False, "generated files up to date", "\n".join(hints))
    else:
        yield Result(True, "generated files up to date")
    missed = [p for p in generated if p in dirty]
    if missed:
        also = f" (their source {', '.join(sources)} too)" if sources else ""
        yield Result(False, "generated files staged", "\n".join([f"unstaged: {', '.join(missed)}{also}", *add([*sources, *missed])]))
    else:
        yield Result(True, "generated files staged")


def check_lock(cfg: Config) -> Result:
    """pyproject.toml matches pytemplate.toml and uv.lock matches pyproject.toml (working tree).

    "Matches" includes what only ./pyt apply brings in line (a hand-edited app.name, the
    [preset.*] options, a hand-edited app.preset): cmd_apply.pending, without its git check."""
    from . import cmd_apply  # lazy: cmd_apply imports this module

    hints: list[str] = []
    if render.pyproject_outdated(cfg):
        hints.append("pyproject.toml does not match pytemplate.toml: ./pyt apply")
    hints += [f"{problem}: {hint}" for problem, hint in cmd_apply.pending(cfg, hook=False)]
    code, out = uv_lock_check(cfg)
    if code != 0:
        hints.append(f"uv lock --check: {envs.uv_error(out) or f'exit code {code}'}\n./pyt lock")
    if hints:
        return Result(False, "pyproject.toml and uv.lock", "\n".join(hints))
    return Result(True, "pyproject.toml and uv.lock up to date")


def check_together(
    group: Sequence[str], touched: set[str], deleted: set[str], dirty: set[str], add: Callable[[Sequence[str]], list[str]] = _plain_add
) -> Result:
    """Once any file of `group` (the config files and the generated ones) is in the commit, the
    config files are committed together: none may keep unstaged changes, or HEAD pairs a new
    file with an old one (uv.lock without its pyproject.toml fails `uv run --locked`; generated
    files without pytemplate.toml fail `render --check`). Unstaged generated files are
    check_generated's finding. `add`: the command lines that stage some paths (add_command)."""
    in_commit = [p for p in group if p in touched]
    if not in_commit:
        return Result(None, "config files: not in this commit")
    missed = [p for p in CONFIG_FILES if p in dirty]
    if not missed:
        return Result(True, "config files staged together")
    hints = [f"in the commit: {', '.join(in_commit)}"]
    kept = [p for p in missed if p in deleted]  # `git rm --cached`: gone from the commit, still on disk
    if kept:
        hints += [f"deleted in the commit but still in the working tree: {', '.join(kept)} (stage them again, or delete them)", *add(kept)]
    rest = [p for p in missed if p not in deleted]
    if rest:
        hints += [f"unstaged changes: {', '.join(rest)} (they are committed together)", *add(rest)]
    return Result(False, f"config files staged together: {', '.join(missed)} not staged", "\n".join(hints))


def check_mypyc(cfg: Config, project: Path, staged: set[str]) -> Result:
    if not cfg.supports("mypyc"):
        return Result(None, "mypyc rules: mypyc is not in backend.supported")
    try:
        sources = mypyc.compiled_sources(cfg)
    except PytError as e:
        return Result(False, "mypyc rules", str(e))
    files = [p for p in sources if _within(p, project) in staged]
    if not files:
        return Result(None, "mypyc rules: no staged compiled module")
    found = lintc.lint(cfg, files)
    strict = cfg.profile_for() == "mypyc"
    if not found:
        return Result(True, f"mypyc rules: {lintc.describe(files)}")
    errors = [str(f) for f in found if strict and not f.note]  # a note never blocks (lintc.Finding)
    warnings = [str(f) for f in found if not strict or f.note]
    if errors:
        return Result(False, f"mypyc rules: {len(errors)} problem(s)", "fix them, or move the code to a boundary module", errors=errors, warnings=warnings)
    why = "notes" if strict else f"non-blocking with profile '{cfg.profile_for()}'"
    return Result(True, f"mypyc rules: {len(warnings)} warning(s) ({why})", warnings=warnings)


def _index_modes(repo: Repo, names: Sequence[str]) -> dict[str, str]:
    modes: dict[str, str] = {}
    for line in _git_output(repo, "ls-files", "-s", "--", *names).splitlines():
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
        for problem, fix in launcher_problems(name, data, modes.get(name) if name == "pyt" else None):
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
        raise PytError(f"cannot load {path}")
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
    as_staged = {p: staged_blob(repo, p) for p in present if p in partial}
    yield from check_ruff(cfg, present, as_staged)
    if missing:
        # git commits the staged version of a file deleted from the disk: almost always an accident.
        # Dropping it from the commit: `git rm --cached` unstages a file the commit adds, but for a
        # tracked one it stages its deletion; `git restore --staged` puts HEAD's version back.
        added = set(staged_files(repo, "A"))
        new = [p for p in missing if p in added]
        tracked = [p for p in missing if p not in added]
        drop = [f"git rm --cached {' '.join(new)}"] if new else []
        drop += [f"git restore --staged {' '.join(tracked)}"] if tracked else []
        yield Result(
            False,
            f"staged files missing from the working tree: {', '.join(missing)}",
            f"keep them: git restore {' '.join(missing)}\ndrop them from the commit: {'; '.join(drop)}",
        )
    generated = sorted({*render.outputs(cfg), STATE_FILE.relative_to(ROOT).as_posix()})
    group = [*CONFIG_FILES, *generated]
    state = staging(repo, group)
    dirty = set(state.dirty)
    add = functools.partial(add_command, repo, state.flags)
    yield from check_generated(cfg, generated, dirty, add)
    yield check_lock(cfg)
    yield check_together(group, staged_set | set(deleted), set(deleted), dirty, add)
    yield check_mypyc(cfg, repo.project, staged_set)
    yield check_launchers(repo, staged_set, content)
    if template_repo:
        yield check_language(repo.project, staged, TEMPLATE / "tests" / "test_no_spanish.py", content)


def _print(result: Result) -> None:
    """The check line, then the tool output, then how to fix it. A check that did not pass keeps
    its output and hint under -q, which hides progress, never the answer: `./pyt -q hooks run`
    (the global options may come first) said which check failed, not which file nor the fix."""
    ui.check_line(result.passed, result.label)
    out = ui.info if result.passed is True else ui.report
    for line in result.output.splitlines():
        out(f"         {line}".rstrip())
    for w in result.warnings:
        ui.warn(w)
    for e in result.errors:
        ui.error(e)
    if result.passed is not True:
        for line in result.hint.splitlines():
            ui.report(f"         {line}")


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
    # The code folders by name, never by what is on disk: a staged file of a src/ or tests/ gone
    # from the working tree is a missing file like any other (filtered by existence, it was dropped
    # without a word, neither checked nor named, and the commit took it in)
    dirs = ["src", "tests"]
    failed = 0
    template_repo = (TEMPLATE / "template-repo").is_file()
    for result in checks(cfg, repo, staged, code_dirs=dirs, template_repo=template_repo, deleted=deleted):
        _print(result)
        failed += result.passed is False
    seconds = time.perf_counter() - start
    if failed:
        # git runs the hook from the top of the work tree, where a project in a subfolder has no
        # ./pyt: the hints, and the paths the tools print, are the project's (typed at the top,
        # `./pyt render` and `git add .vscode/tasks.json` failed)
        folder = pasteable(repo.prefix)  # `cd apps/R&D` ran `cd apps/R` in the background, then `D`
        where = f"\n  (the commands and paths above are the project's: run them in its folder, cd {folder} from the top of the repository)" if repo.prefix else ""
        ui.error(
            f"pre-commit: {failed} check{'s' if failed != 1 else ''} failed ({seconds:.1f} s). Fix, `git add` and commit again\n"
            f"  (skip the hook once: git commit --no-verify){where}"
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
        raise PytError(f"hooks: unknown subcommand {sub!r}  ({USAGE})")
    flags = only_flags(f"hooks {sub}", rest, allowed[sub])
    if sub == "status":
        return show_status(cfg, ROOT)  # explicit: a default argument is bound at import time
    repo = find_repo(ROOT)
    if sub == "run":
        return run(cfg, repo)
    try:
        message = install(repo, force="--force" in flags) if sub == "install" else uninstall(repo)
    except OSError as e:  # a hooks folder the user may not write (another user's, read-only, immutable)
        name = e.filename or (repo.default_dir / HOOK)
        raise PytError(f"hooks {sub}: cannot change {name}: {e.strerror or e}") from None
    # What was done, or left alone and why (a kept hook that never runs): the answer to the
    # command, never hidden by -q (5.3), which hid "not pytemplate's hook: left alone"
    if ui.QUIET:
        ui.report(message)
    elif sub == "install":
        ui.ok(message)
    else:
        ui.info(message)
    return 0
