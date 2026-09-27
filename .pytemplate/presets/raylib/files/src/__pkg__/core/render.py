"""Per-entity draw loop: one raylib call per bunny (compiled with mypyc).

`_draw_texture: Final = rl.DrawTexture`: with the Final alias, mypyc calls the function
directly instead of looking up `rl.DrawTexture` on every iteration. It is valid without
Any thanks to the fixed stub in typings/raylib (./pyt stubs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import raylib as rl

from .world import World

if TYPE_CHECKING:
    # These only exist in the stub: at runtime the raylib structs are cdata
    from raylib import Color, Texture

_draw_texture: Final = rl.DrawTexture


def draw_world(world: World, texture: Texture, tints: list[Color]) -> None:
    for b in world.bunnies:
        _draw_texture(texture, int(b.x), int(b.y), tints[b.tint])
