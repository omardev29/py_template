"""Development commands: run, check, lint, fmt, test, report."""

from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path

from . import envs, lintc, mypyc, proc, render, ui
from .config import BACKENDS, Config
from .project import BUILD, SRC, code_dirs, rel
from .ui import DeployError

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
        raise DeployError(f"{command}: unrecognized arguments: {' '.join(unknown)}  ({valid})", 2)
    return set(args)


def split_backend(cfg: Config, args: list[str], *, allow_all: bool = False) -> tuple[str, list[str]]:
    """Take the first argument as the backend only if it is a valid one; pass the rest through as-is."""
    if args and (args[0] in BACKENDS or (allow_all and args[0] == "all")):
        return args[0], args[1:]
    return cfg.backend.active, args


def _profile_file(cfg: Config, profile: str, kind: str) -> Path:
    """Write the config of a specific profile to .build/cfg/ (it may not be the editor's active one)."""
    out = BUILD / "cfg" / f"{kind}-{profile}.{'ini' if kind == 'mypy' else 'toml'}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if kind == "mypy":
        text = render.mypy_ini(cfg, profile)
    else:
        text = render.to_toml(render.ruff_config(cfg, profile, absolute=True)) + "\n"
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


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
    parser = argparse.ArgumentParser(prog="./deploy compile")
    parser.add_argument("--release", action="store_true", help="the release stage (asserts stripped per deploy.optimize)")
    ns = parser.parse_args(args)
    if not cfg.supports("mypyc"):
        raise DeployError("compile: 'mypyc' is not in backend.supported")
    stage = mypyc.build(cfg, "release" if ns.release else "dev")
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
    ruff_args: list[str | Path] = ["ruff", "check", "--config", ruff_cfg]
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
            (ui.error if strict else ui.warn)(str(f))
        if findings and strict:
            ok = False
        elif not findings:
            ui.ok(f"mypyc rules: no problems in {lintc.describe(files)}")

    if cfg.typing.editor == "basedpyright":
        # Profile config of THIS backend (the editor's pyrightconfig.json is the active backend's).
        # --with: used without adding it to uv.lock (the VS Code extension ships its own)
        conf = BUILD / "cfg" / f"pyright-{profile}.json"
        conf.write_text(json.dumps(render.pyright_config(cfg, profile, absolute=True), indent=2), encoding="utf-8", newline="\n")
        r = envs.uv(tool, ["run", "--locked", "--with", BASEDPYRIGHT, "--with", BASEDPYRIGHT_NODE, "basedpyright", "--project", conf], check=False)
        if r.returncode != 0 and blocking:
            ok = False
    return ok


def cmd_check(cfg: Config, args: list[str]) -> int:
    """check [BACKEND|all]: ruff + mypy (the backend's profile) + mypyc rules."""
    target, rest = split_backend(cfg, args, allow_all=True)
    if rest:
        raise DeployError(f"check: unrecognized arguments: {' '.join(rest)}")
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


def test_backend(cfg: Config, backend: str, pytest_args: list[str]) -> int:
    envs.ensure_supported(cfg, backend)
    env = envs.runtime_env(cfg, backend)
    if backend == "mypyc":
        stage = mypyc.build(cfg, "dev")
        ui.step("pytest against the compiled modules (mypyc)")
        return envs.uv_run(
            env,
            ["python", "-m", "pytest", "-o", f"pythonpath={rel(stage)}", *pytest_args],
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
        except DeployError as e:  # Ctrl+C (a KeyboardInterrupt) still stops everything
            ui.error(f"test {b}: {e}")
            results[b] = e.code or 1
            reasons[b] = str(e).splitlines()[0] if str(e) else f"exit code {results[b]}"
    if len(results) > 1:
        ui.step("test summary")
        for b, code in results.items():
            ui.check_line(code == 0, b, "" if code == 0 else reasons.get(b, f"exit code {code}"))
    return 0 if all(c == 0 for c in results.values()) else 1


# --- report --------------------------------------------------------------------------------------


def cmd_report(cfg: Config, args: list[str]) -> int:
    """report [--open]: mypyc HTML report (slow lines) + mypy Any reports."""
    parser = argparse.ArgumentParser(prog="./deploy report", add_help=True)
    parser.add_argument("--open", action="store_true", help="open the report in the browser")
    parser.add_argument("--no-mypy", action="store_true", help="only the mypyc report")
    ns = parser.parse_args(args)
    if not cfg.supports("mypyc"):
        raise DeployError("the report comes from mypyc, and 'mypyc' is not in backend.supported")
    html = mypyc.ANNOTATE_HTML
    reports = html.parent
    # The report is generated before compiling C: no compiler needed
    mypyc.build(cfg, "dev", annotate=html, compile_c=False)
    ui.ok(f"mypyc report: {rel(html)}  (in red: generic/slow operations and how to avoid them)")
    if not ns.no_mypy:
        tool = envs.tool_env(cfg)
        config_file = _profile_file(cfg, "mypyc", "mypy")
        envs.uv_run(
            tool,
            ["mypy", "--config-file", config_file, "--any-exprs-report", reports / "any", "--lineprecision-report", reports / "precision"],
            check=False,
        )
        ui.ok(f"Any expressions per module: {rel(reports / 'any' / 'any-exprs.txt')}")
    if ns.open and not proc.DRY_RUN:
        webbrowser.open(html.resolve().as_uri())
    return 0
