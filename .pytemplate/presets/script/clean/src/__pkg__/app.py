"""The app (boundary module, interpreted): input, output and the user interface.

The code worth compiling (pure computation, strictly typed, no Any) goes in {{pkg}}.core:
mypyc compiles every module there (compile.modules in pytemplate.toml).
"""


def main() -> int:
    """src/main.py calls it: its return value is the exit code."""
    print("Hello from {{name}}")  # fmt: skip
    return 0
