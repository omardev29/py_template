"""Pure CPU work: the ideal case for mypyc and for PyPy.

Rules for mypyc to generate fast code (./pyt report checks them):
- Constants with Final: a global without Final is looked up in a dict on every access.
- No Any: with Any, mypyc uses generic operations (it can be slower than CPython).
- Concrete types: list[bool] compiles to direct accesses; bytearray takes the generic
  path (measured: sieve with list[bool] 4.2x faster compiled, bytearray only 1.9x).
"""

from typing import Final

SIEVE_LIMIT: Final = 5_000_000
COLLATZ_LIMIT: Final = 300_000


def count_primes(limit: int) -> int:
    """Count the primes <= limit with the sieve of Eratosthenes."""
    if limit < 2:
        return 0
    sieve = [True] * (limit + 1)
    sieve[0] = False
    sieve[1] = False
    i = 2
    while i * i <= limit:
        if sieve[i]:
            j = i * i
            while j <= limit:
                sieve[j] = False
                j += i
        i += 1
    count = 0
    for is_prime in sieve:
        if is_prime:
            count += 1
    return count


def collatz_max(n: int) -> tuple[int, int]:
    """Return the number < n with the longest Collatz sequence: (number, steps)."""
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
