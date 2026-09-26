"""Build methods (runner/cmd_build.py, runner/methods/*): argv construction, output discovery,
pyz/portable layouts and bootstraps. No network, no packager: the packager calls are recorded."""

from __future__ import annotations

import importlib.machinery
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_build, config, envs, mypyc, proc, upx  # noqa: E402
from runner.cmd_build import BuildRequest  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.methods import common, exe, nuitka  # noqa: E402
from runner.ui import DeployError  # noqa: E402

IS_WINDOWS = os.name == "nt"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


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
    assert ["--add-data", f"{stage / 'assets'}:assets"] == argv[argv.index("--add-data") : argv.index("--add-data") + 2]
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


def test_exe_size_args_use_upx_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(exe, "IS_WINDOWS", True)
    monkeypatch.setattr(upx, "unsupported_reason", lambda: "")
    monkeypatch.setattr(upx, "find", lambda cfg: tmp_path / "upx.exe")
    args, env = exe.size_args(make({"deploy": {"upx": {"enabled": True, "level": "7", "exclude": ["flutter*.dll"]}}}))
    assert f"--upx-dir={tmp_path}" in args and "--noupx" not in args
    assert "--upx-exclude=flutter_windows.dll" in args and "--upx-exclude=flutter*.dll" in args
    assert env == {"UPX": "-7"}


# --- exe with the flet preset: flet pack ---------------------------------------------------------


def _flet_cfg(name: str = "fletdemo", **exe_cfg: Any) -> Config:
    return make(
        {
            "app": {"name": name, "preset": "flet", "gui": True},
            "backend": {"active": "cpython", "supported": ["cpython", "mypyc"]},
            "compile": {"modules": [f"{name.replace('-', '_').lower()}.core"]},
            "deploy": {"exe": {"mode": "onedir", **exe_cfg}},
        }
    )


def _flet_pack(sandbox: Path, monkeypatch: pytest.MonkeyPatch, cfg: Config, backend: str, *, windows: bool, macos: bool) -> Recorder:
    app = fake_app(sandbox / "payload", cfg.pkg)

    def effect(args: list[str], cwd: Path | None) -> None:
        assert cwd is not None
        (cwd / "dist" / cfg.app.name).mkdir(parents=True)  # what flet pack writes into its cwd

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
    # ./deploy new): PyInstaller's COLLECT failed with NotADirectoryError
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


@pytest.mark.parametrize(("console", "expected"), [("auto", False), ("yes", True), ("no", False)])
def test_flet_pack_console_and_utf8(sandbox: Path, monkeypatch: pytest.MonkeyPatch, console: str, expected: bool) -> None:
    # flet pack always adds --noconsole unless --debug-console has a value: deploy.exe.console
    # was ignored. And the exe ran without UTF-8 mode, unlike ./deploy run and PyInstaller.
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
    # extensions): with app.name == pkg (the default of ./deploy new) and no .exe the binary
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


def test_nuitka_without_output_is_a_clear_error(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({})
    app = _nuitka_app(sandbox / "payload", cfg.pkg)
    monkeypatch.setattr(envs, "uv", FakeNuitka(cfg.pkg, produce=False))
    with pytest.raises(DeployError, match=r"\*\.dist folder"):
        nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))
    with pytest.raises(DeployError, match="without producing myapp"):
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
    monkeypatch.setattr(nuitka, "IS_WINDOWS", True)
    monkeypatch.setattr(envs, "uv", fake)
    monkeypatch.setattr(upx, "active", lambda cfg: True)
    monkeypatch.setattr(upx, "find", lambda cfg: Path("/opt/upx/upx"))
    nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app, extra=["--report=r.xml"]))
    argv = fake.argv
    stage = sandbox / "build" / "nuitka-stage" / "cpython"
    assert argv[argv.index(nuitka.NUITKA) + 1 :][:4] == ["python", "-m", "nuitka", str(stage / "main.py")]
    assert "--output-filename=myapp.exe" in argv and "--include-package=myapp" in argv
    assert ("--python-flag=no_asserts" in argv) is (optimize >= 1)
    assert ("--python-flag=no_docstrings" in argv) is (optimize >= 2)
    assert "--nofollow-import-to=PIL" in argv and "--nofollow-import-to=ssl" in argv
    assert "--windows-console-mode=disable" in argv  # app.gui on Windows
    from runner.project import ROOT

    assert f"--windows-icon-from-ico={ROOT / 'art' / 'app.ico'}" in argv
    assert f"--include-data-dir={stage / 'assets'}=assets" in argv
    assert "--plugin-enable=upx" in argv and f"--upx-binary={Path('/opt/upx/upx')}" in argv
    assert argv[-2:] == ["--lto=no", "--report=r.xml"]  # extra_args, then the command line
    assert not [a for a in argv if a.startswith("--include-module=")]  # cpython: Nuitka follows the imports


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
    with pytest.raises(DeployError, match="could not check the imports"):
        nuitka.includable(make({}), tmp_path, ["json"])


def test_nuitka_pin_supports_the_default_python() -> None:
    # Raising the template's default python.cpython beyond what the pinned Nuitka supports would
    # break --method nuitka for every new project: bump NUITKA and NUITKA_PYTHON with it
    assert nuitka._minor(make({}).python.cpython) <= nuitka._minor(nuitka.NUITKA_PYTHON)
    assert nuitka.NUITKA_PYTHON.count(".") == 1


@pytest.mark.parametrize("extra", [[], ["--experimental=python3.15"], ["--experimental", "python3.15"]])
def test_nuitka_python_newer_than_the_pin_is_refused_before_any_work(monkeypatch: pytest.MonkeyPatch, extra: list[str]) -> None:
    # nuitka==4.2.2 stops with FATAL on CPython 3.15 after the checks and the mypyc compile
    def must_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the build went ahead")

    monkeypatch.setattr(cmd_build, "run_checks", must_not_run)
    monkeypatch.setattr(cmd_build, "payload", must_not_run)
    cfg = make({"python": {"cpython": "3.15"}})
    if not extra:
        with pytest.raises(DeployError) as e:
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
    with pytest.raises(DeployError) as e:
        nuitka.build(BuildRequest(cfg, "cpython", "nuitka", app))
    assert e.value.code == 1 and nuitka.NUITKA in str(e.value) and "methods/nuitka.py" in str(e.value)
