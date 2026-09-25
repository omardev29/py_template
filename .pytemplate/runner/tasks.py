"""Recetas propias estilo Justfile: la tabla [tasks] de pytemplate.toml.

    [tasks.gen]
    help = "Genera los assets"
    cmd = ["python", "scripts/gen.py", "{backend}"]   # argv, sin shell: igual en todos los shells
    deps = ["check"]                                   # otras tareas o comandos de ./deploy
    env = { SEED = "42" }
    backend = "pypy"   # entorno en el que corre (vacío = backend activo)
    uv = true          # true: `uv run` dentro de ese entorno; false: ejecuta el programa tal cual

Marcadores en cmd/env/cwd: {root} {src} {build} {dist} {backend} {name} {pkg} {python}.
Los argumentos extra de `./deploy <tarea> ...` se añaden al final de cmd.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable

from . import envs, proc, ui
from .config import Config
from .project import BUILD, DIST, ROOT, SRC
from .ui import DeployError

Dispatcher = Callable[[list[str]], int]


def _placeholders(cfg: Config, backend: str) -> dict[str, str]:
    env = envs.runtime_env(cfg, backend)
    return {
        "root": str(ROOT),
        "src": str(SRC),
        "build": str(BUILD),
        "dist": str(DIST),
        "backend": backend,
        "name": cfg.app.name,
        "pkg": cfg.pkg,
        "python": str(env.python),
    }


def list_tasks(cfg: Config) -> None:
    if not cfg.tasks:
        ui.info("No hay tareas propias. Añádelas en pytemplate.toml, sección [tasks].")
        return
    ui.step("tareas propias (pytemplate.toml [tasks])")
    for name, task in cfg.tasks.items():
        what = task.help or " ".join(task.cmd) or "deps: " + ", ".join(task.deps)
        ui.info(f"  {name:<14} {what}")


def run_task(cfg: Config, name: str, extra: list[str], dispatch: Dispatcher, stack: tuple[str, ...] = ()) -> int:
    if name in stack:
        raise DeployError(f"tareas en ciclo: {' -> '.join((*stack, name))}")
    task = cfg.tasks[name]
    for dep in task.deps:
        argv = shlex.split(dep)
        if argv and argv[0] in cfg.tasks:
            code = run_task(cfg, argv[0], argv[1:], dispatch, (*stack, name))
        else:
            code = dispatch(argv)
        if code != 0:
            return code
    if not task.cmd:
        return 0
    backend = task.backend or cfg.backend.active
    values = _placeholders(cfg, backend)
    try:
        argv = [a.format_map(values) for a in task.cmd] + extra
        extra_env = {k: v.format_map(values) for k, v in task.env.items()}
        cwd = ROOT / task.cwd.format_map(values) if task.cwd else ROOT
    except KeyError as e:
        raise DeployError(f"tarea '{name}': marcador desconocido {e}") from None
    ui.step(f"tarea {name}")
    if task.uv:
        env = envs.runtime_env(cfg, backend)
        return envs.uv_run(env, argv, cwd=cwd, extra_env=extra_env, check=False).returncode
    base = proc.base_env()
    base.update(extra_env)
    return proc.run(argv, cwd=cwd, env=base, check=False).returncode
