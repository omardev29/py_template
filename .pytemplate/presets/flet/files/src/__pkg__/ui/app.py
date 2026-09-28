"""Flet UI (boundary, interpreted): controls, state and ALL the handlers.

Rules for Flet and mypyc to coexist without losing performance:
- No Flet in compiled code (compile.forbid_imports checks it).
- Coarse calls into the core: one per event, with simple types. Convert the Flet values
  (float | None, str...) BEFORE the call: once compiled, the core checks the types
  at runtime and would raise TypeError.
- Heavy work in ANOTHER PROCESS (ProcessPoolExecutor): compiled code does not release
  the GIL, so in a thread it would freeze the UI just like in the event loop. Where Python
  cannot start processes (flet build for the web, Android, iOS) it runs here instead.
- Update the UI in one batch: change several controls and call page.update() once.
"""

from __future__ import annotations

import asyncio
import functools
import platform
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import flet as ft

from {{pkg}}.core import fractal
from {{pkg}}.resources import assets_dir

WIDTH = 640
HEIGHT = 400


# Where Python cannot start processes: WebAssembly (Pyodide) and the mobile platforms
NO_PROCESSES = ("emscripten", "wasi", "android", "ios")


@functools.cache
def _executor() -> ProcessPoolExecutor | None:
    """One worker process, created on first use (not on import: child processes re-import it).
    None where no process can be started: the work then runs in the event loop."""
    if sys.platform in NO_PROCESSES:
        return None
    try:
        return ProcessPoolExecutor(max_workers=1)
    except NotImplementedError:  # a Python without working multiprocessing (named semaphores)
        return None
    except OSError:  # named semaphores that fail when made: no writable /dev/shm (a container)
        return None


async def _render_png(width: int, height: int, max_iter: int) -> bytes:
    if _executor() is None:
        return fractal.render_png(width, height, max_iter)  # the UI waits meanwhile
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor(), fractal.render_png, width, height, max_iter)


def _backend() -> str:
    impl = platform.python_implementation()
    return f"mypyc/{impl}" if (fractal.__file__ or "").endswith((".pyd", ".so")) else impl


async def main(page: ft.Page) -> None:
    page.title = "{{name}}"
    page.theme_mode = ft.ThemeMode.DARK

    iterations = ft.Slider(
        min=50, max=2000, divisions=39, value=300, label="{value} iterations", expand=True
    )
    image = ft.Image(src=fractal.render_png(8, 5, 1), width=WIDTH, height=HEIGHT)
    status = ft.Text(f"Core: {_backend()}. Press Draw.")

    async def draw(e: ft.Event[ft.Button]) -> None:
        button.disabled = True
        status.value = "Computing..."
        page.update()
        start = time.perf_counter()
        max_iter = int(iterations.value or 300)
        done = False
        try:
            image.src = await _render_png(WIDTH, HEIGHT, max_iter)
            done = True
        finally:  # the button comes back whatever happened
            elapsed = time.perf_counter() - start
            status.value = (
                f"{max_iter} iterations in {elapsed:.2f} s (core: {_backend()})"
                if done
                else "Draw failed (see the console)"
            )
            button.disabled = False
            page.update()

    button = ft.Button("Draw", on_click=draw)
    page.add(ft.Row([iterations, button]), image, status)


def run() -> None:
    """Start the app (used by src/main.py and by the wheel command)."""
    ft.run(main, assets_dir=str(assets_dir()))
