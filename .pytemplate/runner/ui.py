"""Salida por consola del runner: todo va a stderr para no mezclarse con la de tu app."""

from __future__ import annotations

import os
import sys


class DeployError(Exception):
    """Error esperado: se muestra sin traceback y el runner sale con `code`.

    Códigos: 2 = uso/configuración, 3 = falta un requisito (uv, compilador, intérprete).
    """

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


VERBOSE = False
QUIET = False

_COLOR = sys.stderr.isatty() and "NO_COLOR" not in os.environ
if _COLOR and os.name == "nt":
    os.system("")  # activa las secuencias ANSI en la consola clásica de Windows


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
    _out(_paint("33", "aviso: ") + msg)


def error(msg: str) -> None:
    _out(_paint("31", "error: ") + msg)


def check_line(passed: bool | None, label: str, hint: str = "") -> None:
    """Una línea de `doctor`: ✓ / ✗ / – (no aplica) más una pista opcional."""
    mark = {True: _paint("32", "ok"), False: _paint("31", "XX"), None: _paint("2", "--")}[passed]
    _out(f"  [{mark}] {label}")
    if hint and passed is not True:
        for line in hint.splitlines():
            _out(_paint("2", f"         {line}"))
