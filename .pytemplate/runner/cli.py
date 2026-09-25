"""./deploy: runner estilo Justfile, sin binarios extra (solo uv).

    ./deploy [-v|-q] [--dry-run] [--no-render] COMANDO [argumentos...]

Las opciones globales van ANTES del comando; todo lo que va después es del comando
(y en `run`/`test`, de tu app o de pytest).
"""

from __future__ import annotations

import importlib
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from . import proc, ui
from .ui import DeployError


@dataclass(frozen=True)
class Command:
    module: str
    func: str
    summary: str
    usage: str = ""
    render: bool = True  # regenerar configs antes de ejecutarlo
    group: str = ""


COMMANDS: dict[str, Command] = {
    # entorno
    "setup": Command("cmd_env", "cmd_setup", "Instala intérpretes y entornos de los backends, bloquea deps y genera configs", group="Entorno"),
    "doctor": Command("cmd_env", "cmd_doctor", "Comprueba uv, compilador, PyPy, JIT, shells y archivos generados", group="Entorno"),
    "sync": Command("cmd_env", "cmd_sync", "uv sync --locked de uno o todos los entornos", "[cpython|pypy|mypyc|all]", group="Entorno"),
    "lock": Command("cmd_env", "cmd_lock", "Aplica lo gestionado de pyproject y re-bloquea uv.lock", "[--upgrade] [--upgrade-package PAQ]", group="Entorno"),
    "add": Command("cmd_env", "cmd_add", "Añade dependencias (uv add)", "PAQ... [--dev|--group G] [--cpython-only]", group="Entorno"),
    "remove": Command("cmd_env", "cmd_remove", "Quita dependencias (uv remove)", "PAQ... [--dev|--group G]", group="Entorno"),
    "clean": Command("cmd_env", "cmd_clean", "Borra .build/ y dist/ (y los entornos con --envs)", "[--envs]", render=False, group="Entorno"),
    # modo y plantilla
    "mode": Command("cmd_mode", "cmd_mode", "Muestra o cambia el modo (backend, soportados, tipado, JIT, editor)", "[BACKEND] [--supports +pypy|-pypy] [--typing off|warn|strict|auto] [--jit on|off] [--editor pylance|basedpyright]", group="Modo"),
    "render": Command("cmd_mode", "cmd_render", "Regenera .mypy.ini, pyrightconfig.json, .ruff.toml y .vscode/", "[--check] [--diff] [--force]", render=False, group="Modo"),
    "init": Command("cmd_mode", "cmd_init", "Convierte este proyecto a un preset (script, raylib, flet)", "PRESET [--name NOMBRE] [--force]", group="Modo"),
    "new": Command("cmd_mode", "cmd_new", "Crea un proyecto nuevo a partir de esta plantilla", "CARPETA [--preset P] [--name NOMBRE]", render=False, group="Modo"),
    # desarrollo
    "run": Command("cmd_dev", "cmd_run", "Ejecuta la app (mypyc: compila antes)", "[BACKEND] [argumentos de la app...]", group="Desarrollo"),
    "check": Command("cmd_dev", "cmd_check", "ruff + mypy con el perfil de tipado del backend + reglas de mypyc", "[BACKEND|all]", group="Desarrollo"),
    "lint": Command("cmd_dev", "cmd_lint", "ruff check", "[--fix]", group="Desarrollo"),
    "fmt": Command("cmd_dev", "cmd_fmt", "ruff format", "[--check]", group="Desarrollo"),
    "test": Command("cmd_dev", "cmd_test", "pytest en un backend (mypyc: contra los .pyd) o en todos", "[BACKEND|all] [argumentos de pytest...]", group="Desarrollo"),
    "report": Command("cmd_dev", "cmd_report", "Informe HTML de mypyc con las líneas lentas + Any de mypy", "[--open] [--no-mypy]", group="Desarrollo"),
    # distribución
    "build": Command("cmd_build", "cmd_build", "Compila y empaqueta en dist/", "[BACKEND] [--method exe|portable|pyz|wheel|nuitka|flet] [--onefile|--onedir] [--target CLAVE]... [--no-check]", group="Distribución"),
    "pyz-merge": Command("cmd_build", "cmd_pyz_merge", "Une los .pyz de cada sistema (p. ej. de la CI) en uno multiplataforma", "A.pyz B.pyz... --out C.pyz", render=False, group="Distribución"),
    # otros
    "tasks": Command("cli", "cmd_tasks", "Lista las tareas propias de pytemplate.toml [tasks]", render=False, group="Otros"),
    "shell-setup": Command("cmd_env", "cmd_shell_setup", "Imprime un alias para usar `deploy` sin ./", "[xonsh|pwsh|bash|zsh]", render=False, group="Otros"),
    "selftest": Command("cli", "cmd_selftest", "Tests y mypy --strict del propio runner (.pytemplate)", render=False, group="Otros"),
    "help": Command("cli", "cmd_help", "Esta ayuda (o la de un comando)", "[COMANDO]", render=False, group="Otros"),
}

EXAMPLES = """\
Ejemplos:
  ./deploy setup                 # primera vez: intérpretes, entornos y configs
  ./deploy run                   # ejecuta con el backend activo (pytemplate.toml)
  ./deploy run mypyc --verbose   # compila con mypyc y ejecuta (--verbose va a tu app)
  ./deploy test all              # pytest en cada backend soportado
  ./deploy mode mypyc            # cambia el backend activo (tipado estricto)
  ./deploy mode --supports +pypy # añade PyPy (sintaxis 3.11)
  ./deploy build mypyc           # exe con PyInstaller (método por defecto de mypyc)
  ./deploy build pypy            # carpeta portable con PyPy dentro
  ./deploy build cpython --method pyz"""


def cmd_help(cfg: object, args: list[str]) -> int:
    if args and args[0] in COMMANDS:
        c = COMMANDS[args[0]]
        print(f"./deploy {args[0]} {c.usage}".rstrip())
        print(f"  {c.summary}")
        return 0
    print("./deploy [-v|-q] [--dry-run] [--no-render] COMANDO [argumentos...]\n")
    groups: dict[str, list[str]] = {}
    for name, c in COMMANDS.items():
        groups.setdefault(c.group, []).append(name)
    for group, names in groups.items():
        print(f"{group}:")
        for name in names:
            print(f"  {name:<12} {COMMANDS[name].summary}")
        print()
    try:
        from . import config

        loaded = config.load(set(COMMANDS))
        if loaded.tasks:
            print("Tareas propias (pytemplate.toml [tasks]):")
            for name, task in loaded.tasks.items():
                print(f"  {name:<12} {task.help or ' '.join(task.cmd)}")
            print()
    except DeployError:
        pass
    print("BACKEND = cpython | pypy | mypyc (por defecto, backend.active de pytemplate.toml)\n")
    print(EXAMPLES)
    return 0


def cmd_tasks(cfg: object, args: list[str]) -> int:
    from . import tasks
    from .config import Config

    assert isinstance(cfg, Config)
    tasks.list_tasks(cfg)
    return 0


def cmd_selftest(cfg: object, args: list[str]) -> int:
    from . import envs
    from .config import Config
    from .project import TEMPLATE

    assert isinstance(cfg, Config)
    tool = envs.tool_env(cfg)
    code = envs.uv_run(tool, ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", TEMPLATE / "tests", *args], check=False).returncode
    typed = envs.uv_run(
        tool,
        ["mypy", "--strict", "--no-incremental", "--python-version", "3.11", "--config-file", TEMPLATE / "tests" / "mypy-runner.ini", TEMPLATE / "runner", TEMPLATE / "deploy.py"],
        check=False,
    ).returncode
    return code or typed


def _parse_globals(argv: list[str]) -> list[str]:
    rest = list(argv)
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in ("-v", "--verbose"):
            ui.VERBOSE = True
        elif flag in ("-q", "--quiet"):
            ui.QUIET = True
        elif flag == "--dry-run":
            proc.DRY_RUN = True
        elif flag == "--no-render":
            _OPTS["no_render"] = True
        elif flag in ("-h", "--help"):
            return ["help"]
        else:
            raise DeployError(f"opción global desconocida: {flag}  (las opciones del comando van DESPUÉS del comando)")
    return rest


_OPTS: dict[str, bool] = {"no_render": False}


def dispatch(argv: list[str]) -> int:
    from . import config, render, tasks

    if not argv or argv[0] == "help":
        return cmd_help(None, argv[1:])
    name, args = argv[0], argv[1:]
    cfg = config.load(set(COMMANDS))
    command = COMMANDS.get(name)
    if command is None:
        if name in cfg.tasks:
            if not _OPTS["no_render"]:
                render.auto(cfg)
            return tasks.run_task(cfg, name, args, dispatch)
        raise DeployError(f"comando desconocido: {name}  (./deploy help)")
    if command.render and not _OPTS["no_render"]:
        render.auto(cfg)
    module = importlib.import_module(f"{__package__}.{command.module}")
    func: Callable[[object, list[str]], int] = getattr(module, command.func)
    return func(cfg, args)


def main(argv: list[str]) -> int:
    try:
        return dispatch(_parse_globals(argv))
    except DeployError as e:
        ui.error(str(e))
        return e.code
    except KeyboardInterrupt:
        ui.error("interrumpido")
        return 130
    except SystemExit as e:  # argparse
        return e.code if isinstance(e.code, int) else 2
    except Exception:
        traceback.print_exc()
        ui.error("fallo interno del runner (lo de arriba es un bug de .pytemplate/runner)")
        return 1
    finally:
        sys.stdout.flush()
