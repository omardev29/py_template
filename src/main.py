"""App launcher. NEVER compiled with mypyc.

A compiled module is always imported (it is never __main__), so all the logic
lives in the myapp package and this file only calls it.
"""

import sys


def _main() -> int:
    from myapp.app import main

    return main()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # needed if you use processes in an executable
    sys.exit(_main())
