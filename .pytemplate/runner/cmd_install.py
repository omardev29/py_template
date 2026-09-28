"""`./pyt install` and `./pyt uninstall`: the `pyt` command in every folder.

`install` runs in a clone of the template repository (the `.pytemplate/template-repo` marker)
and never in the copy it makes. It copies the files git tracks there, with their working-tree
content as `./pyt new` reads them, into the user data folder (`snapshot_dir`: the installed
template), without the template's own CI (`.github/workflows/template-*`) but with its marker,
README.md and LICENSE, so that `pyt new` from that copy makes the project `./pyt new` makes
here. The copy is marked by its record (`project.INSTALL_RECORD`: the commit, whether the tree
had uncommitted changes, the source folder, the bin folder). Then it writes the launchers into
uv's tool bin folder (`uv tool dir --bin`, where `uv tool install` puts its commands): `pyt`,
and on Windows also `pyt.cmd` and `pyt.ps1` (cmd runs `pyt.cmd`, PowerShell `pyt.ps1`, Git
Bash and MSYS2 `pyt`: CLAUDE.md 4.2). Each carries the MARKER line; a file there without it is
never overwritten. Where a launcher finds no project, it runs the installed template's runner
in its global mode (PYTEMPLATE_GLOBAL=1, CLAUDE.md 4.1).

`uninstall` removes the launchers that carry the MARKER and the installed template that holds
its record, and says what it left and why.

Neither ever edits PATH, a shell's startup files or the registry: when the bin folder is not
on PATH they say so and name `uv tool update-shell`. Every refusal comes before the first
write, all of them in one message; the new copy is written into a folder of its own and
swapped in whole, and a failure at any step (Ctrl+C, SIGTERM and SIGHUP too) puts the old copy
and the old launchers back.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import envs, presets, proc, project, shells, ui
from .cmd_dev import only_flags
from .config import Config
from .project import IS_WINDOWS, ROOT, TEMPLATE
from .ui import PytError

Check = Callable[[bool | None, str, str], None]

# The line every launcher carries in its header: `pyt install` overwrites, and `pyt uninstall`
# removes, only a file of the bin folder that has it
MARKER = re.compile(rb"^(?:#|rem) pytemplate-launcher:", re.MULTILINE)
NAMES = ("pyt", "pyt.cmd", "pyt.ps1")
# The launchers install writes. On Windows all three: PowerShell's command discovery takes
# pyt.ps1, and cmd pyt.cmd, before the extensionless sh launcher of the same folder, which Git
# Bash and MSYS2 run (npm's cmd-shim lays out its commands the same way).
LAUNCHERS = NAMES if IS_WINDOWS else ("pyt",)
RECORD = project.INSTALL_RECORD
SCHEMA = 1
# What an unfinished install leaves: folders next to the installed template, and staged
# launchers in the bin folder (_stage)
NEW, OLD = ".template-new-", ".template-old-"
STAGED = re.compile(r"\.pyt(?:\.cmd|\.ps1)?-install-")
UPDATE_SHELL = "uv tool update-shell"
FROM_A_CLONE = f"./pyt install in a clone of the template, {presets.TEMPLATE_URL}"


# --- where things go ------------------------------------------------------------------------------


def data_home(environ: Mapping[str, str] | None = None, windows: bool = IS_WINDOWS) -> Path | None:
    """The user data folder, where the launchers look for the installed template: %LOCALAPPDATA%
    on Windows (else %USERPROFILE%\\AppData\\Local), elsewhere an absolute $XDG_DATA_HOME (the
    XDG spec ignores a relative one), else ~/.local/share of an absolute $HOME (a relative one
    would name a folder below whatever folder a command runs in). None when nothing names one."""
    env = os.environ if environ is None else environ
    if windows:
        upper = {k.upper(): v for k, v in env.items()}
        if upper.get("LOCALAPPDATA"):
            return Path(upper["LOCALAPPDATA"])
        return Path(upper["USERPROFILE"]) / "AppData" / "Local" if upper.get("USERPROFILE") else None
    xdg = env.get("XDG_DATA_HOME", "")
    if xdg.startswith("/"):
        return Path(xdg)
    home = env.get("HOME", "")
    return Path(home) / ".local" / "share" if home.startswith("/") else None


def snapshot_dir(environ: Mapping[str, str] | None = None, windows: bool = IS_WINDOWS) -> Path | None:
    """The installed template, <data home>/pytemplate/template: where the launchers look for it
    (`_pt_installed` in pyt, `$data` in pyt.ps1, `:installed` in pyt.cmd)."""
    home = data_home(environ, windows)
    return home / "pytemplate" / "template" if home is not None else None


def data_home_names() -> str:
    """The variables that name the user data folder (data_home), for a message when none does."""
    return "LOCALAPPDATA (or USERPROFILE)" if IS_WINDOWS else "HOME (or an absolute XDG_DATA_HOME)"


def bin_dir() -> Path:
    """uv's tool bin folder (`uv tool dir --bin`: UV_TOOL_BIN_DIR, XDG_BIN_HOME,
    XDG_DATA_HOME/../bin or ~/.local/bin), where `uv tool install` puts its commands."""
    r = proc.run([proc.find_uv(), "tool", "dir", "--bin"], capture=True, check=False, echo=False)
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if r.returncode != 0 or not lines:
        reason = envs.uv_error((r.stderr or "") + (r.stdout or "")) or f"exit code {r.returncode}"
        raise PytError(f"uv cannot name its tool bin folder (uv tool dir --bin): {reason}", 3)
    folder = Path(lines[-1])
    if not folder.is_absolute():
        raise PytError(f"uv's tool bin folder is the relative path {folder} (UV_TOOL_BIN_DIR?): set it to an absolute folder")
    return folder


def installed_here() -> bool:
    """Whether this runner is the installed template's own: global mode (project.GLOBAL: a
    launcher found no project, or the root holds the install record), or the root is the folder
    the launchers look in."""
    if project.GLOBAL:
        return True
    snapshot = snapshot_dir()
    try:
        return snapshot is not None and os.path.samefile(snapshot, ROOT)
    except OSError:
        return False


def is_launcher(path: Path) -> bool:
    """A launcher pyt install wrote: a regular file (never a link) with the MARKER line."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        with open(path, "rb") as f:
            return MARKER.search(f.read(4096)) is not None
    except OSError:
        return False


def not_ours(path: Path) -> str:
    """Why pyt install may not overwrite (nor uninstall remove) this file of the bin folder, or
    "" when it is a launcher pyt install wrote."""
    if os.path.islink(path):
        return "a symbolic link, which pyt install never writes"
    if (path.parent / ".pytemplate").is_dir():
        return "a project's own launcher (its folder holds .pytemplate)"
    if not is_launcher(path):
        return "not a pytemplate launcher (no `pytemplate-launcher` line)"
    return ""


def _is_link(path: Path) -> bool:
    """A symbolic link, or a Windows junction (cmd_env._is_link: os.path.islink misses those)."""
    from .cmd_env import _is_link  # cmd_env imports this module

    return _is_link(path)


def not_an_install(snapshot: Path) -> str:
    """Why pyt install may not replace (nor uninstall delete) what is at `snapshot`, the folder of
    the installed template: "" when it is the copy an earlier install made there. A link is never
    followed: the folder it names is not pyt install's to replace or delete (a link to a real
    install was refused as one that "holds no" record, which it did hold)."""
    if _is_link(snapshot):
        try:
            target = f" to {os.readlink(snapshot)}"
        except (OSError, ValueError):
            target = ""
        return f"it is a symbolic link{target} (pyt install never makes one, nor writes through one)"
    if not snapshot.is_dir():
        return "it is not a folder (pyt install did not make it)"
    if read_record(snapshot) is None:
        return f"it holds no {RECORD} (pyt install did not make it)"
    return ""


def _same_folder(a: Path, b: Path) -> bool:
    """Whether two paths name one folder (spelled differently, or through a link, too)."""
    if _norm(a) == _norm(b):
        return True
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def recorded_bin(record: Mapping[str, object] | None) -> Path | None:
    """The bin folder an install record names (where that install wrote its launchers)."""
    value = record.get("bin") if record else None
    return Path(value) if isinstance(value, str) and value and os.path.isabs(value) else None


# PATHEXT without a PATHEXT variable: cmd's own default order
DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"


def pathext_shadows(folder: Path, environ: Mapping[str, str] | None = None) -> list[Path]:
    """Windows: the files of `folder` that a bare `pyt` runs instead of pyt.cmd. cmd, xonsh,
    nushell and Python's subprocess take the first pyt<ext> of a folder in PATHEXT order, and
    .COM, .EXE and .BAT come before .CMD: another tool's pyt.exe there (`uv tool install
    python-taint` puts one) won in them, and nothing said so. PowerShell takes pyt.ps1 first."""
    if not IS_WINDOWS:
        return []
    env = os.environ if environ is None else environ
    pathext = next((v for k, v in env.items() if k.upper() == "PATHEXT"), "") or DEFAULT_PATHEXT
    exts = [e.strip().lower() for e in pathext.split(";") if e.strip().startswith(".")]
    before = exts[: exts.index(".cmd")] if ".cmd" in exts else exts
    return [folder / f"pyt{ext}" for ext in dict.fromkeys(before) if os.path.lexists(folder / f"pyt{ext}")]


SHADOW_FIX = (
    "remove such a file: another tool's command (`uv tool list` names the tool, `uv tool uninstall "
    "TOOL` removes it), or set UV_TOOL_BIN_DIR to a folder of its own and run ./pyt install again"
)


def _shadow_problem(folder: Path, shadows: list[Path]) -> str:
    names = ", ".join(p.name for p in shadows)
    which = "it comes" if len(shadows) == 1 else "they come"
    return (
        f"{names} in {folder} would run instead of pyt.cmd: cmd, xonsh, nushell and Python start the "
        f"first pyt<ext> of a folder in PATHEXT order, and {which} before .cmd"
    )


def policy_notes() -> list[tuple[str, str]]:
    """Windows: (what, what to do) for each PowerShell whose execution policy blocks scripts. A
    bare `pyt` there runs pyt.ps1 (PowerShell takes it before pyt.cmd), which then does not
    start: Restricted is Windows PowerShell 5.1's default on client Windows (shells.doctor reads
    the same policies)."""
    if not IS_WINDOWS or "pyt.ps1" not in LAUNCHERS:
        return []
    return [
        (
            f"{label}: ExecutionPolicy = {policy}: a bare `pyt` there runs pyt.ps1, which this policy does not let run",
            "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned, or type pyt.cmd in that PowerShell",
        )
        for label, _edition, policy in shells.ps_policies()
        if policy in shells.BLOCKING_POLICIES
    ]


# pyt.cmd names itself here (%~f0): cmd reads a batch file one line at a time, opening it again
# by name once the command of a line has ended, so the pyt.cmd that started this run must be
# neither deleted nor replaced under it (it said "The batch file cannot be found.", or ran what
# the new file held at that offset)
LAUNCHER_FILE = "PYTEMPLATE_LAUNCHER_FILE"
# The line cmd reads after the uv call once uninstall put self_deleting() in place: the batch
# ends ((goto) to no label), then the file is deleted and the exit code is uv's again (a
# %ERRORLEVEL% of that line is expanded after uv returned)
SELF_DELETE = b'(goto) 2>nul & del "%~f0" & "%ComSpec%" /d /c exit %ERRORLEVEL%\r\n'
_LEFTOVER = (
    b"@echo off\r\n"
    b"rem pytemplate-launcher: what pyt uninstall left of pyt.cmd while cmd ran it; it deletes itself.\r\n"
    b'>&2 echo pyt: pyt is not installed (pyt uninstall ran): delete this leftover file, "%~f0"\r\n'
    b"exit /b 1\r\n"
)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def run_by_cmd(path: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Whether cmd runs `path` for this very run: pyt.cmd hands over its own path (LAUNCHER_FILE)
    with PYTEMPLATE_LAUNCHER=cmd, and cmd reads that file again when this run ends."""
    env = os.environ if environ is None else environ
    running = env.get(LAUNCHER_FILE, "")
    if not running or not env.get("PYTEMPLATE_LAUNCHER", "").startswith("cmd"):
        return False
    try:
        return os.path.samefile(running, path)
    except OSError:
        return False


def self_deleting(running: bytes) -> bytes | None:
    """What uninstall writes in place of the pyt.cmd cmd is running (`running`, its bytes): the
    same length up to the line after its uv call (the one line with the argument list), where
    cmd goes on reading, and there SELF_DELETE. Started on its own the file only says what it is
    (_LEFTOVER), exit 1. None when `running` has no such line where the rest fits before it."""
    start = running.find(b"%*")
    end = running.find(b"\n", start) if start >= 0 and running.find(b"%*", start + 2) < 0 else -1
    if end < 0:
        return None
    pad = end + 1 - len(_LEFTOVER)  # bytes of rem lines between _LEFTOVER and SELF_DELETE
    if pad < 0:
        return None
    head = _LEFTOVER
    if pad < 5:  # shorter than "rem" + CRLF: the marker line takes them as trailing blanks
        head = head.replace(b"itself.\r\n", b"itself." + b" " * pad + b"\r\n", 1)
        pad = 0
    lines = []
    while pad:
        size = pad if pad <= 4000 else (4000 if pad - 4000 >= 5 else pad - 5)
        lines.append(b"rem" + b" " * (size - 5) + b"\r\n")
        pad -= size
    return head + b"".join(lines) + SELF_DELETE


def read_record(snapshot: Path | None) -> dict[str, object] | None:
    """The record of the installed template: None when there is none (no such folder, or one
    pyt install did not make), {} when it cannot be read."""
    if snapshot is None:
        return None
    path = snapshot / RECORD
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --- git ----------------------------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    """git in the template (no optional locks: a status must not write the index); None without git."""
    git = shutil.which("git")
    if git is None:
        return None
    env = {**presets._git_env(), "LC_ALL": "C"}
    return proc.run([git, "--no-optional-locks", *args], cwd=ROOT, env=env, capture=True, check=False, echo=False)


def source_state() -> tuple[str | None, bool | None]:
    """This clone's commit and whether its tracked files have uncommitted changes (None: unknown)."""
    head = _git("rev-parse", "--verify", "--quiet", "HEAD")
    commit = head.stdout.strip() if head is not None and head.returncode == 0 else ""
    status = _git("status", "--porcelain", "--untracked-files=no", "--", ".")
    dirty = bool(status.stdout.strip()) if status is not None and status.returncode == 0 else None
    return commit or None, dirty


def _short(commit: object) -> str:
    return str(commit)[:7] if isinstance(commit, str) and commit else "unknown"


def commit_text(record: Mapping[str, object]) -> str:
    """`commit abc1234`, with the uncommitted changes the record notes."""
    return f"commit {_short(record.get('commit'))}" + (" with uncommitted changes" if record.get("dirty") else "")


def describe(record: Mapping[str, object]) -> str:
    """What an install record says, for one line of output."""
    parts = [commit_text(record)]
    when = record.get("installed")
    if isinstance(when, str) and when:
        parts.append(f"installed {when[:10]}")
    source = record.get("source")
    if isinstance(source, str) and source:
        parts.append(f"from {source}")
    return ", ".join(parts)


def age(record: Mapping[str, object]) -> str:
    """In the template repository: how the installed template's commit relates to this clone's
    ("" when they are the same, or when this is no clone)."""
    if not (TEMPLATE / "template-repo").is_file() or installed_here():
        return ""
    commit = record.get("commit")
    head, _dirty = source_state()
    if not isinstance(commit, str) or not commit or head is None or commit == head:
        return ""
    r = _git("merge-base", "--is-ancestor", commit, "HEAD")
    if r is not None and r.returncode == 0:
        return f"the installed template (commit {_short(commit)}) is older than this clone ({_short(head)})"
    if r is not None and r.returncode == 1:
        return f"the installed template (commit {_short(commit)}) is not in the history of this clone's HEAD ({_short(head)}): newer, or another branch"
    return f"the installed template's commit {_short(commit)} is unknown to this clone"


# --- PATH ---------------------------------------------------------------------------------------


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.expanduser(str(path))))


def on_path(folder: Path, path: str) -> bool:
    """Whether `folder` is an entry of a PATH value (spelled differently, or reached through a
    link, too)."""
    want = _norm(folder)
    for entry in path.split(os.pathsep):
        entry = entry.strip().strip('"') if IS_WINDOWS else entry
        if not entry:
            continue
        if _norm(entry) == want:
            return True
        with contextlib.suppress(OSError, ValueError):
            if os.path.samefile(os.path.expanduser(entry), folder):
                return True
    return False


def path_state(folder: Path, environ: Mapping[str, str] | None = None) -> str:
    """"on" (this process's PATH has `folder`), "registry" (Windows: only the PATH stored in the
    registry has it, and a console opened before it was added keeps its old PATH) or "off"."""
    env = os.environ if environ is None else environ
    path = next((v for k, v in env.items() if k.upper() == "PATH"), "") if IS_WINDOWS else env.get("PATH", "")
    if on_path(folder, path):
        return "on"
    if IS_WINDOWS and on_path(folder, os.pathsep.join(shells.registry_path_dirs(env))):
        return "registry"
    return "off"


def path_problem(folder: Path) -> tuple[str, str] | None:
    """(what, what to do) when `pyt` in `folder` is not found by its name; None when it is."""
    state = path_state(folder)
    if state == "on":
        return None
    if state == "registry":
        return (
            f"{folder} is on your PATH, but not on this terminal's",
            "open a new terminal: one opened before the change keeps its old PATH",
        )
    also = " (on Windows, a console opened before the change keeps its old PATH)" if IS_WINDOWS else ""
    return (
        f"{folder} is not on PATH, so `pyt` is not found by its name",
        f"run `{UPDATE_SHELL}` (it adds uv's tool folder to PATH), then open a new terminal{also}",
    )


def first_pyt(path: str) -> Path | None:
    """The first `pyt` command of a PATH value, the folder where a shell stops looking: on Windows
    the first folder that holds pyt.ps1, pyt.cmd, pyt.exe, pyt.bat or pyt (which one runs depends
    on the shell), elsewhere the first executable file named pyt. The current folder is not
    searched (shutil.which puts it first on Windows: a project's own pyt.cmd, typed there as
    `pyt`, is the right one to run)."""
    names = ("pyt.ps1", "pyt.cmd", "pyt.exe", "pyt.bat", "pyt") if IS_WINDOWS else ("pyt",)
    for entry in path.split(os.pathsep):
        entry = entry.strip().strip('"') if IS_WINDOWS else entry
        if not entry:
            continue
        for name in names:
            candidate = Path(os.path.expanduser(entry)) / name
            if candidate.is_file() and (IS_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def shadowing(folder: Path) -> str:
    """Another `pyt` that comes before the installed one on PATH ("" when there is none)."""
    found = first_pyt(proc.base_env().get("PATH", ""))  # without the runner's own bin folder
    if found is None:
        return ""
    with contextlib.suppress(OSError, ValueError):
        if _norm(found.parent) == _norm(folder) or os.path.samefile(found.parent, folder):
            return ""
    return str(found)


# --- install ------------------------------------------------------------------------------------


@dataclass
class Plan:
    snapshot: Path  # the installed template
    bin: Path  # uv's tool bin folder
    files: list[str]  # the paths (relative to ROOT) the installed template gets
    record: dict[str, object]
    old: dict[str, object] | None  # the record of the installed template it replaces
    launchers: dict[str, bytes]  # launcher -> its bytes
    replaced: list[str] = field(default_factory=list)  # launchers of an earlier install it rewrites
    left_out: list[tuple[str, list[str]]] = field(default_factory=list)  # (why, paths) not copied
    # The launchers of the earlier install in the bin folder its record names, when uv's tool bin
    # folder is another one now (UV_TOOL_BIN_DIR changed): removed once the swap has succeeded
    earlier: list[Path] = field(default_factory=list)


def _template_ci(rel_path: str) -> bool:
    """The template's own CI (`.github/workflows/template-*`), which `new` never copies either."""
    parts = rel_path.split("/")
    return len(parts) >= 3 and parts[:2] == [".github", "workflows"] and parts[2].startswith("template-")


def _inside(path: Path, folder: Path) -> bool:
    """Whether `path` is `folder` or below it (both resolved)."""
    try:
        path, folder = path.resolve(), folder.resolve()
    except OSError:
        return False
    return path == folder or folder in path.parents


def _refuse_elsewhere() -> None:
    if os.environ.get("PYTEMPLATE_GLOBAL") == "1":  # typed outside any project: a launcher ran the installed template
        raise PytError(f"outside a project pyt install installs nothing: to install or update pyt, run {FROM_A_CLONE}")
    if installed_here():
        raise PytError(f"this is the installed copy of the template ({ROOT}): {FROM_A_CLONE} updates it")
    if not (TEMPLATE / "template-repo").is_file():
        raise PytError(f"pyt install installs the template itself, and this is a project made with pyt new: run {FROM_A_CLONE}")


def _launcher_bytes(name: str) -> bytes:
    """A launcher of this template, checked as `./pyt doctor` checks it, with its MARKER line."""
    try:
        data = (ROOT / name).read_bytes()
    except OSError as e:
        raise PytError(f"cannot read the template's {name}: {e.strerror or e}  (restore it: git checkout -- {name})") from None
    problems = shells.launcher_problems(name, data, None)
    if MARKER.search(data[:4096]) is None:
        problems.append(("no `pytemplate-launcher` line in its header", f"restore it: git checkout -- {name}"))
    if problems:
        fixes = "\n  ".join(dict.fromkeys(f for _, f in problems))
        raise PytError(f"the template's {name} cannot be installed: {'; '.join(p for p, _ in problems)}\n  {fixes}")
    return data


def _refuse(problems: list[str]) -> None:
    """Every refusal of one run in one message (each on its own line when there are several)."""
    if len(problems) == 1:
        raise PytError(problems[0])
    if problems:
        lines = "\n".join("  - " + p.replace("\n", "\n    ") for p in problems)
        raise PytError(f"pyt install writes nothing, for {len(problems)} reasons:\n{lines}")


def make_plan() -> Plan:
    """Everything install writes, and every refusal, before the first write. What stops it is
    gathered and said in one message: fixing one thing, then meeting the next on the next run,
    took a run per problem. Only what makes the rest uncheckable stops it at once (this is no
    clone of the template; no data folder; uv cannot name its bin folder: exit 3)."""
    _refuse_elsewhere()
    snapshot = snapshot_dir()
    if snapshot is None:
        raise PytError(f"no user data folder to install the template into: {data_home_names()} is not set", 3)
    folder = bin_dir()
    problems: list[str] = []
    if _inside(snapshot, ROOT) or _inside(ROOT, snapshot):
        problems.append(f"the installed template would be {snapshot}, which overlaps this clone ({ROOT}): set another data folder")
    elif os.path.lexists(snapshot) and (why := not_an_install(snapshot)):
        problems.append(f"{snapshot}: {why}: move it away (or set another data folder), then run ./pyt install again")
    tracked, how = presets._tracked_template()
    if tracked is None:
        problems.append(f"pyt install copies the files git tracks in the template, and here it cannot tell which ({how}): use a git clone ({presets.TEMPLATE_URL})")
    for inside in (ROOT, snapshot.parent):
        if _inside(folder, inside):
            problems.append(f"uv's tool bin folder {folder} is inside {inside}: set UV_TOOL_BIN_DIR to a folder of its own")
    if (folder / ".pytemplate").exists():
        problems.append(f"uv's tool bin folder {folder} is a project's folder (it holds .pytemplate): set UV_TOOL_BIN_DIR to a folder of its own")
    launchers: dict[str, bytes] = {}
    for name in LAUNCHERS:
        try:
            launchers[name] = _launcher_bytes(name)
        except PytError as e:
            problems.append(str(e))
    for name, data in launchers.items():
        target = folder / name
        if run_by_cmd(target) and _read(target) != data:
            problems.append(
                f"cmd runs {target}, which pyt install would replace, and reads it again once this run ends: "
                f"run the clone's own launcher instead, .\\pyt.cmd install in {ROOT} (or pyt install from PowerShell)"
            )
    blocked = [f"{folder / n}: {why}" for n in LAUNCHERS if os.path.lexists(folder / n) and (why := not_ours(folder / n))]
    if blocked:
        lines = "\n  ".join(blocked)
        problems.append(f"pyt install never overwrites a file it did not write:\n  {lines}\n  Move it away (or set UV_TOOL_BIN_DIR), then run ./pyt install again")
    shadows = pathext_shadows(folder)
    if shadows:
        problems.append(f"{_shadow_problem(folder, shadows)}:\n  {SHADOW_FIX}")
    _refuse(problems)
    assert tracked is not None  # a refusal otherwise
    untracked = [p for p in presets._git_files("--others", "--exclude-standard") or [] if not _template_ci(p)]
    deleted = [p for p in tracked if not os.path.lexists(ROOT / p)]
    commit, dirty = source_state()
    record: dict[str, object] = {
        "schema": SCHEMA,
        "commit": commit,
        "dirty": dirty,
        "source": str(ROOT),
        "bin": str(folder),
        "launchers": list(LAUNCHERS),
        "installed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    old = read_record(snapshot)
    earlier = recorded_bin(old)
    return Plan(
        snapshot=snapshot,
        bin=folder,
        files=[p for p in tracked if not _template_ci(p) and p != RECORD and os.path.lexists(ROOT / p)],
        record=record,
        old=old,
        launchers=launchers,
        replaced=[n for n in LAUNCHERS if is_launcher(folder / n)],
        left_out=[(why, paths) for why, paths in (("deleted in the working tree", deleted), ("not tracked by git", untracked)) if paths],
        earlier=[earlier / n for n in NAMES if is_launcher(earlier / n)] if earlier is not None and not _same_folder(earlier, folder) else [],
    )


def _stage(folder: Path, name: str, data: bytes) -> Path:
    """`data` in a new temporary file of `folder`, executable, ready to replace `name`."""
    fd, tmp = tempfile.mkstemp(prefix=f".{name}-install-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o755)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return Path(tmp)


def _rename(src: Path, dst: Path) -> None:
    """os.rename, retried on Windows for a few seconds: a scanner may still hold a file of a
    folder that was just written."""
    for attempt in range(8):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == 7:
                raise
            time.sleep(0.25 * (attempt + 1))


def _copy_files(files: Iterable[str], dest: Path) -> None:
    """The template's files into `dest` (links as links, as copy_template copies them)."""
    for rel_path in files:
        src, target = ROOT / rel_path, dest / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            presets._copy_link(src, target, rel_path, command="install")
        elif src.is_dir():  # a submodule
            shutil.copytree(src, target, symlinks=True, ignore=presets._ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target)


def remove_leftovers(snapshot: Path, folders: Iterable[Path], stuck: list[Path] | None = None) -> list[Path]:
    """What an unfinished install or uninstall left: `.template-new-*`/`.template-old-*` next to
    the installed template, and staged launchers in the bin folders. Returns what it removed;
    what it could not remove goes to `stuck` (a file in use)."""
    gone: list[Path] = []
    with contextlib.suppress(OSError):
        for entry in snapshot.parent.iterdir():
            if entry.name.startswith((NEW, OLD)):
                if presets._remove(entry):
                    gone.append(entry)
                elif stuck is not None:
                    stuck.append(entry)
    for folder in folders:
        with contextlib.suppress(OSError):
            for entry in folder.iterdir():
                if STAGED.match(entry.name) and is_launcher(entry):
                    if _unlink(entry):
                        gone.append(entry)
                    elif stuck is not None:
                        stuck.append(entry)
    return gone


def _free_name(parent: Path, prefix: str) -> Path:
    """A path in `parent` that holds nothing yet: `prefix` and a random suffix."""
    while True:
        path = parent / f"{prefix}{secrets.token_hex(4)}"
        if not os.path.lexists(path):
            return path


def _new_folder(parent: Path, prefix: str) -> Path:
    """A new empty folder in `parent` with the mode a plain mkdir gives (0o777 minus the umask).
    tempfile.mkdtemp makes it 0o700, and so the installed template's own folder was: `pyt new`
    from it gave every new project that mode (copy_template copied the folder's mode then)."""
    while True:
        path = _free_name(parent, prefix)
        try:
            os.mkdir(path)
        except FileExistsError:  # made by someone else in between
            continue
        return path


class _Swap:
    """install's writes, each one undone when a later one fails (Ctrl+C too)."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self.made_parent = False  # <data home>/pytemplate did not exist
        self.fresh: Path | None = None  # the new copy until it is swapped in
        self.aside: Path | None = None  # the old copy, moved aside
        self.swapped = False
        self.staged: list[tuple[Path, Path]] = []  # (temporary file, launcher)
        self.replaced: list[tuple[Path, bytes | None]] = []  # launcher, its bytes before (None: new)

    def run(self) -> None:
        plan = self.plan
        parent = plan.snapshot.parent
        self.made_parent = not parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
        # Noted only once it exists: undo reads a missing self.fresh as renamed into place
        self.fresh = _new_folder(parent, NEW)
        _copy_files(plan.files, self.fresh)
        record = self.fresh / RECORD
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(plan.record, indent=2) + "\n", encoding="utf-8", newline="\n")
        plan.bin.mkdir(parents=True, exist_ok=True)
        for name, data in plan.launchers.items():
            self.staged.append((_stage(plan.bin, name, data), plan.bin / name))
        if os.path.lexists(plan.snapshot):
            self.aside = parent / (OLD + self.fresh.name[len(NEW) :])
            _rename(plan.snapshot, self.aside)
        _rename(self.fresh, plan.snapshot)
        self.fresh, self.swapped = None, True
        while self.staged:
            tmp, target = self.staged[0]
            # Recorded before the replace: an interrupt right after it must still undo it
            self.replaced.append((target, target.read_bytes() if os.path.lexists(target) else None))
            try:
                os.replace(tmp, target)
            except OSError as e:
                # Nothing was replaced (os.replace is all or nothing): undo has nothing to put back
                # there (it said "could not put back" for a launcher that never changed), and the
                # error names the launcher, not the staged file (its filename)
                self.replaced.pop()
                if e.errno is None:
                    raise
                raise OSError(e.errno, e.strerror or os.strerror(e.errno), str(target)) from e
            self.staged.pop(0)

    def undo(self) -> list[str]:
        """Put every launcher and the old copy back; return what could not be."""
        failed: list[str] = []
        for target, before in reversed(self.replaced):
            if before is not None and _read(target) == before:
                continue  # the replace never happened (or put the same bytes there)
            staged: Path | None = None
            try:
                if before is None:
                    with contextlib.suppress(FileNotFoundError):  # the replace never happened
                        target.unlink()
                else:
                    staged = _stage(target.parent, target.name, before)
                    os.replace(staged, target)
                    staged = None
            except OSError as e:
                failed.append(f"{target} ({e.strerror or e})")
            finally:
                if staged is not None:  # its replace failed: never a staged file left behind
                    _unlink(staged)
        for tmp, _target in self.staged:
            with contextlib.suppress(OSError):
                tmp.unlink()
        snapshot = self.plan.snapshot
        # The new copy's own folder can only have gone by its rename to the snapshot (an
        # interrupt right after that rename, before `swapped` was set)
        if self.fresh is not None and not os.path.lexists(self.fresh):
            self.fresh, self.swapped = None, True
        if self.swapped:
            gone = snapshot.parent / f"{NEW}undone-{os.getpid()}"
            try:
                _rename(snapshot, gone)
                presets._remove(gone)
            except OSError as e:
                failed.append(f"{snapshot} ({e.strerror or e})")
        if self.aside is not None and not os.path.lexists(snapshot):
            try:
                _rename(self.aside, snapshot)
            except OSError as e:
                failed.append(f"{snapshot}: the old copy is {self.aside} ({e.strerror or e})")
        if self.fresh is not None:
            presets._remove(self.fresh)
        if self.made_parent:
            with contextlib.suppress(OSError):
                snapshot.parent.rmdir()
        return failed

    def finish(self) -> None:
        """Delete the old copy (a warning when a file of it is still in use)."""
        if self.aside is not None and not presets._remove(self.aside):
            ui.warn(f"could not delete the old copy {self.aside} completely (a file in it is in use?): delete it by hand")


@contextlib.contextmanager
def _terminations_interrupt() -> Iterator[None]:
    """While the swap runs, SIGTERM and SIGHUP (POSIX) stop it like Ctrl+C: proc.Interrupted, so
    its undo runs, and cli.main ends the command as it ends every interrupted one (`error:
    terminated (SIGTERM)`, 128 + N). Their default action killed the runner at once and left a
    whole `.template-new-*` copy next to the installed template. The first one also ignores the
    next ones, so the undo is never cut short. Only in the main thread, and only while the
    signal has its default handler (under nohup it stays ignored), as proc.run does."""
    installed: list[int] = []
    if sys.platform != "win32" and threading.current_thread() is threading.main_thread():
        watched = (signal.SIGTERM, signal.SIGHUP)

        def interrupt(signum: int, _frame: object) -> None:
            for sig in installed:
                signal.signal(sig, signal.SIG_IGN)
            raise proc.Interrupted(0, signum)

        for sig in watched:
            if signal.getsignal(sig) is signal.SIG_DFL:
                signal.signal(sig, interrupt)
                installed.append(sig)
    try:
        yield
    finally:
        for signum in installed:
            signal.signal(signum, signal.SIG_DFL)


def install(plan: Plan) -> None:
    """Write the installed template and the launchers: all or nothing. Then the launchers of the
    earlier install in another bin folder go (Plan.earlier)."""
    remove_leftovers(plan.snapshot, [plan.bin])
    swap = _Swap(plan)
    with _terminations_interrupt():
        try:
            swap.run()
        except BaseException as e:
            failed = swap.undo()
            note = "nothing was changed" if not failed else "could not put back: " + ", ".join(failed)
            if isinstance(e, PytError):
                raise PytError(f"{e}\n  {note}", e.code) from e
            if isinstance(e, OSError):
                where = f"{e.filename}: " if e.filename else ""
                raise PytError(f"pyt install failed: {where}{e.strerror or e}\n  {note}", 1) from e
            ui.error(note)  # Ctrl+C, SIGTERM, SIGHUP (or a bug: its traceback follows)
            raise
    swap.finish()
    _remove_earlier(plan)


def _remove_earlier(plan: Plan) -> None:
    """The launchers the earlier install wrote into another bin folder (UV_TOOL_BIN_DIR changed
    since): they stayed on PATH, and neither uninstall (it reads the new record) nor doctor looked
    there any more. Only files with the MARKER (Plan.earlier: never a link); the pyt.cmd cmd runs
    for this very run gets its self-deleting stand-in (_retire)."""
    if not plan.earlier:
        return
    ui.report(f"  the earlier install's launchers in {plan.earlier[0].parent} (uv's tool bin folder is {plan.bin} now):")
    for path in plan.earlier:
        removed: list[str] = []
        failed: list[str] = []
        if run_by_cmd(path):
            _retire(path, removed, [], failed)
        elif _unlink(path):
            removed.append(f"removed {path}")
        else:
            failed.append(f"{path}: in use or not writable")
        for line in removed:
            ui.report(f"    {line}")
        for line in failed:
            ui.warn(f"could not remove the earlier install's launcher {line}: delete it by hand")


def _say_plan(plan: Plan) -> None:
    ui.step(f"pyt install: the template of {ROOT} ({commit_text(plan.record)})")
    replaces = f"; it replaces the one of {commit_text(plan.old)}" if plan.old is not None else ""
    ui.info(f"  installed template: {plan.snapshot} ({len(plan.files)} files{replaces})")
    ui.info(f"  launchers in {plan.bin}: {', '.join(plan.launchers)}" + (" (rewritten)" if plan.replaced else ""))
    for why, paths in plan.left_out:
        more = f" and {len(paths) - 5} more" if len(paths) > 5 else ""
        ui.info(f"  not copied ({why}): {', '.join(paths[:5])}{more}")
    if plan.earlier:
        names = ", ".join(p.name for p in plan.earlier)
        ui.info(f"  the earlier install's launchers in {plan.earlier[0].parent} ({names}) go once the new ones are in place")


def _say_notes(folder: Path) -> None:
    """What stands between the user and a bare `pyt`: PATH, another pyt before it, and on Windows
    a PowerShell whose execution policy blocks pyt.ps1."""
    problem = path_problem(folder)
    if problem is not None:
        ui.warn(f"{problem[0]}: {problem[1]}")
    else:
        other = shadowing(folder)
        if other:
            ui.warn(f"another pyt comes first on PATH: {other} (it runs instead of the installed one: remove it, or move {folder} before it)")
    for what, fix in policy_notes():
        ui.warn(f"{what}: {fix}")


def cmd_install(cfg: Config, args: list[str]) -> int:
    """install: the template into the user data folder, the launchers into uv's tool bin folder."""
    only_flags("install", args, ())
    plan = make_plan()
    _say_plan(plan)
    if proc.DRY_RUN:
        targets = ", ".join(str(plan.bin / n) for n in plan.launchers)
        also = f", and remove {', '.join(str(p) for p in plan.earlier)}" if plan.earlier else ""
        ui.report(f"(--dry-run) would copy {len(plan.files)} files into {plan.snapshot} and write {targets}{also}")
        _say_notes(plan.bin)
        return 0
    install(plan)
    ui.ok("pyt is installed: in a project `pyt` runs that project's runner; elsewhere the installed template (pyt new DIR --preset P, pyt help)")
    _say_notes(plan.bin)
    return 0


# --- uninstall ----------------------------------------------------------------------------------


def _dedupe(folders: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for folder in folders:
        if all(_norm(folder) != _norm(f) for f in out):
            out.append(folder)
    return out


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _retire(path: Path, removed: list[str], left: list[str], failed: list[str], retry: bool = False) -> bool:
    """The pyt.cmd cmd runs for this run, last: once everything else is gone, self_deleting() takes
    its place (cmd reads on from there, deletes it and keeps the exit code). After a failure it is
    left whole while `pyt uninstall` can run again from it (`retry`: the installed template's
    runner is still there, _runs_pyt); without that it goes too: kept, it only said "install it"
    (its installed template had no ENTRY any more) when uninstall was typed again. Returns
    whether it was left whole."""
    if failed and retry:
        left.append(f"left {path}: cmd runs it, and it goes only once the rest is removed (pyt uninstall runs again from it)")
        return True
    running = _read(path)
    stand_in = self_deleting(running) if running is not None else None
    if stand_in is None:  # no uv line to read on from (not our layout): cmd will not find it
        if _unlink(path):
            removed.append(f"removed {path} (cmd, which runs it, may say it cannot find the batch file)")
        else:
            failed.append(f"{path}: in use or not writable")
        return False
    try:
        project.write_whole(path, stand_in)
    except OSError as e:
        failed.append(f"{path}: {e.strerror or e}")
        return False
    removed.append(f"removed {path} (cmd runs it: it deletes itself as this run ends)")
    return False


# The installed template's runner: the launchers look for ENTRY (without it they say "install
# it"), and in its global mode the runner loads pytemplate.toml, whose app.preset must name a
# preset of .pytemplate/presets. _remove_in_place deletes them last, ENTRY first of them
ENTRY = ".pytemplate/pyt.py"


def _runs_pyt(snapshot: Path | None) -> bool:
    """Whether the launchers still run the installed template at `snapshot`, and so `pyt
    uninstall` again: its ENTRY is there. _remove_in_place deletes the ENTRY only once
    everything but the runner is gone, and the rest of the runner only after it: while the
    ENTRY is there, the runner is whole."""
    return snapshot is not None and (snapshot / ENTRY).is_file()


def remove_installed(snapshot: Path) -> Path | None:
    """Delete the installed template so that whatever a failure leaves is still known as pyt
    install's; return what is left (None: all gone). It moves aside first (`.template-old-*`,
    which remove_leftovers deletes on the next uninstall or install) and goes from there; where
    it cannot move (Windows: a terminal's current folder inside it) it is deleted in place, its
    record last. Deleted in place in any order, the record could go first: a file in use then
    left a folder that the next uninstall called the user's own and install refused."""
    aside = _free_name(snapshot.parent, OLD)
    try:
        _rename(snapshot, aside)
    except OSError:
        return _remove_in_place(snapshot)
    return None if presets._remove(aside) else aside


def _remove_in_place(snapshot: Path) -> Path | None:
    """The installed template deleted where it is, in rounds, each only once the one before has
    deleted everything: what its runner does not need, then the runner's ENTRY, then the rest of
    the runner and pytemplate.toml, then its record (RECORD) and the folder. A file that cannot
    go keeps the record, so the next uninstall finds the folder by it (deleted in any order, the
    record could go first: a file in use then left a folder that the next uninstall called the
    user's own and install refused), and, in the first round (Windows: a terminal's folder in
    it), the runner whole, so the launchers still run `pyt uninstall` (_runs_pyt): deleted in any
    order, its ENTRY went too, and the pyt.cmd kept for a retry said "install it"."""
    record = snapshot / RECORD
    inner = record.parent  # .pytemplate: the runner and the record
    config = snapshot / project.CONFIG_FILE.name
    try:
        entries = list(snapshot.iterdir())
        real = inner.is_dir() and not _is_link(inner)
        runner = list(inner.iterdir()) if real else []
    except OSError:
        return snapshot
    entry = snapshot / ENTRY
    rounds = [
        [e for e in entries if e != config and not (real and e == inner)],
        [entry],
        [*(e for e in runner if e not in (entry, record)), config],
    ]
    for paths in rounds:
        done = True
        for path in paths:
            done = presets._remove(path) and done
        if not done:
            return snapshot  # the record stays: the next uninstall finds the folder by it
    return None if presets._remove(snapshot) else snapshot


def cmd_uninstall(cfg: Config, args: list[str]) -> int:
    """uninstall: the launchers and the installed template that pyt install wrote, nothing else."""
    only_flags("uninstall", args, ())
    ui.step("pyt uninstall")
    snapshot = snapshot_dir()
    record = read_record(snapshot)
    folders: list[Path] = []
    try:
        folders.append(bin_dir())
    except PytError as e:
        ui.warn(f"{e}: no launcher is looked for there")
    recorded = recorded_bin(record)
    if recorded is not None:
        folders.append(recorded)
    removed: list[str] = []
    left: list[str] = []
    failed: list[str] = []

    running: Path | None = None  # the pyt.cmd cmd runs for this very run: it goes last
    stays: list[Path] = []  # launchers of ours it could not remove: they still run the installed template
    for folder in _dedupe(folders):
        for name in NAMES:
            path = folder / name
            if os.path.lexists(path):
                why = not_ours(path)
                if why:
                    left.append(f"left {path}: {why}")
                elif running is None and not proc.DRY_RUN and run_by_cmd(path):
                    running = path
                elif proc.DRY_RUN:
                    removed.append(f"would remove {path}")
                elif _unlink(path):
                    removed.append(f"removed {path}")
                else:
                    failed.append(f"{path}: in use or not writable")
                    stays.append(path)
    if snapshot is not None and os.path.lexists(snapshot):
        why = not_an_install(snapshot)
        if why:
            left.append(f"left {snapshot}: {why}")
        elif proc.DRY_RUN:
            removed.append(f"would remove {snapshot} (the installed template)")
        elif stays and _runs_pyt(snapshot):
            # Deleted, it left those launchers on PATH saying "install it", and `pyt uninstall`
            # could not run again from them
            left.append(f"left {snapshot} (the installed template): the launchers it could not remove run it, and it goes with them")
        else:
            stuck = remove_installed(snapshot)
            if stuck is None:
                removed.append(f"removed {snapshot} (the installed template)")
            else:
                failed.append(f"{stuck}: in use or not writable")
    if snapshot is not None and not proc.DRY_RUN:
        stuck_leftovers: list[Path] = []
        for gone in remove_leftovers(snapshot, folders, stuck_leftovers):
            removed.append(f"removed {gone} (left by an unfinished install or uninstall)")
        failed.extend(f"{p}: in use or not writable" for p in stuck_leftovers)
        with contextlib.suppress(OSError):
            snapshot.parent.rmdir()  # <data home>/pytemplate, when nothing else is in it
    failed = list(dict.fromkeys(failed))  # a folder left aside above is a leftover too
    kept = running is not None and _retire(running, removed, left, failed, retry=_runs_pyt(snapshot))
    for line in [*removed, *left]:
        ui.report(f"  {line}")
    if failed:
        # `pyt uninstall` again only where a launcher of ours is left that still runs the
        # installed template: it was promised after every failure, and a kept pyt.cmd, or a
        # launcher it could not remove, then said "install it"
        again = (kept or bool(stays)) and _runs_pyt(snapshot)
        how = "run pyt uninstall again" if again else "run ./pyt uninstall in a project or in a clone of the template"
        raise PytError("could not remove:\n  " + "\n  ".join(failed) + f"\n  Close what uses them (or make them writable), then {how}", 1)
    if not removed:
        ui.ok("pyt is not installed: nothing to remove")
    elif not proc.DRY_RUN:
        ui.ok(f"pyt is uninstalled; to install it again: {FROM_A_CLONE}")
    return 0


# --- doctor -------------------------------------------------------------------------------------


def doctor(check: Check) -> None:
    """`./pyt doctor`'s "pyt install" lines, in every mode: the launchers in uv's tool bin folder,
    the installed template (in the template repository: whether it is older than this clone), and
    whether the bin folder is on PATH. Notes only: pyt works without being installed."""
    ui.step("pyt install")
    try:
        _doctor(check)
    except (PytError, OSError) as e:
        check(None, f"could not check the installed pyt: {str(e).splitlines()[0]}", "")


def _doctor(check: Check) -> None:
    snapshot = snapshot_dir()
    record = read_record(snapshot)
    folder: Path | None = None
    try:
        folder = bin_dir()
    except PytError as e:
        check(None, str(e).splitlines()[0], "")
    ours = [n for n in NAMES if folder is not None and is_launcher(folder / n)]
    foreign = [folder / n for n in LAUNCHERS if folder is not None and os.path.lexists(folder / n) and n not in ours]
    for path in foreign:
        check(None, f"{path} is not a pytemplate launcher (pyt install leaves it alone)", "move it away before ./pyt install")
    for path in pathext_shadows(folder) if folder is not None else []:
        check(None, _shadow_problem(path.parent, [path]), SHADOW_FIX)
    if record is None and not ours:
        check(None, "pyt is not installed: outside projects there is no `pyt` command", f"to install it: {FROM_A_CLONE}")
        return
    if record is None:
        where = f"in {snapshot}" if snapshot is not None else f"(no user data folder: {data_home_names()} is not set)"
        check(None, f"launchers in {folder}, but no installed template {where}: outside projects they only say how to install it", FROM_A_CLONE)
    else:
        check(True, f"installed template: {snapshot} ({describe(record)})", "")
        older = age(record)
        if older:
            check(None, older, "./pyt install updates it")
    if folder is None:
        return
    earlier = recorded_bin(record)
    if earlier is not None and not _same_folder(earlier, folder):
        left = [n for n in NAMES if is_launcher(earlier / n)]
        if left:
            check(
                None,
                f"{', '.join(left)} in {earlier}: the launchers of this install, but uv's tool bin folder is {folder} now",
                f"pyt uninstall removes them; {FROM_A_CLONE} writes them into {folder} (and removes these)",
            )
    missing = [n for n in LAUNCHERS if n not in ours]
    stale = [n for n in ours if snapshot is not None and (snapshot / n).is_file() and (snapshot / n).read_bytes() != (folder / n).read_bytes()]
    if missing:
        check(None, f"no launcher {', '.join(missing)} in {folder}", FROM_A_CLONE)
    elif stale:
        check(None, f"{', '.join(stale)} in {folder} {'differs' if len(stale) == 1 else 'differ'} from the installed template's", FROM_A_CLONE)
    else:
        check(True, f"launchers in {folder}: {', '.join(ours)}", "")
    if "pyt.ps1" in ours:
        for what, fix in policy_notes():
            check(None, what, fix)
    problem = path_problem(folder)
    if problem is not None:
        check(None, problem[0], problem[1])
        return
    other = shadowing(folder)
    if other:
        check(None, f"another pyt comes first on PATH: {other}", f"remove it, or move {folder} before it on PATH")
    else:
        check(True, f"{folder} is on PATH", "")
