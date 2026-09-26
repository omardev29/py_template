"""selftest --e2e: end-to-end test of the template.

    ./deploy selftest --e2e [PRESET ...] [--backends B,..] [--methods M,..] [--quick | --full]
                            [--gui auto|on|off] [--keep] [--reuse] [--json] [--base DIR]

For each preset (default: script, raylib, flet) it creates <base>/<preset> with THIS
template's `./deploy new`, then works in it like a user would: render --check, setup, doctor,
check, test, run and build (the runner through `uv run --script`, stdin closed, a clean
environment), checks every artifact in dist/ and smoke-runs the cheap headless ones.

- default: every (backend, method) pair that cmd_build.COMPAT allows, except nuitka (slow).
- --quick: one build per backend (its deploy.default method).
- --full:  adds nuitka and a pypy round trip (`mode --supports +pypy`, test, and back).
- `flet build` (Flutter SDK + Windows Developer Mode) is SKIP unless both are detected.

Every step is one PASS/FAIL/SKIP row with its time; its output goes to
<base>/logs/<preset>/NN-step.log. A failed step fails its preset (a failed `new` or `setup`
skips the rest of that preset) and the next presets still run. The base dir is removed at
the end unless --keep or something failed. The planning (plan, parse_args, scrub_env) is
pure and unit-tested in .pytemplate/tests/test_e2e_plan.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import proc, ui
from .cmd_build import COMPAT
from .config import BACKENDS, METHODS, Config
from .project import CONFIG_FILE, ENV_SUFFIX, IS_WINDOWS, PRESETS, ROOT, host_arch, host_os, user_path, venv_python
from .ui import DeployError

DEFAULT_PRESETS = ("script", "raylib", "flet")
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
MARKER = ".pytemplate-e2e"  # in the base dir: only a dir carrying it is ever wiped
# Dropped from every child's environment: they would point the new project's uv at this
# runner's own environment or interpreter, or leak the outer launcher's state (PYTEMPLATE_*)
SCRUBBED = frozenset({"VIRTUAL_ENV", "UV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYTHONHOME", "PYTHONPATH"})

# Seconds per step kind (builds: per method). Generous: the first run downloads interpreters,
# PyPy and wheels; PyInstaller, portable runtime copies and mypyc compiles take minutes.
TIMEOUTS = {"new": 900, "verify": 120, "render": 300, "setup": 1800, "doctor": 300, "check": 900, "test": 1200, "run": 600, "mode": 1800, "smoke": 300}
BUILD_TIMEOUTS = {"exe": 1200, "portable": 1200, "pyz": 900, "wheel": 900, "nuitka": 3600, "flet": 3600}
# How each preset's app ends on its own, and the text its output must contain. A preset
# missing here runs with no arguments if it is a console app and is skipped if it is a GUI.
SMOKE: dict[str, tuple[tuple[str, ...], str]] = {
    "script": ((), "Primes"),
    "raylib": (("--frames", "5"), "frames in"),
}
# What the app prints when its core really is the mypyc binary (not the .py fallback)
COMPILED_MARK = {"script": "mypyc (compiled)", "raylib": "mypyc/"}
SMOKE_METHODS = ("exe", "portable", "pyz", "wheel", "nuitka")  # `flet build`: files only
# Backends a preset cannot install on a host: (preset, "<os>-<arch>") -> {backend: reason}.
# The project is switched off them (`mode`) before setup; render.ci_workflow leaves the same
# ones out of the generated CI matrix.
HOST_GAPS: dict[tuple[str, str], dict[str, str]] = {
    ("raylib", "macos-aarch64"): {"pypy": "raylib publishes no PyPy wheels for macOS arm64"},
}


# --- planning (pure: unit-tested) --------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    presets: tuple[str, ...] = DEFAULT_PRESETS
    backends: tuple[str, ...] = ()  # empty = every supported backend
    methods: tuple[str, ...] = ()  # empty = the default selection
    quick: bool = False
    full: bool = False
    gui: str = "auto"  # auto | on | off
    keep: bool = False
    reuse: bool = False
    as_json: bool = False
    base: str = ""  # as typed by the user ("" = default_base())


@dataclass(frozen=True)
class Host:
    os: str  # windows | linux | macos
    display: str = ""  # why a GUI window cannot open here ("" = it can)
    gui_wrap: tuple[str, ...] = ()  # argv prefix for GUI runs (xvfb-run on a headless Linux)
    flet_build: str = ""  # why `flet build` cannot run here ("" = it can)
    arch: str = ""  # x86_64 | aarch64 (uv names; "" = unknown)


@dataclass(frozen=True)
class PresetInfo:
    name: str
    supported: tuple[str, ...]
    defaults: Mapping[str, str]  # deploy.default: backend -> method
    gui: bool

    @property
    def app(self) -> str:
        """App name of the test project (never the preset name: `flet` or `raylib` would shadow the library)."""
        return f"e2e-{self.name}"


@dataclass(frozen=True)
class Step:
    preset: str
    name: str  # row label, unique within the preset
    kind: str  # new | verify | deploy | build | smoke
    args: tuple[str, ...] = ()  # ./deploy arguments (deploy, build)
    timeout: float = 600
    skip: str = ""  # SKIP with this reason, never run
    required: bool = False  # if it fails, the rest of the preset is skipped
    after: str = ""  # a step that must PASS first (smoke -> its build)
    expect: tuple[str, ...] = ()  # text the app's stdout must contain
    wrap: tuple[str, ...] = ()  # argv prefix (xvfb-run)
    backend: str = ""
    method: str = ""


def _deploy(preset: str, name: str, args: tuple[str, ...], **kw: Any) -> Step:
    kind = args[0]
    return Step(preset, name, "deploy", args, timeout=TIMEOUTS.get(kind, 600), **kw)


def expected_output(preset: str, backend: str) -> tuple[str, ...]:
    text = SMOKE.get(preset, ((), ""))[1]
    if not text:
        return ()
    mark = COMPILED_MARK.get(preset, "")
    return (text, mark) if backend == "mypyc" and mark else (text,)


def run_step(info: PresetInfo, backend: str, host: Host) -> Step:
    name = f"run {backend}"
    smoke = SMOKE.get(info.name)
    if smoke is None and info.gui:
        return Step(info.name, name, "deploy", skip="GUI app that does not close itself: `test` covers its core", backend=backend)
    args = smoke[0] if smoke else ()
    skip = host.display if info.gui else ""
    wrap = host.gui_wrap if info.gui and not skip else ()
    return _deploy(info.name, name, ("run", backend, *args), skip=skip, wrap=wrap, expect=expected_output(info.name, backend), backend=backend)


def build_methods(info: PresetInfo, backend: str, opts: Options, host: Host) -> list[tuple[str, str]]:
    """Return the (method, skip reason) pairs to build for a backend ("" = build it)."""
    candidates = [info.defaults.get(backend, "exe")] if opts.quick else list(METHODS)
    out: list[tuple[str, str]] = []
    for method in candidates:
        if COMPAT.get(method, {}).get(backend):
            continue  # unsupported by design (e.g. PyInstaller + PyPy): not a row
        if method == "flet" and info.name != "flet":
            continue
        if opts.methods and method not in opts.methods:
            continue
        reason = ""
        if method == "nuitka" and not opts.full and method not in opts.methods:
            reason = "slow (compiles everything to C): add --full or --methods nuitka"
        elif method == "flet" and host.flet_build:
            reason = host.flet_build
        out.append((method, reason))
    return out


def pypy_round_trip(info: PresetInfo, opts: Options) -> list[Step]:
    """--full: add PyPy, test it and remove it again (the reverse for presets that have it)."""
    if opts.backends and "pypy" not in opts.backends:
        return []
    p = info.name
    has = "pypy" in info.supported
    first, back = ("-pypy", "+pypy") if has else ("+pypy", "-pypy")
    head = f"round trip: mode --supports {first}"
    test = ("test", "all") if has else ("test", "pypy")
    return [
        _deploy(p, head, ("mode", "--supports", first)),
        _deploy(p, f"round trip: {' '.join(test)}", test, after=head),
        _deploy(p, f"round trip: mode --supports {back}", ("mode", "--supports", back), after=head),
        _deploy(p, "round trip: render --check", ("render", "--check"), after=head),
    ]


def host_gaps(info: PresetInfo, host: Host) -> dict[str, str]:
    """Return the preset's backends this host cannot install, with the reason."""
    gaps = HOST_GAPS.get((info.name, f"{host.os}-{host.arch}"), {})
    return {b: why for b, why in gaps.items() if b in info.supported}


def plan(info: PresetInfo, opts: Options, host: Host) -> list[Step]:
    """Return every step (row) for one preset, in order."""
    p = info.name
    steps = [
        Step(p, "new", "new", timeout=TIMEOUTS["new"], required=True),
        Step(p, "verify copy", "verify", timeout=TIMEOUTS["verify"]),
        _deploy(p, "render --check", ("render", "--check")),
    ]
    gaps = host_gaps(info, host)
    keep = [b for b in info.supported if b not in gaps]
    if gaps:
        # `mode` BACKEND also moves the active backend off a gap (raylib's is pypy)
        steps.append(_deploy(p, f"mode --supports {','.join(keep)}", ("mode", keep[0], "--supports", ",".join(keep)), required=True))
    steps += [_deploy(p, "setup", ("setup",), required=True), _deploy(p, "doctor", ("doctor",))]
    backends = [b for b in keep if not opts.backends or b in opts.backends]
    targets = backends if opts.backends else ["all"]
    for verb in ("check", "test"):
        steps += [_deploy(p, f"{verb} {t}", (verb, t)) for t in targets]
    steps += [run_step(info, b, host) for b in backends]
    steps += [Step(p, f"{b} (every step)", "deploy", skip=why, backend=b) for b, why in gaps.items() if not opts.backends or b in opts.backends]
    for b in backends:
        for method, reason in build_methods(info, b, opts, host):
            build = f"build {b} {method}"
            steps.append(
                Step(p, build, "build", ("build", b, "--method", method, "--no-check"), timeout=BUILD_TIMEOUTS.get(method, 1200), skip=reason, backend=b, method=method)
            )
            if not reason and not info.gui and method in SMOKE_METHODS:
                steps.append(Step(p, f"smoke {b} {method}", "smoke", timeout=TIMEOUTS["smoke"], after=build, expect=expected_output(p, b), backend=b, method=method))
    if opts.full and "pypy" not in gaps:
        steps += pypy_round_trip(info, opts)
    return steps


def _csv(raw: str, allowed: Sequence[str], flag: str) -> tuple[str, ...]:
    names = [n.strip() for n in raw.split(",") if n.strip()]
    bad = [n for n in names if n not in allowed]
    if bad:
        raise DeployError(f"selftest --e2e {flag}: unknown {', '.join(bad)} (valid: {', '.join(allowed)})")
    return tuple(dict.fromkeys(names))


def parse_args(args: Sequence[str], available: Sequence[str]) -> Options:
    parser = argparse.ArgumentParser(prog="./deploy selftest --e2e", description="End-to-end test: ./deploy new + setup/check/test/run/build per preset.")
    parser.add_argument("presets", nargs="*", metavar="PRESET", help="presets to test, spaces or commas (default: script raylib flet)")
    parser.add_argument("--backends", default="", help="only these backends, e.g. cpython,mypyc")
    parser.add_argument("--methods", default="", help="only these build methods, e.g. exe,pyz (naming nuitka runs it without --full)")
    size = parser.add_mutually_exclusive_group()
    size.add_argument("--quick", action="store_true", help="one build per backend (its deploy.default method)")
    size.add_argument("--full", action="store_true", help="also nuitka and a `mode --supports +pypy` round trip")
    parser.add_argument("--gui", choices=("auto", "on", "off"), default="auto", help="GUI runs (raylib: 5 frames); auto skips them with no display or on a Windows/macOS CI runner")
    parser.add_argument("--keep", action="store_true", help="keep the base dir even when everything passes")
    parser.add_argument("--reuse", action="store_true", help="reuse <base>/<preset> kept by an earlier run instead of recreating it")
    parser.add_argument("--json", action="store_true", help="print the results as JSON on stdout")
    parser.add_argument("--base", default="", help="base dir (default: %%TEMP%%\\pt\\e2e on Windows, $TMPDIR/pt-e2e elsewhere)")
    ns = parser.parse_intermixed_args(list(args))  # `raylib --quick flet` works too
    names = [n for chunk in ns.presets for n in chunk.split(",") if n] or list(DEFAULT_PRESETS)
    unknown = [n for n in names if n not in available]
    if unknown:
        raise DeployError(f"selftest --e2e: unknown preset {', '.join(unknown)} (available: {', '.join(available)})")
    return Options(
        presets=tuple(dict.fromkeys(names)),
        backends=_csv(ns.backends, BACKENDS, "--backends"),
        methods=_csv(ns.methods, METHODS, "--methods"),
        quick=ns.quick,
        full=ns.full,
        gui=ns.gui,
        keep=ns.keep,
        reuse=ns.reuse,
        as_json=ns.json,
        base=ns.base,
    )


def scrub_env(environ: Mapping[str, str], drop_dirs: Sequence[str] = ()) -> dict[str, str]:
    """Return a copy of `environ` without SCRUBBED and PYTEMPLATE_* (and `drop_dirs` removed from PATH)."""
    env = {k: v for k, v in environ.items() if k.upper() not in SCRUBBED and not k.upper().startswith("PYTEMPLATE_")}
    drop = {os.path.normcase(os.path.normpath(d)) for d in drop_dirs}
    for key in [k for k in env if k.upper() == "PATH"]:
        parts = [p for p in env[key].split(os.pathsep) if p and os.path.normcase(os.path.normpath(p)) not in drop]
        env[key] = os.pathsep.join(parts)
    return env


def find_artifact(dist: Path, app: str, backend: str, method: str) -> Path | None:
    """Return the non-empty output dir of a build: dist/<app>-<backend>-<method>[-<platform key>]."""
    stem = f"{app}-{backend}-{method}"
    candidates = [dist / stem]
    if method in ("portable", "flet"):  # portable with runtime = "system" has no -<key> suffix
        candidates = sorted(p for p in dist.glob(f"{stem}-*") if p.is_dir()) + candidates
    return next((c for c in candidates if c.is_dir() and any(c.iterdir())), None)


def smoke_target(artifact: Path, app: str, method: str, windows: bool) -> Path | None:
    """Return the file to run (exe, launcher, .pyz) or install (.whl) inside a build's output dir."""
    if method in ("exe", "nuitka"):
        exe = app + (".exe" if windows else "")
        hits = sorted((p for p in artifact.rglob(exe) if p.is_file()), key=lambda p: len(p.parts))
        return hits[0] if hits else None
    names = {"portable": f"{app}.{'cmd' if windows else 'sh'}", "pyz": f"{app}.pyz"}
    if method in names:
        path = artifact / names[method]
        return path if path.is_file() else None
    if method == "wheel":
        return next(iter(sorted(artifact.glob("*.whl"))), None)
    return None


# --- host and project data -----------------------------------------------------------------------


def preset_info(name: str) -> PresetInfo:
    """Read the preset's own pytemplate.toml (the schema's defaults fill in the rest)."""
    from . import config

    data = tomllib.loads((PRESETS / name / "files" / CONFIG_FILE.name).read_text(encoding="utf-8"))
    cfg: Config = config._build(Config, data, "")
    return PresetInfo(name, tuple(cfg.backend.supported), dict(cfg.deploy.default), cfg.app.gui)


def flet_build_reason(os_name: str) -> str:
    flutter = shutil.which("flutter") or next(iter(sorted(Path.home().glob("flutter/*/bin/flutter*"))), None)
    if not flutter:
        return "needs the Flutter SDK (flet build fetches ~1 GB): not in PATH or ~/flutter"
    if os_name == "windows":
        from .methods.flet import _developer_mode

        if not _developer_mode():
            return "needs Windows Developer Mode (Settings > System > For developers)"
    return ""


def detect_host(gui: str) -> Host:
    os_name = host_os()
    display = "--gui off" if gui == "off" else ""
    wrap: tuple[str, ...] = ()
    headless_linux = os_name == "linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if gui != "off" and headless_linux:
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            wrap = (xvfb, "-a", "-s", "-screen 0 1280x720x24")  # depth 24: GLX has no 8-bit visuals
        elif gui == "auto":
            display = "no display (DISPLAY unset) and no xvfb-run"
    elif gui == "auto" and os_name != "linux" and os.environ.get("CI"):
        display = "CI runner without an OpenGL 3.3 context (--gui on forces it)"
    return Host(os_name, display, wrap, flet_build_reason(os_name), host_arch())


def default_base() -> Path:
    """A SHORT path: LongPathsEnabled=0 breaks PyPy runtime copies and Flet client extraction."""
    tmp = Path(tempfile.gettempdir())
    return tmp / "pt" / "e2e" if IS_WINDOWS else tmp / "pt-e2e"


def child_env() -> dict[str, str]:
    """This process's environment, scrubbed, without the runner's own bin/ on PATH but with uv's dir."""
    own = [str(Path(sys.prefix) / ("Scripts" if IS_WINDOWS else "bin"))] if sys.prefix != sys.base_prefix else []
    env = scrub_env(os.environ, own)
    uv_dir = str(Path(proc.find_uv()).parent)
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    if not shutil.which("uv", path=env.get(key, "")):
        env[key] = os.pathsep.join(p for p in (uv_dir, env.get(key, "")) if p)
    return env


# --- running steps --------------------------------------------------------------------------------


@dataclass
class Result:
    preset: str
    step: str
    status: str  # PASS | FAIL | SKIP
    seconds: float = 0.0
    detail: str = ""
    log: str = ""  # relative to the base dir


@dataclass
class Context:
    info: PresetInfo
    base: Path
    project: Path
    logs: Path
    work: Path  # scratch for smoke runs (wheel venvs, pyz cache)
    uv: str
    env: dict[str, str]
    opts: Options
    results: dict[str, str] = field(default_factory=dict)  # step name -> status


def rmtree(path: Path) -> None:
    """Remove a tree even with read-only files (.git objects) and paths over 260 characters."""
    if not path.exists():
        return
    target = "\\\\?\\" + str(path.resolve()) if IS_WINDOWS else str(path)

    def retry(func: Callable[..., object], name: str, exc: object) -> None:
        os.chmod(name, stat.S_IWRITE)
        func(name)

    for attempt in range(5):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(target, onexc=retry)
            else:
                shutil.rmtree(target, onerror=retry)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(1 + attempt)  # a just-exited exe or an antivirus scan may still hold a file


def kill_tree(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)], stdin=subprocess.DEVNULL, capture_output=True, check=False)
    else:
        import signal

        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        child.kill()
    except OSError:
        pass
    child.wait()


def run_logged(argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path, timeout: float) -> tuple[int | None, str]:
    """Run `argv` (stdin closed): stderr goes to `log`, stdout to `log` and the returned text.

    Files, not pipes: a grandchild that outlives its parent cannot hang the wait. The exit
    code is None on a timeout (the whole process tree is killed).
    """
    out_path = log.with_suffix(".out")
    with log.open("ab") as err, out_path.open("w+b") as out:
        err.write(f"$ {proc.show(list(argv))}\n  (in {cwd})\n".encode())
        err.flush()
        child = subprocess.Popen(list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=not IS_WINDOWS)
        code: int | None
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(child)
            code = None
        except BaseException:
            kill_tree(child)
            raise
        out.seek(0)
        data = out.read()
        if data:
            err.write(b"--- stdout ---\n" + data + (b"" if data.endswith(b"\n") else b"\n"))
        err.write(f"--- exit code: {'timeout' if code is None else code}\n\n".encode())
    try:
        out_path.unlink(missing_ok=True)
    except OSError:
        pass  # still held by a killed process's orphan: it goes with the base dir
    return code, data.decode("utf-8", errors="replace")


def call(argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path, timeout: float, expect: Sequence[str] = ()) -> tuple[str, str]:
    code, stdout = run_logged(argv, cwd, env, log, timeout)
    if code is None:
        return FAIL, f"timeout after {timeout:.0f} s"
    if code != 0:
        return FAIL, f"exit code {code}"
    missing = [e for e in expect if e not in stdout]
    if missing:
        return FAIL, f"exit code 0 but the output lacks {missing[0]!r}"
    return PASS, ""


def runner_argv(uv: str, root: Path, args: Sequence[str]) -> list[str]:
    """./deploy ARGS of the project at `root`, the way its launchers run it."""
    return [uv, "run", "--quiet", "--script", str(root / ".pytemplate" / "deploy.py"), *args]


def _log_note(log: Path, text: str) -> None:
    with log.open("a", encoding="utf-8", newline="\n") as f:
        f.write(text.rstrip("\n") + "\n")


def do_new(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    if ctx.opts.reuse and (ctx.project / CONFIG_FILE.name).is_file():
        return SKIP, f"reusing {ctx.project} (--reuse)"
    rmtree(ctx.project)
    argv = runner_argv(ctx.uv, ROOT, ["new", str(ctx.project), "--preset", ctx.info.name, "--name", ctx.info.app])
    return call(argv, ROOT, ctx.env, log, step.timeout)


def do_verify(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    """What `new` must (not) copy, and the exec bit of ./deploy in the new repository."""
    p = ctx.project
    problems: list[str] = []
    notes: list[str] = []
    if (p / ".pytemplate" / "template-repo").exists():
        problems.append("copied .pytemplate/template-repo")
    problems += [f"copied .github/workflows/{w.name}" for w in sorted((p / ".github" / "workflows").glob("template-*"))]
    if (p / ".claude").exists():
        problems.append("copied .claude/")
    if not (p / "deploy").is_file():
        problems.append("no ./deploy launcher")
    git = shutil.which("git")
    if git and (p / ".git").exists():
        r = subprocess.run([git, "ls-files", "-s", "deploy"], cwd=p, env=ctx.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=step.timeout)
        mode = r.stdout.split()[0] if r.stdout.split() else "missing from the index"
        _log_note(log, f"git ls-files -s deploy: {r.stdout.strip() or r.stderr.strip()}")
        if mode != "100755":
            problems.append(f"git mode of ./deploy is {mode}, not 100755")
    else:
        notes.append("no git repository: exec bit not checked")
    _log_note(log, "\n".join(problems + notes) or "all checks passed")
    return (FAIL, "; ".join(problems)) if problems else (PASS, "; ".join(notes))


def do_build(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    status, detail = call(runner_argv(ctx.uv, ctx.project, step.args), ctx.project, ctx.env, log, step.timeout)
    if status != PASS:
        return status, detail
    artifact = find_artifact(ctx.project / "dist", ctx.info.app, step.backend, step.method)
    if artifact is None:
        return FAIL, f"the build passed but dist/{ctx.info.app}-{step.backend}-{step.method}* is missing or empty"
    size = sum(f.stat().st_size for f in artifact.rglob("*") if f.is_file())
    shown = f"{size / 1_048_576:.1f} MB" if size >= 1_048_576 else f"{size / 1024:.0f} KB"
    return PASS, f"{artifact.relative_to(ctx.project).as_posix()} ({shown})"


def runtime_python(ctx: Context, backend: str) -> Path:
    return venv_python(ctx.project / (f".venv-pypy{ENV_SUFFIX}" if backend == "pypy" else f".venv{ENV_SUFFIX}"))


def do_smoke(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    """Run the artifact itself, outside the project's environments."""
    artifact = find_artifact(ctx.project / "dist", ctx.info.app, step.backend, step.method)
    target = smoke_target(artifact, ctx.info.app, step.method, IS_WINDOWS) if artifact else None
    if target is None:
        return FAIL, f"nothing to run in dist/ for {step.method}"
    env = dict(ctx.env)
    commands: list[list[str]] = []
    if step.method in ("exe", "nuitka"):
        commands.append([str(target)])
    elif step.method == "portable":
        commands.append([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(target)] if IS_WINDOWS else [str(target)])
    elif step.method == "pyz":
        # -S: no site-packages, so the .pyz must bring its own dependencies. Its bootstrap
        # extracts to the user cache: point that at the scratch dir.
        cache = ctx.work / "cache"
        env.update({"LOCALAPPDATA": str(cache), "XDG_CACHE_HOME": str(cache)})
        if sys.platform == "darwin":
            env["HOME"] = str(ctx.work / "home")
        commands.append([str(runtime_python(ctx, step.backend)), "-S", str(target)])
    elif step.method == "wheel":
        venv = ctx.work / f"wheel-{step.backend}"
        rmtree(venv)
        script = venv_python(venv).parent / (ctx.info.app + (".exe" if IS_WINDOWS else ""))
        commands += [
            [ctx.uv, "venv", "--quiet", "--python", str(runtime_python(ctx, step.backend)), str(venv)],
            [ctx.uv, "pip", "install", "--quiet", "--python", str(venv_python(venv)), str(target)],
            [str(script)],
        ]
    for i, argv in enumerate(commands):
        last = i == len(commands) - 1
        status, detail = call(argv, ctx.work, env, log, step.timeout, step.expect if last else ())
        if status != PASS:
            return status, detail if last else f"{Path(argv[0]).stem} {argv[1]}: {detail}"
    return PASS, target.relative_to(ctx.project).as_posix()


def execute(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
    if step.kind == "new":
        return do_new(ctx, step, log)
    if step.kind == "verify":
        return do_verify(ctx, step, log)
    if step.kind == "build":
        return do_build(ctx, step, log)
    if step.kind == "smoke":
        return do_smoke(ctx, step, log)
    argv = [*step.wrap, *runner_argv(ctx.uv, ctx.project, step.args)]
    return call(argv, ctx.project, ctx.env, log, step.timeout, step.expect)


def _tail(log: Path, lines: int = 30) -> list[str]:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.rstrip("\n").splitlines()[-lines:]


def run_preset(ctx: Context, steps: list[Step]) -> list[Result]:
    out: list[Result] = []
    blocked = ""
    for i, step in enumerate(steps, 1):
        if step.skip:
            result = Result(step.preset, step.name, SKIP, detail=step.skip)
        elif blocked:
            result = Result(step.preset, step.name, SKIP, detail=f"'{blocked}' failed")
        elif step.after and ctx.results.get(step.after) != PASS:
            result = Result(step.preset, step.name, SKIP, detail=f"needs '{step.after}' to pass")
        else:
            log = ctx.logs / f"{i:02d}-{re.sub(r'[^a-z0-9]+', '-', step.name.lower()).strip('-')[:40]}.log"
            ui.step(f"e2e {step.preset}: {step.name}")
            t0 = time.perf_counter()
            try:
                status, detail = execute(ctx, step, log)
            except (OSError, subprocess.SubprocessError, DeployError) as e:
                status, detail = FAIL, f"{type(e).__name__}: {e}"
                _log_note(log, detail)
            result = Result(step.preset, step.name, status, round(time.perf_counter() - t0, 1), detail, log.relative_to(ctx.base).as_posix())
            ui.info(f"     {status} in {result.seconds:.1f} s" + (f": {detail}" if detail else ""))
            if status == FAIL:
                for line in _tail(log):
                    ui.info(f"     | {line}")
                ui.info(f"     full log: {log}")
        ctx.results[step.name] = result.status
        if result.status == FAIL and step.required:
            blocked = step.name
        out.append(result)
    return out


# --- entry point ----------------------------------------------------------------------------------


def _prepare_base(base: Path) -> None:
    if base.resolve() == ROOT or ROOT in base.resolve().parents:
        raise DeployError("selftest --e2e: the base dir cannot be inside this template")
    if base.exists() and not base.is_dir():
        raise DeployError(f"selftest --e2e: {base} is not a directory")
    if base.is_dir() and any(base.iterdir()) and not (base / MARKER).is_file():
        raise DeployError(f"selftest --e2e: {base} is not empty and was not made by selftest --e2e (no {MARKER}): pick another --base")
    base.mkdir(parents=True, exist_ok=True)
    (base / MARKER).write_text("Made by ./deploy selftest --e2e: safe to delete.\n", encoding="utf-8", newline="\n")


def _cleanup(base: Path, presets: Sequence[str]) -> None:
    """Remove what this run made (other presets kept by an earlier --keep stay), then the base if empty."""
    try:
        for p in presets:
            for d in (base / p, base / "logs" / p, base / "work" / p):
                rmtree(d)
        for d in (base / "logs", base / "work"):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        if {x.name for x in base.iterdir()} <= {MARKER}:
            rmtree(base)
    except OSError as e:
        ui.warn(f"could not remove {base}: {e}")


def _print_table(results: list[Result], seconds: float) -> None:
    ui.step("e2e results")
    width = max([len(r.step) for r in results] + [4])
    ui.info(f"  {'preset':<8} {'step':<{width}}  result     time  detail")
    for r in results:
        time_text = f"{r.seconds:.1f}s" if r.status != SKIP else "-"
        ui.info(f"  {r.preset:<8} {r.step:<{width}}  {r.status:<6} {time_text:>8}  {r.detail}".rstrip())
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, FAIL, SKIP)}
    minutes, secs = divmod(int(seconds), 60)
    ui.info(f"  total: {counts[PASS]} PASS, {counts[FAIL]} FAIL, {counts[SKIP]} SKIP in {minutes}m{secs:02d}s")


def selftest(cfg: Config, args: list[str]) -> int:
    """selftest --e2e [PRESET ...] [--backends ..] [--methods ..] [--quick|--full] [--gui ..] [--keep] [--reuse] [--json] [--base DIR]"""
    from . import presets

    opts = parse_args(args, presets.available())
    base = user_path(opts.base) if opts.base else default_base()
    _prepare_base(base)
    host = detect_host(opts.gui)
    env = child_env()
    uv = proc.find_uv()
    ui.step(f"selftest --e2e: {', '.join(opts.presets)} in {base}" + (" (--quick)" if opts.quick else " (--full)" if opts.full else ""))
    results: list[Result] = []
    interrupted = False
    t0 = time.perf_counter()
    try:
        for name in opts.presets:
            info = preset_info(name)
            ctx = Context(info, base, base / name, base / "logs" / name, base / "work" / name, uv, env, opts)
            for d in (ctx.logs, ctx.work):
                rmtree(d)
                d.mkdir(parents=True)
            results += run_preset(ctx, plan(info, opts, host))
    except KeyboardInterrupt:
        interrupted = True
    seconds = time.perf_counter() - t0
    failed = interrupted or any(r.status == FAIL for r in results)
    if results:
        _print_table(results, seconds)
    kept = failed or opts.keep
    if kept:
        ui.info(f"kept for inspection: {base}  (logs in {base / 'logs'})")
    else:
        _cleanup(base, opts.presets)
    if opts.as_json:
        report = {
            "ok": not failed,
            "interrupted": interrupted,
            "base": str(base),
            "kept": kept,
            "seconds": round(seconds, 1),
            "host": asdict(host),
            "options": asdict(opts),
            "results": [asdict(r) for r in results],
        }
        print(json.dumps(report, indent=2))
    if interrupted:
        ui.error("interrupted")
        return 130
    if failed:
        ui.error("selftest --e2e: some steps failed (table above)")
        return 1
    ui.ok("selftest --e2e: everything passed")
    return 0
