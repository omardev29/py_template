"""Planning logic of `./deploy selftest --e2e` (runner/e2e.py): no project is created here."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import e2e  # noqa: E402
from runner.cmd_build import COMPAT  # noqa: E402
from runner.e2e import Host, Options, PresetInfo, Step  # noqa: E402
from runner.ui import DeployError  # noqa: E402

AVAILABLE = ["flet", "raylib", "script"]
HOST = Host("windows")  # a display, no Flutter restriction
DEFAULTS = {"cpython": "exe", "mypyc": "exe", "pypy": "portable"}
SCRIPT = PresetInfo("script", ("cpython", "mypyc"), DEFAULTS, gui=False)
RAYLIB = PresetInfo("raylib", ("cpython", "pypy", "mypyc"), DEFAULTS, gui=True)
FLET = PresetInfo("flet", ("cpython", "mypyc"), DEFAULTS, gui=True)


def names(steps: list[Step]) -> list[str]:
    return [s.name for s in steps]


def by_name(steps: list[Step]) -> dict[str, Step]:
    return {s.name: s for s in steps}


def builds(steps: list[Step], *, skipped: bool | None = None) -> set[tuple[str, str]]:
    return {(s.backend, s.method) for s in steps if s.kind == "build" and (skipped is None or bool(s.skip) == skipped)}


# --- plan ------------------------------------------------------------------------------------


def test_default_plan_order_and_rows() -> None:
    steps = e2e.plan(SCRIPT, Options(), HOST)
    assert names(steps)[:7] == ["new", "verify copy", "render --check", "setup", "doctor", "check all", "test all"]
    assert len(set(names(steps))) == len(steps), "row names must be unique (after= refers to them)"
    assert [s.name for s in steps if s.required] == ["new", "setup"]
    run = by_name(steps)["run mypyc"]
    assert run.args == ("run", "mypyc") and run.expect == ("Primes", "mypyc (compiled)")
    assert by_name(steps)["run cpython"].expect == ("Primes",)


def test_default_plan_honours_compat() -> None:
    for info in (SCRIPT, RAYLIB, FLET):
        planned = builds(e2e.plan(info, Options(), HOST))
        for backend, method in planned:
            assert not COMPAT[method].get(backend), (backend, method)
            assert backend in info.supported
        for backend in info.supported:
            expected = {m for m in ("exe", "portable", "pyz", "wheel", "nuitka") if not COMPAT[m].get(backend)}
            assert {m for b, m in planned if b == backend} - {"flet"} == expected
    raylib = builds(e2e.plan(RAYLIB, Options(), HOST))
    assert ("pypy", "portable") in raylib and ("pypy", "pyz") in raylib
    assert ("pypy", "exe") not in raylib and ("pypy", "nuitka") not in raylib


def test_nuitka_is_skipped_unless_full_or_named() -> None:
    assert builds(e2e.plan(SCRIPT, Options(), HOST), skipped=True) == {("cpython", "nuitka"), ("mypyc", "nuitka")}
    assert not builds(e2e.plan(SCRIPT, Options(full=True), HOST), skipped=True)
    named = e2e.plan(SCRIPT, Options(methods=("nuitka",)), HOST)
    assert builds(named) == {("cpython", "nuitka"), ("mypyc", "nuitka")}
    assert not builds(named, skipped=True)


def test_build_steps_skip_the_check_and_smoke_only_console_apps() -> None:
    steps = e2e.plan(SCRIPT, Options(), HOST)
    build = by_name(steps)["build mypyc pyz"]
    assert build.args == ("build", "mypyc", "--method", "pyz", "--no-check")
    smokes = [s for s in steps if s.kind == "smoke"]
    assert {(s.backend, s.method) for s in smokes} == builds(steps, skipped=False)
    assert all(s.after == f"build {s.backend} {s.method}" for s in smokes)
    assert by_name(steps)["smoke mypyc exe"].expect == ("Primes", "mypyc (compiled)")
    for gui in (RAYLIB, FLET):
        assert not [s for s in e2e.plan(gui, Options(), HOST) if s.kind == "smoke"]


def test_quick_builds_the_default_method_per_backend() -> None:
    assert builds(e2e.plan(RAYLIB, Options(quick=True), HOST)) == {("cpython", "exe"), ("pypy", "portable"), ("mypyc", "exe")}
    custom = PresetInfo("script", ("cpython", "mypyc"), {"cpython": "pyz", "mypyc": "wheel"}, gui=False)
    assert builds(e2e.plan(custom, Options(quick=True), HOST)) == {("cpython", "pyz"), ("mypyc", "wheel")}
    assert not [s for s in e2e.plan(SCRIPT, Options(quick=True), HOST) if s.name.startswith("round trip")]


def test_full_adds_the_pypy_round_trip() -> None:
    steps = e2e.plan(SCRIPT, Options(full=True), HOST)
    trip = [s for s in steps if s.name.startswith("round trip")]
    assert [s.args for s in trip] == [
        ("mode", "--supports", "+pypy"),
        ("test", "pypy"),
        ("mode", "--supports", "-pypy"),
        ("render", "--check"),
    ]
    assert all(s.after == trip[0].name for s in trip[1:])
    reverse = [s.args for s in e2e.plan(RAYLIB, Options(full=True), HOST) if s.name.startswith("round trip")]
    assert reverse[0] == ("mode", "--supports", "-pypy") and reverse[2] == ("mode", "--supports", "+pypy")
    assert not [s for s in e2e.plan(SCRIPT, Options(full=True, backends=("cpython",)), HOST) if s.name.startswith("round trip")]


def test_filters() -> None:
    steps = e2e.plan(RAYLIB, Options(backends=("cpython",), methods=("pyz", "exe")), HOST)
    assert builds(steps) == {("cpython", "pyz"), ("cpython", "exe")}
    assert "check cpython" in names(steps) and "test cpython" in names(steps)
    assert "check all" not in names(steps)
    assert not [s for s in steps if s.backend in ("pypy", "mypyc")]
    assert builds(e2e.plan(RAYLIB, Options(backends=("pypy",), methods=("exe",)), HOST)) == set()


def test_gui_runs_follow_the_host() -> None:
    run = by_name(e2e.plan(RAYLIB, Options(), HOST))["run pypy"]
    assert run.args == ("run", "pypy", "--frames", "5") and not run.skip and not run.wrap
    assert run.expect == ("frames in",)
    assert by_name(e2e.plan(RAYLIB, Options(), HOST))["run mypyc"].expect == ("frames in", "mypyc/")
    headless = Host("linux", display="no display")
    assert by_name(e2e.plan(RAYLIB, Options(), headless))["run cpython"].skip == "no display"
    xvfb = Host("linux", gui_wrap=("xvfb-run", "-a"))
    assert by_name(e2e.plan(RAYLIB, Options(), xvfb))["run cpython"].wrap == ("xvfb-run", "-a")
    assert not by_name(e2e.plan(SCRIPT, Options(), headless))["run cpython"].skip, "console apps need no display"
    flet_runs = [s for s in e2e.plan(FLET, Options(), HOST) if s.name.startswith("run ")]
    assert flet_runs and all(s.skip for s in flet_runs)


def test_host_gaps_switch_the_project_off_the_backend() -> None:
    mac_arm = Host("macos", arch="aarch64")
    steps = e2e.plan(RAYLIB, Options(full=True), mac_arm)
    assert names(steps)[:6] == ["new", "verify copy", "render --check", "mode --supports cpython,mypyc", "setup", "doctor"]
    mode = by_name(steps)["mode --supports cpython,mypyc"]
    assert mode.args == ("mode", "cpython", "--supports", "cpython,mypyc") and mode.required
    assert "arm64" in by_name(steps)["pypy (every step)"].skip
    assert not [s for s in steps if s.backend == "pypy" and not s.skip]
    assert not [s for s in steps if s.name.startswith("round trip")], "the round trip would re-add pypy"
    assert builds(steps) == builds(e2e.plan(RAYLIB, Options(full=True, backends=("cpython", "mypyc")), HOST))
    for info, host in ((RAYLIB, Host("macos", arch="x86_64")), (RAYLIB, HOST), (SCRIPT, mac_arm), (FLET, mac_arm)):
        assert not [s for s in e2e.plan(info, Options(), host) if s.name.startswith("mode")]


def test_flet_build_only_for_the_flet_preset_and_only_when_available() -> None:
    assert ("cpython", "flet") in builds(e2e.plan(FLET, Options(), HOST), skipped=False)
    no_flutter = Host("windows", flet_build="needs Flutter")
    assert by_name(e2e.plan(FLET, Options(), no_flutter))["build cpython flet"].skip == "needs Flutter"
    assert not [m for _, m in builds(e2e.plan(SCRIPT, Options(), HOST)) if m == "flet"]
    assert not [m for _, m in builds(e2e.plan(FLET, Options(quick=True), HOST)) if m == "flet"]


def test_real_presets_are_read_from_their_files() -> None:
    script, raylib, flet = (e2e.preset_info(n) for n in ("script", "raylib", "flet"))
    assert script.supported == ("cpython", "mypyc") and not script.gui
    assert "pypy" in raylib.supported and raylib.gui and raylib.defaults["pypy"] == "portable"
    assert flet.gui and flet.app == "e2e-flet"


# --- options -----------------------------------------------------------------------------------


def test_parse_args() -> None:
    assert e2e.parse_args([], AVAILABLE).presets == ("script", "raylib", "flet")
    opts = e2e.parse_args(["raylib", "flet,script", "--quick", "--backends", "cpython,mypyc", "--json"], AVAILABLE)
    assert opts.presets == ("raylib", "flet", "script") and opts.quick and opts.as_json
    assert opts.backends == ("cpython", "mypyc")
    assert e2e.parse_args(["script,script"], AVAILABLE).presets == ("script",)
    assert e2e.parse_args(["raylib", "--quick", "flet"], AVAILABLE).presets == ("raylib", "flet")
    with pytest.raises(DeployError, match="unknown preset"):
        e2e.parse_args(["nope"], AVAILABLE)
    with pytest.raises(DeployError, match="--methods"):
        e2e.parse_args(["--methods", "exe,zip"], AVAILABLE)
    with pytest.raises(SystemExit):
        e2e.parse_args(["--quick", "--full"], AVAILABLE)


# --- environment and artifacts -------------------------------------------------------------------


def test_scrub_env() -> None:
    own = os.path.join("x", "runner-env", "bin")
    environ = {
        "VIRTUAL_ENV": "v",
        "UV": "uv.exe",
        "UV_PROJECT_ENVIRONMENT": "p",
        "UV_PYTHON": "3.14",
        "PYTHONPATH": "pp",
        "PYTEMPLATE_CALLER_CWD": "c",
        "pytemplate_launcher": "l",
        "UV_CACHE_DIR": "keep",
        "HOME": "keep",
        "Path": os.pathsep.join([own, "keep1", "", "keep2"]),
    }
    env = e2e.scrub_env(environ, [own])
    assert env == {"UV_CACHE_DIR": "keep", "HOME": "keep", "Path": os.pathsep.join(["keep1", "keep2"])}
    assert environ["UV"] == "uv.exe", "the input is not modified"


def test_find_artifact_and_smoke_target(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "e2e-script-cpython-exe").mkdir(parents=True)
    assert e2e.find_artifact(dist, "e2e-script", "cpython", "exe") is None, "an empty dir is no artifact"
    (dist / "e2e-script-cpython-exe" / "e2e-script.exe").write_text("x")
    exe_dir = e2e.find_artifact(dist, "e2e-script", "cpython", "exe")
    assert exe_dir is not None
    assert e2e.smoke_target(exe_dir, "e2e-script", "exe", True) == exe_dir / "e2e-script.exe"
    onedir = dist / "e2e-script-mypyc-exe" / "e2e-script"
    onedir.mkdir(parents=True)
    (onedir / "e2e-script").write_text("x")
    found = e2e.find_artifact(dist, "e2e-script", "mypyc", "exe")
    assert found is not None and e2e.smoke_target(found, "e2e-script", "exe", False) == onedir / "e2e-script"
    portable = dist / "e2e-script-pypy-portable-pp311-windows-x86_64"
    portable.mkdir()
    (portable / "e2e-script.cmd").write_text("x")
    (dist / "e2e-script-pypy-portable-pp311-windows-x86_64.zip").write_text("x")
    assert e2e.find_artifact(dist, "e2e-script", "pypy", "portable") == portable
    assert e2e.smoke_target(portable, "e2e-script", "portable", True) == portable / "e2e-script.cmd"
    assert e2e.smoke_target(portable, "e2e-script", "portable", False) is None
    system = dist / "e2e-script-cpython-portable"  # runtime = "system": no platform key
    system.mkdir()
    (system / "e2e-script.sh").write_text("x")
    assert e2e.find_artifact(dist, "e2e-script", "cpython", "portable") == system
    assert e2e.smoke_target(system, "e2e-script", "portable", False) == system / "e2e-script.sh"
    wheel = dist / "e2e-script-cpython-wheel"
    wheel.mkdir()
    (wheel / "e2e_script-0.1.0-py3-none-any.whl").write_text("x")
    assert e2e.smoke_target(wheel, "e2e-script", "wheel", True) == wheel / "e2e_script-0.1.0-py3-none-any.whl"


def test_failures_block_what_depends_on_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failing = {"build cpython exe"}

    def fake_execute(ctx: e2e.Context, step: Step, log: Path) -> tuple[str, str]:
        log.write_text("line 1\nline 2\n")
        return (e2e.FAIL, "exit code 1") if step.name in failing else (e2e.PASS, "")

    monkeypatch.setattr(e2e, "execute", fake_execute)

    def run() -> dict[str, str]:
        ctx = e2e.Context(SCRIPT, tmp_path, tmp_path / "p", tmp_path / "logs", tmp_path / "work", "uv", {}, Options())
        ctx.logs.mkdir(exist_ok=True)
        return {r.step: r.status for r in e2e.run_preset(ctx, e2e.plan(SCRIPT, Options(), HOST))}

    status = run()
    assert status["build cpython exe"] == e2e.FAIL and status["smoke cpython exe"] == e2e.SKIP
    assert status["build cpython pyz"] == e2e.PASS and status["smoke cpython pyz"] == e2e.PASS
    assert status["build cpython nuitka"] == e2e.SKIP
    failing = {"setup"}  # required: the rest of the preset is skipped
    status = run()
    assert status["new"] == e2e.PASS and status["setup"] == e2e.FAIL
    after_setup = list(status)[list(status).index("setup") + 1 :]
    assert after_setup and all(status[s] == e2e.SKIP for s in after_setup)


def test_detect_host_gui_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    which: dict[str, str] = {"xvfb-run": "/usr/bin/xvfb-run"}
    monkeypatch.setattr(e2e, "host_os", lambda: "linux")
    monkeypatch.setattr(e2e, "host_arch", lambda: "x86_64")
    monkeypatch.setattr(e2e, "flet_build_reason", lambda os_name: "n/a")
    monkeypatch.setattr(e2e.shutil, "which", lambda name, *a, **k: which.get(name))
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "CI"):
        monkeypatch.delenv(var, raising=False)
    assert e2e.detect_host("auto").gui_wrap[0] == "/usr/bin/xvfb-run"
    assert e2e.detect_host("on").gui_wrap[0] == "/usr/bin/xvfb-run"
    off = e2e.detect_host("off")
    assert off.display == "--gui off" and not off.gui_wrap
    which.clear()
    assert "xvfb-run" in e2e.detect_host("auto").display
    assert e2e.detect_host("on").display == ""
    monkeypatch.setenv("DISPLAY", ":0")
    assert e2e.detect_host("auto") == Host("linux", flet_build="n/a", arch="x86_64")
    monkeypatch.setattr(e2e, "host_os", lambda: "windows")
    assert e2e.detect_host("auto").display == ""
    monkeypatch.setenv("CI", "true")
    assert "CI" in e2e.detect_host("auto").display
    assert e2e.detect_host("on").display == ""


def test_default_base_is_short() -> None:
    base = e2e.default_base()
    assert base.name in ("e2e", "pt-e2e")
    assert len(str(base)) < 80
