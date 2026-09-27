"""Pieces shared by portable and pyz: platform keys, per-target dependencies."""

from __future__ import annotations

import configparser
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import sysconfig
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import envs, proc, ui
from ..config import Config
from ..imports import PARSE_ERRORS, iter_runtime_nodes, parse
from ..project import BUILD, EXT_SUFFIXES, PYPROJECT, SRC, host_os, rel
from ..ui import PytError

NATIVE_SUFFIXES = (*EXT_SUFFIXES, ".dll", ".dylib")
LOCK = PYPROJECT.parent / "uv.lock"
# ASCII digits only (\d also matches other scripts' digits) and no trailing newline ($ allows one):
# parse_key uses fullmatch
KEY_RE = re.compile(r"(cp|pp)([0-9])([0-9]+)-(windows|linux|macos)-(x86_64|aarch64)")
# uv platform (--python-platform) for each (OS, architecture). It is also the floor of every
# target, cross or host: manylinux_2_28 = glibc 2.28+ (RHEL 8, Debian 10, Ubuntu 20.04) on
# x86_64, glibc 2.35+ on aarch64; macOS: MACOS_FLOOR; Windows wheels have no OS floor.
UV_PLATFORMS = {
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
    ("windows", "aarch64"): "aarch64-pc-windows-msvc",
    ("linux", "x86_64"): "x86_64-manylinux_2_28",
    ("linux", "aarch64"): "aarch64-manylinux_2_35",
    ("macos", "x86_64"): "x86_64-apple-darwin",
    ("macos", "aarch64"): "aarch64-apple-darwin",
}
# The oldest macOS the wheels of a macOS target must support: uv's own default for
# --python-platform *-apple-darwin in 0.12, pinned (MACOSX_DEPLOYMENT_TARGET, unless the user
# sets it) so a uv upgrade or a newer build machine cannot move it
MACOS_FLOOR = "13.0"
# uv's names of an architecture: platform.machine() spellings, sysconfig's on Windows, and a
# 32-bit interpreter on a 64-bit kernel. templates/pyz/__main__.py (_arch) mirrors host_arch
ARCH_NAMES = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64", "x86": "x86", "i386": "x86", "i686": "x86"}
WINDOWS_ARCH = {"win-amd64": "x86_64", "win-arm64": "aarch64", "win32": "x86"}
ARCH_32BIT = {"x86_64": "x86", "aarch64": "armv7l"}


def host_arch() -> str:
    """The architecture of the interpreter the build installs for (x86_64 | aarch64 | x86...):
    the runner's, which is uv's managed python.cpython like .venv's. Not the machine's CPU: on
    Windows platform.machine() asks WMI for the native CPU, so an x64 Python on Windows on ARM
    labelled its x64 wheels aarch64; a 32-bit Python on a 64-bit kernel likewise. The pyz
    bootstrap computes the same name for the interpreter that runs it."""
    if sys.platform == "win32" and sysconfig.get_platform() in WINDOWS_ARCH:
        return WINDOWS_ARCH[sysconfig.get_platform()]
    machine = platform.machine().lower()
    arch = ARCH_NAMES.get(machine, machine)
    return ARCH_32BIT.get(arch, arch) if sys.maxsize <= 2**32 else arch


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
    m = KEY_RE.fullmatch(key)
    if not m:
        raise PytError(
            f"invalid platform key: {key!r} (format: cp314-linux-x86_64, cp314-windows-x86_64...)"
        )
    impl, major, minor, os_name, arch = m.groups()
    return Target(impl, int(major), int(minor), os_name, arch)


def ensure_env(env: envs.PyEnv) -> envs.PyEnv:
    """Create the environment when its interpreter is missing (a fresh clone, `git clean -fdx`).

    The build methods run env.python directly (the interpreter query, `uv pip install
    --python`), not through `uv run --locked`, which would create it by itself.
    """
    if not env.python.is_file():
        ui.info(f"  {rel(env.dir)} does not exist yet: creating it")
        envs.sync(env)
    return env


def host_target(cfg: Config, backend: str) -> Target:
    info = envs.interpreter_info(ensure_env(envs.runtime_env(cfg, backend)).python)
    impl = "pp" if info["impl"] == "pypy" else "cp"
    major, minor, _ = str(info["version"]).split(".")
    return Target(impl, int(major), int(minor), host_os(), host_arch())


def config_host_key(cfg: Config, backend: str) -> str:
    """The key of the interpreter a build of `backend` installs for on this machine, from the
    config alone (the environments follow python.cpython and python.pypy)."""
    if backend == "pypy":
        m = re.search(r"@(\d+)\.(\d+)", cfg.python.pypy)
        impl, version = "pp", (f"{m[1]}{m[2]}" if m else "")
    else:
        impl, version = "cp", cfg.python.cpython.replace(".", "")
    return f"{impl}{version}-{host_os()}-{host_arch()}"


def check_key(cfg: Config, backend: str, key: str) -> Target:
    """Parse an extra target key and refuse the ones uv.lock cannot serve (before any work).

    uv.lock (the managed `environments`) only resolves CPython python.cpython and the pinned
    PyPy minor: another CPython minor used to get the build interpreter's binaries (same OS)
    or an incomplete lib/ (the exported markers exclude it), and uv installs PyPy wheels only
    with a real PyPy, so a PyPy key is only the pypy build's own interpreter on this machine.
    """
    t = parse_key(key)
    if t.impl == "cp" and t.version != cfg.python.cpython:
        locked = "cp" + cfg.python.cpython.replace(".", "")
        raise PytError(
            f"{key}: uv.lock only resolves CPython {cfg.python.cpython} (python.cpython): use "
            f"{locked}-{t.os}-{t.arch}, or change python.cpython and run ./pyt lock",
            2,
        )
    if t.impl == "pp" and t.key != config_host_key(cfg, backend):
        raise PytError(
            f"{key}: PyPy dependencies only come from a pypy build on that machine "
            "(./pyt build pypy --method pyz): uv cannot resolve PyPy wheels from CPython or for "
            "another OS. Join the parts with ./pyt pyz-merge",
            2,
        )
    return t


def targets_for(cfg: Config, backend: str, keys: list[str]) -> list[Target]:
    """Return the host target first, then the extra keys (validated, without duplicates)."""
    extra = [check_key(cfg, backend, k) for k in keys if k != "host"]
    host = host_target(cfg, backend)
    out = [host]
    for t in extra:
        if t.key not in {x.key for x in out}:
            out.append(t)
    return out


def export_requirements(cfg: Config) -> Path:
    """Export the runtime dependencies (no dev) with exact versions and hashes from uv.lock.

    --locked, never --frozen: a uv.lock older than pyproject.toml (a dependency added by hand, a
    merge) is refused like every `uv run --locked`; --frozen exported the old lock and the pyz or
    portable build shipped without the new dependency. --no-editable: a workspace or path
    dependency (`./pyt add ./libs/x`) is exported as a path and installed as a real package;
    editable, `uv pip install --target` left only a .pth naming this machine's source folder.
    """
    out = BUILD / "deploy" / "requirements.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    envs.uv(
        envs.tool_env(cfg),
        ["export", "--locked", "--no-dev", "--no-editable", "--no-emit-project", "--format", "requirements.txt", "--output-file", out, "--quiet"],
    )
    return out


def _version_tuple(text: str) -> tuple[int, int] | None:
    parts = text.split(".")
    try:
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None


def _macos_floor() -> str:
    return os.environ.get("MACOSX_DEPLOYMENT_TARGET") or MACOS_FLOOR


def host_floor(target: Target) -> str | None:
    """Return the --python-platform of a HOST target: the floor of a cross build, when this
    machine can load those wheels. None keeps the host's own tags (musl, a glibc or macOS older
    than the floor, an architecture without a uv platform, Windows)."""
    plat = UV_PLATFORMS.get((target.os, target.arch))
    if plat is None:
        return None
    if target.os == "linux":
        m = re.search(r"manylinux_(\d+)_(\d+)$", plat)
        libc, version = platform.libc_ver()  # the RUNNING glibc (os.confstr)
        have = _version_tuple(version) if libc == "glibc" else None
        return plat if m and have and have >= (int(m[1]), int(m[2])) else None
    if target.os == "macos":
        have = _version_tuple(platform.mac_ver()[0])
        floor = _version_tuple(_macos_floor())
        return plat if have and floor and have >= floor else None
    return None


def _runs_on(target: Target) -> str:
    if target.os == "linux":
        return f"glibc {platform.libc_ver()[1]}"
    return f"macOS {platform.mac_ver()[0]}"


def install_deps(cfg: Config, backend: str, target: Target, dest: Path, requirements: Path) -> Path:
    """Install the runtime deps for one target (host or cross) with `uv pip install --target`.

    Cross targets get binary wheels for UV_PLATFORMS (an sdist built here would produce host
    binaries), except the packages that publish no wheel at all (source_only): those are built
    here, and a native result is refused. The host target gets the same platform floor when this
    machine can load those wheels (host_floor): without it uv picks the newest the build machine
    allows, e.g. manylinux_2_34 on Ubuntu 24.04, and the result silently needed that glibc.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if not requirements.read_text(encoding="utf-8").strip():
        return dest
    own = "pp" if backend == "pypy" else "cp"
    env = envs.runtime_env(cfg, backend) if target.impl == own else envs.tool_env(cfg)
    extra_env = {"MACOSX_DEPLOYMENT_TARGET": _macos_floor()} if target.os == "macos" else {}
    base: list[str | Path] = ["pip", "install", "--quiet", "--target", dest, "--no-deps", "-r", requirements]
    if not target.is_host:
        # Wheels only: an sdist built here for another OS gives this machine's binaries. Except
        # the packages that publish no wheel at all (docopt, a workspace library): built here,
        # and kept only when the result is pure Python
        build_here = source_only(LOCK)
        argv = [
            *base,
            "--python", ensure_env(envs.tool_env(cfg)).python,
            "--python-platform", UV_PLATFORMS[(target.os, target.arch)],
            "--python-version", target.version,
            "--only-binary", ":all:",
            *(arg for name in build_here for arg in ("--no-binary", name)),
        ]
        envs.uv(env, argv, extra_env=extra_env)
        native = _built_native(dest, build_here)
        if native:
            raise PytError(
                f"{target.key}: {', '.join(native)} publishes no wheel, and building it here gives this machine's "
                f"binaries: build the pyz for {target.key} on that platform (./pyt build ... --method pyz there) "
                "and join the parts with ./pyt pyz-merge",
                2,
            )
    else:
        python = ensure_env(env).python
        base += ["--python", python]
        floor = host_floor(target)
        # The interpreter's full version: uv reads 3.14 as 3.14.0, and a requirement marked
        # python_full_version >= '3.14.1' was left out of the build for this very interpreter
        version = str(envs.interpreter_info(python)["version"])
        try:
            # no --only-binary: the host can still build an sdist
            envs.uv(env, [*base, "--python-platform", floor, "--python-version", version] if floor else base, extra_env=extra_env)
        except proc.CommandFailed:
            if not floor:
                raise
            ui.warn(
                f"{target.key}: a dependency has no wheel for {floor} (see above); using the wheels this "
                f"machine prefers, so the build needs {_runs_on(target)} or newer where it runs"
            )
            shutil.rmtree(dest)
            dest.mkdir(parents=True)
            envs.uv(env, base, extra_env=extra_env)
    drop_install_junk(dest)
    return dest


def source_only(lock: Path) -> list[str]:
    """The packages of uv.lock without any wheel: an sdist-only release on the index, or a path,
    git or URL source (a workspace library). The project's own entry is left out."""
    try:
        data = tomllib.loads(lock.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    names: set[str] = set()
    for package in data.get("package", []):
        if not isinstance(package, dict) or package.get("wheels") or not isinstance(package.get("name"), str):
            continue
        source = package.get("source")
        if isinstance(source, dict) and "." in (source.get("virtual"), source.get("editable"), source.get("directory")):
            continue  # the project itself
        names.add(package["name"])
    return sorted(names)


def _lock_packages(lock: Path) -> list[dict[str, Any]]:
    try:
        data = tomllib.loads(lock.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    packages = data.get("package", [])
    return [p for p in packages if isinstance(p, dict) and isinstance(p.get("name"), str)] if isinstance(packages, list) else []


def binary_only(lock: Path) -> set[str]:
    """The normalized names of the index packages of uv.lock that publish no pure wheel
    (`-none-any`): native packages (msgpack, numpy) and sdist-only releases."""
    out: set[str] = set()
    for package in _lock_packages(lock):
        source = package.get("source")
        if not isinstance(source, dict) or "registry" not in source:
            continue  # the project, a path, git or URL source: never a name==version pin
        wheels = package.get("wheels")
        files = [str(w.get("url") or w.get("path") or w.get("filename") or "") for w in wheels if isinstance(w, dict)] if isinstance(wheels, list) else []
        if not any(f.rsplit("/", 1)[-1].endswith("-none-any.whl") for f in files):
            out.add(_norm_name(package["name"]))
    return out


def project_specifiers(lock: Path) -> dict[str, str]:
    """The version bounds the project itself declares for its runtime dependencies (its
    requires-dist in uv.lock), by normalized name; a name declared twice with other bounds (per
    marker) is left out."""
    out: dict[str, str] = {}
    seen: set[str] = set()
    for package in _lock_packages(lock):
        source = package.get("source")
        if not isinstance(source, dict) or "." not in (source.get("virtual"), source.get("editable")):
            continue
        metadata = package.get("metadata")
        requires = metadata.get("requires-dist") if isinstance(metadata, dict) else None
        for entry in requires if isinstance(requires, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            name, specifier = _norm_name(entry["name"]), entry.get("specifier")
            text = specifier if isinstance(specifier, str) else ""
            if name in seen and out.get(name) != text:
                out.pop(name, None)
                continue
            if name not in seen:
                out[name] = text
            seen.add(name)
    return out


def unpin_binaries(pins: list[str], *, lock: Path | None = None) -> tuple[list[str], list[str]]:
    """For `flet build` of a mobile or web target: the `name==version` pins of the packages
    uv.lock has no pure wheel for (`binary_only`) lose uv.lock's version and keep the project's own
    bounds (`project_specifiers`) and their markers. flet build installs those targets' binaries
    from Flet's own index (pypi.flet.dev, `--only-binary :all:`), which holds other releases than
    PyPI (msgpack 1.1.x, where a new flet project locks 1.2.2): the exact pin had no solution, and
    pip now picks a release that fits every package's bounds. Returns the requirements and the
    pins it relaxed."""
    lock_file = LOCK if lock is None else lock
    binary = binary_only(lock_file)
    own = project_specifiers(lock_file) if binary else {}
    out: list[str] = []
    relaxed: list[str] = []
    for pin in pins:
        requirement, marked, marker = pin.partition(" ;")
        m = _PIN_RE.match(requirement.strip())
        name = _norm_name(m[1]) if m else ""
        loose = f"{m[1]}{own.get(name, '')}" if m else ""
        if not m or name not in binary or loose == requirement.strip():
            out.append(pin)
            continue
        relaxed.append(f"{m[1]}=={m[2]}")
        out.append(loose + (f" ;{marker}" if marked else ""))
    return out, relaxed


def _built_native(site: Path, names: list[str]) -> list[str]:
    """Which of `names` got a platform-specific wheel in `site` (a native sdist built here)."""
    wanted = {_norm_name(n) for n in names}
    out = []
    for wheel in sorted(site.glob("*.dist-info/WHEEL")):
        name = wheel.parent.name[: -len(".dist-info")].rpartition("-")[0]
        if _norm_name(name) in wanted and _platform_wheel(wheel):
            out.append(name)
    return out


class _CaseSensitiveParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr  # script names keep their case


def _entry_points(dest: Path) -> set[str]:
    """The console and GUI script names the installed distributions declare."""
    names: set[str] = set()
    for ep in dest.glob("*.dist-info/entry_points.txt"):
        parser = _CaseSensitiveParser(delimiters=("=",), interpolation=None, strict=False)
        try:
            parser.read(ep, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            continue
        for section in ("console_scripts", "gui_scripts"):
            if parser.has_section(section):
                names.update(parser.options(section))
    return names


def drop_install_junk(dest: Path) -> None:
    """Remove what `uv pip install --target` leaves that no app needs: its .lock file, the venv
    hooks (_virtualenv*) and the console/GUI script wrappers in bin/ (Scripts/), whose shebang
    or .exe trampoline holds this machine's absolute .venv path (dead elsewhere, and it leaks the
    developer's folder). Other files in bin/ stay: wheels such as ruff or uv ship a native binary
    there and find it at <target>/bin; a real package named bin (with __init__.py) stays too.
    In each *.dist-info: uv's cache files, and a direct_url.json naming a folder of this machine
    (a local library, installed for real since --no-editable), taken out of RECORD too.
    """
    for junk in [*dest.glob("_virtualenv*"), dest / ".lock"]:
        if junk.is_file() or junk.is_symlink():
            junk.unlink()
    for info in dest.glob("*.dist-info"):
        _drop_build_records(info)
    names = _entry_points(dest)
    for scripts in (dest / "bin", dest / "Scripts"):
        if not scripts.is_dir() or (scripts / "__init__.py").exists():
            continue
        for f in scripts.iterdir():
            if f.is_file() and (f.name in names or (f.suffix.lower() == ".exe" and f.stem in names)):
                f.unlink()
        if not any(scripts.iterdir()):
            scripts.rmdir()


def _drop_build_records(info: Path) -> None:
    """Delete uv_cache.json, uv_build.json and a file: direct_url.json (PEP 610) of a dist-info,
    and their RECORD rows. A URL requirement keeps its direct_url.json: it names no machine."""
    junk = [info / "uv_cache.json", info / "uv_build.json"]
    direct = info / "direct_url.json"
    try:
        data = json.loads(direct.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        data = None
    if isinstance(data, dict) and str(data.get("url", "")).startswith("file:"):
        junk.append(direct)
    gone = {f"{info.name}/{p.name}" for p in junk if p.is_file()}
    if not gone:
        return
    for name in gone:
        (info.parent / name).unlink()
    record = info / "RECORD"
    try:
        rows = record.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return
    kept = [row for row in rows if row.split(",", 1)[0] not in gone]
    if len(kept) != len(rows):
        record.write_text("".join(kept), encoding="utf-8", newline="")


def _platform_wheel(wheel: Path) -> bool:
    """True when a *.dist-info/WHEEL declares an ABI or platform tag (cp314-cp314-..., py3-none-win_amd64)."""
    try:
        text = wheel.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "tag":
            parts = value.strip().split("-")
            if len(parts) == 3 and (parts[1] != "none" or parts[2] != "any"):
                return True
    return False


# The extension ABI in a file name (templates/pyz/__main__.py has the same ABI_RE and abi_tag,
# which read the running interpreter's EXT_SUFFIX with it): cpython-314[t], cp314[t] (Windows),
# pypy311-pp73. A target key does not tell PyPy 7.3 (pp73) from PyPy 8 (pp80), nor CPython 3.14
# from its free-threaded build (3.14t)
ABI_RE = re.compile(r"\.(cpython-(\d+t?)|cp(\d+t?)|pypy(\d+)-(pp\d+))[-.]")


def abi_tag(name: str) -> str:
    """cp314, cp314t or pypy311_pp73 (as wheel tags name them), "" when `name` carries none."""
    m = ABI_RE.search(name)
    if not m:
        return ""
    return f"cp{m.group(2) or m.group(3)}" if m.group(4) is None else f"pypy{m.group(4)}_{m.group(5)}"


def extension_abis(folder: Path) -> list[str]:
    """The ABIs the extension modules below `folder` were built for (abi3 and an untagged
    .so/.pyd name none: any interpreter of the platform loads them)."""
    tags = {abi_tag(p.name) for p in folder.rglob("*") if p.name.endswith(EXT_SUFFIXES) and p.is_file()}
    return sorted(tags - {""})


def has_native(path: Path) -> bool:
    """True when a lib/ is platform-specific: a platform wheel (read from its WHEEL tags, which
    also catches pure-Python wheels that ship an executable, such as imageio-ffmpeg) or a binary."""
    if any(_platform_wheel(w) for w in path.glob("*.dist-info/WHEEL")):
        return True
    return any(p.suffix in NATIVE_SUFFIXES or ".so." in p.name for p in path.rglob("*") if p.is_file())


_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")


def _norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _norm_version(version: str) -> str:
    """PEP 440 spellings of one release compare equal (1.02 == 1.2; wheel names escape - as _)."""
    parts = version.strip().lower().replace("_", "-").split(".")
    return ".".join(str(int(p)) if p.isdigit() else p for p in parts)


def installed(site: Path) -> frozenset[tuple[str, str]]:
    """The (name, version) of every distribution installed in a --target folder."""
    out: set[tuple[str, str]] = set()
    for info in site.glob("*.dist-info"):
        name, _, version = info.name[: -len(".dist-info")].rpartition("-")
        out.add((_norm_name(name), _norm_version(version)))
    return frozenset(out)


_DIRECT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*@")


def _local_key(base: Path, raw: str) -> str:
    """One spelling of a local source path, relative to `base` or absolute (a file: URL too)."""
    if raw.startswith("file:"):  # a path outside the project: uv exports it as a URL
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        raw = url2pathname(urlparse(raw).path)
    return os.path.normcase(os.path.normpath(os.path.join(base, raw)))


def _local_names(lock: Path) -> dict[str, str]:
    """The packages of uv.lock that come from a local folder or file (a workspace library, a
    path dependency), by their path: `uv export --no-editable` writes them as a bare path."""
    try:
        data = tomllib.loads(lock.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    out: dict[str, str] = {}
    for package in data.get("package", []):
        source = package.get("source") if isinstance(package, dict) else None
        if not isinstance(source, dict) or not isinstance(package.get("name"), str):
            continue
        for kind in ("editable", "directory", "path"):
            if isinstance(source.get(kind), str):
                out[_local_key(lock.parent, source[kind])] = package["name"]
    return out


def direct_reference(line: str, *, lock: Path | None = None) -> str:
    """A line of `uv export --no-editable` as a PEP 508 requirement: a local library, which it
    writes as a bare path relative to the project (`./libs/x ; <markers>`) or a `file:` URL, becomes
    `name @ file:///absolute/path ; <markers>` (its name from uv.lock). Pins and direct references
    stay as they are. A path uv.lock does not name is a PytError."""
    lock_file = LOCK if lock is None else lock
    requirement, marked, marker = line.partition(" ;")  # PEP 508: a URL needs a blank before ;
    requirement = requirement.strip()
    if not requirement or _PIN_RE.match(requirement) or _DIRECT_RE.match(requirement):
        return line
    name = _local_names(lock_file).get(_local_key(lock_file.parent, requirement))
    if name is None:
        raise PytError(f"cannot name the local requirement {requirement!r}: uv.lock has no package from there (./pyt lock)")
    url = requirement if requirement.startswith("file:") else (lock_file.parent / requirement).resolve().as_uri()
    return f"{name} @ {url}" + (f" ;{marker}" if marked else "")


def skipped_requirements(requirements: Path, site: Path, *, lock: Path | None = None) -> list[str]:
    """Return the locked requirements that were NOT installed into `site`: their markers
    (sys_platform, python_version, implementation_name...) exclude that target's platform or
    interpreter.

    Pins are compared by name and version. A direct reference (`name @ url`) and a local library
    (a bare path: `uv export --no-editable`, named through uv.lock) only matter with a marker
    (without one they are installed everywhere) and are compared by name; a path uv.lock does
    not name counts as skipped, so the build goes per target instead of claiming to be pure.
    """
    have = installed(site)
    names = {name for name, _ in have}
    lock_file = LOCK if lock is None else lock
    local: dict[str, str] | None = None
    out: list[str] = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if not line or line[0].isspace() or line.startswith(("#", "-")):
            continue  # hashes and comments (continuation lines), options
        m = _PIN_RE.match(line)
        if m:
            if (_norm_name(m[1]), _norm_version(m[2])) not in have:
                out.append(f"{m[1]}=={m[2]}")
            continue
        requirement, marked, _ = line.rstrip("\\").partition(" ;")  # PEP 508: a URL needs a blank before ;
        requirement = requirement.strip()
        if not marked:
            continue
        direct = _DIRECT_RE.match(requirement)
        if direct:
            if _norm_name(direct[1]) not in names:
                out.append(direct[1])
            continue
        if local is None:
            local = _local_names(lock_file)
        name = local.get(_local_key(lock_file.parent, requirement))
        if name is None:
            out.append(requirement)
        elif _norm_name(name) not in names:
            out.append(f"{name} ({requirement})")
    return out


def requirements_digest(requirements: Path) -> str:
    """A fingerprint of the locked dependency set: the requirement lines only (the export's
    header holds this machine's --output-file path, and hashes/comments are continuation lines)."""
    lines = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if line and not line[0].isspace() and not line.startswith(("#", "-")):
            lines.append(line.rstrip("\\").strip())
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()[:16]


def _move(src: Path, dst: Path) -> None:
    os.replace(src, dst)


def remove_output(path: Path, *also: Path) -> None:
    """Remove a previous build output in dist/ (files or folders of one folder: portable's folder
    and its archives) whole, or not at all.

    Each is first moved aside into one scratch folder next to them: Windows refuses that while a
    file is in use (the app still running from the folder, a console in it, an open archive), and
    rmtree used to delete half of the folder before it failed with a traceback. When one cannot
    move, the ones already moved come back, so nothing is deleted. What the moved copies still
    hold (a scanner, an immutable file) is only a warning: the new output has its place.
    """
    present = [p for p in (path, *also) if p.exists() or p.is_symlink()]
    if not present:
        return
    hint = "\n  Is the app still running? Close it (or the window that uses the folder) and build again"
    aside = Path(tempfile.mkdtemp(prefix=f".{present[0].name}.old-", dir=present[0].parent))
    moved: list[Path] = []
    for p in present:
        try:
            _move(p, aside / p.name)
        except OSError as e:
            for back in reversed(moved):
                try:
                    _move(aside / back.name, back)
                except OSError as err:
                    ui.warn(f"could not put {rel(back)} back ({_why(err)}): it is in {rel(aside)}")
            if not any(aside.iterdir()):
                aside.rmdir()
            what = "a file in it is in use" if p.is_dir() and not p.is_symlink() else "it is in use or read-only"
            raise PytError(f"cannot replace {rel(p)}: {what} ({_why(e)}){hint}", 1) from None
        moved.append(p)
    shutil.rmtree(aside, ignore_errors=True)
    if aside.exists():  # read-only files an older build copied from src/ (Windows deletes none)
        from ..mypyc import make_writable

        with contextlib.suppress(OSError):
            make_writable(aside)
        shutil.rmtree(aside, ignore_errors=True)
    if aside.exists():
        ui.warn(f"could not delete all of the previous output, moved to {rel(aside)}: delete it by hand")


def _why(e: OSError) -> str:
    """The OS error and, when it names one, the file (the one a running app keeps open)."""
    return f"{e.strerror or e}: {e.filename}" if e.filename else str(e.strerror or e)


def copy_app(app_dir: Path, dest: Path, *, extensions: bool) -> None:
    """Copy the payload. extensions=False keeps only the .py files (pure fallback)."""
    from ..mypyc import copy_writable, remove_tree  # read-only files of src/: see copy_writable

    if dest.exists():
        remove_tree(dest)

    def ignore(directory: str, names: list[str]) -> set[str]:
        skip = {n for n in names if n in {"__pycache__", ".mypy_cache"}}
        if not extensions:
            skip |= {n for n in names if n.endswith(EXT_SUFFIXES)}
        return skip

    shutil.copytree(app_dir, dest, ignore=ignore, copy_function=copy_writable)


def uses_tkinter(*extra: Path) -> bool:
    """Return True when a .py file in src/ or under `extra` imports tkinter or turtle.

    The portable prune passes the installed lib/: a dependency such as customtkinter or
    ttkbootstrap needs tkinter even when the app never imports it itself. The walk follows
    symlinked folders (mypyc.walk, like the payload's sync_tree): Path.rglob skips them.
    """
    import ast

    from ..mypyc import walk

    for root in (SRC, *extra):
        for path in walk(root):
            if path.suffix != ".py" or not path.is_file():
                continue
            try:
                data = path.read_bytes()
                if b"tkinter" not in data and b"turtle" not in data:
                    continue  # fast path: lib/ can hold thousands of files
                tree = parse(path)
            except OSError:
                continue
            except PARSE_ERRORS:
                return True  # mentions tkinter but this Python cannot parse it: keep Tk (safe side)
            for node in iter_runtime_nodes(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                if any(n.split(".")[0] in {"tkinter", "turtle"} for n in names):
                    return True
    return False


def windowed(cmd: str) -> str:
    """Return the no-console twin of a Windows interpreter command, for app.gui launchers.

    pyw.exe, pythonw.exe and pypyw.exe ship next to py.exe, python.exe and pypy.exe; there is
    no python3w.exe or pypy3w.exe (checked in the PyPy Windows zip).
    """
    if cmd == "py" or cmd.startswith("py "):
        return "pyw" + cmd[2:]
    return "pypyw" if cmd.startswith("pypy") else "pythonw"


def tree_bytes(path: Path) -> int:
    """Bytes of a file or folder; a symlink (runtime/bin/python3 -> python3.14) is not counted again."""
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if not p.is_symlink() and p.is_file())


def dir_size_mb(path: Path) -> float:
    return tree_bytes(path) / 1_048_576
