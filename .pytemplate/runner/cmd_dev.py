"""Development commands: run, check, lint, fmt, test, report."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import shlex
import tomllib
import webbrowser
from pathlib import Path
from typing import Any

from . import envs, lintc, mypyc, proc, render, ui
from .config import BACKENDS, Config
from .project import BUILD, ROOT, SRC, code_dirs, rel
from .ui import PytError

# basedpyright is not in uv.lock (`uv run --with`; the VS Code extension ships its own), so it
# is pinned here to keep `check` reproducible: the latest release on PyPI in September 2026.
# Bump it deliberately.
BASEDPYRIGHT = "basedpyright==1.40.1"
# basedpyright always runs on the Node.js of its only dependency, nodejs-wheel-binaries
# (`>=20.13.1`, never a system Node): pinned too, or every new Node LTS on PyPI would change
# `check` (and its glibc/macOS floor) on an untouched project. What 1.40.1 resolved to in
# September 2026; bump both together.
BASEDPYRIGHT_NODE = "nodejs-wheel-binaries==24.19.0"


def only_flags(command: str, args: list[str], allowed: tuple[str, ...]) -> set[str]:
    """Return the flags given, rejecting anything else (a typo must never be silently ignored)."""
    unknown = [a for a in args if a not in allowed]
    if unknown:
        valid = f"valid: {' '.join(allowed)}" if allowed else "it takes no arguments"
        raise PytError(f"{command}: unrecognized arguments: {' '.join(unknown)}  ({valid})", 2)
    return set(args)


def split_backend(cfg: Config, args: list[str], *, allow_all: bool = False) -> tuple[str, list[str]]:
    """Take the first argument as the backend only if it is a valid one; pass the rest through as-is."""
    if args and (args[0] in BACKENDS or (allow_all and args[0] == "all")):
        return args[0], args[1:]
    return cfg.backend.active, args


def _profile_file(cfg: Config, profile: str, kind: str) -> Path:
    """Write the config of a specific profile to .build/cfg/ (it may not be the editor's active one).
    The ruff one holds paths relative to ROOT, where every caller starts ruff (`config_arg`)."""
    out = BUILD / "cfg" / f"{kind}-{profile}.{'ini' if kind == 'mypy' else 'toml'}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if kind == "mypy":
        text = render.mypy_ini(cfg, profile)
    else:
        text = render.to_toml(render.ruff_config(cfg, profile, relative_to=ROOT)) + "\n"
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


def config_arg(path: Path) -> str:
    """The --config argument of a ruff started in ROOT (proc.run's default folder): relative to
    it, because ruff expands $NAME and ${NAME} in that argument too (section 15.1): absolute, it
    named no file in a project folder such as app$v2, and check, build and the hook failed."""
    return render.relative_path(path, ROOT)


# --- run -----------------------------------------------------------------------------------------


def cmd_run(cfg: Config, args: list[str]) -> int:
    """run [BACKEND] [app args...]"""
    backend, rest = split_backend(cfg, args)
    envs.ensure_supported(cfg, backend)
    env = envs.runtime_env(cfg, backend)
    if backend == "mypyc":
        stage = mypyc.build(cfg, "dev")
        ui.step(f"running {rel(stage / 'main.py')} (mypyc)")
        return envs.uv_run(env, ["python", stage / "main.py", *rest], check=False).returncode
    ui.step(f"running src/main.py ({backend})")
    return envs.uv_run(env, ["python", SRC / "main.py", *rest], check=False).returncode


def cmd_compile(cfg: Config, args: list[str]) -> int:
    """compile [--release]: build the mypyc stage without running it (debuggers, editors)."""
    parser = argparse.ArgumentParser(prog="./pyt compile")
    parser.add_argument("--release", action="store_true", help="the release stage (asserts stripped per deploy.optimize)")
    ns = parser.parse_args(args)
    if not cfg.supports("mypyc"):
        raise PytError("compile: 'mypyc' is not in backend.supported")
    stage = mypyc.build(cfg, "release" if ns.release else "dev")
    if proc.DRY_RUN:
        ui.info(f"(--dry-run) would compile the stage: {rel(stage)}")
    else:
        ui.ok(f"compiled stage: {rel(stage)}  (run it with: {rel(stage / 'main.py')})")
    return 0


# --- check / lint / fmt --------------------------------------------------------------------------


def _run_mypy(cfg: Config, profile: str, blocking: bool) -> bool:
    tool = envs.tool_env(cfg)
    config_file = _profile_file(cfg, profile, "mypy")
    argv: list[str | Path] = ["mypy", "--config-file", config_file, *render.mypy_cli_args(cfg, tool.python)]
    r = envs.uv_run(tool, argv, check=False)
    if r.returncode == 0:
        return True
    if not blocking and r.returncode == 1:
        ui.warn(f"mypy: type warnings (profile '{profile}', non-blocking)")
        return True
    return False


def run_checks(cfg: Config, backend: str, *, rules: bool = True) -> bool:
    """ruff + mypy with the backend's typing profile, the mypyc rules (unless `rules` is False)
    and basedpyright when it is the editor. Return whether everything passed."""
    profile = cfg.profile_for(backend)
    data = render.load_profile(profile)
    blocking = bool(data.get("blocking", False))
    ui.step(f"check {backend}: typing profile '{profile}' ({data.get('description', '')})")
    tool = envs.tool_env(cfg)
    ok = True

    ruff_cfg = _profile_file(cfg, profile, "ruff")
    ruff_args: list[str | Path] = ["ruff", "check", "--config", config_arg(ruff_cfg)]
    if data.get("ruff", {}).get("exit_zero"):
        ruff_args.append("--exit-zero")
    if envs.uv_run(tool, [*ruff_args, *code_dirs()], check=False).returncode != 0:
        ok = False

    if not data.get("skip_mypy"):
        ok = _run_mypy(cfg, profile, blocking) and ok

    if rules and cfg.supports("mypyc"):
        files = mypyc.compiled_sources(cfg)
        findings = lintc.lint(cfg, files)
        strict = profile == "mypyc"
        for f in findings:
            (ui.error if strict and not f.note else ui.warn)(str(f))
        if strict and any(not f.note for f in findings):
            ok = False
        elif not findings:
            ui.ok(f"mypyc rules: no problems in {lintc.describe(files)}")

    if cfg.typing.editor == "basedpyright":
        # Profile config of THIS backend (the editor's pyrightconfig.json is the active backend's).
        # --with: used without adding it to uv.lock (the VS Code extension ships its own)
        conf = BUILD / "cfg" / f"pyright-{profile}.json"
        conf.write_text(json.dumps(render.pyright_config(cfg, profile, absolute=True), indent=2), encoding="utf-8", newline="\n")
        ok = _run_basedpyright(cfg, profile, blocking, conf) and ok
    return ok


def _run_basedpyright(cfg: Config, profile: str, blocking: bool, conf: Path) -> bool:
    """basedpyright with the pinned versions. Its exit 1 (findings) is only a warning under a
    profile that is not blocking; basedpyright that cannot run is always a failure. uv exits 1
    too when it cannot install the pins (offline, a cold cache): that is asked first, without a
    command line (the environment it resolves is cached for the real run), so it is never taken
    for findings under a non-blocking profile and followed by `ok check: no errors`. Not
    captured: on a cold cache that step downloads basedpyright and Node.js (tens of MB), and
    uv's progress shows it (nothing prints once they are cached; -q hides it); uv prints its own
    error too."""
    tool = envs.tool_env(cfg)
    with_pins = ["run", "--locked", "--with", BASEDPYRIGHT, "--with", BASEDPYRIGHT_NODE]
    if not proc.DRY_RUN:  # a dry run never installs anything
        ready = envs.uv(tool, [*with_pins, "python", "-c", ""], check=False, echo=False)
        if ready.returncode != 0:  # uv's reason is above
            ui.error(f"basedpyright could not run: uv could not install {BASEDPYRIGHT} (exit code {ready.returncode})")
            return False
    r = envs.uv(tool, [*with_pins, "basedpyright", "--project", conf], check=False)
    if r.returncode == 0:
        return True
    if r.returncode == 1 and not blocking:
        ui.warn(f"basedpyright: type warnings (profile '{profile}', non-blocking)")
        return True
    if r.returncode != 1:  # 2: a fatal error, 3: its config, 4: its command line
        ui.error(f"basedpyright stopped (exit code {r.returncode}): fix the problem above")
    return False


def cmd_check(cfg: Config, args: list[str]) -> int:
    """check [BACKEND|all]: ruff + mypy (the backend's profile) + mypyc rules."""
    target, rest = split_backend(cfg, args, allow_all=True)
    if rest:
        raise PytError(f"check: unrecognized arguments: {' '.join(rest)}")
    targets = cfg.backend.supported if target == "all" else [target]
    # Each profile is checked only once (cpython and pypy usually share it), and the mypyc rules
    # only once, with the strictest profile (otherwise every finding shows up twice)
    chosen: dict[str, str] = {}
    for b in targets:
        envs.ensure_supported(cfg, b)
        chosen.setdefault(cfg.profile_for(b), b)
    rules_profile = "mypyc" if "mypyc" in chosen else next(iter(chosen))
    ok = True
    for profile, b in chosen.items():
        ok = run_checks(cfg, b, rules=profile == rules_profile) and ok
    if ok and proc.DRY_RUN:
        ui.info("(--dry-run) check: ruff, mypy and basedpyright were not run (the mypyc rules were)")
        return 0
    if ok:
        ui.ok("check: no errors")
        return 0
    ui.error("check found errors")
    return 1


def cmd_lint(cfg: Config, args: list[str]) -> int:
    """lint [--fix]: ruff check with the active profile (.ruff.toml); a profile that never
    blocks (ruff exit_zero, e.g. `warn`) reports the findings with exit 0, like `check` and the
    pre-commit hook."""
    flags = only_flags("lint", args, ("--fix",))
    exit_zero = ["--exit-zero"] if render.load_profile(cfg.profile_for()).get("ruff", {}).get("exit_zero") else []
    r = envs.uv_run(envs.tool_env(cfg), ["ruff", "check", *sorted(flags), *exit_zero, *code_dirs()], check=False)
    return r.returncode


def cmd_fmt(cfg: Config, args: list[str]) -> int:
    """fmt [--check]: ruff format."""
    flags = only_flags("fmt", args, ("--check",))
    r = envs.uv_run(envs.tool_env(cfg), ["ruff", "format", *sorted(flags), *code_dirs()], check=False)
    return r.returncode


# --- test ----------------------------------------------------------------------------------------


# pytest's configuration files, in the order pytest looks for them in a folder: (name, kind)
PYTEST_CONFIGS = (
    ("pytest.toml", "toml"),
    (".pytest.toml", "toml"),
    ("pytest.ini", "ini"),
    (".pytest.ini", "ini"),
    ("pyproject.toml", "pyproject"),
    ("tox.ini", "tox"),
    ("setup.cfg", "cfg"),
)


def _pytest_table(path: Path, kind: str) -> dict[str, Any] | None:
    """The pytest settings of one configuration file, or None when pytest would not use it."""
    text = path.read_text(encoding="utf-8-sig")
    if kind in ("toml", "pyproject"):
        data = tomllib.loads(text)
        if kind == "toml":  # pytest.toml is always the configuration, even without [pytest]
            table = data.get("pytest", {})
            return table if isinstance(table, dict) else {}
        tool = data.get("tool", {}).get("pytest")
        if not isinstance(tool, dict):
            return None
        native = {k: v for k, v in tool.items() if k != "ini_options"}  # [tool.pytest] (pytest 9)
        if native:
            return native
        ini = tool.get("ini_options")
        return ini if isinstance(ini, dict) else None
    parser = configparser.RawConfigParser()
    parser.read_string(text, str(path))
    section = "tool:pytest" if kind == "cfg" else "pytest"
    if parser.has_section(section):
        return dict(parser.items(section))
    return {} if kind == "ini" else None  # pytest.ini is always the configuration, even empty


def pytest_pythonpath(root: Path = ROOT) -> list[str]:
    """The `pythonpath` entries of the project's pytest configuration (relative to `root`), read
    from the first file pytest itself uses in the project folder; [] when there is none."""
    for name, kind in PYTEST_CONFIGS:
        path = root / name
        if not path.is_file():
            continue
        try:
            table = _pytest_table(path, kind)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, configparser.Error):
            return []  # pytest itself reports the broken file
        if table is None:
            continue
        value = table.get("pythonpath", [])
        if isinstance(value, str):
            return shlex.split(value)
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
    return []


def stage_pythonpath(stage: Path, root: Path = ROOT, src: Path = SRC) -> str:
    """The value of `-o pythonpath=` for the mypyc tests: the project's own entries with the
    `src` one replaced by the stage (tests/helpers and the like stay), the stage first when none
    is `src`. An -o value replaces the whole setting, so the other entries must be repeated."""

    def is_src(entry: str) -> bool:
        path = Path(entry) if os.path.isabs(entry) else root / entry
        return os.path.normcase(str(path.resolve())) == os.path.normcase(str(src.resolve()))

    staged = rel(stage)
    entries = [staged if is_src(e) else e for e in pytest_pythonpath(root)]
    if staged not in entries:
        entries.insert(0, staged)
    return shlex.join(entries)


def test_backend(cfg: Config, backend: str, pytest_args: list[str]) -> int:
    envs.ensure_supported(cfg, backend)
    env = envs.runtime_env(cfg, backend)
    if backend == "mypyc":
        stage = mypyc.build(cfg, "dev")
        ui.step("pytest against the compiled modules (mypyc)")
        return envs.uv_run(
            env,
            ["python", "-m", "pytest", "-o", f"pythonpath={stage_pythonpath(stage)}", *pytest_args],
            check=False,
            extra_env=mypyc.runtime_env_vars(cfg),
        ).returncode
    ui.step(f"pytest ({backend})")
    return envs.uv_run(
        env, ["python", "-m", "pytest", *pytest_args], check=False, extra_env={"PYTEMPLATE_BACKEND": backend}
    ).returncode


def cmd_test(cfg: Config, args: list[str]) -> int:
    """test [BACKEND|all] [pytest args...]: one backend returns pytest's own exit code (5 = no
    tests collected, 4 = a pytest usage error); `all` tests every supported backend, even after
    one fails to build (a mypyc error, no C compiler), prints a summary and returns 0 or 1."""
    target, rest = split_backend(cfg, args, allow_all=True)
    if target != "all":
        return test_backend(cfg, target, rest)
    results: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for b in cfg.backend.supported:
        try:
            results[b] = test_backend(cfg, b, rest)
        except PytError as e:  # Ctrl+C (a KeyboardInterrupt) still stops everything
            ui.error(f"test {b}: {e}")
            results[b] = e.code or 1
            reasons[b] = str(e).splitlines()[0] if str(e) else f"exit code {results[b]}"
    failed = [b for b, code in results.items() if code != 0]
    # --dry-run: pytest never ran, so no [ok] row; a backend whose checks failed (a compile.exclude
    # naming nothing) is still listed, and the exit code is 1 as in a real run
    if len(results) > 1 and (failed or not proc.DRY_RUN):
        ui.step("test summary")
        for b, code in results.items():
            if code == 0 and proc.DRY_RUN:
                continue
            ui.check_line(code == 0, b, "" if code == 0 else reasons.get(b, f"exit code {code}"))
    return 1 if failed else 0


# --- report --------------------------------------------------------------------------------------


def cmd_report(cfg: Config, args: list[str]) -> int:
    """report [--open]: mypyc HTML report (slow lines) + mypy Any reports."""
    parser = argparse.ArgumentParser(prog="./pyt report", add_help=True)
    parser.add_argument("--open", action="store_true", help="open the report in the browser")
    parser.add_argument("--no-mypy", action="store_true", help="only the mypyc report")
    ns = parser.parse_args(args)
    if not cfg.supports("mypyc"):
        raise PytError("the report comes from mypyc, and 'mypyc' is not in backend.supported")
    html = mypyc.ANNOTATE_HTML
    reports = html.parent
    # The report is generated before compiling C: no compiler needed
    mypyc.build(cfg, "dev", annotate=html, compile_c=False)
    if proc.DRY_RUN:
        ui.info(f"(--dry-run) would write the mypyc report: {rel(html)}")
    else:
        ui.ok(f"mypyc report: {rel(html)}  (in red: generic/slow operations and how to avoid them)")
    code = 0
    if not ns.no_mypy:
        tool = envs.tool_env(cfg)
        config_file = _profile_file(cfg, "mypyc", "mypy")
        any_report = reports / "any" / "any-exprs.txt"
        r = envs.uv_run(
            tool,
            ["mypy", "--config-file", config_file, "--any-exprs-report", reports / "any", "--lineprecision-report", reports / "precision"],
            check=False,
        )
        if proc.DRY_RUN:
            ui.info(f"(--dry-run) would write the Any reports: {rel(any_report)}")
        elif r.returncode in (0, 1):  # 1: type errors (above); the reports cover the whole code
            ui.ok(f"Any expressions per module: {rel(any_report)}")
        else:  # 2: mypy stopped (a syntax error, a bad config): the reports are empty or stale
            ui.error(f"mypy stopped (exit code {r.returncode}) before its Any reports: fix the errors above")
            code = 1
    if ns.open and not proc.DRY_RUN:
        webbrowser.open(html.resolve().as_uri())
    return code
