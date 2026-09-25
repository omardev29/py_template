"""Compiled core (mypyc): the Mandelbrot set as a PNG image.

It does not import Flet: it takes and returns simple types (int, float, bytes), so the UI
can call it from a handler or from another process. Standard library only.
"""

from __future__ import annotations

import struct
import zlib
from typing import Final

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"


def escape_time(cr: float, ci: float, max_iter: int) -> int:
    """Return the iterations until the point escapes (max_iter if it is in the set)."""
    zr = 0.0
    zi = 0.0
    n = 0
    while n < max_iter:
        zr2 = zr * zr
        zi2 = zi * zi
        if zr2 + zi2 > 4.0:
            return n
        zi = 2.0 * zr * zi + ci
        zr = zr2 - zi2 + cr
        n += 1
    return max_iter


def render_rgb(width: int, height: int, max_iter: int, center_x: float = -0.6, center_y: float = 0.0) -> bytes:
    """Return RGB pixels row by row, each row preceded by the PNG filter byte (0)."""
    scale = 3.0 / width
    data: list[int] = []
    for y in range(height):
        data.append(0)
        ci = center_y + (y - height / 2) * scale
        for x in range(width):
            n = escape_time(center_x + (x - width / 2) * scale, ci, max_iter)
            if n >= max_iter:
                data.append(0)
                data.append(0)
                data.append(0)
            else:
                t = n / max_iter
                data.append(int(9.0 * (1.0 - t) * t * t * t * 255.0))
                data.append(int(15.0 * (1.0 - t) * (1.0 - t) * t * t * 255.0))
                data.append(int(8.5 * (1.0 - t) * (1.0 - t) * (1.0 - t) * t * 255.0))
    return bytes(data)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def to_png(width: int, height: int, raw: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8 bits, RGB
    return PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b"")


def render_png(width: int, height: int, max_iter: int) -> bytes:
    return to_png(width, height, render_rgb(width, height, max_iter))
