"""Flet UI (boundary, interpreted): controls, state and ALL the handlers.

Rules for Flet and mypyc to coexist without losing performance:
- No Flet in compiled code (compile.forbid_imports checks it).
- Coarse calls into the core: one per event, with simple types. Convert the Flet values
  (float | None, str...) BEFORE the call: once compiled, the core checks the types
  at runtime and would raise TypeError.
- Heavy work in ANOTHER PROCESS (ProcessPoolExecutor): compiled code does not release
  the GIL, so in a thread it would freeze the UI just like in the event loop.
- Update the UI in one batch: change several controls and call page.update() once.
"""

from __future__ import annotations

import asyncio
import functools
import platform
import time
from concurrent.futures import ProcessPoolExecutor

import flet as ft

from {{pkg}}.core import fractal
from {{pkg}}.resources import assets_dir

WIDTH = 640
HEIGHT = 400


@functools.cache
def _executor() -> ProcessPoolExecutor:
    """One worker process, created on first use (not on import: child processes re-import it)."""
    return ProcessPoolExecutor(max_workers=1)


def _backend() -> str:
    impl = platform.python_implementation()
    return f"mypyc/{impl}" if (fractal.__file__ or "").endswith((".pyd", ".so")) else impl


async def main(page: ft.Page) -> None:
    page.title = "{{name}}"
    page.theme_mode = ft.ThemeMode.DARK

    iterations = ft.Slider(min=50, max=2000, divisions=39, value=300, label="{value} iterations", expand=True)
    image = ft.Image(src=fractal.render_png(8, 5, 1), width=WIDTH, height=HEIGHT)
    status = ft.Text(f"Core: {_backend()}. Press Draw.")

    async def draw(e: ft.Event[ft.Button]) -> None:
        button.disabled = True
        status.value = "Computing..."
        page.update()
        start = time.perf_counter()
        max_iter = int(iterations.value or 300)
        loop = asyncio.get_running_loop()
        png = await loop.run_in_executor(_executor(), fractal.render_png, WIDTH, HEIGHT, max_iter)
        image.src = png
        status.value = f"{max_iter} iterations in {time.perf_counter() - start:.2f} s (core: {_backend()})"
        button.disabled = False
        page.update()

    button = ft.Button("Draw", on_click=draw)
    page.add(ft.Row([iterations, button]), image, status)


def run() -> None:
    """Start the app (used by src/main.py and by the wheel command)."""
    ft.run(main, assets_dir=str(assets_dir()))
