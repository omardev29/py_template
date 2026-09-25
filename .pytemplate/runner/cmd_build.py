"""build [BACKEND] [--method M]: prepare the backend's payload and package it."""

from __future__ import annotations

import argparse
import importlib
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import mypyc, proc, ui
from .cmd_dev import run_checks, split_backend
from .config import BACKENDS, METHODS, Config
from .project import BUILD, DIST, SRC, rel, user_path
from .ui import DeployError

# Which backends each method supports (and why not the rest)
COMPAT: dict[str, dict[str, str]] = {
    "exe": {"pypy": "PyInstaller does not support PyPy. Use --method portable (a folder with PyPy inside)."},
    "nuitka": {"pypy": "Nuitka only compiles for CPython. Use --method portable."},
    "flet": {"pypy": "flet build embeds CPython. Use --method portable."},
    "portable": {},
    "pyz": {},
    "wheel": {},
}


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


def payload(cfg: Config, backend: str) -> Path:
    """Return the app code ready to package: src/ as-is, or the mypyc release stage."""
    if backend == "mypyc":
        return mypyc.build(cfg, "release")
    dest = BUILD / "payload" / backend
    mypyc.sync_tree(SRC, dest)
    return dest


def cmd_build(cfg: Config, args: list[str]) -> int:
    backend, rest = split_backend(cfg, args)
    parser = argparse.ArgumentParser(prog="./deploy build")
    parser.add_argument("--method", choices=METHODS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--onefile", action="store_true")
    mode.add_argument("--onedir", action="store_true")
    parser.add_argument("--target", action="append", default=[], help="extra platforms (pyz/portable), e.g. cp314-linux-x86_64")
    parser.add_argument("--no-check", action="store_true", help="do not run ./deploy check first")
    ns, extra = parser.parse_known_args(rest)
    if backend not in BACKENDS:
        raise DeployError(f"unknown backend: {backend}")
    from . import envs

    envs.ensure_supported(cfg, backend)
    method = ns.method or cfg.deploy.default.get(backend, "exe")
    reason = COMPAT[method].get(backend)
    if reason:
        raise DeployError(f"{method} + {backend}: {reason}")

    if not ns.no_check and not run_checks(cfg, backend):
        raise DeployError("check failed: fix it or use --no-check")
    if proc.DRY_RUN:
        ui.info(f"(--dry-run) build {backend} -> {method}: would output {rel(DIST)}/{cfg.app.name}-{backend}-{method}*")
        return 0

    app_dir = payload(cfg, backend)
    onefile = True if ns.onefile else False if ns.onedir else None
    req = BuildRequest(cfg, backend, method, app_dir, onefile, ns.target, extra)
    ui.step(f"build {backend} -> {method}")
    module = importlib.import_module(f"{__package__}.methods.{method}")
    result: Path = module.build(req)
    ui.ok(f"done: {rel(result)}  ({_size(result)})")
    return 0


def _size(path: Path) -> str:
    total = path.stat().st_size if path.is_file() else sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return f"{total / 1_048_576:.1f} MB"


def fresh_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def dist_path(req: BuildRequest, suffix: str = "") -> Path:
    DIST.mkdir(exist_ok=True)
    return DIST / (req.out_name + suffix)


def cmd_pyz_merge(cfg: Config, args: list[str]) -> int:
    """pyz-merge A.pyz B.pyz ... --out C.pyz: merge the per-OS .pyz files into one.

    The paths are the user's (relative to the folder ./deploy was typed in, /c/x, ~ ...).
    """
    parser = argparse.ArgumentParser(prog="./deploy pyz-merge")
    parser.add_argument("parts", nargs="+", metavar="PART.pyz")
    parser.add_argument("--out", required=True, metavar="OUT.pyz")
    ns = parser.parse_args(args)
    parts = [user_path(p) for p in ns.parts]
    out = user_path(ns.out)
    if len(parts) < 2:
        raise DeployError("pyz-merge needs at least two .pyz files")
    for part in parts:
        if not part.is_file():
            raise DeployError(f"pyz-merge: {part} not found")
        if not zipfile.is_zipfile(part):
            raise DeployError(f"pyz-merge: {part} is not a .pyz (zip) file")
    if out.is_dir():
        raise DeployError(f"pyz-merge: --out {out} is a folder; give the path of the .pyz to write")
    if proc.DRY_RUN:
        ui.step("pyz-merge: dry run, nothing is written")
        for part in parts:
            ui.info(f"  in   {part}")
        ui.info(f"  out  {out}" + ("   (exists: would be replaced)" if out.exists() else ""))
        return 0
    from .methods import pyz

    pyz.merge(parts, out)
    return 0
