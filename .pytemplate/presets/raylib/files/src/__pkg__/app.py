"""Main loop (boundary, interpreted): window, input and draw order.

The hot code lives in {{pkg}}.core: the physics (world) and the per-entity draw loop
(render). That is what mypyc or the PyPy JIT speeds up.
"""

from __future__ import annotations

import argparse
import gc
import platform
import sys
import time

import raylib as rl

from . import gfx
from .core import render, world
from .core.world import SPRITE, World

WIDTH = 1280
HEIGHT = 720
START_BUNNIES = 2_000
SPAWN_PER_FRAME = 100
PALETTE = [
    (230, 41, 55),
    (0, 228, 48),
    (0, 121, 241),
    (253, 249, 0),
    (200, 122, 255),
    (255, 161, 0),
]


def _backend() -> str:
    impl = platform.python_implementation()
    if (world.__file__ or "").endswith((".pyd", ".so")):
        return f"mypyc/{impl}"
    return impl


def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more: {value}")
    return value


def _options(argv: list[str]) -> tuple[int, int]:
    """(frames, bunnies): `--frames N` quits after N frames, without the FPS cap (benchmark,
    CI); `--bunnies N` is the start count. A wrong value is a usage error (exit 2)."""
    parser = argparse.ArgumentParser(
        prog="{{name}}",
        description="Bunnymark: click to add bunnies.",
    )
    parser.add_argument(
        "--frames", type=_count, default=0, metavar="N", help="quit after N frames, no FPS cap"
    )
    parser.add_argument(
        "--bunnies", type=_count, default=START_BUNNIES, metavar="N", help="bunnies at the start"
    )
    options = parser.parse_args(argv)
    return int(options.frames), int(options.bunnies)


def main(argv: list[str] | None = None) -> int:
    limit, bunnies = _options(sys.argv[1:] if argv is None else argv)
    backend = _backend()
    rl.SetTraceLogLevel(rl.LOG_WARNING)
    rl.InitWindow(
        WIDTH,
        HEIGHT,
        b"{{name}}",
    )
    rl.SetTargetFPS(0 if limit else 144)

    texture = gfx.bunny_texture(SPRITE)
    tints = [gfx.color(r, g, b) for r, g, b in PALETTE]
    background = gfx.color(24, 24, 32)
    panel = gfx.color(0, 0, 0, 200)
    white = gfx.color(245, 245, 245)

    game = World(WIDTH, HEIGHT)
    game.spawn(WIDTH / 2, HEIGHT / 3, bunnies, len(tints))
    # PyPy: incremental GC spread across frames (avoids stutters); it does not exist on CPython
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
        hud = f"{len(game.bunnies)} bunnies | {rl.GetFPS()} FPS | {backend} | click: more bunnies"
        gfx.text(hud, 10, 8, 20, white)
        rl.EndDrawing()

        if gc_step is not None:
            gc_step()
        frames += 1
        if limit and frames >= limit:
            break

    elapsed = time.perf_counter() - start
    rl.UnloadTexture(texture)
    rl.CloseWindow()
    print(
        f"{frames} frames in {elapsed:.2f} s: {frames / elapsed:.0f} FPS average "
        f"with {len(game.bunnies)} bunnies ({backend})"
    )
    return 0
