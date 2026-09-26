"""Planning logic of `./deploy selftest --e2e` (runner/e2e.py): no project is created here.

The pure parts: the plan per preset and depth, the option parser, the selection checks, the
environment scrub and git isolation, the artifact lookup, the lock/pin, docs and file-state
checks, and the template-e2e.yml workflow that runs the suite (template repository only).
The running parts (logs, timeouts, exit codes, signals, the step kinds) are in test_e2e_run.py.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config, e2e, render  # noqa: E402
from runner.cmd_build import COMPAT  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.e2e import Host, Options, PresetInfo, Step  # noqa: E402
from runner.project import PRESETS, ROOT  # noqa: E402
from runner.ui import DeployError  # noqa: E402

AVAILABLE = ["flet", "raylib", "script"]
HOST = Host("windows")  # a display, no Flutter restriction
DEFAULTS = {"cpython": "exe", "mypyc": "exe", "pypy": "portable"}
SCRIPT = PresetInfo("script", ("cpython", "mypyc"), DEFAULTS, gui=False, tasks=("ci",))
RAYLIB = PresetInfo("raylib", ("cpython", "pypy", "mypyc"), DEFAULTS, gui=True, active="pypy", tasks=("stubs", "bunnymark", "ci"))
FLET = PresetInfo("flet", ("cpython", "mypyc"), DEFAULTS, gui=True, tasks=("dev", "ci"))
TEMPLATE_REPO = (ROOT / ".pytemplate" / "template-repo").is_file()
WORKFLOW = ROOT / ".github" / "workflows" / "template-e2e.yml"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")


def names(steps: list[Step]) -> list[str]:
    return [s.name for s in steps]


def by_name(steps: list[Step]) -> dict[str, Step]:
    return {s.name: s for s in steps}


def builds(steps: list[Step], *, skipped: bool | None = None) -> set[tuple[str, str]]:
    return {(s.backend, s.method) for s in steps if s.kind == "build" and not s.app and (skipped is None or bool(s.skip) == skipped)}


def git_env(tmp_path: Path) -> dict[str, str]:
    """This environment without GIT_* and with no user or system git configuration."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": str(tmp_path / "no-gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"})
    return env


# --- plan ------------------------------------------------------------------------------------


def test_default_plan_order_and_rows() -> None:
    steps = e2e.plan(SCRIPT, Options(), HOST)
    first = ["new", "verify copy", "pristine skeleton", "render --check", "setup", "doctor", "fmt --check", "first commit", "check all", "test all"]
    assert names(steps)[: len(first)] == first
    assert len(set(names(steps))) == len(steps), "row names must be unique (after= refers to them)"
    assert [s.name for s in steps if s.required] == ["new", "setup"]
    assert by_name(steps)["pristine skeleton"].args == ("--dry-run", "__init", "script", "--name", "e2escript")
    assert by_name(steps)["first commit"].kind == "commit" and by_name(steps)["first commit"].args == ("First commit",)
    run = by_name(steps)["run mypyc"]
    assert run.args == ("run", "mypyc") and run.expect == ("Primes", "mypyc (compiled)")
    assert by_name(steps)["run cpython"].expect == ("Primes",)
    for s in steps:  # every after= names an earlier row
        if s.after:
            assert s.after in names(steps)[: names(steps).index(s.name)], s.name


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
    smokes = [s for s in steps if s.kind == "smoke" and not s.app]
    assert {(s.backend, s.method) for s in smokes} == builds(steps, skipped=False)
    assert all(s.after == f"build {s.backend} {s.method}" for s in smokes)
    assert by_name(steps)["smoke mypyc exe"].expect == ("Primes", "mypyc (compiled)")
    for gui in (RAYLIB, FLET):
        assert not [s for s in e2e.plan(gui, Options(), HOST) if s.kind == "smoke"]


def test_quick_builds_the_default_method_per_backend() -> None:
    assert builds(e2e.plan(RAYLIB, Options(quick=True), HOST)) == {("cpython", "exe"), ("pypy", "portable"), ("mypyc", "exe")}
    custom = PresetInfo("script", ("cpython", "mypyc"), {"cpython": "pyz", "mypyc": "wheel"}, gui=False)
    assert builds(e2e.plan(custom, Options(quick=True), HOST)) == {("cpython", "pyz"), ("mypyc", "wheel")}


def test_journey_steps_per_depth() -> None:
    """Every depth commits through the hook and checks the format; default adds the runner's
    own tests in the project, a usage error and the rename round trip; --full the pypy round
    trip and the [preset.*] option edit (for presets that have options)."""
    quick = names(e2e.plan(SCRIPT, Options(quick=True), HOST))
    assert "first commit" in quick and "fmt --check" in quick
    assert not [n for n in quick if n.startswith(("selftest", "usage error", "rename", "round trip", "[preset"))]
    default = e2e.plan(SCRIPT, Options(), HOST)
    assert "selftest in the project" in names(default)
    usage = by_name(default)["usage error: build cpython pyz"]
    assert usage.args == ("build", "cpython", "pyz") and usage.expect_code == 2
    assert "rename e2escript-2" in names(default)
    assert not [n for n in names(default) if n.startswith(("round trip", "[preset"))]
    full = names(e2e.plan(SCRIPT, Options(full=True), HOST))
    assert "round trip: mode --supports +pypy" in full
    assert not [n for n in full if n.startswith("[preset")], "the script preset has no options"
    for info, option in ((RAYLIB, "[preset.raylib] package = raylib_sdl: apply"), (FLET, "[preset.flet] version = 1.0.0: apply")):
        steps = e2e.plan(info, Options(full=True), HOST)
        edit = by_name(steps)[option]
        assert edit.kind == "option" and edit.args == (f"preset.{info.name}", *e2e.OPTION_EDITS[info.name])
        again = by_name(steps)["apply again (no change)"]
        assert again.args == ("apply",) and again.snapshot == again.restores == again.name and again.after == option
        assert by_name(steps)["doctor (option applied)"].after == option
        assert option not in names(e2e.plan(info, Options(), HOST))


def test_stubs_only_for_presets_with_the_task() -> None:
    step = by_name(e2e.plan(RAYLIB, Options(quick=True), HOST))["stubs (typings/ unchanged)"]
    assert step.args == ("stubs",) and step.scope == "typings" and step.snapshot == step.restores == "stubs"
    for info in (SCRIPT, FLET):
        assert not [s for s in e2e.plan(info, Options(), HOST) if s.name.startswith("stubs")]


def test_rename_round_trip_commits_before_renaming_back() -> None:
    """rename refuses uncommitted changes: the way back needs the renamed project committed
    (through the hook), and the files must then be what they were before the rename."""
    steps = e2e.plan(SCRIPT, Options(), HOST)
    rows = names(steps)
    start = rows.index("rename e2escript-2 (dry run)")
    assert rows[start:] == [
        "rename e2escript-2 (dry run)",
        "rename e2escript-2",
        "render --check (e2escript-2)",
        "test all (e2escript-2)",
        "build cpython wheel (e2escript-2)",
        "smoke cpython wheel (e2escript-2)",
        "commit (e2escript-2)",
        "rename e2escript (back)",
    ]
    step = by_name(steps)
    dry = step["rename e2escript-2 (dry run)"]
    assert dry.args == ("--dry-run", "rename", "e2escript-2") and dry.snapshot == dry.restores == dry.name
    assert step["rename e2escript-2"].after == "first commit" and step["rename e2escript-2"].snapshot == "rename e2escript-2"
    wheel = step["build cpython wheel (e2escript-2)"]
    assert wheel.app == "e2escript-2" and wheel.args == ("build", "cpython", "--method", "wheel", "--no-check")
    assert step["smoke cpython wheel (e2escript-2)"].app == "e2escript-2" and step["smoke cpython wheel (e2escript-2)"].expect == ("Primes",)
    commit = step["commit (e2escript-2)"]
    assert commit.kind == "commit" and commit.after == "rename e2escript-2"
    back = step["rename e2escript (back)"]
    assert back.args == ("rename", "e2escript") and back.after == commit.name and back.restores == "rename e2escript-2"
    # GUI presets: no wheel run under the new name; name != package flips to name == package
    raylib = names(e2e.plan(RAYLIB, Options(), HOST))
    assert "rename e2eraylib2" in raylib and not [n for n in raylib if n.startswith(("build", "smoke")) and "(e2eraylib2)" in n]
    # only the selected backends are tested, and a wheel only where one is built
    mypyc = names(e2e.plan(SCRIPT, Options(backends=("mypyc",), methods=("pyz",)), HOST))
    assert "test mypyc (e2escript-2)" in mypyc and not [n for n in mypyc if "wheel (e2escript-2)" in n]
    assert "build mypyc wheel (e2escript-2)" in names(e2e.plan(SCRIPT, Options(backends=("mypyc",)), HOST))


def test_renamed_app_flips_name_equals_package() -> None:
    for app in ("e2escript", "e2e-raylib", "e2e_flet", "myapp", "My-Game"):
        new = e2e.renamed_app(app)
        assert config.APP_NAME.fullmatch(new), new
        same = app == app.replace("-", "_").lower()
        assert (new == new.replace("-", "_").lower()) != same, (app, new)


def test_one_test_project_is_named_like_its_package_and_one_is_not() -> None:
    """name == package (the default of `./deploy new DIR`) is what makes nuitka name its binary
    <app>.bin and put the package folder next to the executables; name != package is what the
    rename context rules and hyphenated scripts need. The e2e covers both."""
    apps = [e2e.preset_info(n).app for n in e2e.DEFAULT_PRESETS]
    shapes = {app == app.replace("-", "_").lower() for app in apps}
    assert shapes == {True, False}, apps
    script = e2e.preset_info("script")
    assert script.app == script.app.replace("-", "_").lower() and not script.gui, "the preset whose artifacts run"


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
    assert trip[0].snapshot == trip[0].name and trip[2].restores == trip[0].name, "the project must end as it was"
    assert not [s for s in e2e.plan(SCRIPT, Options(full=True, backends=("cpython",)), HOST) if s.name.startswith("round trip")]


def test_round_trip_restores_the_active_backend() -> None:
    """raylib is active on pypy: `mode --supports -pypy` moves it to cpython, so the way back
    must name pypy again (else the project ends on another backend)."""
    reverse = [s for s in e2e.plan(RAYLIB, Options(full=True), HOST) if s.name.startswith("round trip")]
    modes = [s.args for s in reverse if s.args[0] == "mode"]
    assert modes == [("mode", "--supports", "-pypy"), ("mode", "pypy", "--supports", "+pypy")]
    assert reverse[1].args == ("test", "all") and reverse[2].restores == reverse[0].snapshot == reverse[0].name
    on_cpython = PresetInfo("raylib", ("cpython", "pypy", "mypyc"), DEFAULTS, gui=True, active="cpython")
    back = [s.args for s in e2e.plan(on_cpython, Options(full=True), HOST) if s.name.startswith("round trip")][2]
    assert back == ("mode", "--supports", "+pypy")


def test_filters() -> None:
    steps = e2e.plan(RAYLIB, Options(backends=("cpython",), methods=("pyz", "exe")), HOST)
    assert builds(steps) == {("cpython", "pyz"), ("cpython", "exe")}
    assert "check cpython" in names(steps) and "test cpython" in names(steps)
    assert "check all" not in names(steps)
    assert not [s for s in steps if s.backend in ("pypy", "mypyc")]
    none = e2e.plan(RAYLIB, Options(backends=("pypy",), methods=("exe",)), HOST)
    assert builds(none) == set()
    row = by_name(none)["build (none selected)"]
    assert row.kind == "unsupported" and row.skip and "exe" in row.skip


def test_an_unsupported_backend_is_a_visible_skip() -> None:
    steps = e2e.plan(SCRIPT, Options(backends=("pypy", "cpython")), HOST)
    row = by_name(steps)["pypy (not supported)"]
    assert row.kind == "unsupported" and "does not support pypy" in row.skip and "--full" in row.skip
    assert "check cpython" in names(steps)
    full = e2e.plan(SCRIPT, Options(backends=("pypy",), full=True), HOST)
    assert "pypy (not supported)" not in names(full), "--full tests it in the round trip"
    assert "round trip: test pypy" in names(full)


def test_selection_that_tests_nothing_is_a_usage_error() -> None:
    def problem(infos: tuple[PresetInfo, ...], opts: Options) -> str:
        return e2e.selection_problem([(i, e2e.plan(i, opts, HOST)) for i in infos], opts)

    assert problem((SCRIPT, RAYLIB, FLET), Options()) == ""
    assert problem((SCRIPT, RAYLIB), Options(backends=("pypy",))) == "", "raylib has pypy"
    message = problem((SCRIPT, FLET), Options(backends=("pypy",)))
    assert "--backends pypy selects nothing" in message and "script: cpython, mypyc" in message and "--full" in message
    assert problem((SCRIPT,), Options(backends=("pypy",), full=True)) == "", "the round trip tests pypy"
    quick = problem((SCRIPT,), Options(methods=("nuitka",), quick=True))
    assert "--methods nuitka selects no build" in quick and "drop --quick" in quick
    assert problem((SCRIPT,), Options(methods=("nuitka",))) == ""
    assert "selects no build" in problem((SCRIPT,), Options(methods=("flet",)))
    assert problem((SCRIPT, FLET), Options(methods=("flet",))) == ""
    assert "selects no build" in problem((RAYLIB,), Options(backends=("pypy",), methods=("exe",)))


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
    assert names(steps)[:7] == ["new", "verify copy", "pristine skeleton", "render --check", "mode --supports cpython,mypyc", "setup", "doctor"]
    mode = by_name(steps)["mode --supports cpython,mypyc"]
    assert mode.args == ("mode", "cpython", "--supports", "cpython,mypyc") and mode.required
    assert "arm64" in by_name(steps)["pypy (every step)"].skip
    assert not [s for s in steps if s.backend == "pypy" and not s.skip]
    assert not [s for s in steps if s.name.startswith("round trip")], "the round trip would re-add pypy"
    assert builds(steps) == builds(e2e.plan(RAYLIB, Options(full=True, backends=("cpython", "mypyc")), HOST))
    for info, host in ((RAYLIB, Host("macos", arch="x86_64")), (RAYLIB, HOST), (SCRIPT, mac_arm), (FLET, mac_arm)):
        assert not [s for s in e2e.plan(info, Options(), host) if s.name.startswith("mode")]


def test_raylib_pypy_gap_on_linux_arm64() -> None:
    """raylib 6.0.1.0 publishes PyPy wheels for x86_64 only: on Linux arm64 (Raspberry Pi,
    Graviton, Docker on a Mac) the project must be switched off PyPy before setup, as on
    Apple Silicon."""
    steps = e2e.plan(RAYLIB, Options(), Host("linux", arch="aarch64"))
    rows = names(steps)
    assert "mode --supports cpython,mypyc" in rows and rows.index("mode --supports cpython,mypyc") < rows.index("setup")
    assert by_name(steps)["mode --supports cpython,mypyc"].args == ("mode", "cpython", "--supports", "cpython,mypyc")
    assert "arm64" in by_name(steps)["pypy (every step)"].skip
    assert not [s for s in steps if s.backend == "pypy" and not s.skip]
    assert not [s for s in e2e.plan(RAYLIB, Options(), Host("linux", arch="x86_64")) if s.name.startswith("mode")]


def _preset_config(name: str) -> Config:
    text = (PRESETS / name / "files" / "pytemplate.toml").read_text("utf-8")
    cfg: Config = config._build(Config, tomllib.loads(text.replace("{{name}}", "myapp").replace("{{pkg}}", "myapp")), "")
    return cfg


def test_host_gaps_match_the_generated_ci_matrix() -> None:
    """Drift guard: the generated ci.yml leaves a backend out of a runner's row exactly where
    e2e.HOST_GAPS says the runner's platform cannot install it (a new runner label, or a gap
    added on one side only, fails here)."""
    runners = {"ubuntu-latest": "linux-x86_64", "windows-latest": "windows-x86_64", "macos-latest": "macos-aarch64"}
    for preset in e2e.DEFAULT_PRESETS:
        cfg = _preset_config(preset)
        rows = dict(re.findall(r'- os: (\S+)\n\s+backends: "([^"]*)"', render.ci_workflow(cfg)))
        assert set(rows) <= set(runners), f"map the new runner label(s) {set(rows) - set(runners)} to their <os>-<arch>"
        for runner, key in runners.items():
            gaps = e2e.HOST_GAPS.get((preset, key), {})
            expected = [b for b in cfg.backend.supported if b not in gaps]
            assert rows.get(runner, "").split() == expected, (preset, runner)


def test_flet_build_only_for_the_flet_preset_and_only_when_available() -> None:
    assert ("cpython", "flet") in builds(e2e.plan(FLET, Options(), HOST), skipped=False)
    no_flutter = Host("windows", flet_build="needs Flutter")
    assert by_name(e2e.plan(FLET, Options(), no_flutter))["build cpython flet"].skip == "needs Flutter"
    assert not [m for _, m in builds(e2e.plan(SCRIPT, Options(), HOST)) if m == "flet"]
    assert not [m for _, m in builds(e2e.plan(FLET, Options(quick=True), HOST)) if m == "flet"]


def test_real_presets_are_read_from_their_files() -> None:
    script, raylib, flet = (e2e.preset_info(n) for n in ("script", "raylib", "flet"))
    assert script.supported == ("cpython", "mypyc") and not script.gui and script.app == "e2escript"
    assert "pypy" in raylib.supported and raylib.gui and raylib.defaults["pypy"] == "portable"
    assert raylib.active == "pypy" and "stubs" in raylib.tasks and raylib.app == "e2e-raylib"
    assert flet.gui and flet.app == "e2e-flet" and flet.active == "cpython"
    for name, (key, value) in e2e.OPTION_EDITS.items():  # an edit must change a real option
        defaults = tomllib.loads((PRESETS / name / "preset.toml").read_text("utf-8"))["options"]
        assert key in defaults and defaults[key] != value, name


# --- host --------------------------------------------------------------------------------------


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


# --- environment and git ---------------------------------------------------------------------------


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
        "GIT_DIR": "/outer/.git",  # a hook's variables would point git at another repository
        "GIT_INDEX_FILE": "index",
        "git_ceiling_directories": "/x",
        "UV_CACHE_DIR": "keep",
        "HOME": "keep",
        "Path": os.pathsep.join([own, "keep1", "", "keep2"]),
    }
    env = e2e.scrub_env(environ, [own])
    assert env == {"UV_CACHE_DIR": "keep", "HOME": "keep", "Path": os.pathsep.join(["keep1", "keep2"])}
    assert environ["UV"] == "uv.exe", "the input is not modified"


def test_isolate_git_sets_the_ceiling_above_the_base(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    e2e.isolate_git(env, tmp_path / "base")
    assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path.resolve())
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and not Path(env["GIT_CONFIG_GLOBAL"]).exists()


def test_child_env_is_scrubbed_and_git_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What every step gets: no git state of the caller (a hook's GIT_DIR/GIT_INDEX_FILE, a
    user's GIT_CEILING_DIRECTORIES), the base's ceiling, and uv reachable on PATH."""
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "outer" / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", "index")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/elsewhere")
    monkeypatch.setenv("PYTEMPLATE_LAUNCHER", "sh")
    uv = tmp_path / "uvdir" / ("uv.exe" if os.name == "nt" else "uv")
    monkeypatch.setattr(e2e.proc, "find_uv", lambda: str(uv))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    env = e2e.child_env(tmp_path / "base")
    assert "GIT_DIR" not in env and "GIT_INDEX_FILE" not in env and "PYTEMPLATE_LAUNCHER" not in env
    assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path.resolve()) and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["PATH"].split(os.pathsep)[0] == str(uv.parent), "uv is found where the runner found it"


@needs_git
def test_isolated_git_never_sees_a_repository_around_the_base(tmp_path: Path) -> None:
    """A base inside a work tree: `new` must still `git init` the project (it checks from the
    base) and `setup` install the hook there (it checks from the project), never in the outer
    repository, where the hook stayed behind or replaced that repository's own."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=git_env(tmp_path))
    base = tmp_path / "e2e"
    (base / "script").mkdir(parents=True)

    def inside(cwd: Path, env: dict[str, str]) -> bool:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, env=env, capture_output=True, text=True, check=False)
        return r.returncode == 0 and r.stdout.strip() == "true"

    plain = git_env(tmp_path)
    assert inside(base, plain) and inside(base / "script", plain), "the outer repository is visible without the isolation"
    env = e2e.scrub_env(plain)
    e2e.isolate_git(env, base)
    assert not inside(base, env) and not inside(base / "script", env)


@needs_git
def test_a_base_that_hides_the_templates_own_repository_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A template in a subfolder of a bigger repository with the base next to it: the ceiling
    that keeps git out of the repository around the base would hide the template's own from
    `new`, which would then copy files git does not track."""
    mono = tmp_path / "mono"
    subprocess.run(["git", "init", "-q", str(mono)], check=True, env=git_env(tmp_path))
    template = mono / "tpl"
    template.mkdir()
    monkeypatch.setattr(e2e, "ROOT", template)
    env = e2e.scrub_env(git_env(tmp_path))
    e2e.isolate_git(env, mono / "e2e")
    assert Path(e2e.hidden_template_repository(env)).resolve() == mono.resolve()
    elsewhere = e2e.scrub_env(git_env(tmp_path))
    e2e.isolate_git(elsewhere, tmp_path / "out" / "e2e")
    assert e2e.hidden_template_repository(elsewhere) == ""


# --- artifacts ---------------------------------------------------------------------------------------


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


def test_smoke_target_finds_the_nuitka_bin_of_an_app_named_like_its_package(tmp_path: Path) -> None:
    """Nuitka standalone on Linux/macOS names the binary <app>.bin when app == package (a file
    <app> would take the place of the package folder next to it)."""
    dist = tmp_path / "e2escript-mypyc-nuitka" / "main.dist"
    (dist / "e2escript").mkdir(parents=True)  # the package folder
    (dist / "e2escript" / "core.so").write_text("x")
    (dist / "e2escript.bin").write_text("x")
    artifact = tmp_path / "e2escript-mypyc-nuitka"
    assert e2e.smoke_target(artifact, "e2escript", "nuitka", False) == dist / "e2escript.bin"
    assert e2e.smoke_target(artifact, "e2escript", "exe", False) is None, "only nuitka renames it"
    assert e2e.smoke_target(artifact, "e2escript", "nuitka", True) is None, "Windows keeps <app>.exe"
    onefile = tmp_path / "e2escript-cpython-nuitka"
    onefile.mkdir()
    (onefile / "e2escript").write_text("x")  # onefile keeps the plain name, and it wins
    assert e2e.smoke_target(onefile, "e2escript", "nuitka", False) == onefile / "e2escript"


# --- pins, docs and file state ---------------------------------------------------------------------


def _lock(path: Path, *packages: tuple[str, str]) -> Path:
    body = "".join(f'\n[[package]]\nname = "{n}"\nversion = "{v}"\nsource = {{ registry = "https://pypi.org/simple" }}\n' for n, v in packages)
    path.write_text(f"version = 1\nrevision = 3\nrequires-python = \">=3.14\"\n{body}", encoding="utf-8")
    return path


def test_requirement_parsing() -> None:
    assert e2e.requirement("flet==1.0.1") == ("flet", "==1.0.1")
    assert e2e.requirement("Raylib_SDL == 6.0.1.0 ; sys_platform != 'win32'") == ("raylib-sdl", "==6.0.1.0")
    assert e2e.requirement("rich[jupyter]>=13, <15") == ("rich", ">=13,<15")
    assert e2e.requirement("types-cffi") == ("types-cffi", "")


def test_lock_problems_follow_the_pins_and_the_templates_own_lock(tmp_path: Path) -> None:
    template = _lock(tmp_path / "template.lock", ("myapp", "0.1.0"), ("rich", "15.0.0"), ("pygments", "2.21.0"))
    project = _lock(tmp_path / "uv.lock", ("e2escript", "0.1.0"), ("rich", "15.0.0"), ("Flet", "1.0.1"))
    assert e2e.lock_problems(project, {"flet": "1.0.1"}, template) == []
    problems = e2e.lock_problems(project, {"flet": "1.0.0", "httpx": "0.28.1"}, template)
    assert problems == ["uv.lock locks flet 1.0.1, constraints.txt has 1.0.0", "uv.lock does not lock httpx (constraints.txt: 0.28.1)"]
    moved = _lock(tmp_path / "moved.lock", ("rich", "14.0.0"))
    assert e2e.lock_problems(moved, {}, template) == ["uv.lock locks rich 14.0.0, the template's uv.lock has 15.0.0"]
    # a package both lock keeps the template's version, whatever an older pin says (a project
    # that runs `new` itself: constraints.txt only reaches the packages its lock lacks)
    assert e2e.lock_problems(project, {"rich": "13.0.0", "flet": "1.0.1"}, template) == []
    assert e2e.lock_problems(tmp_path / "missing.lock", {}, template) == ["cannot read missing.lock"]


def test_requirement_problems_after_an_option_edit(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "g"\ndependencies = ["raylib-sdl==6.0.1.0"]\n[dependency-groups]\ndev = ["types-cffi"]\n', encoding="utf-8")
    _lock(tmp_path / "uv.lock", ("raylib-sdl", "6.0.1.0"), ("types-cffi", "1.0"))
    before, after = {"raylib": "6.0.1.0"}, {"raylib-sdl": "6.0.1.0"}
    assert e2e.requirement_problems(tmp_path, before, after) == []
    _lock(tmp_path / "uv.lock", ("raylib", "6.0.1.0"), ("raylib-sdl", "6.0.1.0"))
    assert e2e.requirement_problems(tmp_path, before, after) == ["uv.lock still locks raylib"]
    assert e2e.requirement_problems(tmp_path, {}, {"raylib-sdl": "6.0.2.0"}) == [
        "pyproject.toml declares raylib-sdl ==6.0.1.0, not ==6.0.2.0",
        "uv.lock locks raylib-sdl 6.0.1.0, not 6.0.2.0",
    ]


def test_docs_problems(tmp_path: Path) -> None:
    """What makes a project made with `new` its own: README.md and description of its own, no
    root LICENSE, the template's README and LICENSE kept byte for byte under .pytemplate/."""
    template, project = tmp_path / "template", tmp_path / "project"
    (template / ".pytemplate").mkdir(parents=True)
    (template / ".pytemplate" / "template-repo").write_text("", encoding="utf-8")
    (template / "README.md").write_bytes(b"# py_template\r\nmanual\r\n")
    (template / "LICENSE").write_bytes(b"MIT\n")
    (project / ".pytemplate").mkdir(parents=True)
    (project / ".pytemplate" / "README.md").write_bytes(b"# py_template\r\nmanual\r\n")
    (project / ".pytemplate" / "LICENSE").write_bytes(b"MIT\n")
    (project / "README.md").write_text("# e2escript\n\nA script.\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname = "e2escript"\ndescription = "A script"\n', encoding="utf-8")
    assert e2e.docs_problems(project, "e2escript", "A script", template) == []
    (project / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (project / "README.md").write_text("# myapp\n", encoding="utf-8")
    (project / ".pytemplate" / "LICENSE").unlink()
    (project / ".pytemplate" / "README.md").write_bytes(b"# py_template\nmanual\n")
    problems = e2e.docs_problems(project, "e2escript", "Another", template)
    assert problems == [
        "README.md does not start with '# e2escript'",
        "README.md names myapp, the template's own app",
        "LICENSE in the project root: the template's belongs in .pytemplate/LICENSE",
        ".pytemplate/README.md differs from the template's README.md",
        "no .pytemplate/LICENSE (a copy of the template's LICENSE)",
        "[project] description is 'A script', not the preset's 'Another'",
    ]
    # made from a project (no template-repo marker): its own .pytemplate/ copies are the source
    (template / ".pytemplate" / "template-repo").unlink()
    (template / ".pytemplate" / "README.md").write_bytes(b"# py_template\nmanual\n")
    assert not [p for p in e2e.docs_problems(project, "e2escript", "Another", template) if "README.md differs" in p]


def test_project_state_and_its_changes(tmp_path: Path) -> None:
    for rel_path in ("src/app/core.py", "pyproject.toml", ".git/index", ".venv/pyvenv.cfg", ".venv-pypy/x", "dist/a.whl", "src/app/__pycache__/core.pyc", "src/app/core.so"):
        (tmp_path / rel_path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel_path).write_text(rel_path, encoding="utf-8")
    before = e2e.project_state(tmp_path)
    assert sorted(before) == ["pyproject.toml", "src/app/core.py"], "environments, builds, caches, .git and binaries are not the project's files"
    (tmp_path / "pyproject.toml").write_text("changed", encoding="utf-8")
    (tmp_path / "src" / "app" / "core.py").unlink()
    (tmp_path / "README.md").write_text("new", encoding="utf-8")
    (tmp_path / ".git" / "index").write_text("committed", encoding="utf-8")
    assert e2e.state_changes(before, e2e.project_state(tmp_path)) == ["~ pyproject.toml", "+ README.md", "- src/app/core.py"]
    assert e2e.project_state(tmp_path / "missing") == {}


# --- template-e2e.yml (template repository only: ./deploy new does not copy template-*.yml) --------


def _paths(text: str, event: str) -> list[str]:
    block = re.search(rf"^  {event}:\n    paths:\n((?:      - .*\n)+)", text, re.M)
    assert block, f"on.{event}.paths not found"
    return [line.split("- ", 1)[1].strip().strip('"') for line in block.group(1).splitlines()]


def e2e_mode(expr: str, event: str, schedule: str = "", chosen: str = "") -> str:
    """Evaluate the workflow's MODE expression for one trigger: `a || b || ...` whose terms are
    inputs.mode, a quoted literal, or (github.<key> == 'v' && 'r')."""
    ctx = {"github.event_name": event, "github.event.schedule": schedule}
    for term in (t.strip() for t in expr.split("||")):
        if term == "inputs.mode":
            value = chosen
        elif m := re.fullmatch(r"'([^']*)'", term):
            value = m.group(1)
        elif m := re.fullmatch(r"\((github\.[a-z_.]+) == '([^']*)' && '([^']*)'\)", term):
            value = m.group(3) if ctx[m.group(1)] == m.group(2) else ""
        else:
            raise AssertionError(f"unexpected term in the MODE expression: {term}")
        if value:
            return value
    return ""


@pytest.mark.skipif(not TEMPLATE_REPO, reason="template repository only")
def test_e2e_workflow_runs_the_right_depth_at_the_right_time() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    expr = re.search(r"MODE: \$\{\{ (.*) \}\}", text)
    assert expr, "MODE expression not found"
    crons = re.findall(r'- cron: "([^"]+)"', text)
    assert crons
    assert e2e_mode(expr.group(1), "push") == "quick" and e2e_mode(expr.group(1), "pull_request") == "quick"
    for chosen in ("quick", "default", "full"):
        assert e2e_mode(expr.group(1), "workflow_dispatch", chosen=chosen) == chosen
    scheduled = {cron: e2e_mode(expr.group(1), "schedule", schedule=cron) for cron in crons}
    assert "quick" not in scheduled.values(), f"a schedule runs only --quick: {scheduled}"
    assert set(scheduled.values()) == {"default", "full"}, scheduled
    assert "quick) args=(--quick)" in text and "full) args=(--full)" in text
    assert 'selftest --e2e "$PRESET" "${args[@]}" --json' in text


@pytest.mark.skipif(not TEMPLATE_REPO, reason="template repository only")
def test_e2e_workflow_triggers_on_what_new_copies() -> None:
    """A new project inherits the runner, the launchers, the root lock and pyproject (tool
    versions), pytemplate.toml, src/ and tests/: a change to any of them must run the e2e."""
    text = WORKFLOW.read_text(encoding="utf-8")
    wanted = {".pytemplate/**", "deploy", "deploy.cmd", "deploy.ps1", "pyproject.toml", "uv.lock", "pytemplate.toml", ".python-version", "src/**", "tests/**", ".gitignore", ".gitattributes", ".github/workflows/template-e2e.yml"}
    for event in ("push", "pull_request"):
        assert wanted <= set(_paths(text, event)), event
    assert "needs: gate" in text and ".pytemplate/template-repo" in text, "skipped in repositories made from the template"
    for kind in ("report", "logs"):  # one artifact per matrix row
        assert f"name: e2e-{kind}-${{{{ matrix.preset }}}}-${{{{ matrix.os }}}}" in text


@pytest.mark.skipif(not TEMPLATE_REPO, reason="template repository only")
@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not installed")
def test_e2e_workflow_passes_actionlint() -> None:
    r = subprocess.run(["actionlint", str(WORKFLOW)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
