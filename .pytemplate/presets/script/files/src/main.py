"""Lanzador de la app. NUNCA se compila con mypyc.

Un módulo compilado siempre se importa (nunca es __main__), así que toda la lógica
vive en el paquete {{pkg}} y este archivo solo la llama.
"""

import sys


def _main() -> int:
    from {{pkg}}.app import main

    return main()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # necesario si usas procesos en un ejecutable
    sys.exit(_main())
