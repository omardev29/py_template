"""Environment commands: setup, doctor, sync, lock, add, remove, clean, shell-setup."""

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
    raise DeployError(f"sync: unknown target '{target}' (cpython | pypy | mypyc | all)")


def ensure_lock(cfg: Config) -> None:
    """Apply the managed parts of pyproject and re-lock if needed."""
    tool = envs.tool_env(cfg)
    if render.write_pyproject(cfg):
        ui.info("pyproject.toml: updated the parts managed by pytemplate")
    r = envs.uv(tool, ["lock", "--check"], check=False, capture=True, echo=False)
    if r.returncode != 0:
        envs.uv(tool, ["lock"])


def cmd_setup(cfg: Config, args: list[str]) -> int:
    """setup: interpreters, uv.lock and the environments of every supported backend."""
    ui.step("setup")
    ensure_lock(cfg)
    for env in _envs_for(cfg, "all"):
        ui.step(f"environment {env.key}: {rel(env.dir)} ({env.request})")
        envs.sync(env)
    _fix_exec_bit()
    render.apply(cfg)
    ui.ok("done. Try: ./deploy run   ·   ./deploy test   ·   ./deploy doctor")
    return 0


def _fix_exec_bit() -> None:
    """Make the POSIX launcher `deploy` executable in git (core.filemode=false on Windows)."""
    if not (ROOT / ".git").exists() or not shutil.which("git"):
        return
    r = proc.run(["git", "ls-files", "-s", "deploy"], capture=True, check=False, echo=False)
    if r.stdout.startswith("100644"):
        proc.run(["git", "update-index", "--chmod=+x", "deploy"], check=False)


def cmd_sync(cfg: Config, args: list[str]) -> int:
    """sync [cpython|pypy|mypyc|all]: `uv sync --locked` of the environment(s)."""
    target = args[0] if args else "all"
    for env in _envs_for(cfg, target):
        envs.sync(env)
    return 0


def cmd_lock(cfg: Config, args: list[str]) -> int:
    """lock [--upgrade] [--upgrade-package PKG]: apply the managed pyproject parts and `uv lock`."""
    if render.write_pyproject(cfg):
        ui.info("pyproject.toml: updated the parts managed by pytemplate")
    envs.uv(envs.tool_env(cfg), ["lock", *args])
    render.apply(cfg)
    return 0


def _add_remove(cfg: Config, verb: str, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"./deploy {verb}")
    parser.add_argument("packages", nargs="+")
    parser.add_argument("--dev", action="store_true", help="development group")
    parser.add_argument("--group", help="dependency group")
    if verb == "add":
        parser.add_argument(
            "--cpython-only",
            action="store_true",
            help="only on CPython/mypyc (C-API libraries such as numpy: slow or unavailable on PyPy)",
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
            "PyPy is supported: if the package uses the CPython C-API (numpy, pillow, pydantic-core...) "
            "it will be slow on PyPy; consider `--cpython-only`. Check with: ./deploy sync pypy"
        )
    return 0


def cmd_add(cfg: Config, args: list[str]) -> int:
    """add PKG... [--dev|--group G] [--cpython-only]"""
    return _add_remove(cfg, "add", args)


def cmd_remove(cfg: Config, args: list[str]) -> int:
    """remove PKG... [--dev|--group G]"""
    return _add_remove(cfg, "remove", args)


def cmd_clean(cfg: Config, args: list[str]) -> int:
    """clean [--envs]: remove .build/ and dist/ (and the .venv* environments with --envs)."""
    targets = [BUILD, DIST]
    if "--envs" in args:
        targets += sorted(p for p in ROOT.glob(".venv*") if p.is_dir())
    for t in targets:
        if t.exists():
            ui.info(f"removing {rel(t)}")
            if not proc.DRY_RUN:
                shutil.rmtree(t, ignore_errors=True)
    return 0


# --- doctor --------------------------------------------------------------------------------------


def _msvc() -> tuple[bool, str]:
    vs = proc.vs_installer_dir()
    if not vs:
        return False, "no Visual Studio / Build Tools (vswhere.exe not found)"
    r = subprocess.run(
        [str(vs / "vswhere.exe"), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
        capture_output=True,
        text=True,
        check=False,
    )
    path = r.stdout.strip()
    if not path:
        return False, "Visual Studio found, but without the C++ tools (VC.Tools.x86.x64)"
    return True, path


def _c_compiler() -> tuple[bool, str]:
    if IS_WINDOWS:
        return _msvc()
    for cc in (os.environ.get("CC"), "cc", "gcc", "clang"):
        if cc and shutil.which(cc):
            return True, shutil.which(cc) or cc
    return False, "no C compiler"


def cmd_doctor(cfg: Config, args: list[str]) -> int:
    """doctor: check requirements, environments and generated files."""
    problems = 0

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        nonlocal problems
        if passed is False:
            problems += 1
        ui.check_line(passed, label, hint)

    ui.step("tools")
    uv_version = proc.output([proc.find_uv(), "--version"])
    check(True, f"uv: {uv_version}")
    check(True, f"runner: Python {sys.version.split()[0]} ({sys.executable})")

    ui.step(f"backends (active: {cfg.backend.active}; supported: {', '.join(cfg.backend.supported)})")
    cp = envs.cpython_env(cfg)
    if cp.python.is_file():
        info = envs.interpreter_info(cp.python)
        check(True, f"CPython {info['version']} in {rel(cp.dir)}  (JIT available: {'yes' if info['jit'] else 'no'})")
    else:
        check(False, f"environment {rel(cp.dir)} is missing", "./deploy setup")
    if cfg.pypy_enabled:
        pp = envs.pypy_env(cfg)
        if pp.python.is_file():
            info = envs.interpreter_info(pp.python)
            check(info["impl"] == "pypy", f"PyPy ({info['version']}) in {rel(pp.dir)}")
        else:
            check(False, f"environment {rel(pp.dir)} is missing ({cfg.python.pypy})", "./deploy setup   (or ./deploy sync pypy)")
    if cfg.python.jit:
        try:
            jit_python = envs.find_jit_interpreter(cfg)
            check(True, f"JIT: {jit_python}")
            if "scoop" in jit_python.lower() and "\\current\\" in jit_python.lower():
                check(
                    None,
                    "the JIT Python is the scoop `current` link: `scoop update` will switch it to another version",
                    f"Pin it: scoop install versions/python{cfg.python.cpython.replace('.', '')} and put its path in python.jit_interpreter",
                )
        except DeployError as e:
            check(False, "JIT: no CPython with JIT", str(e))
    if cfg.supports("mypyc"):
        found, where = _c_compiler()
        check(found, f"C compiler for mypyc: {where}", mypyc.has_compiler_hint())
    if IS_WINDOWS and cfg.supports("mypyc"):
        long_paths = _long_paths()
        check(
            True if long_paths else None,
            "Windows long paths (LongPathsEnabled)" + ("" if long_paths else ": disabled (optional)"),
            "Avoids MSVC errors when the project is in a very deep path (>260 characters)",
        )

    ui.step("project")
    changed, edited = render.apply(cfg, check=True)
    check(not changed and not edited, "generated files up to date", "They update with any command (or ./deploy render)")
    for path in edited:
        check(False, f"{path} hand-edited", "Edit pytemplate.toml or .pytemplate/templates, or ./deploy render --force")
    check(not render.pyproject_outdated(cfg), "pyproject.toml matches pytemplate.toml", "./deploy lock")
    r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], check=False, capture=True, echo=False)
    check(r.returncode == 0, "uv.lock up to date", "./deploy lock")
    launcher = (ROOT / "deploy").read_bytes() if (ROOT / "deploy").is_file() else b""
    check(launcher.startswith(b"#!/bin/sh") and b"\r\n" not in launcher, "launcher ./deploy: #!/bin/sh with LF line endings")

    ui.step("shell")
    if IS_WINDOWS:
        bash = shutil.which("bash") or ""
        if "system32" in bash.lower():
            check(None, f"`bash` points to WSL ({bash})", "On Windows use ./deploy from xonsh, pwsh or cmd; in WSL the runner uses separate -wsl environments")
        policy = _ps_policy()
        if policy:
            check(policy not in ("Restricted", "AllSigned"), f"PowerShell: ExecutionPolicy = {policy}", "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   (or use .\\deploy.cmd)")
    if IS_WSL:
        check(None, "WSL on /mnt: .venv*-wsl environments and .build/wsl kept separate from Windows")
    ui.info("")
    if problems:
        ui.error(f"{problems} problem(s)")
        return 1
    ui.ok("all good")
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
# --- ./deploy without "./" in any project made from the template (paste into ~/.xonshrc) ---
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
        return ["echo", "deploy: no .pytemplate/deploy.py in this directory or its parents"]
    return ["uv", "run", "--quiet", "--script", str(script), *args]
'''

PWSH_SNIPPET = r"""
# --- ./deploy without ".\" in any project made from the template (paste into $PROFILE) ---
function deploy {
    $d = Get-Item -LiteralPath (Get-Location)
    while ($d -and -not (Test-Path (Join-Path $d.FullName '.pytemplate/deploy.py'))) { $d = $d.Parent }
    if (-not $d) { Write-Error 'deploy: no .pytemplate/deploy.py here or in any parent directory'; return }
    uv run --quiet --script (Join-Path $d.FullName '.pytemplate/deploy.py') @args
}
"""

BASH_SNIPPET = r"""
# --- ./deploy without "./" in any project made from the template (paste into ~/.bashrc or ~/.zshrc) ---
deploy() {
    d=$PWD
    while [ "$d" != "/" ] && [ ! -f "$d/.pytemplate/deploy.py" ]; do d=$(dirname "$d"); done
    if [ ! -f "$d/.pytemplate/deploy.py" ]; then echo "deploy: no .pytemplate/deploy.py found" >&2; return 1; fi
    uv run --quiet --script "$d/.pytemplate/deploy.py" "$@"
}
"""


def cmd_shell_setup(cfg: Config, args: list[str]) -> int:
    """shell-setup xonsh|pwsh|bash: print a `deploy` alias to use it without ./"""
    shell = args[0] if args else "xonsh"
    snippets = {"xonsh": XONSH_SNIPPET, "pwsh": PWSH_SNIPPET, "powershell": PWSH_SNIPPET, "bash": BASH_SNIPPET, "zsh": BASH_SNIPPET}
    if shell not in snippets:
        raise DeployError(f"shell-setup: unknown shell '{shell}' (xonsh | pwsh | bash | zsh)")
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
