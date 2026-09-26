"""rename NEW_NAME [--force]: rename the app and its Python package everywhere.

Renames src/<pkg>/ and rewrites every reference to the old name/package in src/, tests/,
pytemplate.toml and pyproject.toml, then re-locks uv.lock and regenerates the generated files.
`./deploy apply` runs the same plan when app.name was changed by hand in pytemplate.toml.

The pure part (`plan`, `apply_plan` and `rewrite` for one text) works on any project folder
and needs no uv: the tests rename each preset skeleton rendered for one name and compare the
result, byte for byte, with the skeleton rendered for the new name (what `./deploy new --name
NEW` writes).

Which occurrences change (whole words only: `myapp_extra` and `my-app-2` never match):
- Python code (the tokenizer tells code from strings and comments): only real package
  references, i.e. the first name of `import myapp[.x]` and `from myapp[.x] import ...`, and,
  in a file that binds the package with `import myapp[.x]` (no `as`), every name `myapp` that
  resolves to that import (`ast` scopes). A function, class body or comprehension that binds
  `myapp` another way (assignment, parameter, loop/with/except target, global/nonlocal) keeps
  every `myapp` in it, and a module that rebinds it keeps all of its own: they are reported,
  not changed. Attributes (`obj.myapp`) and keyword arguments (`f(myapp=1)`) never change.
- Strings, comments, docstrings and other text files: every occurrence except right after a
  dot (`x.myapp` is a submodule or an attribute, never the top-level package) and a path
  segment right after the package itself (`src/myapp/myapp` is a submodule of it).
- When the old name is also the old package but the new name is not a package name
  (`alpha` -> `My-Game`, package `my_game`), each text occurrence is either the package or the
  name. Package: path-like (`src/alpha/`, `alpha\\core`), dotted (`alpha.core`, `alpha.*`,
  `alpha:main`), next to the words package/module, after `import` or `-m`, in `from alpha
  import`, and strings passed to `import_module()`, `__import__()`, `find_spec()` or `files()`.
  Name: everything else (titles, `\"\"\"alpha\"\"\"`, `f"alpha: ..."`) and artifact names
  (`alpha.exe`, `alpha.pyz`, `alpha-cpython-exe`...).
- pytemplate.toml: app.name (its comment is kept) and package references only, always chosen
  by context, never a TOML key or table header. The values of the keys that hold module names
  (MODULE_KEYS: compile.modules, [[typing.mypy_overrides]] module, deploy.exe.hidden_imports...)
  are package references even when bare (`modules = ["alpha"]`). Any other occurrence of the
  old name is reported, not changed ([tasks] can use `{name}` and `{pkg}`).
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
import contextlib
import dataclasses
import functools
import io
import keyword
import os
import re
import tokenize
import tomllib
import warnings
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import config, envs, presets, proc, render, ui
from .config import Config
from .project import DIST, ROOT
from .project import ROOT as _RUNNER_CWD  # where the tools run (tests move ROOT, never this)
from .ui import DeployError

Kind = Literal["pkg", "name", "keep", "skip"]

_APP_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_DRY = "(--dry-run: nothing is written)"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", ".flet"}
PY_SUFFIXES = {".py", ".pyi", ".pyw"}
# Root files that never get a "this file also mentions the old name" note (rename edits them)
ROOT_SKIP = {"pytemplate.toml", "pyproject.toml", "uv.lock", "pyrightconfig.json"}
# Folders never searched for other mentions of the old name: caches, environments, builds, the
# runner, Claude Code's state (it holds whole copies of the project in worktrees)
MENTION_SKIP_DIRS = {*SKIP_DIRS, ".build", "dist", ".pytemplate", ".claude", ".tox", ".nox", ".eggs", ".idea", "node_modules"}
MENTION_MAX_BYTES = 2 * 1024 * 1024
# Functions whose string argument is a module name: import_module("alpha") is the package
LOADERS = {"import_module", "__import__", "find_spec", "files"}
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
_FROM_BEFORE = re.compile(r"(?:^|[^\w.])from[ \t]+$")
_IMPORT_AFTER = re.compile(r"[ \t]+import\b")
_DASH_M_BEFORE = re.compile(r"(?:^|[\s\"'\[(,])-m[\s\"',]+$")  # -m alpha, "-m", "alpha"
# TOML: a bare key (`alpha = `, `x.alpha = `) or a table header (`[alpha]`, `[[x.alpha]]`)
_TOML_KEY_BEFORE = re.compile(r"[ \t]*(?:\[\[?[ \t]*)?(?:[A-Za-z0-9_-]+[ \t]*\.[ \t]*)*")
_TOML_KEY_AFTER = re.compile(r"[ \t]*[.=\]]")
_TOML_TABLE = re.compile(r"[ \t]*\[\[?[ \t]*([A-Za-z0-9_.\- \t]+?)[ \t]*\]\]?[ \t]*(?:#.*)?$")
_TOML_ASSIGN = re.compile(r"[ \t]*([A-Za-z0-9_-]+(?:[ \t]*\.[ \t]*[A-Za-z0-9_-]+)*)[ \t]*=")
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
    forced: bool = False  # argument of import_module() & co.: a module name
    fstring: bool = False  # a whole f-string as ONE token (Python 3.11): {fields} are code


@dataclass
class _Code:
    names: dict[int, str]  # offset -> NAME token
    refs: set[int]  # offsets of the NAME tokens that are the package
    bound: bool  # `import pkg[.x]` without `as` binds the name `pkg` in this file
    regions: list[_Region]  # sorted, never overlapping
    scoped: bool = False  # refs of the bound name come from the ast scope analysis
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


def _python_code(text: str, pkg: str) -> _Code | None:
    """Tokenize a Python source: NAME tokens, package references and string/comment regions.

    Return None if the tokenizer rejects it (the caller then treats it as plain text).
    """
    shift = 1 if text.startswith("\ufeff") else 0
    body = text[shift:]
    line_starts = [0, *(m.end() for m in re.finditer("\n", body))]

    def offset(pos: tuple[int, int]) -> int:
        return shift + line_starts[pos[0] - 1] + pos[1]

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
        lines = body.split("\n")
        for line, col in uses:
            chars = len(lines[line - 1].encode("utf-8")[:col].decode("utf-8", "replace")) if line <= len(lines) else col
            used.add(offset((line, chars)))
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
    depth = 0
    fstart = 0
    last: list[tokenize.TokenInfo] = []  # the last two significant tokens
    for tok in tokens:
        kind = tokenize.tok_name.get(tok.type, "")
        if kind.endswith("STRING_START"):  # Python 3.12+: f-strings, t-strings (and any later family) are text
            if depth == 0:
                fstart = offset(tok.start)
            depth += 1
        elif kind.endswith("STRING_END"):
            depth -= 1
            if depth == 0:
                regions.append(_Region(fstart, offset(tok.end)))
        elif depth == 0 and tok.type in (tokenize.STRING, tokenize.COMMENT):
            forced = fstring = False
            if tok.type == tokenize.STRING:
                forced = (
                    len(last) == 2
                    and last[1].type == tokenize.OP
                    and last[1].string == "("
                    and last[0].type == tokenize.NAME
                    and last[0].string in LOADERS
                )
                prefix = re.match(r"[A-Za-z]*", tok.string)
                fstring = prefix is not None and "f" in prefix.group().lower()
            regions.append(_Region(offset(tok.start), offset(tok.end), forced, fstring))
        if tok.type not in trivia:
            last = [*last[-1:], tok]
    return _Code(
        names={offset(t.start): t.string for t in sig if t.type == tokenize.NAME},
        refs={offset(sig[i].start) for i in refs} | used,
        bound=bound,
        regions=sorted(regions, key=lambda r: r.start),
        scoped=uses is not None,
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
    return not (seg >= 2 and text[seg - 1] == "-" and _is_word(text[seg - 2]))


def _text_kind(text: str, start: int, end: int, word: str, names: Names, *, contextual: bool = False) -> Kind:
    """Classify an occurrence in text (a string, a comment, a Markdown or TOML file).

    `contextual`: decide by context even when the new name is also a package name (pytemplate.toml,
    where `[app]`, `editor = ` or a button named like the app must never change).
    """
    prev = text[start - 1 : start]
    if prev == "." and start >= 2 and _is_word(text[start - 2]):
        return "keep"  # x.alpha: a submodule or an attribute, never the top-level package
    if prev in ("/", "\\") and _inside_package(text, start, names.old_pkg):
        return "keep"  # src/alpha/alpha: a submodule of the package, like alpha.alpha
    if names.old_name != names.old_pkg:
        return "pkg" if word == names.old_pkg else "name"
    if names.new_name == names.new_pkg and not contextual:
        return "pkg"  # the new name is also the new package: no need to tell them apart
    if _ARTIFACT_SUFFIX.match(text, end) or _BACKEND_SUFFIX.match(text, end):
        return "name"
    nxt = text[end : end + 1]
    nxt2 = text[end + 1 : end + 2]
    if nxt in ("/", "\\") or prev in ("/", "\\"):
        return "pkg"
    if nxt in (".", ":") and (nxt2 == "*" or (nxt2 != "" and _is_word(nxt2))):
        return "pkg"
    before = text[max(0, start - 80) : start]
    if _PKG_WORD_AFTER.match(text, end) or _PKG_WORD_BEFORE.search(before):
        return "pkg"
    if _IMPORT_BEFORE.search(before) or _DASH_M_BEFORE.search(before):
        return "pkg"
    if _FROM_BEFORE.search(before) and _IMPORT_AFTER.match(text, end):
        return "pkg"
    return "name"


def _classify(text: str, start: int, end: int, word: str, names: Names, code: _Code | None, *, contextual: bool = False) -> Kind:
    if code is None:
        return _text_kind(text, start, end, word, names, contextual=contextual) if _whole_word(text, start, end) else "skip"
    token = code.names.get(start)
    if token is not None:  # code
        if token != word:
            return "skip"  # e.g. `My` of the expression My-Game
        return "pkg" if start in code.refs else "keep"
    region = code.region_at(start)
    if region is None:
        return "skip"
    if region.fstring and _in_fstring_field(text, region, start):  # code inside a 3.11 f-string
        if code.scoped:
            return "pkg" if start in code.refs else "keep"
        return "pkg" if word == names.old_pkg and code.bound and text[start - 1 : start] != "." else "keep"
    if not _whole_word(text, start, end):
        return "skip"
    kind = _text_kind(text, start, end, word, names)
    return "pkg" if region.forced and kind == "name" else kind


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
    module_keys: frozenset[str] = frozenset(),
) -> Rewrite:
    """Replace the old name/package in `text`. Line endings and everything else are kept.

    `python`: tell code from strings and comments with the tokenizer. `only_pkg`: change only
    the package references, chosen by context, and report the other occurrences as kept
    (pytemplate.toml). `toml`: TOML keys and table headers never change. `module_keys`: TOML
    keys whose quoted values are module names (a bare old package there is the package).
    """
    code = _python_code(text, names.old_pkg) if python else None
    module_lines = module_value_lines(text, module_keys) if module_keys else set()
    line_starts = [0, *(m.end() for m in re.finditer("\n", text))]
    pieces: list[str] = []
    last = 0
    count = 0
    kept_at: list[int] = []
    for m in _pattern(names).finditer(text):
        start, end = m.span()
        word = m.group()
        if toml and _toml_key(text, start, end):
            continue
        kind = _classify(text, start, end, word, names, code, contextual=only_pkg)
        if kind == "skip":
            continue
        module_line = bool(module_lines) and bisect.bisect_right(line_starts, start) in module_lines
        if (
            kind == "name"
            and module_line
            and word == names.old_pkg
            and text[start - 1 : start] in ("'", '"')
            and text[end : end + 1] == text[start - 1 : start]
        ):
            kind = "pkg"  # modules = ["alpha"]
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
    old_lines = text.split("\n")
    kept_lines = sorted({bisect.bisect_right(line_starts, pos) for pos in kept_at})
    return Rewrite(
        text=new_text,
        count=count,
        changes=_line_changes(text, new_text),
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


def _source_encoding(data: bytes) -> str | None:
    """The PEP 263 encoding a Python source declares when it is not UTF-8 (None: none usable)."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError:
        return None
    return None if encoding in ("utf-8", "utf-8-sig") else encoding


def _code_files(root: Path) -> Iterator[tuple[str, Path]]:
    """Every regular file of src/ and tests/ (no caches, no symlinks), sorted."""
    for top in ("src", "tests"):
        base = root / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".venv"))
            for filename in sorted(filenames):
                path = Path(dirpath) / filename
                if not path.is_symlink() and path.is_file():
                    yield path.relative_to(root).as_posix(), path


def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def package_dir(src: Path, pkg: str) -> Path | None:
    """Return src/<pkg>/ as spelled on disk (a case-insensitive file system may hold src/Alpha/)."""
    try:
        with os.scandir(src) as it:
            dirs = [Path(e.path) for e in it if e.is_dir()]
    except OSError:
        return None
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


def _plan_config(root: Path, names: Names) -> TextEdit:
    path = root / "pytemplate.toml"
    old = path.read_text(encoding="utf-8-sig")
    result = rewrite(old, names, only_pkg=True, toml=True, module_keys=MODULE_KEYS)
    new = config.set_value(result.text, "app", "name", names.new_name)
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"rename: the new pytemplate.toml would not be valid TOML ({e}); nothing was changed") from None
    renamed = {n for n, _, _ in _line_changes(result.text, new)}
    kept = [(n, line) for n, line in result.kept if n not in renamed]  # app.name itself
    return TextEdit("pytemplate.toml", old, new, result.count, kept, f'app.name = "{names.new_name}"')


def _plan_pyproject(root: Path, names: Names) -> TextEdit | None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    old = path.read_text(encoding="utf-8-sig")  # an editor or PowerShell 5.1 may add a BOM (written back without it)
    try:
        new = presets.set_project_name(old, names.new_name)
    except DeployError as e:
        raise DeployError(f"rename: {e}; nothing was changed") from None
    lines = new.split("\n")
    begin = next((i for i, ln in enumerate(lines) if ln.strip() == presets.EXTRA_BEGIN), None)
    end = next((i for i, ln in enumerate(lines) if ln.strip() == presets.EXTRA_END), None)
    count = 0
    if begin is not None and end is not None and end > begin + 1:
        block = rewrite("\n".join(lines[begin + 1 : end]), names, toml=True)
        lines[begin + 1 : end] = block.text.split("\n")
        count = block.count
        new = "\n".join(lines)
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"rename: the new pyproject.toml would not be valid TOML ({e}); nothing was changed") from None
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
            and not (here / d).is_symlink()
        )
        for filename in sorted(filenames):
            path = here / filename
            rel_path = path.relative_to(root).as_posix()
            if (top and filename in ROOT_SKIP) or rel_path in skipped or path.is_symlink():
                continue
            try:
                if path.stat().st_size > MENTION_MAX_BYTES:
                    continue
                text = _decode(path.read_bytes())
            except OSError:
                continue
            if text is not None and pattern.search(text):
                out.append(rel_path)
    return out


def plan(root: Path, old_name: str, new_name: str, *, generated: Iterable[str] = ()) -> Plan:
    """Compute every change of renaming the project in `root` (nothing is written).

    `generated`: the generated files (re-rendered after the rename, so never listed as mentions).
    """
    names = Names(old_name, new_name)
    src = root / "src"
    old_dir = package_dir(src, names.old_pkg)
    if old_dir is None:
        raise DeployError(
            f"rename: src/{names.old_pkg}/ not found (app.name = '{old_name}' says the package is there)"
        )
    move: tuple[str, str] | None = None
    if old_dir.name != names.new_pkg:
        new_dir = src / names.new_pkg
        if new_dir.exists() and not same_file(old_dir, new_dir):
            raise DeployError(f"rename: src/{names.new_pkg}/ already exists: move or delete it first")
        if (src / f"{names.new_pkg}.py").exists():
            raise DeployError(f"rename: src/{names.new_pkg}.py exists: the package src/{names.new_pkg}/ would clash with it")
        move = (f"src/{old_dir.name}", f"src/{names.new_pkg}")
    files: list[FileEdit] = []
    binary: list[str] = []
    unreadable: list[str] = []
    pattern = _pattern(names)
    for rel_path, path in _code_files(root):
        data = path.read_bytes()
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
            if b"\0" not in data and pattern.search(data.decode("latin-1")):
                unreadable.append(rel_path)
            continue
        result = rewrite(text, names, python=path.suffix in PY_SUFFIXES)
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
    )


def _move_dir(root: Path, old_rel: str, new_rel: str) -> None:
    old, new = root / old_rel, root / new_rel
    try:
        if new.exists() and same_file(old, new):  # case-only rename on a case-insensitive file system
            tmp = old.with_name(f"{old.name}.pt-rename-{os.getpid()}")
            old.rename(tmp)
            try:
                tmp.rename(new)
            except OSError:
                tmp.rename(old)
                raise
        else:
            old.rename(new)
    except OSError as e:
        raise DeployError(
            f"rename: could not move {old_rel}/ to {new_rel}/: {e.strerror or e}. Nothing was changed.\n"
            "  Close the programs that use files in it (a running app, a terminal inside it) and try again"
        ) from None


def apply_plan(root: Path, plan_: Plan) -> None:
    """Write a plan: move src/<pkg>/ first (the step that can fail on a locked file), then the files.

    A file that cannot be written (read-only, locked by another program) undoes what was already
    done: the files written so far get their old bytes back and the folder moves back."""
    if plan_.move is not None:
        _move_dir(root, *plan_.move)
    writes: list[tuple[Path, bytes]] = [(root / f.target, f.new) for f in plan_.changed_files]
    for edit in (plan_.config, plan_.pyproject):
        if edit is not None and edit.new != edit.old:
            writes.append((root / edit.path, edit.new.encode("utf-8")))  # LF, no BOM (the runner's own files)
    done: list[tuple[Path, bytes]] = []
    for path, data in writes:
        try:
            old = path.read_bytes()
            path.write_bytes(data)
        except OSError as e:
            for written, previous in reversed(done):
                with contextlib.suppress(OSError):
                    written.write_bytes(previous)
            undone = "the files written so far were restored"
            if plan_.move is not None:
                try:
                    _move_dir(root, plan_.move[1], plan_.move[0])
                except DeployError:
                    undone += f", but {plan_.move[1]}/ could not be moved back to {plan_.move[0]}/ (move it by hand)"
            raise DeployError(
                f"rename: could not write {path.relative_to(root).as_posix()}: {e.strerror or e}. The rename was undone "
                f"({undone}).\n  Close the programs that use it (or make it writable) and try again"
            ) from None
        done.append((path, old))


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


def check_new_name(cfg: Config, new_name: str, *, who: str = "rename", retry: str = "./deploy rename OTHER_NAME") -> None:
    """Reject names that cannot work: format, keywords, stdlib modules, dependencies (uv.lock too)."""
    if not _APP_NAME.fullmatch(new_name):
        raise DeployError(f"{who}: the name may only contain letters, digits, '-' and '_' (and must start with a letter)")
    pkg = package_of(new_name)
    if keyword.iskeyword(pkg):
        raise DeployError(f"{who}: the package '{pkg}' would be a Python keyword (import {pkg} is a syntax error)")
    if presets.shadows_stdlib(pkg):
        raise DeployError(f"{who}: src/{pkg}/ would shadow the standard library module '{pkg}'. Choose another name: {retry}")
    try:
        presets.check_name_free(cfg, cfg.app.preset, new_name)
    except DeployError as e:
        reason = str(e).splitlines()[0]
        raise DeployError(f"{who}: {reason}\n  Choose another name: {retry}") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:  # it reads pyproject.toml
        raise DeployError(f"{who}: pyproject.toml cannot be read as TOML ({e}): fix it first; nothing was changed") from None
    clash = presets._norm_name(new_name)
    if clash in locked_names(ROOT):
        raise DeployError(
            f"{who}: '{new_name}' is also the name of a package in uv.lock ({clash}, a dependency of a dependency): "
            f"uv would refuse the project and src/{pkg}/ would shadow the library.\n  Choose another name: {retry}"
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

    None: git is not installed or `root` is not in a git work tree. A str (git's first error
    line): git failed for another reason (dubious ownership, a corrupt index...), so the tree
    could not be checked. git runs with LC_ALL=C so "not a git repository" is never translated.
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
    except DeployError:  # git not installed
        return None
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
        raise DeployError(f"rename: the renamed pytemplate.toml would not be valid TOML ({e}); nothing was changed") from None
    except DeployError as e:
        raise DeployError(f"rename: the renamed pytemplate.toml would be invalid ({e}); nothing was changed") from None
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


def _ruff(cfg: Config, args: Sequence[str | Path], files: Sequence[Path]) -> tuple[int, str]:
    """`uv run ruff ARGS FILES` in the tools environment (absolute paths, in batches)."""
    code, out = 0, []
    for batch in _batches([str(f) for f in files]):
        argv: list[str | Path] = ["run", "--quiet", "--locked", "ruff", *args, *batch]
        r = envs.uv(envs.tool_env(cfg), argv, cwd=_RUNNER_CWD, check=False, capture=True, echo=False)
        code = max(code, r.returncode)
        out.append(r.stdout + r.stderr)
    return code, "\n".join(out)


@dataclass
class Tidy:
    """The rewritten Python files ruff accepted BEFORE the rename (paths as in the plan). Only
    those are tidied afterwards: a file you keep unformatted or unsorted stays as you wrote it."""

    formatted: set[str]  # `ruff format --check` passed
    sorted_imports: set[str]  # no I001 (import block un-sorted) under the project's typing profile


def tidy_before(cfg: Config, plan_: Plan, root: Path | None = None) -> Tidy | None:
    """Before the rename: which rewritten Python files ruff accepts as they are (formatting, and
    the import order when the typing profile selects ruff's I rules). None: ruff cannot run."""
    from .cmd_dev import _profile_file

    root = root or ROOT
    files = [f.path for f in _python_edits(plan_)]
    if not files or proc.DRY_RUN or not envs.tool_env(cfg).python.is_file():
        return None
    paths = [root / f for f in files]
    try:
        config_file = _profile_file(cfg, cfg.profile_for(), "ruff")
        fmt_code, fmt_out = _ruff(cfg, ["format", "--check", "--config", config_file, "--force-exclude", "--output-format", "concise"], paths)
        lint_code, lint_out = _ruff(cfg, ["check", "--config", config_file, "--force-exclude", "--no-fix", "--output-format", "concise"], paths)
    except (DeployError, OSError):
        return None
    unformatted = {_same_path(m.group("old") or m.group("path")) for m in _UNFORMATTED.finditer(fmt_out)}
    if fmt_code not in (0, 1) or (fmt_code == 1 and not unformatted):
        return None  # ruff failed, or an output format this runner does not know: tidy nothing
    unsorted = {_same_path(m.group("path")) for m in _UNSORTED.finditer(lint_out)}
    return Tidy(
        formatted={f for f in files if _same_path(root / f) not in unformatted},
        sorted_imports={f for f in files if _same_path(root / f) not in unsorted} if lint_code in (0, 1) else set(),
    )


def tidy_after(cfg: Config, plan_: Plan, clean: Tidy | None, root: Path | None = None) -> None:
    """After the rename: sort the imports the new name moved (only when the typing profile selects
    ruff's I rules) and re-format, each only in the files that were clean before. Best effort: it
    never fails the rename."""
    from .cmd_dev import _profile_file

    root = root or ROOT
    edits = _python_edits(plan_)
    if not edits:
        return
    hint = "the new name can change import order and line wrapping: ./deploy lint --fix and ./deploy fmt"
    if clean is None:
        ui.info(f"  note: {hint}")
        return
    sortable = [root / f.target for f in edits if f.path in clean.sorted_imports]
    formatted = [root / f.target for f in edits if f.path in clean.formatted]
    before = {t: t.read_bytes() for t in (*sortable, *formatted) if t.is_file()}
    try:
        config_file = _profile_file(cfg, cfg.profile_for(), "ruff")
        if sortable:
            _ruff(cfg, ["check", "--config", config_file, "--force-exclude", "--fix-only", "--fixable", "I001", "--quiet"], sortable)
        if formatted:
            _ruff(cfg, ["format", "--config", config_file, "--force-exclude", "--quiet"], formatted)
    except (DeployError, OSError) as e:
        ui.warn(f"ruff could not tidy the renamed files ({e}): {hint}")
        return
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
    kept = [(f.target, line, text) for f in plan_.files for line, text in f.result.kept]
    kept += [(e.path, line, text) for e in (plan_.config, plan_.pyproject) if e is not None for line, text in e.kept]
    if kept:
        ui.info(f"  left unchanged   {len(kept)} line(s) still mention '{n.old_name}' (not changed; review them):")
        for path, line, text in kept[:10]:
            ui.info(f"    {path}:{line}: {_clip(text, 90)}")
        if len(kept) > 10:
            ui.info(f"    ... {len(kept) - 10} more")
    if plan_.mentions:
        shown = ", ".join(plan_.mentions[:10]) + (f" and {len(plan_.mentions) - 10} more" if len(plan_.mentions) > 10 else "")
        ui.info(f"  not changed      {shown} also mention '{n.old_name}' (edit them by hand if needed)")
    if plan_.unreadable:
        paths = ", ".join(_target(p, plan_.move) for p in plan_.unreadable)
        ui.warn(f"not rewritten (not UTF-8 text): {paths} mention '{n.old_name}': edit them by hand")
    if plan_.binary:
        ui.detail(f"  skipped (binary or not UTF-8): {', '.join(plan_.binary)}")


def needs_pypi(stderr: str) -> bool:
    """Whether a failed `./deploy rename` failed only because its `uv lock` could not reach the index."""
    return "$ uv lock" in stderr and any(marker in stderr for marker in PYPI_UNREACHABLE)


def cmd_rename(cfg: Config, args: list[str]) -> int:
    """rename NEW_NAME [--force]"""
    from . import cmd_apply

    parser = argparse.ArgumentParser(prog="./deploy rename", description="Rename the app and its package src/<pkg>/ everywhere.")
    parser.add_argument("new_name", metavar="NEW_NAME")
    parser.add_argument("--force", action="store_true", help="rename even with uncommitted changes in git")
    ns, unknown = parser.parse_known_args(args)
    if unknown:
        raise DeployError(f"./deploy rename: unknown argument(s): {' '.join(unknown)}  (./deploy rename -h lists the options)")
    new_name: str = ns.new_name
    old_name = cfg.app.name
    generated = render.outputs(cfg)
    ignore = derived_paths(generated)
    if package_dir(ROOT / "src", cfg.pkg) is None:
        # app.name was changed by hand: rename from the name the project really has
        applied = cmd_apply.applied_name(cfg)
        if applied is None:
            raise DeployError(
                f"rename: src/{cfg.pkg}/ not found (app.name = '{cfg.app.name}' says the package is there).\n"
                "  If app.name was changed by hand: put the old name back in pytemplate.toml and run "
                f"./deploy rename {new_name} again"
            )
        ui.info(f"note: app.name = '{cfg.app.name}' was changed by hand; the project is still '{applied}': renaming from '{applied}'")
        old_name = applied
        ignore.add("pytemplate.toml")  # dirty by definition
    if new_name == old_name == cfg.app.name:
        ui.ok(f"the app is already called '{new_name}' (package src/{cfg.pkg}/): nothing to do")
        return 0
    check_new_name(cfg, new_name)
    changes = git_changes(ROOT)
    if isinstance(changes, list):
        changes = [p for p in changes if p not in ignore]
    message = dirty_tree_message(changes, "rename", f"./deploy rename {new_name} --force")
    if message and not ns.force:
        if not proc.DRY_RUN:
            raise DeployError(message)
        ui.warn(message)
    planned = plan(ROOT, old_name, new_name, generated=generated)
    new_cfg = validate_config(planned.config.new)
    report(planned, dry=proc.DRY_RUN)
    if proc.DRY_RUN:
        relock = presets._norm_name(old_name) != presets._norm_name(new_name)
        ui.info("  uv.lock          " + ("would re-lock (uv lock): the project name changes" if relock else "unchanged (same normalized project name)"))
        rerendered, edited = render.apply(new_cfg)  # --dry-run: compares only (what the real run writes)
        ui.info("  generated files  " + (f"would re-render {', '.join(rerendered)}" if rerendered else "unchanged"))
        if edited:
            ui.info(f"                   hand-edited, left untouched: {', '.join(edited)}")
        return 0

    from .cmd_env import ensure_lock

    record = cmd_apply.load_record()  # read first: re-rendering rewrites state.json
    clean = tidy_before(cfg, planned)
    apply_plan(ROOT, planned)  # new_cfg: the renamed pytemplate.toml, validated before anything was written
    try:
        ensure_lock(new_cfg)
    except DeployError as e:
        raise DeployError(f"{e}\n  The files are already renamed: fix the problem above and run ./deploy apply", e.code) from None
    changed, edited = render.apply(new_cfg)
    if changed:
        ui.info(f"render: updated {', '.join(changed)}")
    if edited:
        ui.warn(f"not overwriting hand-edited generated files: {', '.join(edited)} (./deploy render --force)")
    tidy_after(new_cfg, planned, clean)
    cmd_apply.rename_record(new_name, record)
    ui.ok(f"renamed '{old_name}' -> '{new_name}' (package src/{new_cfg.pkg}/)")
    ui.info("  Next: ./deploy test all, and review the changes with git diff")
    if DIST.is_dir() and any(DIST.iterdir()):
        ui.info(f"  dist/ still holds the artifacts built as '{old_name}' (./deploy clean removes dist/ and .build/)")
    return 0
