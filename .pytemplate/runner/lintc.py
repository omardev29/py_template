"""Extra (AST) rules for the modules that mypyc compiles.

They catch what mypy accepts but mypyc compiles badly or not at all: classes that silently
become slow Python classes, imports that do not belong in compiled code
(compile.forbid_imports, librt), nested classes, t-strings, `if __name__ == "__main__"`, and a
module-level `__file__` where mypyc runs the module body with a relative one.
"""

from __future__ import annotations

import ast
import itertools
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .config import Config, compiled_paths
from .imports import PARSE_ERRORS, iter_runtime_nodes, module_name, parse, parse_error
from .project import PYPROJECT, SRC

# The class decorators that keep a class native, by FULL name, as mypyc (2.3.1) decides it:
# irbuild/util.py DATACLASS_DECORATORS, is_trait_decorator and get_mypyc_attr_call, plus mypy's
# FINAL_DECORATOR_NAMES. Any other decorator (attrs' define/frozen/mutable included) turns the
# class into a regular Python class. test_mypyc_core checks the set against the locked mypyc.
NATIVE_CLASS_DECORATORS = frozenset(
    {
        "dataclasses.dataclass",
        "attr.s",
        "attr.attrs",
        "typing.final",
        "typing_extensions.final",
        "mypy_extensions.trait",
        "mypy_extensions.mypyc_attr",
    }
)
# The metaclasses a native class may have (irbuild/util.py is_implicit_extension_class): any
# other one, and a NamedTuple or TypedDict class, make mypyc compile the class as a regular
# Python class without a word. Every Enum has one (EnumMeta). test_mypyc_core checks the set
# against the locked mypyc.
NATIVE_METACLASSES = frozenset({"abc.ABCMeta", "typing.TypingMeta", "typing.GenericMeta"})
NON_NATIVE_BASES = {
    **{f"enum.{name}": "an Enum (metaclass EnumMeta)" for name in ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "ReprEnum")},
    **{f"{module}.NamedTuple": "a NamedTuple" for module in ("typing", "typing_extensions")},
    **{f"{module}.TypedDict": "a TypedDict" for module in ("typing", "typing_extensions", "mypy_extensions")},
}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    message: str
    note: bool = False  # never blocking, a warning under every profile (an Enum, a NamedTuple...)

    def __str__(self) -> str:
        return f"{self.path.relative_to(SRC.parent).as_posix()}:{self.line}: {self.message}"


def _decorator_name(node: ast.expr) -> str:
    """The dotted name as written (`attrs.define` for `@attrs.define(...)`); "" for anything else."""
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if not isinstance(target, ast.Name):
        return ""
    parts.append(target.id)
    return ".".join(reversed(parts))


def _add_import(aliases: dict[str, str], node: ast.Import | ast.ImportFrom) -> None:
    """Record the names an absolute import binds: local name -> full dotted name.

    Relative imports stay unresolved: a local `final` or `dataclass` is not the real one.
    """
    if isinstance(node, ast.Import):
        for a in node.names:
            if a.asname:
                aliases[a.asname] = a.name
            else:
                top = a.name.partition(".")[0]
                aliases[top] = top
    elif not node.level and node.module:
        for a in node.names:
            if a.name == "*":  # mypy resolves star imports too
                for full in (*NATIVE_CLASS_DECORATORS, *NATIVE_METACLASSES, *NON_NATIVE_BASES):
                    module, _, name = full.rpartition(".")
                    if module == node.module:
                        aliases.setdefault(name, full)
            else:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"


def _scope_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """The statements that run in the scope of `body`, in source order: those in its if/try/with/
    for/while/match blocks too, never those of a nested function or class body. Iterative: an
    elif chain nests one `orelse` per branch, and 1000 of them (a generated dispatch table)
    passed Python's recursion limit."""
    stack: list[Iterator[ast.stmt]] = [iter(body)]
    while stack:
        stmt = next(stack[-1], None)
        if stmt is None:
            stack.pop()
            continue
        yield stmt
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        blocks = [inner for name in ("body", "orelse", "finalbody") if isinstance(inner := getattr(stmt, name, None), list)]
        blocks += [block.body for block in (*getattr(stmt, "handlers", ()), *getattr(stmt, "cases", ()))]
        stack.append(itertools.chain.from_iterable(blocks))


def _import_aliases(tree: ast.Module) -> dict[ast.ClassDef, dict[str, str]]:
    """Each class -> the names its decorators resolve through (what mypy resolves): the absolute
    imports of the scope the class statement runs in over those of the scopes around it. A
    function's own import never decides a module-level decorator."""
    out: dict[ast.ClassDef, dict[str, str]] = {}
    stack: list[tuple[list[ast.stmt], dict[str, str]]] = [(tree.body, {})]
    while stack:
        body, outer = stack.pop()
        aliases = dict(outer)
        statements = list(_scope_statements(body))
        for stmt in statements:
            if isinstance(stmt, ast.Import | ast.ImportFrom):
                _add_import(aliases, stmt)
        for stmt in statements:
            if isinstance(stmt, ast.ClassDef):
                out[stmt] = aliases
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                stack.append((stmt.body, aliases))
    return out


def _full_name(written: str, aliases: dict[str, str]) -> str:
    head, dot, rest = written.partition(".")
    return aliases.get(head, head) + dot + rest


def _is_explicitly_non_native(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and _decorator_name(node).rpartition(".")[2] == "mypyc_attr"
        and any(k.arg == "native_class" and isinstance(k.value, ast.Constant) and k.value.value is False for k in node.keywords)
    )


def _non_native_kind(node: ast.ClassDef, aliases: dict[str, str], local: dict[str, str]) -> str | None:
    """What makes mypyc compile the class as a regular Python class through its metaclass or its
    bases (NATIVE_METACLASSES, NON_NATIVE_BASES), None when nothing does. `local`: the classes of
    the module seen so far whose subclasses are such a class too (a metaclass is inherited, and
    a subclass of a TypedDict is one; a subclass of a NamedTuple is a mypyc error of its own).
    A base imported from another module is not looked into."""
    for keyword in node.keywords:
        if keyword.arg == "metaclass":
            written = _decorator_name(keyword.value)
            if not written or _full_name(written, aliases) not in NATIVE_METACLASSES:
                return f"has the metaclass {written or '<expression>'}"
    for base in node.bases:
        written = _decorator_name(base.value if isinstance(base, ast.Subscript) else base)
        full = _full_name(written, aliases)
        if full in NON_NATIVE_BASES:
            return f"is {NON_NATIVE_BASES[full]}"
        if written in local:
            return f"inherits from '{written}', which {local[written]}"
    return None


def _non_native_kinds(tree: ast.Module, aliases: dict[ast.ClassDef, dict[str, str]]) -> dict[ast.ClassDef, str]:
    """_non_native_kind of every class, the module's own classes read in source order."""
    kinds: dict[ast.ClassDef, str] = {}
    local: dict[str, str] = {}
    for stmt in _scope_statements(tree.body):
        if isinstance(stmt, ast.ClassDef):
            kind = _non_native_kind(stmt, aliases.get(stmt, {}), local)
            if kind:
                kinds[stmt] = kind
                if kind != "is a NamedTuple":  # a subclass says what its base class is
                    local[stmt.name] = kind.split("', which ", 1)[1] if kind.startswith("inherits from '") else kind
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node not in kinds:
            kind = _non_native_kind(node, aliases.get(node, {}), local)
            if kind:
                kinds[node] = kind
    return kinds


def _within(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _own_classes(scope: ast.AST) -> Iterator[ast.ClassDef]:
    """Classes whose nearest enclosing class or function is `scope` (each class is reported once)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.ClassDef):
            yield node
        elif not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            stack.extend(ast.iter_child_nodes(node))


def _is_main_check(test: ast.expr) -> bool:
    """`__name__ == "__main__"`, in either order."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = (test.left, test.comparators[0])
    return any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides) and any(
        isinstance(s, ast.Constant) and s.value == "__main__" for s in sides
    )


def _import_time_nodes(stmt: ast.stmt) -> Iterator[ast.AST]:
    """The nodes of a module-level statement that run while the module is imported: class
    bodies, decorators and default values do; function and lambda bodies do not."""
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            args = node.args
            stack += [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
            if not isinstance(node, ast.Lambda):
                stack += node.decorator_list
        else:
            stack.extend(ast.iter_child_nodes(node))


def relative_file_at_import(cfg: Config) -> bool:
    """Whether mypyc runs the compiled module bodies with a RELATIVE `__file__`.

    mypyc (>= 1.20.2) sets `__file__` before a module body runs, from the folder of the shared
    lib `<group>__mypyc`. It builds no shared lib when it compiles exactly one top-level module:
    that body then sees the bare `<mod><EXT_SUFFIX>`, which resolves against the cwd. Functions
    always see the real path. Pinned by test_mypyc_core against the locked mypyc.

    With the [compile] rules of config.validate (an exclude is always inside an entry), that is
    exactly one compile.modules entry that is a top-level module file, the one Python imports
    (config.compiled_paths: a leftover folder without __init__.py never hides it).
    """
    paths = compiled_paths(cfg)
    return len(paths) == 1 and "/" not in paths[0] and paths[0].endswith(".py")


def _runtime_dependencies() -> set[str]:
    """Normalised names in pyproject.toml [project] dependencies (builds install no dev group)."""
    try:
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return set()
    project = data.get("project")
    deps = project.get("dependencies") if isinstance(project, dict) else None
    names: set[str] = set()
    for dep in deps if isinstance(deps, list) else []:
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", dep) if isinstance(dep, str) else None
        if m:
            names.add(re.sub(r"[-_.]+", "-", m.group(1)).lower())
    return names


def lint_file(cfg: Config, path: Path) -> list[Finding]:
    findings: list[Finding] = []

    def add(node: ast.AST, msg: str, note: bool = False) -> None:
        finding = Finding(path, getattr(node, "lineno", 1), msg, note)
        if finding not in findings:
            findings.append(finding)

    try:
        tree = parse(path)
    except PARSE_ERRORS as e:  # ruff and mypy report a real syntax error
        line, msg = parse_error(e)
        return [Finding(path, line, f"{msg} (the mypyc rules skipped this file)")]

    aliases = _import_aliases(tree)
    kinds = _non_native_kinds(tree, aliases)
    for node in iter_runtime_nodes(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif node.level or not node.module:
                names = []  # a relative import: a module of the app, not a library
            else:
                # `from a import b` also imports the submodule a.b (forbid_imports = ["a.b"])
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names if a.name != "*")]
            for banned in cfg.compile.forbid_imports:
                hit = next((n for n in names if _within(n, banned)), None)
                if hit:
                    add(node, f"import of '{hit}' is forbidden in compiled code (compile.forbid_imports): move it to a boundary module")
            if any(_within(n, "librt") for n in names):
                if cfg.pypy_enabled:
                    add(node, "librt does not exist on PyPy: do not use it while PyPy is in backend.supported")
                elif "librt" not in _runtime_dependencies():
                    add(
                        node,
                        "librt is only installed with mypy (dev group), so pyz/portable/wheel builds lack it: "
                        "add it to the app with ./pyt add librt --cpython-only",
                    )
        elif isinstance(node, ast.ClassDef):
            decorators = node.decorator_list
            if not any(_is_explicitly_non_native(d) for d in decorators):
                written = [_decorator_name(d) for d in decorators]
                scope = aliases.get(node, {})
                bad = [w or "<expression>" for w in written if _full_name(w, scope) not in NATIVE_CLASS_DECORATORS]
                why = f"uses @{bad[0]}" if bad else kinds.get(node)
                if why and not bad and "has the metaclass" not in why:
                    # an Enum, a NamedTuple, a TypedDict (or a subclass of one): standard idioms that
                    # work compiled, only slower; a note, where a decorator or a metaclass of the
                    # user's own is the surprise the rule is for
                    add(
                        node,
                        f"class '{node.name}' {why}: mypyc compiles it as a regular (slow) Python class, which "
                        "works (a note, never blocking; @mypyc_attr(native_class=False) silences it)",
                        note=True,
                    )
                elif why:
                    add(
                        node,
                        f"class '{node.name}' {why}: mypyc compiles it as a regular (slow) Python class. "
                        "Move it to a boundary module, or mark it @mypyc_attr(native_class=False) if intended",
                    )
            for inner in _own_classes(node):
                add(inner, f"nested class '{inner.name}': mypyc does not support it; move it to module level")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in _own_classes(node):
                add(inner, f"class '{inner.name}' defined inside a function: mypyc does not support it")
        elif hasattr(ast, "TemplateStr") and isinstance(node, ast.TemplateStr):  # 3.14+
            add(node, "t-strings: mypyc does not support them")

    relative_file = relative_file_at_import(cfg)
    for stmt in tree.body:
        if isinstance(stmt, ast.If) and _is_main_check(stmt.test):
            add(stmt, "`if __name__ == \"__main__\"` never runs in a compiled module: put it in src/main.py")
        if not relative_file:
            continue
        for child in _import_time_nodes(stmt):
            if isinstance(child, ast.Name) and child.id == "__file__":
                add(
                    child,
                    "`__file__` at module level is a relative path when mypyc compiles a single top-level "
                    "module (no shared lib): use it inside a function",
                )
    return findings


def lint(cfg: Config, files: list[Path]) -> list[Finding]:
    out: list[Finding] = []
    for path in files:
        out += lint_file(cfg, path)
    return sorted(out, key=lambda f: (str(f.path), f.line))


def describe(files: list[Path]) -> str:
    return ", ".join(module_name(p, SRC) for p in files)
