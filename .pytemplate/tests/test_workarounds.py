"""Workarounds of dependency defects and limitations (CLAUDE.md section 15.1) that no other test
covered. Each test fails when its workaround is removed; the `pin` tests also fail when the
upstream behaviour changes (then the workaround may go). Fast and hermetic: fakes, no network."""

from __future__ import annotations

import ast
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_build, cmd_env, config, e2e, envs, lintc, mypyc, proc, shells, upx  # noqa: E402
from runner.cmd_build import BuildRequest  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.methods import common, nuitka, portable  # noqa: E402
from runner.project import ROOT  # noqa: E402
from runner.ui import DeployError  # noqa: E402

PRESETS = ROOT / ".pytemplate" / "presets"
PLUGIN = ROOT / ".pytemplate" / "nvim"


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


def _preset_toml(preset: str) -> dict[str, Any]:
    text = (PRESETS / preset / "files" / "pytemplate.toml").read_text(encoding="utf-8")
    return tomllib.loads(text.replace("{{name}}", "demo").replace("{{pkg}}", "demo"))


# --- Nuitka + Flet: lazy imports and the client flet-desktop does not ship ----------------------


class FakeUv:
    """envs.uv stand-in: answers flet_desktop's archive query, records the Nuitka call and writes
    the standalone layout Nuitka 4.2.2 produces (<output-dir>/main.dist/<output-filename>)."""

    ARCHIVE = "flet-linux-amd64.tar.gz"
    VERSION = "1.0.1"

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.queries = 0

    def __call__(self, env: object, argv: Any, *, cwd: Path | None = None, **_: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        if "--with" not in args:  # `uv run --locked python -c <flet_desktop query>`
            self.queries += 1
            assert "flet_desktop.get_artifact_filename()" in args[-1]
            return subprocess.CompletedProcess(args, 0, f"{self.ARCHIVE} {self.VERSION}\n", "")
        self.argv = args
        out = Path(next(a for a in args if a.startswith("--output-dir=")).split("=", 1)[1])
        name = os.path.basename(next(a for a in args if a.startswith("--output-filename=")).split("=", 1)[1])
        (out / "main.dist").mkdir(parents=True)
        (out / "main.dist" / name).write_bytes(b"binary")
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def build_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """BUILD and DIST under tmp_path (never the project's own)."""
    monkeypatch.setattr(nuitka, "BUILD", tmp_path / "build")
    monkeypatch.setattr(common, "BUILD", tmp_path / "build")
    monkeypatch.setattr(cmd_build, "DIST", tmp_path / "dist")
    yield tmp_path


def _payload(root: Path, pkg: str) -> Path:
    (root / pkg).mkdir(parents=True)
    (root / "main.py").write_text(f"from {pkg}.ui import app\n", encoding="utf-8")
    (root / pkg / "__init__.py").write_text("", encoding="utf-8")
    return root


def _client_archive(zip_format: bool = False) -> bytes:
    """A small Flet client archive as flet_desktop extracts it (flet-linux-*.tar.gz, or
    flet-windows.zip); incompressible, so that a cut lands in the middle of the data."""
    import random
    import tarfile
    import zipfile

    files = {"flet/flet": random.Random(1).randbytes(60_000), "flet/lib/libapp.so": random.Random(2).randbytes(90_000)}
    buffer = io.BytesIO()
    if zip_format:
        with zipfile.ZipFile(buffer, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz") as t:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _http_response(body: bytes, *, announce: int | None = None, chunked: bool = False, cut: bool = False) -> Any:
    """A REAL http.client response read from bytes: what urlopen returns when the server sends
    `body`, announcing `announce` bytes (Content-Length) or chunked, and with `cut` closes the
    connection before the last chunk."""
    import http.client

    if chunked:
        raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + f"{announce or len(body):x}\r\n".encode() + body
        raw += b"" if cut else b"\r\n0\r\n\r\n"
    else:
        raw = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body) if announce is None else announce}\r\n\r\n".encode() + body

    class Socket:
        def makefile(self, mode: str) -> io.BytesIO:
            return io.BytesIO(raw)

    response = http.client.HTTPResponse(Socket())  # type: ignore[arg-type]
    response.begin()
    return response


def test_nuitka_bundles_the_flet_client(build_dirs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nuitka cannot follow flet's lazy controls (module __getattr__ + importlib), and the
    # flet-desktop wheel has no Flutter client (the app would download it at its first start):
    # both packages are included and the release archive goes where flet_desktop looks for it.
    cfg = make({"app": {"name": "demo", "preset": "flet", "gui": True}})
    app = _payload(build_dirs / "payload", cfg.pkg)
    fake = FakeUv()
    urls: list[str] = []
    client = _client_archive()

    def urlopen(url: str, timeout: float = 0) -> Any:
        urls.append(url)
        return _http_response(client)

    monkeypatch.delenv("FLET_CLIENT_URL", raising=False)
    monkeypatch.setattr(nuitka, "IS_WINDOWS", False)
    monkeypatch.setattr(envs, "uv", fake)
    monkeypatch.setattr(nuitka.urllib.request, "urlopen", urlopen)
    nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))
    archive = build_dirs / "build" / "flet-client" / FakeUv.VERSION / FakeUv.ARCHIVE
    assert urls == [f"https://github.com/flet-dev/flet/releases/download/v{FakeUv.VERSION}/{FakeUv.ARCHIVE}"]
    assert archive.read_bytes() == client
    assert "--include-package=flet" in fake.argv and "--include-package=flet_desktop" in fake.argv
    assert f"--include-data-files={archive}=flet_desktop/app/{FakeUv.ARCHIVE}" in fake.argv
    # ft.Icons reads icons.json through importlib.resources: without it the app died at its
    # first icon (FileNotFoundError), and Nuitka bundles no package data by default
    assert "--include-package-data=flet.controls.material:icons.json" in fake.argv
    assert "--include-package-data=flet.controls.cupertino:cupertino_icons.json" in fake.argv
    # Downloaded once: the next build takes the cached archive
    nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))
    assert len(urls) == 1 and fake.queries == 2
    # A failed download is a missing requirement and leaves nothing that looks cached
    archive.unlink()

    def offline(url: str, timeout: float = 0) -> io.BytesIO:
        raise OSError("no network")

    monkeypatch.setattr(nuitka.urllib.request, "urlopen", offline)
    with pytest.raises(DeployError) as info:
        nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))
    assert info.value.code == 3 and "cannot download the Flet client" in str(info.value)
    assert list(archive.parent.iterdir()) == []


def _serve_client(monkeypatch: pytest.MonkeyPatch, responses: list[Any], archive: str = FakeUv.ARCHIVE) -> list[str]:
    """envs.uv answers flet_desktop's query with `archive`; urlopen hands out `responses` in order."""
    fake = FakeUv()
    fake.ARCHIVE = archive
    urls: list[str] = []

    def urlopen(url: str, timeout: float = 0) -> Any:
        urls.append(url)
        return responses.pop(0)

    monkeypatch.delenv("FLET_CLIENT_URL", raising=False)
    monkeypatch.setattr(envs, "uv", fake)
    monkeypatch.setattr(nuitka.urllib.request, "urlopen", urlopen)
    return urls


@pytest.mark.parametrize("case", ["cut", "chunked cut", "not an archive", "damaged zip"])
def test_a_flet_client_download_cut_short_is_never_cached(build_dirs: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    # http.client ends a body cut short (the connection closed, a ragged TLS end) like a whole
    # one when the server announced its length: the short file was cached as the client, every
    # later build bundled it and reported success, and the app failed at its first start.
    # Chunked, the same cut raised IncompleteRead, which is no OSError: a runner traceback.
    zip_format = case == "damaged zip"
    client = _client_archive(zip_format)
    name = "flet-windows.zip" if zip_format else FakeUv.ARCHIVE
    if case == "cut":
        bad = _http_response(client[: len(client) // 2], announce=len(client))
        message = f"ended after {len(client) // 2} of {len(client)} bytes"
    elif case == "chunked cut":
        bad = _http_response(client[: len(client) // 2], announce=len(client), chunked=True, cut=True)
        message = "cannot download the Flet client"
    elif case == "not an archive":
        bad = _http_response(b"<html>a proxy's error page</html>")
        message = "not a whole archive"
    else:
        damaged = bytearray(client)
        damaged[len(damaged) // 3] ^= 0xFF  # same length, one byte of a member changed
        bad = _http_response(bytes(damaged))
        message = "not a whole archive"
    urls = _serve_client(monkeypatch, [bad, _http_response(client)], name)
    with pytest.raises(DeployError, match=message) as info:
        nuitka._flet_client_archive(make({"app": {"preset": "flet"}}))
    assert info.value.code == 3
    folder = build_dirs / "build" / "flet-client" / FakeUv.VERSION
    assert list(folder.iterdir()) == []  # neither the archive nor a .part
    # The next build downloads it again, whole
    archive = nuitka._flet_client_archive(make({"app": {"preset": "flet"}}))
    assert archive == folder / name and archive.read_bytes() == client and len(urls) == 2


def test_a_damaged_cached_flet_client_is_downloaded_again(build_dirs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # A short archive an older runner cached stayed in .build/flet-client/ for every build
    client = _client_archive()
    urls = _serve_client(monkeypatch, [_http_response(client)])
    cached = build_dirs / "build" / "flet-client" / FakeUv.VERSION / FakeUv.ARCHIVE
    cached.parent.mkdir(parents=True)
    cached.write_bytes(client[:1000])
    assert nuitka.archive_problem(cached)
    assert nuitka._flet_client_archive(make({"app": {"preset": "flet"}})) == cached
    assert cached.read_bytes() == client and len(urls) == 1 and nuitka.archive_problem(cached) == ""
    assert "is damaged" in capsys.readouterr().err
    # A whole cached archive is used as it is
    assert nuitka._flet_client_archive(make({"app": {"preset": "flet"}})) == cached and len(urls) == 1


def test_the_flet_client_follows_flet_client_url(build_dirs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # flet_desktop downloads its client from FLET_CLIENT_URL when it is set (a mirror behind a
    # firewall): flet run and flet pack honoured it, the Nuitka build went to github.com anyway
    client = _client_archive()
    urls = _serve_client(monkeypatch, [_http_response(client)])
    monkeypatch.setenv("FLET_CLIENT_URL", "https://mirror.example/flet-linux.tar.gz")
    archive = nuitka._flet_client_archive(make({"app": {"preset": "flet"}}))
    assert urls == ["https://mirror.example/flet-linux.tar.gz"]
    assert archive.name == FakeUv.ARCHIVE and archive.read_bytes() == client  # cached under flet_desktop's own name
    assert "(FLET_CLIENT_URL)" in capsys.readouterr().err


def test_nuitka_includes_flet_only_for_the_flet_preset(build_dirs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({})
    fake = FakeUv()
    monkeypatch.setattr(nuitka, "IS_WINDOWS", False)
    monkeypatch.setattr(envs, "uv", fake)
    nuitka.build(BuildRequest(cfg, "cpython", "nuitka", _payload(build_dirs / "payload", cfg.pkg)))
    assert fake.queries == 0 and "--include-package=myapp" in fake.argv
    assert not [a for a in fake.argv if a.startswith("--include-package=flet") or "flet_desktop/app/" in a]


# --- PyPy: the Windows zip lacks the VC++ runtime ---------------------------------------------------


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


def test_portable_pypy_on_windows_gets_the_vc_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A bundled PyPy portable folder did not start on a Windows machine without the VC++
    # redistributable: PyPy's zip does not ship vcruntime140*.dll, uv's CPython does.
    pypy_base = _tree(tmp_path / "pypy", {"pypy.exe": b"", "python.exe": b"", "libpypy3.11-c.dll": b"", "vcruntime140.dll": b"pypy's own", "Lib/os.py": b""})
    cpython_base = _tree(tmp_path / "cpython", {"python.exe": b"", "vcruntime140.dll": b"cpython 140", "vcruntime140_1.dll": b"cpython 140_1"})

    def info(python: Path) -> dict[str, str]:
        pypy = "-pypy" in str(python)
        return {"base_prefix": str(pypy_base if pypy else cpython_base), "version": "3.11.15" if pypy else "3.14.7", "impl": "pypy" if pypy else "cpython"}

    monkeypatch.setattr(portable, "IS_WINDOWS", True)
    monkeypatch.setattr(portable, "long_path", lambda p: str(p.resolve()))  # no \\?\ prefix on this host
    monkeypatch.setattr(envs, "interpreter_info", info)
    monkeypatch.setattr(common, "ensure_env", lambda env: env)
    monkeypatch.setattr(common, "SRC", tmp_path / "src")
    (tmp_path / "src").mkdir()
    (tmp_path / "lib").mkdir()
    cfg = make({"backend": {"active": "pypy", "supported": ["cpython", "pypy"]}})
    dest = tmp_path / "out" / "runtime"
    assert portable.copy_runtime(cfg, "pypy", dest, lib=tmp_path / "lib") == dest / "python.exe"
    assert (dest / "vcruntime140_1.dll").read_bytes() == b"cpython 140_1"  # the missing one comes from CPython
    assert (dest / "vcruntime140.dll").read_bytes() == b"pypy's own"  # one PyPy ships is never replaced
    # Only PyPy runtimes get it: a CPython base is copied as it is
    bare = _tree(tmp_path / "bare", {"python.exe": b""})
    monkeypatch.setattr(envs, "interpreter_info", lambda python: {"base_prefix": str(bare), "version": "3.14.7", "impl": "cpython"})
    other = tmp_path / "out2" / "runtime"
    portable.copy_runtime(make({}), "cpython", other, lib=tmp_path / "lib")
    assert sorted(p.name for p in other.iterdir()) == ["python.exe"]


# --- PyPy: no wheels for the CPython tools of the dev group -----------------------------------------


def test_cpython_only_dev_tools_carry_the_marker() -> None:
    # mypy needs the Rust ast-serialize and PyInstaller does not run on PyPy: without the marker
    # .venv-pypy could not be synced. pytest is the one dev tool the PyPy environment needs.
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = {re.split(r"[<>=!~;\s\[]", req, maxsplit=1)[0].lower(): req for req in data["dependency-groups"]["dev"]}
    for tool in ("debugpy", "mypy", "pyinstaller", "ruff", "setuptools"):
        assert "implementation_name == 'cpython'" in dev[tool], dev[tool]
    assert ";" not in dev["pytest"]


# --- PyInstaller (and flet pack): processes in a frozen app, what analysis drags in ------------------


def _main_block(tree: ast.Module) -> list[ast.stmt]:
    for node in tree.body:
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test):
            return node.body
    raise AssertionError("no `if __name__ == '__main__':` block")


@pytest.mark.parametrize("preset", ["script", "raylib", "flet"])
def test_every_preset_main_calls_freeze_support_first(preset: str) -> None:
    # In a frozen executable a ProcessPoolExecutor child starts the executable again: without
    # multiprocessing.freeze_support() before anything else runs, it ran the app once more.
    source = (PRESETS / preset / "files" / "src" / "main.py").read_text(encoding="utf-8").replace("{{pkg}}", "demo")
    statements = [ast.unparse(s) for s in _main_block(ast.parse(source)) if not isinstance(s, (ast.Import, ast.ImportFrom))]
    assert statements and statements[0] == "multiprocessing.freeze_support()", statements


def test_presets_exclude_what_the_packagers_drag_in() -> None:
    # PyInstaller and Nuitka follow imports inside functions: flet's lazy `from PIL import ...`
    # bundled Pillow (13 MB), and cffi's build-time imports bundled setuptools and pycparser.
    assert "PIL" in _preset_toml("flet")["deploy"]["exclude_modules"]
    extra = _preset_toml("raylib")["deploy"]["exe"]["extra_args"]
    excluded = {extra[i + 1] for i, arg in enumerate(extra[:-1]) if arg == "--exclude-module"}
    assert {"setuptools", "pycparser", "_distutils_hack"} <= excluded


def test_flet_skeleton_runs_compiled_work_in_a_process() -> None:
    # Compiled code never releases the GIL: a heavy compiled call froze the Flet UI from a
    # thread just as from the event loop, so the skeleton hands it to a worker process.
    source = (PRESETS / "flet" / "files" / "src" / "__pkg__" / "ui" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source.replace("{{pkg}}", "demo").replace("{{name}}", "demo"))
    executor = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_executor")
    made = [ast.unparse(n.value.func) for n in ast.walk(executor) if isinstance(n, ast.Return) and isinstance(n.value, ast.Call)]
    assert made == ["ProcessPoolExecutor"]
    handed = [n.args for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "run_in_executor"]
    assert handed and all(ast.unparse(a[0]) == "_executor()" and ast.unparse(a[1]).startswith("fractal.") for a in handed)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"ThreadPoolExecutor", "Thread", "to_thread"}


# --- MSVC / Visual Studio ------------------------------------------------------------------------------


def test_base_env_puts_the_vs_installer_on_path_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # VS 2026's vcvarsall.bat runs vswhere.exe by its bare name: outside a developer prompt
    # setuptools then failed with "Unable to find a compatible Visual Studio installation".
    installer = tmp_path / "Microsoft Visual Studio" / "Installer"
    monkeypatch.setattr(proc, "IS_WINDOWS", True)
    monkeypatch.setattr(proc, "vs_installer_dir", lambda: installer)
    monkeypatch.setenv("PATH", os.pathsep.join(["/a", "/b"]))
    assert proc.base_env()["PATH"].split(os.pathsep)[-1] == str(installer)
    monkeypatch.setenv("PATH", os.pathsep.join([str(installer), "/a"]))
    assert proc.base_env()["PATH"].split(os.pathsep).count(str(installer)) == 1  # never twice
    monkeypatch.setattr(proc, "vs_installer_dir", lambda: None)  # no Visual Studio installer
    monkeypatch.setenv("PATH", "/a")
    assert proc.base_env()["PATH"] == "/a"
    monkeypatch.setattr(proc, "IS_WINDOWS", False)
    monkeypatch.setattr(proc, "vs_installer_dir", lambda: installer)
    assert proc.base_env()["PATH"] == "/a"  # only Windows


def test_mypyc_build_asks_msvc_for_english_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # MSVC prints in the system language, which reached the terminal as unreadable cp1252 text
    src = _tree(tmp_path / "src", {"main.py": b"", "myapp/__init__.py": b"", "myapp/core/__init__.py": b"", "myapp/core/m.py": b"X = 1\n"})
    for module in (mypyc, config, lintc):
        monkeypatch.setattr(module, "SRC", src)
    monkeypatch.setattr(mypyc, "BUILD", tmp_path / ".build")
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    seen: list[dict[str, str]] = []

    def run(argv: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        if any(str(a).endswith("mypyc_build.py") for a in argv):  # the compile step
            seen.append(kw["env"])
        return subprocess.CompletedProcess([str(a) for a in argv], mypyc.MYPYC_REJECTED, "", "")

    monkeypatch.setattr(mypyc.proc, "run", run)
    with pytest.raises(DeployError):  # the fake compiler rejects the code: the env is what counts
        mypyc.build(make({}), "dev")
    assert len(seen) == 1 and seen[0].get("VSLANG") == "1033"


# --- UPX ---------------------------------------------------------------------------------------------------


def test_upx_is_off_on_macos_and_windows_arm64_runs_the_x64_build(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # UPX cannot pack current macOS binaries (and packing breaks their code signature); there is
    # no Windows arm64 release, and the x64 build runs there emulated.
    monkeypatch.setattr(upx, "IS_MACOS", True)
    assert "macOS" in upx.unsupported_reason()
    assert upx.active(make({"deploy": {"upx": {"enabled": True}}})) is False
    assert "deploy.upx.enabled is ignored" in capsys.readouterr().err
    assert upx.ASSETS[("windows", "aarch64")] == upx.ASSETS[("windows", "x86_64")]


# --- GitHub Actions: xvfb-run's default screen has no GLX visuals; contexts only where allowed ---------


def test_xvfb_screen_has_24_bit_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    # xvfb-run starts an 8-bit screen by default, where GLX has no visuals: raylib and Flet could
    # not open a window on headless Linux.
    monkeypatch.setattr(e2e, "host_os", lambda: "linux")
    monkeypatch.setattr(e2e.shutil, "which", lambda name, *a, **k: "/usr/bin/xvfb-run" if name == "xvfb-run" else None)
    for var in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    wrap = e2e.detect_host("on").gui_wrap
    assert wrap and wrap[0] == "/usr/bin/xvfb-run"
    assert any(re.fullmatch(r"-screen \d+ \d+x\d+x24", arg) for arg in wrap), wrap


def test_template_workflows_use_contexts_where_actions_allows_them() -> None:
    # The runner context exists only at step level: `${{ runner.temp }}` in a job's env makes
    # GitHub reject the whole workflow file. actionlint knows where each context is available
    # (shellcheck and pyflakes off: this is about the workflows, not the scripts in them).
    workflows = sorted((ROOT / ".github" / "workflows").glob("template-*.yml"))
    if not workflows:
        pytest.skip("no template workflows here (a project made with ./deploy new)")
    actionlint = shutil.which("actionlint")
    if actionlint is None:
        pytest.skip("actionlint is not installed")
    argv = [actionlint, "-no-color", "-shellcheck=", "-pyflakes=", *(str(p) for p in workflows)]
    r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


# --- WSL: `wsl -l -q` prints UTF-16 -------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-8"])
def test_wsl_distros_are_read_as_utf16(monkeypatch: pytest.MonkeyPatch, encoding: str) -> None:
    listing = "Ubuntu-24.04\r\ndocker-desktop\r\nDebian\r\n".encode(encoding)
    monkeypatch.setattr(shells.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, listing, b""))
    assert shells.wsl_distros("wsl.exe") == ["Ubuntu-24.04", "Debian"]  # docker-desktop is no shell


# --- Python 3.11: Path.is_symlink() is False for a Windows junction ----------------------------------------


def test_a_windows_junction_counts_as_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `clean --envs` on a junctioned .venv must remove the junction, never what it points to;
    # Python 3.11 has no Path.is_junction, so the reparse tag is read.
    real_lstat = os.lstat
    junction = tmp_path / ".venv"
    junction.mkdir()

    def lstat(path: Any, *a: Any, **k: Any) -> Any:
        if Path(path) == junction:
            return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_reparse_tag=cmd_env._JUNCTION)
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(cmd_env.os, "lstat", lstat)
    assert cmd_env._is_link(junction) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cmd_env._is_link(plain) is False
    assert cmd_env._is_link(tmp_path / "missing") is False


# --- ruff: `format --check` on stdin says nothing (pin) ------------------------------------------------------


def _venv_ruff() -> str | None:
    return shutil.which("ruff", path=str(Path(sys.executable).parent))


def test_ruff_format_check_says_nothing_for_stdin(tmp_path: Path) -> None:
    """Pin: the hook writes its own "Would reformat: <path> (its staged version)" line because
    ruff prints nothing for an unformatted stdin. When this fails, that line can go."""
    ruff = _venv_ruff()
    if ruff is None:
        pytest.skip("ruff is not in this Python's environment (./deploy setup)")
    r = subprocess.run(
        [ruff, "format", "--check", "--no-cache", "--stdin-filename", "src/a.py", "-"],
        input="x=1\n", capture_output=True, text=True, cwd=tmp_path, timeout=60, check=False,
    )  # fmt: skip
    assert r.returncode == 1 and r.stdout.strip() == "" and r.stderr.strip() == "", r


# --- Neovim plugin: nvim-dap, neotest-python, overseer, nvim-lint, LazyVim's server switch ----------------

PLUGIN_CHECKS = r"""
vim.opt.rtp:prepend(vim.env.PT_PLUGIN)
local root = vim.env.PT_TEST_ROOT
local out = {}
local function check(name, ok, msg)
  out[#out + 1] = (ok and "PTCHECK ok " or "PTCHECK FAIL ") .. name .. (ok and "" or ("  " .. tostring(msg):gsub("\n", " ")))
end
local function run(name, fn)
  local ok, err = pcall(fn)
  if not ok then check(name, false, err) end
end
package.loaded["lint.linters.mypy"] = { parser = function() return {} end }
local pt = require("pytemplate")
pt.config.root = root
local integ = require("pytemplate.integrations")

-- overseer: the .vscode/tasks.json provider would duplicate every label and run deploy.cmd
run("overseer", function()
  local opts = { disable_template_modules = { "mine" } }
  integ.overseer(nil, opts)
  integ.overseer(nil, opts)
  local n = #vim.tbl_filter(function(m) return m == "overseer.template.vscode" end, opts.disable_template_modules)
  check("overseer", n == 1 and opts.disable_template_modules[1] == "mine", vim.inspect(opts))
end)

-- neotest-python: its pyvenv.cfg glob matched .venv and .venv-pypy and built a broken path
run("neotest", function()
  local opts = {}
  integ.neotest(nil, opts)
  local a = opts.adapters["neotest-python"]
  local py = type(a.python) == "function" and a.python() or nil
  local f = opts.discovery.filter_dir
  local ok = type(py) == "table" and type(py[1]) == "string"
    and not f(".venv", ".venv", root) and not f(".venv-pypy", ".venv-pypy", root)
    and not f("dist", "dist", root) and not f("typings", "typings", root)
    and f("tests", "tests", root) and f("src", "src", root)
  local mine = { adapters = { { name = "an adapter object" } } }
  integ.neotest(nil, mine)
  check("neotest", ok and mine.discovery == nil, vim.inspect({ py = py }))
end)

-- nvim-dap: a cold adapter needs more than the default 4 s; a configuration's own value stays
run("dap", function()
  package.loaded["dap-python"] = { setup = function() end }
  local dap = { providers = { configs = {} } }
  dap.adapters = {
    python = function(cb, config)
      cb({ type = "executable", command = "python", args = { "-m", "debugpy.adapter" }, options = config.options })
    end,
  }
  package.loaded["dap"] = dap
  require("pytemplate.dap").setup()
  local got, kept
  dap.adapters.python(function(a) got = a end, {})
  dap.adapters.python(function(a) kept = a end, { options = { initialize_timeout_sec = 5 } })
  check("dap", got.options.initialize_timeout_sec == 30 and kept.options.initialize_timeout_sec == 5
    and dap.adapters.debugpy == dap.adapters.python, vim.inspect({ got, kept }))
end)

-- nvim-dap spawns adapters without PATHEXT: the adapter command is always an absolute path
run("dap adapter", function()
  local cmd = require("pytemplate.dap").adapter_cmd()
  local first = cmd and cmd[1] or ""
  check("dap adapter", cmd == nil or first:sub(1, 1) == "/" or first:match("^%a:[\\/]") ~= nil, vim.inspect(cmd))
end)

-- nvim-dap expands ${workspaceFolder} to Neovim's cwd: from src/ it must still be the root
run("dap subfolder", function()
  package.loaded["dap.ext.vscode"] = {
    getconfigs = function()
      return { { name = "x", program = "${workspaceFolder}/src/main.py", cwd = "${workspaceFolder}" } }
    end,
  }
  local before = vim.uv.cwd()
  vim.fn.chdir(root .. "/src")
  local cfgs = require("pytemplate.dap").launch_configs()
  vim.fn.chdir(before)
  check("dap subfolder", #cfgs == 1 and cfgs[1].program == root .. "/src/main.py" and cfgs[1].cwd == root, vim.inspect(cfgs))
end)

-- nvim-lint REPLACES the environment of a linter that has `env`
run("mypy env", function()
  vim.env.VIRTUAL_ENV = "/somewhere/else"
  vim.env.PT_KEEP_ME = "1"
  local env = integ.mypy_linter().env
  check("mypy env", env.PYTHONUTF8 == "1" and env.VIRTUAL_ENV == nil and env.PT_KEEP_ME == "1"
    and (env.PATH or env.Path) ~= nil, vim.inspect(env))
end)

-- LazyVim reads vim.g.lazyvim_python_lsp at its first import: the servers are switched in opts
run("lsp", function()
  vim.g.pytemplate_python_lsp = nil
  local opts = { servers = { pyright = { enabled = true } } }
  integ.lsp(nil, opts)
  local based = opts.servers.basedpyright.enabled == true and opts.servers.pyright.enabled == false
  vim.g.pytemplate_python_lsp = "pyright"
  local other = {}
  integ.lsp(nil, other)
  vim.g.pytemplate_python_lsp = nil
  local py = other.servers.pyright.enabled == true and other.servers.basedpyright.enabled == false
  check("lsp", based and py and opts.servers.ruff.enabled == true and opts.servers.ruff_lsp.enabled == false,
    vim.inspect({ opts, other }))
end)

io.stdout:write(table.concat(out, "\n") .. "\nPTLUA DONE\n")
vim.cmd("qa!")
"""

PLUGIN_CHECK_NAMES = ["overseer", "neotest", "dap", "dap adapter", "dap subfolder", "mypy env", "lsp"]


def _nvim() -> str | None:
    exe = shutil.which("nvim")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"NVIM v(\d+)\.(\d+)", out)
    return exe if m and (int(m[1]), int(m[2])) >= (0, 10) else None


@pytest.fixture(scope="module")
def plugin_results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Run PLUGIN_CHECKS once in `nvim --headless --clean`, isolated from the user's Neovim."""
    exe = _nvim()
    if not exe:
        pytest.skip("Neovim >= 0.10 not on PATH")
    tmp = tmp_path_factory.mktemp("nvim")
    script = tmp / "checks.lua"
    script.write_text(PLUGIN_CHECKS, encoding="utf-8", newline="\n")
    env = {k: v for k, v in os.environ.items() if k != "NVIM_APPNAME"}
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        env[var] = str(tmp / var.lower())
    env.update(NVIM_LOG_FILE=str(tmp / "nvim.log"), PT_TEST_ROOT=ROOT.as_posix(), PT_PLUGIN=PLUGIN.as_posix())
    r = subprocess.run(
        [exe, "--headless", "--clean", "-n", "-i", "NONE", "-c", f"luafile {script.as_posix()}"],
        cwd=tmp, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
    )  # fmt: skip
    assert "PTLUA DONE" in r.stdout, r.stdout + r.stderr
    results: dict[str, str] = {}
    for line in r.stdout.splitlines():
        m = re.match(r"PTCHECK (ok|FAIL) (.+?)(?:  (.*))?$", line)
        if m:
            results[m[2]] = "" if m[1] == "ok" else m[3] or "failed"
    return results


@pytest.mark.parametrize("name", PLUGIN_CHECK_NAMES)
def test_nvim_plugin_workarounds(plugin_results: dict[str, str], name: str) -> None:
    assert name in plugin_results, f"{name}: no result (the check did not run)"
    assert plugin_results[name] == "", plugin_results[name]
