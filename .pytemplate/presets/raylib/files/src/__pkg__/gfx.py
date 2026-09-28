"""raylib boundary (interpreted): everything that touches cdata, bytes or resources.

Rules that keep the rest of the game fast:
- Colors and structs are created ONCE with ffi (cdata). Passing tuples such as `rl.RED`
  forces a conversion on every call: with tuples PyPy loses all its advantage.
- Raw raylib (`import raylib as rl`), never pyray in loops: pyray wraps every
  call in Python (~700 ns versus ~100 ns).
- Text and paths always go to raylib as bytes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import raylib as rl
from raylib import ffi

from .resources import asset

if TYPE_CHECKING:
    from raylib import Color, Texture


def color(r: int, g: int, b: int, a: int = 255) -> Color:
    """Return a real Color (cdata); create it once, outside the loop."""
    return cast("Color", ffi.new("Color *", (r, g, b, a))[0])


def bunny_texture(size: int) -> Texture:
    """Return a generated texture (no files): white, so it can be tinted any color."""
    image = rl.GenImageChecked(size, size, size // 4, size // 4, rl.WHITE, rl.LIGHTGRAY)
    texture = rl.LoadTextureFromImage(image)
    rl.UnloadImage(image)
    return texture


def load_texture(name: str) -> Texture:
    """Load a texture from src/assets/ (an example of using resources)."""
    return rl.LoadTexture(str(asset(name)).encode())


def text(message: str, x: int, y: int, size: int, tint: Color) -> None:
    rl.DrawText(message.encode(), x, y, size, tint)
