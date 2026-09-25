"""Game launcher. NEVER compiled with mypyc.

A compiled module is always imported (it is never __main__), so the logic lives
in the {{pkg}} package and this file only calls it.
"""

import sys


def _main() -> int:
    from {{pkg}}.app import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    sys.exit(_main())
