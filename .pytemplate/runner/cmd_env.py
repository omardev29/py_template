"""Comandos de entorno: setup, doctor, sync, lock, add, remove, clean, shell-setup."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import envs, mypyc, proc, render, ui
from .config import Config
from .project import BUILD, DIST, IS_WINDOWS, IS_WSL, ROOT, rel
from .ui import DeployError


def _envs_for(cfg: Config, target: str) -> list[envs.PyEnv]:
    if target == "all":
        out = [envs.cpython_env(cfg)]
        if cfg.pypy_enabled:
            out.append(envs.pypy_env(cfg))
        if cfg.python.jit:
            out.append(envs.jit_env(cfg))
        return out
    if target in ("cpython", "mypyc"):
        return [envs.runtime_env(cfg, target), envs.tool_env(cfg)] if cfg.python.jit else [envs.cpython_env(cfg)]
    if target == "pypy":
        envs.ensure_supported(cfg, "pypy")
        return [envs.pypy_env(cfg)]
    raise DeployError(f"sync: destino desconocido '{target}' (cpython | pypy | mypyc | all)")


def ensure_lock(cfg: Config) -> None:
    """Aplica las partes gestionadas de pyproject y re-bloquea si hace falta."""
    tool = envs.tool_env(cfg)
    if render.write_pyproject(cfg):
        ui.info("pyproject.toml: actualizadas las partes gestionadas por pytemplate")
    r = envs.uv(tool, ["lock", "--check"], check=False, capture=True, echo=False)
    if r.returncode != 0:
        envs.uv(tool, ["lock"])


def cmd_setup(cfg: Config, args: list[str]) -> int:
    """setup: intérpretes, uv.lock y entornos de todos los backends soportados."""
    ui.step("setup")
    ensure_lock(cfg)
    for env in _envs_for(cfg, "all"):
        ui.step(f"entorno {env.key}: {rel(env.dir)} ({env.request})")
        envs.sync(env)
    _fix_exec_bit()
    render.apply(cfg)
    ui.ok("listo. Prueba: ./deploy run   ·   ./deploy test   ·   ./deploy doctor")
    return 0


def _fix_exec_bit() -> None:
    """El lanzador POSIX `deploy` debe ser ejecutable en git (core.filemode=false en Windows)."""
    if not (ROOT / ".git").exists() or not shutil.which("git"):
        return
    r = proc.run(["git", "ls-files", "-s", "deploy"], capture=True, check=False, echo=False)
    if r.stdout.startswith("100644"):
        proc.run(["git", "update-index", "--chmod=+x", "deploy"], check=False)


def cmd_sync(cfg: Config, args: list[str]) -> int:
    """sync [cpython|pypy|mypyc|all]: `uv sync --locked` del entorno o entornos."""
    target = args[0] if args else "all"
    for env in _envs_for(cfg, target):
        envs.sync(env)
    return 0


def cmd_lock(cfg: Config, args: list[str]) -> int:
    """lock [--upgrade] [--upgrade-package PAQ]: aplica pyproject gestionado y `uv lock`."""
    if render.write_pyproject(cfg):
        ui.info("pyproject.toml: actualizadas las partes gestionadas por pytemplate")
    envs.uv(envs.tool_env(cfg), ["lock", *args])
    render.apply(cfg)
    return 0


def _add_remove(cfg: Config, verb: str, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"./deploy {verb}")
    parser.add_argument("packages", nargs="+")
    parser.add_argument("--dev", action="store_true", help="grupo de desarrollo")
    parser.add_argument("--group", help="grupo de dependencias")
    if verb == "add":
        parser.add_argument(
            "--cpython-only",
            action="store_true",
            help="solo en CPython/mypyc (librerías de la C-API como numpy: van lentas o no existen en PyPy)",
        )
    ns = parser.parse_args(args)
    argv: list[str] = [verb]
    if ns.dev:
        argv.append("--dev")
    if ns.group:
        argv += ["--group", ns.group]
    if verb == "add" and ns.cpython_only:
        argv += ["--marker", "implementation_name == 'cpython'"]
    argv += ns.packages
    envs.uv(envs.tool_env(cfg), argv)
    if verb == "add" and cfg.pypy_enabled and not ns.cpython_only:
        ui.info(
            "PyPy está soportado: si el paquete usa la C-API de CPython (numpy, pillow, pydantic-core...) "
            "irá lento en PyPy; considera `--cpython-only`. Comprueba con: ./deploy sync pypy"
        )
    return 0


def cmd_add(cfg: Config, args: list[str]) -> int:
    """add PAQ... [--dev|--group G] [--cpython-only]"""
    return _add_remove(cfg, "add", args)


def cmd_remove(cfg: Config, args: list[str]) -> int:
    """remove PAQ... [--dev|--group G]"""
    return _add_remove(cfg, "remove", args)


def cmd_clean(cfg: Config, args: list[str]) -> int:
    """clean [--envs]: borra .build/ y dist/ (y los entornos .venv* con --envs)."""
    targets = [BUILD, DIST]
    if "--envs" in args:
        targets += sorted(p for p in ROOT.glob(".venv*") if p.is_dir())
    for t in targets:
        if t.exists():
            ui.info(f"borrando {rel(t)}")
            if not proc.DRY_RUN:
                shutil.rmtree(t, ignore_errors=True)
    return 0


# --- doctor --------------------------------------------------------------------------------------


def _msvc() -> tuple[bool, str]:
    vs = proc.vs_installer_dir()
    if not vs:
        return False, "no hay Visual Studio / Build Tools (no existe vswhere.exe)"
    r = subprocess.run(
        [str(vs / "vswhere.exe"), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
        capture_output=True,
        text=True,
        check=False,
    )
    path = r.stdout.strip()
    if not path:
        return False, "Visual Studio encontrado, pero sin las herramientas de C++ (VC.Tools.x86.x64)"
    return True, path


def _c_compiler() -> tuple[bool, str]:
    if IS_WINDOWS:
        return _msvc()
    for cc in (os.environ.get("CC"), "cc", "gcc", "clang"):
        if cc and shutil.which(cc):
            return True, shutil.which(cc) or cc
    return False, "no hay compilador de C"


def cmd_doctor(cfg: Config, args: list[str]) -> int:
    """doctor: comprueba requisitos, entornos y archivos generados."""
    problems = 0

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        nonlocal problems
        if passed is False:
            problems += 1
        ui.check_line(passed, label, hint)

    ui.step("herramientas")
    uv_version = proc.output([proc.find_uv(), "--version"])
    check(True, f"uv: {uv_version}")
    check(True, f"runner: Python {sys.version.split()[0]} ({sys.executable})")

    ui.step(f"backends (activo: {cfg.backend.active}; soportados: {', '.join(cfg.backend.supported)})")
    cp = envs.cpython_env(cfg)
    if cp.python.is_file():
        info = envs.interpreter_info(cp.python)
        check(True, f"CPython {info['version']} en {rel(cp.dir)}  (JIT disponible: {'sí' if info['jit'] else 'no'})")
    else:
        check(False, f"falta el entorno {rel(cp.dir)}", "./deploy setup")
    if cfg.pypy_enabled:
        pp = envs.pypy_env(cfg)
        if pp.python.is_file():
            info = envs.interpreter_info(pp.python)
            check(info["impl"] == "pypy", f"PyPy ({info['version']}) en {rel(pp.dir)}")
        else:
            check(False, f"falta el entorno {rel(pp.dir)} ({cfg.python.pypy})", "./deploy setup   (o ./deploy sync pypy)")
    if cfg.python.jit:
        try:
            jit_python = envs.find_jit_interpreter(cfg)
            check(True, f"JIT: {jit_python}")
            if "scoop" in jit_python.lower() and "\\current\\" in jit_python.lower():
                check(
                    None,
                    "el Python del JIT es el enlace `current` de scoop: `scoop update` lo pasará a otra versión",
                    f"Fíjalo: scoop install versions/python{cfg.python.cpython.replace('.', '')} y pon su ruta en python.jit_interpreter",
                )
        except DeployError as e:
            check(False, "JIT: no hay CPython con JIT", str(e))
    if cfg.supports("mypyc"):
        found, where = _c_compiler()
        check(found, f"compilador C para mypyc: {where}", mypyc.has_compiler_hint())
    if IS_WINDOWS and cfg.supports("mypyc"):
        long_paths = _long_paths()
        check(
            True if long_paths else None,
            "rutas largas de Windows (LongPathsEnabled)" + ("" if long_paths else ": desactivadas (opcional)"),
            "Evita errores de MSVC si el proyecto está en una ruta muy profunda (>260 caracteres)",
        )

    ui.step("proyecto")
    changed, edited = render.apply(cfg, check=True)
    check(not changed and not edited, "archivos generados al día", "Se actualizan con cualquier comando (o ./deploy render)")
    for path in edited:
        check(False, f"{path} editado a mano", "Edita pytemplate.toml o .pytemplate/templates, o ./deploy render --force")
    check(not render.pyproject_outdated(cfg), "pyproject.toml coincide con pytemplate.toml", "./deploy lock")
    r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], check=False, capture=True, echo=False)
    check(r.returncode == 0, "uv.lock al día", "./deploy lock")
    launcher = (ROOT / "deploy").read_bytes() if (ROOT / "deploy").is_file() else b""
    check(launcher.startswith(b"#!/bin/sh") and b"\r\n" not in launcher, "lanzador ./deploy: #!/bin/sh con finales LF")

    ui.step("shell")
    if IS_WINDOWS:
        bash = shutil.which("bash") or ""
        if "system32" in bash.lower():
            check(None, f"`bash` apunta a WSL ({bash})", "En Windows usa ./deploy desde xonsh, pwsh o cmd; en WSL el runner usa entornos -wsl aparte")
        policy = _ps_policy()
        if policy:
            check(policy not in ("Restricted", "AllSigned"), f"PowerShell: ExecutionPolicy = {policy}", "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   (o usa .\\deploy.cmd)")
    if IS_WSL:
        check(None, "WSL sobre /mnt: entornos .venv*-wsl y .build/wsl separados de Windows")
    ui.info("")
    if problems:
        ui.error(f"{problems} problema(s)")
        return 1
    ui.ok("todo en orden")
    return 0


def _long_paths() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return bool(value)
    except OSError:
        return False


def _ps_policy() -> str:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return ""
    r = subprocess.run([exe, "-NoProfile", "-Command", "Get-ExecutionPolicy"], capture_output=True, text=True, check=False)
    return r.stdout.strip()


# --- shell-setup ---------------------------------------------------------------------------------


XONSH_SNIPPET = r'''
# --- ./deploy sin "./" en cualquier proyecto de la plantilla (pegar en ~/.xonshrc) ---
from pathlib import Path as _DeployPath

def _deploy_find():
    for d in (_DeployPath.cwd(), *_DeployPath.cwd().parents):
        if (d / ".pytemplate" / "deploy.py").is_file():
            return d / ".pytemplate" / "deploy.py"
    return None

def _deploy_complete(context):
    words = "setup doctor mode render sync lock add remove init new run check lint fmt test report build clean tasks shell-setup help cpython pypy mypyc all".split()
    return {w for w in words if w.startswith(context.prefix)} if hasattr(context, "prefix") else set(words)

@aliases.register("deploy")
@aliases.return_command
def _deploy(args):
    script = _deploy_find()
    if script is None:
        return ["echo", "deploy: no hay .pytemplate/deploy.py en este directorio ni en sus padres"]
    return ["uv", "run", "--quiet", "--script", str(script), *args]
'''

PWSH_SNIPPET = r"""
# --- ./deploy sin ".\" en cualquier proyecto de la plantilla (pegar en $PROFILE) ---
function deploy {
    $d = Get-Item -LiteralPath (Get-Location)
    while ($d -and -not (Test-Path (Join-Path $d.FullName '.pytemplate/deploy.py'))) { $d = $d.Parent }
    if (-not $d) { Write-Error 'deploy: no hay .pytemplate/deploy.py aqui ni en los padres'; return }
    uv run --quiet --script (Join-Path $d.FullName '.pytemplate/deploy.py') @args
}
"""

BASH_SNIPPET = r"""
# --- ./deploy sin "./" en cualquier proyecto de la plantilla (pegar en ~/.bashrc o ~/.zshrc) ---
deploy() {
    d=$PWD
    while [ "$d" != "/" ] && [ ! -f "$d/.pytemplate/deploy.py" ]; do d=$(dirname "$d"); done
    if [ ! -f "$d/.pytemplate/deploy.py" ]; then echo "deploy: no hay .pytemplate/deploy.py" >&2; return 1; fi
    uv run --quiet --script "$d/.pytemplate/deploy.py" "$@"
}
"""


def cmd_shell_setup(cfg: Config, args: list[str]) -> int:
    """shell-setup xonsh|pwsh|bash: imprime un alias `deploy` para usarlo sin ./"""
    shell = args[0] if args else "xonsh"
    snippets = {"xonsh": XONSH_SNIPPET, "pwsh": PWSH_SNIPPET, "powershell": PWSH_SNIPPET, "bash": BASH_SNIPPET, "zsh": BASH_SNIPPET}
    if shell not in snippets:
        raise DeployError(f"shell-setup: shell desconocido '{shell}' (xonsh | pwsh | bash | zsh)")
    print(snippets[shell].strip("\n"))
    return 0


def uv_version_tuple() -> tuple[int, ...]:
    out = proc.output([proc.find_uv(), "--version"])
    parts = out.split()[1].split(".") if len(out.split()) > 1 else []
    return tuple(int(p) for p in parts if p.isdigit())


def managed_python_dir() -> Path | None:
    try:
        return Path(proc.output([proc.find_uv(), "python", "dir"]))
    except DeployError:
        return None
