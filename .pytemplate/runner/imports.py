"""Static import analysis (ast) for the compiled modules.

PyInstaller and Nuitka discover dependencies by reading bytecode: inside a mypyc .pyd
they see nothing, so we hand them what each compiled module imports.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from .project import EXT_SUFFIXES


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def type_checking(test: ast.expr) -> bool | None:
    """True for the test `TYPE_CHECKING` (or `typing.TYPE_CHECKING`), False for `not
    TYPE_CHECKING`, None for any other: mypy reads the first as true and the second as false."""
    if _is_type_checking(test):
        return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and _is_type_checking(test.operand):
        return False
    return None


def iter_runtime_nodes(tree: ast.AST) -> list[ast.AST]:
    """Return every node except those under `if TYPE_CHECKING:` (they do not exist at runtime).

    The `else:` of such a block runs, and so does the body of `if not TYPE_CHECKING:` (whose
    `else:` does not).
    """
    out: list[ast.AST] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        out.append(node)
        checking = type_checking(node.test) if isinstance(node, ast.If) else None
        if isinstance(node, ast.If) and checking is not None:
            stack.extend(node.orelse if checking else node.body)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def parse(path: Path) -> ast.Module:
    # As bytes: that way ast honors the BOM that Windows PowerShell 5.1 adds
    return ast.parse(path.read_bytes(), filename=str(path))


# What `parse` raises for a source it cannot read: a syntax error (or syntax newer than the
# runner's Python), a NUL byte (ValueError), and a source nested too deeply for the compiler's
# own stack (RecursionError "Stack overflow during compilation": an expression of 100000
# terms; MemoryError on some Pythons). Callers catch them all: an internal error otherwise.
PARSE_ERRORS = (SyntaxError, ValueError, RecursionError, MemoryError)


def parse_error(e: Exception) -> tuple[int, str]:
    """(line, message) for a source that `parse` rejected (one of PARSE_ERRORS).

    Either a real syntax error (ruff and mypy report it too), syntax newer than the runner's
    own Python (only a runner started by hand on another Python: the launchers run it on
    python.cpython), or a source too deeply nested for the compiler's stack.
    """
    line = (e.lineno if isinstance(e, SyntaxError) else None) or 1
    msg = (e.msg if isinstance(e, SyntaxError) else str(e)) or type(e).__name__
    return line, f"cannot parse it with the runner's Python {sys.version_info.major}.{sys.version_info.minor}: {msg}"


def local_module(src: Path, name: str) -> bool:
    """Whether the dotted `name` is a module or package of the app in `src`: a .py file, a
    package folder (with or without __init__.py) or an extension file (.pyd/.so)."""
    parts = name.split(".")
    if not all(parts):
        return False
    path = src.joinpath(*parts)
    if path.is_dir() or path.with_name(f"{path.name}.py").is_file():
        return True
    folder = path.parent
    return folder.is_dir() and any(p.name.endswith(EXT_SUFFIXES) for p in folder.glob(f"{path.name}.*"))


def is_local(src: Path, name: str) -> bool:
    """Whether `name` belongs to the app: its top-level package or module is in `src`."""
    return local_module(src, name.partition(".")[0])


def _relative_base(package: str, level: int) -> str | None:
    """The package a relative import starts from, or None when it goes beyond the top-level
    package (or the module has no package): Python refuses those imports."""
    parts = package.split(".") if package else []
    if level > len(parts):
        return None
    return ".".join(parts[: len(parts) - (level - 1)])


def imports_of(path: Path, module: str, src: Path, candidates: set[str] | None = None) -> set[str]:
    """Return the modules that `path` (module name `module`) imports at runtime.

    `from X import a` imports X.a too when `a` is a submodule. For the app's own packages that
    is known here (src/); for any other X (`from html import parser`) the name `X.a` goes into
    `candidates`, when given, for the caller to resolve where the packages are installed.
    """
    is_pkg = path.name == "__init__.py"
    package = module if is_pkg else module.rpartition(".")[0]
    found: set[str] = set()
    for node in iter_runtime_nodes(parse(path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _relative_base(package, node.level)
                if base is None:
                    continue  # beyond the top-level package: Python refuses it (mypy reports it)
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            if not target or target == "__future__":
                continue
            found.add(target)
            local = is_local(src, target)
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidate = f"{target}.{alias.name}"
                if local:
                    if local_module(src, candidate):  # `from package import submodule`
                        found.add(candidate)
                elif candidates is not None and not node.level:
                    candidates.add(candidate)
    return found


def module_name(path: Path, src: Path) -> str:
    rel = path.relative_to(src).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)
