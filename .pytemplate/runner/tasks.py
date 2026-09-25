"""Custom Justfile-style recipes: the [tasks] table of pytemplate.toml.

    [tasks.gen]
    help = "Generate the assets"
    cmd = ["python", "scripts/gen.py", "{backend}"]   # argv, no shell: the same in every shell
    deps = ["check"]                                   # other tasks or ./deploy commands
    env = { SEED = "42" }
    backend = "pypy"   # environment it runs in (empty = active backend; "mypyc" = the .venv,
                       # interpreted: add deps = ["compile"] and run the stage to use the binaries)
    uv = true          # true: `uv run` inside that environment; false: run the program as-is

Placeholders in cmd/env/cwd: {root} {src} {build} {dist} {backend} {name} {pkg} {python}.
{python} (the backend's interpreter) is only resolved when used. Extra arguments to
`./deploy <task> ...` are appended to the end of cmd.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable

from . import envs, proc, ui
from .config import Config
from .project import BUILD, DIST, ROOT, SRC
from .ui import DeployError

Dispatcher = Callable[[list[str]], int]


class Placeholders(dict[str, str]):
    """The placeholder values. {python} is resolved only when a task uses it: with
    python.jit = true that means looking for the JIT interpreter, which may not exist."""

    def __init__(self, cfg: Config, backend: str) -> None:
        super().__init__(
            root=str(ROOT),
            src=str(SRC),
            build=str(BUILD),
            dist=str(DIST),
            backend=backend,
            name=cfg.app.name,
            pkg=cfg.pkg,
        )
        self._cfg = cfg
        self._backend = backend

    def __missing__(self, key: str) -> str:
        if key != "python":
            raise KeyError(key)
        value = str(envs.runtime_env(self._cfg, self._backend).python)
        self[key] = value
        return value


def list_tasks(cfg: Config) -> None:
    if not cfg.tasks:
        ui.info("No custom tasks. Add them in pytemplate.toml, section [tasks].")
        return
    ui.step("custom tasks (pytemplate.toml [tasks])")
    for name, task in cfg.tasks.items():
        what = task.help or " ".join(task.cmd) or "deps: " + ", ".join(task.deps)
        ui.info(f"  {name:<14} {what}")


def run_task(cfg: Config, name: str, extra: list[str], dispatch: Dispatcher, stack: tuple[str, ...] = ()) -> int:
    if name in stack:
        raise DeployError(f"task cycle: {' -> '.join((*stack, name))}")
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
    values = Placeholders(cfg, backend)
    try:
        argv = [a.format_map(values) for a in task.cmd] + extra
        extra_env = {k: v.format_map(values) for k, v in task.env.items()}
        cwd = ROOT / task.cwd.format_map(values) if task.cwd else ROOT
    except KeyError as e:
        raise DeployError(f"task '{name}': unknown placeholder {e}") from None
    ui.step(f"task {name}")
    if task.uv:
        env = envs.runtime_env(cfg, backend)
        return envs.uv_run(env, argv, cwd=cwd, extra_env=extra_env, check=False).returncode
    base = proc.base_env()
    base.update(extra_env)
    return proc.run(argv, cwd=cwd, env=base, check=False).returncode
