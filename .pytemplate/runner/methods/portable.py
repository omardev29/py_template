"""portable: self-contained folder  runtime/ + lib/ + app/ + boot.py + launchers.

The only standalone option for PyPy (PyInstaller and Nuitka only support CPython).
- runtime = "bundled": copies the backend's interpreter (managed by uv, relocatable) and
  prunes it. Host OS only.
- runtime = "system": no interpreter; the launchers use the Python/PyPy of the target machine.
  With pure dependencies, that folder works on any OS.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import envs, proc, ui
from ..cmd_build import BuildRequest, dist_path
from ..config import Config
from ..project import IS_WINDOWS, TEMPLATES, rel
from ..ui import DeployError
from . import common

STDLIB_PRUNE = {"test", "idlelib", "turtledemo", "ensurepip", "site-packages"}
LONG_PREFIX = "\\\\?\\"


def long_path(path: Path) -> str:
    """Return the path in extended-length form on Windows (\\\\?\\C:\\...): no 260-character limit."""
    resolved = str(path.resolve())
    return LONG_PREFIX + resolved if IS_WINDOWS and not resolved.startswith(LONG_PREFIX) else resolved
ROOT_PRUNE = {"include", "libs", "Tools", "share"}
TK = {"tkinter", "_tkinter", "turtle.py"}


def _stdlib_dirs(base: Path, version: str) -> set[Path]:
    return {base / "Lib", base / "lib" / f"python{version}", base / "lib" / f"pypy{version}"}


def copy_runtime(cfg: Config, backend: str, dest: Path) -> Path:
    """Copy the (pruned) interpreter and return the path of its executable inside `dest`."""
    info = envs.interpreter_info(envs.runtime_env(cfg, backend).python)
    base = Path(str(info["base_prefix"])).resolve()
    version = ".".join(str(info["version"]).split(".")[:2])
    prune = cfg.deploy.portable.prune
    keep_tk = common.uses_tkinter()
    stdlib = _stdlib_dirs(base, version)

    def ignore(directory: str, names: list[str]) -> set[str]:
        d = Path(directory.removeprefix(LONG_PREFIX)).resolve()
        skip = {n for n in names if n == "__pycache__" and d not in stdlib}
        if not prune:
            return skip
        if d == base:
            skip |= {n for n in names if n in ROOT_PRUNE or (not keep_tk and n.lower().startswith("tcl"))}
        if d in stdlib:
            skip |= {n for n in names if n in STDLIB_PRUNE or (not keep_tk and n in TK)}
        if d.name == "hpy" and d.parent in stdlib:
            skip |= {"devel"}  # HPy C headers (PyPy): only needed to compile extensions
        return skip

    ui.info(f"  runtime: {base} -> {rel(dest)}{' (pruned)' if prune else ''}")
    try:
        shutil.copytree(long_path(base), long_path(dest), ignore=ignore, symlinks=True)
    except (shutil.Error, OSError) as e:
        raise DeployError(
            f"could not copy the interpreter to {rel(dest)}: {str(e)[:300]}\n"
            "  On Windows this is usually the 260-character limit: shorten the project path or enable\n"
            "  LongPathsEnabled (./deploy doctor checks it)."
        ) from None
    for marker in dest.rglob("EXTERNALLY-MANAGED"):
        marker.unlink()
    if IS_WINDOWS and info["impl"] == "pypy":
        # The PyPy zip does not ship the VC++ runtime (uv's CPython does)
        cp_base = Path(str(envs.interpreter_info(envs.cpython_env(cfg).python)["base_prefix"])).resolve()
        for dll in ("vcruntime140.dll", "vcruntime140_1.dll"):
            if (cp_base / dll).is_file() and not (dest / dll).exists():
                shutil.copy2(cp_base / dll, dest / dll)
    if IS_WINDOWS:
        return dest / ("pythonw.exe" if cfg.app.gui else "python.exe")
    return dest / "bin" / "python3"


def _env_lines(cfg: Config, windows: bool) -> list[str]:
    env = {"PYTHONUTF8": "1", "PYTHON_JIT": "1" if cfg.python.jit else "0", **cfg.deploy.portable.env}
    if windows:
        return ['set "PYTHONHOME="', 'set "PYTHONPATH="', *(f'set "{k}={v}"' for k, v in env.items())]
    return ["unset PYTHONHOME PYTHONPATH", *(f"export {k}='{v}'" for k, v in env.items())]


def _opt_flag(cfg: Config) -> str:
    return {0: "", 1: " -O", 2: " -OO"}[cfg.deploy.optimize]


def write_launchers(cfg: Config, backend: str, out: Path, python: Path | None) -> list[Path]:
    """Write <name>.cmd (Windows) and/or <name>.sh (POSIX). Never -I/-E: they would disable PYTHON_JIT."""
    name = cfg.app.name
    flags = f"-s{_opt_flag(cfg)}"
    written: list[Path] = []
    if python is None or IS_WINDOWS:
        if python is not None:
            exe = f'"%~dp0{python.relative_to(out).as_posix().replace("/", chr(92))}"'
            start = 'start "" ' if cfg.app.gui else ""
            body = [f'{start}{exe} {flags} "%~dp0boot.py" %*', "exit /b %ERRORLEVEL%"]
        else:
            order = ["pypy3", "pypy"] if backend == "pypy" else [f"py -{cfg.python.cpython}", "python3", "python"]
            body = []
            for i, cmd in enumerate(order):
                prog = cmd.split()[0]
                body.append(f"where {prog} >nul 2>nul && goto run{i}")
            body += [f"echo {name}: needs {'PyPy' if backend == 'pypy' else 'Python ' + cfg.python.cpython} in PATH 1>&2", "exit /b 9009"]
            for i, cmd in enumerate(order):
                body += [f":run{i}", f'{cmd} {flags} "%~dp0boot.py" %*', "exit /b %ERRORLEVEL%"]
        lines = ["@echo off", f"rem Launcher for {name} (portable folder generated by ./deploy)", "setlocal", *_env_lines(cfg, True), *body]
        path = out / f"{name}.cmd"
        path.write_text("\r\n".join(lines) + "\r\n", encoding="ascii", errors="replace", newline="")
        written.append(path)
    if python is None or not IS_WINDOWS:
        if python is not None:
            prog = f'"$HERE/{python.relative_to(out).as_posix()}"'
        else:
            prog = "$(command -v pypy3 || command -v pypy)" if backend == "pypy" else f"$(command -v python{cfg.python.cpython} || command -v python3)"
        lines = [
            "#!/bin/sh",
            f"# Launcher for {name} (portable folder generated by ./deploy)",
            'HERE=$(cd "$(dirname "$0")" && pwd)',
            *_env_lines(cfg, False),
            f'exec {prog} {flags} "$HERE/boot.py" "$@"',
        ]
        path = out / f"{name}.sh"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)
        written.append(path)
    return written


def build(req: BuildRequest) -> Path:
    cfg = req.cfg
    bundled = cfg.deploy.portable.runtime == "bundled"
    host = common.host_target(cfg, req.backend)
    if req.targets and bundled:
        raise DeployError("portable with a bundled runtime is only built for the host OS; use --method pyz for other OSes")
    out = dist_path(req, f"-{host.key}" if bundled else "")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    common.copy_app(req.app_dir, out / "app", extensions=True)
    requirements = common.export_requirements(cfg)
    common.install_deps(cfg, req.backend, host, out / "lib", requirements)
    if not bundled and common.has_native(out / "lib"):
        ui.warn("runtime = \"system\" with native dependencies: it will only work on " + host.key)
    python = copy_runtime(cfg, req.backend, out / "runtime") if bundled else None
    shutil.copy2(TEMPLATES / "portable" / "boot.py", out / "boot.py")
    launchers = write_launchers(cfg, req.backend, out, python)

    if python is not None:
        console_python = python.with_name("python.exe") if IS_WINDOWS else python
        dirs: list[Path] = [out / "lib", out / "app"]
        if (out / "runtime" / "Lib").is_dir() and not any((out / "runtime" / "Lib").rglob("*.pyc")):
            dirs.append(out / "runtime" / "Lib")  # PyPy ships no .pyc: without this it recompiles the stdlib on every startup
        levels = ["-o", "0"] + (["-o", str(cfg.deploy.optimize)] if cfg.deploy.optimize else [])
        compiled = proc.run([console_python, "-m", "compileall", "-q", "-j", "0", *levels, *dirs], check=False, capture=True)
        if compiled.returncode != 0:
            failed = compiled.stdout.count("*** Error compiling")
            ui.warn(
                f"could not precompile {failed} file(s) to .pyc (paths longer than 260 characters?). "
                "The app still works; it just starts a bit slower the first time."
            )
        if req.compiled:
            _smoke_compiled(cfg, console_python, out)

    if cfg.deploy.portable.archive:
        fmt = "zip" if IS_WINDOWS else "gztar"
        archive = shutil.make_archive(str(out), fmt, root_dir=out.parent, base_dir=out.name)
        ui.info(f"  archive: {rel(Path(archive))}")
    ui.info(f"  run:{', '.join(rel(p) for p in launchers)}  ({common.dir_size_mb(out):.0f} MB)")
    return out


def _smoke_compiled(cfg: Config, python: Path, out: Path) -> None:
    from .. import mypyc

    modules = mypyc.compiled_modules(cfg)
    code = (
        "import importlib,sys;sys.path.insert(0,'app');"
        f"bad=[m for m in {modules!r} if not (importlib.import_module(m).__file__ or '').endswith(('.pyd','.so'))];"
        "print(','.join(bad))"
    )
    bad = proc.output([python, "-s", "-c", code], cwd=out)
    if bad:
        raise DeployError(f"the portable folder does not load the mypyc binaries for: {bad}")
    ui.ok("verified: the compiled modules load from .pyd/.so")
