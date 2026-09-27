"""Running parts of `./deploy selftest --e2e` (runner/e2e.py), without a real project or build.

Logged steps (output, exit code, timeout that kills the whole tree), the outcome of a step, the
base-dir guards and cleanup, the step kinds on small fake projects (verify, commit, smoke of a
moved portable folder, build size, option edit, round-trip snapshots), the host detection, and
the whole suite with its steps faked: exit codes (0, 1 on any FAIL, 2 for a selection that tests
nothing, 130 interrupted), the JSON report, and SIGTERM/SIGHUP ending the running step.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath
from typing import cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config, e2e, presets  # noqa: E402
from runner.e2e import FAIL, PASS, SKIP, Context, Host, Options, PresetInfo, Step  # noqa: E402
from runner.hooks import MARKER as HOOK_MARKER  # noqa: E402
from runner.project import ROOT  # noqa: E402
from runner.ui import DeployError  # noqa: E402

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX processes, signals and symlinks")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")
SCRIPT = PresetInfo("script", ("cpython", "mypyc"), {"cpython": "exe", "mypyc": "exe"}, gui=False, tasks=("ci",))
HOST = Host("linux", arch="x86_64")


def make_ctx(tmp_path: Path, info: PresetInfo = SCRIPT, env: dict[str, str] | None = None, opts: Options = Options()) -> Context:
    base = tmp_path / "base"
    ctx = Context(info, base, base / info.name, base / "logs" / info.name, base / "work" / info.name, "uv", dict(os.environ) if env is None else env, opts)
    for d in (ctx.project, ctx.logs, ctx.work):
        d.mkdir(parents=True, exist_ok=True)
    return ctx


def isolated_git_env(base: Path) -> dict[str, str]:
    env = e2e.scrub_env(os.environ)
    e2e.isolate_git(env, base)
    return env


def gone(pid: int, wait: float = 10.0) -> bool:
    """True once `pid` runs no more (a zombie waiting for its parent counts as gone)."""
    deadline = time.monotonic() + wait
    while True:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        stat = Path(f"/proc/{pid}/stat")
        if stat.is_file():
            try:
                if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
            except OSError:
                return True
        elif shutil.which("ps"):
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
            if not state or state.startswith("Z"):
                return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)


# --- logged steps ----------------------------------------------------------------------------------


def test_run_logged_records_the_command_its_output_and_its_exit_code(tmp_path: Path) -> None:
    log = tmp_path / "step.log"
    code, out = e2e.run_logged([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], tmp_path, os.environ, log, 60)
    assert code == 3 and out.strip() == "out"
    text = log.read_text(encoding="utf-8")
    assert text.startswith("$ ") and f"(in {tmp_path})" in text and "err" in text
    assert "--- stdout ---\nout" in text.replace("\r\n", "\n") and text.rstrip().endswith("--- exit code: 3")
    assert not log.with_suffix(".out").exists(), "the stdout file goes once it is in the log"


def test_run_logged_timeout_kills_the_whole_tree(tmp_path: Path) -> None:
    """A step that hangs is killed with everything it started (a build's compilers)."""
    pidfile = tmp_path / "grandchild.pid"
    parent = "\n".join(
        [
            "import subprocess, sys, time",
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])",
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))",
            "print('started', flush=True)",
            "time.sleep(120)",
        ]
    )
    t0 = time.monotonic()
    code, out = e2e.run_logged([sys.executable, "-c", parent], tmp_path, os.environ, tmp_path / "step.log", 5)
    assert code is None and "started" in out
    assert time.monotonic() - t0 < 60
    assert (tmp_path / "step.log").read_text(encoding="utf-8").rstrip().endswith("--- exit code: timeout")
    if sys.platform != "win32":  # Windows: a handle of the killed tree may hold .out a moment (left for the cleanup); os.kill(pid, 0) would terminate a process
        assert not (tmp_path / "step.out").exists()
        assert gone(int(pidfile.read_text())), "the grandchild outlived the timeout"


@pytest.mark.parametrize(
    "result, kw, expected",
    [
        ((0, "Primes: 2 3 5"), {"expect": ("Primes",)}, (PASS, "")),
        ((0, "nothing"), {"expect": ("Primes",)}, (FAIL, "exit code 0 but the output lacks 'Primes'")),
        ((3, ""), {}, (FAIL, "exit code 3")),
        ((None, ""), {}, (FAIL, "timeout after 60 s")),
        ((2, ""), {"code_ok": 2}, (PASS, "")),
        ((0, ""), {"code_ok": 2}, (FAIL, "exit code 0, expected 2")),
    ],
)
def test_call_outcomes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: tuple[int | None, str], kw: dict[str, object], expected: tuple[str, str]) -> None:
    monkeypatch.setattr(e2e, "run_logged", lambda *a, **k: result)
    assert e2e.call(["x"], tmp_path, {}, tmp_path / "log", 60, **kw) == expected  # type: ignore[arg-type]


# --- base dir ---------------------------------------------------------------------------------------


def test_prepare_base_refuses_what_it_must_not_wipe(tmp_path: Path) -> None:
    for inside in (ROOT, ROOT / "x" / "y"):
        with pytest.raises(DeployError, match="inside this template"):
            e2e._prepare_base(inside)
    assert not (ROOT / "x").exists()
    a_file = tmp_path / "file"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(DeployError, match="not a directory"):
        e2e._prepare_base(a_file)
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DeployError, match="not empty"):
        e2e._prepare_base(mine)
    assert not (mine / e2e.MARKER).exists()
    fresh = tmp_path / "a" / "b"
    e2e._prepare_base(fresh)
    assert (fresh / e2e.MARKER).is_file()
    (fresh / "script").mkdir()
    e2e._prepare_base(fresh)  # a base an earlier run made
    if sys.platform != "win32":
        link = tmp_path / "link"
        link.symlink_to(ROOT / ".pytemplate", target_is_directory=True)
        with pytest.raises(DeployError, match="inside this template"):
            e2e._prepare_base(link / "e2e")


def test_cleanup_removes_this_runs_presets_only(tmp_path: Path) -> None:
    base = tmp_path / "base"
    e2e._prepare_base(base)
    for d in ("script/src", "raylib/src", "logs/script", "logs/raylib", "work/script"):
        (base / d).mkdir(parents=True)
    e2e._cleanup(base, ["script"])
    assert sorted(p.name for p in base.iterdir()) == sorted([e2e.MARKER, "logs", "raylib"])
    assert (base / "logs" / "raylib").is_dir()
    e2e._cleanup(base, ["raylib"])
    assert not base.exists(), "nothing but the marker was left"


# --- step kinds on fake projects ---------------------------------------------------------------------


def test_failures_block_what_depends_on_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failing = {"build cpython exe"}

    def fake_execute(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
        log.write_text("line 1\nline 2\n", encoding="utf-8")
        return (FAIL, "exit code 1") if step.name in failing else (PASS, "")

    monkeypatch.setattr(e2e, "execute", fake_execute)

    def run() -> dict[str, str]:
        ctx = make_ctx(tmp_path)
        return {r.step: r.status for r in e2e.run_preset(ctx, e2e.plan(SCRIPT, Options(), HOST))}

    status = run()
    assert status["build cpython exe"] == FAIL and status["smoke cpython exe"] == SKIP
    assert status["build cpython pyz"] == PASS and status["smoke cpython pyz"] == PASS
    assert status["build cpython nuitka"] == SKIP
    failing = {"setup"}  # required: the rest of the preset is skipped
    status = run()
    assert status["new"] == PASS and status["setup"] == FAIL
    after_setup = list(status)[list(status).index("setup") + 1 :]
    assert after_setup and all(status[s] == SKIP for s in after_setup)
    failing = {"first commit"}  # the rename needs its clean tree, the way back its own commit
    status = run()
    assert status["rename e2escript-2 (dry run)"] == PASS and status["rename e2escript-2"] == SKIP
    assert status["rename e2escript (back)"] == SKIP


def test_an_interrupt_keeps_the_rows_so_far(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_execute(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
        if step.name == "doctor":
            raise KeyboardInterrupt
        return PASS, ""

    monkeypatch.setattr(e2e, "execute", fake_execute)
    into: list[e2e.Result] = []
    with pytest.raises(KeyboardInterrupt):
        e2e.run_preset(make_ctx(tmp_path), e2e.plan(SCRIPT, Options(quick=True), HOST), into)
    assert [r.step for r in into] == ["new", "verify copy", "pristine skeleton", "render --check", "setup", "doctor"]
    assert into[-1].status == FAIL and into[-1].detail == "interrupted"


def test_a_round_trip_must_restore_the_projects_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx(tmp_path)
    (ctx.project / "pytemplate.toml").write_text("before", encoding="utf-8")
    writes = {"there": "after", "back": "before", "broken back": "other", "same": None}

    def fake_execute(c: Context, step: Step, log: Path) -> tuple[str, str]:
        text = writes[step.name]
        if text is not None:
            (c.project / "pytemplate.toml").write_text(text, encoding="utf-8")
        return PASS, ""

    monkeypatch.setattr(e2e, "_execute", fake_execute)
    log = ctx.logs / "step.log"
    assert e2e.execute(ctx, Step("script", "there", "deploy", snapshot="there"), log) == (PASS, "")
    assert e2e.execute(ctx, Step("script", "back", "deploy", restores="there"), log) == (PASS, "")
    status, detail = e2e.execute(ctx, Step("script", "broken back", "deploy", restores="there"), log)
    assert status == FAIL and detail == "the project changed since 'there': ~ pytemplate.toml"
    assert "~ pytemplate.toml" in log.read_text(encoding="utf-8")
    (ctx.project / "pytemplate.toml").write_text("before", encoding="utf-8")
    assert e2e.execute(ctx, Step("script", "same", "deploy", snapshot="same", restores="same"), log) == (PASS, "")
    status, detail = e2e.execute(ctx, Step("script", "same", "deploy", restores="missing"), log)
    assert status == FAIL and "bug" in detail


@needs_git
def test_verify_checks_what_new_made(tmp_path: Path) -> None:
    base = tmp_path / "base"
    # The preset of the project the suite runs in: its uv.lock holds that preset's tested pins
    info = e2e.preset_info(config.load(set()).app.preset)
    ctx = make_ctx(tmp_path, info, env=isolated_git_env(base))
    p = ctx.project
    for launcher in ("deploy", "deploy.ps1"):
        (p / launcher).write_text("#!/bin/sh\n", encoding="utf-8")
    shutil.copyfile(presets.LOCK, p / "uv.lock")  # the template's own versions
    description = str(presets.load(info.name)["description"])
    (p / "pyproject.toml").write_text(f"[project]\nname = \"{info.app}\"\ndescription = {json.dumps(description)}\n", encoding="utf-8")
    (p / "README.md").write_text(f"# {info.app}\n", encoding="utf-8")
    template_repo = (ROOT / ".pytemplate" / "template-repo").is_file()
    for name, target in presets.TEMPLATE_DOCS.items():
        source = ROOT / (name if template_repo else target)
        if source.is_file():
            (p / target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, p / target)
    git = ["git", "-C", str(p)]
    subprocess.run([*git, "init", "-q"], check=True, env=ctx.env)
    subprocess.run([*git, "add", "--chmod=+x", "deploy", "deploy.ps1"], check=True, env=ctx.env)
    ctx.results["new"] = PASS
    (p / ".build" / "init").mkdir(parents=True)  # __init's own scratch (the pins it hands to uv)
    (p / ".build" / "init" / "constraints.txt").write_text("raylib==6.0.1.0\n", encoding="utf-8")
    step = Step(info.name, "verify copy", "verify", timeout=60)
    log = ctx.logs / "verify.log"
    assert e2e.do_verify(ctx, step, log) == (PASS, ""), log.read_text(encoding="utf-8")
    subprocess.run([*git, "update-index", "--chmod=-x", "deploy.ps1"], check=True, env=ctx.env)
    (p / ".venv").mkdir()
    (p / ".build" / "cfg").mkdir()  # not made by __init: copied from the template
    (p / "LICENSE").write_text("MIT\n", encoding="utf-8")
    status, detail = e2e.do_verify(ctx, step, log)
    assert status == FAIL
    assert "git mode of deploy.ps1 is 100644, not 100755" in detail and "LICENSE in the project root" in detail
    assert "copied .venv/" in detail and "copied .build/" in detail
    e2e.rmtree(p / ".git")  # git objects are read-only on Windows
    assert "no git repository (new runs git init)" in e2e.do_verify(ctx, step, log)[1]
    ctx.results["new"] = SKIP
    assert e2e.do_verify(ctx, step, log)[0] == SKIP, "--reuse: nothing new to verify"


@POSIX
def test_cleanup_of_a_symlinked_base_never_touches_the_folder_it_names(tmp_path: Path) -> None:
    # rmtree(link) called the retry hook with os.path.islink: its chmod followed the link and
    # made the real folder 0o200, and the next run as a normal user died with a traceback
    import stat

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (real / e2e.MARKER).write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    e2e._cleanup(link, ["script"])
    assert stat.S_IMODE(real.stat().st_mode) == 0o700
    assert not os.path.lexists(link) and real.is_dir() and not any(real.iterdir())  # the link and the marker went
    (real / "keep").write_text("k", encoding="utf-8")
    link.symlink_to(real, target_is_directory=True)
    e2e.rmtree(link)  # a link goes as a link
    assert not os.path.lexists(link) and (real / "keep").is_file() and stat.S_IMODE(real.stat().st_mode) == 0o700


def test_rmtree_reaches_a_base_on_a_network_share(monkeypatch: pytest.MonkeyPatch) -> None:
    # It removed \\?\\\server\share\... (no valid name) for a --base on a share or a mapped drive
    from runner.methods import portable

    class Share:
        def exists(self) -> bool:
            return True

        def resolve(self) -> PureWindowsPath:
            return PureWindowsPath(r"\\server\share\pt\e2e")

    from runner import cmd_env

    removed: list[str] = []
    monkeypatch.setattr(cmd_env, "_is_link", lambda path: False)  # never asks the (made-up) share
    monkeypatch.setattr(e2e, "IS_WINDOWS", True)
    monkeypatch.setattr(portable, "IS_WINDOWS", True)
    monkeypatch.setattr(e2e.shutil, "rmtree", lambda target, **kwargs: removed.append(target))
    e2e.rmtree(cast(Path, Share()))
    assert removed == [r"\\?\UNC\server\share\pt\e2e"]


@needs_git
def test_commit_goes_through_the_projects_hook(tmp_path: Path) -> None:
    base = tmp_path / "base"
    ctx = make_ctx(tmp_path, env=isolated_git_env(base))
    subprocess.run(["git", "-C", str(ctx.project), "init", "-q"], check=True, env=ctx.env)
    hook = ctx.project / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)

    def write_hook(code: int, marker: str = HOOK_MARKER) -> None:
        hook.write_text(f"#!/bin/sh\n# {marker}\nexit {code}\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)

    first = Step("script", "first commit", "commit", ("First commit",), timeout=60)
    log = ctx.logs / "commit.log"
    ctx.results["new"] = PASS
    (ctx.project / "app.py").write_text("x = 1\n", encoding="utf-8")
    write_hook(0, marker="another tool's hook")
    status, detail = e2e.do_commit(ctx, first, log)
    assert status == FAIL and "setup installed no pytemplate pre-commit hook" in detail
    write_hook(1)
    assert e2e.do_commit(ctx, first, log) == (FAIL, "the pre-commit hook or git refused the commit (see the log)")
    write_hook(0)
    assert e2e.do_commit(ctx, first, log) == (PASS, "")
    subject = subprocess.run(["git", "-C", str(ctx.project), "log", "-1", "--format=%s"], capture_output=True, text=True, check=True, env=ctx.env)
    assert subject.stdout.strip() == "First commit"
    assert e2e.do_commit(ctx, first, log) == (FAIL, "nothing to commit"), "the step before must have changed something"
    ctx.results["new"] = SKIP
    assert e2e.do_commit(ctx, first, log)[0] == PASS, "--reuse: committed by an earlier run"


@POSIX
def test_a_portable_folder_runs_from_another_path(tmp_path: Path) -> None:
    """A portable build must work wherever it is copied: the smoke run moves the folder away
    (the build path is gone while it runs) and puts it back afterwards."""
    ctx = make_ctx(tmp_path)
    folder = ctx.project / "dist" / "e2escript-cpython-portable-cp314-linux-x86_64"
    folder.mkdir(parents=True)
    launcher = folder / "e2escript.sh"
    launcher.write_text(f'#!/bin/sh\n[ -e {shlex.quote(str(folder))} ] && exit 7\necho "Primes from ${{0%/*}}"\n', encoding="utf-8")
    launcher.chmod(0o755)
    step = Step("script", "smoke cpython portable", "smoke", backend="cpython", method="portable", expect=("Primes",), timeout=60)
    log = ctx.logs / "smoke.log"
    status, detail = e2e.do_smoke(ctx, step, log)
    assert (status, detail) == (PASS, "dist/e2escript-cpython-portable-cp314-linux-x86_64/e2escript.sh (run from another folder)"), log.read_text(encoding="utf-8")
    assert launcher.is_file(), "the folder is back in dist/"
    assert f"Primes from {ctx.work / 'moved' / folder.name}" in log.read_text(encoding="utf-8")
    launcher.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    assert e2e.do_smoke(ctx, step, log) == (FAIL, "exit code 3")
    assert launcher.is_file(), "back in dist/ after a failure too"


@POSIX
def test_build_size_counts_a_symlinked_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bundled runtime's bin/python3 -> python3.14: the size line counted it twice."""
    ctx = make_ctx(tmp_path)
    folder = ctx.project / "dist" / "e2escript-cpython-portable-cp314-linux-x86_64"
    (folder / "runtime" / "bin").mkdir(parents=True)
    (folder / "runtime" / "bin" / "python3.14").write_bytes(b"x" * 2 * 1_048_576)
    (folder / "runtime" / "bin" / "python3").symlink_to("python3.14")
    monkeypatch.setattr(e2e, "call", lambda *a, **k: (PASS, ""))
    step = Step("script", "build cpython portable", "build", ("build", "cpython", "--method", "portable", "--no-check"), backend="cpython", method="portable")
    assert e2e.do_build(ctx, step, ctx.logs / "b.log") == (PASS, "dist/e2escript-cpython-portable-cp314-linux-x86_64 (2.0 MB)")
    renamed = Step("script", "build cpython pyz (x)", "build", ("build",), backend="cpython", method="pyz", app="e2escript-2")
    assert e2e.do_build(ctx, renamed, ctx.logs / "b.log") == (FAIL, "the build passed but dist/e2escript-2-cpython-pyz* is missing or empty")


def test_an_option_edit_is_applied_and_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[preset.raylib] package = raylib_sdl through `./deploy apply`: pyproject.toml and uv.lock
    must follow (the new requirement declared and locked at its version, the old one gone)."""
    raylib = PresetInfo("raylib", ("cpython", "pypy", "mypyc"), {}, gui=True, active="pypy")
    ctx = make_ctx(tmp_path, info=raylib)
    config_text = '[app]\nname = "e2e-raylib"\n\n[preset.raylib]\npackage = "raylib"   # raylib | raylib_sdl\nversion = "6.0.1.0"\n'
    (ctx.project / "pytemplate.toml").write_text(config_text, encoding="utf-8")
    (ctx.project / "pyproject.toml").write_text('[project]\nname = "e2e-raylib"\ndependencies = ["raylib==6.0.1.0"]\n', encoding="utf-8")
    (ctx.project / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "raylib"\nversion = "6.0.1.0"\n', encoding="utf-8")
    applied: list[list[str]] = []

    def fake_apply(argv: list[str], *a: object, **k: object) -> tuple[str, str]:
        applied.append(argv)
        (ctx.project / "pyproject.toml").write_text('[project]\nname = "e2e-raylib"\ndependencies = ["raylib-sdl==6.0.1.0"]\n', encoding="utf-8")
        (ctx.project / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "raylib-sdl"\nversion = "6.0.1.0"\n', encoding="utf-8")
        return PASS, ""

    monkeypatch.setattr(e2e, "call", fake_apply)
    step = e2e.option_edit(raylib)[0]
    assert step.args == ("preset.raylib", "package", "raylib_sdl")
    assert e2e.do_option(ctx, step, ctx.logs / "o.log") == (PASS, "raylib-sdl==6.0.1.0")
    assert applied and applied[0][-1] == "apply"
    assert 'package = "raylib_sdl"   # raylib | raylib_sdl' in (ctx.project / "pytemplate.toml").read_text(encoding="utf-8")
    (ctx.project / "pytemplate.toml").write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(e2e, "call", lambda *a, **k: (PASS, ""))  # an apply that changes nothing
    (ctx.project / "pyproject.toml").write_text('[project]\nname = "e2e-raylib"\ndependencies = ["raylib==6.0.1.0"]\n', encoding="utf-8")
    (ctx.project / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "raylib"\nversion = "6.0.1.0"\n', encoding="utf-8")
    status, detail = e2e.do_option(ctx, step, ctx.logs / "o.log")
    assert status == FAIL and "pyproject.toml declares raylib-sdl nowhere, not ==6.0.1.0" in detail and "uv.lock still locks raylib" in detail


def test_move_retries_a_locked_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: a folder just built can stay locked a moment (an antivirus scan, an exe that just
    exited): the portable smoke's move away and back retries before it fails the step."""
    src, dst = tmp_path / "built", tmp_path / "moved"
    src.mkdir()
    replace = os.replace
    calls: list[str] = []

    def locked_twice(a: str, b: str) -> None:
        calls.append(str(a))
        if len(calls) < 3:
            raise PermissionError(13, "The process cannot access the file")
        replace(a, b)

    monkeypatch.setattr(e2e.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(e2e.os, "replace", locked_twice)
    e2e._move(src, dst)
    assert dst.is_dir() and not src.exists() and len(calls) == 3

    def always_locked(a: str, b: str) -> None:
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(e2e.os, "replace", always_locked)
    with pytest.raises(PermissionError):
        e2e._move(dst, src)


# --- the whole suite, steps faked --------------------------------------------------------------------


@pytest.fixture
def faked(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, object], list[str]]:
    """selftest --e2e with every step faked: outcomes[step name] is its (status, detail), or an
    exception to raise; `ran` lists the steps that ran."""
    outcomes: dict[str, object] = {}
    ran: list[str] = []

    def fake_execute(ctx: Context, step: Step, log: Path) -> tuple[str, str]:
        ran.append(step.name)
        log.write_text(step.name + "\n", encoding="utf-8")
        outcome = outcomes.get(step.name, (PASS, ""))
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, tuple)
        return outcome

    monkeypatch.setattr(e2e, "execute", fake_execute)
    monkeypatch.setattr(e2e, "detect_host", lambda gui: HOST)
    monkeypatch.setattr(e2e, "hidden_template_repository", lambda env: "")
    monkeypatch.setattr(e2e.proc, "find_uv", lambda: sys.executable)
    return outcomes, ran


def test_selftest_exit_codes_and_json_report(tmp_path: Path, faked: tuple[dict[str, object], list[str]], capsys: pytest.CaptureFixture[str]) -> None:
    outcomes, ran = faked
    base = tmp_path / "base"
    argv = ["script", "--quick", "--json", "--base", str(base)]
    assert e2e.selftest(None, argv) == 0  # type: ignore[arg-type]
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"ok", "interrupted", "base", "kept", "seconds", "host", "options", "results"}
    assert report["ok"] is True and report["interrupted"] is False and report["kept"] is False
    assert set(report["results"][0]) == {"preset", "step", "status", "seconds", "detail", "log"}
    assert [r["step"] for r in report["results"]][:3] == ["new", "verify copy", "pristine skeleton"]
    assert report["results"][0]["log"] == "logs/script/01-new.log"
    assert not base.exists(), "everything passed: the base goes"
    outcomes["check all"] = (FAIL, "exit code 1")
    assert e2e.selftest(None, argv) == 1  # type: ignore[arg-type]
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["kept"] is True and (base / e2e.MARKER).is_file()
    assert {r["step"]: r["status"] for r in report["results"]}["check all"] == FAIL
    outcomes["check all"] = (PASS, "")
    outcomes["setup"] = (FAIL, "exit code 2")  # required: the rest SKIPs, the run still fails
    assert e2e.selftest(None, argv) == 1  # type: ignore[arg-type]
    statuses = [r["status"] for r in json.loads(capsys.readouterr().out)["results"]]
    assert statuses[statuses.index(FAIL) + 1 :] and set(statuses[statuses.index(FAIL) + 1 :]) == {SKIP}


def test_quiet_keeps_the_results_of_a_failed_run(tmp_path: Path, faked: tuple[dict[str, object], list[str]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # -q hid the table, the failed step, its log and the kept base: the only line said
    # "some steps failed (table above)" about a table that was never printed
    outcomes, _ = faked
    outcomes["check all"] = (FAIL, "exit code 1")
    monkeypatch.setattr(e2e.ui, "QUIET", True)
    base = tmp_path / "base"
    assert e2e.selftest(None, ["script", "--quick", "--base", str(base)]) == 1  # type: ignore[arg-type]
    err = capsys.readouterr().err
    row = next(line for line in err.splitlines() if line.split()[:2] == ["script", "check"])
    assert "FAIL" in row and "exit code 1" in row and "total: " in err  # the table
    assert "FAIL in " in err and "(script: check all)" in err and "full log: " in err and "check-all.log" in err
    assert f"kept for inspection: {base}" in err
    assert "PASS in " not in err and "==> " not in err  # progress stays hidden


def test_selftest_refuses_a_selection_that_tests_nothing(tmp_path: Path, faked: tuple[dict[str, object], list[str]]) -> None:
    _, ran = faked
    base = tmp_path / "base"
    for argv in (["script", "--backends", "pypy"], ["script", "--quick", "--methods", "nuitka"]):
        with pytest.raises(DeployError, match="selects no") as e:
            e2e.selftest(None, [*argv, "--base", str(base)])  # type: ignore[arg-type]
        assert e.value.code == 2
    assert not base.exists() and not ran, "refused before anything is created"
    with pytest.raises(SystemExit):  # argparse: exit 2
        e2e.selftest(None, ["--quick", "--full", "--base", str(base)])  # type: ignore[arg-type]


def test_selftest_interrupted_returns_130_and_keeps_the_base(tmp_path: Path, faked: tuple[dict[str, object], list[str]], capsys: pytest.CaptureFixture[str]) -> None:
    outcomes, _ = faked
    outcomes["test all"] = KeyboardInterrupt()
    base = tmp_path / "base"
    assert e2e.selftest(None, ["script", "--quick", "--json", "--base", str(base)]) == 130  # type: ignore[arg-type]
    report = json.loads(capsys.readouterr().out)
    assert report["interrupted"] is True and report["ok"] is False and report["kept"] is True
    assert report["results"][-1]["step"] == "test all" and report["results"][-1]["detail"] == "interrupted"
    assert base.is_dir()


@POSIX
def test_termination_handlers_are_restored() -> None:
    import signal

    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    with e2e.termination_as_interrupt():
        assert all(signal.getsignal(sig) != handler for sig, handler in before.items())
    assert {sig: signal.getsignal(sig) for sig in before} == before


@POSIX
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_termination_signal_kills_the_running_step(tmp_path: Path, signame: str) -> None:
    """SIGTERM/SIGHUP (`timeout`, a closed terminal, `kill`, uv forwarding either) end the running
    step, which lives in its own session, and the run reports 'interrupted' (130) like Ctrl+C.
    Without the handlers the runner died at once and the step (a build) went on as an orphan."""
    import signal

    pidfile = tmp_path / "step.pid"
    step = f"import os, pathlib, time; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(120)"
    script = "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})",
            "from runner import e2e, proc",
            "proc.find_uv = lambda: sys.executable",
            "e2e.detect_host = lambda gui: e2e.Host('linux')",
            "e2e.hidden_template_repository = lambda env: ''",
            "def fake_run_preset(ctx, steps, into=None):",
            f"    e2e.run_logged([sys.executable, '-c', {step!r}], ctx.work, ctx.env, ctx.logs / 'step.log', 300)",
            "    return []",
            "e2e.run_preset = fake_run_preset",
            f"sys.exit(e2e.selftest(None, ['script', '--quick', '--base', {str(tmp_path / 'base')!r}]))",
        ]
    )
    log = tmp_path / "runner.log"
    with log.open("wb") as out:
        runner = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=out, stderr=out)
    child = 0
    try:
        deadline = time.monotonic() + 60
        while not (pidfile.is_file() and pidfile.read_text()):
            assert runner.poll() is None, log.read_text()
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        child = int(pidfile.read_text())
        runner.send_signal(getattr(signal, signame))
        assert runner.wait(timeout=60) == 130, log.read_text()
        assert "interrupted" in log.read_text()
        assert gone(child), "the step outlived the runner"
    finally:
        if runner.poll() is None:
            runner.kill()
        if child and not gone(child, wait=0):
            os.kill(child, signal.SIGKILL)
