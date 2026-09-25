"""Frontera con raylib (interpretada): todo lo que toca cdata, bytes o recursos.

Reglas que hacen ir rápido al resto del juego:
- Colores y structs se crean UNA vez con ffi (cdata). Pasar tuplas como `rl.RED`
  obliga a convertirlas en cada llamada: con tuplas PyPy pierde toda su ventaja.
- raylib crudo (`import raylib as rl`), nunca pyray en bucles: pyray envuelve cada
  llamada en Python (~700 ns frente a ~100 ns).
- Texto y rutas a raylib siempre como bytes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import raylib as rl
from raylib import ffi

from {{pkg}}.resources import asset

if TYPE_CHECKING:
    from raylib import Color, Texture


def color(r: int, g: int, b: int, a: int = 255) -> Color:
    """Un Color de verdad (cdata), para crearlo una vez fuera del bucle."""
    return cast("Color", ffi.new("Color *", (r, g, b, a))[0])


def bunny_texture(size: int) -> Texture:
    """Textura generada (sin archivos): blanca, para teñirla con cualquier color."""
    image = rl.GenImageChecked(size, size, size // 4, size // 4, rl.WHITE, rl.LIGHTGRAY)
    texture = rl.LoadTextureFromImage(image)
    rl.UnloadImage(image)
    return texture


def load_texture(name: str) -> Texture:
    """Textura desde src/assets/ (ejemplo de uso de recursos)."""
    return rl.LoadTexture(str(asset(name)).encode())


def text(message: str, x: int, y: int, size: int, tint: Color) -> None:
    rl.DrawText(message.encode(), x, y, size, tint)
