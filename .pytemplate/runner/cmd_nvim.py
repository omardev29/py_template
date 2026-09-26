"""nvim [doctor|trust|extras|bootstrap|sync]: Neovim/LazyVim integration helper.

The project side needs no install step: `.lazy.lua` (a static copy of
.pytemplate/templates/nvim/lazy.lua) loads the local plugin .pytemplate/nvim/ when Neovim
starts inside the project and the file is trusted. This command checks that setup and does
the one-time steps for the user (trust, LazyVim extras, starter config, plugin sync).

Neovim is asked for its own directories (one headless `--clean` call), so NVIM_APPNAME and
XDG_* are respected and nothing is hard-coded. Only `trust`, `extras`, `bootstrap` and `sync`
write anything, and only when the user runs them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import envs, proc, ui
from .config import Config
from .project import IS_MACOS, IS_WINDOWS, ROOT
from .ui import DeployError

Check = Callable[[bool | None, str, str], None]

MIN_LAZYVIM = (0, 11, 2)  # LazyVim 15+
MIN_PATH_TRUST = (0, 12, 0)  # vim.secure.trust({path=...}); older versions need a buffer
STARTER = "https://github.com/LazyVim/starter"
# The starter commit `selftest --nvim` tests against (with nvimtest.LOCK); `nvim bootstrap`
# takes the newest, as LazyVim's own install steps do.
STARTER_REV = "803bc181d7c0d6d5eeba9274d9be49b287294d99"
EXTRAS = (
    "lazyvim.plugins.extras.lang.python",
    "lazyvim.plugins.extras.lang.toml",
    "lazyvim.plugins.extras.dap.core",
    "lazyvim.plugins.extras.test.core",
    "lazyvim.plugins.extras.editor.overseer",
)
EXTRA_PREFIX = "lazyvim.plugins.extras."
LAZY_LUA = ROOT / ".lazy.lua"
MARK = "PTNVIM"  # prefix of the JSON line the headless snippets print

# `-c` snippets: one line each, no double quotes (they go through the Windows command line).
# QUERY_LUA must work on ANY Neovim, so an old one is reported as too old (doctor) or skipped
# (selftest --nvim): vim.version() is a plain table before 0.10 (tostring gives "table: 0x..."),
# and stdpath('state') is an error before 0.8 (the data dir held the shada then).
QUERY_LUA = (
    "lua local v = vim.version(); local has_state, state = pcall(vim.fn.stdpath, 'state'); "
    "io.stdout:write('" + MARK + "' .. vim.json.encode({"
    "config = vim.fn.stdpath('config'), data = vim.fn.stdpath('data'), "
    "state = has_state and state or vim.fn.stdpath('data'), cache = vim.fn.stdpath('cache'), "
    "version = v.major .. '.' .. v.minor .. '.' .. v.patch, progpath = vim.v.progpath"
    "}) .. '\\n')"
)
# The file comes in $PT_TRUST_FILE (no quoting problems). The trust DB is written with
# io.open(state .. '/trust', 'w'), which fails if the state directory does not exist yet.
TRUST_LUA = (
    "lua local p = vim.env.PT_TRUST_FILE; vim.fn.mkdir(vim.fn.stdpath('state'), 'p'); local ok, msg; "
    "if vim.fn.has('nvim-0.12') == 1 then ok, msg = vim.secure.trust({ action = 'allow', path = p }) "
    "else local b = vim.fn.bufadd(p); vim.fn.bufload(b); ok, msg = vim.secure.trust({ action = 'allow', bufnr = b }) end; "
    "io.stdout:write('" + MARK + "' .. vim.json.encode({ ok = ok, msg = msg or '', "
    "state = vim.fn.stdpath('state') }) .. '\\n')"
)


# --- Neovim itself ---------------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Return (major, minor, patch) from `0.12.5+v0.12.5`, `NVIM v0.11.2` or `0.12.0-dev-12+gabc`."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def version_str(version: tuple[int, int, int]) -> str:
    return ".".join(str(n) for n in version)


@dataclass(frozen=True)
class Nvim:
    """A Neovim binary and the directories it uses (as it reports them)."""

    exe: str  # v:progpath (the real binary, not a scoop shim) when it still exists
    version: tuple[int, int, int]
    config: Path
    data: Path
    state: Path
    cache: Path

    @property
    def version_text(self) -> str:
        return version_str(self.version)

    @property
    def lazyvim_json(self) -> Path:
        """LazyVim reads vim.g.lazyvim_json first; the default is <config>/lazyvim.json."""
        return self.config / "lazyvim.json"

    @property
    def trust_db(self) -> Path:
        return self.state / "trust"

    def lazyvim_installed(self) -> bool:
        return (self.config / "lua" / "config" / "lazy.lua").is_file() or (self.data / "lazy" / "LazyVim").is_dir()


def which(name: str) -> str | None:
    """shutil.which on the PATH that child processes get (without this runner's own venv)."""
    return shutil.which(name, path=proc.base_env().get("PATH"))


def find_nvim() -> str | None:
    return which("nvim")


def parse_marker(stdout: str) -> dict[str, Any] | None:
    """Return the JSON object of the first `PTNVIM{...}` line in `stdout`."""
    for line in stdout.splitlines():
        i = line.find(MARK)
        if i < 0:
            continue
        try:
            data = json.loads(line[i + len(MARK) :])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def headless(
    exe: str,
    lua: str,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    """Run `nvim --headless --clean -n -i NONE -c LUA -c qa!` and return the PTNVIM{json} it prints.

    --clean skips every config and plugin; -n and -i NONE write no swap file and no shada.
    NVIM_LOG_FILE goes to a temporary directory unless `env` names one: if the log cannot be
    written, Neovim drops an nvim.log into the cwd. A Lua error still exits with 0, hence the
    marker line.
    """
    argv = [exe, "--headless", "--clean", "-n", "-i", "NONE", "-c", lua, "-c", "qa!"]
    ui.detail("$ " + proc.show(argv))
    with tempfile.TemporaryDirectory(prefix="pt-nvim-", ignore_cleanup_errors=True) as tmp:
        full = dict(env) if env is not None else proc.base_env()
        if env is None or "NVIM_LOG_FILE" not in env:
            full["NVIM_LOG_FILE"] = str(Path(tmp) / "nvim.log")
        try:
            r = subprocess.run(
                argv,
                cwd=cwd or tmp,
                env=full,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise DeployError(f"program not found: {exe}", 3) from None
        except subprocess.TimeoutExpired:
            raise DeployError(f"Neovim did not answer in {timeout:.0f} s: {proc.show(argv)}", 3) from None
    data = parse_marker(r.stdout)
    if data is None:
        detail = (r.stderr or r.stdout).strip()
        raise DeployError(f"Neovim headless call failed (exit code {r.returncode}): {proc.show(argv)}" + (f"\n{detail}" if detail else ""), 3)
    return data


def query(exe: str | None = None, *, env: Mapping[str, str] | None = None) -> Nvim | None:
    """Ask Neovim for its version and stdpath() directories; None when `nvim` is not in PATH."""
    exe = exe or find_nvim()
    if not exe:
        return None
    data = headless(exe, QUERY_LUA, env=env)
    version = parse_version(str(data.get("version", "")))
    if version is None:
        raise DeployError(f"could not parse the Neovim version: {data.get('version')!r}", 3)
    progpath = str(data.get("progpath") or "")
    return Nvim(
        exe=progpath if progpath and Path(progpath).is_file() else exe,
        version=version,
        config=Path(str(data["config"])),
        data=Path(str(data["data"])),
        state=Path(str(data["state"])),
        cache=Path(str(data["cache"])),
    )


def require_nvim() -> Nvim:
    nv = query()
    if nv is None:
        raise DeployError("nvim not found in PATH (https://neovim.io; LazyVim needs Neovim >= 0.11.2)", 3)
    return nv


# --- trust database (vim.secure) -------------------------------------------------------------------


@dataclass(frozen=True)
class Trust:
    state: str  # trusted | changed | denied | untrusted | missing
    path: str  # real path of the file: the key Neovim uses in the trust database
    sha256: str  # hash of the file's current bytes ("" when it does not exist)
    recorded: str  # hash stored in the database ("" if no entry, "!" if denied)

    def describe(self) -> str:
        return {
            "trusted": "trusted",
            "changed": "changed since it was trusted (Neovim skips it until it is trusted again)",
            "denied": "denied in Neovim's trust database",
            "untrusted": "not trusted yet (Neovim skips it, or asks, until it is)",
            "missing": "missing (./deploy render generates it)",
        }[self.state]


def read_trust_db(db: Path) -> list[tuple[str, str]]:
    """Parse Neovim's trust database: one `<sha256|!> <full path>` per line (CRLF on Windows)."""
    try:
        text = db.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        digest, sep, path = line.partition(" ")
        if sep and digest and path:
            out.append((digest, path))
    return out


def _fold(path: str) -> str:
    """A macOS path as its (case- and normalization-insensitive) volume compares it."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())


def same_path(a: str, b: str, *, windows: bool = IS_WINDOWS, macos: bool = IS_MACOS) -> bool:
    """Compare two real paths as the OS does (Windows: case-insensitive, / and \\ alike; macOS:
    case- and Unicode-form-insensitive, like its default APFS/HFS+ volumes)."""
    if windows:
        return ntpath.normcase(a) == ntpath.normcase(b)
    if macos:
        return _fold(a) == _fold(b)
    return a == b


def trust_status(db: Path, file: Path, *, windows: bool = IS_WINDOWS, macos: bool = IS_MACOS) -> Trust:
    """Return the trust state of `file`: its entry (by real path) compared with its sha256.

    Neovim keys the database by vim.uv.fs_realpath() and hashes the raw bytes, so a moved
    project or an LF/CRLF change means "not trusted" again. That key is realpath(3)'s, which on
    macOS has the on-disk case and Unicode form, while os.path.realpath keeps the typed ones
    (`cd ~/projects/mygame` for `MyGame`): an exact entry wins, else one that is the same path
    for the OS.
    """
    real = os.path.realpath(file)
    if not file.is_file():
        return Trust("missing", real, "", "")
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    entries = read_trust_db(db)
    exact = [h for h, p in entries if p == real]
    loose = [h for h, p in entries if same_path(p, real, windows=windows, macos=macos)]
    recorded = (exact or loose or [""])[-1]
    if not recorded:
        state = "untrusted"
    elif recorded == "!":
        state = "denied"
    else:
        state = "trusted" if recorded.lower() == digest else "changed"
    return Trust(state, real, digest, recorded)


def trust_file(
    exe: str,
    file: Path,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    """Trust `file` with Neovim's own API (vim.secure.trust), in the state dir `env` selects."""
    full = dict(env) if env is not None else proc.base_env()
    full["PT_TRUST_FILE"] = str(file)
    data = headless(exe, TRUST_LUA, env=full, cwd=cwd, timeout=timeout)
    if data.get("ok") is not True:
        raise DeployError(f"vim.secure.trust failed for {file}: {data.get('msg')}", 3)
    return data


# --- LazyVim config ----------------------------------------------------------------------------


_LOCAL_SPEC_OFF = re.compile(r"\blocal_spec\s*=\s*false\b")


def local_spec_off(config: Path) -> list[Path]:
    """Return the config files that set lazy.nvim's `local_spec = false` (.lazy.lua ignored)."""
    files = [config / "init.lua", *sorted((config / "lua").rglob("*.lua"))] if config.is_dir() else []
    found: list[Path] = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if any(_LOCAL_SPEC_OFF.search(line.split("--", 1)[0]) for line in lines):
            found.append(f)
    return found


def load_lazyvim_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise DeployError(f"{path}: cannot read it as JSON ({e})") from None
    if not isinstance(data, dict):
        raise DeployError(f"{path}: expected a JSON object")
    return data


def missing_extras(path: Path, wanted: Sequence[str] = EXTRAS) -> list[str] | None:
    """Return the `wanted` extras absent from lazyvim.json (None if it does not exist or is invalid)."""
    if not path.is_file():
        return None
    try:
        data = load_lazyvim_json(path)
    except DeployError:
        return None
    extras = data.get("extras")
    have = set(extras) if isinstance(extras, list) else set()
    return [e for e in wanted if e not in have]


def merge_extras(data: dict[str, Any], wanted: Sequence[str] = EXTRAS) -> list[str]:
    """Append the missing `wanted` extras to data["extras"] (in place) and return them."""
    extras = data.get("extras", [])
    if not isinstance(extras, list):
        raise DeployError("lazyvim.json: 'extras' is not a list")
    added = [e for e in wanted if e not in extras]
    data["extras"] = [*extras, *added]
    return added


def lazyvim_json_text(data: Mapping[str, Any]) -> str:
    """Format lazyvim.json like LazyVim's own writer: sorted keys, 2-space indent, no final newline."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)


def enable_extras(
    path: Path,
    wanted: Sequence[str] = EXTRAS,
    *,
    stamp: str | None = None,
    dry_run: bool = False,
) -> tuple[list[str], Path | None]:
    """Add the missing extras to lazyvim.json after a timestamped backup; return (added, backup).

    Nothing is written (no backup either) when every extra is already there or under dry_run.
    """
    raw = path.read_bytes()
    data = load_lazyvim_json(path)
    added = merge_extras(data, wanted)
    if not added or dry_run:
        return added, None
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    n = 1
    while backup.exists():
        n += 1
        backup = path.with_name(f"{path.name}.{stamp}-{n}.bak")
    backup.write_bytes(raw)
    path.write_text(lazyvim_json_text(data), encoding="utf-8", newline="\n")
    return added, backup


def short_extra(name: str) -> str:
    return name.removeprefix(EXTRA_PREFIX)


# --- helpers ----------------------------------------------------------------------------------------


def remove_tree(path: Path) -> None:
    """shutil.rmtree that also deletes read-only files (git objects on Windows)."""

    def retry(func: Callable[[str], object], name: str, *_: object) -> None:
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
        func(name)

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def c_compiler() -> str | None:
    """Return the C compiler nvim-treesitter's tree-sitter CLI would use (CC, gcc, cc, clang, MSVC).

    macOS: /usr/bin/cc, gcc and clang exist on every Mac, even without the developer tools
    (xcrun shims): they only count when cmd_env._xcode_problem finds none (as doctor does)."""
    for name in (os.environ.get("CC"), "gcc", "cc", "clang"):
        found = which(name) if name else None
        if found:
            if IS_MACOS and os.path.dirname(found) == "/usr/bin":
                from .cmd_env import _xcode_problem  # cmd_env imports this module: import it lazily

                if _xcode_problem():
                    continue  # a shim without the developer tools: maybe a later name is real
            return found
    if IS_WINDOWS:
        from .cmd_env import _msvc  # cmd_env imports this module: import it lazily

        found_msvc, where = _msvc()
        if found_msvc:
            return f"MSVC ({where})"
    return None


def _venv_exe(env_dir: Path, name: str) -> Path:
    return env_dir / "Scripts" / f"{name}.exe" if IS_WINDOWS else env_dir / "bin" / name


def _has_package(env_dir: Path, package: str) -> bool:
    if IS_WINDOWS:
        return (env_dir / "Lib" / "site-packages" / package).is_dir()
    return any(env_dir.glob(f"lib/python3*/site-packages/{package}"))


# --- doctor --------------------------------------------------------------------------------------------


def doctor(check: Check) -> None:
    """One-line Neovim/LazyVim summary for ./deploy doctor (no output when Neovim is absent)."""
    exe = find_nvim()
    if not exe:
        return
    ui.step("neovim")
    try:
        nv = query(exe)
    except DeployError as e:
        check(None, f"Neovim: {str(e).splitlines()[0]}", "details: ./deploy nvim doctor")
        return
    if nv is None:
        return
    lazyvim = nv.lazyvim_installed()
    trust = trust_status(nv.trust_db, LAZY_LUA)
    trusted = {"trusted": "trusted", "missing": "missing"}.get(trust.state, "NOT trusted")
    good = nv.version >= MIN_LAZYVIM and lazyvim and trust.state == "trusted"
    hint = "details: ./deploy nvim doctor" + ("   (trust it once: ./deploy nvim trust)" if lazyvim and trust.state != "trusted" else "")
    check(
        True if good else None,
        f"Neovim {nv.version_text}, LazyVim {'yes' if lazyvim else 'no'}, .lazy.lua {trusted}",
        hint,
    )


TOOLS: tuple[tuple[tuple[str, ...], bool, str], ...] = (
    # (names, required, what for)
    (("git",), True, "lazy.nvim installs every plugin with git"),
    (("curl",), True, "downloads (Mason packages, blink.cmp binaries)"),
    (("tar",), True, "Mason unpacks its packages with tar"),
    (("rg",), False, "ripgrep: live grep in the pickers"),
    (("fd", "fdfind"), False, "fd: faster file pickers"),
    (("tree-sitter",), False, "tree-sitter CLI builds the parsers (LazyVim installs it with Mason when missing)"),
    (("python3", "python") if not IS_WINDOWS else ("python",), False, "Mason's PyPI packages (basedpyright, debugpy)"),
    (("node",), False, "only for pyright from Mason (basedpyright needs no Node.js)"),
    (("uvx",), False, "runs basedpyright when .venv has no basedpyright-langserver"),
)


def _which_any(names: Sequence[str]) -> str | None:
    for name in names:
        found = which(name)
        if found:
            return found
    return None


def cmd_doctor(cfg: Config) -> int:
    """nvim doctor: Neovim, LazyVim, trust of .lazy.lua, extras, tools and the project's .venv."""
    problems = 0

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        nonlocal problems
        if passed is False:
            problems += 1
        ui.check_line(passed, label, hint)

    ui.step("neovim")
    exe = find_nvim()
    if not exe:
        check(False, "nvim not found in PATH", "Install Neovim >= 0.11.2 (https://neovim.io), e.g. scoop install neovim / brew install neovim")
        return 1
    nv = query(exe)
    assert nv is not None
    check(nv.version >= MIN_LAZYVIM, f"Neovim {nv.version_text} ({nv.exe})", "LazyVim needs Neovim >= 0.11.2")
    if nv.version < MIN_PATH_TRUST:
        check(None, "Neovim < 0.12: the trust prompt still has (a)llow; ./deploy nvim trust uses the buffer form", "")
    appname = os.environ.get("NVIM_APPNAME")
    for label, path in (("config", nv.config), ("data", nv.data), ("state", nv.state)):
        check(None, f"{label:<6} {path}" + ("" if path.is_dir() else "   (does not exist)"), "")
    if appname:
        check(None, f"NVIM_APPNAME={appname}", "")

    ui.step("lazyvim")
    installed = nv.lazyvim_installed()
    check(
        installed,
        "LazyVim installed" if installed else f"LazyVim not found ({nv.config / 'lua' / 'config' / 'lazy.lua'})",
        "./deploy nvim bootstrap   (clones the LazyVim starter; an existing config is never touched)",
    )
    off = local_spec_off(nv.config) if installed else []
    if off:
        check(
            False,
            "local_spec = false in " + ", ".join(str(p) for p in off) + ": lazy.nvim ignores .lazy.lua",
            "Remove it (or load .pytemplate/nvim from your own plugin spec and run ./deploy nvim extras)",
        )
    trust = trust_status(nv.trust_db, LAZY_LUA)
    check(trust.state == "trusted", f".lazy.lua {trust.describe()}", "./deploy nvim trust   (or open Neovim here: (v)iew, :trust, restart)")
    if trust.state != "missing":
        ui.detail(f"         {trust.path}  sha256 {trust.sha256}  (database: {nv.trust_db})")
    missing = missing_extras(nv.lazyvim_json)
    if missing is None:
        if installed:
            check(None, f"{nv.lazyvim_json} not found", "Start Neovim once: LazyVim creates it (then ./deploy nvim extras)")
    elif missing:
        check(
            None,
            "extras not enabled in lazyvim.json: " + ", ".join(short_extra(e) for e in missing),
            ".lazy.lua imports them anyway; ./deploy nvim extras makes it permanent and silences LazyVim's import-order warning",
        )
    else:
        check(True, "recommended extras enabled in lazyvim.json", "")

    ui.step("tools")
    for names, required, why in TOOLS:
        found = _which_any(names)
        if found and IS_WINDOWS and "windowsapps" in found.lower() and names[-1] == "python":
            check(None, f"python is the Microsoft Store alias ({found})", "Install a real Python (scoop install python, or python.org) for Mason's PyPI packages")
            continue
        if found:
            check(True, f"{names[0]}: {found}", "")
        else:
            check(False if required else None, f"{names[0]} not found: {why}", "")
    cc = c_compiler()
    check(cc is not None, f"C compiler: {cc}" if cc else "no C compiler (CC, gcc, cc, clang or MSVC)", "nvim-treesitter compiles its parsers (scoop install mingw, or VS Build Tools)")

    ui.step("project")
    venv = envs.tool_env(cfg).dir
    if not venv.is_dir():
        check(False, f"{venv.name} is missing", "./deploy setup")
    else:
        for tool in ("ruff", "mypy"):
            exe_path = _venv_exe(venv, tool)
            check(exe_path.is_file(), f"{tool} in {venv.name}", "./deploy sync")
        debugpy = _has_package(venv, "debugpy")
        check(debugpy, f"debugpy in {venv.name} (nvim-dap)", "./deploy sync   (debugpy is in the dev group, CPython only)")
        based = _venv_exe(venv, "basedpyright-langserver").is_file()
        check(True if based else None, f"basedpyright in {venv.name}" if based else f"basedpyright not in {venv.name} (optional)", ".lazy.lua falls back to uvx, then to Mason")

    ui.info("")
    if problems:
        ui.error(f"{problems} problem(s)")
        return 1
    ui.ok("Neovim integration ready")
    return 0


# --- trust / extras / bootstrap / sync --------------------------------------------------------------


def cmd_trust(nv: Nvim) -> int:
    """nvim trust: pre-trust ROOT/.lazy.lua with vim.secure.trust (the same as (v)iew + :trust)."""
    before = trust_status(nv.trust_db, LAZY_LUA)
    if before.state == "missing":
        raise DeployError(".lazy.lua not found: ./deploy render generates it")
    if before.state == "trusted":
        ui.ok(f"already trusted: {before.path}")
        ui.info(f"  sha256 {before.sha256}")
        return 0
    if proc.DRY_RUN:
        ui.info(f"would trust {before.path}\n  sha256 {before.sha256}\n  in {nv.trust_db}")
        return 0
    trust_file(nv.exe, LAZY_LUA, cwd=ROOT)
    after = trust_status(nv.trust_db, LAZY_LUA)
    if after.state != "trusted":
        raise DeployError(f"Neovim reported success, but {nv.trust_db} has no matching entry for {after.path} ({after.state})", 3)
    ui.ok(f"trusted {after.path}")
    ui.info(f"  sha256 {after.sha256}\n  database {nv.trust_db}")
    ui.info("  It also lets .lazy.lua load the local plugin .pytemplate/nvim/. Any change to the file needs a new trust.")
    return 0


def cmd_extras(nv: Nvim) -> int:
    """nvim extras: enable the recommended LazyVim extras in the user's lazyvim.json."""
    path = nv.lazyvim_json
    if not nv.config.is_dir():
        raise DeployError(f"no Neovim config in {nv.config}: ./deploy nvim bootstrap installs the LazyVim starter", 3)
    if not path.is_file():
        raise DeployError(f"{path} does not exist yet. Start Neovim once (LazyVim creates it), then run ./deploy nvim extras again", 3)
    added, backup = enable_extras(path, dry_run=proc.DRY_RUN)
    if not added:
        ui.ok(f"every recommended extra is already enabled in {path}")
        return 0
    verb = "would enable" if proc.DRY_RUN else "enabled"
    ui.ok(f"{verb} in {path}: " + ", ".join(short_extra(e) for e in added))
    if backup:
        ui.info(f"  backup: {backup}")
    ui.info("  Restart Neovim: LazyVim imports them in order (see :LazyExtras).")
    return 0


def cmd_bootstrap(nv: Nvim) -> int:
    """nvim bootstrap: clone the LazyVim starter into <config> (only if <config> does not exist)."""
    if nv.config.exists():
        ui.info(f"{nv.config} already exists: nothing done (an existing Neovim config is never touched)")
        if not nv.lazyvim_installed():
            ui.info("  It is not a LazyVim config. See https://lazyvim.github.io/installation to switch by hand.")
        return 0
    git = which("git")
    if not git:
        raise DeployError("git not found in PATH (needed to clone the starter and by lazy.nvim)", 3)
    ui.step(f"LazyVim starter -> {nv.config}")
    proc.run([git, "clone", "--depth", "1", STARTER, nv.config])
    if proc.DRY_RUN:
        ui.info(f"would remove {nv.config / '.git'}")
        return 0
    remove_tree(nv.config / ".git")  # LazyVim's install steps: the config becomes your own
    ui.ok(f"LazyVim starter installed in {nv.config}")
    ui.info("  Next: start nvim once (LazyVim installs its plugins), then ./deploy nvim trust && ./deploy nvim sync")
    return 0


def cmd_sync(nv: Nvim) -> int:
    """nvim sync: `nvim --headless "+Lazy! install" +qa` from the project (installs what .lazy.lua adds).

    install, never sync: Lazy! sync would also update every plugin of the user's config
    (rewriting lazy-lock.json) and clean the plugins its spec does not name.
    """
    if not nv.lazyvim_installed():
        raise DeployError("LazyVim is not installed: ./deploy nvim bootstrap", 3)
    trust = trust_status(nv.trust_db, LAZY_LUA)
    if trust.state == "missing":
        raise DeployError(".lazy.lua not found: ./deploy render generates it")
    if trust.state != "trusted":
        # Neovim would ask (confirm()), which never returns headless; and from another folder the
        # project's plugins are not in the spec, so there would be nothing to install.
        raise DeployError(f".lazy.lua is {trust.describe()}: ./deploy nvim trust first, then ./deploy nvim sync", 3)
    # Headless Neovim exits 0 after a Lua error (a clone that failed: "Too many rounds of missing
    # plugins"; no lazy.nvim: "E492: Not an editor command"), so lazy.nvim itself is asked
    # afterwards which plugins are installed (SYNC_CHECK_LUA, run from a file: short argv).
    argv = [nv.exe, "--headless", "+Lazy! install", "+lua dofile(vim.env.PT_NVIM_CHECK)", "+qa"]
    ui.command(proc.show(argv))
    if proc.DRY_RUN:
        return 0
    with tempfile.TemporaryDirectory(prefix="pt-nvim-", ignore_cleanup_errors=True) as tmp:
        env = proc.base_env()
        env.setdefault("NVIM_LOG_FILE", str(Path(tmp) / "nvim.log"))  # else it may land in ROOT
        check_lua, result = Path(tmp) / "check.lua", Path(tmp) / "plugins.json"
        check_lua.write_text(SYNC_CHECK_LUA, encoding="utf-8", newline="\n")
        env.update(PT_NVIM_CHECK=str(check_lua), PT_NVIM_RESULT=str(result))
        code = subprocess.run(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, check=False).returncode
        report = _read_report(result)
    if code != 0:
        raise proc.CommandFailed(argv, code)
    if report is None:
        raise DeployError("Neovim did not say which plugins are installed (see its messages above): run ./deploy nvim sync again", 1)
    if report.get("lazy") is not True:
        raise DeployError("lazy.nvim did not start (no :Lazy command; see the messages above): ./deploy nvim doctor", 3)
    missing, failed = _names(report.get("missing")), _names(report.get("failed"))
    if missing:
        raise DeployError(
            f"lazy.nvim could not install: {', '.join(missing)} (see the messages above: no network, a proxy?). "
            "Run ./deploy nvim sync again, or start Neovim in the project",
            1,
        )
    if failed:
        ui.warn(f"lazy.nvim reported errors for: {', '.join(failed)} (see the messages above, or :Lazy)")
    ui.ok("plugins installed (your other plugins were neither updated nor removed)")
    return 0


# Run by `nvim sync` after `Lazy! install` (which waits): lazy.nvim's own view of every plugin
# of the spec, written as JSON to $PT_NVIM_RESULT (the user's terminal keeps Neovim's output).
SYNC_CHECK_LUA = """\
local r = { lazy = false, missing = {}, failed = {} }
local ok, cfg = pcall(require, "lazy.core.config")
if ok and type(cfg) == "table" and type(cfg.plugins) == "table" and vim.fn.exists(":Lazy") == 2 then
  r.lazy = true
  local okp, plugin = pcall(require, "lazy.core.plugin")
  for name, p in pairs(cfg.plugins) do
    if not (type(p) == "table" and type(p._) == "table" and p._.installed) then
      table.insert(r.missing, name)
    elseif okp and type(plugin) == "table" and type(plugin.has_errors) == "function" and plugin.has_errors(p) then
      table.insert(r.failed, name)
    end
  end
end
local f = assert(io.open(vim.env.PT_NVIM_RESULT, "w"))
f:write(vim.json.encode(r))
f:close()
"""


def _read_report(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _names(value: object) -> list[str]:
    """Plugin names from the report (an empty Lua table may arrive as {} instead of [])."""
    return sorted(str(v) for v in value) if isinstance(value, list) else []


ACTIONS = ("doctor", "trust", "extras", "bootstrap", "sync")


def cmd_nvim(cfg: Config, args: list[str]) -> int:
    """nvim [doctor|trust|extras|bootstrap|sync]"""
    parser = argparse.ArgumentParser(
        prog="./deploy nvim",
        description="Neovim/LazyVim integration. doctor (default): check it; trust: pre-trust .lazy.lua; "
        "extras: enable the recommended extras in lazyvim.json; bootstrap: install the LazyVim starter "
        "if there is no config; sync: install the plugins .lazy.lua adds (Lazy! install: nothing is "
        "updated or removed; .lazy.lua must be trusted).",
    )
    parser.add_argument("action", nargs="?", default="doctor", choices=ACTIONS)
    ns = parser.parse_args(args)
    if ns.action == "doctor":
        return cmd_doctor(cfg)
    nv = require_nvim()
    if nv.version < MIN_LAZYVIM:
        ui.warn(f"Neovim {nv.version_text}: LazyVim needs >= {version_str(MIN_LAZYVIM)}")
    actions: dict[str, Callable[[Nvim], int]] = {
        "trust": cmd_trust,
        "extras": cmd_extras,
        "bootstrap": cmd_bootstrap,
        "sync": cmd_sync,
    }
    return actions[ns.action](nv)
