"""Bucle principal (frontera, interpretado): ventana, entrada y orden de dibujo.

Lo caliente está en {{pkg}}.core: la física (world) y el bucle de dibujo por entidad
(render). Eso es lo que acelera mypyc o el JIT de PyPy.
"""

from __future__ import annotations

import gc
import platform
import sys
import time

import raylib as rl

from {{pkg}} import gfx
from {{pkg}}.core import render, world
from {{pkg}}.core.world import SPRITE, World

WIDTH = 1280
HEIGHT = 720
START_BUNNIES = 2_000
SPAWN_PER_FRAME = 100
PALETTE = [(230, 41, 55), (0, 228, 48), (0, 121, 241), (253, 249, 0), (200, 122, 255), (255, 161, 0)]


def _backend() -> str:
    impl = platform.python_implementation()
    if (world.__file__ or "").endswith((".pyd", ".so")):
        return f"mypyc/{impl}"
    return impl


def _option(argv: list[str], name: str, default: int) -> int:
    """`--frames N` sale tras N frames sin límite de FPS (medir, CI); `--bunnies N` inicial."""
    if name in argv:
        return int(argv[argv.index(name) + 1])
    return default


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    limit = _option(argv, "--frames", 0)
    bunnies = _option(argv, "--bunnies", START_BUNNIES)
    backend = _backend()
    rl.SetTraceLogLevel(rl.LOG_WARNING)
    rl.InitWindow(WIDTH, HEIGHT, b"{{name}}")
    rl.SetTargetFPS(0 if limit else 144)

    texture = gfx.bunny_texture(SPRITE)
    tints = [gfx.color(r, g, b) for r, g, b in PALETTE]
    background = gfx.color(24, 24, 32)
    panel = gfx.color(0, 0, 0, 200)
    white = gfx.color(245, 245, 245)

    game = World(WIDTH, HEIGHT)
    game.spawn(WIDTH / 2, HEIGHT / 3, bunnies, len(tints))
    # PyPy: GC incremental repartido por frame (evita tirones); no existe en CPython
    gc_step = getattr(gc, "collect_step", None)

    frames = 0
    start = time.perf_counter()
    while not rl.WindowShouldClose():
        if rl.IsMouseButtonDown(rl.MOUSE_BUTTON_LEFT):
            game.spawn(float(rl.GetMouseX()), float(rl.GetMouseY()), SPAWN_PER_FRAME, len(tints))
        game.update(min(rl.GetFrameTime(), 1 / 30))

        rl.BeginDrawing()
        rl.ClearBackground(background)
        render.draw_world(game, texture, tints)
        rl.DrawRectangle(0, 0, WIDTH, 36, panel)
        gfx.text(f"{len(game.bunnies)} conejos | {rl.GetFPS()} FPS | {backend} | clic: mas conejos", 10, 8, 20, white)
        rl.EndDrawing()

        if gc_step is not None:
            gc_step()
        frames += 1
        if limit and frames >= limit:
            break

    elapsed = time.perf_counter() - start
    rl.UnloadTexture(texture)
    rl.CloseWindow()
    print(f"{frames} frames en {elapsed:.2f} s: {frames / elapsed:.0f} FPS de media con {len(game.bunnies)} conejos ({backend})")
    return 0
