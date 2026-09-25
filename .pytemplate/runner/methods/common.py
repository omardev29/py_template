"""Piezas comunes de portable y pyz: claves de plataforma, dependencias por destino."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import envs, proc, ui
from ..config import Config
from ..imports import iter_runtime_nodes, parse
from ..project import BUILD, EXT_SUFFIXES, SRC, host_arch, host_os
from ..ui import DeployError

NATIVE_SUFFIXES = (*EXT_SUFFIXES, ".dll", ".dylib")
KEY_RE = re.compile(r"^(cp|pp)(\d)(\d+)-(windows|linux|macos)-(x86_64|aarch64)$")
# Plataforma de uv (--python-platform) para cada (so, arquitectura)
UV_PLATFORMS = {
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
    ("windows", "aarch64"): "aarch64-pc-windows-msvc",
    ("linux", "x86_64"): "x86_64-manylinux_2_28",
    ("linux", "aarch64"): "aarch64-manylinux_2_35",
    ("macos", "x86_64"): "x86_64-apple-darwin",
    ("macos", "aarch64"): "aarch64-apple-darwin",
}


@dataclass(frozen=True)
class Target:
    impl: str  # cp | pp
    major: int
    minor: int
    os: str
    arch: str

    @property
    def key(self) -> str:
        return f"{self.impl}{self.major}{self.minor}-{self.os}-{self.arch}"

    @property
    def version(self) -> str:
        return f"{self.major}.{self.minor}"

    @property
    def is_host(self) -> bool:
        return self.os == host_os() and self.arch == host_arch()


def parse_key(key: str) -> Target:
    m = KEY_RE.match(key)
    if not m:
        raise DeployError(
            f"clave de plataforma no válida: {key!r} (formato: cp314-linux-x86_64, pp311-windows-x86_64...)"
        )
    impl, major, minor, os_name, arch = m.groups()
    return Target(impl, int(major), int(minor), os_name, arch)


def host_target(cfg: Config, backend: str) -> Target:
    info = envs.interpreter_info(envs.runtime_env(cfg, backend).python)
    impl = "pp" if info["impl"] == "pypy" else "cp"
    major, minor, _ = str(info["version"]).split(".")
    return Target(impl, int(major), int(minor), host_os(), host_arch())


def targets_for(cfg: Config, backend: str, keys: list[str]) -> list[Target]:
    host = host_target(cfg, backend)
    out = [host]
    for k in keys:
        if k == "host":
            continue
        t = parse_key(k)
        if t.impl == "pp" and not t.is_host:
            raise DeployError(f"{k}: uv no puede resolver wheels de PyPy para otro sistema; solo el PyPy del host")
        if t.key not in {x.key for x in out}:
            out.append(t)
    return out


def export_requirements(cfg: Config) -> Path:
    """Dependencias de runtime (sin dev) con versiones y hashes exactos de uv.lock."""
    out = BUILD / "deploy" / "requirements.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    envs.uv(
        envs.tool_env(cfg),
        ["export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements.txt", "--output-file", out, "--quiet"],
    )
    return out


def install_deps(cfg: Config, backend: str, target: Target, dest: Path, requirements: Path) -> Path:
    """`uv pip install --target` de las deps de runtime para un destino (host o cruzado)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if not requirements.read_text(encoding="utf-8").strip():
        return dest
    env = envs.runtime_env(cfg, backend) if target.impl == ("pp" if backend == "pypy" else "cp") else envs.tool_env(cfg)
    argv: list[str | Path] = ["pip", "install", "--quiet", "--target", dest, "--no-deps", "-r", requirements]
    if target.is_host and env.python.is_file():
        argv += ["--python", env.python]
        if target.impl == "cp" and backend == "pypy":
            raise DeployError("destino CPython desde un build PyPy: usa claves pp311-...")
    else:
        argv += [
            "--python", envs.tool_env(cfg).python,
            "--python-platform", UV_PLATFORMS[(target.os, target.arch)],
            "--python-version", target.version,
            "--only-binary", ":all:",  # compilar sdists para otro SO daría binarios del host
        ]
    envs.uv(env, argv)
    for junk in dest.glob("_virtualenv*"):
        junk.unlink()
    return dest


def has_native(path: Path) -> bool:
    return any(p.suffix in NATIVE_SUFFIXES for p in path.rglob("*") if p.is_file())


def copy_app(app_dir: Path, dest: Path, *, extensions: bool) -> None:
    """Copia la carga útil. extensions=False deja solo los .py (fallback puro)."""
    if dest.exists():
        shutil.rmtree(dest)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skip = {n for n in names if n in {"__pycache__", ".mypy_cache"}}
        if not extensions:
            skip |= {n for n in names if n.endswith(EXT_SUFFIXES)}
        return skip

    shutil.copytree(app_dir, dest, ignore=ignore)


def uses_tkinter() -> bool:
    import ast

    for path in SRC.rglob("*.py"):
        try:
            tree = parse(path)
        except SyntaxError:
            continue
        for node in iter_runtime_nodes(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.split(".")[0] in {"tkinter", "turtle"} for n in names):
                return True
    return False


def dir_size_mb(path: Path) -> float:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1_048_576


def note(msg: str) -> None:
    ui.info(f"  {msg}")


def run_python(python: Path, args: list[str | Path], *, cwd: Path | None = None) -> None:
    proc.run([python, *args], cwd=cwd)
