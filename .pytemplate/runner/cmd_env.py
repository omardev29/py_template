"""Environment commands: setup, doctor, sync, lock, add, remove, clean."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from . import cmd_nvim, envs, mypyc, proc, render, shells, ui
from .cmd_dev import only_flags
from .config import Config
from .project import BUILD, DIST, IS_WINDOWS, ROOT, rel
from .ui import DeployError


def _envs_for(cfg: Config, target: str) -> list[envs.PyEnv]:
    if target == "all":
        out = [envs.cpython_env(cfg)]
        if cfg.pypy_enabled:
            out.append(envs.pypy_env(cfg))
        if cfg.python.jit:
            out.append(envs.jit_env(cfg))
        return out
    if target in ("cpython", "mypyc"):
        return [envs.runtime_env(cfg, target), envs.tool_env(cfg)] if cfg.python.jit else [envs.cpython_env(cfg)]
    if target == "pypy":
        envs.ensure_supported(cfg, "pypy")
        return [envs.pypy_env(cfg)]
    raise DeployError(f"sync: unknown target '{target}' (cpython | pypy | mypyc | all)")


def ensure_lock(cfg: Config) -> None:
    """Apply the managed parts of pyproject and re-lock if needed."""
    tool = envs.tool_env(cfg)
    if render.write_pyproject(cfg):
        ui.info("pyproject.toml: updated the parts managed by pytemplate")
    r = envs.uv(tool, ["lock", "--check"], check=False, capture=True, echo=False)
    if r.returncode != 0:
        envs.uv(tool, ["lock"])


def cmd_setup(cfg: Config, args: list[str]) -> int:
    """setup: interpreters, uv.lock and the environments of every supported backend."""
    only_flags("setup", args, ())
    ui.step("setup")
    ensure_lock(cfg)
    for env in _envs_for(cfg, "all"):
        ui.step(f"environment {env.key}: {rel(env.dir)} ({env.request})")
        envs.sync(env)
    _fix_exec_bit()
    render.apply(cfg)
    ui.ok("done. Try: ./deploy run  |  ./deploy test  |  ./deploy doctor")
    return 0


def _fix_exec_bit() -> None:
    """Make the POSIX launcher `deploy` executable in git (core.filemode=false on Windows)."""
    if not (ROOT / ".git").exists() or not shutil.which("git"):
        return
    r = proc.run(["git", "ls-files", "-s", "deploy"], capture=True, check=False, echo=False)
    if r.stdout.startswith("100644"):
        proc.run(["git", "update-index", "--chmod=+x", "deploy"], check=False)


def cmd_sync(cfg: Config, args: list[str]) -> int:
    """sync [cpython|pypy|mypyc|all]: `uv sync --locked` of the environment(s)."""
    if len(args) > 1:
        raise DeployError(f"sync: unrecognized arguments: {' '.join(args[1:])}  (one target: cpython | pypy | mypyc | all)")
    target = args[0] if args else "all"
    for env in _envs_for(cfg, target):
        envs.sync(env)
    return 0


def cmd_lock(cfg: Config, args: list[str]) -> int:
    """lock [--upgrade] [--upgrade-package PKG]: apply the managed pyproject parts and `uv lock`."""
    if render.write_pyproject(cfg):
        ui.info("pyproject.toml: updated the parts managed by pytemplate")
    envs.uv(envs.tool_env(cfg), ["lock", *args])
    render.apply(cfg)
    return 0


def _add_remove(cfg: Config, verb: str, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"./deploy {verb}")
    parser.add_argument("packages", nargs="+")
    parser.add_argument("--dev", action="store_true", help="development group")
    parser.add_argument("--group", help="dependency group")
    if verb == "add":
        parser.add_argument(
            "--cpython-only",
            action="store_true",
            help="only on CPython/mypyc (C-API libraries such as numpy: slow or unavailable on PyPy)",
        )
    ns = parser.parse_args(args)
    argv: list[str] = [verb]
    if ns.dev:
        argv.append("--dev")
    if ns.group:
        argv += ["--group", ns.group]
    if verb == "add" and ns.cpython_only:
        argv += ["--marker", "implementation_name == 'cpython'"]
    argv += ns.packages
    envs.uv(envs.tool_env(cfg), argv)
    if verb == "add" and cfg.pypy_enabled and not ns.cpython_only:
        ui.info(
            "PyPy is supported: if the package uses the CPython C-API (numpy, pillow, pydantic-core...) "
            "it will be slow on PyPy; consider `--cpython-only`. Check with: ./deploy sync pypy"
        )
    return 0


def cmd_add(cfg: Config, args: list[str]) -> int:
    """add PKG... [--dev|--group G] [--cpython-only]"""
    return _add_remove(cfg, "add", args)


def cmd_remove(cfg: Config, args: list[str]) -> int:
    """remove PKG... [--dev|--group G]"""
    return _add_remove(cfg, "remove", args)


def cmd_clean(cfg: Config, args: list[str]) -> int:
    """clean [--envs]: remove .build/ and dist/ (and the .venv* environments with --envs)."""
    flags = only_flags("clean", args, ("--envs",))
    targets = [BUILD, DIST]
    if "--envs" in flags:
        targets += sorted(p for p in ROOT.glob(".venv*") if p.is_dir())
    for t in targets:
        if t.exists():
            ui.info(f"removing {rel(t)}")
            if not proc.DRY_RUN:
                shutil.rmtree(t, ignore_errors=True)
    return 0


# --- doctor --------------------------------------------------------------------------------------


def _msvc() -> tuple[bool, str]:
    vs = proc.vs_installer_dir()
    if not vs:
        return False, "no Visual Studio / Build Tools (vswhere.exe not found)"
    r = subprocess.run(
        [str(vs / "vswhere.exe"), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
        capture_output=True,
        text=True,
        check=False,
    )
    path = r.stdout.strip()
    if not path:
        return False, "Visual Studio found, but without the C++ tools (VC.Tools.x86.x64)"
    return True, path


def _c_compiler() -> tuple[bool, str]:
    if IS_WINDOWS:
        return _msvc()
    for cc in (os.environ.get("CC"), "cc", "gcc", "clang"):
        if cc and shutil.which(cc):
            return True, shutil.which(cc) or cc
    return False, "no C compiler"


def cmd_doctor(cfg: Config, args: list[str]) -> int:
    """doctor: check requirements, environments and generated files."""
    only_flags("doctor", args, ())
    problems = 0

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        nonlocal problems
        if passed is False:
            problems += 1
        ui.check_line(passed, label, hint)

    ui.step("tools")
    uv_version = proc.output([proc.find_uv(), "--version"])
    check(True, f"uv: {uv_version}")
    check(True, f"runner: Python {sys.version.split()[0]} ({sys.executable})")

    ui.step(f"backends (active: {cfg.backend.active}; supported: {', '.join(cfg.backend.supported)})")
    cp = envs.cpython_env(cfg)
    if cp.python.is_file():
        info = envs.interpreter_info(cp.python)
        check(True, f"CPython {info['version']} in {rel(cp.dir)}  (JIT available: {'yes' if info['jit'] else 'no'})")
    else:
        check(False, f"environment {rel(cp.dir)} is missing", "./deploy setup")
    if cfg.pypy_enabled:
        pp = envs.pypy_env(cfg)
        if pp.python.is_file():
            info = envs.interpreter_info(pp.python)
            check(info["impl"] == "pypy", f"PyPy ({info['version']}) in {rel(pp.dir)}")
        else:
            check(False, f"environment {rel(pp.dir)} is missing ({cfg.python.pypy})", "./deploy setup   (or ./deploy sync pypy)")
    if cfg.python.jit:
        try:
            jit_python = envs.find_jit_interpreter(cfg)
            check(True, f"JIT: {jit_python}")
            if "scoop" in jit_python.lower() and "\\current\\" in jit_python.lower():
                check(
                    None,
                    "the JIT Python is the scoop `current` link: `scoop update` will switch it to another version",
                    f"Pin it: scoop install versions/python{cfg.python.cpython.replace('.', '')} and put its path in python.jit_interpreter",
                )
        except DeployError as e:
            check(False, "JIT: no CPython with JIT", str(e))
    if cfg.supports("mypyc"):
        found, where = _c_compiler()
        check(found, f"C compiler for mypyc: {where}", mypyc.has_compiler_hint())
    if IS_WINDOWS and cfg.supports("mypyc"):
        long_paths = _long_paths()
        check(
            True if long_paths else None,
            "Windows long paths (LongPathsEnabled)" + ("" if long_paths else ": disabled (optional)"),
            "Avoids MSVC errors when the project is in a very deep path (>260 characters)",
        )

    ui.step("project")
    changed, edited = render.apply(cfg, check=True)
    check(not changed and not edited, "generated files up to date", "They update with any command (or ./deploy render)")
    for path in edited:
        check(False, f"{path} hand-edited", "Edit pytemplate.toml or .pytemplate/templates, or ./deploy render --force")
    check(not render.pyproject_outdated(cfg), "pyproject.toml matches pytemplate.toml", "./deploy lock")
    r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], check=False, capture=True, echo=False)
    check(r.returncode == 0, "uv.lock up to date", "./deploy lock")

    shells.doctor(check)  # launchers and shells
    cmd_nvim.doctor(check)  # Neovim/LazyVim summary (details: ./deploy nvim doctor)
    ui.info("")
    if problems:
        ui.error(f"{problems} problem(s)")
        return 1
    ui.ok("all good")
    return 0


def _long_paths() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return bool(value)
    except OSError:
        return False
