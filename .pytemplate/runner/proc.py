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
from pathlib import Path

from . import ui
from .project import IS_WINDOWS, ROOT, rel
from .ui import DeployError

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
# - UV_NO_DEV, UV_NO_DEFAULT_GROUPS: an environment without the dev group (no mypy, ruff,
#   pytest; a PATH-wide ruff or mypy of another version runs instead);
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
    "UV_NO_SYNC",
)


def exit_code(code: int) -> int:
    """POSIX: a child killed by signal N (returncode -N) -> 128 + N, the convention of sh and uv
    (as the runner's own exit status, -N would become 256 - N). Windows codes are never negative."""
    return 128 - code if code < 0 else code


class CommandFailed(DeployError):
    def __init__(self, argv: Sequence[str | Path], code: int) -> None:
        code = exit_code(code)
        super().__init__(f"failed (exit code {code}): {show(argv)}", code)


class Interrupted(KeyboardInterrupt):
    """Ctrl+C arrived while a child ran, and the child has exited with `code`.

    The command stops here (a KeyboardInterrupt: `check`, `test all` and task deps do not go on
    to the next step); cli.main exits with `code`, or 130 when the child exited 0 or died of
    the Ctrl+C: an interrupted command never reports success.
    """

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def find_uv() -> str:
    """Find uv: it exports its own path in $UV to the processes it starts with `uv run`."""
    uv = os.environ.get("UV")
    if uv and Path(uv).is_file():
        return uv
    found = shutil.which("uv")
    if not found:
        raise DeployError("uv not found in PATH", 3)
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
    - PYTHONUTF8=1: mypy/mypyc open files with the locale encoding (cp1252 on Windows).
    - On Windows, the Visual Studio installer in PATH: VS 2026's vcvarsall.bat calls
      vswhere.exe without a path and, if that fails, setuptools cannot find the compiler.
    """
    env = dict(os.environ)
    for key in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", *UV_SELECTION):
        env.pop(key, None)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    if sys.prefix != sys.base_prefix:
        own = os.path.normcase(str(Path(sys.prefix) / ("Scripts" if IS_WINDOWS else "bin")))
        parts = [p for p in parts if os.path.normcase(p) != own]
    if IS_WINDOWS:
        vs = vs_installer_dir()
        if vs and os.path.normcase(str(vs)) not in {os.path.normcase(p) for p in parts}:
            parts.append(str(vs))
    env["PATH"] = os.pathsep.join(parts)
    env["PYTHONUTF8"] = "1"
    return env


def show(argv: Sequence[str | Path]) -> str:
    """The command line for display (never for running it): paths inside the project relative
    to it, a program outside it by its bare name (uv.exe -> uv)."""
    out: list[str] = []
    for i, a in enumerate(argv):
        s = str(a)
        if os.path.isabs(s):
            try:
                s = Path(s).relative_to(ROOT).as_posix()
            except ValueError:
                if i == 0:
                    s = Path(s).stem
        out.append(s if s and not any(c in s for c in " \t\"'&|<>^;") else shlex.quote(s))
    return " ".join(out)


@contextlib.contextmanager
def _wait_through_ctrl_c() -> Iterator[list[int]]:
    """Record Ctrl+C while a child runs instead of letting it interrupt the wait.

    Ctrl+C reaches the child too (the terminal signals the whole foreground process group, the
    console every attached process). Without this, subprocess.run SIGKILLs the child 0.25 s
    after the KeyboardInterrupt (bpo-25942): an app's cleanup is cut short, and under `uv run`
    uv dies while the app keeps running as an orphan. The handler is a no-op Python function,
    never SIG_IGN (which the child would inherit), and it is only installed in the main thread
    while SIGINT has its default handler: a runner started with SIGINT ignored (a background
    job) keeps passing SIG_IGN on to its children.
    """
    hits: list[int] = []
    active = threading.current_thread() is threading.main_thread() and signal.getsignal(signal.SIGINT) is signal.default_int_handler
    if active:
        signal.signal(signal.SIGINT, lambda _signum, _frame: hits.append(1))
    try:
        yield hits
    finally:
        if active:
            signal.signal(signal.SIGINT, signal.default_int_handler)


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
      Interrupted stops the command.
    - A missing working folder, a missing program or one that cannot be started (no exec bit,
      no #! line, a folder) are DeployErrors, never tracebacks.
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
        raise DeployError(f"{what}: {rel(workdir)}  (the working folder of {show(args[:1])})")
    with _wait_through_ctrl_c() as interrupts:
        try:
            proc = subprocess.run(
                args,
                cwd=workdir,
                env=dict(env) if env is not None else base_env(),
                capture_output=capture,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            raise DeployError(f"program not found: {args[0]}", 3) from None
        except OSError as e:  # PermissionError (no exec bit, a folder), ENOEXEC (no #! line), WinError 193
            raise DeployError(f"cannot run {args[0]}: {e.strerror or e}  (is it executable? a script needs a #! line)") from None
        except ValueError as e:  # an argument or environment value subprocess cannot pass (NUL, '=' in a name)
            raise DeployError(f"cannot run {args[0]}: {e}") from None
    proc.returncode = exit_code(proc.returncode)
    if interrupts:
        raise Interrupted(proc.returncode)
    if check and proc.returncode != 0:
        if capture and proc.stderr:
            ui.report(proc.stderr.rstrip())  # why it failed: shown even with -q
        raise CommandFailed(args, proc.returncode)
    return proc


def output(argv: Sequence[str | Path], *, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> str:
    """Run without echo and return stdout (raises CommandFailed on failure)."""
    return run(argv, env=env, cwd=cwd, capture=True, echo=False).stdout.strip()
