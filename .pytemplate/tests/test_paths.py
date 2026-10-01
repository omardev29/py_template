"""User paths, console colors and --dry-run (run them with `./pyt selftest`).

- native_path / caller_cwd / user_path: every spelling a Windows or POSIX shell can hand over.
- ui colors: NO_COLOR / TERM=dumb / not a terminal, and the Windows console mode (no cmd.exe).
- --dry-run: the runner runs in a throwaway copy of this project and must leave every file
  byte-identical; unknown arguments are errors.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipapp
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_DIR))

from runner import cmd_build, cmd_mode, config, presets, proc, project, ui  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import caller_cwd, native_path, user_path  # noqa: E402
from runner.ui import PytError  # noqa: E402

windows = pytest.mark.skipif(os.name != "nt", reason="Windows path rules")
posix = pytest.mark.skipif(os.name == "nt", reason="POSIX path rules")
_LAUNCHER_VARS = ("PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No launcher variables leak in from the shell that started the selftest."""
    for name in _LAUNCHER_VARS:
        monkeypatch.delenv(name, raising=False)


# --- WSL on a Windows checkout (project.IS_WSL) ------------------------------------------------------

# /proc/self/mounts as WSL writes it (a backslash is \134, a blank \040).
WSL2_MOUNTS = (
    "none /mnt/wsl tmpfs rw,relatime 0 0\n"
    "drivers /usr/lib/wsl/drivers 9p ro,nosuid,nodev,noatime,dirsync,aname=drivers;fmask=222;dmask=222,mmap,access=client 0 0\n"
    "/dev/sdc / ext4 rw,relatime,discard,errors=remount-ro,data=ordered 0 0\n"
    "C:\\134 /mnt/c 9p rw,noatime,dirsync,aname=drvfs;path=C:\\134;uid=1000;gid=1000;symlinkroot=/mnt/,mmap,access=client 0 0\n"
    "D: /d 9p rw,noatime,dirsync,aname=drvfs;path=D:;uid=1000;gid=1000 0 0\n"
    "\\134\\134nas\\134share /mnt/my\\040share 9p rw,noatime,aname=drvfs;path=UNC\\134nas\\134share 0 0\n"
    "tmpfs /mnt/c/Users/me/scratch tmpfs rw 0 0\n"
)
WSL1_MOUNTS = "rootfs / lxfs rw,noatime 0 0\nC:\\134 /mnt/c drvfs rw,noatime,uid=1000,gid=1000,case=off 0 0\n"


@pytest.mark.parametrize(
    ("root", "mounts", "expected"),
    [
        ("/mnt/c/Users/me/p", WSL2_MOUNTS, True),
        ("/mnt/c", WSL2_MOUNTS, True),
        ("/d/work/p", WSL2_MOUNTS, True),  # mount -t drvfs D: /d, or automount root = /
        ("/mnt/my share/p", WSL2_MOUNTS, True),  # a network share, blanks escaped
        ("/home/me/p", WSL2_MOUNTS, False),  # the distro's own ext4: its .venv is Linux-only
        ("/mnt/cx/p", WSL2_MOUNTS, False),  # /mnt/c is not a prefix of /mnt/cx
        ("/mnt/c/Users/me/scratch/p", WSL2_MOUNTS, False),  # the deepest mount decides
        ("/mnt/c/p", WSL1_MOUNTS, True),
        ("/home/me/p", WSL1_MOUNTS, False),
        ("/mnt/c/p", None, True),  # /proc/self/mounts unreadable: WSL's default automount
        ("/home/me/p", None, False),
    ],
)
def test_windows_checkout_follows_the_mount_of_the_root(root: str, mounts: str | None, expected: bool) -> None:
    assert project.windows_checkout(Path(root), mounts) is expected


def test_windows_checkout_reads_the_root_as_a_posix_path() -> None:
    """The suite runs on Windows too: str() of the WindowsPath of /mnt/c/p is \\mnt\\c\\p, which
    matched no mount point, and 10 tests (the Lua comparison too) failed there."""
    from pathlib import PureWindowsPath

    assert project.windows_checkout(PureWindowsPath("/mnt/c/Users/me/p"), WSL2_MOUNTS) is True
    assert project.windows_checkout(PureWindowsPath("/home/me/p"), WSL2_MOUNTS) is False


def test_wsl_kernel_needs_no_wsl_distro_name(tmp_path: Path) -> None:
    """sudo, sshd, cron and systemd units run without WSL_DISTRO_NAME: the kernel says WSL."""
    missing = str(tmp_path / "WSLInterop")
    assert project.wsl_kernel({}, "5.15.167.4-microsoft-standard-WSL2", missing)
    assert project.wsl_kernel({}, "4.4.0-19041-Microsoft", missing)
    assert project.wsl_kernel({"WSL_DISTRO_NAME": "Ubuntu"}, "6.8.0-generic", missing)
    (tmp_path / "WSLInterop").write_text("enabled\n", encoding="ascii")
    assert project.wsl_kernel({}, "6.6.87-custom", missing)  # interop registered: a custom WSL kernel
    assert not project.wsl_kernel({}, "6.8.0-45-generic", str(tmp_path / "none"))
    assert not project.wsl_kernel({}, "5.15.0-1057-azure", str(tmp_path / "none"))


WSL_RELEASE = "5.15.167.4-microsoft-standard-WSL2"
# (root, WSL_DISTRO_NAME, kernel release, WSLInterop registered, /proc/self/mounts, IS_WSL)
WSL_CASES = [
    ("/mnt/c/Users/me/p", None, WSL_RELEASE, False, WSL2_MOUNTS, True),  # sudo, sshd, cron: no WSL_DISTRO_NAME
    ("/d/work/p", None, "6.6.87-custom", True, WSL2_MOUNTS, True),  # a custom WSL kernel, automount root = /
    ("/mnt/c/p", "Ubuntu", "6.8.0-generic", False, WSL1_MOUNTS, True),
    ("/mnt/c/x", None, WSL_RELEASE, False, None, True),  # mounts unreadable: WSL's default automount
    ("/home/me/p", "Ubuntu", WSL_RELEASE, True, WSL2_MOUNTS, False),  # the distro's own ext4
    ("/srv/p", None, WSL_RELEASE, False, None, False),
    ("/mnt/c/p", None, "6.8.0-45-generic", False, WSL2_MOUNTS, False),  # plain Linux, a drvfs-like mount
]


@pytest.mark.parametrize(("root", "distro", "release", "interop", "mounts", "expected"), WSL_CASES)
def test_is_wsl_needs_a_wsl_kernel_and_a_windows_checkout(
    tmp_path: Path, root: str, distro: str | None, release: str, interop: bool, mounts: str | None, expected: bool
) -> None:
    environ = {"WSL_DISTRO_NAME": distro} if distro else {}
    marker = tmp_path / "WSLInterop"
    if interop:
        marker.write_text("enabled\n", encoding="ascii")
    got = project.detect_wsl(Path(root), system="linux", environ=environ, release=release, interop=str(marker), read_mounts=lambda: mounts)
    assert got is expected
    windows = project.detect_wsl(Path(root), system="win32", environ=environ, release=release, interop=str(marker), read_mounts=lambda: mounts)
    assert windows is False


def test_is_wsl_reads_the_mounts_only_under_a_wsl_kernel(tmp_path: Path) -> None:
    def unread() -> str | None:
        raise AssertionError("/proc/self/mounts read outside WSL")

    assert not project.detect_wsl(Path("/mnt/c/p"), system="linux", environ={}, release="6.8.0-generic", interop=str(tmp_path / "x"), read_mounts=unread)


_IMPORT_PROJECT = r"""
import json, os, pathlib, platform, sys
case = json.loads(os.environ["PT_CASE"])
real_read = pathlib.Path.read_text
def read_text(self, *args, **kwargs):
    if str(self) == "/proc/self/mounts":
        if case["mounts"] is None:
            raise PermissionError(str(self))
        return case["mounts"]
    return real_read(self, *args, **kwargs)
pathlib.Path.read_text = read_text
real_exists = os.path.exists
os.path.exists = lambda p: case["interop"] if str(p) == "/proc/sys/fs/binfmt_misc/WSLInterop" else real_exists(p)
platform.release = lambda: case["release"]
os.environ.pop("WSL_DISTRO_NAME", None)
if case["distro"]:
    os.environ["WSL_DISTRO_NAME"] = case["distro"]
sys.path.insert(0, case["template"])
from runner import project
print("PTWSL" + json.dumps([project.IS_WSL, project.ENV_SUFFIX, project.BUILD.relative_to(project.ROOT).as_posix()]))
"""


def _own_mounts(windows: bool) -> str:
    """A WSL 2 mount table whose Windows drive (or the distro's ext4) holds this project's root."""
    point = str(project.ROOT).replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")
    drive = f"C:\\134 {point} 9p rw,noatime,aname=drvfs;path=C:\\134;uid=1000 0 0\n" if windows else ""
    return "none /mnt/wsl tmpfs rw 0 0\n/dev/sdc / ext4 rw,relatime 0 0\n" + drive


@pytest.mark.skipif(sys.platform != "linux", reason="IS_WSL is only ever true on Linux")
@pytest.mark.parametrize(
    ("distro", "release", "windows", "expected"),
    [
        (None, WSL_RELEASE, True, True),  # sudo, sshd, cron, systemd: no WSL_DISTRO_NAME
        ("Ubuntu", "6.8.0-generic", True, True),
        (None, WSL_RELEASE, False, False),  # the distro's own ext4
        (None, "6.8.0-45-generic", True, False),  # no WSL kernel
    ],
)
def test_is_wsl_at_import_follows_the_kernel_and_the_mount_of_the_root(distro: str | None, release: str, windows: bool, expected: bool) -> None:
    """project.IS_WSL, ENV_SUFFIX and BUILD as a fresh runner computes them at import, with the
    kernel release, WSL_DISTRO_NAME and /proc/self/mounts faked around THIS project's root."""
    data = {"distro": distro, "release": release, "interop": False, "mounts": _own_mounts(windows), "template": str(TEMPLATE_DIR)}
    env = {**os.environ, "PT_CASE": json.dumps(data)}
    r = subprocess.run([sys.executable, "-c", _IMPORT_PROJECT], env=env, capture_output=True, text=True, timeout=60, check=False)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("PTWSL")), None)
    assert line is not None, r.stdout + r.stderr
    assert json.loads(line[len("PTWSL") :]) == ([True, "-wsl", ".build/wsl"] if expected else [False, "", ".build"])


# --- native_path ---------------------------------------------------------------------------------


@windows
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/c/x", "C:\\x"),
        ("/C/x/y", "C:\\x\\y"),
        ("/c", "C:\\"),
        ("/c/", "C:\\"),
        ("/cygdrive/c/x", "C:\\x"),
        ("/cygdrive/D/a/b", "D:\\a\\b"),
        ("/cygdrive/c", "C:\\"),
        ("/c/a/./b/../x", "C:\\a\\x"),
        ("C:/x", "C:\\x"),
        ("c:\\x", "C:\\x"),
        ("c:/a//b/../x", "C:\\a\\x"),
        ("//server/share/x", "\\\\server\\share\\x"),
        ("\\\\server\\share\\x", "\\\\server\\share\\x"),
        # Left for user_path: relative, drive-relative, home, and MSYS-root paths without a launcher
        ("a/b", "a/b"),
        ("..\\x", "..\\x"),
        ("C:x", "C:x"),
        ("~", "~"),
        ("~/x", "~/x"),
        ("/home/x", "/home/x"),
        ("/cygdrive", "/cygdrive"),
        ("/ab/x", "/ab/x"),
    ],
)
def test_native_path_windows(raw: str, expected: str) -> None:
    assert native_path(raw) == expected


@posix
@pytest.mark.parametrize("raw", ["/c/x", "C:/x", "c:\\x", "//server/share", "a/b", "~/x", "/home/x"])
def test_native_path_posix_is_unchanged(raw: str) -> None:
    assert native_path(raw) == raw


def _real_cygpath_dir() -> Path | None:
    """A cygpath.exe that belongs to an MSYS2 or Git for Windows install (next to msys-2.0.dll)."""
    candidates: list[Path] = []
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # <git>\cmd\git.exe or <git>\bin\git.exe
        candidates += [root / "usr" / "bin", root.parent / "usr" / "bin"]
    found = shutil.which("cygpath")
    if found:
        candidates.append(Path(found).parent)
    for d in candidates:
        if (d / "cygpath.exe").is_file() and (d / "msys-2.0.dll").is_file():
            return d
    return None


@windows
def test_msys_root_paths_use_the_shells_cygpath(monkeypatch: pytest.MonkeyPatch) -> None:
    usr_bin = _real_cygpath_dir()
    if usr_bin is None:
        pytest.skip("no MSYS2 / Git for Windows cygpath.exe on this machine")
    for name in ("SHELL", "EXEPATH", "MSYSTEM_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(usr_bin))
    monkeypatch.setenv("PYTEMPLATE_LAUNCHER", "sh:bash:msys")
    home = native_path("/home/someone")
    assert re.match(r"[A-Z]:\\", home) and home.endswith("\\home\\someone"), home
    assert re.match(r"[A-Z]:\\.*\\x$", native_path("/tmp/x")), native_path("/tmp/x")
    assert native_path("/c/x") == "C:\\x"  # drive mounts never need cygpath
    # Other launchers (cmd, PowerShell, niubash): no MSYS root to map into
    for launcher in ("cmd", "ps1:Core:7.6", "sh:niubash", ""):
        monkeypatch.setenv("PYTEMPLATE_LAUNCHER", launcher)
        assert native_path("/home/someone") == "/home/someone"


@windows
def test_cygpath_without_the_msys_runtime_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # WinuxCmd (niubash, xonsh-shell-kit) ships a cygpath.exe that maps /tmp/x to \tmp\x
    (tmp_path / "cygpath.exe").write_bytes(b"")
    for name in ("SHELL", "EXEPATH", "MSYSTEM_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert project.find_cygpath("msys-2.0.dll") is None
    (tmp_path / "msys-2.0.dll").write_bytes(b"")
    assert project.find_cygpath("msys-2.0.dll") == str(tmp_path / "cygpath.exe")
    assert project.find_cygpath("cygwin1.dll") is None


@windows
def test_msys_path_without_cygpath_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("SHELL", "EXEPATH", "MSYSTEM_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PYTEMPLATE_LAUNCHER", "sh:bash:msys")
    monkeypatch.chdir(tmp_path)
    assert user_path("/tmp/x") == Path(tmp_path.drive + "\\tmp\\x")
    assert "no cygpath" in capsys.readouterr().err


# --- caller_cwd ----------------------------------------------------------------------------------


def test_caller_cwd_without_the_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert caller_cwd() == Path.cwd()


def test_stale_caller_cwd_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # niubash keeps old exports: a directory that is not the process cwd means nothing
    (tmp_path / "old").mkdir()
    (tmp_path / "now").mkdir()
    monkeypatch.chdir(tmp_path / "now")
    monkeypatch.setenv("PYTEMPLATE_CALLER_CWD", str(tmp_path / "old"))
    assert caller_cwd() == Path.cwd()
    monkeypatch.setenv("PYTEMPLATE_CALLER_CWD", str(tmp_path / "gone"))
    assert caller_cwd() == Path.cwd()
    monkeypatch.setenv("PYTEMPLATE_CALLER_CWD", ".")  # relative: never trusted
    assert caller_cwd() == Path.cwd()
    assert caller_cwd().is_absolute()


def test_base_lock_refuses_a_second_run_and_releases(tmp_path: Path) -> None:
    """One run at a time per scratch base (selftest --e2e and --nvim use it around their run): a
    second holder is refused, naming the command, and the lock is released when the first exits."""
    with project.base_lock(tmp_path, "selftest --e2e"):
        with pytest.raises(ui.PytError, match=r"selftest --e2e: another run is using"):
            with project.base_lock(tmp_path, "selftest --e2e"):
                pass
    with project.base_lock(tmp_path, "selftest --nvim"):  # released: a later run takes it
        pass


def test_caller_cwd_keeps_the_shells_spelling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    raw = str(tmp_path)
    if os.name == "nt":
        raw = raw[0].lower() + raw[1:].replace("\\", "/") + "/"  # c:/Users/.../  (niubash's $PWD)
    monkeypatch.setenv("PYTEMPLATE_CALLER_CWD", raw)
    assert str(caller_cwd()) == str(Path(native_path(raw)))
    if os.name == "nt":
        assert str(caller_cwd()).startswith(tmp_path.drive.upper() + "\\")
        assert "/" not in str(caller_cwd())


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink (POSIX) or junction (Windows: no privileges needed)."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_caller_cwd_is_the_logical_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        _link_dir(link, real)
    except (OSError, ImportError, AttributeError) as e:
        pytest.skip(f"cannot create a directory link here: {e}")
    monkeypatch.chdir(real)  # the process sees the physical folder, the shell typed the link
    monkeypatch.setenv("PYTEMPLATE_CALLER_CWD", str(link))
    assert caller_cwd() == link
    assert user_path("x") == link / "x"


# --- user_path -----------------------------------------------------------------------------------


def test_user_path_relative_to_the_callers_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTEMPLATE_CALLER_CWD", str(tmp_path))
    assert user_path("a/b.pyz") == tmp_path / "a" / "b.pyz"
    assert user_path("./p1") == tmp_path / "p1"
    assert user_path(str(tmp_path / "abs")) == tmp_path / "abs"
    assert user_path("~") == Path.home()
    assert user_path("~/x") == Path.home() / "x"
    for empty in ("", "  "):
        with pytest.raises(PytError, match="empty path"):
            user_path(empty)


@windows
def test_user_path_windows_spellings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    drive = tmp_path.drive.upper()  # e.g. C:
    posix_form = "/" + drive[0].lower() + tmp_path.as_posix()[2:]  # /c/Users/...
    assert user_path(posix_form + "/p1") == tmp_path / "p1"
    assert user_path("/cygdrive" + posix_form + "/p1") == tmp_path / "p1"
    assert user_path(tmp_path.as_posix() + "/a/../p1") == tmp_path / "p1"
    assert str(user_path("..\\x")) == str(tmp_path.parent / "x")  # normalized like Windows does
    assert user_path("\\x") == Path(drive + "\\x")  # root of the current drive
    assert user_path("/usr/x") == Path(drive + "\\usr\\x")  # no :msys launcher: same rule


@posix
def test_user_path_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert user_path("~/x") == tmp_path / "home" / "x"
    assert user_path("/c/x") == Path("/c/x")
    assert user_path("a\\b") == tmp_path / "a\\b"  # a backslash is part of a POSIX file name


# --- console colors ------------------------------------------------------------------------------


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_ui_never_spawns_cmd_for_colors() -> None:
    import ast

    tree = ast.parse((TEMPLATE_DIR / "runner" / "ui.py").read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("system", "popen")
    ]
    assert not calls, "ui.py must not start a shell (os.system('') ran cmd.exe on every ./pyt)"


def test_no_color_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert ui.color_enabled() is False  # not a terminal
    monkeypatch.setattr(sys, "stderr", _FakeTty())
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui.color_enabled() is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert ui.color_enabled() is False
    monkeypatch.setenv("TERM", "xterm-256color")
    if os.name != "nt":
        assert ui.color_enabled() is True
        assert ui.enable_vt_mode() is False  # nothing to turn on outside Windows


_CONSOLE_PROBE = r"""
import ctypes, json, sys
from ctypes import wintypes
sys.path.insert(0, sys.argv[1])
k = ctypes.WinDLL("kernel32")
k.GetStdHandle.argtypes = [wintypes.DWORD]
k.GetStdHandle.restype = wintypes.HANDLE
def mode():
    m = wintypes.DWORD()
    ok = k.GetConsoleMode(k.GetStdHandle(0xFFFFFFF4), ctypes.byref(m))
    return [bool(ok), m.value]
spawned = []
def hook(event, args):
    if event in ("os.system", "subprocess.Popen", "_winapi.CreateProcess", "os.startfile"):
        spawned.append(event)
sys.addaudithook(hook)
before = mode()
from runner import ui
data = {"before": before, "after": mode(), "color": ui._COLOR, "spawned": spawned}
json.dump(data, open(sys.argv[2], "w"))
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console API")
@pytest.mark.parametrize("no_color", [False, True])
def test_windows_console_gets_ansi_without_cmd(tmp_path: Path, no_color: bool) -> None:
    out = tmp_path / "probe.json"
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "TERM")}
    if no_color:
        env["NO_COLOR"] = "1"
    # CREATE_NO_WINDOW: the child gets a real (hidden) console, so stderr is a console handle
    subprocess.run(
        [sys.executable, "-c", _CONSOLE_PROBE, str(TEMPLATE_DIR), str(out)],
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=60,
        check=True,
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["before"][0], "the child should have had a console"
    assert data["spawned"] == []  # no cmd.exe (the old os.system("") trick)
    vt = 0x0004
    if no_color:
        assert data["color"] is False
        assert data["after"] == data["before"]  # console left alone
    else:
        assert data["color"] is True
        assert data["after"][1] & vt


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console API")
def test_windows_redirected_output_has_no_colors(tmp_path: Path) -> None:
    out = tmp_path / "probe.json"
    subprocess.run([sys.executable, "-c", _CONSOLE_PROBE, str(TEMPLATE_DIR), str(out)], capture_output=True, timeout=60, check=True)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {"before": [False, 0], "after": [False, 0], "color": False, "spawned": []}


# --- --dry-run in a throwaway copy of the project ------------------------------------------------

needs_uv = pytest.mark.skipif(shutil.which("uv") is None and not os.environ.get("UV"), reason="uv not found")


@pytest.fixture(scope="module")
def project_copy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("dry")
    presets.copy_template(dest)
    return dest


def _snapshot(root: Path) -> dict[str, str]:
    """Every file (sha256) and folder under root: any write shows up as a difference."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts:
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<dir>"
    return out


def _pyt(root: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in (*_LAUNCHER_VARS, "VIRTUAL_ENV")}
    env.update(NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return subprocess.run(
        [sys.executable, "-B", str(root / ".pytemplate" / "pyt.py"), *args],
        cwd=cwd or root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def _copy_config(root: Path) -> dict[str, Any]:
    """The copy's pytemplate.toml: a project made with ./pyt new has its own preset and backends."""
    return tomllib.loads((root / "pytemplate.toml").read_text(encoding="utf-8-sig"))


def _another_backend(cfg: dict[str, Any]) -> str:
    """A backend `mode BACKEND` switches this copy to: mypyc, else another one (never mypyc with
    a typing.profile of warn or off, which that backend refuses)."""
    active, profile = cfg["backend"]["active"], cfg.get("typing", {}).get("profile", "auto")
    candidates = ("mypyc", *cfg["backend"]["supported"], "cpython", "pypy")
    return next(b for b in candidates if b != active and not (b == "mypyc" and profile in ("warn", "off")))


def _supports_and_typing(cfg: dict[str, Any]) -> list[str]:
    """`mode --supports ... --typing ...` with changes this copy has not made yet (-mypyc, or
    +mypyc where it is not supported, +cpython where it is the only backend)."""
    supported = cfg["backend"]["supported"]
    supports = "+mypyc" if "mypyc" not in supported else "-mypyc" if len(supported) > 1 else "+cpython"
    relaxed = cfg.get("typing", {}).get("relaxed", "off")
    return ["mode", "--supports", supports, "--typing", "warn" if relaxed == "strict" else "strict"]


@pytest.fixture
def unchanged(project_copy: Path) -> Iterator[Path]:
    before = _snapshot(project_copy)
    yield project_copy
    after = _snapshot(project_copy)
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert not changed, f"--dry-run wrote: {changed}"


@needs_uv
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["mode", "mypyc"], '[backend] active = "mypyc"'),
        (["mode", "--supports", "-mypyc", "--typing", "strict"], "[typing] relaxed"),
        # --force: a project that has its own code in src/ and tests/ (every real one) is refused
        # without it, and this test is about what --dry-run writes
        (["__init", "raylib", "--force"], "+ typings/raylib/__init__.pyi"),
        (["__init", "flet", "--name", "other", "--force"], "as 'other'"),
        (["render", "--force"], "generated files up to date"),
    ],
)
def test_dry_run_writes_nothing(unchanged: Path, args: list[str], expected: str) -> None:
    cfg = _copy_config(unchanged)
    if args[:2] == ["__init", "raylib"] and cfg["app"]["preset"] == "raylib":
        args, expected = ["__init", "script", "--force"], "- typings/raylib/__init__.pyi"  # a raylib project: the other way
    # mode prints only real changes: in a project where `./pyt mode mypyc` or `./pyt mode --typing
    # strict` already ran, the plan said "unchanged" or had no typing line, and the test failed
    if args[0] == "mode" and args[1] != "--supports":
        target = _another_backend(cfg)
        args, expected = ["mode", target], f'[backend] active = "{target}"'
    elif args[0] == "mode":
        args = _supports_and_typing(cfg)
    r = _pyt(unchanged, "--dry-run", *args)
    assert r.returncode == 0, r.stderr
    assert expected in r.stderr, r.stderr


@needs_uv
def test_the_tests_that_copy_the_project_pass_in_one_with_its_own_code(tmp_path: Path) -> None:
    """CLAUDE.md 13.1: the suite must pass in every project made with ./pyt new, whose src/
    and tests/ hold its own code. The tests that copy the project and run `__init`, `apply` or
    `rename` in the copy assumed the pristine skeleton: `__init` refused the copy ("have changes
    compared to the skeleton") and the plans lacked the skeleton's docstring, so 7 tests failed
    in every real project. They run here in a copy with code of its own (the ones that work
    offline: the rename's real run then skips, as it does without the package index)."""
    own = tmp_path / "own"
    presets.copy_template(own)
    pkg = str(_copy_config(own)["app"]["name"]).replace("-", "_").lower()
    (own / "src" / pkg / "__init__.py").write_text('"""My own benchmarks."""\n', encoding="utf-8")
    (own / "src" / pkg / "extra.py").write_text('"""Mine."""\n\n\ndef double(x: int) -> int:\n    return 2 * x\n', encoding="utf-8")
    (own / "tests" / "test_extra.py").write_text(
        f'"""Mine."""\n\nfrom {pkg}.extra import double\n\n\ndef test_double() -> None:\n    assert double(2) == 4\n', encoding="utf-8"
    )
    nodes = [
        "test_paths.py::test_dry_run_writes_nothing",
        "test_apply.py::test_real_dry_run_in_a_copy",
        "test_presets.py::test_init_that_fails_in_uv_changes_nothing",
        "test_rename.py::test_real_run_skips_without_the_index",
    ]
    drop = (*_LAUNCHER_VARS, "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "t"),
         *(f".pytemplate/tests/{node}" for node in nodes)],
        cwd=own,
        env={k: v for k, v in os.environ.items() if k not in drop},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        check=False,
    )
    assert r.returncode == 0, r.stdout[-6000:] + r.stderr[-2000:]


def test_the_task_tests_pass_in_a_project_whose_ci_task_is_its_own(tmp_path: Path) -> None:
    """[tasks] entries belong to the project's user. test_task_exit_codes_cross_pyt_py ran the
    preset's deps-only `ci` task with an argument: in a project that renamed or deleted it, or
    gave it a cmd, one test of ./pyt selftest failed. They run here in a copy whose `ci` runs a
    program of its own (exit 42), as the tasks they run are now the fixture's own."""
    own = tmp_path / "own"
    presets.copy_template(own)
    toml = own / "pytemplate.toml"
    text = toml.read_bytes().decode("utf-8")
    for key, value in (("deps", []), ("cmd", [sys.executable, "-c", "raise SystemExit(42)"]), ("uv", False)):
        text = config.set_value(text, "tasks.ci", key, value)
    toml.write_bytes(text.encode("utf-8"))
    nodes = ["test_cli_core.py::test_task_exit_codes_cross_pyt_py", "test_cli_core.py::test_task_exit_codes_cross_the_sh_launcher"]
    drop = (*_LAUNCHER_VARS, "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "t"),
         *(f".pytemplate/tests/{node}" for node in nodes)],
        cwd=own,
        env={k: v for k, v in os.environ.items() if k not in drop},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        check=False,
    )
    assert r.returncode == 0, r.stdout[-6000:] + r.stderr[-2000:]


def _run_tests_in(own: Path, tmp: Path, nodes: list[str]) -> subprocess.CompletedProcess[str]:
    """The test nodes (of .pytemplate/tests) run by pytest in the copy `own`, with the runner
    there and the suite's own pytest settings, as ./pyt selftest runs them (-ra lists the skips)."""
    drop = (*_LAUNCHER_VARS, "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", ".pytemplate/tests/pytest.ini", "--rootdir=.",
         "--basetemp", str(tmp / "t"), *(f".pytemplate/tests/{node}" for node in nodes)],
        cwd=own, env={k: v for k, v in os.environ.items() if k not in drop}, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=900, check=False,
    )  # fmt: skip


@needs_uv
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_install_tests_pass_in_a_project_with_install_and_uninstall_tasks(tmp_path: Path) -> None:
    """README (Custom tasks) and CLAUDE.md 5.2: a [tasks] entry named like a builtin added after
    the contract (install, uninstall) keeps its name in its project, where ./pyt install runs it.
    test_install made its template clones from the project's own files, pytemplate.toml
    included, and each `install` of a clone ran the project's task (uv tool install, with its
    downloads, about 20 times); test_every_command_rejects_an_unknown_argument got the task's
    exit code for install and uninstall. 22 tests failed in such a project. They run here in a
    copy whose install and uninstall tasks run a program of their own (exit 42)."""
    own = tmp_path / "own"
    presets.copy_template(own)
    toml = own / "pytemplate.toml"
    text = toml.read_bytes().decode("utf-8")
    for name in ("install", "uninstall"):
        for key, value in (("cmd", [sys.executable, "-c", "raise SystemExit(42)"]), ("uv", False)):
            text = config.set_value(text, f"tasks.{name}", key, value)
    toml.write_bytes(text.encode("utf-8"))
    rejects = "test_cli_core.py::test_every_command_rejects_an_unknown_argument"
    nodes = [
        "test_install.py::test_install_copies_the_tracked_template_and_writes_the_launchers",
        "test_install.py::test_dry_runs_write_nothing",
        "test_install.py::test_uninstall_removes_only_what_install_wrote",
        *(f"{rejects}[{name}-{bogus}]" for name in ("install", "uninstall") for bogus in ("--pt-bogus-flag", "pt-bogus-positional")),
    ]
    r = _run_tests_in(own, tmp_path, nodes)
    assert r.returncode == 0 and "7 passed" in r.stdout, r.stdout[-6000:] + r.stderr[-2000:]


@needs_uv
def test_dry_run_mode_supports_pypy(unchanged: Path) -> None:
    if "pypy" in _copy_config(unchanged)["backend"]["supported"]:
        pytest.skip("this project already supports PyPy (the raylib preset): +pypy changes nothing")
    r = _pyt(unchanged, "--dry-run", "mode", "--supports", "+pypy")
    assert r.returncode == 0, r.stderr
    assert "pyproject.toml   would rewrite the managed parts" in r.stderr
    assert "uv.lock          would re-lock" in r.stderr
    assert "would sync .venv-pypy" in r.stderr
    assert "(--dry-run) skipped" in r.stderr  # no .venv in the copy: the precheck never creates it
    assert not (unchanged / ".venv").exists()


@needs_uv
def test_dry_run_render_reports_would_update(unchanged: Path) -> None:
    ruff = unchanged / ".ruff.toml"
    original = ruff.read_bytes()
    ruff.write_bytes(original + b"# hand edit\n")
    try:
        r = _pyt(unchanged, "--dry-run", "render", "--force")
        assert r.returncode == 0, r.stderr
        assert "would update: .ruff.toml" in r.stderr
        assert ruff.read_bytes() == original + b"# hand edit\n"
    finally:
        ruff.write_bytes(original)


@needs_uv
def test_dry_run_new_copies_nothing(unchanged: Path, tmp_path: Path) -> None:
    r = _pyt(unchanged, "--dry-run", "new", "p1", "--preset", "raylib", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert str(tmp_path / "p1") in r.stderr and "raylib" in r.stderr
    assert not (tmp_path / "p1").exists()


# In-process checks: they fail before doing anything, and DRY_RUN is on in case they did not.


@pytest.fixture
def dry(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setattr(proc, "DRY_RUN", True)
    return config.load(set())


@pytest.mark.parametrize(
    ("command", "args", "message"),
    [
        ("cmd_render", ["--bogus"], "unknown argument(s): --bogus"),
        ("cmd_render", ["extra"], "unknown argument(s): extra"),
        ("cmd_mode", ["mypyc", "--bogus"], "unknown argument(s): --bogus"),
        ("cmd_mode", ["--supports"], "--supports needs a value"),
        ("cmd_mode", ["--supports="], "--supports needs a value"),
        ("cmd_mode", ["--supports", "-cpython,-pypy,-mypyc"], "at least one backend"),  # every preset's backends
        ("cmd_mode", ["--supports", "+pypy,cpython"], "mixes changes (+name, -name) with plain names"),
        ("cmd_init", ["raylib", "--bogus"], "unknown argument(s): --bogus"),
        ("cmd_new", ["somewhere", "--bogus"], "unknown argument(s): --bogus"),
    ],
)
def test_bad_arguments_are_clear_errors(dry: Config, command: str, args: list[str], message: str) -> None:
    with pytest.raises(PytError) as e:
        getattr(cmd_mode, command)(dry, args)
    assert message in str(e.value)
    assert e.value.code == 2


def test_new_checks_the_app_name_before_copying(dry: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PytError, match="--name NAME"):
        cmd_mode.cmd_new(dry, ["1game"])
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("", encoding="utf-8")
    with pytest.raises(PytError, match="not empty"):
        cmd_mode.cmd_new(dry, ["full"])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["full"]


def test_dry_run_pyz_merge(dry: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    parts = [_fake_pyz(tmp_path / "in" / f"{k}.pyz", key) for k, key in (("a", "cp314-windows-x86_64"), ("b", "cp314-linux-x86_64"))]
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert cmd_build.cmd_pyz_merge(dry, ["../in/a.pyz", "../in/b.pyz", "--out", "out/c.pyz"]) == 0
    err = capsys.readouterr().err
    shown = [line.split(maxsplit=1) for line in err.splitlines() if line.startswith(("  in ", "  out "))]
    assert [kind for kind, _ in shown] == ["in", "in", "out", "out"], err
    # Compared resolved: on POSIX user_path keeps `..` for the OS to resolve (symlinked folders)
    assert [Path(p).resolve() for _, p in shown] == [p.resolve() for p in (*parts, work / "out" / "c.pyz", work / "out" / "c.cmd")]
    assert not (work / "out").exists()


# --- pyz-merge with the user's paths -------------------------------------------------------------


def _fake_pyz(path: Path, key: str) -> Path:
    """A minimal .pyz as methods/pyz.py builds it: _pyz.json + common/ + targets/<key>/."""
    src = path.parent / f"{path.stem}-src"
    (src / "common" / "app").mkdir(parents=True)
    (src / "targets" / key / "lib").mkdir(parents=True)
    (src / "__main__.py").write_text("print('demo')\n", encoding="utf-8")
    (src / "common" / "app" / "demo.py").write_text("X = 1\n", encoding="utf-8")
    (src / "targets" / key / "lib" / "native.txt").write_text(key, encoding="utf-8")
    info = {"name": "demo", "build_id": "x", "min_python": [3, 11], "targets": [key], "pure": False, "backend": "cpython"}
    (src / "_pyz.json").write_text(json.dumps(info), encoding="utf-8")
    zipapp.create_archive(src, path)
    return path


def test_pyz_merge_resolves_paths_against_the_callers_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import zipfile

    _fake_pyz(tmp_path / "in" / "a.pyz", "cp314-windows-x86_64")
    _fake_pyz(tmp_path / "in" / "b.pyz", "cp314-linux-x86_64")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("PYTEMPLATE_CALLER_CWD", str(work))
    cfg: Config = config._build(Config, {}, "")
    assert cmd_build.cmd_pyz_merge(cfg, ["../in/a.pyz", "../in/b.pyz", "--out", "out/c.pyz"]) == 0
    with zipfile.ZipFile(work / "out" / "c.pyz") as archive:
        info = json.loads(archive.read("_pyz.json"))
        names = set(archive.namelist())
    assert info["targets"] == ["cp314-linux-x86_64", "cp314-windows-x86_64"]
    assert {"targets/cp314-linux-x86_64/lib/native.txt", "targets/cp314-windows-x86_64/lib/native.txt"} <= names
    with pytest.raises(PytError, match="not found"):
        cmd_build.cmd_pyz_merge(cfg, ["../in/a.pyz", "../in/missing.pyz", "--out", "x.pyz"])
    with pytest.raises(PytError, match="not a .pyz"):
        cmd_build.cmd_pyz_merge(cfg, ["../in/a.pyz", "../in/a-src/_pyz.json", "--out", "x.pyz"])
    with pytest.raises(PytError, match="at least two"):
        cmd_build.cmd_pyz_merge(cfg, ["../in/a.pyz", "--out", "x.pyz"])
    assert not (work / "x.pyz").exists()
