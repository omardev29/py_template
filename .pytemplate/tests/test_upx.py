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


def test_candidates_skip_runtime_dlls_and_user_globs(tmp_path: Path) -> None:
    names = ("app.exe", "core.cp314-win_amd64.pyd", "vcruntime140.dll", "python314.dll", "flutter_windows.dll", "skipme.dll", "data.bin")
    for name in names:
        (tmp_path / name).write_bytes(b"MZ" + bytes(100))
    found = {p.name for p in upx.candidates(tmp_path, make({"upx": {"exclude": ["skip*.dll"]}}))}
    if WINDOWS:
        assert found == {"app.exe", "core.cp314-win_amd64.pyd"}
    else:
        assert found == set()  # not ELF executables


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
    if upx.unsupported_reason():
        return
    fake = tmp_path / ("upx.exe" if WINDOWS else "upx")
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
