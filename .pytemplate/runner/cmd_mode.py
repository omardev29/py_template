"""Comandos de modo y plantilla: mode, render, init, new."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import config, envs, presets, proc, render, ui
from .config import BACKENDS, Config
from .project import ROOT, code_dirs
from .ui import DeployError


def _describe(cfg: Config) -> None:
    ui.step("modo actual")
    ui.info(f"  app            {cfg.app.name}  (preset {cfg.app.preset}, paquete src/{cfg.pkg}/)")
    ui.info(f"  backend activo {cfg.backend.active}")
    ui.info(f"  soportados     {', '.join(cfg.backend.supported)}  (sintaxis Python {cfg.min_python}+)")
    for b in cfg.backend.supported:
        ui.info(f"  tipado {b:<8} {cfg.profile_for(b)}")
    ui.info(f"  editor         {cfg.typing.editor}")
    ui.info(f"  compila mypyc  {', '.join(cfg.compile.modules)}")
    ui.info(f"  JIT CPython    {'sí' if cfg.python.jit else 'no'}")


def _supports_after(cfg: Config, spec: str) -> list[str]:
    current = list(cfg.backend.supported)
    if spec.startswith(("+", "-")):
        for token in spec.split(","):
            name = token[1:]
            if name not in BACKENDS:
                raise DeployError(f"mode --supports: backend desconocido '{name}'")
            if token[0] == "+" and name not in current:
                current.append(name)
            elif token[0] == "-" and name in current:
                current.remove(name)
        return [b for b in BACKENDS if b in current]
    names = [n.strip() for n in spec.split(",") if n.strip()]
    for n in names:
        if n not in BACKENDS:
            raise DeployError(f"mode --supports: backend desconocido '{n}'")
    return [b for b in BACKENDS if b in names]


def _precheck_py311(cfg: Config) -> None:
    """Antes de soportar PyPy: ¿el código es válido en Python 3.11?"""
    ui.step("comprobando que el código es válido en Python 3.11 (requisito de PyPy)")
    tool = envs.tool_env(cfg)
    dirs = code_dirs()
    # 1) sintaxis: ruff marca como error la sintaxis que no existe en la versión objetivo
    r = envs.uv_run(
        tool,
        ["ruff", "check", "--no-cache", "--isolated", "--target-version", "py311", "--select", "E9,F63,F7,F82", *dirs],
        check=False,
    )
    if r.returncode != 0:
        raise DeployError("hay sintaxis que no existe en Python 3.11 (ver arriba); corrígela antes de activar PyPy")

    # 2) APIs: errores de mypy que aparecen SOLO al comprobar como 3.11 (p. ej. typing.override)
    def mypy_errors(version: str) -> set[str]:
        argv = [
            proc.find_uv(), "run", "--locked", "mypy", "--no-incremental", "--ignore-missing-imports",
            "--follow-imports", "silent", "--no-error-summary", "--hide-error-context",
            "--python-version", version, "--python-executable", str(tool.python), *dirs,
        ]
        out = proc.run(argv, env=envs.env_vars(tool), capture=True, check=False, echo=False).stdout
        return {ln.strip() for ln in out.splitlines() if ": error:" in ln}

    new = sorted(mypy_errors("3.11") - mypy_errors(cfg.python.cpython))
    if new:
        for line in new:
            ui.error(line)
        raise DeployError(
            "el código usa APIs que no existen en Python 3.11 (arriba). Corrígelo antes de activar PyPy "
            "(p. ej. typing.override -> typing_extensions.override)"
        )
    ui.ok("el código es válido en Python 3.11")


def cmd_mode(cfg: Config, args: list[str]) -> int:
    """mode [BACKEND] [--supports +pypy|-pypy|a,b] [--typing off|warn|strict|auto] [--jit on|off] [--editor pylance|basedpyright]"""
    parser = argparse.ArgumentParser(prog="./deploy mode")
    parser.add_argument("backend", nargs="?", choices=BACKENDS)
    parser.add_argument("--supports", help="+pypy, -pypy o lista completa (cpython,mypyc)")
    parser.add_argument("--typing", choices=("auto", "off", "warn", "strict", "mypyc"))
    parser.add_argument("--jit", choices=("on", "off"))
    parser.add_argument("--editor", choices=config.EDITORS)
    # `--supports -pypy`: argparse tomaría "-pypy" por una opción; lo unimos como --supports=-pypy
    fixed: list[str] = []
    it = iter(args)
    for a in it:
        if a == "--supports":
            value = next(it, "")
            fixed.append(f"--supports={value}")
        else:
            fixed.append(a)
    ns = parser.parse_args(fixed)
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

    adding_pypy = "pypy" in supported and not cfg.pypy_enabled
    if adding_pypy:
        _precheck_py311(cfg)

    config.update_file(changes)
    new_cfg = config.load()
    heavy = supported != cfg.backend.supported
    if heavy or render.pyproject_outdated(new_cfg):
        from .cmd_env import ensure_lock

        ensure_lock(new_cfg)
    changed, _ = render.apply(new_cfg)
    if changed:
        ui.info(f"render: actualizado {', '.join(changed)}")
    if adding_pypy:
        envs.sync(envs.pypy_env(new_cfg))
    if ns.jit == "on":
        envs.sync(envs.jit_env(new_cfg))
    _describe(new_cfg)
    return 0


def cmd_render(cfg: Config, args: list[str]) -> int:
    """render [--check] [--diff] [--force]: regenera los archivos de configuración."""
    check = "--check" in args
    changed, edited = render.apply(cfg, force="--force" in args, check=check, show_diff="--diff" in args)
    for path in changed:
        ui.info(("desactualizado: " if check else "actualizado: ") + path)
    for path in edited:
        ui.warn(f"editado a mano (no se toca sin --force): {path}")
    if render.pyproject_outdated(cfg):
        ui.warn("pyproject.toml no coincide con pytemplate.toml: ./deploy lock")
        if check:
            return 1
    if check and (changed or edited):
        return 1
    if not changed and not edited:
        ui.ok("archivos generados al día")
    return 0


def cmd_init(cfg: Config, args: list[str]) -> int:
    """init PRESET [--name NOMBRE] [--force]: convierte este proyecto al preset."""
    parser = argparse.ArgumentParser(prog="./deploy init")
    parser.add_argument("preset", choices=presets.available())
    parser.add_argument("--name")
    parser.add_argument("--force", action="store_true")
    ns = parser.parse_args(args)
    presets.init(cfg, ns.preset, ns.name, force=ns.force)
    return 0


def cmd_new(cfg: Config, args: list[str]) -> int:
    """new CARPETA [--preset P] [--name NOMBRE]: copia la plantilla a un proyecto nuevo."""
    parser = argparse.ArgumentParser(prog="./deploy new")
    parser.add_argument("dest")
    parser.add_argument("--preset", default="script", choices=presets.available())
    parser.add_argument("--name")
    ns = parser.parse_args(args)
    import os

    base = Path(os.environ.get("PYTEMPLATE_CALLER_CWD") or Path.cwd())
    dest = Path(ns.dest)
    if not dest.is_absolute():
        dest = base / dest
    if dest.resolve() == ROOT or ROOT in dest.resolve().parents:
        raise DeployError("new: la carpeta destino no puede estar dentro de esta plantilla")
    presets.new(dest, ns.preset, ns.name)
    return 0
