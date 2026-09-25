"""Análisis estático de imports (ast), para los módulos compilados.

PyInstaller y Nuitka descubren dependencias leyendo bytecode: dentro de un .pyd de
mypyc no ven nada, así que les pasamos a mano lo que importa cada módulo compilado.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def iter_runtime_nodes(tree: ast.AST) -> list[ast.AST]:
    """Todos los nodos salvo los que están bajo `if TYPE_CHECKING:` (no existen en runtime)."""
    out: list[ast.AST] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            stack.extend(node.orelse)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def parse(path: Path) -> ast.Module:
    # En bytes: así ast respeta el BOM que añade Windows PowerShell 5.1
    return ast.parse(path.read_bytes(), filename=str(path))


def imports_of(path: Path, module: str, src: Path) -> set[str]:
    """Módulos que importa `path` (nombre de módulo `module`) en tiempo de ejecución."""
    is_pkg = path.name == "__init__.py"
    package = module if is_pkg else module.rpartition(".")[0]
    found: set[str] = set()
    for node in iter_runtime_nodes(parse(path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                base_parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join(base_parts)
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            if not target or target == "__future__":
                continue
            found.add(target)
            # `from paquete import submodulo`: si es un submódulo local, también cuenta
            for alias in node.names:
                candidate = f"{target}.{alias.name}"
                rel = candidate.replace(".", "/")
                if (src / f"{rel}.py").is_file() or (src / rel / "__init__.py").is_file():
                    found.add(candidate)
    return found


def module_name(path: Path, src: Path) -> str:
    rel = path.relative_to(src).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)
