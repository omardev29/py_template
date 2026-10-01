"""Flet app launcher. NEVER compiled with mypyc (`flet build` also needs it as .py).

The UI lives in {{pkg}}.ui (interpreted) and the heavy work in {{pkg}}.core (compiled).
"""


def _main() -> None:
    import {{pkg}}.ui.app as app

    app.run()


if __name__ == "__main__":
    import multiprocessing

    # Required with ProcessPoolExecutor in an executable (PyInstaller / flet pack)
    multiprocessing.freeze_support()
    _main()
