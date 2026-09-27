"""portable: self-contained folder  runtime/ + lib/ + app/ + boot.py + launchers.

The only standalone option for PyPy (PyInstaller and Nuitka only support CPython).
- runtime = "bundled": copies the backend's interpreter (managed by uv, relocatable) and
  prunes it. Host OS only.
- runtime = "system": no interpreter; the launchers use the Python/PyPy of the target machine.
  With pure dependencies that no marker limits to some platforms or Python versions, that folder
  works on any OS; otherwise the build warns that it only fits the machine that built it.
"""

from __future__ import annotations

import re
import shlex
import shutil
import struct
import zipfile
from pathlib import Path

from .. import envs, proc, ui, upx
from ..cmd_build import BuildRequest, dist_path
from ..config import Config
from ..project import IS_WINDOWS, TEMPLATES, host_os, rel
from ..ui import DeployError
from . import common

STDLIB_PRUNE = {"test", "idlelib", "turtledemo", "ensurepip", "site-packages"}
# Scripts: the console scripts of the base (Windows), like bin/ below
ROOT_PRUNE = {"include", "libs", "Tools", "share", "Scripts"}
# What bin/ keeps (POSIX): the interpreter and its links, and PyPy's libpypy*-c.so. The rest
# are console scripts of packages installed into the base (pip, idle3, or a tool someone
# installed there, such as a 24 MB ruff): their site-packages is pruned, and their shebangs
# point at the build machine
BIN_KEEP = ("python", "pypy", "libpypy")
TK = {"tkinter", "_tkinter", "turtle.py"}
# Tcl/Tk next to the stdlib, loaded only by _tkinter: uv's CPython on Linux/macOS keeps lib/
# libtcl9.0.so, libtcl9tk9.0.so, tcl9.0/, tk9.0/, itcl4.3.8/, thread3.0.6/ and
# lib-dynload/_tkinter.*.so; PyPy lib/libtcl8.6.so, tk8.6/...; Windows DLLs/tcl86t.dll, _tkinter.pyd
TCL_RE = re.compile(r"(lib)?(tcl|tk|itcl|thread)\d|_tkinter\.", re.IGNORECASE)
# The shared libpython next to a statically linked interpreter (Linux, python-build-standalone:
# bin/python3.X holds the whole interpreter, 31 MB, and lib/libpython3.X.so.1.0 is another 33 MB
# copy for programs that embed Python). Pruned only when the interpreter does not need it (its
# ELF DT_NEEDED entries, _elf_needed): extension modules never link libpython on Linux (3.8+)
LIBPYTHON_RE = re.compile(r"libpython3[.0-9]*[a-z]*\.so(\.[.0-9]+)?")
# PyPy still ships lib2to3's deliberately broken test data: it never compiles
COMPILE_EXCLUDE = r"[/\\]lib2to3[/\\]tests[/\\]"
LONG_PREFIX = "\\\\?\\"
LONG_UNC = LONG_PREFIX + "UNC\\"  # the extended-length form of \\server\share\...


def long_path(path: Path) -> str:
    """Return the path in extended-length form on Windows (\\\\?\\C:\\...): no 260-character limit.

    A network path (a share or a mapped drive: resolve() gives \\\\server\\share\\...) takes the UNC
    form \\\\?\\UNC\\server\\share\\...: \\\\?\\ in front of \\\\server is no valid name (WinError 123), and
    the bundled portable build of a project on a share failed.
    """
    resolved = str(path.resolve())
    if not IS_WINDOWS or resolved.startswith((LONG_PREFIX, "\\\\.\\")):
        return resolved
    if resolved.startswith("\\\\"):
        return LONG_UNC + resolved[2:]
    return LONG_PREFIX + resolved


def short_path(text: str) -> str:
    """The usual form of a path `long_path` made long (\\\\?\\UNC\\server\\x -> \\\\server\\x)."""
    if text.startswith(LONG_UNC):
        return "\\\\" + text[len(LONG_UNC) :]
    return text.removeprefix(LONG_PREFIX)


def _elf_needed(path: Path) -> list[str] | None:
    """The DT_NEEDED entries (shared libraries) of an ELF executable; None when `path` is not an
    ELF file this can read (then the caller keeps what it would have pruned)."""
    try:
        with path.open("rb") as f:
            ident = f.read(16)
            if len(ident) < 16 or ident[:4] != b"\x7fELF" or ident[4] not in (1, 2) or ident[5] not in (1, 2):
                return None
            wide, end = ident[4] == 2, "<" if ident[5] == 1 else ">"
            head = struct.unpack(end + ("HHIQQQIHHHHHH" if wide else "HHIIIIIHHHHHH"), f.read(48 if wide else 36))
            phoff, phentsize, phnum = head[4], head[8], head[9]
            loads: list[tuple[int, int, int]] = []  # (vaddr, filesz, offset) of each PT_LOAD
            dynamic: tuple[int, int] | None = None  # (offset, size) of PT_DYNAMIC
            for i in range(phnum):
                f.seek(phoff + i * phentsize)
                if wide:
                    p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz = struct.unpack(end + "IIQQQQ", f.read(40))
                else:
                    p_type, p_offset, p_vaddr, _paddr, p_filesz = struct.unpack(end + "IIIII", f.read(20))
                if p_type == 1:
                    loads.append((p_vaddr, p_filesz, p_offset))
                elif p_type == 2:
                    dynamic = (p_offset, p_filesz)
            if dynamic is None:
                return []  # fully static: needs no shared library at all
            f.seek(dynamic[0])
            raw = f.read(dynamic[1])
            size = 16 if wide else 8
            needed: list[int] = []
            strtab = strsz = 0
            for i in range(0, len(raw) - size + 1, size):
                tag, value = struct.unpack(end + ("qQ" if wide else "iI"), raw[i : i + size])
                if tag == 0:
                    break
                if tag == 1:
                    needed.append(value)
                elif tag == 5:
                    strtab = value
                elif tag == 10:
                    strsz = value
            start = next((strtab - v + off for v, n, off in loads if v <= strtab < v + n), None)
            if start is None or not strsz:
                return None
            f.seek(start)
            table = f.read(strsz)
    except (OSError, struct.error):
        return None
    return [table[i : table.find(b"\0", i)].decode("utf-8", "replace") for i in needed if i < len(table)]


def _keeps_libpython(base: Path, version: str) -> bool:
    """Whether the copied runtime must keep lib/libpython3.X.so*: everywhere but Linux, and on
    Linux when the interpreter itself needs it (a shared build) or cannot be read as ELF."""
    if host_os() != "linux":
        return True
    needed = _elf_needed((base / "bin" / f"python{version}").resolve())
    return needed is None or any(LIBPYTHON_RE.fullmatch(n) for n in needed)


def _stdlib_dirs(base: Path, version: str) -> set[Path]:
    return {base / "Lib", base / "lib" / f"python{version}", base / "lib" / f"pypy{version}"}


def copy_runtime(cfg: Config, backend: str, dest: Path, lib: Path | None = None) -> Path:
    """Copy the (pruned) interpreter and return the path of its executable inside `dest`.

    Tk stays when src/ or the installed dependencies in `lib` (default: the lib/ next to `dest`)
    import tkinter or turtle: customtkinter or ttkbootstrap need it even when the app never
    imports it itself.
    """
    info = envs.interpreter_info(common.ensure_env(envs.runtime_env(cfg, backend)).python)
    base = Path(str(info["base_prefix"])).resolve()
    version = ".".join(str(info["version"]).split(".")[:2])
    prune = cfg.deploy.portable.prune
    keep_tk = common.uses_tkinter(lib if lib is not None else dest.parent / "lib")
    keep_libpython = info["impl"] != "cpython" or _keeps_libpython(base, version)
    stdlib = _stdlib_dirs(base, version)
    tk_dirs = {base / "lib", base / "DLLs"} | {s / "lib-dynload" for s in stdlib}

    def ignore(directory: str, names: list[str]) -> set[str]:
        d = Path(short_path(directory)).resolve()
        # The base's bytecode caches are partial and mostly at the wrong -O level: build()
        # compiles the stdlib at the launchers' level instead
        skip = {n for n in names if n == "__pycache__"}
        if not prune:
            return skip
        skip |= {n for n in names if n.endswith(".debug")}  # detached debug symbols (PyPy: 16 MB)
        if d == base:
            skip |= {n for n in names if n in ROOT_PRUNE or (not keep_tk and n.lower().startswith("tcl"))}
        if d == base / "bin":
            skip |= {n for n in names if not n.startswith(BIN_KEEP)}
        if d == base / "lib" and not keep_libpython:
            skip |= {n for n in names if LIBPYTHON_RE.fullmatch(n)}
        if not keep_tk and d in tk_dirs and d not in stdlib:  # not in stdlib: on Windows lib == Lib
            skip |= {n for n in names if TCL_RE.match(n)}
        if d in stdlib:
            skip |= {n for n in names if n in STDLIB_PRUNE or (not keep_tk and n in TK)}
        if d.parent in stdlib:
            skip |= {n for n in names if n in {"test", "tests"}}  # PyPy: unittest/test, lib2to3/tests...
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
        cp_base = Path(str(envs.interpreter_info(common.ensure_env(envs.cpython_env(cfg)).python)["base_prefix"])).resolve()
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


def check(cfg: Config) -> None:
    """Refuse what the launchers cannot hold (cmd_build calls it before the checks and the
    payload, also in --dry-run): a .cmd is written on Windows and for runtime = "system"."""
    if IS_WINDOWS or cfg.deploy.portable.runtime != "bundled":
        _env_lines(cfg, windows=True)


def _env_lines(cfg: Config, windows: bool, extra: dict[str, str] | None = None) -> list[str]:
    env = {"PYTHONUTF8": "1", **(extra or {}), **cfg.deploy.portable.env}  # the user's values win
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


def cmd_launcher(cfg: Config, backend: str, out: Path, python: Path | None, env: dict[str, str] | None = None) -> str:
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
            # app.gui: the windowed twin (pyw/pythonw/pypyw) without a console window, like the
            # bundled branch; the probe above keeps the console names (it needs the exit code)
            run = f'start "" {common.windowed(cmd)}' if cfg.app.gui else cmd
            body += [f":run{i}", f'{run} {flags} "%~dp0boot.py" %*', "exit /b %ERRORLEVEL%"]
    lines = ["@echo off", f"rem Launcher for {name} (portable folder generated by ./deploy)", "setlocal", *_env_lines(cfg, True, env), *body]
    return "\r\n".join(lines) + "\r\n"


def sh_launcher(cfg: Config, backend: str, out: Path, python: Path | None, env: dict[str, str] | None = None) -> str:
    """Return the POSIX launcher (#!/bin/sh, LF)."""
    name = cfg.app.name
    flags = f"-s{_opt_flag(cfg)}"
    lines = [
        "#!/bin/sh",
        f"# Launcher for {name} (portable folder generated by ./deploy)",
        # The folder of this script. BASH_SOURCE first: shells that run sh scripts in-process
        # (niubash) keep the caller's $0. Symlinks are followed (a link in ~/.local/bin), each
        # relative target joined to the PHYSICAL folder of its link; CDPATH='' because an
        # exported CDPATH made cd print the folder (HERE got two lines) or pick another one.
        "_pt_self=${BASH_SOURCE:-$0}",
        "_pt_n=0",
        'while [ -h "$_pt_self" ] && [ "$_pt_n" -lt 40 ]; do',
        "    _pt_n=$((_pt_n + 1))",
        '    _pt_dir=$(dirname "$_pt_self")',
        "    _pt_dir=$(CDPATH='' cd -P -- \"$_pt_dir\" && pwd -P)",
        '    _pt_link=$(readlink "$_pt_self")',
        "    case $_pt_link in",
        "        /*) _pt_self=$_pt_link ;;",
        "        *) _pt_self=$_pt_dir/$_pt_link ;;",
        "    esac",
        "done",
        '_pt_dir=$(dirname "$_pt_self")',
        "HERE=$(CDPATH='' cd -P -- \"$_pt_dir\" && pwd -P)",
        "unset _pt_self _pt_dir _pt_link _pt_n",
        *_env_lines(cfg, False, env),
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


def write_launchers(cfg: Config, backend: str, out: Path, python: Path | None, env: dict[str, str] | None = None) -> list[Path]:
    """Write <name>.cmd (Windows) and/or <name>.sh (POSIX). Never -I/-E: they would ignore PYTHONUTF8."""
    written: list[Path] = []
    if python is None or IS_WINDOWS:
        path = out / f"{cfg.app.name}.cmd"
        path.write_text(cmd_launcher(cfg, backend, out, python, env), encoding="ascii", newline="")
        written.append(path)
    if python is None or not IS_WINDOWS:
        path = out / f"{cfg.app.name}.sh"
        path.write_text(sh_launcher(cfg, backend, out, python, env), encoding="utf-8", newline="\n")
        path.chmod(0o755)
        written.append(path)
    return written


def runtime_stdlib(runtime: Path, version: str) -> Path | None:
    """Return the stdlib folder of a copied runtime (lib/pythonX.Y or lib/pypyX.Y before Lib/:
    macOS folder names ignore case, so Lib/ would also match lib/)."""
    for d in (runtime / "lib" / f"python{version}", runtime / "lib" / f"pypy{version}", runtime / "Lib"):
        if d.is_dir():
            return d
    return None


def compile_calls(cfg: Config, python: Path, out: Path, version: str) -> list[list[str | Path]]:
    """Return the compileall runs of a bundled folder.

    - lib/ and app/ at levels 0 and deploy.optimize; the bundled stdlib only at the launchers'
      -O level, the one they import (a read-only install used to recompile the stdlib at every
      start, a writable one wrote .pyc into runtime/).
    - checked-hash .pyc (PEP 552): timestamp ones went stale after the Windows zip (2-second
      local DOS times) or an extractor that drops mtimes; -f rewrites any that uv wrote.
    - -s <out>: the .pyc do not embed this machine's folder (Python fixes co_filename on load).
    - -B: compileall's own imports leave no stray level-0 .pyc in runtime/.
    """
    base: list[str | Path] = [python, "-B", "-m", "compileall", "-q", "-f", "-j", "0", "--invalidation-mode", "checked-hash", "-s", out]
    levels = ["-o", "0"] + (["-o", str(cfg.deploy.optimize)] if cfg.deploy.optimize else [])
    calls: list[list[str | Path]] = [[*base, *levels, out / "lib", out / "app"]]
    stdlib = runtime_stdlib(out / "runtime", version)
    if stdlib is not None:
        calls.append([*base, "-x", COMPILE_EXCLUDE, "-o", str(cfg.deploy.optimize), stdlib])
    return calls


def make_archive(out: Path, fmt: str) -> Path:
    """Archive the folder `out` next to it and return the archive (gztar keeps modes and mtimes).

    The zip (Windows) is written here: os.stat reports 0o666 for a .sh on Windows and ZipInfo
    records MS-DOS entries, so unzip and bsdtar extracted <name>.sh without its x bit. The .sh
    entries are written as Unix entries with mode 0755 (create_system 3 is needed too: unzip
    ignores the mode bits of MS-DOS entries). strict_timestamps=False: a file older than 1980
    (a copy from the Nix store) made zipfile raise ValueError.
    """
    if fmt != "zip":
        return Path(shutil.make_archive(str(out), fmt, root_dir=out.parent, base_dir=out.name))
    archive = out.with_name(out.name + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
        for path in [out, *sorted(out.rglob("*"))]:
            name = path.relative_to(out.parent).as_posix()
            if path.suffix == ".sh" and path.is_file():
                info = zipfile.ZipInfo.from_file(path, name, strict_timestamps=False)
                info.create_system = 3
                info.external_attr = 0o100755 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())
            else:
                zf.write(path, name)
    return archive


def build(req: BuildRequest) -> Path:
    cfg = req.cfg
    bundled = cfg.deploy.portable.runtime == "bundled"
    if req.targets and bundled:  # cmd_build refuses --target first (pyz only)
        raise DeployError("portable with a bundled runtime is only built for the host OS; use --method pyz for other OSes")
    host = common.host_target(cfg, req.backend)
    out = dist_path(req, f"-{host.key}" if bundled else "")
    # Before anything is removed: `uv export --locked` refuses a stale uv.lock, which left a
    # folder holding only app/ where the previous build was
    requirements = common.export_requirements(cfg)
    # The folder and its archives go together or not at all (the app may still run from the
    # folder): the previous archive must never sit next to a new or failed folder
    common.remove_output(out, *(Path(f"{out}{suffix}") for suffix in (".zip", ".tar.gz")))
    out.mkdir(parents=True)

    common.copy_app(req.app_dir, out / "app", extensions=True)
    common.install_deps(cfg, req.backend, host, out / "lib", requirements)
    if not bundled:
        _warn_host_only(host.key, requirements, out / "lib")
    client_env = _bundle_flet_client(cfg, out / "lib") if bundled else {}
    python = copy_runtime(cfg, req.backend, out / "runtime", out / "lib") if bundled else None
    shutil.copy2(TEMPLATES / "portable" / "boot.py", out / "boot.py")
    launchers = write_launchers(cfg, req.backend, out, python, client_env)
    # The console interpreter runs the build's own steps (pythonw.exe has no stdout)
    console_python = None if python is None else python.with_name("python.exe") if IS_WINDOWS else python

    if console_python is not None:
        _check_interpreter(console_python)
        failed = 0
        for argv in compile_calls(cfg, console_python, out, host.version):
            compiled = proc.run(argv, check=False, capture=True)
            if compiled.returncode != 0:
                failed += max(1, compiled.stdout.count("*** Error compiling"))
        if failed:
            ui.warn(
                f"could not precompile {failed} file(s) to .pyc (paths longer than 260 characters?). "
                "The app still works; it just starts a bit slower the first time."
            )

    if upx.active(cfg):
        upx.pack_tree(cfg, out)  # before the smoke tests, so that they load the packed binaries
    if console_python is not None:
        _smoke_runtime(cfg, console_python, out)
        if req.compiled:
            _smoke_compiled(cfg, console_python, out)

    if cfg.deploy.portable.archive:
        archive = make_archive(out, "zip" if IS_WINDOWS else "gztar")
        ui.info(f"  archive: {rel(archive)}")
    ui.info(f"  run: {', '.join(rel(p) for p in launchers)}  ({common.dir_size_mb(out):.0f} MB)")
    return out


def _bundle_flet_client(cfg: Config, lib: Path) -> dict[str, str]:
    """A Flet app's desktop client, in the folder: the flet-desktop wheel has none, so a bundled
    folder downloaded ~40 MB from GitHub at its first start (offline it could not start). The
    release archive goes where flet_desktop looks for a bundled one (lib/flet_desktop/app/), as
    `flet pack` and the nuitka method do; the returned variables go into the launchers so that
    the app looks for exactly that archive (nuitka.flet_client_env)."""
    if not (lib / "flet_desktop").is_dir():
        return {}
    from .nuitka import _flet_client_archive, fingerprint_file, flet_client_env

    archive = _flet_client_archive(cfg)
    (lib / "flet_desktop" / "app").mkdir(exist_ok=True)
    for file in (archive, fingerprint_file(archive)):  # the fingerprint: no hashing at every start
        shutil.copy2(file, lib / "flet_desktop" / "app" / file.name)
    linux = flet_client_env(archive.name)
    return {**linux, "FLET_APP_ID": cfg.app.name} if linux else {}  # the taskbar groups it as the app


def _warn_host_only(key: str, requirements: Path, lib: Path) -> None:
    """runtime = "system": lib/ was installed for this machine's interpreter, but both launchers
    start the app with any Python at or above the minimum, on any OS. Native wheels, and pins that
    a marker left out here (tzdata on win32, backports-tarfile below 3.12), make it host-only."""
    skipped = common.skipped_requirements(requirements, lib)
    reasons = ["native dependencies"] if common.has_native(lib) else []
    if skipped:
        reasons.append(f"dependencies for other platforms or Python versions ({', '.join(skipped)})")
    abis = common.extension_abis(lib)  # the key does not tell PyPy 7.3 (pp73) from PyPy 8 (pp80)
    if reasons:
        ui.warn(
            f'runtime = "system" with {" and ".join(reasons)}: lib/ only fits {key}'
            f"{' (' + ', '.join(abis) + ')' if abis else ''}, and the launchers "
            "start it on any OS and Python version. Build the folder on each platform, or use "
            "--method pyz with [deploy.pyz] targets"
        )


SMOKE_MARK = "PTSMOKE:"
PREFIX_MARK = "PTPREFIX:"


def _check_interpreter(python: Path) -> None:
    if not python.is_file():
        raise DeployError(f"the copied runtime has no {rel(python)}: did the interpreter's layout change?")


def _smoke_runtime(cfg: Config, python: Path, out: Path) -> None:
    """Start the bundled interpreter as the launchers do: it must run on its OWN runtime/.

    A prune rule that removes something the interpreter needs, a binary UPX broke, or a layout
    change of python-build-standalone or PyPy (bin/python3 missing, an interpreter that finds
    the uv base again) would otherwise ship as "done" and only fail on another machine.
    """
    _check_interpreter(python)
    code = f"import sys; print({PREFIX_MARK!r} + sys.prefix)"
    argv: list[str | Path] = [python, "-s", *_opt_flag(cfg).split(), "-B", "-c", code]
    result = proc.run(argv, cwd=out, capture=True, check=False, echo=False)
    marks = [ln for ln in result.stdout.splitlines() if ln.startswith(PREFIX_MARK)]
    if result.returncode != 0 or not marks:
        if result.stderr:
            ui.info(result.stderr.rstrip())
        raise DeployError(f"the bundled interpreter {rel(python)} does not start (exit code {result.returncode})")
    prefix = Path(marks[-1].removeprefix(PREFIX_MARK))
    runtime = (out / "runtime").resolve()
    if not prefix.resolve().is_relative_to(runtime):
        raise DeployError(
            f"the bundled interpreter runs on {prefix}, not on its own {rel(out / 'runtime')}: "
            "the folder would only work on this machine"
        )


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
    # The launchers' flags (-s plus -O/-OO), and -B: the smoke run must not write .pyc into the folder
    argv: list[str | Path] = [python, "-s", *_opt_flag(cfg).split(), "-B", "-c", code]
    result = proc.run(argv, cwd=out, capture=True, check=False, echo=False)
    marks = [ln for ln in result.stdout.splitlines() if ln.startswith(SMOKE_MARK)]
    if result.returncode != 0 or not marks:
        if result.stderr:
            ui.info(result.stderr.rstrip())
        raise DeployError(f"the portable folder cannot import the compiled modules (exit code {result.returncode})")
    bad = marks[-1].removeprefix(SMOKE_MARK).strip()
    if bad:
        raise DeployError(f"the portable folder does not load the mypyc binaries for: {bad}")
    ui.ok("verified: the compiled modules load from .pyd/.so")
