"""Extra (AST) rules for the modules that mypyc compiles.

They catch things mypy accepts but that, under mypyc, make the code slow or silently
break it: classes that become "non-native", imports of UI libraries in the
compiled core, `__file__` at module level, etc.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .imports import iter_runtime_nodes, module_name, parse
from .project import SRC

# Class decorators that keep the class native (any other one turns it into a slow Python class)
NATIVE_CLASS_DECORATORS = {"dataclass", "mypyc_attr", "trait", "final", "define", "frozen", "mutable", "attrs"}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path.relative_to(SRC.parent).as_posix()}:{self.line}: {self.message}"


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _is_explicitly_non_native(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and _decorator_name(node) == "mypyc_attr"
        and any(k.arg == "native_class" and isinstance(k.value, ast.Constant) and k.value.value is False for k in node.keywords)
    )


def lint_file(cfg: Config, path: Path, *_compat: object) -> list[Finding]:
    # *_compat: the removed `forbidden_calls` argument (the preset [lint] forbid_calls_file
    # support was dead code). Drop it once no caller passes it (test_runner.py still does).
    findings: list[Finding] = []
    tree = parse(path)

    def add(node: ast.AST, msg: str) -> None:
        findings.append(Finding(path, getattr(node, "lineno", 1), msg))

    for node in iter_runtime_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                for banned in cfg.compile.forbid_imports:
                    if name == banned or name.startswith(banned + "."):
                        add(node, f"import of '{name}' is forbidden in compiled code (compile.forbid_imports): move it to a boundary module")
                if cfg.pypy_enabled and (name == "librt" or name.startswith("librt.")):
                    add(node, "librt does not exist on PyPy: do not use it while PyPy is in backend.supported")
        elif isinstance(node, ast.ClassDef):
            decorators = node.decorator_list
            if not any(_is_explicitly_non_native(d) for d in decorators):
                bad = [n for n in (_decorator_name(d) for d in decorators) if n not in NATIVE_CLASS_DECORATORS]
                if bad:
                    add(
                        node,
                        f"class '{node.name}' uses @{bad[0]}: mypyc compiles it as a regular (slow) Python class. "
                        "Move it to a boundary module, or mark it @mypyc_attr(native_class=False) if intended",
                    )
            for child in ast.walk(node):
                if child is not node and isinstance(child, ast.ClassDef):
                    add(child, f"nested class '{child.name}': mypyc does not support it; move it to module level")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                if isinstance(child, ast.ClassDef):
                    add(child, f"class '{child.name}' defined inside a function: mypyc does not support it")
        elif hasattr(ast, "TemplateStr") and isinstance(node, ast.TemplateStr):  # 3.14+
            add(node, "t-strings: mypyc does not support them")

    for stmt in tree.body:
        if isinstance(stmt, ast.If):
            test = stmt.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            ):
                add(stmt, "`if __name__ == \"__main__\"` never runs in a compiled module: put it in src/main.py")
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for child in ast.walk(stmt):
            if isinstance(child, ast.Name) and child.id == "__file__":
                add(child, "`__file__` at module level does not work when compiled (mypyc#700): use it inside a function")
                break
    return findings


def lint(cfg: Config, files: list[Path]) -> list[Finding]:
    out: list[Finding] = []
    for path in files:
        out += lint_file(cfg, path)
    return sorted(out, key=lambda f: (str(f.path), f.line))


def describe(files: list[Path]) -> str:
    return ", ".join(module_name(p, SRC) for p in files)
