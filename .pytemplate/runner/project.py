"""Project paths and platform detection."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path, PurePath

from . import ui
from .ui import DeployError

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / ".pytemplate"
TEMPLATES = TEMPLATE / "templates"
PRESETS = TEMPLATE / "presets"
TOOLS = TEMPLATE / "tools"
SRC = ROOT / "src"
DIST = ROOT / "dist"
CONFIG_FILE = ROOT / "pytemplate.toml"
PYPROJECT = ROOT / "pyproject.toml"
STATE_FILE = TEMPLATE / "state.json"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

WSL_INTEROP = "/proc/sys/fs/binfmt_misc/WSLInterop"
_WINDOWS_SOURCE = re.compile(r"[A-Za-z]:|\\\\")  # C:\ (drvfs, 9p) or \\server\share


def wsl_kernel(environ: Mapping[str, str] = os.environ, release: str | None = None, interop: str = WSL_INTEROP) -> bool:
    """Whether this Linux runs under WSL, from the kernel: WSL sets WSL_DISTRO_NAME only for what
    wsl.exe starts, not under sudo, sshd, cron or a systemd unit."""
    if release is None:
        release = platform.release()  # 5.15.x-microsoft-standard-WSL2, 4.4.0-19041-Microsoft
    return "WSL_DISTRO_NAME" in environ or "microsoft" in release.lower() or os.path.exists(interop)


def _mount_field(text: str) -> str:
    """/proc/self/mounts writes a blank, tab, newline and backslash as \\040 \\011 \\012 \\134."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), text)


def windows_checkout(root: PurePath, mounts: str | None) -> bool:
    """Whether `root` lies on a Windows drive mounted into WSL. `mounts` is /proc/self/mounts:
    the deepest mount above `root` decides (drvfs in WSL 1, 9p with aname=drvfs in WSL 2, or a
    source that is a drive or a UNC share: `mount -t drvfs D: /d`, automount root = /).
    Without it (unreadable), a checkout under /mnt/ counts, where WSL mounts the drives."""
    target = root.as_posix()  # a Linux path; str() of a WindowsPath would hold backslashes
    best: tuple[int, bool] | None = None
    for line in (mounts or "").splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        source, point, fstype, options = (_mount_field(f) for f in fields[:4])
        if target != point and not target.startswith(point.rstrip("/") + "/"):
            continue
        windows = fstype == "drvfs" or "aname=drvfs" in options or bool(_WINDOWS_SOURCE.match(source))
        if best is None or len(point) >= best[0]:  # the later of two equal mounts is on top
            best = (len(point), windows)
    if best is None:
        return target.startswith("/mnt/")
    return best[1]


def _read_mounts() -> str | None:
    try:
        return Path("/proc/self/mounts").read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None


def detect_wsl(
    root: Path, *, system: str = sys.platform, environ: Mapping[str, str] = os.environ, release: str | None = None,
    interop: str = WSL_INTEROP, read_mounts: Callable[[], str | None] = _read_mounts,
) -> bool:  # fmt: skip
    """IS_WSL: Linux under a WSL kernel, with `root` on a Windows drive (the mounts are read
    only then). The Neovim plugin mirrors it (`init.lua` env_suffix)."""
    return system == "linux" and wsl_kernel(environ, release, interop) and windows_checkout(root, read_mounts())


# WSL on a Windows checkout (/mnt/c/...): separate environments and builds, so the
# Windows .venv is not turned into a Linux one.
IS_WSL = detect_wsl(ROOT)
ENV_SUFFIX = "-wsl" if IS_WSL else ""
BUILD = ROOT / ".build" / "wsl" if IS_WSL else ROOT / ".build"

EXT_SUFFIXES = (".pyd", ".so")


def venv_python(env_dir: Path) -> Path:
    """Return the interpreter inside a virtual environment (Scripts/ on Windows, bin/ elsewhere)."""
    if IS_WINDOWS:
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def host_os() -> str:
    """windows | linux | macos"""
    if IS_WINDOWS:
        return "windows"
    return "macos" if IS_MACOS else "linux"


def host_arch() -> str:
    """x86_64 | aarch64 | x86 (uv names)."""
    machine = platform.machine().lower()
    return {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "x86": "x86",
        "i386": "x86",
        "i686": "x86",
    }.get(machine, machine)


def rel(path: Path | str) -> str:
    """Return a path relative to the project root for display (or the absolute one if outside)."""
    p = Path(path)
    try:
        return p.resolve().relative_to(ROOT).as_posix() or "."
    except ValueError:
        return str(p)


def code_dirs() -> list[str]:
    """Return the code folders that ruff/mypy check (those that exist)."""
    return [d for d in ("src", "tests") if (ROOT / d).is_dir()]


# --- paths typed by the user ------------------------------------------------------------------
#
# Every path the RUNNER takes from the command line (./deploy new DEST, pyz-merge ... --out X)
# goes through user_path(). Arguments forwarded to the app or to pytest are never touched.

_DRIVE_ABS = re.compile(r"[A-Za-z]:[\\/]")
_DRIVE_MOUNT = re.compile(r"/(?:cygdrive/)?([A-Za-z])(?:/(.*))?", re.DOTALL)
# PYTEMPLATE_LAUNCHER suffix -> the DLL of that POSIX runtime (next to its real cygpath.exe)
_POSIX_RUNTIMES = {":msys": "msys-2.0.dll", ":cygwin": "cygwin1.dll"}


def _upper_drive(path: str) -> str:
    return path[0].upper() + path[1:] if _DRIVE_ABS.match(path) else path


def find_cygpath(dll: str) -> str | None:
    """Return the cygpath.exe of the calling MSYS2 / Git for Windows / Cygwin installation.

    Only a cygpath.exe next to the runtime DLL counts: WinuxCmd (niubash, xonsh-shell-kit)
    ships its own cygpath.exe, which knows nothing about the MSYS root (/tmp -> \\tmp). Order:
    MSYSTEM_PREFIX (MSYS2 login shells), EXEPATH (Git Bash), SHELL, then the first one on PATH.
    MSYS turns those variables into Windows paths for native programs such as this one.
    """
    dirs: list[Path] = []
    prefix = os.environ.get("MSYSTEM_PREFIX", "")  # <root>\usr or <root>\mingw64
    if _DRIVE_ABS.match(prefix):
        dirs.append(Path(prefix).parent / "usr" / "bin")
    exepath = os.environ.get("EXEPATH", "")  # <git>\bin
    if _DRIVE_ABS.match(exepath):
        dirs += [Path(exepath).parent / "usr" / "bin", Path(exepath) / "usr" / "bin"]
    shell = os.environ.get("SHELL", "")  # <root>\usr\bin\bash.exe, or Git's <git>\bin\bash.exe
    if _DRIVE_ABS.match(shell):
        dirs += [Path(shell).parent, Path(shell).parent.parent / "usr" / "bin"]
    found = shutil.which("cygpath")
    if found:
        dirs.append(Path(found).parent)
    for d in dirs:
        if (d / "cygpath.exe").is_file() and (d / dll).is_file():
            return str(d / "cygpath.exe")
    return None


def _posix_root_path(raw: str) -> str | None:
    """Map a path inside the MSYS/Cygwin root (/home/x, /tmp/x) with that shell's cygpath."""
    launcher = os.environ.get("PYTEMPLATE_LAUNCHER", "")
    dll = next((d for suffix, d in _POSIX_RUNTIMES.items() if launcher.endswith(suffix)), None)
    cygpath = find_cygpath(dll) if dll else None
    if cygpath is None:
        return None
    try:
        out = subprocess.run(
            [cygpath, "-w", "--", raw],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if _DRIVE_ABS.match(out) or out.startswith("\\\\"):
        return _upper_drive(out)
    return None


def native_path(raw: str) -> str:
    """Map a path typed in a POSIX shell on Windows to a Windows path. Elsewhere: unchanged.

    MSYS2/Git Bash rewrite such arguments themselves before starting a native program, but
    Cygwin, niubash, busybox-w32 and MSYS2 with MSYS2_ARG_CONV_EXCL=* do not, so the runner
    can get /c/x, /cygdrive/c/x, C:/x, //server/share or a path inside the MSYS root. The last
    kind needs the shell's cygpath (only when PYTEMPLATE_LAUNCHER ends in :msys or :cygwin);
    without it the path is returned as-is. Relative paths and ~ are returned as-is too.
    """
    if not IS_WINDOWS:
        return raw
    if _DRIVE_ABS.match(raw):  # C:/x, c:\x
        return _upper_drive(os.path.normpath(raw))
    if raw.startswith(("//", "\\\\")):  # UNC: //server/share, \\server\share
        return os.path.normpath(raw)
    if not raw.startswith("/"):
        return raw
    m = _DRIVE_MOUNT.fullmatch(raw)  # /c/x, /C/x, /cygdrive/c/x
    if m:
        return os.path.normpath(m[1].upper() + ":\\" + (m[2] or ""))
    return _posix_root_path(raw) or raw


def caller_cwd() -> Path:
    """Return the directory ./deploy was typed in (the shell's logical path when known).

    The launchers export it in PYTEMPLATE_CALLER_CWD. It is trusted only while it still names
    the process cwd (`uv run --script` never changes the cwd): niubash sessions keep stale
    exports, and a relative or vanished value means nothing.
    """
    try:
        cwd = Path.cwd()
    except OSError:
        raise DeployError("the current directory no longer exists") from None
    raw = os.environ.get("PYTEMPLATE_CALLER_CWD", "")
    if raw:
        p = Path(native_path(raw))
        try:
            if p.is_absolute() and os.path.samefile(p, cwd):
                return p
        except OSError:
            pass
    return cwd


def user_path(raw: str) -> Path:
    """Return a path argument typed by the user, resolved against the caller's cwd.

    Accepts every spelling native_path() knows, ~ and ~/x, and relative paths. On Windows
    the result is normalized (C:\\a\\..\\b -> C:\\b, as Windows itself would read it).
    """
    if not raw.strip():
        raise DeployError("empty path argument")
    native = native_path(raw)
    if IS_WINDOWS and native.startswith("/") and os.environ.get("PYTEMPLATE_LAUNCHER", "").endswith(
        tuple(_POSIX_RUNTIMES)
    ):
        ui.warn(f"{raw}: no cygpath of the calling shell found; read as a path on the current drive")
    try:
        p = Path(native).expanduser()
    except RuntimeError as e:  # ~ without HOME/USERPROFILE
        raise DeployError(f"{raw}: {e}") from None
    if not p.is_absolute():
        p = caller_cwd() / p
    return Path(os.path.normpath(p)) if IS_WINDOWS else p
