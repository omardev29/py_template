"""selftest --nvim: headless smoke test of the LazyVim integration in an isolated LazyVim.

Installs the LazyVim starter under a throwaway XDG_* tree (never the user's config), trusts
each scratch project's .lazy.lua through Neovim's API, installs the plugins and runs
.pytemplate/nvim/tests/smoke.lua in a project made from each preset.

Pinned, so a red run means a regression and not upstream drift: the starter is checked out at
cmd_nvim.STARTER_REV and the plugins at the commits of LOCK (a lazy-lock.json of a green run,
applied by a `Lazy! restore` once everything is installed, and by the startup install of each
project's plugins; any plugin left elsewhere fails the run). Without LOCK it takes the latest of
everything (starter HEAD, `Lazy! sync`, never a base kept from an earlier run): a run without it
shows upstream changes coming. Each run copies the resolved lazy-lock.json and the starter
commit to <dir>/logs/ (the new pins after a green run without LOCK).

Layout of the work directory (short on purpose: Windows MAX_PATH, deep plugin trees):

    <dir>/x/{config,data,state,cache}   XDG_*_HOME of every Neovim call (LazyVim + plugins)
    <dir>/base.json                     marker: the base above is complete (reused while pinned)
    <dir>/p/<preset>                    scratch projects (./pyt new), removed unless --keep
    <dir>/logs/                         output of every step of the last run (+ the resolved pins)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import cmd_nvim, presets, proc, ui
from .config import Config
from .project import IS_WINDOWS, ROOT, base_lock, check_private_dir, make_private_dir, scratch_name, user_path
from .ui import PytError

DEFAULT_PRESETS = ("script", "raylib", "flet")
SMOKE = ".pytemplate/nvim/tests/smoke.lua"  # relative to the project (the cwd of the smoke run)
LOCK = ROOT / ".pytemplate" / "nvim" / "tests" / "lazy-lock.json"  # plugin commits of a green run
DIR_MARKER = ".pytemplate-nvim-test"  # only directories with this file are ever deleted from
CLONE_TIMEOUT = 300.0
BASE_SYNC_TIMEOUT = 1800.0
STEP_TIMEOUT = 900.0
KILL_GRACE = 5.0  # seconds between SIGTERM (Neovim stops its jobs) and SIGKILL of a timed-out tree
FAILED = 1  # exit code of a step that failed (a FAIL of the suite; 2 is a usage error)
PHASES = ("new+sync", "trust+lazy", "smoke")
# The smoke's mypy check needs a typing profile: every preset defaults to typing.relaxed = off.
SMOKE_TYPING = "strict"

# The inner ./pyt runs as if typed in a fresh shell: nothing from this runner's own
# `uv run --script` environment, nor from a shell's stale PYTEMPLATE_* exports.
RUNNER_DROP = frozenset({"VIRTUAL_ENV", "UV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON"})
# Anything that could make Neovim read the user's own config, data or server.
NVIM_DROP = frozenset({"NVIM", "NVIM_APPNAME", "NVIM_LISTEN_ADDRESS", "NVIM_LOG_FILE", "VIMINIT", "EXINIT", "MYVIMRC", "MYGVIMRC"})
XDG_HOMES = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")


def default_dir() -> Path:
    """%TEMP%\\pt\\nvim on Windows (short), $TMPDIR/pt-nvim-<uid> elsewhere (/tmp is shared)."""
    tmp = Path(tempfile.gettempdir())
    return tmp / "pt" / "nvim" if IS_WINDOWS else tmp / scratch_name("pt-nvim")


@dataclass(frozen=True)
class Layout:
    base: Path

    @property
    def xdg(self) -> Path:
        return self.base / "x"

    def home(self, key: str) -> Path:
        """XDG_CONFIG_HOME -> <base>/x/config, and so on."""
        return self.xdg / key.removeprefix("XDG_").removesuffix("_HOME").lower()

    @property
    def marker(self) -> Path:
        return self.base / "base.json"

    @property
    def projects(self) -> Path:
        return self.base / "p"

    @property
    def logs(self) -> Path:
        return self.base / "logs"

    @property
    def nvim_log(self) -> Path:
        return self.logs / "nvim.log"


# --- environments ---------------------------------------------------------------------------------


def _key(name: str) -> str:
    return name.upper() if IS_WINDOWS else name


def runner_env(source: Mapping[str, str]) -> dict[str, str]:
    """Environment of the inner ./pyt calls: `source` minus VIRTUAL_ENV, UV*, PYTEMPLATE_*."""
    return {k: v for k, v in source.items() if _key(k) not in RUNNER_DROP and not _key(k).startswith("PYTEMPLATE_")}


def nvim_env(layout: Layout, source: Mapping[str, str], keep: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment of every Neovim call: isolated XDG_* homes and log, no user config/appname.

    `keep` adds variables on top (uv's cache and Python dirs, so that uv run from inside
    Neovim does not start from an empty cache when XDG_CACHE_HOME/XDG_DATA_HOME move).
    """
    env = {k: v for k, v in runner_env(source).items() if _key(k) not in NVIM_DROP and not _key(k).startswith("XDG_")}
    for key in XDG_HOMES:
        env[key] = str(layout.home(key))
    env["NVIM_LOG_FILE"] = str(layout.nvim_log)
    for k, v in (keep or {}).items():
        env.setdefault(k, v)
    return env


def uv_dirs(env: Mapping[str, str]) -> dict[str, str]:
    """Return UV_CACHE_DIR / UV_PYTHON_INSTALL_DIR / UV_TOOL_DIR as uv resolves them in `env`."""
    out: dict[str, str] = {}
    uv = proc.find_uv()
    for var, args in (("UV_CACHE_DIR", ["cache", "dir"]), ("UV_PYTHON_INSTALL_DIR", ["python", "dir"]), ("UV_TOOL_DIR", ["tool", "dir"])):
        if var in env:
            continue
        r = subprocess.run([uv, *args], env=dict(env), capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
        value = r.stdout.strip()
        if r.returncode == 0 and value:
            out[var] = value
    return out


def user_git_config(base: Path, source: Mapping[str, str]) -> str | None:
    """A GIT_CONFIG_GLOBAL file for Neovim's git (lazy.nvim clones the plugins with the user's
    config: a proxy, url.*.insteadOf, http.sslCAInfo), or None when there is nothing to include.

    git's global config is two files, ~/.gitconfig and $XDG_CONFIG_HOME/git/config, but nvim_env
    moves XDG_CONFIG_HOME into the isolated tree, so git would read the empty moved one and miss
    the user's. GIT_CONFIG_GLOBAL replaces BOTH defaults, so this file includes both of the user's
    (resolved from `source`, the env before the isolation), and is written under `base`. In git's
    own order: the XDG file first, then ~/.gitconfig, whose value wins for a key set in both
    (the last one read wins; the other order gave Neovim's git the XDG file's proxy).
    """
    files: list[Path] = []
    home = source.get("HOME") or source.get("USERPROFILE")
    xdg = source.get("XDG_CONFIG_HOME")
    if xdg or home:
        files.append(Path(xdg) / "git" / "config" if xdg else Path(str(home)) / ".config" / "git" / "config")
    if home:
        files.append(Path(home) / ".gitconfig")
    includes = [f"[include]\n\tpath = {git_config_value(f.as_posix())}\n" for f in files if f.is_file()]
    if not includes:
        return None
    path = base / "gitconfig"
    path.write_text("".join(includes), encoding="utf-8", newline="\n")
    return str(path)


def git_config_value(text: str) -> str:
    """`text` as a git config value: in double quotes, where `#` and `;` start no comment, with
    `\\` and `"` escaped and a line break, tab or backspace written as git's escape. Raw, a home
    folder named `user#1` ended the include at `#` (git dropped the user's config without a word)
    and one holding `\\` made every git call fail (a bad escape)."""
    for raw, escaped in (("\\", "\\\\"), ('"', '\\"'), ("\n", "\\n"), ("\t", "\\t"), ("\b", "\\b")):
        text = text.replace(raw, escaped)
    return f'"{text}"'


# --- smoke output ------------------------------------------------------------------------------------


_RESULT = re.compile(r"^(ok|FAIL|SKIP)\s+(\S.*?)\s*$")
_DONE = re.compile(r"^DONE (\d+)\s*$")


@dataclass
class Smoke:
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (name, detail lines)
    skipped: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)
    expected: int | None = None  # smoke.lua's own count (its last line, `DONE n`)

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed) + len(self.skipped)

    @property
    def complete(self) -> bool:
        """Every result smoke.lua reported was parsed: none got lost in leaked output."""
        return self.expected is not None and self.expected == self.total


def parse_smoke(text: str) -> Smoke:
    """Parse smoke.lua's `ok   NAME` / `FAIL NAME` / `SKIP NAME` lines and its final `DONE n`.

    Lines after a FAIL that are not results (its traceback) become that failure's detail.
    """
    out = Smoke()
    detail: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        done = _DONE.match(line)
        if done:
            out.expected, detail = int(done[1]), None
            continue
        m = _RESULT.match(line)
        if m:
            kind, name = m[1], m[2]
            detail = None
            if kind == "ok":
                out.passed.append(name)
            elif kind == "SKIP":
                out.skipped.append(name)
            else:
                detail = []
                out.failed.append((name, ""))
            continue
        if detail is not None and out.failed:
            detail.append(line)
            name = out.failed[-1][0]
            out.failed[-1] = (name, "\n".join(detail).strip("\n"))
        elif line.strip():
            out.other.append(line)
    return out


# --- running things -----------------------------------------------------------------------------------


def kill_tree(p: subprocess.Popen[bytes], grace: float = KILL_GRACE) -> None:
    """Kill `p` and everything it started (git, Mason, uv, debugpy, language servers).

    Windows: taskkill /T follows the parent links. POSIX: `p` runs in its own session, so its
    process group gets SIGTERM first (Neovim then stops its jobstart jobs, which sit in their
    own sessions), and SIGKILL after `grace` seconds.
    """
    if sys.platform == "win32":
        proc.taskkill(p.pid)
    else:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            p.kill()
        try:
            p.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _wait(p: subprocess.Popen[bytes], timeout: float) -> int | None:
    """p's exit code, or None after a timeout; the whole tree dies on a timeout or on Ctrl+C
    (which no longer reaches a child in its own session)."""
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        return None
    except BaseException:
        kill_tree(p)
        raise


def _run_logged(
    argv: Sequence[str | Path],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log: Path,
    timeout: float,
) -> int | None:
    """Run with stdin closed and stdout+stderr in `log`; return the exit code (None on timeout).

    Files instead of pipes: a grandchild that outlives a killed Neovim (git, Mason) would keep
    a pipe open and block the wait forever.
    """
    args = [str(a) for a in argv]
    ui.detail(f"$ {proc.show(args)}   (in {cwd}; log {log})")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8", errors="replace") as out:
        try:
            p = subprocess.Popen(
                [proc.program(args[0]), *args[1:]], cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=not IS_WINDOWS,
            )  # fmt: skip
        except FileNotFoundError:
            raise PytError(f"program not found: {args[0]}", 3) from None
        return _wait(p, timeout)


def _remove(path: Path) -> None:
    try:
        cmd_nvim.remove_tree(path)
    except OSError as e:
        raise PytError(f"cannot remove {path}: {e}\n  Is a Neovim (or git/tar/curl) process still using it?", FAILED) from None


def _tail(log: Path, lines: int = 15) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip().splitlines()[-lines:])


def _failed(message: str, log: Path) -> PytError:
    """A step of the suite that failed: exit 1 (a FAIL), with the end of its log."""
    tail = _tail(log)
    return PytError(f"{message} (log: {log})" + (f"\n{tail}" if tail else ""), FAILED)


def _step(argv: Sequence[str | Path], *, cwd: Path, env: Mapping[str, str], log: Path, timeout: float, what: str) -> None:
    code = _run_logged(argv, cwd=cwd, env=env, log=log, timeout=timeout)
    if code != 0:
        why = f"timed out after {timeout:.0f} s" if code is None else f"exit code {code}"
        raise _failed(f"{what}: {why}", log)


def _check_isolated(nv: cmd_nvim.Nvim, layout: Layout) -> None:
    """Refuse to go on unless Neovim really uses the throwaway tree (never the user's config)."""
    root = os.path.normcase(str(layout.xdg.resolve()))
    for path in (nv.config, nv.data, nv.state, nv.cache):
        if not os.path.normcase(str(path.resolve())).startswith(root + os.sep):
            raise PytError(f"isolation check failed: Neovim reports {path}, outside {layout.xdg}", 3)


def _prepare_dir(layout: Layout) -> None:
    from .e2e import unusable

    base = layout.base
    try:  # a --dir it cannot look into (below a folder it may not enter: Path.exists raises there on
        # Python 3.11-3.13; its own without the read bit; a link loop: RuntimeError on 3.11 and 3.12)
        resolved = base.resolve()
        if resolved == ROOT or ROOT in resolved.parents:
            raise PytError(f"--dir must be outside the template ({base}): Neovim would find its .lazy.lua")
        # The projects are made below it (layout.projects), and spec.lua switches the whole ./pyt
        # integration off in a folder Neovim cannot put on its runtimepath: every smoke check failed
        # after minutes of installs, naming .lazy.lua's trust. Checked as `nvim trust` checks ROOT.
        unsafe = cmd_nvim.rtp_unsafe_char(resolved)
        if unsafe:
            raise PytError(
                f"--dir {resolved} holds `{unsafe}`, which Neovim cannot put on its 'runtimepath': the ./pyt "
                f"integration could not load in the projects made below it. Pick a --dir without {cmd_nvim.rtp_unsafe_text()}"
            )
        if base.exists() and not base.is_dir():
            raise PytError(f"--dir {base} is not a folder: pick another --dir")
        check_private_dir(base, "--dir")
        if base.exists() and any(base.iterdir()) and not (base / DIR_MARKER).is_file():
            raise PytError(f"{base} is not empty and was not created by selftest --nvim: pick another --dir")
    except (OSError, RuntimeError) as e:  # it was an internal-error traceback, exit 1
        raise PytError(f"cannot use --dir {base}: {unusable(e)}: pick another --dir") from None
    try:
        make_private_dir(base, "--dir")
        (base / DIR_MARKER).write_text("work directory of ./pyt selftest --nvim (safe to delete)\n", encoding="utf-8", newline="\n")
    except OSError as e:  # a parent that is a file, no permission
        raise PytError(f"cannot create --dir {base}: {e.strerror or e}") from None


def base_info(nv: cmd_nvim.Nvim, lock: Path | None) -> dict[str, str]:
    """What the isolated LazyVim is made of; a base.json that differs means: install it again."""
    return {
        "starter": cmd_nvim.STARTER,
        "rev": cmd_nvim.STARTER_REV if lock else "HEAD",
        "lock": hashlib.sha256(lock.read_bytes()).hexdigest() if lock else "",
        "nvim": nv.version_text,
    }


def _read_marker(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def use_lock(nv: cmd_nvim.Nvim, lock: Path | None) -> None:
    """Put the pinned lazy-lock.json in the isolated config. lazy.nvim rewrites it after each run
    and drops the plugins its spec did not name, so it goes back before every Neovim run that
    installs plugins: the startup install then checks them out at the locked commits."""
    if lock:
        shutil.copyfile(lock, nv.config / "lazy-lock.json")


def lock_drift(lock: Path, resolved: Path) -> list[str]:
    """Plugins that lazy.nvim left at another commit than the pinned lock ('name at X, pinned
    Y'), among those both files name; a file that is not a lazy-lock.json is drift too."""
    try:
        want = json.loads(lock.read_text(encoding="utf-8"))
        have = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"cannot compare {resolved} with {lock}: {e}"]
    if not isinstance(want, dict) or not isinstance(have, dict):
        return [f"{lock if not isinstance(want, dict) else resolved} is not a lazy-lock.json"]

    def commit(entry: object) -> str:
        return str(entry.get("commit", "")) if isinstance(entry, dict) else ""

    return [f"{name} at {commit(have[name])[:12] or '?'}, pinned {commit(want[name])[:12] or '?'}" for name in sorted(set(want) & set(have)) if commit(want[name]) != commit(have[name])]


def _check_pins(lock: Path | None, nv: cmd_nvim.Nvim, what: str, log: Path) -> None:
    if lock:
        drift = lock_drift(lock, nv.config / "lazy-lock.json")
        if drift:
            raise PytError(
                f"{what} left plugins off the pinned commits of {rel_lock()}: {'; '.join(drift)} "
                f"(log: {log}; --fresh reinstalls the isolated LazyVim)",
                FAILED,
            )


def prepare_base(layout: Layout, exe: str, env: Mapping[str, str], *, fresh: bool) -> tuple[cmd_nvim.Nvim, float | None]:
    """Install the LazyVim starter + plugins in the isolated tree once; return (nvim, seconds or None if reused)."""
    if fresh:
        _remove(layout.xdg)
        layout.marker.unlink(missing_ok=True)
    for key in XDG_HOMES:
        layout.home(key).mkdir(parents=True, exist_ok=True)
    nv = cmd_nvim.query(exe, env=env)
    assert nv is not None
    _check_isolated(nv, layout)
    lock = LOCK if LOCK.is_file() else None
    want = base_info(nv, lock)
    have = _read_marker(layout.marker)
    # Only a pinned base is reused: without LOCK the run takes the latest of everything, and a
    # base from an earlier run holds the starter and plugin commits of that day (record_pins
    # would report them as the new pins).
    if lock and have and all(have.get(k) == v for k, v in want.items()) and nv.lazyvim_installed():
        ui.info(f"reusing the isolated LazyVim in {layout.xdg}   (--fresh reinstalls it)")
        return nv, None
    if have and not lock:
        ui.info(f"no {rel_lock()}: reinstalling the isolated LazyVim to take the latest of everything")
    elif have:
        changed = ", ".join(f"{k} {have.get(k)} -> {v}" for k, v in want.items() if have.get(k) != v)
        ui.info(f"the isolated LazyVim was made for something else ({changed or 'incomplete'}): reinstalling it")
    ui.step(f"isolated LazyVim in {layout.xdg} (this takes a few minutes)")
    start = time.perf_counter()
    _remove(layout.xdg)
    for key in XDG_HOMES:
        layout.home(key).mkdir(parents=True, exist_ok=True)
    git = cmd_nvim.which("git") or "git"
    clone_log = layout.logs / "clone.log"
    if lock:
        # the pinned starter commit: a full clone (the repository is tiny), then a checkout
        ui.command(f"git clone {cmd_nvim.STARTER} {nv.config} && git checkout {cmd_nvim.STARTER_REV}")
        _step([git, "clone", cmd_nvim.STARTER, nv.config], cwd=layout.base, env=env, log=clone_log, timeout=CLONE_TIMEOUT, what="clone the LazyVim starter")
        _step(
            [git, "-C", nv.config, "-c", "advice.detachedHead=false", "checkout", cmd_nvim.STARTER_REV],
            cwd=layout.base, env=env, log=layout.logs / "checkout.log", timeout=CLONE_TIMEOUT, what=f"check out the LazyVim starter at {cmd_nvim.STARTER_REV}",
        )  # fmt: skip
    else:
        ui.command(f"git clone --depth 1 {cmd_nvim.STARTER} {nv.config}   (no {rel_lock()}: the latest)")
        _step([git, "clone", "--depth", "1", cmd_nvim.STARTER, nv.config], cwd=layout.base, env=env, log=clone_log, timeout=CLONE_TIMEOUT, what="clone the LazyVim starter")
    commit = _starter_commit(git, nv, layout, env)
    _remove(nv.config / ".git")
    use_lock(nv, lock)
    # From the work dir: there is no .lazy.lua above it, so only LazyVim's own plugins. Pinned:
    # install them (restore follows); else sync = the newest of everything.
    action = "install" if lock else "sync"
    ui.command(f'nvim --headless "+Lazy! {action}" +qa   (in {layout.base})')
    sync_log = layout.logs / "base-sync.log"
    _step(
        [nv.exe, "--headless", f"+Lazy! {action}", "+qa"],
        cwd=layout.base, env=env, log=sync_log, timeout=BASE_SYNC_TIMEOUT, what=f"Lazy! {action}",
    )
    if not (nv.data / "lazy" / "LazyVim").is_dir():
        raise _failed(f"LazyVim was not installed in {nv.data}", sync_log)
    if lock:
        # A fresh config installs in two rounds: LazyVim first, then the plugins its specs name.
        # After the first round lazy.nvim rewrites the lock, on disk and in memory, with the
        # plugins named so far, so the second round (and a restore in that same run) takes the
        # newest commits. The second run starts with everything installed and restores the lock.
        use_lock(nv, lock)
        restore_log = layout.logs / "base-restore.log"
        ui.command(f'nvim --headless "+Lazy! restore" +qa   (in {layout.base}; everything installed now)')
        _step(
            [nv.exe, "--headless", "+Lazy! restore", "+qa"],
            cwd=layout.base, env=env, log=restore_log, timeout=BASE_SYNC_TIMEOUT, what="Lazy! restore",
        )
        _check_pins(lock, nv, "Lazy! restore", restore_log)
    seconds = time.perf_counter() - start
    info = {**want, "commit": commit, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(seconds)}
    layout.marker.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    ui.ok(f"isolated LazyVim ready in {seconds:.0f} s")
    return nv, seconds


_SHA = re.compile(r"(?m)^([0-9a-f]{40})\s*$")


def _starter_commit(git: str, nv: cmd_nvim.Nvim, layout: Layout, env: Mapping[str, str]) -> str:
    """The commit the starter clone holds, read before its .git goes ('' if git cannot tell).
    Without LOCK it is the newest one: a green run makes it the next cmd_nvim.STARTER_REV."""
    log = layout.logs / "starter-commit.log"
    _step([git, "-C", nv.config, "rev-parse", "HEAD"], cwd=layout.base, env=env, log=log, timeout=CLONE_TIMEOUT, what="read the LazyVim starter commit")
    m = _SHA.search(log.read_text(encoding="utf-8", errors="replace")) if log.is_file() else None
    return m[1] if m else ""


def record_pins(layout: Layout, nv: cmd_nvim.Nvim) -> str:
    """Copy what this run used into the logs (the CI artifact): the lazy-lock.json lazy.nvim
    resolved and the starter commit. After a green run without LOCK they are the new pins
    (LOCK and cmd_nvim.STARTER_REV). Returns the summary line's text."""
    resolved = nv.config / "lazy-lock.json"
    if resolved.is_file():
        shutil.copyfile(resolved, layout.logs / "lazy-lock.json")
    commit = str(_read_marker(layout.marker).get("commit") or "")
    commit = commit if re.fullmatch(r"[0-9a-f]{40}", commit) else ""
    if commit:
        (layout.logs / "starter-commit.txt").write_text(commit + "\n", encoding="ascii", newline="\n")
    if LOCK.is_file():
        return f"starter {cmd_nvim.STARTER_REV[:12]}, plugins of {rel_lock()}"
    return f"the latest (no {rel_lock()}): starter {commit or 'commit unknown'}, plugins in {layout.logs / 'lazy-lock.json'}"


def rel_lock() -> str:
    try:
        return LOCK.relative_to(ROOT).as_posix()
    except ValueError:
        return str(LOCK)


# --- one preset ------------------------------------------------------------------------------------------


@dataclass
class Row:
    preset: str
    times: dict[str, float] = field(default_factory=dict)
    smoke: Smoke | None = None
    code: int | None = None  # exit code of the smoke run (None: timeout or not run)
    error: str = ""
    log: Path | None = None

    @property
    def ok(self) -> bool:
        s = self.smoke
        return not self.error and self.code == 0 and s is not None and not s.failed and s.total > 0 and s.complete


def run_smoke(nv: cmd_nvim.Nvim, proj: Path, env: Mapping[str, str], log: Path, err_log: Path, timeout: float) -> int | None:
    """nvim --headless -c "doautocmd UIEnter" -c "luafile smoke.lua" in `proj`: stdout alone in
    `log` (smoke.lua's result lines), stderr in `err_log`; the exit code, None on a timeout."""
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [nv.exe, "--headless", "-c", "doautocmd UIEnter", "-c", f"luafile {SMOKE}"]
    ui.detail(f"$ {proc.show(argv)}   (in {proj})")
    with log.open("w", encoding="utf-8", errors="replace") as out, err_log.open("w", encoding="utf-8", errors="replace") as err:
        p = subprocess.Popen(argv, cwd=proj, env=dict(env), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=not IS_WINDOWS)
        return _wait(p, timeout)


def smoke_problem(s: Smoke) -> str:
    """What makes a smoke run incomplete although nothing FAILed ('' when nothing does)."""
    if s.expected is None:
        return "smoke.lua did not reach its end (no DONE line)"
    if s.expected != s.total:
        leaked = "; ".join(s.other[:5])
        return f"smoke.lua ran {s.expected} checks but {s.total} result lines were parsed" + (f" (other output: {leaked})" if leaked else "")
    names = [*s.passed, *(n for n, _ in s.failed), *s.skipped]
    mypy = [n for n in names if n.startswith("mypy diagnostics")]
    if not mypy or mypy[0].endswith("(profile off)"):
        return f"the mypy diagnostics check did not run with a typing profile (./pyt mode --typing {SMOKE_TYPING})"
    return ""


def run_preset(
    preset: str,
    layout: Layout,
    nv: cmd_nvim.Nvim,
    *,
    renv: Mapping[str, str],
    venv: Mapping[str, str],
    timeout: float,
) -> Row:
    row = Row(preset)
    proj = layout.projects / preset
    logs = layout.logs / preset
    uv = proc.find_uv()

    def timed(name: str, func: Callable[[], None]) -> None:
        start = time.perf_counter()
        try:
            func()
        finally:
            row.times[name] = time.perf_counter() - start

    def pyt(script_root: Path, cwd: Path, *args: str | Path) -> None:
        ui.command("./pyt " + proc.show([str(a) for a in args]))
        _step(
            proc.runner_argv(uv, script_root, args),
            cwd=cwd, env=renv, log=logs / f"{args[0]}.log", timeout=STEP_TIMEOUT, what=f"./pyt {args[0]} ({preset})",
        )

    def new() -> None:
        _remove(proj)
        proj.parent.mkdir(parents=True, exist_ok=True)
        # from THIS template; not named after the preset: a project "flet" cannot depend on flet
        pyt(ROOT, layout.base, "new", proj, "--preset", preset, "--name", f"pt-{preset}")
        pyt(proj, proj, "sync", "cpython")  # .venv: ruff, mypy, debugpy for the editor
        # every preset ships typing.relaxed = off: the mypy linter would never run
        pyt(proj, proj, "mode", "--typing", SMOKE_TYPING)

    def lazy() -> None:
        lazy_lua = proj / ".lazy.lua"
        ui.command(f"trust {lazy_lua}   (vim.secure.trust in the isolated state dir)")
        cmd_nvim.trust_file(nv.exe, lazy_lua, env=venv, cwd=proj)
        state = cmd_nvim.trust_status(nv.trust_db, lazy_lua)
        if state.state != "trusted":
            raise PytError(f"{lazy_lua} is {state.state} after vim.secure.trust")
        # install, not sync: only what .lazy.lua adds (never an update), at the locked commits
        # (one round: LazyVim is installed, so the startup install knows every plugin at once)
        lock = LOCK if LOCK.is_file() else None
        use_lock(nv, lock)
        ui.command('nvim --headless "+Lazy! install" +qa')
        install_log = logs / "lazy-install.log"
        _step(
            [nv.exe, "--headless", "+Lazy! install", "+qa"],
            cwd=proj, env=venv, log=install_log, timeout=STEP_TIMEOUT, what=f"Lazy! install ({preset})",
        )
        _check_pins(lock, nv, f"Lazy! install ({preset})", install_log)

    def smoke() -> None:
        log = logs / "smoke.log"
        err_log = logs / "smoke-stderr.log"
        row.log = log
        ui.command(f'nvim --headless -c "doautocmd UIEnter" -c "luafile {SMOKE}"')
        row.code = run_smoke(nv, proj, {**venv, "PT_ROOT": str(proj)}, log, err_log, timeout)
        row.smoke = parse_smoke(log.read_text(encoding="utf-8", errors="replace"))
        if row.code is None:
            row.error = f"smoke.lua timed out after {timeout:.0f} s"
        elif row.smoke.total == 0:
            tail = _tail(err_log)
            row.error = "smoke.lua printed no ok/FAIL/SKIP line" + (f"\n{tail}" if tail else "")
        else:
            row.error = smoke_problem(row.smoke)

    ui.step(f"preset {preset}")
    try:
        for name, func in zip(PHASES, (new, lazy, smoke), strict=True):
            timed(name, func)
    except PytError as e:
        row.error = str(e)
    return row


def _table(rows: Sequence[Row], base_seconds: float | None) -> None:
    """The results, with the reason of each FAIL and the SKIP lines: `ui.report`, as `selftest
    --shells` prints its own (`-q` hides progress, never what was asked for; it left a bare
    `script: FAIL <check>` with no reason and no table)."""
    ui.step("selftest --nvim results")
    base = "reused (cached)" if base_seconds is None else f"installed in {base_seconds:.0f} s"
    ui.report(f"  isolated LazyVim: {base}")
    ui.report(f"  {'preset':<8} {'result':<6} {'ok':>3} {'fail':>4} {'skip':>4} {'exit':>4} {'new+sync':>9} {'trust+lazy':>10} {'smoke':>6} {'total':>6}")
    for r in rows:
        s = r.smoke or Smoke()
        exit_text = "-" if r.code is None else str(r.code)
        new, lazy, smoke = (f"{r.times[k]:.0f}s" if k in r.times else "-" for k in PHASES)
        ui.report(
            f"  {r.preset:<8} {'PASS' if r.ok else 'FAIL':<6} {len(s.passed):>3} {len(s.failed):>4} {len(s.skipped):>4} {exit_text:>4} "
            f"{new:>9} {lazy:>10} {smoke:>6} {sum(r.times.values()):>5.0f}s"
        )
    for r in rows:
        s = r.smoke or Smoke()
        for name, detail in s.failed:
            ui.error(f"{r.preset}: FAIL {name}")
            for line in detail.splitlines():
                ui.report(f"    {line}")
        for name in s.skipped:
            ui.report(f"  {r.preset}: SKIP {name}")
        if r.error:
            ui.error(f"{r.preset}: {r.error}")
        elif r.code not in (0, None) and not s.failed:
            ui.error(f"{r.preset}: Neovim exited with {r.code}" + (f" (output: {r.log})" if r.log else ""))


def _parse_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="./pyt selftest --nvim")
    parser.add_argument("presets", nargs="?", default=",".join(DEFAULT_PRESETS), help="comma-separated presets (default: script,raylib,flet)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch projects")
    parser.add_argument("--fresh", action="store_true", help="reinstall the isolated LazyVim (clone the starter, install and restore the pinned plugins)")
    parser.add_argument("--require", action="store_true", help="fail instead of skipping when nvim or git is missing (CI)")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds for each smoke.lua run (default 600)")
    parser.add_argument("--dir", help=f"work directory (default {default_dir()})")
    return parser.parse_args(args)


def selftest(cfg: Config, args: list[str]) -> int:
    """selftest --nvim [PRESET,...] [--keep] [--fresh] [--require] [--timeout S] [--dir DIR]"""
    ns = _parse_args(args)
    names = [p.strip() for p in ns.presets.split(",") if p.strip()]
    unknown = [p for p in names if p not in presets.available()]
    if unknown or not names:
        raise PytError(f"selftest --nvim: unknown preset(s) {', '.join(unknown) or '(none)'} (available: {', '.join(presets.available())})")

    missing = [t for t in ("nvim", "git") if not cmd_nvim.which(t)]
    if missing:
        msg = f"selftest --nvim: {' and '.join(missing)} not found in PATH"
        if ns.require:
            raise PytError(msg + " (--require)", 3)
        ui.warn(msg + ": skipped")
        return 0
    exe = cmd_nvim.which("nvim") or "nvim"
    layout = Layout(user_path(ns.dir) if ns.dir else default_dir())
    from .e2e import termination_as_interrupt

    # Every step runs in a session of its own (kill_tree), so a SIGTERM or SIGHUP sent to this
    # run's process group (`timeout`, a closed terminal) never reaches it: the runner died at once
    # and the step's Neovim, git and uv went on as orphans, writing into --dir. They now stop the
    # run like Ctrl+C, and _wait kills the running step's tree first.
    with termination_as_interrupt():
        try:
            return _run(ns, names, exe, layout)
        except KeyboardInterrupt:
            if layout.logs.is_dir():
                ui.report(f"  logs of the interrupted run: {layout.logs}")
            raise


def _run(ns: argparse.Namespace, names: list[str], exe: str, layout: Layout) -> int:
    from .e2e import hidden_template_repository, isolate_git

    source = proc.base_env()  # the user's env, before the git isolation below; for the git config
    renv = runner_env(source)
    # Neovim keeps the user's git configuration: lazy.nvim clones the plugins with it (a proxy,
    # url.*.insteadOf). nvim_env moves XDG_CONFIG_HOME, so point git at a config that includes the
    # user's two global files, else the $XDG_CONFIG_HOME/git/config one would be lost (below).
    venv = nvim_env(layout, renv, uv_dirs(renv))
    # The ./pyt steps work in --dir only, as those of selftest --e2e: git there never sees a
    # repository around --dir (`new` skipped git init there and, with core.filemode = false, staged
    # its launchers in the user's repository, which kept them once the scratch project was gone),
    # nor the user's global or system git configuration.
    isolate_git(renv, layout.base)
    hidden = hidden_template_repository(renv)
    if hidden:
        raise PytError(
            f"selftest --nvim: --dir {layout.base} is next to this template inside its git repository {hidden}: "
            "git in --dir must not see a repository around it (./pyt new would skip git init), which would "
            "hide the template's own. Pick a --dir outside that repository"
        )
    _prepare_dir(layout)
    if "GIT_CONFIG_GLOBAL" not in venv:  # let the user's own GIT_CONFIG_GLOBAL win
        global_config = user_git_config(layout.base, source)
        if global_config is not None:
            venv["GIT_CONFIG_GLOBAL"] = global_config
    # One run at a time per --dir: a second run deletes this one's logs (below), removes x/ under
    # it while this one installs the base, and its project cleanup collides with this one's build.
    # _prepare_dir above created the dir, so a second run is refused here.
    with base_lock(layout.base, "selftest --nvim"):
        _remove(layout.logs)  # logs of the previous run
        layout.logs.mkdir(parents=True)
        version = cmd_nvim.query(exe, env=venv)
        if version is not None and version.version < cmd_nvim.MIN_LAZYVIM:
            msg = f"selftest --nvim: Neovim {version.version_text} is older than LazyVim's minimum {cmd_nvim.version_str(cmd_nvim.MIN_LAZYVIM)}"
            if ns.require:
                raise PytError(msg, 3)
            ui.warn(msg + ": skipped")
            return 0
        nv, base_seconds = prepare_base(layout, exe, venv, fresh=ns.fresh)

        rows = [run_preset(p, layout, nv, renv=renv, venv=venv, timeout=ns.timeout) for p in names]
        if not ns.keep:
            for p in names:
                try:
                    cmd_nvim.remove_tree(layout.projects / p)
                except OSError as e:
                    ui.warn(f"could not remove {layout.projects / p}: {e}")
        # the starter and plugin commits this run used (the uploaded CI logs; new pins after a green
        # run without LOCK)
        pins = record_pins(layout, nv)
        _table(rows, base_seconds)
        ui.report(f"  pinned to: {pins}")
        ui.report(f"  logs: {layout.logs}" + (f"   projects: {layout.projects}" if ns.keep else "   (--keep keeps the projects)"))
        if all(r.ok for r in rows):
            ui.ok(f"selftest --nvim: {len(rows)} preset(s) passed")
            return 0
        ui.error(f"selftest --nvim: {sum(not r.ok for r in rows)} of {len(rows)} preset(s) failed")
        return 1
