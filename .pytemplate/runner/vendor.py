"""`./pyt vendor`: the files uv.lock installs, kept in vendor/<platform>/ (CLAUDE.md 5.10).

uv.lock stays the project's lock on its index (PyPI): every file it names comes with its URL and
its sha256. `./pyt vendor` downloads exactly those files, for every platform of `[vendor]
platforms` (linux-x86_64, windows-x86_64...: from any machine, the files are in uv.lock), into
vendor/<platform>/, checks each against uv.lock's hash and size, and removes the files the lock no
longer names. The environments then install from there (prepare: `uv pip install --no-index
--find-links vendor/<platform> --require-hashes --offline`, before every `uv run` and `uv sync`
of the project), so uv finds every package installed and downloads nothing: setup, sync, run,
test and check need no network, and neither PyPI nor a package deleted from it can stop them.

Why not uv's own `no-index = true` with `find-links = ["vendor/..."]` in pyproject.toml (measured
with uv 0.11.32 and 0.12.19, CLAUDE.md 15.1): uv then reads uv.lock as stale; locked again, every
package names the folder WITHOUT a hash, the lock holds only the vendored platform's wheels (the
others cannot sync), and a universal re-lock fails on a package of another platform the folder
lacks (colorama for win32). Kept on the index, uv.lock keeps its hashes, every platform, and the
way a new dependency is found: `./pyt add`, `lock` and `apply` resolve against the index, then
vendor the new files at once (follow_lock).

Which files: `uv export` names every package and the marker that installs it (every dependency
group, as envs.sync installs them); evaluate() reads that marker for each runtime (CPython of
python.cpython, PyPy of python.pypy while supported) on each platform, and of the wheels of
uv.lock that runtime loads there, one per oldest machine they serve is kept (choose_wheels: a
manylinux_2_34 build next to a manylinux_2_17 one only while uv ranks it better; the pure
fallback only while some machine of the platform loads no native one), else the sdist (built
where it installs, with its build backend from vendor/ when uv.lock holds it).
uv itself then checks each folder (verify: an offline dry-run install for each platform).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import os
import re
import tarfile
import tempfile
import threading
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import config, envs, proc, ui
from .config import TOML_ERRORS, VENDOR_PLATFORMS, Config
from .envs import _holds
from .project import BUILD, PYPROJECT, ROOT, host_os, rel, write_whole
from .ui import PytError

VENDOR = ROOT / "vendor"
LOCK = ROOT / "uv.lock"
STAMP = ".pytemplate-vendor"  # in an environment: the uv.lock it was installed from vendor/ for
# What a file of vendor/ may be (anything else there is left alone, with a warning)
DISTRIBUTIONS = (".whl", ".tar.gz", ".zip", ".tar.bz2", ".tgz")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"
DOWNLOADS = 6  # files downloaded at once
# The uv platform (--python-platform) verify checks each folder for: the newest of each family, so
# that every wheel vendored for some machine of the platform counts (a manylinux_2_34 one too)
VERIFY_PLATFORMS = {
    "linux-x86_64": "x86_64-manylinux_2_40",
    "linux-aarch64": "aarch64-manylinux_2_40",
    "macos-x86_64": "x86_64-apple-darwin",
    "macos-aarch64": "aarch64-apple-darwin",
    "windows-x86_64": "x86_64-pc-windows-msvc",
    "windows-aarch64": "aarch64-pc-windows-msvc",
}
VERIFY_MACOS = "26.0"  # MACOSX_DEPLOYMENT_TARGET of that check: every macOS wheel counts
# The ABI of PyPy 7.3 (pypy@3.11.15): pypy311_pp73. A PyPy 8 pin (pp80) needs this changed
PYPY_ABI = "pp73"


# --- platforms -------------------------------------------------------------------------------------


def host_platform() -> str | None:
    """This machine's platform as [vendor] platforms names it (the interpreter's architecture,
    as the builds read it: an x64 Python on Windows on ARM installs x64 wheels), or None where
    vendor/ cannot serve it (why_not_host says why)."""
    from .methods import common  # common imports envs, which imports this module lazily

    if host_os() == "linux" and common.this_libc() != "glibc":
        return None  # musl (Alpine), Android: the manylinux wheels do not load there
    name = f"{host_os()}-{common.host_arch()}"
    return name if name in VENDOR_PLATFORMS else None


def why_not_host() -> str:
    from .methods import common

    if host_os() == "linux" and common.this_libc() != "glibc":
        return "vendor/ holds manylinux wheels, which need glibc (this Python's C library is not glibc)"
    return f"vendor/ has no platform for {host_os()}-{common.host_arch()} (one of {', '.join(VENDOR_PLATFORMS)})"


def folder(platform: str) -> Path:
    return VENDOR / platform


# --- PEP 508 markers -------------------------------------------------------------------------------

# A marker variable's value on a platform: a string, a (major, minor) pair (a version known to its
# minor only: CPython's patch release is uv's choice), or None (not known: platform_release...)
Value = str | tuple[int, int] | None
_TOKEN = re.compile(r"""\s*(?:('[^']*'|"[^"]*")|(===|==|!=|<=|>=|~=|<|>|\(|\))|([A-Za-z_][A-Za-z0-9_.]*))""")
_VERSION_VARS = frozenset({"python_version", "python_full_version", "implementation_version", "platform_release"})
_VERSION_OPS = frozenset({"==", "!=", "<=", ">=", "<", ">", "~="})
_RELEASE = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_LAST_PATCH = 1 << 20


class MarkerError(ValueError):
    pass


def _tokens(marker: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(marker):
        if marker[pos:].strip() == "":
            break
        m = _TOKEN.match(marker, pos)
        if not m or m.end() == pos:
            raise MarkerError(f"cannot read the marker at {marker[pos:]!r}")
        pos = m.end()
        if m[1] is not None:
            out.append(("str", m[1][1:-1]))
        elif m[2] is not None:
            out.append(("op", m[2]))
        else:
            out.append(("word", m[3]))
    return out


def _and(values: Iterable[bool | None]) -> bool | None:
    seen = list(values)
    return False if False in seen else (None if None in seen else True)


def _or(values: Iterable[bool | None]) -> bool | None:
    seen = list(values)
    return True if True in seen else (None if None in seen else False)


def _release(text: str) -> tuple[tuple[int, ...], bool] | None:
    """A version literal as a release tuple and whether it ends in `.*`; None for anything else
    (a pre-release, a local version...), which is then read as "not known"."""
    star = text.endswith(".*")
    body = text[:-2] if star else text
    if not _RELEASE.fullmatch(body):
        return None
    try:
        return tuple(int(n) for n in body.split(".")), star
    except ValueError:  # over sys.get_int_max_str_digits() digits
        return None


def _version_op(op: str, spec_text: str, value: str | tuple[int, int]) -> bool | None:
    """`value op spec`, PEP 440 on final releases (PEP 508 compares versions as packaging's
    Specifier(op + spec).contains(value)). A value known to its minor only holds when it holds for
    every patch release, fails when it fails for every one, and is not known otherwise."""
    spec = _release(spec_text)
    if spec is None or (spec[1] and op not in ("==", "!=")) or (op == "~=" and len(spec[0]) < 2):
        return None
    number, star = spec
    if isinstance(value, tuple):
        patches = {0, _LAST_PATCH}
        if number[:2] == value and len(number) > 2:
            patches |= {max(number[2] - 1, 0), number[2], number[2] + 1}
        results = {_holds(op, number, star, (*value, p)) for p in patches}
        return results.pop() if len(results) == 1 else None
    parsed = _release(value)
    if parsed is None or parsed[1]:
        return None
    return _holds(op, number, star, parsed[0])


def _compare(left: tuple[str, str], op: str, right: tuple[str, str], env: Mapping[str, Value]) -> bool | None:
    def value(token: tuple[str, str]) -> Value:
        kind, text = token
        if kind == "str":
            return text
        if text not in env:
            raise MarkerError(f"unknown marker variable {text!r}")
        return env[text]

    lhs, rhs = value(left), value(right)
    if lhs is None or rhs is None:
        return None
    versions = any(kind == "word" and text in _VERSION_VARS for kind, text in (left, right))
    if op in ("in", "not in"):
        if isinstance(lhs, tuple) or isinstance(rhs, tuple):
            return None
        return (lhs in rhs) == (op == "in")
    if op == "===":
        return None if isinstance(lhs, tuple) or isinstance(rhs, tuple) else lhs == rhs
    if versions:
        if isinstance(rhs, tuple):  # the spec is the variable: not known to its patch
            return None
        return _version_op(op, rhs, lhs)
    if isinstance(lhs, tuple) or isinstance(rhs, tuple):
        return None
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    # An ordering of two strings: packaging 22 to 25 compares them as Python does, 26 answers
    # False for < and > and equality for <= and >=; uv has its own rules. No real marker does it
    return None


def evaluate(marker: str, env: Mapping[str, Value]) -> bool | None:
    """A PEP 508 marker on a platform: True, False, or None when it depends on what `env` does
    not know (None values, a version known to its minor only). Raises MarkerError for what it
    cannot read."""
    tokens = _tokens(marker)
    pos = 0

    def peek() -> tuple[str, str] | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> tuple[str, str]:
        nonlocal pos
        if pos >= len(tokens):
            raise MarkerError(f"the marker ends too early: {marker!r}")
        pos += 1
        return tokens[pos - 1]

    def expression() -> bool | None:
        values = [conjunction()]
        while peek() == ("word", "or"):
            take()
            values.append(conjunction())
        return _or(values)

    def conjunction() -> bool | None:
        values = [atom()]
        while peek() == ("word", "and"):
            take()
            values.append(atom())
        return _and(values)

    def atom() -> bool | None:
        token = take()
        if token == ("op", "("):
            result = expression()
            if take() != ("op", ")"):
                raise MarkerError(f"unbalanced parentheses: {marker!r}")
            return result
        op_token = take()
        if op_token == ("word", "not"):
            if take() != ("word", "in"):
                raise MarkerError(f"'not' without 'in': {marker!r}")
            op = "not in"
        elif op_token == ("word", "in") or op_token[0] == "op":
            op = op_token[1]
        else:
            raise MarkerError(f"no comparison at {op_token[1]!r}: {marker!r}")
        if op in ("(", ")"):
            raise MarkerError(f"no comparison at {op!r}: {marker!r}")
        right = take()
        if right[0] not in ("str", "word"):
            raise MarkerError(f"no value after {op!r}: {marker!r}")
        if token[0] not in ("str", "word"):
            raise MarkerError(f"no value before {op!r}: {marker!r}")
        return _compare(token, op, right, env)

    result = expression()
    if pos != len(tokens):
        raise MarkerError(f"cannot read the marker at {tokens[pos][1]!r}: {marker!r}")
    return result


@dataclass(frozen=True)
class Runtime:
    impl: str  # cpython | pypy
    major: int
    minor: int
    full: tuple[int, int, int] | None = None  # the exact Python version, when the pin names it (PyPy)

    @property
    def label(self) -> str:
        return f"{'CPython' if self.impl == 'cpython' else 'PyPy'} {self.major}.{self.minor}"


def runtimes(cfg: Config) -> list[Runtime]:
    """The interpreters of the project's environments: CPython of python.cpython (every backend's
    tools run there), PyPy of python.pypy while it is supported."""
    major, minor = (int(n) for n in cfg.python.cpython.split("."))
    out = [Runtime("cpython", major, minor)]
    m = re.fullmatch(r"pypy@([0-9]+)\.([0-9]+)\.([0-9]+)", cfg.python.pypy)
    if cfg.pypy_enabled and m:
        out.append(Runtime("pypy", int(m[1]), int(m[2]), (int(m[1]), int(m[2]), int(m[3]))))
    return out


_SYSTEM = {"linux": ("linux", "Linux", "posix"), "macos": ("darwin", "Darwin", "posix"), "windows": ("win32", "Windows", "nt")}
_MACHINE = {
    "linux-x86_64": "x86_64",
    "linux-aarch64": "aarch64",
    "macos-x86_64": "x86_64",
    "macos-aarch64": "arm64",
    "windows-x86_64": "AMD64",
    "windows-aarch64": "ARM64",
}


def marker_env(runtime: Runtime, platform: str) -> dict[str, Value]:
    """The marker variables of `runtime` on `platform`, as that machine's Python reports them.
    CPython's version is known to its minor only (uv picks the patch release); what depends on the
    machine itself (platform_release, platform_version) and the extras of a conflict are not known."""
    sys_platform, system, os_name = _SYSTEM[platform.partition("-")[0]]
    minor = (runtime.major, runtime.minor)
    full: Value = ".".join(map(str, runtime.full)) if runtime.full else minor
    cpython = runtime.impl == "cpython"
    env: dict[str, Value] = {
        "os_name": os_name,
        "sys_platform": sys_platform,
        "platform_system": system,
        "platform_machine": _MACHINE[platform],
        "platform_python_implementation": "CPython" if cpython else "PyPy",
        "implementation_name": runtime.impl,
        "python_version": f"{runtime.major}.{runtime.minor}",
        "python_full_version": full,
        "implementation_version": full if cpython else None,
        "platform_release": None,
        "platform_version": None,
        "extra": None,
    }
    for name in list(env):  # the legacy spellings PEP 508 still reads
        env[name.replace("_", ".", 1)] = env[name]
    return env


# --- wheel tags ------------------------------------------------------------------------------------

_PLATFORM_TAGS = {
    "linux-x86_64": re.compile(r"(manylinux1|manylinux2010|manylinux2014|manylinux_[0-9]+_[0-9]+|linux)_x86_64"),
    "linux-aarch64": re.compile(r"(manylinux2014|manylinux_[0-9]+_[0-9]+|linux)_aarch64"),
    "macos-x86_64": re.compile(r"macosx_[0-9]+_[0-9]+_(x86_64|intel|universal|universal2|fat3|fat32|fat64)"),
    "macos-aarch64": re.compile(r"macosx_[0-9]+_[0-9]+_(arm64|universal2)"),
    "windows-x86_64": re.compile(r"win_amd64"),
    "windows-aarch64": re.compile(r"win_arm64"),
}


def parse_wheel(filename: str) -> tuple[str, str, frozenset[tuple[str, str, str]]] | None:
    """A wheel's (normalized name, version, tags) from its file name, PEP 427 with compressed tag
    sets (`py2.py3`, `manylinux2014_x86_64.manylinux_2_17_x86_64`); None for another name."""
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) not in (5, 6):
        return None
    py, abi, plat = parts[-3:]
    tags = frozenset((p, a, t) for p in py.split(".") for a in abi.split(".") for t in plat.split("."))
    return norm_name(parts[0]), parts[1], tags


def _interpreter_rank(py: str, abi: str, runtime: Runtime) -> int | None:
    """How well a wheel's interpreter and ABI tags fit `runtime` (0 best, as uv prefers them), or
    None when it cannot load them: packaging's cpython_tags and compatible_tags, for one minor."""
    x, y = runtime.major, runtime.minor
    if runtime.impl == "cpython":
        if py == f"cp{x}{y}" and abi == f"cp{x}{y}":
            return 0
        if abi == "abi3":
            m = re.fullmatch(rf"cp{x}([0-9]+)", py)
            if m and int(m[1]) <= y:
                return 1 + (y - int(m[1]))
            return None
        if py == f"cp{x}{y}" and abi == "none":
            return 40
    else:
        if py == f"pp{x}{y}" and abi == f"pypy{x}{y}_{PYPY_ABI}":
            return 0
        if py == f"pp{x}{y}" and abi == "none":
            return 40
    if abi != "none":
        return None
    if py == f"py{x}{y}":
        return 41
    if py == f"py{x}":
        return 42
    m = re.fullmatch(rf"py{x}([0-9]+)", py)
    if m and int(m[1]) < y:
        return 43 + (y - int(m[1]))
    return None


def wheel_rank(tags: Iterable[tuple[str, str, str]], runtime: Runtime, platform: str) -> int | None:
    """The best rank of a wheel's tags for `runtime` on `platform`, or None when none loads there."""
    best: int | None = None
    for py, abi, plat in tags:
        if plat != "any" and not _PLATFORM_TAGS[platform].fullmatch(plat):
            continue
        rank = _interpreter_rank(py, abi, runtime)
        if rank is not None and (best is None or rank < best):
            best = rank
    return best


# The oldest machine of each platform a vendored wheel must serve: what the interpreters uv
# installs need (python-build-standalone: glibc 2.17, macOS 10.13 on x86_64, 11.0 on arm64). A
# wheel for an older one serves exactly the same machines as one for this floor
PLATFORM_FLOORS: dict[str, tuple[int, ...]] = {
    "linux-x86_64": (2, 17),
    "linux-aarch64": (2, 17),
    "macos-x86_64": (10, 13),
    "macos-aarch64": (11, 0),
    "windows-x86_64": (),
    "windows-aarch64": (),
}
_LEGACY_MANYLINUX = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}


def _tag_floor(plat: str) -> tuple[int, ...]:
    """The oldest glibc or macOS a platform tag loads on ((): any, Windows, a bare linux_ tag)."""
    m = re.match(r"(manylinux1|manylinux2010|manylinux2014)_", plat)
    if m:
        return _LEGACY_MANYLINUX[m[1]]
    m = re.match(r"(?:manylinux|macosx)_([0-9]+)_([0-9]+)_", plat)
    return (int(m[1]), int(m[2])) if m else ()


def choose_wheels(wheels: Iterable[LockFile], runtime: Runtime, platform: str) -> list[LockFile]:
    """The wheels of one package version that `runtime` on `platform` may install, and no more:
    for each oldest machine they serve (a manylinux_2_17 and a manylinux_2_34 build, below
    PLATFORM_FLOORS one bucket) the one uv prefers there (its tags' rank; the smaller file of a
    tie: a thin arm64 wheel before a universal2 one), a build for newer machines only while it
    ranks better than those for older ones, and the pure fallback only while some machine of the
    platform loads none of the native ones."""
    floor = PLATFORM_FLOORS[platform]
    natives: dict[tuple[int, ...], tuple[int, int, LockFile]] = {}  # oldest machine -> (rank, size, wheel)
    pure: tuple[int, int, LockFile] | None = None
    for wheel in wheels:
        parsed = parse_wheel(wheel.filename)
        if parsed is None:
            continue
        rank = wheel_rank(parsed[2], runtime, platform)
        if rank is None:
            continue
        loads = [t for _, _, t in parsed[2] if t == "any" or _PLATFORM_TAGS[platform].fullmatch(t)]
        candidate = (rank, wheel.size or 0, wheel)
        if "any" in loads:
            if pure is None or candidate[:2] < pure[:2]:
                pure = candidate
            continue
        oldest = max(min(_tag_floor(t) for t in loads), floor)
        if oldest not in natives or candidate[:2] < natives[oldest][:2]:
            natives[oldest] = candidate
    kept: list[LockFile] = []
    best_rank: int | None = None
    for oldest in sorted(natives):  # older machines first: a newer-only build must rank better
        rank, _, wheel = natives[oldest]
        if best_rank is None or rank < best_rank:
            kept.append(wheel)
            best_rank = rank
    if pure is not None and (not natives or min(natives) > floor):
        kept.append(pure[2])
    return kept


# --- uv.lock and the export ------------------------------------------------------------------------


def norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class LockFile:
    """A file uv.lock names: a wheel or an sdist, with its URL, sha256 and size."""

    name: str
    version: str
    url: str
    sha256: str
    size: int | None
    kind: str  # wheel | sdist

    @property
    def filename(self) -> str:
        path = urllib.parse.urlsplit(self.url).path
        return urllib.parse.unquote(path.rsplit("/", 1)[-1])


_PLAIN_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+!-]*")  # a distribution's file name: no separator, no leading dot


@dataclass
class LockPackage:
    name: str
    version: str
    wheels: list[LockFile]
    sdist: LockFile | None


def _lock_file(name: str, version: str, entry: object, kind: str, registry: str) -> LockFile | None:
    """A file of uv.lock: `url =` (an index on the network), or `path =` (an index or find-links
    folder on this machine: relative to that folder, which is relative to the project). None without
    a sha256 (uv records none for a find-links folder's file): nothing could check it."""
    if not isinstance(entry, dict):
        return None
    url, path, digest, size = entry.get("url"), entry.get("path"), entry.get("hash"), entry.get("size")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    if not isinstance(url, str):
        if not isinstance(path, str) or "://" in registry:
            return None
        url = (ROOT / registry / path).resolve().as_uri()
    found = LockFile(name, version, url, digest.partition(":")[2].lower(), size if isinstance(size, int) else None, kind)
    if not _PLAIN_NAME.fullmatch(found.filename) or found.filename in (".", ".."):
        # The file's name becomes a path in vendor/: one a URL encodes as `..%2F..` would land
        # outside it, and a dot name would hide among the folder's own files
        raise PytError(f"uv.lock names {url}, whose file name {found.filename!r} is no plain file name: nothing is vendored")
    return found


def read_lock(lock: Path | None = None) -> dict[tuple[str, str], LockPackage]:
    """The packages of uv.lock that come from an index, by (normalized name, version)."""
    lock = lock or LOCK
    try:
        data = tomllib.loads(lock.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise PytError("uv.lock not found: ./pyt lock") from None
    except OSError as e:
        raise PytError(f"cannot read uv.lock: {e.strerror or e}") from None
    except (UnicodeDecodeError, *TOML_ERRORS) as e:
        raise PytError(f"uv.lock is not valid TOML: {e}") from None
    out: dict[tuple[str, str], LockPackage] = {}
    for package in data.get("package", []):
        if not isinstance(package, dict):
            continue
        name, version, source = package.get("name"), package.get("version"), package.get("source")
        if not isinstance(name, str) or not isinstance(version, str) or not isinstance(source, dict) or "registry" not in source:
            continue
        key, registry = norm_name(name), str(source["registry"])
        wheels = [f for w in package.get("wheels", []) if (f := _lock_file(key, version, w, "wheel", registry))]
        out[(key, version)] = LockPackage(key, version, wheels, _lock_file(key, version, package.get("sdist"), "sdist", registry))
    return out


@dataclass(frozen=True)
class Entry:
    """A requirement of the export: a pinned package of an index, or another source (`kind`)."""

    name: str
    version: str
    marker: str
    kind: str  # pin | local | git | url
    text: str  # the requirement as the export wrote it (with its hashes, when exported with them)


_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(?:;\s*(.*?))?\s*")
_DIRECT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*@\s*(\S+)\s*(?:;\s*(.*?))?\s*")


def parse_export(text: str) -> list[Entry]:
    """The requirements of a `uv export --format requirements.txt`: one entry per requirement,
    its continuation lines (the hashes) joined."""
    chunks: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace() and chunks:
            chunks[-1] += "\n" + line
        else:
            chunks.append(line)
    entries: list[Entry] = []
    for chunk in chunks:
        first = chunk.split("\n", 1)[0].rstrip("\\").strip()
        if m := _PIN.fullmatch(first):
            entries.append(Entry(norm_name(m[1]), m[2], m[3] or "", "pin", chunk))
        elif m := _DIRECT.fullmatch(first):
            kind = "git" if m[2].startswith("git+") else ("local" if m[2].startswith("file:") else "url")
            entries.append(Entry(norm_name(m[1]), "", m[3] or "", kind, chunk))
        else:  # -e ./libs/x, ./libs/x: a local library (a workspace member)
            entries.append(Entry(first.removeprefix("-e").strip(), "", "", "local", chunk))
    return entries


def _no_groups(env: envs.PyEnv) -> list[str]:
    return [a for group, _ in envs.left_out(env) for a in ("--no-group", group)]


def export(cfg: Config, env: envs.PyEnv, *, hashes: bool, frozen: bool = False) -> str:
    """`uv export` of what envs.sync installs into `env`: every dependency group but those uv
    cannot install there (envs.left_out), the project itself left out. `frozen`: uv.lock as it is,
    for a `uv run --frozen` (the hook's ruff), which never asks whether it is stale."""
    argv = ["export", "--frozen" if frozen else "--locked", "--format", "requirements.txt", "--no-emit-project", "--no-header", "--no-annotate", "--all-groups", *_no_groups(env)]
    if not hashes:
        argv.append("--no-hashes")
    r = envs.uv(envs.tool_env(cfg), argv, capture=True, check=False, echo=False)
    if r.returncode != 0:
        raise PytError(f"uv export failed: {envs.uv_error(r.stderr or r.stdout) or f'exit code {r.returncode}'}\n  ./pyt lock brings uv.lock in line first")
    return r.stdout


def _no_build(project: Path | None = None) -> tuple[Callable[[str], bool], Callable[[str], bool]]:
    """Whether a package may NOT be built from its sdist, and whether it may not be installed from
    a wheel (pyproject.toml's [tool.uv] no-build, no-build-package, no-binary, no-binary-package)."""
    try:
        data = tomllib.loads((project or PYPROJECT).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, *TOML_ERRORS):
        data = {}
    tool = data.get("tool")
    table = tool.get("uv") if isinstance(tool, dict) else None
    table = table if isinstance(table, dict) else {}

    def names(key: str) -> set[str]:
        value = table.get(key)
        return {norm_name(v) for v in value if isinstance(v, str)} if isinstance(value, list) else set()

    no_build, no_binary = names("no-build-package"), names("no-binary-package")
    all_build, all_binary = table.get("no-build") is True, table.get("no-binary") is True
    return (lambda n: all_build or n in no_build), (lambda n: all_binary or n in no_binary)


# --- the plan --------------------------------------------------------------------------------------


@dataclass
class PlatformPlan:
    platform: str
    files: dict[str, LockFile] = field(default_factory=dict)  # file name -> the file
    notes: list[str] = field(default_factory=list)  # what this platform installs some other way

    @property
    def size(self) -> int:
        return sum(f.size or 0 for f in self.files.values())


@dataclass
class Plan:
    platforms: dict[str, PlatformPlan]
    notes: list[str]  # requirements no platform takes from vendor/: git, URL and local sources


def make_plan(cfg: Config, platforms: Iterable[str] | None = None, *, lock: Path | None = None, frozen: bool = False) -> Plan:
    """What vendor/<platform>/ must hold for each platform (default: [vendor] platforms).
    `frozen`: uv.lock as it is, even when pyproject.toml moved past it (export)."""
    packages = read_lock(lock)
    no_build, no_binary = _no_build()
    kinds = runtimes(cfg)
    exported: dict[str, list[Entry]] = {}
    for runtime in kinds:
        env = envs.cpython_env(cfg) if runtime.impl == "cpython" else envs.pypy_env(cfg)
        groups = " ".join(_no_groups(env))
        if groups not in exported:
            exported[groups] = parse_export(export(cfg, env, hashes=False, frozen=frozen))
    per_runtime = {runtime: exported[" ".join(_no_groups(envs.cpython_env(cfg) if runtime.impl == "cpython" else envs.pypy_env(cfg)))] for runtime in kinds}
    plan = Plan({}, [])
    others: dict[str, str] = {}
    for entries in per_runtime.values():
        for entry in entries:
            if entry.kind != "pin":
                why = {"git": "comes from git", "url": "comes from a URL", "local": "is a local library"}[entry.kind]
                others[entry.name] = f"{entry.name} {why}: never vendored, it installs from its source"
    plan.notes = sorted(others.values())
    wanted = list(cfg.vendor.platforms if platforms is None else platforms)
    for platform in wanted:
        pp = PlatformPlan(platform)
        for runtime, entries in per_runtime.items():
            markers = marker_env(runtime, platform)
            for entry in entries:
                if entry.kind != "pin":
                    continue
                try:
                    needed = evaluate(entry.marker, markers) if entry.marker else True
                except MarkerError as e:
                    needed = None  # kept: a marker this reader cannot read may hold there
                    pp.notes.append(f"{entry.name}: {e}")
                if needed is False:
                    continue
                package = packages.get((entry.name, entry.version))
                if package is None:
                    pp.notes.append(f"{entry.name}=={entry.version} is in the export but not in uv.lock: ./pyt lock")
                    continue
                chosen = [] if no_binary(entry.name) else choose_wheels(package.wheels, runtime, platform)
                if chosen:
                    for wheel in chosen:
                        pp.files[wheel.filename] = wheel
                elif package.sdist is not None and not no_build(entry.name):
                    pp.files[package.sdist.filename] = package.sdist
                    pp.notes.append(
                        f"{entry.name} {entry.version} has no wheel for {runtime.label} on {platform}: its sdist is "
                        "vendored, and installing it builds it (its build backend must be in vendor/ too, or come from the network)"
                    )
                else:
                    pp.notes.append(
                        f"{entry.name} {entry.version} has no file {runtime.label} can install on {platform}"
                        f"{' (no wheel, and [tool.uv] forbids building it)' if package.sdist else ''}: it cannot be vendored there"
                    )
        pp.notes = list(dict.fromkeys(pp.notes))
        plan.platforms[platform] = pp
    return plan


# --- vendor/<platform>/ on disk --------------------------------------------------------------------


@dataclass
class FolderState:
    missing: list[LockFile] = field(default_factory=list)  # planned, absent or of another size
    pointers: list[str] = field(default_factory=list)  # Git LFS pointers in place of a planned file
    damaged: list[str] = field(default_factory=list)  # planned, of the right size, another sha256 (full check)
    extra: list[Path] = field(default_factory=list)  # distribution files uv.lock no longer names
    foreign: list[Path] = field(default_factory=list)  # other files: left alone

    @property
    def in_line(self) -> bool:
        return not (self.missing or self.damaged or self.extra)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _is_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(len(LFS_POINTER)) == LFS_POINTER
    except OSError:
        return False


def state(pp: PlatformPlan, *, full: bool = False) -> FolderState:
    """How vendor/<platform>/ compares with its plan: by name and size, and with `full` by sha256."""
    out = FolderState()
    where = folder(pp.platform)
    present: dict[str, Path] = {}
    try:
        for entry in os.scandir(where):
            if entry.is_file(follow_symlinks=False):
                present[entry.name] = Path(entry.path)
    except FileNotFoundError:
        pass
    except OSError as e:
        raise PytError(f"cannot list {rel(where)}: {e.strerror or e}") from None
    for name, wanted in sorted(pp.files.items()):
        path = present.get(name)
        if path is None:
            out.missing.append(wanted)
            continue
        size = path.stat().st_size
        if wanted.size is not None and size != wanted.size:
            if _is_pointer(path):
                out.pointers.append(name)
            out.missing.append(wanted)
        elif full and _sha256(path) != wanted.sha256:
            out.damaged.append(name)
    for name, path in sorted(present.items()):
        if name in pp.files:
            continue
        if name.endswith(DISTRIBUTIONS) or (name.startswith(".") and name.endswith(".part")):
            out.extra.append(path)
        elif not name.startswith("."):
            out.foreign.append(path)
    return out


_stop = threading.Event()  # set when a run stops (a failure, Ctrl+C): the other downloads end at once


class _Stopped(Exception):
    pass


class _Damaged(Exception):
    """A download that is not the file uv.lock names."""

    def __init__(self, why: str, *, cut_short: bool = False) -> None:
        super().__init__(why)
        self.cut_short = cut_short


def _download(f: LockFile, dest: Path) -> None:
    """Download one file into `dest` (a temporary name beside it, then os.replace): only the very
    bytes uv.lock names (its sha256 and size) ever take the file's name."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, partial = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".part")
    try:
        digest, written = hashlib.sha256(), 0
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(f.url, timeout=60) as r:  # noqa: S310 (uv.lock's URL)
            while chunk := r.read(1 << 20):
                if _stop.is_set():
                    raise _Stopped
                out.write(chunk)
                digest.update(chunk)
                written += len(chunk)
        if f.size is not None and written != f.size:
            raise _Damaged(f"the download ended after {written} of {f.size} bytes", cut_short=written < f.size)
        if digest.hexdigest() != f.sha256:
            raise _Damaged(f"its sha256 is {digest.hexdigest()}, uv.lock names {f.sha256}")
        os.replace(partial, dest)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(partial)


def fetch(f: LockFile, dest: Path) -> None:
    """_download with the retries of the runner's other downloads: a failure that may not happen
    again (common.transient: a 5xx answer, a dropped connection, a body cut short) is tried again
    after each pause of common.DOWNLOAD_RETRIES; a file that is not the one uv.lock names never is."""
    from .methods import common

    pauses = common.DOWNLOAD_RETRIES
    for attempt in range(len(pauses) + 1):
        last = attempt == len(pauses)
        try:
            _download(f, dest)
            return
        except _Stopped:
            raise
        except _Damaged as e:
            if not e.cut_short:
                raise PytError(f"{f.url} is not the file uv.lock names ({e}): not vendored. Was the file replaced on its index?", 1) from None
            why = str(e)
            if last:
                raise PytError(f"cannot download {f.url}: {why}", 3) from None
        except (OSError, ValueError, http.client.HTTPException) as e:
            why = str(e) or type(e).__name__
            if last or not common.transient(e):
                raise PytError(f"cannot download {f.url}: {why}", 3) from None
        ui.warn(f"{f.filename}: the download failed ({why}): trying again in {pauses[attempt]:g} s")
        if _stop.wait(pauses[attempt]):  # the run stopped meanwhile
            raise _Stopped


def _fetch_all(jobs: list[tuple[LockFile, Path]]) -> None:
    """Download every job, DOWNLOADS at a time. The first failure stops the others from starting;
    the files downloaded before it stay (each is whole and checked) for the caller to keep or drop."""
    if not jobs:
        return
    errors: list[PytError] = []
    _stop.clear()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(DOWNLOADS, len(jobs)))
    try:
        futures = [pool.submit(fetch, f, dest) for f, dest in jobs]
        for done in concurrent.futures.as_completed(futures):
            e = done.exception()
            if isinstance(e, PytError):
                errors.append(e)
                _stop.set()
            elif e is not None and not isinstance(e, _Stopped):
                raise e
    except BaseException:
        _stop.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)  # each running download ends at its next chunk
    if errors:
        raise PytError("\n".join(str(e) for e in errors), max(e.code for e in errors))


def _mb(size: int) -> str:
    return f"{size / 1e6:.1f} MB"


def follow_lock(cfg: Config, platforms: Iterable[str] | None = None, *, full: bool = False, plan: Plan | None = None) -> Plan:
    """Bring vendor/<platform>/ in line with uv.lock for each platform (default: [vendor]
    platforms): download what is missing, then remove what the lock no longer names. A failed
    download removes what this run downloaded, so the folder stays as the previous lock left it.
    `full`: compare every file's sha256 too (./pyt vendor; the others compare names and sizes)."""
    plan = plan or make_plan(cfg, platforms)
    jobs: list[tuple[LockFile, Path]] = []
    states: dict[str, FolderState] = {}
    for platform, pp in plan.platforms.items():
        st = states[platform] = state(pp, full=full)
        for name in st.pointers:
            ui.warn(f"vendor/{platform}/{name} is a Git LFS pointer, not the file: git lfs pull (after git lfs install) gets it; downloading it now")
        for name in st.damaged:
            ui.warn(f"vendor/{platform}/{name} is not the file uv.lock names (another sha256): downloading it again")
        for f in [*st.missing, *(pp.files[n] for n in st.damaged)]:
            jobs.append((f, folder(platform) / f.filename))
    if proc.DRY_RUN:
        for platform, st in states.items():
            fetches = [j for j in jobs if j[1].parent == folder(platform)]
            if fetches or st.extra:
                ui.report(f"(--dry-run) vendor/{platform}: would download {len(fetches)} file(s) ({_mb(sum(f.size or 0 for f, _ in fetches))}), would remove {len(st.extra)}")
        return plan
    if jobs:
        total = sum(f.size or 0 for f, _ in jobs)
        ui.info(f"vendor: downloading {len(jobs)} file(s) ({_mb(total)}) that uv.lock names")
        try:
            _fetch_all(jobs)
        except BaseException:  # whatever this run added goes: the folder stays as it was
            _drop_new(jobs, states)
            raise
    for platform, st in states.items():
        for path in st.extra:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        for path in st.foreign:
            ui.warn(f"{rel(path)} is no file uv.lock names, and vendor/ holds only those: left alone")
        fetched = [f for f, dest in jobs if dest.parent.name == platform]
        if fetched or st.extra:
            ui.info(
                f"vendor/{platform}: {len(fetched)} file(s) downloaded ({_mb(sum(f.size or 0 for f in fetched))}), "
                f"{len(st.extra)} removed; {len(plan.platforms[platform].files)} in all ({_mb(plan.platforms[platform].size)})"
            )
    return plan


def _drop_new(jobs: list[tuple[LockFile, Path]], states: Mapping[str, FolderState]) -> None:
    """Remove the files a failed follow_lock downloaded that were not there before (a damaged
    file it replaced is gone either way)."""
    for f, dest in jobs:
        before = states.get(dest.parent.name)
        was_missing = before is not None and any(m.filename == f.filename for m in before.missing)
        if was_missing:
            with contextlib.suppress(OSError):
                if dest.is_file() and dest.stat().st_size == (f.size or -1):
                    dest.unlink()


def build_requires(sdist: Path) -> list[str] | None:
    """The package names of an sdist's [build-system] requires (pyproject.toml at its top; without
    one, setuptools: pip's and uv's legacy default), or None when the archive cannot be read."""
    try:
        with tarfile.open(sdist) as archive:
            names = archive.getnames()
            top = {n.split("/", 1)[0] for n in names}
            member = next((f"{t}/pyproject.toml" for t in sorted(top) if f"{t}/pyproject.toml" in names), None)
            stream = archive.extractfile(member) if member else None
            data = stream.read() if stream else None
    except (OSError, tarfile.TarError):
        return None
    if data is None:
        return ["setuptools"]
    try:
        system = tomllib.loads(data.decode("utf-8-sig")).get("build-system", {})
    except (UnicodeDecodeError, *TOML_ERRORS):
        return None
    requires = system.get("requires", ["setuptools"]) if isinstance(system, dict) else []
    names_out = [m[0] for r in requires if isinstance(r, str) and (m := re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", r.strip()))]
    return [norm_name(n) for n in names_out]


def backend_notes(cfg: Config, plan: Plan) -> list[str]:
    """For each vendored sdist, the build backend uv.lock does not hold: building it where it
    installs needs that from the network (vendor/ holds only uv.lock's files)."""
    locked = {name for name, _ in read_lock()}
    notes: list[str] = []
    for platform, pp in plan.platforms.items():
        for name, f in sorted(pp.files.items()):
            if f.kind != "sdist" or not (folder(platform) / name).is_file():
                continue
            requires = build_requires(folder(platform) / name)
            if requires is None:
                notes.append(f"vendor/{platform}/{name}: its build requirements cannot be read")
                continue
            missing = [r for r in requires if r not in locked]
            if missing:
                notes.append(
                    f"{f.name} builds with {', '.join(missing)}, which uv.lock does not hold: installing it without the "
                    f"network fails ({'; '.join(f'./pyt add --group build {m}' for m in missing)} keeps it in uv.lock and vendor/)"
                )
    return list(dict.fromkeys(notes))


def remove_platform(platform: str) -> None:
    """Delete vendor/<platform>/'s distribution files, then the folder when it is empty, then
    vendor/ when it is empty."""
    where = folder(platform)
    if proc.DRY_RUN:
        if where.is_dir():
            ui.report(f"(--dry-run) would remove {rel(where)}")
        return
    with contextlib.suppress(FileNotFoundError):
        for entry in os.scandir(where):
            if entry.is_file(follow_symlinks=False) and (entry.name.endswith(DISTRIBUTIONS) or entry.name.endswith(".part")):
                os.unlink(entry.path)
    for path in (where, VENDOR):
        with contextlib.suppress(OSError):
            path.rmdir()
    if where.exists():
        ui.warn(f"{rel(where)} keeps files that are not uv.lock's: left alone")


# --- installing from vendor/ -----------------------------------------------------------------------


def _digest(lock: Path | None = None) -> str:
    try:
        return hashlib.sha256((lock or LOCK).read_bytes()).hexdigest()
    except OSError:
        return ""


def _stamp(env: envs.PyEnv, platform: str) -> str:
    return json.dumps({"lock": _digest(), "platform": platform, "request": env.request}, sort_keys=True)


_prepared: dict[Path, str] = {}  # environment -> the uv.lock digest it was prepared for in this run
_noted: set[str] = set()


def prepare(cfg: Config, env: envs.PyEnv, *, sync: bool = False, frozen: bool = False) -> None:
    """Install uv.lock's files into `env` from this machine's vendor/<platform>/ before a `uv run`
    or `uv sync` of the project (envs.uv), so uv finds them installed and downloads nothing. Only
    when uv.lock changed since `env` was last installed from vendor/ (its STAMP); a folder that
    does not match uv.lock (a pull that brought a new uv.lock without vendor/) is brought in line
    first, which downloads (follow_lock). On a platform vendor/ does not serve, `sync` (setup,
    sync, apply) says so once, and uv installs from the index as always. `frozen`: the uv call
    reads uv.lock as it is (`uv run --frozen`), and so does the install."""
    digest = _digest()
    if _prepared.get(env.dir) == digest:
        return
    _prepared[env.dir] = digest
    platform = host_platform()
    if platform is None or platform not in cfg.vendor.platforms:
        if sync and "host" not in _noted:
            _noted.add("host")
            why = why_not_host() if platform is None else f"[vendor] platforms has no {platform}: ./pyt vendor --platform {platform} adds it"
            ui.info(f"vendor: {rel(env.dir)} installs from the package index ({why})")
        return
    stamp = env.dir / STAMP
    try:
        if stamp.read_text(encoding="utf-8") == _stamp(env, platform) and env.python.is_file():
            return
    except OSError:
        pass
    if proc.DRY_RUN:
        ui.report(f"(--dry-run) would install uv.lock's packages into {rel(env.dir)} from vendor/{platform}")
        return
    plan = make_plan(cfg, [platform], frozen=frozen)
    if not state(plan.platforms[platform]).in_line:
        ui.warn(f"vendor/{platform} does not match uv.lock: bringing it in line (commit vendor/ with uv.lock)")
        follow_lock(cfg, [platform], plan=plan)
    install(cfg, env, platform, frozen=frozen)
    with contextlib.suppress(OSError):
        stamp.write_text(_stamp(env, platform), encoding="utf-8")


def pins(text: str) -> str:
    """The pinned packages of an export with hashes (every source vendor/ holds), one per entry."""
    return "".join(e.text + "\n" for e in parse_export(text) if e.kind == "pin")


def fits(env: envs.PyEnv) -> bool:
    """Whether `env` runs the interpreter it is made for (its request: python.cpython's minor,
    python.pypy's version). One made with another (python.cpython moved from 3.14 to 3.13) is
    made again, as uv sync makes it again: installed into, it got the other Python's wheels, or
    none."""
    if not env.python.is_file():
        return False
    try:
        info = envs.interpreter_info(env.python)
    except (OSError, ValueError, proc.CommandFailed):
        return False
    impl, _, version = env.request.rpartition("@")
    if (impl or "cpython") != info.get("impl"):
        return False
    have = str(info.get("version", ""))
    return have == version or have.startswith(version + ".")


def install(cfg: Config, env: envs.PyEnv, platform: str, *, frozen: bool = False) -> None:
    """`uv pip install` uv.lock's pinned packages into `env` from vendor/<platform>, without an
    index and offline, each file checked against uv.lock's hashes. The environment is made first
    when it does not exist or runs another interpreter (fits; uv sync makes it otherwise)."""
    where = folder(platform)
    if not env.python.is_file():
        envs.uv(env, ["venv", "--allow-existing", "--quiet", env.dir])
    elif not fits(env):
        ui.info(f"{rel(env.dir)} runs another Python than {env.request}: making it again")
        envs.uv(env, ["venv", "--clear", "--quiet", env.dir])
    requirements = BUILD / "vendor" / f"{env.key}.txt"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    write_whole(requirements, pins(export(cfg, env, hashes=True, frozen=frozen)).encode("utf-8"))
    argv: list[str | Path] = ["pip", "install", "--python", env.python, "--no-index", "--find-links", where, "--offline", "--require-hashes", "--no-deps", "-r", requirements]
    r = envs.uv(env, argv, check=False)
    if r.returncode != 0:
        raise PytError(
            f"{rel(env.dir)}: uv could not install uv.lock's packages from vendor/{platform} (see above).\n"
            "  ./pyt vendor brings vendor/ in line with uv.lock (it downloads what is missing)",
            r.returncode if r.returncode > 2 else 1,
        )


def build_source(cfg: Config, os_name: str, arch: str, requirements: Path) -> list[str | Path] | None:
    """The `uv pip install` arguments of a pyz or portable build (methods.common.install_deps) that
    take uv.lock's files from vendor/ for a target of a vendored platform, or None. Without an
    index and offline; a project with local libraries keeps the index (their build backend comes
    from there) and adds vendor/ as a place to find the others."""
    platform = f"{os_name}-{arch}"
    if platform not in cfg.vendor.platforms or not folder(platform).is_dir():
        return None
    text = requirements.read_text(encoding="utf-8")
    local = any(e.kind != "pin" for e in parse_export(text))
    index: list[str | Path] = [] if local else ["--no-index", "--offline"]
    return [*index, "--find-links", folder(platform), "-r", requirements]


# --- checks ----------------------------------------------------------------------------------------


def verify(cfg: Config, plan: Plan) -> list[str]:
    """Ask uv whether each folder installs uv.lock there: an offline dry-run install from it alone,
    for CPython on every platform (--python-platform) and PyPy on this machine (its interpreter:
    uv evaluates a PyPy marker only on a real PyPy). Returns uv's reasons, one per failure."""
    problems: list[str] = []
    cpython = envs.cpython_env(cfg)
    requirements = BUILD / "vendor" / "verify.txt"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    write_whole(requirements, pins(export(cfg, cpython, hashes=True)).encode("utf-8"))
    host = host_platform()
    for platform in plan.platforms:
        with tempfile.TemporaryDirectory(prefix="pt-vendor-") as target:
            argv: list[str | Path] = [
                "pip", "install", "--dry-run", "--offline", "--no-index", "--find-links", folder(platform), "--require-hashes", "--no-deps",
                "-r", requirements, "--target", target, "--python-platform", VERIFY_PLATFORMS[platform], "--python-version", cfg.python.cpython,
            ]  # fmt: skip
            extra = {"MACOSX_DEPLOYMENT_TARGET": VERIFY_MACOS} if platform.startswith("macos") else {}
            r = envs.uv(cpython, argv, capture=True, check=False, echo=False, extra_env=extra)
            if r.returncode != 0:
                problems.append(f"vendor/{platform} (CPython {cfg.python.cpython}): {envs.uv_error(r.stderr or r.stdout) or f'exit code {r.returncode}'}")
        if cfg.pypy_enabled and platform == host and envs.pypy_env(cfg).python.is_file():
            pypy = envs.pypy_env(cfg)
            pypy_requirements = BUILD / "vendor" / "verify-pypy.txt"
            write_whole(pypy_requirements, pins(export(cfg, pypy, hashes=True)).encode("utf-8"))
            argv = ["pip", "install", "--dry-run", "--offline", "--no-index", "--find-links", folder(platform), "--require-hashes", "--no-deps", "-r", pypy_requirements, "--python", pypy.python]
            r = envs.uv(pypy, argv, capture=True, check=False, echo=False)
            if r.returncode != 0:
                problems.append(f"vendor/{platform} (PyPy): {envs.uv_error(r.stderr or r.stdout) or f'exit code {r.returncode}'}")
    return problems


def describe(st: FolderState, platform: str) -> str:
    """What is wrong with a folder, for doctor and the hook."""
    parts = []
    if st.pointers:
        parts.append(f"{len(st.pointers)} Git LFS pointer(s) in place of the files (git lfs pull)")
    rest = len(st.missing) - len(st.pointers)
    if rest:
        parts.append(f"{rest} file(s) uv.lock names missing")
    if st.damaged:
        parts.append(f"{len(st.damaged)} file(s) with another sha256")
    if st.extra:
        parts.append(f"{len(st.extra)} file(s) uv.lock no longer names")
    return f"vendor/{platform}: " + ", ".join(parts)


def check_folders(cfg: Config) -> list[str]:
    """What keeps vendor/ from matching uv.lock (by name and size): one line per platform, empty
    when every folder matches or nothing is vendored."""
    if not cfg.vendor.platforms:
        return []
    plan = make_plan(cfg)
    return [describe(st, p) for p, pp in plan.platforms.items() if not (st := state(pp)).in_line]


def doctor(cfg: Config, check: Callable[..., None]) -> None:
    """doctor's vendor lines: each vendored platform against uv.lock, this machine's platform."""
    if not cfg.vendor.platforms:
        leftover = [p.name for p in sorted(VENDOR.iterdir()) if p.is_dir() and p.name in VENDOR_PLATFORMS] if VENDOR.is_dir() else []
        if leftover:
            check(None, f"vendor/ holds {', '.join(leftover)}, but [vendor] platforms is empty: nothing installs from it", "./pyt vendor --off removes it")
        return
    try:
        plan = make_plan(cfg)
    except PytError as e:
        check(False, "vendor/ not checked", str(e))
        return
    for platform, pp in plan.platforms.items():
        st = state(pp)
        if st.in_line:
            check(True, f"vendor/{platform}: the {len(pp.files)} files of uv.lock ({_mb(pp.size)})")
        else:
            check(False, describe(st, platform), "./pyt vendor   (downloads what is missing, removes the rest)")
    host = host_platform()
    if host is None or host not in cfg.vendor.platforms:
        why = why_not_host() if host is None else f"./pyt vendor --platform {host} adds it"
        check(None, f"this machine's environments install from the package index, not vendor/ ({why})")
    for note in plan.notes:
        check(None, f"vendor: {note}")


# --- ./pyt vendor ----------------------------------------------------------------------------------


def _parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="./pyt vendor", allow_abbrev=False)
    parser.add_argument("--platform", action="append", default=[], choices=VENDOR_PLATFORMS, help="vendor this platform too")
    parser.add_argument("--remove", action="append", default=[], choices=VENDOR_PLATFORMS, help="stop vendoring this platform")
    parser.add_argument("--off", action="store_true", help="stop vendoring: [vendor] platforms = [], vendor/ removed")
    parser.add_argument("--check", action="store_true", help="check every file against uv.lock (sha256), and ask uv whether each folder installs it")
    ns = parser.parse_args(args)
    if ns.check and (ns.platform or ns.remove or ns.off):
        raise PytError("vendor: --check changes nothing, and takes no other option")
    if ns.off and (ns.platform or ns.remove):
        raise PytError("vendor: --off removes every platform: it takes no --platform or --remove")
    return ns


def _check(cfg: Config) -> int:
    if not cfg.vendor.platforms:
        ui.report("vendor: nothing is vendored ([vendor] platforms is empty: ./pyt vendor starts with this machine's platform)")
        return 0
    plan = make_plan(cfg)
    problems = [describe(st, p) for p, pp in plan.platforms.items() if not (st := state(pp, full=True)).in_line]
    if not problems:
        problems = verify(cfg, plan)
    for platform, pp in plan.platforms.items():
        ui.report(f"vendor/{platform}: {len(pp.files)} files ({_mb(pp.size)})")
        for note in pp.notes:
            ui.report(f"  note: {note}")
    for note in plan.notes:
        ui.report(f"note: {note}")
    for problem in problems:
        ui.error(problem)
    if problems:
        ui.error("./pyt vendor brings vendor/ in line with uv.lock")
        return 1
    ui.ok("vendor/ holds every file uv.lock installs on each platform, each with uv.lock's sha256")
    return 0


def _lfs_tracked() -> bool:
    try:
        text = (ROOT / ".gitattributes").read_text(encoding="utf-8-sig")
    except OSError:
        return False
    return any(line.split()[:1] and line.split()[0].startswith("vendor") and "filter=lfs" in line for line in text.splitlines())


LFS_HINT_SIZE = 50_000_000  # past it, `./pyt vendor` names Git LFS (README: Vendoring)


def cmd_vendor(cfg: Config, args: list[str]) -> int:
    """vendor [--platform P]... [--remove P]... [--off] [--check]"""
    from . import render

    ns = _parse(args)
    if ns.check:
        return _check(cfg)
    before = list(cfg.vendor.platforms)
    unknown = [p for p in ns.remove if p not in before]
    if unknown:
        raise PytError(f"vendor: {', '.join(unknown)} is not vendored ([vendor] platforms: {', '.join(before) or 'none'})")
    if ns.off:
        after: list[str] = []
    else:
        after = [p for p in before if p not in ns.remove] + [p for p in dict.fromkeys(ns.platform) if p not in before]
        if not before and not ns.platform and not ns.remove:
            host = host_platform()
            if host is None:
                raise PytError(f"vendor: {why_not_host()}: name the platforms to vendor, e.g. ./pyt vendor --platform linux-x86_64", 3)
            after = [host]
    if after != before:
        config.update_file([("vendor", "platforms", after)])
        if proc.DRY_RUN:
            ui.report(f"(--dry-run) would set [vendor] platforms = {json.dumps(after)}")
        cfg.vendor.platforms = after
    for platform in [p for p in before if p not in after]:
        remove_platform(platform)
    if not after:
        render.apply(cfg)  # ci.yml checks out without Git LFS again
        if not proc.DRY_RUN:
            ui.ok("nothing is vendored now ([vendor] platforms = [])")
        return 0
    plan = follow_lock(cfg, full=True)
    for note in [*plan.notes, *([] if proc.DRY_RUN else backend_notes(cfg, plan))]:
        ui.info(f"note: {note}")
    for platform, pp in plan.platforms.items():
        for note in pp.notes:
            ui.info(f"note ({platform}): {note}")
    if proc.DRY_RUN:
        render.apply(cfg)  # says which generated files would update (ci.yml: Git LFS)
        return 0
    problems = verify(cfg, plan)
    render.apply(cfg)  # ci.yml checks out vendor/ with Git LFS
    for problem in problems:
        ui.error(problem)
    total = sum(pp.size for pp in plan.platforms.values())
    if problems:
        ui.error("uv cannot install uv.lock from these folders alone (above): the project needs the network there")
        return 1
    if total > LFS_HINT_SIZE and not _lfs_tracked():
        ui.info(f"vendor/ holds {_mb(total)}: Git LFS keeps it out of every clone's history (README: Vendoring, Git LFS)")
    # What the command did: -q keeps it (5.3), as it keeps what `nvim extras` did
    done = ui.report if ui.QUIET else ui.ok
    done(
        f"vendor/: {', '.join(f'{p} ({len(pp.files)} files, {_mb(pp.size)})' for p, pp in plan.platforms.items())}. "
        "Commit it with uv.lock: git add vendor"
    )
    return 0
