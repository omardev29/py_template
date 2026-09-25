"""Lanzador del juego. NUNCA se compila con mypyc.

Un módulo compilado siempre se importa (nunca es __main__), así que la lógica vive
en el paquete {{pkg}} y este archivo solo la llama.
"""

import sys


def _main() -> int:
    from {{pkg}}.app import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(_main())
