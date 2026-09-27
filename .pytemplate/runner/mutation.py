"""selftest --mutation [--diff BASE] [--jobs N] [--json]: mutation testing of the runner.

A mutant is one runner module with one small change: a comparison turned around, a condition
negated, a number moved by one, an exception handler switched off... Cosmic Ray's operators make
them (tools/mutation_cr.py, run in an environment of its own). The tests that import the module
then run against the mutant: a failure KILLS it; a mutant that passes every test SURVIVED, a
change no test notices. Only the runner's own modules are mutated (SCOPE). With --diff BASE, only
the mutants on the lines changed since BASE (`git diff BASE`: uncommitted changes count too).

How (CLAUDE.md 13.1):
  * the mutants: the operators of OPERATORS (NumberReplacer's +1 only), none in an annotation, in
    the test or body of `if TYPE_CHECKING:` or on a line marked `# pragma: no mutate`; the runner
    makes ExceptionReplacer's itself (own_mutant: the class becomes `()`, `*()` in a tuple, so
    the handler lets it through), and skips a mutant that is no valid Python (made);
  * the tests of a module: the test files that import it (test_map), those likeliest to fail
    soon first (ordered: mentions of the module per second of baseline), with -x: the first
    failure ends the run;
  * every worker has a throwaway copy of the project (the files git lists, as the working tree
    has them) with a git repository and a .venv of its own, in the scratch base; a mutant is
    written into the copy, its tests run there, and the module gets its own bytes back;
  * the workers' tests run with a home, XDG and temp folders of their own (worker_env): a mutant
    can break the very code a test counts on to keep its writes in tmp_path, and they land
    there instead of in the user's folders; uv keeps its cache and Pythons (the UV_* variables
    that name them: a test that drops those sees the worker's empty home);
  * a module's tests first run once as they are (its baseline): they must pass, and their time
    sets the limit of each of its mutants (TIMEOUT_FACTOR, TIMEOUT_EXTRA);
  * a run counts only with pytest's own summary line in its output (classify): one that ends
    without it is an error, never a kill nor a survivor.

Exit codes: 0 when every mutant was judged (the survivors are the report, not a failure); 1 when
a baseline failed or a mutant's run ended in an error; 2 usage; 3 a missing requirement (git, uv,
Cosmic Ray's environment); 130 interrupted (Ctrl+C, SIGTERM, SIGHUP): the report of what ran.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import difflib
import io
import itertools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import warnings
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any

from . import envs, proc, ui
from .config import Config
from .e2e import child_env, kill_tree, rmtree, scrub_env, termination_as_interrupt
from .presets import _git_path
from .project import IS_WINDOWS, ROOT, TOOLS, check_private_dir, scratch_name, venv_python
from .ui import PytError

SCOPE = ".pytemplate/runner"  # the modules mutated, relative to the project
TESTS = ".pytemplate/tests"
DRIVER = TOOLS / "mutation_cr.py"  # Cosmic Ray's side (PEP 723 script, locked next to it)
MARKER = ".pytemplate-mutation"  # in the base dir: only a dir carrying it is ever wiped
# Cosmic Ray has an operator for every pair of comparisons and of binary operators (213 in all),
# mostly the same finding again. These make one change per symbol: the one that moves a boundary
# or turns a test around. Left out: `/` -> `*` (in the runner `/` joins paths: such a mutant only
# tells whether the line runs), decorator removal and variable insertion (mostly changes no test
# can see, or none at all).
_COMPARISONS = ("Eq_NotEq", "NotEq_Eq", "Lt_LtE", "LtE_Lt", "Gt_GtE", "GtE_Gt", "Is_IsNot", "IsNot_Is")
_BINARY = (
    "Add_Sub", "Sub_Add", "Mul_Div", "FloorDiv_Div", "Mod_FloorDiv", "Pow_Mul", "LShift_RShift", "RShift_LShift",
    "BitAnd_BitOr", "BitOr_BitAnd", "BitXor_BitOr",
)  # fmt: skip
_OTHERS = (
    "AddNot", "ReplaceTrueWithFalse", "ReplaceFalseWithTrue", "ReplaceAndWithOr", "ReplaceOrWithAnd",
    "ReplaceUnaryOperator_Delete_Not", "ReplaceUnaryOperator_Delete_USub", "ReplaceBreakWithContinue",
    "ReplaceContinueWithBreak", "ZeroIterationForLoop", "ExceptionReplacer", "NumberReplacer",
)  # fmt: skip
OPERATORS = (
    *(f"core/ReplaceComparisonOperator_{c}" for c in _COMPARISONS),
    *(f"core/ReplaceBinaryOperator_{b}" for b in _BINARY),
    *(f"core/{o}" for o in _OTHERS),
)
EXCEPTION_REPLACER = "core/ExceptionReplacer"  # its mutants are the runner's own (own_mutant)
PRAGMA = re.compile(r"#\s*pragma:\s*no mutate\b")
TIMEOUT_FACTOR, TIMEOUT_EXTRA = 2, 60.0  # a mutant's tests: twice their baseline time, plus a minute
BASELINE_TIMEOUT = 3600.0  # one module's tests as they are
KILLED, TIMEOUT, SURVIVED, ERROR, UNTESTED, SKIPPED, NOT_RUN = "killed", "timeout", "survived", "error", "untested", "skipped", "not run"
STATUSES = (KILLED, TIMEOUT, SURVIVED, ERROR, UNTESTED, SKIPPED, NOT_RUN)
PASS, FAIL = "PASS", "FAIL"
GIT_IDENTITY = ("-c", "user.name=pytemplate mutation", "-c", "user.email=mutation@example.invalid")
# a user's PYTEST_ADDOPTS (-n auto, --lf...) and PYTEST_PLUGINS would change what every run means
PYTEST_VARIABLES = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")


# --- options ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    diff: str | None
    jobs: int
    as_json: bool


def default_jobs() -> int:
    """Half the CPUs, at most 8: a test run also starts shells, uv and git, and the launcher tests
    take several times longer on a loaded machine."""
    return max(1, min(8, (os.cpu_count() or 1) // 2))


def parse_args(args: Sequence[str]) -> Options:
    parser = argparse.ArgumentParser(
        prog="./pyt selftest --mutation", allow_abbrev=False,
        description="Mutation testing of the runner with Cosmic Ray: the mutants no test notices.",
    )  # fmt: skip
    parser.add_argument("--diff", metavar="BASE", help="only the mutants on the lines changed since the commit BASE (git diff BASE)")
    parser.add_argument("--jobs", type=int, default=default_jobs(), metavar="N", help=f"workers, each with a copy of the project (default here: {default_jobs()})")
    parser.add_argument("--json", action="store_true", help="print the results as JSON on stdout")
    ns = parser.parse_args(list(args))
    if ns.jobs < 1:
        raise PytError(f"selftest --mutation --jobs: {ns.jobs} is not a number of workers (1 or more)")
    if ns.diff is not None and not ns.diff.strip():
        raise PytError("selftest --mutation --diff: name the commit to compare with (e.g. origin/main)")
    return Options(diff=ns.diff, jobs=ns.jobs, as_json=ns.json)


# --- what to mutate and which tests run -----------------------------------------------------------


def scope_files(root: Path) -> list[str]:
    """The runner's modules, relative to the project (posix)."""
    return sorted(p.relative_to(root).as_posix() for p in (root / SCOPE).rglob("*.py") if "__pycache__" not in p.parts)


def module_of(rel: str) -> str:
    """".pytemplate/runner/methods/common.py" -> "runner.methods.common" (__init__.py: the package)."""
    parts = list(PurePosixPath(rel).relative_to(PurePosixPath(SCOPE).parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _names(data: bytes) -> Counter[str]:
    try:
        return Counter(t.string for t in tokenize.tokenize(io.BytesIO(data).readline) if t.type == tokenize.NAME)
    except (tokenize.TokenError, SyntaxError):
        return Counter(re.findall(r"\w+", data.decode("utf-8", errors="replace")))


def test_map(root: Path, modules: Iterable[str]) -> dict[str, dict[str, int]]:
    """Each runner module (dotted) -> the test files that import it, relative to the project,
    with how often each names what it imports (ordered: first). Imports anywhere in a file
    count: `from runner import x`, `from runner.x import y`, `import runner.x`. A module no test
    file imports gets no test: its mutants are untested."""
    known = set(modules)
    found: dict[str, dict[str, int]] = {m: {} for m in known}
    for path in sorted((root / TESTS).glob("test_*.py")):
        data = path.read_bytes()
        try:
            tree = ast.parse(data)
        except (SyntaxError, ValueError):  # pytest says what is wrong with it; no module is its
            continue
        bound: dict[str, set[str]] = {}  # module -> the names (dotted for `import runner.x`) the file uses it by
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.split(".")[0] == "runner":
                for alias in node.names:
                    name = f"{node.module}.{alias.name}"
                    module = name if name in known else node.module
                    if module in known:
                        bound.setdefault(module, set()).add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in known:
                        bound.setdefault(alias.name, set()).add(alias.asname or alias.name)
        if bound:
            words = _names(data)
            text = data.decode("utf-8", errors="replace")
            rel = path.relative_to(root).as_posix()
            for module, names in bound.items():
                found[module][rel] = sum(len(re.findall(rf"(?<![\w.]){re.escape(n)}\b", text)) if "." in n else words[n] for n in names)
    return found


def ordered(mentions: Mapping[str, int], seconds: Mapping[str, float] | None = None) -> list[str]:
    """The test files of a module in the order its mutants run them (-x: the first failure ends
    the run): the likeliest to fail soon first, by mentions of the module per second of their
    baseline (a slow file that names it often comes after a quick one), without times by
    mentions alone; then by name."""

    def rank(f: str) -> float:
        return mentions[f] / max(seconds.get(f, 1.0), 0.5) if seconds is not None else float(mentions[f])

    return sorted(mentions, key=lambda f: (-rank(f), f))


def junit_seconds(xml: Path, files: Iterable[str]) -> dict[str, float]:
    """The time pytest's JUnit XML gives each test file (the sum of its test cases): a case's
    classname is its file's path, dotted, without .py (then its class, if any). A report that
    cannot be read gives no time."""
    import xml.etree.ElementTree as ET  # our own pytest's report, never someone else's file

    dotted = {f.removesuffix(".py").replace("/", "."): f for f in files}
    seconds: dict[str, float] = {}
    try:
        cases = list(ET.parse(xml).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return seconds
    for case in cases:
        name = case.get("classname", "")
        while name and name not in dotted:
            name = name.rpartition(".")[0]
        if name:
            try:
                seconds[dotted[name]] = seconds.get(dotted[name], 0.0) + float(case.get("time", "0"))
            except ValueError:
                continue
    return seconds


_HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(text: str) -> dict[str, set[int]]:
    """`git diff --unified=0` output -> the lines (numbered in the new file) each file added or
    replaced. A deletion alone changes no line that is still there. A hunk's lines are counted
    off its header, so an added line that reads like a file header ("+++ x") stays a line; only
    git's own line breaks end a line (a changed line may hold a form feed). A name comes as git
    writes it: in C quotes when it holds a byte above 0x7f or a control character, and followed
    by a tab when it holds a blank."""
    changed: dict[str, set[int]] = {}
    current: set[int] | None = None
    left = 0  # lines of the current hunk still to come
    for line in text.split("\n"):
        if left > 0:
            if line.startswith(("+", "-", " ")):
                left -= 1
            continue
        if line.startswith("+++ "):
            target = _git_path(line[4:].removesuffix("\t"))
            current = None if target == "/dev/null" else changed.setdefault(target.removeprefix("b/"), set())
        elif m := _HUNK.match(line):
            removed, start = int(m[1]) if m[1] is not None else 1, int(m[2])
            added = int(m[3]) if m[3] is not None else 1
            left = removed + added
            if current is not None:
                current.update(range(start, start + added))
    return {path: lines for path, lines in changed.items() if lines}


def _git(root: Path, env: Mapping[str, str], *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    git = shutil.which("git")
    if git is None:
        raise PytError("selftest --mutation needs git: its workers are git repositories, and --diff asks git", 3)
    try:
        r = subprocess.run([git, *args], cwd=root, env=dict(env), stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PytError(f"selftest --mutation: git {' '.join(args[:2])} did not run in {root}: {e}", 3) from None
    if check and r.returncode != 0:
        why = r.stderr.decode("utf-8", errors="replace").strip() or f"exit code {r.returncode}"
        raise PytError(f"selftest --mutation: git {' '.join(args[:2])} failed in {root}: {why}", 3)
    return r


def root_env() -> dict[str, str]:
    """git of the project itself: the user's view of it (no GIT_* of a caller such as a hook,
    which would point git at another repository), messages in English."""
    return {**scrub_env(os.environ), "LC_ALL": "C"}


def changed_lines(root: Path, base: str, env: Mapping[str, str]) -> dict[str, set[int]]:
    """The runner's lines changed since the commit `base`: tracked files against the working
    tree (uncommitted changes included), and every line of an untracked module. The options
    that shape the output are all given, whatever the user's git configuration says (hunks
    merged by diff.interHunkContext would count the lines between them as changed)."""
    if _git(root, env, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False).returncode != 0:
        raise PytError(f"selftest --mutation --diff: {base!r} names no commit here (fetch it first, e.g. git fetch origin main)")
    text = _git(
        root, env, "diff", "--relative", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=0",
        "--inter-hunk-context=0", "--src-prefix=a/", "--dst-prefix=b/", base, "--", SCOPE,
    ).stdout.decode("utf-8", errors="replace")  # fmt: skip
    changed = {path: lines for path, lines in parse_diff(text).items() if path.endswith(".py")}
    untracked = _git(root, env, "ls-files", "-z", "--others", "--exclude-standard", "--", SCOPE).stdout
    for raw in filter(None, untracked.split(b"\0")):
        path = os.fsdecode(raw)
        if path.endswith(".py") and (root / path).is_file():
            lines = _lines((root / path).read_bytes().decode("utf-8", errors="replace"))
            count = len(lines) - (lines[-1] == "")  # a last line break ends a line, it starts none
            changed[path] = set(range(1, count + 1))
    return changed


def _lines(source: str) -> list[str]:
    """The lines as Python and parso number them: split at LF, CRLF and a lone CR only."""
    return re.split(r"\r\n|\r|\n", source)


Position = tuple[int, int]  # (line from 1, column from 0 in characters), as parso gives them


def _char_pos(lines: Sequence[str], line: int, byte_col: int) -> Position:
    """ast's (line, column in UTF-8 bytes) -> a Position (column in characters)."""
    text = lines[line - 1] if 0 < line <= len(lines) else ""
    return line, len(text.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))


def skipped_spans(source: str) -> list[tuple[Position, Position]]:
    """Where no mutant is made: annotations (never evaluated: every runner module has `from
    __future__ import annotations`) and the test and body of `if TYPE_CHECKING:` (never run)."""
    lines = _lines(source)

    def pos(line: int, byte_col: int) -> Position:
        return _char_pos(lines, line, byte_col)

    spans: list[tuple[Position, Position]] = []
    for node in ast.walk(ast.parse(source)):
        annotations: list[ast.expr] = []
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotations.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
        elif isinstance(node, ast.If) and _is_type_checking(node.test):
            last = node.body[-1]
            spans.append((pos(node.lineno, node.col_offset), pos(last.end_lineno or last.lineno, last.end_col_offset or 0)))
        for a in annotations:
            spans.append((pos(a.lineno, a.col_offset), pos(a.end_lineno or a.lineno, a.end_col_offset or 0)))
    return spans


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def handler_classes(source: str) -> dict[tuple[Position, Position], str]:
    """Where the exception classes of the except clauses are written, each with the text that
    switches it off (an ExceptionReplacer mutant): the whole expression (`OSError`,
    `subprocess.TimeoutExpired`) becomes `()`, an empty tuple, which catches nothing; an element
    of a tuple (`except (OSError, ValueError):` names two) becomes `*()`, which leaves no element
    in its place (Python refuses a tuple inside that tuple: a TypeError when it is matched), with
    the parentheses of its own it may have (`((OSError), B)`: `(*())` is no valid Python). The
    elements of a tuple without parentheses (`except A, B:`, Python 3.14) are left out: there
    `*()` would read as `except*`."""
    lines = _lines(source)
    starts = _line_starts(source)
    classes: dict[tuple[Position, Position], str] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            t = node.type
            if isinstance(t, ast.Tuple) and not _parenthesized(ast.get_source_segment(source, t) or ""):
                continue
            for e in t.elts if isinstance(t, ast.Tuple) else [t]:
                span = _char_pos(lines, e.lineno, e.col_offset), _char_pos(lines, e.end_lineno or e.lineno, e.end_col_offset or 0)
                if isinstance(t, ast.Tuple):  # with the parentheses of its own
                    begin, end = (starts[line - 1] + column for line, column in span)
                    while True:
                        before, after = _skip_blanks(source, begin - 1, -1), _skip_blanks(source, end, 1)
                        if before < 0 or source[before] != "(" or source[after : after + 1] != ")":
                            break
                        begin, end = before, after + 1
                    span = _position(starts, begin), _position(starts, end)
                classes[span] = "*()" if isinstance(t, ast.Tuple) else "()"
    return classes


def _line_starts(source: str) -> list[int]:
    """Where each line starts in `source`, as _lines counts them."""
    return [0] + [m.end() for m in re.finditer(r"\r\n|\r|\n", source)]


def _position(starts: Sequence[int], offset: int) -> Position:
    line = bisect.bisect_right(starts, offset)
    return line, offset - starts[line - 1]


def _skip_blanks(source: str, index: int, step: int) -> int:
    """The first index from `index` on (going by `step`) that holds no blank, line break or line
    continuation; -1 or len(source) past an end."""
    while 0 <= index < len(source) and source[index] in " \t\f\r\n\\":
        index += step
    return index


def _parenthesized(text: str) -> bool:
    """Whether a tuple's text is in parentheses of its own: `(A, B)`, not `(A), (B)`."""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    try:
        ast.parse("[" + text[1:-1] + "\n]", mode="eval")  # the line break ends a comment on the last line
    except SyntaxError:
        return False
    return True


# --- mutants ------------------------------------------------------------------------------------


@dataclass
class Mutant:
    file: str  # relative to the project
    operator: str  # Cosmic Ray's name: core/ReplaceComparisonOperator_Eq_NotEq
    occurrence: int  # Cosmic Ray's: the rank among the mutants of this operator in the file
    line: int
    column: int
    end_line: int
    end_column: int
    function: str | None  # the function or class around it
    status: str = NOT_RUN
    seconds: float = 0.0
    detail: str = ""  # the failing test of a kill; why for an error, a skip or an untested one
    diff: list[str] = field(default_factory=list)

    @property
    def module(self) -> str:
        return module_of(self.file)

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}"


def select(file: str, listed: Iterable[Sequence[Any]], source: str, changed: set[int] | None) -> list[Mutant]:
    """Cosmic Ray's list for `file` -> the mutants to test: NumberReplacer's +1 only (its -1 is
    the same finding again), none in skipped_spans or on a line with the pragma, and with a set
    of `changed` lines (--diff) only the mutants that touch one of them. An ExceptionReplacer
    mutant takes the span of the exception class it is in (handler_classes: Cosmic Ray names a
    part of a dotted one), one mutant per class."""
    spans = skipped_spans(source)
    classes = handler_classes(source)
    lines = _lines(source)
    out: list[Mutant] = []
    seen: set[tuple[Position, Position]] = set()
    for item in listed:
        name, function = str(item[0]), None if item[7] is None else str(item[7])
        occurrence, index, line, column, end_line, end_column = (int(x) for x in item[1:7])
        if name == "core/NumberReplacer" and index != 0:
            continue
        if name == EXCEPTION_REPLACER:
            span = next(((a, b) for a, b in classes if a <= (line, column) < b), None)
            if span is not None:
                if span in seen:
                    continue
                seen.add(span)
                (line, column), (end_line, end_column) = span
        if any(a <= (line, column) <= b for a, b in spans):
            continue
        if 0 < line <= len(lines) and PRAGMA.search(lines[line - 1]):
            continue
        if changed is not None and not changed.intersection(range(line, end_line + 1)):
            continue
        out.append(Mutant(file, name, occurrence, line, column, end_line, end_column, function))
    return out


def own_mutant(m: Mutant, source: str) -> str | None:
    """The mutant the runner makes itself, None for one Cosmic Ray makes. ExceptionReplacer: the
    exception class at the mutant's span is switched off (handler_classes), so its handler no
    longer catches it and the exception goes on. Cosmic Ray writes CosmicRayTestingException
    there, a class only its own package defines: every exception that reached the handler became
    a NameError, which fails a test that expects another exception to pass through (a kill that
    says nothing about the handler), and a dotted name in a tuple made it fail (CLAUDE.md 15.1).
    A span that is no exception class (select could not place Cosmic Ray's) is left to it."""
    span = ((m.line, m.column), (m.end_line, m.end_column))
    off = handler_classes(source).get(span) if m.operator == EXCEPTION_REPLACER else None
    if off is None:
        return None
    starts = _line_starts(source)
    begin, end = (starts[line - 1] + column for line, column in span)
    return source[:begin] + off + source[end:]


_COMPILE_LOCK = threading.Lock()  # warnings.catch_warnings changes the filters of every thread


def made(answer: Mapping[str, Any], original: bytes, file: str) -> tuple[str, str, str | None]:
    """Cosmic Ray's answer to a mutate request -> (status, detail, the mutated module). NOT_RUN
    with the module: a mutant to test. SKIPPED: one no test run can judge: Cosmic Ray cannot make
    it, it is the module unchanged, or it is no valid Python (pytest would stop at collection,
    which reads as a kill: `**` of a dict display turned into `*`, `not` before a walrus). ERROR:
    an answer that makes no sense, a problem of the harness."""
    if "error" in answer:
        return ERROR, f"Cosmic Ray's side failed: {answer['error']}", None
    if answer.get("cannot"):
        return SKIPPED, f"Cosmic Ray cannot make this mutant: {answer['cannot']}", None
    code = answer.get("code")
    if not isinstance(code, str):
        return ERROR, "Cosmic Ray made no mutant at an occurrence its list named", None
    if code.encode("utf-8") == original:
        return SKIPPED, "the mutant is the module unchanged", None
    try:
        with _COMPILE_LOCK, warnings.catch_warnings():
            warnings.simplefilter("ignore")  # SyntaxWarnings of a mutant are no verdict
            compile(code, file, "exec", dont_inherit=True)
    except SyntaxError as e:
        return SKIPPED, f"the mutant is no valid Python: {e.msg} (line {e.lineno})", code
    except ValueError as e:  # a NUL byte
        return SKIPPED, f"the mutant is no valid Python: {e}", code
    return NOT_RUN, "", code


def mutant_diff(original: str, mutated: str, limit: int = 12) -> list[str]:
    """The lines the mutant changes, `-` before and `+` after."""
    changed = difflib.unified_diff(_lines(original), _lines(mutated), lineterm="", n=0)
    return [line for line in changed if line[:1] in "+-" and not line.startswith(("+++", "---"))][:limit]


_ITEM = r"\d+ [a-z]+(?: [a-z]+)*"  # "12 passed", "3 subtests passed"
_SUMMARY = re.compile(rf"^=*\s*({_ITEM}(?:, {_ITEM})*) in \d+(?:\.\d+)?s\b")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def pytest_counts(output: str) -> dict[str, int] | None:
    """The counts of pytest's last summary line ("1 failed, 12 passed in 3.2s"), or None when
    the output has none: pytest did not get to the end. Colours are read through (a FORCE_COLOR
    or PY_COLORS the runs did not turn off would colour the line)."""
    for line in reversed(output.splitlines()):
        m = _SUMMARY.match(_ANSI.sub("", line).strip())
        if m:
            counts: dict[str, int] = {}
            for part in m[1].split(", "):
                number, word = part.split(" ", 1)
                word = {"errors": "error", "warnings": "warning"}.get(word, word)
                counts[word] = counts.get(word, 0) + int(number)
            return counts
    return None


def _failures(counts: Mapping[str, int]) -> int:
    """Failed and erroring tests, subtests included ("2 subtests failed"); never "xfailed"."""
    return sum(n for words, n in counts.items() if words.split()[-1] in ("failed", "error", "errors"))


def _first_failure(output: str) -> str:
    for line in output.splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            return line.strip()[:200]
    return _last_line(output)


def _last_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no output"


def classify(code: int | None, output: str, *, stopped: bool = False) -> tuple[str, str]:
    """A test run -> (status, detail). `stopped`: the whole run was stopped (an interrupt) while
    this one ran, which then proves nothing, whatever it returned (on Windows a Ctrl+C reaches
    the tests too, and pytest ends with its own code 2). `code` None: the run was ended by its
    time limit. A kill needs pytest's word for it: exit code 1 or 2 with failed or erroring tests
    in its summary; a survivor its summary with none; any other end (no summary: a crash, a lost
    child, a Python that did not start) proves neither and is an error."""
    if stopped:
        return NOT_RUN, "interrupted"
    if code is None:
        return TIMEOUT, ""
    output = _ANSI.sub("", output)
    counts = pytest_counts(output)
    if counts is None:
        return ERROR, f"exit code {proc.exit_code(code)} without pytest's summary line: {_last_line(output)}"
    failures = _failures(counts)
    if code == 0 and failures == 0:
        return SURVIVED, ""
    if code in (1, 2) and failures:
        return KILLED, _first_failure(output)
    return ERROR, f"exit code {proc.exit_code(code)}: {_last_line(output)}"


# --- Cosmic Ray (tools/mutation_cr.py) ------------------------------------------------------------


class Driver:
    """tools/mutation_cr.py serve in its own environment (`uv run --locked --script`): one request
    at a time, from any worker thread. Its stderr goes to `log`."""

    def __init__(self, uv: str, cfg: Config, log: Path, argv: Sequence[str] = ()) -> None:
        argv = list(argv) or [
            uv, "run", "--quiet", "--locked", "--python", cfg.python.cpython, "--python-preference", "only-managed",
            "--script", str(DRIVER), "serve",
        ]  # fmt: skip
        self.log = log
        self._err: IO[bytes] = log.open("ab")
        self._lock = threading.Lock()
        try:
            self._child: subprocess.Popen[str] = subprocess.Popen(
                argv, cwd=log.parent, env=proc.base_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._err,
                text=True, encoding="utf-8", errors="replace",
            )  # fmt: skip
        except OSError as e:
            self._err.close()
            raise PytError(f"selftest --mutation: cannot start uv for Cosmic Ray: {e}", 3) from None

    def ask(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """The answer to `request`; a driver that stopped is a PytError (3) naming its log."""
        with self._lock:
            assert self._child.stdin is not None and self._child.stdout is not None
            try:
                self._child.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
                self._child.stdin.flush()
                line = self._child.stdout.readline()
            except (OSError, ValueError):
                line = ""
        if not line:
            self._child.wait()
            raise PytError(
                f"selftest --mutation: Cosmic Ray's side stopped (exit code {self._child.returncode}): {self._tail()}"
                f"  (log: {self.log}; uv makes its environment from {DRIVER.name}.lock: offline, it must be in uv's cache)",
                3,
            )
        try:
            answer = json.loads(line)
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise PytError(f"selftest --mutation: Cosmic Ray's side answered {line.strip()[:200]!r}  (log: {self.log})", 3)
        return answer

    def _tail(self) -> str:
        try:
            lines = [ln.strip() for ln in self.log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        except OSError:
            return "no log"
        return lines[-1][:300] if lines else "no output"

    def close(self) -> None:
        try:
            if self._child.stdin is not None:
                self._child.stdin.close()
            self._child.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self._child.kill()
            self._child.wait()
        finally:
            self._err.close()


def list_mutants(driver: Driver, root: Path, files: Sequence[str], changed: Mapping[str, set[int]] | None, snapshot: Path) -> tuple[list[Mutant], dict[str, bytes]]:
    """The mutants of `files` to test, and each file's bytes (what the workers' copies hold, and
    get back after each mutant). Both come from a snapshot of the files, taken now into
    `snapshot`, which Cosmic Ray reads for every mutant too: the runner may be edited while a run
    goes on (a whole pass takes hours), and a mutant is only ever the change of one line."""
    mutants: list[Mutant] = []
    originals: dict[str, bytes] = {}
    for rel in files:
        data = (root / rel).read_bytes()
        (snapshot / rel).parent.mkdir(parents=True, exist_ok=True)
        (snapshot / rel).write_bytes(data)
        answer = driver.ask({"op": "list", "path": str(snapshot / rel), "operators": list(OPERATORS)})
        listed = answer.get("mutants")
        if "error" in answer or not isinstance(listed, list):
            raise PytError(f"selftest --mutation: Cosmic Ray could not list the mutants of {rel}: {answer.get('error', answer)}", 1)
        try:
            chosen = select(rel, listed, data.decode("utf-8"), None if changed is None else changed.get(rel, set()))
        except (SyntaxError, UnicodeDecodeError, ValueError) as e:
            raise PytError(f"selftest --mutation: {rel} is no Python the runner can read: {e}", 1) from None
        originals[rel] = data
        mutants += chosen
    return mutants, originals


# --- workers ------------------------------------------------------------------------------------


@dataclass
class Worker:
    index: int
    copy: Path  # the throwaway copy of the project
    python: Path  # the Python of the copy's .venv
    tmp: Path  # pytest's --basetemp
    log: Path  # the output of its last run
    env: dict[str, str]  # worker_env


@dataclass
class Baseline:
    module: str
    tests: list[str]  # the order of the baseline; then the order of its mutants (by time too)
    status: str = NOT_RUN
    seconds: float = 0.0
    detail: str = ""
    log: str = ""


def default_base() -> Path:
    """A short path (MAX_PATH on Windows), per user on POSIX (check_private_dir)."""
    tmp = Path(tempfile.gettempdir())
    return tmp / "pt" / "mut" if IS_WINDOWS else tmp / scratch_name("pt-mutation")


def prepare_base(base: Path, root: Path = ROOT) -> None:
    if base.resolve() == root.resolve() or root.resolve() in base.resolve().parents:
        raise PytError("selftest --mutation: the scratch base cannot be inside the project")
    if base.exists() and not base.is_dir():
        raise PytError(f"selftest --mutation: {base} is not a directory")
    check_private_dir(base, "scratch folder (TMPDIR)")
    if base.is_dir() and any(base.iterdir()) and not (base / MARKER).is_file():
        raise PytError(f"selftest --mutation: {base} is not empty and was not made by selftest --mutation (no {MARKER})")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    (base / MARKER).write_text("Made by ./pyt selftest --mutation: safe to delete.\n", encoding="utf-8", newline="\n")


@contextmanager
def base_lock(base: Path) -> Iterator[None]:
    """One run at a time per base: a lock on <base>/lock for the whole run (the OS drops it when
    the process ends, however it ends). A second run would wipe the first one's copies."""
    fd = os.open(base / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PytError(f"selftest --mutation: another run is using {base}: wait for it to end") from None
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)  # releases the lock (POSIX)


def listed_files(root: Path, env: Mapping[str, str]) -> list[str]:
    """What a worker's copy holds: the files git lists (tracked, and untracked but not ignored),
    as the working tree has them."""
    out = _git(root, env, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    return sorted({os.fsdecode(p) for p in out.split(b"\0") if p})


def make_copy(root: Path, dest: Path, files: Sequence[str], env: Mapping[str, str], contents: Mapping[str, bytes] | None = None) -> None:
    """A copy of the project with a git repository of its own (one commit of every file), so the
    tests that ask git about the project find one. Links stay links. The files `contents` names
    hold its bytes (list_mutants' snapshot), not the working tree's."""
    contents = contents or {}
    for rel, data in contents.items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(data)
    for rel in files:
        if rel in contents:
            continue
        src = root / rel
        if not os.path.lexists(src) or (src.is_dir() and not src.is_symlink()):
            continue  # deleted from the working tree, or a submodule
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, target, follow_symlinks=False)
        except OSError:
            if not src.is_symlink():
                raise
            shutil.copy2(src, target)  # Windows without the right to make links: what it names
    _git(dest, env, "init", "-q")
    _git(dest, env, "add", "-A")
    _git(dest, env, *GIT_IDENTITY, "commit", "-q", "--no-verify", "-m", "selftest --mutation")


def sync_copy(venv: envs.PyEnv, copy: Path) -> None:
    """The copy's .venv as envs.sync makes the project's (the copy is the project: its cwd), with
    one --quiet whatever ./pyt's -q says: uv's progress goes, its errors stay (`uv -qq` fails
    without a word)."""
    no_groups = [a for group, _ in envs.left_out(venv) for a in ("--no-group", group)]
    envs.uv(venv, ["--quiet", "sync", "--locked", "--all-groups", *no_groups], cwd=copy, quiet=False)


def worker_env(worker_copy: Path, home: Path, tmp: Path, base_env: Mapping[str, str], keep: Mapping[str, str], cfg: Config, uv: str) -> dict[str, str]:
    """What `./pyt selftest` gives pytest (uv run in the project's environment), for the copy:
    its .venv first on PATH, VIRTUAL_ENV, UV and the variables of envs.env_vars. No bytecode
    written: a module's mutants and its own bytes come and go (and set_mtime keeps a stale .pyc
    from ever matching one of them). The home, XDG and temp folders are the worker's own (the
    XDG ones default below its home): whatever a mutant makes a test write there stays in the
    base. `keep` holds uv's own folders as uv resolves them outside (nvimtest.uv_dirs: its cache,
    Pythons and tools), which the moved home would otherwise take away."""
    env = {k: v for k, v in base_env.items() if not k.upper().startswith("XDG_") and k.upper() not in PYTEST_VARIABLES}
    env["HOME"] = str(home)
    if IS_WINDOWS:
        env.update(USERPROFILE=str(home), LOCALAPPDATA=str(home / "AppData" / "Local"), APPDATA=str(home / "AppData" / "Roaming"))
        env.update(TEMP=str(tmp), TMP=str(tmp))
    else:
        env["TMPDIR"] = str(tmp)
    env.update(keep)
    venv = worker_copy / ".venv"
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    env[key] = os.pathsep.join(p for p in (str(venv_python(venv).parent), env.get(key, "")) if p)
    env.update(
        VIRTUAL_ENV=str(venv), UV=uv, UV_PROJECT_ENVIRONMENT=str(venv), UV_PYTHON=cfg.python.cpython,
        UV_PYTHON_PREFERENCE="only-managed", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
    )  # fmt: skip
    return env


_last_mtime = 0
_MTIME_LOCK = threading.Lock()


def set_mtime(path: Path) -> None:
    """A time no earlier write had (whole seconds, as a .pyc records it): each one after the one
    before and after the clock, so never the time of a write made by the clock either (the copy
    of the project): a .pyc made from one version of a module never passes for another of the
    same size."""
    global _last_mtime
    with _MTIME_LOCK:
        _last_mtime = seconds = max(_last_mtime + 1, int(time.time()) + 1)
    os.utime(path, ns=(seconds * 10**9, seconds * 10**9))


def write_module(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    set_mtime(path)


def descendants(pid: int) -> list[int]:
    """The processes below `pid` (its children, theirs...), whatever their session. Linux reads
    /proc, the other POSIX systems ask `ps`; none when neither answers."""
    children: dict[int, list[int]] = {}
    if sys.platform.startswith("linux"):
        try:
            names = [e.name for e in os.scandir("/proc") if e.name.isdigit()]
        except OSError:
            names = []
        for name in names:
            try:
                stat = Path("/proc", name, "stat").read_bytes()
            except OSError:
                continue  # it ended meanwhile
            fields = stat.rpartition(b")")[2].split()  # after "PID (NAME)": a name may hold anything
            if len(fields) > 1 and fields[1].isdigit():
                children.setdefault(int(fields[1]), []).append(int(name))
    else:
        ps = shutil.which("ps")
        try:
            out = b"" if ps is None else subprocess.run([ps, "-A", "-o", "pid=", "-o", "ppid="], stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = b""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    todo = [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child != pid and child not in found:
                found.append(child)
                todo.append(child)
    return found


def kill_run(child: subprocess.Popen[bytes]) -> None:
    """End a test run, tree and all. A test may start a process in a session of its own (the
    runner's signal tests do), which a signal to pytest's group never reaches: on POSIX every
    process below pytest is found first (once pytest is dead, they belong to init) and killed
    too, as taskkill /T does on Windows. A mutant can make such a process loop for ever."""
    if sys.platform == "win32":
        kill_tree(child)
    else:
        import signal

        below = descendants(child.pid) if child.poll() is None else []
        kill_tree(child)
        for pid in below:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass  # gone already


class Runs:
    """The pytest runs of the workers: started with a time limit, and killed, tree and all, when
    it passes or on stop() (an interrupt)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        self.stopped = threading.Event()

    def stop(self) -> None:
        self.stopped.set()
        with self._lock:
            children = list(self._children.values())
        for child in children:
            kill_run(child)

    def run(self, worker: Worker, tests: Sequence[str], timeout: float, junit: Path | None = None) -> tuple[int | None, str, float]:
        """pytest on `tests` in the worker's copy: (exit code or None when ended, output, seconds).
        The same seed for Hypothesis every time: a mutant is judged on the inputs its baseline had."""
        argv = [
            str(worker.python), "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "--color=no", f"--basetemp={worker.tmp}",
            "--hypothesis-seed=0", *([f"--junitxml={junit}"] if junit else []), *tests,
        ]  # fmt: skip
        start = time.perf_counter()
        code: int | None = None
        with worker.log.open("wb") as out:
            if self.stopped.is_set():
                return None, "", 0.0
            child = subprocess.Popen(
                argv, cwd=worker.copy, env=worker.env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=not IS_WINDOWS,
            )  # fmt: skip
            with self._lock:
                self._children[worker.index] = child
            try:
                while True:
                    try:
                        code = child.wait(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        if self.stopped.is_set() or time.perf_counter() - start > timeout:
                            kill_run(child)
                            break
            finally:
                with self._lock:
                    self._children.pop(worker.index, None)
                if child.poll() is None:
                    kill_run(child)
        if self.stopped.is_set():
            code = None  # stop() may kill it before this loop sees the stop: its exit code is then stop()'s kill
        return code, worker.log.read_bytes().decode("utf-8", errors="replace"), time.perf_counter() - start


def run_all(workers: Sequence[Worker], items: Sequence[Any], handle: Callable[[Worker, Any], None], runs: Runs) -> None:
    """handle(worker, item) for every item, each worker taking the next one, until all ran or
    runs.stop(). A Ctrl+C (or SIGTERM, SIGHUP: termination_as_interrupt) reaches the main thread:
    it stops the runs, waits for the workers and goes on as a KeyboardInterrupt."""
    todo: queue.SimpleQueue[Any] = queue.SimpleQueue()
    for item in items:
        todo.put(item)
    failures: list[BaseException] = []

    def loop(worker: Worker) -> None:
        while not runs.stopped.is_set():
            try:
                item = todo.get_nowait()
            except queue.Empty:
                return
            try:
                handle(worker, item)
            except BaseException as e:  # noqa: BLE001 - reported by the main thread
                failures.append(e)
                runs.stop()
                return

    threads = [threading.Thread(target=loop, args=(w,), name=f"mutation-w{w.index}", daemon=True) for w in workers]
    try:
        for t in threads:  # inside the try: an interrupt may come while the others still start
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.2)
    except KeyboardInterrupt:
        runs.stop()
        for t in threads:
            if t.ident is not None:  # started
                t.join()
        raise
    if failures:
        raise failures[0]


# --- the run --------------------------------------------------------------------------------------


@dataclass
class Report:
    mutants: list[Mutant]
    baselines: dict[str, Baseline]
    base: Path
    seconds: float = 0.0
    interrupted: bool = False
    cosmic_ray: str = ""
    workers: int = 0
    note: str = ""  # why there is no mutant to test
    kept: bool = False  # the logs stay in the base
    error: BaseException | None = None  # what stopped the run (selftest raises it after the report)

    def counts(self) -> dict[str, int]:
        found = Counter(m.status for m in self.mutants)
        return {s: found.get(s, 0) for s in STATUSES}

    def score(self) -> float | None:
        """The killed share of the judged mutants (a timeout counts as a kill), None with none."""
        c = self.counts()
        judged = c[KILLED] + c[TIMEOUT] + c[SURVIVED]
        return (c[KILLED] + c[TIMEOUT]) / judged if judged else None

    def failed(self) -> bool:
        return (
            self.error is not None
            or any(b.status == FAIL for b in self.baselines.values())
            or any(m.status == ERROR for m in self.mutants)
        )

    def as_json(self, opts: Options) -> dict[str, Any]:
        score = self.score()
        return {
            "ok": not (self.interrupted or self.failed()),
            "interrupted": self.interrupted,
            "error": None if self.error is None else f"{type(self.error).__name__}: {self.error}",
            "base": str(self.base),
            "kept": self.kept,
            "seconds": round(self.seconds, 1),
            "cosmic_ray": self.cosmic_ray,
            "workers": self.workers,
            "options": {"diff": opts.diff, "jobs": opts.jobs},
            "counts": self.counts(),
            "score": None if score is None else round(score, 4),
            "baselines": [asdict(b) for b in sorted(self.baselines.values(), key=lambda b: b.module)],
            "mutants": [{**asdict(m), "module": m.module} for m in self.mutants],
        }


def print_report(report: Report) -> None:
    """The answer, shown even with -q (ui.report): a line per file, the score, then every mutant
    no test noticed with its change, and the ones that could not be judged."""
    if not report.mutants and report.error is not None:
        return  # nothing was listed: the error says why
    ui.step("mutation results")
    if not report.mutants:
        ui.report(f"  no mutant to test: {report.note or 'the runner has no module'}")
        return
    per_file: dict[str, Counter[str]] = {}
    for m in report.mutants:
        per_file.setdefault(m.file, Counter())[m.status] += 1
    shown = [s for s in STATUSES if any(c[s] for c in per_file.values())]
    width = max([len(f) for f in per_file] + [4])
    ui.report(f"  {'file':<{width}}  " + "  ".join(f"{s:>8}" for s in shown))
    for file in sorted(per_file):
        ui.report(f"  {file:<{width}}  " + "  ".join(f"{per_file[file][s]:>8}" for s in shown))
    c = report.counts()
    score = report.score()
    minutes, secs = divmod(int(report.seconds), 60)
    judged = c[KILLED] + c[TIMEOUT] + c[SURVIVED]
    score_text = "no mutant judged" if score is None else f"{score:.1%} ({c[KILLED] + c[TIMEOUT]} of {judged} judged mutants killed)"
    ui.report(f"  score: {score_text}, {len(report.mutants)} mutants in {minutes}m{secs:02d}s, {report.workers} workers")
    for b in sorted(report.baselines.values(), key=lambda b: b.module):
        if b.status == FAIL:
            ui.report(f"  baseline FAILED: the tests of {b.module} fail without a mutant: {b.detail}  (log: {b.log})")
    for title, status in (("survived", SURVIVED), ("errors", ERROR)):
        chosen = [m for m in report.mutants if m.status == status]
        if chosen:
            ui.report(f"{title} ({len(chosen)}):")
        for m in chosen:
            around = f" in {m.function}" if m.function else ""
            ui.report(f"  {m.where}{around}: {m.operator.split('/')[-1]}" + (f"  ({m.detail})" if m.detail else ""))
            for line in m.diff:
                ui.report(f"      {line}")


def _progress(lock: threading.Lock, done: Iterator[int], total: int, m: Mutant) -> None:
    with lock:
        n = next(done)
        ui.info(f"[{n}/{total}] {m.status:<8} {m.where} {m.operator.split('/')[-1]} ({m.seconds:.1f} s)")


def selftest(cfg: Config, args: list[str]) -> int:
    """selftest --mutation [--diff BASE] [--jobs N] [--json]"""
    opts = parse_args(args)
    uv = proc.find_uv()
    report = run(cfg, opts, uv, ROOT, default_base())
    with deferred_interrupts() as got:  # the report is what the run was for: it is written whole
        print_report(report)
        if opts.as_json:
            print(json.dumps(report.as_json(opts), indent=2), flush=True)
        if report.kept:
            ui.report(f"logs kept for inspection: {report.base / 'logs'}")
    if report.error is not None:
        raise report.error  # its own message and exit code (a traceback for a runner bug)
    if report.interrupted or got:
        ui.error("interrupted")
        return 130
    if report.failed():
        ui.error("selftest --mutation: a baseline failed or some mutants could not be judged (above)")
        return 1
    ui.ok("selftest --mutation: every mutant judged")
    return 0


def run(cfg: Config, opts: Options, uv: str, root: Path, base: Path) -> Report:
    """List the mutants, make the workers, run the baselines, then the mutants. What stops the
    run (an interrupt, an exception: `report.error`) leaves the report of what ran. The workers'
    folders always go; the logs stay when they are worth reading (an interrupt, a failed
    baseline, an error), and go otherwise. The base itself stays, with its marker and lock file
    only (removed, it could vanish under a second run that just prepared it)."""
    env = root_env()
    changed = changed_lines(root, opts.diff, env) if opts.diff else None
    scope = scope_files(root)
    files = [f for f in scope if changed is None or f in changed]
    report = Report([], {}, base)
    if not files:  # nothing to start, nothing written
        report.note = f"no runner line changed since {opts.diff}" if opts.diff else "the runner has no module"
        return report
    tests = test_map(root, {module_of(f) for f in scope})
    prepare_base(base, root)
    t0 = time.perf_counter()
    with base_lock(base), termination_as_interrupt():
        try:
            _run_locked(cfg, opts, uv, root, base, files, changed, tests, report)
        except KeyboardInterrupt:
            report.interrupted = True
        except Exception as e:  # noqa: BLE001 - the report of what ran comes first, then the error
            report.error = e
        finally:
            with deferred_interrupts() as got:  # the cleanup, whatever comes now (then the report)
                report.seconds = time.perf_counter() - t0
                _remove_workers(base)
                report.kept = report.interrupted or report.failed() or bool(got)
                if not report.kept:
                    _remove_logs(base)
            report.interrupted = report.interrupted or bool(got)
    return report


@contextmanager
def deferred_interrupts() -> Iterator[list[int]]:
    """Ctrl+C, SIGTERM and SIGHUP wait while the block runs (a cleanup, the report): the list
    gets each one that came, and the old handlers come back at its end. Main thread only: other
    threads never get a signal."""
    import signal

    got: list[int] = []
    saved: list[tuple[int, Any]] = []
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            number = getattr(signal, name, None)  # no SIGHUP on Windows
            if number is not None:
                saved.append((number, signal.signal(number, lambda signum, frame: got.append(signum))))
    try:
        yield got
    finally:
        for number, handler in saved:
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


def _run_locked(cfg: Config, opts: Options, uv: str, root: Path, base: Path, files: Sequence[str], changed: Mapping[str, set[int]] | None,
                tests: Mapping[str, Mapping[str, int]], report: Report) -> None:  # fmt: skip
    logs, snapshot = base / "logs", base / "snapshot"
    for d in (logs, snapshot):
        rmtree(d)
    logs.mkdir()
    driver = Driver(uv, cfg, logs / "cosmic-ray.log")
    try:
        report.cosmic_ray = str(driver.ask({"op": "version"}).get("version", ""))
        report.mutants, originals = list_mutants(driver, root, files, changed, snapshot)
        for m in report.mutants:
            if not tests.get(m.module):
                m.status, m.detail = UNTESTED, "no test file imports this module"
        todo = [m for m in report.mutants if m.status == NOT_RUN]
        what = f"{len(report.mutants)} mutants in {len(files)} files" + (f" changed since {opts.diff}" if opts.diff else "")
        ui.step(f"selftest --mutation: {what}, Cosmic Ray {report.cosmic_ray}, in {base}")
        if not report.mutants:
            report.note = "the changed lines hold nothing Cosmic Ray mutates here" if opts.diff else "no mutant in the runner"
        if todo:
            _test(cfg, opts, uv, root, base, tests, todo, originals, snapshot, driver, report)
    finally:
        driver.close()


def _test(cfg: Config, opts: Options, uv: str, root: Path, base: Path, tests: Mapping[str, Mapping[str, int]], todo: list[Mutant],
          originals: Mapping[str, bytes], snapshot: Path, driver: Driver, report: Report) -> None:  # fmt: skip
    """Make the workers, run the baselines of the modules in `todo`, then the mutants."""
    from .nvimtest import uv_dirs

    env = child_env(base)
    keep = uv_dirs(env)
    files = listed_files(root, root_env())
    workers: list[Worker] = []
    for i in range(min(opts.jobs, len(todo))):
        copy, home, scratch = base / f"w{i}", base / f"h{i}", base / f"t{i}"
        for d in (copy, home, scratch):
            rmtree(d)
        ui.info(f"worker {i}: a copy of the project in {copy}")
        make_copy(root, copy, files, env, originals)
        venv = envs.PyEnv("cpython", copy / ".venv", cfg.python.cpython, "only-managed")
        sync_copy(venv, copy)
        for d in (home, *((home / "AppData" / "Local", home / "AppData" / "Roaming") if IS_WINDOWS else ()), scratch / "tmp", scratch / "pytest"):
            d.mkdir(parents=True)
        wenv = worker_env(copy, home, scratch / "tmp", env, keep, cfg, uv)
        workers.append(Worker(i, copy, venv.python, scratch / "pytest", base / "logs" / f"w{i}.log", wenv))
    report.workers = len(workers)
    runs = Runs()
    modules = sorted({m.module for m in todo})
    report.baselines = {mod: Baseline(mod, ordered(tests[mod])) for mod in modules}

    def baseline(worker: Worker, module: str) -> None:
        b = report.baselines[module]
        junit = base / "logs" / f"junit-{module}.xml"
        code, output, b.seconds = runs.run(worker, b.tests, BASELINE_TIMEOUT, junit)
        status, detail = classify(code, output, stopped=runs.stopped.is_set())
        if status == SURVIVED:
            b.status = PASS
            b.tests = ordered(tests[module], junit_seconds(junit, b.tests))
        elif status != NOT_RUN:
            b.status, b.detail = FAIL, detail or f"no result after {BASELINE_TIMEOUT:.0f} s"
            saved = base / "logs" / f"baseline-{module}.log"
            shutil.copyfile(worker.log, saved)
            b.log = str(saved)
        ui.info(f"baseline of {module}: {b.status} ({b.seconds:.1f} s, {len(b.tests)} test files)")

    ui.step(f"baselines: the tests of {len(modules)} modules without a mutant")
    run_all(workers, modules, baseline, runs)
    ready = [m for m in todo if report.baselines[m.module].status == PASS]
    for m in todo:
        if report.baselines[m.module].status == FAIL:
            m.detail = "its module's tests fail without a mutant"
    lock, done = threading.Lock(), itertools.count(1)
    errors = itertools.count(1)

    def mutant(worker: Worker, m: Mutant) -> None:
        original = originals[m.file]
        own = own_mutant(m, original.decode("utf-8"))
        request = {"op": "mutate", "path": str(snapshot / m.file), "operator": m.operator, "occurrence": m.occurrence}
        answer = {"code": own} if own is not None else driver.ask(request)
        m.status, m.detail, code_text = made(answer, original, m.file)
        if code_text is not None:
            m.diff = mutant_diff(original.decode("utf-8"), code_text)
        if code_text is not None and m.status == NOT_RUN:
            target = worker.copy / m.file
            b = report.baselines[m.module]
            limit = TIMEOUT_FACTOR * b.seconds + TIMEOUT_EXTRA
            try:
                write_module(target, code_text.encode("utf-8"))
                code, output, m.seconds = runs.run(worker, b.tests, limit)
            finally:
                write_module(target, original)
            m.status, m.detail = classify(code, output, stopped=runs.stopped.is_set())
            if m.status == TIMEOUT:
                m.detail = f"no result after {limit:.0f} s"
            elif m.status == ERROR:
                saved = base / "logs" / f"error-{next(errors)}.log"
                shutil.copyfile(worker.log, saved)
                m.detail += f"  (log: {saved})"
        if m.status != NOT_RUN:
            _progress(lock, done, len(ready), m)

    if ready:
        ui.step(f"mutants: {len(ready)} to test, {len(workers)} workers")
        run_all(workers, ready, mutant, runs)


def _remove_workers(base: Path) -> None:
    """The workers' copies (a .venv each), homes and temp folders, and the snapshot of the
    modules: they always go."""
    try:
        for d in sorted(base.iterdir()):
            if d.is_dir() and (d.name == "snapshot" or re.fullmatch(r"[wht]\d+", d.name)):
                rmtree(d)
    except OSError as e:
        ui.warn(f"could not remove the workers' folders in {base}: {e}")


def _remove_logs(base: Path) -> None:
    """The logs, once nothing in them is worth reading (the lock is still held)."""
    try:
        rmtree(base / "logs")
    except OSError as e:
        ui.warn(f"could not remove {base / 'logs'}: {e}")
