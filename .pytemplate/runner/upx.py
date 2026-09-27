"""UPX: optional compression of the executables and native libraries a build ships.

[deploy.upx] in pytemplate.toml (off by default):
  enabled   pack the binaries of the exe (PyInstaller / flet pack), nuitka, portable and flet
            methods. Smaller files and downloads; in exchange every start unpacks them in memory
            (slower start, no page sharing between processes) and some antivirus engines flag
            UPX-packed files.
  level     1..9 | best | brute | ultra-brute (brute levels: much slower builds, a few % smaller)
  lzma      LZMA instead of NRV (smaller, slower to unpack)
  exclude   file-name globs never packed, on top of BUILTIN_EXCLUDE
  path      an explicit upx executable, absolute or relative to the project root (default:
            `upx` on PATH, else the cached pinned download)

How each method uses it:
  - exe (PyInstaller, and flet pack, which runs PyInstaller): PyInstaller's own UPX step
    (--upx-dir): it packs every collected binary before bundling, skips Control Flow Guard
    DLLs and Qt plugins, and always adds --lzma. The level goes in the UPX environment
    variable, which upx reads as default options. Windows only: PyInstaller disables UPX on
    every other OS, so there the exe is not packed (exe.size_args warns).
  - nuitka: a standalone folder goes through pack_tree() when it is done, like portable and
    flet (Nuitka's upx plugin has no exclude option: it packed every DLL it copied); a onefile
    binary through Nuitka's upx plugin (always --best --lzma), which leaves the libraries inside
    its payload alone, unless the binary's own name is excluded.
  - portable and flet: pack_tree() on the finished folder.
Never used: pyz (the zip is already deflated) and wheel.

UPX refuses inputs over 768 MiB: files over MAX_INPUT (600 MiB) are skipped with a warning,
leaving a margin. --force is never passed: UPX refuses Control Flow Guard binaries (most
CPython and Flutter DLLs), and forcing them breaks them. macOS is not supported (UPX cannot
pack current macOS binaries and packing breaks code signing).
"""

from __future__ import annotations

import fnmatch
import hashlib
import http.client
import io
import os
import shlex
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import proc, ui
from .config import Config
from .project import IS_MACOS, IS_WINDOWS, ROOT, host_arch, rel
from .ui import DeployError

VERSION = "5.2.1"  # pinned: bump VERSION and the SHA-256 values together
URL = "https://github.com/upx/upx/releases/download/v{version}/{asset}"
# (os, arch) -> (release asset, sha256). Windows on arm64 runs the x64 build (emulated).
ASSETS = {
    ("windows", "x86_64"): ("upx-5.2.1-win64.zip", "eabc6792a347d45e945be7748423e7868fd01b0d2bcaa2f4b1031fd71ff69bda"),
    ("windows", "aarch64"): ("upx-5.2.1-win64.zip", "eabc6792a347d45e945be7748423e7868fd01b0d2bcaa2f4b1031fd71ff69bda"),
    ("linux", "x86_64"): ("upx-5.2.1-amd64_linux.tar.xz", "402162aad30af47e60dbd767fb2e64ca394ace9727ba1f40283641f1d1b91657"),
    ("linux", "aarch64"): ("upx-5.2.1-arm64_linux.tar.xz", "a72d112c5970a904a31da0b9c84f919bc16b9a311787c12245508544a78c7d36"),
}
MAX_INPUT = 600 * 1024 * 1024  # UPX's limit is 768 MiB
LEVELS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "best", "brute", "ultra-brute")
# Packing these breaks them or gains nothing: C runtime, API sets, the Python DLL, and the
# Flutter engine (a packed flutter_windows.dll hangs the app at startup: measured with Flet
# 1.0.1's `flet build windows`; every other Flutter plugin DLL packs and runs fine)
BUILTIN_EXCLUDE = (
    "vcruntime140*.dll",
    "msvcp140*.dll",
    "concrt140.dll",
    "ucrtbase.dll",
    "api-ms-win-*.dll",
    "python3*.dll",
    "libpython3*",
    "flutter_windows.dll",
)
PE_SUFFIXES = (".exe", ".dll", ".pyd")


def unsupported_reason() -> str:
    """Return why UPX cannot be used on this host ("" when it can)."""
    if IS_MACOS:
        return "UPX cannot pack current macOS binaries (and packing breaks code signing)"
    os_name = "windows" if IS_WINDOWS else "linux"
    if (os_name, host_arch()) not in ASSETS:
        return f"no UPX build for {os_name}-{host_arch()}"
    return ""


def active(cfg: Config) -> bool:
    """Return whether this build packs with UPX (enabled and supported; warns when unsupported)."""
    if not cfg.deploy.upx.enabled:
        return False
    reason = unsupported_reason()
    if reason:
        ui.warn(f"deploy.upx.enabled is ignored: {reason}")
        return False
    return True


def level_flags(cfg: Config) -> list[str]:
    level = cfg.deploy.upx.level
    flag = f"-{level}" if level.isdigit() else f"--{level}"
    return [flag, "--lzma"] if cfg.deploy.upx.lzma else [flag]


def env_value(cfg: Config) -> str:
    """The UPX environment variable for PyInstaller's own upx calls (it adds --lzma itself)."""
    return " ".join(f for f in level_flags(cfg) if f != "--lzma")


def excludes(cfg: Config) -> list[str]:
    return [*BUILTIN_EXCLUDE, *cfg.deploy.upx.exclude]


def excluded(cfg: Config, name: str) -> bool:
    """Whether a file of this name is never packed: BUILTIN_EXCLUDE and deploy.upx.exclude, as
    case-insensitive globs."""
    lower = name.lower()
    return any(fnmatch.fnmatch(lower, p.lower()) for p in excludes(cfg))


def _cache_dir() -> Path:
    """The folder of the pinned download: always absolute. A relative XDG_CACHE_HOME (or
    LOCALAPPDATA) is ignored, as the XDG spec says and the pyz bootstrap does: it put the download
    under the caller's folder (src/, which the payloads ship) and handed Nuitka, which runs in its
    stage, a relative --upx-binary."""
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        base = Path(local) if os.path.isabs(local) else Path.home() / "AppData" / "Local"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "")
        base = Path(xdg) if os.path.isabs(xdg) else Path.home() / ".cache"
    return base / "pytemplate" / "tools" / f"upx-{VERSION}"


def _exe_name() -> str:
    return "upx.exe" if IS_WINDOWS else "upx"


def _asset() -> tuple[str, str, str]:
    """This host's pinned release asset: (file name, sha256, URL)."""
    asset, sha256 = ASSETS[("windows" if IS_WINDOWS else "linux", host_arch())]
    return asset, sha256, URL.format(version=VERSION, asset=asset)


def _download(dest: Path) -> Path:
    asset, sha256, url = _asset()
    ui.info(f"upx: downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 (fixed https URL)
            data = r.read()
    except (OSError, http.client.HTTPException) as e:  # a connection closed halfway: IncompleteRead, no OSError
        raise DeployError(f"upx: cannot download {url}: {e}\n  Install it yourself (scoop/winget/apt) or set deploy.upx.path", 3) from None
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        raise DeployError(f"upx: {asset} has SHA-256 {digest}, expected {sha256}: not using it", 3)
    binary: bytes | None = None
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next((n for n in z.namelist() if n.endswith("/upx.exe")), None)
            binary = z.read(member) if member else None
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as t:
            info = next((m for m in t.getmembers() if m.name.endswith("/upx") and m.isfile()), None)
            extracted = t.extractfile(info) if info else None
            binary = extracted.read() if extracted else None
    if binary is None:
        raise DeployError(f"upx: {asset} has no {_exe_name()} binary", 3)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / _exe_name()
    partial = target.with_name(target.name + ".part")  # an interrupted write must never look cached
    partial.write_bytes(binary)
    if not IS_WINDOWS:
        partial.chmod(0o755)
    partial.replace(target)
    return target


def locate(cfg: Config) -> Path | None:
    """Return the upx executable find() would use without downloading one (None: it would).

    A relative deploy.upx.path starts at the project root, never the caller's cwd (the tools run
    with other working folders: flet pack in its stage, Nuitka in its own): it reaches them
    absolute, and not resolved, because PyInstaller wants <upx-dir>/upx and Nuitka a file named upx.
    """
    if cfg.deploy.upx.path:
        given = Path(cfg.deploy.upx.path).expanduser()
        path = given if given.is_absolute() else ROOT / given
        if not path.is_file():
            raise DeployError(
                f"deploy.upx.path = {cfg.deploy.upx.path!r} does not exist ({path}; a relative path starts at the project root)", 3
            )
        if os.path.normcase(path.name) != os.path.normcase(_exe_name()):  # tools/upx-5.2.1
            raise DeployError(
                f"deploy.upx.path = {cfg.deploy.upx.path!r} must be a file named {_exe_name()} ({path}): Nuitka takes no"
                f" other name (it searched PATH instead) and PyInstaller looks for <folder>/{_exe_name()}; rename it",
                2,
            )
        if not _runnable(path):  # a checkout from Windows or a zip lost its x bit: pack_file died later
            raise DeployError(f"deploy.upx.path = {cfg.deploy.upx.path!r} is not executable ({path}): chmod +x {shlex.quote(str(path))}", 3)
        return path
    on_path = shutil.which("upx", path=proc.base_env().get("PATH"))
    if on_path:
        return Path(on_path).absolute()  # a relative PATH entry: the tools run in other folders
    cached = _cache_dir() / _exe_name()
    return cached if cached.is_file() and _runnable(cached) else None  # a download that lost its x bit comes again


def _runnable(path: Path) -> bool:
    return IS_WINDOWS or os.access(path, os.X_OK)


def find(cfg: Config) -> Path:
    """Return the upx executable: deploy.upx.path, `upx` on PATH, the cache, or a fresh download."""
    return locate(cfg) or _download(_cache_dir())


def uses(cfg: Config, method: str) -> bool:
    """Whether a build with this method packs with UPX on this host (no warning: active() gives
    it during the build). exe (PyInstaller, flet pack) packs on Windows only; flet only desktop
    targets (mobile and web builds ship no binary of ours); pyz and wheel never."""
    if not cfg.deploy.upx.enabled or unsupported_reason():
        return False
    if method == "exe":
        return IS_WINDOWS
    if method == "flet":
        from .methods.flet import MOBILE_WEB  # lazily: methods.flet imports this module

        return cfg.deploy.flet.target not in MOBILE_WEB
    return method in ("nuitka", "portable")


def _always_packs(cfg: Config, method: str) -> bool:
    """Whether a build that uses UPX always holds a binary to pack: the exe, Nuitka's binary, a
    bundled interpreter, the flet desktop runner. A portable build with runtime = "system"
    bundles no interpreter: only a native dependency or a mypyc extension in app/ or lib/ gives
    upx something to do (a pure-Python app on Linux has nothing: .so files are never packed)."""
    return not (method == "portable" and cfg.deploy.portable.runtime == "system")


def preflight(cfg: Config, method: str) -> str:
    """Resolve the upx executable before a build that packs with it does any work, and return
    what it will use ("" when the build packs nothing).

    cmd_build calls this before the checks and the payload, also in --dry-run: a deploy.upx.path
    that does not exist, or a download that fails, stops the build now, not after the runtime
    copy or the whole `flet build`. A dry run names the download instead of doing it. A build
    that may hold nothing to pack (_always_packs) checks deploy.upx.path now but leaves the
    download to pack_tree, which asks for upx only when it has a candidate: a pure-Python
    runtime = "system" portable build must not need the network for a tool it never runs.
    """
    if not uses(cfg, method):
        return ""
    found = locate(cfg)  # a deploy.upx.path that does not exist fails here, whatever the build holds
    if found is None:
        url = f"{_asset()[2]} into {_cache_dir()}"
        if not _always_packs(cfg, method):
            return f"upx: would download {url} if the build holds a binary to pack" if proc.DRY_RUN else ""
        if proc.DRY_RUN:
            return f"upx: would download {url}"
        found = _download(_cache_dir())
    return f"upx: {found}"


@dataclass
class Result:
    path: Path
    before: int
    after: int
    status: str  # packed | skipped | failed
    reason: str = ""


def candidates(root: Path, cfg: Config) -> list[Path]:
    """Return the files under `root` UPX may pack (PE files on Windows, ELF executables on Linux)."""
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or excluded(cfg, path.name):
            continue
        name = path.name.lower()
        if IS_WINDOWS:
            if path.suffix.lower() in PE_SUFFIXES:
                out.append(path)
        elif os.access(path, os.X_OK) and not name.endswith(".so") and ".so." not in name:
            with path.open("rb") as f:
                if f.read(4) == b"\x7fELF":
                    out.append(path)
    return out


def _classify(output: str) -> str:
    text = output.lower()
    if "alreadypacked" in text:
        return "already packed"
    if "guard_cf" in text:  # "GUARD_CF enabled PE files are not supported (use --force...)"
        return "Control Flow Guard binary"
    if "notcompressible" in text:
        return "not compressible"
    if "cantpack" in text:
        return "UPX cannot pack it"
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    return lines[-1][:120] if lines else "upx failed"


def pack_file(upx: Path, path: Path, flags: list[str]) -> Result:
    before = path.stat().st_size
    if before > MAX_INPUT:
        return Result(path, before, before, "skipped", f"{before / 1_048_576:.0f} MiB is over the {MAX_INPUT // 1_048_576} MiB limit (UPX: 768 MiB)")
    try:
        r = subprocess.run(
            [str(upx), "-q", "--no-progress", "--compress-icons=0", *flags, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError as e:  # no x bit, another architecture, gone: the build stops with this, never a traceback
        raise DeployError(f"upx: cannot run {upx}: {e.strerror or e}", 3) from None
    if r.returncode == 0:
        return Result(path, before, path.stat().st_size, "packed")
    reason = _classify(r.stdout + r.stderr)
    status = "skipped" if reason in ("already packed", "Control Flow Guard binary", "not compressible", "UPX cannot pack it") else "failed"
    return Result(path, before, path.stat().st_size, status, reason)


def pack_tree(cfg: Config, root: Path) -> list[Result]:
    """Pack every candidate binary under `root` in parallel and print a summary."""
    files = candidates(root, cfg)
    if not files:
        return []
    flags = level_flags(cfg)
    ui.step(f"upx {' '.join(flags)}: {len(files)} binaries in {rel(root)}")
    if proc.DRY_RUN:  # never reached from ./deploy build (a dry run stops before any method builds)
        return []
    upx = find(cfg)  # resolved by preflight() before the build, except for a runtime = "system" portable build
    with ThreadPoolExecutor(max_workers=max(1, (os.cpu_count() or 2))) as pool:
        results = list(pool.map(lambda p: pack_file(upx, p, flags), files))
    packed = [r for r in results if r.status == "packed"]
    before = sum(r.before for r in packed)
    after = sum(r.after for r in packed)
    for r in results:
        if r.status != "packed":
            ui.detail(f"  {r.status}: {rel(r.path)} ({r.reason})")
        if r.status == "failed":
            ui.warn(f"upx failed on {rel(r.path)}: {r.reason} (left uncompressed)")
    if packed:
        ui.ok(
            f"upx: packed {len(packed)} of {len(files)} binaries, "
            f"{before / 1_048_576:.1f} MB -> {after / 1_048_576:.1f} MB"
            + (f" ({len(results) - len(packed)} skipped: see -v)" if len(results) > len(packed) else "")
        )
    else:
        ui.info(f"upx: nothing to pack in {rel(root)} ({len(results)} skipped: see -v)")
    return results
