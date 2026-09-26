"""UPX support (runner/upx.py) and the size options of the exe method."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config, upx  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.methods import exe  # noqa: E402
from runner.ui import DeployError  # noqa: E402

WINDOWS = sys.platform == "win32"


def make(deploy: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, {"deploy": deploy}, "")
    config.validate(cfg)
    return cfg


def test_level_flags_and_pyinstaller_env() -> None:
    assert upx.level_flags(make({})) == ["--best", "--lzma"]
    assert upx.level_flags(make({"upx": {"level": "9", "lzma": False}})) == ["-9"]
    assert upx.level_flags(make({"upx": {"level": "ultra-brute"}})) == ["--ultra-brute", "--lzma"]
    # PyInstaller adds --lzma itself: the UPX variable only carries the level
    assert upx.env_value(make({"upx": {"level": "brute"}})) == "--brute"


def test_invalid_level_and_module_names_are_rejected() -> None:
    with pytest.raises(DeployError, match="deploy.upx.level"):
        make({"upx": {"level": "max"}})
    with pytest.raises(DeployError, match="exclude_modules"):
        make({"exclude_modules": ["not a module"]})


def test_candidates_skip_runtime_dlls_and_user_globs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The PE rules are file-name rules: exercised on every OS (it asserted an empty set off
    # Windows, so dropping flutter_windows.dll from BUILTIN_EXCLUDE passed the suite)
    monkeypatch.setattr(upx, "IS_WINDOWS", True)
    names = ("app.exe", "core.cp314-win_amd64.pyd", "vcruntime140.dll", "python314.dll", "flutter_windows.dll", "skipme.dll", "data.bin", "API-MS-WIN-core.dll")
    for name in names:
        (tmp_path / name).write_bytes(b"MZ" + bytes(100))
    found = {p.name for p in upx.candidates(tmp_path, make({"upx": {"exclude": ["skip*.dll"]}}))}
    assert found == {"app.exe", "core.cp314-win_amd64.pyd"}


@pytest.mark.skipif(WINDOWS, reason="ELF rules")
def test_candidates_on_posix_are_elf_executables(tmp_path: Path) -> None:
    # A packed .so crashes when loaded: only ELF executables are packed, never libpython or a .so
    elf = b"\x7fELF" + bytes(60)
    for name in ("app", "python3.14", "libpython3.14.so.1.0", "_ssl.cpython-314-x86_64-linux-gnu.so", "libfoo.so", "skipme"):
        (tmp_path / name).write_bytes(elf)
        (tmp_path / name).chmod(0o755)
    (tmp_path / "not-executable").write_bytes(elf)
    (tmp_path / "not-executable").chmod(0o644)
    (tmp_path / "script.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "script.sh").chmod(0o755)
    (tmp_path / "link").symlink_to("app")
    found = {p.name for p in upx.candidates(tmp_path, make({"upx": {"exclude": ["skip*"]}}))}
    assert found == {"app", "python3.14"}


def test_builtin_excludes_and_assets_are_pinned() -> None:
    # A packed flutter_windows.dll hangs the app at startup (measured): never drop it
    assert {"flutter_windows.dll", "libpython3*", "python3*.dll", "vcruntime140*.dll", "api-ms-win-*.dll"} <= set(upx.BUILTIN_EXCLUDE)
    for (os_name, _arch), (asset, sha256) in upx.ASSETS.items():
        assert asset.startswith(f"upx-{upx.VERSION}-") and asset.endswith(".zip" if os_name == "windows" else ".tar.xz")
        assert len(sha256) == 64 and all(c in "0123456789abcdef" for c in sha256)


def test_relative_upx_path_resolves_against_the_project_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # "tools/upx" was checked against the caller's cwd (./deploy typed from src/ failed) and
    # reached flet pack and Nuitka relative while they run in their own folders
    root = tmp_path / "proj"
    fake = root / "tools" / ("upx.exe" if WINDOWS else "upx")
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"")
    (root / "src").mkdir()
    monkeypatch.setattr(upx, "ROOT", root)
    cfg = make({"upx": {"enabled": True, "path": f"tools/{fake.name}"}})
    for cwd in (root, root / "src", tmp_path):
        monkeypatch.chdir(cwd)
        found = upx.find(cfg)
        assert found.is_absolute() and found == fake
    monkeypatch.setattr(exe, "IS_WINDOWS", True)
    monkeypatch.setattr(upx, "unsupported_reason", lambda: "")
    assert f"--upx-dir={fake.parent}" in exe.size_args(cfg)[0]
    absolute = tmp_path / "elsewhere" / "upx"
    absolute.parent.mkdir()
    absolute.write_bytes(b"")
    assert upx.find(make({"upx": {"path": str(absolute)}})) == absolute
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert upx.find(make({"upx": {"path": "~/elsewhere/upx"}})) == absolute
    with pytest.raises(DeployError, match="relative path starts at the project root") as e:
        upx.find(make({"upx": {"path": "tools/missing"}}))
    assert e.value.code == 3


def test_find_order_path_setting_then_path_then_cache_then_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(upx, "_cache_dir", lambda: cache)
    downloads: list[Path] = []

    def fake_download(dest: Path) -> Path:
        downloads.append(dest)
        return dest / "fresh"

    monkeypatch.setattr(upx, "_download", fake_download)
    monkeypatch.setattr(upx.proc, "base_env", lambda: {"PATH": str(tmp_path / "empty")})
    assert upx.find(make({})) == cache / "fresh" and downloads == [cache]  # nothing anywhere: download
    cached = cache / upx._exe_name()
    cache.mkdir()
    cached.write_bytes(b"")
    assert upx.find(make({})) == cached and len(downloads) == 1  # the cache, no second download
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = bindir / upx._exe_name()
    tool.write_bytes(b"")
    tool.chmod(0o755)
    monkeypatch.setattr(upx.proc, "base_env", lambda: {"PATH": str(bindir)})
    assert Path(upx.find(make({}))).resolve() == tool.resolve()  # PATH beats the cache
    explicit = tmp_path / "explicit" / "upx"
    explicit.parent.mkdir()
    explicit.write_bytes(b"")
    assert upx.find(make({"upx": {"path": str(explicit)}})) == explicit  # the setting beats PATH


# --- the pinned download, with crafted archives (no network) -----------------------------------------


def _serve(monkeypatch: pytest.MonkeyPatch, data: bytes, asset: str, *, windows: bool, sha256: str = "") -> list[str]:
    import hashlib
    import io

    urls: list[str] = []

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def urlopen(url: str, timeout: float = 0) -> Response:
        urls.append(url)
        return Response(data)

    monkeypatch.setattr(upx, "IS_WINDOWS", windows)
    monkeypatch.setattr(upx.urllib.request, "urlopen", urlopen)
    key = ("windows" if windows else "linux", upx.host_arch())
    monkeypatch.setitem(upx.ASSETS, key, (asset, sha256 or hashlib.sha256(data).hexdigest()))
    return urls


def _tar_xz(members: dict[str, bytes]) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buffer.getvalue()


def test_download_extracts_the_linux_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    asset = f"upx-{upx.VERSION}-amd64_linux.tar.xz"
    data = _tar_xz({f"upx-{upx.VERSION}-amd64_linux/README": b"doc", f"upx-{upx.VERSION}-amd64_linux/upx": b"\x7fELF upx"})
    urls = _serve(monkeypatch, data, asset, windows=False)
    target = upx._download(tmp_path / "tools")
    assert urls == [upx.URL.format(version=upx.VERSION, asset=asset)]
    assert target == tmp_path / "tools" / "upx" and target.read_bytes() == b"\x7fELF upx"
    if not WINDOWS:
        assert target.stat().st_mode & 0o777 == 0o755
    assert sorted(p.name for p in (tmp_path / "tools").iterdir()) == ["upx"]  # no .part left


def test_download_extracts_the_windows_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    asset = f"upx-{upx.VERSION}-win64.zip"
    _serve(monkeypatch, _zip({f"upx-{upx.VERSION}-win64/upx.exe": b"MZ upx", f"upx-{upx.VERSION}-win64/NEWS": b""}), asset, windows=True)
    target = upx._download(tmp_path / "tools")
    assert target == tmp_path / "tools" / "upx.exe" and target.read_bytes() == b"MZ upx"


def _cut_response(body: bytes, *, chunked: bool) -> Any:
    """A REAL http.client response whose server closed the connection halfway through `body`
    (announced with Content-Length, or chunked)."""
    import http.client
    import io

    half = body[: len(body) // 2]
    if chunked:
        raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + f"{len(body):x}\r\n".encode() + half
    else:
        raw = f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + half

    class Socket:
        def makefile(self, mode: str) -> io.BytesIO:
            return io.BytesIO(raw)

    response = http.client.HTTPResponse(Socket())  # type: ignore[arg-type]
    response.begin()
    return response


@pytest.mark.parametrize("case", ["bad-sha", "no-binary", "offline", "cut", "chunked cut"])
def test_download_failures_are_clear_and_leave_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    # A connection closed halfway raises http.client.IncompleteRead, which is no OSError: it
    # ended as an "internal runner error" traceback instead of this clear exit 3
    asset = f"upx-{upx.VERSION}-amd64_linux.tar.xz"
    data = _tar_xz({f"upx-{upx.VERSION}-amd64_linux/upx": b"\x7fELF"})
    if case == "bad-sha":
        _serve(monkeypatch, data, asset, windows=False, sha256="0" * 64)
        message = "has SHA-256"
    elif case == "no-binary":
        _serve(monkeypatch, _tar_xz({f"upx-{upx.VERSION}-amd64_linux/README": b""}), asset, windows=False)
        message = "has no upx binary"
    elif case in ("cut", "chunked cut"):
        _serve(monkeypatch, data, asset, windows=False)
        monkeypatch.setattr(upx.urllib.request, "urlopen", lambda url, timeout=0: _cut_response(data, chunked=case == "chunked cut"))
        message = "cannot download"
    else:
        _serve(monkeypatch, data, asset, windows=False)

        def offline(url: str, timeout: float = 0) -> Any:
            raise OSError("network is unreachable")

        monkeypatch.setattr(upx.urllib.request, "urlopen", offline)
        message = "cannot download"
    with pytest.raises(DeployError, match=message) as e:
        upx._download(tmp_path / "tools")
    assert e.value.code == 3
    assert not (tmp_path / "tools").exists() or not any((tmp_path / "tools").iterdir())


def test_files_over_the_limit_are_never_packed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    big = tmp_path / "big.dll"
    big.write_bytes(bytes(2048))
    monkeypatch.setattr(upx, "MAX_INPUT", 1024)
    called: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    result = upx.pack_file(Path("upx"), big, ["--best"])
    assert result.status == "skipped" and "limit" in result.reason and not called
    assert upx.MAX_INPUT < 768 * 1024 * 1024  # the real constant keeps a margin under UPX's limit


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ("upx: x.dll: AlreadyPackedException: already packed by UPX", "already packed"),
        ("upx: x.dll: CantPackException: GUARD_CF enabled PE files are not supported (use --force to disable)", "Control Flow Guard binary"),
        ("upx: x.pyd: NotCompressibleException", "not compressible"),
        ("upx: x.exe: CantPackException: can't pack new-exe", "UPX cannot pack it"),
    ],
)
def test_upx_messages_are_classified(output: str, reason: str) -> None:
    assert upx._classify(output) == reason


def test_pyinstaller_size_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    args, env = exe.size_args(make({"exclude_modules": ["PIL"]}))
    assert "--noupx" in args and "--exclude-module=PIL" in args and env == {}
    # PyInstaller packs with UPX only on Windows (test_build_methods covers the other OSes):
    # the Windows branch is exercised on every OS
    monkeypatch.setattr(exe, "IS_WINDOWS", True)
    monkeypatch.setattr(upx, "unsupported_reason", lambda: "")
    fake = tmp_path / "upx.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(upx, "find", lambda cfg: fake)
    args, env = exe.size_args(make({"upx": {"enabled": True, "level": "7", "exclude": ["flutter*.dll"]}}))
    assert f"--upx-dir={tmp_path}" in args and "--noupx" not in args
    assert "--upx-exclude=vcruntime140*.dll" in args and "--upx-exclude=flutter*.dll" in args
    assert env == {"UPX": "-7"}


def _available_upx() -> Path | None:
    """A upx that needs no download (PATH or the cache): the real test never hits the network."""
    if upx.unsupported_reason():
        return None
    cached = upx._cache_dir() / upx._exe_name()
    found = shutil.which("upx")
    return Path(found) if found else (cached if cached.is_file() else None)


@pytest.mark.skipif(_available_upx() is None, reason="no upx on PATH or in the cache")
def test_real_upx_packs_a_binary_that_still_runs(tmp_path: Path) -> None:
    tool = _available_upx()
    assert tool is not None
    # Test data: the upx executable itself (a plain, non-CFG binary). Its release build is
    # already packed, so unpack the copy first, then pack it again and check it still runs.
    target = tmp_path / tool.name
    shutil.copy2(tool, target)
    subprocess.run([str(tool), "-d", "-q", str(target)], capture_output=True, check=False)
    before = target.stat().st_size
    result = upx.pack_file(tool, target, ["-1"])
    if result.status == "skipped":
        pytest.skip(f"upx refused its own binary: {result.reason}")
    assert result.status == "packed" and result.after < before
    r = subprocess.run([str(target), "--version"], capture_output=True, text=True, check=False)
    assert r.returncode == 0 and "upx" in r.stdout.lower()
