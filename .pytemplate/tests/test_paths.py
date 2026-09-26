"""User paths, console colors and --dry-run (run them with `./deploy selftest`).

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
import zipapp
from collections.abc import Iterator
from pathlib import Path

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_DIR))

from runner import cmd_build, cmd_mode, config, presets, proc, project, ui  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import caller_cwd, native_path, user_path  # noqa: E402
from runner.ui import DeployError  # noqa: E402

windows = pytest.mark.skipif(os.name != "nt", reason="Windows path rules")
posix = pytest.mark.skipif(os.name == "nt", reason="POSIX path rules")
_LAUNCHER_VARS = ("PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No launcher variables leak in from the shell that started the selftest."""
    for name in _LAUNCHER_VARS:
        monkeypatch.delenv(name, raising=False)


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
        with pytest.raises(DeployError, match="empty path"):
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
    assert not calls, "ui.py must not start a shell (os.system('') ran cmd.exe on every ./deploy)"


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


def _deploy(root: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in (*_LAUNCHER_VARS, "VIRTUAL_ENV")}
    env.update(NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return subprocess.run(
        [sys.executable, "-B", str(root / ".pytemplate" / "deploy.py"), *args],
        cwd=cwd or root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


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
        (["__init", "raylib"], "+ typings/raylib/__init__.pyi"),
        (["__init", "flet", "--name", "other"], "as 'other'"),
        (["render", "--force"], "generated files up to date"),
    ],
)
def test_dry_run_writes_nothing(unchanged: Path, args: list[str], expected: str) -> None:
    r = _deploy(unchanged, "--dry-run", *args)
    assert r.returncode == 0, r.stderr
    assert expected in r.stderr, r.stderr


@needs_uv
def test_dry_run_mode_supports_pypy(unchanged: Path) -> None:
    r = _deploy(unchanged, "--dry-run", "mode", "--supports", "+pypy")
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
        r = _deploy(unchanged, "--dry-run", "render", "--force")
        assert r.returncode == 0, r.stderr
        assert "would update: .ruff.toml" in r.stderr
        assert ruff.read_bytes() == original + b"# hand edit\n"
    finally:
        ruff.write_bytes(original)


@needs_uv
def test_dry_run_new_copies_nothing(unchanged: Path, tmp_path: Path) -> None:
    r = _deploy(unchanged, "--dry-run", "new", "p1", "--preset", "raylib", cwd=tmp_path)
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
        ("cmd_mode", ["--supports", "-cpython,-mypyc"], "at least one backend"),
        ("cmd_mode", ["--supports", "+pypy,cpython"], "mixes changes (+name, -name) with plain names"),
        ("cmd_init", ["raylib", "--bogus"], "unknown argument(s): --bogus"),
        ("cmd_new", ["somewhere", "--bogus"], "unknown argument(s): --bogus"),
    ],
)
def test_bad_arguments_are_clear_errors(dry: Config, command: str, args: list[str], message: str) -> None:
    with pytest.raises(DeployError) as e:
        getattr(cmd_mode, command)(dry, args)
    assert message in str(e.value)
    assert e.value.code == 2


def test_new_checks_the_app_name_before_copying(dry: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DeployError, match="--name NAME"):
        cmd_mode.cmd_new(dry, ["1game"])
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("", encoding="utf-8")
    with pytest.raises(DeployError, match="not empty"):
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
    with pytest.raises(DeployError, match="not found"):
        cmd_build.cmd_pyz_merge(cfg, ["../in/a.pyz", "../in/missing.pyz", "--out", "x.pyz"])
    with pytest.raises(DeployError, match="not a .pyz"):
        cmd_build.cmd_pyz_merge(cfg, ["../in/a.pyz", "../in/a-src/_pyz.json", "--out", "x.pyz"])
    with pytest.raises(DeployError, match="at least two"):
        cmd_build.cmd_pyz_merge(cfg, ["../in/a.pyz", "--out", "x.pyz"])
    assert not (work / "x.pyz").exists()
