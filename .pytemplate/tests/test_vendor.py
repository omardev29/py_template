"""Vendoring (runner/vendor.py, `./pyt vendor`): the PEP 508 marker reader and the wheel tags
against packaging's, the plan of each platform, the downloads (file:// URLs: no network), the
folders' state, installing an environment from vendor/ (with the real uv, offline, from a local
package index), the builds' source, the hook, doctor, the CI checkout and the command's options."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config, envs, hooks, proc, render, ui, vendor  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.methods import common  # noqa: E402
from runner.project import ROOT  # noqa: E402
from runner.ui import PytError  # noqa: E402

packaging_markers = pytest.importorskip("packaging.markers")
packaging_tags = pytest.importorskip("packaging.tags")

IS_WINDOWS = os.name == "nt"
CP = vendor.Runtime("cpython", 3, 14)
PP = vendor.Runtime("pypy", 3, 11, (3, 11, 15))


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


def own() -> Config:
    """A Config with this project's python.cpython (the real uv runs against its .venv)."""
    return make({"python": {"cpython": config.load(set()).python.cpython}})


# --- markers ---------------------------------------------------------------------------------------

_STRINGS = {
    "os_name": ["nt", "posix", "java"],
    "sys_platform": ["linux", "win32", "darwin", "cygwin", "lin"],
    "platform_system": ["Linux", "Windows", "Darwin"],
    "platform_machine": ["x86_64", "AMD64", "arm64", "aarch64", "ARM64"],
    "platform_python_implementation": ["CPython", "PyPy"],
    "implementation_name": ["cpython", "pypy"],
}
_VERSIONS = ["3.10", "3.11", "3.11.0", "3.11.15", "3.11.16", "3.12", "3.13", "3.14", "3.14.0", "3.14.1", "3.14.7", "3.15", "3", "3.14.*", "3.11.*", "4"]
@st.composite
def _atoms(draw: st.DrawFn) -> str:
    kind = draw(st.integers(0, 2))
    if kind == 0:
        name = draw(st.sampled_from(sorted(_STRINGS)))
        return f"{name} {draw(st.sampled_from(['==', '!=', 'in', 'not in', '<', '>=']))} '{draw(st.sampled_from(_STRINGS[name]))}'"
    name = draw(st.sampled_from(["python_version", "python_full_version"]))
    version = draw(st.sampled_from(_VERSIONS))
    if kind == 1:
        return f"{name} {draw(st.sampled_from(['==', '!=', '<', '<=', '>', '>=', '~=']))} '{version}'"
    return f"'{version}' {draw(st.sampled_from(['<', '<=', '>', '>=']))} {name}"


@st.composite
def _markers(draw: st.DrawFn, depth: int = 0) -> str:
    if depth >= 3 or draw(st.booleans()):
        return draw(_atoms())
    if draw(st.booleans()):
        return f"({draw(_markers(depth + 1))})"
    return f"{draw(_markers(depth + 1))} {draw(st.sampled_from(['and', 'or']))} {draw(_markers(depth + 1))}"


_PLACES = [(CP, p) for p in config.VENDOR_PLATFORMS] + [(PP, "linux-x86_64"), (PP, "windows-x86_64"), (PP, "macos-aarch64")]


def _packaging_env(runtime: vendor.Runtime, platform: str, patch: int) -> dict[str, str]:
    env = {k: v for k, v in vendor.marker_env(runtime, platform).items() if isinstance(v, str) and "." not in k}
    full = ".".join(map(str, runtime.full)) if runtime.full else f"{runtime.major}.{runtime.minor}.{patch}"
    return {**env, "python_full_version": full, "implementation_version": full, "platform_release": "", "platform_version": "", "extra": ""}


@settings(max_examples=max(settings.default.max_examples, 400))
@given(_markers(), st.sampled_from(_PLACES))
def test_markers_read_like_packaging(marker: str, place: tuple[vendor.Runtime, str]) -> None:
    """What vendor.evaluate decides, packaging decides alike, for every patch release of a CPython
    known to its minor only; what depends on the patch is None (vendored, to be safe)."""
    runtime, platform = place
    ours = vendor.evaluate(marker, vendor.marker_env(runtime, platform))
    answers = set()
    for patch in (0, 1, 7, 15, 16, 40):
        try:
            answers.add(packaging_markers.Marker(marker).evaluate(_packaging_env(runtime, platform, patch)))
        except packaging_markers.UndefinedComparison:  # `~= '3'`: packaging refuses the whole marker
            return
    if ours is not None:
        assert answers == {ours}, (marker, platform, runtime, answers)


def test_markers_uv_writes_are_read() -> None:
    env = vendor.marker_env(CP, "windows-x86_64")
    cases = {
        "python_full_version < '3.12' and implementation_name == 'cpython' and sys_platform == 'win32'": False,
        "implementation_name == 'cpython' and sys_platform == 'win32'": True,
        "(platform_machine == 'AMD64' and sys_platform == 'win32') or (platform_machine == 'x86_64' and sys_platform == 'linux')": True,
        "python_full_version >= '3.14.1'": None,  # uv picks the patch release
        "python_version >= '3.11'": True,
        "platform_release >= '5.0'": None,
        "extra == 'group-7-myapp-gpu'": None,
        "os.name == 'nt'": True,  # PEP 508's legacy spelling
        "'win' in sys_platform": True,
        "sys_platform not in 'linux darwin'": True,
        "sys_platform < 'x'": None,  # packaging's versions disagree on ordering strings
    }
    for marker, expected in cases.items():
        assert vendor.evaluate(marker, env) is expected, marker
    assert vendor.evaluate("python_full_version >= '3.11.10'", vendor.marker_env(PP, "linux-x86_64")) is True


@pytest.mark.parametrize("bad", ["python_version >=", "(sys_platform == 'linux'", "sys_platform = 'linux'", "nothing == 'x'", "sys_platform not 'x'", "'a' 'b'"])
def test_a_marker_it_cannot_read_raises(bad: str) -> None:
    with pytest.raises(vendor.MarkerError):
        vendor.evaluate(bad, vendor.marker_env(CP, "linux-x86_64"))


# --- wheel tags ------------------------------------------------------------------------------------

_TRICKY = [
    "a-1-cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
    "a-1-cp314-cp314t-manylinux_2_17_x86_64.whl",  # free-threaded: never for cp314
    "a-1-cp39-abi3-manylinux_2_28_aarch64.whl",
    "a-1-cp315-abi3-win_amd64.whl",  # abi3 of a newer minor: no
    "a-1-cp314-cp314-musllinux_1_2_x86_64.whl",
    "a-1-cp314-cp314-macosx_10_13_universal2.whl",
    "a-1-cp314-cp314-macosx_11_0_arm64.whl",
    "a-1-cp314-cp314-macosx_10_9_x86_64.macosx_11_0_arm64.whl",
    "a-1-cp314-cp314-win32.whl",
    "a-1-cp314-cp314-win_arm64.whl",
    "a-1-pp311-pypy311_pp73-manylinux_2_17_x86_64.whl",
    "a-1-pp310-pypy310_pp73-win_amd64.whl",
    "a-1-pp311-none-any.whl",
    "a-1-py3-none-any.whl",
    "a-1-py2.py3-none-any.whl",
    "a-1-py314-none-any.whl",
    "a-1-py311-none-macosx_11_0_arm64.whl",
    "a-1-py3-none-manylinux_2_17_x86_64.whl",
    "a-1-cp314-none-any.whl",
    "a-1-2-cp314-cp314-linux_x86_64.whl",  # a build tag, a bare linux_ tag
    "a-1-cp27-cp27mu-manylinux1_x86_64.whl",
]


def _lock_wheels() -> list[str]:
    try:
        lock = vendor.read_lock(ROOT / "uv.lock")
    except PytError:
        return []
    return sorted({w.filename for p in lock.values() for w in p.wheels})


def _supported(runtime: vendor.Runtime, platform: str) -> set[str]:
    """Every tag a machine of `platform` running `runtime` loads, by packaging."""
    os_name, arch = platform.split("-")
    if os_name == "linux":
        plats = [f"manylinux_2_{n}_{arch}" for n in range(45, 4, -1)] + [f"manylinux2014_{arch}", f"manylinux2010_{arch}", f"manylinux1_{arch}", f"linux_{arch}"]
    elif os_name == "macos":
        plats = list(packaging_tags.mac_platforms((30, 0), "arm64" if arch == "aarch64" else "x86_64"))
    else:
        plats = ["win_amd64" if arch == "x86_64" else "win_arm64"]
    version = (runtime.major, runtime.minor)
    if runtime.impl == "cpython":
        tags = [*packaging_tags.cpython_tags(version, [f"cp{version[0]}{version[1]}"], plats), *packaging_tags.compatible_tags(version, f"cp{version[0]}{version[1]}", plats)]
    else:
        interpreter = f"pp{version[0]}{version[1]}"
        tags = [*packaging_tags.generic_tags(interpreter, [f"pypy{version[0]}{version[1]}_pp73"], plats), *packaging_tags.compatible_tags(version, interpreter, plats)]
    return {str(t) for t in tags}


@pytest.mark.parametrize(("runtime", "platform"), _PLACES)
def test_wheel_tags_load_where_packaging_says(runtime: vendor.Runtime, platform: str) -> None:
    """A wheel is kept for a platform exactly when packaging says a machine of it loads one of its
    tags: every wheel of this project's uv.lock, and the tricky names."""
    supported = _supported(runtime, platform)
    for name in [*_TRICKY, *_lock_wheels()]:
        parsed = vendor.parse_wheel(name)
        assert parsed is not None, name
        ours = vendor.wheel_rank(parsed[2], runtime, platform) is not None
        theirs = any(f"{py}-{abi}-{plat}" in supported for py, abi, plat in parsed[2])
        assert ours == theirs, (name, runtime, platform)


def _wheel(name: str, size: int = 10) -> vendor.LockFile:
    return vendor.LockFile("a", "1", f"https://example.invalid/{name}", "0" * 64, size, "wheel")


def _chosen(names: list[str], runtime: vendor.Runtime = CP, platform: str = "linux-x86_64", sizes: dict[str, int] | None = None) -> list[str]:
    files = [_wheel(n, (sizes or {}).get(n, 10)) for n in names]
    return sorted(w.filename for w in vendor.choose_wheels(files, runtime, platform))


def test_choose_wheels_keeps_one_wheel_per_kind_of_machine() -> None:
    cp = "a-1-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
    abi3 = "a-1-cp37-abi3-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl"
    pure = "a-1-py3-none-any.whl"
    # the same machines (glibc 2.17 and newer: what uv's Pythons need): the best tags only
    assert _chosen([cp, abi3, pure]) == [cp]
    # a native build for newer machines only: the pure one serves the older ones
    new_only = "a-1-cp314-cp314-manylinux_2_34_x86_64.whl"
    assert _chosen([new_only, pure]) == sorted([new_only, pure])
    # an older build that ranks worse, and a newer one that ranks better: both
    old_abi3 = "a-1-cp39-abi3-manylinux_2_17_x86_64.whl"
    assert _chosen([old_abi3, new_only]) == sorted([old_abi3, new_only])
    # a newer build that ranks no better than the older one is never picked
    new_abi3 = "a-1-cp39-abi3-manylinux_2_34_x86_64.whl"
    assert _chosen([cp, new_abi3]) == [cp]
    # a thin arm64 wheel and a universal2 one serve the same Macs: the smaller one
    thin, fat = "a-1-cp314-cp314-macosx_11_0_arm64.whl", "a-1-cp314-cp314-macosx_10_13_universal2.whl"
    assert _chosen([thin, fat], platform="macos-aarch64", sizes={thin: 5, fat: 9}) == [thin]
    assert _chosen([thin, fat], platform="macos-x86_64") == [fat]
    assert _chosen([pure]) == [pure]
    assert _chosen(["a-1-cp313-cp313-win_amd64.whl"], platform="windows-x86_64") == []


# --- the export and uv.lock ------------------------------------------------------------------------

EXPORT = """\
alpha==1.0 ; sys_platform == 'win32' \\
    --hash=sha256:aaa \\
    --hash=sha256:bbb
Beta_Two==2.0
    # via alpha
gamma @ git+https://example.invalid/gamma@abc ; python_version >= '3.11'
delta @ https://example.invalid/delta-1.0.tar.gz
-e ./libs/local
./libs/other
"""


def test_parse_export_reads_every_kind_of_requirement() -> None:
    entries = vendor.parse_export(EXPORT)
    assert [(e.name, e.version, e.marker, e.kind) for e in entries] == [
        ("alpha", "1.0", "sys_platform == 'win32'", "pin"),
        ("beta-two", "2.0", "", "pin"),
        ("gamma", "", "python_version >= '3.11'", "git"),
        ("delta", "", "", "url"),
        ("./libs/local", "", "", "local"),
        ("./libs/other", "", "", "local"),
    ]
    assert entries[0].text.count("--hash") == 2
    assert vendor.pins(EXPORT) == entries[0].text + "\nBeta_Two==2.0\n"


# A small project: its files on disk, a uv.lock naming them by file:// URL (or by path), the export
def _files(tmp_path: Path) -> dict[str, bytes]:
    return {
        "alpha-1.0-py3-none-any.whl": b"alpha wheel",
        "beta-2.0-cp314-cp314-win_amd64.whl": b"beta windows wheel",
        "beta-2.0-cp314-cp314-manylinux_2_17_x86_64.whl": b"beta linux wheel",
        "beta-2.0.tar.gz": b"beta sdist",
        "gamma-3.0.tar.gz": b"gamma sdist only",
    }


def _write_lock(tmp_path: Path, files: dict[str, bytes], *, by_path: bool = False, wrong: str = "", bigger: str = "") -> Path:
    store = tmp_path / "index"
    store.mkdir(exist_ok=True)
    for name, data in files.items():
        (store / name).write_bytes(data)

    def entry(name: str) -> str:
        data = files[name]
        digest = "1" * 64 if name == wrong else hashlib.sha256(data).hexdigest()
        size = len(data) + (5 if name == bigger else 0)
        where = f'path = "{(store / name).as_posix()}"' if by_path else f'url = "{(store / name).as_uri()}"'
        return f'{{ {where}, hash = "sha256:{digest}", size = {size} }}'

    registry = store.as_posix() if by_path else "https://example.invalid/simple"
    text = f"""version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "alpha"
version = "1.0"
source = {{ registry = "{registry}" }}
wheels = [{entry("alpha-1.0-py3-none-any.whl")}]

[[package]]
name = "beta"
version = "2.0"
source = {{ registry = "{registry}" }}
sdist = {entry("beta-2.0.tar.gz")}
wheels = [{entry("beta-2.0-cp314-cp314-win_amd64.whl")}, {entry("beta-2.0-cp314-cp314-manylinux_2_17_x86_64.whl")}]

[[package]]
name = "gamma"
version = "3.0"
source = {{ registry = "{registry}" }}
sdist = {entry("gamma-3.0.tar.gz")}

[[package]]
name = "local"
version = "0.1"
source = {{ editable = "libs/local" }}
"""
    lock = tmp_path / "uv.lock"
    lock.write_text(text, encoding="utf-8")
    return lock


PROJECT_EXPORT = """\
alpha==1.0
beta==2.0 ; sys_platform == 'win32' or sys_platform == 'linux'
gamma==3.0 ; implementation_name == 'cpython'
-e ./libs/local
"""


@pytest.fixture
def small(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path]:
    """vendor.py pointed at a small project in tmp_path (no uv: the export is PROJECT_EXPORT)."""
    lock = _write_lock(tmp_path, _files(tmp_path))
    monkeypatch.setattr(vendor, "LOCK", lock)
    monkeypatch.setattr(vendor, "VENDOR", tmp_path / "vendor")
    monkeypatch.setattr(vendor, "PYPROJECT", tmp_path / "pyproject.toml")
    monkeypatch.setattr(vendor, "export", lambda cfg, env, hashes, frozen=False: PROJECT_EXPORT)
    monkeypatch.setattr(envs, "left_out", lambda env: [])
    monkeypatch.setattr(common, "DOWNLOAD_RETRIES", (0.0,))
    cfg = make({"python": {"cpython": "3.14"}, "vendor": {"platforms": ["linux-x86_64", "windows-x86_64"]}})
    return cfg, tmp_path


def test_the_plan_takes_what_each_platform_installs(small: tuple[Config, Path]) -> None:
    cfg, _ = small
    plan = vendor.make_plan(cfg)
    assert sorted(plan.platforms["linux-x86_64"].files) == ["alpha-1.0-py3-none-any.whl", "beta-2.0-cp314-cp314-manylinux_2_17_x86_64.whl", "gamma-3.0.tar.gz"]
    assert sorted(plan.platforms["windows-x86_64"].files) == ["alpha-1.0-py3-none-any.whl", "beta-2.0-cp314-cp314-win_amd64.whl", "gamma-3.0.tar.gz"]
    assert any("gamma 3.0 has no wheel" in n for n in plan.platforms["linux-x86_64"].notes)
    assert plan.notes == ["./libs/local is a local library: never vendored, it installs from its source"]
    macos = vendor.make_plan(cfg, ["macos-aarch64"]).platforms["macos-aarch64"]
    assert not any(name.startswith("beta") for name in macos.files)  # its marker: Windows and Linux only


def test_a_platform_without_a_wheel_gets_the_sdist(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, _ = small
    monkeypatch.setattr(vendor, "export", lambda cfg, env, hashes, frozen=False: "beta==2.0\n")
    macos = vendor.make_plan(cfg, ["macos-aarch64"]).platforms["macos-aarch64"]
    assert list(macos.files) == ["beta-2.0.tar.gz"] and "builds it" in macos.notes[0]


def test_the_plan_follows_no_build_and_no_binary(small: tuple[Config, Path]) -> None:
    cfg, tmp_path = small
    (tmp_path / "pyproject.toml").write_text('[tool.uv]\nno-build-package = ["gamma"]\nno-binary-package = ["Beta"]\n', encoding="utf-8")
    pp = vendor.make_plan(cfg, ["linux-x86_64"]).platforms["linux-x86_64"]
    assert "gamma-3.0.tar.gz" not in pp.files and any("gamma 3.0 has no file" in n and "forbids building" in n for n in pp.notes)
    assert "beta-2.0.tar.gz" in pp.files and not any(n.startswith("beta-2.0-") for n in pp.files)


def test_a_package_that_is_needed_nowhere_is_not_vendored(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, _ = small
    monkeypatch.setattr(vendor, "export", lambda cfg, env, hashes, frozen=False: "alpha==1.0\nbeta==2.0 ; sys_platform == 'win32'\n")
    plan = vendor.make_plan(cfg)
    assert "beta-2.0-cp314-cp314-win_amd64.whl" in plan.platforms["windows-x86_64"].files
    assert not any(n.startswith("beta") for n in plan.platforms["linux-x86_64"].files)


def test_a_pypy_project_vendors_for_both_interpreters(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, tmp_path = small
    files = {**_files(tmp_path), "beta-2.0-pp311-pypy311_pp73-manylinux_2_17_x86_64.whl": b"beta pypy"}
    lock = _write_lock(tmp_path, files)
    text = lock.read_text(encoding="utf-8").replace(
        '{ url = "' + (tmp_path / "index" / "beta-2.0-cp314-cp314-win_amd64.whl").as_uri(),
        '{ url = "' + (tmp_path / "index" / "beta-2.0-pp311-pypy311_pp73-manylinux_2_17_x86_64.whl").as_uri()
        + f'", hash = "sha256:{hashlib.sha256(b"beta pypy").hexdigest()}", size = 9 }}, {{ url = "' + (tmp_path / "index" / "beta-2.0-cp314-cp314-win_amd64.whl").as_uri(),
    )
    lock.write_text(text, encoding="utf-8")
    pypy = make({"python": {"cpython": "3.14"}, "backend": {"active": "cpython", "supported": ["cpython", "pypy"]}, "vendor": {"platforms": ["linux-x86_64"]}})
    files_of = vendor.make_plan(pypy).platforms["linux-x86_64"].files
    assert {"beta-2.0-cp314-cp314-manylinux_2_17_x86_64.whl", "beta-2.0-pp311-pypy311_pp73-manylinux_2_17_x86_64.whl"} <= set(files_of)
    assert "gamma-3.0.tar.gz" in files_of  # CPython needs it (its marker), PyPy does not


def test_read_lock_takes_files_named_by_path_and_skips_files_without_a_hash(tmp_path: Path) -> None:
    lock = _write_lock(tmp_path, _files(tmp_path), by_path=True)
    packages = vendor.read_lock(lock)
    assert packages[("alpha", "1.0")].wheels[0].url == (tmp_path / "index" / "alpha-1.0-py3-none-any.whl").resolve().as_uri()
    assert ("local", "0.1") not in packages  # an editable source: the project's own
    flat = tmp_path / "flat.lock"
    flat.write_text('version = 1\n[[package]]\nname = "x"\nversion = "1"\nsource = { registry = "../vendor" }\nwheels = [{ path = "x-1-py3-none-any.whl" }]\n', encoding="utf-8")
    assert vendor.read_lock(flat)[("x", "1")].wheels == []
    with pytest.raises(PytError, match="not found"):
        vendor.read_lock(tmp_path / "missing.lock")


@pytest.mark.parametrize("name", ["..%2F..%2Fpwned.whl", "a%5Cb.whl", ".hidden-1.0-py3-none-any.whl", "%2E%2E"])
def test_a_lock_file_name_that_is_no_plain_name_is_refused(tmp_path: Path, name: str) -> None:
    """The file's name becomes a path in vendor/: decoded from the URL, `..%2F..` named a file
    outside it."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        f'version = 1\n[[package]]\nname = "x"\nversion = "1"\nsource = {{ registry = "https://example.invalid/simple" }}\n'
        f'wheels = [{{ url = "https://example.invalid/files/{name}", hash = "sha256:{"0" * 64}", size = 1 }}]\n',
        encoding="utf-8",
    )
    with pytest.raises(PytError, match="no plain file name"):
        vendor.read_lock(lock)


# --- downloads and the folders ---------------------------------------------------------------------


def test_follow_lock_downloads_checks_and_prunes(small: tuple[Config, Path]) -> None:
    cfg, tmp_path = small
    linux = tmp_path / "vendor" / "linux-x86_64"
    linux.mkdir(parents=True)
    (linux / "old-0.1-py3-none-any.whl").write_bytes(b"old")  # a file the lock no longer names
    (linux / "README.txt").write_text("mine", encoding="utf-8")  # not a distribution: left alone
    (linux / ".gitattributes").write_text("* -text\n", encoding="utf-8")
    (linux / ".alpha-1.0-py3-none-any.whl.x1.part").write_bytes(b"cut")  # a killed run's leftover
    vendor.follow_lock(cfg)
    assert sorted(p.name for p in linux.iterdir()) == [".gitattributes", "README.txt", "alpha-1.0-py3-none-any.whl", "beta-2.0-cp314-cp314-manylinux_2_17_x86_64.whl", "gamma-3.0.tar.gz"]
    assert (linux / "alpha-1.0-py3-none-any.whl").read_bytes() == b"alpha wheel"
    assert (tmp_path / "vendor" / "windows-x86_64" / "beta-2.0-cp314-cp314-win_amd64.whl").read_bytes() == b"beta windows wheel"
    plan = vendor.make_plan(cfg)
    assert all(vendor.state(pp, full=True).in_line for pp in plan.platforms.values())
    assert vendor.check_folders(cfg) == []


def test_a_file_that_is_not_the_one_uv_lock_names_is_never_vendored(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, tmp_path = small
    monkeypatch.setattr(vendor, "LOCK", _write_lock(tmp_path, _files(tmp_path), wrong="alpha-1.0-py3-none-any.whl"))
    with pytest.raises(PytError, match="not the file uv.lock names") as e:
        vendor.follow_lock(cfg, ["linux-x86_64"])
    assert e.value.code == 1
    left = sorted(p.name for p in (tmp_path / "vendor" / "linux-x86_64").iterdir())
    assert left == []  # what this run downloaded went with it, and no .part stayed


def test_a_download_cut_short_is_tried_again_then_refused(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg, tmp_path = small
    monkeypatch.setattr(vendor, "LOCK", _write_lock(tmp_path, _files(tmp_path), bigger="gamma-3.0.tar.gz"))
    with pytest.raises(PytError, match="ended after") as e:
        vendor.follow_lock(cfg, ["linux-x86_64"])
    assert e.value.code == 3
    assert "trying again" in capsys.readouterr().err


def test_a_lfs_pointer_and_a_damaged_file_are_downloaded_again(small: tuple[Config, Path], capsys: pytest.CaptureFixture[str]) -> None:
    cfg, tmp_path = small
    vendor.follow_lock(cfg, ["linux-x86_64"])
    linux = tmp_path / "vendor" / "linux-x86_64"
    (linux / "alpha-1.0-py3-none-any.whl").write_bytes(vendor.LFS_POINTER + b"\noid sha256:x\nsize 11\n")
    (linux / "gamma-3.0.tar.gz").write_bytes(b"gamma sdist ONLY")  # its size, another sha256
    pp = vendor.make_plan(cfg, ["linux-x86_64"]).platforms["linux-x86_64"]
    st = vendor.state(pp, full=True)
    assert st.pointers == ["alpha-1.0-py3-none-any.whl"] and st.damaged == ["gamma-3.0.tar.gz"]
    assert "Git LFS pointer" in vendor.describe(st, "linux-x86_64")
    vendor.follow_lock(cfg, ["linux-x86_64"], full=True)
    err = capsys.readouterr().err
    assert "git lfs pull" in err and "another sha256" in err
    assert (linux / "alpha-1.0-py3-none-any.whl").read_bytes() == b"alpha wheel"
    assert (linux / "gamma-3.0.tar.gz").read_bytes() == b"gamma sdist only"


def test_follow_lock_under_dry_run_writes_nothing(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg, tmp_path = small
    monkeypatch.setattr(proc, "DRY_RUN", True)
    vendor.follow_lock(cfg)
    assert not (tmp_path / "vendor").exists()
    assert "(--dry-run) vendor/linux-x86_64: would download 3 file(s)" in capsys.readouterr().err


def test_remove_platform_takes_only_distribution_files(small: tuple[Config, Path]) -> None:
    cfg, tmp_path = small
    vendor.follow_lock(cfg)
    vendor.remove_platform("windows-x86_64")
    assert not (tmp_path / "vendor" / "windows-x86_64").exists()
    linux = tmp_path / "vendor" / "linux-x86_64"
    (linux / "notes.txt").write_text("mine", encoding="utf-8")
    vendor.remove_platform("linux-x86_64")
    assert sorted(p.name for p in linux.iterdir()) == ["notes.txt"]


def _sdist(path: Path, pyproject: str | None) -> None:
    with tarfile.open(path, "w:gz") as archive:
        files = {"pkg-1.0/setup.py": "", **({"pkg-1.0/pyproject.toml": pyproject} if pyproject is not None else {})}
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def test_build_requires_reads_the_sdists_build_system(tmp_path: Path) -> None:
    _sdist(tmp_path / "a.tar.gz", '[build-system]\nrequires = ["hatchling>=1.0", "Hatch-VCS ; python_version > \'3\'"]\n')
    _sdist(tmp_path / "b.tar.gz", None)
    _sdist(tmp_path / "c.tar.gz", '[project]\nname = "c"\n')
    (tmp_path / "d.tar.gz").write_bytes(b"no archive")
    assert vendor.build_requires(tmp_path / "a.tar.gz") == ["hatchling", "hatch-vcs"]
    assert vendor.build_requires(tmp_path / "b.tar.gz") == ["setuptools"]
    assert vendor.build_requires(tmp_path / "c.tar.gz") == ["setuptools"]
    assert vendor.build_requires(tmp_path / "d.tar.gz") is None


def test_backend_notes_name_a_build_backend_uv_lock_lacks(small: tuple[Config, Path]) -> None:
    cfg, tmp_path = small
    plan = vendor.follow_lock(cfg, ["linux-x86_64"])
    _sdist(tmp_path / "vendor" / "linux-x86_64" / "gamma-3.0.tar.gz", '[build-system]\nrequires = ["flit_core"]\n')
    notes = vendor.backend_notes(cfg, plan)
    assert notes == ["gamma builds with flit-core, which uv.lock does not hold: installing it without the network fails (./pyt add --group build flit-core keeps it in uv.lock and vendor/)"]


# --- installing from vendor/ -----------------------------------------------------------------------


def test_uv_calls_that_install_prepare_the_environment() -> None:
    assert envs._installs(["run", "--locked", "python"], None)
    assert envs._installs(["run", "--quiet", "--frozen", "ruff"], None)
    assert envs._installs(["sync", "--locked", "--all-groups"], ROOT)
    assert envs._installs(["run", "--locked", "--project", str(ROOT), "flet"], ROOT / ".build" / "x")
    assert not envs._installs(["run", "--locked", "--no-sync", "ruff"], None)
    assert not envs._installs(["sync", "--dry-run"], None)
    assert not envs._installs(["lock", "--check"], None)
    assert not envs._installs(["pip", "install", "x"], None)
    assert not envs._installs(["run", "--locked", "--script", "tool.py"], None)
    assert not envs._installs(["run", "--no-project", "--with", "debugpy", "python"], None)
    assert not envs._installs(["sync", "--locked"], ROOT.parent / "copy")  # mutation's copy of the project


def test_environments_carry_the_config_only_while_something_is_vendored() -> None:
    plain = make({})
    vendored = make({"vendor": {"platforms": ["linux-x86_64"]}})
    assert envs.cpython_env(plain).vendored is None and envs.pypy_env(plain).vendored is None
    assert envs.cpython_env(vendored).vendored is vendored and envs.tool_env(vendored).vendored is vendored
    assert envs.cpython_env(vendored) == envs.cpython_env(plain)  # the config is no part of the environment's identity


def test_prepare_installs_once_per_lock(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg, tmp_path = small
    vendor.follow_lock(cfg)
    venv = tmp_path / ".venv"
    python = envs.venv_python(venv)
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    env = envs.PyEnv("cpython", venv, "3.14", "only-managed", cfg)
    installs: list[str] = []
    monkeypatch.setattr(vendor, "install", lambda cfg, env, platform, frozen=False: installs.append(platform))
    monkeypatch.setattr(vendor, "host_platform", lambda: "linux-x86_64")
    monkeypatch.setattr(vendor, "_prepared", {})
    vendor.prepare(cfg, env)
    vendor.prepare(cfg, env)  # the same run, the same lock
    monkeypatch.setattr(vendor, "_prepared", {})
    vendor.prepare(cfg, env)  # another run: the stamp says it is installed
    assert installs == ["linux-x86_64"]
    vendor.LOCK.write_text(vendor.LOCK.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # a new uv.lock
    vendor.prepare(cfg, env)
    assert installs == ["linux-x86_64", "linux-x86_64"]
    # a uv.lock whose files vendor/ lacks: brought in line first, with a warning
    (tmp_path / "vendor" / "linux-x86_64" / "alpha-1.0-py3-none-any.whl").unlink()
    vendor.LOCK.write_text(vendor.LOCK.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    vendor.prepare(cfg, env)
    assert "does not match uv.lock" in capsys.readouterr().err
    assert (tmp_path / "vendor" / "linux-x86_64" / "alpha-1.0-py3-none-any.whl").is_file()


def test_prepare_on_a_platform_vendor_does_not_serve_says_so_once_and_installs_nothing(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg, tmp_path = small
    env = envs.PyEnv("cpython", tmp_path / ".venv", "3.14", "only-managed", cfg)
    monkeypatch.setattr(vendor, "install", lambda *a, **k: pytest.fail("installed"))
    monkeypatch.setattr(vendor, "host_platform", lambda: "macos-aarch64")
    monkeypatch.setattr(vendor, "_prepared", {})
    monkeypatch.setattr(vendor, "_noted", set())
    vendor.prepare(cfg, env)  # a uv run: silent
    assert capsys.readouterr().err == ""
    monkeypatch.setattr(vendor, "_prepared", {})
    vendor.prepare(cfg, env, sync=True)
    monkeypatch.setattr(vendor, "_prepared", {})
    vendor.prepare(cfg, env, sync=True)
    err = capsys.readouterr().err
    assert err.count("installs from the package index") == 1 and "./pyt vendor --platform macos-aarch64" in err


def test_build_source_takes_a_vendored_platform_from_vendor(small: tuple[Config, Path]) -> None:
    cfg, tmp_path = small
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("alpha==1.0 \\\n    --hash=sha256:aa\n", encoding="utf-8")
    assert vendor.build_source(cfg, "linux", "x86_64", requirements) is None  # no folder yet
    vendor.follow_lock(cfg)
    folder = tmp_path / "vendor" / "linux-x86_64"
    assert vendor.build_source(cfg, "linux", "x86_64", requirements) == ["--no-index", "--offline", "--find-links", folder, "-r", requirements]
    assert vendor.build_source(cfg, "macos", "aarch64", requirements) is None  # not vendored
    requirements.write_text("alpha==1.0\n./libs/local\n", encoding="utf-8")
    assert vendor.build_source(cfg, "linux", "x86_64", requirements) == ["--find-links", folder, "-r", requirements]  # its backend from the index


@pytest.mark.parametrize(
    ("request_", "info", "fits"),
    [
        ("3.14", {"impl": "cpython", "version": "3.14.7"}, True),
        ("3.14", {"impl": "cpython", "version": "3.13.9"}, False),
        ("3.1", {"impl": "cpython", "version": "3.14.7"}, False),
        ("pypy@3.11.15", {"impl": "pypy", "version": "3.11.15"}, True),
        ("pypy@3.11.15", {"impl": "pypy", "version": "3.11.13"}, False),
        ("pypy@3.11.15", {"impl": "cpython", "version": "3.11.15"}, False),
    ],
)
def test_an_environment_fits_only_the_interpreter_it_is_made_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request_: str, info: dict[str, str], fits: bool) -> None:
    env = envs.PyEnv("cpython", tmp_path / ".venv", request_, "only-managed")
    assert not vendor.fits(env)  # no interpreter at all
    env.python.parent.mkdir(parents=True)
    env.python.write_text("", encoding="utf-8")
    monkeypatch.setattr(envs, "interpreter_info", lambda python: info)
    assert vendor.fits(env) is fits


def test_install_makes_an_environment_of_another_python_again(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """python.cpython moved from 3.14 to 3.13: installed into as it was, the old .venv got the
    3.13 wheels (or none) on 3.14; it is made again first, as uv sync would make it."""
    cfg, tmp_path = small
    vendor.follow_lock(cfg)
    env = envs.PyEnv("cpython", tmp_path / ".venv", "3.13", "only-managed", cfg)
    env.python.parent.mkdir(parents=True)
    env.python.write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(envs, "uv", lambda env, args, **kw: calls.append([str(a) for a in args]) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(envs, "interpreter_info", lambda python: {"impl": "cpython", "version": "3.14.7"})
    vendor.install(cfg, env, "linux-x86_64")
    assert calls[0] == ["venv", "--clear", "--quiet", str(env.dir)] and calls[1][:2] == ["pip", "install"]
    calls.clear()
    monkeypatch.setattr(envs, "interpreter_info", lambda python: {"impl": "cpython", "version": "3.13.9"})
    vendor.install(cfg, env, "linux-x86_64")
    assert [c[:2] for c in calls] == [["pip", "install"]]  # the right Python: kept


def test_a_frozen_uv_run_prepares_with_a_frozen_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook's ruff runs `uv run --frozen`: the install it prepares reads uv.lock as it is too,
    never with --locked, which a stale lock fails (the hook's lock check reports that)."""
    cfg = make({"vendor": {"platforms": ["linux-x86_64"]}})
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(vendor, "prepare", lambda cfg, env, **kw: seen.append(kw))
    monkeypatch.setattr(proc, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    monkeypatch.setattr(envs, "require_min_uv", lambda uv: None)
    env = envs.cpython_env(cfg)
    envs.uv(env, ["run", "--quiet", "--frozen", "ruff"])
    envs.uv(env, ["sync", "--locked", "--all-groups"])
    assert seen == [{"sync": False, "frozen": True}, {"sync": True, "frozen": False}]


def test_export_reads_uv_lock_frozen_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(envs, "uv", lambda env, args, **kw: calls.append([str(a) for a in args]) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(envs, "left_out", lambda env: [])
    cfg = make({})
    vendor.export(cfg, envs.cpython_env(cfg), hashes=False, frozen=True)
    vendor.export(cfg, envs.cpython_env(cfg), hashes=True)
    assert [c[1] for c in calls] == ["--frozen", "--locked"]
    assert "--no-hashes" in calls[0] and "--no-hashes" not in calls[1]

# --- the real uv, offline: a local package index -----------------------------------------------------


def _real_wheel(folder: Path, name: str, version: str, deps: tuple[str, ...] = ()) -> Path:
    path = folder / f"{name}-{version}-py3-none-any.whl"
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": f"VERSION = {version!r}\n",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(f"Requires-Dist: {d}\n" for d in deps),
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    with zipfile.ZipFile(path, "w") as z:
        for member, text in files.items():
            z.writestr(member, text)
            digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
            record.append(f"{member},sha256={digest},{len(text)}")
        z.writestr(f"{dist}/RECORD", "\n".join([*record, f"{dist}/RECORD,,"]) + "\n")
    return path


def test_an_environment_installs_from_vendor_with_the_real_uv_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A project locked against a local index; vendor/ filled from it; then the index gone, uv's
    cache empty and uv offline: install() puts every package in the environment, and `uv sync
    --locked --offline` finds nothing to do."""
    cfg = own()
    python = config.load(set()).python.cpython
    if not envs.cpython_env(cfg).python.is_file():
        pytest.skip("needs the project's .venv (./pyt setup): uv runs on its python.cpython")
    files = tmp_path / "files"
    files.mkdir()
    wheels = [_real_wheel(files, "alpha", "1.0", ("beta",)), _real_wheel(files, "beta", "2.0")]
    for name in ("alpha", "beta"):
        page = tmp_path / "simple" / name
        page.mkdir(parents=True)
        links = "".join(f'<a href="{w.as_uri()}#sha256={hashlib.sha256(w.read_bytes()).hexdigest()}">{w.name}</a>\n' for w in wheels if w.name.startswith(name))
        (page / "index.html").write_text(f"<html><body>{links}</body></html>", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        f'[project]\nname = "proj"\nversion = "0"\nrequires-python = ">={python}"\ndependencies = ["alpha"]\n\n'
        f'[[tool.uv.index]]\nname = "local"\nurl = "{(tmp_path / "simple").as_uri()}"\ndefault = true\n',
        encoding="utf-8",
    )
    uv_env = {**envs.without_lock_mode(proc.base_env()), "UV_CACHE_DIR": str(tmp_path / "cache"), "UV_PYTHON": python, "UV_PYTHON_PREFERENCE": "only-managed"}
    r = subprocess.run([proc.find_uv(), "lock", "--offline"], cwd=project, env=uv_env, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        pytest.skip(f"uv cannot lock against a local index here: {r.stderr.strip()[-300:]}")
    platform = vendor.host_platform()
    if platform is None:
        pytest.skip(vendor.why_not_host())
    exported = subprocess.run(
        [proc.find_uv(), "export", "--locked", "--offline", "--format", "requirements.txt", "--no-emit-project", "--no-header", "--no-annotate"],
        cwd=project, env=uv_env, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    monkeypatch.setattr(vendor, "LOCK", project / "uv.lock")
    monkeypatch.setattr(vendor, "VENDOR", project / "vendor")
    monkeypatch.setattr(vendor, "BUILD", project / ".build")
    monkeypatch.setattr(vendor, "PYPROJECT", project / "pyproject.toml")
    monkeypatch.setattr(vendor, "export", lambda cfg, env, hashes, frozen=False: exported if hashes else exported)
    monkeypatch.setattr(envs, "left_out", lambda env: [])
    vendored = dataclasses.replace(cfg, vendor=config.VendorConfig([platform]))
    vendor.follow_lock(vendored)
    assert sorted(p.name for p in (project / "vendor" / platform).iterdir()) == ["alpha-1.0-py3-none-any.whl", "beta-2.0-py3-none-any.whl"]
    for page in (tmp_path / "simple", files):  # the index is gone: only vendor/ is left
        for path in sorted(page.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "empty-cache"))
    env = envs.PyEnv("cpython", project / ".venv", python, "only-managed")
    vendor.install(vendored, env, platform)
    out = subprocess.run([env.python, "-c", "import alpha, beta; print(alpha.VERSION, beta.VERSION)"], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["1.0", "2.0"]
    synced = subprocess.run(
        [proc.find_uv(), "sync", "--locked", "--offline"], cwd=project, env={**uv_env, "UV_CACHE_DIR": str(tmp_path / "empty-cache"), "UV_PROJECT_ENVIRONMENT": str(env.dir)},
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert synced.returncode == 0, synced.stderr
    assert "Installed" not in synced.stderr and "Prepared" not in synced.stderr, synced.stderr


# --- doctor, the hook, CI, the config, the command -------------------------------------------------


def test_doctor_lines(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, tmp_path = small
    monkeypatch.setattr(vendor, "host_platform", lambda: "linux-x86_64")
    lines: list[tuple[bool | None, str]] = []
    vendor.doctor(cfg, lambda passed, label, hint="": lines.append((passed, label)))
    assert (False, "vendor/linux-x86_64: 3 file(s) uv.lock names missing") in lines
    vendor.follow_lock(cfg)
    lines.clear()
    vendor.doctor(cfg, lambda passed, label, hint="": lines.append((passed, label)))
    assert [passed for passed, _ in lines if passed is not None] == [True, True]
    assert (None, "vendor: ./libs/local is a local library: never vendored, it installs from its source") in lines
    lines.clear()
    off = make({})
    vendor.doctor(off, lambda passed, label, hint="": lines.append((passed, label)))
    assert lines == [(None, "vendor/ holds linux-x86_64, windows-x86_64, but [vendor] platforms is empty: nothing installs from it")]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True, capture_output=True)


def test_the_hook_wants_vendor_in_line_and_staged(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, tmp_path = small
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    _git(tmp_path, "init", "-q")
    repo = hooks.find_repo(tmp_path, environ={}, cwd=tmp_path)
    result = hooks.check_vendor(cfg, repo)
    assert result.passed is False and "./pyt vendor, then git add vendor" in result.hint
    vendor.follow_lock(cfg)
    result = hooks.check_vendor(cfg, repo)
    assert result.passed is False and result.label == "vendor/ staged" and "git add vendor" in result.hint
    _git(tmp_path, "add", "vendor")
    assert hooks.check_vendor(cfg, repo).passed is True
    _git(tmp_path, "commit", "-qm", "v", "--no-verify")
    (tmp_path / "vendor" / "linux-x86_64" / "alpha-1.0-py3-none-any.whl").unlink()  # deleted, not staged
    vendor.follow_lock(cfg)  # back
    assert hooks.check_vendor(cfg, repo).passed is True


def test_the_ci_checkout_brings_vendor_with_git_lfs(minimal_ci_template: Path) -> None:
    assert "lfs" not in render.ci_workflow(make({}))
    text = render.ci_workflow(make({"vendor": {"platforms": ["linux-x86_64"]}}))
    assert "      - uses: actions/checkout@v7\n        with:\n          lfs: true\n" in text
    shipped = (Path(__file__).resolve().parents[1] / "templates" / "ci.yml").read_text(encoding="utf-8")
    assert "__CHECKOUT_LFS__" in shipped


def test_the_vendor_table_is_checked() -> None:
    assert make({}).vendor.platforms == []
    with pytest.raises(PytError, match="vendor.platforms"):
        make({"vendor": {"platforms": ["linux-x86"]}})
    with pytest.raises(PytError, match="more than once"):
        make({"vendor": {"platforms": ["linux-x86_64", "linux-x86_64"]}})
    with pytest.raises(PytError, match="unknown key"):
        make({"vendor": {"platform": []}})


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--check", "--off"], "--check changes nothing"),
        (["--off", "--platform", "linux-x86_64"], "--off removes every platform"),
        (["--remove", "macos-aarch64"], "is not vendored"),
    ],
)
def test_cmd_vendor_refuses_options_that_disagree(args: list[str], message: str) -> None:
    with pytest.raises(PytError, match=message):
        vendor.cmd_vendor(make({}), args)


def test_cmd_vendor_starts_with_this_machine_and_off_removes_it(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, tmp_path = small
    edits: list[list[tuple[str, str, Any]]] = []
    monkeypatch.setattr(config, "update_file", edits.append)
    monkeypatch.setattr(vendor, "host_platform", lambda: "linux-x86_64")
    monkeypatch.setattr(vendor, "verify", lambda cfg, plan: [])
    monkeypatch.setattr(render, "apply", lambda cfg, **kw: ([], []))
    empty = make({"python": {"cpython": "3.14"}})
    assert vendor.cmd_vendor(empty, []) == 0
    assert edits == [[("vendor", "platforms", ["linux-x86_64"])]] and empty.vendor.platforms == ["linux-x86_64"]
    assert (tmp_path / "vendor" / "linux-x86_64" / "alpha-1.0-py3-none-any.whl").is_file()
    assert vendor.cmd_vendor(empty, ["--platform", "windows-x86_64", "--platform", "windows-x86_64"]) == 0
    assert edits[-1] == [("vendor", "platforms", ["linux-x86_64", "windows-x86_64"])]
    assert vendor.cmd_vendor(empty, ["--remove", "windows-x86_64"]) == 0
    assert not (tmp_path / "vendor" / "windows-x86_64").exists()
    assert vendor.cmd_vendor(empty, ["--check"]) == 0
    assert vendor.cmd_vendor(empty, ["--off"]) == 0
    assert edits[-1] == [("vendor", "platforms", [])] and not (tmp_path / "vendor").exists()
    monkeypatch.setattr(vendor, "host_platform", lambda: None)
    with pytest.raises(PytError, match="name the platforms") as e:
        vendor.cmd_vendor(make({}), [])
    assert e.value.code == 3


def test_q_keeps_what_cmd_vendor_did(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """-q hides progress, never what was asked for (5.3): the folders vendor/ holds now and the
    `git add vendor` to commit them."""
    cfg, tmp_path = small
    monkeypatch.setattr(vendor, "verify", lambda cfg, plan: [])
    monkeypatch.setattr(render, "apply", lambda cfg, **kw: ([], []))
    monkeypatch.setattr(ui, "QUIET", True)
    assert vendor.cmd_vendor(cfg, []) == 0
    err = capsys.readouterr().err
    assert "vendor/: linux-x86_64 (3 files" in err and "git add vendor" in err


def test_cmd_vendor_check_fails_on_a_folder_out_of_line(small: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, tmp_path = small
    monkeypatch.setattr(vendor, "verify", lambda cfg, plan: [])
    assert vendor.cmd_vendor(cfg, ["--check"]) == 1
    vendor.follow_lock(cfg)
    assert vendor.cmd_vendor(cfg, ["--check"]) == 0
    (tmp_path / "vendor" / "linux-x86_64" / "gamma-3.0.tar.gz").write_bytes(b"gamma sdist ONLY")
    assert vendor.cmd_vendor(cfg, ["--check"]) == 1


def test_new_never_copies_a_projects_vendor_folder() -> None:
    from runner import presets

    assert "vendor" in presets.SKIP_AT_ROOT
    assert presets._skipped("vendor/linux-x86_64/x.whl") and not presets._skipped("src/app/vendor/x.py")


def test_every_preset_offers_the_vendor_table() -> None:
    for preset in sorted((Path(__file__).resolve().parents[1] / "presets").glob("*/files/pytemplate.toml")):
        text = preset.read_text(encoding="utf-8")
        assert "\n[vendor]\n" in text and "\nplatforms = []\n" in text, preset
        assert json.loads(json.dumps(config._build(Config, __import__("tomllib").loads(text), "").vendor.platforms)) == []
