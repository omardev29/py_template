"""Main loop (boundary, interpreted): window, input and draw order.

The game logic worth compiling (physics, per-entity loops: no raylib) goes in {{pkg}}.core,
which mypyc or the PyPy JIT speeds up. What touches raylib's cdata stays here and in gfx.
"""

from __future__ import annotations

import gc

import raylib as rl

from . import gfx

WIDTH = 1280
HEIGHT = 720


def main(argv: list[str] | None = None) -> int:
    """src/main.py calls it with the command line's arguments: the exit code it returns."""
    rl.SetTraceLogLevel(rl.LOG_WARNING)
    rl.InitWindow(
        WIDTH,
        HEIGHT,
        b"{{name}}",
    )
    rl.SetTargetFPS(60)
    background = gfx.color(24, 24, 32)  # created once: a cdata Color (gfx explains why)
    # PyPy: incremental GC spread across frames (avoids stutters); it does not exist on CPython
    gc_step = getattr(gc, "collect_step", None)
    while not rl.WindowShouldClose():
        rl.BeginDrawing()
        rl.ClearBackground(background)
        rl.EndDrawing()
        if gc_step is not None:
            gc_step()
    rl.CloseWindow()
    return 0
