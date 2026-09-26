"""Environment commands: setup, doctor, sync, lock, add, remove, clean."""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import cmd_nvim, envs, hooks, mypyc, proc, render, shells, ui
from .cmd_dev import only_flags
from .config import Config
from .project import BUILD, DIST, ENV_SUFFIX, IS_MACOS, IS_WINDOWS, PYPROJECT, ROOT, rel
from .ui import DeployError

LAUNCHERS_X = ("deploy", "deploy.ps1")  # the launchers that must stay executable (100755)


def _envs_for(cfg: Config, target: str) -> list[envs.PyEnv]:
    if target == "all":
        out = [envs.cpython_env(cfg)]
        if cfg.pypy_enabled:
            out.append(envs.pypy_env(cfg))
        return out
    if target in ("cpython", "mypyc"):
        return [envs.cpython_env(cfg)]
    if target == "pypy":
        envs.ensure_supported(cfg, "pypy")
        return [envs.pypy_env(cfg)]
    raise DeployError(f"sync: unknown target '{target}' (cpython | pypy | mypyc | all)")


def ensure_lock(cfg: Config) -> None:
    """Apply the managed parts of pyproject and re-lock if needed."""
    tool = envs.tool_env(cfg)
    if render.write_pyproject(cfg):
        ui.info(render.pyproject_message())
        if proc.DRY_RUN:
            # Nothing was written, so `uv lock --check` would read the old pyproject.toml and
            # pass: show the re-lock the real run makes (echoed, skipped under --dry-run).
            envs.uv(tool, ["lock"])
            return
    r = envs.uv(tool, ["lock", "--check"], check=False, capture=True, echo=False)
    if r.returncode != 0:
        envs.uv(tool, ["lock"])


def cmd_setup(cfg: Config, args: list[str]) -> int:
    """setup [--force]: the first-time name of ./deploy apply (one implementation: cmd_apply.apply)."""
    from . import cmd_apply  # cmd_apply imports this module

    return cmd_apply.apply(cfg, args, command="setup")


def _fix_exec_bit() -> None:
    """Keep `deploy` and `deploy.ps1` executable: the files themselves (POSIX) and their git
    mode 100755 (core.filemode=false on Windows loses it).

    deploy.ps1 needs it for `./deploy.ps1` from pwsh on Linux/macOS.
    """
    # The files first, with or without git: a copy or an archive that dropped the mode leaves
    # ./deploy unusable, and with core.filemode=true an index-only fix is undone by the next
    # `git add` (it records the file's 100644 again).
    if not IS_WINDOWS:
        for launcher in LAUNCHERS_X:
            path = ROOT / launcher
            if path.is_file() and not os.access(path, os.X_OK):
                ui.command(f"chmod +x {launcher}")
                if not proc.DRY_RUN:
                    path.chmod(path.stat().st_mode | 0o111)
    # rev-parse, not ROOT/.git: the project may live in a subfolder of a bigger repository
    if not shutil.which("git"):
        return
    if proc.run(["git", "rev-parse", "--is-inside-work-tree"], capture=True, check=False, echo=False).returncode != 0:
        return
    for launcher in LAUNCHERS_X:
        r = proc.run(["git", "ls-files", "-s", launcher], capture=True, check=False, echo=False)
        if r.stdout.startswith("100644"):
            proc.run(["git", "update-index", "--chmod=+x", launcher], check=False)


def cmd_sync(cfg: Config, args: list[str]) -> int:
    """sync [cpython|pypy|mypyc|all]: `uv sync --locked` of the environment(s)."""
    if len(args) > 1:
        raise DeployError(f"sync: unrecognized arguments: {' '.join(args[1:])}  (one target: cpython | pypy | mypyc | all)")
    target = args[0] if args else "all"
    for env in _envs_for(cfg, target):
        envs.sync(env)
    return 0


# `uv lock` arguments (and the variables uv reads for them) that make it write no uv.lock
LOCK_READ_ONLY = ("--check", "--locked", "--check-exists", "--frozen", "--dry-run")
LOCK_READ_ONLY_ENV = ("UV_LOCKED", "UV_FROZEN")


def _lock_read_only(args: list[str]) -> str:
    """The argument or variable that keeps `uv lock` from writing uv.lock, or ""."""
    for a in args:
        if a in LOCK_READ_ONLY:
            return a
    for name in LOCK_READ_ONLY_ENV:  # uv's boolean variables: 1/true/yes/on
        if os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on", "y", "t"):
            return name
    return ""


def cmd_lock(cfg: Config, args: list[str]) -> int:
    """lock [--upgrade] [--upgrade-package PKG]: apply the managed pyproject parts and `uv lock`.

    pyproject.toml and uv.lock change together or not at all: when `uv lock` fails (offline, no
    solution, Ctrl+C) or writes no uv.lock (--check, --dry-run...), pyproject.toml gets its old
    bytes back. Otherwise the two would disagree and every `uv run --locked` would fail.
    """
    before = PYPROJECT.read_bytes() if PYPROJECT.is_file() else None
    changed = render.write_pyproject(cfg)
    if changed:
        ui.info(render.pyproject_message())

    def restore(why: str) -> None:
        if changed and before is not None and not proc.DRY_RUN and PYPROJECT.read_bytes() != before:
            PYPROJECT.write_bytes(before)
            ui.info(f"pyproject.toml: put back as it was ({why})")

    try:
        envs.uv(envs.tool_env(cfg), ["lock", *args])
    except BaseException:  # a failed uv lock leaves uv.lock alone; Ctrl+C too
        restore("uv lock did not update uv.lock")
        raise
    read_only = _lock_read_only(args)
    if read_only:
        restore(f"uv lock with {read_only} writes no uv.lock")
    render.apply(cfg)
    return 0


def _add_remove(cfg: Config, verb: str, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"./deploy {verb}")
    parser.add_argument("packages", nargs="+")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--dev", action="store_true", help="development group")
    # setup and sync install every group (envs.sync: --all-groups), so it stays installed
    where.add_argument("--group", help="dependency group (add, remove, setup and sync install every group)")
    if verb == "add":
        parser.add_argument(
            "--cpython-only",
            action="store_true",
            help="only on CPython/mypyc (C-API libraries such as numpy: slow or unavailable on PyPy)",
        )
    ns = parser.parse_args(args)
    # --no-sync, then envs.sync: uv's own sync after `remove` is EXACT for the default groups
    # only, so it uninstalled every package of the other groups (`add --group G`); `uv sync
    # --all-groups` keeps them, as setup and sync do.
    argv: list[str] = [verb, "--no-sync"]
    if ns.dev:
        argv.append("--dev")
    if ns.group:
        argv += ["--group", ns.group]
    if verb == "add" and ns.cpython_only:
        argv += ["--marker", "implementation_name == 'cpython'"]
    argv += ns.packages
    tool = envs.tool_env(cfg)
    envs.uv(tool, argv)
    envs.sync(tool)
    if verb == "add" and cfg.pypy_enabled and not ns.cpython_only:
        ui.info(
            "PyPy is supported: if the package uses the CPython C-API (numpy, pillow, pydantic-core...) "
            "it will be slow on PyPy; consider `--cpython-only`. Check with: ./deploy sync pypy"
        )
    return 0


def cmd_add(cfg: Config, args: list[str]) -> int:
    """add PKG... [--dev|--group G] [--cpython-only]"""
    return _add_remove(cfg, "add", args)


def cmd_remove(cfg: Config, args: list[str]) -> int:
    """remove PKG... [--dev|--group G]"""
    return _add_remove(cfg, "remove", args)


_JUNCTION = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT: a Windows junction (Path.is_symlink() is False)


def _is_link(path: Path) -> bool:
    """A symlink or a Windows junction: clean removes the link itself, never what it points to."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    tag: int = getattr(st, "st_reparse_tag", 0)
    return stat.S_ISLNK(st.st_mode) or tag == _JUNCTION


def _env_dirs() -> list[Path]:
    """The .venv* environments of this side. WSL on a Windows checkout (/mnt/...) keeps its own
    `.venv*-wsl` next to the Windows ones (project.ENV_SUFFIX): each side removes only its own."""
    wsl = bool(ENV_SUFFIX)
    found = [
        p
        for p in ROOT.glob(".venv*")
        if p.name.endswith("-wsl") == wsl and (p.is_dir() or (_is_link(p) and not p.exists()))  # never a file
    ]
    return sorted(found)


def _make_writable(root: Path) -> None:
    """Clear read-only flags below `root` (links are neither followed nor changed)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_link(Path(dirpath, d))]
        for name in (*dirnames, *filenames):
            path = os.path.join(dirpath, name)
            with contextlib.suppress(OSError):
                mode = os.lstat(path).st_mode
                if not stat.S_ISLNK(mode):
                    os.chmod(path, mode | stat.S_IWRITE | (stat.S_IRWXU if stat.S_ISDIR(mode) else 0))


def _remove(path: Path) -> bool:
    """Remove a folder, or only the link when it is a symlink/junction; return whether it is gone."""
    if _is_link(path):
        with contextlib.suppress(OSError):
            os.unlink(path)  # on Windows this also removes a directory symlink or a junction
        return not os.path.lexists(path)
    # ignore_errors, then check: onerror is deprecated since 3.12 and onexc does not exist in 3.11
    shutil.rmtree(path, ignore_errors=True)
    if os.path.lexists(path):  # read-only files (Windows) or folders (POSIX): once more, writable
        _make_writable(path)
        shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def _shown(path: Path) -> str:
    """The path relative to the root, without resolving links (rel() would show a link's target)."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def cmd_clean(cfg: Config, args: list[str]) -> int:
    """clean [--envs]: remove .build/ and dist/ (and this side's .venv* environments with --envs).

    A folder that cannot be removed completely (a file in use on Windows: the editor's mypy or
    ruff server runs from .venv) is an error (exit 1): a half-deleted .venv breaks every command.
    """
    flags = only_flags("clean", args, ("--envs",))
    targets = [BUILD, DIST]
    if "--envs" in flags:
        targets += _env_dirs()
    failed: list[str] = []
    for t in targets:
        if not os.path.lexists(t):
            continue
        if proc.DRY_RUN:
            ui.info(f"would remove {_shown(t)}")
            continue
        ui.info(f"removing {_shown(t)}")
        if not _remove(t):
            failed.append(_shown(t))
    if failed:
        again = "./deploy clean --envs" if "--envs" in flags else "./deploy clean"
        ui.error(
            f"could not remove {', '.join(failed)} completely: a file in it is in use or not writable.\n"
            "  Close what uses it (editors and their language servers, debuggers, the running app), "
            f"then run {again} again"
        )
        return 1
    return 0


# --- doctor --------------------------------------------------------------------------------------


def _msvc(platform: str = "win-amd64") -> tuple[bool, str]:
    """Visual Studio's C++ tools for `platform`, the sysconfig.get_platform() of the Python that
    runs mypyc (.venv): the same vswhere query setuptools makes (win-arm64 -> VC.Tools.arm64,
    anything else -> VC.Tools.x86.x64; uv installs x86_64 CPython even on Windows on ARM)."""
    vs = proc.vs_installer_dir()
    if not vs:
        return False, "no Visual Studio / Build Tools (vswhere.exe not found)"
    component = "VC.Tools.arm64" if platform == "win-arm64" else "VC.Tools.x86.x64"
    argv = [str(vs / "vswhere.exe"), "-latest", "-prerelease", "-products", "*"]
    argv += ["-requires", f"Microsoft.VisualStudio.Component.{component}", "-property", "installationPath"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as e:
        return False, f"vswhere.exe does not run: {e}"
    path = r.stdout.strip()
    if not path:
        return False, f"Visual Studio found, but without the C++ tools for {platform} ({component})"
    return True, path


# Where the active developer folder (`xcode-select -p`) keeps the clang the /usr/bin shims run:
# the Command Line Tools (/Library/Developer/CommandLineTools/usr/bin/clang) or an Xcode.app's
# default toolchain (Xcode 26's Contents/Developer has no usr/bin/xcrun, so that was no test)
XCODE_CLANG = (("usr", "bin", "clang"), ("Toolchains", "XcodeDefault.xctoolchain", "usr", "bin", "clang"))


def _xcode_problem() -> str | None:
    """macOS: why the /usr/bin compiler shims cannot compile, or None.

    /usr/bin/cc, gcc and clang exist on every Mac, even without the developer tools: they run
    the clang of the active developer folder. The shims themselves never run here (on a fresh
    Mac they open the install dialog)."""
    try:
        r = proc.run(["/usr/bin/xcode-select", "-p"], capture=True, check=False, echo=False)
    except DeployError:
        return "no xcode-select"
    dev = r.stdout.strip()
    if r.returncode != 0 or not dev:
        return "no Xcode Command Line Tools"
    if not any(os.path.isfile(os.path.join(dev, *parts)) for parts in XCODE_CLANG):
        return f"the developer folder {dev} has no clang (usual after a macOS upgrade)"
    return None


def _c_compiler(platform: str = "", cc: str = "") -> tuple[bool, str]:
    """The C compiler mypyc would use. Windows: MSVC for `platform` (the .venv Python's sysconfig
    platform). Elsewhere what setuptools runs, nothing else: $CC when it is set, else `cc`, the
    CC of the .venv Python's sysconfig ("cc -pthread"; "cc" when unknown). A gcc or clang next to
    a missing `cc`, or a CC naming a missing program, is no compiler: the build would fail."""
    if IS_WINDOWS:
        return _msvc(platform or "win-amd64")
    user = "CC" in os.environ
    command = os.environ["CC"] if user else cc or "cc"
    source = f"CC={command!r}" if user else f"the CC of the .venv Python: {command!r}"
    try:
        words = shlex.split(command)
    except ValueError:  # unbalanced quotes
        words = []
    if not words:
        return False, f"no C compiler ({source})"
    found = shutil.which(words[0])  # "ccache gcc" runs ccache
    if not found:
        return False, f"{words[0]} not found ({source})"
    if IS_MACOS and os.path.dirname(found) == "/usr/bin":
        problem = _xcode_problem()
        if problem:
            return False, f"{found} is an Xcode shim: {problem}"
    return True, found


def cmd_doctor(cfg: Config, args: list[str]) -> int:
    """doctor: check requirements, environments and generated files."""
    only_flags("doctor", args, ())
    problems = 0

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        nonlocal problems
        if passed is False:
            problems += 1
        ui.check_line(passed, label, hint)

    def env_info(env: envs.PyEnv) -> dict[str, object] | None:
        # A python that exists but cannot start (Windows: its base Python was uninstalled and the
        # venv launcher exits 103 "No Python at ..."; no exec bit; garbage output) is a problem
        # to report, not a crash: every later check still runs.
        try:
            return envs.interpreter_info(env.python)
        except (DeployError, OSError, ValueError):  # ValueError: json.JSONDecodeError
            check(
                False,
                f"environment {rel(env.dir)} is broken (its Python does not start)",
                "./deploy setup   (if it still fails: ./deploy clean --envs, then ./deploy setup)",
            )
            return None

    ui.step("tools")
    uv_version = proc.output([proc.find_uv(), "--version"])
    too_old = envs.uv_problem(uv_version)
    if too_old:
        check(False, f"uv: {uv_version}", f"{too_old}\nUpdate it: {envs.UV_UPDATE}")
    elif envs.uv_version(uv_version) is None:
        check(None, f"uv: {uv_version} (version not recognised; this project needs uv {envs.MIN_UV} or newer)")
    else:
        check(True, f"uv: {uv_version}")
    check(True, f"runner: Python {sys.version.split()[0]} ({sys.executable})")

    ui.step(f"backends (active: {cfg.backend.active}; supported: {', '.join(cfg.backend.supported)})")
    cp = envs.cpython_env(cfg)
    platform = ""  # the .venv Python's sysconfig platform: which MSVC tools mypyc needs
    cc = ""  # and its sysconfig CC: the compiler setuptools runs when $CC is not set
    if cp.python.is_file():
        if (info := env_info(cp)) is not None:
            platform = str(info.get("platform", ""))
            cc = str(info.get("cc") or "")
            check(True, f"CPython {info['version']} in {rel(cp.dir)}")
    else:
        check(False, f"environment {rel(cp.dir)} is missing", "./deploy setup")
    if cfg.pypy_enabled:
        pp = envs.pypy_env(cfg)
        if pp.python.is_file():
            if (info := env_info(pp)) is not None:
                check(info["impl"] == "pypy", f"PyPy ({info['version']}) in {rel(pp.dir)}")
        else:
            check(False, f"environment {rel(pp.dir)} is missing ({cfg.python.pypy})", "./deploy setup   (or ./deploy sync pypy)")
    if cfg.supports("mypyc"):
        found, where = _c_compiler(platform, cc=cc)
        check(found, f"C compiler for mypyc: {where}", mypyc.has_compiler_hint(platform or "win-amd64"))
    if IS_WINDOWS and cfg.supports("mypyc"):
        long_paths = _long_paths()
        check(
            True if long_paths else None,
            "Windows long paths (LongPathsEnabled)" + ("" if long_paths else ": disabled (optional)"),
            "Avoids MSVC errors when the project is in a very deep path (>260 characters)",
        )

    ui.step("project")
    changed, edited = render.apply(cfg, check=True)
    if changed or not edited:  # each hand-edited file gets its own line below: count every problem once
        check(not changed, "generated files up to date", f"outdated: {', '.join(changed)}\nThey update with any command (or ./deploy render)")
    for path in edited:
        check(
            False,
            f"{path} hand-edited",
            "Move the change into pytemplate.toml (e.g. [vscode] settings) or .pytemplate/templates\n"
            "(./deploy render --diff shows it), or drop it: ./deploy render --force",
        )
    check(not render.pyproject_outdated(cfg), "pyproject.toml matches pytemplate.toml", "./deploy apply")
    from . import cmd_apply  # lazy: cmd_apply imports this module

    cmd_apply.doctor(cfg, check)  # app.name, app.preset, [preset.*], hooks.pre_commit edited but not applied
    try:
        r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], check=False, capture=True, echo=False)
        check(r.returncode == 0, "uv.lock up to date", "\n".join(filter(None, [envs.uv_error(r.stderr or r.stdout), "./deploy lock"])))
    except DeployError as e:  # uv too old to create .venv, or it does not start
        check(False, "uv.lock up to date: uv lock --check did not run", str(e))

    shells.doctor(check)  # launchers and shells
    hooks.doctor(cfg, check)  # git pre-commit hook
    cmd_nvim.doctor(check)  # Neovim/LazyVim summary (details: ./deploy nvim doctor)
    ui.info("")
    if problems:
        ui.error(f"{problems} problem(s)")
        return 1
    ui.ok("all good")
    return 0


def _long_paths() -> bool:
    # `sys.platform == "win32"` (not an early return): mypy --strict checks this module on every
    # OS and would flag the winreg code as unreachable/undefined elsewhere.
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
                value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
                return bool(value)
        except OSError:
            pass
    return False
