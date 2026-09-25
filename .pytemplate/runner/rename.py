"""rename NEW_NAME [--force]: rename the app and its Python package everywhere.

Renames src/<pkg>/ and rewrites every reference to the old name/package in src/, tests/,
pytemplate.toml and pyproject.toml, then re-locks uv.lock and regenerates the generated files.

The pure part (`plan`, `apply_plan` and `rewrite` for one text) works on any project folder
and needs no uv: the tests rename each preset skeleton rendered for one name and compare the
result, byte for byte, with the skeleton rendered for the new name (what `./deploy new --name
NEW` writes).

Which occurrences change (whole words only: `myapp_extra` and `my-app-2` never match):
- Python code (the tokenizer tells code from strings and comments): only real package
  references, i.e. the first name of `import myapp[.x]` and `from myapp[.x] import ...`, and,
  in a file that binds the package with `import myapp[.x]` (no `as`), every `myapp` that is
  not an attribute (`obj.myapp`) nor an assignment or keyword target (`myapp = 1`).
- Strings, comments, docstrings and other text files: every occurrence except right after a
  dot (`x.myapp` is a submodule or an attribute, never the top-level package).
- When the old name is also the old package but the new name is not a package name
  (`alpha` -> `My-Game`, package `my_game`), each text occurrence is either the package or the
  name. Package: path-like (`src/alpha/`, `alpha\\core`), dotted (`alpha.core`, `alpha.*`,
  `alpha:main`), next to the words package/module, after `import` or `-m`, in `from alpha
  import`, and strings passed to `import_module()`, `__import__()`, `find_spec()` or `files()`.
  Name: everything else (titles, `\"\"\"alpha\"\"\"`, `f"alpha: ..."`) and artifact names
  (`alpha.exe`, `alpha.pyz`, `alpha-cpython-exe`...).
- pytemplate.toml: app.name (its comment is kept) and package references only; any other
  occurrence of the old name is reported, not changed ([tasks] can use `{name}` and `{pkg}`).
- pyproject.toml: [project] name and the preset block (`# >>> pytemplate-preset`).
"""

from __future__ import annotations

import argparse
import bisect
import io
import keyword
import os
import re
import sys
import tokenize
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import config, presets, proc, render, ui
from .config import Config
from .project import DIST, ROOT
from .ui import DeployError

Kind = Literal["pkg", "name", "keep", "skip"]

_APP_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_DRY = "(--dry-run: nothing is written)"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", ".flet"}
PY_SUFFIXES = {".py", ".pyi", ".pyw"}
# Root files that never get a "this file also mentions the old name" note
ROOT_SKIP = {"pytemplate.toml", "pyproject.toml", "uv.lock", "pyrightconfig.json"}
# Functions whose string argument is a module name: import_module("alpha") is the package
LOADERS = {"import_module", "__import__", "find_spec", "files"}
SAMPLES = 3  # sample lines per file in the --dry-run plan

# Context rules for an occurrence of an old name that is also the old package
_ARTIFACT_SUFFIX = re.compile(r"\.(?:exe|pyz|cmd|bat|sh|ps1|spec|zip|tar|dmg|msi|apk|aab|ipa)\b")
_BACKEND_SUFFIX = re.compile(r"-(?:cpython|pypy|mypyc)\b")  # dist/<name>-<backend>-<method>
_PKG_WORD_AFTER = re.compile(r"[ \t]+(?:package|module)\b")
_PKG_WORD_BEFORE = re.compile(r"(?:^|\W)(?:package|module)[ \t]+$")
_IMPORT_BEFORE = re.compile(r"(?:^|[^\w.])import[ \t]+$")
_FROM_BEFORE = re.compile(r"(?:^|[^\w.])from[ \t]+$")
_IMPORT_AFTER = re.compile(r"[ \t]+import\b")
_DASH_M_BEFORE = re.compile(r"(?:^|[\s\"'\[(,])-m[\s\"',]+$")  # -m alpha, "-m", "alpha"


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
    if bound:  # uses of the bound package: pkg.core.fn(), but not obj.pkg, pkg = ..., f(pkg=...)
        for idx in range(len(sig)):
            if idx in refs or idx in in_import or not is_name(idx, pkg):
                continue
            if idx > 0 and (is_op(idx - 1, ".") or is_name(idx - 1, "def") or is_name(idx - 1, "class")):
                continue
            if is_op(idx + 1, "="):
                continue
            refs.add(idx)

    regions: list[_Region] = []
    depth = 0
    fstart = 0
    last: list[tokenize.TokenInfo] = []  # the last two significant tokens
    for tok in tokens:
        kind = tokenize.tok_name.get(tok.type, "")
        if kind in ("FSTRING_START", "TSTRING_START"):  # Python 3.12+: the whole f-string is text
            if depth == 0:
                fstart = offset(tok.start)
            depth += 1
        elif kind in ("FSTRING_END", "TSTRING_END"):
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
        refs={offset(sig[i].start) for i in refs},
        bound=bound,
        regions=sorted(regions, key=lambda r: r.start),
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


def _text_kind(text: str, start: int, end: int, word: str, names: Names) -> Kind:
    """Classify an occurrence in text (a string, a comment, a Markdown or TOML file)."""
    prev = text[start - 1 : start]
    if prev == "." and start >= 2 and _is_word(text[start - 2]):
        return "keep"  # x.alpha: a submodule or an attribute, never the top-level package
    if names.old_name != names.old_pkg:
        return "pkg" if word == names.old_pkg else "name"
    if names.new_name == names.new_pkg:
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


def _classify(text: str, start: int, end: int, word: str, names: Names, code: _Code | None) -> Kind:
    if code is None:
        return _text_kind(text, start, end, word, names) if _whole_word(text, start, end) else "skip"
    token = code.names.get(start)
    if token is not None:  # code
        if token != word:
            return "skip"  # e.g. `My` of the expression My-Game
        return "pkg" if start in code.refs else "keep"
    region = code.region_at(start)
    if region is None:
        return "skip"
    if region.fstring and _in_fstring_field(text, region, start):  # code inside a 3.11 f-string
        return "pkg" if word == names.old_pkg and code.bound and text[start - 1 : start] != "." else "keep"
    if not _whole_word(text, start, end):
        return "skip"
    kind = _text_kind(text, start, end, word, names)
    return "pkg" if region.forced and kind == "name" else kind


def rewrite(text: str, names: Names, *, python: bool = False, only_pkg: bool = False) -> Rewrite:
    """Replace the old name/package in `text`. Line endings and everything else are kept.

    `python`: tell code from strings and comments with the tokenizer. `only_pkg`: change only
    the package references and report the other occurrences as kept (pytemplate.toml).
    """
    code = _python_code(text, names.old_pkg) if python else None
    pieces: list[str] = []
    last = 0
    count = 0
    kept_at: list[int] = []
    for m in _pattern(names).finditer(text):
        start, end = m.span()
        word = m.group()
        kind = _classify(text, start, end, word, names, code)
        if kind == "skip":
            continue
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
    line_starts = [0, *(m.end() for m in re.finditer("\n", text))]
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
    mentions: list[str]  # root files (README.md...) that mention the old name: not changed

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


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _package_dir(src: Path, pkg: str) -> Path | None:
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
        if d.name.lower() == pkg and _same_file(d, src / pkg):
            return d
    return None


def _target(path: str, move: tuple[str, str] | None) -> str:
    if move is not None and (path == move[0] or path.startswith(move[0] + "/")):
        return move[1] + path[len(move[0]) :]
    return path


def _plan_config(root: Path, names: Names) -> TextEdit:
    path = root / "pytemplate.toml"
    old = path.read_text(encoding="utf-8-sig")
    result = rewrite(old, names, only_pkg=True)
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
    old = path.read_text(encoding="utf-8")
    new = presets._set_project_name(old, names.new_name)
    lines = new.split("\n")
    begin = next((i for i, ln in enumerate(lines) if ln.strip() == presets.EXTRA_BEGIN), None)
    end = next((i for i, ln in enumerate(lines) if ln.strip() == presets.EXTRA_END), None)
    count = 0
    if begin is not None and end is not None and end > begin + 1:
        block = rewrite("\n".join(lines[begin + 1 : end]), names)
        lines[begin + 1 : end] = block.text.split("\n")
        count = block.count
        new = "\n".join(lines)
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"rename: the new pyproject.toml would not be valid TOML ({e}); nothing was changed") from None
    detail = f'[project] name = "{names.new_name}"' + (f" and {count} in the preset block" if count else "")
    return TextEdit("pyproject.toml", old, new, count, [], detail)


def _root_mentions(root: Path, names: Names) -> list[str]:
    pattern = _pattern(names)
    out: list[str] = []
    with os.scandir(root) as it:
        entries = sorted((e for e in it if e.is_file() and not e.name.startswith(".")), key=lambda e: e.name)
    for entry in entries:
        if entry.name in ROOT_SKIP:
            continue
        try:
            text = _decode(Path(entry.path).read_bytes())
        except OSError:
            continue
        if text is not None and pattern.search(text):
            out.append(entry.name)
    return out


def plan(root: Path, old_name: str, new_name: str) -> Plan:
    """Compute every change of renaming the project in `root` (nothing is written)."""
    names = Names(old_name, new_name)
    src = root / "src"
    old_dir = _package_dir(src, names.old_pkg)
    if old_dir is None:
        raise DeployError(
            f"rename: src/{names.old_pkg}/ not found (app.name = '{old_name}' says the package is there)"
        )
    move: tuple[str, str] | None = None
    if old_dir.name != names.new_pkg:
        new_dir = src / names.new_pkg
        if new_dir.exists() and not _same_file(old_dir, new_dir):
            raise DeployError(f"rename: src/{names.new_pkg}/ already exists: move or delete it first")
        if (src / f"{names.new_pkg}.py").exists():
            raise DeployError(f"rename: src/{names.new_pkg}.py exists: the package src/{names.new_pkg}/ would clash with it")
        move = (f"src/{old_dir.name}", f"src/{names.new_pkg}")
    files: list[FileEdit] = []
    binary: list[str] = []
    for rel_path, path in _code_files(root):
        data = path.read_bytes()
        text = _decode(data)
        if text is None:
            binary.append(rel_path)
            continue
        result = rewrite(text, names, python=path.suffix in PY_SUFFIXES)
        if result.count or result.kept:
            files.append(FileEdit(rel_path, _target(rel_path, move), data, result.text.encode("utf-8"), result))
    return Plan(
        names=names,
        move=move,
        files=files,
        config=_plan_config(root, names),
        pyproject=_plan_pyproject(root, names),
        binary=binary,
        mentions=_root_mentions(root, names),
    )


def _move_dir(root: Path, old_rel: str, new_rel: str) -> None:
    old, new = root / old_rel, root / new_rel
    try:
        if new.exists() and _same_file(old, new):  # case-only rename on a case-insensitive file system
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
    """Write a plan: move src/<pkg>/ first (the step that can fail on a locked file), then the files."""
    if plan_.move is not None:
        _move_dir(root, *plan_.move)
    for f in plan_.changed_files:
        (root / f.target).write_bytes(f.new)
    for edit in (plan_.config, plan_.pyproject):
        if edit is not None and edit.new != edit.old:
            (root / edit.path).write_text(edit.new, encoding="utf-8", newline="\n")


# --- the command -------------------------------------------------------------------------------


def check_new_name(cfg: Config, new_name: str) -> None:
    """Reject names that cannot work: format, keywords, stdlib modules, dependencies."""
    if not _APP_NAME.fullmatch(new_name):
        raise DeployError("rename: the name may only contain letters, digits, '-' and '_' (and must start with a letter)")
    pkg = package_of(new_name)
    if keyword.iskeyword(pkg):
        raise DeployError(f"rename: the package '{pkg}' would be a Python keyword (import {pkg} is a syntax error)")
    if pkg in sys.stdlib_module_names:
        raise DeployError(f"rename: src/{pkg}/ would shadow the standard library module '{pkg}'. Choose another name")
    try:
        presets.check_name_free(cfg, cfg.app.preset, new_name)
    except DeployError as e:
        reason = str(e).splitlines()[0]
        raise DeployError(f"rename: {reason}\n  Choose another name: ./deploy rename OTHER_NAME") from None


def git_changes(root: Path) -> list[str] | None:
    """Return `git status --porcelain` for the project folder (None: no git or not a work tree)."""
    try:
        r = proc.run(["git", "status", "--porcelain", "--", "."], cwd=root, capture=True, check=False, echo=False)
    except DeployError:  # git not installed
        return None
    if r.returncode != 0:
        return None
    return [line for line in r.stdout.splitlines() if line.strip()]


def _clip(line: str, width: int = 110) -> str:
    line = line.strip()
    return line if len(line) <= width else line[: width - 3] + "..."


def _samples(changes: list[tuple[int, str, str]], limit: int) -> None:
    for _, old, new in changes[:limit]:
        ui.info(f"      - {_clip(old)}")
        ui.info(f"      + {_clip(new)}")
    if len(changes) > limit:
        ui.info(f"      ... {len(changes) - limit} more line(s)")


def _report(plan_: Plan, *, dry: bool) -> None:
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
        listing(f"    {f.target} ({f.result.count})" + (f"  [{f.result.note}]" if f.result.note else ""))
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
    kept += [(plan_.config.path, line, text) for line, text in plan_.config.kept]
    if kept:
        ui.info(f"  left unchanged   {len(kept)} line(s) still mention '{n.old_name}' (not a package reference here; review them):")
        for path, line, text in kept[:10]:
            ui.info(f"    {path}:{line}: {_clip(text, 90)}")
        if len(kept) > 10:
            ui.info(f"    ... {len(kept) - 10} more")
    if plan_.mentions:
        ui.info(f"  not changed      {', '.join(plan_.mentions)} also mention '{n.old_name}' (edit by hand if needed)")
    if plan_.binary:
        ui.detail(f"  skipped (binary or not UTF-8): {', '.join(plan_.binary)}")


def _validate_config(text: str) -> Config:
    from .cli import COMMANDS

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise DeployError(f"rename: the new pytemplate.toml would not be valid TOML ({e})") from None
    new_cfg: Config = config._build(Config, data, "")
    config.validate(new_cfg, set(COMMANDS))
    return new_cfg


def cmd_rename(cfg: Config, args: list[str]) -> int:
    """rename NEW_NAME [--force]"""
    parser = argparse.ArgumentParser(prog="./deploy rename", description="Rename the app and its package src/<pkg>/ everywhere.")
    parser.add_argument("new_name", metavar="NEW_NAME")
    parser.add_argument("--force", action="store_true", help="rename even with uncommitted changes in git")
    ns, unknown = parser.parse_known_args(args)
    if unknown:
        raise DeployError(f"./deploy rename: unknown argument(s): {' '.join(unknown)}  (./deploy rename -h lists the options)")
    new_name: str = ns.new_name
    old_name = cfg.app.name
    if new_name == old_name:
        ui.ok(f"the app is already called '{new_name}' (package src/{cfg.pkg}/): nothing to do")
        return 0
    check_new_name(cfg, new_name)
    changes = git_changes(ROOT)
    if changes and not ns.force:
        message = (
            f"uncommitted changes in git ({len(changes)} path(s)): commit or stash first: rename rewrites many files.\n"
            f"  To rename anyway: ./deploy rename {new_name} --force"
        )
        if not proc.DRY_RUN:
            raise DeployError(message)
        ui.warn(message)
    planned = plan(ROOT, old_name, new_name)
    _validate_config(planned.config.new)
    _report(planned, dry=proc.DRY_RUN)
    if proc.DRY_RUN:
        relock = presets._norm_name(old_name) != presets._norm_name(new_name)
        ui.info("  uv.lock          " + ("would re-lock (uv lock): the project name changes" if relock else "unchanged (same normalized project name)"))
        mention = _pattern(planned.names)
        generated = sorted(path for path, content in render.outputs(cfg).items() if mention.search(content))
        ui.info("  generated files  " + (f"would re-render {', '.join(generated)}" if generated else "unchanged"))
        return 0

    from .cli import COMMANDS
    from .cmd_env import ensure_lock

    apply_plan(ROOT, planned)
    new_cfg = config.load(set(COMMANDS))
    try:
        ensure_lock(new_cfg)
    except DeployError as e:
        raise DeployError(f"{e}\n  The files are already renamed: fix the problem above and run ./deploy lock", e.code) from None
    changed, edited = render.apply(new_cfg)
    if changed:
        ui.info(f"render: updated {', '.join(changed)}")
    if edited:
        ui.warn(f"not overwriting hand-edited generated files: {', '.join(edited)} (./deploy render --force)")
    ui.ok(f"renamed '{old_name}' -> '{new_name}' (package src/{new_cfg.pkg}/)")
    ui.info("  Next: ./deploy test all, and review the changes with git diff")
    if DIST.is_dir() and any(DIST.iterdir()):
        ui.info(f"  dist/ still holds the artifacts built as '{old_name}' (./deploy clean removes dist/ and .build/)")
    return 0
