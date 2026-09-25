"""Interfaz Flet (frontera, interpretada): controles, estado y TODOS los handlers.

Reglas para que Flet y mypyc convivan sin perder rendimiento:
- Nada de Flet en código compilado (compile.forbid_imports lo comprueba).
- Llamadas gruesas al núcleo: una por evento, con tipos simples. Convierte los valores
  de Flet (float | None, str...) ANTES de llamar: compilado, el núcleo comprueba los
  tipos en runtime y lanzaría TypeError.
- Trabajo pesado en OTRO PROCESO (ProcessPoolExecutor): el código compilado no suelta
  el GIL, así que en un hilo congelaría la interfaz igual que en el bucle de eventos.
- Actualiza la UI en bloque: cambia varios controles y llama a page.update() una vez.
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
    """Un proceso de trabajo, creado al primer uso (no al importar: lo reimportan los hijos)."""
    return ProcessPoolExecutor(max_workers=1)


def _backend() -> str:
    impl = platform.python_implementation()
    return f"mypyc/{impl}" if (fractal.__file__ or "").endswith((".pyd", ".so")) else impl


async def main(page: ft.Page) -> None:
    page.title = "{{name}}"
    page.theme_mode = ft.ThemeMode.DARK

    iterations = ft.Slider(min=50, max=2000, divisions=39, value=300, label="{value} iteraciones", expand=True)
    image = ft.Image(src=fractal.render_png(8, 5, 1), width=WIDTH, height=HEIGHT)
    status = ft.Text(f"Núcleo: {_backend()}. Pulsa Dibujar.")

    async def draw(e: ft.Event[ft.Button]) -> None:
        button.disabled = True
        status.value = "Calculando..."
        page.update()
        start = time.perf_counter()
        max_iter = int(iterations.value or 300)
        loop = asyncio.get_running_loop()
        png = await loop.run_in_executor(_executor(), fractal.render_png, WIDTH, HEIGHT, max_iter)
        image.src = png
        status.value = f"{max_iter} iteraciones en {time.perf_counter() - start:.2f} s (núcleo: {_backend()})"
        button.disabled = False
        page.update()

    button = ft.Button("Dibujar", on_click=draw)
    page.add(ft.Row([iterations, button]), image, status)


def run() -> None:
    """Arranca la app (lo usan src/main.py y el comando del wheel)."""
    ft.run(main, assets_dir=str(assets_dir()))
