"""nuitka: executable built with Nuitka (CPython only). It also compiles your dependencies to C.

Much slower builds than PyInstaller, in exchange for a native executable. With the
mypyc backend, your core modules are already compiled by mypyc (Nuitka includes the
.pyd files as-is) and Nuitka compiles the rest. Nuitka runs through `uv run --with nuitka==...`
(it does not go into uv.lock or the development environment).
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import tarfile
import urllib.request
import zipfile
import zlib
from collections.abc import Sequence
from pathlib import Path

from .. import envs, mypyc, proc, ui, upx
from ..cmd_build import BuildRequest, dist_path
from ..config import Config
from ..project import BUILD, IS_MACOS, IS_WINDOWS, ROOT, rel
from ..ui import PytError
from .common import remove_output

# Nuitka is not in uv.lock (`uv run --with`), so it is pinned here to keep builds reproducible:
# the latest release on PyPI in September 2026. Bump it deliberately, together with NUITKA_PYTHON.
NUITKA = "nuitka==4.2.2"
# The newest CPython minor NUITKA supports: 4.2.2 stops with FATAL on 3.15 ("not supported by
# Nuitka '4.2.2'") and only warns on later minors, then fails obscurely in the C compile.
NUITKA_PYTHON = "3.14"


def _minor(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def check_python(cfg: Config, args: Sequence[str]) -> None:
    """Refuse a python.cpython newer than the pinned Nuitka supports (cmd_build calls this before
    the checks and the payload: Nuitka itself would stop after minutes of work). Nuitka's own
    `--experimental=python3.X` (deploy.nuitka.extra_args or the command line) skips it."""
    wanted = cfg.python.cpython
    flag = f"python{wanted}"
    given = list(args)
    experimental = f"--experimental={flag}" in given or any(
        a == "--experimental" and b == flag for a, b in zip(given, given[1:], strict=False)
    )
    if _minor(wanted) <= _minor(NUITKA_PYTHON) or experimental:
        return
    raise PytError(
        f"python.cpython = {wanted!r}, but {NUITKA} only supports CPython up to {NUITKA_PYTHON}.\n"
        f"  Bump NUITKA and NUITKA_PYTHON in .pytemplate/runner/methods/nuitka.py to a Nuitka release\n"
        f"  that supports {wanted}, or try the pinned one anyway with --experimental={flag}",
        3,
    )


PGO_NOTE = (
    "  note: deploy.nuitka.pgo is experimental in standalone and onefile builds (Nuitka says so itself); "
    "measured: 10-15% faster pure-Python loops, nothing elsewhere. A dependency with a pure-Python "
    "fallback (msgpack) may be profiled on that fallback path."
)


def optimization_args(cfg: Config) -> list[str]:
    """The Nuitka flags of [deploy.nuitka] lto/pgo. build() puts them BEFORE extra_args and the
    command line, so an --lto given there still wins (Nuitka takes the last value).

    lto: Nuitka's "auto" is yes with uv's CPython (gcc/clang on Linux and macOS, MSVC on Windows)
    unless more than 250 modules are compiled: the stdlib goes in as bytecode and does not count,
    the app and the third-party code Nuitka follows or includes do (flet: ~800 modules, no LTO).
    pgo: --pgo-c runs the app once during the build to profile the C code; pgo_args are the
    app's arguments for that run, one string that Nuitka splits with shlex on every OS.
    """
    nuitka = cfg.deploy.nuitka
    args = [f"--lto={nuitka.lto}"]
    if nuitka.pgo:
        args.append("--pgo-c")
        if nuitka.pgo_args:
            args.append(f"--pgo-args={shlex.join(nuitka.pgo_args)}")
    return args


# What SCons (Nuitka compiles through it) substitutes in a path Nuitka hands it: $NAME, ${...},
# $$, $( and $) (SCons.Subst._dollar_exps_str, the same in the SCons 3 and 4 Nuitka 4.2.2 ships).
# Nuitka passes the absolute paths of --output-dir unescaped: under app$v2 SCons made
# app/<...>/.build/nuitka/<b>/main.build OUTSIDE the project (another project's folder, maybe),
# and the build failed where that path could not be made.
SCONS_EXPANDS = re.compile(r"\$(?:[$()]|[_A-Za-z][.\w]*|\{[^}]*\}?)")


def check_options(cfg: Config, backend: str) -> None:
    """What the build refuses before any work (cmd_build calls this before the checks, also in
    --dry-run): a project folder SCons would expand, and the PGO rules that depend on the build
    (config.validate checks the config-only ones: app.gui, app.assets, pgo_args without pgo)."""
    found = SCONS_EXPANDS.search(str(BUILD))
    if found:
        raise PytError(
            f"nuitka: the project's folder holds {found[0]!r} ({ROOT}), which SCons (Nuitka compiles "
            "through it) reads as a variable: the build would write outside the project, or fail.\n"
            "  Move the project to a folder whose path has no '$', or build with --method exe, portable or pyz",
            2,
        )
    if not cfg.deploy.nuitka.pgo:
        return
    if backend == "mypyc":
        raise PytError(
            "deploy.nuitka.pgo does not work with the mypyc backend: Nuitka's profiling run starts the app "
            "before main.dist holds the compiled extension modules, so they fail to import (ImportError) "
            "while Nuitka still reports success. Use ./pyt build cpython --method nuitka, or set pgo = false",
            2,
        )
    if IS_MACOS:
        raise PytError(
            "deploy.nuitka.pgo is not available on macOS: Nuitka 4.2.2 has no clang profdata step there. "
            "Set pgo = false, or build on Linux or Windows",
            2,
        )


# Runs in the tools environment with the stage first on sys.path and prints the names whose
# top-level module exists there and is not built in (find_spec of a top-level name imports
# nothing; Nuitka resolves dotted names such as os.path itself)
_LOCATE = r"""
import importlib.util, json, sys
sys.path.insert(0, sys.argv[1])
def found(name):
    top = name.partition(".")[0]
    if top in sys.builtin_module_names:
        return False
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError):
        return False
print("PTLOCATE" + json.dumps([n for n in sys.argv[2:] if found(n)]))
"""


def _module_of(ext: Path, stage: Path) -> str:
    """Module name of an extension: myapp/core/bench.cpython-314-x86_64-linux-gnu.so -> myapp.core.bench."""
    path = ext.relative_to(stage)
    return ".".join([*path.parent.parts, path.name.split(".")[0]])


def includable(cfg: Config, stage: Path, names: Sequence[str]) -> list[str]:
    """Return the hidden imports Nuitka can take as --include-module.

    Nuitka stops with FATAL on a module it cannot locate, and mypyc.hidden_imports lists every
    import of the compiled code, also a platform-guarded `import winreg` or an optional
    `try: import orjson`. The compiled modules and extensions are always kept; the other names
    only when the build environment finds their top-level module (built-ins are dropped).
    """
    compiled = set(mypyc.compiled_modules(cfg)) | {_module_of(p, stage) for p in mypyc.extension_files(stage)}
    imported = sorted(set(names) - compiled)
    keep: set[str] = set()
    if imported:
        argv: list[str | Path] = ["run", "--locked", "python", "-c", _LOCATE, stage, *imported]
        out = envs.uv(envs.tool_env(cfg), argv, capture=True, echo=False).stdout
        line = next((ln for ln in out.splitlines() if ln.startswith("PTLOCATE")), None)
        if line is None:
            raise PytError(f"could not check the imports of the compiled modules: {out.strip()!r}")
        keep = set(json.loads(line[len("PTLOCATE") :]))
        dropped = [n for n in imported if n not in keep]
        if dropped:
            ui.detail(f"  not passed to Nuitka (not importable here, or built in): {', '.join(dropped)}")
    return sorted((set(names) & compiled) | keep)


def archive_problem(path: Path, name: str = "") -> str:
    """Return why a Flet client archive cannot be bundled ("" when it is whole).

    It reads the archive to its end the way flet_desktop extracts it at the app's first start
    (zipfile, or tarfile over gzip; `name`, default the file's own, tells which): a download cut
    short, or a page that is no archive at all, would otherwise ship, and the app would fail
    there with no download to fall back on.
    """
    try:
        if (name or path.name).endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                damaged = z.testzip()  # reads every member and checks its CRC
                if damaged is not None:
                    return f"{damaged} is damaged"
                return "" if z.namelist() else "it holds no file"
        with gzip.open(path, "rb") as g, tarfile.open(fileobj=g, mode="r:") as t:
            members = t.getmembers()
            while g.read(1 << 20):  # the rest of the stream: gzip checks its length and CRC at the end
                pass
        return "" if members else "it holds no file"
    except (OSError, EOFError, ValueError, tarfile.TarError, zipfile.BadZipFile, zlib.error) as e:
        return str(e) or type(e).__name__


# flet_desktop names the Linux client from the machine it runs on: the glibc bracket (a distro),
# the flavor (FLET_DESKTOP_FLAVOR, else [tool.flet] desktop_flavor of a pyproject.toml in its
# CURRENT folder, else light) and the CPU: flet-linux-<distro>[-light]-<arch>.tar.gz
_LINUX_CLIENT = re.compile(r"flet-linux-(?P<distro>.+?)(?P<light>-light)?-(?P<arch>[a-z0-9_]+)\.tar\.gz")


def flet_client_env(name: str) -> dict[str, str]:
    """The environment under which the shipped app looks for exactly the bundled archive `name`.

    Linux: the distro and flavor the name was made of. The user's glibc, a pyproject.toml in
    the folder the app was started from, or their own FLET_* variables named another archive,
    and the app downloaded its client at its first start (offline: it failed). Windows and macOS
    have one name each: nothing to pin.
    """
    m = _LINUX_CLIENT.fullmatch(name)
    if not m:
        return {}
    return {"FLET_LINUX_DISTRO": m.group("distro"), "FLET_DESKTOP_FLAVOR": "light" if m.group("light") else "full"}


def _flet_client_archive(cfg: Config) -> Path:
    """Return the Flet desktop client archive of the locked flet-desktop (downloaded once).

    Same file and URL as flet_desktop's own first-start download (flet-windows.zip,
    flet-macos.tar.gz or the glibc-matched Linux tarball; FLET_CLIENT_URL replaces the URL, as
    in flet_desktop), cached in .build/flet-client/. The download must deliver every byte the
    server announced and the archive must read to its end (archive_problem) before it is
    cached; a cached archive is checked again, so a damaged one is downloaded anew.
    """
    query = "import flet_desktop, flet_desktop.version as v; print(flet_desktop.get_artifact_filename(), v.version)"
    out = envs.uv(envs.tool_env(cfg), ["run", "--locked", "python", "-c", query], capture=True, echo=False).stdout.split()
    if len(out) != 2:
        raise PytError(f"could not ask flet_desktop for its client archive: {' '.join(out)!r}")
    name, version = out
    archive = BUILD / "flet-client" / version / name
    if archive.is_file():
        problem = archive_problem(archive)
        if not problem:
            _fingerprint(archive)  # a cache from before the sidecar
            return archive
        ui.warn(f"{rel(archive)} is damaged ({problem}): downloading it again")
        archive.unlink()
    fingerprint_file(archive).unlink(missing_ok=True)  # it names the old bytes
    url = f"https://github.com/flet-dev/flet/releases/download/v{version}/{name}"
    url = os.environ.get("FLET_CLIENT_URL") or url  # flet_desktop's own override (a mirror)
    source = f"{url} (FLET_CLIENT_URL)" if os.environ.get("FLET_CLIENT_URL") else url
    ui.info(f"  downloading the Flet client to bundle: {source}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    partial = archive.with_suffix(archive.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=300) as r, partial.open("wb") as f:  # noqa: S310 (https URL or the user's mirror)
            announced = r.headers.get("Content-Length")
            written = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                written += len(chunk)
        # http.client ends a body cut short (a closed connection, a ragged TLS end) like a whole
        # one when the server announced its length: only the count shows it
        if announced is not None and announced.strip().isdecimal() and written != int(announced):
            problem = f"the download ended after {written} of {int(announced)} bytes"
        else:
            problem = archive_problem(partial, name)
    except (OSError, ValueError, http.client.HTTPException) as e:  # IncompleteRead is no OSError
        partial.unlink(missing_ok=True)
        raise PytError(f"cannot download the Flet client {source}: {str(e) or type(e).__name__}", 3) from None
    if problem:
        partial.unlink(missing_ok=True)
        raise PytError(f"the Flet client downloaded from {source} is not a whole archive: {problem}. Build again to retry", 3)
    partial.replace(archive)
    _fingerprint(archive)
    return archive


def fingerprint_file(archive: Path) -> Path:
    """<archive>.sha256, next to the archive in the cache and where it is bundled."""
    return archive.with_name(archive.name + ".sha256")


def _fingerprint(archive: Path) -> None:
    """Write `<sha256> <size>` into fingerprint_file(archive), as `flet pack` does: flet_desktop
    reads it at every start instead of hashing the whole client (tens of MB) again, which it
    did at every start that could not keep its own copy next to the archive (every onefile
    start, a read-only install). A sidecar that names the current size is kept."""
    sidecar = fingerprint_file(archive)
    size = archive.stat().st_size
    try:
        digest, recorded = sidecar.read_text(encoding="ascii").split()
        if recorded == str(size) and len(digest) == 64:
            return
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    h = hashlib.sha256()
    with archive.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    sidecar.write_text(f"{h.hexdigest()} {size}", encoding="ascii")


# The package data a Flet app reads at runtime (flet_cli/__pyinstaller/hook-flet.py collects the same)
FLET_PACKAGE_DATA = ("flet.controls.material:icons.json", "flet.controls.cupertino:cupertino_icons.json")


def _stage_icon(cfg: Config, stage: Path) -> str:
    """deploy.exe.icon, copied into the stage (Nuitka's cwd) and named relative to it.

    Nuitka reads what follows the last '#' of --windows-icon-from-ico as an icon index ("ICON#N")
    on Windows, and stops when it is no number (with a TypeError: its message has two %s for one
    value): the absolute path of a project under a folder such as C:\\dev\\C#\\game stopped every
    build with an icon before it compiled anything. The copy's name holds no '#'.
    """
    icon = ROOT / cfg.deploy.exe.icon
    if not icon.is_file():
        raise PytError(f"nuitka: deploy.exe.icon = {cfg.deploy.exe.icon!r} does not exist (relative to the project root)")
    name = "pyt-icon" + ("" if "#" in icon.suffix else icon.suffix)
    mypyc.copy_writable(os.fspath(icon), os.fspath(stage / name))
    return name


def build(req: BuildRequest) -> Path:
    cfg = req.cfg
    check_python(cfg, [*cfg.deploy.nuitka.extra_args, *req.extra])
    check_options(cfg, req.backend)
    stage = BUILD / "nuitka-stage" / req.backend
    if req.compiled:
        mypyc.exe_stage(cfg, req.app_dir, stage)
    else:
        if stage.exists():
            mypyc.remove_tree(stage)
        shutil.copytree(req.app_dir, stage, ignore=shutil.ignore_patterns("__pycache__"), copy_function=mypyc.copy_writable)
    work = BUILD / "nuitka" / req.backend
    if work.exists():
        mypyc.remove_tree(work)
    onefile = req.onefile if req.onefile is not None else cfg.deploy.nuitka.mode == "onefile"
    exe_name = cfg.app.name + (".exe" if IS_WINDOWS else "")
    if not IS_WINDOWS and not onefile and cfg.app.name.lower() == cfg.pkg:
        # standalone puts the binary in main.dist/ next to the package folder <pkg>/ (mypyc
        # extensions, package data): named like the app it would be a FILE where that folder
        # must go (NotADirectoryError; case-insensitive on macOS). .bin is Nuitka's own POSIX
        # suffix for its intermediate binaries.
        exe_name += ".bin"

    # -P: `python -m` puts the cwd (the stage) first on sys.path, so an app package named like
    # Nuitka, or like a module Nuitka imports (zstandard...), ran instead ("'nuitka' is a package
    # and cannot be directly executed"). Nuitka finds the app from the folder of main.py.
    argv: list[str | Path] = [
        "python", "-P", "-m", "nuitka", stage / "main.py",
        f"--mode={'onefile' if onefile else 'standalone'}",
        f"--output-dir={work}",
        f"--output-filename={exe_name}",
        "--assume-yes-for-downloads",
        f"--include-package={cfg.pkg}",
    ]
    if req.compiled:
        argv += [f"--include-module={m}" for m in includable(cfg, stage, mypyc.hidden_imports(cfg, stage))]
    if cfg.deploy.optimize >= 1:
        argv.append("--python-flag=no_asserts")
    if cfg.deploy.optimize >= 2:
        argv.append("--python-flag=no_docstrings")
    # Data paths relative to the stage (Nuitka's cwd): Nuitka splits a data source at every ','
    # and '=' and reads it as a glob, so an absolute path under a folder like `game, v2` left
    # the Flet client out with only a warning (and '=' or '[' stopped the build).
    # --include-raw-dir, never --include-data-dir: that one copies only "non-code files" and
    # left out, without a word, every asset named like code (level1.bin, model.bin, tool.exe,
    # plugin.dll, libfmod.so, a level script .py: Nuitka 4.2.2's default_ignored_suffixes),
    # py.typed and .DS_Store, while the other methods ship the whole folder
    assets = cfg.app.assets
    if assets and (stage / assets).is_dir():
        argv.append(f"--include-raw-dir={assets}={assets}")
    if cfg.app.gui and IS_WINDOWS:
        argv.append("--windows-console-mode=disable")
    if cfg.deploy.exe.icon and IS_WINDOWS:
        argv.append(f"--windows-icon-from-ico={_stage_icon(cfg, stage)}")
    argv += [f"--nofollow-import-to={m}" for m in cfg.deploy.exclude_modules]
    use_upx = upx.active(cfg)
    if use_upx and onefile:
        # onefile: Nuitka's plugin packs the one binary (--best --lzma: deploy.upx.level does not
        # apply); the libraries inside its zstd payload are never packed. A standalone folder is
        # packed when it is done (pack_tree, below): the plugin packed every DLL it copied, the
        # Python DLL and deploy.upx.exclude included (it has no exclude option)
        if upx.excluded(cfg, exe_name):
            ui.info(f"  upx: {exe_name} matches deploy.upx.exclude: not packed")
        else:
            argv += ["--plugin-enable=upx", f"--upx-binary={upx.find(cfg)}"]
    if cfg.app.preset == "flet":
        # flet loads its controls lazily (module __getattr__ + importlib), which Nuitka cannot
        # follow; and the flet-desktop wheel has no Flutter client: bundle the release archive
        # where flet_desktop looks for one (flet_desktop/app/), as `flet pack` does. Nuitka
        # bundles no package data by default: ft.Icons and ft.CupertinoIcons read these two
        # JSON files (flet_cli's own PyInstaller hook adds the same), and without them the app
        # died with FileNotFoundError at its first icon.
        archive = _flet_client_archive(cfg)
        sidecar = fingerprint_file(archive)
        (stage / "flet-client").mkdir(exist_ok=True)
        for file in (archive, sidecar):
            shutil.copy2(file, stage / "flet-client" / file.name)
        linux = flet_client_env(archive.name)
        # The taskbar identity flet pack's runtime hook sets: on Linux the client window groups
        # and labels as the app (FLET_APP_ID), not as the shared "flet" binary. Windows wants
        # the executable's path at runtime (FLET_APP_USER_MODEL_ID), which Nuitka cannot force
        identity = {"FLET_APP_ID": cfg.app.name} if linux else {}
        argv += [
            "--include-package=flet",
            "--include-package=flet_desktop",
            *(f"--include-package-data={data}" for data in FLET_PACKAGE_DATA),
            f"--include-data-files=flet-client/{archive.name}=flet_desktop/app/{archive.name}",
            # flet_desktop reads the fingerprint instead of hashing the client at every start
            f"--include-data-files=flet-client/{sidecar.name}=flet_desktop/app/{sidecar.name}",
            # Linux: the app looks for exactly this archive, not for the name of the user's glibc
            # or of a pyproject.toml in the folder it starts from (it downloaded one at first start)
            *(f"--force-runtime-environment-variable={k}={v}" for k, v in {**linux, **identity}.items()),
        ]
    argv += optimization_args(cfg)  # before extra_args and the command line: a later --lto wins
    argv += cfg.deploy.nuitka.extra_args + req.extra

    out = dist_path(req)
    remove_output(out)  # before minutes of work: a running build of it is refused now
    ui.info("  Nuitka compiles everything to C: the first build takes several minutes")
    if cfg.deploy.nuitka.pgo:
        ui.info(PGO_NOTE)
    try:
        envs.uv(envs.tool_env(cfg), ["run", "--locked", "--with", NUITKA, *argv], cwd=stage)
    except proc.CommandFailed as e:
        raise PytError(
            f"{e}\n  Nuitka is pinned to {NUITKA} (NUITKA in .pytemplate/runner/methods/nuitka.py): if it says"
            f" Python {cfg.python.cpython} is not supported, bump it to a release that supports it",
            e.code,
        ) from None

    produced = sorted(work.iterdir()) if work.is_dir() else []
    if onefile:
        exe = next((p for p in produced if p.is_file() and p.name.startswith(cfg.app.name)), None)
        if exe is None:
            raise PytError(f"nuitka finished without producing {cfg.app.name}* in {work}")
        out.mkdir(parents=True)
        shutil.move(str(exe), str(out / exe.name))
        return out / exe.name
    dist_dir = next((p for p in produced if p.is_dir() and p.name.endswith(".dist")), None)
    if dist_dir is None:
        raise PytError(f"nuitka finished without producing a *.dist folder in {work}")
    shutil.move(str(dist_dir), str(out))
    if use_upx:
        upx.pack_tree(cfg, out)  # BUILTIN_EXCLUDE, deploy.upx.exclude and the level apply, as for portable
    ui.info(f"  run: {rel(out / exe_name)}")
    return out
