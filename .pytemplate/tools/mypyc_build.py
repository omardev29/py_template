"""Compila con mypyc. Se ejecuta DENTRO del .venv del proyecto (necesita mypy y setuptools).

Uso (lo llama ./deploy): python mypyc_build.py <spec.json>

Llamamos a mypycify() en lugar de `python -m mypyc` porque la CLI no permite
strip_asserts, group_name ni multi_file, y siempre escribe en ./build.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    os.chdir(spec["stage"])

    from mypyc.build import mypycify

    args = ["--config-file", spec["config"], "--cache-dir", spec["cache_dir"]]
    if spec.get("annotate"):
        args += ["-a", spec["annotate"]]
    args += spec["files"]
    extensions = mypycify(
        args,
        opt_level=spec["opt_level"],
        debug_level=spec["debug_level"],
        strip_asserts=spec["strip_asserts"],
        multi_file=spec["multi_file"],
        separate=spec["separate"],
        strict_dunder_typing=spec["strict_dunder_typing"],
        group_name=None if spec["separate"] else spec["group"],
        target_dir=spec["c_dir"],
    )
    if not spec.get("compile", True):
        return 0

    from setuptools import setup

    setup(
        name=spec["group"],
        ext_modules=extensions,
        script_args=[
            "--quiet",
            "build_ext",
            "--inplace",
            "--build-temp",
            spec["build_temp"],
            "--build-lib",
            spec["build_lib"],
            "--parallel",
            str(os.cpu_count() or 1),
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
