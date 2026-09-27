"""build [BACKEND] [--method M]: prepare the backend's payload and package it."""

from __future__ import annotations

import argparse
import difflib
import importlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import mypyc, proc, ui
from .cmd_dev import run_checks, split_backend
from .config import BACKENDS, METHODS, Config
from .project import BUILD, DIST, SRC, rel, user_path
from .ui import PytError

# Which backends each method supports (and why not the rest)
COMPAT: dict[str, dict[str, str]] = {
    "exe": {"pypy": "PyInstaller does not support PyPy. Use --method portable (a folder with PyPy inside)."},
    "nuitka": {"pypy": "Nuitka only compiles for CPython. Use --method portable."},
    "flet": {"pypy": "flet build embeds CPython. Use --method portable."},
    "portable": {},
    "pyz": {},
    "wheel": {},
}
# What a method takes from the command line besides --method and --no-check. Anything else
# would be silently dropped, so it is refused with exit 2 (a typo is never ignored). Only these
# methods hand the unknown flags to their packager (PyInstaller / flet pack, Nuitka, flet build).
PASSTHROUGH = ("exe", "nuitka", "flet")
ONEFILE_METHODS = ("exe", "nuitka")  # --onefile / --onedir
TARGET_METHODS = ("pyz",)  # --target: the other methods build for this OS only
GLOBAL_FLAGS = ("--dry-run", "--no-render")  # ./pyt's own options: they go before the command
OWN_PAYLOAD = ("wheel",)  # builds its own project from src/: no payload (no mypyc release stage)


@dataclass
class BuildRequest:
    cfg: Config
    backend: str
    method: str
    app_dir: Path  # folder with main.py, the package (plus the .pyd files with mypyc) and assets/
    onefile: bool | None = None
    targets: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)

    @property
    def out_name(self) -> str:
        return f"{self.cfg.app.name}-{self.backend}-{self.method}"

    @property
    def compiled(self) -> bool:
        return self.backend == "mypyc"


def check_lock(cfg: Config) -> None:
    """Refuse a uv.lock that pyproject.toml moved past (read-only), before any work.

    Every method stops on it (`uv run --locked`, `uv sync --locked`, `uv export --locked`), but
    only where it gets there: with --no-check after the payload and the mypyc compile, and exe
    and nuitka after the previous output was removed; a --dry-run said "would output".
    """
    from . import envs

    r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], check=False, capture=True, echo=False)
    if r.returncode != 0:
        why = envs.uv_error(r.stderr or r.stdout) or f"uv lock --check: exit code {r.returncode}"
        why = why[len("error:") :].strip() if why.lower().startswith("error:") else why
        if "needs to be updated" not in why:  # no answer (offline, no interpreter): not a stale lock
            raise PytError(f"cannot check uv.lock against pyproject.toml: {why}", 3)
        raise PytError(
            f"uv.lock does not match pyproject.toml ({why})\n"
            "  Run ./pyt lock (./pyt apply after a pytemplate.toml edit), then build again"
        )


def payload(cfg: Config, backend: str) -> Path:
    """Return the app code ready to package: src/ as-is, or the mypyc release stage."""
    if backend == "mypyc":
        return mypyc.build(cfg, "release")
    dest = BUILD / "payload" / backend
    mypyc.sync_tree(SRC, dest)
    return dest


def _stray_word(word: str, args: list[str]) -> str:
    """The error for a bare word where build takes none: a typo'd backend, a method without --method."""
    if word in METHODS:
        return f"build: unexpected argument '{word}': did you mean --method {word}?"
    if args and args[0] == word:
        close = difflib.get_close_matches(word, BACKENDS, n=1)
        hint = f": did you mean {close[0]}?" if close else ""
        return f"build: unknown backend '{word}'{hint} (backends: {' | '.join(BACKENDS)}; the method goes after --method)"
    return f"build: unexpected argument '{word}' (usage: ./pyt build [BACKEND] [--method METHOD] [options])"


def _check_arguments(method: str, ns: argparse.Namespace, extra: list[str]) -> None:
    """Refuse what the chosen method would silently ignore (before the checks and the payload)."""
    for flag in GLOBAL_FLAGS:
        if flag in extra:
            raise PytError(f"build: {flag} is a global option: put it before the command (./pyt {flag} build ...)", 2)
    if extra and method not in PASSTHROUGH:
        raise PytError(
            f"build --method {method}: unrecognized arguments: {' '.join(extra)}  "
            f"(only {', '.join(PASSTHROUGH)} pass extra arguments to their packager)",
            2,
        )
    if (ns.onefile or ns.onedir) and method not in ONEFILE_METHODS:
        flag = "--onefile" if ns.onefile else "--onedir"
        raise PytError(f"build --method {method}: {flag} only applies to --method {' or '.join(ONEFILE_METHODS)}", 2)
    if ns.target and method not in TARGET_METHODS:
        raise PytError(f"build --method {method}: --target only applies to --method pyz ({method} builds for this OS only)", 2)


def cmd_build(cfg: Config, args: list[str]) -> int:
    backend, rest = split_backend(cfg, args)
    parser = argparse.ArgumentParser(prog="./pyt build", allow_abbrev=False)  # --onedri is not --onedir
    parser.add_argument("--method", choices=METHODS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--onefile", action="store_true", help="exe and nuitka only")
    mode.add_argument("--onedir", action="store_true", help="exe and nuitka only")
    parser.add_argument("--target", action="append", default=[], help="extra platforms (pyz only), e.g. cp314-linux-x86_64")
    parser.add_argument("--no-check", action="store_true", help="do not run ./pyt check first")
    ns, extra = parser.parse_known_args(rest)
    if extra and not extra[0].startswith("-"):
        # The first leftover can never be a known option's value (argparse consumed those)
        raise PytError(_stray_word(extra[0], args), 2)
    from . import envs

    envs.ensure_supported(cfg, backend)
    method = ns.method or cfg.deploy.default.get(backend, "exe")
    reason = COMPAT[method].get(backend)
    if reason:
        raise PytError(f"{method} + {backend}: {reason}")
    _check_arguments(method, ns, extra)
    if method == "nuitka":
        from .methods import nuitka

        nuitka.check_python(cfg, [*cfg.deploy.nuitka.extra_args, *extra])  # before minutes of checks and compiling
        nuitka.check_options(cfg, backend)
    if method == "pyz":
        from .methods import common

        for key in [*cfg.deploy.pyz.targets, *ns.target]:  # a bad key fails now, also in --dry-run
            if key != "host":
                common.check_key(cfg, backend, key)
    if method == "flet":
        from .methods import flet

        flet.check_options(cfg)  # the preset and Windows Developer Mode, before minutes of work
    if method == "portable":
        from .methods import portable

        portable.check(cfg)  # [deploy.portable] env values the .cmd launcher cannot hold
    if method == "wheel":
        from .methods import wheel

        wheel.check(cfg)  # a dependency source a wheel cannot declare (a local library)
    check_lock(cfg)  # a stale uv.lock fails now, also in --dry-run
    from . import upx

    upx_line = upx.preflight(cfg, method)  # a bad deploy.upx.path or a failed download fails now

    if not ns.no_check and not run_checks(cfg, backend):
        raise PytError("check failed: fix it or use --no-check", 1)  # like ./pyt check
    if proc.DRY_RUN:
        ui.info(f"(--dry-run) build {backend} -> {method}: would output {rel(DIST)}/{cfg.app.name}-{backend}-{method}*")
        if upx_line:
            ui.info(f"  {upx_line}")
        if method == "nuitka":
            from .methods import nuitka as nuitka_method  # [deploy.nuitka] lto/pgo, then the extras

            options = [*nuitka_method.optimization_args(cfg), *cfg.deploy.nuitka.extra_args, *extra]
            ui.info(f"  Nuitka options: {proc.show(options)}")
            if cfg.deploy.nuitka.pgo:
                ui.info(nuitka_method.PGO_NOTE)
        if method == "flet":
            from .methods import flet as flet_method  # the target, and what happens to the pins

            for line in flet_method.plan_lines(cfg):
                ui.info(f"  {line}")
        return 0

    # The wheel builds its own project from src/ (its setup.py compiles with mypyc): the mypyc
    # release stage of the payload was compiled for nothing, at twice the build time
    app_dir = SRC if method in OWN_PAYLOAD else payload(cfg, backend)
    onefile = True if ns.onefile else False if ns.onedir else None
    req = BuildRequest(cfg, backend, method, app_dir, onefile, ns.target, extra)
    ui.step(f"build {backend} -> {method}")
    module = importlib.import_module(f"{__package__}.methods.{method}")
    result: Path = module.build(req)
    if not result.exists() or (result.is_dir() and not any(result.iterdir())):
        # e.g. `./pyt build -v`: PyInstaller read -v as --version, printed it and made nothing
        hint = ""
        if any(arg in ("-v", "--verbose", "-q", "--quiet") for arg in extra):
            hint = "; ./pyt's own -v and -q go before the command (./pyt -v build ...): after it they reach the packager"
        raise PytError(f"build {backend} -> {method}: no output at {rel(result)} (see the packager's output above){hint}")
    ui.ok(f"done: {rel(result)}  ({_size(result)})")
    return 0


def _size(path: Path) -> str:
    from .methods.common import tree_bytes  # symlinks (runtime/bin/python3 -> python3.14) count once

    return f"{tree_bytes(path) / 1_048_576:.1f} MB"


def dist_path(req: BuildRequest, suffix: str = "") -> Path:
    DIST.mkdir(exist_ok=True)
    return DIST / (req.out_name + suffix)


def cmd_pyz_merge(cfg: Config, args: list[str]) -> int:
    """pyz-merge A.pyz B.pyz ... --out C.pyz: merge the per-OS .pyz files into one.

    The paths are the user's (relative to the folder ./pyt was typed in, /c/x, ~ ...).
    """
    parser = argparse.ArgumentParser(prog="./pyt pyz-merge")
    parser.add_argument("parts", nargs="+", metavar="PART.pyz")
    parser.add_argument("--out", required=True, metavar="OUT.pyz")
    ns = parser.parse_args(args)
    parts = [user_path(p) for p in ns.parts]
    out = user_path(ns.out)
    if len(parts) < 2:
        raise PytError("pyz-merge needs at least two .pyz files")
    for part in parts:
        if not part.is_file():
            raise PytError(f"pyz-merge: {part} not found")
        if not zipfile.is_zipfile(part):
            raise PytError(f"pyz-merge: {part} is not a .pyz (zip) file")
    if out.is_dir():
        raise PytError(f"pyz-merge: --out {out} is a folder; give the path of the .pyz to write")
    from .methods import pyz

    if proc.DRY_RUN:
        pyz.check_parts(parts, out)  # the real checks: one app, one build, valid _pyz.json
        ui.step("pyz-merge: dry run, nothing is written")
        for part in parts:
            ui.info(f"  in   {part}")
        for path in (out, pyz.wrapper_path(out)):
            ui.info(f"  out  {path}" + ("   (exists: would be replaced)" if path.exists() else ""))
        return 0
    pyz.merge(parts, out, cfg)
    return 0
