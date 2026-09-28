"""wheel: installable package (`uv tool install`, pip) with a command.

- cpython / pypy: pure wheel (py3-none-any), works on any compatible interpreter.
- mypyc: platform wheel (cp314-win_amd64...) with the extensions compiled by
  setuptools + mypycify; it also includes the .py files (Python loads the binary first).

The build project is generated in .build/wheel/<backend>/: your pyproject.toml does not
need [build-system] (that way uv treats it as an app, not as a package). It is built in the
locked tools environment (.venv, no build isolation): setuptools, mypy and the project's
dependencies exactly as uv.lock pins them, the same packages the mypyc stage compiles with,
and no network. An isolated build env would resolve its requirements from PyPI at build time,
and mypycify there could not see the project's dependencies.
"""

from __future__ import annotations

import glob
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .. import envs, mypyc, proc, render, ui
from ..cmd_build import BuildRequest, dist_path
from ..config import Config, compiled_paths, toml_value
from ..project import BUILD, EXT_SUFFIXES, PYPROJECT, SRC, rel
from ..ui import PytError
from . import common


def _read_toml(path: Path) -> dict[str, Any]:
    """pyproject.toml or uv.lock, read as uv reads them (a BOM is fine). One that cannot be read
    or is no valid TOML is a PytError naming it: wheel.check reads pyproject.toml before
    cmd_build.check_lock, and a broken one ended in an internal-error traceback."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise PytError(f"wheel: cannot read {path.name}: {e.strerror or e}") from None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise PytError(f"wheel: {path.name} is not valid TOML: {e}") from None


def _locked_version(package: str) -> str:
    lock = _read_toml(PYPROJECT.parent / "uv.lock")
    for pkg in lock.get("package", []):
        if pkg.get("name") == package:
            return str(pkg["version"])
    raise PytError(
        f"wheel: {package} is not in uv.lock: the wheel is built with the locked {package} of the dev group "
        f"(./pyt add {package} --dev --cpython-only)"
    )


def _outside_package(cfg: Config) -> list[str]:
    """The top-level entries of src/ that compile.modules names besides the package: a lone
    module (`fastbench.py`) or another package. The app imports them, so the wheel carries them
    (mypycify compiles them from the build project; without them it stopped with "Cannot read
    file 'src/fastbench.py'", and a cpython wheel left them out). An entry that names nothing
    is left to mypyc.compiled_sources, which refuses it for a mypyc build."""
    tops: list[str] = []
    for rel_path in compiled_paths(cfg):
        top = rel_path.split("/")[0]
        if top != cfg.pkg and top not in tops and (SRC / top).exists():
            tops.append(top)
    return tops


_REQ_HEAD = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dependencies(data: dict[str, Any]) -> list[str]:
    """[project] dependencies as a wheel's metadata can declare them.

    [tool.uv.sources] says where uv takes a dependency from; a wheel only knows index names
    and direct references. Copied as they were, a local library became a PyPI requirement of
    that name (an unrelated package when PyPI has one, installed without a word). A git or URL
    source becomes a direct reference (`name @ git+URL@REV`); a path, workspace or editable
    source, a named index, a marker or several sources cannot be declared: PytError (2).
    """
    deps = [str(d) for d in data.get("project", {}).get("dependencies", [])]
    sources = {_norm(k): v for k, v in data.get("tool", {}).get("uv", {}).get("sources", {}).items()}
    out: list[str] = []
    refused: list[str] = []
    for dep in deps:
        m = _REQ_HEAD.match(dep)
        source = sources.get(_norm(m[1])) if m else None
        if m is None or source is None:
            out.append(dep)
            continue
        name, extras = m[1], m[2] or ""
        marker = dep.split(";", 1)[1].strip() if ";" in dep else ""
        where = ""
        if isinstance(source, dict) and "marker" not in source:
            if isinstance(source.get("git"), str):
                url = source["git"] if source["git"].startswith("git+") else f"git+{source['git']}"
                ref = next((source[k] for k in ("rev", "tag", "branch") if isinstance(source.get(k), str)), "")
                where = url + (f"@{ref}" if ref else "") + (f"#subdirectory={source['subdirectory']}" if source.get("subdirectory") else "")
            elif isinstance(source.get("url"), str):
                # An archive whose project is not at its root: uv's own requirement names the
                # folder (without it pip and uv built the archive's root: another project, or none)
                sub = source.get("subdirectory")
                where = source["url"] + (f"{'&' if '#' in source['url'] else '#'}subdirectory={sub}" if sub else "")
        if not where:
            refused.append(f"{name} ({json.dumps(source, ensure_ascii=False)})")
            continue
        out.append(f"{name}{extras} @ {where}" + (f" ; {marker}" if marker else ""))
    if refused:
        raise PytError(
            "wheel: these dependencies come from a source a wheel cannot declare ([tool.uv.sources]): "
            + ", ".join(refused)
            + ". The wheel would name them as PyPI packages. Publish them to an index and depend on them by "
            "name, or build with --method pyz or portable, which carry them",
            2,
        )
    return out


def check(cfg: Config) -> None:
    """What the wheel cannot build, refused before the checks and the payload (also --dry-run)."""
    dependencies(_read_toml(PYPROJECT))


def _package_data(package: Path) -> list[str]:
    """The package-data patterns that name every file of a package copied into the build project.

    setuptools expands them with the stdlib glob, whose "**/*" matches no name that starts with
    a dot and enters no folder that does (glob's include_hidden stays off): the wheel left out
    src/assets/.fonts/, data/.keep or a .env-style file without a word, while every other method
    ships them. Each such file is named by its own path, escaped: glob returns a literal path that
    exists, and a pattern whose name starts with a dot matches a hidden one.
    """
    patterns = ["**/*"]
    for folder, dirs, files in os.walk(package):
        dirs.sort()
        for name in sorted(files):
            path = (Path(folder) / name).relative_to(package).as_posix()
            if not any(part.startswith(".") for part in path.split("/")):
                continue  # "**/*" matches it
            if any("\ud800" <= c <= "\udfff" for c in path):
                raise PytError(f"wheel: {rel(Path(folder) / name)}: its name is not valid UTF-8, which a wheel cannot hold: rename it")
            patterns.append(glob.escape(path))
    return patterns


def _pyproject(cfg: Config, compiled: bool, src: Path | None = None) -> str:
    """The build project's pyproject.toml; `src`: its src/ folder, whose hidden files are listed."""
    data = _read_toml(PYPROJECT)
    project = data["project"]
    outside = _outside_package(cfg)
    modules = [top.removesuffix(".py") for top in outside if top.endswith(".py")]
    packages = [cfg.pkg, *(top for top in outside if not top.endswith(".py"))]
    entry = cfg.deploy.wheel.entry or f"{cfg.pkg}.app:main"
    # Informational: the build runs without isolation, with what .venv has (these exact versions)
    requires = [f"setuptools=={_locked_version('setuptools')}"] + ([f"mypy=={_locked_version('mypy')}"] if compiled else [])
    # A mypyc wheel is a cpXY platform wheel: with the project's open range uv tool install took
    # the newest CPython it has (3.14 for a python.cpython of 3.13, 3.15 once installed) and
    # found "no wheels with a matching Python version tag"; pinned, it takes (or fetches) this one
    python = f"=={cfg.python.cpython}.*" if compiled else project.get("requires-python", ">=3.11")
    lines = [
        "[build-system]",
        f"requires = {json.dumps(requires)}",
        'build-backend = "setuptools.build_meta"',
        "",
        "[project]",
        f"name = {json.dumps(project['name'])}",
        f"version = {json.dumps(project['version'])}",
        f"description = {json.dumps(project.get('description', ''), ensure_ascii=False)}",
        f"requires-python = {json.dumps(python)}",
        f"dependencies = {json.dumps(dependencies(data))}",
        "",
        # A GUI app gets a launcher without a console window on Windows (like exe's console = auto)
        "[project.gui-scripts]" if cfg.app.gui else "[project.scripts]",
        f"{json.dumps(cfg.app.name)} = {json.dumps(entry)}",
        "",
        *(["[tool.setuptools]", f"py-modules = {json.dumps(modules)}", ""] if modules else []),
        "[tool.setuptools.packages.find]",
        'where = ["src"]',
        "",
        # Every file of the package travels with it: data files, py.typed, vendored native
        # libraries, and the assets (copied into <pkg>/assets, where resources.py looks)
        "[tool.setuptools.package-data]",
        *(f"{json.dumps(name)} = {toml_value(_package_data(src / name) if src else ['**/*'])}" for name in packages),
    ]
    return "\n".join(lines) + "\n"


# The install step bdist_wheel runs, in every generated setup.py. setuptools expands $NAME and
# {NAME} in the install prefix, sys.prefix: the .venv of a project folder such as app$v2 or br{x}
# stopped every wheel build ("invalid variable 'v2'"), and inside a virtual environment it
# ignores a prefix given in setup.cfg. The wheel never installs there (bdist_wheel names every
# folder it installs to), so the class gives the step a prefix that holds nothing to expand.
INSTALL_CLASS = '''\
from setuptools.command.install import install


class Install(install):
    """The install step of bdist_wheel, with a prefix that holds no $NAME: setuptools expands
    those of sys.prefix, the .venv of the project folder, which the wheel never installs to."""

    def finalize_options(self):
        if self.prefix is None and self.home is None and not self.user and self.install_base is None:
            self.prefix = "."
        super().finalize_options()
'''

PURE_SETUP_PY = '''\
"""Generated by ./pyt build --method wheel."""
from setuptools import setup

{install}

setup(cmdclass={{"install": Install}})
'''

SETUP_PY = '''\
"""Generated by ./pyt build --method wheel: compiles the modules in compile.modules with mypyc."""
import os
import sys

from mypyc.build import mypycify
from setuptools import setup

# Each module is named from src/ (an explicit package base), as Python imports it: without it
# mypy named a namespace folder's nsx/fast.py "fast" (tools/mypyc_build.py does the same)
os.environ["MYPYPATH"] = os.pathsep.join(["src", *filter(None, [os.environ.get("MYPYPATH")])])
extensions = mypycify(
    ["--config-file", "mypy.ini", "--explicit-package-bases", *{files!r}],
    opt_level={opt!r},
    debug_level="0",
    strip_asserts={strip!r},
    multi_file={multi_file!r},
    separate={separate!r},
    strict_dunder_typing={strict_dunder_typing!r},
    group_name={group!r},
)

# The C flags the mypyc stage adds too (tools/mypyc_build.py: compiler_type, extra_cflags)
from distutils import ccompiler, sysconfig  # noqa: E402 (setuptools' copy, after mypyc.build)

compiler = ccompiler.new_compiler()
sysconfig.customize_compiler(compiler)
flags = []
if compiler.compiler_type == "unix":
    flags.append("-fno-strict-overflow")
    if {no_semantic_interposition!r} and sys.platform == "linux":
        flags.append("-fno-semantic-interposition")
for ext in extensions:
    ext.extra_compile_args = [*ext.extra_compile_args, *flags]

{install}

setup(ext_modules=extensions, cmdclass={{"install": Install}})
'''


def setup_py(cfg: Config, compiled: bool = True) -> str:
    """Return the setup.py of the wheel's build project: for mypyc the same [compile] options as
    the mypyc stage; every one runs the install step with INSTALL_CLASS."""
    if not compiled:
        return PURE_SETUP_PY.format(install=INSTALL_CLASS)
    return SETUP_PY.format(
        files=[f"src/{p.relative_to(SRC).as_posix()}" for p in mypyc.compiled_sources(cfg)],
        opt=cfg.compile.opt_level,
        no_semantic_interposition=cfg.compile.no_semantic_interposition,
        strip=cfg.deploy.optimize >= 1,
        multi_file=cfg.compile.multi_file,
        separate=cfg.compile.separate,
        strict_dunder_typing=cfg.compile.strict_dunder_typing,
        group=None if cfg.compile.separate else mypyc.group_name(cfg),
        install=INSTALL_CLASS,
    )


def _stray_output(path: Path) -> bool:
    """A build output left in src/ (a stray in-place compile): an extension next to the .py it
    was built from, or a mypyc shared lib. Other .so/.pyd files are app content."""
    if not path.name.endswith(EXT_SUFFIXES):
        return False
    stem = path.name.split(".")[0]
    return stem.endswith("__mypyc") or path.with_name(f"{stem}.py").is_file()


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy a folder of src/ into the build project as the stage and the payloads copy it
    (mypyc.walk: a symlinked folder is followed, a link back up its own path is not, caches stay
    out; a broken link is a warning), leaving out stray build outputs; a file already there is
    replaced (the assets go into the package's folder). shutil.copytree followed a link cycle
    until the path was too long, and stopped on a dangling link, in an internal-error traceback.
    Every copy is owner-writable (mypyc.copy_writable): build_ext --inplace writes next to the
    sources, and the next build must be able to delete them."""
    from ..cli import NO_ROOM

    dst.mkdir(parents=True, exist_ok=True)
    for path in mypyc.walk(src):
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(exist_ok=True)
        elif not path.exists():
            ui.warn(f"{rel(path)}: broken symbolic link, not copied")
        elif not _stray_output(path):
            try:
                mypyc.copy_writable(os.fspath(path), os.fspath(target))
            except OSError as e:
                if e.errno in NO_ROOM:
                    raise  # a full disk: cli.main names the file
                raise PytError(f"wheel: cannot copy {rel(path)}: {e.strerror or e}") from None


def build(req: BuildRequest) -> Path:
    cfg = req.cfg
    package = SRC / cfg.pkg
    if not package.is_dir():
        raise PytError(f"wheel: package src/{cfg.pkg}/ not found")
    work = BUILD / "wheel" / req.backend
    if work.exists():
        mypyc.remove_tree(work)
    (work / "src").mkdir(parents=True)
    _copy_tree(package, work / "src" / cfg.pkg)
    for top in _outside_package(cfg):  # compile.modules outside the package (a lone module)
        if (SRC / top).is_dir():
            _copy_tree(SRC / top, work / "src" / top)
        else:
            mypyc.copy_writable(str(SRC / top), str(work / "src" / top))
    assets = cfg.app.assets
    if assets and (SRC / assets).is_dir():
        # In a wheel the assets travel inside the package (resources.py looks for them there)
        _copy_tree(SRC / assets, work / "src" / cfg.pkg / "assets")
    mypyc.make_writable(work / "src")
    (work / "pyproject.toml").write_text(_pyproject(cfg, req.compiled, work / "src"), encoding="utf-8", newline="\n")
    (work / "setup.py").write_text(setup_py(cfg, req.compiled), encoding="utf-8", newline="\n")
    if req.compiled:
        (work / "mypy.ini").write_text(render.mypy_ini(cfg, "mypyc", for_compile=work), encoding="utf-8", newline="\n")
    tool = envs.tool_env(cfg)
    # Synced first (a `--no-check` build never ran `uv run --locked`), before the previous wheel
    # is removed: a failed sync left no wheel at all. `uv build` ignores UV_PROJECT_ENVIRONMENT
    # (it would take ./.venv, wrong under WSL), hence --python
    envs.sync(tool)
    out = dist_path(req)
    common.remove_output(out)
    argv: list[str | Path] = ["build", "--wheel", "--no-build-isolation", "--python", tool.python, "--out-dir", out, work]
    # -q hides uv's progress, never why the build failed (mypy's errors, the C compiler's, which
    # uv's --quiet dropped): captured then, and shown when it fails
    built = envs.uv(tool, argv, extra_env={"VSLANG": "1033"}, capture=ui.QUIET, check=False)
    if built.returncode != 0:
        output = ((built.stdout or "") + (built.stderr or "")).rstrip()
        if output:
            ui.report(output)
        # A C compiler mypyc cannot start is a missing requirement (exit 3 and how to get one),
        # as for the stage: uv's exit code 2 and its generic "build failures" hint said neither
        missing = mypyc.missing_compiler(tool) if req.compiled else None
        if missing:
            raise PytError(f"wheel: {missing}", 3)
        raise proc.CommandFailed(built.args, built.returncode)
    wheels = sorted(out.glob("*.whl"))
    if not wheels:
        raise PytError("uv build did not produce any wheel")
    # uv tool install takes the newest CPython it has, whatever the wheel's Requires-Python says
    # (uv 0.10.12 and 0.12.19): a mypyc wheel, which loads only in its minor, needs the request
    python = f"--python {cfg.python.cpython} " if req.compiled else ""
    ui.info(f"  install it with: uv tool install {python}{rel(wheels[0])}   (command: {cfg.app.name})")
    return wheels[0]
