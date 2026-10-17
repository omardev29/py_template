import {{pkg}}.core.logic as logic


def test_the_core_imports() -> None:
    # ./pyt test mypyc runs the tests against the compiled modules (tests/conftest.py checks it)
    assert logic.__name__.endswith(".core.logic")
