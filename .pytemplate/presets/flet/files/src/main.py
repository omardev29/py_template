"""Lanzador de la app Flet. NUNCA se compila con mypyc (`flet build` también lo necesita en .py).

La UI vive en {{pkg}}.ui (interpretada) y el trabajo pesado en {{pkg}}.core (compilado).
"""


def _main() -> None:
    from {{pkg}}.ui.app import run

    run()


if __name__ == "__main__":
    import multiprocessing

    # Imprescindible con ProcessPoolExecutor en un ejecutable (PyInstaller / flet pack)
    multiprocessing.freeze_support()
    _main()
