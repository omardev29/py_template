import sys
import time
from typing import Final

from rich.console import Console
from rich.table import Table

LIMIT: Final = 5_000_000


def count_primes(limit: int) -> int:
    """Criba de Eratóstenes: trabajo CPU puro, ideal para mypyc."""
    sieve = bytearray([1]) * (limit + 1)
    sieve[0] = 0
    sieve[1] = 0
    i = 2
    while i * i <= limit:
        if sieve[i]:
            j = i * i
            while j <= limit:
                sieve[j] = 0
                j += i
        i += 1
    # Nota: `for v in sieve` (iterar un bytearray) provoca un error interno en mypyc 2.3.1
    return sieve.count(1)


def collatz_max(n: int) -> tuple[int, int]:
    best_n = 1
    best_len = 1
    for start in range(1, n):
        x = start
        steps = 1
        while x != 1:
            x = x // 2 if x % 2 == 0 else 3 * x + 1
            steps += 1
        if steps > best_len:
            best_len = steps
            best_n = start
    return best_n, best_len


def main() -> int:
    console = Console()
    compiled = __file__.endswith((".so", ".pyd"))

    t0 = time.perf_counter()
    primes = count_primes(LIMIT)
    t1 = time.perf_counter()
    cn, cl = collatz_max(300_000)
    t2 = time.perf_counter()

    table = Table(title=f"miapp — {'compilado (mypyc)' if compiled else 'interpretado'}")
    table.add_column("Tarea")
    table.add_column("Resultado", justify="right")
    table.add_column("Tiempo", justify="right")
    table.add_row(f"Primos ≤ {LIMIT:,}", f"{primes:,}", f"{t1 - t0:.3f} s")
    table.add_row("Collatz más largo < 300k", f"{cn} ({cl} pasos)", f"{t2 - t1:.3f} s")
    console.print(table)
    return 0
