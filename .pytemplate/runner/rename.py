"""rename NEW_NAME [--force]: rename the app and its Python package everywhere.

Renames src/<pkg>/ and rewrites every reference to the old name/package in src/, tests/,
pytemplate.toml and pyproject.toml, then re-locks uv.lock and regenerates the generated files.
`./pyt apply` runs the same plan when app.name was changed by hand in pytemplate.toml.

The pure part (`plan`, `apply_plan` and `rewrite` for one text) works on any project folder
and needs no uv: the tests rename each preset skeleton rendered for one name and compare the
result, byte for byte, with the skeleton rendered for the new name (what `./pyt new --name
NEW` writes).

Which occurrences change (whole words only: `myapp_extra` and `my-app-2` never match):
- Python code (the tokenizer tells code from strings and comments): only real package
  references, i.e. the first name of `import myapp[.x]` and `from myapp[.x] import ...`, and,
  in a file that binds the package with `import myapp[.x]` (no `as`), every name `myapp` that
  resolves to that import (`ast` scopes). A function, class body or comprehension that binds
  `myapp` another way (assignment, parameter, loop/with/except target, global/nonlocal) keeps
  every `myapp` in it, and a module that rebinds it keeps all of its own: they are reported,
  not changed. So is a reference the new name would capture (_captured): a use below a scope
  that binds the new name (a parameter, a local, a class attribute), and the import and uses of
  a scope where the renamed import would clash with another binding of the new name or hide a
  name read from further out (a module name, a builtin such as `map`). Attributes (`obj.myapp`)
  and keyword arguments (`f(myapp=1)`) never change.
- Strings, comments, docstrings and other text files: every occurrence except right after a
  dot (`x.myapp` is a submodule or an attribute, never the top-level package), a path
  segment right after the package itself (`src/myapp/myapp` is a submodule of it), and a file
  or a folder named like the app outside src/ (`tests/myapp/data`, `asset("myapp.png")`: only
  src/myapp/ moves, so they are reported, not changed). In Python,
  TOML and JSON strings the prefix (`f`, `rb`...) and escapes (`\\n`, `\\x89`) are never the
  name, and a name right after a single backslash that makes no escape (`r"\\d"`,
  `"\\myapp"`) and a one-letter name that ends a format directive (`"%d"`, `"{:d}"`,
  `f"{x:d}"`, strftime's `"%Y"`) or is a struct format character after a byte order or count
  (`">I"`) are reported, not changed: an app may be called `f`, `n`, `r` or `d`. An escape
  next to the name is neither a path separator nor part of its word (`"myapp\\n"`,
  `"Usage:\\nmyapp"`: prose). Comments and other text files (Markdown, YAML...) have no escapes.
- When the old name is also the old package but the new name is not a package name
  (`alpha` -> `My-Game`, package `my_game`), each text occurrence is either the package or the
  name. Package: path-like (`src/alpha/`, `alpha\\core`), dotted (`alpha.core`, `alpha.*`,
  `alpha:main`), next to the words package/module, after `import` or `-m`, in `from alpha
  import`, the module-name arguments of loader calls (LOADERS: `import_module()`,
  `find_spec()`, `files()`, the importlib.resources functions, `pkgutil.get_data()`,
  `runpy.run_module()`, `pytest.importorskip()`; positional or as a `name=`, `package=`,
  `anchor=`, `mod_name=`, `modname=` keyword), and the other strings only a module name can
  stand for (_module_name_strings: `sys.modules["alpha"]`, `"alpha" in sys.modules`,
  `__name__ == "alpha"`, `__package__ != "alpha"`, `__name__.startswith("alpha.")`).
  Name: everything else (titles, `\"\"\"alpha\"\"\"`, `f"alpha: ..."`) and artifact names
  (`alpha.exe`, `alpha.pyz`, `alpha-cpython-exe`...).
- pytemplate.toml: app.name (its comment is kept) and package references only, always chosen
  by context, never a TOML key or table header. A path is the package only right inside src/
  (`src/alpha/data`; not `tools/alpha.py`, `assets/alpha.ico`, `./pyt`), a dotted word only
  when it names a module of src/<pkg>/ (`alpha.core`; not `alpha.ico`, `uv.lock`). The values
  of the keys that hold module names (MODULE_KEYS: compile.modules, [[typing.mypy_overrides]]
  module, deploy.exe.hidden_imports...) are package references even when bare
  (`modules = ["alpha"]`). Any other occurrence of the old name is reported, not changed
  ([tasks] can use `{name}` and `{pkg}`).
- pyproject.toml: [project] name and the preset block (`# >>> pytemplate-preset`); other
  occurrences are reported.
- A Python file saved in another encoding is rewritten in the encoding its PEP 263 cookie
  declares; other files that are not UTF-8 text but mention the old name are listed as a
  warning. Files outside src/ and tests/ (README.md, scripts/, docs/, your own workflows) are
  only listed.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import codecs
import contextlib
import dataclasses
import functools
import io
import keyword
import os
import re
import stat
import tokenize
import tomllib
import warnings
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import config, envs, presets, proc, render, ui
from .config import Config
from .project import DIST, EXT_SUFFIXES, ROOT, write_whole
from .project import ROOT as _RUNNER_CWD  # where the tools run (tests move ROOT, never this)
from .ui import PytError

Kind = Literal["pkg", "name", "keep", "skip"]

_DRY = "(--dry-run: nothing is written)"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".hypothesis", ".mypy_cache", ".ruff_cache", ".git", ".flet"}
PY_SUFFIXES = {".py", ".pyi", ".pyw"}
# Root files that never get a "this file also mentions the old name" note: rename edits them, or
# they are the template's own (the launchers and CLAUDE.md, whose words such as `app` or `p` are
# no app name: the note invited editing them by hand)
ROOT_SKIP = {"pytemplate.toml", "pyproject.toml", "uv.lock", "pyrightconfig.json", "pyt", "pyt.cmd", "pyt.ps1", "CLAUDE.md"}
# Folders never searched for other mentions of the old name: caches, environments, builds, the
# runner, Claude Code's state (it holds whole copies of the project in worktrees)
MENTION_SKIP_DIRS = {*SKIP_DIRS, ".build", "dist", ".pytemplate", ".claude", ".tox", ".nox", ".eggs", ".idea", "node_modules"}
MENTION_MAX_BYTES = 2 * 1024 * 1024
# Calls whose string arguments are module names, never the display name: the argument positions,
# or a keyword of MODULE_ARGUMENTS (import_module("alpha"), import_module(".x", package="alpha"))
LOADERS: dict[str, tuple[int, ...]] = {
    "import_module": (0, 1),  # importlib.import_module(name, package)
    "__import__": (0,),
    "find_spec": (0, 1),  # importlib.util.find_spec(name, package)
    "files": (0,),  # importlib.resources.files(anchor)
    "read_text": (0,),  # importlib.resources' functional API: (package, resource, ...)
    "read_binary": (0,),
    "open_text": (0,),
    "open_binary": (0,),
    "path": (0,),
    "is_resource": (0,),
    "contents": (0,),
    "get_data": (0,),  # pkgutil.get_data(package, resource)
    "run_module": (0,),  # runpy.run_module(mod_name)
    "importorskip": (0,),  # pytest.importorskip(modname): renamed to a display name, the tests skipped
}
MODULE_ARGUMENTS = frozenset({"name", "package", "anchor", "mod_name", "modname"})
# The loaders of LOADERS whose names a helper of the project may have too (read_text(name),
# path(name), contents(name)...): their argument is the package only when it names no file or
# folder named after the app (`read_text("alpha.txt")` keeps its file's name: _classify)
RESOURCE_FUNCTIONS = frozenset({"files", "read_text", "read_binary", "open_text", "open_binary", "path", "is_resource", "contents", "get_data"})
SAMPLES = 3  # sample lines per file in the --dry-run plan
# pytemplate.toml keys whose values are module names: a bare "alpha" there is the package
MODULE_KEYS = frozenset(
    {
        "compile.modules",
        "compile.exclude",
        "compile.forbid_imports",
        "typing.mypy_overrides.module",
        "deploy.exe.hidden_imports",
        "deploy.exclude_modules",
        "deploy.wheel.entry",
    }
)
# uv's messages when `uv lock` cannot reach the package index
PYPI_UNREACHABLE = ("Failed to fetch", "network was disabled", "not found in the cache", "dns error", "client error (Connect)")

# Context rules for an occurrence of an old name that is also the old package
_ARTIFACT_SUFFIX = re.compile(r"\.(?:exe|pyz|cmd|bat|sh|ps1|spec|zip|tar|dmg|msi|apk|aab|ipa)\b")
_BACKEND_SUFFIX = re.compile(r"-(?:cpython|pypy|mypyc)\b")  # dist/<name>-<backend>-<method>
_PKG_WORD_AFTER = re.compile(r"[ \t]+(?:package|module)\b")
_PKG_WORD_BEFORE = re.compile(r"(?:^|\W)(?:package|module)[ \t]+$")
_IMPORT_BEFORE = re.compile(r"(?:^|[^\w.])import[ \t]+$")
_LAUNCHER_BEFORE = re.compile(r"(?:^|[^\w.])\.[/\\]$")  # ./ or .\ as a path's start: `./pyt report`
_FROM_BEFORE = re.compile(r"(?:^|[^\w.])from[ \t]+$")
_IMPORT_AFTER = re.compile(r"[ \t]+import\b")
# -m alpha, "-m", "alpha", and a prefixed string after it: "-m", f"alpha.{x}", r"alpha"
_DASH_M_BEFORE = re.compile(r"(?:^|[\s\"'\[(,])-m[\s\"',]+(?:[rRbBuUfFtT]{1,2}[\"'])?$")
# TOML: a bare key (`alpha = `, `x.alpha = `) or a table header (`[alpha]`, `[[x.alpha]]`)
_TOML_KEY_BEFORE = re.compile(r"[ \t]*(?:\[\[?[ \t]*)?(?:[A-Za-z0-9_-]+[ \t]*\.[ \t]*)*")
_TOML_KEY_AFTER = re.compile(r"[ \t]*[.=\]]")
_TOML_TABLE = re.compile(r"[ \t]*\[\[?[ \t]*([A-Za-z0-9_.\- \t]+?)[ \t]*\]\]?[ \t]*(?:#.*)?$")
_TOML_ASSIGN = re.compile(r"[ \t]*([A-Za-z0-9_-]+(?:[ \t]*\.[ \t]*[A-Za-z0-9_-]+)*)[ \t]*=")
# Strings: the prefix before the opening quote, and the letters a backslash turns into an escape
_STRING_PREFIX = re.compile(r"[A-Za-z]*(?=['\"])")
_PY_ESCAPES = frozenset("abfnrtvxNuU01234567")
_BYTES_ESCAPES = frozenset("abfnrtvx01234567")
_TOML_ESCAPES = frozenset("btnfruUex")  # TOML 1.0, plus \e and \x of TOML 1.1
_JSON_ESCAPES = frozenset("bfnrtu")
_YAML_ESCAPES = frozenset("0abtnvfreNLPxuU")  # YAML 1.2, in double-quoted scalars only
# The escapes of one letter, after which the old name starts a word ("Usage:\nalpha", "Name:\talpha");
# \x, \u, \U and Python's \N{...} take more than their letter
_LETTER_ESCAPES = frozenset("abefnrtvLP")
_JSON_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"?')
# Data files of src/ and tests/ whose strings have escapes (other text files are plain text): a
# notebook is JSON, and YAML's double-quoted strings have escapes (an app named n turned their
# `\n` into `\b` when renamed to b)
DATA_STRINGS = {
    ".toml": "toml",
    **dict.fromkeys((".json", ".ipynb", ".jsonc", ".geojson", ".jsonl", ".ndjson"), "json"),
    **dict.fromkeys((".yaml", ".yml"), "yaml"),
}
# A format directive in a string ends in one letter: printf's `%d`, `%(k)-5s` and strftime's
# `%Y`, `%-m`, `%^a` (any letter after a `%`: time, datetime and logging use them), str.format's
# `{:d}`, `{0:>4x}`, `{!r}` (the text before the letter, and the letters it can end in)
_PRINTF_BEFORE = re.compile(r"%(?:\([^()\n]*\))?[#0 +\-^]*(?:\*|\d+)?(?:\.(?:\*|\d*))?[hlL]?\Z")
_FORMAT_BEFORE = re.compile(r"\{[^{}\n]*[:!][^{}\n]*\Z")
_DIRECTIVE_LETTERS = frozenset("abcdeEfFgGinorsuxX")
# struct's format strings: a byte order or a count first, then format characters (">I", "<2H I")
_STRUCT_LETTERS = "xcbBhHiIlLqQnNefdspP"
_STRUCT_BEFORE = re.compile(r"(?:'''|\"\"\"|['\"])(?:[<>=!@]|\d)[\d\s" + _STRUCT_LETTERS + r"?]*\Z")
# ruff format --check --output-format concise: "path:1:2: unformatted: ..." (older: "Would reformat: path")
_UNFORMATTED = re.compile(r"^(?:Would reformat: (?P<old>.+)|(?P<path>.+?):\d+:\d+: unformatted\b)", re.MULTILINE)
# ruff check --output-format concise: "path:1:1: I001 [*] Import block is un-sorted or un-formatted"
_UNSORTED = re.compile(r"^(?P<path>.+?):\d+:\d+: I001\b", re.MULTILINE)


def package_of(name: str) -> str:
    """Return the Python package of an app name (the same rule as config.Config.pkg)."""
    return name.replace("-", "_").lower()


@dataclass(frozen=True)
class Names:
    old_name: str
    new_name: str

    @property
    def old_pkg(self) -> str:
        return package_of(self.old_name)

    @property
    def new_pkg(self) -> str:
        return package_of(self.new_name)


# --- rewriting one text ------------------------------------------------------------------------


@dataclass
class Rewrite:
    text: str
    count: int = 0  # references changed
    changes: list[tuple[int, str, str]] = field(default_factory=list)  # (line, old, new) per changed line
    kept: list[tuple[int, str]] = field(default_factory=list)  # occurrences left as they were: (line, text)
    note: str = ""


@dataclass(frozen=True)
class _Region:
    """A string or comment of a Python file: [start, end) offsets in the text."""

    start: int
    end: int
    forced: bool = False  # argument of import_module() & co., a key of sys.modules...: a module name
    fstring: bool = False  # an f-string or t-string: its {fields} are code, their format specs syntax
    resource: bool = False  # forced by a loader of RESOURCE_FUNCTIONS (or a helper of that name)


class _Positions:
    """Offsets in a Python text of the positions that tokenize (line, column in characters) and
    ast (line, column in UTF-8 bytes) give for its body: the text without a BOM, a lone CR read
    as LF (the compiler counts it as a line break), one character for one."""

    def __init__(self, text: str) -> None:
        self.shift = 1 if text.startswith("\ufeff") else 0
        self.body = re.sub(r"\r(?!\n)", "\n", text[self.shift :])
        self.line_starts = [0, *(m.end() for m in re.finditer("\n", self.body))]
        self._lines: list[str] | None = None

    def token(self, pos: tuple[int, int]) -> int:
        return self.shift + self.line_starts[pos[0] - 1] + pos[1]

    def ast(self, line: int, col: int) -> int:
        if self._lines is None:
            self._lines = self.body.split("\n")
        lines = self._lines
        chars = len(lines[line - 1].encode("utf-8")[:col].decode("utf-8", "replace")) if line <= len(lines) else col
        return self.token((line, chars))


@dataclass
class _Code:
    names: dict[int, str]  # offset -> NAME token
    refs: set[int]  # offsets of the NAME tokens that are the package
    bound: bool  # `import pkg[.x]` without `as` binds the name `pkg` in this file
    regions: list[_Region]  # sorted, never overlapping
    scoped: bool = False  # refs of the bound name come from the ast scope analysis
    positions: _Positions | None = None  # the offsets of the text's ast positions
    starts: list[int] = field(init=False)

    def __post_init__(self) -> None:
        self.starts = [r.start for r in self.regions]

    def region_at(self, pos: int) -> _Region | None:
        i = bisect.bisect_right(self.starts, pos) - 1
        if i >= 0 and pos < self.regions[i].end:
            return self.regions[i]
        return None


def _is_word(char: str) -> bool:
    return char == "_" or char.isalnum()


def _pattern(names: Names) -> re.Pattern[str]:
    words = sorted({names.old_name, names.old_pkg}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)")


def _escaped_pattern(names: Names) -> re.Pattern[str]:
    """The old name right after a backslash and a letter (`"Usage:\\nalpha"`): to _pattern the
    letter makes it part of a longer word. rewrite takes it only where that is a string's escape
    (_after_an_escape): it was neither renamed nor reported."""
    words = sorted({names.old_name, names.old_pkg}, key=len, reverse=True)
    return re.compile(r"(?<=\\[A-Za-z])(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)")


def _mentioned(text: str, names: Names) -> bool:
    """Whether `text` holds an occurrence rewrite looks at (_pattern, _escaped_pattern)."""
    return _pattern(names).search(text) is not None or _escaped_pattern(names).search(text) is not None


# --- which names resolve to the package (ast scopes) -----------------------------------------------

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _scopes(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Map every node to the scope it is evaluated in: the module, a function, a lambda, a class
    body or a comprehension. Defaults, decorators, annotations, bases and a comprehension's first
    iterable belong to the enclosing scope, as in Python itself. Iterative: no recursion limit."""
    out: dict[ast.AST, ast.AST] = {}
    stack: list[tuple[ast.AST, ast.AST]] = [(tree, tree)]
    while stack:
        node, scope = stack.pop()
        out[node] = scope
        if isinstance(node, _FUNCTIONS):
            if not isinstance(node, ast.Lambda):
                stack += [(d, scope) for d in node.decorator_list]
                if node.returns is not None:
                    stack.append((node.returns, scope))
            args = node.args
            out[args] = node
            for a in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
                if a is not None:
                    out[a] = node  # a parameter binds in the function
                    if a.annotation is not None:
                        stack.append((a.annotation, scope))
            stack += [(d, scope) for d in (*args.defaults, *args.kw_defaults) if d is not None]
            stack += [(p, node) for p in getattr(node, "type_params", ())]
            if isinstance(node, ast.Lambda):
                stack.append((node.body, node))
            else:
                stack += [(b, node) for b in node.body]
        elif isinstance(node, ast.ClassDef):
            stack += [(x, scope) for x in (*node.decorator_list, *node.bases, *node.keywords)]
            stack += [(p, node) for p in getattr(node, "type_params", ())]
            stack += [(b, node) for b in node.body]
        elif isinstance(node, _COMPREHENSIONS):
            first, *rest = node.generators
            out[first] = node
            stack.append((first.iter, scope))
            stack += [(first.target, node), *((c, node) for c in first.ifs), *((g, node) for g in rest)]
            parts = (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,)
            stack += [(p, node) for p in parts]
        else:
            stack += [(child, scope) for child in ast.iter_child_nodes(node)]
    return out


def _package_uses(body: str, pkg: str) -> set[tuple[int, int]] | None:
    """(line, UTF-8 column) of every name `pkg` that resolves to an `import pkg[.x]` (no `as`).

    A scope that binds `pkg` any other way owns its `pkg` names, and so do the scopes nested in
    it (class bodies excepted, as in Python); when a scope both imports and rebinds it, it is
    ambiguous and nothing in it counts. None: `ast` cannot parse the file (syntax newer than the
    runner's Python): the caller falls back to the token rule.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # SyntaxWarning (invalid escapes) is not the user's business here
            tree = ast.parse(body)
        scope = _scopes(tree)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    shadowed: set[ast.AST] = set()
    imported: set[ast.AST] = set()  # scopes with `import pkg[.x]`: there `pkg` is the package
    walrus = {id(n.target) for n in ast.walk(tree) if isinstance(n, ast.NamedExpr)}
    for node in ast.walk(tree):
        binds = False
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname == pkg:
                    binds = True  # import other as pkg
                elif alias.asname is None and alias.name.split(".")[0] == pkg:
                    imported.add(scope[node])
        elif isinstance(node, ast.ImportFrom):
            binds = any((alias.asname or alias.name) == pkg for alias in node.names)
        elif isinstance(node, ast.Name):
            binds = node.id == pkg and not isinstance(node.ctx, ast.Load)
        elif isinstance(node, ast.arg):
            binds = node.arg == pkg
        elif isinstance(node, ast.Global):
            if pkg in node.names:
                shadowed.add(tree)  # the function can rebind the module's own name
            continue
        elif isinstance(node, ast.Nonlocal):
            binds = pkg in node.names
        elif isinstance(node, ast.MatchMapping):
            binds = node.rest == pkg
        elif isinstance(node, (ast.alias, ast.keyword)):
            continue  # handled with their statement / a keyword argument binds nothing
        else:
            binds = getattr(node, "name", None) == pkg  # def/class, except ... as, match captures, type params
        if not binds:
            continue
        where = scope[node]
        shadowed.add(where)
        if id(node) in walrus:
            while isinstance(where, _COMPREHENSIONS):  # a walrus in a comprehension binds outside it
                where = scope[where]
                shadowed.add(where)

    def is_package(node: ast.AST) -> bool:
        where, innermost = scope[node], True
        while True:
            visible = innermost or not isinstance(where, ast.ClassDef)  # class bodies are invisible to nested scopes
            if visible and where in shadowed:
                return False
            if visible and where in imported:
                return True
            if where is tree:
                return False
            innermost, where = False, scope[where]

    return {(n.lineno, n.col_offset) for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == pkg and is_package(n)}


def _binds(node: ast.AST, name: str) -> bool:
    """Whether `node` binds `name` in the scope it is evaluated in (_scopes), an `import name[.x]`
    included: an assignment, a parameter, a loop, with or except target, def/class, an import,
    a match capture, a type parameter, a nonlocal declaration."""
    if isinstance(node, ast.Import):
        return any(alias.asname == name or (alias.asname is None and alias.name.split(".")[0] == name) for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return any((alias.asname or alias.name) == name for alias in node.names)
    if isinstance(node, ast.Name):
        return node.id == name and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.arg):
        return node.arg == name
    if isinstance(node, ast.Nonlocal):
        return name in node.names
    if isinstance(node, ast.MatchMapping):
        return node.rest == name
    if isinstance(node, (ast.alias, ast.keyword, ast.Global)):
        return False  # handled with their statement / a keyword argument binds nothing / below
    return getattr(node, "name", None) == name  # def/class, except ... as, match captures, type params


def _binding_scopes(tree: ast.Module, scope: dict[ast.AST, ast.AST], name: str) -> set[ast.AST]:
    """The scopes that bind `name` (_binds); `global name` binds it in the module, and a walrus in
    a comprehension in the scope around it too, as in Python."""
    out: set[ast.AST] = set()
    walrus = {id(n.target) for n in ast.walk(tree) if isinstance(n, ast.NamedExpr)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            if name in node.names:
                out.add(tree)
            continue
        if not _binds(node, name):
            continue
        where = scope[node]
        out.add(where)
        if id(node) in walrus:
            while isinstance(where, _COMPREHENSIONS):
                where = scope[where]
                out.add(where)
    return out


def _captured(body: str, pkg: str, new: str) -> set[tuple[int, int]] | None:
    """(line, UTF-8 column) of the package references that a rename to `new` would hand to
    another binding of `new`, so that they are kept and reported instead:
    - a use of `pkg` below a scope that binds `new` another way before the scope that imports
      it (a parameter, a local, a loop or comprehension variable, a class attribute): renamed,
      `new.core.x()` read that binding;
    - in a scope whose `import pkg[.x]` would bind `new` where `new` is bound another way (`from
      engine import game`, `def game`) or read from further out (an enclosing function, the
      module, a builtin such as `map` or `input`), that import and every use of it: renamed, the
      import and that binding took each other's name.
    None: ast cannot parse it (syntax newer than the runner's Python)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(body)
        scope = _scopes(tree)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    imports: dict[ast.AST, list[ast.alias]] = {}  # scope -> its `import pkg[.x]` (no `as`)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None and alias.name.split(".")[0] == pkg:
                    imports.setdefault(scope[node], []).append(alias)
    if not imports:
        return set()
    binders = _binding_scopes(tree, scope, new)

    def lookup(node: ast.AST) -> Iterator[ast.AST]:
        """The scopes a name of `node` is looked up in, innermost first, up to the module (class
        bodies are invisible to the scopes nested in them)."""
        where, innermost = scope[node], True
        while True:
            if innermost or not isinstance(where, ast.ClassDef):
                yield where
            if where is tree:
                return
            innermost, where = False, scope[where]

    clashing = {s for s in imports if s in binders}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == new:
            for where in lookup(node):
                if where in binders:
                    break
                if where in imports:  # a name the new import would take over
                    clashing.add(where)
                    break
    out = {(alias.lineno, alias.col_offset) for s in clashing for alias in imports[s]}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id == pkg):
            continue
        for where in lookup(node):
            if where in imports:
                if where in clashing:
                    out.add((node.lineno, node.col_offset))
                break
            if where in binders:  # renamed, it would read that binding
                out.add((node.lineno, node.col_offset))
                break
    return out


def _keep_captured(code: _Code, names: Names) -> None:
    """Leave the package references a rename would hand to another binding of the new name
    (_captured) out of code.refs: they are kept and reported, like a use under a binding of the
    old name. Without ast, a file that names the new name anywhere keeps them all."""
    if code.positions is None:
        return
    captured = _captured(code.positions.body, names.old_pkg, names.new_pkg)
    if captured is None:
        if names.new_pkg in code.names.values():
            code.refs = set()
        return
    code.refs -= {code.positions.ast(line, col) for line, col in captured}


_MODULES_METHODS = frozenset({"get", "pop", "setdefault"})  # sys.modules.get("alpha")
_MODULE_DUNDERS = frozenset({"__name__", "__package__"})


def _module_name_strings(sig: list[tokenize.TokenInfo]) -> set[int]:
    """Indexes in `sig` (the significant tokens of a file) of the strings that stand where only a
    module name can: a key of sys.modules (`sys.modules["alpha"]`, its `get`, `pop` and
    `setdefault`, `"alpha" in sys.modules`, monkeypatch's `setitem(sys.modules, "alpha", m)` and
    `delitem`) and what a module's own name is compared with (`__name__ == "alpha"`,
    `__package__ != "alpha"`, `__spec__.name`, `__name__.startswith("alpha.")`). Renamed to a
    display name (My-Game) they named no module: a test failed, or skipped. A 3.12+ f-string
    counts from its start token."""

    def name(i: int, *values: str) -> bool:
        return 0 <= i < len(sig) and sig[i].type == tokenize.NAME and (not values or sig[i].string in values)

    def op(i: int, *values: str) -> bool:
        return 0 <= i < len(sig) and sig[i].type == tokenize.OP and sig[i].string in values

    def own_name_ends(i: int) -> bool:  # sig[i] ends a bare __name__, __package__ or __spec__.name
        if name(i, *_MODULE_DUNDERS):
            return not op(i - 1, ".")  # cls.__name__ is a class's name
        return name(i, "name", "parent") and op(i - 1, ".") and name(i - 2, "__spec__") and not op(i - 3, ".")

    def own_name_starts(i: int) -> bool:
        return name(i, *_MODULE_DUNDERS) or (name(i, "__spec__") and op(i + 1, ".") and name(i + 2, "name", "parent"))

    def modules_start(i: int) -> int:  # sig[i] ends `sys.modules` or `modules`: its first index (-1: no)
        if not name(i, "modules"):
            return -1
        return i - 2 if op(i - 1, ".") and name(i - 2, "sys") else i

    def modules_at(i: int) -> bool:  # `sys.modules` or `modules` starts at sig[i]
        return (name(i, "sys") and op(i + 1, ".") and name(i + 2, "modules")) or name(i, "modules")

    found: set[int] = set()
    depth = 0
    first = 0
    for i, tok in enumerate(sig):
        kind = tokenize.tok_name.get(tok.type, "")
        if kind.endswith("STRING_START"):
            if depth == 0:
                first = i
            depth += 1
            continue
        if kind.endswith("STRING_END"):
            depth -= 1
            if depth:
                continue
            a, b = first, i
        elif tok.type == tokenize.STRING and depth == 0:
            a = b = i
        else:
            continue
        keyed = op(a - 1, "[") and name(a - 2, "modules")
        method = op(a - 1, "(") and name(a - 2, *_MODULES_METHODS) and op(a - 3, ".") and name(a - 4, "modules")
        start = modules_start(a - 2) if op(a - 1, ",") else -1
        patched = start >= 0 and op(start - 1, "(") and name(start - 2, "setitem", "delitem")
        member = (name(b + 1, "in") and modules_at(b + 2)) or (name(b + 1, "not") and name(b + 2, "in") and modules_at(b + 3))
        compared = (op(a - 1, "==", "!=") and own_name_ends(a - 2)) or (op(b + 1, "==", "!=") and own_name_starts(b + 2))
        prefix = op(a - 1, "(") and name(a - 2, "startswith") and op(a - 3, ".") and own_name_ends(a - 4)
        if keyed or method or patched or member or compared or prefix:
            found.add(a)
    return found


def _python_code(text: str, pkg: str) -> _Code | None:
    """Tokenize a Python source: NAME tokens, package references and string/comment regions.

    Return None if the tokenizer rejects it (the caller then treats it as plain text).
    """
    # A lone CR ends a line for the compiler (ast counts it): read as LF, one character for one,
    # so every offset stays the same
    positions = _Positions(text)
    body, offset = positions.body, positions.token
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(body).readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    trivia = {tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}
    sig = [t for t in tokens if t.type not in trivia]

    def is_name(i: int, value: str | None = None) -> bool:
        return i < len(sig) and sig[i].type == tokenize.NAME and (value is None or sig[i].string == value)

    def is_op(i: int, value: str) -> bool:
        return i < len(sig) and sig[i].type == tokenize.OP and sig[i].string == value

    def after_dotted(i: int) -> int:
        i += 1
        while is_op(i, ".") and is_name(i + 1):
            i += 2
        return i

    refs: set[int] = set()  # indexes in sig
    in_import: set[int] = set()
    bound = False
    i = 0
    while i < len(sig):
        if is_name(i, "from"):
            j = i + 1
            relative = False
            while j < len(sig) and sig[j].type == tokenize.OP and sig[j].string in (".", "..."):
                relative = True
                j += 1
            first = j if is_name(j) and not is_name(j, "import") else None
            k = after_dotted(j) if first is not None else j
            if is_name(k, "import"):  # `from X import ...` (not `yield from` / `raise ... from`)
                if first is not None and not relative and sig[first].string == pkg:
                    refs.add(first)
                end = k
                while end < len(sig) and sig[end].type not in (tokenize.NEWLINE, tokenize.ENDMARKER) and not is_op(end, ";"):
                    end += 1
                in_import.update(range(i, end))
                i = end
                continue
        elif is_name(i, "import"):
            j = i + 1
            while is_name(j):
                first = j
                j = after_dotted(j)
                is_pkg = sig[first].string == pkg
                if is_pkg:
                    refs.add(first)
                if is_name(j, "as"):
                    j += 2
                elif is_pkg:
                    bound = True
                if not is_op(j, ","):
                    break
                j += 1
            in_import.update(range(i, j))
            i = j
            continue
        i += 1
    used: set[int] = set()  # offsets of the names that resolve to the bound package (ast)
    uses = _package_uses(body, pkg) if bound else None
    if uses is not None:
        used.update(positions.ast(line, col) for line, col in uses)
    elif bound:  # no ast (syntax newer than this Python): pkg.core.fn(), but not obj.pkg, pkg = ..., f(pkg=...)
        for idx in range(len(sig)):
            if idx in refs or idx in in_import or not is_name(idx, pkg):
                continue
            if idx > 0 and (is_op(idx - 1, ".") or is_name(idx - 1, "def") or is_name(idx - 1, "class")):
                continue
            debug_field = is_op(idx + 2, "}") or is_op(idx + 2, "!") or is_op(idx + 2, ":")
            if is_op(idx + 1, "=") and not debug_field:  # f"{pkg=}" is a use, not an assignment
                continue
            refs.add(idx)

    regions: list[_Region] = []
    module_names = {offset(sig[i].start) for i in _module_name_strings(sig)}  # sys.modules["alpha"]...
    depth = 0
    fstart = 0
    floader = ""
    last: list[tokenize.TokenInfo] = []  # the last two significant tokens
    # Open brackets: [the called name (for a call), index of the current argument, its keyword]
    brackets: list[list[str | int | None]] = []

    def loader_argument() -> str:
        """The loader (LOADERS) whose module-name argument a string that starts here is, else ""."""
        if not (brackets and last and last[-1].type == tokenize.OP and last[-1].string in ("(", ",", "=")):
            return ""
        callee, index, keyword = brackets[-1]
        positions = LOADERS.get(callee, ()) if isinstance(callee, str) else ()
        found = bool(positions) and (keyword in MODULE_ARGUMENTS if keyword is not None else index in positions)
        return str(callee) if found else ""

    for tok in tokens:
        kind = tokenize.tok_name.get(tok.type, "")
        if kind.endswith("STRING_START"):  # Python 3.12+: f-strings, t-strings (and any later family) are text
            if depth == 0:
                fstart, floader = offset(tok.start), loader_argument()  # import_module(f"alpha.{x}") as on 3.11
            depth += 1
        elif kind.endswith("STRING_END"):
            depth -= 1
            if depth == 0:  # {fields} are code, as on 3.11
                forced = bool(floader) or fstart in module_names
                regions.append(_Region(fstart, offset(tok.end), forced, fstring=True, resource=floader in RESOURCE_FUNCTIONS))
        elif depth == 0 and tok.type in (tokenize.STRING, tokenize.COMMENT):
            loader, fstring = "", False
            if tok.type == tokenize.STRING:
                loader = loader_argument()
                prefix = re.match(r"[A-Za-z]*", tok.string)
                fstring = prefix is not None and "f" in prefix.group().lower()
            forced = bool(loader) or offset(tok.start) in module_names
            regions.append(_Region(offset(tok.start), offset(tok.end), forced, fstring, resource=loader in RESOURCE_FUNCTIONS))
        elif depth == 0 and tok.type == tokenize.OP:
            if tok.string in "([{":
                called = tok.string == "(" and last and last[-1].type == tokenize.NAME
                brackets.append([last[-1].string if called else None, 0, None])
            elif tok.string in ")]}" and brackets:
                brackets.pop()
            elif tok.string == "," and brackets:
                brackets[-1][1:] = [int(brackets[-1][1] or 0) + 1, None]
            elif tok.string == "=" and brackets and len(last) == 2 and last[1].type == tokenize.NAME and last[0].string in ("(", ","):
                brackets[-1][2] = last[1].string  # f(name=...): a keyword argument
        if tok.type not in trivia:
            last = [*last[-1:], tok]
    return _Code(
        names={offset(t.start): t.string for t in sig if t.type == tokenize.NAME},
        refs={offset(sig[i].start) for i in refs} | used,
        bound=bound,
        regions=sorted(regions, key=lambda r: r.start),
        scoped=uses is not None,
        positions=positions,
    )


def _in_fstring_field(text: str, region: _Region, pos: int) -> bool:
    """Whether `pos` is inside a {replacement field} of an f-string token (not in `{{`)."""
    token = text[region.start : region.end]
    m = re.match(r"[A-Za-z]*('''|\"\"\"|'|\")", token)
    if m is None:
        return False
    i = region.start + m.end()
    depth = 0
    while i < pos:
        char = text[i]
        if depth == 0 and char in "{}" and text[i + 1 : i + 2] == char:
            i += 2  # {{ or }}: a literal brace
            continue
        if char == "{":
            depth += 1
        elif char == "}" and depth:
            depth -= 1
        i += 1
    return depth > 0


def _whole_word(text: str, start: int, end: int) -> bool:
    """Whether the match is a word of its own and not part of a hyphenated name (`my-app-2`)."""
    if start >= 2 and text[start - 1] == "-" and _is_word(text[start - 2]):
        return False
    if end + 1 < len(text) and text[end] == "-" and _is_word(text[end + 1]):
        return bool(_BACKEND_SUFFIX.match(text, end))  # dist/<name>-cpython-exe is the name
    return True


def _inside_package(text: str, start: int, pkg: str) -> bool:
    """Whether the path segment at `start` follows the package folder: `src/alpha/alpha`."""
    sep = start
    while sep > 0 and text[sep - 1] in "/\\":  # "src\\alpha\\alpha" in Python source: two characters
        sep -= 1
    seg = sep - len(pkg)
    if seg < 0 or text[seg:sep] != pkg:
        return False
    if seg >= 1 and _is_word(text[seg - 1]):
        return False
    if pkg == "src" and not _after_src(text, seg):
        return False  # an app named src: in src/src/core the first one is the project's folder
    return not (seg >= 2 and text[seg - 1] == "-" and _is_word(text[seg - 2]))


def _after_src(text: str, start: int) -> bool:
    """Whether the path segment at `start` is right inside a `src` folder: `src/alpha`, `src\\alpha`."""
    sep = start
    while sep > 0 and text[sep - 1] in "/\\":
        sep -= 1
    if sep == start or text[sep - 3 : sep] != "src":
        return False
    return not (sep >= 4 and (_is_word(text[sep - 4]) or text[sep - 4] == "-"))


def _names_a_file(text: str, start: int, end: int, modules: frozenset[str]) -> bool:
    """Whether the occurrence names a file after the app (`alpha.png`, `sfx/alpha.wav`,
    "alpha.json"): `.` and a word that is no module or subpackage of src/<pkg>/ (`modules`),
    outside an import, `-m` or package context. Such a file keeps its name, so a rewritten
    reference no longer found it. An artifact the next build names after the new name
    (`alpha.exe`, `alpha-cpython-exe`) is no such file."""
    member = re.match(r"\.([A-Za-z0-9_]+)", text[end : end + 65])
    if member is None or member.group(1) in modules or _ARTIFACT_SUFFIX.match(text, end) or _BACKEND_SUFFIX.match(text, end):
        return False
    before = text[max(0, start - 80) : start]
    contexts = (_IMPORT_BEFORE, _FROM_BEFORE, _DASH_M_BEFORE, _PKG_WORD_BEFORE)
    return not any(pattern.search(before) for pattern in contexts)


_SEGMENT = re.compile(r"[/\\]+([^/\\\s'\"`,;:()\[\]{}<>|*?]+)")


def _names_another_folder(text: str, start: int, end: int, entries: frozenset[str]) -> bool:
    """Whether a path segment named like the app is a folder other than src/<pkg>/, the one the
    rename moves: `tests/alpha/data`, `tests/alpha/core/`, `assets/alpha/logo.png`, `docs/alpha/`.
    Under another folder the package is only right inside src/ (`src/alpha/x`); a path that starts
    with the name is the package when it leads into an entry of src/<pkg>/ (`alpha/core/x.py`,
    `alpha/data.json`: `entries`) or ends there (`alpha/`). Such a folder keeps its name, so the
    reference is kept and reported, as a file named after the app is (_names_a_file): it was
    rewritten, and a test or the app no longer found its files. A file or artifact name
    (`alpha.png`, `alpha-cpython-exe`) is no folder here."""
    prev, nxt = text[start - 1 : start], text[end : end + 1]
    if prev in ("/", "\\"):
        return nxt not in ("-", ".") and not _after_src(text, start)
    if nxt in ("/", "\\"):
        into = _SEGMENT.match(text, end)
        return into is not None and into.group(1) not in entries  # alpha/logo.png: an asset folder named alpha
    return False


def _text_kind(
    text: str,
    start: int,
    end: int,
    word: str,
    names: Names,
    *,
    contextual: bool = False,
    modules: frozenset[str] | None = None,
    entries: frozenset[str] | None = None,
) -> Kind:
    """Classify an occurrence in text (a string, a comment, a Markdown or TOML file).

    `contextual`: decide by context even when the new name is also a package name (pytemplate.toml,
    where `[app]`, `editor = ` or a button named like the app must never change). There a path is
    the package only right inside `src/` (the folder the rename moves: `tools/alpha.py`,
    `assets/alpha.ico` and `./pyt` stay) and, when `modules` (the modules and subpackages of
    src/<pkg>/) is given, a dotted word only when it names one of them (`alpha.core`; not
    `alpha.ico` or `uv.lock`). Elsewhere (src/, tests/), with `entries` (the names in src/<pkg>/),
    a path segment is the package only on the way into it: _names_another_folder.
    """
    prev = text[start - 1 : start]
    if prev == "." and start >= 2 and _is_word(text[start - 2]):
        return "keep"  # x.alpha: a submodule or an attribute, never the top-level package
    if word in presets.LAUNCHER_NAMES and _LAUNCHER_BEFORE.search(text[max(0, start - 3) : start]) and text[end : end + 1] not in ("/", "\\"):
        return "keep"  # an app named pyt (new refuses the name now): `./pyt report` is the launcher
    if prev in ("/", "\\") and _inside_package(text, start, names.old_pkg):
        return "keep"  # src/alpha/alpha: a submodule of the package, like alpha.alpha
    if names.old_pkg == "src" and text[end : end + 1] in ("/", "\\") and not _after_src(text, start):
        return "skip"  # an app named src (made by hand: new refuses it): src/src/x is the folder, then the package
    if not contextual and modules is not None and _names_a_file(text, start, end, modules):
        return "keep"  # asset("alpha.png"), "alpha.json": the file keeps its name (reported)
    if not contextual and entries is not None and _names_another_folder(text, start, end, entries):
        return "keep"  # tests/alpha/data, asset("alpha/logo.png"): the folder keeps its name (reported)
    if names.old_name != names.old_pkg:
        if not contextual:
            return "pkg" if word == names.old_pkg else "name"
        if word != names.old_pkg:
            return "name"  # the display name (My-Game): never a package reference
        # pytemplate.toml: the package spelling goes through the context rules below, as for an
        # app named like its package (every "auto" value of an app named Auto was rewritten)
    if names.new_name == names.new_pkg and not contextual:
        return "pkg"  # the new name is also the new package: no need to tell them apart
    if _ARTIFACT_SUFFIX.match(text, end) or _BACKEND_SUFFIX.match(text, end):
        return "name"
    nxt = text[end : end + 1]
    nxt2 = text[end + 1 : end + 2]
    if nxt in ("/", "\\") or prev in ("/", "\\"):
        return "pkg" if not contextual or _after_src(text, start) else "name"
    if nxt in (".", ":") and (nxt2 == "*" or (nxt2 != "" and _is_word(nxt2))):
        member = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[end + 1 : end + 65])
        if not contextual or modules is None or nxt == ":" or nxt2 == "*" or (member is not None and member.group() in modules):
            return "pkg"
    before = text[max(0, start - 80) : start]
    if _PKG_WORD_AFTER.match(text, end) or _PKG_WORD_BEFORE.search(before):
        return "pkg"
    if _IMPORT_BEFORE.search(before) or _DASH_M_BEFORE.search(before):
        return "pkg"
    if _FROM_BEFORE.search(before) and _IMPORT_AFTER.match(text, end):
        return "pkg"
    return "name"


def _escaped(text: str, start: int, floor: int, *, raw: bool, escapes: frozenset[str]) -> Kind | None:
    """An occurrence right after an odd number of backslashes inside a string (which starts at
    `floor`): "skip" when that backslash makes its first letter an escape (`\\n`, `\\x41`: not the
    name at all), else "keep" (a raw string's `\\d`, a path like `src\\alpha`: rewritten, it could
    become another escape or regex; reported, never changed). None: no backslash before it."""
    i = start
    while i > floor and text[i - 1] == "\\":
        i -= 1
    if (start - i) % 2 == 0:
        return None
    return "skip" if not raw and text[start] in escapes else "keep"


# A \N{...} escape up to an occurrence inside its braces, and the rest of it after one: a
# character's name (letters, digits, blanks and hyphens; matched without case: \N{bullet})
_NAMED_ESCAPE_BEFORE = re.compile(r"(?<!\\)(\\+)N\{[A-Za-z0-9 \-]*\Z")
_NAMED_ESCAPE_AFTER = re.compile(r"[A-Za-z0-9 \-]*\}")


def _named_escape(text: str, start: int, end: int, floor: int, *, raw: bool, unicode: bool) -> Kind | None:
    """An occurrence inside the braces of a \\N{...} escape of a string that starts at `floor`:
    "skip" in a str literal that is not raw (an escape, `"\\N{greek small letter alpha}"`: renamed,
    the file stopped compiling, "unknown Unicode character name", or named another character
    without a word), else "keep": a raw string's (or an escaped backslash's) \\N{...}, which the re
    module reads as the same escape, reported, never changed. None: not in one."""
    m = _NAMED_ESCAPE_BEFORE.search(text, max(floor, start - 100), start)
    if m is None or _NAMED_ESCAPE_AFTER.match(text, end) is None:
        return None
    return "skip" if unicode and not raw and len(m.group(1)) % 2 == 1 else "keep"


def _after_an_escape(text: str, start: int, floor: int, *, raw: bool, escapes: frozenset[str]) -> bool:
    """Whether the occurrence at `start` (an _escaped_pattern match, in a string that starts at
    `floor`) follows an escape of one letter (`"Usage:\\nalpha"`): then it starts a word of the
    string's text. A raw string's `\\n` is two characters, `\\\\nalpha` an escaped backslash."""
    return start - 1 > floor and text[start - 1] in (escapes & _LETTER_ESCAPES) and _escaped(text, start - 1, floor, raw=raw, escapes=escapes) == "skip"


def _escape_read_as_blank(text: str, end: int, *, raw: bool, escapes: frozenset[str]) -> str:
    """`text` as the context rules read it after an occurrence that ends at `end` in a string:
    an escape right after it (`"alpha\\n"`, `"alpha\\tready"`) is no path separator, so its
    backslash reads as a blank (it was taken for `alpha/n`, a folder named like the app, and the
    line was kept). An escaped backslash (`"alpha\\\\data"`) still is one; offsets stay the same."""
    if not raw and text[end : end + 1] == "\\" and text[end + 1 : end + 2] in escapes:
        return f"{text[:end]} {text[end + 1 :]}"
    return text


def _directive(text: str, start: int, end: int, floor: int) -> bool:
    """Whether a one-letter occurrence in a string (which starts at `floor`) is format syntax: a
    printf or strftime directive (`"%d" % x`, `strftime("%Y-%m")`: renamed, the date format and a
    struct format broke silently), a str.format one (`"{:d}".format(x)`, `"{!r}"`), or a struct
    format character after a byte order or count (`struct.pack(">I", n)`). Whether the string is
    ever formatted is unknown: it is reported, never changed."""
    letter = text[start]
    if end - start != 1 or not (letter.isascii() and letter.isalpha()):
        return False
    before = text[max(floor, start - 100) : start]  # a directive is short: never scan a whole docstring
    if _PRINTF_BEFORE.search(before):
        return True
    if letter in _DIRECTIVE_LETTERS and text[end : end + 1] in ("}", ":") and _FORMAT_BEFORE.search(before) is not None:
        return True
    return letter in _STRUCT_LETTERS and _STRUCT_BEFORE.fullmatch(before) is not None


def _string_quote(text: str, region: _Region) -> tuple[int, str] | None:
    """(offset of the opening quote, prefix) of a string region (`rb"..."`); None for a comment."""
    m = _STRING_PREFIX.match(text, region.start)
    return None if m is None else (m.end(), m.group().lower())


def _classify(
    text: str,
    start: int,
    end: int,
    word: str,
    names: Names,
    code: _Code | None,
    *,
    contextual: bool = False,
    modules: frozenset[str] | None = None,
    entries: frozenset[str] | None = None,
    escaped_before: bool = False,
) -> Kind:
    """`escaped_before`: an _escaped_pattern match (right after a backslash and a letter), which
    counts only after an escape of one letter in a string; the caller checks it in a data file."""
    if code is None:
        if not _whole_word(text, start, end):
            return "skip"
        return _text_kind(text, start, end, word, names, contextual=contextual, modules=modules, entries=entries)
    token = code.names.get(start)
    if token is not None:  # code
        if token != word:
            return "skip"  # e.g. `My` of the expression My-Game
        return "pkg" if start in code.refs else "keep"
    region = code.region_at(start)
    if region is None:
        return "skip"
    quote = _string_quote(text, region)
    if quote is not None and start < quote[0]:
        return "skip"  # the string prefix (f, r, b, rb...): syntax, never the name
    if quote is not None:  # \N{greek small letter alpha}: a character's name (an f-string's too)
        named = _named_escape(text, start, end, quote[0], raw="r" in quote[1], unicode="b" not in quote[1])
        if named is not None:
            return named
    in_field = region.fstring and _in_fstring_field(text, region, start)
    if escaped_before and (quote is None or in_field):
        return "skip"  # a comment or a field's code: no escape there
    if in_field:  # code, or the format spec of a field
        if code.scoped:
            return "pkg" if start in code.refs else "keep"
        return "pkg" if word == names.old_pkg and code.bound and text[start - 1 : start] != "." else "keep"
    if quote is not None:
        prefix = quote[1]
        raw, escapes = "r" in prefix, (_BYTES_ESCAPES if "b" in prefix else _PY_ESCAPES)
        if escaped_before and not _after_an_escape(text, start, quote[0], raw=raw, escapes=escapes):
            return "skip"  # a raw string's \nalpha, an escaped backslash's: one longer word
        escaped = _escaped(text, start, quote[0], raw=raw, escapes=escapes)
        if escaped is not None:
            return escaped
        if _directive(text, start, end, quote[0]):
            return "keep"
        text = _escape_read_as_blank(text, end, raw=raw, escapes=escapes)  # "alpha\n" leads into no folder n
    if not _whole_word(text, start, end):
        return "skip"
    if region.forced:  # a loader's argument is a module
        if region.resource and _text_kind(text, start, end, word, names, modules=modules, entries=entries) == "keep":
            # read_text("alpha.txt"), contents("alpha/data"): a helper of the project named like a
            # loader, reading a file or folder named after the app, which keeps its name
            return "keep"
        return "pkg" if (kind := _text_kind(text, start, end, word, names)) == "name" else kind
    # contextual (references_left): a string's or a comment's prose is no package reference
    return _text_kind(text, start, end, word, names, contextual=contextual, modules=modules, entries=entries)


def _toml_strings(text: str) -> list[tuple[int, int, bool]]:
    """(start, end, literal) of every string of a TOML text, in order (a text it cannot scan:
    the strings found up to there)."""
    out: list[tuple[int, int, bool]] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "#":  # a comment: its quotes and backslashes are plain text
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
        elif char in "\"'":
            try:
                end = config._string_end(text, i)
            except config._ScanError:
                break
            out.append((i, end, char == "'"))
            i = end
        else:
            i += 1
    return out


def _json_strings(text: str) -> list[tuple[int, int, bool]]:
    """(start, end, False) of every string of a JSON text: a quote starts one only there. YAML's
    double-quoted scalars read the same way (on one line: the usual form)."""
    return [(m.start(), m.end(), False) for m in _JSON_STRING.finditer(text)]


def _toml_key(text: str, start: int, end: int) -> bool:
    """Whether the occurrence is a TOML key or a table header (`[alpha]`, `alpha = `, `x.alpha.y =`)."""
    line_start = text.rfind("\n", 0, start) + 1
    return bool(_TOML_KEY_BEFORE.fullmatch(text, line_start, start) and _TOML_KEY_AFTER.match(text, end))


@functools.cache
def _config_keys() -> dict[str, frozenset[str]]:
    """table -> its keys in the pytemplate.toml schema (app -> name, preset, gui...)."""
    out: dict[str, frozenset[str]] = {}

    def walk(cls: type[object], prefix: str) -> None:
        fields = dataclasses.fields(cls)  # type: ignore[arg-type]
        out[prefix] = frozenset(f.name for f in fields)
        for f in fields:
            default = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default
            if dataclasses.is_dataclass(default):
                walk(type(default), f"{prefix}.{f.name}".lstrip("."))

    walk(Config, "")
    out["preset"] = frozenset(presets.available())
    return out


def _config_path(text: str, end: int, word: str) -> bool:
    """Whether `word` starts a pytemplate.toml key path here (`app.gui`, `deploy.exe.mode`): an app
    named like a table must keep the comments that talk about its keys."""
    m = re.match(r"\.([A-Za-z_][A-Za-z0-9_]*)", text[end : end + 64])
    return m is not None and m.group(1) in _config_keys().get(word, frozenset())


def module_value_lines(text: str, keys: Iterable[str] = MODULE_KEYS) -> set[int]:
    """Line numbers (1-based) holding the value of one of `keys` ("table.key", multi-line
    arrays included) in a TOML text: the current table header and key are tracked line by line."""
    wanted = set(keys)
    out: set[int] = set()
    table = key = ""
    for n, line in enumerate(text.split("\n"), 1):
        head = _TOML_TABLE.fullmatch(line.rstrip("\r"))
        if head is not None:
            table, key = re.sub(r"[ \t]", "", head.group(1)), ""
            continue
        m = _TOML_ASSIGN.match(line)
        if m is not None:
            key = re.sub(r"[ \t]", "", m.group(1))
        if key and line.strip() and f"{table}.{key}".lstrip(".") in wanted:
            out.add(n)
    return out


def rewrite(
    text: str,
    names: Names,
    *,
    python: bool = False,
    only_pkg: bool = False,
    toml: bool = False,
    strings: str = "",
    module_keys: frozenset[str] = frozenset(),
    package_modules: frozenset[str] | None = None,
    package_entries: frozenset[str] | None = None,
) -> Rewrite:
    """Replace the old name/package in `text`. Line endings and everything else are kept.

    `python`: tell code from strings and comments with the tokenizer. `only_pkg`: change only
    the package references, chosen by context, and report the other occurrences as kept
    (pytemplate.toml; a Python file's strings and comments for references_left). `toml`: TOML
    keys and table headers never change. `strings` ("toml",
    "json", "yaml"; `toml` implies "toml"): the string syntax of a data file, whose escapes are never the
    name (other text is plain: `a\\n` has no escape there). `module_keys`: TOML
    keys whose quoted values are module names (a bare old package there is the package).
    `package_modules`: the modules and subpackages of src/<old pkg>/ (only_pkg: `pkg.x` is the
    package only when x is one of them; `pkg.ico`, `uv.lock` are file names; in other text a
    file named after the app, `asset("pkg.png")`, is kept and reported: _names_a_file).
    `package_entries`: the names of the files and folders in src/<old pkg>/ (other text: a path
    segment is the package only right inside src/ or on its way into one of them; another folder
    named like the app, `tests/pkg/data`, is kept and reported: _names_another_folder).
    A text that never mentions the old name is searched once and returned as it is: tokenized and
    split into lines three times, a data asset of 100 MB (a level, a CSV) took 11 s and ~950 MB.
    """
    pattern = _pattern(names)
    if not _mentioned(text, names):
        return Rewrite(text=text)
    code = _python_code(text, names.old_pkg) if python else None
    if code is not None and code.bound and names.new_pkg != names.old_pkg:
        _keep_captured(code, names)  # renamed, they would read another binding of the new name
    module_lines = module_value_lines(text, module_keys) if module_keys else set()
    line_starts = [0, *(m.end() for m in re.finditer("\n", text))]
    syntax = "toml" if toml else strings
    found = _toml_strings(text) if syntax == "toml" else _json_strings(text) if syntax in ("json", "yaml") else []
    escapes = {"json": _JSON_ESCAPES, "yaml": _YAML_ESCAPES}.get(syntax, _TOML_ESCAPES)
    string_starts = [s[0] for s in found]
    pieces: list[str] = []
    last = 0
    count = 0
    kept_at: list[int] = []
    # Every occurrence, in order: the name as a word of its own, and right after an escape of one
    # letter ("Usage:\nalpha"), which only a string's escapes make one
    matches = sorted([*((m, False) for m in pattern.finditer(text)), *((m, True) for m in _escaped_pattern(names).finditer(text))], key=lambda found_at: found_at[0].start())
    for m, escaped_before in matches:
        start, end = m.span()
        word = m.group()
        if toml and _toml_key(text, start, end):
            continue
        view = text  # the text the context rules read (_escape_read_as_blank)
        i = bisect.bisect_right(string_starts, start) - 1
        if i >= 0 and start < found[i][1]:  # inside a TOML or JSON string: its escapes are never the name
            raw = found[i][2]
            if escaped_before and not _after_an_escape(text, start, found[i][0], raw=raw, escapes=escapes):
                continue
            escaped = _escaped(text, start, found[i][0], raw=raw, escapes=escapes)
            if escaped == "skip":
                continue
            if escaped == "keep":
                kept_at.append(start)
                continue
            view = _escape_read_as_blank(text, end, raw=raw, escapes=escapes)
        elif escaped_before and code is None:
            continue  # plain text (Markdown, a comment of a data file): `\nalpha` is no escape there
        kind = _classify(
            view, start, end, word, names, code, contextual=only_pkg, modules=package_modules, entries=package_entries, escaped_before=escaped_before
        )
        if kind == "skip":
            continue
        module_line = bool(module_lines) and bisect.bisect_right(line_starts, start) in module_lines
        if (
            kind == "name"
            and module_line
            and word == names.old_pkg
            and text[start - 1 : start] in ("'", '"')
            and (text[end : end + 1] == text[start - 1 : start] or text[end : end + 1] in (".", ":"))
        ):
            kind = "pkg"  # modules = ["alpha"], exclude = ["alpha.slow"] (a module that does not exist yet)
        elif kind == "pkg" and toml and not module_line and _config_path(text, end, word):
            kind = "keep"  # "# auto (= not app.gui)" in a project named app
        if kind == "keep" or (only_pkg and kind == "name"):
            kept_at.append(start)
            continue
        new = names.new_pkg if kind == "pkg" else names.new_name
        if new == word:
            continue
        pieces += [text[last:start], new]
        last = end
        count += 1
    pieces.append(text[last:])
    new_text = "".join(pieces)
    kept_lines = sorted({bisect.bisect_right(line_starts, pos) for pos in kept_at})
    old_lines = text.split("\n") if kept_lines else []
    return Rewrite(
        text=new_text,
        count=count,
        changes=_line_changes(text, new_text) if count else [],
        kept=[(n, old_lines[n - 1].rstrip("\r")) for n in kept_lines],
        note="the tokenizer rejected it: rewritten as plain text" if python and code is None else "",
    )


def _line_changes(old: str, new: str) -> list[tuple[int, str, str]]:
    """(line number, old line, new line) of each changed line (appended lines are ignored)."""
    pairs = zip(old.split("\n"), new.split("\n"), strict=False)
    return [(i + 1, a.rstrip("\r"), b.rstrip("\r")) for i, (a, b) in enumerate(pairs) if a != b]


# --- planning a whole project ------------------------------------------------------------------


@dataclass
class FileEdit:
    path: str  # relative to the project root, before the move
    target: str  # where it ends up (after the move)
    old: bytes
    new: bytes
    result: Rewrite
    encoding: str = "utf-8"  # a Python file with a PEP 263 cookie keeps its own encoding


@dataclass
class TextEdit:
    path: str  # pytemplate.toml | pyproject.toml
    old: str
    new: str
    count: int  # references changed besides the name itself
    kept: list[tuple[int, str]]
    detail: str  # e.g. [project] name = "My-Game"
    bom: bool = False  # written back with its UTF-8 BOM (pytemplate.toml keeps it, like config.update_file)

    @property
    def changes(self) -> list[tuple[int, str, str]]:
        return _line_changes(self.old, self.new)


@dataclass
class Plan:
    names: Names
    move: tuple[str, str] | None  # (src/<old dir as on disk>, src/<new pkg>)
    files: list[FileEdit]  # src/ and tests/ files with changes or kept occurrences
    config: TextEdit
    pyproject: TextEdit | None
    binary: list[str]  # files skipped because they are not UTF-8 text
    mentions: list[str]  # other files of the project that mention the old name: not changed
    unreadable: list[str] = field(default_factory=list)  # skipped (not UTF-8 text) but mention the old name
    linked: list[str] = field(default_factory=list)  # links/junctions in src/, tests/ that mention it: never rewritten

    @property
    def changed_files(self) -> list[FileEdit]:
        return [f for f in self.files if f.old != f.new]


def _decode(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# UTF-16 and UTF-32 text, which holds NUL bytes, is known by its BOM (PowerShell 5.1's `>` and
# Out-File write UTF-16 LE with one); the longest first: FF FE 00 00 is UTF-32 LE
_BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))


def _bom_text(data: bytes) -> str | None:
    """The text of a UTF-16 or UTF-32 file that starts with its BOM (None: another file). It is
    never rewritten, only searched for the old name: skipped as a binary, it was never named."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                return None
    return None


def _source_encoding(data: bytes) -> str | None:
    """The PEP 263 encoding a Python source declares when it is not UTF-8 (None: none usable)."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError:
        return None
    return None if encoding in ("utf-8", "utf-8-sig") else encoding


def _is_link(path: Path) -> bool:
    """A symbolic link or a Windows junction (which os.walk and Path.is_symlink do not see)."""
    from .cmd_env import _is_link as is_link  # reads st_reparse_tag (Python 3.11 has no is_junction)

    return is_link(path)


def _code_files(root: Path, links: list[str] | None = None) -> Iterator[tuple[str, Path]]:
    """Every regular file of src/ and tests/ (no caches), sorted. Links and junctions are never
    followed (their target may be shared with other projects): they go to `links` (a folder
    with a trailing slash), src/ and tests/ themselves too (os.walk follows its top: a tests/
    shared through a link was rewritten for every project that shares it)."""
    def unlistable(e: OSError) -> None:
        """A folder that cannot be listed stops the plan, as an unreadable file does: os.walk skipped
        it, and its files kept the old imports inside the moved package."""
        where = str(e.filename or "a folder of src/ or tests/")
        with contextlib.suppress(ValueError):
            where = Path(where).relative_to(root).as_posix()
        raise PytError(
            f"rename: cannot list {where}/: {e.strerror or e}; nothing was changed.\n"
            "  Make it readable (or move it out of src/ and tests/) and try again"
        )

    for top in ("src", "tests"):
        base = root / top
        if _is_link(base):
            if links is not None:
                links.append(f"{top}/")
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, onerror=unlistable):
            here = Path(dirpath)
            kept: list[str] = []
            for d in sorted(dirnames):
                if d in SKIP_DIRS or d.startswith(".venv"):
                    continue
                if _is_link(here / d):
                    if links is not None:
                        links.append((here / d).relative_to(root).as_posix() + "/")
                    continue
                kept.append(d)
            dirnames[:] = kept
            for filename in sorted(filenames):
                path = here / filename
                if _is_link(path):
                    if links is not None:
                        links.append(path.relative_to(root).as_posix())
                elif path.is_file():
                    yield path.relative_to(root).as_posix(), path


def _mentions_name(path: Path, pattern: re.Pattern[str]) -> bool:
    """Whether a text file (a regular file of at most MENTION_MAX_BYTES) mentions the old name.
    A named pipe, a socket or a device is never read: opening a FIFO waits for a writer, and the
    rename (or apply after an app.name edit, --dry-run too) hung forever."""
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MENTION_MAX_BYTES:
            return False
        data = path.read_bytes()
    except OSError:
        return False
    text = _decode(data)
    if text is None:
        text = _bom_text(data)
    return text is not None and pattern.search(text) is not None


def _dangles(link: Path, move: tuple[Path, Path] | None) -> bool:
    """Whether the link resolves into the package folder that `move` (old, new) moves and no
    longer resolves to the same file once it moved (tests/data -> ../src/alpha/data). Its target
    text is never searched for the name: an absolute target through the project's own folder,
    named like the app, was reported although the link kept working."""
    if move is None:
        return False
    old, new = move
    try:
        target = os.readlink(link)
    except (OSError, ValueError):
        return False  # a junction on an old Python, or not readable: what it holds is looked at
    if target.startswith(("\\\\?\\", "\\??\\")):  # a Windows junction's target
        target = target[4:]
    lexical = Path(os.path.normpath(link.parent / target))
    real, real_old = Path(os.path.realpath(lexical)), Path(os.path.realpath(old))
    if real != real_old and real_old not in real.parents:
        return False  # it names nothing the rename moves
    if lexical != old and old not in lexical.parents:
        return True  # through another spelling of the folder (a linked parent folder): it stays there
    parent = new / link.parent.relative_to(old) if old in link.parents else link.parent  # the link moves too
    return Path(os.path.normpath(parent / target)) != new / lexical.relative_to(old)


def _link_mentions(link: Path, pattern: re.Pattern[str], move: tuple[Path, Path] | None = None) -> bool:
    """Whether a link or junction dangles once the package folder moves (_dangles), or its file,
    or a text file below its folder, mentions the old name."""
    if _dangles(link, move):
        return True
    if not link.is_dir():
        return link.is_file() and _mentions_name(link, pattern)
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(link, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:  # a link back up: a cycle
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames[:] = [d for d in dirnames if d not in MENTION_SKIP_DIRS and not d.startswith(".venv")]
        if any(_mentions_name(Path(dirpath) / f, pattern) for f in filenames):
            return True
    return False


def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _is_dir(entry: os.DirEntry[str]) -> bool:
    """os.DirEntry.is_dir() that says no, as os.path.isdir does, for an entry it cannot stat: a
    link loop, a dead mount, a link into a folder the user may not enter, a link Windows cannot
    follow (WinError 123); is_dir() itself ignores only a missing target."""
    try:
        return entry.is_dir()
    except OSError:
        return False


def package_dir(src: Path, pkg: str) -> Path | None:
    """Return src/<pkg>/ as spelled on disk (a case-insensitive file system may hold src/Alpha/).
    An entry of src/ that cannot be stat'ed is no folder (_is_dir): one such link hid the app
    package from doctor, the hook, rename and apply."""
    try:
        with os.scandir(src) as it:
            entries = list(it)
    except OSError:
        return None
    dirs = [Path(e.path) for e in entries if _is_dir(e)]
    for d in dirs:
        if d.name == pkg:
            return d
    for d in dirs:
        if d.name.lower() == pkg and same_file(d, src / pkg):
            return d
    return None


def _target(path: str, move: tuple[str, str] | None) -> str:
    if move is not None and (path == move[0] or path.startswith(move[0] + "/")):
        return move[1] + path[len(move[0]) :]
    return path


def _package_modules(root: Path, pkg: str) -> frozenset[str]:
    """The modules and subpackages of src/<pkg>/: `core` for core.py, core/ or core.<tag>.so."""
    folder = package_dir(root / "src", pkg)
    try:
        entries = list(os.scandir(folder)) if folder is not None else []
    except OSError:
        return frozenset()
    out: set[str] = set()
    for entry in entries:
        stem = entry.name.partition(".")[0]
        if entry.name in SKIP_DIRS or not stem.isidentifier():
            continue
        # a link Windows cannot follow (WinError 123) names no folder here
        if _is_dir(entry) or Path(entry.name).suffix in PY_SUFFIXES or entry.name.endswith(EXT_SUFFIXES):
            out.add(stem)
    return frozenset(out)


def _package_entries(root: Path, pkg: str) -> frozenset[str]:
    """The names of the files and folders right inside src/<pkg>/ (caches aside): a path that
    leads into one of them (`alpha/core/x.py`, `alpha/data.json`) is the package's."""
    folder = package_dir(root / "src", pkg)
    if folder is None:
        return frozenset()
    try:
        with os.scandir(folder) as it:
            return frozenset(e.name for e in it if e.name not in SKIP_DIRS)
    except OSError:
        return frozenset()


def _plan_config(root: Path, names: Names) -> TextEdit:
    path = root / "pytemplate.toml"
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise PytError(f"rename: cannot read pytemplate.toml: {e.strerror or e}") from None
    old = config._decode(raw, "pytemplate.toml")  # a clear error for UTF-16/ANSI; CRLF kept
    modules = _package_modules(root, names.old_pkg)
    result = rewrite(old, names, only_pkg=True, toml=True, module_keys=MODULE_KEYS, package_modules=modules)
    new = config.set_value(result.text, "app", "name", names.new_name)
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise PytError(f"rename: the new pytemplate.toml would not be valid TOML ({e}); nothing was changed") from None
    renamed = {n for n, _, _ in _line_changes(result.text, new)}
    kept = [(n, line) for n, line in result.kept if n not in renamed]  # app.name itself
    return TextEdit("pytemplate.toml", old, new, result.count, kept, f'app.name = "{names.new_name}"', bom=raw.startswith(b"\xef\xbb\xbf"))


@functools.cache
def _named_keys() -> frozenset[tuple[str, ...]]:
    """The keys of the presets' pyproject blocks whose value holds the app's name ({{name}},
    {{pkg}}), with their table: the only values of the block a rename rewrites."""
    out: set[tuple[str, ...]] = set()
    for preset in presets.available():
        template = str(presets.load(preset).get("pyproject", ""))
        for stmt in config.scan(template) or []:
            if stmt.kind == "key" and re.search(r"\{\{(?:name|pkg)\}\}", template[stmt.value[0] : stmt.value[1]]):
                out.add(stmt.path)
    return frozenset(out)


def _plan_pyproject(root: Path, names: Names) -> TextEdit | None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    try:  # bytes, so its line endings stay (a CRLF checkout); a BOM is dropped (written back without it)
        old = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise PytError(f"rename: cannot read pyproject.toml ({e}); nothing was changed") from None
    try:
        new = presets.set_project_name(old, names.new_name)
    except PytError as e:
        raise PytError(f"rename: {e}; nothing was changed") from None
    lines = new.split("\n")
    begin = next((i for i, ln in enumerate(lines) if ln.strip() == presets.EXTRA_BEGIN), None)
    end = next((i for i, ln in enumerate(lines) if ln.strip() == presets.EXTRA_END), None)
    count = 0
    if begin is not None and end is not None and end > begin + 1:
        # Only the values the preset writes with the name: flet's org = "com.example" is a
        # reverse domain, and for an app named com it became "beta.example"
        block, named, parts, pos = "\n".join(lines[begin + 1 : end]), _named_keys(), [], 0
        for stmt in config.scan(block) or []:
            if stmt.kind == "key" and stmt.path in named:
                value = rewrite(block[stmt.value[0] : stmt.value[1]], names, toml=True)
                parts += [block[pos : stmt.value[0]], value.text]
                pos, count = stmt.value[1], count + value.count
        lines[begin + 1 : end] = "".join([*parts, block[pos:]]).split("\n")
        new = "\n".join(lines)
    try:
        tomllib.loads(new)
    except config.TOML_ERRORS as e:  # an integer of 5000 digits, arrays nested a thousand deep: no TOMLDecodeError
        raise PytError(f"rename: the new pyproject.toml would not be valid TOML ({config.toml_error(e)}); nothing was changed") from None
    # What is left (other tables: [tool.coverage] source = ["alpha"]...) is only reported
    rest = rewrite(new, names, only_pkg=True, toml=True)
    left = {n for n, _, _ in rest.changes} | {n for n, _ in rest.kept}
    new_lines = new.split("\n")
    kept = [(n, new_lines[n - 1].rstrip("\r")) for n in sorted(left)]
    detail = f'[project] name = "{names.new_name}"' + (f" and {count} in the preset block" if count else "")
    return TextEdit("pyproject.toml", old, new, count, kept, detail)


def _mentions(root: Path, names: Names, skip: Iterable[str] = ()) -> list[str]:
    """Other text files of the project that mention the old name (not src/ or tests/, which are
    rewritten; not the generated files, which are re-rendered): listed, never changed."""
    pattern = _pattern(names)
    skipped = set(skip)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        top = here == root
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in MENTION_SKIP_DIRS
            and not d.startswith(".venv")
            and not (top and d in ("src", "tests", "build"))
            and not _is_link(here / d)  # a symlink or a junction: never followed
        )
        for filename in sorted(filenames):
            path = here / filename
            rel_path = path.relative_to(root).as_posix()
            if (top and filename in ROOT_SKIP) or rel_path in skipped or _is_link(path):
                continue
            if _mentions_name(path, pattern):
                out.append(rel_path)
    return out


def references_left(root: Path, names: Names) -> list[str]:
    """The files of the project at `root` that still name the old package of `names` where a
    rename rewrites it: pytemplate.toml (a package reference: compile.modules, deploy.wheel.entry,
    src/<old>/...) and the Python files of src/ and tests/ that import it (or use a name bound to
    such an import) or name it as a package in a string or a comment (`"old.core.x"` for a module
    of the package, wherever it is now: `import_module`, `mock.patch`; `"-m", "old"`), never in
    prose (a docstring that says the app replaces old). cmd_apply.moved_by_hand tells a package
    folder moved by hand (they are left) from a package written anew in place of the old one
    (none is). What cannot be read or planned counts: it may name it."""
    out: list[str] = []
    try:
        if _plan_config(root, names).count:
            out.append("pytemplate.toml")
    except PytError:
        out.append("pytemplate.toml")
    try:
        files = list(_code_files(root))
    except PytError:  # a folder of src/ or tests/ it cannot list
        return [*out, "src/ or tests/ (a folder that cannot be listed)"]
    modules = _package_modules(root, names.old_pkg) | _package_modules(root, names.new_pkg)  # where the package is now
    for rel_path, path in files:
        if path.suffix not in PY_SUFFIXES:
            continue
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            out.append(rel_path)
            continue
        if not _mentioned(text, names):
            continue
        code = _python_code(text, names.old_pkg)
        # `import old.x`, `from old import y` and the names bound to them (refs: before any capture
        # by the new name), then the package references of its strings and comments (only_pkg:
        # chosen by context, never prose)
        if code is None or code.refs or rewrite(text, names, python=True, only_pkg=True, package_modules=modules).count:
            out.append(rel_path)
    return out


def plan(root: Path, old_name: str, new_name: str, *, generated: Iterable[str] = ()) -> Plan:
    """Compute every change of renaming the project in `root` (nothing is written).

    `generated`: the generated files (re-rendered after the rename, so never listed as mentions).
    """
    names = Names(old_name, new_name)
    src = root / "src"
    if _is_link(src):
        # The package lives in it: the move would go through the link, into a folder other projects
        # may share, and its files would be left unchanged (a link is never followed)
        raise PytError(
            "rename: src/ is a symbolic link or junction: rename never writes through one (its target may be "
            "shared with other projects), and the package it would move lives there; nothing was changed.\n"
            "  Put a folder of the project in its place (a copy of what it points to), or rename by hand"
        )
    old_dir = package_dir(src, names.old_pkg)
    if old_dir is None:
        raise PytError(
            f"rename: src/{names.old_pkg}/ not found (app.name = '{old_name}' says the package is there)"
        )
    move: tuple[str, str] | None = None
    if old_dir.name != names.new_pkg:
        new_dir = src / names.new_pkg
        if new_dir.exists() and not same_file(old_dir, new_dir):
            raise PytError(f"rename: src/{names.new_pkg}/ already exists: move or delete it first")
        if (src / f"{names.new_pkg}.py").exists():
            raise PytError(f"rename: src/{names.new_pkg}.py exists: the package src/{names.new_pkg}/ would clash with it")
        move = (f"src/{old_dir.name}", f"src/{names.new_pkg}")
    files: list[FileEdit] = []
    binary: list[str] = []
    unreadable: list[str] = []
    pattern = _pattern(names)
    links: list[str] = []
    modules = _package_modules(root, names.old_pkg)  # `alpha.core` is the package, `alpha.png` a file
    entries = _package_entries(root, names.old_pkg)  # `alpha/core/x.py` is the package, `tests/alpha/` a folder
    for rel_path, path in _code_files(root, links):
        try:
            data = path.read_bytes()
        except OSError as e:  # a root-owned file (a container run), a file another program locks
            raise PytError(
                f"rename: cannot read {rel_path}: {e.strerror or e}; nothing was changed.\n"
                "  Make it readable (or move it out of src/ and tests/) and try again"
            ) from None
        text = _decode(data)
        encoding = "utf-8"
        if text is None and path.suffix in PY_SUFFIXES and b"\0" not in data:
            declared = _source_encoding(data)  # e.g. "# -*- coding: cp1252 -*-"
            if declared is not None:
                try:
                    text, encoding = data.decode(declared), declared
                except (UnicodeDecodeError, LookupError):
                    text = None
        if text is None:
            binary.append(rel_path)
            other = _bom_text(data)  # UTF-16/32 text; else an ANSI file, read byte for byte
            if other is None and b"\0" not in data:
                other = data.decode("latin-1")
            if other is not None and pattern.search(other):
                unreadable.append(rel_path)
            continue
        if not _mentioned(text, names):  # a file that never mentions it (a data asset): searched once
            continue
        strings = DATA_STRINGS.get(path.suffix.lower(), "")
        result = rewrite(text, names, python=path.suffix in PY_SUFFIXES, strings=strings, package_modules=modules, package_entries=entries)
        if result.count or result.kept:
            try:
                new = result.text.encode(encoding)
            except UnicodeEncodeError:  # cannot happen with an ASCII name in an ASCII-compatible codec
                binary.append(rel_path)
                unreadable.append(rel_path)
                continue
            files.append(FileEdit(rel_path, _target(rel_path, move), data, new, result, encoding))
    return Plan(
        names=names,
        move=move,
        files=files,
        config=_plan_config(root, names),
        pyproject=_plan_pyproject(root, names),
        binary=binary,
        mentions=_mentions(root, names, generated),
        unreadable=unreadable,
        linked=[rel for rel in links if _link_mentions(root / rel, pattern, (old_dir, src / names.new_pkg) if move else None)],
    )


def _move_dir(root: Path, old_rel: str, new_rel: str) -> None:
    old, new = root / old_rel, root / new_rel
    try:
        if new.exists() and same_file(old, new):  # case-only rename on a case-insensitive file system
            tmp = _case_tmp(old)
            old.rename(tmp)
            try:
                tmp.rename(new)
            except OSError:
                tmp.rename(old)
                raise
        else:
            old.rename(new)
    except OSError as e:
        raise PytError(
            f"rename: could not move {old_rel}/ to {new_rel}/: {e.strerror or e}. Nothing was changed.\n"
            "  Close the programs that use files in it (a running app, a terminal inside it) and try again"
        ) from None


def _case_tmp(old: Path) -> Path:
    """The temporary name of the package folder during a case-only move (_move_dir)."""
    return old.with_name(f"{old.name}.pt-rename-{os.getpid()}")


def _moved_to(root: Path, move: tuple[str, str]) -> str | None:
    """Where an interrupt that came as the package folder moved left it: its new name, the
    temporary one of a case-only move, or None while it keeps its old name. Read from the listing
    of src/, which spells the names as the disk does (on a case-insensitive file system `alpha`
    is found under either spelling)."""
    old, new = root / move[0], root / move[1]
    try:
        names = set(os.listdir(old.parent))
    except OSError:
        return None
    tmp = _case_tmp(old)
    if tmp.name in names:
        return tmp.relative_to(root).as_posix()
    if new.name in names and old.name not in names:
        return move[1]
    return None


def _replace_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` so that it is never left half-written, keeping its permissions,
    owner, group and hard links; a symlink stays a link and a read-only file is an error, as with
    a plain write (project.write_whole)."""
    write_whole(path, data)


def apply_plan(root: Path, plan_: Plan) -> None:
    """Write a plan: move src/<pkg>/ first (the step that can fail on a locked file), then the files.

    A file that cannot be written (read-only, locked by another program, a full disk) undoes what
    was already done: that file is never touched (`_replace_bytes`), the files written so far get
    their old bytes back and the folder moves back. The error says what could not be undone. A
    Ctrl+C, SIGTERM or SIGHUP is undone the same way, then goes on (SIGTERM and SIGHUP are
    exceptions only while it writes, as for `pyt install`: their default action ended the runner
    at once): it left the tree half-renamed with only `error: interrupted`. Another Ctrl+C waits
    for the undo (_undo_shield): it cut the undo short, and nothing said so."""
    from .cmd_install import _terminations_interrupt, _undo_shield  # the swap of `pyt install` uses the same

    # (path, new bytes, the text or bytes the plan read there): a file that no longer holds what was
    # planned (an editor saved it meanwhile) is never overwritten
    writes: list[tuple[Path, bytes, str | bytes]] = [(root / f.target, f.new, f.old) for f in plan_.changed_files]
    for edit in (plan_.config, plan_.pyproject):
        if edit is not None and edit.new != edit.old:
            writes.append((root / edit.path, (("\ufeff" if edit.bom else "") + edit.new).encode("utf-8"), edit.old))
    done: list[tuple[Path, bytes]] = []  # written, with their old bytes
    writing: tuple[Path, bytes] | None = None  # the write in progress
    moved = False
    with _terminations_interrupt():
        try:
            if plan_.move is not None:
                _move_dir(root, *plan_.move)  # a PytError there: nothing was changed
                moved = True
            for path, data, planned in writes:
                current = path.read_bytes()
                if not _as_planned(current, planned):
                    raise _ChangedSincePlan(path)
                writing = (path, current)
                _replace_bytes(path, data)
                done.append(writing)
                writing = None
        except _ChangedSincePlan as e:
            with _undo_shield():
                undone = _undo(root, plan_, done, moved)
            raise PytError(
                f"rename: {e.path.relative_to(root).as_posix()} changed after the rename was planned (an editor saved it?), "
                f"and is left as it is. {undone}.\n  Save your work, then run it again"
            ) from None
        except OSError as e:
            with _undo_shield():
                undone = _undo(root, plan_, done, moved)
            raise PytError(
                f"rename: could not write {path.relative_to(root).as_posix()}: {e.strerror or e}. {undone}.\n"
                "  Close the programs that use it (or make it writable, or free some disk space) and try again"
            ) from None
        except PytError:
            raise
        except BaseException:  # Ctrl+C, SIGTERM, SIGHUP (or a bug: its traceback follows)
            # the write in progress too: an interrupt may come right after it
            with _undo_shield():
                undone = _undo(root, plan_, [*done, *([writing] if writing else [])], moved)
            ui.error(f"rename: interrupted. {undone}")
            raise


class _ChangedSincePlan(Exception):
    """A file to write no longer holds what the plan read there (apply_plan)."""

    def __init__(self, path: Path) -> None:
        super().__init__(str(path))
        self.path = path


def _as_planned(current: bytes, planned: str | bytes) -> bool:
    """Whether a file's bytes are still what the plan read: the same bytes (src/, tests/), or the
    same text (pytemplate.toml and pyproject.toml, planned as text without their BOM)."""
    if isinstance(planned, bytes):
        return current == planned
    try:
        return current.decode("utf-8-sig") == planned
    except UnicodeDecodeError:
        return False


def _undo(root: Path, plan_: Plan, done: list[tuple[Path, bytes]], moved: bool) -> str:
    """Give the written files their old bytes back and move the folder back; say how it went."""
    lost: list[str] = []  # files that kept the new bytes
    for written, previous in reversed(done):
        try:
            _replace_bytes(written, previous)
        except OSError:
            lost.append(written.relative_to(root).as_posix())
    source: str | None = None  # where the package folder is now, when it moved
    if plan_.move is not None:
        # an interrupt as the folder moved, before `moved` was set: the listing says where it is
        source = plan_.move[1] if moved else _moved_to(root, plan_.move)
    back: tuple[str, str] | None = None  # where the lost files are now
    if source is not None and plan_.move is not None:
        try:
            _move_dir(root, source, plan_.move[0])
            back = (plan_.move[1], plan_.move[0])
        except PytError:
            pass
    problems = [f"{_target(p, back)} could not be restored" for p in reversed(lost)]
    if source is not None and plan_.move is not None and back is None:
        problems.append(f"{source}/ could not be moved back to {plan_.move[0]}/")
    if problems:
        return f"The rename was NOT fully undone: {'; '.join(problems)} (fix it by hand: git status shows what changed)"
    return "The rename was undone (the files written so far were restored)"


# --- checks ----------------------------------------------------------------------------------------


def locked_names(root: Path) -> set[str]:
    """Return the normalized names in uv.lock (indirect dependencies included), minus the project itself."""
    try:
        data = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return set()
    names: set[str] = set()
    packages = data.get("package", [])
    for entry in packages if isinstance(packages, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        source = entry.get("source")
        if isinstance(source, dict) and "." in (source.get("virtual"), source.get("editable")):
            continue  # the project's own entry
        names.add(presets._norm_name(entry["name"]))
    return names


def check_new_name(cfg: Config, new_name: str, *, who: str = "rename", retry: str = "./pyt rename OTHER_NAME") -> None:
    """Reject names that cannot work: format, keywords, stdlib modules, dependencies (uv.lock too)."""
    if not config.APP_NAME.fullmatch(new_name):
        raise PytError(f"{who}: the name may only contain {config.NAME_RULE}")
    pkg = package_of(new_name)
    if keyword.iskeyword(pkg):
        raise PytError(f"{who}: the package '{pkg}' would be a Python keyword (import {pkg} is a syntax error)")
    if presets.shadows_stdlib(pkg):
        raise PytError(f"{who}: src/{pkg}/ would shadow the standard library module '{pkg}'. Choose another name: {retry}")
    if pkg in config.BACKENDS:  # src/mypyc/ shadows mypy's compiler; tests/conftest.py and [backend] use these words
        raise PytError(f"{who}: '{new_name}' is the name of a backend ({', '.join(config.BACKENDS)}). Choose another name: {retry}")
    try:  # check_name_free reads it: a broken file is not a problem of the name
        presets.read_pyproject()
    except PytError as e:
        raise PytError(f"{who}: {e}: fix it first; nothing was changed") from None
    try:
        presets.check_name_free(cfg, cfg.app.preset, new_name)
    except PytError as e:
        reason = str(e).splitlines()[0]
        raise PytError(f"{who}: {reason}\n  Choose another name: {retry}") from None
    clash = presets._norm_name(new_name)
    if clash in locked_names(ROOT):
        raise PytError(
            f"{who}: '{new_name}' is also the name of a package in uv.lock ({clash}, a dependency of a dependency): "
            f"uv would refuse the project or resolve that dependency to the project itself, and src/{pkg}/ "
            f"would shadow the library.\n  Choose another name: {retry}"
        )


def _porcelain_paths(out: str, prefix: str) -> list[str]:
    """The paths of `git status --porcelain -z` (relative to the top), made relative to `prefix`."""
    fields = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # the next field is the original path of a rename or copy
        paths.append(path[len(prefix) :] if prefix and path.startswith(prefix) else path)
    return paths


def git_changes(root: Path) -> list[str] | str | None:
    """The paths (relative to `root`, POSIX) that `git status` reports as changed or untracked.

    None: `root` is not in a git work tree (or git is not installed and no `.git` is in or above
    it). A str (git's first error line): git failed for another reason (dubious ownership, a
    corrupt index...), or git is not on PATH in a repository (hooks.NO_GIT: GitHub Desktop, Fork
    and SourceTree bring a git of their own, and rename rewrote a dirty tree without a word), so
    the tree could not be checked. git runs with LC_ALL=C so "not a git repository" is never
    translated.
    """
    env = {k: v for k, v in proc.base_env().items() if k not in ("LANGUAGE", "LANG", "LC_ALL", "LC_MESSAGES")}
    env["LC_ALL"] = "C"

    def failure(code: int, stderr: str) -> str | None:
        error = stderr.strip()
        if "not a git repository" in error:
            return None
        return (error.splitlines() or [f"git exited with {code}"])[0]

    try:
        r = proc.run(["git", "rev-parse", "--show-prefix"], cwd=root, env=env, capture=True, check=False, echo=False)
    except PytError:  # git not installed, or not on PATH
        from . import hooks  # hooks imports lintc and mypyc: only here

        return hooks.NO_GIT if hooks.git_missing_here(root) else None
    if r.returncode != 0:
        return failure(r.returncode, r.stderr)
    prefix = r.stdout.strip()
    # -uall: every untracked file, never a whole untracked folder (the project itself may be one)
    r = proc.run(["git", "status", "--porcelain", "-z", "-uall", "--", "."], cwd=root, env=env, capture=True, check=False, echo=False)
    if r.returncode != 0:
        return failure(r.returncode, r.stderr)
    return _porcelain_paths(r.stdout, prefix)


def derived_paths(generated: Iterable[str]) -> set[str]:
    """Files a rename rewrites anyway: the generated files and their state (never "dirty")."""
    return {*generated, ".pytemplate/state.json"}


def dirty_tree_message(changes: list[str] | str | None, action: str, retry: str) -> str | None:
    """The refusal for uncommitted changes (None: the tree is clean or not in git)."""
    if not changes:
        return None
    if isinstance(changes, str):
        problem = f"could not check for uncommitted changes in git ({changes})"
    else:
        shown = ", ".join(changes[:5]) + (f" and {len(changes) - 5} more" if len(changes) > 5 else "")
        problem = f"uncommitted changes in git ({len(changes)} path(s): {shown})"
    return f"{problem}: commit or stash first: {action} rewrites many files.\n  To go ahead anyway: {retry}"


def validate_config(text: str) -> Config:
    """Validate the renamed pytemplate.toml in memory (a miss here must not blame the user's file)."""
    from .cli import COMMANDS

    try:
        data = tomllib.loads(text)
        new_cfg: Config = config._build(Config, data, "")
        config.validate(new_cfg, set(COMMANDS))
    except tomllib.TOMLDecodeError as e:
        raise PytError(f"rename: the renamed pytemplate.toml would not be valid TOML ({e}); nothing was changed") from None
    except PytError as e:
        raise PytError(f"rename: the renamed pytemplate.toml would be invalid ({e}); nothing was changed") from None
    return new_cfg


# --- import order and line wrapping (the new name has another length and sort position) -------


def _python_edits(plan_: Plan) -> list[FileEdit]:
    return [f for f in plan_.changed_files if Path(f.path).suffix in (".py", ".pyi") and f.target.split("/", 1)[0] in ("src", "tests")]


def _batches(files: Sequence[str], size: int = 100) -> Iterator[list[str]]:
    for i in range(0, len(files), size):
        yield list(files[i : i + size])


def _same_path(path: str | Path) -> str:
    """A path as ruff prints it (relative to its cwd when inside it) -> a comparable form."""
    return os.path.normcase(os.path.abspath(_RUNNER_CWD / path))


def _ruff(cfg: Config, args: Sequence[str | Path], files: Sequence[Path], *, sync: bool = True) -> tuple[int, str]:
    """`uv run ruff ARGS FILES` in the tools environment (absolute paths, in batches). `sync`:
    `--locked`, else `--no-sync`: the ruff of the environment as it is, which needs no lock (after
    a rename, before the re-lock, both `--locked` and `--frozen` fail on the new project name)."""
    code, out = 0, []
    for batch in _batches([str(f) for f in files]):
        argv: list[str | Path] = ["run", "--quiet", "--locked" if sync else "--no-sync", "ruff", *args, *batch]
        r = envs.uv(envs.tool_env(cfg), argv, cwd=_RUNNER_CWD, check=False, capture=True, echo=False)
        code = max(code, r.returncode)
        out.append(r.stdout + r.stderr)
    return code, "\n".join(out)


@dataclass
class Tidy:
    """The rewritten Python files ruff accepted BEFORE the rename (paths as in the plan). Only
    those are tidied afterwards: a file you keep unformatted or unsorted stays as you wrote it."""

    formatted: set[str]  # `ruff format --check` passed
    sorted_imports: set[str]  # no I001 (import block un-sorted), when the import order is checked


def _checks_import_order(cfg: Config) -> bool:
    """Whether `check all` (and the generated CI, which runs it) checks the import order: a typing
    profile of a supported backend selects ruff's I001. Only the active one's was asked, and every
    preset's defaults (`off` on cpython: no I; `mypyc` on mypyc: I) left the imports a new name
    moved unsorted, and check all failed with I001 that no ./pyt command fixed."""
    def selects(prefixes: object) -> bool:
        return isinstance(prefixes, list) and any(isinstance(p, str) and (p == "ALL" or "I001".startswith(p)) for p in prefixes)

    for profile in {cfg.profile_for(b) for b in cfg.backend.supported}:
        ruff = render.load_profile(profile).get("ruff", {})
        if selects(ruff.get("select")) and not selects(ruff.get("ignore")):
            return True
    return False


def tidy_before(cfg: Config, plan_: Plan, root: Path | None = None) -> Tidy | None:
    """Before the rename: which rewritten Python files ruff accepts as they are (formatting, and
    the import order when check all checks it: _checks_import_order). None: ruff cannot run."""
    from .cmd_dev import _profile_file, config_arg

    root = root or ROOT
    files = [f.path for f in _python_edits(plan_)]
    if not files or proc.DRY_RUN or not envs.tool_env(cfg).python.is_file():
        return None
    paths = [root / f for f in files]
    order = _checks_import_order(cfg)
    try:
        config_file = config_arg(_profile_file(cfg, cfg.profile_for(), "ruff"))
        fmt_code, fmt_out = _ruff(cfg, ["format", "--check", "--config", config_file, "--force-exclude", "--output-format", "concise"], paths)
        lint_code, lint_out = (
            _ruff(cfg, ["check", "--config", config_file, "--force-exclude", "--no-fix", "--select", "I001", "--output-format", "concise"], paths)
            if order
            else (0, "")
        )
    except (PytError, OSError):
        return None
    unformatted = {_same_path(m.group("old") or m.group("path")) for m in _UNFORMATTED.finditer(fmt_out)}
    if fmt_code not in (0, 1) or (fmt_code == 1 and not unformatted):
        return None  # ruff failed, or an output format this runner does not know: tidy nothing
    unsorted = {_same_path(m.group("path")) for m in _UNSORTED.finditer(lint_out)}
    return Tidy(
        formatted={f for f in files if _same_path(root / f) not in unformatted},
        sorted_imports={f for f in files if _same_path(root / f) not in unsorted} if order and lint_code in (0, 1) else set(),
    )


def tidy_after(cfg: Config, plan_: Plan, clean: Tidy | None, root: Path | None = None) -> None:
    """After the rename: sort the imports the new name moved (when check all checks their order,
    whatever the active profile selects: `--select I001`) and re-format, each only in the files
    that were clean before. Best effort: it
    never fails the rename. It runs right after the files are written, before the re-lock (the
    ruff of the environment as it is: `--no-sync`): a re-lock that failed or was interrupted
    left the rename to `./pyt apply`, which finds the names in line and tidies nothing."""
    from .cmd_dev import _profile_file, config_arg

    root = root or ROOT
    edits = _python_edits(plan_)
    if not edits:
        return
    hint = "the new name can change import order and line wrapping: ./pyt lint --fix and ./pyt fmt"
    if clean is None:
        ui.info(f"  note: {hint}")
        return
    sortable = [root / f.target for f in edits if f.path in clean.sorted_imports]
    formatted = [root / f.target for f in edits if f.path in clean.formatted]
    before = {t: t.read_bytes() for t in (*sortable, *formatted) if t.is_file()}
    try:
        config_file = config_arg(_profile_file(cfg, cfg.profile_for(), "ruff"))
        codes = [0]
        if sortable:
            argv = ["check", "--config", config_file, "--force-exclude", "--fix-only", "--select", "I001", "--fixable", "I001", "--quiet"]
            codes.append(_ruff(cfg, argv, sortable, sync=False)[0])
        if formatted:
            codes.append(_ruff(cfg, ["format", "--config", config_file, "--force-exclude", "--quiet"], formatted, sync=False)[0])
    except (PytError, OSError) as e:
        ui.warn(f"ruff could not tidy the renamed files ({e}): {hint}")
        return
    if max(codes) > 1:  # uv could not start ruff, or ruff stopped on an error
        ui.warn(f"ruff could not tidy the renamed files (exit code {max(codes)}): {hint}")
    touched = sorted(t.relative_to(root).as_posix() for t, data in before.items() if t.is_file() and t.read_bytes() != data)
    if touched:
        ui.info(f"  ruff: import order / formatting fixed in {', '.join(touched)}")


# --- the command -------------------------------------------------------------------------------


def _clip(line: str, width: int = 110) -> str:
    line = line.strip()
    return line if len(line) <= width else line[: width - 3] + "..."


def _samples(changes: list[tuple[int, str, str]], limit: int) -> None:
    for _, old, new in changes[:limit]:
        ui.info(f"      - {_clip(old)}")
        ui.info(f"      + {_clip(new)}")
    if len(changes) > limit:
        ui.info(f"      ... {len(changes) - limit} more line(s)")


def _agreement(paths: Sequence[str]) -> tuple[str, str]:
    """The verb ending and the pronoun for a list of paths: ("s", "it") for one ("README.md also
    mentions"), ("", "them") for more."""
    return ("s", "it") if len(paths) == 1 else ("", "them")


def report(plan_: Plan, *, dry: bool) -> None:
    n = plan_.names
    pkg = f"package {n.old_pkg} -> {n.new_pkg}" if n.old_pkg != n.new_pkg else f"package {n.new_pkg} unchanged"
    ui.step(f"rename '{n.old_name}' -> '{n.new_name}' ({pkg})" + (f" {_DRY}" if dry else ""))
    listing = ui.info if dry else ui.detail
    if plan_.move is not None:
        verb = "would move" if dry else "move"
        ui.info(f"  {verb:<16} {plan_.move[0]}/ -> {plan_.move[1]}/")
    changed = sorted(plan_.changed_files, key=lambda f: f.target)
    total = sum(f.result.count for f in changed)
    ui.info(f"  src/, tests/     {len(changed)} file(s), {total} reference(s)")
    for f in changed:
        extra = [x for x in (f.result.note, f"kept in {f.encoding}" if f.encoding != "utf-8" else "") if x]
        listing(f"    {f.target} ({f.result.count})" + (f"  [{'; '.join(extra)}]" if extra else ""))
        if dry:
            _samples(f.result.changes, SAMPLES)
    for edit in (plan_.config, plan_.pyproject):
        if edit is None:
            continue
        refs = f" and {edit.count} package reference(s)" if edit.path == "pytemplate.toml" and edit.count else ""
        ui.info(f"  {edit.path:<16} {edit.detail}{refs}")
        if dry:
            _samples(edit.changes, 2 * SAMPLES)
    # What the user must review: ui.report, which -q never hides (a kept `return p.core` next to
    # a renamed `import beta.core` fails at runtime, and -q printed nothing at all)
    kept = [(f.target, line, text) for f in plan_.files for line, text in f.result.kept]
    kept += [(e.path, line, text) for e in (plan_.config, plan_.pyproject) if e is not None for line, text in e.kept]
    if kept:
        ui.report(f"  left unchanged   {len(kept)} line(s) still mention '{n.old_name}' (not changed; review them):")
        for path, line, text in kept[:10]:
            ui.report(f"    {path}:{line}: {_clip(text, 90)}")
        if len(kept) > 10:
            ui.report(f"    ... {len(kept) - 10} more")
    if plan_.mentions:
        shown = ", ".join(plan_.mentions[:10]) + (f" and {len(plan_.mentions) - 10} more" if len(plan_.mentions) > 10 else "")
        s, them = _agreement(plan_.mentions)
        ui.report(f"  not changed      {shown} also mention{s} '{n.old_name}' (edit {them} by hand if needed)")
    if plan_.linked:
        paths = ", ".join(_target(p, plan_.move) for p in plan_.linked)
        s, them = _agreement(plan_.linked)
        ui.warn(
            f"not rewritten (symbolic links or junctions: their targets may be shared): {paths} mention{s} "
            f"'{n.old_name}' or point{s} through it: edit {them} by hand"
        )
    if plan_.unreadable:
        paths = ", ".join(_target(p, plan_.move) for p in plan_.unreadable)
        s, them = _agreement(plan_.unreadable)
        ui.warn(f"not rewritten (not UTF-8 text): {paths} mention{s} '{n.old_name}': edit {them} by hand")
    if plan_.binary:
        ui.detail(f"  skipped (binary or not UTF-8): {', '.join(plan_.binary)}")


def _lock_change(new_cfg: Config, old_name: str, new_name: str) -> str | None:
    """Why the real run's cmd_env.ensure_lock re-locks uv.lock (None: it stays): the same
    normalized project name still re-locks a stale lock. A PytError when `uv lock --check`
    cannot run."""
    if presets._norm_name(old_name) != presets._norm_name(new_name):
        return "the project name changes"
    if render.pyproject_outdated(new_cfg):
        return "the managed parts of pyproject.toml change"
    r = envs.uv(envs.tool_env(new_cfg), ["lock", "--check"], cwd=ROOT, check=False, capture=True, echo=False)
    return None if r.returncode == 0 else "uv.lock is not up to date"


def _lock_forecast(new_cfg: Config, old_name: str, new_name: str) -> str:
    """What the real run's cmd_env.ensure_lock does to uv.lock, for --dry-run (nothing is written)."""
    try:
        why = _lock_change(new_cfg, old_name, new_name)
    except PytError as e:
        return f"cannot tell: uv lock --check could not run ({e})"
    return f"would re-lock (uv lock): {why}" if why is not None else "up to date (uv lock --check)"


def _refuse_what_the_lock_would(new_cfg: Config, old_name: str, new_name: str) -> None:
    """The refusals of cmd_env.ensure_lock that are known before the first write, made there (a
    dry run makes them too): managed pyproject parts it cannot rewrite (broken markers, a key of
    the user's between them: render.check_pyproject, as apply's make_plan asks), and a re-lock
    under the user's UV_FROZEN or UV_LOCKED, with which `uv lock` writes nothing. Both came once
    every file was renamed ("The files are already renamed ... ./pyt apply")."""
    from .cmd_env import _lock_read_only, _refuse_a_frozen_lock  # imported where used, as ensure_lock is

    try:
        render.check_pyproject(new_cfg)
        if _lock_read_only([]):
            why = _lock_change(new_cfg, old_name, new_name)
            if why is not None:
                _refuse_a_frozen_lock(why)
    except PytError as e:
        raise PytError(f"rename: {e}\n  Nothing was changed", e.code) from None


def needs_pypi(stderr: str) -> bool:
    """Whether a failed `./pyt rename` failed only because its `uv lock` could not reach the index."""
    return "$ uv lock" in stderr and any(marker in stderr for marker in PYPI_UNREACHABLE)


def _check_the_name_is_applied(cfg: Config, new_name: str) -> None:
    """Refuse what doctor and apply report about the name when src/<pkg>/ of app.name exists: an
    app.name set by hand to ANOTHER package of src/ (the rename would move that package and
    leave the app where it is; `rename <that name>` said "nothing to do"), and a pyproject.toml
    [project] name edited by hand ("nothing to do" while doctor reports it)."""
    from . import cmd_apply

    try:
        project_name = cmd_apply.read_project().name
    except PytError:
        return  # check_new_name says what is wrong with pyproject.toml
    record = cmd_apply.trusted_record(cfg, project_name)
    other = cmd_apply._other_package(cfg, record, project_name)
    if other is not None:
        either = "" if record is not None else f"\n  (or, if pyproject.toml [project] name is the line edited by hand, put back name = \"{cfg.app.name}\" there)"
        raise PytError(
            f"rename: app.name = '{cfg.app.name}' names src/{cfg.pkg}/, another package: the app is '{other}' "
            f"(src/{package_of(other)}/).\n  Put back app.name = \"{other}\" in pytemplate.toml, then ./pyt rename {new_name}{either}"
        )
    moved = cmd_apply.moved_by_hand(cfg, record)  # src/<old>/ moved by hand: renaming from there left every old reference
    if moved is not None:
        raise PytError(f"rename: {cmd_apply.moved_by_hand_message(cfg, moved, f'./pyt rename {new_name}')}")
    if new_name == cfg.app.name and project_name is not None and project_name != cfg.app.name:
        moved = f"src/{package_of(project_name)}/" if config.APP_NAME.fullmatch(project_name) else "its old folder"
        # a name of another spelling (Alpha, my-app for my_app) has app.name's own folder: nothing moved
        hint = "" if package_of(project_name) == cfg.pkg else (
            f"; if src/{cfg.pkg}/ was moved by hand, move it back to {moved} first, then ./pyt rename {new_name} (it rewrites the imports too)"
        )
        raise PytError(
            f"rename: the app is already called '{new_name}' (src/{cfg.pkg}/), but pyproject.toml [project] name = "
            f"'{project_name}'.\n  ./pyt apply writes '{new_name}' there{hint}"
        )


def cmd_rename(cfg: Config, args: list[str]) -> int:
    """rename NEW_NAME [--force]"""
    from . import cmd_apply

    parser = argparse.ArgumentParser(prog="./pyt rename", description="Rename the app and its package src/<pkg>/ everywhere.")
    parser.add_argument("new_name", metavar="NEW_NAME")
    parser.add_argument("--force", action="store_true", help="rename even with uncommitted changes in git")
    ns, unknown = parser.parse_known_args(args)
    if unknown:
        raise PytError(f"./pyt rename: unknown argument(s): {' '.join(unknown)}  (./pyt rename -h lists the options)")
    new_name: str = ns.new_name
    old_name = cfg.app.name
    generated = render.outputs(cfg)
    ignore = derived_paths(generated)
    # app.name was changed by hand (src/<pkg>/ missing, or the same folder: alpha -> Alpha):
    # rename from the name the project really has, like apply
    applied = cmd_apply.applied_name(cfg)
    if applied is not None:
        ui.info(f"note: app.name = '{cfg.app.name}' was changed by hand; the project is still '{applied}': renaming from '{applied}'")
        old_name = applied
        ignore.add("pytemplate.toml")  # dirty by definition
    elif package_dir(ROOT / "src", cfg.pkg) is None:
        raise PytError(
            f"rename: src/{cfg.pkg}/ not found (app.name = '{cfg.app.name}' says the package is there).\n"
            "  If app.name was changed by hand: put the old name back in pytemplate.toml and run "
            f"./pyt rename {new_name} again"
        )
    else:
        _check_the_name_is_applied(cfg, new_name)
    if new_name == old_name == cfg.app.name:
        ui.ok(f"the app is already called '{new_name}' (package src/{cfg.pkg}/): nothing to do")
        return 0
    check_new_name(cfg, new_name)
    changes = git_changes(ROOT)
    if isinstance(changes, list):
        changes = [p for p in changes if p not in ignore]
    message = dirty_tree_message(changes, "rename", f"./pyt rename {new_name} --force")
    if message and not ns.force:
        if not proc.DRY_RUN:
            raise PytError(message)
        ui.warn(message)
    planned = plan(ROOT, old_name, new_name, generated=generated)
    new_cfg = validate_config(planned.config.new)
    _refuse_what_the_lock_would(new_cfg, old_name, new_name)
    report(planned, dry=proc.DRY_RUN)
    if proc.DRY_RUN:
        ui.info(f"  uv.lock          {_lock_forecast(new_cfg, old_name, new_name)}")
        rerendered, edited = render.apply(new_cfg)  # --dry-run: compares only (what the real run writes)
        ui.info("  generated files  " + (f"would re-render {', '.join(rerendered)}" if rerendered else "unchanged"))
        if edited:
            ui.info(f"                   hand-edited, left untouched: {', '.join(edited)}")
        return 0

    from .cmd_env import ensure_lock

    record = cmd_apply.project_record(cfg)  # before the rename: its name is still the old one
    clean = tidy_before(cfg, planned)
    apply_plan(ROOT, planned)  # new_cfg: the renamed pytemplate.toml, validated before anything was written
    # The record follows the files at once (as in apply): named after the old app it is no longer
    # trusted, and the ./pyt apply that finishes an interrupted rename reads it
    cmd_apply.rename_record(new_name, record)
    tidied = False
    try:
        # before the re-lock, which can fail: the ./pyt apply that finishes the job finds the
        # names in line and tidies nothing
        tidy_after(new_cfg, planned, clean)
        tidied = True
        try:
            ensure_lock(new_cfg)
        except PytError as e:
            raise PytError(f"{e}\n  The files are already renamed: fix the problem above and run ./pyt apply", e.code) from None
        changed, edited = render.apply(new_cfg)
        if changed:
            ui.info(f"render: updated {', '.join(changed)}")
        if edited:
            ui.warn(f"not overwriting hand-edited generated files: {', '.join(edited)} (./pyt render --force)")
    except KeyboardInterrupt:  # Ctrl+C, or SIGTERM/SIGHUP passed on to uv (proc.Interrupted)
        also = "" if tidied else ", then ./pyt lint --fix and ./pyt fmt (import order and line wrapping)"
        ui.warn(f"the files are already renamed: run ./pyt apply to finish (uv.lock and the generated files){also}")
        raise
    ui.ok(f"renamed '{old_name}' -> '{new_name}' (package src/{new_cfg.pkg}/)")
    ui.info("  Next: ./pyt test all, and review the changes with git diff")
    if DIST.is_dir() and any(DIST.iterdir()):
        ui.info(f"  dist/ still holds the artifacts built as '{old_name}' (./pyt clean removes dist/ and .build/)")
    return 0
