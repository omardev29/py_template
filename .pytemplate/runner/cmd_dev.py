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


# --- check / lint / fmt --------------------------------------------------------------------------


def _run_mypy(cfg: Config, backend: str, profile: str, blocking: bool) -> bool:
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


def run_checks(cfg: Config, backend: str) -> bool:
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
        ok = _run_mypy(cfg, backend, profile, blocking) and ok

    if cfg.supports("mypyc"):
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
        conf.write_text(json.dumps(render.pyright_config(cfg, profile, absolute=True), indent=2), encoding="utf-8")
        r = envs.uv(tool, ["run", "--locked", "--with", "basedpyright", "basedpyright", "--project", conf], check=False)
        if r.returncode != 0 and blocking:
            ok = False
    return ok


def cmd_check(cfg: Config, args: list[str]) -> int:
    """check [BACKEND|all]: ruff + mypy (the backend's profile) + mypyc rules."""
    target, rest = split_backend(cfg, args, allow_all=True)
    if rest:
        raise DeployError(f"check: unrecognized arguments: {' '.join(rest)}")
    targets = cfg.backend.supported if target == "all" else [target]
    # Each profile is checked only once (cpython and pypy usually share it)
    seen: set[str] = set()
    ok = True
    for b in targets:
        envs.ensure_supported(cfg, b)
        profile = cfg.profile_for(b)
        if profile in seen:
            continue
        seen.add(profile)
        ok = run_checks(cfg, b) and ok
    if ok:
        ui.ok("check: no errors")
        return 0
    ui.error("check found errors")
    return 1


def cmd_lint(cfg: Config, args: list[str]) -> int:
    """lint [--fix]: ruff check with the active profile."""
    fix = ["--fix"] if "--fix" in args else []
    r = envs.uv_run(envs.tool_env(cfg), ["ruff", "check", *fix, *code_dirs()], check=False)
    return r.returncode


def cmd_fmt(cfg: Config, args: list[str]) -> int:
    """fmt [--check]: ruff format."""
    extra = ["--check"] if "--check" in args else []
    r = envs.uv_run(envs.tool_env(cfg), ["ruff", "format", *extra, *code_dirs()], check=False)
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
    """test [BACKEND|all] [pytest args...]"""
    target, rest = split_backend(cfg, args, allow_all=True)
    targets = cfg.backend.supported if target == "all" else [target]
    results = {b: test_backend(cfg, b, rest) for b in targets}
    if len(results) > 1:
        ui.step("test summary")
        for b, code in results.items():
            ui.check_line(code == 0, b, "" if code == 0 else f"exit code {code}")
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
    reports = BUILD / "reports"
    html = reports / "mypyc-annotate.html"
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
