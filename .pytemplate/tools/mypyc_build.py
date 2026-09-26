"""mypyc build script. Runs INSIDE the project's .venv (needs mypy and setuptools).

Usage (called by ./deploy): python mypyc_build.py <spec.json>

We call mypycify() instead of `python -m mypyc` because the CLI does not allow
strip_asserts, group_name or multi_file, and it always writes to ./build.

Exit codes: 0 ok; MYPYC_REJECTED when mypy/mypyc rejected the code (the errors are printed,
no C compiler ran); anything else is a failure of the C build (setuptools / the compiler).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

MYPYC_REJECTED = 4  # mirrored by mypyc.MYPYC_REJECTED in the runner


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    os.chdir(spec["stage"])

    from mypyc.build import mypycify

    args = ["--config-file", spec["config"], "--cache-dir", spec["cache_dir"]]
    if spec.get("annotate"):
        args += ["-a", spec["annotate"]]
    args += spec["files"]
    try:
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
    except SystemExit as exc:  # mypy/mypyc printed the errors, then sys.exit(1) or sys.exit("message")
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
        return MYPYC_REJECTED
    except Exception:  # a crash inside mypyc: not a problem of the C compiler either
        traceback.print_exc()
        return MYPYC_REJECTED
    if not spec.get("compile", True):
        return 0

    from setuptools import setup

    setup(
        name=spec["group"],
        ext_modules=extensions,
        script_args=[
            "--quiet",
            "build_ext",
            # Options that only reach the C compiler (opt_level) leave the C files unchanged, and
            # setuptools skips an extension that is newer than its sources: the runner forces it
            *(["--force"] if spec.get("force") else []),
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
