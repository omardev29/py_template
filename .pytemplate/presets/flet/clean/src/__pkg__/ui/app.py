"""Flet UI (boundary, interpreted): controls, state and ALL the handlers.

Rules for Flet and mypyc to coexist without losing performance:
- No Flet in compiled code (compile.forbid_imports checks it).
- Coarse calls into {{pkg}}.core: one per event, with simple types. Convert the Flet values
  (float | None, str...) BEFORE the call: once compiled, the core checks the types
  at runtime and would raise TypeError.
- Heavy work in ANOTHER PROCESS (concurrent.futures.ProcessPoolExecutor): compiled code does
  not release the GIL, so in a thread it would freeze the UI just like in the event loop.
  Where Python cannot start processes (flet build for the web, Android, iOS) run it here.
- Update the UI in one batch: change several controls and call page.update() once.
"""

from __future__ import annotations

import flet as ft

from ..resources import assets_dir


async def main(page: ft.Page) -> None:
    page.title = "{{name}}"  # fmt: skip
    page.theme_mode = ft.ThemeMode.DARK
    page.add(ft.Text("Hello from {{name}}"))  # fmt: skip


def run() -> None:
    """Start the app (used by src/main.py and by the wheel command)."""
    ft.run(main, assets_dir=str(assets_dir()))
