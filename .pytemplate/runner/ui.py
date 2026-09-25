"""Runner console output: everything goes to stderr so it does not mix with your app's output."""

from __future__ import annotations

import os
import sys


class DeployError(Exception):
    """Expected error: shown without a traceback, and the runner exits with `code`.

    Codes: 2 = usage/configuration, 3 = a requirement is missing (uv, compiler, interpreter).
    """

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


VERBOSE = False
QUIET = False

# Windows console API (GetStdHandle takes the DWORD values of -11 and -12)
_STD_OUTPUT_HANDLE = 0xFFFFFFF5
_STD_ERROR_HANDLE = 0xFFFFFFF4
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


if sys.platform == "win32":

    def enable_vt_mode() -> bool:
        """Turn on ANSI escape sequences in the Windows console; return whether stderr has them.

        Windows Terminal and VS Code already have them on; the classic console (conhost) needs
        ENABLE_VIRTUAL_TERMINAL_PROCESSING on each output handle that is a console. The old
        trick, os.system(""), started a cmd.exe on every run. A handle that is not a console
        (a pipe, a file, NUL, a mintty pty) is left alone.
        """
        import ctypes
        from ctypes import wintypes

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            get_std_handle = kernel32.GetStdHandle
            get_std_handle.argtypes = [wintypes.DWORD]
            get_std_handle.restype = wintypes.HANDLE
            get_mode = kernel32.GetConsoleMode
            get_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            get_mode.restype = wintypes.BOOL
            set_mode = kernel32.SetConsoleMode
            set_mode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            set_mode.restype = wintypes.BOOL
        except (OSError, AttributeError):
            return False
        stderr_ok = False
        for which in (_STD_ERROR_HANDLE, _STD_OUTPUT_HANDLE):
            handle = get_std_handle(which)
            mode = wintypes.DWORD()
            if not handle or not get_mode(handle, ctypes.byref(mode)):
                continue  # not a console (redirected), or no handle at all
            on = bool(mode.value & _ENABLE_VIRTUAL_TERMINAL_PROCESSING) or bool(
                set_mode(handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)
            )
            if which == _STD_ERROR_HANDLE:
                stderr_ok = on
        return stderr_ok

else:

    def enable_vt_mode() -> bool:
        """Only Windows consoles need ANSI sequences turned on."""
        return False


def color_enabled() -> bool:
    """Colors only on a terminal, never with NO_COLOR (any non-empty value) or TERM=dumb."""
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        if not sys.stderr.isatty():
            return False
    except (AttributeError, ValueError):  # replaced or closed stream
        return False
    return enable_vt_mode() if sys.platform == "win32" else True


_COLOR = color_enabled()


def _paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _out(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def step(msg: str) -> None:
    if not QUIET:
        _out(_paint("1;36", "==> ") + _paint("1", msg))


def info(msg: str) -> None:
    if not QUIET:
        _out(msg)


def detail(msg: str) -> None:
    if VERBOSE:
        _out(_paint("2", msg))


def command(text: str) -> None:
    if not QUIET:
        _out(_paint("2", "$ " + text))


def ok(msg: str) -> None:
    if not QUIET:
        _out(_paint("32", "ok ") + msg)


def warn(msg: str) -> None:
    _out(_paint("33", "warning: ") + msg)


def error(msg: str) -> None:
    _out(_paint("31", "error: ") + msg)


def check_line(passed: bool | None, label: str, hint: str = "") -> None:
    """Print one `doctor` line: [ok] / [XX] / [--] (not applicable) plus an optional hint."""
    mark = {True: _paint("32", "ok"), False: _paint("31", "XX"), None: _paint("2", "--")}[passed]
    _out(f"  [{mark}] {label}")
    if hint and passed is not True:
        for line in hint.splitlines():
            _out(_paint("2", f"         {line}"))
