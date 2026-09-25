"""Interfaz de consola (módulo frontera, interpretado): mide y muestra el núcleo.

El trabajo pesado está en {{pkg}}.core, que es lo que compila mypyc. Este módulo
solo orquesta y pinta, así que no gana nada compilándose.
"""

import platform
import time

from rich.console import Console
from rich.table import Table

from {{pkg}}.core import bench


def _backend() -> str:
    impl = platform.python_implementation()
    if bench.__file__ and bench.__file__.endswith((".pyd", ".so")):
        return f"mypyc (compilado) sobre {impl}"
    return f"{impl} (interpretado)"


def main() -> int:
    t0 = time.perf_counter()
    primes = bench.count_primes(bench.SIEVE_LIMIT)
    t1 = time.perf_counter()
    number, steps = bench.collatz_max(bench.COLLATZ_LIMIT)
    t2 = time.perf_counter()

    table = Table(title=f"{{name}}: {_backend()}")
    table.add_column("Tarea")
    table.add_column("Resultado", justify="right")
    table.add_column("Tiempo", justify="right")
    table.add_row(f"Primos <= {bench.SIEVE_LIMIT:,}", f"{primes:,}", f"{t1 - t0:.3f} s")
    table.add_row(f"Collatz más largo < {bench.COLLATZ_LIMIT:,}", f"{number} ({steps} pasos)", f"{t2 - t1:.3f} s")
    Console().print(table)
    return 0
