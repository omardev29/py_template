"""Custom Justfile-style recipes: the [tasks] table of pytemplate.toml.

    [tasks.gen]
    help = "Generate the assets"
    cmd = ["python", "scripts/gen.py", "{backend}"]   # argv, no shell: the same in every shell
    deps = ["check"]                                   # other tasks or ./deploy commands
    env = { SEED = "42" }
    backend = "pypy"   # environment it runs in (empty = active backend; "mypyc" = the .venv,
                       # interpreted: add deps = ["compile"] and run the stage to use the binaries)
    uv = true          # true: `uv run` inside that environment; false: run the program as-is

Placeholders in cmd/env/cwd: {root} {src} {build} {dist} {backend} {name} {pkg} {python}; write
a literal brace doubled ({{ and }}). {python} (the backend's interpreter) is only resolved when
used; with uv = false its environment is synced first if it does not exist yet (a fresh clone,
git clean -fdx). Extra arguments to `./deploy <task> ...` are appended to the end of cmd; a task
without cmd (deps only) takes none (exit 2). Every dependency runs at most once per ./deploy
invocation, like just: a dependency shared by two others (a diamond) runs once. With uv = false
a relative program with a folder in it (tools/gen.sh) runs from the task's cwd on every OS, and
on Windows a bare name is found on PATH with PATHEXT, as in a shell (npm -> npm.cmd).
"""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Callable

from . import config, envs, proc, ui
from .config import Config, TaskConfig
from .project import BUILD, DIST, IS_WINDOWS, ROOT, SRC
from .ui import DeployError

Dispatcher = Callable[[list[str]], int]


def _task_env(cfg: Config, backend: str) -> envs.PyEnv:
    """The environment a task runs in. PyPy is the only backend with its own environment, which
    uv.lock only covers while it is supported: fail with the fix, not with uv's requires-python
    error after downloading PyPy. cpython and mypyc always run in .venv."""
    if backend == "pypy":
        envs.ensure_supported(cfg, backend)
    return envs.runtime_env(cfg, backend)


class Placeholders(dict[str, str]):
    """The placeholder values. {python} (the backend's interpreter) is resolved only when a task uses it."""

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
        self.env: envs.PyEnv | None = None  # the environment of {python}, once it is used

    def __missing__(self, key: str) -> str:
        if key != "python":
            raise KeyError(key)
        self.env = _task_env(self._cfg, self._backend)
        value = str(self.env.python)
        self[key] = value
        return value


def describe(task: TaskConfig) -> str:
    """One line for listings: its help, else its command, else its deps."""
    return task.help or " ".join(task.cmd) or "deps: " + ", ".join(task.deps)


def list_tasks(cfg: Config) -> None:
    if not cfg.tasks:
        ui.report("No custom tasks. Add them in pytemplate.toml, section [tasks].")
        return
    ui.step("custom tasks (pytemplate.toml [tasks])")
    for name, task in cfg.tasks.items():
        ui.report(f"  {name:<14} {describe(task)}")


def _dep_argv(name: str, dep: str) -> list[str]:
    try:
        argv = shlex.split(dep)
    except ValueError as e:  # unbalanced quotes
        raise DeployError(f"task '{name}': deps entry {dep!r}: {e}") from None
    if not argv:
        raise DeployError(f"task '{name}': empty deps entry")
    return argv


def _format(name: str, text: str, values: Placeholders) -> str:
    # The syntax is checked first (config.validate does it at load, this also covers a Config
    # built without it), so format_map can only miss a name: '{}', '{0}', a lone '{' and
    # '{root.x}' are never a traceback.
    problem = config.task_format_error(text)
    if problem:
        raise DeployError(f"task '{name}': {text!r}: {problem}")
    try:
        return text.format_map(values)
    except KeyError as e:
        known = " ".join("{" + p + "}" for p in config.TASK_PLACEHOLDERS)
        raise DeployError(f"task '{name}': unknown placeholder {e} in {text!r} (placeholders: {known}; {config.BRACES_HINT})") from None


def run_task(
    cfg: Config,
    name: str,
    extra: list[str],
    dispatch: Dispatcher,
    stack: tuple[str, ...] = (),
    done: set[tuple[str, ...]] | None = None,
) -> int:
    """Run a [tasks] entry: its deps in order (stopping at the first failure, whose code is
    returned), then its cmd with `extra` appended; return the cmd's exit code.

    `done` holds the deps that already succeeded in this invocation: each runs at most once.
    """
    if name in stack:
        raise DeployError(f"task cycle: {' -> '.join((*stack, name))}")
    task = cfg.tasks[name]
    if extra and not task.cmd:
        # Checked before any dep runs: `./deploy ci --help` must not start the whole CI, and
        # `./deploy bunnymark mypyc` must not measure the active backend instead
        raise DeployError(f"task '{name}' only runs its deps ({', '.join(task.deps)}) and takes no arguments: {shlex.join(extra)}")
    if done is None:
        done = set()
    deps = [(dep, _dep_argv(name, dep)) for dep in task.deps]  # all parsed before the first runs
    for dep, argv in deps:
        key = tuple(argv)
        if key in done:
            ui.info(f"task {name}: {dep!r} already ran")
            continue
        if argv[0] in cfg.tasks:
            code = run_task(cfg, argv[0], argv[1:], dispatch, (*stack, name), done)
        else:
            code = dispatch(argv)
        if code != 0:
            return code
        done.add(key)
    if not task.cmd:
        return 0
    backend = task.backend or cfg.backend.active
    values = Placeholders(cfg, backend)
    argv = [_format(name, a, values) for a in task.cmd] + extra
    extra_env = {k: _format(name, v, values) for k, v in task.env.items()}
    cwd = ROOT / _format(name, task.cwd, values) if task.cwd else ROOT
    if not proc.DRY_RUN and not cwd.is_dir():  # a dep may create it (a dry run skips the deps)
        raise DeployError(f"task '{name}': cwd {task.cwd!r} is not a folder ({cwd})")
    if task.uv:
        env = _task_env(cfg, backend)
        ui.step(f"task {name}")
        return envs.uv_run(env, argv, cwd=cwd, extra_env=extra_env, check=False).returncode
    ui.step(f"task {name}")
    if values.env is not None and not values.env.python.exists():
        envs.sync(values.env)  # {python} of an environment that does not exist yet
    base = proc.base_env()
    base.update(extra_env)
    program = argv[0]
    if not os.path.isabs(program) and any(sep and sep in program for sep in (os.sep, os.altsep)):
        # Windows (CreateProcess) resolves a relative program against the runner's cwd (the
        # caller's folder), not cwd=: anchor tools/gen.sh to the task cwd, as POSIX does
        argv[0] = str(cwd / program)
    elif IS_WINDOWS and not os.path.isabs(program):
        # A bare name: CreateProcess only tries `<name>.exe`, so npm, yarn or mvn (npm.cmd...)
        # were "not found". Search the task's PATH with PATHEXT, as a shell does (not found:
        # the name stays, and proc.run says so)
        found = shutil.which(program, path=base.get("PATH", ""))
        if found:
            argv[0] = os.path.abspath(found)
    return proc.run(argv, cwd=cwd, env=base, check=False).returncode
