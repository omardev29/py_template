"""selftest --nvim: headless smoke test of the LazyVim integration in an isolated LazyVim.

Installs the LazyVim starter under a throwaway XDG_* tree (never the user's config), trusts
each scratch project's .lazy.lua through Neovim's API, syncs the plugins and runs
.pytemplate/nvim/tests/smoke.lua in a project made from each preset.

Layout of the work directory (short on purpose: Windows MAX_PATH, deep plugin trees):

    <dir>/x/{config,data,state,cache}   XDG_*_HOME of every Neovim call (LazyVim + plugins)
    <dir>/base.json                     marker: the base above is complete and reusable
    <dir>/p/<preset>                    scratch projects (./deploy new), removed unless --keep
    <dir>/logs/                         output of every step of the last run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import cmd_nvim, presets, proc, ui
from .config import Config
from .project import IS_WINDOWS, ROOT, user_path
from .ui import DeployError

DEFAULT_PRESETS = ("script", "raylib", "flet")
SMOKE = ".pytemplate/nvim/tests/smoke.lua"  # relative to the project (the cwd of the smoke run)
DIR_MARKER = ".pytemplate-nvim-test"  # only directories with this file are ever deleted from
CLONE_TIMEOUT = 300.0
BASE_SYNC_TIMEOUT = 1800.0
STEP_TIMEOUT = 900.0
PHASES = ("new+sync", "trust+lazy", "smoke")

# The inner ./deploy runs as if typed in a fresh shell: nothing from this runner's own
# `uv run --script` environment, nor from a shell's stale PYTEMPLATE_* exports.
RUNNER_DROP = frozenset({"VIRTUAL_ENV", "UV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON"})
# Anything that could make Neovim read the user's own config, data or server.
NVIM_DROP = frozenset({"NVIM", "NVIM_APPNAME", "NVIM_LISTEN_ADDRESS", "NVIM_LOG_FILE", "VIMINIT", "EXINIT", "MYVIMRC", "MYGVIMRC"})
XDG_HOMES = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")


def default_dir() -> Path:
    """%TEMP%\\pt\\nvim on Windows (short), $TMPDIR/pt-nvim elsewhere."""
    tmp = Path(tempfile.gettempdir())
    return tmp / "pt" / "nvim" if IS_WINDOWS else tmp / "pt-nvim"


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
    """Environment of the inner ./deploy calls: `source` minus VIRTUAL_ENV, UV*, PYTEMPLATE_*."""
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


# --- smoke output ------------------------------------------------------------------------------------


_RESULT = re.compile(r"^(ok|FAIL|SKIP)\s+(\S.*?)\s*$")


@dataclass
class Smoke:
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (name, detail lines)
    skipped: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed) + len(self.skipped)


def parse_smoke(text: str) -> Smoke:
    """Parse smoke.lua's `ok   NAME` / `FAIL NAME` / `SKIP NAME` lines.

    Lines after a FAIL that are not results (its traceback) become that failure's detail.
    """
    out = Smoke()
    detail: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
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
            p = subprocess.Popen(args, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            raise DeployError(f"program not found: {args[0]}", 3) from None
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            return None


def _remove(path: Path) -> None:
    try:
        cmd_nvim.remove_tree(path)
    except OSError as e:
        raise DeployError(f"cannot remove {path}: {e}\n  Is a Neovim (or git/tar/curl) process still using it?") from None


def _tail(log: Path, lines: int = 15) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip().splitlines()[-lines:])


def _step(argv: Sequence[str | Path], *, cwd: Path, env: Mapping[str, str], log: Path, timeout: float, what: str) -> None:
    code = _run_logged(argv, cwd=cwd, env=env, log=log, timeout=timeout)
    if code != 0:
        why = f"timed out after {timeout:.0f} s" if code is None else f"exit code {code}"
        tail = _tail(log)
        raise DeployError(f"{what}: {why} (log: {log})" + (f"\n{tail}" if tail else ""))


def _check_isolated(nv: cmd_nvim.Nvim, layout: Layout) -> None:
    """Refuse to go on unless Neovim really uses the throwaway tree (never the user's config)."""
    root = os.path.normcase(str(layout.xdg.resolve()))
    for path in (nv.config, nv.data, nv.state, nv.cache):
        if not os.path.normcase(str(path.resolve())).startswith(root + os.sep):
            raise DeployError(f"isolation check failed: Neovim reports {path}, outside {layout.xdg}", 3)


def _prepare_dir(layout: Layout) -> None:
    base = layout.base
    resolved = base.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise DeployError(f"--dir must be outside the template ({base}): Neovim would find its .lazy.lua")
    if base.exists() and any(base.iterdir()) and not (base / DIR_MARKER).is_file():
        raise DeployError(f"{base} is not empty and was not created by selftest --nvim: pick another --dir")
    base.mkdir(parents=True, exist_ok=True)
    (base / DIR_MARKER).write_text("work directory of ./deploy selftest --nvim (safe to delete)\n", encoding="utf-8", newline="\n")


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
    if layout.marker.is_file() and nv.lazyvim_installed():
        ui.info(f"reusing the isolated LazyVim in {layout.xdg}   (--fresh reinstalls it)")
        return nv, None
    ui.step(f"isolated LazyVim in {layout.xdg} (first run: this takes a few minutes)")
    start = time.perf_counter()
    _remove(layout.xdg)
    for key in XDG_HOMES:
        layout.home(key).mkdir(parents=True, exist_ok=True)
    git = cmd_nvim.which("git") or "git"
    ui.command(f"git clone --depth 1 {cmd_nvim.STARTER} {nv.config}")
    _step(
        [git, "clone", "--depth", "1", cmd_nvim.STARTER, nv.config],
        cwd=layout.base, env=env, log=layout.logs / "clone.log", timeout=CLONE_TIMEOUT, what="clone the LazyVim starter",
    )
    _remove(nv.config / ".git")
    # From the work dir: there is no .lazy.lua above it, so only LazyVim's own plugins
    ui.command(f'nvim --headless "+Lazy! sync" +qa   (in {layout.base})')
    sync_log = layout.logs / "base-sync.log"
    _step(
        [nv.exe, "--headless", "+Lazy! sync", "+qa"],
        cwd=layout.base, env=env, log=sync_log, timeout=BASE_SYNC_TIMEOUT, what="Lazy! sync",
    )
    if not (nv.data / "lazy" / "LazyVim").is_dir():
        raise DeployError(f"LazyVim was not installed in {nv.data} (log: {sync_log})\n{_tail(sync_log)}")
    seconds = time.perf_counter() - start
    info = {"starter": cmd_nvim.STARTER, "nvim": nv.version_text, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(seconds)}
    layout.marker.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    ui.ok(f"isolated LazyVim ready in {seconds:.0f} s")
    return nv, seconds


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
        return not self.error and self.code == 0 and self.smoke is not None and not self.smoke.failed and self.smoke.total > 0


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

    def deploy(script_root: Path, cwd: Path, *args: str | Path) -> None:
        ui.command("./deploy " + proc.show([str(a) for a in args]))
        _step(
            [uv, "run", "--quiet", "--script", script_root / ".pytemplate" / "deploy.py", *args],
            cwd=cwd, env=renv, log=logs / f"{args[0]}.log", timeout=STEP_TIMEOUT, what=f"./deploy {args[0]} ({preset})",
        )

    def new() -> None:
        _remove(proj)
        proj.parent.mkdir(parents=True, exist_ok=True)
        # from THIS template; not named after the preset: a project "flet" cannot depend on flet
        deploy(ROOT, layout.base, "new", proj, "--preset", preset, "--name", f"pt-{preset}")
        deploy(proj, proj, "sync", "cpython")  # .venv: ruff, mypy, debugpy for the editor

    def lazy() -> None:
        lazy_lua = proj / ".lazy.lua"
        ui.command(f"trust {lazy_lua}   (vim.secure.trust in the isolated state dir)")
        cmd_nvim.trust_file(nv.exe, lazy_lua, env=venv, cwd=proj)
        state = cmd_nvim.trust_status(nv.trust_db, lazy_lua)
        if state.state != "trusted":
            raise DeployError(f"{lazy_lua} is {state.state} after vim.secure.trust")
        # install, not sync: only what .lazy.lua adds (the base was synced once; no update churn)
        ui.command('nvim --headless "+Lazy! install" +qa')
        _step(
            [nv.exe, "--headless", "+Lazy! install", "+qa"],
            cwd=proj, env=venv, log=logs / "lazy-install.log", timeout=STEP_TIMEOUT, what=f"Lazy! install ({preset})",
        )

    def smoke() -> None:
        log = logs / "smoke.log"
        row.log = log
        ui.command(f'nvim --headless -c "doautocmd UIEnter" -c "luafile {SMOKE}"')
        env = {**venv, "PT_ROOT": str(proj)}
        # stdout alone in the log file: smoke.lua writes its result lines there
        err_log = logs / "smoke-stderr.log"
        logs.mkdir(parents=True, exist_ok=True)
        with log.open("w", encoding="utf-8", errors="replace") as out, err_log.open("w", encoding="utf-8", errors="replace") as err:
            argv = [nv.exe, "--headless", "-c", "doautocmd UIEnter", "-c", f"luafile {SMOKE}"]
            ui.detail(f"$ {proc.show(argv)}   (in {proj})")
            p = subprocess.Popen(argv, cwd=proj, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            try:
                row.code = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
                row.code = None
        row.smoke = parse_smoke(log.read_text(encoding="utf-8", errors="replace"))
        if row.code is None:
            row.error = f"smoke.lua timed out after {timeout:.0f} s"
        elif row.smoke.total == 0:
            tail = _tail(err_log)
            row.error = "smoke.lua printed no ok/FAIL/SKIP line" + (f"\n{tail}" if tail else "")

    ui.step(f"preset {preset}")
    try:
        for name, func in zip(PHASES, (new, lazy, smoke), strict=True):
            timed(name, func)
    except DeployError as e:
        row.error = str(e)
    return row


def _table(rows: Sequence[Row], base_seconds: float | None) -> None:
    ui.step("selftest --nvim results")
    base = "reused (cached)" if base_seconds is None else f"installed in {base_seconds:.0f} s (first run)"
    ui.info(f"  isolated LazyVim: {base}")
    ui.info(f"  {'preset':<8} {'result':<6} {'ok':>3} {'fail':>4} {'skip':>4} {'exit':>4} {'new+sync':>9} {'trust+lazy':>10} {'smoke':>6} {'total':>6}")
    for r in rows:
        s = r.smoke or Smoke()
        exit_text = "-" if r.code is None else str(r.code)
        new, lazy, smoke = (f"{r.times[k]:.0f}s" if k in r.times else "-" for k in PHASES)
        ui.info(
            f"  {r.preset:<8} {'PASS' if r.ok else 'FAIL':<6} {len(s.passed):>3} {len(s.failed):>4} {len(s.skipped):>4} {exit_text:>4} "
            f"{new:>9} {lazy:>10} {smoke:>6} {sum(r.times.values()):>5.0f}s"
        )
    for r in rows:
        s = r.smoke or Smoke()
        for name, detail in s.failed:
            ui.error(f"{r.preset}: FAIL {name}")
            for line in detail.splitlines():
                ui.info(f"    {line}")
        for name in s.skipped:
            ui.info(f"  {r.preset}: SKIP {name}")
        if r.error:
            ui.error(f"{r.preset}: {r.error}")
        elif r.code not in (0, None) and not s.failed:
            ui.error(f"{r.preset}: Neovim exited with {r.code}" + (f" (output: {r.log})" if r.log else ""))


def _parse_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="./deploy selftest --nvim")
    parser.add_argument("presets", nargs="?", default=",".join(DEFAULT_PRESETS), help="comma-separated presets (default: script,raylib,flet)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch projects")
    parser.add_argument("--fresh", action="store_true", help="reinstall the isolated LazyVim (clone + Lazy! sync)")
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
        raise DeployError(f"selftest --nvim: unknown preset(s) {', '.join(unknown) or '(none)'} (available: {', '.join(presets.available())})")

    missing = [t for t in ("nvim", "git") if not cmd_nvim.which(t)]
    if missing:
        msg = f"selftest --nvim: {' and '.join(missing)} not found in PATH"
        if ns.require:
            raise DeployError(msg + " (--require)", 3)
        ui.warn(msg + ": skipped")
        return 0
    exe = cmd_nvim.which("nvim") or "nvim"

    layout = Layout(user_path(ns.dir) if ns.dir else default_dir())
    _prepare_dir(layout)
    _remove(layout.logs)  # logs of the previous run
    layout.logs.mkdir(parents=True)
    renv = runner_env(proc.base_env())
    venv = nvim_env(layout, renv, uv_dirs(renv))
    version = cmd_nvim.query(exe, env=venv)
    if version is not None and version.version < cmd_nvim.MIN_LAZYVIM:
        msg = f"selftest --nvim: Neovim {version.version_text} is older than LazyVim's minimum {cmd_nvim.version_str(cmd_nvim.MIN_LAZYVIM)}"
        if ns.require:
            raise DeployError(msg, 3)
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
    _table(rows, base_seconds)
    ui.info(f"  logs: {layout.logs}" + (f"   projects: {layout.projects}" if ns.keep else "   (--keep keeps the projects)"))
    if all(r.ok for r in rows):
        ui.ok(f"selftest --nvim: {len(rows)} preset(s) passed")
        return 0
    ui.error(f"selftest --nvim: {sum(not r.ok for r in rows)} of {len(rows)} preset(s) failed")
    return 1
