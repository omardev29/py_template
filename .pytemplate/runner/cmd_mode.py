"""Mode and template commands: mode, render, init, new.

Under --dry-run each of them prints what it would do and writes nothing: no pytemplate.toml,
pyproject.toml, uv.lock or generated file, no environment synced, no project copied.
Read-only checks (the Python 3.11 precheck, `uv lock --check`) still run.
"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path
from typing import Any

from . import config, envs, presets, proc, render, ui
from .config import BACKENDS, Config
from .project import CONFIG_FILE, ENV_SUFFIX, PYPROJECT, ROOT, code_dirs, rel, user_path
from .ui import DeployError

_APP_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_DRY = "(--dry-run: nothing is written)"


def _describe(cfg: Config, title: str = "current mode") -> None:
    ui.step(title)
    ui.info(f"  app            {cfg.app.name}  (preset {cfg.app.preset}, package src/{cfg.pkg}/)")
    ui.info(f"  active backend {cfg.backend.active}")
    ui.info(f"  supported      {', '.join(cfg.backend.supported)}  (Python {cfg.min_python}+ syntax)")
    for b in cfg.backend.supported:
        ui.info(f"  {'typing ' + b:<14} {cfg.profile_for(b)}")
    ui.info(f"  editor         {cfg.typing.editor}")
    ui.info(f"  mypyc compiles {', '.join(cfg.compile.modules)}")
    ui.info(f"  CPython JIT    {'yes' if cfg.python.jit else 'no'}")


def _parse(parser: argparse.ArgumentParser, args: list[str]) -> argparse.Namespace:
    """parse_args, but an unknown argument is a clear DeployError instead of argparse's exit."""
    ns, unknown = parser.parse_known_args(args)
    if unknown:
        raise DeployError(f"{parser.prog}: unknown argument(s): {' '.join(unknown)}  ({parser.prog} -h lists the options)")
    return ns


def _config_from_text(text: str, where: str) -> Config:
    """Validate a pytemplate.toml text in memory (what config.load does with the file)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"{where}: not valid TOML: {e}") from None
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


# --- mode ----------------------------------------------------------------------------------------


def _supports_after(cfg: Config, spec: str) -> list[str]:
    current = list(cfg.backend.supported)
    if spec.startswith(("+", "-")):
        for token in (t.strip() for t in spec.split(",")):
            sign, name = token[:1], token[1:]
            if sign not in ("+", "-") or name not in BACKENDS:
                raise DeployError(f"mode --supports: unknown backend '{token}' (use +name or -name)")
            if sign == "+" and name not in current:
                current.append(name)
            elif sign == "-" and name in current:
                current.remove(name)
        out = [b for b in BACKENDS if b in current]
    else:
        names = [n.strip() for n in spec.split(",") if n.strip()]
        for n in names:
            if n not in BACKENDS:
                raise DeployError(f"mode --supports: unknown backend '{n}'")
        out = [b for b in BACKENDS if b in names]
    if not out:
        raise DeployError(f"mode --supports {spec}: at least one backend must stay supported")
    return out


def _precheck_py311(cfg: Config) -> None:
    """Check that the code is valid on Python 3.11 before adding PyPy support.

    Under --dry-run both checks still run, but with `uv run --no-sync` so the environment is
    never installed or updated (and they are skipped when it does not exist yet).
    """
    ui.step("checking that the code is valid on Python 3.11 (required by PyPy)")
    tool = envs.tool_env(cfg)
    dry = proc.DRY_RUN
    if dry and not tool.python.is_file():
        ui.info(f"  (--dry-run) skipped: {rel(tool.dir)} does not exist yet (./deploy setup), and creating it is a side effect")
        return
    run = ["run", "--locked", "--no-sync"] if dry else ["run", "--locked"]
    dirs = code_dirs()
    if dry:
        ui.info("  (--dry-run) running the read-only checks: ruff and mypy as Python 3.11, with uv run --no-sync")
    # 1) syntax: ruff reports syntax that does not exist in the target version as an error
    r = envs.uv(
        tool,
        [*run, "ruff", "check", "--no-cache", "--isolated", "--target-version", "py311", "--select", "E9,F63,F7,F82", *dirs],
        check=False,
        echo=not dry,  # proc.run skips echoed commands under --dry-run
    )
    if r.returncode != 0:
        raise DeployError("the code uses syntax that does not exist in Python 3.11 (see above); fix it before enabling PyPy")

    # 2) APIs: mypy errors that appear ONLY when checking as 3.11 (e.g. typing.override)
    def mypy_errors(version: str) -> set[str]:
        argv = [
            *run, "mypy", "--no-incremental", "--ignore-missing-imports",
            "--follow-imports", "silent", "--no-error-summary", "--hide-error-context",
            "--python-version", version, "--python-executable", str(tool.python), *dirs,
        ]
        out = envs.uv(tool, argv, capture=True, check=False, echo=False).stdout
        return {ln.strip() for ln in out.splitlines() if ": error:" in ln}

    new = sorted(mypy_errors("3.11") - mypy_errors(cfg.python.cpython))
    if new:
        for line in new:
            ui.error(line)
        raise DeployError(
            "the code uses APIs that do not exist in Python 3.11 (above). Fix it before enabling PyPy "
            "(e.g. typing.override -> typing_extensions.override)"
        )
    ui.ok("the code is valid on Python 3.11")


def _current(cfg: Config, table: str, key: str) -> Any:
    return getattr(getattr(cfg, table), key)


def _leftover_envs(cfg: Config, new_cfg: Config) -> None:
    """One-line hint for the environments a mode change leaves unused (never deleted here)."""
    left: list[Path] = []
    if cfg.pypy_enabled and not new_cfg.pypy_enabled:
        left.append(envs.pypy_env(new_cfg).dir)
    if cfg.python.jit and not new_cfg.python.jit:
        left.append(ROOT / f".venv-jit{ENV_SUFFIX}")  # envs.jit_env would look for the interpreter
    names = [rel(d) for d in left if d.is_dir()]
    if names:
        ui.info(
            f"note: {', '.join(names)} {'is' if len(names) == 1 else 'are'} no longer used: "
            "./deploy clean --envs removes the .venv* environments (./deploy setup recreates the ones in use)"
        )


def _plan_mode(cfg: Config, new_cfg: Config, changes: list[tuple[str, str, object]], syncs: list[envs.PyEnv]) -> None:
    """--dry-run: print what `mode` would change, without writing or syncing anything."""
    ui.step(f"mode {_DRY}")
    effective = [(t, k, v) for t, k, v in changes if _current(cfg, t, k) != v]
    if not effective:
        ui.info("  pytemplate.toml  unchanged")
    for table, key, value in effective:
        ui.info(
            f"  pytemplate.toml  [{table}] {key} = {config.toml_value(value)}"
            f"   (now {config.toml_value(_current(cfg, table, key))})"
        )
    pyproject = render.pyproject_outdated(new_cfg)
    ui.info(
        "  pyproject.toml   "
        + ("would rewrite the managed parts (requires-python, [tool.uv] block)" if pyproject else "unchanged")
    )
    if pyproject:
        lock = "would re-lock (uv lock)"
    elif new_cfg.backend.supported != cfg.backend.supported:
        r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], check=False, capture=True, echo=False)
        lock = "up to date (uv lock --check)" if r.returncode == 0 else "would re-lock (uv lock)"
    else:
        lock = "unchanged"
    ui.info(f"  uv.lock          {lock}")
    changed, edited = render.apply(new_cfg)  # --dry-run: only compares
    ui.info("  generated files  " + (f"would update {', '.join(changed)}" if changed else "unchanged"))
    if edited:
        ui.info(f"                   hand-edited, left untouched: {', '.join(edited)}")
    for env in syncs:
        ui.info(f"  environment      would sync {rel(env.dir)} ({env.request})")


def cmd_mode(cfg: Config, args: list[str]) -> int:
    """mode [BACKEND] [--supports +pypy|-pypy|a,b] [--typing off|warn|strict|auto] [--jit on|off] [--editor pylance|basedpyright]"""
    parser = argparse.ArgumentParser(prog="./deploy mode")
    parser.add_argument("backend", nargs="?", choices=BACKENDS)
    parser.add_argument("--supports", help="+pypy, -pypy or a full list (cpython,mypyc)")
    parser.add_argument("--typing", choices=("auto", "off", "warn", "strict", "mypyc"))
    parser.add_argument("--jit", choices=("on", "off"))
    parser.add_argument("--editor", choices=config.EDITORS)
    # `--supports -pypy`: argparse would take "-pypy" for an option; join it as --supports=-pypy
    fixed: list[str] = []
    it = iter(args)
    for a in it:
        fixed.append(f"--supports={next(it, '')}" if a == "--supports" else a)
    ns = _parse(parser, fixed)
    if ns.supports is not None and not ns.supports.strip():
        raise DeployError("mode --supports needs a value: +pypy, -pypy or a list such as cpython,mypyc")
    if not any((ns.backend, ns.supports, ns.typing, ns.jit, ns.editor)):
        _describe(cfg)
        return 0

    changes: list[tuple[str, str, object]] = []
    supported = _supports_after(cfg, ns.supports) if ns.supports else list(cfg.backend.supported)
    if ns.backend and ns.backend not in supported:
        supported = [b for b in BACKENDS if b in {*supported, ns.backend}]
    if supported != cfg.backend.supported:
        changes.append(("backend", "supported", supported))
    active = ns.backend or cfg.backend.active
    if active not in supported:
        active = supported[0]
    if active != cfg.backend.active:
        changes.append(("backend", "active", active))
    if ns.typing:
        if ns.typing in ("off", "warn", "strict"):
            changes += [("typing", "profile", "auto"), ("typing", "relaxed", ns.typing)]
        else:
            changes.append(("typing", "profile", ns.typing))
    if ns.jit:
        changes.append(("python", "jit", ns.jit == "on"))
    if ns.editor:
        changes.append(("typing", "editor", ns.editor))

    # The new configuration, validated in memory BEFORE anything is written
    text = CONFIG_FILE.read_text(encoding="utf-8-sig")
    for table, key, value in changes:
        text = config.set_value(text, table, key, value)
    planned = _config_from_text(text, "mode: the new pytemplate.toml")

    adding_pypy = planned.pypy_enabled and not cfg.pypy_enabled
    syncs: list[envs.PyEnv] = []
    if adding_pypy:
        syncs.append(envs.pypy_env(planned))
    if ns.jit == "on":
        syncs.append(envs.jit_env(planned))  # finds the JIT interpreter now: fail before writing
    if adding_pypy:
        _precheck_py311(cfg)

    if proc.DRY_RUN:
        _plan_mode(cfg, planned, changes, syncs)
        _leftover_envs(cfg, planned)
        _describe(planned, "mode after the change (not applied: --dry-run)")
        return 0

    config.update_file(changes)
    new_cfg = config.load()
    heavy = supported != cfg.backend.supported
    if heavy or render.pyproject_outdated(new_cfg):
        from .cmd_env import ensure_lock

        ensure_lock(new_cfg)
    changed, _ = render.apply(new_cfg)
    if changed:
        ui.info(f"render: updated {', '.join(changed)}")
    for env in syncs:
        envs.sync(env)
    _leftover_envs(cfg, new_cfg)
    _describe(new_cfg)
    return 0


# --- render --------------------------------------------------------------------------------------


def cmd_render(cfg: Config, args: list[str]) -> int:
    """render [--check] [--diff] [--force]: regenerate the configuration files."""
    parser = argparse.ArgumentParser(prog="./deploy render")
    parser.add_argument("--check", action="store_true", help="only report outdated files (exit code 1)")
    parser.add_argument("--diff", action="store_true", help="show how hand-edited files differ")
    parser.add_argument("--force", action="store_true", help="also overwrite hand-edited generated files")
    ns = _parse(parser, args)
    changed, edited = render.apply(cfg, force=ns.force, check=ns.check, show_diff=ns.diff)
    prefix = "outdated: " if ns.check else "would update: " if proc.DRY_RUN else "updated: "
    for path in changed:
        ui.info(prefix + path)
    for path in edited:
        ui.warn(f"hand-edited (left untouched without --force): {path}")
    if render.pyproject_outdated(cfg):
        ui.warn("pyproject.toml does not match pytemplate.toml: ./deploy lock")
        if ns.check:
            return 1
    if ns.check and (changed or edited):
        return 1
    if not changed and not edited:
        ui.ok("generated files up to date")
    return 0


# --- init / new ----------------------------------------------------------------------------------


def _req_name(requirement: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else requirement


def _owned_now() -> dict[str, bytes]:
    """The files of src/, tests/ and typings/ (what init replaces), CRLF-normalized."""
    out: dict[str, bytes] = {}
    for d in presets.OWNED_DIRS:
        base = ROOT / d
        if base.is_dir():
            for path in base.rglob("*"):
                if path.is_file() and not any(p in path.parts for p in ("__pycache__", ".pytest_cache")):
                    out[path.relative_to(ROOT).as_posix()] = path.read_bytes().replace(b"\r\n", b"\n")
    return out


def _plan_init(cfg: Config, preset: str, name: str | None, *, force: bool) -> None:
    """--dry-run: print what `init` would replace (same checks as presets.init, no writes)."""
    new_name = name or cfg.app.name
    if not _APP_NAME.fullmatch(new_name):
        raise DeployError("the name may only contain letters, digits, '-' and '_' (and must start with a letter)")
    target = presets.load(preset)
    if not force and not presets.pristine(cfg):
        raise DeployError(
            "src/, tests/ or typings/ have changes compared to the skeleton of the current preset "
            f"('{cfg.app.preset}'). init would replace them.\n  If you are sure: ./deploy init {preset} --force"
        )
    files = presets.skeleton(preset, new_name)
    owned = _owned_now()
    ui.step(f"init {preset} ({target.get('description', '')}) as '{new_name}' {_DRY}")
    marks: list[str] = []
    same = 0
    for path in sorted({*files, *owned}):
        target_file = ROOT / path
        if path not in files:
            marks.append(f"    - {path}")
        elif not target_file.is_file():
            marks.append(f"    + {path}")
        elif target_file.read_bytes().replace(b"\r\n", b"\n") != files[path]:
            marks.append(f"    ~ {path}")
        else:
            same += 1
    ui.info(f"  files (- deleted, + new, ~ replaced; {same} identical):")
    for line in marks:
        ui.info(line)

    config_text = files.get(CONFIG_FILE.name, b"").decode("utf-8")
    new_cfg = _config_from_text(config_text, f"preset {preset}: pytemplate.toml") if config_text else cfg
    old_deps, old_dev = presets.dependencies(cfg)
    new_deps, new_dev = presets.dependencies(new_cfg, preset)
    for label, old, new in (("dependencies", old_deps, new_deps), ("dev group", old_dev, new_dev)):
        keep = {_req_name(r) for r in new}
        drop = [r for r in old if _req_name(r) not in keep]
        ui.info(f"  {label + ':':<14} remove {', '.join(drop) or '-'}; add {', '.join(new) or '-'}")
    ui.info(f"  {PYPROJECT.name}: name = \"{new_name}\", the preset's extra tables and the managed [tool.uv] block")
    ui.info("  uv.lock: re-locked (uv lock); generated files: re-rendered with --force")


def cmd_init(cfg: Config, args: list[str]) -> int:
    """init PRESET [--name NAME] [--force]: convert this project to the preset."""
    parser = argparse.ArgumentParser(prog="./deploy init")
    parser.add_argument("preset", choices=presets.available())
    parser.add_argument("--name")
    parser.add_argument("--force", action="store_true")
    ns = _parse(parser, args)
    if proc.DRY_RUN:
        presets.check_name_free(cfg, ns.preset, ns.name or cfg.app.name)
        _plan_init(cfg, ns.preset, ns.name, force=ns.force)
        return 0
    presets.init(cfg, ns.preset, ns.name, force=ns.force)
    return 0


def cmd_new(cfg: Config, args: list[str]) -> int:
    """new DIR [--preset P] [--name NAME]: copy the template to a new project."""
    parser = argparse.ArgumentParser(prog="./deploy new")
    parser.add_argument("dest")
    parser.add_argument("--preset", default="script", choices=presets.available())
    parser.add_argument("--name")
    ns = _parse(parser, args)
    dest = user_path(ns.dest)
    resolved = dest.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise DeployError("new: the destination folder cannot be inside this template")
    if dest.exists() and not dest.is_dir():
        raise DeployError(f"new: {dest} exists and is not a folder")
    if dest.is_dir() and any(dest.iterdir()):
        raise DeployError(f"new: {dest} already exists and is not empty")
    # Checked here, before copying: `init` in the copy would reject it with a half-made project
    name = ns.name or re.sub(r"[^A-Za-z0-9_-]", "-", resolved.name)
    if not _APP_NAME.fullmatch(name):
        raise DeployError(
            f"new: '{name}' is not a valid app name (letters, digits, '-' and '_', starting with a letter). "
            "Choose one with --name NAME"
        )
    presets.check_name_free(cfg, ns.preset, name)
    if proc.DRY_RUN:
        ui.step(f"new project in {dest} {_DRY}")
        ui.info(f"  preset  {ns.preset}")
        ui.info(f"  name    {name}  (package src/{name.replace('-', '_').lower()}/)")
        ui.info(
            f"  would copy this template there (without .git, environments, builds or caches), "
            f"run `./deploy init {ns.preset} --name {name} --force` in it and `git init`"
        )
        return 0
    presets.new(dest, ns.preset, name)
    return 0
