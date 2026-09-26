"""mypyc build script. Runs INSIDE the project's .venv (needs mypy and setuptools).

Usage (called by ./deploy): python mypyc_build.py <spec.json>

We call mypycify() instead of `python -m mypyc` because the CLI does not allow
strip_asserts, group_name or multi_file, and it always writes to ./build. The C flags of
extra_cflags() are added to mypyc's own.

Exit codes: 0 ok; MYPYC_REJECTED when mypy/mypyc rejected the code (the errors are printed,
no C compiler ran); C_BUILD_FAILED when setuptools or the C compiler failed. Anything else
comes from uv (a stale uv.lock) or Python before this script ran: no compiler involved.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

MYPYC_REJECTED = 4  # mirrored by mypyc.MYPYC_REJECTED in the runner
C_BUILD_FAILED = 5  # mirrored by mypyc.C_BUILD_FAILED: only this one gets the compiler hint


def compiler_type() -> str:
    """The kind of C compiler setuptools builds with ("unix" = gcc/clang, "msvc"), found the way
    mypycify finds it for its own flags (-O3 or /O2)."""
    from distutils import ccompiler, sysconfig  # setuptools' copy (mypyc.build imported setuptools)

    compiler = ccompiler.new_compiler()
    sysconfig.customize_compiler(compiler)
    return str(compiler.compiler_type)


def extra_cflags(compiler: str, platform: str, no_semantic_interposition: bool) -> list[str]:
    """The C flags added to mypyc's own for every extension (the wheel's setup.py mirrors this).

    gcc/clang (compiler "unix"):
    - -fno-strict-overflow: Python's own CFLAGS carry it, but a CFLAGS environment variable
      REPLACES them (setuptools), and without it the wrap-around of i64/i32 arithmetic is
      undefined behaviour in C. It is kept whatever CFLAGS says; the user's flags stay (a
      repeated flag is harmless).
    - Linux, compile.no_semantic_interposition: -fno-semantic-interposition, as CPython itself
      is built. The functions of a -fPIC shared object are exported symbols that another
      library could interpose, so gcc never inlines a call between compiled functions, not even
      inside one module. Not on macOS (Apple clang: not verified) nor MSVC.
    """
    if compiler != "unix":
        return []
    flags = ["-fno-strict-overflow"]
    if no_semantic_interposition and platform == "linux":
        flags.append("-fno-semantic-interposition")
    return flags


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

    try:
        flags = extra_cflags(compiler_type(), sys.platform, spec["no_semantic_interposition"])
        for ext in extensions:
            # A new list for each: mypycify hands the SAME list object to every extension
            ext.extra_compile_args = [*ext.extra_compile_args, *flags]
        setup(
            name=spec["group"],
            ext_modules=extensions,
            script_args=[
                "--quiet",
                "build_ext",
                # Options that only reach the C compiler (opt_level) leave the C files unchanged,
                # and setuptools skips an extension newer than its sources: the runner forces it
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
    except SystemExit as exc:  # setuptools: SystemExit("error: command 'gcc' failed ...")
        if exc.code in (None, 0):
            return 0
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
        return C_BUILD_FAILED
    except Exception:  # a crash in setuptools or the compiler driver
        traceback.print_exc()
        return C_BUILD_FAILED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
