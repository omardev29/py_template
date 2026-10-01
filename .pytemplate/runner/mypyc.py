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

import contextlib
import json
import os
import re
import shutil
import stat
import tomllib
import uuid
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from . import envs, proc, render, ui
from .config import TOML_ERRORS, Config, compiled_paths
from .imports import PARSE_ERRORS, imports_of, is_local, local_module, module_name, parse_error
from .project import BUILD, EXT_SUFFIXES, IS_WINDOWS, PYPROJECT, SRC, TOOLS, rel
from .ui import PytError

SKIP_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache", ".hypothesis", ".ruff_cache"}
# The annotated HTML report (slow lines): ./pyt report, and every build with compile.annotate
ANNOTATE_HTML = BUILD / "reports" / "mypyc-annotate.html"
# Exit codes of tools/mypyc_build.py: mypy/mypyc rejected the code (no C compiler ran yet);
# the C build failed because setuptools cannot start the C compiler (a missing requirement:
# exit 3, like every other missing program); setuptools or the C compiler failed. Only the last
# two get the compiler hint: a code 1 or 2 comes from uv (a stale uv.lock) before the script ran.
MYPYC_REJECTED = 4
COMPILER_MISSING = 5
C_BUILD_FAILED = 6
# The options of the last SUCCESSFUL compile of a profile (in its folder): see build()
COMPILED_STAMP = "compiled-options.json"
# spec.json keys that do not change the binaries (every other key does, see build())
_NOT_BINARY = ("annotate", "compile", "files", "force")
# Environment variables that change the binaries but not the generated C: what setuptools
# builds with (CC, CFLAGS: it REPLACES Python's own flags, CPPFLAGS, LDSHARED, LDFLAGS), macOS
# ARCHFLAGS and MSVC's CL/_CL_. Recorded with the options: a change forces a rebuild too.
COMPILER_ENV = ("CC", "CFLAGS", "CPPFLAGS", "LDSHARED", "LDFLAGS", "ARCHFLAGS", "CL", "_CL_")
# The packages whose code goes into every extension besides the generated C: mypyc compiles the
# C runtime of the INSTALLED mypy (its lib-rt: CPy.h, init.c...) into each one, which mypycify
# leaves out of Extension.depends, and setuptools drives the compiler. A new release of either
# with the same generated C (common: the skeleton's C is the same under mypy 2.2.0 and 2.3.1)
# kept the old binaries. Their versions in LOCK, the lock `uv run --locked` syncs the tools
# environment to before tools/mypyc_build.py runs, are recorded with the options.
TOOLCHAIN = ("mypy", "setuptools")
LOCK = PYPROJECT.with_name("uv.lock")
# ABI tag at the start of an extension suffix: cpython-314-x86_64-linux-gnu.so,
# cpython-314-darwin.so, cp314-win_amd64.pyd (a trailing "t" = free-threaded build)
_ABI_RE = re.compile(r"(?:cpython-|cp)(\d)(\d+)(t?)(?=[-.])")
# Windows: the folder next to a stage where its extensions go when they leave it (a stale one,
# one the build replaces). Windows deletes no DLL a process has loaded (the app still running
# from the stage, a debug session) but renames it on the same volume; every build empties it,
# best effort (what is still loaded stays for the next one)
SET_ASIDE = "old-extensions"


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


def walk(root: Path) -> Iterator[Path]:
    """Every entry below `root`, each folder before its contents, following symlinked folders.

    Path.rglob does not descend into a symlinked folder (3.11-3.14): a linked src/assets or
    subpackage reached the stage empty. A link back to a folder on the way down (a cycle) is
    skipped; two links to the same folder are both followed. Cache folders are not entered.
    A folder that cannot be listed (entered but not read: mode 0311, another user's 0711) is a
    PytError naming it: os.walk skipped it without a word, so every build shipped it empty while
    the app still opened its files by name in development (and sync_tree deleted the copy an
    earlier build had made). One that is gone (the root, or a folder deleted meanwhile) is no loss.
    """

    def unlistable(e: OSError) -> None:
        if isinstance(e, (FileNotFoundError, NotADirectoryError)):
            return
        where = rel(Path(os.fsdecode(e.filename))) if e.filename else rel(root)
        raise PytError(
            f"cannot list {where}/: {e.strerror or e}: a build would leave out every file below it.\n"
            "  Make it readable (chmod u+rx), or move it out of the folder, and try again"
        )

    chains = {os.fspath(root): frozenset({os.path.realpath(root)})}
    for dirpath, dirnames, filenames in os.walk(root, onerror=unlistable, followlinks=True):
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
    """Return the .py files (in src/) that mypyc compiles: the modules Python can import below
    each compile.modules entry (`_importable`: never an editor's leftover copy).

    compile.exclude takes modules and subpackages (a prefix) of those packages; an entry that
    names nothing that exists is an error (a typo must not silently compile everything).
    """
    matched: set[str] = set()
    files: list[Path] = []
    for rel_path in compiled_paths(cfg):
        path = SRC / rel_path
        stem = rel_path.removesuffix(".py")
        if path.is_dir():
            candidates = sorted(
                p for p in walk(path) if p.suffix == ".py" and p.name != "__init__.py" and _importable(p) and p.is_file()
            )
            if not candidates:  # an entry that compiles nothing is a mistake, never skipped silently
                raise PytError(f"compile.modules: neither src/{stem}.py nor src/{stem}/ holds a module to compile")
        elif path.is_file():
            candidates = [path]
        else:
            raise PytError(f"compile.modules: neither src/{stem}.py nor src/{stem}/ exists")
        for p in candidates:
            name = module_name(p, SRC)
            hits = {ex for ex in cfg.compile.exclude if name == ex or name.startswith(ex + ".")}
            matched |= hits
            if not hits:
                files.append(p)
    unknown = [ex for ex in dict.fromkeys(cfg.compile.exclude) if ex not in matched and not local_module(SRC, ex)]
    if unknown:
        raise PytError(
            f"compile.exclude: {', '.join(map(repr, unknown))} matches no module in compile.modules "
            f"(use modules or subpackages of those packages, e.g. \"{cfg.pkg}.core.slow\")"
        )
    if not files:
        raise PytError("compile.modules contains no .py file to compile")
    return list(dict.fromkeys(files))


def _importable(path: Path) -> bool:
    """Whether Python can import the .py file `path` of src/ as a module: every part of its dotted
    name an identifier. JupyterLab's .ipynb_checkpoints/<name>-checkpoint.py, a copy named
    `bench copy.py` or `bench.old.py` and a data folder `sample-data/` hold none: mypyc made C
    names with '-' or ' ' of them (a C compile error, with the C compiler hint), or a stray
    top-level extension of a module mypy named from inside the folder, and lintc failed `check`
    and the hook on a stale copy of the code; compile.exclude cannot name them."""
    return all(part.isidentifier() for part in path.relative_to(SRC).with_suffix("").parts)


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


def _owner_writable(path: str | Path) -> None:
    """Add the owner's write bit (on Windows: clear the read-only attribute); links are left alone."""
    mode = os.lstat(path).st_mode
    if not stat.S_ISLNK(mode):
        wanted = stat.S_IMODE(mode) | (stat.S_IRWXU if stat.S_ISDIR(mode) else stat.S_IWUSR)
        if wanted != stat.S_IMODE(mode):
            os.chmod(path, wanted)


def copy_writable(src: str, dst: str) -> str:
    """shutil.copy2, then owner-writable: a copy of a read-only file of src/ (a Perforce
    checkout, a link into the Nix store) kept its mode, and the next build could neither replace
    it once the file changed nor delete it (Windows). For copytree's copy_function too."""
    shutil.copy2(src, dst)
    _owner_writable(dst)
    return dst


def make_writable(root: Path) -> None:
    """Make `root` and everything below it owner-writable (folders: rwx), links neither followed
    nor changed: copytree also copies a read-only FOLDER's mode, in which nothing can be written
    or deleted (POSIX)."""
    if os.path.islink(root):
        return
    _owner_writable(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            _owner_writable(os.path.join(dirpath, name))


def remove_tree(path: Path) -> None:
    """shutil.rmtree for a copy of src/ that may hold read-only entries (made by an older
    ./pyt, see copy_writable): Windows does not delete a read-only file and POSIX does not
    empty a read-only folder, so what is left is made writable and removed again. A link goes
    as a link."""
    if os.path.islink(path):
        os.unlink(path)
        return
    try:
        shutil.rmtree(path)
    except OSError:
        make_writable(path)
        shutil.rmtree(path)


_T = TypeVar("_T")


def _source(path: Path, step: Callable[[], _T]) -> _T:
    """Run a step that reads the file `path` of src/: a file it cannot read (another user's, one
    locked by another program, a named pipe) is one error naming it; it ended run mypyc, test
    mypyc, compile and every build in an internal-error traceback. What fails on the copy's side
    (a file under .build/ another user left: cli names it) and a full disk go on as they are."""
    from .cli import NO_ROOM

    try:
        return step()
    except OSError as e:
        if e.errno in NO_ROOM or (e.filename is not None and os.fsdecode(e.filename) != os.fspath(path)):
            raise
        raise PytError(f"cannot copy {rel(path)}: {e.strerror or e}") from None


def _spelled_otherwise(target: Path, listed: dict[str, set[str] | None]) -> bool:
    """Whether the file system finds `target` while its folder lists no entry of exactly that
    name: a case-insensitive volume (macOS's and Windows' default) after a case-only rename in
    src/ (Data.py -> data.py, Core/ -> core/). Kept, the copy went on under its old name: Windows
    (whose paths compare without case) shipped that spelling, which Python's case-sensitive import
    never finds, and macOS (whose paths compare with case) deleted it as a file src/ no longer
    has, so the first build after the rename was without the module. `listed` caches each
    folder's names; a folder it cannot list counts as spelled right."""
    folder = os.fspath(target.parent)
    if folder not in listed:
        try:
            listed[folder] = set(os.listdir(folder))
        except OSError:
            listed[folder] = None
    names = listed[folder]
    return names is not None and target.name not in names and os.path.lexists(target)


def sync_tree(src: Path, dst: Path, owned: Collection[str] = ()) -> int:
    """Copy src -> dst: only what changed; remove what was deleted (except mypyc's extensions).

    "Changed" = different size or different mtime in nanoseconds: copy2 preserves the exact
    mtime, so a same-size edit within the same second is still detected. Symlinked folders are
    copied with their contents; a path that turned from file to folder (or back) is replaced;
    a folder deleted from src goes with its caches (it must not stay importable as a namespace
    package). `owned`: the compiled modules, whose extensions mypyc manages (see _mypyc_output).
    Every copy is owner-writable (copy_writable), and a read-only one an older ./pyt left is
    made writable before it is replaced or deleted. A copy spelled otherwise than its source
    (a case-only rename on a case-insensitive volume, `_spelled_otherwise`) is made again.
    """
    changed = 0
    dst.mkdir(parents=True, exist_ok=True)
    seen: set[Path] = set()
    listed: dict[str, set[str] | None] = {}
    for path in walk(src):
        if _mypyc_output(path, src, owned):
            continue
        target = dst / path.relative_to(src)
        if _spelled_otherwise(target, listed):
            if target.is_dir() and not target.is_symlink():
                remove_tree(target)
            else:
                _owner_writable(target)  # Windows deletes no read-only file
                target.unlink()
            changed += 1
        if path.is_dir():
            seen.add(target)
            if target.is_symlink() or (target.exists() and not target.is_dir()):  # a file became a folder
                _owner_writable(target)  # Windows deletes no read-only file
                target.unlink()
                changed += 1
            target.mkdir(exist_ok=True)
            continue
        if not path.exists():
            ui.warn(f"{rel(path)}: broken symbolic link, not copied")
            continue
        seen.add(target)
        st = _source(path, path.stat)
        if target.is_symlink():  # never written through (sync_tree makes no links)
            target.unlink()
        elif target.is_dir():  # a folder became a file
            remove_tree(target)
        elif target.is_file():
            _owner_writable(target)  # a read-only copy an older ./pyt made
            tt = target.stat()
            if tt.st_size == st.st_size and tt.st_mtime_ns == st.st_mtime_ns:
                continue
        _source(path, lambda: copy_writable(os.fspath(path), os.fspath(target)))
        changed += 1
    for path in sorted(dst.rglob("*"), reverse=True):  # children before their folder
        if path in seen or SKIP_DIRS & set(path.relative_to(dst).parts) or _mypyc_output(path, dst, owned):
            continue
        if path.is_dir() and not path.is_symlink():
            for cache in SKIP_DIRS:
                with contextlib.suppress(OSError):  # best effort: a folder still holding one stays
                    if (path / cache).is_dir():
                        remove_tree(path / cache)
            if not any(path.iterdir()):
                _owner_writable(path)
                path.rmdir()
        else:
            _owner_writable(path)  # Windows deletes no read-only file
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
        elif _ext_module(ext, stage) not in wanted:
            ui.detail(f"  - {rel(ext)} (no longer compiled)")
        else:
            continue
        try:
            set_aside(ext, stage)
        except OSError as e:
            raise PytError(f"cannot remove {rel(ext)}: {e.strerror or e}. {_STILL_RUNNING.format(stage=rel(stage))}") from None


_STILL_RUNNING = "Is the app still running from {stage} (./pyt run mypyc, a debug session)? Close it and try again"


def set_aside(ext: Path, stage: Path) -> None:
    """Take an extension out of the stage: deleted, or on Windows moved into SET_ASIDE next to the
    stage, since a DLL a process has loaded cannot be deleted there, only renamed."""
    if not IS_WINDOWS:
        ext.unlink()
        return
    folder = stage.parent / SET_ASIDE
    folder.mkdir(exist_ok=True)
    os.replace(ext, folder / f"{uuid.uuid4().hex}-{ext.name}")


def _empty_set_aside(stage: Path) -> None:
    """Delete what earlier builds set aside, best effort: a file still loaded stays."""
    folder = stage.parent / SET_ASIDE
    with contextlib.suppress(OSError):
        for path in folder.iterdir():
            with contextlib.suppress(OSError):
                path.unlink()


def _read_json(path: Path) -> object:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data


def toolchain() -> dict[str, list[str]]:
    """The versions LOCK pins for TOOLCHAIN (a lock it cannot read gives none: `uv run --locked`
    then refuses to compile anyway)."""
    try:
        data = tomllib.loads(LOCK.read_text(encoding="utf-8-sig"))
    except (OSError, *TOML_ERRORS):
        return {}
    packages = data.get("package")
    found: dict[str, set[str]] = {}
    for package in packages if isinstance(packages, list) else []:
        if isinstance(package, dict) and package.get("name") in TOOLCHAIN:
            found.setdefault(str(package["name"]), set()).add(str(package.get("version", "")))
    return {name: sorted(versions) for name, versions in sorted(found.items())}


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
    _empty_set_aside(prof.stage)
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
    # be kept, and so was the binary of an older mypyc or setuptools (TOOLCHAIN). The options of
    # the last SUCCESSFUL compile are recorded (the record is deleted before compiling: a failed
    # or interrupted build forces the next one); any difference forces a full rebuild
    # (build_ext --force).
    options = {k: v for k, v in spec.items() if k not in _NOT_BINARY}
    options["env"] = {name: os.environ[name] for name in COMPILER_ENV if name in os.environ}
    options["toolchain"] = toolchain()
    stamp = prof.dir / COMPILED_STAMP
    spec["force"] = compile_c and _read_json(stamp) != options
    spec_file = prof.dir / "spec.json"
    spec_file.write_text(json.dumps(spec, indent=2), encoding="utf-8", newline="\n")
    if annotate:
        annotate.parent.mkdir(parents=True, exist_ok=True)
    if compile_c and not proc.DRY_RUN:
        stamp.unlink(missing_ok=True)
        if IS_WINDOWS:
            # setuptools' build_ext --inplace deletes the extension in the stage before it copies a
            # new one in: while the app still ran from the stage (Windows deletes no loaded DLL)
            # every build that changed it failed, with the compiler-install hint
            for ext in extension_files(prof.stage):
                if _mypyc_output(ext, prof.stage, modules):
                    with contextlib.suppress(OSError):  # left in place: setuptools then says why
                        set_aside(ext, prof.stage)
    # mypyc reuses the IR of its cache (compile.separate: incremental) and the C files of the last
    # run for a module whose source did not change: strip_asserts and strict_dunder_typing are no
    # part of mypy's cache key, so deploy.optimize 0 -> 1 kept the asserts in the release binary.
    # A forced build starts from neither.
    stale = [prof.dir / "mypy_cache", prof.dir / "c"] if spec["force"] else []
    if annotate and cfg.compile.separate and not stale:
        # A module mypy loads from that cache gets no IR, and mypyc annotates only the modules it
        # built IR for: after an unchanged compile the report was an empty page (and one module
        # after an edit of one), with the success line. mypyc writes a C file only when its text
        # changes, so the C compiler still rebuilds nothing that did not change.
        stale = [prof.dir / "mypy_cache"]
    if stale and not proc.DRY_RUN:
        for cached in stale:
            try:
                if cached.exists():
                    shutil.rmtree(cached)
            except OSError as e:
                raise PytError(f"cannot remove {rel(cached)}, which mypyc must not reuse: {e.strerror or e} (delete it, or ./pyt clean)") from None

    tool = envs.tool_env(cfg)
    # MSVC/setuptools output is only shown on failure (or with -v). VSLANG=1033: compiler
    # messages in English, which avoids unreadable cp1252 text in the terminal.
    envs_extra = {"VSLANG": "1033"}
    argv: list[str | Path] = [proc.find_uv(), "run", "--locked", "python", TOOLS / "mypyc_build.py", spec_file]
    result = proc.run(argv, env=envs.env_vars(tool, envs_extra), capture=not ui.VERBOSE, check=False)
    if result.returncode != 0:
        output = ((result.stdout or "") + (result.stderr or "")).rstrip()
        if output:
            ui.report(output)  # why it failed (mypy's errors, the compiler's): shown even with -q
        if result.returncode == MYPYC_REJECTED:  # mypy/mypyc rejected the code: no compiler involved
            raise PytError("mypyc failed (exit code 1): fix the errors above", 1)
        if result.returncode == COMPILER_MISSING:
            hint = has_compiler_hint(_venv_platform(tool))
            raise PytError(f"mypyc failed: the C compiler cannot start (above)\n{hint}", 3)
        if result.returncode == C_BUILD_FAILED:
            if "could not delete '" in output:  # setuptools: an extension it replaces is in use (Windows)
                still = _STILL_RUNNING.format(stage=rel(prof.stage))
                raise PytError(f"mypyc failed (exit code 1): a file of the stage is in use (above). {still}", 1)
            raise PytError(f"mypyc failed (exit code 1)\n{has_compiler_hint(_venv_platform(tool))}", 1)
        # uv, or Python before the script ran (a stale uv.lock: uv's error is above)
        raise PytError(f"mypyc failed (exit code {result.returncode}): see the error above", result.returncode)
    if annotate and result.stdout:
        ui.detail(result.stdout)
    if from_config and annotate and not proc.DRY_RUN:
        ui.info(f"  mypyc report (compile.annotate): {rel(annotate)}")
    if not compile_c or proc.DRY_RUN:
        return prof.stage

    built = {_ext_module(p, prof.stage) for p in extension_files(prof.stage)}
    missing = [m for m in modules if m not in built]
    if missing:
        raise PytError(f"mypyc did not generate an extension for: {', '.join(missing)}")
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
        except PARSE_ERRORS as e:  # a runner older than the project's syntax, a too deeply nested source
            line, msg = parse_error(e)
            raise PytError(f"{rel(path)}:{line}: {msg}") from None
        except OSError as e:  # another user's, locked by another program: never a traceback
            raise PytError(f"{rel(path)}: cannot read it: {e.strerror or e}") from None
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
    from .methods.common import copy_tree  # a full disk: one error line (cli.NO_ROOM), no traceback

    if dest.exists():
        remove_tree(dest)
    copy_tree(stage, dest, ignore=shutil.ignore_patterns(*SKIP_DIRS), copy_function=copy_writable)
    for path in compiled_sources(cfg):
        target = dest / path.relative_to(SRC)
        if target.exists():
            target.unlink()
    return dest


def _venv_platform(tool: envs.PyEnv) -> str:
    """sysconfig.get_platform() of the .venv Python, whose MSVC tools the hint names (Windows
    only: elsewhere the hint does not depend on it)."""
    if not IS_WINDOWS:
        return ""
    try:
        return str(envs.interpreter_info(tool.python)["platform"])
    except (OSError, ValueError, KeyError, proc.CommandFailed, PytError):
        return ""


# tools/mypyc_build.py missing_compiler, asked in .venv (setuptools' distutils needs setuptools
# imported first); the answer is the last PTCC: line
_MISSING_COMPILER_CODE = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import setuptools, mypyc_build\n"
    "try:\n"
    "    problem = mypyc_build.missing_compiler()\n"
    "except Exception as e:\n"
    "    problem = f'setuptools cannot set up a C compiler: {e}'\n"
    "print('PTCC:' + (problem or ''))\n"
)


def missing_compiler(tool: envs.PyEnv) -> str | None:
    """After a C build of mypyc code failed outside `build` (the wheel's setup.py, whose failure
    is uv's exit code, never the script's COMPILER_MISSING): why setuptools cannot start the C
    compiler of `tool`, with the hint to get one, or None (it can, or the question cannot run)."""
    argv: list[str | Path] = ["run", "--locked", "python", "-c", _MISSING_COMPILER_CODE, TOOLS]
    r = envs.uv(tool, argv, extra_env={"VSLANG": "1033"}, capture=True, check=False, echo=False)
    marks = [line for line in (r.stdout or "").splitlines() if line.startswith("PTCC:")]
    problem = marks[-1].removeprefix("PTCC:") if r.returncode == 0 and marks else ""
    return f"{problem}\n{has_compiler_hint(_venv_platform(tool))}" if problem else None


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
