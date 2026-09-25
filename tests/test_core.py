from myapp.core import bench


def test_count_primes() -> None:
    assert bench.count_primes(1) == 0
    assert bench.count_primes(10) == 4
    assert bench.count_primes(100) == 25


def test_collatz_max() -> None:
    assert bench.collatz_max(10) == (9, 20)
