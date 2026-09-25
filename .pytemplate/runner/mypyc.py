"""mypyc backend: incremental stage of src/ + compilation with mypycify.

Compilation happens in a COPY of src/ (.build/mypyc-<profile>/stage): if the .pyd ended
up next to your .py in src/, Python would import that binary instead of your edited code.
The .py stays next to the .pyd in the stage (the extension loader takes
precedence) so that a pyz/portable can fall back to the .py on another interpreter.

Two profiles:
- dev (run/test/report): asserts enabled and debug symbols.
- release (build): asserts stripped if deploy.optimize >= 1, no symbols.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import envs, proc, render, ui
from .config import Config, compiled_paths
from .imports import imports_of, module_name
from .project import BUILD, EXT_SUFFIXES, SRC, TOOLS, rel
from .ui import DeployError

SKIP_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


@dataclass(frozen=True)
class Profile:
    name: str  # dev | release
    strip_asserts: bool
    debug_level: str

    @property
    def dir(self) -> Path:
        return BUILD / f"mypyc-{self.name}"

    @property
    def stage(self) -> Path:
        return self.dir / "stage"


def profile(cfg: Config, name: str) -> Profile:
    if name == "dev":
        return Profile("dev", strip_asserts=False, debug_level="1")
    return Profile("release", strip_asserts=cfg.deploy.optimize >= 1, debug_level="0")


def group_name(cfg: Config) -> str:
    return cfg.pkg


def compiled_sources(cfg: Config) -> list[Path]:
    """Return the .py files (in src/) that mypyc compiles."""
    excluded = set(cfg.compile.exclude)
    files: list[Path] = []
    for rel_path in compiled_paths(cfg):
        path = SRC / rel_path
        if path.is_dir():
            candidates = sorted(p for p in path.rglob("*.py") if p.name != "__init__.py" and not SKIP_DIRS & set(p.parts))
        elif path.is_file():
            candidates = [path]
        else:
            raise DeployError(f"compile.modules: src/{rel_path} does not exist")
        files += [p for p in candidates if module_name(p, SRC) not in excluded]
    if not files:
        raise DeployError("compile.modules contains no .py file to compile")
    return files


def compiled_modules(cfg: Config) -> list[str]:
    return [module_name(p, SRC) for p in compiled_sources(cfg)]


def _is_ext(name: str) -> bool:
    return name.endswith(EXT_SUFFIXES)


def sync_tree(src: Path, dst: Path) -> int:
    """Copy src -> dst: only what changed; remove what was deleted (except compiled extensions)."""
    changed = 0
    dst.mkdir(parents=True, exist_ok=True)
    seen: set[Path] = set()
    for path in src.rglob("*"):
        if SKIP_DIRS & set(path.relative_to(src).parts) or _is_ext(path.name):
            continue
        target = dst / path.relative_to(src)
        seen.add(target)
        if path.is_dir():
            target.mkdir(exist_ok=True)
            continue
        st = path.stat()
        if target.is_file():
            tt = target.stat()
            if tt.st_size == st.st_size and int(tt.st_mtime) == int(st.st_mtime):
                continue
        shutil.copy2(path, target)
        changed += 1
    for path in sorted(dst.rglob("*"), reverse=True):
        if path in seen or _is_ext(path.name) or SKIP_DIRS & set(path.relative_to(dst).parts):
            continue
        if path.is_dir():
            if not any(path.iterdir()):
                path.rmdir()
        else:
            path.unlink()
            changed += 1
    return changed


def _ext_module(path: Path, stage: Path) -> str:
    rel_path = path.relative_to(stage)
    stem = rel_path.name.split(".")[0]
    return ".".join([*rel_path.parent.parts, stem])


def extension_files(stage: Path) -> list[Path]:
    return sorted(p for p in stage.rglob("*") if p.is_file() and _is_ext(p.name) and "__pycache__" not in p.parts)


def remove_stale_extensions(stage: Path, modules: list[str], group: str) -> None:
    wanted = set(modules) | {f"{group}__mypyc"}
    for ext in extension_files(stage):
        if _ext_module(ext, stage) not in wanted:
            ui.detail(f"  - {rel(ext)} (no longer compiled)")
            ext.unlink()


def build(cfg: Config, profile_name: str, *, annotate: Path | None = None, compile_c: bool = True) -> Path:
    """Prepare the stage and compile. Return the stage path."""
    prof = profile(cfg, profile_name)
    sources = compiled_sources(cfg)
    modules = [module_name(p, SRC) for p in sources]
    group = group_name(cfg)

    ui.step(f"mypyc ({prof.name}): {', '.join(modules)}")
    changed = sync_tree(SRC, prof.stage)
    ui.detail(f"  stage: {changed} file(s) updated in {rel(prof.stage)}")
    remove_stale_extensions(prof.stage, modules, group)

    config_file = prof.dir / "mypy.ini"
    config_file.write_text(render.mypy_ini(cfg, "mypyc", for_compile=True), encoding="utf-8", newline="\n")
    spec = {
        "stage": str(prof.stage),
        "config": str(config_file),
        "cache_dir": str(prof.dir / "mypy_cache"),
        "annotate": str(annotate) if annotate else "",
        "files": [p.relative_to(SRC).as_posix() for p in sources],
        "opt_level": cfg.compile.opt_level,
        "debug_level": prof.debug_level,
        "strip_asserts": prof.strip_asserts,
        "multi_file": cfg.compile.multi_file,
        "separate": cfg.compile.separate,
        "strict_dunder_typing": cfg.compile.strict_dunder_typing,
        "group": group,
        # Paths relative to the stage: short, because of the MSVC MAX_PATH limit
        "c_dir": "../c",
        "build_temp": "../obj",
        "build_lib": "../lib",
        "compile": compile_c,
    }
    spec_file = prof.dir / "spec.json"
    spec_file.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    if annotate:
        annotate.parent.mkdir(parents=True, exist_ok=True)

    tool = envs.tool_env(cfg)
    # MSVC/setuptools output is only shown on failure (or with -v). VSLANG=1033: compiler
    # messages in English, which avoids unreadable cp1252 text in the terminal.
    envs_extra = {"VSLANG": "1033"}
    argv: list[str | Path] = [proc.find_uv(), "run", "--locked", "python", TOOLS / "mypyc_build.py", spec_file]
    result = proc.run(argv, env=envs.env_vars(tool, envs_extra), capture=not ui.VERBOSE, check=False)
    if result.returncode != 0:
        if not ui.VERBOSE:
            ui.info((result.stdout or "") + (result.stderr or ""))
        hint = "" if "error: " in (result.stdout or "") else "\n" + has_compiler_hint()
        raise DeployError(f"mypyc failed (exit code {result.returncode}){hint}", result.returncode)
    if annotate and result.stdout:
        ui.detail(result.stdout)
    if not compile_c or proc.DRY_RUN:
        return prof.stage

    built = {_ext_module(p, prof.stage) for p in extension_files(prof.stage)}
    missing = [m for m in modules if m not in built]
    if missing:
        raise DeployError(f"mypyc did not generate an extension for: {', '.join(missing)}")
    ui.ok(f"compiled in {rel(prof.stage)}")
    return prof.stage


def runtime_env_vars(cfg: Config) -> dict[str, str]:
    """Return the variables that let tests/conftest.py verify the .pyd files were loaded."""
    return {"PYTEMPLATE_BACKEND": "mypyc", "PYTEMPLATE_COMPILED": ",".join(compiled_modules(cfg))}


def hidden_imports(cfg: Config, stage: Path) -> list[str]:
    """Return what PyInstaller/Nuitka cannot see: the compiled modules and everything they import."""
    hidden: set[str] = set()
    for path in compiled_sources(cfg):
        module = module_name(path, SRC)
        hidden.add(module)
        hidden |= imports_of(path, module, SRC)
    for ext in extension_files(stage):
        hidden.add(_ext_module(ext, stage))
    return sorted(hidden)


def exe_stage(cfg: Config, stage: Path, dest: Path) -> Path:
    """Copy the stage WITHOUT the compiled .py files, so the packager can only bundle the binary."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(stage, dest, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    for path in compiled_sources(cfg):
        target = dest / path.relative_to(SRC)
        if target.exists():
            target.unlink()
    return dest


def clean_bytecode(path: Path) -> None:
    for cache in path.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def has_compiler_hint() -> str:
    if os.name == "nt":
        return (
            "mypyc needs MSVC (Visual Studio Build Tools) with the Windows SDK:\n"
            'winget install -e --id Microsoft.VisualStudio.BuildTools --override "--wait --passive '
            '--add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.Windows11SDK.26100"'
        )
    return "mypyc needs a C compiler (gcc/clang; on macOS: xcode-select --install)"
