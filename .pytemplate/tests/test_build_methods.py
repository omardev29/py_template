"""Build methods (runner/cmd_build.py, runner/methods/*): argv construction, output discovery,
pyz/portable layouts and bootstraps. No network, no packager: the packager calls are recorded."""

from __future__ import annotations

import ast
import hashlib
import importlib.machinery
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_build, config, envs, mypyc, presets, proc, upx  # noqa: E402
from runner.cmd_build import BuildRequest  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.methods import common, exe, nuitka  # noqa: E402
from runner.project import ROOT, SRC  # noqa: E402
from runner.ui import PytError  # noqa: E402

IS_WINDOWS = os.name == "nt"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


TEMPLATE_REPO = (Path(__file__).resolve().parents[1] / "template-repo").is_file()


def _exports_rich() -> bool:
    """Whether `uv export --no-dev` of this project installs rich (the script preset's dependency)."""
    try:
        deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8-sig"))["project"]["dependencies"]
    except (OSError, KeyError, ValueError):
        return False
    roots = {re.sub(r"[-_.]+", "-", m.group(0)).lower() for d in deps if (m := re.match(r"[A-Za-z0-9._-]+", str(d)))}
    return "rich" in presets.locked_names(roots, ROOT / "uv.lock")


# The REAL builds install this project's own uv.lock and run an app that imports rich (a script
# preset project; a raylib project locks no rich)
needs_rich = pytest.mark.skipif(not _exports_rich(), reason="installs this project's uv.lock and imports rich, which it does not lock")


def skip_when_older_than(cfg: Config) -> None:
    """Skip a test that starts what it built with sys.executable when this interpreter is older
    than the build's Python (the template's CI runs the suite on the runner's floor, 3.11)."""
    if sys.version_info[:2] < tuple(int(part) for part in cfg.min_python.split(".")):
        pytest.skip(f"runs the build with Python {sys.version_info[0]}.{sys.version_info[1]}, older than the {cfg.min_python} it needs")


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


def real(data: dict[str, Any]) -> Config:
    """make() for a test that runs uv against this project's .venv: with its python.cpython (the
    template's default, 3.14, is not the interpreter of a project on another minor)."""
    own = config.load(set()).python.cpython
    return make({**data, "python": {"cpython": own, **data.get("python", {})}})


def fake_app(root: Path, pkg: str = "myapp", *, assets: bool = True) -> Path:
    """A payload as cmd_build.payload returns it: main.py, the package, assets/."""
    (root / pkg / "core").mkdir(parents=True)
    (root / "main.py").write_text(f"from {pkg} import app\n", encoding="utf-8")
    (root / pkg / "__init__.py").write_text("", encoding="utf-8")
    (root / pkg / "app.py").write_text("def main() -> None:\n    print('hi')\n", encoding="utf-8")
    (root / pkg / "core" / "__init__.py").write_text("", encoding="utf-8")
    if assets:
        (root / "assets").mkdir()
        (root / "assets" / "logo.txt").write_text("logo", encoding="utf-8")
    return root


class Recorder:
    """Stands in for envs.uv / envs.uv_run: records (argv, cwd, extra_env) and runs a side effect."""

    def __init__(self, effect: Any = None) -> None:
        self.calls: list[tuple[list[str], Path | None, dict[str, str]]] = []
        self.effect = effect

    def __call__(self, env: object, argv: Any, *, cwd: Path | None = None, extra_env: Any = None, **_: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        self.calls.append((args, cwd, dict(extra_env or {})))
        if self.effect is not None:
            self.effect(args, cwd)
        return subprocess.CompletedProcess(args, 0, "", "")

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """BUILD and DIST under tmp_path for every method module (never the project's own)."""
    from runner.methods import flet, nuitka, pyz

    build = tmp_path / "build"
    for module in (exe, nuitka, flet, pyz, common):
        monkeypatch.setattr(module, "BUILD", build)
    monkeypatch.setattr(cmd_build, "DIST", tmp_path / "dist")
    yield tmp_path


# --- exe (PyInstaller) ----------------------------------------------------------------------------


def _pyinstaller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: Config, backend: str = "cpython", **req: Any) -> tuple[Path, Recorder]:
    """Run exe.build with PyInstaller recorded; the fake writes what PyInstaller would."""
    app = fake_app(tmp_path / "payload", cfg.pkg)

    def effect(args: list[str], cwd: Path | None) -> None:
        out = Path(args[args.index("--distpath") + 1])
        name = args[args.index("--name") + 1]
        if "--onefile" in args:
            out.mkdir(parents=True, exist_ok=True)
            (out / (name + (".exe" if IS_WINDOWS else ""))).write_bytes(b"exe")
        else:
            (out / name).mkdir(parents=True)
            (out / name / (name + (".exe" if IS_WINDOWS else ""))).write_bytes(b"exe")

    rec = Recorder(effect)
    monkeypatch.setattr(envs, "uv_run", rec)
    monkeypatch.setattr(mypyc, "hidden_imports", lambda cfg, stage: [f"{cfg.pkg}__mypyc", f"{cfg.pkg}.core", "typing", "typing"])
    monkeypatch.setattr(mypyc, "exe_stage", lambda cfg, src, dst: Path(shutil.copytree(src, dst)))
    result = exe.build(BuildRequest(cfg, backend, "exe", app, **req))
    return result, rec


def test_exe_default_argv_and_output(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({})
    result, rec = _pyinstaller(sandbox, monkeypatch, cfg)
    argv = rec.argv
    stage = sandbox / "build" / "exe-stage" / "cpython"
    assert argv[:4] == ["python", "-m", "PyInstaller", str(stage / "main.py")]
    assert argv[argv.index("--name") + 1] == "myapp"
    assert "--onefile" in argv and "--onedir" not in argv
    assert argv[argv.index("--optimize") + 1] == "1"
    assert argv[argv.index("--python-option") + 1] == "X utf8"  # UTF-8 as in development
    assert "--noupx" in argv and "--noconsole" not in argv  # a console app by default
    # relative to the folder of the spec, which PyInstaller reads a relative data source from
    source, dest = argv[argv.index("--add-data") + 1].rsplit(":", 1)
    spec = Path(argv[argv.index("--specpath") + 1])
    assert dest == "assets" and (spec / source).resolve() == (stage / "assets").resolve()
    assert "--hidden-import" not in argv  # cpython: PyInstaller sees every import itself
    assert result == sandbox / "dist" / "myapp-cpython-exe" / ("myapp" + (".exe" if IS_WINDOWS else ""))
    assert result.is_file()


@pytest.mark.parametrize(("gui", "console", "expected"), [(False, "auto", True), (True, "auto", False), (True, "yes", True), (False, "no", False)])
def test_exe_console_follows_gui_and_console(sandbox: Path, monkeypatch: pytest.MonkeyPatch, gui: bool, console: str, expected: bool) -> None:
    cfg = make({"app": {"gui": gui}, "deploy": {"exe": {"console": console}}})
    _, rec = _pyinstaller(sandbox, monkeypatch, cfg)
    assert ("--noconsole" not in rec.argv) is expected


def test_exe_mypyc_hidden_imports_icon_and_extra_args_order(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({"backend": {"active": "mypyc"}, "deploy": {"exe": {"mode": "onedir", "icon": "art/app.ico", "extra_args": ["--collect-all", "x"]}}})
    result, rec = _pyinstaller(sandbox, monkeypatch, cfg, "mypyc", extra=["--log-level", "DEBUG"])
    argv = rec.argv
    hidden = [argv[i + 1] for i, a in enumerate(argv) if a == "--hidden-import"]
    assert hidden == sorted({"myapp__mypyc", "myapp.core", "typing"})  # sorted, no duplicates
    from runner.project import ROOT

    assert argv[argv.index("--icon") + 1] == str(ROOT / "art" / "app.ico")
    assert argv[-4:] == ["--collect-all", "x", "--log-level", "DEBUG"]  # extra_args, then the command line
    assert "--onedir" in argv
    assert result == sandbox / "dist" / "myapp-mypyc-exe" / "myapp" and result.is_dir()


def _pyinstaller_split(value: str, pathsep: str) -> tuple[str, str]:
    """--add-data SOURCE:DEST as PyInstaller 6.22.3 reads it (makespec.SourceDestAction): the one
    separator, ':' or os.pathsep, that is not a Windows drive's."""
    (separator,) = (m for m in re.finditer(rf"(^\w:[/\\])|[:{pathsep}]", value) if not m[1])
    return value[: separator.start()], value[separator.end() :]


@pytest.mark.parametrize("flet", [False, True])
def test_exe_assets_hold_no_separator_pyinstaller_splits_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flet: bool) -> None:
    # A project in C:\Users\me\games;2026\proj: PyInstaller found two separators in the absolute
    # source (';' is os.pathsep on Windows) and stopped with "Wrong syntax, should be
    # --add-data=SOURCE:DEST"; flet pack hands the value to PyInstaller unchanged
    root = tmp_path / "games;2026"
    monkeypatch.setattr(exe, "BUILD", root / "build")
    monkeypatch.setattr(cmd_build, "DIST", root / "dist")
    if flet:
        rec = _flet_pack(root, monkeypatch, _flet_cfg(), "cpython", windows=True, macos=False)
        spec = root / "build" / "flet-pack" / "cpython"  # flet pack writes the spec in its cwd
    else:
        _, rec = _pyinstaller(root, monkeypatch, make({}))
        spec = root / "build" / "pyinstaller" / "cpython"  # --specpath
    value = rec.argv[rec.argv.index("--add-data") + 1]
    for pathsep in (":", ";"):  # os.pathsep on POSIX and on Windows
        source, dest = _pyinstaller_split(value, pathsep)
        assert dest == "assets" and (spec / source).resolve() == (root / "build" / "exe-stage" / "cpython" / "assets").resolve()


def test_exe_without_assets_folder_adds_no_data(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({"app": {"assets": ""}})
    _, rec = _pyinstaller(sandbox, monkeypatch, cfg)
    assert "--add-data" not in rec.argv


# --- UPX in the exe method: PyInstaller only packs on Windows --------------------------------------


def test_exe_size_args_skip_upx_off_windows(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # PyInstaller's configure.get_config disables UPX on every non-Windows OS: downloading UPX
    # there (or failing offline with exit 3) for a step PyInstaller skips was pointless
    monkeypatch.setattr(exe, "IS_WINDOWS", False)
    monkeypatch.setattr(upx, "unsupported_reason", lambda: "")

    def no_find(cfg: Config) -> Path:
        raise AssertionError("upx.find must not run: PyInstaller would not use it")

    monkeypatch.setattr(upx, "find", no_find)
    args, env = exe.size_args(make({"deploy": {"upx": {"enabled": True}}}))
    assert "--noupx" in args and env == {}
    assert not [a for a in args if a.startswith(("--upx-dir", "--upx-exclude"))]
    assert "only on Windows" in capsys.readouterr().err
    # UPX off: no warning at all
    args, _ = exe.size_args(make({}))
    assert "--noupx" in args and capsys.readouterr().err == ""


def test_exe_size_args_say_why_upx_is_off_on_macos(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # On macOS neither warning fired (the Windows-only one skipped hosts UPX does not support,
    # and upx.active, which names them, was never called): UPX was dropped without a word
    monkeypatch.setattr(exe, "IS_WINDOWS", False)
    monkeypatch.setattr(upx, "IS_MACOS", True)
    monkeypatch.setattr(upx, "find", lambda cfg: pytest.fail("upx.find must not run"))
    args, env = exe.size_args(make({"deploy": {"upx": {"enabled": True}}}))
    assert "--noupx" in args and env == {}
    err = capsys.readouterr().err
    assert "UPX cannot pack current macOS binaries" in err and "not UPX-packed" in err
    exe.size_args(make({}))
    assert capsys.readouterr().err == ""


def test_exe_size_args_use_upx_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(exe, "IS_WINDOWS", True)
    monkeypatch.setattr(upx, "unsupported_reason", lambda: "")
    monkeypatch.setattr(upx, "find", lambda cfg: tmp_path / "upx.exe")
    args, env = exe.size_args(make({"deploy": {"upx": {"enabled": True, "level": "7", "exclude": ["flutter*.dll"]}}}))
    assert f"--upx-dir={tmp_path}" in args and "--noupx" not in args
    assert "--upx-exclude=flutter_windows.dll" in args and "--upx-exclude=flutter*.dll" in args
    assert env == {"UPX": "-7"}


# --- exe with the flet preset: flet pack ---------------------------------------------------------


def _flet_cfg(name: str = "fletdemo", deploy: dict[str, Any] | None = None, **exe_cfg: Any) -> Config:
    return make(
        {
            "app": {"name": name, "preset": "flet", "gui": True},
            "backend": {"active": "cpython", "supported": ["cpython", "mypyc"]},
            "compile": {"modules": [f"{name.replace('-', '_').lower()}.core"]},
            "deploy": {"exe": {"mode": "onedir", **exe_cfg}, **(deploy or {})},
        }
    )


def _flet_pack(sandbox: Path, monkeypatch: pytest.MonkeyPatch, cfg: Config, backend: str, *, windows: bool, macos: bool) -> Recorder:
    app = fake_app(sandbox / "payload", cfg.pkg)

    def effect(args: list[str], cwd: Path | None) -> None:
        # What flet pack 1.0.1 writes: PyInstaller's output into <cwd>/<--distpath>, then (Linux)
        # a desktop entry next to it whose Exec is the absolute path of the executable
        assert cwd is not None
        dist = cwd / args[args.index("--distpath") + 1]
        name = cfg.app.name
        (dist / name).mkdir(parents=True)
        (dist / name / name).write_bytes(b"\x7fELF")
        if not windows and not macos:
            (dist / f"{name}.desktop").write_text(f'[Desktop Entry]\nType=Application\nExec="{dist / name / name}"\n', encoding="utf-8")

    rec = Recorder(effect)
    monkeypatch.setattr(exe, "IS_WINDOWS", windows)
    monkeypatch.setattr(exe, "IS_MACOS", macos)
    monkeypatch.setattr(envs, "uv_run", rec)
    monkeypatch.setattr(mypyc, "hidden_imports", lambda cfg, stage: [f"{cfg.pkg}.core"])
    monkeypatch.setattr(mypyc, "exe_stage", lambda cfg, src, dst: Path(shutil.copytree(src, dst)))
    out = exe.build(BuildRequest(cfg, backend, "exe", app))
    assert out == sandbox / "dist" / f"{cfg.app.name}-{backend}-exe" and (out / cfg.app.name).is_dir()
    assert rec.calls[-1][1] == sandbox / "build" / "flet-pack" / backend  # flet pack wipes <cwd>/build
    return rec


@pytest.mark.parametrize("backend", ["cpython", "mypyc"])
@pytest.mark.parametrize("name", ["fletdemo", "myapp", "My-App"])
def test_flet_pack_onedir_linux_keeps_internal(sandbox: Path, monkeypatch: pytest.MonkeyPatch, name: str, backend: str) -> None:
    # With --contents-directory=. the executable dist/<name>/<name> was a FILE exactly where the
    # package folder dist/<name>/<pkg>/core/*.so had to go (app.name == pkg, the default of
    # ./pyt new): PyInstaller's COLLECT failed with NotADirectoryError
    argv = _flet_pack(sandbox, monkeypatch, _flet_cfg(name), backend, windows=False, macos=False).argv
    assert argv[:3] == ["flet", "pack", str(sandbox / "build" / "exe-stage" / backend / "main.py")]
    assert "--onedir" in argv
    assert not [a for a in argv if "--contents-directory" in a]


def test_flet_pack_onedir_windows_is_flat(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = _flet_pack(sandbox, monkeypatch, _flet_cfg(), "mypyc", windows=True, macos=False).argv
    assert "--onedir" in argv and "--pyinstaller-build-args=--contents-directory=." in argv


def test_flet_pack_macos_is_never_onedir(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv = _flet_pack(sandbox, monkeypatch, _flet_cfg(), "cpython", windows=False, macos=True).argv
    assert "--onedir" not in argv and not [a for a in argv if "--contents-directory" in a]


def test_flet_pack_desktop_entry_names_the_shipped_executable(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # flet pack writes the Linux desktop entry after PyInstaller, with an absolute Exec built from
    # --distpath: that pointed into .build/flet-pack/<b>/dist, which the build then moved to
    # dist/ (the entry launched nothing). The output folder itself is the distpath now.
    cfg = _flet_cfg()
    _flet_pack(sandbox, monkeypatch, cfg, "cpython", windows=False, macos=False)
    out = sandbox / "dist" / "fletdemo-cpython-exe"
    entry = (out / "fletdemo.desktop").read_text(encoding="utf-8")
    executable = Path(re.search(r'^Exec="(.*)"$', entry, re.MULTILINE).group(1))  # type: ignore[union-attr]
    assert executable.is_file() and executable.parent.parent == out


def test_flet_pack_cleans_pyinstallers_cache(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # PyInstaller's binary cache (global, keyed without the UPX level) handed back binaries
    # packed at another deploy.upx.level: the plain exe build passes --clean, flet pack did not
    argv = _flet_pack(sandbox, monkeypatch, _flet_cfg(), "cpython", windows=True, macos=False).argv
    assert "--pyinstaller-build-args=--clean" in argv


@pytest.mark.parametrize("method", ["exe", "flet pack", "nuitka"])
def test_an_output_in_use_is_a_clear_error_before_the_packager_runs(sandbox: Path, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    # Windows refuses to delete a running exe or a loaded DLL: rebuilding while the previous
    # build ran gave an "internal runner error" traceback (after Nuitka's minutes of work)
    cfg = _flet_cfg() if method == "flet pack" else make({})
    name = {"exe": "myapp-cpython-exe", "flet pack": "fletdemo-cpython-exe", "nuitka": "myapp-cpython-nuitka"}[method]
    out = sandbox / "dist" / name
    (out / "sub").mkdir(parents=True)
    (out / "sub" / "app.exe").write_bytes(b"MZ")

    def locked(src: Any, dst: Any) -> None:  # Windows: a folder whose exe runs cannot be moved
        raise PermissionError(13, "The process cannot access the file because it is being used by another process", str(out / "sub" / "app.exe"))

    monkeypatch.setattr(common, "_move", locked)
    started: list[list[str]] = []
    monkeypatch.setattr(envs, "uv_run", lambda env, argv, **k: started.append([str(a) for a in argv]))
    monkeypatch.setattr(envs, "uv", lambda env, argv, **k: started.append([str(a) for a in argv]))
    monkeypatch.setattr(exe, "IS_WINDOWS", True)
    monkeypatch.setattr(nuitka, "IS_WINDOWS", True)
    app = fake_app(sandbox / "payload", cfg.pkg)
    module = nuitka if method == "nuitka" else exe
    with pytest.raises(PytError) as e:
        module.build(BuildRequest(cfg, "cpython", "nuitka" if method == "nuitka" else "exe", app))
    assert f"dist{os.sep}{name}" in str(e.value) or f"dist/{name}" in str(e.value)
    assert "still running" in str(e.value) and "app.exe" in str(e.value)
    assert started == []  # refused before the packager ran


@pytest.mark.parametrize(("console", "expected"), [("auto", False), ("yes", True), ("no", False)])
def test_flet_pack_console_and_utf8(sandbox: Path, monkeypatch: pytest.MonkeyPatch, console: str, expected: bool) -> None:
    # flet pack always adds --noconsole unless --debug-console has a value: deploy.exe.console
    # was ignored. And the exe ran without UTF-8 mode, unlike ./pyt run and PyInstaller.
    argv = _flet_pack(sandbox, monkeypatch, _flet_cfg(console=console), "cpython", windows=True, macos=False).argv
    assert ("--debug-console=true" in argv) is expected
    assert "--pyinstaller-build-args=--python-option=X utf8" in argv
    assert "--pyinstaller-build-args=--optimize=1" in argv
    assert "--pyinstaller-build-args=--noupx" in argv


# --- nuitka ---------------------------------------------------------------------------------------

EXT = importlib.machinery.EXTENSION_SUFFIXES[0]


def _run_locate(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the REAL locate snippet of nuitka.includable (as `uv run python -c` would)."""
    assert args[:4] == ["run", "--locked", "python", "-c"], args
    return subprocess.run([sys.executable, *args[3:]], capture_output=True, text=True, check=True, timeout=120)


class FakeNuitka:
    """envs.uv stand-in: the locate snippet runs for real; `nuitka` writes the layout of Nuitka
    4.2.2 (standalone: main.dist/<basename of --output-filename>; onefile: <output-dir>/<name>)."""

    def __init__(self, pkg: str, *, produce: bool = True) -> None:
        self.pkg = pkg
        self.produce = produce
        self.argv: list[str] = []
        self.locate: list[list[str]] = []

    def __call__(self, env: object, argv: Any, *, cwd: Path | None = None, **_: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        if "--with" not in args:
            self.locate.append(args)
            return _run_locate(args)
        self.argv = args
        assert cwd is not None and args[args.index("--with") + 1] == nuitka.NUITKA
        if not self.produce:
            return subprocess.CompletedProcess(args, 0, "", "")
        out = Path(next(a for a in args if a.startswith("--output-dir=")).split("=", 1)[1])
        name = os.path.basename(next(a for a in args if a.startswith("--output-filename=")).split("=", 1)[1])
        if "--mode=onefile" in args:
            out.mkdir(parents=True, exist_ok=True)
            (out / name).write_bytes(b"\x7fELF onefile")
            return subprocess.CompletedProcess(args, 0, "", "")
        dist = out / "main.dist"
        dist.mkdir(parents=True)
        (dist / name).write_bytes(b"\x7fELF")
        # The compiled package goes next to the binary; case-insensitive like macOS' APFS
        if any(p.name.lower() == self.pkg.lower() for p in dist.iterdir()):
            raise NotADirectoryError(f"{dist / self.pkg / 'core'}: Not a directory")
        (dist / self.pkg / "core").mkdir(parents=True)
        (dist / self.pkg / "core" / f"bench{EXT}").write_bytes(b"")
        return subprocess.CompletedProcess(args, 0, "", "")


def _nuitka_app(root: Path, pkg: str) -> Path:
    app = fake_app(root, pkg)
    (app / pkg / "core" / f"bench{EXT}").write_bytes(b"")
    (app / f"{pkg}__mypyc{EXT}").write_bytes(b"")
    return app


@pytest.mark.parametrize("backend", ["cpython", "mypyc"])
@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("name", ["myapp", "MyApp", "my-app", "My_App"])
def test_nuitka_standalone_binary_never_clashes_with_the_package(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, name: str, windows: bool, backend: str
) -> None:
    # Nuitka standalone writes main.dist/<output-filename> NEXT TO main.dist/<pkg>/ (mypyc
    # extensions): with app.name == pkg (the default of ./pyt new) and no .exe the binary
    # was a FILE where the package folder had to go -> NotADirectoryError after minutes of work
    cfg = make({"app": {"name": name}, "compile": {"modules": [f"{name.replace('-', '_').lower()}.core"]}})
    app = _nuitka_app(sandbox / "payload", cfg.pkg)
    fake = FakeNuitka(cfg.pkg)
    monkeypatch.setattr(nuitka, "IS_WINDOWS", windows)
    monkeypatch.setattr(envs, "uv", fake)
    monkeypatch.setattr(mypyc, "exe_stage", lambda cfg, src, dst: Path(shutil.copytree(src, dst)))
    monkeypatch.setattr(mypyc, "hidden_imports", lambda cfg, stage: [f"{cfg.pkg}.core.bench", f"{cfg.pkg}__mypyc"])
    monkeypatch.setattr(mypyc, "compiled_modules", lambda cfg: [f"{cfg.pkg}.core.bench"])
    out = nuitka.build(BuildRequest(cfg, backend, "nuitka", app))
    assert "--mode=standalone" in fake.argv
    assert out == sandbox / "dist" / f"{name}-{backend}-nuitka"
    binaries = [p.name for p in out.iterdir() if p.is_file()]
    assert len(binaries) == 1 and binaries[0].lower() != cfg.pkg
    if windows:
        assert binaries == [f"{name}.exe"]
    else:
        assert binaries == [name + (".bin" if name.lower() == cfg.pkg else "")]  # renamed only when it clashes
    assert (out / cfg.pkg / "core").is_dir()


def test_nuitka_onefile_keeps_the_plain_name_and_moves_the_file(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({})
    app = _nuitka_app(sandbox / "payload", cfg.pkg)
    monkeypatch.setattr(nuitka, "IS_WINDOWS", False)
    monkeypatch.setattr(envs, "uv", FakeNuitka(cfg.pkg))
    result = nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app, onefile=True))
    assert result == sandbox / "dist" / "myapp-cpython-nuitka" / "myapp" and result.read_bytes() == b"\x7fELF onefile"


def test_nuitka_starts_the_compiler_even_for_an_app_named_nuitka(sandbox: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`python -m nuitka` ran in the stage, whose own package nuitka/ came first on sys.path:
    "'nuitka' is a package and cannot be directly executed". The command the build runs is
    replayed here with a stand-in compiler on PYTHONPATH (where the tools env keeps Nuitka)."""
    cfg = make({"app": {"name": "nuitka"}, "compile": {"modules": ["nuitka.core"]}})
    app = _nuitka_app(sandbox / "payload", cfg.pkg)
    seen: dict[str, Any] = {}

    def uv(env: object, argv: Any, *, cwd: Path | None = None, **_: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        if "--with" not in args:
            return _run_locate(args)
        seen.update(argv=args[args.index("--with") + 2 :], cwd=cwd)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(envs, "uv", uv)
    with pytest.raises(PytError):  # the stand-in above builds nothing
        nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))
    compiler = tmp_path / "site" / "nuitka"
    compiler.mkdir(parents=True)
    (compiler / "__init__.py").write_text("", encoding="utf-8")
    (compiler / "__main__.py").write_text("print('the compiler')\n", encoding="utf-8")
    assert seen["argv"][0] == "python" and (Path(seen["cwd"]) / "nuitka" / "__init__.py").is_file()
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "site")}
    r = subprocess.run([sys.executable, *seen["argv"][1:]], cwd=seen["cwd"], env=env, capture_output=True, text=True, check=False)
    assert (r.returncode, r.stdout.strip()) == (0, "the compiler"), r.stderr


def test_nuitka_without_output_is_a_clear_error(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({})
    app = _nuitka_app(sandbox / "payload", cfg.pkg)
    monkeypatch.setattr(envs, "uv", FakeNuitka(cfg.pkg, produce=False))
    with pytest.raises(PytError, match=r"\*\.dist folder"):
        nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))
    with pytest.raises(PytError, match="without producing myapp"):
        nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app, onefile=True))


@pytest.mark.parametrize("optimize", [0, 1, 2])
def test_nuitka_argv_follows_the_config(sandbox: Path, monkeypatch: pytest.MonkeyPatch, optimize: int) -> None:
    deploy = {
        "optimize": optimize,
        "exclude_modules": ["PIL", "ssl"],
        "exe": {"icon": "art/app.ico"},
        "nuitka": {"extra_args": ["--lto=no"]},
        "upx": {"enabled": True},
    }
    cfg = make({"app": {"gui": True}, "deploy": deploy})
    app = _nuitka_app(sandbox / "payload", cfg.pkg)
    fake = FakeNuitka(cfg.pkg)
    # A project under a folder named with '#' (C:\dev\C#\game): Nuitka reads what follows the
    # last '#' of the icon option as an icon index, and stopped the build
    root = sandbox / "C#" / "game"
    (root / "art").mkdir(parents=True)
    (root / "art" / "app.ico").write_bytes(b"\x00\x00\x01\x00icon")
    monkeypatch.setattr(nuitka, "ROOT", root)
    monkeypatch.setattr(nuitka, "IS_WINDOWS", True)
    monkeypatch.setattr(envs, "uv", fake)
    monkeypatch.setattr(upx, "active", lambda cfg: True)
    monkeypatch.setattr(upx, "find", lambda cfg: Path("/opt/upx/upx"))
    packed: list[Path] = []
    monkeypatch.setattr(upx, "pack_tree", lambda cfg, root: packed.append(root) or [])
    out = nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app, extra=["--report=r.xml"]))
    argv = fake.argv
    stage = sandbox / "build" / "nuitka-stage" / "cpython"
    # -P: the stage (the cwd) is not on Nuitka's own sys.path, so an app named nuitka never runs instead
    assert argv[argv.index(nuitka.NUITKA) + 1 :][:5] == ["python", "-P", "-m", "nuitka", str(stage / "main.py")]
    assert "--output-filename=myapp.exe" in argv and "--include-package=myapp" in argv
    assert ("--python-flag=no_asserts" in argv) is (optimize >= 1)
    assert ("--python-flag=no_docstrings" in argv) is (optimize >= 2)
    assert "--nofollow-import-to=PIL" in argv and "--nofollow-import-to=ssl" in argv
    assert "--windows-console-mode=disable" in argv  # app.gui on Windows
    # The icon is copied into the stage and named relative to it (Nuitka's cwd), with no '#'
    # for Nuitka's "ICON#N" split: what its option check reads, os.path.exists from the stage
    icon = next(a for a in argv if a.startswith("--windows-icon-from-ico=")).split("=", 1)[1]
    assert icon == "pyt-icon.ico" and "#" not in icon
    assert (stage / icon).read_bytes() == (root / "art" / "app.ico").read_bytes()
    assert "--include-raw-dir=assets=assets" in argv  # relative to the stage: Nuitka splits a path at ',' and '='
    assert not [a for a in argv if str(a).startswith("--include-data-dir")]  # it leaves out assets named like code
    assert not any(str(stage) in str(a) for a in argv if str(a).startswith(("--include-data", "--include-raw")))
    # a standalone folder is packed when it is done (the excludes apply), never by Nuitka's plugin
    assert "--plugin-enable=upx" not in argv and packed == [out]
    assert argv[-2:] == ["--lto=no", "--report=r.xml"]  # extra_args, then the command line
    assert not [a for a in argv if a.startswith("--include-module=")]  # cpython: Nuitka follows the imports


def test_nuitka_names_a_missing_icon_before_it_runs(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({"deploy": {"exe": {"icon": "art/none.ico"}}})
    app = _nuitka_app(sandbox / "payload", cfg.pkg)
    monkeypatch.setattr(nuitka, "ROOT", sandbox)
    monkeypatch.setattr(nuitka, "IS_WINDOWS", True)
    monkeypatch.setattr(envs, "uv", lambda *a, **k: pytest.fail("Nuitka ran without its icon"))
    with pytest.raises(PytError, match=r"deploy\.exe\.icon = 'art/none\.ico' does not exist"):
        nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))


# Runs in an environment with the pinned Nuitka, in the stage (Nuitka's cwd): its own option
# parsing of the data options nuitka.build passes, then its own collection of the data files. The
# launching process's helper that nuitka/__main__.py installs is given its "no parent" answer.
_NUITKA_DATA_FILES = r"""
import json, sys
import nuitka
nuitka.getLaunchingNuitkaProcessEnvironmentValue = lambda name: None
sys.argv = ["nuitka", "--mode=standalone", *sys.argv[1:], "main.py"]
from nuitka.options import Options
Options.parseArgs()
from nuitka.freezer import IncludedDataFiles
found = sorted(f.dest_path.replace("\\", "/") for f in IncludedDataFiles._addIncludedDataFilesFromFileOptions())
print("PTDATA" + json.dumps(found))
"""


def test_nuitka_ships_every_asset_whatever_its_suffix(sandbox: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--include-data-dir copies only what Nuitka calls non-code files: its default_ignored_suffixes
    (.py .pyw .pyc .pyo .pyi .so .pyd .pyx .dll .dylib .exe .bin and the extension suffixes) left a
    game's level1.bin, a model.bin, a helper tool.exe, a plugin.dll, libfmod.so or a level script
    out of every nuitka build, without a word ("ok done"; the app then died on FileNotFoundError).
    The pinned Nuitka (from uv's cache, offline) parses the data options nuitka.build passes and
    collects the files itself."""
    cfg = make({})
    app = _nuitka_app(sandbox / "payload", cfg.pkg)
    assets = ["logo.png", "notes.txt", "model.bin", "tool.exe", "plugin.dll", "libfmod.so", "levels/level1.bin", "levels/script.py", "levels/data.pyd"]
    for name in assets:
        (app / "assets" / name).parent.mkdir(parents=True, exist_ok=True)
        (app / "assets" / name).write_bytes(b"x")
    fake = FakeNuitka(cfg.pkg)
    monkeypatch.setattr(envs, "uv", fake)
    nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))
    data = [a for a in fake.argv if a.startswith(("--include-data", "--include-raw"))]
    assert data == ["--include-raw-dir=assets=assets"]
    stage = sandbox / "build" / "nuitka-stage" / "cpython"
    probe = tmp_path / "probe.py"
    probe.write_text(_NUITKA_DATA_FILES, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_PROJECT", "UV_PYTHON", "VIRTUAL_ENV", "PYTEMPLATE_"))}
    with_nuitka = [proc.find_uv(), "run", "--offline", "--no-project", "--python", sys.executable, "--with", nuitka.NUITKA, "python"]

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*with_nuitka, *args], cwd=stage, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300, check=False)

    ready = run("-c", "import nuitka")
    if ready.returncode != 0:
        pytest.skip(f"{nuitka.NUITKA} is not in the uv cache: {ready.stderr.strip()[-300:]}")
    r = run(str(probe), *data)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("PTDATA")), None)
    assert line is not None, r.stdout[-3000:] + r.stderr[-3000:]
    assert json.loads(line[len("PTDATA") :]) == sorted(f"assets/{name}" for name in ["logo.txt", *assets])


def test_nuitka_upx_honours_the_excludes(sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # Nuitka's upx plugin has no exclude option: in a standalone folder it packed every DLL it
    # copied, the Python DLL and the files of deploy.upx.exclude (a DLL UPX breaks) included
    cfg = make({"deploy": {"upx": {"enabled": True, "exclude": ["mylib*.dll"]}}})
    app = _nuitka_app(sandbox / "payload", cfg.pkg)
    fake = FakeNuitka(cfg.pkg)

    def nuitka_with_dlls(env: object, argv: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        done = fake(env, argv, **kw)
        dist = sandbox / "build" / "nuitka" / "cpython" / "main.dist"
        if dist.is_dir():
            for name in ("python314.dll", "vcruntime140.dll", "mylib_core.dll", "libssl-3.dll"):
                (dist / name).write_bytes(b"MZ")
        return done

    monkeypatch.setattr(envs, "uv", nuitka_with_dlls)
    monkeypatch.setattr(nuitka, "IS_WINDOWS", True)
    monkeypatch.setattr(upx, "IS_WINDOWS", True)
    monkeypatch.setattr(upx, "active", lambda cfg: True)
    monkeypatch.setattr(upx, "find", lambda cfg: Path("/opt/upx/upx"))
    packed: list[str] = []

    def pack_file(binary: Path, path: Path, flags: list[str]) -> upx.Result:
        packed.append(path.name)
        return upx.Result(path, 2, 1, "packed")

    monkeypatch.setattr(upx, "pack_file", pack_file)
    nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))
    assert "--plugin-enable=upx" not in fake.argv
    # never python3*.dll, the C runtime or mylib*; the compiled module FakeNuitka writes is a PE
    # .pyd, packed like any extension, on Windows only (a .so elsewhere: no PE name)
    pyd = [f"bench{EXT}"] if EXT.endswith(".pyd") else []
    assert sorted(packed) == sorted(["libssl-3.dll", "myapp.exe", *pyd])
    # onefile: the plugin packs the one binary (the libraries inside its payload never)...
    packed.clear()
    nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app, onefile=True))
    assert "--plugin-enable=upx" in fake.argv and f"--upx-binary={Path('/opt/upx/upx')}" in fake.argv and packed == []
    # ...unless its own name is excluded
    cfg = make({"deploy": {"upx": {"enabled": True, "exclude": ["MyApp*"]}}})
    nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app, onefile=True))
    assert "--plugin-enable=upx" not in fake.argv and packed == []
    assert "myapp.exe matches deploy.upx.exclude: not packed" in capsys.readouterr().err


def test_nuitka_includable_drops_what_the_build_env_cannot_locate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A compiled module with `if sys.platform == "win32": import winreg` (or an optional
    # `try: import orjson`) made Nuitka stop: "FATAL: Error, failed to locate module 'winreg'"
    other_os = "fcntl" if IS_WINDOWS else "winreg"
    stage = tmp_path / "stage"
    (stage / "myapp" / "core").mkdir(parents=True)
    (stage / "myapp" / "__init__.py").write_text("", encoding="utf-8")
    (stage / "myapp" / "boundary.py").write_text("", encoding="utf-8")
    (stage / "myapp" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (stage / "myapp" / "core" / f"m{EXT}").write_bytes(b"")
    (stage / f"myapp__mypyc{EXT}").write_bytes(b"")
    fake = FakeNuitka("myapp")
    monkeypatch.setattr(envs, "uv", fake)
    monkeypatch.setattr(mypyc, "compiled_modules", lambda cfg: ["myapp.core.m"])
    names = ["myapp.core.m", "myapp__mypyc", "json", "os.path", "sys", other_os, "not_installed_xyz", "myapp.boundary", "json"]
    kept = nuitka.includable(make({}), stage, names)
    assert kept == sorted({"myapp.core.m", "myapp__mypyc", "json", "os.path", "myapp.boundary"})
    assert len(fake.locate) == 1
    asked = fake.locate[0][fake.locate[0].index(str(stage)) + 1 :]
    assert "myapp.core.m" not in asked and "myapp__mypyc" not in asked  # compiled names: always kept
    # Nothing to resolve: no process at all
    assert nuitka.includable(make({}), stage, ["myapp.core.m"]) == ["myapp.core.m"] and len(fake.locate) == 1


def test_nuitka_includable_reports_a_broken_resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mypyc, "compiled_modules", lambda cfg: [])
    monkeypatch.setattr(envs, "uv", lambda *a, **k: subprocess.CompletedProcess([], 0, "a .pth file printed this\n", ""))
    with pytest.raises(PytError, match="could not check the imports"):
        nuitka.includable(make({}), tmp_path, ["json"])


def test_nuitka_pin_supports_the_default_python() -> None:
    # Raising the template's default python.cpython beyond what the pinned Nuitka supports would
    # break --method nuitka for every new project: bump NUITKA and NUITKA_PYTHON with it
    assert nuitka._minor(make({}).python.cpython) <= nuitka._minor(nuitka.NUITKA_PYTHON)
    assert nuitka.NUITKA_PYTHON.count(".") == 1


@pytest.mark.parametrize("extra", [[], ["--experimental=python3.15"], ["--experimental", "python3.15"]])
def test_nuitka_python_newer_than_the_pin_is_refused_before_any_work(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: list[str]) -> None:
    # nuitka==4.2.2 stops with FATAL on CPython 3.15 after the checks and the mypyc compile
    def must_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the build went ahead")

    monkeypatch.setattr(cmd_build, "run_checks", must_not_run)
    monkeypatch.setattr(cmd_build, "payload", must_not_run)
    monkeypatch.setattr(cmd_build, "check_lock", lambda cfg: None)  # uv would download a CPython 3.15
    # nuitka.check_options refuses a project folder SCons would expand (app$v2): not this test's
    monkeypatch.setattr(nuitka, "BUILD", tmp_path / ".build")
    cfg = make({"python": {"cpython": "3.15"}})
    if not extra:
        with pytest.raises(PytError) as e:
            cmd_build.cmd_build(cfg, ["cpython", "--method", "nuitka"])
        assert e.value.code == 3
        assert nuitka.NUITKA in str(e.value) and "nuitka.py" in str(e.value) and "3.15" in str(e.value)
        return
    # Nuitka's own opt-in skips the check (then the checks run: the stand-in stops there)
    with pytest.raises(AssertionError, match="went ahead"):
        cmd_build.cmd_build(cfg, ["cpython", "--method", "nuitka", *extra])
    nuitka.check_python(make({"python": {"cpython": "3.15"}}), extra)


def test_nuitka_failure_names_the_pin(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({})
    app = _nuitka_app(sandbox / "payload", cfg.pkg)

    def failing(env: object, argv: Any, **_: Any) -> Any:
        raise proc.CommandFailed(["uv", "run"], 1)

    monkeypatch.setattr(envs, "uv", failing)
    with pytest.raises(PytError) as e:
        nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))
    assert e.value.code == 1 and nuitka.NUITKA in str(e.value) and "methods/nuitka.py" in str(e.value)


# --- nuitka: [deploy.nuitka] lto and pgo -----------------------------------------------------------

CONSOLE_APP = {"app": {"gui": False, "assets": ""}}  # what PGO needs (its profiling run starts the app)
PGO_ARGS = ["--frames", "10", "two words", "", "C:\\data\\in.txt", "it's"]


def _nuitka_argv(sandbox: Path, monkeypatch: pytest.MonkeyPatch, cfg: Config, backend: str = "cpython", extra: list[str] | None = None) -> list[str]:
    app = _nuitka_app(sandbox / f"payload-{len(list(sandbox.glob('payload-*')))}", cfg.pkg)
    fake = FakeNuitka(cfg.pkg)
    monkeypatch.setattr(nuitka, "IS_MACOS", False)
    monkeypatch.setattr(envs, "uv", fake)
    nuitka.build(BuildRequest(cfg, backend, "nuitka", app, extra=extra or []))
    return fake.argv


def test_nuitka_optimization_defaults() -> None:
    cfg = make({})
    assert (cfg.deploy.nuitka.lto, cfg.deploy.nuitka.pgo, cfg.deploy.nuitka.pgo_args) == ("auto", False, [])
    assert nuitka.optimization_args(cfg) == ["--lto=auto"]  # always passed: the build says what it asked for


@pytest.mark.parametrize("lto", ["auto", "yes", "no"])
def test_nuitka_lto_goes_before_extra_args_and_the_command_line(sandbox: Path, monkeypatch: pytest.MonkeyPatch, lto: str) -> None:
    cfg = make({"deploy": {"nuitka": {"lto": lto}}})
    argv = _nuitka_argv(sandbox, monkeypatch, cfg)
    assert [a for a in argv if a.startswith("--lto")] == [f"--lto={lto}"]
    # A later --lto wins in Nuitka: extra_args, then the command line, can still override it
    both = make({"deploy": {"nuitka": {"lto": lto, "extra_args": ["--lto=no", "--show-scons"]}}})
    argv = _nuitka_argv(sandbox, monkeypatch, both, extra=["--lto=yes"])
    assert [a for a in argv if a.startswith("--lto")] == [f"--lto={lto}", "--lto=no", "--lto=yes"]
    assert argv[-3:] == ["--lto=no", "--show-scons", "--lto=yes"]
    assert argv.index(f"--lto={lto}") > argv.index(f"--include-package={cfg.pkg}")


def test_nuitka_pgo_flags(sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import shlex

    cfg = make({**CONSOLE_APP, "deploy": {"nuitka": {"pgo": True, "pgo_args": PGO_ARGS, "extra_args": ["--report=r.xml"]}}})
    argv = _nuitka_argv(sandbox, monkeypatch, cfg)
    assert argv[-4:] == ["--lto=auto", "--pgo-c", f"--pgo-args={shlex.join(PGO_ARGS)}", "--report=r.xml"]
    # ONE argv item (uv gets a list), which Nuitka splits with shlex on every OS: a round trip
    value = next(a for a in argv if a.startswith("--pgo-args=")).split("=", 1)[1]
    assert shlex.split(value) == PGO_ARGS
    err = capsys.readouterr().err
    assert "experimental" in err and "10-15%" in err and "msgpack" in err  # the note
    # pgo without arguments: the profiling run starts the app with none
    bare = _nuitka_argv(sandbox, monkeypatch, make({**CONSOLE_APP, "deploy": {"nuitka": {"pgo": True}}}))
    assert "--pgo-c" in bare and not [a for a in bare if a.startswith("--pgo-args")]
    # pgo off: neither flag, and no note
    capsys.readouterr()
    off = _nuitka_argv(sandbox, monkeypatch, make({}))
    assert not [a for a in off if a.startswith("--pgo")] and "experimental" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"deploy": {"nuitka": {"lto": "maybe"}}}, "'deploy.nuitka.lto' = 'maybe' is not valid (auto | yes | no)"),
        ({"deploy": {"nuitka": {"lto": "Yes"}}}, "'deploy.nuitka.lto' = 'Yes' is not valid"),
        ({**CONSOLE_APP, "deploy": {"nuitka": {"pgo_args": ["--bench"]}}}, "pgo_args is set but deploy.nuitka.pgo is false"),
        ({"app": {"gui": True, "assets": ""}, "deploy": {"nuitka": {"pgo": True}}}, "needs app.gui = false"),
        ({"app": {"gui": True, "assets": ""}, "deploy": {"nuitka": {"pgo": True}}}, "until its window is closed"),
        ({"app": {"gui": False, "assets": "assets"}, "deploy": {"nuitka": {"pgo": True}}}, "needs app.assets = \"\""),
        ({"app": {"gui": False, "assets": "assets"}, "deploy": {"nuitka": {"pgo": True}}}, "FileNotFoundError"),
        ({"deploy": {"nuitka": {"pgo": "yes"}}}, "'deploy.nuitka.pgo' must be of type boolean"),
        ({"deploy": {"nuitka": {"pgo_args": "--bench"}}}, "'deploy.nuitka.pgo_args' must be of type list"),
        ({"deploy": {"nuitka": {"pgo_args": [1]}}}, "'deploy.nuitka.pgo_args[0]' must be of type string"),
    ],
)
def test_nuitka_options_config_rules(data: dict[str, Any], message: str) -> None:
    with pytest.raises(PytError) as e:
        make(data)
    assert message in str(e.value) and e.value.code == 2


def test_nuitka_options_config_accepts_what_pgo_needs() -> None:
    cfg = make({**CONSOLE_APP, "deploy": {"nuitka": {"lto": "yes", "pgo": True, "pgo_args": PGO_ARGS}}})
    assert (cfg.deploy.nuitka.lto, cfg.deploy.nuitka.pgo, cfg.deploy.nuitka.pgo_args) == ("yes", True, PGO_ARGS)
    assert make({"app": {"gui": True}, "deploy": {"nuitka": {"lto": "no"}}}).deploy.nuitka.lto == "no"  # lto has no rule


@pytest.mark.parametrize("dry_run", [False, True])
def test_nuitka_pgo_refuses_mypyc_and_macos_before_any_work(no_build: None, monkeypatch: pytest.MonkeyPatch, dry_run: bool) -> None:
    monkeypatch.setattr(proc, "DRY_RUN", dry_run)
    monkeypatch.setattr(nuitka, "IS_MACOS", False)
    cfg = make({**CONSOLE_APP, "deploy": {"nuitka": {"pgo": True}}})
    with pytest.raises(PytError) as e:
        cmd_build.cmd_build(cfg, ["mypyc", "--method", "nuitka"])
    assert e.value.code == 2 and "mypyc backend" in str(e.value) and "ImportError" in str(e.value) and "reports success" in str(e.value)
    monkeypatch.setattr(nuitka, "IS_MACOS", True)
    with pytest.raises(PytError) as e:
        cmd_build.cmd_build(cfg, ["cpython", "--method", "nuitka"])
    assert e.value.code == 2 and "macOS" in str(e.value) and "profdata" in str(e.value)
    # Without pgo neither rule applies (the stand-in stops at the checks)
    with pytest.raises(AssertionError, match="went past"):
        cmd_build.cmd_build(make(CONSOLE_APP), ["mypyc", "--method", "nuitka"])


def test_nuitka_build_itself_refuses_pgo_with_mypyc(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # nuitka.build checks too (the method can be called without cmd_build, as the tests do)
    cfg = make({**CONSOLE_APP, "deploy": {"nuitka": {"pgo": True}}})
    with pytest.raises(PytError, match="mypyc backend"):
        _nuitka_argv(sandbox, monkeypatch, cfg, backend="mypyc")
    monkeypatch.setattr(nuitka, "IS_MACOS", True)
    app = _nuitka_app(sandbox / "payload2", cfg.pkg)
    with pytest.raises(PytError, match="not available on macOS"):
        nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))


@pytest.mark.parametrize("dry_run", [False, True])
def test_nuitka_refuses_a_project_folder_scons_would_expand(no_build: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dry_run: bool) -> None:
    # SCons substitutes $NAME, ${...}, $$, $( and $) in the paths Nuitka hands it: under app$v2
    # the build wrote app/.../main.build outside the project, and failed where that path could
    # not be made. Refused before the checks and the payload, also in --dry-run
    monkeypatch.setattr(proc, "DRY_RUN", dry_run)
    monkeypatch.setattr(nuitka, "IS_MACOS", False)
    for folder, token in (("app$v2", "$v2"), ("a${x}b", "${x}"), ("cash$$", "$$"), ("x$(y", "$(")):
        root = tmp_path / folder / "proj"
        monkeypatch.setattr(nuitka, "ROOT", root)
        monkeypatch.setattr(nuitka, "BUILD", root / ".build")
        with pytest.raises(PytError) as e:
            cmd_build.cmd_build(make({}), ["cpython", "--method", "nuitka"])
        assert e.value.code == 2 and repr(token) in str(e.value) and "--method exe, portable or pyz" in str(e.value)
    for folder in ("price$1", "cash$"):  # a '$' SCons leaves as it is: the build goes on
        monkeypatch.setattr(nuitka, "BUILD", tmp_path / folder / "proj" / ".build")
        with pytest.raises(AssertionError, match="went past"):
            cmd_build.cmd_build(make({}), ["cpython", "--method", "nuitka"])


def test_nuitka_dry_run_shows_the_flags(no_build: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(proc, "DRY_RUN", True)
    monkeypatch.setattr(nuitka, "IS_MACOS", False)
    assert cmd_build.cmd_build(make({}), ["--method", "nuitka", "--no-check", "--report=r.xml"]) == 0
    err = capsys.readouterr().err
    assert "would output dist/myapp-cpython-nuitka*" in err and "  Nuitka options: --lto=auto --report=r.xml" in err
    assert "experimental" not in err
    cfg = make({**CONSOLE_APP, "deploy": {"nuitka": {"lto": "yes", "pgo": True, "pgo_args": ["--frames", "10", "a b"]}}})
    assert cmd_build.cmd_build(cfg, ["--method", "nuitka", "--no-check"]) == 0
    err = capsys.readouterr().err
    assert "  Nuitka options: --lto=yes --pgo-c '--pgo-args=--frames 10 '\"'\"'a b'\"'\"''" in err
    assert "experimental" in err


def test_flet_dry_run_shows_the_target_and_the_pins(sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`--dry-run build --method flet` names the target and, for a mobile or web one, the markers
    and pins the build project gets instead of uv.lock's: a real build said so only once it ran."""
    from runner.methods import flet

    pins = ["flet==1.0.1", "msgpack==1.2.2", "tomli-w==1.2.0 ; sys_platform == 'android'"]
    monkeypatch.setattr(flet, "_pinned_requirements", lambda env: list(pins))
    (sandbox / "uv.lock").write_text(FLET_LOCK, encoding="utf-8")
    monkeypatch.setattr(common, "LOCK", sandbox / "uv.lock")
    monkeypatch.setattr(flet, "host_os", lambda: "linux")
    lines = flet.plan_lines(_flet_cfg(deploy={"flet": {"target": "apk"}}))
    assert lines[0] == "flet build apk" and len(lines) == 3, lines
    assert lines[1].endswith("tomli-w==1.2.0 ; platform_system == 'Android'") and "msgpack==1.2.2 -> msgpack" in lines[2], lines
    assert flet.plan_lines(_flet_cfg(deploy={"flet": {"target": "host"}})) == ["flet build linux"]  # desktop: every pin as locked
    monkeypatch.setattr(proc, "DRY_RUN", True)
    monkeypatch.setattr(cmd_build, "check_lock", lambda cfg: None)
    monkeypatch.setattr(flet, "_developer_mode", lambda: True)
    assert cmd_build.cmd_build(_flet_cfg(deploy={"flet": {"target": "apk"}}), ["--method", "flet", "--no-check"]) == 0
    err = capsys.readouterr().err
    assert "  flet build apk" in err and "platform_system == 'Android'" in err and "msgpack==1.2.2 -> msgpack" in err


def test_nuitka_keys_of_every_preset_load() -> None:
    # The [deploy.nuitka] lines of the four pytemplate.toml files are valid (defaults: auto, no PGO)
    import tomllib

    for path in (config.CONFIG_FILE, *sorted(config.PRESETS.glob("*/files/pytemplate.toml"))):
        table = tomllib.loads(path.read_text(encoding="utf-8").replace("{{name}}", "demo").replace("{{pkg}}", "demo"))["deploy"]["nuitka"]
        assert table == {"lto": "auto", "pgo": False, "pgo_args": []}, path
        cfg: Config = config._build(Config, {"deploy": {"nuitka": table}}, "")
        assert nuitka.optimization_args(cfg) == ["--lto=auto"]


# --- pyz: the bootstrap (templates/pyz/__main__.py), run for real ---------------------------------


def _host_key() -> str:
    """The key the bootstrap computes for THIS interpreter (templates/pyz/__main__.py _key)."""
    key: str = _bootstrap_namespace()["_key"]()
    return key


MAIN_WAITS = (
    "import os, sys, time\n"
    "flag = os.environ.get('PT_WAIT')\n"
    "while flag and not os.path.exists(flag):\n"
    "    time.sleep(0.05)\n"
    "import lazymod\n"
    "print('ok', lazymod.WHERE, os.environ.get('PYTEMPLATE_ASSETS', ''), sys.argv[0], *sys.argv[1:])\n"
)


def fake_pyz(path: Path, *, build_id: str = "b1", targets: list[str] | None = None, pure: bool = True, files: dict[str, str] | None = None, **info: Any) -> Path:
    """A .pyz as methods/pyz.py lays it out, with the REAL bootstrap and pyz._write_archive."""
    from runner.methods import pyz

    root = path.parent / f"{path.stem}-root"
    if root.exists():
        shutil.rmtree(root)
    content = files if files is not None else {"common/app/main.py": MAIN_WAITS, "common/lib/lazymod.py": "WHERE = 'common'\n"}
    for name, text in content.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8", newline="")
    data = {"name": "demo", "build_id": build_id, "min_python": [3, 11], "targets": targets or [], "pure": pure, "backend": "cpython", **info}
    (root / "_pyz.json").write_text(json.dumps(data), encoding="utf-8")
    shutil.copy2(TEMPLATES / "pyz" / "__main__.py", root / "__main__.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    pyz._write_archive(root, path)
    return path


def pyz_env(cache: Path, **extra: str) -> dict[str, str]:
    """A child env whose user cache is `cache` (never the real one) on every OS."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEMPLATE_", "PT_"))}
    env.update({"LOCALAPPDATA": str(cache), "XDG_CACHE_HOME": str(cache), "HOME": str(cache), "USERPROFILE": str(cache)})
    env.update(extra)
    return env


def pyz_root(cache: Path) -> Path:
    """Where the bootstrap keeps the builds of the app "demo" under `pyz_env(cache)`: macOS has
    no XDG cache, its user cache is ~/Library/Caches (the bootstrap's `_cache_root`)."""
    return (cache / "Library" / "Caches" if sys.platform == "darwin" else cache) / "demo" / "pyz"


def run_pyz(pyz_file: Path, env: dict[str, str], *args: str, prelude: str = "") -> subprocess.CompletedProcess[str]:
    """`python -S app.pyz ARGS` (no site-packages: the .pyz must bring its own dependencies)."""
    if prelude:
        code = prelude + f"import runpy, sys\nsys.argv = [{str(pyz_file)!r}, *{list(args)!r}]\nrunpy.run_path({str(pyz_file)!r}, run_name='__main__')\n"
        argv = [sys.executable, "-S", "-c", code]
    else:
        argv = [sys.executable, "-S", str(pyz_file), *args]
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120, check=False)


def test_pyz_names_its_own_assets_whatever_it_inherits(tmp_path: Path) -> None:
    # setdefault kept the PYTEMPLATE_ASSETS of the app that started this one (a launcher, or a
    # pyz that restarts its updated version)
    cache = tmp_path / "cache"
    child = fake_pyz(tmp_path / "child.pyz", build_id="child")
    r = run_pyz(child, pyz_env(cache, PYTEMPLATE_ASSETS=str(tmp_path / "parent" / "app" / "assets")))
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[2] == str(pyz_root(cache) / "child" / "pure" / "app" / "assets")


def test_pyz_bootstrap_picks_the_flavour(tmp_path: Path) -> None:
    key = _host_key()
    cache = tmp_path / "cache"
    files = {
        "common/app/main.py": MAIN_WAITS,
        "common/app/assets/logo.txt": "x",
        "common/lib/lazymod.py": "WHERE = 'common'\n",
        f"targets/{key}/lib/lazymod.py": "WHERE = 'target'\n",
    }
    own = fake_pyz(tmp_path / "own.pyz", build_id="own", targets=[key], pure=False, files=files)
    r = run_pyz(own, pyz_env(cache), "a b", "")
    assert r.returncode == 0, r.stderr
    root = pyz_root(cache) / "own" / key
    assert r.stdout.split() == ["ok", "target", str(root / "app" / "assets"), str(root / "app" / "main.py"), "a", "b"]
    pure = fake_pyz(tmp_path / "pure.pyz", build_id="pure", targets=["cp399-nowhere-x86_64"], pure=True)
    r = run_pyz(pure, pyz_env(cache))
    assert r.returncode == 0 and r.stdout.split()[:2] == ["ok", "common"], r.stderr
    assert (pyz_root(cache) / "pure" / "pure" / ".complete").is_file()
    other = fake_pyz(tmp_path / "other.pyz", build_id="other", targets=["cp399-nowhere-x86_64"], pure=False)
    r = run_pyz(other, pyz_env(cache))
    assert r.returncode == 1
    assert f"no build for this interpreter and platform ({key})" in r.stderr and "Built for: cp399-nowhere-x86_64" in r.stderr


def test_pyz_bootstrap_refuses_an_older_python(tmp_path: Path) -> None:
    newer = fake_pyz(tmp_path / "n.pyz", min_python=[3, 99])
    r = run_pyz(newer, pyz_env(tmp_path / "cache"))
    assert r.returncode == 1 and "demo: needs Python 3.99 or newer" in r.stderr
    assert not (tmp_path / "cache").exists()  # nothing extracted


ABI_NAMES = {
    "_x.cpython-314-x86_64-linux-gnu.so": "cp314",
    "_x.cpython-314t-x86_64-linux-gnu.so": "cp314t",  # free-threaded
    "_x.cpython-313-darwin.so": "cp313",
    "_x.cp314-win_amd64.pyd": "cp314",
    "_x.cp314t-win_arm64.pyd": "cp314t",
    "_x.pypy311-pp73-x86_64-linux-gnu.so": "pypy311_pp73",
    "_x.pypy311-pp80-darwin.so": "pypy311_pp80",  # PyPy 8.0: a new ABI for the same pp311 key
    "_x.pypy311-pp73-win_amd64.pyd": "pypy311_pp73",
    "_x.abi3.so": "",  # no version: its wheel's tag says which CPython (wheel_abis)
    "_x.so": "",
    "_x.pyd": "",
    ".cpython-314-x86_64-linux-gnu.so": "cp314",  # EXT_SUFFIX itself (the bootstrap's _abi)
    ".cp314-win_amd64.pyd": "cp314",
    ".pypy311-pp73-x86_64-linux-gnu.so": "pypy311_pp73",
}


def test_abi_tags_agree_with_the_bootstrap() -> None:
    import sysconfig

    bootstrap = _bootstrap_namespace()
    for name, tag in ABI_NAMES.items():
        assert common.abi_tag(name) == tag == bootstrap["abi_tag"](name), name
    assert bootstrap["ABI_RE"].pattern == common.ABI_RE.pattern
    assert bootstrap["_abi"]() == common.abi_tag(sysconfig.get_config_var("EXT_SUFFIX") or "") != ""


def test_pyz_records_the_abi_of_every_targets_binaries(tmp_path: Path) -> None:
    from runner.methods import pyz

    root = tmp_path / "root"
    for name in ("targets/a/lib/dep/_x.pypy311-pp73-x86_64-linux-gnu.so", "targets/a/lib/dep/_y.abi3.so", "targets/b/app/pkg/core.cpython-314-x86_64-linux-gnu.so", "targets/c/lib/dep.py"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"")
    assert pyz._target_abis(root, ["a", "b", "c"]) == {"a": ["pypy311_pp73"], "b": ["cp314"]}
    # pyz-merge records the ABIs of what it wrote
    parts = [
        fake_pyz(tmp_path / "p1.pyz", targets=[LINUX], pure=False, host=LINUX, files={"common/app/main.py": MERGE_MAIN, f"targets/{LINUX}/lib/_d.cpython-314-x86_64-linux-gnu.so": ""}),
        fake_pyz(tmp_path / "p2.pyz", targets=[WIN], pure=False, host=WIN, files={"common/app/main.py": MERGE_MAIN, f"targets/{WIN}/lib/_d.cp314-win_amd64.pyd": ""}),
    ]
    info, _ = _merge(parts, tmp_path / "m.pyz")
    assert info["abi"] == {LINUX: ["cp314"], WIN: ["cp314"]}


def test_pyz_records_an_abi3_wheel_as_the_keys_own_cpython(tmp_path: Path) -> None:
    # bcrypt 5.0.0 ships one cp39-abi3 wheel per platform: its files name no version, so "abi"
    # stayed empty, a free-threaded 3.14t (which lists .abi3.so among its suffixes) took the target
    # and died of a segmentation fault instead of the "no build for this interpreter" message
    from runner.methods import pyz

    root = tmp_path / "root"
    wheels = {
        LINUX: ("cp39-abi3-manylinux_2_28_x86_64", "bcrypt/_bcrypt.abi3.so"),
        WIN: ("cp39-abi3-win_amd64", "bcrypt/_bcrypt.pyd"),  # Windows names an abi3 extension bare
        "cp314-windows-aarch64": ("cp314-cp314-win_arm64", "dep/_speedups.pyd"),  # bare, yet for cp314
        "pp311-linux-x86_64": ("pp311-pypy311_pp73-manylinux_2_28_x86_64", "dep/_speedups.pypy311-pp73-x86_64-linux-gnu.so"),
        MAC: ("py3-none-macosx_13_0_arm64", "dep/libhelper.so"),  # a ctypes library: any Python loads it
    }
    for key, (tag, ext) in wheels.items():
        _wheel(root / "targets" / key / "lib", "dep", "1.0", tag, {ext: b""})
    assert pyz._target_abis(root, list(wheels)) == {
        LINUX: ["cp314"],
        WIN: ["cp314"],
        "cp314-windows-aarch64": ["cp314"],
        "pp311-linux-x86_64": ["pypy311_pp73"],
    }
    # pyz-merge records it from the wheels too
    parts = [
        fake_pyz(tmp_path / "p1.pyz", targets=[LINUX], pure=False, host=LINUX, files={
            "common/app/main.py": MERGE_MAIN,
            f"targets/{LINUX}/lib/bcrypt/_bcrypt.abi3.so": "",
            f"targets/{LINUX}/lib/bcrypt-5.0.0.dist-info/WHEEL": "Wheel-Version: 1.0\nTag: cp39-abi3-manylinux_2_28_x86_64\n",
        }),
        fake_pyz(tmp_path / "p2.pyz", targets=[WIN], pure=False, host=WIN, files={
            "common/app/main.py": MERGE_MAIN,
            f"targets/{WIN}/lib/bcrypt/_bcrypt.pyd": "",
            f"targets/{WIN}/lib/bcrypt-5.0.0.dist-info/WHEEL": "Wheel-Version: 1.0\nTag: cp39-abi3-win_amd64\n",
        }),
    ]
    info, _ = _merge(parts, tmp_path / "m.pyz")
    assert info["abi"] == {LINUX: ["cp314"], WIN: ["cp314"]}


@pytest.mark.parametrize("pure", [False, True])
def test_pyz_bootstrap_takes_no_target_built_for_another_abi(tmp_path: Path, pure: bool) -> None:
    # PyPy 8.0 (pp80) has the key pp311-... of a PyPy 7.3 build (pp73): it took that target and
    # died in an ImportError of a dependency; a free-threaded CPython 3.14t likewise
    import sysconfig

    key = _host_key()
    mine = common.abi_tag(sysconfig.get_config_var("EXT_SUFFIX") or "")
    files = {"common/app/main.py": MAIN_WAITS, "common/lib/lazymod.py": "WHERE = 'common'\n", f"targets/{key}/lib/lazymod.py": "WHERE = 'target'\n"}
    other = fake_pyz(tmp_path / "other.pyz", build_id=f"o{pure}", targets=[key], pure=pure, files=files, abi={key: ["pypy311_pp99"]})
    r = run_pyz(other, pyz_env(tmp_path / "cache"))
    if pure:  # a pure build with a compiled overlay: the .py runs
        assert r.returncode == 0 and r.stdout.split()[:2] == ["ok", "common"], r.stderr
    else:
        assert r.returncode == 1 and "Traceback" not in r.stderr
        assert f"no build for this interpreter and platform ({key}, {mine})" in r.stderr
        assert f"Built for: {key} (pypy311_pp99)" in r.stderr
    own = fake_pyz(tmp_path / "own.pyz", build_id=f"w{pure}", targets=[key], pure=pure, files=files, abi={key: [mine, "pypy311_pp99"]})
    r = run_pyz(own, pyz_env(tmp_path / "cache"))
    assert r.returncode == 0 and r.stdout.split()[:2] == ["ok", "target"], r.stderr  # its own ABI: the target


@pytest.mark.parametrize(
    ("tags", "floor"),
    [
        # librt's one wheel: any of its tags is enough, so the least of them
        ([["cp314-cp314-manylinux_2_17_x86_64", "cp314-cp314-manylinux2014_x86_64", "cp314-cp314-manylinux_2_28_x86_64"]], "glibc 2.17"),
        # the newest glibc a wheel of the target needs (a compressed tag set counts as its tags)
        ([["cp314-cp314-manylinux_2_17_x86_64.manylinux2014_x86_64"], ["py3-none-manylinux_2_28_x86_64"], ["py3-none-any"]], "glibc 2.28"),
        ([["cp311-cp311-manylinux2010_x86_64"], ["cp311-cp311-manylinux1_x86_64"]], "glibc 2.12"),
        ([["cp314-cp314-musllinux_1_2_x86_64"]], "musl"),  # a build made on Alpine
        ([["cp314-cp314-macosx_11_0_arm64"], ["cp314-cp314-macosx_13_0_arm64", "cp314-cp314-macosx_14_0_arm64"]], "macos 13.0"),
        ([["cp314-cp314-macosx_10_9_x86_64"]], "macos 10.9"),
        ([["cp314-cp314-linux_x86_64"]], "glibc"),  # built from its sdist on this (glibc) machine
        ([["cp314-cp314-linux_x86_64"], ["cp314-cp314-manylinux_2_28_x86_64"]], "glibc 2.28"),
        ([["cp314-cp314-manylinux_2_17_x86_64.musllinux_1_1_x86_64"]], ""),  # one wheel for both C libraries
        ([["cp314-cp314-win_amd64"], ["py3-none-any"]], ""),  # Windows wheels need nothing more
        ([["py3-none-any"]], ""),
        ([], ""),
    ],
)
def test_platform_floor_reads_what_the_wheels_need(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tags: list[list[str]], floor: str) -> None:
    monkeypatch.setattr(common, "this_libc", lambda: "glibc")
    for n, wheel in enumerate(tags):
        info = tmp_path / f"dep{n}-1.0.dist-info"
        info.mkdir()
        (info / "WHEEL").write_text("Wheel-Version: 1.0\n" + "".join(f"Tag: {t}\n" for t in wheel), encoding="utf-8")
    assert common.platform_floor(tmp_path) == floor


@pytest.mark.parametrize(
    ("confstr", "ext_suffix", "libc"),
    [
        ("glibc 2.39", ".cpython-314-x86_64-linux-gnu.so", "glibc"),
        (OSError(22, "Invalid argument"), ".cpython-314-x86_64-linux-musl.so", "musl"),  # os.confstr on musl
        (OSError(22, "Invalid argument"), ".cpython-312.so", ""),  # another C library (Android)
        (None, ".cpython-314-darwin.so", ""),
    ],
)
def test_the_build_machine_and_the_bootstrap_name_the_c_library_alike(monkeypatch: pytest.MonkeyPatch, confstr: object, ext_suffix: str, libc: str) -> None:
    # A wheel built on the build machine (linux_x86_64) needs its C library
    import types

    def fake_confstr(name: str) -> str | None:
        assert name == "CS_GNU_LIBC_VERSION"
        if isinstance(confstr, OSError):
            raise confstr
        return confstr if isinstance(confstr, str) else None

    fake_sysconfig = types.SimpleNamespace(get_config_var=lambda name: ext_suffix if name == "EXT_SUFFIX" else None)
    monkeypatch.setattr(common, "sys", types.SimpleNamespace(platform="linux"))
    monkeypatch.setattr(common, "os", types.SimpleNamespace(confstr=fake_confstr))
    monkeypatch.setattr(common, "sysconfig", fake_sysconfig)
    assert common.this_libc() == libc
    namespace = _bootstrap_namespace()
    namespace.update(os=types.SimpleNamespace(confstr=fake_confstr), sysconfig=fake_sysconfig)
    assert namespace["_libc"]()[0] == libc


@pytest.mark.parametrize(
    ("confstr", "ext_suffix", "mac", "floor", "meets"),
    [
        ("glibc 2.39", ".cpython-314-x86_64-linux-gnu.so", "", "glibc 2.28", True),
        ("glibc 2.39", ".cpython-314-x86_64-linux-gnu.so", "", "glibc 2.40", False),  # newer wheels than this glibc
        ("glibc 2.17", ".cpython-314-x86_64-linux-gnu.so", "", "glibc 2.28", False),  # CentOS 7
        ("glibc 2.39", ".cpython-314-x86_64-linux-gnu.so", "", "glibc", True),
        ("glibc 2.39", ".cpython-314-x86_64-linux-gnu.so", "", "musl", False),  # a build made on Alpine
        ("glibc 2.39", ".cpython-314-x86_64-linux-gnu.so", "", "", True),
        (OSError(22, "Invalid argument"), ".cpython-314-x86_64-linux-musl.so", "", "glibc 2.17", False),  # Alpine
        (OSError(22, "Invalid argument"), ".cpython-314-x86_64-linux-musl.so", "", "glibc", False),
        (OSError(22, "Invalid argument"), ".cpython-314-x86_64-linux-musl.so", "", "musl", True),
        (OSError(22, "Invalid argument"), ".cpython-312.so", "", "glibc 2.17", False),  # Android's C library
        (OSError(22, "Invalid argument"), ".cpython-312.so", "", "musl", False),
        (None, ".cpython-314-darwin.so", "14.5", "macos 13.0", True),
        (None, ".cpython-314-darwin.so", "12.7.6", "macos 13.0", False),
        (None, ".cpython-314-darwin.so", "26.0", "macos 10.9", True),
        (None, ".cpython-314-darwin.so", "10.16", "macos 13.0", True),  # an old SDK's name for any macOS 11+
    ],
)
def test_pyz_bootstrap_meets_what_the_wheels_need(confstr: object, ext_suffix: str, mac: str, floor: str, meets: bool) -> None:
    # A key does not tell glibc from musl nor their versions: a musl Python (Alpine) took the
    # target of a glibc build and died in "No module named 'librt.base64'"
    import types

    def fake_confstr(name: str) -> str | None:
        if isinstance(confstr, OSError):
            raise confstr
        return confstr if isinstance(confstr, str) else None

    namespace = _bootstrap_namespace()
    namespace.update(
        os=types.SimpleNamespace(confstr=fake_confstr),
        sysconfig=types.SimpleNamespace(get_config_var=lambda name: ext_suffix if name == "EXT_SUFFIX" else None),
        platform=types.SimpleNamespace(mac_ver=lambda: (mac, ("", "", ""), "")),
    )
    assert namespace["_meets"](floor) is meets


def _floors_of_this_machine() -> tuple[str, str]:
    """A floor this machine does not meet, and one it meets."""
    import platform

    if sys.platform == "darwin":
        return "macos 99.0", "macos 10.9"
    if sys.platform == "win32":
        return "glibc 2.5", ""  # no glibc there; Windows wheels need nothing more than their key
    if platform.libc_ver()[0] != "glibc":
        pytest.skip("a Linux without glibc")
    return "glibc 99.0", "glibc 2.5"


@pytest.mark.parametrize("pure", [False, True])
def test_pyz_bootstrap_takes_no_target_this_machine_cannot_load(tmp_path: Path, pure: bool) -> None:
    import sysconfig

    key = _host_key()
    mine = common.abi_tag(sysconfig.get_config_var("EXT_SUFFIX") or "")
    unmet, met = _floors_of_this_machine()
    files = {"common/app/main.py": MAIN_WAITS, "common/lib/lazymod.py": "WHERE = 'common'\n", f"targets/{key}/lib/lazymod.py": "WHERE = 'target'\n"}
    other = fake_pyz(tmp_path / "other.pyz", build_id=f"o{pure}", targets=[key], pure=pure, files=files, floor={key: unmet})
    r = run_pyz(other, pyz_env(tmp_path / "cache"))
    if pure:
        assert r.returncode == 0 and r.stdout.split()[:2] == ["ok", "common"], r.stderr
    else:
        assert r.returncode == 1 and "Traceback" not in r.stderr, r.stderr
        assert f"no build for this interpreter and platform ({key}, {mine}, " in r.stderr
        assert f"Built for: {key} ({unmet})" in r.stderr
    for floor in ([met] if met else []) + (["musl"] if unmet.startswith("glibc 9") else []):
        # its own C library and version: the target; and a musl build on this glibc machine: not
        build = fake_pyz(tmp_path / "met.pyz", build_id=f"m{pure}{floor[:1]}", targets=[key], pure=pure, files=files, floor={key: floor})
        r = run_pyz(build, pyz_env(tmp_path / "cache"))
        taken = floor == met
        assert r.stdout.split()[:2] == (["ok", "target"] if taken else ["ok", "common"] if pure else []), (floor, r.stderr)


def test_pyz_records_what_every_targets_wheels_need(tmp_path: Path) -> None:
    from runner.methods import pyz

    root = tmp_path / "root"
    _wheel(root / "targets" / "a" / "lib", "dep", "1.0", "cp314-cp314-manylinux_2_28_x86_64")
    _wheel(root / "targets" / "b" / "lib", "dep", "1.0", "cp314-cp314-win_amd64")
    (root / "targets" / "c" / "app").mkdir(parents=True)  # a mypyc overlay alone
    assert pyz._target_floors(root, ["a", "b", "c"]) == {"a": "glibc 2.28"}
    # pyz-merge records what the libs it wrote need
    parts = [
        fake_pyz(tmp_path / "p1.pyz", targets=[LINUX], pure=False, host=LINUX, files={"common/app/main.py": MERGE_MAIN, f"targets/{LINUX}/lib/dep-1.0.dist-info/WHEEL": "Tag: cp314-cp314-manylinux_2_17_x86_64\n"}),
        fake_pyz(tmp_path / "p2.pyz", targets=[MAC], pure=False, host=MAC, files={"common/app/main.py": MERGE_MAIN, f"targets/{MAC}/lib/dep-1.0.dist-info/WHEEL": "Tag: cp314-cp314-macosx_11_0_arm64\n"}),
    ]
    info, _ = _merge(parts, tmp_path / "m.pyz")
    assert info["floor"] == {LINUX: "glibc 2.17", MAC: "macos 11.0"}


def _old_pythons() -> list[str]:
    """Interpreters older than 3.11 on this machine (macOS's /usr/bin/python3 is 3.9)."""
    found: dict[str, str] = {}
    for name in ("python3.8", "python3.9", "python3.10", "/usr/bin/python3"):
        exe = shutil.which(name)
        if not exe:
            continue
        r = subprocess.run([exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], capture_output=True, text=True, timeout=60, check=False)
        version = r.stdout.strip()
        if r.returncode == 0 and version and tuple(int(x) for x in version.split(".")) < (3, 11):
            found.setdefault(version, exe)
    return sorted(found.values())


def test_pyz_bootstrap_reaches_its_version_check_on_an_old_python(tmp_path: Path) -> None:
    # `def _lock(fd: int) -> bool | None` is evaluated when the module loads: Python 3.9 (macOS's
    # python3, Debian 11) died with "TypeError: unsupported operand type(s) for |" instead of
    # "needs Python 3.11 or newer". Nothing before main()'s check may need a newer Python
    source = (TEMPLATES / "pyz" / "__main__.py").read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 7))
    futures = {a.name for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "__future__" for a in node.names}
    assert "annotations" in futures  # no annotation is evaluated: PEP 604 and list[int] need 3.10 / 3.9
    old = _old_pythons()
    if not old:
        pytest.skip("no Python older than 3.11 here (macOS has one)")
    app = fake_pyz(tmp_path / "app.pyz", min_python=[3, 11])
    for exe in old:
        r = subprocess.run([exe, "-S", str(app)], capture_output=True, text=True, env=pyz_env(tmp_path / "cache"), timeout=120, check=False)
        assert r.returncode == 1 and "demo: needs Python 3.11 or newer (you have" in r.stderr, (exe, r.stderr)
        assert "Traceback" not in r.stderr


@pytest.mark.parametrize("case", ["no_home", "unwritable"])
def test_pyz_runs_without_a_usable_cache(tmp_path: Path, case: str) -> None:
    # A random UID without a passwd entry (Path.home() raises RuntimeError) or a read-only home
    # (service users): the bootstrap crashed with a traceback. Now: a private per-run folder.
    blocker = tmp_path / "blocker"
    blocker.write_text("")  # a FILE: every cache folder below it fails to mkdir, even as root
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = pyz_env(blocker / "x", TMPDIR=str(scratch), TEMP=str(scratch), TMP=str(scratch))
    prelude = ""
    if case == "no_home":
        for name in ("LOCALAPPDATA", "XDG_CACHE_HOME", "HOME", "USERPROFILE"):
            env.pop(name, None)
        prelude = "import pathlib\ndef _no_home(cls): raise RuntimeError('Could not determine home directory.')\npathlib.Path.home = classmethod(_no_home)\n"
    r = run_pyz(fake_pyz(tmp_path / "t.pyz"), env, "arg", prelude=prelude)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[:2] == ["ok", "common"] and r.stdout.split()[-1] == "arg"
    assert not any(scratch.iterdir())  # the per-run folder is removed at exit


@pytest.mark.skipif(IS_WINDOWS or sys.platform == "darwin", reason="XDG_CACHE_HOME is the Linux/BSD cache")
def test_pyz_ignores_a_relative_xdg_cache_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    work.mkdir()
    env = pyz_env(home, XDG_CACHE_HOME="relative/cache")
    r = subprocess.run([sys.executable, "-S", str(fake_pyz(tmp_path / "t.pyz"))], cwd=work, capture_output=True, text=True, env=env, timeout=120, check=False)
    assert r.returncode == 0, r.stderr
    assert (home / ".cache" / "demo" / "pyz" / "b1" / "pure" / ".complete").is_file()
    assert not (work / "relative").exists()


def test_pyz_concurrent_first_starts_extract_once(tmp_path: Path) -> None:
    pyz_file = fake_pyz(tmp_path / "t.pyz")
    cache = tmp_path / "cache"
    procs = [subprocess.Popen([sys.executable, "-S", str(pyz_file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=pyz_env(cache)) for _ in range(8)]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        assert out.split()[:2] == ["ok", "common"]
    build = pyz_root(cache) / "b1"
    assert sorted(p.name for p in build.iterdir()) == ["pure"]  # no .tmp-* or .stale-* left behind
    assert (build / "pure" / ".complete").is_file()


@pytest.mark.parametrize("damage", ["marker", "marker+file"])
def test_pyz_repairs_an_incomplete_cache(tmp_path: Path, damage: str) -> None:
    # An interrupted prune (or a Windows delete that skipped a loaded DLL) left the build folder
    # without .complete: every later start failed in os.replace with "Directory not empty"
    pyz_file = fake_pyz(tmp_path / "t.pyz")
    cache = tmp_path / "cache"
    assert run_pyz(pyz_file, pyz_env(cache)).returncode == 0
    dest = pyz_root(cache) / "b1" / "pure"
    (dest / ".complete").unlink()
    if damage == "marker+file":
        (dest / "lib" / "lazymod.py").unlink()
    for _ in range(2):
        r = run_pyz(pyz_file, pyz_env(cache))
        assert r.returncode == 0, r.stderr
        assert r.stdout.split()[:2] == ["ok", "common"]
    assert (dest / ".complete").is_file()
    assert sorted(p.name for p in dest.parent.iterdir()) == ["pure"]


def _add_members(pyz_file: Path, members: dict[str, int]) -> None:
    """Append files with these Unix modes to a .pyz, whatever this OS's file modes are."""
    import zipfile

    with zipfile.ZipFile(pyz_file, "a", compression=zipfile.ZIP_DEFLATED) as z:
        for name, mode in members.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, "#!/bin/sh\necho run\n")


EXEC_MEMBERS = {"common/app/tool.sh": 0o755, "common/lib/bin/tool": 0o755, "common/app/data.txt": 0o644}
MAIN_EXEC = "import os, pathlib\nhere = pathlib.Path(__file__).parent\nprint(*[os.access(here / n, os.X_OK) for n in ('tool.sh', '../lib/bin/tool', 'data.txt')])\n"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX file modes")
def test_pyz_bootstrap_keeps_the_executable_bit(tmp_path: Path) -> None:
    # A helper script of the app or a dependency's binary (ruff's bin/ruff, which ruff looks up at
    # <target>/bin) was extracted as 0644: PermissionError when the app ran it
    pyz_file = fake_pyz(tmp_path / "x.pyz", files={"common/app/main.py": MAIN_EXEC})
    _add_members(pyz_file, EXEC_MEMBERS)
    r = run_pyz(pyz_file, pyz_env(tmp_path / "cache"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["True", "True", "False"]


def _cached_builds(cache: Path) -> list[str]:
    return sorted(p.name for p in pyz_root(cache).iterdir())


def test_pyz_prune_never_deletes_a_running_build(tmp_path: Path) -> None:
    # Builds 1-3 cached; build 1 still running; builds 4-6 started: the prune used to delete
    # build 1 (oldest extraction), which then died on its next lazy import
    cache = tmp_path / "cache"
    parts = {n: fake_pyz(tmp_path / f"b{n}.pyz", build_id=f"build{n}") for n in range(1, 7)}
    for n in (1, 2, 3):
        assert run_pyz(parts[n], pyz_env(cache)).returncode == 0
    flag = tmp_path / "go"
    slow = subprocess.Popen([sys.executable, "-S", str(parts[1])], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=pyz_env(cache, PT_WAIT=str(flag)))
    try:
        for n in (4, 5, 6):
            assert run_pyz(parts[n], pyz_env(cache)).returncode == 0
    finally:
        flag.write_text("")
        out, err = slow.communicate(timeout=120)
    assert slow.returncode == 0, err
    assert out.split()[:2] == ["ok", "common"]
    assert "build1" in _cached_builds(cache)


MAIN_SIGNALS = (
    "import os, pathlib, time\n"
    "pathlib.Path(os.environ['PT_STARTED']).write_text('')\n"
    "while not os.path.exists(os.environ['PT_WAIT']):\n"
    "    time.sleep(0.05)\n"
    "import lazymod\n"
    "print('ok', lazymod.WHERE)\n"
)


def _start_waiting(pyz_file: Path, cache: Path, tmp_path: Path) -> tuple[subprocess.Popen[str], Path]:
    """Start a pyz whose main waits for a flag file; return once its main runs."""
    started, flag = tmp_path / f"{pyz_file.stem}.started", tmp_path / f"{pyz_file.stem}.go"
    proc_ = subprocess.Popen([sys.executable, "-S", str(pyz_file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=pyz_env(cache, PT_STARTED=str(started), PT_WAIT=str(flag)))
    deadline = time.time() + 120
    while not started.exists():
        assert proc_.poll() is None and time.time() < deadline, proc_.communicate()[1] if proc_.poll() is not None else "timeout"
        time.sleep(0.05)
    return proc_, flag


def _age(folder: Path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(folder, (old, old))


def test_pyz_prune_never_deletes_a_build_running_for_days(tmp_path: Path) -> None:
    # A server started more than a day ago (its folder's mtime, touched at start, is that old),
    # then three newer builds started: the prune deleted the running build, whose next lazy
    # import failed with ModuleNotFoundError
    cache = tmp_path / "cache"
    files = {"common/app/main.py": MAIN_SIGNALS, "common/lib/lazymod.py": "WHERE = 'common'\n"}
    old = fake_pyz(tmp_path / "old.pyz", build_id="old", files=files)
    running, flag = _start_waiting(old, cache, tmp_path)
    try:
        _age(pyz_root(cache) / "old", 2)
        for n in (1, 2, 3):
            assert run_pyz(fake_pyz(tmp_path / f"n{n}.pyz", build_id=f"new{n}"), pyz_env(cache)).returncode == 0
        assert "old" in _cached_builds(cache)
    finally:
        flag.write_text("")
        out, err = running.communicate(timeout=120)
    assert running.returncode == 0, err
    assert out.split() == ["ok", "common"]


@pytest.mark.skipif(IS_WINDOWS, reason="the OS releases a killed process's lock at its own pace on Windows")
def test_pyz_prune_removes_an_old_build_whose_start_was_killed(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    files = {"common/app/main.py": MAIN_SIGNALS, "common/lib/lazymod.py": "WHERE = 'common'\n"}
    old = fake_pyz(tmp_path / "old.pyz", build_id="old", files=files)
    killed, _flag = _start_waiting(old, cache, tmp_path)
    killed.kill()
    killed.communicate(timeout=120)
    _age(pyz_root(cache) / "old", 2)
    for n in (1, 2, 3):
        assert run_pyz(fake_pyz(tmp_path / f"n{n}.pyz", build_id=f"new{n}"), pyz_env(cache)).returncode == 0
    assert _cached_builds(cache) == ["new1", "new2", "new3"]


def test_pyz_prune_still_removes_old_unused_builds(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    for n in (1, 2, 3):
        assert run_pyz(fake_pyz(tmp_path / f"b{n}.pyz", build_id=f"build{n}"), pyz_env(cache)).returncode == 0
    old = time.time() - 3 * 86400
    for n, name in enumerate(("build1", "build2", "build3")):
        os.utime(pyz_root(cache) / name, (old + n, old + n))
    assert run_pyz(fake_pyz(tmp_path / "b4.pyz", build_id="build4"), pyz_env(cache)).returncode == 0
    assert _cached_builds(cache) == ["build2", "build3", "build4"]  # the 3 most recently started


def test_pyz_rerun_marks_a_build_as_recently_used(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    pyz_file = fake_pyz(tmp_path / "t.pyz")
    assert run_pyz(pyz_file, pyz_env(cache)).returncode == 0
    build = pyz_root(cache) / "b1"
    os.utime(build, (1_000_000_000, 1_000_000_000))
    assert run_pyz(pyz_file, pyz_env(cache)).returncode == 0
    assert build.stat().st_mtime > time.time() - 3600


def _bootstrap_namespace() -> dict[str, Any]:
    """The bootstrap's functions, without running main()."""
    source = (TEMPLATES / "pyz" / "__main__.py").read_text(encoding="utf-8")
    assert source.rstrip().endswith("main()")
    namespace: dict[str, Any] = {"__name__": "pt_bootstrap"}
    exec(compile(source.rstrip()[: -len("main()")], "__main__.py", "exec"), namespace)  # noqa: S102
    return namespace


@pytest.mark.parametrize(
    ("plat", "sys_platform", "machine", "maxsize", "arch"),
    [
        ("win32", "win-amd64", "ARM64", 2**63 - 1, "x86_64"),  # an x64 Python on Windows on ARM (WMI names the CPU)
        ("win32", "win-arm64", "ARM64", 2**63 - 1, "aarch64"),
        ("win32", "win-amd64", "AMD64", 2**63 - 1, "x86_64"),
        ("win32", "win32", "AMD64", 2**31 - 1, "x86"),  # a 32-bit Python on 64-bit Windows
        ("linux", "linux-x86_64", "x86_64", 2**63 - 1, "x86_64"),
        ("linux", "linux-aarch64", "aarch64", 2**63 - 1, "aarch64"),
        ("linux", "linux-i686", "i686", 2**31 - 1, "x86"),  # the builder said x86, the bootstrap i686
        ("linux", "linux-armv7l", "aarch64", 2**31 - 1, "armv7l"),  # Raspberry Pi OS 32-bit on a 64-bit kernel
        ("linux", "linux-armv7l", "armv7l", 2**31 - 1, "armv7l"),
        ("darwin", "macosx-11.0-arm64", "arm64", 2**63 - 1, "aarch64"),
        ("darwin", "macosx-10.13-x86_64", "x86_64", 2**63 - 1, "x86_64"),  # Rosetta: uname names the process
    ],
)
def test_pyz_key_names_the_interpreter_not_the_machine(monkeypatch: pytest.MonkeyPatch, plat: str, sys_platform: str, machine: str, maxsize: int, arch: str) -> None:
    # platform.machine() on Windows asks WMI for the native CPU: an x64 Python on Windows on ARM got
    # the aarch64 key and refused a pyz with a windows-x86_64 target; 32-bit x86 was "x86" for the
    # builder and "i686" for the bootstrap, so a pyz never found the target it was built for
    import types

    fake_sys = types.SimpleNamespace(platform=plat, maxsize=maxsize, implementation=sys.implementation, version_info=sys.version_info)
    fake_platform = types.SimpleNamespace(machine=lambda: machine)
    fake_sysconfig = types.SimpleNamespace(get_platform=lambda: sys_platform)
    namespace = _bootstrap_namespace()
    namespace.update(sys=fake_sys, platform=fake_platform, sysconfig=fake_sysconfig)
    assert namespace["_key"]().rsplit("-", 1)[1] == arch
    monkeypatch.setattr(common, "sys", fake_sys)
    monkeypatch.setattr(common, "platform", fake_platform)
    monkeypatch.setattr(common, "sysconfig", fake_sysconfig)
    assert common.host_arch() == arch  # the build's host key: the same name


def test_pyz_prune_tolerates_vanishing_folders(tmp_path: Path) -> None:
    # Two builds pruning at once: a folder listed by iterdir() is gone before its stat()
    base = type(tmp_path)

    class Racy(base):  # type: ignore[valid-type,misc]
        def iterdir(self) -> Iterator[Path]:
            yield from super().iterdir()
            yield self / "ghost"

        def is_dir(self, **kwargs: Any) -> bool:
            return True if self.name == "ghost" else super().is_dir(**kwargs)

        def stat(self, **kwargs: Any) -> os.stat_result:
            if self.name == "ghost":
                raise FileNotFoundError(str(self))
            return super().stat(**kwargs)

    (tmp_path / "old1").mkdir()
    os.utime(tmp_path / "old1", (1, 1))
    namespace = _bootstrap_namespace()
    namespace["_prune_old"](Racy(tmp_path), "current")  # must not raise
    namespace["_prune_old"](Racy(tmp_path / "missing"), "current")  # a cache root that vanished


# --- pyz: build() with the installs recorded --------------------------------------------------------


def _wheel(site: Path, name: str, version: str, tag: str = "py3-none-any", files: dict[str, bytes] | None = None) -> None:
    """Fake an installed distribution: <name>-<version>.dist-info/WHEEL (+ files)."""
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "WHEEL").write_text(f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n", encoding="utf-8")
    for rel_name, data in (files or {f"{name.replace('-', '_')}/__init__.py": b""}).items():
        (site / rel_name).parent.mkdir(parents=True, exist_ok=True)
        (site / rel_name).write_bytes(data)


def _requirements(tmp_path: Path, *pins: str) -> Path:
    lines = ["# This file was autogenerated by uv via the following command:", f"#    uv export --output-file {tmp_path}/requirements.txt"]
    for pin in pins:
        lines += [f"{pin} \\", "    --hash=sha256:" + "0" * 64, "    # via myapp"]
    path = tmp_path / "requirements.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _pyz_build(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
    sites: dict[str, Any],
    pins: list[str],
    *,
    backend: str = "cpython",
    cfg: Config | None = None,
    compiled_files: bool = False,
    app: Path | None = None,
) -> tuple[Path, dict[str, Any], set[str]]:
    """Run pyz.build with the target list and the installs faked: `sites` maps a key to a
    function filling that target's site folder."""
    from runner.methods import pyz

    cfg = cfg or make({})
    app = app or fake_app(sandbox / "payload", cfg.pkg)
    if compiled_files:
        (app / cfg.pkg / "core" / f"bench{EXT}").write_bytes(b"\x7fELF")
        (app / cfg.pkg / "core" / "bench.py").write_text("X = 1\n", encoding="utf-8")
        (app / f"{cfg.pkg}__mypyc{EXT}").write_bytes(b"\x7fELF")
    keys = list(sites)
    monkeypatch.setattr(common, "targets_for", lambda c, b, k: [common.parse_key(x) for x in keys])
    requirements = _requirements(sandbox, *pins)
    monkeypatch.setattr(common, "export_requirements", lambda c: requirements)

    def install(c: Config, b: str, t: common.Target, dest: Path, req: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        sites[t.key](dest)
        return dest

    monkeypatch.setattr(common, "install_deps", install)
    out = pyz.build(BuildRequest(cfg, backend, "pyz", app))
    import zipfile

    with zipfile.ZipFile(out) as archive:
        info = json.loads(archive.read("_pyz.json"))
        names = set(archive.namelist())
    return out, info, names


LINUX = "cp314-linux-x86_64"
WIN = "cp314-windows-x86_64"
MAC = "cp314-macos-aarch64"


def test_pyz_pure_build_layout(sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    out, info, names = _pyz_build(sandbox, monkeypatch, {LINUX: lambda d: _wheel(d, "rich", "15.0.0")}, ["rich==15.0.0 ; implementation_name == 'cpython'"])
    assert info["pure"] is True and info["targets"] == [] and info["host"] == LINUX
    assert {"common/lib/rich/__init__.py", "common/app/main.py", "common/app/assets/logo.txt", "__main__.py", "_pyz.json"} <= names
    assert not [n for n in names if n.startswith("targets/")]
    assert len(info["deps"]) == 16 and info["build_id"]
    assert "pure: works with CPython >= 3.14 on any OS" in capsys.readouterr().err  # PyPy only when supported
    assert out.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    if not IS_WINDOWS:
        assert os.stat(out).st_mode & 0o111
    assert (out.parent / "myapp.cmd").is_file()


def test_pyz_build_id_is_stable(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pins = ["rich==15.0.0"]
    _, first, _ = _pyz_build(sandbox, monkeypatch, {LINUX: lambda d: _wheel(d, "rich", "15.0.0")}, pins)
    shutil.rmtree(sandbox / "payload")
    _, second, _ = _pyz_build(sandbox, monkeypatch, {LINUX: lambda d: _wheel(d, "rich", "15.0.0")}, pins)
    assert first["build_id"] == second["build_id"] and first["deps"] == second["deps"]


def test_pyz_with_marker_skipped_deps_is_not_pure(sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # loguru needs colorama/win32_setctime on Windows, jaraco.context needs backports-tarfile on
    # 3.11/PyPy: the host install skips them, yet the pyz said "pure ... on any OS"
    pins = ["colorama==0.4.6 ; sys_platform == 'win32'", "loguru==0.7.3 ; implementation_name == 'cpython'"]
    _, info, names = _pyz_build(sandbox, monkeypatch, {LINUX: lambda d: _wheel(d, "loguru", "0.7.3")}, pins)
    assert info["pure"] is False and info["targets"] == [LINUX]
    assert f"targets/{LINUX}/lib/loguru/__init__.py" in names
    assert not [n for n in names if n.startswith("common/lib/")]
    err = capsys.readouterr().err
    assert "colorama==0.4.6" in err and "any OS" not in err and "pyz-merge" in err


def test_pyz_uses_per_target_libs_when_sites_differ(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # tzlocal needs tzdata on Windows only: an explicit Windows target installed it, and the
    # "pure" build then threw that site away
    pins = ["tzlocal==5.4.4", "tzdata==2026.4 ; sys_platform == 'win32'"]
    sites = {
        LINUX: lambda d: _wheel(d, "tzlocal", "5.4.4"),
        WIN: lambda d: (_wheel(d, "tzlocal", "5.4.4"), _wheel(d, "tzdata", "2026.4")),
    }
    _, info, names = _pyz_build(sandbox, monkeypatch, sites, pins)
    assert info["pure"] is False and info["targets"] == sorted([LINUX, WIN])
    assert f"targets/{WIN}/lib/tzdata/__init__.py" in names and f"targets/{LINUX}/lib/tzlocal/__init__.py" in names
    assert f"targets/{LINUX}/lib/tzdata/__init__.py" not in names


def test_pyz_platform_wheel_without_extension_is_native(sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # imageio-ffmpeg: a py3-none-<platform> wheel whose binary has no .so/.pyd suffix
    sites = {
        LINUX: lambda d: _wheel(d, "imageio-ffmpeg", "0.6.0", "py3-none-manylinux2014_x86_64", {"imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2": b"\x7fELF"}),
        WIN: lambda d: _wheel(d, "imageio-ffmpeg", "0.6.0", "py3-none-win_amd64", {"imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe": b"MZ"}),
    }
    _, info, names = _pyz_build(sandbox, monkeypatch, sites, ["imageio-ffmpeg==0.6.0"])
    assert info["pure"] is False and info["targets"] == sorted([LINUX, WIN])
    assert f"targets/{WIN}/lib/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe" in names
    assert "runs on:" in capsys.readouterr().err
    assert info["floor"] == {LINUX: "glibc 2.17"}  # the bootstrap: never on musl nor an older glibc


def test_pyz_mypyc_overlay_holds_only_extensions(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({"backend": {"active": "mypyc"}})
    _, info, names = _pyz_build(sandbox, monkeypatch, {LINUX: lambda d: _wheel(d, "rich", "15.0.0")}, ["rich==15.0.0"], backend="mypyc", cfg=cfg, compiled_files=True)
    overlay = sorted(n for n in names if n.startswith(f"targets/{LINUX}/app/"))
    assert overlay == [f"targets/{LINUX}/app/myapp/core/bench{EXT}", f"targets/{LINUX}/app/myapp__mypyc{EXT}"]
    assert not [n for n in names if n.startswith("common/") and n.endswith((".so", ".pyd"))]
    assert "common/app/myapp/core/bench.py" in names  # the pure fallback of other platforms
    assert info["pure"] is True and info["targets"] == [LINUX]


def test_pyz_accepts_payload_files_older_than_1980(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A copy from the Nix store has mtime 1: zipapp raised "ZIP does not support timestamps
    # before 1980" (an internal-error traceback)
    import zipfile

    def old_site(d: Path) -> None:
        _wheel(d, "rich", "15.0.0")
        os.utime(d / "rich" / "__init__.py", (0, 0))

    app = fake_app(sandbox / "payload")
    os.utime(app / "myapp" / "app.py", (1, 1))
    out, _, names = _pyz_build(sandbox, monkeypatch, {LINUX: old_site}, ["rich==15.0.0"], app=app)
    assert "common/app/myapp/app.py" in names and "common/lib/rich/__init__.py" in names
    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None
        assert archive.getinfo("common/app/myapp/app.py").date_time[0] == 1980
    assert os.stat(app / "myapp" / "app.py").st_mtime == 1  # the payload itself is untouched


@pytest.mark.parametrize(("backend", "windowed"), [("cpython", ["pyw -3.14", "pythonw", "pythonw", "pypyw"]), ("pypy", ["pypyw", "pypyw", "pyw", "pythonw"])])
def test_pyz_wrapper_gui_runs_windowed(backend: str, windowed: list[str]) -> None:
    from runner.methods import pyz

    cfg = make({"app": {"gui": True}, "backend": {"supported": ["cpython", "pypy"]}})
    lines = pyz._wrapper_cmd(cfg, backend, "a.pyz").split("\r\n")
    runs = [lines[i + 1] for i, line in enumerate(lines) if line.startswith(":run")]
    assert runs == [f'start "" {w} "%~dp0a.pyz" %*' for w in windowed]
    assert [ln for ln in lines if ">nul 2>nul" in ln][0].split(" -c ")[0] in ("py -3.14", "pypy3")  # the probe keeps console names
    console = pyz._wrapper_cmd(make({"backend": {"supported": ["cpython", "pypy"]}}), backend, "a.pyz")
    assert 'start ""' not in console and "pythonw" not in console and "pyw" not in console
    assert "PYTHON_MANAGER_" not in console  # the Python install manager's settings stay the user's


# --- pyz-merge ------------------------------------------------------------------------------------

FOREIGN = "cp399-nowhere-x86_64"  # a platform no test machine has
MERGE_MAIN = "import dep\nprint('dep=' + dep.WHERE)\n"


def _merge(parts: list[Path], out: Path, cfg: Config | None = None) -> tuple[dict[str, Any], set[str]]:
    import zipfile

    from runner.methods import pyz

    pyz.merge(parts, out, cfg or make({}))
    with zipfile.ZipFile(out) as archive:
        return json.loads(archive.read("_pyz.json")), set(archive.namelist())


def _native_part(path: Path, *, compiled: bool = False, key: str = FOREIGN, main: str = MERGE_MAIN, **info: Any) -> Path:
    files = {"common/app/main.py": main, f"targets/{key}/lib/dep.py": "WHERE = 'native part'\n"}
    if compiled:
        files[f"targets/{key}/app/overlay.txt"] = "native"
    info.setdefault("host", key)
    info.setdefault("backend", "mypyc" if compiled else "cpython")
    return fake_pyz(path, targets=[key], pure=False, files=files, **info)


def _pure_part(
    path: Path, *, compiled: bool = False, host: str | None = None, record_host: bool = True, where: str = "pure part", **info: Any
) -> Path:
    key = host or _host_key()
    files = {"common/app/main.py": MERGE_MAIN, "common/lib/dep.py": f"WHERE = {where!r}\n"}
    if compiled:
        files[f"targets/{key}/app/overlay.txt"] = "pure"
    if record_host:  # an older ./pyt did not record it
        info["host"] = key
    return fake_pyz(path, targets=[key] if compiled else [], pure=True, files=files, backend="mypyc" if compiled else "cpython", **info)


@pytest.mark.parametrize("compiled", [False, True])
@pytest.mark.parametrize("pure_first", [True, False])
def test_pyz_merge_keeps_the_dependencies_of_a_pure_part(tmp_path: Path, pure_first: bool, compiled: bool) -> None:
    # CI merges pyz-parts/*/*.pyz with macOS first: a native macOS part plus a pure Linux part
    # lost Linux's dependencies ("no binaries for this interpreter", or ModuleNotFoundError)
    host = _host_key()
    pure = _pure_part(tmp_path / "pure.pyz", compiled=compiled)
    native = _native_part(tmp_path / "native.pyz", compiled=compiled)
    info, names = _merge([pure, native] if pure_first else [native, pure], tmp_path / "merged.pyz")
    assert info["pure"] is False and info["targets"] == sorted([FOREIGN, host]) and "host" not in info
    assert {f"targets/{host}/lib/dep.py", f"targets/{FOREIGN}/lib/dep.py"} <= names
    assert not [n for n in names if n.startswith("common/lib/")]
    if compiled:
        assert {f"targets/{host}/app/overlay.txt", f"targets/{FOREIGN}/app/overlay.txt"} <= names
    r = run_pyz(tmp_path / "merged.pyz", pyz_env(tmp_path / "cache"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "dep=pure part"


def test_pyz_merge_of_pure_parts_stays_pure(tmp_path: Path) -> None:
    a = _pure_part(tmp_path / "a.pyz", host="cp314-linux-x86_64", where="first")
    b = _pure_part(tmp_path / "b.pyz", host="cp314-windows-x86_64", where="second")
    info, names = _merge([a, b], tmp_path / "m.pyz")
    assert info["pure"] is True and info["targets"] == [] and "common/lib/dep.py" in names
    r = run_pyz(tmp_path / "m.pyz", pyz_env(tmp_path / "cache"))
    assert r.returncode == 0 and r.stdout.strip() == "dep=first", r.stderr
    # --out may be one of the inputs: every part is read before the output is replaced
    info, _ = _merge([a, b], a)
    assert info["pure"] is True
    r = run_pyz(a, pyz_env(tmp_path / "cache2"))
    assert r.returncode == 0 and r.stdout.strip() == "dep=first", r.stderr


def test_pyz_merge_says_a_pure_result_runs_everywhere(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A pure part plus a pure mypyc part: _pyz.json said pure, the message "runs on <the overlay key>"
    a = _pure_part(tmp_path / "a.pyz", host=LINUX, where="first")
    b = _pure_part(tmp_path / "b.pyz", compiled=True, host=WIN)
    info, _ = _merge([a, b], tmp_path / "m.pyz")
    assert info["pure"] is True and info["targets"] == [WIN]
    err = capsys.readouterr().err
    assert "pure: works with Python >= 3.11 on any OS" in err and f"compiled code for {WIN}" in err and "runs on" not in err
    _merge([_native_part(tmp_path / "x.pyz", key=LINUX), _native_part(tmp_path / "y.pyz", key=WIN)], tmp_path / "n.pyz")
    err = capsys.readouterr().err
    assert f"runs on {LINUX}, {WIN}" in err and "pure:" not in err


def test_pyz_merge_to_a_place_it_cannot_write_is_a_clear_error(tmp_path: Path) -> None:
    # --out is the user's: in a folder it may not write (another user's, read-only, /sys) it
    # ended in an "internal runner error" traceback
    from runner.methods import pyz

    a = _pure_part(tmp_path / "a.pyz", host="cp314-linux-x86_64")
    b = _pure_part(tmp_path / "b.pyz", host="cp314-windows-x86_64")
    (tmp_path / "file").write_text("a file where the folder must go", encoding="utf-8")
    with pytest.raises(PytError, match=r"pyz-merge: cannot write .*file.*all\.pyz") as e:
        pyz.merge([a, b], tmp_path / "file" / "all.pyz", make({}))
    assert e.value.code == 2
    (tmp_path / "out" / "all.pyz.tmp").mkdir(parents=True)  # the archive cannot be written there
    with pytest.raises(PytError, match=r"pyz-merge: cannot write .*all\.pyz") as e:
        pyz.merge([a, b], tmp_path / "out" / "all.pyz", make({}))
    assert e.value.code == 2 and sorted(p.name for p in (tmp_path / "out").iterdir()) == ["all.pyz.tmp"]


def test_pyz_merge_takes_each_lib_from_the_part_built_there(tmp_path: Path) -> None:
    # Every CI part carries targets/<every key>/lib with [deploy.pyz] targets: mixing two
    # installs file by file could mix versions; the part built ON that platform wins
    def part(name: str, host: str) -> Path:
        files = {"common/app/main.py": MERGE_MAIN}
        for key in (LINUX, WIN):
            files[f"targets/{key}/lib/dep.py"] = f"WHERE = '{name} for {key}'\n"
        return fake_pyz(tmp_path / f"{name}.pyz", targets=[LINUX, WIN], pure=False, files=files, host=host)

    import zipfile

    out = tmp_path / "m.pyz"
    _merge([part("a", LINUX), part("b", WIN)], out)
    with zipfile.ZipFile(out) as archive:
        assert archive.read(f"targets/{LINUX}/lib/dep.py").decode() == f"WHERE = 'a for {LINUX}'\n"
        assert archive.read(f"targets/{WIN}/lib/dep.py").decode() == f"WHERE = 'b for {WIN}'\n"


def test_pyz_merge_refuses_invalid_parts(tmp_path: Path) -> None:
    import zipfile

    good = _native_part(tmp_path / "good.pyz")
    no_info = tmp_path / "no-info.zip"
    with zipfile.ZipFile(no_info, "w") as z:
        z.writestr("common/app/main.py", "")
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("_pyz.json", "{not json")
    partial = tmp_path / "partial.zip"
    with zipfile.ZipFile(partial, "w") as z:
        z.writestr("_pyz.json", json.dumps({"name": "demo"}))
    wrong_type = tmp_path / "wrong-type.zip"
    with zipfile.ZipFile(wrong_type, "w") as z:
        z.writestr("_pyz.json", json.dumps({"name": "demo", "build_id": "x", "min_python": "3.11", "targets": [], "pure": True}))
    out = tmp_path / "out.pyz"
    for bad in (no_info, broken, partial, wrong_type):
        with pytest.raises(PytError, match="no valid _pyz.json"):  # was a KeyError traceback
            _merge([good, bad], out)
    assert not out.exists()
    # A member name that climbs out of the scratch folder is refused, never written
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(evil, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr(f"targets/{FOREIGN}/lib/../../../../escaped.py", "x")
    with pytest.raises(PytError, match="unsafe member name"):
        _merge([evil, _native_part(tmp_path / "other.pyz", key=LINUX)], out)
    assert not out.exists() and not list(tmp_path.parent.glob("escaped.py"))


def test_pyz_merge_refuses_two_compiled_apps_for_one_platform(tmp_path: Path) -> None:
    a = _native_part(tmp_path / "a.pyz", compiled=True)
    b = _native_part(tmp_path / "b.pyz", compiled=True)
    with pytest.raises(PytError, match="one part per platform"):
        _merge([a, b], tmp_path / "m.pyz")
    assert not (tmp_path / "m.pyz").exists()


def test_pyz_merge_refuses_parts_of_different_builds(tmp_path: Path) -> None:
    a = _native_part(tmp_path / "a.pyz", key=LINUX, deps="d1")
    crlf = _native_part(tmp_path / "crlf.pyz", key=WIN, deps="d1", main=MERGE_MAIN.replace("\n", "\r\n"))
    info, _ = _merge([a, crlf], tmp_path / "ok.pyz")  # a CRLF checkout (Windows CI) is not another build
    assert info["targets"] == [LINUX, WIN]
    other_code = _native_part(tmp_path / "v2.pyz", key=WIN, deps="d1", main=MERGE_MAIN + "# v2\n")
    with pytest.raises(PytError, match="other app code"):
        _merge([a, other_code], tmp_path / "m.pyz")
    other_deps = _native_part(tmp_path / "deps.pyz", key=WIN, deps="d2")
    with pytest.raises(PytError, match="different dependencies"):
        _merge([a, other_deps], tmp_path / "m.pyz")
    other_min = _native_part(tmp_path / "min.pyz", key=WIN, deps="d1", min_python=[3, 12])
    with pytest.raises(PytError, match="minimum Python"):
        _merge([a, other_min], tmp_path / "m.pyz")
    renamed = _native_part(tmp_path / "other.pyz", key=WIN, name="other")
    with pytest.raises(PytError, match="different apps"):
        _merge([a, renamed], tmp_path / "m.pyz")
    assert not (tmp_path / "m.pyz").exists()


def test_pyz_merge_needs_the_host_of_a_pure_part(tmp_path: Path) -> None:
    native = _native_part(tmp_path / "native.pyz")
    old = _pure_part(tmp_path / "old.pyz", record_host=False)
    with pytest.raises(PytError, match="rebuild it"):
        _merge([native, old], tmp_path / "m.pyz")
    assert not (tmp_path / "m.pyz").exists()
    # An older pure mypyc part names its host through its single overlay target
    old_mypyc = _pure_part(tmp_path / "old-mypyc.pyz", compiled=True, host=LINUX, record_host=False)
    info, names = _merge([_native_part(tmp_path / "n2.pyz", compiled=True), old_mypyc], tmp_path / "m2.pyz")
    assert info["targets"] == sorted([FOREIGN, LINUX]) and f"targets/{LINUX}/lib/dep.py" in names
    # Two old NON-pure parts need no host (test_paths covers them too)
    info, _ = _merge([_native_part(tmp_path / "x.pyz", key=LINUX, host=""), _native_part(tmp_path / "y.pyz", key=WIN, host="")], tmp_path / "m3.pyz")
    assert info["targets"] == [LINUX, WIN]


def test_pyz_merge_says_how_to_merge_a_merged_pure_file_again(tmp_path: Path) -> None:
    # CI's merged .pyz (every part pure) merged again with a per-platform part: "an older ./pyt
    # made it: rebuild it", though this ./pyt made it and a merge cannot be rebuilt
    from runner.methods import pyz

    first = _pure_part(tmp_path / "linux.pyz", host=LINUX, where="first")
    second = _pure_part(tmp_path / "win.pyz", host=WIN, where="second")
    info, _ = _merge([first, second], tmp_path / "all.pyz")
    assert info["merged"] is True and "host" not in info
    native = _native_part(tmp_path / "native.pyz")
    for check in (lambda: pyz.check_parts([tmp_path / "all.pyz", native], tmp_path / "x.pyz"), lambda: _merge([native, tmp_path / "all.pyz"], tmp_path / "x.pyz")):
        with pytest.raises(PytError) as e:
            check()  # --dry-run (check_parts) says it too
        assert "is a merge of pure parts" in str(e.value) and "pass the parts it was merged from to one ./pyt pyz-merge call" in str(e.value)
        assert "older ./pyt" not in str(e.value)
    assert not (tmp_path / "x.pyz").exists()
    # What it says works: the parts it was merged from, with the other one, in one call
    info, names = _merge([first, second, native], tmp_path / "x.pyz")
    assert info["targets"] == sorted([FOREIGN, LINUX, WIN]) and f"targets/{WIN}/lib/dep.py" in names
    # A merge made before the "merged" key: both causes are named
    old_merge = fake_pyz(tmp_path / "old-all.pyz", pure=True, files={"common/app/main.py": MERGE_MAIN, "common/lib/dep.py": "WHERE = 'x'\n"})
    with pytest.raises(PytError, match="if an earlier pyz-merge made it, pass the parts it was merged from .*; if an older ./pyt built it, rebuild it"):
        pyz.check_parts([old_merge, native], tmp_path / "y.pyz")


@pytest.mark.parametrize("gui", [False, True])
@pytest.mark.parametrize("backends", [("cpython", "cpython"), ("pypy", "pypy"), ("pypy", "cpython")])
def test_pyz_merge_writes_the_windows_wrapper(tmp_path: Path, gui: bool, backends: tuple[str, str]) -> None:
    # CI's merged .pyz had no <name>.cmd: Windows users ran it without UTF-8 mode and without the
    # interpreter search a build writes next to its .pyz. The wrapper follows the PARTS (their
    # name and minimum Python), the order of a pypy build only when every part is one.
    from runner.methods import pyz

    a = _native_part(tmp_path / "a.pyz", key=LINUX, name="parts-app", min_python=[3, 12], backend=backends[0])
    b = _native_part(tmp_path / "b.pyz", key=WIN, name="parts-app", min_python=[3, 12], backend=backends[1])
    cfg = make({"app": {"gui": gui}, "backend": {"supported": ["cpython", "pypy"]}})
    _merge([a, b], tmp_path / "dist" / "merged.pyz", cfg)
    wrapper = tmp_path / "dist" / "merged.cmd"
    data = wrapper.read_bytes()
    assert data.isascii() and data.endswith(b"\r\n") and b"\n" not in data.replace(b"\r\n", b"")
    text = data.decode("ascii")
    expected = pyz._wrapper_cmd(cfg, "pypy" if backends == ("pypy", "pypy") else "cpython", "merged.pyz", name="parts-app", min_python="3.12")
    assert text == expected
    assert '"%~dp0merged.pyz" %*' in text and "(3, 12)" in text and "parts-app: needs Python or PyPy 3.12" in text
    assert ('start ""' in text) is gui
    first_probe = next(ln for ln in text.split("\r\n") if ln.endswith("&& goto run0"))
    assert first_probe.startswith("pypy3 -c " if backends == ("pypy", "pypy") else "py -3.14 -c ")


def test_pyz_merge_refuses_an_output_its_wrapper_would_replace(tmp_path: Path) -> None:
    from runner.methods import pyz

    a = _native_part(tmp_path / "a.pyz", key=LINUX)
    b = _native_part(tmp_path / "b.pyz", key=WIN)
    for name in ("merged.cmd", "merged.CMD"):  # any case: macOS and Windows folders ignore it
        with pytest.raises(PytError, match="its own .cmd wrapper"):
            pyz.merge([a, b], tmp_path / name, make({}))
    assert not list(tmp_path.glob("merged.*"))
    assert pyz.wrapper_path(Path("x/app")) == Path("x/app.cmd") and pyz.wrapper_path(Path("app.pyz")) == Path("app.cmd")


@pytest.mark.parametrize("name", ["juego-ni\u00f1o.pyz", "100%.pyz", 'say"hi".pyz', "a!b.pyz", "a&b.pyz", "tab\there.pyz"])
def test_pyz_merge_refuses_an_output_name_its_wrapper_cannot_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    # A non-ASCII --out: the .pyz was written, then the ASCII .cmd raised UnicodeEncodeError (an
    # internal error with a traceback) and left a 0-byte wrapper; --dry-run accepted the name. A
    # % in it would be expanded by cmd inside "%~dp0<name>"
    from runner.methods import pyz

    a = _native_part(tmp_path / "a.pyz", key=LINUX)
    b = _native_part(tmp_path / "b.pyz", key=WIN)
    out = tmp_path / "out" / name
    with pytest.raises(PytError, match="wrapper") as e:
        pyz.merge([a, b], out, make({}))
    assert e.value.code == 2
    assert not (tmp_path / "out").exists()  # nothing written
    monkeypatch.setattr(proc, "DRY_RUN", True)
    monkeypatch.setattr(cmd_build, "user_path", lambda raw: Path(raw))
    with pytest.raises(PytError, match="wrapper"):
        cmd_build.cmd_pyz_merge(make({}), [str(a), str(b), "--out", str(out)])


def test_pyz_merge_keeps_the_executable_bits(tmp_path: Path) -> None:
    # pyz-merge rewrote every member from a scratch copy: the parts' 0755 became 0644 (and on
    # Windows, where files have no modes, it would be lost whatever the copy did)
    import zipfile

    a = _native_part(tmp_path / "a.pyz", key=LINUX)
    b = _native_part(tmp_path / "b.pyz", key=WIN)
    _add_members(a, {"common/app/tool.sh": 0o755, f"targets/{LINUX}/lib/bin/tool": 0o755, "common/app/data.txt": 0o644})
    _add_members(b, {"common/app/tool.sh": 0o755, "common/app/data.txt": 0o644})
    out = tmp_path / "m.pyz"
    _merge([a, b], out)
    with zipfile.ZipFile(out) as merged:
        modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in merged.infolist()}
    assert modes["common/app/tool.sh"] == 0o755 and modes[f"targets/{LINUX}/lib/bin/tool"] == 0o755
    assert not modes["common/app/data.txt"] & 0o111 and not modes["common/app/main.py"] & 0o111
    # A first part made on Windows stores no mode: the app's executable keeps it from another part
    windows_first = _native_part(tmp_path / "w.pyz", key=WIN)
    _add_members(windows_first, {"common/app/tool.sh": 0o644, "common/app/data.txt": 0o644})
    _merge([windows_first, a], out)
    with zipfile.ZipFile(out) as merged:
        assert (merged.getinfo("common/app/tool.sh").external_attr >> 16) & 0o777 == 0o755


def test_pyz_merge_refuses_an_app_name_that_is_no_app_name(tmp_path: Path) -> None:
    # The part's name goes into the wrapper's unquoted `echo <name>: needs Python...`
    a = _native_part(tmp_path / "a.pyz", key=LINUX, name="demo & calc")
    b = _native_part(tmp_path / "b.pyz", key=WIN, name="demo & calc")
    with pytest.raises(PytError, match="no valid _pyz.json"):
        _merge([a, b], tmp_path / "m.pyz")
    assert not (tmp_path / "m.pyz").exists()


def test_pyz_merge_dry_run_checks_the_parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # --dry-run used to accept any zip: the real merge then refused it (another app, no _pyz.json)
    import zipfile

    a = _native_part(tmp_path / "a.pyz", key=LINUX)
    other = _native_part(tmp_path / "other.pyz", key=WIN, name="other")
    no_info = tmp_path / "plain.zip"
    with zipfile.ZipFile(no_info, "w") as z:
        z.writestr("x.txt", "")
    monkeypatch.setattr(proc, "DRY_RUN", True)
    monkeypatch.setattr(cmd_build, "user_path", lambda raw: Path(raw))
    cfg = make({})
    with pytest.raises(PytError, match="different apps"):
        cmd_build.cmd_pyz_merge(cfg, [str(a), str(other), "--out", str(tmp_path / "m.pyz")])
    with pytest.raises(PytError, match="no valid _pyz.json"):
        cmd_build.cmd_pyz_merge(cfg, [str(a), str(no_info), "--out", str(tmp_path / "m.pyz")])
    capsys.readouterr()
    b = _native_part(tmp_path / "b.pyz", key=WIN)
    assert cmd_build.cmd_pyz_merge(cfg, [str(a), str(b), "--out", str(tmp_path / "m.pyz")]) == 0
    outs = [line.split(maxsplit=1)[1] for line in capsys.readouterr().err.splitlines() if line.startswith("  out ")]
    assert outs == [str(tmp_path / "m.pyz"), str(tmp_path / "m.cmd")]
    assert not (tmp_path / "m.pyz").exists() and not (tmp_path / "m.cmd").exists()


@needs_rich
def test_pyz_merge_of_a_real_build(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A REAL host .pyz (uv export of this project's lock + uv pip install), run with `python -S`,
    then merged with a native part of another platform and run again."""
    from runner.methods import pyz

    cfg = real({})
    skip_when_older_than(cfg)
    app = fake_app(sandbox / "payload")
    (app / "main.py").write_text("import rich, sys\nprint('rich', rich.__name__, *sys.argv[1:])\n", encoding="utf-8")
    try:
        out = pyz.build(BuildRequest(cfg, "cpython", "pyz", app))
    except proc.CommandFailed as e:
        pytest.skip(f"uv could not export/install the locked dependencies (offline?): {e}")
    r = run_pyz(out, pyz_env(sandbox / "cache"), "arg")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "rich rich arg"
    import zipfile

    with zipfile.ZipFile(out) as archive:
        info = json.loads(archive.read("_pyz.json"))
        names = archive.namelist()
        app_files = {n: archive.read(n).decode() for n in names if n.startswith("common/app/")}
    if not info["pure"] and not TEMPLATE_REPO:  # the template's lock is pure Python: there it must be pure
        pytest.skip("this project's dependencies are not pure Python: the merge below needs a pure host part")
    assert info["pure"] is True and info["host"] == _host_key()
    assert not [n for n in names if "/bin/" in n or n.endswith("/.lock")]  # no uv junk
    # The same build made on another platform, whose dependencies are native there
    files = {**app_files, f"targets/{FOREIGN}/lib/dep.py": "WHERE = 'native'\n"}
    other = fake_pyz(sandbox / "native.pyz", targets=[FOREIGN], pure=False, files=files, name="myapp", min_python=info["min_python"], deps=info["deps"], host=FOREIGN)
    merged, merged_names = _merge([other, out], sandbox / "merged.pyz")
    assert merged["targets"] == sorted([FOREIGN, _host_key()]) and f"targets/{_host_key()}/lib/rich/__init__.py" in merged_names
    r = run_pyz(sandbox / "merged.pyz", pyz_env(sandbox / "cache2"), "x")
    assert r.returncode == 0 and r.stdout.strip() == "rich rich x", r.stderr


# --- target keys, installs, environments --------------------------------------------------------


@pytest.mark.parametrize(
    ("supported", "backend", "key", "message"),
    [
        (["cpython"], "cpython", "cp313-windows-x86_64", "only resolves CPython 3.14"),  # below min_python
        (["cpython"], "cpython", "cp315-windows-x86_64", "only resolves CPython 3.14"),  # above the lock bound
        (["cpython", "pypy"], "cpython", "cp312-windows-x86_64", "only resolves CPython 3.14"),  # >= min 3.11, not locked
        (["cpython", "pypy"], "cpython", "cp313-linux-x86_64", "only resolves CPython 3.14"),  # host OS: got cp314 wheels
        (["cpython", "pypy"], "cpython", "pp311-linux-x86_64", "pypy build"),  # got CPython wheels
        (["cpython"], "cpython", "pp311-linux-x86_64", "pypy build"),  # PyPy not supported
        (["cpython", "pypy"], "pypy", "pp310-linux-x86_64", "pypy build"),
        (["cpython", "pypy"], "pypy", "pp311-windows-x86_64", "pypy build"),
        (["cpython"], "cpython", "cp314-plan9-x86_64", "invalid platform key"),
    ],
)
def test_target_keys_the_lock_cannot_serve_are_refused(monkeypatch: pytest.MonkeyPatch, supported: list[str], backend: str, key: str, message: str) -> None:
    monkeypatch.setattr(common, "host_os", lambda: "linux")
    monkeypatch.setattr(common, "host_arch", lambda: "x86_64")
    cfg = make({"backend": {"supported": supported}})
    with pytest.raises(PytError, match=message) as e:
        common.check_key(cfg, backend, key)
    assert e.value.code == 2


@pytest.mark.parametrize(
    "key",
    [
        "cp314-linux-x86_64\n",  # `$` let a trailing newline through
        "cp3\u0661\u0664-linux-x86_64",  # Arabic-Indic digits: `\d` matched them, int() read 314
        " cp314-linux-x86_64",
        "cp314-linux-x86_64-extra",
        "CP314-linux-x86_64",
    ],
)
def test_parse_key_takes_exactly_the_documented_form(key: str) -> None:
    with pytest.raises(PytError, match="invalid platform key") as e:
        common.parse_key(key)
    assert e.value.code == 2
    assert common.parse_key("cp314-linux-x86_64") == common.Target("cp", 3, 14, "linux", "x86_64")


def test_target_keys_of_the_locked_minor_work_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "host_os", lambda: "linux")
    monkeypatch.setattr(common, "host_arch", lambda: "x86_64")
    cfg = make({"backend": {"supported": ["cpython", "pypy"]}})
    host = {"cpython": common.Target("cp", 3, 14, "linux", "x86_64"), "pypy": common.Target("pp", 3, 11, "linux", "x86_64")}
    monkeypatch.setattr(common, "host_target", lambda c, b: host[b])
    keys = ["host", LINUX, WIN, MAC, WIN]
    assert [t.key for t in common.targets_for(cfg, "cpython", keys)] == [LINUX, WIN, MAC]  # host first, no duplicates
    # A pypy build: its own PyPy plus CPython keys of the locked minor (the .venv installs them)
    assert [t.key for t in common.targets_for(cfg, "pypy", ["pp311-linux-x86_64", WIN, LINUX])] == ["pp311-linux-x86_64", WIN, LINUX]


def _install_recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fail_first: str = "") -> list[tuple[str, list[str], dict[str, str]]]:
    """Record the uv calls of install_deps; `fail_first`: uv's error for the first one."""
    calls: list[tuple[str, list[str], dict[str, str]]] = []

    def fake_uv(env: envs.PyEnv, argv: Any, *, extra_env: Any = None, check: bool = True, **_: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        calls.append((env.key, args, dict(extra_env or {})))
        if fail_first and len(calls) == 1:
            if check:
                raise proc.CommandFailed(["uv", *args], 2)
            return subprocess.CompletedProcess(["uv", *args], 2, "", fail_first)
        return subprocess.CompletedProcess(args, 0, "", "")

    for name in ("cpython", "pypy"):
        env = envs.PyEnv(name, tmp_path / f"venv-{name}", "x", "only-managed")
        env.python.parent.mkdir(parents=True)
        env.python.write_text("", encoding="utf-8")
        monkeypatch.setattr(envs, f"{name}_env", lambda cfg, env=env: env)
    monkeypatch.setattr(envs, "uv", fake_uv)
    def info(python: Any) -> dict[str, object]:
        return {"impl": "pypy", "version": "3.11.15"} if "pypy" in str(python) else {"impl": "cpython", "version": "3.14.7"}

    monkeypatch.setattr(envs, "interpreter_info", info)
    monkeypatch.setattr(common, "host_os", lambda: "linux")
    monkeypatch.setattr(common, "host_arch", lambda: "x86_64")
    return calls


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


@pytest.mark.parametrize(("libc", "floor"), [(("glibc", "2.39"), "x86_64-manylinux_2_28"), (("glibc", "2.17"), None), (("musl", "1.2.5"), None), (("", ""), None)])
def test_host_linux_target_gets_the_platform_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, libc: tuple[str, str], floor: str | None) -> None:
    # Built on Ubuntu 24.04 (glibc 2.39), uv picked cryptography's manylinux_2_34 wheel for the
    # host: the pyz/portable failed on Debian 11 or RHEL 8 with "GLIBC_2.33 not found"
    calls = _install_recorder(tmp_path, monkeypatch)
    monkeypatch.setattr(common.platform, "libc_ver", lambda *a, **k: libc)
    req = _requirements(tmp_path, "rich==15.0.0")
    common.install_deps(make({}), "cpython", common.parse_key(LINUX), tmp_path / "site", req)
    ((key, args, _),) = calls
    assert key == "cpython" and _flag(args, "--python") == str(envs.cpython_env(make({})).python)
    assert _flag(args, "--python-platform") == floor
    # the interpreter's full version: uv read 3.14 as 3.14.0 and dropped a requirement marked
    # python_full_version >= '3.14.1' from the build for this very 3.14.7 interpreter
    assert _flag(args, "--python-version") == ("3.14.7" if floor else None)
    # wheels only at the floor, as for a cross target: an sdist uv built there failed, or shipped a
    # binary built here instead of the locked wheel; without a floor the host may build any sdist
    assert _flag(args, "--only-binary") == (":all:" if floor else None)


def test_host_and_cross_builds_share_one_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_recorder(tmp_path, monkeypatch)
    monkeypatch.setattr(common.platform, "libc_ver", lambda *a, **k: ("glibc", "2.39"))
    req = _requirements(tmp_path, "rich==15.0.0")
    common.install_deps(make({}), "cpython", common.parse_key(LINUX), tmp_path / "a", req)
    monkeypatch.setattr(common, "host_os", lambda: "windows")
    common.install_deps(make({}), "cpython", common.parse_key(LINUX), tmp_path / "b", req)
    host_args, cross_args = calls[0][1], calls[1][1]
    assert _flag(host_args, "--python-platform") == _flag(cross_args, "--python-platform") == common.UV_PLATFORMS[("linux", "x86_64")]
    assert _flag(cross_args, "--only-binary") == ":all:"


# What uv (0.10.12 and 0.12.19) says when a locked package has no wheel for the platform floor
NO_FLOOR_WHEEL = (
    "error: Package `newglibc` can't be installed because it doesn't have a source distribution or wheel for the current platform\n\n"
    "hint: You're on Linux (`manylinux_2_28_x86_64`), but `newglibc` (v1.0) only has wheels for the following platform: `manylinux_2_34_x86_64`"
)


def test_host_floor_falls_back_to_the_host_wheels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = _install_recorder(tmp_path, monkeypatch, fail_first=NO_FLOOR_WHEEL)
    monkeypatch.setattr(common.platform, "libc_ver", lambda *a, **k: ("glibc", "2.39"))
    site = tmp_path / "site"
    common.install_deps(make({}), "cpython", common.parse_key(LINUX), site, _requirements(tmp_path, "rich==15.0.0"))
    assert len(calls) == 2 and "--python-platform" in calls[0][1] and "--python-platform" not in calls[1][1]
    err = capsys.readouterr().err
    assert "needs glibc 2.39 or newer" in err and "only has wheels for the following platform" in err  # uv's reason, then the fallback
    assert site.is_dir()


@pytest.mark.parametrize(
    ("host", "tag", "needs"),
    [(LINUX, "cp314-cp314-manylinux_2_34_x86_64", "glibc 2.34"), (MAC, "cp314-cp314-macosx_14_0_arm64", "macOS 14.0")],
)
def test_host_floor_fallback_names_what_the_wheels_it_took_need(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], host: str, tag: str, needs: str
) -> None:
    # The warning named this machine's glibc (2.39 on Ubuntu 24.04) as what the build needs, while
    # the wheels it took needed 2.34, the floor _pyz.json records: it read as if Ubuntu 22.04 and
    # Debian 12 were out
    calls = _install_recorder(tmp_path, monkeypatch, fail_first=NO_FLOOR_BINARY)
    record = envs.uv  # _install_recorder's

    def install(env: envs.PyEnv, argv: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        done = record(env, argv, **kw)
        if len(calls) == 2:  # the fallback: this machine's wheels
            _wheel(Path(str(argv[argv.index("--target") + 1])), "newglibc", "1.0", tag)
        return done

    monkeypatch.setattr(envs, "uv", install)
    monkeypatch.setattr(common.platform, "libc_ver", lambda *a, **k: ("glibc", "2.39"))
    if host == MAC:
        monkeypatch.delenv("MACOSX_DEPLOYMENT_TARGET", raising=False)
        monkeypatch.setattr(common, "host_os", lambda: "macos")
        monkeypatch.setattr(common, "host_arch", lambda: "aarch64")
        monkeypatch.setattr(common.platform, "mac_ver", lambda *a, **k: ("26.0", ("", "", ""), "arm64"))
    site = common.install_deps(make({}), "cpython", common.parse_key(host), tmp_path / "site", _requirements(tmp_path, "newglibc==1.0"))
    err = capsys.readouterr().err
    assert f"so the build needs {needs} or newer where it runs" in err, err
    assert common.platform_floor(site) == needs.replace("macOS", "macos")  # what _pyz.json records


# What uv (0.10.12 and 0.12.19) says from the floor attempt, which asks for wheels only
# (--only-binary :all:), when a locked package's wheels all need more than the floor: an sdist it
# publishes too is no way out
NO_FLOOR_BINARY = "error: Package `newglibc` can't be installed because it is marked as `--no-build` but has no binary distribution"


def test_host_floor_takes_wheels_only_and_falls_back_on_uvs_no_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # The floor attempt built such a package's sdist (it failed without its toolchain, or shipped a
    # binary built here instead of the locked wheel): now only what publishes no wheel is built there
    calls = _install_recorder(tmp_path, monkeypatch, fail_first=NO_FLOOR_BINARY)
    lock = tmp_path / "uv.lock"
    lock.write_text(SOURCE_ONLY_LOCK, encoding="utf-8")
    monkeypatch.setattr(common, "LOCK", lock)
    monkeypatch.setattr(common.platform, "libc_ver", lambda *a, **k: ("glibc", "2.39"))
    common.install_deps(make({}), "cpython", common.parse_key(LINUX), tmp_path / "site", _requirements(tmp_path, "docopt==0.6.2", "six==1.17.0"))
    (_, at_floor, _), (_, own, _) = calls
    assert _flag(at_floor, "--python-platform") == "x86_64-manylinux_2_28" and _flag(at_floor, "--only-binary") == ":all:"
    assert [at_floor[i + 1] for i, a in enumerate(at_floor) if a == "--no-binary"] == ["docopt", "mylib"]
    assert "--python-platform" not in own and "--only-binary" not in own  # this machine's wheels, or a build
    assert "a dependency has no wheel for x86_64-manylinux_2_28" in capsys.readouterr().err


def test_host_floor_falls_back_for_real_on_uvs_own_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The fallback reads uv's error: pinned with the real uv (the uv-floor job runs the oldest), a
    locked package that only has a manylinux_2_34 wheel, on a host whose glibc loads it."""
    libc, version = common.platform.libc_ver()
    have = common._version_tuple(version) if libc == "glibc" else None
    if common.host_os() != "linux" or common.host_arch() != "x86_64" or have is None or have < (2, 34):
        pytest.skip("needs Linux x86_64 with glibc 2.34 or newer")
    cfg = real({})
    if not envs.tool_env(cfg).python.is_file():
        pytest.skip("needs .venv (./pyt setup)")
    # the wheel of the Python that installs it (this project's .venv): a cp314 one had no
    # interpreter to go to in a project on another minor, with the floor and without it
    major, minor = (int(part) for part in str(envs.interpreter_info(envs.tool_env(cfg).python)["version"]).split(".")[:2])
    tag = f"cp{major}{minor}-cp{major}{minor}-manylinux_2_34_x86_64"
    requirements = _explicit_index_project(tmp_path, monkeypatch, {"newglibc": tag, "ptdemo": "py3-none-any"})
    host = common.Target("cp", major, minor, "linux", "x86_64")
    site = common.install_deps(cfg, "cpython", host, tmp_path / "site", requirements)
    assert common.installed(site) == {("newglibc", "1.0"), ("ptdemo", "1.0")}
    err = capsys.readouterr().err
    assert "a dependency has no wheel for x86_64-manylinux_2_28" in err and "newglibc" in err
    assert "so the build needs glibc 2.34 or newer" in err  # what the wheel needs, not this machine's glibc


def test_host_floor_falls_back_for_a_package_whose_sdist_it_cannot_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A package with a manylinux_2_34 wheel and an sdist (tree-sitter-language-pack, ibm-db): the
    floor attempt built the sdist instead of saying it has no wheel for the floor, and the pyz and
    portable builds failed where it cannot build (or shipped a binary built here instead of the
    locked wheel), while ./pyt run used the wheel. The real uv, offline."""
    libc, version = common.platform.libc_ver()
    have = common._version_tuple(version) if libc == "glibc" else None
    if common.host_os() != "linux" or common.host_arch() != "x86_64" or have is None or have < (2, 34):
        pytest.skip("needs Linux x86_64 with glibc 2.34 or newer")
    requirements = _explicit_index_project(
        tmp_path, monkeypatch, {"newglibc": "py3-none-manylinux_2_34_x86_64", "ptdemo": "py3-none-any"}, failing_sdists=("newglibc",)
    )
    host = common.Target("cp", 3, 14, "linux", "x86_64")
    site = common.install_deps(make({}), "cpython", host, tmp_path / "site", requirements)
    assert common.installed(site) == {("newglibc", "1.0"), ("ptdemo", "1.0")}
    tags = common._wheel_tags(next(site.glob("newglibc-*.dist-info")) / "WHEEL")
    assert tags == ["py3-none-manylinux_2_34_x86_64"]  # the locked wheel, never a build of the sdist
    err = capsys.readouterr().err
    assert "a dependency has no wheel for x86_64-manylinux_2_28" in err and "newglibc" in err
    assert "needs a toolchain" not in err


def test_host_floor_is_kept_when_the_install_fails_for_another_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # Any failure of the floor install (the network, an index, a hash, a failed sdist build) was
    # blamed on a missing floor wheel and retried without the floor: the same error twice with a
    # false explanation between them, and after a transient error a build that silently needed
    # this machine's glibc although the floor's wheels existed
    network = "error: Failed to download `rich==15.0.0`\n  Caused by: Network connectivity is disabled, but the requested data wasn't found in the cache"
    calls = _install_recorder(tmp_path, monkeypatch, fail_first=network)
    monkeypatch.setattr(common.platform, "libc_ver", lambda *a, **k: ("glibc", "2.39"))
    with pytest.raises(proc.CommandFailed) as failed:
        common.install_deps(make({}), "cpython", common.parse_key(LINUX), tmp_path / "site", _requirements(tmp_path, "rich==15.0.0"))
    assert failed.value.code == 2 and len(calls) == 1  # uv's own exit code, no second try
    err = capsys.readouterr().err
    assert "Network connectivity is disabled" in err and "no wheel for" not in err and "needs glibc" not in err


def test_macos_targets_pin_the_deployment_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_recorder(tmp_path, monkeypatch)
    monkeypatch.delenv("MACOSX_DEPLOYMENT_TARGET", raising=False)
    monkeypatch.setattr(common, "host_os", lambda: "macos")
    monkeypatch.setattr(common, "host_arch", lambda: "aarch64")
    req = _requirements(tmp_path, "rich==15.0.0")
    for version, floor in (("26.0", "aarch64-apple-darwin"), ("12.7", None)):
        monkeypatch.setattr(common.platform, "mac_ver", lambda *a, v=version, **k: (v, ("", "", ""), "arm64"))
        common.install_deps(make({}), "cpython", common.parse_key(MAC), tmp_path / "s", req)
        assert _flag(calls[-1][1], "--python-platform") == floor
        assert calls[-1][2] == {"MACOSX_DEPLOYMENT_TARGET": common.MACOS_FLOOR}
    monkeypatch.setattr(common, "host_os", lambda: "linux")
    monkeypatch.setattr(common, "host_arch", lambda: "x86_64")
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "11.0")  # the user's own floor wins
    common.install_deps(make({}), "cpython", common.parse_key(MAC), tmp_path / "s", req)
    assert _flag(calls[-1][1], "--python-platform") == "aarch64-apple-darwin" and calls[-1][2] == {"MACOSX_DEPLOYMENT_TARGET": "11.0"}


SOURCE_ONLY_LOCK = """version = 1
requires-python = ">=3.14"

[[package]]
name = "myapp"
version = "0.1.0"
source = { virtual = "." }

[[package]]
name = "docopt"
version = "0.6.2"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/docopt-0.6.2.tar.gz", hash = "sha256:00", size = 25901 }

[[package]]
name = "mylib"
version = "0.1.0"
source = { editable = "libs/mylib" }

[[package]]
name = "six"
version = "1.17.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/six-1.17.0.tar.gz", hash = "sha256:00", size = 34031 }
wheels = [{ url = "https://files.pythonhosted.org/six-1.17.0-py2.py3-none-any.whl", hash = "sha256:00", size = 11050 }]
"""


# docopt (an sdist only) and six as `uv export --format pylock.toml` writes them (uv 0.12.19)
SOURCE_ONLY_PYLOCK = """lock-version = "1.0"
created-by = "uv"
requires-python = ">=3.14"

[[packages]]
name = "docopt"
version = "0.6.2"
index = "https://pypi.org/simple"
sdist = { url = "https://files.pythonhosted.org/packages/a2/55/8f8cab2afd404cf578136ef2cc5dfb50baa1761b68c9da1fb1e4eed343c9/docopt-0.6.2.tar.gz", size = 25901, hashes = { sha256 = "49b3a825280bd66b3aa83585ef59c4a8c82f2c8a522dbe754a8bc8d08c85c491" } }

[[packages]]
name = "six"
version = "1.17.0"
index = "https://pypi.org/simple"
wheels = [{ url = "https://files.pythonhosted.org/packages/b7/ce/149a00dd41f10bc29e5921b496af8b574d8413afcd5e30dfa0ed46c2cc5e/six-1.17.0-py2.py3-none-any.whl", size = 11050, hashes = { sha256 = "4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274" } }]
"""


@pytest.mark.parametrize(("docopt_tag", "error"), [("py3-none-any", None), ("cp314-cp314-linux_x86_64", "docopt")])
def test_cross_target_builds_a_package_that_publishes_no_wheel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docopt_tag: str, error: str | None) -> None:
    # --only-binary :all: refused docopt (an sdist only, pure Python) for every other OS, so no pyz
    # with a --target could be built; a workspace library (./pyt add ./libs/x) the same
    calls = _install_recorder(tmp_path, monkeypatch)
    lock = tmp_path / "uv.lock"
    lock.write_text(SOURCE_ONLY_LOCK, encoding="utf-8")
    monkeypatch.setattr(common, "LOCK", lock)
    record = envs.uv  # _install_recorder's

    def install(env: envs.PyEnv, argv: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        dest = Path(str(argv[argv.index("--target") + 1]))
        _wheel(dest, "docopt", "0.6.2", docopt_tag, {"docopt.py": b""})
        _wheel(dest, "mylib", "0.1.0")
        _wheel(dest, "six", "1.17.0", files={"six.py": b""})
        return record(env, argv, **kw)

    monkeypatch.setattr(envs, "uv", install)
    req = _requirements(tmp_path, "docopt==0.6.2", "six==1.17.0")
    if error is None:
        common.install_deps(make({}), "cpython", common.parse_key(WIN), tmp_path / "site", req)
    else:
        with pytest.raises(PytError, match=f"{error}.*{WIN}.*pyz-merge") as e:
            common.install_deps(make({}), "cpython", common.parse_key(WIN), tmp_path / "site", req)
        assert e.value.code == 2
    ((_key, args, _),) = calls
    assert _flag(args, "--only-binary") == ":all:"  # every other package: a wheel for that platform
    no_binary = [args[i + 1] for i, a in enumerate(args) if a == "--no-binary"]
    assert no_binary == ["docopt", "mylib"]  # built here: the project itself and six are not


def test_cross_target_builds_a_pure_sdist_for_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv's own flags: `--only-binary :all: --no-binary docopt` builds docopt (pip's rule: the
    later option wins for that package) and still takes wheels for everything else."""
    probe = subprocess.run(
        [proc.find_uv(), "pip", "compile", "--no-deps", "--no-header", "--python-version", "3.14", "-"],
        input="docopt==0.6.2\n", capture_output=True, text=True, env=proc.base_env(), timeout=120, check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"needs PyPI (uv could not resolve docopt): {probe.stderr.strip()[-200:]}")
    cfg = real({})
    if not envs.tool_env(cfg).python.is_file():
        pytest.skip("needs .venv (./pyt setup)")
    lock = tmp_path / "uv.lock"
    lock.write_text(SOURCE_ONLY_LOCK, encoding="utf-8")
    monkeypatch.setattr(common, "LOCK", lock)
    req = tmp_path / "requirements.txt"
    req.write_text("docopt==0.6.2\nsix==1.17.0\n", encoding="utf-8")
    # what install_deps installs, exported by a project on this python.cpython (uv checks the
    # requires-python of the export against the .venv's interpreter, which installs it)
    minor = cfg.python.cpython
    pylock = SOURCE_ONLY_PYLOCK.replace('requires-python = ">=3.14"', f'requires-python = ">={minor}"')
    common.pylock_path(req).write_text(pylock, encoding="utf-8")
    target = common.parse_key(f"cp{minor.replace('.', '')}-{'linux' if IS_WINDOWS else 'windows'}-x86_64")  # a key the lock serves
    site = common.install_deps(cfg, "cpython", target, tmp_path / "site", req)
    assert (site / "docopt.py").is_file() and (site / "six.py").is_file()
    assert common.installed(site) == {("docopt", "0.6.2"), ("six", "1.17.0")}


def test_pypy_build_installs_cpython_keys_with_the_tools_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # It used to raise "CPython target from a PyPy build", but only for the host OS
    calls = _install_recorder(tmp_path, monkeypatch)
    monkeypatch.setattr(common.platform, "libc_ver", lambda *a, **k: ("glibc", "2.39"))
    cfg = make({"backend": {"supported": ["cpython", "pypy"]}})
    req = _requirements(tmp_path, "rich==15.0.0")
    common.install_deps(cfg, "pypy", common.parse_key(LINUX), tmp_path / "a", req)
    common.install_deps(cfg, "pypy", common.parse_key("pp311-linux-x86_64"), tmp_path / "b", req)
    assert calls[0][0] == "cpython" and _flag(calls[0][1], "--python") == str(envs.cpython_env(cfg).python)
    assert calls[1][0] == "pypy" and _flag(calls[1][1], "--python") == str(envs.pypy_env(cfg).python)
    assert _flag(calls[1][1], "--python-version") == "3.11.15"  # the interpreter's full version


def test_install_deps_removes_uv_junk_but_keeps_native_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # uv pip install --target leaves .lock and console scripts whose shebang (or .exe
    # trampoline) holds the build machine's absolute .venv path: none of it may ship
    def fake_uv(env: object, argv: Any, **_: Any) -> subprocess.CompletedProcess[str]:
        dest = Path(str(argv[argv.index("--target") + 1]))
        (dest / ".lock").write_text("")
        (dest / "_virtualenv.pth").write_text("import _virtualenv\n")
        (dest / "_virtualenv.py").write_text("")
        info = dest / "pygments-2.21.0.dist-info"
        info.mkdir()
        (info / "entry_points.txt").write_text("[console_scripts]\npygmentize = pygments.cmdline:main\n\n[gui_scripts]\nPyGUI = pygments.gui:main\n", encoding="utf-8")
        (dest / "pygments").mkdir()
        (dest / "pygments" / "__init__.py").write_text("")
        (dest / "bin").mkdir()
        (dest / "bin" / "pygmentize").write_text("#!/home/someone/proj/.venv/bin/python\n")
        (dest / "bin" / "pygmentize.exe").write_bytes(b"MZ C:\\Users\\someone\\.venv\\Scripts\\python.exe")
        (dest / "bin" / "PyGUI").write_text("#!/home/someone/proj/.venv/bin/python\n")
        (dest / "bin" / "ruff").write_bytes(b"\x7fELF native binary")  # ruff/uv/ty look it up at <target>/bin
        (dest / "Scripts").mkdir()
        (dest / "Scripts" / "pygmentize.exe").write_bytes(b"MZ")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(envs, "uv", fake_uv)
    monkeypatch.setattr(common, "ensure_env", lambda env: env)
    site = common.install_deps(make({}), "cpython", common.parse_key(WIN if not IS_WINDOWS else LINUX), tmp_path / "site", _requirements(tmp_path, "pygments==2.21.0"))
    assert sorted(p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file()) == [
        "bin/ruff",
        "pygments-2.21.0.dist-info/entry_points.txt",
        "pygments/__init__.py",
    ]
    (site / "bin" / "ruff").unlink()
    common.drop_install_junk(site)
    assert not (site / "bin").exists()  # an emptied bin/ goes
    (site / "bin").mkdir()
    (site / "bin" / "__init__.py").write_text("")
    (site / "bin" / "pygmentize").write_text("a package named bin keeps its modules\n")
    common.drop_install_junk(site)
    assert (site / "bin" / "pygmentize").is_file()


def test_install_junk_drops_the_build_machines_path_of_a_local_library(tmp_path: Path) -> None:
    # Since --no-editable a local library is installed for real, and uv writes its source folder
    # on this machine into direct_url.json (file:///home/someone/proj/libs/mylib), plus its own
    # cache files: every pyz and portable build shipped them. A URL requirement keeps its
    # direct_url.json (PEP 610; it names no machine).
    site = tmp_path / "site"
    local = site / "mylib-0.1.0.dist-info"
    remote = site / "six-1.17.0.dist-info"
    built = site / "docopt-0.6.2.dist-info"
    for info in (local, remote, built):
        info.mkdir(parents=True)
        (info / "METADATA").write_text("Metadata-Version: 2.4\n", encoding="utf-8")
    (local / "direct_url.json").write_text('{"url":"file:///home/someone/proj/libs/mylib","dir_info":{}}', encoding="utf-8")
    (local / "uv_cache.json").write_text('{"timestamp":{"secs_since_epoch":1}}', encoding="utf-8")
    (local / "uv_build.json").write_text("{}", encoding="utf-8")
    (built / "uv_build.json").write_text("{}", encoding="utf-8")
    (remote / "direct_url.json").write_text('{"url":"https://example.org/six-1.17.0-py2.py3-none-any.whl","archive_info":{}}', encoding="utf-8")
    for info in (local, remote, built):
        files = sorted(p.name for p in info.iterdir())
        rows = [f"{info.name}/{name},sha256=x,1" for name in files] + [f"{info.name}/RECORD,,"]
        (info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    common.drop_install_junk(site)
    assert sorted(p.name for p in local.iterdir()) == ["METADATA", "RECORD"]
    assert sorted(p.name for p in built.iterdir()) == ["METADATA", "RECORD"]
    assert sorted(p.name for p in remote.iterdir()) == ["METADATA", "RECORD", "direct_url.json"]
    for info in (local, built):  # RECORD lists only the files that are there
        listed = [row.split(",")[0] for row in (info / "RECORD").read_text(encoding="utf-8").splitlines()]
        assert listed == [f"{info.name}/METADATA", f"{info.name}/RECORD"]
    assert "someone" not in "".join(p.read_text(encoding="utf-8") for p in site.rglob("*") if p.is_file())


def test_empty_requirements_install_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envs, "uv", lambda *a, **k: pytest.fail("no install for no dependency"))
    req = tmp_path / "requirements.txt"
    req.write_text("\n", encoding="utf-8")
    dest = tmp_path / "site"
    (dest / "stale").mkdir(parents=True)
    assert common.install_deps(make({}), "cpython", common.parse_key(LINUX), dest, req) == dest
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize("present", [False, True])
def test_host_target_creates_a_missing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    # pyz and portable run the env's python directly: on a fresh clone (or after git clean -fdx)
    # `build --method pyz --no-check` failed with "program not found: .venv/bin/python"
    env = envs.PyEnv("pypy", tmp_path / ".venv-pypy", "pypy@3.11.15", "only-managed")
    if present:
        env.python.parent.mkdir(parents=True)
        env.python.write_text("", encoding="utf-8")
    synced: list[envs.PyEnv] = []

    def fake_sync(e: envs.PyEnv, **_: Any) -> None:
        synced.append(e)
        e.python.parent.mkdir(parents=True, exist_ok=True)
        e.python.write_text("", encoding="utf-8")

    def fake_info(python: str | Path) -> dict[str, object]:
        assert Path(python).is_file(), "interpreter_info ran before the environment existed"
        return {"impl": "pypy", "version": "3.11.15"}

    monkeypatch.setattr(envs, "runtime_env", lambda cfg, backend: env)
    monkeypatch.setattr(envs, "sync", fake_sync)
    monkeypatch.setattr(envs, "interpreter_info", fake_info)
    target = common.host_target(make({"backend": {"supported": ["cpython", "pypy"]}}), "pypy")
    assert synced == ([] if present else [env])
    assert (target.impl, target.major, target.minor) == ("pp", 3, 11)


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"x-1.0.dist-info/WHEEL": "Tag: py3-none-any\n", "x/__init__.py": ""}, False),
        ({"x-1.0.dist-info/WHEEL": "Tag: py2.py3-none-any\n"}, False),
        ({"x-1.0.dist-info/WHEEL": "Tag: py3-none-manylinux2014_x86_64\n", "x/bin/ffmpeg-linux": ""}, True),
        ({"x-1.0.dist-info/WHEEL": "Tag: py3-none-win_amd64\n", "x/tool.exe": ""}, True),
        ({"x-1.0.dist-info/WHEEL": "Tag: cp311-abi3-manylinux_2_17_x86_64\nTag: cp311-abi3-manylinux2014_x86_64\n"}, True),
        ({"x-1.0.dist-info/WHEEL": "Tag: py3-none-macosx_11_0_arm64\n"}, True),
        ({"x-1.0.dist-info/WHEEL": "Tag: py3-none-any\n", "x/_speedups.cpython-314-x86_64-linux-gnu.so": ""}, True),
        ({"x-1.0.dist-info/WHEEL": "Tag: py3-none-any\n", "x/_speedups.pyd": ""}, True),
        ({"a.py": ""}, False),  # no dist-info at all: the file suffixes decide
        ({"a.py": "", "libfoo.so.1": ""}, True),
        ({"a.py": "", "b.dylib": ""}, True),
    ],
)
def test_has_native_reads_wheel_tags_and_binaries(tmp_path: Path, files: dict[str, str], expected: bool) -> None:
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert common.has_native(tmp_path) is expected


def test_skipped_requirements_compares_names_and_versions(tmp_path: Path) -> None:
    site = tmp_path / "site"
    _wheel(site, "jaraco_context", "6.1.2")
    _wheel(site, "pkg", "2.0")
    _wheel(site, "Zope.Interface", "05.4.0")
    req = _requirements(tmp_path, "jaraco-context==6.1.2", "pkg==1.9 ; python_full_version < '3.12'", "pkg==2.0 ; python_full_version >= '3.12'", "zope-interface==5.4.0", "colorama==0.4.6 ; sys_platform == 'win32'")
    assert common.skipped_requirements(req, site) == ["pkg==1.9", "colorama==0.4.6"]


LOCAL_LOCK = """version = 1
requires-python = ">=3.14"

[[package]]
name = "myapp"
version = "0.1.0"
source = { virtual = "." }

[[package]]
name = "anylib"
version = "0.1.0"
source = { editable = "libs/anylib" }

[[package]]
name = "winlib"
version = "0.1.0"
source = { editable = "libs/winlib" }

[[package]]
name = "shared"
version = "2.0"
source = { directory = "../shared" }

[[package]]
name = "six"
version = "1.17.0"
source = { url = "https://example.org/six-1.17.0-py2.py3-none-any.whl" }
"""

# What `uv export --no-editable` writes for local libraries and a URL requirement (uv 0.12.19)
LOCAL_EXPORT = """# This file was autogenerated by uv via the following command:
#    uv export --locked --no-dev --no-editable --no-emit-project --format requirements.txt
./libs/anylib
    # via myapp
./libs/winlib ; sys_platform == 'win32'
    # via myapp
../shared ; python_full_version < '3.12'
    # via myapp
six @ https://example.org/six-1.17.0-py2.py3-none-any.whl ; sys_platform == 'win32' \\
    --hash=sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274
    # via myapp
"""


@pytest.mark.parametrize(
    ("have", "expected"),
    [
        (["anylib"], ["winlib (./libs/winlib)", "shared (../shared)", "six"]),
        (["anylib", "winlib", "shared", "six"], []),  # the target the markers name got them all
    ],
)
def test_skipped_requirements_reads_local_and_url_requirements(tmp_path: Path, have: list[str], expected: list[str]) -> None:
    # --no-editable exports a local library as its path: only name==version lines were read, so
    # a library behind a marker (win32 only) was missing from a pyz that said "pure: ... on any OS"
    (tmp_path / "uv.lock").write_text(LOCAL_LOCK, encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text(LOCAL_EXPORT, encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()
    for name in have:
        _wheel(site, name, "0.1.0")
    assert common.skipped_requirements(req, site, lock=tmp_path / "uv.lock") == expected


def test_skipped_requirements_counts_a_local_library_it_cannot_name(tmp_path: Path) -> None:
    # A path uv.lock does not know (never the case for uv's own export) cannot be told apart:
    # counted as skipped, so the build goes per target instead of claiming to run anywhere
    (tmp_path / "uv.lock").write_text(LOCAL_LOCK, encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text("./libs/other ; sys_platform == 'win32'\n./libs/always\n", encoding="utf-8")
    site = tmp_path / "site"
    _wheel(site, "other", "1.0")
    assert common.skipped_requirements(req, site, lock=tmp_path / "uv.lock") == ["./libs/other"]
    assert common.skipped_requirements(req, site, lock=tmp_path / "missing.lock") == ["./libs/other"]


def test_skipped_requirements_reads_a_library_outside_the_project_as_a_file_url(tmp_path: Path) -> None:
    # uv exports a path outside the project (an absolute one) as a file: URL
    far = tmp_path / "shared libs" / "far"
    (tmp_path / "proj").mkdir()
    lock = tmp_path / "proj" / "uv.lock"
    lock.write_text(f'version = 1\n\n[[package]]\nname = "far"\nversion = "0.1.0"\nsource = {{ directory = {json.dumps(str(far))} }}\n', encoding="utf-8")
    req = tmp_path / "requirements.txt"
    req.write_text(f"{far.as_uri()} ; sys_platform == 'win32'\n    # via myapp\n", encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()
    assert common.skipped_requirements(req, site, lock=lock) == [f"far ({far.as_uri()})"]
    _wheel(site, "far", "0.1.0")
    assert common.skipped_requirements(req, site, lock=lock) == []


def test_pyz_with_a_local_library_behind_a_marker_is_not_pure(sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    lock = sandbox / "uv.lock"
    lock.write_text(LOCAL_LOCK, encoding="utf-8")
    monkeypatch.setattr(common, "LOCK", lock)
    pins = ["./libs/anylib", "./libs/winlib ; sys_platform == 'win32'"]
    _, info, names = _pyz_build(sandbox, monkeypatch, {LINUX: lambda d: _wheel(d, "anylib", "0.1.0")}, pins)
    assert info["pure"] is False and info["targets"] == [LINUX]
    assert f"targets/{LINUX}/lib/anylib/__init__.py" in names
    err = capsys.readouterr().err
    assert "winlib (./libs/winlib)" in err and "any OS" not in err


def test_skipped_requirements_reads_a_real_export_of_a_local_library_behind_a_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The path lines as uv itself writes them, matched against the lock uv wrote."""
    project = _workspace_project(tmp_path / "proj", marked="winlib")
    monkeypatch.setattr(proc, "ROOT", project)
    monkeypatch.setattr(common, "BUILD", tmp_path / "build")
    monkeypatch.setattr(common, "LOCK", project / "uv.lock")
    monkeypatch.setenv("UV_OFFLINE", "1")
    requirements = common.export_requirements(make({}))
    site = tmp_path / "site"
    _wheel(site, "mylib", "0.1.0")
    assert common.skipped_requirements(requirements, site) == ["winlib (./libs/winlib)"]
    _wheel(site, "winlib", "0.1.0")
    assert common.skipped_requirements(requirements, site) == []


def _workspace_project(root: Path, *, marked: str = "", grouped: bool = False) -> Path:
    """A project that depends on a local library the way `./pyt add ./libs/mylib` leaves it
    (a uv workspace member, which uv installs editable), locked offline (static metadata).
    `marked` adds a second library, a dependency on win32 only. `grouped` adds two libraries in
    dependency groups that [tool.uv] default-groups installs by default: `devlib` (dev) and
    `toollib` (lint), as `./pyt add --group lint ...` leaves them."""
    libs = ["mylib", marked] if marked else ["mylib"]
    groups = ["devlib", "toollib"] if grouped else []
    for lib in [*libs, *groups]:
        (root / "libs" / lib / "src" / lib).mkdir(parents=True)
        (root / "libs" / lib / "pyproject.toml").write_text(
            f'[project]\nname = "{lib}"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n\n'
            '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n',
            encoding="utf-8",
        )
        (root / "libs" / lib / "src" / lib / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    deps = '"mylib"' + (f", \"{marked} ; sys_platform == 'win32'\"" if marked else "")
    grouping = '[dependency-groups]\ndev = ["devlib"]\nlint = ["toollib"]\n\n' if grouped else ""
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "wsapp"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = [{deps}]\n\n'
        + grouping
        + ('[tool.uv]\ndefault-groups = ["dev", "lint"]\n\n' if grouped else "")
        + f"[tool.uv.workspace]\nmembers = [{', '.join(repr('libs/' + lib) for lib in [*libs, *groups])}]\n\n"
        + "[tool.uv.sources]\n"
        + "".join(f"{lib} = {{ workspace = true }}\n" for lib in [*libs, *groups]),
        encoding="utf-8",
    )
    env = {k: v for k, v in proc.base_env().items() if not k.startswith(("UV_PROJECT", "UV_PYTHON"))}
    r = subprocess.run([proc.find_uv(), "lock", "--offline", "--quiet"], cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False)
    if r.returncode != 0:
        pytest.skip(f"uv could not lock the scratch workspace offline: {r.stderr.strip()[-300:]}")
    return root


def test_export_ships_path_dependencies_and_refuses_a_stale_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A workspace library was exported as `-e ./libs/mylib`: `uv pip install --target` left only a
    # .pth pointing at this machine's folder, so the pyz/portable app failed anywhere else. A
    # dependency added to pyproject.toml without re-locking was silently left out (--frozen).
    project = _workspace_project(tmp_path / "proj")
    monkeypatch.setattr(proc, "ROOT", project)  # uv finds the project from its working folder
    monkeypatch.setattr(common, "BUILD", tmp_path / "build")
    monkeypatch.setenv("UV_OFFLINE", "1")  # an up-to-date lock is checked without the network
    lines = common.export_requirements(make({})).read_text(encoding="utf-8").splitlines()
    assert "./libs/mylib" in [ln.split(";")[0].strip().replace("\\", "/") for ln in lines]
    assert not [ln for ln in lines if ln.startswith("-e")]
    digest = common.requirements_digest(tmp_path / "build" / "deploy" / "requirements.txt")
    assert digest != hashlib.sha256(b"").hexdigest()[:16]  # the path line is part of the fingerprint
    text = (project / "pyproject.toml").read_text(encoding="utf-8")
    (project / "pyproject.toml").write_text(text.replace('["mylib"]', '["mylib", "six>=1.16"]'), encoding="utf-8")
    with pytest.raises(proc.CommandFailed):
        common.export_requirements(make({}))


def _wheel_file(folder: Path, name: str, version: str, tag: str = "py3-none-any") -> Path:
    """A minimal wheel (a flat index for uv: a folder of wheel files)."""
    import zipfile

    folder.mkdir(parents=True, exist_ok=True)
    info = f"{name}-{version}.dist-info"
    pure = "true" if tag.endswith("-none-any") else "false"
    files = {
        f"{name}/__init__.py": "VALUE = 1\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: {pure}\nTag: {tag}\n",
    }
    whl = folder / f"{name}-{version}-{tag}.whl"
    with zipfile.ZipFile(whl, "w") as z:
        for member, text in files.items():
            z.writestr(member, text)
        z.writestr(f"{info}/RECORD", "".join(f"{m},,\n" for m in [*files, f"{info}/RECORD"]))
    return whl


def _failing_sdist(folder: Path, name: str, version: str) -> Path:
    """An sdist next to a package's wheels whose build always fails, as one does without the
    toolchain it needs (Rust, a C library): its in-tree backend needs nothing to install."""
    import io
    import tarfile

    folder.mkdir(parents=True, exist_ok=True)
    root = f"{name}-{version}"
    files = {
        "PKG-INFO": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        "pyproject.toml": '[build-system]\nrequires = []\nbuild-backend = "failing_backend"\nbackend-path = ["."]\n',
        "failing_backend.py": (
            "def build_wheel(*args, **kwargs):\n"
            "    raise SystemExit('this sdist needs a toolchain this machine lacks')\n\n\n"
            "build_sdist = prepare_metadata_for_build_wheel = build_wheel\n"
        ),
    }
    sdist = folder / f"{root}.tar.gz"
    with tarfile.open(sdist, "w:gz") as tar:
        for member, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{member}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return sdist


def _explicit_index_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wheels: dict[str, str], *, failing_sdists: tuple[str, ...] = ()) -> Path:
    """A project whose dependencies (name: wheel tag, version 1.0) all come from an `explicit =
    true` flat index, locked offline with the real uv; the runner works on it from here (offline),
    and the return value is its export (common.export_requirements). `failing_sdists`: the
    packages that also publish an sdist, one that never builds."""
    if not envs.tool_env(make({})).python.is_file():
        pytest.skip("needs .venv (./pyt setup)")
    project = tmp_path / "proj"
    index = tmp_path / "index"
    for name, tag in wheels.items():
        _wheel_file(index, name, "1.0", tag)
    for name in failing_sdists:
        _failing_sdist(index, name, "1.0")
    project.mkdir()
    (project / "pyproject.toml").write_text(
        f'[project]\nname = "xapp"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = {json.dumps(list(wheels))}\n\n'
        f'[[tool.uv.index]]\nname = "local"\nurl = {json.dumps(index.as_uri())}\nformat = "flat"\nexplicit = true\n\n'
        "[tool.uv.sources]\n" + "".join(f'{name} = {{ index = "local" }}\n' for name in wheels),
        encoding="utf-8",
    )
    env = {k: v for k, v in proc.base_env().items() if not k.startswith(("UV_PROJECT", "UV_PYTHON"))}
    r = subprocess.run([proc.find_uv(), "lock", "--offline", "--quiet"], cwd=project, env=env, capture_output=True, text=True, timeout=120, check=False)
    if r.returncode != 0:
        pytest.skip(f"uv could not lock the scratch project offline: {r.stderr.strip()[-300:]}")
    monkeypatch.setattr(proc, "ROOT", project)
    monkeypatch.setattr(common, "BUILD", tmp_path / "build")
    monkeypatch.setattr(common, "LOCK", project / "uv.lock")
    monkeypatch.setenv("UV_OFFLINE", "1")
    return common.export_requirements(make({}))


def test_pyz_and_portable_install_a_package_of_an_explicit_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `explicit = true` plus a [tool.uv.sources] `{ index = "name" }`, uv's way to take a package
    # from a private index or PyTorch's: the requirements.txt export keeps no index and uv pip reads
    # no sources, so `uv pip install -r requirements.txt` looked for it elsewhere ("No solution
    # found") for every target, although README and the wheel's refusal sent such projects to pyz
    # and portable. The real uv, offline: a flat index of one wheel
    requirements = _explicit_index_project(tmp_path, monkeypatch, {"ptdemo": "py3-none-any"})
    host = common.Target("cp", 3, 14, common.host_os(), common.host_arch())
    cross = common.parse_key(WIN if common.host_os() != "windows" else LINUX)
    for target in (host, cross):
        site = common.install_deps(make({}), "cpython", target, tmp_path / "site" / target.key, requirements)
        assert common.installed(site) == {("ptdemo", "1.0")}, target.key


def test_the_pylock_export_names_local_libraries_from_its_own_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # uv writes a pylock.toml's relative paths from the project folder but reads them, as PEP 751
    # says, from the file's own folder: .build/deploy/pylock.toml named .build/deploy/libs/mylib
    project = _workspace_project(tmp_path / "proj", marked="winlib")
    monkeypatch.setattr(proc, "ROOT", project)
    monkeypatch.setattr(common, "BUILD", tmp_path / "build")
    monkeypatch.setattr(common, "LOCK", project / "uv.lock")
    monkeypatch.setenv("UV_OFFLINE", "1")
    lock = common.pylock_path(common.export_requirements(make({})))
    packages = {p["name"]: p for p in tomllib.loads(lock.read_text(encoding="utf-8"))["packages"]}
    for name in ("mylib", "winlib"):
        folder = lock.parent / packages[name]["directory"]["path"]
        assert folder.resolve() == (project / "libs" / name).resolve() and (folder / "pyproject.toml").is_file()
    assert "sys_platform == 'win32'" in packages["winlib"]["marker"]


def test_the_pylock_rebase_moves_only_what_names_the_project(tmp_path: Path) -> None:
    # A path uv already wrote from the file's folder (the fix upstream is on its way: uv#16299)
    # must not move twice; an absolute one and one that names nothing stay too
    project = tmp_path / "proj"
    for folder in ("libs/a", "libs/b", "wheels"):
        (project / folder).mkdir(parents=True)
    (project / "wheels" / "c-1.0-py3-none-any.whl").write_bytes(b"")
    lock = project / ".build" / "deploy" / "pylock.toml"
    lock.parent.mkdir(parents=True)
    absolute = json.dumps((project / "libs" / "a").as_posix())
    lock.write_text(
        '[[packages]]\nname = "a"\ndirectory = { path = "libs/a" }\n\n'
        '[[packages]]\nname = "b"\ndirectory = { path = "../../libs/b" }\n\n'
        '[[packages]]\nname = "c"\nversion = "1.0"\nwheels = [{ path = "wheels/c-1.0-py3-none-any.whl", hashes = {} }]\n\n'
        f'[[packages]]\nname = "d"\ndirectory = {{ path = {absolute} }}\n\n'
        '[[packages]]\nname = "e"\ndirectory = { path = "libs/gone" }\n',
        encoding="utf-8",
    )
    common._rebase_paths(lock, project)
    packages = {p["name"]: p for p in tomllib.loads(lock.read_text(encoding="utf-8"))["packages"]}
    assert packages["a"]["directory"]["path"] == "../../libs/a"
    assert packages["b"]["directory"]["path"] == "../../libs/b"
    assert packages["c"]["wheels"][0]["path"] == "../../wheels/c-1.0-py3-none-any.whl"
    assert packages["d"]["directory"]["path"] == (project / "libs" / "a").as_posix()
    assert packages["e"]["directory"]["path"] == "libs/gone"


def test_the_builds_export_no_dependency_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `uv export --no-dev` leaves out the dev group only: with [tool.uv] default-groups naming
    # another one (a lint group of tools), the pyz, the portable lib/ and the flet build project
    # got its packages (ruff: a pure pyz became one for this platform only, and an apk build
    # asked for a ruff Android has no wheel of). The real uv, offline, on a real lock
    from runner.methods import flet

    project = _workspace_project(tmp_path / "proj", grouped=True)
    monkeypatch.setattr(proc, "ROOT", project)
    monkeypatch.setattr(common, "BUILD", tmp_path / "build")
    monkeypatch.setattr(common, "LOCK", project / "uv.lock")
    monkeypatch.setenv("UV_OFFLINE", "1")
    lines = common.export_requirements(make({})).read_text(encoding="utf-8").splitlines()
    exported = {ln.split(";")[0].strip().replace("\\", "/") for ln in lines if ln.startswith(".")}
    assert exported == {"./libs/mylib"}
    pins = flet._pinned_requirements(envs.tool_env(make({})))
    assert [pin.split(" @ ")[0] for pin in pins] == ["mylib"]


def test_requirements_digest_ignores_the_header_and_hashes(tmp_path: Path) -> None:
    first = common.requirements_digest(_requirements(tmp_path, "rich==15.0.0", "mdurl==0.1.2"))
    b = tmp_path / "b.txt"
    b.write_text("# via /other/machine/path\nmdurl==0.1.2 \\\n    --hash=sha256:ff\nrich==15.0.0\n", encoding="utf-8")
    assert common.requirements_digest(b) == first
    b.write_text("rich==15.0.1\nmdurl==0.1.2\n", encoding="utf-8")
    assert common.requirements_digest(b) != first


def test_tree_bytes_counts_symlinked_files_once(tmp_path: Path) -> None:
    from runner import cmd_build

    root = tmp_path / "out"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python3.14").write_bytes(b"\0" * 1_048_576)
    try:
        os.symlink("python3.14", root / "bin" / "python3")  # a file symlink: counted once
        os.symlink("bin", root / "bindir")  # a folder symlink: not walked
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert common.dir_size_mb(root) == 1.0
    assert cmd_build._size(root) == "1.0 MB"  # the "done: ... (N MB)" line (201 MB shown for 108 MB)
    assert common.tree_bytes(root / "bin" / "python3.14") == 1_048_576


@pytest.mark.parametrize(("cmd", "expected"), [("py -3.14", "pyw -3.14"), ("py", "pyw"), ("python3", "pythonw"), ("python", "pythonw"), ("pypy3", "pypyw"), ("pypy", "pypyw")])
def test_windowed_twins_exist(cmd: str, expected: str) -> None:
    assert common.windowed(cmd) == expected  # PyPy's Windows zip has pypyw.exe, no pypy3w.exe


# --- cmd_build: arguments ---------------------------------------------------------------------------

ALL_BACKENDS = {"backend": {"supported": ["cpython", "pypy", "mypyc"]}}


@pytest.fixture
def no_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every argument error must come before the checks and the payload (minutes of work)."""

    def must_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the build went past the argument checks")

    monkeypatch.setattr(cmd_build, "run_checks", must_not_run)
    monkeypatch.setattr(cmd_build, "payload", must_not_run)
    # nuitka.check_options refuses a project folder SCons would expand (app$v2): not this one
    monkeypatch.setattr(nuitka, "BUILD", tmp_path / ".build")
    _plain_pyproject(monkeypatch, tmp_path)


def _plain_pyproject(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """wheel.check (cmd_build calls it for --method wheel) reads the project's pyproject.toml and
    refuses a local library, a workspace member or a named index there, which pyz and portable
    support: the tests that build a wheel for another reason failed in such a project. They get
    a pyproject.toml without [tool.uv.sources]."""
    from runner.methods import wheel

    pyproject = tmp_path / "plain" / "pyproject.toml"
    pyproject.parent.mkdir()
    pyproject.write_text('[project]\nname = "myapp"\nversion = "0.1.0"\ndependencies = ["rich"]\n', encoding="utf-8")
    monkeypatch.setattr(wheel, "PYPROJECT", pyproject)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["cpython", "--method", "pyz", "--tagret", "cp314-windows-x86_64"], "unrecognized arguments: --tagret cp314-windows-x86_64"),
        (["cpython", "--method", "portable", "--bogus"], "unrecognized arguments: --bogus"),
        (["cpython", "--method", "wheel", "--noconfirm"], "unrecognized arguments: --noconfirm"),
        (["pypy", "--method", "portable", "-v"], "unrecognized arguments: -v"),
        (["cpython", "--method", "pyz", "--onedri"], "unrecognized arguments: --onedri"),  # no abbreviation of --onedir
        (["cpython", "--method", "pyz", "--onefile"], "--onefile only applies to --method exe or nuitka"),
        (["cpython", "--method", "portable", "--onedir"], "--onedir only applies"),
        (["cpython", "--method", "wheel", "--onefile"], "--onefile only applies"),
        (["cpython", "--method", "exe", "--target", "cp314-linux-x86_64"], "--target only applies to --method pyz"),
        (["cpython", "--method", "nuitka", "--target", "cp314-linux-x86_64"], "--target only applies"),
        (["cpython", "--method", "portable", "--target", "cp314-windows-x86_64"], "--target only applies"),
        (["cpython", "--method", "wheel", "--target", "cp314-windows-x86_64"], "--target only applies"),
        (["cpython", "--method", "exe", "--dry-run"], "--dry-run is a global option: put it before the command"),
        (["cpython", "--method", "nuitka", "--no-render"], "--no-render is a global option"),
        (["--method", "pyz", "--no-check", "--dry-run"], "--dry-run is a global option"),
        (["mypy", "--method", "pyz"], "unknown backend 'mypy': did you mean mypyc?"),
        (["pypi", "--method", "exe"], "unknown backend 'pypi': did you mean pypy?"),
        (["pyz"], "did you mean --method pyz?"),
        (["cpython", "portable"], "did you mean --method portable?"),
        (["pypy", "pyz", "--no-check"], "did you mean --method pyz?"),
        (["--method", "exe", "--no-check", "stray"], "unexpected argument 'stray'"),
        (["cpython", "--method", "pyz", "--target", "bogus"], "invalid platform key"),
        (["cpython", "--method", "pyz", "--target", "cp313-windows-x86_64"], "only resolves CPython 3.14"),
        (["cpython", "--method", "pyz", "--target", "pp311-linux-x86_64"], "pypy build"),
    ],
)
def test_build_rejects_arguments_the_method_would_ignore(no_build: None, monkeypatch: pytest.MonkeyPatch, args: list[str], message: str, dry_run: bool) -> None:
    # They were all silently dropped (pyz, portable and wheel read no extra argument): a typo'd
    # --target built a host-only .pyz, `build --dry-run` REALLY built, `build mypy` built cpython
    monkeypatch.setattr(proc, "DRY_RUN", dry_run)
    with pytest.raises(PytError) as e:
        cmd_build.cmd_build(make(ALL_BACKENDS), args)
    assert message in str(e.value)
    assert e.value.code == 2


def test_build_rejects_a_bad_configured_target_key(no_build: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "DRY_RUN", True)
    with pytest.raises(PytError, match="only resolves CPython 3.14") as e:
        cmd_build.cmd_build(make({"deploy": {"pyz": {"targets": ["host", "cp315-linux-x86_64"]}}}), ["--method", "pyz"])
    assert e.value.code == 2
    # Other methods ignore [deploy.pyz] targets
    assert cmd_build.cmd_build(make({"deploy": {"pyz": {"targets": ["cp315-linux-x86_64"]}}}), ["--method", "portable", "--no-check"]) == 0


def test_the_wheel_never_compiles_the_mypyc_stage_it_does_not_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`build mypyc --method wheel` compiled the mypyc release stage (a full mypyc and C
    compile) and then compiled again in the wheel's own project, which reads src/."""
    seen: list[BuildRequest] = []

    class FakeWheel:
        @staticmethod
        def build(req: BuildRequest) -> Path:
            seen.append(req)
            out = tmp_path / "p.whl"
            out.write_bytes(b"x")
            return out

    monkeypatch.setattr(cmd_build, "payload", lambda cfg, backend: pytest.fail("the payload of a wheel build"))
    monkeypatch.setattr(cmd_build, "check_lock", lambda cfg: None)
    _plain_pyproject(monkeypatch, tmp_path)  # wheel.check: never the sources of the project's own
    monkeypatch.setattr(cmd_build.importlib, "import_module", lambda name: FakeWheel)
    assert cmd_build.cmd_build(make(ALL_BACKENDS), ["mypyc", "--method", "wheel", "--no-check"]) == 0
    assert seen[-1].app_dir == SRC and seen[-1].compiled


def test_build_forwards_extras_to_the_packagers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Guard against over-rejecting: exe/nuitka/flet get the unknown flags (their values too),
    # exe/nuitka get --onefile/--onedir, pyz gets --target
    seen: list[BuildRequest] = []

    class FakeMethod:
        @staticmethod
        def build(req: BuildRequest) -> Path:
            seen.append(req)
            out = tmp_path / "out.bin"
            out.write_bytes(b"x")
            return out

    monkeypatch.setattr(cmd_build, "payload", lambda cfg, backend: tmp_path)
    monkeypatch.setattr(nuitka, "BUILD", tmp_path / ".build")  # nuitka.check_options: never the project's folder
    monkeypatch.setattr(cmd_build.importlib, "import_module", lambda name: FakeMethod)
    cfg = make(ALL_BACKENDS)
    assert cmd_build.cmd_build(cfg, ["cpython", "--method", "exe", "--no-check", "--onedir", "--add-data", "a:b", "--icon", "x.ico"]) == 0
    assert seen[-1].extra == ["--add-data", "a:b", "--icon", "x.ico"] and seen[-1].onefile is False
    assert cmd_build.cmd_build(cfg, ["mypyc", "--method", "nuitka", "--no-check", "--onefile", "--lto=no"]) == 0
    assert seen[-1].extra == ["--lto=no"] and seen[-1].onefile is True and seen[-1].backend == "mypyc"
    assert cmd_build.cmd_build(cfg, ["--method", "exe", "--no-check", "--onedri"]) == 0  # PyInstaller judges it
    assert seen[-1].extra == ["--onedri"] and seen[-1].onefile is None
    monkeypatch.setattr(common, "host_os", lambda: "linux")
    monkeypatch.setattr(common, "host_arch", lambda: "x86_64")
    assert cmd_build.cmd_build(cfg, ["cpython", "--method", "pyz", "--no-check", "--target", WIN]) == 0
    assert seen[-1].targets == [WIN] and seen[-1].extra == []
    assert cmd_build.cmd_build(cfg, ["pypy", "--method", "pyz", "--no-check", "--target", "pp311-linux-x86_64", "--target", WIN]) == 0
    assert seen[-1].targets == ["pp311-linux-x86_64", WIN]
    from runner.methods import flet

    monkeypatch.setattr(flet, "IS_WINDOWS", False)  # no Developer Mode check on a Windows runner
    flet_cfg = make({"app": {"preset": "flet"}})
    assert cmd_build.cmd_build(flet_cfg, ["--method", "flet", "--no-check", "--build-number", "3"]) == 0
    assert seen[-1].extra == ["--build-number", "3"]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("data", "args", "message", "code"),
    [
        # flet build on a script project: it ran the checks and compiled mypyc first; --dry-run said fine
        ({}, ["--method", "flet"], "--method flet is for the flet preset", 2),
        # a value the .cmd launcher cannot hold: the real build deleted the previous folder and
        # installed lib/ first, then stopped with a folder without launchers
        ({"deploy": {"portable": {"runtime": "system", "env": {"GREETING": "h\u00e9llo"}}}}, ["--method", "portable"], "cannot hold this value", 2),
    ],
)
def test_build_refuses_what_the_method_would_refuse_before_any_work(
    no_build: None, monkeypatch: pytest.MonkeyPatch, data: dict[str, Any], args: list[str], message: str, code: int, dry_run: bool
) -> None:
    monkeypatch.setattr(proc, "DRY_RUN", dry_run)
    with pytest.raises(PytError, match=re.escape(message)) as e:
        cmd_build.cmd_build(make(data), args)
    assert e.value.code == code


def test_build_refuses_flet_build_without_developer_mode_before_any_work(no_build: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from runner.methods import flet

    monkeypatch.setattr(flet, "IS_WINDOWS", True)
    monkeypatch.setattr(flet, "_developer_mode", lambda: False)
    monkeypatch.setattr(flet, "host_os", lambda: "windows")
    monkeypatch.setattr(proc, "DRY_RUN", True)
    with pytest.raises(PytError, match="Developer Mode") as e:
        cmd_build.cmd_build(_flet_cfg(), ["--method", "flet"])
    assert e.value.code == 3


def test_build_check_failure_is_exit_code_1(monkeypatch: pytest.MonkeyPatch) -> None:
    # "check failed" exited 2 (usage/config) while ./pyt check exits 1 for the same findings
    monkeypatch.setattr(cmd_build, "run_checks", lambda cfg, backend: False)
    monkeypatch.setattr(cmd_build, "payload", lambda *a: pytest.fail("no payload after a failed check"))
    with pytest.raises(PytError, match="check failed") as e:
        cmd_build.cmd_build(make({}), ["--method", "pyz"])
    assert e.value.code == 1


@pytest.mark.parametrize(("output", "extra"), [("missing", ["-v"]), ("empty folder", [])])
def test_build_without_output_never_reports_done(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], output: str, extra: list[str]) -> None:
    # `./pyt build -v`: PyInstaller read -v as --version, printed it and made nothing, and the
    # build said "ok done: dist/p-cpython-exe (0.0 MB)" with exit 0
    class FakeMethod:
        @staticmethod
        def build(req: BuildRequest) -> Path:
            out = tmp_path / "dist" / "p-cpython-exe"
            if output == "empty folder":
                out.mkdir(parents=True)
            return out

    monkeypatch.setattr(cmd_build, "payload", lambda cfg, backend: tmp_path)
    monkeypatch.setattr(cmd_build.importlib, "import_module", lambda name: FakeMethod)
    with pytest.raises(PytError, match="no output") as e:
        cmd_build.cmd_build(make({}), ["--method", "exe", "--no-check", *extra])
    assert ("before the command" in str(e.value)) is bool(extra)
    assert "done:" not in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [False, True])
def test_flet_method_refuses_before_any_work(no_build: None, monkeypatch: pytest.MonkeyPatch, dry_run: bool) -> None:
    # Both refusals lived in flet.build: `--dry-run build --method flet` printed a plan for a
    # build that must fail, and the real one ran check and the mypyc compile first
    from runner.methods import flet

    monkeypatch.setattr(proc, "DRY_RUN", dry_run)
    monkeypatch.setattr(flet, "IS_WINDOWS", False)
    with pytest.raises(PytError, match="for the flet preset") as e:
        cmd_build.cmd_build(make({}), ["cpython", "--method", "flet"])
    assert e.value.code == 2
    monkeypatch.setattr(flet, "IS_WINDOWS", True)
    monkeypatch.setattr(flet, "host_os", lambda: "windows")
    monkeypatch.setattr(flet, "_developer_mode", lambda: False)
    for cfg in (_flet_cfg(), _flet_cfg(deploy={"flet": {"target": "web"}})):  # a web build too
        with pytest.raises(PytError, match="Developer Mode") as e:
            cmd_build.cmd_build(cfg, ["cpython", "--method", "flet"])
        assert e.value.code == 3
    # A machine that has it on goes on, whatever the target
    monkeypatch.setattr(flet, "_developer_mode", lambda: True)
    for cfg in (_flet_cfg(deploy={"flet": {"target": "web"}}), _flet_cfg()):
        if dry_run:
            assert cmd_build.cmd_build(cfg, ["cpython", "--method", "flet", "--no-check"]) == 0
        else:
            with pytest.raises(AssertionError, match="went past"):
                cmd_build.cmd_build(cfg, ["cpython", "--method", "flet"])


UPX_MISSING = {"deploy": {"upx": {"enabled": True, "path": "tools/upx-missing"}}}


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("method", "cfg_data", "windows", "packs"),
    [
        ("portable", {}, False, True),
        ("nuitka", {}, False, True),
        ("exe", {}, True, True),
        ("exe", {}, False, False),  # PyInstaller packs only on Windows
        ("flet", {"app": {"preset": "flet"}}, False, True),
        ("flet", {"app": {"preset": "flet"}, "deploy": {"flet": {"target": "web"}}}, False, False),  # web ships no binary
        ("pyz", {}, False, False),
        ("wheel", {}, False, False),
    ],
)
def test_upx_is_resolved_before_any_work(
    no_build: None, monkeypatch: pytest.MonkeyPatch, method: str, cfg_data: dict[str, Any], windows: bool, packs: bool, dry_run: bool
) -> None:
    # upx.find ran at the END of the build (after compileall and the runtime copy, after the
    # whole `flet build`): a deploy.upx.path that does not exist, or a failed download, failed
    # the build after the work, and the dry run said nothing about UPX at all
    from runner.methods import flet

    monkeypatch.setattr(proc, "DRY_RUN", dry_run)
    monkeypatch.setattr(upx, "unsupported_reason", lambda: "")
    monkeypatch.setattr(upx, "IS_WINDOWS", windows)
    monkeypatch.setattr(flet, "IS_WINDOWS", False)
    deploy = {**UPX_MISSING["deploy"], **cfg_data.get("deploy", {})}
    cfg = make({**cfg_data, "deploy": deploy})
    if packs:
        with pytest.raises(PytError, match="deploy.upx.path = 'tools/upx-missing' does not exist") as e:
            cmd_build.cmd_build(cfg, ["cpython", "--method", method])
        assert e.value.code == 3
    elif dry_run:
        assert cmd_build.cmd_build(cfg, ["cpython", "--method", method, "--no-check"]) == 0
    else:
        with pytest.raises(AssertionError, match="went past"):
            cmd_build.cmd_build(cfg, ["cpython", "--method", method])


def test_upx_download_happens_before_the_work_and_never_in_a_dry_run(
    no_build: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    downloads: list[Path] = []

    def download(dest: Path) -> Path:
        downloads.append(dest)
        raise PytError("upx: cannot download it (offline)", 3)

    monkeypatch.setattr(upx, "unsupported_reason", lambda: "")
    monkeypatch.setattr(upx, "_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(upx, "_download", download)
    monkeypatch.setattr(cmd_build, "check_lock", lambda cfg: None)  # real uv under the fake PATH below
    monkeypatch.setattr(upx.proc, "base_env", lambda: {"PATH": str(tmp_path / "empty")})
    cfg = make({"deploy": {"upx": {"enabled": True}}})
    # Real build: the download fails before the checks and the payload
    with pytest.raises(PytError, match="offline"):
        cmd_build.cmd_build(cfg, ["cpython", "--method", "portable"])
    assert downloads == [tmp_path / "cache"]
    # Dry run: it names the download instead of doing it
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert cmd_build.cmd_build(cfg, ["cpython", "--method", "portable", "--no-check"]) == 0
    err = capsys.readouterr().err
    assert len(downloads) == 1 and "upx: would download https://github.com/upx/upx/releases/" in err
    # With upx on PATH the dry run names it
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = bindir / ("upx.exe" if IS_WINDOWS else "upx")
    tool.write_bytes(b"")
    tool.chmod(0o755)
    monkeypatch.setattr(upx.proc, "base_env", lambda: {"PATH": str(bindir)})
    assert cmd_build.cmd_build(cfg, ["cpython", "--method", "portable", "--no-check"]) == 0
    # Windows: shutil.which spells the suffix as PATHEXT does (upx.EXE)
    assert os.path.normcase(f"upx: {tool}") in os.path.normcase(capsys.readouterr().err) and len(downloads) == 1


@pytest.mark.parametrize("windows", [False, True])
def test_a_system_runtime_portable_downloads_upx_only_for_a_binary_to_pack(
    no_build: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], windows: bool
) -> None:
    # runtime = "system" bundles no interpreter: a pure-Python app has nothing UPX packs, yet the
    # preflight downloaded UPX for it, and offline the build that used to succeed exited 3
    downloads: list[Path] = []

    def download(dest: Path) -> Path:
        downloads.append(dest)
        raise PytError("upx: cannot download it (offline)", 3)

    monkeypatch.setattr(upx, "unsupported_reason", lambda: "")
    monkeypatch.setattr(upx, "IS_WINDOWS", windows)
    monkeypatch.setattr(upx, "_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(upx, "_download", download)
    monkeypatch.setattr(cmd_build, "check_lock", lambda cfg: None)  # real uv under the fake PATH below
    monkeypatch.setattr(upx.proc, "base_env", lambda: {"PATH": str(tmp_path / "empty")})
    cfg = make({"deploy": {"upx": {"enabled": True}, "portable": {"runtime": "system"}}})
    with pytest.raises(AssertionError, match="went past"):  # on to the build, nothing downloaded
        cmd_build.cmd_build(cfg, ["cpython", "--method", "portable"])
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert cmd_build.cmd_build(cfg, ["cpython", "--method", "portable", "--no-check"]) == 0
    assert "upx: would download https://github.com/upx/upx/releases/" in capsys.readouterr().err
    assert downloads == []
    # The build itself downloads it only when the folder holds a binary to pack
    monkeypatch.setattr(proc, "DRY_RUN", False)
    out = tmp_path / "out"
    (out / "app").mkdir(parents=True)
    (out / "app" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (out / "myapp.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (out / "myapp.sh").chmod(0o755)
    assert upx.pack_tree(cfg, out) == [] and downloads == []
    (out / "lib").mkdir()
    (out / "lib" / ("tool.pyd" if windows else "tool")).write_bytes(b"\x7fELF" + b"\0" * 60)
    (out / "lib" / ("tool.pyd" if windows else "tool")).chmod(0o755)
    with pytest.raises(PytError, match="offline"):
        upx.pack_tree(cfg, out)
    assert downloads == [tmp_path / "cache"]
    # A deploy.upx.path that does not exist is a config error whatever the build holds
    downloads.clear()
    cfg = make({"deploy": {"upx": {"enabled": True, "path": "tools/upx-missing"}, "portable": {"runtime": "system"}}})
    with pytest.raises(PytError, match="does not exist") as e:
        cmd_build.cmd_build(cfg, ["cpython", "--method", "portable"])
    assert e.value.code == 3 and downloads == []


def test_build_dry_run_stops_after_the_argument_checks(no_build: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert cmd_build.cmd_build(make({}), ["--method", "pyz", "--no-check", "--target", WIN]) == 0
    assert "(--dry-run) build cpython -> pyz: would output dist/myapp-cpython-pyz*" in capsys.readouterr().err


@pytest.mark.parametrize(("method", "reason"), [("exe", "PyInstaller does not support PyPy"), ("nuitka", "Nuitka only compiles for CPython"), ("flet", "flet build embeds CPython")])
def test_build_refuses_methods_without_pypy(no_build: None, method: str, reason: str) -> None:
    with pytest.raises(PytError, match=reason) as e:
        cmd_build.cmd_build(make(ALL_BACKENDS), ["pypy", "--method", method])
    assert e.value.code == 2


def test_build_default_method_per_backend(no_build: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(proc, "DRY_RUN", True)
    cfg = make(ALL_BACKENDS)
    for backend, method in (("cpython", "exe"), ("mypyc", "exe"), ("pypy", "portable")):
        assert cmd_build.cmd_build(cfg, [backend, "--no-check"]) == 0
        assert f"build {backend} -> {method}: would output" in capsys.readouterr().err
    with pytest.raises(PytError, match="not in backend.supported"):
        cmd_build.cmd_build(make({}), ["pypy", "--no-check"])


# --- portable: the .sh launcher -------------------------------------------------------------------

POSIX_SHELLS = [s for s in ("sh", "dash", "bash", "zsh", "ksh", "mksh", "yash", "busybox") if shutil.which(s)]
BOOT_ARGV = "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
LAUNCH_ARGS = ["a b", "", "*"]


def _shell_argv(shell: str, script: str) -> list[str]:
    if shell == "busybox":
        return ["busybox", "sh", script]
    return [shell, script] if shell else [script]  # "" = the kernel runs the shebang


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX launchers and symlinks")
@pytest.mark.parametrize("runtime", ["bundled", "system"])
def test_portable_sh_launcher_via_symlinks_cdpath_and_spaces(tmp_path: Path, runtime: str) -> None:
    # HERE was the folder of the SYMLINK (a link in ~/.local/bin: exec .../bin/runtime/bin/python3
    # not found), and an exported CDPATH made `cd` print the folder (HERE got two lines) or pick
    # a decoy folder with the same relative path
    from runner.methods import portable

    top = tmp_path / "dir with space"
    out = top / "opt" / "app"
    out.mkdir(parents=True)
    (out / "boot.py").write_text(BOOT_ARGV, encoding="utf-8")
    python: Path | None = None
    if runtime == "bundled":
        python = out / "runtime" / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.symlink_to(Path(sys.executable).resolve())
    cfg = real({"deploy": {"portable": {"runtime": runtime}}})
    if runtime == "system":  # the launcher looks for cfg.min_python on PATH: this interpreter
        skip_when_older_than(cfg)
    launcher = out / "app.sh"
    launcher.write_text(portable.sh_launcher(cfg, "cpython", out, python), encoding="utf-8", newline="\n")
    launcher.chmod(0o755)
    (top / "real" / "bin").mkdir(parents=True)
    (top / "bin").symlink_to(top / "real" / "bin")  # the links are reached through a symlinked folder
    (top / "real" / "bin" / "rel").symlink_to(Path("..") / ".." / "opt" / "app" / "app.sh")
    (top / "real" / "bin" / "abs").symlink_to(launcher)
    (top / "real" / "bin" / "chain").symlink_to("rel")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "opt" / "app").mkdir(parents=True)  # a CDPATH hit must not win
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    env["CDPATH"] = f"{elsewhere}:."
    paths = [str(launcher), str(top / "bin" / "rel"), str(top / "bin" / "abs"), str(top / "bin" / "chain"), "../dir with space/bin/chain", "../dir with space/opt/app/app.sh"]
    shells = [*POSIX_SHELLS, ""] if runtime == "bundled" else ["sh", ""]
    for shell in shells:
        for path in paths:
            r = subprocess.run([*_shell_argv(shell, path), *LAUNCH_ARGS], cwd=elsewhere, capture_output=True, text=True, env=env, timeout=120, check=False)
            assert r.returncode == 0, (shell, path, r.stdout, r.stderr)
            assert json.loads(r.stdout.strip().splitlines()[-1]) == LAUNCH_ARGS, (shell, path)
    # Through PATH, from the folder of the links
    env["PATH"] = os.pathsep.join([str(top / "bin"), env["PATH"]])
    r = subprocess.run(["chain", "x"], cwd=top / "bin", capture_output=True, text=True, env=env, timeout=120, check=False)
    assert r.returncode == 0 and json.loads(r.stdout.strip().splitlines()[-1]) == ["x"], r.stderr


def test_portable_sh_launcher_is_posix_and_leaks_nothing() -> None:
    from runner.methods import portable

    for python in (None, Path("/x/runtime/bin/python3")):
        text = portable.sh_launcher(make({}), "cpython", Path("/x"), python)
        assert text.isascii() and "\r" not in text
        assert "unset _pt_self _pt_dir _pt_link _pt_n" in text  # niubash runs it in-process
        assert "CDPATH='' cd -P --" in text and 'readlink "$_pt_self"' in text
        assert '"$(' not in text  # niubash keeps the inner quotes of "...$(cmd "$x")..."
        for shell in ("dash", "bash", "mksh", "yash"):
            found = shutil.which(shell)
            # never Windows' System32 bash.exe: the WSL launcher, not a shell
            if found and "system32" not in found.lower() and "windowsapps" not in found.lower():
                # bytes: a text-mode stdin would hand the shell CRLF on Windows
                r = subprocess.run([found, "-n"], input=text.encode("ascii"), capture_output=True, timeout=60, check=False)
                assert r.returncode == 0, (shell, r.stderr)


# --- portable: the .cmd launcher --------------------------------------------------------------------


@pytest.mark.parametrize(("backend", "windowed"), [("cpython", ["pyw -3.14", "pythonw", "pythonw"]), ("pypy", ["pypyw", "pypyw"])])
def test_system_cmd_launcher_gui_is_windowless(tmp_path: Path, backend: str, windowed: list[str]) -> None:
    # runtime = "system" with app.gui ran the console python: a console window stayed open for
    # the whole life of the GUI app (the bundled branch already used start "" pythonw)
    from runner.methods import portable

    supported = {"backend": {"supported": ["cpython", "pypy"]}}
    gui = make({**supported, "app": {"gui": True}, "deploy": {"optimize": 0, "portable": {"runtime": "system"}}})
    lines = portable.cmd_launcher(gui, backend, tmp_path, None).split("\r\n")
    runs = [lines[i + 1] for i, line in enumerate(lines) if line.startswith(":run")]
    assert runs == [f'start "" {w} -s "%~dp0boot.py" %*' for w in windowed]
    probes = [ln for ln in lines if ln.endswith(">nul 2>nul && goto run0")]
    assert probes and probes[0].startswith(("py -3.14 -c ", "pypy3 -c "))  # the probe keeps console names
    console = make({**supported, "deploy": {"optimize": 0, "portable": {"runtime": "system"}}})
    text = portable.cmd_launcher(console, backend, tmp_path, None)
    assert 'start ""' not in text and "pythonw" not in text and "pyw" not in text
    assert "PYTHON_MANAGER_" not in text  # the user's install-manager settings are theirs


# --- portable: copy_runtime prune -----------------------------------------------------------------


def _fake_base(base: Path, files: list[str], links: dict[str, str]) -> None:
    for name in files:
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_bytes(b"x")
    for name, target in links.items():
        (base / name).symlink_to(target)


def _copy_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: Path, version: str, impl: str, *, lib_code: str = "", prune: bool = True) -> set[str]:
    from runner.methods import portable

    monkeypatch.setattr(envs, "interpreter_info", lambda python: {"base_prefix": str(base), "version": version, "impl": impl})
    monkeypatch.setattr(common, "ensure_env", lambda env: env)
    monkeypatch.setattr(portable, "IS_WINDOWS", False)
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    monkeypatch.setattr(common, "SRC", src)  # the app itself imports nothing
    out = tmp_path / f"out-{impl}-{bool(lib_code)}-{prune}"
    (out / "lib" / "gui").mkdir(parents=True)
    (out / "lib" / "gui" / "__init__.py").write_text(lib_code, encoding="utf-8")
    cfg = make({"deploy": {"portable": {"prune": prune}}, "backend": {"supported": ["cpython", "pypy"]}})
    python = portable.copy_runtime(cfg, "pypy" if impl == "pypy" else "cpython", out / "runtime")
    assert python == out / "runtime" / "bin" / "python3"
    dest = out / "runtime"
    return {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file() or p.is_symlink()}


CPYTHON_BASE = [
    "bin/python3.14",
    "bin/python3.14-config",
    "bin/pip3",  # the base's console scripts: pip, idle, and a tool someone installed into it
    "bin/idle3.14",
    "bin/ruff",
    "include/python3.14/Python.h",
    "share/man/man1/python3.1",
    "lib/libpython3.14.so.1.0",
    "lib/pkgconfig/python3.pc",
    "lib/libtcl9.0.so",
    "lib/libtcl9tk9.0.so",
    "lib/tcl9/x.tcl",
    "lib/tcl9.0/init.tcl",
    "lib/tk9.0/tk.tcl",
    "lib/itcl4.3.8/itcl.tcl",
    "lib/thread3.0.6/thread.tcl",
    "lib/python3.14/os.py",
    "lib/python3.14/__pycache__/os.cpython-314.pyc",
    "lib/python3.14/encodings/__init__.py",
    "lib/python3.14/encodings/__pycache__/__init__.cpython-314.pyc",
    "lib/python3.14/test/test_os.py",
    "lib/python3.14/tkinter/__init__.py",
    "lib/python3.14/turtle.py",
    "lib/python3.14/idlelib/idle.py",
    "lib/python3.14/site-packages/README.txt",
    "lib/python3.14/EXTERNALLY-MANAGED",
    "lib/python3.14/unittest/__init__.py",
    "lib/python3.14/unittest/test/test_case.py",
    "lib/python3.14/lib-dynload/_ssl.cpython-314-x86_64-linux-gnu.so",
    "lib/python3.14/lib-dynload/_tkinter.cpython-314-x86_64-linux-gnu.so",
]
CPYTHON_LINKS = {"bin/python3": "python3.14", "bin/python": "python3.14", "lib/libpython3.14.so": "libpython3.14.so.1.0"}
CPYTHON_KEPT = {
    "bin/python3.14",
    "bin/python3.14-config",
    "bin/python3",
    "bin/python",
    "lib/libpython3.14.so.1.0",
    "lib/libpython3.14.so",
    "lib/pkgconfig/python3.pc",
    "lib/python3.14/os.py",
    "lib/python3.14/encodings/__init__.py",
    "lib/python3.14/unittest/__init__.py",
    "lib/python3.14/lib-dynload/_ssl.cpython-314-x86_64-linux-gnu.so",
}
CPYTHON_TK = {
    "lib/libtcl9.0.so",
    "lib/libtcl9tk9.0.so",
    "lib/tcl9/x.tcl",
    "lib/tcl9.0/init.tcl",
    "lib/tk9.0/tk.tcl",
    "lib/itcl4.3.8/itcl.tcl",
    "lib/thread3.0.6/thread.tcl",
    "lib/python3.14/tkinter/__init__.py",
    "lib/python3.14/turtle.py",
    "lib/python3.14/lib-dynload/_tkinter.cpython-314-x86_64-linux-gnu.so",
}
PYPY_BASE = [
    "bin/pypy3.11",
    "bin/libpypy3.11-c.so",
    "bin/mypy",
    "bin/libpypy3.11-c.so.debug",
    "bin/pypy3.11.debug",
    "lib/libsqlite3.so.0",
    "lib/libtcl8.6.so",
    "lib/libtk8.6.so",
    "lib/tcl8.6/init.tcl",
    "lib/tk8.6/tk.tcl",
    "lib/pypy3.11/os.py",
    "lib/pypy3.11/_tkinter/__init__.py",
    "lib/pypy3.11/unittest/__init__.py",
    "lib/pypy3.11/unittest/test/test_case.py",
    "lib/pypy3.11/lib2to3/__init__.py",
    "lib/pypy3.11/lib2to3/tests/data/py2_test_grammar.py",
    "lib/pypy3.11/ctypes/__init__.py",
    "lib/pypy3.11/ctypes/test/test_x.py",
    "lib/pypy3.11/hpy/devel/include/hpy.h",
    "lib/pypy3.11/hpy/__init__.py",
]
PYPY_LINKS = {"bin/pypy3": "pypy3.11", "bin/python3": "pypy3.11"}
PYPY_KEPT = {
    "bin/pypy3.11",
    "bin/libpypy3.11-c.so",
    "bin/pypy3",
    "bin/python3",
    "lib/libsqlite3.so.0",
    "lib/pypy3.11/os.py",
    "lib/pypy3.11/unittest/__init__.py",
    "lib/pypy3.11/lib2to3/__init__.py",
    "lib/pypy3.11/ctypes/__init__.py",
    "lib/pypy3.11/hpy/__init__.py",
}
PYPY_TK = {"lib/libtcl8.6.so", "lib/libtk8.6.so", "lib/tcl8.6/init.tcl", "lib/tk8.6/tk.tcl", "lib/pypy3.11/_tkinter/__init__.py"}


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX runtime layouts (symlinks)")
@pytest.mark.parametrize("impl", ["cpython", "pypy"])
@pytest.mark.parametrize("keep_tk", [False, True])
def test_portable_prune_posix_layouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, impl: str, keep_tk: bool) -> None:
    # Tcl/Tk (9 MB), PyPy's detached .debug symbols (16 MB), nested stdlib test folders and every
    # base __pycache__ were shipped; the tkinter prune looked only at the Windows layout
    base = tmp_path / f"base-{impl}"
    if impl == "cpython":
        _fake_base(base, CPYTHON_BASE, CPYTHON_LINKS)
        kept, tk, version = CPYTHON_KEPT, CPYTHON_TK, "3.14.7"
    else:
        _fake_base(base, PYPY_BASE, PYPY_LINKS)
        kept, tk, version = PYPY_KEPT, PYPY_TK, "3.11.15"
    got = _copy_runtime(tmp_path, monkeypatch, base, version, impl, lib_code="import tkinter\n" if keep_tk else "")
    assert got == (kept | tk if keep_tk else kept)


def _elf(needed: list[str], *, dynamic: bool = True) -> bytes:
    """A minimal 64-bit little-endian ELF executable whose dynamic section lists `needed`."""
    import struct

    strtab = b"\0" + b"".join(n.encode() + b"\0" for n in needed)
    offsets = [1 + sum(len(n) + 1 for n in needed[:i]) for i in range(len(needed))]
    phnum = 2 if dynamic else 1
    dyn_off = 64 + 56 * phnum
    entries = [(1, o) for o in offsets] + [(5, 0), (10, len(strtab)), (0, 0)]  # NEEDED..., STRTAB, STRSZ, NULL
    str_off = dyn_off + 16 * len(entries)
    vaddr = 0x400000
    entries[len(offsets)] = (5, vaddr + str_off)
    total = str_off + len(strtab)
    header = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    header += struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 64, 0, 0, 64, 56, phnum, 0, 0, 0)
    phdrs = struct.pack("<IIQQQQQQ", 1, 5, 0, vaddr, vaddr, total, total, 0x1000)
    if dynamic:
        phdrs += struct.pack("<IIQQQQQQ", 2, 6, dyn_off, vaddr + dyn_off, vaddr + dyn_off, 16 * len(entries), 16 * len(entries), 8)
    body = b"".join(struct.pack("<qQ", tag, value) for tag, value in entries) if dynamic else b""
    out = header + phdrs
    out += bytes(dyn_off - len(out)) + body
    out += bytes(str_off - len(out)) + strtab if dynamic else b""
    return out


def test_elf_needed_reads_the_dynamic_section(tmp_path: Path) -> None:
    from runner.methods import portable

    exe = tmp_path / "python3.14"
    exe.write_bytes(_elf(["libc.so.6", "libpython3.14.so.1.0"]))
    assert portable._elf_needed(exe) == ["libc.so.6", "libpython3.14.so.1.0"]
    exe.write_bytes(_elf(["libm.so.6", "libc.so.6"]))
    assert portable._elf_needed(exe) == ["libm.so.6", "libc.so.6"]
    exe.write_bytes(_elf([], dynamic=False))
    assert portable._elf_needed(exe) == []  # fully static
    exe.write_bytes(b"x")
    assert portable._elf_needed(exe) is None  # not ELF: the caller keeps everything
    exe.write_bytes(_elf(["libc.so.6"])[:70])
    assert portable._elf_needed(exe) is None  # truncated
    assert portable._elf_needed(tmp_path / "missing") is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="a real ELF executable (Linux)")
def test_elf_needed_agrees_with_a_real_executable() -> None:
    from runner.methods import portable

    needed = portable._elf_needed(Path(sys.executable).resolve())
    assert needed is not None and any(n.startswith("libc.so") for n in needed), needed


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX runtime layouts (symlinks)")
@pytest.mark.parametrize(
    ("needed", "host", "kept"),
    [
        (["libm.so.6", "libc.so.6"], "linux", False),  # python-build-standalone: a static interpreter
        (["libpython3.14.so.1.0", "libc.so.6"], "linux", True),  # a shared build needs it
        (None, "linux", True),  # not readable as ELF: keep it (safe side)
        (["libc.so.6"], "macos", True),  # only pruned on Linux
    ],
)
def test_portable_prunes_libpython_only_when_the_interpreter_does_not_need_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, needed: list[str] | None, host: str, kept: bool) -> None:
    # 33 MB of lib/libpython3.14.so.1.0 next to a 31 MB interpreter that already holds all of it
    from runner.methods import portable

    base = tmp_path / "base"
    _fake_base(base, CPYTHON_BASE, CPYTHON_LINKS)
    if needed is not None:
        (base / "bin" / "python3.14").write_bytes(_elf(needed))
    monkeypatch.setattr(portable, "host_os", lambda: host)
    got = _copy_runtime(tmp_path, monkeypatch, base, "3.14.7", "cpython")
    libpython = {"lib/libpython3.14.so.1.0", "lib/libpython3.14.so"}
    assert (libpython <= got) is kept and (not (libpython & got)) is (not kept)
    assert got - libpython == CPYTHON_KEPT - libpython  # nothing else changes


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX runtime layouts (symlinks)")
def test_portable_prune_off_copies_everything_but_the_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "base"
    _fake_base(base, CPYTHON_BASE, CPYTHON_LINKS)
    got = _copy_runtime(tmp_path, monkeypatch, base, "3.14.7", "cpython", prune=False)
    expected = {n for n in [*CPYTHON_BASE, *CPYTHON_LINKS] if "__pycache__" not in n and not n.endswith("EXTERNALLY-MANAGED")}
    assert got == expected


class _ResolvesTo:
    """A path whose resolve() is a Windows path, whatever the host (long_path reads only that)."""

    def __init__(self, text: str) -> None:
        self.text = text

    def resolve(self) -> PureWindowsPath:
        return PureWindowsPath(self.text)


@pytest.mark.parametrize(
    ("resolved", "long", "volume", "short"),
    [
        (r"C:\p\dist\x", r"\\?\C:\p\dist\x", r"\\?\C:", r"C:\p\dist\x"),
        (r"\\server\share\p\dist\x", r"\\?\UNC\server\share\p\dist\x", r"\\?\UNC\server\share", r"\\server\share\p\dist\x"),
        (r"\\?\C:\p", r"\\?\C:\p", r"\\?\C:", r"C:\p"),  # already long
        (r"\\?\UNC\server\share\p", r"\\?\UNC\server\share\p", r"\\?\UNC\server\share", r"\\server\share\p"),
    ],
)
def test_long_paths_keep_a_network_share_valid(monkeypatch: pytest.MonkeyPatch, resolved: str, long: str, volume: str, short: str) -> None:
    # A project on a share, or on a mapped drive (resolve() turns Z:\ into \\server\share\): the
    # bundled portable build copied the interpreter to \\?\\\server\... (WinError 123, blamed on
    # the 260-character limit), and e2e.rmtree built the same name
    from runner.methods import portable

    monkeypatch.setattr(portable, "IS_WINDOWS", True)
    got = portable.long_path(cast(Path, _ResolvesTo(resolved)))
    assert got == long
    assert ntpath.splitdrive(got)[0] == volume  # a real volume, never the bare \\?\
    assert portable.short_path(got) == short
    monkeypatch.setattr(portable, "IS_WINDOWS", False)
    assert portable.long_path(tmp := Path("x")) == str(tmp.resolve())  # POSIX: unchanged


@pytest.mark.skipif(IS_WINDOWS, reason="spells the Windows names on a POSIX file system (a share would be reached)")
def test_the_runtime_prune_reads_the_folders_of_a_network_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # copytree hands the prune callback \\?\UNC\server\share\...: without its UNC prefix it read
    # UNC\server\..., a relative name that matched no folder of the base, and a runtime on a
    # share was copied whole (the base's console scripts, tests, Tk...)
    from runner.methods import portable

    monkeypatch.chdir(tmp_path)
    base = r"\\server\share\py"  # a relative name here: resolve() puts it in tmp_path
    monkeypatch.setattr(envs, "interpreter_info", lambda python: {"base_prefix": base, "version": "3.14.7", "impl": "cpython"})
    monkeypatch.setattr(common, "ensure_env", lambda env: env)
    monkeypatch.setattr(common, "SRC", tmp_path / "src")
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(portable, "long_path", lambda p: portable.LONG_UNC + base[2:] if p == Path(base).resolve() else str(p))
    seen: dict[str, Any] = {}

    def copytree(src: str, dst: str, ignore: Any, symlinks: bool) -> None:
        seen.update(src=src, ignore=ignore)

    monkeypatch.setattr(portable.shutil, "copytree", copytree)
    portable.copy_runtime(make({}), "cpython", tmp_path / "out" / "runtime", lib=tmp_path / "lib")
    assert seen["src"] == r"\\?\UNC\server\share\py"
    assert seen["ignore"](seen["src"] + "/bin", ["python3.14", "ruff"]) == {"ruff"}  # bin/: the interpreter only
    assert seen["ignore"](seen["src"], ["include", "lib"]) == {"include"}


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX runtime layouts (symlinks)")
@pytest.mark.parametrize(
    "dep_code",
    [
        "from tkinter import Variable\n",  # customtkinter
        "import tkinter as tk\nimport tkinter.ttk\n",  # ttkbootstrap, FreeSimpleGUI
        "def f():\n    import turtle\n",  # a lazy import
        "print 'py2 tkinter code'\n",  # mentions tkinter but cannot be parsed: keep Tk (safe side)
    ],
)
def test_portable_keeps_tkinter_when_a_dependency_imports_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dep_code: str) -> None:
    # customtkinter in lib/: the app never imports tkinter itself, the prune removed it and the
    # portable app died at start with ModuleNotFoundError: No module named 'tkinter'
    base = tmp_path / "base"
    _fake_base(base, CPYTHON_BASE, CPYTHON_LINKS)
    got = _copy_runtime(tmp_path, monkeypatch, base, "3.14.7", "cpython", lib_code=dep_code)
    assert CPYTHON_TK <= got


def test_portable_prunes_tkinter_when_nothing_imports_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = tmp_path / "lib"
    (lib / "other").mkdir(parents=True)
    (lib / "other" / "__init__.py").write_text("# works with tkinter too\nNAME = 'turtle'\nfrom typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import tkinter\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(common, "SRC", src)
    assert common.uses_tkinter(lib) is False
    (src / "main.py").write_text("import turtle\n", encoding="utf-8")
    assert common.uses_tkinter(lib) is True  # the app's own import still counts


def test_uses_tkinter_keeps_tk_for_a_source_too_deep_to_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ast.parse raises RecursionError on it: an internal runner error, now the safe side
    src = tmp_path / "src"
    src.mkdir()
    (src / "table.py").write_text("NAME = 'tkinter'\nx = " + " + ".join(["1"] * 100_000) + "\n", encoding="utf-8")
    monkeypatch.setattr(common, "SRC", src)
    assert common.uses_tkinter(tmp_path / "lib") is True


def test_portable_keeps_tkinter_imported_in_a_symlinked_src_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # src/myapp/gui -> ../../shared_gui: Path.rglob does not enter a symlinked folder, so the prune
    # removed Tk although the payload (sync_tree follows the link) imports tkinter
    shared = tmp_path / "shared_gui"
    shared.mkdir()
    (shared / "win.py").write_text("import tkinter\n", encoding="utf-8")
    src = tmp_path / "src"
    (src / "myapp").mkdir(parents=True)
    (src / "myapp" / "__init__.py").write_text("", encoding="utf-8")
    try:
        (src / "myapp" / "gui").symlink_to(shared, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    (src / "myapp" / "loop").symlink_to(src / "myapp", target_is_directory=True)  # a cycle must not hang it
    monkeypatch.setattr(common, "SRC", src)
    assert common.uses_tkinter(tmp_path / "no-lib") is True


# --- portable: precompile, archive, stale files ------------------------------------------------------


@pytest.mark.parametrize("optimize", [0, 1, 2])
def test_portable_precompiles_the_stdlib_at_the_launcher_level(tmp_path: Path, optimize: int) -> None:
    # The bundled stdlib was never compiled on Linux/macOS (only runtime/Lib was looked at) and
    # never at the launchers' -O level: a read-only install recompiled it on every start
    from runner.methods import portable

    version = f"{sys.version_info[0]}.{sys.version_info[1]}"
    out = tmp_path / "out"
    stdlib = out / "runtime" / ("Lib" if IS_WINDOWS else f"lib/{'pypy' if sys.implementation.name == 'pypy' else 'python'}{version}")
    (stdlib / "pkg").mkdir(parents=True)
    (stdlib / "pkg" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (stdlib / "lib2to3" / "tests" / "data").mkdir(parents=True)
    (stdlib / "lib2to3" / "tests" / "data" / "py2.py").write_text("print 'x'\n", encoding="utf-8")
    (out / "app").mkdir()
    (out / "app" / "main.py").write_text("import pkg\n", encoding="utf-8")
    (out / "lib").mkdir()
    cfg = make({"deploy": {"optimize": optimize}})
    calls = portable.compile_calls(cfg, Path(sys.executable), out, version)
    assert len(calls) == 2
    for argv in calls:
        r = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=300, check=False)
        assert r.returncode == 0, r.stdout + r.stderr
    tag = sys.implementation.cache_tag
    level = f".opt-{optimize}" if optimize else ""
    assert sorted(p.name for p in stdlib.rglob("*.pyc")) == [f"__init__.{tag}{level}.pyc"]  # launcher level only, lib2to3 tests skipped
    app_pycs = sorted(p.name for p in (out / "app").rglob("*.pyc"))
    assert app_pycs == sorted({f"main.{tag}.pyc", f"main.{tag}{level}.pyc"})
    for pyc in [*stdlib.rglob("*.pyc"), *(out / "app").rglob("*.pyc")]:
        data = pyc.read_bytes()
        assert int.from_bytes(data[4:8], "little") == 0b11, pyc  # checked-hash (PEP 552)
        assert str(out).encode() not in data, pyc  # -s: this machine's folder is not embedded


@pytest.mark.parametrize("windows", [False, True])
def test_portable_names_the_files_it_could_not_precompile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], windows: bool) -> None:
    # compileall's output was captured and dropped: the warning named no file, blamed paths longer
    # than 260 characters on every OS, and said "The app still works" for a syntax error in app/
    # that a --no-check build let through, while the app did not start
    from runner import ui
    from runner.methods import portable

    monkeypatch.setattr(ui, "QUIET", True)  # compileall's lines are the reason: -q keeps them
    monkeypatch.setattr(portable, "IS_WINDOWS", windows)
    out = tmp_path / "out"
    for name, text in {
        "app/pkg/__init__.py": "",
        "app/pkg/app.py": "def main() -> int\n    return 0\n",
        "lib/dep/__init__.py": "",
        "lib/dep/old.py": "print 'python 2'\n",
    }.items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_text(text, encoding="utf-8")
    portable._precompile(make({}), Path(sys.executable), out, "0.0")
    err = capsys.readouterr().err
    assert "Error compiling" in err and "SyntaxError" in err  # compileall's own lines
    assert "1 file(s) of the app do not compile" in err and "the app fails where it imports them: app/pkg/app.py" in err
    assert "could not precompile 1 file(s) of lib/ or the runtime" in err
    assert "still works" not in err
    assert ("260 characters" in err) == windows  # Windows' own limit
    capsys.readouterr()
    (out / "app" / "pkg" / "app.py").write_text("def main() -> int:\n    return 0\n", encoding="utf-8")
    (out / "lib" / "dep" / "old.py").write_text("X = 1\n", encoding="utf-8")
    portable._precompile(make({}), Path(sys.executable), out, "0.0")
    assert "compile" not in capsys.readouterr().err  # nothing to say


def test_portable_pycs_survive_a_zip_round_trip(tmp_path: Path) -> None:
    # Timestamp .pyc went stale after the Windows zip (2-second DOS times, local time zone)
    import importlib.util

    from runner.methods import portable

    out = tmp_path / "myapp-portable"
    (out / "app" / "pkg").mkdir(parents=True)
    (out / "lib").mkdir()
    (out / "app" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (out / "app" / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    for py in (out / "app").rglob("*.py"):
        os.utime(py, (1_000_000_001, 1_000_000_001))  # an odd second: a zip truncates it
    (argv, *_rest) = portable.compile_calls(make({}), Path(sys.executable), out, "0.0")
    assert subprocess.run([str(a) for a in argv], capture_output=True, timeout=300, check=False).returncode == 0
    archive = portable.make_archive(out, "zip")
    extracted = tmp_path / "x"
    shutil.unpack_archive(archive, extracted)
    for py in (extracted / out.name).rglob("*.py"):
        os.utime(py, (1_000_003_600, 1_000_003_600))  # extracted one time zone away
    for pyc in (extracted / out.name).rglob("*.pyc"):
        source = pyc.parent.parent / (pyc.name.split(".")[0] + ".py")
        assert pyc.read_bytes()[8:16] == importlib.util.source_hash(source.read_bytes()), pyc
    before = {p: p.stat().st_mtime_ns for p in (extracted / out.name).rglob("*.pyc")}
    for flag in ([], ["-O"]):
        subprocess.run([sys.executable, *flag, "-c", "import pkg.mod"], cwd=extracted / out.name / "app", check=True, timeout=120)
    assert {p: p.stat().st_mtime_ns for p in (extracted / out.name).rglob("*.pyc")} == before  # nothing recompiled


def test_portable_zip_keeps_the_sh_launcher_executable(tmp_path: Path) -> None:
    # On Windows a .sh stats as 0o666 and zip entries are MS-DOS ones: unzip extracted
    # myapp.sh without its x bit ("permission denied" on Linux/macOS)
    import zipfile

    from runner.methods import portable

    out = tmp_path / "myapp-cpython-portable"
    (out / "app" / "myapp").mkdir(parents=True)
    (out / "app" / "myapp" / "__init__.py").write_text("", encoding="utf-8")
    (out / "boot.py").write_text("", encoding="utf-8")
    (out / "myapp.cmd").write_text("@echo off\r\n", encoding="ascii", newline="")
    sh = out / "myapp.sh"
    sh.write_text("#!/bin/sh\necho launched\n", encoding="utf-8", newline="\n")
    sh.chmod(0o644)  # what os.stat reports on Windows
    reference = shutil.make_archive(str(tmp_path / "ref"), "zip", root_dir=tmp_path, base_dir=out.name)
    os.utime(out / "boot.py", (1, 1))  # a pre-1980 file (Nix store): shutil's zip raised ValueError
    with pytest.raises(ValueError, match="1980"):
        shutil.make_archive(str(tmp_path / "old"), "zip", root_dir=tmp_path, base_dir=out.name)
    archive = portable.make_archive(out, "zip")
    assert archive == tmp_path / "myapp-cpython-portable.zip"
    with zipfile.ZipFile(archive) as ours, zipfile.ZipFile(reference) as theirs:
        assert sorted(ours.namelist()) == sorted(theirs.namelist())  # the same content as before
        info = ours.getinfo("myapp-cpython-portable/myapp.sh")
        assert info.create_system == 3 and (info.external_attr >> 16) & 0o170777 == 0o100755
        assert ours.read(info.filename) == sh.read_bytes()
    if shutil.which("unzip") and not IS_WINDOWS:
        dest = tmp_path / "unzipped"
        subprocess.run(["unzip", "-q", str(archive), "-d", str(dest)], check=True, timeout=120)
        r = subprocess.run([str(dest / out.name / "myapp.sh")], capture_output=True, text=True, timeout=60, check=False)
        assert r.returncode == 0 and r.stdout == "launched\n"


def _system_portable(sandbox: Path, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> Path:
    from runner.methods import portable

    cfg = make({"app": {"name": "x"}, "deploy": {"portable": {"runtime": "system", "archive": False}}})
    monkeypatch.setattr(common, "host_target", lambda c, b: common.Target("cp", 3, 14, "linux", "x86_64"))
    monkeypatch.setattr(common, "export_requirements", lambda c: _requirements(sandbox, "rich==15.0.0"))

    def install(c: Config, b: str, t: common.Target, dest: Path, req: Path) -> Path:
        if fail:
            raise PytError("simulated failure")
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    monkeypatch.setattr(common, "install_deps", install)
    app = fake_app(sandbox / "payload", "x")
    return portable.build(BuildRequest(cfg, "cpython", "portable", app))


@pytest.mark.parametrize(
    ("installed", "pins", "warning"),
    [
        # tzdata only on win32: the Linux build left it out without a word, and the folder's .cmd
        # started the app on Windows, where zoneinfo then failed
        ({"tzlocal": "5.4.4"}, ["tzlocal==5.4.4", "tzdata==2026.4 ; sys_platform == 'win32'"], "tzdata==2026.4"),
        # backports-tarfile only below 3.12: the launcher accepts a Python 3.11 when PyPy is supported
        ({"jaraco-context": "6.1.2"}, ["jaraco-context==6.1.2", "backports-tarfile==1.2.0 ; python_full_version < '3.12'"], "backports-tarfile==1.2.0"),
        ({"rich": "15.0.0"}, ["rich==15.0.0 ; implementation_name == 'cpython'"], None),  # the same everywhere: no warning
    ],
)
def test_portable_system_warns_about_pins_other_platforms_need(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], installed: dict[str, str], pins: list[str], warning: str | None
) -> None:
    from runner.methods import portable

    cfg = make({"app": {"name": "x"}, "deploy": {"portable": {"runtime": "system", "archive": False}}})
    monkeypatch.setattr(common, "host_target", lambda c, b: common.Target("cp", 3, 14, "linux", "x86_64"))
    monkeypatch.setattr(common, "export_requirements", lambda c: _requirements(sandbox, *pins))

    def install(c: Config, b: str, t: common.Target, dest: Path, req: Path) -> Path:
        for name, version in installed.items():
            _wheel(dest, name, version)
        return dest

    monkeypatch.setattr(common, "install_deps", install)
    portable.build(BuildRequest(cfg, "cpython", "portable", fake_app(sandbox / "payload", "x")))
    err = capsys.readouterr().err
    if warning is None:
        assert "warning:" not in err
    else:
        assert warning in err and "cp314-linux-x86_64" in err and "warning:" in err


def _old_output(dist: Path) -> Path:
    out = dist / "x-cpython-pyz"
    out.mkdir(parents=True)
    (out / "x.pyz").write_bytes(b"old")
    (out / "x.cmd").write_text("old", encoding="utf-8")
    return out


def test_a_previous_output_in_use_is_left_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows: the previous app still runs from dist/ (or a console sits in it). rmtree deleted
    # half of the folder, then PermissionError: a traceback and "internal runner error"
    out = _old_output(tmp_path / "dist")

    def in_use(src: Any, dst: Any) -> None:
        raise PermissionError(13, "The process cannot access the file because it is being used by another process", str(src))

    monkeypatch.setattr(common, "_move", in_use)
    with pytest.raises(PytError, match="in use") as e:
        common.remove_output(out)
    assert e.value.code == 1 and "x-cpython-pyz" in str(e.value)
    assert sorted(p.name for p in out.iterdir()) == ["x.cmd", "x.pyz"]  # nothing deleted
    assert [p.name for p in out.parent.iterdir()] == ["x-cpython-pyz"]  # no scratch folder left


def test_a_previous_output_that_cannot_be_deleted_is_moved_aside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # A file the moved folder still holds (an immutable file, a scanner): the build goes on
    out = _old_output(tmp_path / "dist")
    monkeypatch.setattr(common.shutil, "rmtree", lambda path, *a, **k: None)
    common.remove_output(out)
    assert not out.exists()
    left = [p for p in out.parent.iterdir()]
    assert len(left) == 1 and left[0].name.startswith(".x-cpython-pyz.old-")
    assert "could not delete" in capsys.readouterr().err
    archive = tmp_path / "dist" / "a.zip"
    archive.write_bytes(b"zip")
    common.remove_output(archive)  # a file goes as it is
    assert not archive.exists()
    common.remove_output(archive)  # a missing one is fine


def _previous_portable(dist: Path) -> dict[str, bytes]:
    """A previous runtime = "system" build of app x: the folder and both archives."""
    out = dist / "x-cpython-portable"
    (out / "app").mkdir(parents=True)
    (out / "lib").mkdir()
    (out / "x.sh").write_text("old launcher", encoding="utf-8")
    for suffix in (".zip", ".tar.gz"):
        (dist / f"x-cpython-portable{suffix}").write_text("old build", encoding="utf-8")
    return _tree_bytes(dist)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else b"<dir>" for p in sorted(root.rglob("*"))}


@pytest.mark.parametrize("fails", ["x-cpython-portable", "x-cpython-portable.zip", "x-cpython-portable.tar.gz"])
def test_a_previous_portable_output_in_use_is_left_whole_with_its_archives(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    # The archives were deleted first, then the folder could not be moved (the app still running
    # from it): the build failed with "nothing deleted" and the .tar.gz was gone. The folder and
    # both archives now go together or not at all: those already moved come back.
    dist = sandbox / "dist"
    before = _previous_portable(dist)
    real_move = common._move

    def move(src: Path, dst: Path) -> None:
        if src == dist / fails:  # Windows: a file inside is in use (or the archive is open)
            raise PermissionError(13, "The process cannot access the file because it is being used by another process", str(src))
        real_move(src, dst)

    monkeypatch.setattr(common, "_move", move)
    with pytest.raises(PytError, match="in use") as e:
        _system_portable(sandbox, monkeypatch)
    assert e.value.code == 1 and fails in str(e.value)
    assert _tree_bytes(dist) == before  # every previous file where it was, no scratch folder left


def test_remove_output_names_what_it_could_not_put_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "x-cpython-portable"
    folder.mkdir()
    archive = tmp_path / "x-cpython-portable.tar.gz"
    archive.write_bytes(b"old")
    real_move = common._move

    def move(src: Path, dst: Path) -> None:
        if src == archive or dst == folder:  # the archive is open, and the folder cannot go back
            raise PermissionError(13, "Access is denied", str(src))
        real_move(src, dst)

    monkeypatch.setattr(common, "_move", move)
    with pytest.raises(PytError, match="x-cpython-portable.tar.gz: it is in use or read-only"):
        common.remove_output(folder, archive)
    aside = [p for p in tmp_path.iterdir() if p.name.startswith(".x-cpython-portable.old-")]
    assert len(aside) == 1 and (aside[0] / folder.name).is_dir() and archive.read_bytes() == b"old"
    assert f"could not put {folder.name} back" in capsys.readouterr().err.replace(str(tmp_path) + os.sep, "")


def test_a_stale_lock_leaves_the_previous_portable_output_alone(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `uv export --locked` refused a stale uv.lock only after the previous folder and its archives
    # were deleted: dist/ kept a folder holding only app/
    from runner.methods import portable

    dist = sandbox / "dist"
    before = _previous_portable(dist)
    cfg = make({"app": {"name": "x"}, "deploy": {"portable": {"runtime": "system", "archive": False}}})
    monkeypatch.setattr(common, "host_target", lambda c, b: common.Target("cp", 3, 14, "linux", "x86_64"))

    def stale(c: Config) -> Path:
        raise proc.CommandFailed(["uv", "export", "--locked"], 1)

    monkeypatch.setattr(common, "export_requirements", stale)
    with pytest.raises(proc.CommandFailed):
        portable.build(BuildRequest(cfg, "cpython", "portable", fake_app(sandbox / "payload", "x")))
    assert _tree_bytes(dist) == before


STALE_LOCK = "error: The lockfile at `uv.lock` needs to be updated, but `--locked` was provided. To update the lockfile, run `uv lock`."


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("method", ["exe", "portable", "pyz", "wheel", "nuitka"])
def test_build_refuses_a_stale_lock_before_any_work(no_build: None, monkeypatch: pytest.MonkeyPatch, method: str, dry_run: bool) -> None:
    # Every method stops on a uv.lock that pyproject.toml moved past, but only where it runs uv
    # --locked: with --no-check after the payload, and exe, nuitka and portable after deleting
    # the previous output; --dry-run said "would output"
    asked: list[list[str]] = []

    def uv(env: envs.PyEnv, args: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        asked.append([str(a) for a in args])
        assert kw.get("echo") is False and kw.get("check") is False  # a read-only query, also in a dry run
        return subprocess.CompletedProcess(args, 1, "", STALE_LOCK + "\n")

    monkeypatch.setattr(envs, "uv", uv)
    monkeypatch.setattr(proc, "DRY_RUN", dry_run)
    with pytest.raises(PytError, match="uv.lock does not match pyproject.toml") as e:
        cmd_build.cmd_build(make({}), ["cpython", "--method", method, "--no-check"])
    assert asked == [["lock", "--check"]]
    assert e.value.code == 2 and "./pyt lock" in str(e.value)
    assert "(The lockfile at `uv.lock` needs to be updated" in str(e.value)  # uv's reason, without a second "error:"


def test_a_lock_uv_cannot_check_is_not_called_stale(no_build: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv lock --check without an answer (offline, no interpreter to download: the Windows runner's
    DNS failed) said "uv.lock does not match pyproject.toml" and advised ./pyt lock."""
    offline = "error: Request failed after 3 retries\n  Caused by: dns error"
    monkeypatch.setattr(envs, "uv", lambda env, args, **kw: subprocess.CompletedProcess(args, 2, "", offline + "\n"))
    with pytest.raises(PytError, match="cannot check uv.lock against pyproject.toml: Request failed") as e:
        cmd_build.cmd_build(make({}), ["cpython", "--method", "pyz", "--no-check"])
    assert e.value.code == 3 and "does not match" not in str(e.value) and "./pyt lock" not in str(e.value)


def test_check_lock_reads_the_real_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _workspace_project(tmp_path / "proj")
    monkeypatch.setattr(proc, "ROOT", project)
    monkeypatch.setenv("UV_OFFLINE", "1")
    cmd_build.check_lock(make({}))  # up to date: nothing to say
    text = (project / "pyproject.toml").read_text(encoding="utf-8")
    # A change uv can check offline, whatever its cache holds (a new PyPI dependency could not be
    # resolved there, and that is "cannot check", not a stale lock)
    (project / "pyproject.toml").write_text(text.replace('["mylib"]', "[]"), encoding="utf-8")
    with pytest.raises(PytError, match="uv.lock does not match pyproject.toml"):
        cmd_build.check_lock(make({}))


@pytest.mark.parametrize("fail", [False, True])
def test_portable_build_removes_the_previous_archive(sandbox: Path, monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    # archive = false (or a failed rebuild) left the old dist/<n>...tar.gz next to the new folder:
    # a release script uploading dist/*.tar.gz shipped stale code
    dist = sandbox / "dist"
    dist.mkdir()
    for suffix in (".zip", ".tar.gz"):
        (dist / f"x-cpython-portable{suffix}").write_text("old build", encoding="utf-8")
    if fail:
        with pytest.raises(PytError, match="simulated"):
            _system_portable(sandbox, monkeypatch, fail=True)
    else:
        assert _system_portable(sandbox, monkeypatch) == dist / "x-cpython-portable"
    assert not (dist / "x-cpython-portable.zip").exists() and not (dist / "x-cpython-portable.tar.gz").exists()


@pytest.mark.skipif(IS_WINDOWS, reason="runs the .sh launcher")
@needs_rich
def test_portable_system_folder_real_build_runs(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A REAL runtime = "system" folder (uv export + uv pip install into lib/), started through its
    .sh launcher with the machine's Python: it must import the locked dependencies from lib/."""
    from runner.methods import portable

    cfg = real({"deploy": {"portable": {"runtime": "system"}}})
    skip_when_older_than(cfg)
    app = fake_app(sandbox / "payload")
    (app / "main.py").write_text("import rich, sys\nprint('rich', rich.__file__, *sys.argv[1:])\n", encoding="utf-8")
    try:
        out = portable.build(BuildRequest(cfg, "cpython", "portable", app))
    except proc.CommandFailed as e:
        pytest.skip(f"uv could not export/install the locked dependencies (offline?): {e}")
    assert sorted(p.name for p in out.iterdir()) == ["app", "boot.py", "lib", "myapp.cmd", "myapp.sh"]
    assert not (out / "lib" / "bin").exists() and not (out / "lib" / ".lock").exists()
    assert (sandbox / "dist" / "myapp-cpython-portable.tar.gz").is_file()
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    r = subprocess.run([str(out / "myapp.sh"), "arg"], capture_output=True, text=True, env=env, timeout=120, check=False)
    assert r.returncode == 0, r.stderr
    word, where, arg = r.stdout.split()
    assert word == "rich" and Path(where).is_relative_to(out / "lib") and arg == "arg"


# --- portable: the bundled runtime is started before the build reports success ----------------------


class FakeRun:
    """proc.run stand-in for the runtime smoke: records the call, answers with `stdout`/`code`."""

    def __init__(self, stdout: str, code: int = 0, stderr: str = "") -> None:
        self.stdout, self.code, self.stderr = stdout, code, stderr
        self.calls: list[tuple[list[str], Path | None]] = []

    def __call__(self, argv: Any, *, cwd: Path | None = None, **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(([str(a) for a in argv], cwd))
        return subprocess.CompletedProcess(argv, self.code, self.stdout, self.stderr)


def _runtime_python(out: Path) -> Path:
    python = out / "runtime" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    return python


@pytest.mark.parametrize(("optimize", "flags"), [(0, ["-s", "-B"]), (1, ["-s", "-O", "-B"]), (2, ["-s", "-OO", "-B"])])
def test_portable_runtime_smoke_runs_like_the_launchers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, optimize: int, flags: list[str]) -> None:
    from runner.methods import portable

    out = tmp_path / "out"
    python = _runtime_python(out)
    fake = FakeRun(f"a banner\n{portable.PREFIX_MARK}{out / 'runtime'}\n")
    monkeypatch.setattr(proc, "run", fake)
    portable._smoke_runtime(make({"deploy": {"optimize": optimize}}), python, out)
    argv, cwd = fake.calls[0]
    assert argv[0] == str(python) and argv[1:-2] == flags and argv[-2] == "-c" and cwd == out  # -B: no .pyc written


@pytest.mark.parametrize(
    ("stdout", "code", "message"),
    [
        ("", 3, "does not start (exit code 3)"),
        ("no marker\n", 0, "does not start (exit code 0)"),
        ("{mark}/usr\n", 0, "not on its own"),
        ("{mark}{out}\n", 0, "not on its own"),  # the folder itself, not its runtime/
    ],
)
def test_portable_runtime_smoke_refuses_a_broken_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, code: int, message: str) -> None:
    from runner.methods import portable

    out = tmp_path / "out"
    python = _runtime_python(out)
    monkeypatch.setattr(proc, "run", FakeRun(stdout.format(mark=portable.PREFIX_MARK, out=out), code, "boom"))
    with pytest.raises(PytError, match=re.escape(message)):
        portable._smoke_runtime(make({}), python, out)


@pytest.mark.parametrize("smoke", ["_smoke_runtime", "_smoke_compiled"])
def test_portable_smoke_runs_show_why_they_failed_even_with_q(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], smoke: str) -> None:
    # -q hid the interpreter's traceback: only "does not start" or "cannot import" was left
    from runner import mypyc, ui
    from runner.methods import portable

    out = tmp_path / "out"
    python = _runtime_python(out)
    monkeypatch.setattr(ui, "QUIET", True)
    # make() is the template's own myapp: the compiled modules are named here, never looked for in
    # the src/ of the project the suite runs in (a project made with ./pyt new has no src/myapp/)
    monkeypatch.setattr(mypyc, "compiled_modules", lambda cfg: ["myapp.core"])
    monkeypatch.setattr(proc, "run", FakeRun("", 1, "Traceback (most recent call last):\nImportError: libfoo.so: cannot open shared object file"))
    with pytest.raises(PytError):
        getattr(portable, smoke)(make({"backend": {"active": "mypyc"}}), python, out)
    assert "ImportError: libfoo.so: cannot open shared object file" in capsys.readouterr().err


def test_portable_build_stops_when_the_copied_runtime_has_no_interpreter(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A layout change (bin/python3 gone) used to surface as "program not found" from compileall
    from runner.methods import portable

    cfg = make({"deploy": {"portable": {"archive": False}}})
    monkeypatch.setattr(common, "host_target", lambda c, b: common.Target("cp", 3, 14, "linux", "x86_64"))
    monkeypatch.setattr(common, "export_requirements", lambda c: _requirements(sandbox))

    def install(c: Config, b: str, t: common.Target, dest: Path, req: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    monkeypatch.setattr(common, "install_deps", install)
    monkeypatch.setattr(portable, "copy_runtime", lambda c, b, dest, lib=None: dest / "bin" / "python3")
    monkeypatch.setattr(proc, "run", FakeRun("", 0))
    with pytest.raises(PytError, match="layout"):
        portable.build(BuildRequest(cfg, "cpython", "portable", fake_app(sandbox / "payload")))
    assert not proc.run.calls  # type: ignore[attr-defined]


@pytest.mark.parametrize("bundled", [True, False])
def test_a_bundled_portable_flet_app_carries_its_desktop_client(sandbox: Path, monkeypatch: pytest.MonkeyPatch, bundled: bool) -> None:
    # The flet-desktop wheel has no client: the folder downloaded ~40 MB from GitHub at its
    # first start and could not start offline (exe and nuitka bundle it)
    from runner.methods import nuitka, portable

    name = "flet-linux-debian12-light-amd64.tar.gz"
    archive = sandbox / "cache" / name
    archive.parent.mkdir()
    archive.write_bytes(b"client")
    archive.with_name(name + ".sha256").write_text(f"{'a' * 64} 6", encoding="ascii")  # nuitka._fingerprint
    cfg = make({"deploy": {"portable": {"archive": False, "runtime": "bundled" if bundled else "system"}}})
    monkeypatch.setattr(portable, "IS_WINDOWS", False)
    monkeypatch.setattr(common, "host_target", lambda c, b: common.Target("cp", 3, 14, "linux", "x86_64"))
    monkeypatch.setattr(common, "export_requirements", lambda c: _requirements(sandbox))
    monkeypatch.setattr(portable, "_warn_host_only", lambda *a: None)

    def install(c: Config, b: str, t: common.Target, dest: Path, req: Path) -> Path:
        (dest / "flet_desktop").mkdir(parents=True)
        (dest / "flet_desktop" / "__init__.py").write_text("", encoding="utf-8")
        return dest

    def runtime(c: Config, b: str, dest: Path, lib: Path | None = None) -> Path:
        (dest / "bin").mkdir(parents=True)
        (dest / "bin" / "python3").write_bytes(b"")
        return dest / "bin" / "python3"

    monkeypatch.setattr(common, "install_deps", install)
    monkeypatch.setattr(portable, "copy_runtime", runtime)
    monkeypatch.setattr(portable, "_smoke_runtime", lambda *a: None)
    monkeypatch.setattr(nuitka, "_flet_client_archive", lambda c: archive)
    monkeypatch.setattr(proc, "run", FakeRun("", 0))
    out = portable.build(BuildRequest(cfg, "cpython", "portable", fake_app(sandbox / "payload")))
    bundled_client = out / "lib" / "flet_desktop" / "app" / name
    launcher = (out / "myapp.sh").read_text(encoding="utf-8")
    if bundled:
        assert bundled_client.read_bytes() == b"client"  # where flet_desktop looks for one
        assert bundled_client.with_name(name + ".sha256").is_file()  # read instead of hashing the client
        # The app looks for exactly that archive: not by the user's glibc or current folder
        assert "export FLET_LINUX_DISTRO=debian12" in launcher and "export FLET_DESKTOP_FLAVOR=light" in launcher
        assert "export FLET_APP_ID=myapp" in launcher  # the taskbar groups the window as the app
    else:  # runtime = "system": a Python of the user's machine, like a pyz (documented)
        assert not bundled_client.exists() and "FLET_" not in launcher


def test_the_flet_client_env_pins_what_the_archive_was_named_for() -> None:
    from runner.methods import nuitka

    assert nuitka.flet_client_env("flet-linux-ubuntu24.04-light-amd64.tar.gz") == {"FLET_LINUX_DISTRO": "ubuntu24.04", "FLET_DESKTOP_FLAVOR": "light"}
    assert nuitka.flet_client_env("flet-linux-debian10-arm64.tar.gz") == {"FLET_LINUX_DISTRO": "debian10", "FLET_DESKTOP_FLAVOR": "full"}
    assert nuitka.flet_client_env("flet-linux-ubuntu22.04-light-arm_7.tar.gz")["FLET_LINUX_DISTRO"] == "ubuntu22.04"
    assert nuitka.flet_client_env("flet-windows.zip") == nuitka.flet_client_env("flet-macos.tar.gz") == {}


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX interpreter layout (symlinks)")
def test_portable_runtime_smoke_with_real_interpreters(tmp_path: Path) -> None:
    from runner.methods import portable

    real = Path(sys.executable).resolve()
    base = Path(sys.base_prefix)
    if not (base / "bin" / real.name).is_file():
        pytest.skip(f"{real} is not in {base}/bin")
    # A runtime/ that IS the interpreter's own prefix (a symlink here: no stdlib copy): accepted
    good = tmp_path / "good"
    good.mkdir()
    (good / "runtime").symlink_to(base, target_is_directory=True)
    portable._smoke_runtime(make({}), good / "runtime" / "bin" / real.name, good)
    # An interpreter that only LINKS to one outside the folder runs on that one's prefix: the
    # folder would only work on this machine
    bad = tmp_path / "bad"
    (bad / "runtime" / "bin").mkdir(parents=True)
    (bad / "runtime" / "bin" / "python3").symlink_to(real)
    with pytest.raises(PytError, match="only work on this machine"):
        portable._smoke_runtime(make({}), bad / "runtime" / "bin" / "python3", bad)
    # One that does not start at all
    broken = tmp_path / "broken"
    (broken / "runtime" / "bin").mkdir(parents=True)
    script = broken / "runtime" / "bin" / "python3"
    script.write_text("#!/bin/sh\necho 'cannot start' >&2\nexit 7\n", encoding="utf-8", newline="\n")
    script.chmod(0o755)
    with pytest.raises(PytError, match=r"exit code 7"):
        portable._smoke_runtime(make({}), script, broken)


# --- flet build ---------------------------------------------------------------------------------------

FLET_LOCK = """version = 1

[[package]]
name = "fletdemo"
version = "0.1.0"
source = { virtual = "." }

[package.metadata]
requires-dist = [
    { name = "flet", specifier = "==1.0.1" },
    { name = "numpy", specifier = ">=2.0,<3" },
]

[[package]]
name = "flet"
version = "1.0.1"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/packages/aa/flet-1.0.1-py3-none-any.whl" }]

[[package]]
name = "msgpack"
version = "1.2.2"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/packages/bb/msgpack-1.2.2.tar.gz" }
wheels = [
    { url = "https://files.pythonhosted.org/packages/cc/msgpack-1.2.2-cp314-cp314-manylinux_2_28_x86_64.whl" },
    { url = "https://files.pythonhosted.org/packages/dd/msgpack-1.2.2-cp314-cp314-win_amd64.whl" },
]

[[package]]
name = "numpy"
version = "2.3.1"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/packages/ee/numpy-2.3.1-cp314-cp314-win_amd64.whl" }]

[[package]]
name = "docopt"
version = "0.6.2"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/packages/ff/docopt-0.6.2.tar.gz" }

[[package]]
name = "mylib"
version = "0.1.0"
source = { editable = "libs/mylib" }
"""

FLET_PYPROJECT ='[project]\nname = "fletdemo"\nversion = "0.1.0"\n\n[tool.flet]\norg = "com.example"\n\n[tool.flet.app]\npath = "src"\nmodule = "main"\n'


def _flet_build(sandbox: Path, monkeypatch: pytest.MonkeyPatch, *, backend: str = "cpython", target: str = "host", produce: bool = True, payload_ext: bool = False, pins: list[str] | None = None, **deploy: Any) -> tuple[Path, Recorder]:
    from runner.methods import flet

    cfg = _flet_cfg(deploy={"flet": {"target": target, "extra_args": ["--build-number", "7"], **deploy}})
    app = sandbox / f"payload-{backend}-{target}-{payload_ext}"
    fake_app(app, cfg.pkg)
    if payload_ext:
        (app / cfg.pkg / "core" / f"fractal{EXT}").write_bytes(b"\x7fELF")
    pyproject = sandbox / "pyproject.toml"
    pyproject.write_text(FLET_PYPROJECT, encoding="utf-8")
    monkeypatch.setattr(flet, "PYPROJECT", pyproject)
    monkeypatch.setattr(flet, "_pinned_requirements", lambda env: list(pins or ["flet==1.0.1", "msgpack==1.1.0"]))
    (sandbox / "uv.lock").write_text(FLET_LOCK, encoding="utf-8")
    monkeypatch.setattr(common, "LOCK", sandbox / "uv.lock")
    monkeypatch.setattr(flet, "host_os", lambda: "linux")
    # flet build on Windows needs Developer Mode, off by default there: not these tests' subject
    monkeypatch.setattr(flet, "_developer_mode", lambda: True)

    def effect(args: list[str], cwd: Path | None) -> None:
        if produce:
            Path(args[args.index("--output") + 1]).mkdir(parents=True, exist_ok=True)

    rec = Recorder(effect)
    monkeypatch.setattr(envs, "uv_run", rec)
    out = flet.build(BuildRequest(cfg, backend, "flet", app, extra=["--verbose"]))
    return out, rec


FLET_LOCAL_LOCK = """version = 1

[[package]]
name = "mylib"
version = "0.1.0"
source = { editable = "libs/mylib" }

[[package]]
name = "extlib"
version = "0.2.0"
source = { directory = "../extlib" }
"""


def test_flet_build_names_local_libraries_by_absolute_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `uv export` without --no-editable wrote "-e ./libs/mylib ; <markers>" into the build
    # project's dependencies: no PEP 508 requirement (pip refused it), and relative to the
    # project where the build stage is two folders deeper
    from runner.methods import flet

    requirements = pytest.importorskip("packaging.requirements")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "uv.lock").write_text(FLET_LOCAL_LOCK, encoding="utf-8")
    monkeypatch.setattr(common, "LOCK", project / "uv.lock")
    marker = "python_full_version < '3.15' and implementation_name == 'cpython'"
    exported = f"./libs/mylib ; {marker}\n../extlib ; {marker}\nanyio==4.15.1 ; {marker}\nrich @ https://example.com/rich.whl ; {marker}\n"
    calls: list[list[str]] = []

    def fake_uv(env: envs.PyEnv, args: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in args])
        return subprocess.CompletedProcess(args, 0, exported, "")

    monkeypatch.setattr(envs, "uv", fake_uv)
    pins = flet._pinned_requirements(envs.tool_env(make({})))
    assert "--no-editable" in calls[0]
    assert pins == [
        f"mylib @ {(project / 'libs' / 'mylib').resolve().as_uri()} ; {marker}",
        f"extlib @ {(tmp_path / 'extlib').resolve().as_uri()} ; {marker}",
        f"anyio==4.15.1 ; {marker}",
        f"rich @ https://example.com/rich.whl ; {marker}",
    ]
    for pin in pins:
        requirements.Requirement(pin)  # every line is PEP 508
    monkeypatch.setattr(envs, "uv", lambda env, args, **kw: subprocess.CompletedProcess(args, 0, "./libs/other\n", ""))
    with pytest.raises(PytError, match="cannot name the local requirement './libs/other'"):
        flet._pinned_requirements(envs.tool_env(make({})))


def test_flet_build_argv_stage_and_pyproject(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tomllib

    out, rec = _flet_build(sandbox, monkeypatch, exclude=["assets/big"])
    work = sandbox / "build" / "flet-build" / "cpython"
    assert out == sandbox / "dist" / "fletdemo-cpython-flet-linux"
    argv, cwd, _ = rec.calls[-1]
    assert argv == ["flet", "build", "linux", str(work), "--yes", "--output", str(out), "--cleanup-app", "--cleanup-packages", "--exclude", "assets/big", "--build-number", "7", "--verbose"]
    assert cwd == work  # envs.uv_run then adds --project <ROOT> (the stage has its own pyproject.toml)
    assert (work / "src" / "main.py").is_file() and (work / "src" / "fletdemo" / "app.py").is_file()
    data = tomllib.loads((work / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["requires-python"] == "==3.14.*"
    assert data["project"]["dependencies"] == ["flet==1.0.1", "msgpack==1.1.0"]
    assert data["tool"]["flet"] == {"org": "com.example", "app": {"path": "src", "module": "main"}}


def test_flet_build_cleanup_false_turns_flets_own_cleanup_off(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # flet_cli 1.0.1 cleans the packages by default (cleanup.packages defaults to true, and
    # --cleanup-packages has no negative form): cleanup = false only dropped --cleanup-app
    import tomllib

    from runner.methods import flet

    _, rec = _flet_build(sandbox, monkeypatch, cleanup=False)
    argv = rec.calls[-1][0]
    assert "--cleanup-app" not in argv and "--cleanup-packages" not in argv
    work = sandbox / "build" / "flet-build" / "cpython"
    tool = tomllib.loads((work / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["flet"]
    assert tool["cleanup"] == {"app": False, "packages": False} and tool["org"] == "com.example"
    # What the project's own [tool.flet.cleanup] says stays (flet reads it after the flags)
    off = make({"deploy": {"flet": {"cleanup": False}}})
    data = tomllib.loads('[project]\nname = "a"\nversion = "1"\n\n[tool.flet.cleanup]\npackages = true\n')
    assert tomllib.loads(flet.build_pyproject(off, data, []))["tool"]["flet"]["cleanup"] == {"packages": True, "app": False}
    with pytest.raises(PytError, match=r"\[tool.flet.cleanup\]"):
        flet.build_pyproject(off, tomllib.loads('[project]\nname = "a"\nversion = "1"\n\n[tool.flet]\ncleanup = true\n'), [])
    # cleanup = true (the default) passes the flags and adds no table
    assert "cleanup" not in tomllib.loads(flet.build_pyproject(make({}), data | {"tool": {}}, []))["tool"]["flet"]


def test_flet_build_keeps_the_project_description() -> None:
    # flet build takes the app's description from [project] description: it was dropped, so
    # the built app described itself as ""
    import tomllib

    from runner.methods import flet

    text = 'Desktop app: "quoted", \\\\ back, caf\u00e9 \U0001f600 and DEL \u007f'
    data = {"project": {"name": "a", "version": "1", "description": text}}
    assert tomllib.loads(flet.build_pyproject(make({}), data, []))["project"]["description"] == text
    assert "description" not in tomllib.loads(flet.build_pyproject(make({}), {"project": {"name": "a", "version": "1"}}, []))["project"]


def test_flet_build_pins_the_python_minor() -> None:
    # flet_cli picks the HIGHEST stable Python of its manifest that matches requires-python:
    # ">=3.13" bundled 3.14 and the cp313 mypyc extensions were silently not loaded
    from runner.methods import flet

    data = {"project": {"name": "a", "version": "1"}}
    for minor in ("3.12", "3.13", "3.14"):
        text = flet.build_pyproject(make({"python": {"cpython": minor}}), data, [])
        assert f'requires-python = "=={minor}.*"' in text


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ('[tool.flet]\norg = "x"\n', {"org": "x", "app": {"path": "src"}}),  # no [tool.flet.app]: flet found no main.py
        ('[tool.flet]\norg = "x"\n[tool.flet.app]\npath = "app"\n', {"org": "x", "app": {"path": "src"}}),
        ("", {"app": {"path": "src"}}),
        ('[tool.flet.app]\nmodule = "ui"\n', {"app": {"module": "ui", "path": "src"}}),
    ],
)
def test_flet_build_pyproject_points_at_the_staged_app(extra: str, expected: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    import tomllib

    from runner.methods import flet

    data = tomllib.loads('[project]\nname = "a"\nversion = "1"\n\n' + extra)
    before = json.dumps(data, sort_keys=True)
    tool = tomllib.loads(flet.build_pyproject(make({}), data, []))["tool"]["flet"]
    assert tool == expected
    assert json.dumps(data, sort_keys=True) == before  # the caller's data is not mutated
    assert ("is ignored" in capsys.readouterr().err) is ('path = "app"' in extra)


@pytest.mark.parametrize("target", ["host", "windows", "apk", "aab", "web"])
def test_flet_build_needs_developer_mode_on_windows(sandbox: Path, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    # Every target: flet build turns Flutter's Windows desktop on there and its template holds a
    # windows/ folder, so an apk or web build stopped late in Flutter's plugin symlinks, after the
    # checks, the payload and a first Flutter download
    from runner.methods import flet

    monkeypatch.setattr(flet, "IS_WINDOWS", True)
    monkeypatch.setattr(flet, "_developer_mode", lambda: False)
    monkeypatch.setattr(flet, "host_os", lambda: "windows")
    monkeypatch.setattr(envs, "uv_run", lambda *a, **k: pytest.fail("flet build must not start"))
    cfg = _flet_cfg(deploy={"flet": {"target": target}})
    with pytest.raises(PytError, match="Developer Mode") as e:
        flet.check_options(cfg)
    assert e.value.code == 3
    with pytest.raises(PytError, match="Developer Mode"):
        flet.build(BuildRequest(cfg, "cpython", "flet", fake_app(sandbox / "p", "fletdemo")))
    with pytest.raises(PytError, match="flet preset"):
        flet.build(BuildRequest(make({}), "cpython", "flet", sandbox / "p"))
    monkeypatch.setattr(flet, "_developer_mode", lambda: True)
    flet.check_options(cfg)  # with it, nothing to refuse


def test_flet_build_mobile_and_web_ship_the_py_code(sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from runner.methods import flet

    src = sandbox / "src"
    fake_app(src, "fletdemo")
    monkeypatch.setattr(flet, "SRC", src)
    # A desktop build leaves its extension in the persistent stage...
    _flet_build(sandbox, monkeypatch, backend="mypyc", payload_ext=True)
    stage = sandbox / "build" / "flet-build" / "mypyc" / "src"
    assert (stage / "fletdemo" / "core" / f"fractal{EXT}").is_file()
    # ...which must not reach a web build: it packages the .py of src/ (interpreted)
    out, _ = _flet_build(sandbox, monkeypatch, backend="mypyc", target="web", payload_ext=True)
    assert out.name == "fletdemo-mypyc-flet-web"
    assert not [p for p in stage.rglob("*") if p.name.endswith((".so", ".pyd"))]
    assert "web: compiled extensions are not supported" in capsys.readouterr().err


def test_flet_build_leaves_mobile_binaries_to_flets_index(sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # flet build installs a mobile or web target's binary packages from Flet's own index
    # (pypi.flet.dev, --only-binary :all:), which held msgpack 1.1.0 and 1.1.2 only: the exact
    # msgpack==1.2.2 of a new flet project had no solution and an apk or ipa build failed
    import tomllib

    requirements = pytest.importorskip("packaging.requirements")
    marker = "python_full_version < '3.15' and implementation_name == 'cpython'"
    pins = [f"flet==1.0.1 ; {marker}", f"msgpack==1.2.2 ; {marker}", "numpy==2.3.1", "docopt==0.6.2", f"mylib @ file:///src/libs/mylib ; {marker}"]
    stage = sandbox / "build" / "flet-build" / "cpython" / "pyproject.toml"
    for target in ("apk", "ipa", "web"):
        _flet_build(sandbox, monkeypatch, target=target, pins=pins)
        deps = tomllib.loads(stage.read_text(encoding="utf-8"))["project"]["dependencies"]
        # the pure flet keeps its pin, a binary one keeps only the project's own bounds and its
        # markers, a direct reference stays
        assert deps == [f"flet==1.0.1 ; {marker}", f"msgpack ; {marker}", "numpy>=2.0,<3", "docopt", f"mylib @ file:///src/libs/mylib ; {marker}"]
        for dep in deps:
            requirements.Requirement(dep)
        err = " ".join(capsys.readouterr().err.split())
        assert "msgpack==1.2.2 -> msgpack, numpy==2.3.1 -> numpy>=2.0,<3, docopt==0.6.2 -> docopt" in err, err
        # where pip looks: the web's binaries come from its Pyodide release first
        assert ("the packages of its Pyodide release and Flet's own index" in err) == (target == "web") and "pypi.flet.dev" in err
        # a lower bound the project keeps may be newer than those hold (./pyt add writes the newest)
        assert "a lower bound may be newer than it holds (numpy>=2.0,<3:" in err and "lower it in pyproject.toml, then ./pyt lock" in err
    # a desktop build installs from PyPI: every pin stays
    _flet_build(sandbox, monkeypatch, target="linux", pins=pins)
    assert tomllib.loads(stage.read_text(encoding="utf-8"))["project"]["dependencies"] == pins
    assert "uv.lock's versions" not in capsys.readouterr().err


def test_flet_relaxed_message_hints_at_a_lower_bound_only_when_one_is_kept() -> None:
    from runner.methods import flet

    assert "lower it" not in flet.relaxed_message("apk", [("msgpack==1.2.2", "msgpack"), ("numpy==2.3.1", "numpy<3,!=2.1.0")])
    for kept in ("numpy>=2.5.3", "numpy~=2.5", "numpy==2.5.3", "numpy>2"):
        assert f"({kept}: `./pyt add` writes" in flet.relaxed_message("apk", [("numpy==2.5.3", kept)]), kept


FLET_DESKTOP_LOCK = """version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "app"
version = "0.1.0"
source = { virtual = "." }
dependencies = [
    { name = "flet" },
    { name = "flet-desktop" },
    { name = "pygments" },
]

[package.metadata]
requires-dist = [
    { name = "flet", specifier = "==1.0.1" },
    { name = "flet-desktop", specifier = "==1.0.1" },
    { name = "pygments", specifier = "==2.19.2" },
]

[[package]]
name = "flet"
version = "1.0.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "httpx", marker = "sys_platform != 'emscripten'" },
]
wheels = [{ url = "https://files.pythonhosted.org/packages/aa/flet-1.0.1-py3-none-any.whl", hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000" }]

[[package]]
name = "flet-desktop"
version = "1.0.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "flet" },
    { name = "rich" },
]
wheels = [{ url = "https://files.pythonhosted.org/packages/bb/flet_desktop-1.0.1-py3-none-any.whl", hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000" }]

[[package]]
name = "httpx"
version = "0.28.1"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/packages/cc/httpx-0.28.1-py3-none-any.whl", hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000" }]

[[package]]
name = "pygments"
version = "2.19.2"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/packages/dd/pygments-2.19.2-py3-none-any.whl", hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000" }]

[[package]]
name = "rich"
version = "14.1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "pygments" },
]
wheels = [{ url = "https://files.pythonhosted.org/packages/ee/rich-14.1.0-py3-none-any.whl", hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000" }]
"""


def test_flet_build_leaves_the_desktop_client_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A flet build app runs embedded in its own Flutter host (FLET_PLATFORM set, or Pyodide on the
    # web) and never starts the desktop client of `flet run` and `flet pack`: flet-desktop and
    # what only it needs (rich, pygments...) were 56% of a web app's Python download. The real
    # uv (the uv-floor job runs this with the oldest one), offline, on a hand-written lock: what
    # the app needs itself stays, and a project without flet-desktop exports as before
    from runner.methods import flet

    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_PROJECT", "VIRTUAL_ENV"))} | {"UV_OFFLINE": "1"}
    calls: list[list[str]] = []

    def real_uv(tool: envs.PyEnv, args: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in args])
        r = subprocess.run([uv, *calls[-1]], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
        assert r.returncode == 0, r.stderr
        return r

    monkeypatch.setattr(envs, "uv", real_uv)
    monkeypatch.setattr(common, "LOCK", tmp_path / "uv.lock")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(FLET_DESKTOP_LOCK, encoding="utf-8")
    pins = flet._pinned_requirements(envs.tool_env(make({})))
    assert calls[0][calls[0].index("--prune") + 1] == flet.DESKTOP_CLIENT == "flet-desktop"
    preset = tomllib.loads((ROOT / ".pytemplate" / "presets" / "flet" / "preset.toml").read_text(encoding="utf-8"))
    assert f"{flet.DESKTOP_CLIENT}=={{version}}" in preset["dependencies"]  # the requirement the prune names
    assert pins == ["flet==1.0.1", "httpx==0.28.1 ; sys_platform != 'emscripten'", "pygments==2.19.2"]
    without = FLET_DESKTOP_LOCK.replace('    { name = "flet-desktop" },\n', "")
    without = without[: without.index('[[package]]\nname = "flet-desktop"')] + without[without.index('[[package]]\nname = "httpx"') :]
    (tmp_path / "uv.lock").write_text(without, encoding="utf-8")
    assert flet._pinned_requirements(envs.tool_env(make({}))) == pins


# The platform each mobile or web target is, as the device's Python sees it (sys.platform,
# platform.system()), and what serious_python's pip sees on a build machine: platform.system()
# faked for the target, the rest the build machine's own
FLET_DEVICES = {"web": ("emscripten", "Emscripten"), "apk": ("android", "Android"), "ipa": ("ios", "iOS")}
BUILD_MACHINES = {"linux": ("linux", "posix"), "windows": ("win32", "nt"), "macos": ("darwin", "posix")}
TARGET_PINS = [
    ("httpx==0.28.1 ; sys_platform != 'emscripten'", "httpx==0.28.1 ; platform_system != 'Emscripten'"),
    ('pyodide-http==0.2.2 ; sys_platform == "emscripten"', "pyodide-http==0.2.2 ; platform_system == 'Emscripten'"),
    (
        "oauthlib==3.3.1 ; python_full_version < '3.15' and sys_platform != 'emscripten'",
        "oauthlib==3.3.1 ; python_full_version < '3.15' and platform_system != 'Emscripten'",
    ),
    ("tomli-w==1.2.0 ; sys_platform == 'android'", "tomli-w==1.2.0 ; platform_system == 'Android'"),
    ("distro==1.9.0 ; sys_platform == 'linux'", "distro==1.9.0 ; platform_system == 'Linux'"),
    ("colorama==0.4.6 ; sys_platform == 'win32'", "colorama==0.4.6 ; platform_system == 'Windows'"),
    ("pyobjc-core==11.0 ; sys_platform == 'darwin'", "pyobjc-core==11.0 ; platform_system == 'Darwin'"),
    ("rubicon-objc==0.5.0 ; sys_platform == 'ios'", "rubicon-objc==0.5.0 ; platform_system == 'iOS'"),
    ("pywin32==311 ; os_name == 'nt'", "pywin32==311 ; platform_system == 'Windows'"),
    ("uvloop==0.21.0 ; os_name != 'nt'", "uvloop==0.21.0 ; platform_system != 'Windows'"),
    ("ptyprocess==0.7.0 ; os_name == 'posix'", "ptyprocess==0.7.0 ; platform_system != 'Windows'"),
    ("pywinpty==2.0.15 ; os_name != 'posix'", "pywinpty==2.0.15 ; platform_system == 'Windows'"),
    (
        "evdev==1.9.2 ; (sys_platform == 'linux' or sys_platform == 'darwin') and python_version >= '3.11'",
        "evdev==1.9.2 ; (platform_system == 'Linux' or platform_system == 'Darwin') and python_version >= '3.11'",
    ),
    ("cygwin-only==1.0 ; sys_platform == 'cygwin'", None),  # platform.system() has no fixed name for it
    ("flet==1.0.1", None),
    ("mylib @ file:///src/emscripten/sys_platform ; sys_platform != 'linux'", "mylib @ file:///src/emscripten/sys_platform ; platform_system != 'Linux'"),
]


@pytest.mark.parametrize(("pin", "written"), TARGET_PINS)
def test_flet_target_markers_say_what_flets_pip_reads(pin: str, written: str | None) -> None:
    """uv writes a `platform_system` marker as a `sys_platform` one (flet's `platform_system !=
    "Emscripten"`, a user's `platform_system == "Android"`), and the pip of flet build
    (serious_python) runs on the build machine with only platform.system() faked for the target:
    every web app got httpx and its tree, an apk lacked its Android-only requirement and got the
    Linux-only one. On every target, from every build machine, pip now reads each marker as the
    device's Python would."""
    from runner.methods import flet

    assert flet.target_markers([pin]) == [written or pin]
    markers = pytest.importorskip("packaging.markers")
    requirements = pytest.importorskip("packaging.requirements")
    marker = requirements.Requirement(pin).marker
    if marker is None:
        return
    rewritten = markers.Marker((written or pin).partition(" ;")[2])
    wrong = []
    for target, (platform, system) in FLET_DEVICES.items():
        device = {"sys_platform": platform, "platform_system": system, "os_name": "posix", "python_full_version": "3.14.7", "python_version": "3.14"}
        wanted = marker.evaluate(device)
        for host, (host_platform, host_os) in BUILD_MACHINES.items():
            pip = device | {"sys_platform": host_platform, "os_name": host_os}
            assert rewritten.evaluate(pip) == wanted, (target, host)
            if marker.evaluate(pip) != wanted:
                wrong.append((target, host))
    assert bool(wrong) == (written is not None), wrong  # what went wrong without it


def test_flet_build_rewrites_the_markers_of_mobile_and_web_pins(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pins = ["flet==1.0.1", "httpx==0.28.1 ; sys_platform != 'emscripten'", "tomli-w==1.2.0 ; sys_platform == 'android'"]
    written = ["flet==1.0.1", "httpx==0.28.1 ; platform_system != 'Emscripten'", "tomli-w==1.2.0 ; platform_system == 'Android'"]
    stage = sandbox / "build" / "flet-build" / "cpython" / "pyproject.toml"
    for target in ("web", "apk", "aab", "ipa", "ios-simulator"):
        _flet_build(sandbox, monkeypatch, target=target, pins=pins)
        assert tomllib.loads(stage.read_text(encoding="utf-8"))["project"]["dependencies"] == written, target
    _flet_build(sandbox, monkeypatch, target="linux", pins=pins)  # a desktop app is built on its own OS: pip reads it right
    assert tomllib.loads(stage.read_text(encoding="utf-8"))["project"]["dependencies"] == pins


def test_flet_build_upx_only_for_desktop_and_missing_output(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packed: list[Path] = []
    monkeypatch.setattr(upx, "active", lambda cfg: True)
    monkeypatch.setattr(upx, "pack_tree", lambda cfg, root: packed.append(root) or [])
    out, _ = _flet_build(sandbox, monkeypatch)
    assert packed == [out]
    _flet_build(sandbox, monkeypatch, target="apk")
    assert packed == [out]  # mobile/web output is not packed
    with pytest.raises(PytError, match="without producing the output"):
        _flet_build(sandbox, monkeypatch, target="macos", produce=False)


# --- couplings and launcher files ---------------------------------------------------------------------


# README/CLAUDE.md 13.2: deleting templates/ci.yml stops CI generation; a project that made that
# documented change has no template to read here (13.1: selftest must pass in every project).
needs_ci_template = pytest.mark.skipif(
    not (TEMPLATES / "ci.yml").is_file(),
    reason="CI generation stopped (templates/ci.yml deleted)",
)


@needs_ci_template
def test_ci_uploads_the_pyz_where_the_build_writes_it(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # templates/ci.yml hard-codes dist/__NAME__-__BUILD_BACKEND__-pyz/__NAME__.pyz
    ci = (TEMPLATES / "ci.yml").read_text(encoding="utf-8")
    if "__BUILD_BACKEND__-pyz" not in ci:
        pytest.skip("the CI template no longer uploads a pyz")
    assert "path: dist/__NAME__-__BUILD_BACKEND__-pyz/__NAME__.pyz" in ci
    out, _, _ = _pyz_build(sandbox, monkeypatch, {LINUX: lambda d: _wheel(d, "rich", "15.0.0")}, ["rich==15.0.0"])
    assert out.relative_to(sandbox).as_posix() == "dist/myapp-cpython-pyz/myapp.pyz"
    assert BuildRequest(make({}), "mypyc", "pyz", sandbox).out_name == "myapp-mypyc-pyz"


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("bundled", [False, True])
def test_portable_write_launchers_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool, bundled: bool) -> None:
    from runner.methods import portable

    monkeypatch.setattr(portable, "IS_WINDOWS", windows)
    cfg = make({"app": {"gui": True}})
    python = (tmp_path / "runtime" / ("pythonw.exe" if windows else "bin/python3")) if bundled else None
    written = portable.write_launchers(cfg, "cpython", tmp_path, python)
    names = sorted(p.name for p in written)
    if not bundled:
        assert names == ["myapp.cmd", "myapp.sh"]  # a system folder may be copied to any OS
    else:
        assert names == (["myapp.cmd"] if windows else ["myapp.sh"])
    for path in written:
        data = path.read_bytes()
        assert data.isascii()
        if path.suffix == ".cmd":
            assert data.endswith(b"\r\n") and b"\n" not in data.replace(b"\r\n", b"")
            if bundled:
                assert b'start "" "%~dp0runtime\\pythonw.exe" -s -O "%~dp0boot.py" %*' in data
        else:
            assert b"\r" not in data and data.startswith(b"#!/bin/sh\n")
            if not IS_WINDOWS:
                assert path.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX launcher")
def test_portable_sh_launcher_exit_code_and_environment(tmp_path: Path) -> None:
    from runner.methods import portable

    out = tmp_path / "out"
    (out / "runtime" / "bin").mkdir(parents=True)
    python = out / "runtime" / "bin" / "python3"
    python.symlink_to(Path(sys.executable).resolve())
    boot = "import os, sys\nprint(os.environ.get('PYTHONHOME'), os.environ.get('PYTHONPATH'), os.environ['PYTHONUTF8'], sys.flags.optimize)\nsys.exit(7)\n"
    (out / "boot.py").write_text(boot, encoding="utf-8")
    launcher = out / "app.sh"
    launcher.write_text(portable.sh_launcher(make({}), "cpython", out, python), encoding="utf-8", newline="\n")
    launcher.chmod(0o755)
    env = {**os.environ, "PYTHONPATH": "/caller/path", "PYTHONUTF8": "0"}
    env.pop("PYTHONHOME", None)
    r = subprocess.run([str(launcher)], capture_output=True, text=True, env=env, timeout=120, check=False)
    assert r.returncode == 7, r.stderr  # the app's exit code
    assert r.stdout.split() == ["None", "None", "1", "1"]  # caller's PYTHONPATH cleared, UTF-8 on, -O


# --- wheel: where the dependencies come from ------------------------------------------------------


def test_wheel_declares_git_and_url_sources_as_direct_references() -> None:
    from runner.methods import wheel

    data = {
        "project": {"dependencies": ["rich>=15", "Tool_Kit[fast] ; sys_platform == 'linux'", "blob", "langchain>=0.2", "hashed"]},
        "tool": {"uv": {"sources": {
            "tool-kit": {"git": "https://github.com/org/toolkit", "tag": "v1.2", "subdirectory": "python"},
            "blob": {"url": "https://example.com/blob-1.0-py3-none-any.whl"},
            # an archive whose project is not at its root: the wheel dropped the folder, and its
            # install built the archive's root (another project, or none)
            "langchain": {"url": "https://example.com/langchain-0.2.0.tar.gz", "subdirectory": "libs/langchain"},
            "hashed": {"url": "https://example.com/mono.tar.gz#sha256=" + "0" * 64, "subdirectory": "pkg"},
        }}},
    }
    assert wheel.dependencies(data) == [
        "rich>=15",
        "Tool_Kit[fast] @ git+https://github.com/org/toolkit@v1.2#subdirectory=python ; sys_platform == 'linux'",
        "blob @ https://example.com/blob-1.0-py3-none-any.whl",
        "langchain @ https://example.com/langchain-0.2.0.tar.gz#subdirectory=libs/langchain",
        "hashed @ https://example.com/mono.tar.gz#sha256=" + "0" * 64 + "&subdirectory=pkg",
    ]
    assert wheel.dependencies({"project": {"dependencies": ["rich"]}}) == ["rich"]


@pytest.mark.parametrize(
    "source",
    [{"workspace": True}, {"path": "libs/b08lib", "editable": True}, {"index": "internal"},
     {"git": "https://github.com/org/b08lib", "marker": "sys_platform == 'linux'"}, [{"path": "a"}, {"path": "b"}]],
)
def test_wheel_refuses_a_source_it_cannot_declare(source: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`./pyt add ./libs/b08lib` puts b08lib in [project] dependencies and its path in
    [tool.uv.sources]: the wheel's metadata said `Requires-Dist: b08lib`, a PyPI name, so
    installing it failed or took an unrelated PyPI package of that name (dependency confusion)."""
    from runner.methods import wheel

    data = {"project": {"dependencies": ["rich", "b08lib"]}, "tool": {"uv": {"sources": {"b08lib": source}}}}
    with pytest.raises(PytError, match=r"wheel cannot declare .*b08lib .*--method pyz or portable") as e:
        wheel.dependencies(data)
    assert e.value.code == 2
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "p"\ndependencies = ["b08lib"]\n[tool.uv.sources]\nb08lib = { workspace = true }\n', encoding="utf-8")
    monkeypatch.setattr(wheel, "PYPROJECT", pyproject)
    with pytest.raises(PytError, match="b08lib"):
        wheel.check(make({}))  # cmd_build calls it before the checks and the payload


# --- the suite in a setup it must pass in: another machine, another project ------------------------


def _suite_run(folder: Path, tmp_path: Path, *selection: str, plugin: str = "") -> subprocess.CompletedProcess[str]:
    """Tests of this suite run by pytest in the project `folder` as `./pyt selftest` runs them (the
    suite's own pytest.ini, --rootdir=.). `plugin`: Python code pytest loads first (-p), which sets
    what differs on the machine or in the project the run stands for (an attribute a check reads)."""
    drop = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYTHONPATH")
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("PYTEMPLATE_")}
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", ".pytemplate/tests/pytest.ini", "--rootdir=."]
    argv += ["--basetemp", str(tmp_path / "t")]
    if plugin:
        (tmp_path / "plugin").mkdir()
        (tmp_path / "plugin" / "pt_setup.py").write_text(plugin, encoding="utf-8")
        env["PYTHONPATH"] = os.pathsep.join([str(tmp_path / "plugin"), str(folder / ".pytemplate")])
        argv += ["-p", "pt_setup"]
    return subprocess.run(
        [*argv, *selection], cwd=folder, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, check=False
    )


def test_the_flet_tests_pass_on_a_windows_without_developer_mode(tmp_path: Path) -> None:
    """Developer Mode is off on a Windows machine by default (README asks for it for flet build
    only), and flet.check_options refuses flet build there for every target: the six tests that
    build through _flet_build failed `./pyt selftest` for every such user. They run here with the
    platform check answering as it does on that machine."""
    windows_without_developer_mode = "import runner.methods.flet as flet\n\nflet.IS_WINDOWS = True\nflet._developer_mode = lambda: False\n"
    r = _suite_run(ROOT, tmp_path, ".pytemplate/tests/test_build_methods.py", "-k", "flet_build", plugin=windows_without_developer_mode)
    assert r.returncode == 0, r.stdout[-6000:] + r.stderr[-2000:]
    assert " passed" in r.stdout and "deselected" in r.stdout, r.stdout[-2000:]


def test_the_wheel_tests_pass_in_a_project_with_local_libraries(tmp_path: Path) -> None:
    """A local library (`./pyt add ./libs/x`, a workspace member to uv), a path library or a named
    index (PyTorch's) in pyproject.toml, which pyz and portable support, is what wheel.check
    refuses: five tests that build a wheel to check something else ran it on the project's own
    pyproject.toml and failed `./pyt selftest` there. They run here with such a pyproject.toml."""
    pyproject = tmp_path / "project" / "pyproject.toml"
    pyproject.parent.mkdir()
    pyproject.write_text(
        '[project]\nname = "game"\nversion = "0.1.0"\ndependencies = ["rich", "mylib", "sharedlib", "torch"]\n\n'
        '[tool.uv.sources]\nmylib = { workspace = true }\nsharedlib = { path = "../shared lib" }\ntorch = { index = "pytorch-cpu" }\n',
        encoding="utf-8",
    )
    plugin = f"from pathlib import Path\n\nimport runner.methods.wheel as wheel\n\nwheel.PYPROJECT = Path({str(pyproject)!r})\n"
    wheel_tests = "wheel and (never_compiles or upx_is_resolved or refuses_a_stale_lock)"
    r = _suite_run(ROOT, tmp_path, ".pytemplate/tests/test_build_methods.py", "-k", wheel_tests, plugin=plugin)
    assert r.returncode == 0, r.stdout[-6000:] + r.stderr[-2000:]
    assert "5 passed" in r.stdout, r.stdout[-2000:]


def test_the_nuitka_tests_pass_in_a_project_folder_scons_would_expand(tmp_path: Path) -> None:
    """A project in a folder such as `app$v2`, which every method but nuitka supports (README):
    the tests that run cmd_build with --method nuitka to check something else reached
    nuitka.check_options on the project's own .build/ and failed `./pyt selftest` there. They run
    here in a copy of the project in such a folder."""
    own = tmp_path / "app$v2"
    presets.copy_template(own)
    nuitka_tests = "nuitka_python_newer_than_the_pin or forwards_extras_to_the_packagers"
    r = _suite_run(own, tmp_path, ".pytemplate/tests/test_build_methods.py", "-k", nuitka_tests)
    assert r.returncode == 0, r.stdout[-6000:] + r.stderr[-2000:]
    assert "4 passed" in r.stdout, r.stdout[-2000:]
