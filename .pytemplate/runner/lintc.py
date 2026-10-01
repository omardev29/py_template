"""Extra (AST) rules for the modules that mypyc compiles.

They catch what mypy accepts but mypyc compiles badly or not at all: classes that silently
become slow Python classes, imports that do not belong in compiled code
(compile.forbid_imports, librt), nested classes, t-strings, `if __name__ == "__main__"`, and a
module-level `__file__` where mypyc runs the module body with a relative one.
"""

from __future__ import annotations

import ast
import itertools
import operator
import re
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .config import TOML_ERRORS, Config, compiled_paths
from .imports import PARSE_ERRORS, _relative_base, module_name, parse, parse_error
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


# --- what mypy reads as unreachable, which mypyc never compiles ---------------------------------

# The comparisons mypy evaluates in a `sys.version_info` test (mypy/reachability.py)
_COMPARE: dict[type[ast.cmpop], str] = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
_SWAPPED = {"==": "==", "!=": "!=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
_OPERATORS: dict[str, Callable[[tuple[int, ...], tuple[int, ...]], bool]] = {
    "==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}  # fmt: skip


def _int(node: ast.expr | None) -> int | None:
    return node.value if isinstance(node, ast.Constant) and type(node.value) is int else None


def _version_info_part(node: ast.expr) -> int | tuple[int | None, int | None] | None:
    """mypy's contains_sys_version_info: `sys.version_info` ((None, None): all of it),
    `sys.version_info[i]` (i) or `sys.version_info[lo:hi]` ((lo, hi)); None for anything else."""

    def is_version_info(expr: ast.expr) -> bool:
        return isinstance(expr, ast.Attribute) and expr.attr == "version_info" and isinstance(expr.value, ast.Name) and expr.value.id == "sys"

    if is_version_info(node):
        return (None, None)
    if not (isinstance(node, ast.Subscript) and is_version_info(node.value)):
        return None
    index = node.slice
    if not isinstance(index, ast.Slice):
        return _int(index)
    if index.step is not None and _int(index.step) != 1:
        return None
    bounds = [None if b is None else _int(b) for b in (index.lower, index.upper)]
    if any(b is None and given is not None for b, given in zip(bounds, (index.lower, index.upper), strict=True)):
        return None  # a bound that is no int literal
    return (bounds[0], bounds[1])


def _ints(node: ast.expr) -> int | tuple[int, ...] | None:
    if _int(node) is not None:
        return _int(node)
    if isinstance(node, ast.Tuple):
        items = [_int(x) for x in node.elts]
        return tuple(i for i in items if i is not None) if all(i is not None for i in items) else None
    return None


def _version_value(test: ast.expr, version: tuple[int, int]) -> bool | None:
    """mypy's value of a `sys.version_info` comparison on Python `version` (its
    consider_sys_version_info): `sys.version_info[i] <op> int` (i 0 or 1),
    `sys.version_info[:n] <op> tuple` and `sys.version_info <op> tuple`, either way round; None for
    any other test (a chained comparison, a tuple longer than what it reads)."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return None
    op = _COMPARE.get(type(test.ops[0]))
    if op is None:
        return None
    part, thing = _version_info_part(test.left), _ints(test.comparators[0])
    if part is None or thing is None:
        part, thing, op = _version_info_part(test.comparators[0]), _ints(test.left), _SWAPPED[op]
    if isinstance(part, int) and isinstance(thing, int):
        return _OPERATORS[op]((version[part],), (thing,)) if 0 <= part <= 1 else None
    if isinstance(part, tuple) and isinstance(thing, tuple):
        lo, hi = 0 if part[0] is None else part[0], 2 if part[1] is None else part[1]
        value = version[lo:hi] if 0 <= lo < hi <= 2 else None
        if value is not None and (len(value) == len(thing) or (len(value) > len(thing) and op not in ("==", "!="))):
            return _OPERATORS[op](value, thing)
    return None


def _static_value(test: ast.expr, version: tuple[int, int], *, checking: bool) -> bool | None:
    """The value mypy gives a test (its infer_condition_value), read three-valued: True, False, or
    None when it cannot tell. A name or attribute TYPE_CHECKING or MYPY is True as mypy reads it
    (`checking`) and False at runtime, PY3 and PY2 are True and False, a `sys.version_info`
    comparison is read on `version`; then `not`, `and` (one False decides it) and `or` (one True
    decides it). sys.platform stays None: mypyc compiles on every OS, and another one takes the
    other branch. A chain of `not` is read in a loop (it takes no parentheses, and a few thousand
    parse), and/or in recursion: a nested one takes parentheses, at most 200 levels of them."""
    negate = False
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, negate = test.operand, not negate
    value: bool | None
    if isinstance(test, ast.BoolOp):
        values = {_static_value(v, version, checking=checking) for v in test.values}
        decides = isinstance(test.op, ast.Or)  # True decides an `or`, False an `and`
        value = decides if decides in values else None if None in values else not decides
    else:
        name = test.id if isinstance(test, ast.Name) else test.attr if isinstance(test, ast.Attribute) else ""
        if name in ("TYPE_CHECKING", "MYPY"):
            value = checking
        elif name in ("PY2", "PY3"):
            value = name == "PY3"
        else:
            value = _version_value(test, version)
    return None if value is None else value != negate


def _reachable(stmt: ast.If, version: tuple[int, int], *, checking: bool) -> list[ast.stmt] | None:
    """The block of `if` statement `stmt` that runs (`checking`: as mypy reads it, else at
    runtime), or None when both may: what mypy reads as unreachable is never compiled."""
    value = _static_value(stmt.test, version, checking=checking)
    return None if value is None else (stmt.body if value else stmt.orelse)


def _runtime_nodes(tree: ast.AST, version: tuple[int, int]) -> list[ast.AST]:
    """Every node of the compiled module but those that never run in it: under `if
    TYPE_CHECKING:` (its else, and the body of `if not TYPE_CHECKING:`, do run), and in the branch
    of a sys.version_info test that is false on the Python mypyc compiles with, which mypy reads as
    unreachable and mypyc skips (imports.iter_runtime_nodes, with the version)."""
    out: list[ast.AST] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        out.append(node)
        block = _reachable(node, version, checking=False) if isinstance(node, ast.If) else None
        stack.extend(block if block is not None else ast.iter_child_nodes(node))
    return out


def compile_version(cfg: Config) -> tuple[int, int]:
    """The Python mypyc compiles with: python.cpython (the tools environment)."""
    major, _, minor = cfg.python.cpython.partition(".")
    return int(major), int(minor)


# --- the names a class decorator, a metaclass or a base resolve to ------------------------------


class _Resolver:
    """mypy's full names of what the app's own modules import. `from dataclasses import
    dataclass` in src/p1/compat.py makes p1.compat.dataclass the real dataclasses.dataclass,
    which mypy follows and mypyc compiles natively: lintc reported `@dataclass` imported from
    there (or `@final`, ABCMeta) as making a slow class, an error that failed check, build and the
    hook on code mypyc compiled natively. A name the module defines itself (a decorator of the
    app's own) stays the module's; so does one of a module that cannot be read."""

    HOPS = 8  # a re-export of a re-export...

    def __init__(self, version: tuple[int, int]) -> None:
        self.version = version
        self._imports: dict[str, dict[str, str] | None] = {}
        self._all: dict[str, set[str]] = {}  # a literal __all__ of a module of the app

    def module_imports(self, module: str) -> dict[str, str] | None:
        """The names the module-level imports of the app's module `module` bind (local name ->
        full name), or None when `module` is no module of src/ this can read."""
        if module in self._imports:
            return self._imports[module]
        self._imports[module] = None  # an import cycle reads as unknown
        parts = module.split(".")
        if not all(p.isidentifier() for p in parts):
            return None
        base = SRC.joinpath(*parts)  # as Python imports it: a package, else a module file
        try:  # is_file raises for a folder it may not enter (Python 3.11 to 3.13)
            path = base / "__init__.py" if (base / "__init__.py").is_file() else base.with_name(base.name + ".py")
            tree = parse(path)
        except (OSError, *PARSE_ERRORS):
            return None
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        names: dict[str, str] = {}
        for stmt in _scope_statements(tree.body, self.version):
            if isinstance(stmt, ast.Import | ast.ImportFrom):
                _add_import(names, stmt, package, self)
            elif isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets):
                if isinstance(stmt.value, ast.List | ast.Tuple):
                    self._all[module] = {e.value for e in stmt.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        self._imports[module] = names
        return names

    def star(self, module: str) -> dict[str, str]:
        """What `from <module> import *` binds from a module of the app: the names of its
        __all__ (a literal list or tuple), else every name it imports that has no leading _."""
        names = self.module_imports(module) or {}
        public = self._all.get(module)
        return {k: v for k, v in names.items() if (k in public if public is not None else not k.startswith("_"))}

    def resolve(self, full: str) -> str:
        """`full`, followed through the app's own modules to the name it re-exports."""
        for _ in range(self.HOPS):
            parts = full.split(".")
            for i in range(len(parts) - 1, 0, -1):  # the longest prefix that is a module of src/
                names = self.module_imports(".".join(parts[:i]))
                if names is not None:
                    break
            else:
                return full
            target = names.get(parts[i])
            if target is None:  # defined there (or not found): the module's own
                return full
            full = ".".join([target, *parts[i + 1 :]])
        return full


def _add_import(aliases: dict[str, str], node: ast.Import | ast.ImportFrom, package: str | None = None, resolver: _Resolver | None = None) -> None:
    """Record the names an import binds: local name -> full dotted name. A relative import is
    resolved against `package` (the package of the module it is in; None outside src/: it stays
    unresolved, a local `final` or `dataclass` is not the real one). A star import of the app's
    own module takes the names that module imports (`resolver`)."""
    if isinstance(node, ast.Import):
        for a in node.names:
            if a.asname:
                aliases[a.asname] = a.name
            else:
                top = a.name.partition(".")[0]
                aliases[top] = top
        return
    if node.level:
        base = _relative_base(package, node.level) if package is not None else None
        if base is None:
            return
        module = f"{base}.{node.module}" if node.module else base
    elif node.module:
        module = node.module
    else:
        return
    for a in node.names:
        if a.name != "*":
            aliases[a.asname or a.name] = f"{module}.{a.name}"
        elif resolver is not None and resolver.module_imports(module) is not None:  # one of the app's modules
            for name, full in resolver.star(module).items():
                aliases.setdefault(name, full)
        else:  # mypy resolves star imports too
            for full in (*NATIVE_CLASS_DECORATORS, *NATIVE_METACLASSES, *NON_NATIVE_BASES):
                owner, _, name = full.rpartition(".")
                if owner == module:
                    aliases.setdefault(name, full)


def _scope_statements(body: list[ast.stmt], version: tuple[int, int]) -> Iterator[ast.stmt]:
    """The statements that run in the scope of `body`, in source order: those in its if/try/with/
    for/while/match blocks too, never those of a nested function or class body, nor those of a
    block mypy reads as unreachable on `version` (_reachable). Iterative: an elif chain nests one
    `orelse` per branch, and 1000 of them (a generated dispatch table) passed Python's
    recursion limit."""
    stack: list[Iterator[ast.stmt]] = [iter(body)]
    while stack:
        stmt = next(stack[-1], None)
        if stmt is None:
            stack.pop()
            continue
        yield stmt
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        reachable = _reachable(stmt, version, checking=True) if isinstance(stmt, ast.If) else None
        if reachable is not None:
            stack.append(iter(reachable))
            continue
        blocks = [inner for name in ("body", "orelse", "finalbody") if isinstance(inner := getattr(stmt, name, None), list)]
        blocks += [block.body for block in (*getattr(stmt, "handlers", ()), *getattr(stmt, "cases", ()))]
        stack.append(itertools.chain.from_iterable(blocks))


def _import_aliases(tree: ast.Module, version: tuple[int, int], package: str | None = None, resolver: _Resolver | None = None) -> dict[ast.ClassDef, dict[str, str]]:
    """Each class -> the names its decorators resolve through (what mypy resolves): the imports
    of the scope the class statement runs in over those of the scopes around it. A function's own
    import never decides a module-level decorator."""
    out: dict[ast.ClassDef, dict[str, str]] = {}
    stack: list[tuple[list[ast.stmt], dict[str, str]]] = [(tree.body, {})]
    while stack:
        body, outer = stack.pop()
        aliases = dict(outer)
        statements = list(_scope_statements(body, version))
        for stmt in statements:
            if isinstance(stmt, ast.Import | ast.ImportFrom):
                _add_import(aliases, stmt, package, resolver)
        for stmt in statements:
            if isinstance(stmt, ast.ClassDef):
                out[stmt] = aliases
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                stack.append((stmt.body, aliases))
    return out


def _full_name(written: str, aliases: dict[str, str], resolver: _Resolver | None = None) -> str:
    head, dot, rest = written.partition(".")
    full = aliases.get(head, head) + dot + rest
    return resolver.resolve(full) if resolver is not None else full


def _is_explicitly_non_native(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and _decorator_name(node).rpartition(".")[2] == "mypyc_attr"
        and any(k.arg == "native_class" and isinstance(k.value, ast.Constant) and k.value.value is False for k in node.keywords)
    )


def _non_native_kind(node: ast.ClassDef, aliases: dict[str, str], local: dict[str, str], resolver: _Resolver | None = None) -> str | None:
    """What makes mypyc compile the class as a regular Python class through its metaclass or its
    bases (NATIVE_METACLASSES, NON_NATIVE_BASES), None when nothing does. `local`: the classes of
    the module seen so far whose subclasses are such a class too (a metaclass is inherited, and
    a subclass of a TypedDict is one; a subclass of a NamedTuple is a mypyc error of its own).
    A base imported from another module is not looked into (its name is: `resolver`)."""
    for keyword in node.keywords:
        if keyword.arg == "metaclass":
            written = _decorator_name(keyword.value)
            if not written or _full_name(written, aliases, resolver) not in NATIVE_METACLASSES:
                return f"has the metaclass {written or '<expression>'}"
    for base in node.bases:
        written = _decorator_name(base.value if isinstance(base, ast.Subscript) else base)
        full = _full_name(written, aliases, resolver)
        if full in NON_NATIVE_BASES:
            return f"is {NON_NATIVE_BASES[full]}"
        if written in local:
            return f"inherits from '{written}', which {local[written]}"
    return None


def _non_native_kinds(tree: ast.Module, aliases: dict[ast.ClassDef, dict[str, str]], version: tuple[int, int], resolver: _Resolver | None = None) -> dict[ast.ClassDef, str]:
    """_non_native_kind of every class, the module's own classes read in source order."""
    kinds: dict[ast.ClassDef, str] = {}
    local: dict[str, str] = {}
    for stmt in _scope_statements(tree.body, version):
        if isinstance(stmt, ast.ClassDef):
            kind = _non_native_kind(stmt, aliases.get(stmt, {}), local, resolver)
            if kind:
                kinds[stmt] = kind
                if kind != "is a NamedTuple":  # a subclass says what its base class is
                    local[stmt.name] = kind.split("', which ", 1)[1] if kind.startswith("inherits from '") else kind
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node not in kinds:
            kind = _non_native_kind(node, aliases.get(node, {}), local, resolver)
            if kind:
                kinds[node] = kind
    return kinds


def _within(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


# The module-level compound statements a class may sit in, by the keyword that opens them
BLOCK_KEYWORDS: dict[type[ast.stmt], str] = {ast.If: "if", ast.Try: "try", ast.TryStar: "try", ast.With: "with", ast.For: "for", ast.While: "while", ast.Match: "match"}


def _block_classes(stmt: ast.stmt, version: tuple[int, int]) -> Iterator[ast.ClassDef]:
    """The classes in the blocks of a module-level compound statement (BLOCK_KEYWORDS), and in
    theirs. mypyc compiles only the classes of the module's own statements (its build_type_map
    reads module.defs) and stops at any other with "Nested class definitions not supported": a
    version check, a `try:` fallback, an `if TYPE_CHECKING:` Protocol. Never one of a block mypy
    reads as unreachable, which mypyc skips (_reachable): the else of `if TYPE_CHECKING:`, the
    body of `if not TYPE_CHECKING:`, and the branch of a sys.version_info test that is false on
    `version` (a backport class under `else:` of `if sys.version_info >= (3, 12):` compiled on
    3.14 was a false error). A class in a function or class of such a block is the other rules'."""
    stack: list[ast.stmt] = [stmt]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.ClassDef) and node is not stmt:
            yield node
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        reachable = _reachable(node, version, checking=True) if isinstance(node, ast.If) else None
        if reachable is not None:
            blocks = [reachable]
        else:
            blocks = [inner for name in ("body", "orelse", "finalbody") if isinstance(inner := getattr(node, name, None), list)]
            blocks += [block.body for block in (*getattr(node, "handlers", ()), *getattr(node, "cases", ()))]
        stack += reversed(list(itertools.chain.from_iterable(blocks)))


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
    except (OSError, *TOML_ERRORS):
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
    except OSError as e:  # another user's, locked by another program: ruff and mypy say so too
        return [Finding(path, 1, f"cannot read it: {e.strerror or e} (the mypyc rules skipped this file)")]

    version = compile_version(cfg)
    try:  # the package relative imports start from (None outside src/: they stay unresolved)
        module = module_name(path, SRC)
        package: str | None = module if path.name == "__init__.py" else module.rpartition(".")[0]
    except ValueError:
        package = None
    resolver = _Resolver(version)
    aliases = _import_aliases(tree, version, package, resolver)
    kinds = _non_native_kinds(tree, aliases, version, resolver)
    for node in _runtime_nodes(tree, version):
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
                bad = [w or "<expression>" for w in written if _full_name(w, scope, resolver) not in NATIVE_CLASS_DECORATORS]
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
        keyword = BLOCK_KEYWORDS.get(type(stmt))
        for inner in _block_classes(stmt, version) if keyword else ():
            add(inner, f"class '{inner.name}' defined inside a module-level `{keyword}` block: mypyc does not support it; define it at module level")
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
