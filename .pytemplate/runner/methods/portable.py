"""portable: self-contained folder  runtime/ + lib/ + app/ + boot.py + launchers.

The only standalone option for PyPy (PyInstaller and Nuitka only support CPython).
- runtime = "bundled": copies the backend's interpreter (managed by uv, relocatable) and
  prunes it. Host OS only.
- runtime = "system": no interpreter; the launchers use the Python/PyPy of the target machine.
  With pure dependencies, that folder works on any OS.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path

from .. import envs, proc, ui, upx
from ..cmd_build import BuildRequest, dist_path
from ..config import Config
from ..project import IS_WINDOWS, TEMPLATES, rel
from ..ui import DeployError
from . import common

STDLIB_PRUNE = {"test", "idlelib", "turtledemo", "ensurepip", "site-packages"}
ROOT_PRUNE = {"include", "libs", "Tools", "share"}
TK = {"tkinter", "_tkinter", "turtle.py"}
LONG_PREFIX = "\\\\?\\"


def long_path(path: Path) -> str:
    """Return the path in extended-length form on Windows (\\\\?\\C:\\...): no 260-character limit."""
    resolved = str(path.resolve())
    return LONG_PREFIX + resolved if IS_WINDOWS and not resolved.startswith(LONG_PREFIX) else resolved


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


def _cmd_value(key: str, value: str) -> str:
    """Return `value` ready for `set "KEY=value"` in a .cmd file (a literal % is written %%)."""
    if not value.isascii() or any(c in value for c in '"\r\n\0'):
        raise DeployError(
            f"deploy.portable.env.{key}: the .cmd launcher cannot hold this value "
            "(ASCII only, no double quotes or line breaks)"
        )
    return value.replace("%", "%%")


def _env_lines(cfg: Config, windows: bool) -> list[str]:
    # PYTHON_JIT is read by its first character ("false" would ENABLE it): always "0" or "1"
    env = {"PYTHONUTF8": "1", "PYTHON_JIT": "1" if cfg.python.jit else "0", **cfg.deploy.portable.env}
    if windows:
        return ['set "PYTHONHOME="', 'set "PYTHONPATH="', *(f'set "{k}={_cmd_value(k, v)}"' for k, v in env.items())]
    return ["unset PYTHONHOME PYTHONPATH", *(f"export {k}={shlex.quote(v)}" for k, v in env.items())]


def _opt_flag(cfg: Config) -> str:
    return {0: "", 1: " -O", 2: " -OO"}[cfg.deploy.optimize]


def _system_candidates(cfg: Config, backend: str, windows: bool) -> list[str]:
    """Return the interpreters a runtime = "system" launcher tries, in order."""
    if backend == "pypy":
        return ["pypy3", "pypy"]
    if windows:
        return [f"py -{cfg.python.cpython}", "python3", "python"]
    return [f"python{cfg.python.cpython}", "python3", "python"]


def _version_probe(cfg: Config) -> str:
    """Return `-c` code that exits 0 only on the minimum Python version or newer."""
    major, minor = cfg.min_python.split(".")
    return f"import sys; sys.exit(sys.version_info[:2] < ({major}, {minor}))"


def cmd_launcher(cfg: Config, backend: str, out: Path, python: Path | None) -> str:
    """Return the Windows launcher (ASCII, CRLF, no ( ) blocks: PATH may contain "(x86)")."""
    name = cfg.app.name
    flags = f"-s{_opt_flag(cfg)}"
    if python is not None:
        exe = f'"%~dp0{python.relative_to(out).as_posix().replace("/", chr(92))}"'
        start = 'start "" ' if cfg.app.gui else ""
        body = [f'{start}{exe} {flags} "%~dp0boot.py" %*', "exit /b %ERRORLEVEL%"]
    else:
        # Run each candidate (it must exist AND meet the minimum version): `py` can be installed
        # with no Python registered, and python3.exe can be the Microsoft Store alias
        order = _system_candidates(cfg, backend, windows=True)
        probe = f'-c "{_version_probe(cfg)}"'
        body = [f"{cmd} {probe} >nul 2>nul && goto run{i}" for i, cmd in enumerate(order)]
        need = "PyPy" if backend == "pypy" else "Python"
        body += [f"echo {name}: needs {need} {cfg.min_python} or newer in PATH 1>&2", "exit /b 9009"]
        for i, cmd in enumerate(order):
            body += [f":run{i}", f'{cmd} {flags} "%~dp0boot.py" %*', "exit /b %ERRORLEVEL%"]
    lines = ["@echo off", f"rem Launcher for {name} (portable folder generated by ./deploy)", "setlocal", *_env_lines(cfg, True), *body]
    return "\r\n".join(lines) + "\r\n"


def sh_launcher(cfg: Config, backend: str, out: Path, python: Path | None) -> str:
    """Return the POSIX launcher (#!/bin/sh, LF)."""
    name = cfg.app.name
    flags = f"-s{_opt_flag(cfg)}"
    lines = [
        "#!/bin/sh",
        f"# Launcher for {name} (portable folder generated by ./deploy)",
        # BASH_SOURCE first: shells that run sh scripts in-process (niubash) keep the caller's $0
        'HERE=$(cd "$(dirname "${BASH_SOURCE:-$0}")" && pwd)',
        *_env_lines(cfg, False),
    ]
    if python is not None:
        lines.append(f'exec "$HERE/{python.relative_to(out).as_posix()}" {flags} "$HERE/boot.py" "$@"')
    else:
        need = "PyPy" if backend == "pypy" else "Python"
        lines += [
            f"for py in {' '.join(_system_candidates(cfg, backend, windows=False))}; do",
            f"    if \"$py\" -c {shlex.quote(_version_probe(cfg))} >/dev/null 2>&1; then",
            f'        exec "$py" {flags} "$HERE/boot.py" "$@"',
            "    fi",
            "done",
            f"echo \"{name}: needs {need} {cfg.min_python} or newer in PATH\" >&2",
            "exit 127",
        ]
    return "\n".join(lines) + "\n"


def write_launchers(cfg: Config, backend: str, out: Path, python: Path | None) -> list[Path]:
    """Write <name>.cmd (Windows) and/or <name>.sh (POSIX). Never -I/-E: they would disable PYTHON_JIT."""
    written: list[Path] = []
    if python is None or IS_WINDOWS:
        path = out / f"{cfg.app.name}.cmd"
        path.write_text(cmd_launcher(cfg, backend, out, python), encoding="ascii", newline="")
        written.append(path)
    if python is None or not IS_WINDOWS:
        path = out / f"{cfg.app.name}.sh"
        path.write_text(sh_launcher(cfg, backend, out, python), encoding="utf-8", newline="\n")
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

    if upx.active(cfg):
        upx.pack_tree(cfg, out)  # before the smoke test, so that it loads the packed binaries
    if python is not None and req.compiled:
        _smoke_compiled(cfg, python.with_name("python.exe") if IS_WINDOWS else python, out)

    if cfg.deploy.portable.archive:
        fmt = "zip" if IS_WINDOWS else "gztar"
        archive = shutil.make_archive(str(out), fmt, root_dir=out.parent, base_dir=out.name)
        ui.info(f"  archive: {rel(Path(archive))}")
    ui.info(f"  run: {', '.join(rel(p) for p in launchers)}  ({common.dir_size_mb(out):.0f} MB)")
    return out


SMOKE_MARK = "PTSMOKE:"


def smoke_code(modules: list[str]) -> str:
    """Return the `-c` code of the smoke test: the same sys.path as boot.py (app/, lib/, the rest).

    It prints SMOKE_MARK + the modules NOT loaded from a binary (imported packages may print too).
    """
    return (
        "import importlib,site,sys;b=list(sys.path);site.addsitedir('lib');"
        "sys.path[:]=[p for p in sys.path if p not in b]+b;sys.path.insert(0,'app');"
        f"bad=[m for m in {modules!r} if not (importlib.import_module(m).__file__ or '').endswith(('.pyd','.so'))];"
        f"print({SMOKE_MARK!r}+','.join(bad))"
    )


def _smoke_compiled(cfg: Config, python: Path, out: Path) -> None:
    from .. import mypyc

    code = smoke_code(mypyc.compiled_modules(cfg))
    result = proc.run([python, "-s", "-c", code], cwd=out, capture=True, check=False, echo=False)
    marks = [ln for ln in result.stdout.splitlines() if ln.startswith(SMOKE_MARK)]
    if result.returncode != 0 or not marks:
        if result.stderr:
            ui.info(result.stderr.rstrip())
        raise DeployError(f"the portable folder cannot import the compiled modules (exit code {result.returncode})")
    bad = marks[-1].removeprefix(SMOKE_MARK).strip()
    if bad:
        raise DeployError(f"the portable folder does not load the mypyc binaries for: {bad}")
    ui.ok("verified: the compiled modules load from .pyd/.so")
