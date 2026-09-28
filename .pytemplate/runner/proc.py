"""Running child processes with an explicit, reproducible environment."""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path, PurePath

from . import ui
from .project import IS_WINDOWS, ROOT, launcher_python, rel
from .ui import PytError

DRY_RUN = False

# Windows: the exit code of a console program ended by Ctrl+C (cli.main reports it as 130)
STATUS_CONTROL_C_EXIT = 0xC000013A

# uv variables of the user's environment that move the runner's `uv run --locked` calls away from
# the project, the interpreter or the environment the runner chose (checked with uv 0.12):
# - UV_PROJECT_ENVIRONMENT, UV_PYTHON: envs.env_vars sets both, always together (CLAUDE.md 7);
# - UV_PROJECT, UV_NO_PROJECT, UV_WORKING_DIR: another project (or none): --locked is ignored,
#   uv.lock is neither checked nor used, and UV_WORKING_DIR also moves every relative path;
# - UV_MANAGED_PYTHON, UV_NO_MANAGED_PYTHON: uv refuses them next to the UV_PYTHON_PREFERENCE
#   that envs.env_vars sets (exit 2 on every call);
# - UV_ISOLATED: a throwaway environment instead of .venv on every call;
# - UV_NO_DEV, UV_NO_DEFAULT_GROUPS, UV_NO_GROUP (=dev: it wins over --all-groups, and sync
#   uninstalled mypy, ruff and pytest): an environment without the dev group (a PATH-wide ruff
#   or mypy of another version runs instead, `test` finds no pytest);
# - UV_NO_SYNC: nothing is synced, so .venv stays empty after git clean -fdx and `add` never
#   reaches the environment.
# Resolution settings (indexes, UV_EXCLUDE_NEWER, UV_RESOLUTION, UV_PRERELEASE...) are the
# user's: they are kept, and uv reports clearly when they disagree with uv.lock.
UV_SELECTION = (
    "UV_PROJECT_ENVIRONMENT",
    "UV_PYTHON",
    "UV_PROJECT",
    "UV_NO_PROJECT",
    "UV_WORKING_DIR",
    "UV_MANAGED_PYTHON",
    "UV_NO_MANAGED_PYTHON",
    "UV_ISOLATED",
    "UV_NO_DEV",
    "UV_NO_DEFAULT_GROUPS",
    "UV_NO_GROUP",
    "UV_NO_SYNC",
)


def exit_code(code: int) -> int:
    """POSIX: a child killed by signal N (returncode -N) -> 128 + N, the convention of sh and uv
    (as the runner's own exit status, -N would become 256 - N). Windows codes are never negative."""
    return 128 - code if code < 0 else code


class CommandFailed(PytError):
    def __init__(self, argv: Sequence[str | Path], code: int) -> None:
        code = exit_code(code)
        super().__init__(f"failed (exit code {code}): {show(argv)}", code)


class Interrupted(KeyboardInterrupt):
    """Ctrl+C (or, on POSIX, a SIGTERM or SIGHUP sent to the runner) arrived while a child ran,
    and the child has exited with `code`. `signum`: the signal (SIGINT for Ctrl+C).

    The command stops here (a KeyboardInterrupt: `check`, `test all` and task deps do not go on
    to the next step); cli.main exits with `code`, or 128 + signum (130 for Ctrl+C) when the
    child exited 0 or died of the Ctrl+C: an interrupted command never reports success.
    """

    def __init__(self, code: int, signum: int = signal.SIGINT) -> None:
        super().__init__(code)
        self.code = code
        self.signum = int(signum)


def find_uv() -> str:
    """Find uv: it exports its own path in $UV to the processes it starts with `uv run`."""
    uv = os.environ.get("UV")
    if uv and Path(uv).is_file():
        return uv
    found = shutil.which("uv")
    if not found:
        raise PytError("uv not found in PATH", 3)
    return found


def vs_installer_dir() -> Path | None:
    root = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    if not root:
        return None
    path = Path(root) / "Microsoft Visual Studio" / "Installer"
    return path if (path / "vswhere.exe").is_file() else None


def base_env() -> dict[str, str]:
    """Return the base environment for child processes.

    - No VIRTUAL_ENV and no bin/ of this runner's isolated environment (`uv run --script`
      exports them and they would confuse the project's `uv`); no PYTHONHOME/PYTHONPATH.
    - None of the user's uv variables in UV_SELECTION: the runner picks the project, the
      interpreter and the environment of every uv call itself (envs.env_vars).
    - No PYTEMPLATE_GLOBAL: the global mode of this runner (project.GLOBAL) is its own; the
      project `new` makes runs its `__init` as a project, and no tool may take it on.
    - PYTHONUTF8=1: mypy/mypyc open files with the locale encoding (cp1252 on Windows).
    - On Windows, the Visual Studio installer in PATH: VS 2026's vcvarsall.bat calls
      vswhere.exe without a path and, if that fails, setuptools cannot find the compiler.
    """
    env = dict(os.environ)
    for key in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "PYTEMPLATE_GLOBAL", *UV_SELECTION):
        env.pop(key, None)
    parts = _without_own_bin(env.get("PATH", ""))
    if IS_WINDOWS:
        vs = vs_installer_dir()
        if vs and os.path.normcase(str(vs)) not in {os.path.normcase(p) for p in parts}:
            parts.append(str(vs))
    env["PATH"] = os.pathsep.join(parts)
    env["PYTHONUTF8"] = "1"
    return env


def _without_own_bin(path: str) -> list[str]:
    """The entries of a PATH value, without the bin/ (Scripts/) of the virtual environment this
    runner runs in (the one `uv run --script` made for it and put first on PATH)."""
    parts = [p for p in path.split(os.pathsep) if p]
    if sys.prefix != sys.base_prefix:
        own = os.path.normcase(str(Path(sys.prefix) / ("Scripts" if IS_WINDOWS else "bin")))
        parts = [p for p in parts if os.path.normcase(p) != own]
    return parts


def runner_env() -> dict[str, str]:
    """The environment of a second runner process (cli._restart): this runner's own, as the
    launcher handed it over, without the virtual environment `uv run --script` made for this one
    (VIRTUAL_ENV, its bin/ on PATH), which is no environment of the other's."""
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env["PATH"] = os.pathsep.join(_without_own_bin(env.get("PATH", "")))
    return env


def runner_argv(uv: str, root: Path, args: Sequence[str | Path], python: str | Path | None = None) -> list[str]:
    """`./pyt ARGS` of the project at `root` as its launchers run it (CLAUDE.md 4.1): uv run
    --script with the launchers' --python= request and --python-preference
    (project.launcher_python: none and only-managed, so uv follows .python-version, while the
    project has an environment; else any CPython 3.11 or newer, a system one too), or on `python`
    itself. The environment must hold no UV_MANAGED_PYTHON nor UV_NO_MANAGED_PYTHON (uv refuses
    them next to --python-preference): base_env drops them."""
    request, preference = (str(python), "managed") if python is not None else launcher_python(root)
    entry = root / ".pytemplate" / "pyt.py"
    return [uv, "run", "--quiet", f"--python={request}", "--python-preference", preference, "--script", str(entry), *map(str, args)]


def show(argv: Sequence[str | Path]) -> str:
    """The command line for display (never for running it): paths inside the project relative
    to it, a program outside it by its bare name (uv.exe -> uv; python3.14 stays python3.14)."""
    out: list[str] = []
    for i, a in enumerate(argv):
        s = str(a)
        if os.path.isabs(s):
            try:
                s = Path(s).relative_to(ROOT).as_posix()
            except ValueError:
                if i == 0:
                    name = PurePath(s).name
                    s = PurePath(s).stem if name.lower().endswith((".exe", ".cmd", ".bat", ".com")) else name
        out.append(s if s and not any(c in s for c in " \t\"'&|<>^;") else shlex.quote(s))
    return " ".join(out)


class _Waiter:
    """What arrived while a child ran: Ctrl+C presses, and the terminating signals passed on."""

    def __init__(self) -> None:
        self.interrupts: list[int] = []
        self.terminated: list[int] = []  # SIGTERM/SIGHUP received (POSIX), in order
        self.child: subprocess.Popen[str] | None = None

    def forward(self, signum: int, _frame: object = None) -> None:
        """A signal handler: pass the signal on to the child and keep waiting for it."""
        self.terminated.append(signum)
        if self.child is not None:
            with contextlib.suppress(OSError):  # the child has just exited
                self.child.send_signal(signum)

    def started(self, child: subprocess.Popen[str]) -> None:
        self.child = child
        if self.terminated:  # it arrived while the child was being started
            with contextlib.suppress(OSError):
                child.send_signal(self.terminated[-1])


@contextlib.contextmanager
def _wait_through_signals() -> Iterator[_Waiter]:
    """Keep waiting for the child through Ctrl+C, SIGTERM and SIGHUP.

    Ctrl+C reaches the child too (the terminal signals the whole foreground process group, the
    console every attached process), so it is only recorded. Without this, subprocess.run
    SIGKILLs the child 0.25 s after the KeyboardInterrupt (bpo-25942): an app's cleanup is cut
    short, and under `uv run` uv dies while the app keeps running as an orphan. The handler is
    a no-op Python function, never SIG_IGN (which the child would inherit).

    SIGTERM and SIGHUP (POSIX) are usually sent to the runner alone (kill PID, a supervisor,
    `docker stop`, Popen.terminate()): their default action killed the runner at once and left
    uv and the app running as orphans that never got the signal. They are passed on to the
    child, like uv run does, and the child is waited for. (A signal sent to the whole process
    group reaches the app more than once: from the group, and again from each process between
    that passes it on: 3 times through ./pyt and `uv run`, twice under a plain `uv run`. The
    runner cannot tell a group signal from its own, and a child in a group of its own would
    lose the terminal's Ctrl+C.)

    Each handler is only installed in the main thread and while the signal has its default
    handler: a runner started with SIGINT ignored (a background job), or under nohup, keeps
    passing SIG_IGN on to its children.
    """
    waiter = _Waiter()
    installed: list[int] = []  # the signals whose handler goes back to the default afterwards
    if threading.current_thread() is threading.main_thread():
        if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
            signal.signal(signal.SIGINT, lambda _signum, _frame: waiter.interrupts.append(1))
            installed.append(signal.SIGINT)
        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGHUP):
                if signal.getsignal(sig) is signal.SIG_DFL:
                    signal.signal(sig, waiter.forward)
                    installed.append(sig)
    try:
        yield waiter
    finally:
        for signum in installed:
            signal.signal(signum, signal.default_int_handler if signum == signal.SIGINT else signal.SIG_DFL)


def _not_found(program: str, workdir: Path, env: Mapping[str, str]) -> str:
    """Why `program` could not be started (FileNotFoundError): it is not there, or (POSIX) it is,
    and what exec needs to start it is not: the interpreter of its #! line, or the loader of a
    binary (a 32-bit or foreign-libc executable)."""
    missing = f"program not found: {program}"
    if IS_WINDOWS:  # CreateProcess only tries <name>.exe: an existing npm.cmd is "not found" too
        return missing
    if os.path.dirname(program):  # a path: exec resolves it in the working folder
        path: Path | None = workdir / program
    else:  # a name: exec searches the child's PATH
        found = shutil.which(program, path=env.get("PATH", os.defpath))
        path = Path(found) if found else None
    if path is None or not path.is_file():
        return missing
    try:
        with path.open("rb") as f:
            first = f.readline(512)
    except OSError:
        return missing
    if first.startswith(b"#!"):
        words = first[2:].decode("utf-8", "replace").split()
        interpreter = words[0] if words else "(empty)"
        if len(words) == 1 and first.rstrip(b"\n").endswith(b"\r"):
            # A CRLF checkout (a Windows checkout used from WSL): exec looks for "/bin/sh\r",
            # and split() dropped the CR, so the message named a /bin/sh that exists.
            return (
                f"cannot run {program}: its #! line ends with a carriage return (Windows line endings), "
                f"so the interpreter it names is {interpreter!r} plus that CR, which does not exist: save it "
                f"with LF line endings (git keeps them with a line such as `{program} text eol=lf` in .gitattributes)"
            )
        return f"cannot run {program}: the interpreter of its #! line was not found: {interpreter}"
    return f"cannot run {program}: it exists, but what it needs to start (its loader or interpreter) was not found"


# Why a program CreateProcess refuses could not start: it starts .exe and .com files, and .bat
# and .cmd ones through cmd.exe (WinError 193 for a .sh or .py: a #! line changes nothing there)
WINDOWS_START_HINT = "Windows starts .exe and .com programs, .bat and .cmd through cmd.exe: run a script through its interpreter, python or sh"


def _start_error(e: OSError, args: Sequence[str], workdir: Path, env: Mapping[str, str]) -> PytError:
    """Why Popen could not start `args` in `workdir`. An error the child met entering the
    working folder (a folder it may not search, another user's) names that folder: on POSIX
    subprocess sets its filename to the cwd then, and CreateProcess says ERROR_DIRECTORY (267).
    It blamed the program, and asked for an exec bit or a #! line."""
    chdir = e.filename is not None and os.fspath(e.filename) == os.fspath(workdir) != args[0]
    if chdir or getattr(e, "winerror", None) == 267:
        return PytError(f"cannot enter the working folder {rel(workdir)}: {e.strerror or e}  (the working folder of {show(args[:1])})")
    if isinstance(e, FileNotFoundError):
        return PytError(_not_found(args[0], workdir, env), 3)
    # PermissionError (no exec bit, a folder), ENOEXEC (no #! line), WinError 193 (not a program)
    hint = WINDOWS_START_HINT if IS_WINDOWS else "is it executable? a script needs a #! line"
    return PytError(f"cannot run {args[0]}: {e.strerror or e}  ({hint})")


def run(
    argv: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
    echo: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a child process (cwd defaults to the project root, env to base_env()).

    - --dry-run skips (and reports as exit 0) only the ECHOED commands; echo=False queries run.
    - A child killed by signal N reports 128 + N (exit_code).
    - Ctrl+C: the child is waited for (it got the Ctrl+C too and may clean up), then
      Interrupted stops the command. SIGTERM/SIGHUP (POSIX): passed on to the child, the same.
    - A missing working folder or one it cannot enter, a missing program or one that cannot be
      started (no exec bit, no #! line, a folder) are PytErrors, never tracebacks.
    """
    args = [str(a) for a in argv]
    where = f"   (in {rel(cwd)})" if cwd is not None and cwd.resolve() != ROOT else ""
    if echo:
        ui.command(show(args) + where)
    else:
        ui.detail("$ " + show(args) + where)
    if DRY_RUN and echo:
        return subprocess.CompletedProcess(args, 0, "", "")
    workdir = cwd or ROOT
    if not workdir.is_dir():
        # Checked before the spawn: its error would blame the program (FileNotFoundError names
        # the cwd on POSIX, and Windows raises NotADirectoryError for it)
        what = "not a folder" if workdir.exists() else "folder not found"
        raise PytError(f"{what}: {rel(workdir)}  (the working folder of {show(args[:1])})")
    pipe = subprocess.PIPE if capture else None
    child_env = dict(env) if env is not None else base_env()
    with _wait_through_signals() as waiter:
        try:
            child = subprocess.Popen(
                args,
                cwd=workdir,
                env=child_env,
                stdout=pipe,
                stderr=pipe,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:
            raise _start_error(e, args, workdir, child_env) from None
        except ValueError as e:  # an argument or environment value subprocess cannot pass (NUL, '=' in a name)
            raise PytError(f"cannot run {args[0]}: {e}") from None
        with child:  # what subprocess.run does, with the child known to the signal handlers
            waiter.started(child)
            try:
                stdout, stderr = child.communicate()
            except BaseException:
                child.kill()
                raise
    proc = subprocess.CompletedProcess(args, exit_code(child.returncode), stdout, stderr)
    if waiter.interrupts:
        raise Interrupted(proc.returncode)
    if waiter.terminated:
        raise Interrupted(proc.returncode, waiter.terminated[0])
    if check and proc.returncode != 0:
        if capture and proc.stderr:
            ui.report(proc.stderr.rstrip())  # why it failed: shown even with -q
        raise CommandFailed(args, proc.returncode)
    return proc


def output(argv: Sequence[str | Path], *, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> str:
    """Run without echo and return stdout (raises CommandFailed on failure)."""
    return run(argv, env=env, cwd=cwd, capture=True, echo=False).stdout.strip()
