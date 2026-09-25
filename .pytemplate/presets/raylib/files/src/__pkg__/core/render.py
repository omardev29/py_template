"""Bucle de dibujo por entidad: una llamada a raylib por conejo (compilado con mypyc).

`_draw_texture: Final = rl.DrawTexture`: con el alias Final, mypyc llama directamente
a la función en lugar de buscar `rl.DrawTexture` en cada iteración. Es válido sin Any
gracias al stub corregido de typings/raylib (./deploy stubs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import raylib as rl

from .world import World

if TYPE_CHECKING:
    # Solo existen en el stub: en runtime los structs de raylib son cdata
    from raylib import Color, Texture

_draw_texture: Final = rl.DrawTexture


def draw_world(world: World, texture: Texture, tints: list[Color]) -> None:
    for b in world.bunnies:
        _draw_texture(texture, int(b.x), int(b.y), tints[b.tint])
