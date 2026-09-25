"""Reglas extra (AST) para los módulos que compila mypyc.

Detectan cosas que mypy acepta pero que con mypyc hacen el código lento o lo rompen
en silencio: clases que pasan a ser "no nativas", imports de librerías de UI en el
núcleo compilado, `__file__` a nivel de módulo, etc.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

from . import presets
from .config import Config
from .imports import iter_runtime_nodes, module_name, parse
from .project import PRESETS, SRC

# Decoradores de clase que mantienen la clase nativa (el resto la vuelve una clase Python lenta)
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


def _forbidden_calls(cfg: Config) -> dict[str, str]:
    """Funciones que un preset prohíbe llamar desde código compilado (p. ej. stubs que mienten)."""
    try:
        data = presets.load(cfg.app.preset)
    except Exception:
        return {}
    spec = data.get("lint", {})
    out: dict[str, str] = {}
    source = spec.get("forbid_calls_file")
    if source:
        path = PRESETS / cfg.app.preset / source
        if path.is_file():
            listing = json.loads(path.read_text(encoding="utf-8"))
            reason = str(listing.get("reason", "prohibido en código compilado"))
            for name in listing.get("functions", []):
                out[str(name)] = reason
    return out


def lint_file(cfg: Config, path: Path, forbidden_calls: dict[str, str]) -> list[Finding]:
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
                        add(node, f"import de '{name}' prohibido en código compilado (compile.forbid_imports): muévelo a un módulo frontera")
                if cfg.pypy_enabled and (name == "librt" or name.startswith("librt.")):
                    add(node, "librt no existe en PyPy: no lo uses si PyPy está en backend.supported")
        elif isinstance(node, ast.ClassDef):
            decorators = node.decorator_list
            if not any(_is_explicitly_non_native(d) for d in decorators):
                bad = [n for n in (_decorator_name(d) for d in decorators) if n not in NATIVE_CLASS_DECORATORS]
                if bad:
                    add(
                        node,
                        f"la clase '{node.name}' usa @{bad[0]}: mypyc la compila como clase Python normal (lenta). "
                        "Muévela a un módulo frontera, o márcala @mypyc_attr(native_class=False) si es intencionado",
                    )
            for child in ast.walk(node):
                if child is not node and isinstance(child, ast.ClassDef):
                    add(child, f"clase anidada '{child.name}': mypyc no la admite; sácala al nivel del módulo")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                if isinstance(child, ast.ClassDef):
                    add(child, f"clase '{child.name}' definida dentro de una función: mypyc no la admite")
        elif isinstance(node, ast.Call) and forbidden_calls:
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if callee in forbidden_calls:
                add(node, f"{callee}(): {forbidden_calls[callee]}")
        elif hasattr(ast, "TemplateStr") and isinstance(node, ast.TemplateStr):  # 3.14+
            add(node, "t-strings: mypyc no las admite")

    for stmt in tree.body:
        if isinstance(stmt, ast.If):
            test = stmt.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
            ):
                add(stmt, "`if __name__ == \"__main__\"` nunca se ejecuta en un módulo compilado: ponlo en src/main.py")
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for child in ast.walk(stmt):
            if isinstance(child, ast.Name) and child.id == "__file__":
                add(child, "`__file__` a nivel de módulo no funciona compilado (mypyc#700): úsalo dentro de una función")
                break
    return findings


def lint(cfg: Config, files: list[Path]) -> list[Finding]:
    forbidden = _forbidden_calls(cfg)
    out: list[Finding] = []
    for path in files:
        out += lint_file(cfg, path, forbidden)
    return sorted(out, key=lambda f: (str(f.path), f.line))


def describe(files: list[Path]) -> str:
    return ", ".join(module_name(p, SRC) for p in files)
