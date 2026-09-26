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
import re
import shutil
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import envs, proc, render, ui
from .config import Config, compiled_paths
from .imports import imports_of, is_local, local_module, module_name, parse_error
from .project import BUILD, EXT_SUFFIXES, SRC, TOOLS, rel
from .ui import DeployError

SKIP_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
# The annotated HTML report (slow lines): ./deploy report, and every build with compile.annotate
ANNOTATE_HTML = BUILD / "reports" / "mypyc-annotate.html"
# Exit code of tools/mypyc_build.py when mypy/mypyc rejected the code (no C compiler ran yet)
MYPYC_REJECTED = 4
# ... and when the C build failed because setuptools cannot start the C compiler: a missing
# requirement (exit 3), like every other missing program
COMPILER_MISSING = 5
# The options of the last SUCCESSFUL compile of a profile (in its folder): see build()
COMPILED_STAMP = "compiled-options.json"
# spec.json keys that do not change the binaries (every other key does, see build())
_NOT_BINARY = ("annotate", "compile", "files", "force")
# Environment variables that change the binaries but not the generated C: what setuptools
# builds with (CC, CFLAGS: it REPLACES Python's own flags, CPPFLAGS, LDSHARED, LDFLAGS), macOS
# ARCHFLAGS and MSVC's CL/_CL_. Recorded with the options: a change forces a rebuild too.
COMPILER_ENV = ("CC", "CFLAGS", "CPPFLAGS", "LDSHARED", "LDFLAGS", "ARCHFLAGS", "CL", "_CL_")
# ABI tag at the start of an extension suffix: cpython-314-x86_64-linux-gnu.so,
# cpython-314-darwin.so, cp314-win_amd64.pyd (a trailing "t" = free-threaded build)
_ABI_RE = re.compile(r"(?:cpython-|cp)(\d)(\d+)(t?)(?=[-.])")


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


def _walk(root: Path) -> Iterator[Path]:
    """Every entry below `root`, each folder before its contents, following symlinked folders.

    Path.rglob does not descend into a symlinked folder (3.11-3.14): a linked src/assets or
    subpackage reached the stage empty. A link back to a folder on the way down (a cycle) is
    skipped; two links to the same folder are both followed. Cache folders are not entered.
    """
    chains = {os.fspath(root): frozenset({os.path.realpath(root)})}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        chain = chains.pop(dirpath)
        kept: list[str] = []
        for name in sorted(dirnames):
            path = os.path.join(dirpath, name)
            real = os.path.realpath(path)
            if name in SKIP_DIRS or real in chain:
                continue
            chains[path] = chain | {real}
            kept.append(name)
        dirnames[:] = kept
        for name in (*kept, *sorted(filenames)):
            yield Path(dirpath, name)


def compiled_sources(cfg: Config) -> list[Path]:
    """Return the .py files (in src/) that mypyc compiles.

    compile.exclude takes modules and subpackages (a prefix) of those packages; an entry that
    names nothing that exists is an error (a typo must not silently compile everything).
    """
    matched: set[str] = set()
    files: list[Path] = []
    for rel_path in compiled_paths(cfg):
        path = SRC / rel_path
        stem = rel_path.removesuffix(".py")
        if path.is_dir():
            candidates = sorted(p for p in _walk(path) if p.suffix == ".py" and p.name != "__init__.py" and p.is_file())
            if not candidates:  # an entry that compiles nothing is a mistake, never skipped silently
                raise DeployError(f"compile.modules: neither src/{stem}.py nor src/{stem}/ holds a module to compile")
        elif path.is_file():
            candidates = [path]
        else:
            raise DeployError(f"compile.modules: neither src/{stem}.py nor src/{stem}/ exists")
        for p in candidates:
            name = module_name(p, SRC)
            hits = {ex for ex in cfg.compile.exclude if name == ex or name.startswith(ex + ".")}
            matched |= hits
            if not hits:
                files.append(p)
    unknown = [ex for ex in dict.fromkeys(cfg.compile.exclude) if ex not in matched and not local_module(SRC, ex)]
    if unknown:
        raise DeployError(
            f"compile.exclude: {', '.join(map(repr, unknown))} matches no module in compile.modules "
            f"(use modules or subpackages of those packages, e.g. \"{cfg.pkg}.core.slow\")"
        )
    if not files:
        raise DeployError("compile.modules contains no .py file to compile")
    return list(dict.fromkeys(files))


def compiled_modules(cfg: Config) -> list[str]:
    return [module_name(p, SRC) for p in compiled_sources(cfg)]


def _is_ext(name: str) -> bool:
    return name.endswith(EXT_SUFFIXES)


def _mypyc_output(path: Path, root: Path, owned: Collection[str]) -> bool:
    """An extension that mypyc builds: a compiled module in `owned`, a `*__mypyc` shared lib, or
    any extension next to the .py it was built from.

    sync_tree never copies one from src/ (a stray in-place build would shadow the stage's, or
    reach a payload without its shared lib) and never deletes one from the stage
    (remove_stale_extensions does). Every other .so/.pyd in src/ (a vendored native library)
    is app content and synced like any file.
    """
    if not _is_ext(path.name):
        return False
    module = _ext_module(path, root)
    stem = path.name.split(".")[0]
    return module in owned or module.endswith("__mypyc") or path.with_name(f"{stem}.py").is_file()


def sync_tree(src: Path, dst: Path, owned: Collection[str] = ()) -> int:
    """Copy src -> dst: only what changed; remove what was deleted (except mypyc's extensions).

    "Changed" = different size or different mtime in nanoseconds: copy2 preserves the exact
    mtime, so a same-size edit within the same second is still detected. Symlinked folders are
    copied with their contents; a path that turned from file to folder (or back) is replaced;
    a folder deleted from src goes with its caches (it must not stay importable as a namespace
    package). `owned`: the compiled modules, whose extensions mypyc manages (see _mypyc_output).
    """
    changed = 0
    dst.mkdir(parents=True, exist_ok=True)
    seen: set[Path] = set()
    for path in _walk(src):
        if _mypyc_output(path, src, owned):
            continue
        target = dst / path.relative_to(src)
        if path.is_dir():
            seen.add(target)
            if target.is_symlink() or (target.exists() and not target.is_dir()):  # a file became a folder
                target.unlink()
                changed += 1
            target.mkdir(exist_ok=True)
            continue
        if not path.exists():
            ui.warn(f"{rel(path)}: broken symbolic link, not copied")
            continue
        seen.add(target)
        st = path.stat()
        if target.is_dir() and not target.is_symlink():  # a folder became a file
            shutil.rmtree(target)
        elif target.is_file():
            tt = target.stat()
            if tt.st_size == st.st_size and tt.st_mtime_ns == st.st_mtime_ns:
                continue
        shutil.copy2(path, target)
        changed += 1
    for path in sorted(dst.rglob("*"), reverse=True):  # children before their folder
        if path in seen or SKIP_DIRS & set(path.relative_to(dst).parts) or _mypyc_output(path, dst, owned):
            continue
        if path.is_dir() and not path.is_symlink():
            for cache in SKIP_DIRS:
                shutil.rmtree(path / cache, ignore_errors=True)
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


def _other_python(ext: Path, python: str) -> bool:
    """Whether the ABI tag of `ext` names another CPython than `python` ("3.14")."""
    abi = _ABI_RE.match(ext.name.partition(".")[2])
    return abi is not None and (f"{abi.group(1)}.{abi.group(2)}" != python or abi.group(3) == "t")


def remove_stale_extensions(
    stage: Path, modules: list[str], group: str, *, python: str, separate: bool = False, src: Path | None = None
) -> None:
    """Delete the extensions of the stage that this build will not produce.

    - built for another Python (python.cpython changed): they hold OLD code, and an interpreter
      of that version would still import them (portable with runtime = "system", flet build);
    - modules no longer compiled, and the shared libs this build does not use: `<group>__mypyc`,
      or one `<module>__mypyc` per module with compile.separate = true.
    A native file of the app itself (a .so/.pyd in src/, synced by sync_tree) is left alone.
    """
    src = SRC if src is None else src
    wanted = set(modules) | ({f"{m}__mypyc" for m in modules} if separate else {f"{group}__mypyc"})
    for ext in extension_files(stage):
        if not _mypyc_output(ext, stage, wanted) and (src / ext.relative_to(stage)).is_file():
            continue
        if _other_python(ext, python):
            ui.detail(f"  - {rel(ext)} (built for another Python)")
            ext.unlink()
        elif _ext_module(ext, stage) not in wanted:
            ui.detail(f"  - {rel(ext)} (no longer compiled)")
            ext.unlink()


def _read_json(path: Path) -> object:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data


def build(cfg: Config, profile_name: str, *, annotate: Path | None = None, compile_c: bool = True) -> Path:
    """Prepare the stage and compile. Return the stage path.

    `annotate`: also write mypyc's annotated HTML report there. With compile.annotate = true
    every build writes it to ANNOTATE_HTML (mypyc generates it from the IR it already built).
    """
    from_config = annotate is None and cfg.compile.annotate
    if from_config:
        annotate = ANNOTATE_HTML
    prof = profile(cfg, profile_name)
    sources = compiled_sources(cfg)
    modules = [module_name(p, SRC) for p in sources]
    group = group_name(cfg)

    ui.step(f"mypyc ({prof.name}): {', '.join(modules)}")
    # Before the sync: a folder emptied here is then removed by sync_tree
    remove_stale_extensions(prof.stage, modules, group, python=cfg.python.cpython, separate=cfg.compile.separate)
    changed = sync_tree(SRC, prof.stage, owned=modules)
    ui.detail(f"  stage: {changed} file(s) updated in {rel(prof.stage)}")

    config_file = prof.dir / "mypy.ini"
    config_file.write_text(render.mypy_ini(cfg, "mypyc", for_compile=prof.dir), encoding="utf-8", newline="\n")
    spec: dict[str, object] = {
        "stage": str(prof.stage),
        "config": str(config_file),
        "cache_dir": str(prof.dir / "mypy_cache"),
        "annotate": str(annotate) if annotate else "",
        "files": [p.relative_to(SRC).as_posix() for p in sources],
        "opt_level": cfg.compile.opt_level,
        "no_semantic_interposition": cfg.compile.no_semantic_interposition,
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
    # setuptools rebuilds an extension only when a source is newer than it: an option that only
    # reaches the C compiler (opt_level, no_semantic_interposition, debug_level, the compiler
    # variables of the environment) leaves the generated C untouched, so the old binary would
    # be kept. The options of the last SUCCESSFUL compile are recorded (the record is deleted
    # before compiling: a failed or interrupted build forces the next one); any difference
    # forces a full rebuild (build_ext --force).
    options = {k: v for k, v in spec.items() if k not in _NOT_BINARY}
    options["env"] = {name: os.environ[name] for name in COMPILER_ENV if name in os.environ}
    stamp = prof.dir / COMPILED_STAMP
    spec["force"] = compile_c and _read_json(stamp) != options
    spec_file = prof.dir / "spec.json"
    spec_file.write_text(json.dumps(spec, indent=2), encoding="utf-8", newline="\n")
    if annotate:
        annotate.parent.mkdir(parents=True, exist_ok=True)
    if compile_c and not proc.DRY_RUN:
        stamp.unlink(missing_ok=True)

    tool = envs.tool_env(cfg)
    # MSVC/setuptools output is only shown on failure (or with -v). VSLANG=1033: compiler
    # messages in English, which avoids unreadable cp1252 text in the terminal.
    envs_extra = {"VSLANG": "1033"}
    argv: list[str | Path] = [proc.find_uv(), "run", "--locked", "python", TOOLS / "mypyc_build.py", spec_file]
    result = proc.run(argv, env=envs.env_vars(tool, envs_extra), capture=not ui.VERBOSE, check=False)
    if result.returncode != 0:
        if not ui.VERBOSE:
            ui.info((result.stdout or "") + (result.stderr or ""))
        if result.returncode == MYPYC_REJECTED:  # mypy/mypyc rejected the code: no compiler involved
            raise DeployError("mypyc failed (exit code 1): fix the errors above", 1)
        if result.returncode == COMPILER_MISSING:
            raise DeployError(f"mypyc failed: the C compiler cannot start (above)\n{has_compiler_hint()}", 3)
        raise DeployError(f"mypyc failed (exit code {result.returncode})\n{has_compiler_hint()}", result.returncode)
    if annotate and result.stdout:
        ui.detail(result.stdout)
    if from_config and annotate and not proc.DRY_RUN:
        ui.info(f"  mypyc report (compile.annotate): {rel(annotate)}")
    if not compile_c or proc.DRY_RUN:
        return prof.stage

    built = {_ext_module(p, prof.stage) for p in extension_files(prof.stage)}
    missing = [m for m in modules if m not in built]
    if missing:
        raise DeployError(f"mypyc did not generate an extension for: {', '.join(missing)}")
    stamp.write_text(json.dumps(options, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    ui.ok(f"compiled in {rel(prof.stage)}")
    return prof.stage


def runtime_env_vars(cfg: Config) -> dict[str, str]:
    """Return the variables that let tests/conftest.py verify the .pyd files were loaded."""
    return {"PYTEMPLATE_BACKEND": "mypyc", "PYTEMPLATE_COMPILED": ",".join(compiled_modules(cfg))}


# Runs in the tools environment, where PyInstaller and Nuitka run. Each argument is "t:name"
# (kept when its top-level module exists: find_spec of a top-level name imports nothing) or
# "f:name" (kept when that exact module exists: this imports its parent package, which may
# print, hence the marker line). Built-in modules are left out: they need no bundling.
_FIND_MARK = "PTMODS:"
_FIND_CODE = f"""\
import importlib.util, sys
def found(arg):
    kind, name = arg[:2], arg[2:]
    top = name.partition('.')[0]
    if top in sys.builtin_module_names:
        return False
    try:
        return importlib.util.find_spec(name if kind == 'f:' else top) is not None
    except (Exception, SystemExit):
        return False
print({_FIND_MARK!r} + ','.join(a[2:] for a in sys.argv[1:] if found(a)))
"""


def importable(cfg: Config, top_level: Collection[str], full: Collection[str]) -> set[str] | None:
    """The names the tools environment can import, of `top_level` (checked by their top-level
    module) and `full` (checked as they are). None when the check itself could not run."""
    args = [f"t:{n}" for n in sorted(top_level)] + [f"f:{n}" for n in sorted(full)]
    if not args:
        return set()
    r = envs.uv(envs.tool_env(cfg), ["run", "--locked", "python", "-c", _FIND_CODE, *args], capture=True, check=False, echo=False)
    marks = [ln for ln in (r.stdout or "").splitlines() if ln.startswith(_FIND_MARK)]
    if r.returncode != 0 or not marks:
        return None
    return {n for n in marks[-1][len(_FIND_MARK) :].strip().split(",") if n}


def hidden_imports(cfg: Config, stage: Path) -> list[str]:
    """Return what PyInstaller/Nuitka cannot see inside the compiled binaries: the compiled
    modules, mypyc's shared libs, and what the compiled code imports.

    Imports of the app itself (src/) are kept when they exist. Any other name is kept only when
    the tools environment can import it: Nuitka aborts on a module it cannot find (a
    platform-guarded `import winreg` on Linux, an optional dependency that is not installed).
    `from X import a` also adds `X.a` when that is a submodule (`from html import parser`):
    X's __init__ may never import it, and the binary would fail at startup.
    """
    sources = compiled_sources(cfg)
    compiled = {module_name(p, SRC) for p in sources}
    keep = set(compiled)
    external: set[str] = set()
    candidates: set[str] = set()
    for path in sources:
        try:
            names = imports_of(path, module_name(path, SRC), SRC, candidates)
        except (SyntaxError, ValueError) as e:  # a runner older than the project's syntax
            line, msg = parse_error(e)
            raise DeployError(f"{rel(path)}:{line}: {msg}") from None
        for name in names:
            if not is_local(SRC, name):
                external.add(name)
            elif local_module(SRC, name):
                keep.add(name)
    for ext in extension_files(stage):
        module = _ext_module(ext, stage)
        if module in compiled or module.endswith("__mypyc"):
            keep.add(module)
    found = importable(cfg, external, candidates - external)
    if found is None:
        ui.warn("could not check the imports of the compiled modules in the tools environment: all of them are passed on")
        found = external
    return sorted(keep | found)


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


def has_compiler_hint(platform: str = "win-amd64") -> str:
    """How to get a C compiler. `platform`: sysconfig.get_platform() of the .venv Python, whose
    MSVC tools setuptools looks for (cmd_env._msvc): ARM64 ones for a win-arm64 Python."""
    if os.name == "nt":
        tools = "VC.Tools.ARM64" if platform == "win-arm64" else "VC.Tools.x86.x64"
        return (
            "mypyc needs MSVC (Visual Studio Build Tools) with the Windows SDK:\n"
            'winget install -e --id Microsoft.VisualStudio.BuildTools --override "--wait --passive '
            f'--add Microsoft.VisualStudio.Component.{tools} --add Microsoft.VisualStudio.Component.Windows11SDK.26100"'
        )
    return "mypyc needs a C compiler (gcc/clang; on macOS: xcode-select --install)"
