"""Console interface (boundary module, interpreted): times the core and shows the results.

The heavy work lives in myapp.core, which is what mypyc compiles. This module
only orchestrates and renders, so it gains nothing from being compiled.
"""

import platform
import time

from rich.console import Console
from rich.table import Table

from myapp.core import bench


def _backend() -> str:
    impl = platform.python_implementation()
    if bench.__file__ and bench.__file__.endswith((".pyd", ".so")):
        return f"mypyc (compiled) on {impl}"
    return f"{impl} (interpreted)"


def main() -> int:
    t0 = time.perf_counter()
    primes = bench.count_primes(bench.SIEVE_LIMIT)
    t1 = time.perf_counter()
    number, steps = bench.collatz_max(bench.COLLATZ_LIMIT)
    t2 = time.perf_counter()

    table = Table(title=f"myapp: {_backend()}")
    table.add_column("Task")
    table.add_column("Result", justify="right")
    table.add_column("Time", justify="right")
    table.add_row(f"Primes <= {bench.SIEVE_LIMIT:,}", f"{primes:,}", f"{t1 - t0:.3f} s")
    table.add_row(
        f"Longest Collatz < {bench.COLLATZ_LIMIT:,}",
        f"{number} ({steps} steps)",
        f"{t2 - t1:.3f} s",
    )
    Console().print(table)
    return 0
