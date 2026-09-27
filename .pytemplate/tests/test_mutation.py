"""Tests for runner/mutation.py: `./pyt selftest --mutation [--diff BASE] [--jobs N] [--json]`.

The pure parts (options, which tests a module gets and in what order, the lines a diff changed,
where no mutant is made, what a test run's output means, the report) on made-up inputs; the
Cosmic Ray side through a fake driver; the workers' processes (time limits, a stop, the threads)
with real pytest runs of tiny test files; and one real run, Cosmic Ray included, on a toy project
of one module (skipped when Cosmic Ray's environment cannot be made: offline, not in uv's cache).
"""

from __future__ import annotations

import _thread
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config, mutation, proc  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.mutation import KILLED, NOT_RUN, SURVIVED, TIMEOUT, Baseline, Mutant, Options, Report, Runs, Worker  # noqa: E402
from runner.project import ROOT  # noqa: E402
from runner.ui import PytError  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git_env(tmp: Path) -> dict[str, str]:
    """git with no configuration of the user's (a hooksPath, a signing key)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=str(tmp / "no-gitconfig"), GIT_CONFIG_NOSYSTEM="1", LC_ALL="C")
    return env


def _git(root: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(["git", *mutation.GIT_IDENTITY, *args], cwd=root, env=env, check=True, capture_output=True)


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    return root


# --- options ------------------------------------------------------------------------------------


def test_options() -> None:
    assert mutation.parse_args([]) == Options(diff=None, jobs=mutation.default_jobs(), as_json=False)
    assert mutation.parse_args(["--diff", "origin/main", "--jobs", "3", "--json"]) == Options("origin/main", 3, True)
    assert 1 <= mutation.default_jobs() <= 8


@pytest.mark.parametrize("args", [["--jobs", "0"], ["--jobs", "-2"], ["--diff", " "]])
def test_options_refuse_what_names_nothing(args: list[str]) -> None:
    with pytest.raises(PytError) as e:
        mutation.parse_args(args)
    assert e.value.code == 2


@pytest.mark.parametrize("args", [["--bogus"], ["--jobs", "x"], ["--diff"], ["--job", "2"], ["main"]])
def test_options_usage_errors_exit_2(args: list[str]) -> None:
    with pytest.raises(SystemExit) as e:
        mutation.parse_args(args)
    assert e.value.code == 2


# --- what is mutated, and which tests run ---------------------------------------------------------


def test_scope_and_module_names(tmp_path: Path) -> None:
    _write(tmp_path, {
        ".pytemplate/runner/__init__.py": "", ".pytemplate/runner/cli.py": "", ".pytemplate/runner/methods/__init__.py": "",
        ".pytemplate/runner/methods/common.py": "", ".pytemplate/runner/__pycache__/cli.cpython-314.py": "", ".pytemplate/tools/x.py": "",
    })  # fmt: skip
    assert mutation.scope_files(tmp_path) == [
        ".pytemplate/runner/__init__.py", ".pytemplate/runner/cli.py", ".pytemplate/runner/methods/__init__.py",
        ".pytemplate/runner/methods/common.py",
    ]  # fmt: skip
    assert [mutation.module_of(f) for f in mutation.scope_files(tmp_path)] == ["runner", "runner.cli", "runner.methods", "runner.methods.common"]


def test_test_map_finds_every_import_and_counts_the_mentions(tmp_path: Path) -> None:
    _write(tmp_path, {
        ".pytemplate/tests/test_a.py": (
            "from runner import alpha, beta as b\nfrom runner.gamma import thing\nimport runner.delta\n"
            "def test_x():\n    alpha.f(); alpha.g(); b.h(); thing(); runner.delta.z()\n"
            "    from runner.methods import common\n    common.x()\n"
        ),
        ".pytemplate/tests/test_b.py": "from runner import alpha\n" + "alpha.f()\n" * 9,
        ".pytemplate/tests/test_broken.py": "from runner import alpha\ndef (\n",  # pytest says what is wrong with it
        ".pytemplate/tests/helper.py": "from runner import eps\n",  # not a test file
    })  # fmt: skip
    modules = {"runner.alpha", "runner.beta", "runner.gamma", "runner.delta", "runner.eps", "runner.methods", "runner.methods.common"}
    found = mutation.test_map(tmp_path, modules)
    a, b = ".pytemplate/tests/test_a.py", ".pytemplate/tests/test_b.py"
    assert found["runner.alpha"] == {a: 3, b: 10}
    assert found["runner.beta"] == {a: 2} and found["runner.gamma"] == {a: 2} and found["runner.delta"] == {a: 2}
    assert found["runner.methods.common"] == {a: 2} and found["runner.methods"] == {}
    assert found["runner.eps"] == {}  # no test file imports it: its mutants are untested
    assert mutation.ordered(found["runner.alpha"]) == [b, a]


def test_ordered_puts_the_quickest_killers_first() -> None:
    mentions = {"slow.py": 40, "quick.py": 10, "none.py": 0, "also_quick.py": 10}
    assert mutation.ordered(mentions) == ["slow.py", "also_quick.py", "quick.py", "none.py"]
    seconds = {"slow.py": 80.0, "quick.py": 2.0, "also_quick.py": 0.1, "none.py": 1.0}
    assert mutation.ordered(mentions, seconds) == ["also_quick.py", "quick.py", "slow.py", "none.py"]  # 0.1 s counts as 0.5


def test_junit_seconds(tmp_path: Path) -> None:
    xml = tmp_path / "junit.xml"
    xml.write_text(
        '<testsuites><testsuite>'
        '<testcase classname=".pytemplate.tests.test_a" name="t1" time="1.5"/>'
        '<testcase classname=".pytemplate.tests.test_a.TestK" name="t2" time="0.5"/>'
        '<testcase classname=".pytemplate.tests.test_b" name="t3" time="2"/>'
        '<testcase classname=".pytemplate.tests.test_ab" name="t4" time="7"/>'
        '<testcase classname="elsewhere" name="t5" time="9"/>'
        '<testcase classname=".pytemplate.tests.test_b" name="t6" time="bad"/>'
        '</testsuite></testsuites>',
        encoding="utf-8",
    )  # fmt: skip
    files = [".pytemplate/tests/test_a.py", ".pytemplate/tests/test_b.py"]
    assert mutation.junit_seconds(xml, files) == {files[0]: 2.0, files[1]: 2.0}
    (tmp_path / "broken.xml").write_text("<testsuites", encoding="utf-8")
    assert mutation.junit_seconds(tmp_path / "broken.xml", files) == {}
    assert mutation.junit_seconds(tmp_path / "missing.xml", files) == {}


DIFF = """\
diff --git a/.pytemplate/runner/a.py b/.pytemplate/runner/a.py
index 1111111..2222222 100644
--- a/.pytemplate/runner/a.py
+++ b/.pytemplate/runner/a.py
@@ -2 +2 @@ def f():
-    return 1
+    return 2
@@ -10,0 +11,2 @@ def g():
++++ not a file header: an added line
+x = 1
@@ -20,3 +21,0 @@
-a
-b
-c
diff --git a/.pytemplate/runner/gone.py b/.pytemplate/runner/gone.py
deleted file mode 100644
--- a/.pytemplate/runner/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x
-y
diff --git a/.pytemplate/runner/new.py b/.pytemplate/runner/new.py
new file mode 100644
--- /dev/null
+++ b/.pytemplate/runner/new.py
@@ -0,0 +1,3 @@
+a
+b
+c
\\ No newline at end of file
"""


def test_parse_diff() -> None:
    assert mutation.parse_diff(DIFF) == {".pytemplate/runner/a.py": {2, 11, 12}, ".pytemplate/runner/new.py": {1, 2, 3}}
    assert mutation.parse_diff("") == {}
    # git ends the name of a file that holds a blank with a tab (as GNU diff does, for patch)
    spaced = "--- a/x y.py\t\n+++ b/x y.py\t\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    assert mutation.parse_diff(spaced) == {"x y.py": {1}}
    # and a name with a byte above 0x7f in C quotes (core.quotePath), octal escapes for its UTF-8
    quoted = '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1 @@\n-x = 1\n+x = 2\n'
    assert mutation.parse_diff(quoted) == {"caf\u00e9.py": {1}}


def test_parse_diff_ends_a_line_at_gits_line_breaks_only() -> None:
    """A form feed in a changed line split it in two for str.splitlines: the hunk ended one line
    early, and an added line that reads like a file header then moved the next hunk to it."""
    diff = "+++ b/a.py\n@@ -0,0 +1,2 @@\n+x = 1  # \x0c\n+++ b/other.py\n@@ -9 +9 @@\n-y = 1\n+y = 2\n"
    assert mutation.parse_diff(diff) == {"a.py": {1, 2, 9}}


@needs_git
def test_changed_lines_against_a_commit(tmp_path: Path) -> None:
    env = _git_env(tmp_path)
    root = _write(tmp_path / "p", {".pytemplate/runner/a.py": "one = 1\ntwo = 2\nthree = 3\n", "README.md": "x\n"})
    _git(root, env, "init", "-q")
    _git(root, env, "add", "-A")
    _git(root, env, "commit", "-q", "-m", "one")
    _write(root, {
        ".pytemplate/runner/a.py": "one = 1\ntwo = 22\nthree = 3\nfour = 4\n", "README.md": "y\n",
        ".pytemplate/runner/new.py": "a = 1\nb = 2\n",  # untracked: every line is new
        ".pytemplate/runner/notes.txt": "not Python\n",
    })  # fmt: skip
    assert mutation.changed_lines(root, "HEAD", env) == {".pytemplate/runner/a.py": {2, 4}, ".pytemplate/runner/new.py": {1, 2}}
    with pytest.raises(PytError, match="'nope' names no commit here") as e:
        mutation.changed_lines(root, "nope", env)
    assert e.value.code == 2


@needs_git
def test_changed_lines_ignore_the_users_diff_configuration(tmp_path: Path) -> None:
    """The user's git configuration never shapes the diff: diff.interHunkContext merged the hunks
    of lines 2 and 8 and counted 3 to 7 as changed; no prefixes, colours or a textconv filter
    (it adds a line on top: every line moved by one) either."""
    env = _git_env(tmp_path)
    root = _write(tmp_path / "p", {".pytemplate/runner/a.py": "".join(f"v{i} = {i}\n" for i in range(1, 13))})
    _git(root, env, "init", "-q")
    _git(root, env, "add", "-A")
    _git(root, env, "commit", "-q", "-m", "one")
    for key, value in (("diff.interHunkContext", "10"), ("diff.noprefix", "true"), ("color.diff", "always"), ("diff.mnemonicPrefix", "true")):
        _git(root, env, "config", key, value)
    if sys.platform != "win32":
        shift = _write(tmp_path, {"shift.sh": '#!/bin/sh\necho "# a line on top"\ncat "$1"\n'}) / "shift.sh"
        shift.chmod(0o755)
        _git(root, env, "config", "diff.shift.textconv", str(shift))
        _write(root, {".git/info/attributes": "*.py diff=shift\n"})
    _write(root, {".pytemplate/runner/a.py": "".join(f"v{i} = {i * 10 if i in (2, 8) else i}\n" for i in range(1, 13))})
    assert mutation.changed_lines(root, "HEAD", env) == {".pytemplate/runner/a.py": {2, 8}}


SOURCE = '''\
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from x import Y

LIMIT: int = 3


def f(a: int | None = 1, *rest: str) -> dict[str, int] | None:
    if a is None:
        return None
    return {"a": a + 1}  # pragma: no mutate
'''


def _at(line: int, text: str, nth: int = 0) -> tuple[int, int]:
    """The position of the nth `text` on a line of SOURCE."""
    source_line = SOURCE.splitlines()[line - 1]
    column = -1
    for _ in range(nth + 1):
        column = source_line.index(text, column + 1)
    return line, column


def _entry(name: str, occurrence: int, index: int, at: tuple[int, int], length: int = 1, function: str | None = None) -> list[Any]:
    return [name, occurrence, index, at[0], at[1], at[0], at[1] + length, function]


def test_select_skips_annotations_type_checking_blocks_pragmas_and_the_minus_one() -> None:
    listed = [
        _entry("core/NumberReplacer", 0, 0, _at(8, "3")),  # kept: a value, not its annotation
        _entry("core/NumberReplacer", 1, 1, _at(8, "3")),  # NumberReplacer's -1: dropped
        _entry("core/AddNot", 0, 0, _at(5, "TYPE_CHECKING"), 13),  # `if TYPE_CHECKING:` never runs otherwise
        _entry("core/ReplaceBinaryOperator_BitOr_BitAnd", 0, 0, _at(11, "|"), 1, "f"),  # an argument's annotation
        _entry("core/NumberReplacer", 2, 0, _at(11, "1"), 1, "f"),  # kept: a default value
        _entry("core/ReplaceBinaryOperator_BitOr_BitAnd", 1, 0, _at(11, "|", 1), 1, "f"),  # the return annotation
        _entry("core/ReplaceComparisonOperator_Is_IsNot", 0, 0, _at(12, "is"), 2, "f"),  # kept
        _entry("core/ReplaceBinaryOperator_Add_Sub", 0, 0, _at(14, "+"), 1, "f"),  # its line has the pragma
    ]
    rel = ".pytemplate/runner/m.py"
    kept = mutation.select(rel, listed, SOURCE, None)
    assert [(m.operator.split("/")[-1], m.line, m.occurrence) for m in kept] == [
        ("NumberReplacer", 8, 0), ("NumberReplacer", 11, 2), ("ReplaceComparisonOperator_Is_IsNot", 12, 0),
    ]  # fmt: skip
    assert kept[2].function == "f" and kept[0].function is None and kept[2].module == "runner.m" and kept[2].where == f"{rel}:12"
    assert [m.line for m in mutation.select(rel, listed, SOURCE, {12, 30})] == [12]  # --diff: the changed lines only
    assert mutation.select(rel, listed, SOURCE, set()) == []


HANDLERS = """\
import subprocess

\u00c9RR = ValueError


def catch(error):
    try:
        raise error
    except (\u00c9RR, OSError, subprocess.TimeoutExpired):
        return "caught"
    except subprocess.CalledProcessError as e:
        return f"called {e.returncode}"
"""


def _classes(source: str) -> list[str]:
    lines = source.splitlines()
    return [lines[a[0] - 1][a[1] : b[1]] for a, b in mutation.handler_classes(source)]


def test_handler_classes_are_the_classes_an_except_clause_names() -> None:
    assert _classes(HANDLERS) == ["\u00c9RR", "OSError", "subprocess.TimeoutExpired", "subprocess.CalledProcessError"]
    assert list(mutation.handler_classes(HANDLERS).values()) == ["*()", "*()", "*()", "()"]  # in a tuple: no element left
    assert _classes("try:\n    pass\nexcept:\n    pass\nexcept (OSError):\n    pass\n") == ["OSError"]


@pytest.mark.skipif(sys.version_info < (3, 14), reason="`except A, B:` is Python 3.14 syntax (PEP 758)")
def test_a_tuple_without_parentheses_is_left_to_cosmic_ray() -> None:
    """There `*()` would read as `except*`: `except *(), B:` catches exception groups."""
    clauses = ("OSError, ValueError", "(OSError), ValueError", "(OSError), (ValueError)", "(KeyError, IndexError)")
    source = "try:\n    pass\n" + "".join(f"except {c}:\n    pass\n" for c in clauses)
    assert _classes(source) == ["KeyError", "IndexError"]


def test_an_element_in_parentheses_of_its_own_goes_with_them() -> None:
    """`((*()), B)` is no valid Python: `(( OSError ), B)` loses the whole `( OSError )`."""
    source = "try:\n    raise OSError\nexcept (\n    (( OSError )),  # the first\n    ValueError,  # the last\n):\n    pass\n"
    assert _classes(source) == ["(( OSError ))", "ValueError"]
    m = _handler_mutant(source, "(( OSError ))")
    code = mutation.own_mutant(m, source)
    assert code == source.replace("(( OSError ))", "*()") and mutation.made({"code": code}, source.encode(), "m.py")[0] == NOT_RUN
    with pytest.raises(OSError):
        exec(compile(code, "m.py", "exec"), {})


def test_select_gives_each_exception_class_one_mutant_with_its_whole_span() -> None:
    """Cosmic Ray names a part of a dotted class (the dot of `a.B`, the first name of `(a.b.C)`,
    sometimes twice): the mutant takes the class's span, once."""
    line = HANDLERS.splitlines()[8]
    at = line.index("subprocess.TimeoutExpired")
    listed = [
        _entry("core/ExceptionReplacer", 0, 0, (9, line.index("OSError")), 7, "catch"),
        _entry("core/ExceptionReplacer", 1, 0, (9, at + len("subprocess")), 1, "catch"),  # its dot
        _entry("core/ExceptionReplacer", 2, 0, (9, at), 10, "catch"),  # the same class again
    ]
    kept = mutation.select(".pytemplate/runner/m.py", listed, HANDLERS, None)
    assert [(m.line, m.column, m.end_line, m.end_column, m.occurrence) for m in kept] == [
        (9, line.index("OSError"), 9, line.index("OSError") + 7, 0), (9, at, 9, at + len("subprocess.TimeoutExpired"), 1),
    ]  # fmt: skip


def _handler_mutant(source: str, text: str) -> Mutant:
    """The ExceptionReplacer mutant of the class `text` (on an except line, if it is on several)."""
    lines = source.splitlines()
    rows = [i for i, x in enumerate(lines, 1) if text in x]
    row = next((i for i in rows if lines[i - 1].lstrip().startswith("except")), rows[0])
    column = lines[row - 1].index(text)
    return Mutant("m.py", mutation.EXCEPTION_REPLACER, 0, row, column, row, column + len(text), "catch")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_an_exception_handler_mutant_catches_nothing(newline: str) -> None:
    """The class is switched off (`()`, or `*()` in a tuple): the exception goes on as it is.
    Cosmic Ray's own mutant turned it into a NameError, which killed a mutant whose tests expect
    the exception to pass through."""
    source = HANDLERS.replace("\n", newline)

    def run(code: str, error: BaseException) -> str:
        scope: dict[str, Any] = {}
        exec(compile(code, "m.py", "exec"), scope)
        return str(scope["catch"](error))

    timeout = subprocess.TimeoutExpired("x", 1)
    assert run(source, OSError()) == run(source, timeout) == run(source, ValueError()) == "caught"
    for text, passes in (("OSError", OSError()), ("subprocess.TimeoutExpired", timeout), ("\u00c9RR", ValueError())):
        code = mutation.own_mutant(_handler_mutant(source, text), source)
        assert code is not None and code.count(newline) == source.count(newline)
        assert code.replace("*()", text, 1) == source  # the one class, and only it
        with pytest.raises(type(passes)):
            run(code, passes)
        assert {run(code, e) for e in (OSError(), timeout, ValueError()) if type(e) is not type(passes)} == {"caught"}
    one = "try:\n    raise OSError\nexcept (OSError,):\n    pass\n"
    code = mutation.own_mutant(Mutant("m.py", mutation.EXCEPTION_REPLACER, 0, 3, 8, 3, 15, None), one)
    assert code == "try:\n    raise OSError\nexcept (*(),):\n    pass\n"
    with pytest.raises(OSError):
        exec(compile(code, "m.py", "exec"), {})
    code = mutation.own_mutant(_handler_mutant(source, "subprocess.CalledProcessError"), source)
    assert code is not None and "except () as e:" in code
    with pytest.raises(subprocess.CalledProcessError):
        run(code, subprocess.CalledProcessError(3, "x"))
    other = _handler_mutant(source, "OSError")
    assert mutation.own_mutant(Mutant(**{**other.__dict__, "operator": "core/AddNot"}), source) is None  # Cosmic Ray's
    assert mutation.own_mutant(Mutant(**{**other.__dict__, "end_column": other.end_column - 1}), source) is None  # no class there


def test_skipped_spans_count_characters_not_bytes() -> None:
    """ast's columns count UTF-8 bytes, parso's (Cosmic Ray's) characters."""
    source = 'def f(x: str = "\u00e9\u00e9", y: int | None = None) -> None:\n    pass\n'  # two 2-byte characters before `int`
    spans = mutation.skipped_spans(source)
    line = source.splitlines()[0]
    assert ((1, line.index("int")), (1, line.index(" = None"))) in spans


@pytest.mark.parametrize(
    ("answer", "status", "detail"),
    [
        ({"code": "x = 2\n"}, NOT_RUN, ""),
        ({"code": "x = 'a' is 'b'\n"}, NOT_RUN, ""),  # a SyntaxWarning is no verdict, not even with -W error
        ({"error": "boom"}, mutation.ERROR, "Cosmic Ray's side failed: boom"),
        ({"code": None, "cannot": "AttributeError: no value"}, mutation.SKIPPED, "Cosmic Ray cannot make this mutant: AttributeError: no value"),
        ({"code": None}, mutation.ERROR, "Cosmic Ray made no mutant at an occurrence its list named"),
        ({"code": "x = 1\n"}, mutation.SKIPPED, "the mutant is the module unchanged"),
        ({"code": 'd = {*extra, "k": 1}\n'}, mutation.SKIPPED, "the mutant is no valid Python: "),  # Pow_Mul on `{**extra, "k": 1}`
        ({"code": "if not x := f():\n    pass\n"}, mutation.SKIPPED, "the mutant is no valid Python: "),  # AddNot before a walrus
        ({"code": "x = 1\0\n"}, mutation.SKIPPED, "the mutant is no valid Python: "),
    ],
)
def test_made(answer: dict[str, Any], status: str, detail: str) -> None:
    """A mutant that is no valid Python never runs: pytest would stop at the import, which reads
    as a kill."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        got = mutation.made(answer, b"x = 1\n", "m.py")
    assert got[0] == status and got[1].startswith(detail) and (detail != "" or got[1] == "")
    assert got[2] == (answer.get("code") if status == NOT_RUN or detail.startswith("the mutant is no valid") else None)


def test_made_leaves_the_warning_filters_of_every_thread_as_they_were() -> None:
    """made() runs in the worker threads, and warnings.catch_warnings is not thread safe: two of
    them that overlap put back each other's filters, and an "ignore" stayed for good."""
    import warnings

    before = list(warnings.filters)
    code = "x = 'a' is 'b'\n" * 300  # a SyntaxWarning each, and time to overlap

    def work() -> None:
        for _ in range(40):
            assert mutation.made({"code": code}, b"", "m.py")[0] == NOT_RUN

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert warnings.filters == before


def test_mutant_diff() -> None:
    assert mutation.mutant_diff("a\nb\r\nc\n", "a\nB\r\nc\n") == ["-b", "+B"]
    assert mutation.mutant_diff("x\n", "x\n") == []


# --- what a test run means ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "output", "status", "detail"),
    [
        (0, "....\n4 passed in 0.12s\n", SURVIVED, ""),
        (0, "= 2 passed, 1 skipped, 1 warning in 0.50s (0:00:00) =\n", SURVIVED, ""),
        (1, ".F\nFAILED t.py::test_x - assert 1 == 2\n!!! stopping after 1 failures !!!\n1 failed, 1 passed in 1.00s\n", KILLED, "FAILED t.py::test_x - assert 1 == 2"),
        (2, "ERROR t.py - NameError: x\n!!! Interrupted: 1 error during collection !!!\n1 error in 0.20s\n", KILLED, "ERROR t.py - NameError: x"),
        (0, "..\n", mutation.ERROR, "exit code 0 without pytest's summary line: .."),  # os._exit(0) in a test
        (5, "no tests ran in 0.01s\n", mutation.ERROR, "exit code 5 without pytest's summary line: no tests ran in 0.01s"),
        (1, "3 passed in 1.00s\n", mutation.ERROR, "exit code 1: 3 passed in 1.00s"),
        (3, "INTERNALERROR> boom\n1 passed in 0.10s\n", mutation.ERROR, "exit code 3: 1 passed in 0.10s"),
        (-9, "", mutation.ERROR, "exit code 137 without pytest's summary line: no output"),
        # FORCE_COLOR or PY_COLORS colour pytest's lines: read through
        (1, "\x1b[31mFAILED\x1b[0m t.py::test_x\n\x1b[31m1 failed\x1b[0m, \x1b[32m1 passed\x1b[0m\x1b[31m in 1.00s\x1b[0m\n", KILLED, "FAILED t.py::test_x"),
        (0, "\x1b[32m\x1b[1m4 passed\x1b[0m\x1b[32m in 0.12s\x1b[0m\n", SURVIVED, ""),
        (1, "2 passed, 1 subtests failed, 3 subtests passed in 1.00s\n", KILLED, "2 passed, 1 subtests failed, 3 subtests passed in 1.00s"),
        (0, "3 passed, 1 xfailed, 1 xpassed in 1.00s\n", SURVIVED, ""),  # an expected failure is none
        (1, "ERROR t.py::test_x - OSError\n1 passed, 2 errors in 1.00s\n", KILLED, "ERROR t.py::test_x - OSError"),
    ],
)
def test_classify(code: int, output: str, status: str, detail: str) -> None:
    assert mutation.classify(code, output) == (status, detail)


def test_classify_a_run_that_was_ended() -> None:
    assert mutation.classify(None, "...") == (TIMEOUT, "")
    assert mutation.classify(None, "...", stopped=True) == (NOT_RUN, "interrupted")
    # a Ctrl+C reached pytest too (Windows: one console): its own end proves nothing either way
    stopped_by_ctrl_c = "!!! KeyboardInterrupt !!!\n3 passed in 1.00s\n"
    assert mutation.classify(2, stopped_by_ctrl_c, stopped=True) == (NOT_RUN, "interrupted")
    assert mutation.classify(1, "FAILED t.py::test_x\n1 failed in 1.00s\n", stopped=True) == (NOT_RUN, "interrupted")


def test_pytest_counts() -> None:
    assert mutation.pytest_counts("x\n1 failed, 2 errors, 3 passed, 1 warning in 2.00s (0:00:02)\n") == {"failed": 1, "error": 2, "passed": 3, "warning": 1}
    assert mutation.pytest_counts("1 passed, 2 subtests passed in 0.1s\n") == {"passed": 1, "subtests passed": 2}
    assert mutation.pytest_counts("12 passed in 3s\nlater noise\n") == {"passed": 12}
    assert mutation.pytest_counts("collected 3 items\n") is None


# --- Cosmic Ray's side, faked -----------------------------------------------------------------------

FAKE_DRIVER = """\
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request["op"] == "version":
        print(json.dumps({"version": "9.9.9"}), flush=True)
    elif request["op"] == "garbage":
        print("not json", flush=True)
    elif request["op"] == "die":
        sys.stderr.write("fake driver: stopping\\n")
        sys.exit(7)
    else:
        print(json.dumps({"echo": request}), flush=True)
"""


def _driver(tmp_path: Path) -> mutation.Driver:
    script = tmp_path / "fake_driver.py"
    script.write_text(FAKE_DRIVER, encoding="utf-8")
    return mutation.Driver("uv", _cfg(), tmp_path / "driver.log", argv=[sys.executable, str(script)])


def _cfg() -> Config:
    cfg: Config = config._build(Config, {}, "")
    config.validate(cfg)
    return cfg


def test_driver_answers_and_stops(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    try:
        assert driver.ask({"op": "version"}) == {"version": "9.9.9"}
        assert driver.ask({"op": "list", "path": "p"}) == {"echo": {"op": "list", "path": "p"}}
        with pytest.raises(PytError, match="answered 'not json'") as e:
            driver.ask({"op": "garbage"})
        assert e.value.code == 3
        with pytest.raises(PytError, match=r"stopped \(exit code 7\): fake driver: stopping") as e:
            driver.ask({"op": "die"})
        assert e.value.code == 3
    finally:
        driver.close()


def test_driver_that_cannot_start(tmp_path: Path) -> None:
    with pytest.raises(PytError, match="cannot start uv") as e:
        mutation.Driver("uv", _cfg(), tmp_path / "driver.log", argv=[str(tmp_path / "missing")])
    assert e.value.code == 3


def test_list_mutants_turns_a_bad_answer_into_an_error(tmp_path: Path) -> None:
    class Fake:
        def __init__(self, answer: dict[str, Any]) -> None:
            self.answer = answer

        def ask(self, request: dict[str, Any]) -> dict[str, Any]:
            return self.answer

    _write(tmp_path, {"m.py": "x = 1\n", "bad.py": "def (\n"})
    snap = tmp_path / "snap"
    for answer in ({"error": "boom"}, {"mutants": "no list"}):
        with pytest.raises(PytError, match="could not list the mutants of m.py"):
            mutation.list_mutants(Fake(answer), tmp_path, ["m.py"], None, snap)  # type: ignore[arg-type]
    with pytest.raises(PytError, match="bad.py is no Python the runner can read"):
        mutation.list_mutants(Fake({"mutants": []}), tmp_path, ["bad.py"], None, snap)  # type: ignore[arg-type]
    fake = Fake({"mutants": [["core/NumberReplacer", 0, 0, 1, 4, 1, 5, None]]})
    mutants, originals = mutation.list_mutants(fake, tmp_path, ["m.py"], None, snap)  # type: ignore[arg-type]
    assert [(m.file, m.line, m.column) for m in mutants] == [("m.py", 1, 4)] and originals == {"m.py": b"x = 1\n"}


@needs_git
def test_mutants_come_from_a_snapshot_of_the_modules(tmp_path: Path) -> None:
    """Cosmic Ray lists (and later mutates) a copy taken at the start: the runner may be edited
    while the run goes on, and the workers' copies hold the same bytes."""
    asked: list[str] = []

    class Fake:
        def ask(self, request: dict[str, Any]) -> dict[str, Any]:
            asked.append(request["path"])
            return {"mutants": []}

    _write(tmp_path / "p", {"sub/m.py": "x = 1\n"})
    _, originals = mutation.list_mutants(Fake(), tmp_path / "p", ["sub/m.py"], None, tmp_path / "snap")  # type: ignore[arg-type]
    _write(tmp_path / "p", {"sub/m.py": "x = 2  # edited meanwhile\n"})
    assert asked == [str(tmp_path / "snap" / "sub" / "m.py")] and (tmp_path / "snap" / "sub" / "m.py").read_bytes() == b"x = 1\n"
    mutation.make_copy(tmp_path / "p", tmp_path / "copy", ["sub/m.py"], _git_env(tmp_path), originals)
    assert (tmp_path / "copy" / "sub" / "m.py").read_bytes() == b"x = 1\n"


# --- the scratch base, the workers' environment and files -------------------------------------------


def test_prepare_base(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(PytError, match="cannot be inside the project"):
        mutation.prepare_base(root / "scratch", root)
    busy = _write(tmp_path / "busy", {"mine.txt": "x"})
    with pytest.raises(PytError, match=f"was not made by selftest --mutation \\(no {mutation.MARKER}\\)"):
        mutation.prepare_base(busy, root)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(PytError, match="is not a directory"):
        mutation.prepare_base(tmp_path / "file", root)
    base = tmp_path / "base"
    mutation.prepare_base(base, root)
    mutation.prepare_base(base, root)  # its own marker: reused
    assert (base / mutation.MARKER).is_file()


def test_one_run_at_a_time_per_base(tmp_path: Path) -> None:
    with mutation.base_lock(tmp_path):
        with pytest.raises(PytError, match="another run is using"):
            with mutation.base_lock(tmp_path):
                pass
    with mutation.base_lock(tmp_path):  # released
        pass


def test_worker_env_moves_home_and_temp_but_keeps_uv(tmp_path: Path) -> None:
    base_env = {
        "PATH": os.pathsep.join(["/usr/bin", "/bin"]), "HOME": "/home/me", "XDG_DATA_HOME": "/home/me/.data", "KEEP": "1",
        "PYTEST_ADDOPTS": "-n auto --lf", "PYTEST_PLUGINS": "mine",  # they would change what every run means
    }  # fmt: skip
    keep = {"UV_CACHE_DIR": "/home/me/.cache/uv", "UV_PYTHON_INSTALL_DIR": "/home/me/.local/share/uv/python"}
    copy, home, tmp = tmp_path / "w0", tmp_path / "h0", tmp_path / "t0"
    env = mutation.worker_env(copy, home, tmp, base_env, keep, _cfg(), "/opt/uv")
    assert env["HOME"] == str(home) and not any(k.startswith(("XDG_", "PYTEST_")) for k in env) and env["KEEP"] == "1"
    assert env["UV_CACHE_DIR"] == keep["UV_CACHE_DIR"] and env["UV_PYTHON_INSTALL_DIR"] == keep["UV_PYTHON_INSTALL_DIR"]
    assert (env["TEMP"] if os.name == "nt" else env["TMPDIR"]) == str(tmp)
    assert env["PATH"].split(os.pathsep)[0] == str(mutation.venv_python(copy / ".venv").parent)
    assert env["VIRTUAL_ENV"] == env["UV_PROJECT_ENVIRONMENT"] == str(copy / ".venv") and env["UV"] == "/opt/uv"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1" and env["UV_PYTHON"] == _cfg().python.cpython


def test_every_write_of_a_module_gets_a_time_of_its_own(tmp_path: Path) -> None:
    """A .pyc records the whole second and the size of its source: two versions of the same size
    written in one second would share it."""
    path = tmp_path / "m.py"
    times = []
    for data in (b"x = 1\n", b"x = 2\n", b"x = 1\n"):
        mutation.write_module(path, data)
        times.append(int(path.stat().st_mtime))
    assert times[0] < times[1] < times[2] and path.read_bytes() == b"x = 1\n"


def test_a_modules_time_comes_after_every_write_of_the_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The copy of the project is written by the clock: a mutant of the same size written in that
    very second would pass for it, and a .pyc of the module then for the mutant's."""
    later = time.time() + 1000  # a clock ahead of every time handed out so far
    monkeypatch.setattr(mutation, "time", SimpleNamespace(time=lambda: later))
    path = tmp_path / "m.py"
    path.write_bytes(b"x = 1\n")
    os.utime(path, (later, later))  # written by that clock
    mutation.write_module(path, b"x = 2\n")
    assert int(path.stat().st_mtime) > int(later)


@needs_git
def test_make_copy_is_a_repository_of_the_listed_files(tmp_path: Path) -> None:
    env = _git_env(tmp_path)
    root = _write(tmp_path / "p", {"a.py": "x\n", "sub/b.txt": "y\n", ".gitignore": "ignored/\n", "ignored/c.txt": "z\n"})
    _git(root, env, "init", "-q")
    _git(root, env, "add", "a.py", ".gitignore")
    _write(root, {"a.py": "changed\n"})  # the working tree's content, not the commit's
    files = mutation.listed_files(root, env)
    assert files == [".gitignore", "a.py", "sub/b.txt"]  # untracked but not ignored counts; ignored does not
    copy = tmp_path / "copy"
    mutation.make_copy(root, copy, [*files, "deleted.py"], env)
    assert (copy / "a.py").read_text(encoding="utf-8") == "changed\n" and not (copy / "ignored").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=copy, env=env, capture_output=True, text=True, check=True).stdout
    assert status == ""  # every file committed


# --- running tests: time limits, a stop, the threads --------------------------------------------------


def _worker(tmp_path: Path, tests: dict[str, str]) -> Worker:
    copy = _write(tmp_path / "copy", tests)
    (tmp_path / "pytest").mkdir()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return Worker(0, copy, Path(sys.executable), tmp_path / "pytest", tmp_path / "w0.log", env)


def test_run_reports_pytests_result(tmp_path: Path) -> None:
    worker = _worker(tmp_path, {"test_ok.py": "def test_ok():\n    assert True\n", "test_bad.py": "def test_bad():\n    assert 1 == 2\n"})
    runs = Runs()
    code, output, seconds = runs.run(worker, ["test_ok.py"], 120, junit=tmp_path / "junit.xml")
    assert mutation.classify(code, output) == (SURVIVED, "") and seconds > 0
    assert mutation.junit_seconds(tmp_path / "junit.xml", ["test_ok.py"]).keys() == {"test_ok.py"}
    code, output, _ = runs.run(worker, ["test_ok.py", "test_bad.py"], 120)
    assert mutation.classify(code, output)[0] == KILLED


def test_run_reads_pytest_whatever_colours_the_user_asked_for(tmp_path: Path) -> None:
    worker = _worker(tmp_path, {"test_ok.py": "def test_ok():\n    assert True\n", "test_bad.py": "def test_bad():\n    assert 1 == 2\n"})
    worker.env.update(FORCE_COLOR="1", PY_COLORS="1")
    runs = Runs()
    assert mutation.classify(*runs.run(worker, ["test_ok.py"], 120)[:2]) == (SURVIVED, "")
    status, detail = mutation.classify(*runs.run(worker, ["test_bad.py"], 120)[:2])
    assert status == KILLED and detail.startswith("FAILED test_bad.py::test_bad")


def test_run_ends_a_run_over_its_time_and_on_stop(tmp_path: Path) -> None:
    worker = _worker(tmp_path, {"test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(120)\n"})
    runs = Runs()
    start = time.monotonic()
    code, _, _ = runs.run(worker, ["test_slow.py"], 3)
    assert code is None and time.monotonic() - start < 60
    threading.Timer(3, runs.stop).start()
    start = time.monotonic()
    code, output, _ = runs.run(worker, ["test_slow.py"], 600)
    assert code is None and time.monotonic() - start < 60
    assert mutation.classify(code, output, stopped=runs.stopped.is_set()) == (NOT_RUN, "interrupted")
    assert runs.run(worker, ["test_slow.py"], 600)[0] is None  # stopped: nothing starts any more


posix = pytest.mark.skipif(sys.platform == "win32", reason="sessions and signals of POSIX")

TREE = """\
import os, subprocess, sys, time
folder, depth = sys.argv[1], int(sys.argv[2])
if depth:
    subprocess.Popen([sys.executable, sys.argv[0], folder, str(depth - 1)], start_new_session=True)
with open(os.path.join(folder, f"pid{depth}"), "w") as f:
    f.write(str(os.getpid()))
time.sleep(300)
"""


def _running(pid: int) -> bool:
    """Whether `pid` still runs (a zombie that init has not reaped yet does not)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")[2].split()[0] != b"Z"
        except (OSError, IndexError):
            return False
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _stopped(pids: list[int]) -> bool:
    deadline = time.monotonic() + 15
    while any(_running(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not any(_running(p) for p in pids)


@pytest.fixture
def tree(tmp_path: Path) -> Any:
    """A process (in a session of its own, as Runs starts pytest) whose child and grandchild each
    start a session of their own: (the process, [the child's pid, the grandchild's])."""
    script = tmp_path / "tree.py"
    script.write_text(TREE, encoding="utf-8")
    child = subprocess.Popen([sys.executable, str(script), str(tmp_path), "2"], stdin=subprocess.DEVNULL, start_new_session=True)
    pids: list[int] = []
    try:
        deadline = time.monotonic() + 60
        while not (tmp_path / "pid0").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)  # the file is written
        pids = [int((tmp_path / f"pid{d}").read_text(encoding="utf-8")) for d in (1, 0)]
        yield child, pids
    finally:
        for pid in [child.pid, *pids]:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        child.wait()


@posix
def test_descendants_cross_sessions(tree: Any) -> None:
    child, pids = tree
    assert set(pids) <= set(mutation.descendants(child.pid))
    assert child.pid not in mutation.descendants(child.pid)


@posix
@pytest.mark.skipif(shutil.which("ps") is None, reason="ps is not installed")
def test_descendants_through_ps(tree: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Where there is no /proc (macOS, the BSDs), ps answers."""
    child, pids = tree
    monkeypatch.setattr(mutation.sys, "platform", "darwin")
    found = mutation.descendants(child.pid)
    monkeypatch.undo()
    assert set(pids) <= set(found)


@posix
def test_kill_run_ends_the_sessions_below_pytest_too(tree: Any) -> None:
    """A test may start a process in a session of its own: a signal to pytest's group never
    reaches it, and once pytest is dead init owns it. kill_run finds it first."""
    child, pids = tree
    mutation.kill_run(child)
    assert child.poll() is not None and _stopped(pids)


@posix
def test_a_run_over_its_time_leaves_no_process_of_its_tests_behind(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    hang = (
        "import subprocess, sys, time\n\n"
        "def test_hangs():\n"
        "    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], start_new_session=True)\n"
        f"    open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "    time.sleep(300)\n"
    )
    worker = _worker(tmp_path, {"test_hang.py": hang})
    runs = Runs()

    def stop_once_started() -> None:
        deadline = time.monotonic() + 120
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        runs.stop()

    threading.Thread(target=stop_once_started, daemon=True).start()
    code, _, _ = runs.run(worker, ["test_hang.py"], 600)
    assert code is None and _stopped([int(pidfile.read_text(encoding="utf-8"))])


def _workers(n: int, tmp_path: Path) -> list[Worker]:
    return [Worker(i, tmp_path, Path(sys.executable), tmp_path, tmp_path / f"w{i}.log", {}) for i in range(n)]


def test_run_all_gives_every_item_to_one_worker(tmp_path: Path) -> None:
    seen: list[tuple[int, int]] = []

    def handle(worker: Worker, item: int) -> None:
        time.sleep(0.01)
        seen.append((worker.index, item))

    mutation.run_all(_workers(3, tmp_path), list(range(20)), handle, Runs())
    assert sorted(item for _, item in seen) == list(range(20)) and len({w for w, _ in seen}) > 1


def test_run_all_stops_at_the_first_failure(tmp_path: Path) -> None:
    runs = Runs()

    def handle(worker: Worker, item: int) -> None:
        if item == 3:
            raise ValueError("boom")
        time.sleep(0.05)

    with pytest.raises(ValueError, match="boom"):
        mutation.run_all(_workers(2, tmp_path), list(range(100)), handle, runs)
    assert runs.stopped.is_set()


def test_run_all_goes_on_as_an_interrupt(tmp_path: Path) -> None:
    """Ctrl+C reaches the main thread only: the runs stop, the workers end, the interrupt goes on."""
    runs = Runs()
    done: list[int] = []

    def handle(worker: Worker, item: int) -> None:
        if item == 0:
            _thread.interrupt_main()
        time.sleep(0.2)
        done.append(item)

    with pytest.raises(KeyboardInterrupt):
        mutation.run_all(_workers(2, tmp_path), list(range(50)), handle, runs)
    assert runs.stopped.is_set() and len(done) < 50


# --- the report and the exit code -------------------------------------------------------------------


def _report(tmp_path: Path, statuses: list[str], baseline: str = mutation.PASS) -> Report:
    mutants = [Mutant(".pytemplate/runner/m.py", "core/AddNot", i, 10 + i, 4, 10 + i, 9, "f", s, 1.0) for i, s in enumerate(statuses)]
    for m in mutants:
        m.diff = [f"-    if x{m.line}:", f"+    if not x{m.line}:"]
    report = Report(mutants, {"runner.m": Baseline("runner.m", ["t.py"], baseline, 2.0)}, tmp_path, seconds=75.0, cosmic_ray="8.7.0", workers=2)
    return report


def test_report_counts_and_score(tmp_path: Path) -> None:
    report = _report(tmp_path, [KILLED, KILLED, TIMEOUT, SURVIVED, mutation.UNTESTED, mutation.SKIPPED])
    assert report.counts()[KILLED] == 2 and report.counts()[SURVIVED] == 1 and report.counts()[NOT_RUN] == 0
    assert report.score() == 0.75 and not report.failed()
    assert _report(tmp_path, [mutation.UNTESTED]).score() is None
    assert _report(tmp_path, [KILLED, mutation.ERROR]).failed()
    assert _report(tmp_path, [NOT_RUN], baseline=mutation.FAIL).failed()
    data = report.as_json(Options("origin/main", 2, True))
    assert data["ok"] is True and data["score"] == 0.75 and data["options"] == {"diff": "origin/main", "jobs": 2}
    assert data["mutants"][3]["status"] == SURVIVED and data["mutants"][3]["module"] == "runner.m" and data["mutants"][3]["line"] == 13
    json.dumps(data)


def test_print_report_shows_the_survivors_with_their_change(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mutation.print_report(_report(tmp_path, [KILLED, SURVIVED, SURVIVED]))
    err = capsys.readouterr().err
    assert "score: 33.3% (1 of 3 judged mutants killed)" in err and "survived (2):" in err
    assert ".pytemplate/runner/m.py:11 in f: AddNot" in err and "      +    if not x12:" in err
    mutation.print_report(Report([], {}, tmp_path, note="no runner line changed since origin/main"))
    assert "no mutant to test: no runner line changed since origin/main" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("statuses", "baseline", "interrupted", "code"),
    [
        ([KILLED, SURVIVED], mutation.PASS, False, 0),  # survivors are the report, not a failure
        ([KILLED, mutation.ERROR], mutation.PASS, False, 1),
        ([NOT_RUN], mutation.FAIL, False, 1),
        ([KILLED, NOT_RUN], mutation.PASS, True, 130),
    ],
)
def test_selftest_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], statuses: list[str], baseline: str, interrupted: bool, code: int
) -> None:
    report = _report(tmp_path, statuses, baseline)
    report.interrupted = interrupted
    monkeypatch.setattr(mutation, "run", lambda *a: report)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    assert mutation.selftest(_cfg(), ["--json"]) == code
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is (code == 0) and data["interrupted"] is interrupted


def test_selftest_prints_the_report_before_the_error_that_stopped_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A worker that could not be made (its uv sync failed: a PytError) stopped the run after 50
    mutants: their report comes first, then the error with its own exit code."""
    report = _report(tmp_path, [KILLED, SURVIVED])
    report.error = PytError("uv sync failed in the copy", 3)
    monkeypatch.setattr(mutation, "run", lambda *a: report)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    with pytest.raises(PytError, match="uv sync failed") as e:
        mutation.selftest(_cfg(), ["--json"])
    assert e.value.code == 3
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert data["ok"] is False and data["error"] == "PytError: uv sync failed in the copy" and "survived (1):" in err


def _toy(tmp_path: Path) -> Path:
    return _write(tmp_path / "toy", {".pytemplate/runner/__init__.py": "", ".pytemplate/runner/calc.py": "x = 1\n", ".pytemplate/tests/test_calc.py": ""})


def test_a_run_that_fails_keeps_its_report_and_logs_and_removes_the_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "base"

    def fails(cfg: Config, opts: Options, uv: str, root: Path, base: Path, files: Any, changed: Any, tests: Any, report: Report) -> None:
        (base / "logs").mkdir()
        (base / "w0" / ".venv").mkdir(parents=True)
        report.mutants = [Mutant(".pytemplate/runner/calc.py", "core/AddNot", 0, 1, 4, 1, 5, None, KILLED)]
        raise PytError("uv sync failed in the copy", 3)

    monkeypatch.setattr(mutation, "_run_locked", fails)
    report = mutation.run(_cfg(), Options(None, 1, False), "uv", _toy(tmp_path), base)
    assert isinstance(report.error, PytError) and report.failed() and report.kept and not report.interrupted
    assert len(report.mutants) == 1 and sorted(p.name for p in base.iterdir()) == sorted([mutation.MARKER, "lock", "logs"])


def test_an_interrupt_during_the_cleanup_waits_for_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second Ctrl+C (or a SIGTERM) while the workers' folders go: they still go, the report
    comes back and says interrupted, and the logs stay."""
    import signal

    base = tmp_path / "base"
    removed: list[bool] = []

    def passes(cfg: Config, opts: Options, uv: str, root: Path, base: Path, files: Any, changed: Any, tests: Any, report: Report) -> None:
        (base / "logs").mkdir()

    def remove_workers(base: Path) -> None:
        signal.raise_signal(signal.SIGINT)
        removed.append(True)

    monkeypatch.setattr(mutation, "_run_locked", passes)
    monkeypatch.setattr(mutation, "_remove_workers", remove_workers)
    report = mutation.run(_cfg(), Options(None, 1, False), "uv", _toy(tmp_path), base)
    assert removed == [True] and report.interrupted and report.kept and (base / "logs").is_dir()


@pytest.mark.parametrize("name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_deferred_interrupts_wait_for_the_block_and_put_the_handlers_back(name: str) -> None:
    import signal

    number = getattr(signal, name, None)
    if number is None:
        pytest.skip(f"no {name} here")
    before = signal.getsignal(number)
    with mutation.deferred_interrupts() as got:
        signal.raise_signal(number)
        time.sleep(0.01)  # a signal's Python handler runs between two bytecodes
    assert got == [number] and signal.getsignal(number) == before


def test_nothing_changed_starts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutation, "changed_lines", lambda *a: {".pytemplate/runner/not_there.py": {1}})
    monkeypatch.setattr(mutation, "Driver", None)  # never started
    base = tmp_path / "base"
    report = mutation.run(_cfg(), Options("HEAD", 1, False), "uv", ROOT, base)
    assert report.mutants == [] and report.note == "no runner line changed since HEAD" and not base.exists()


# --- a real run: Cosmic Ray, the workers and pytest, on a toy project ---------------------------------

SAMPLE = '''\
def f(a, b, items):
    if a == b or a != b and a is None or a is not b:
        pass
    if a < b and a <= b and a > b and a >= b:
        pass
    x = a + b - a * b // 2 % 3 ** 2 << 1 >> 1 & 1 | 2 ^ 3
    z, w, t, u = -a, not a, True, False
    for i in items:
        if i:
            break
        continue
    try:
        pass
    except ValueError:
        pass
    return x, z, w, t, u
'''


@pytest.fixture(scope="module")
def cosmic_ray(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Cosmic Ray's side for real (skipped when its environment cannot be made here)."""
    try:
        uv = proc.find_uv()
    except PytError:
        pytest.skip("uv not found")
    driver = mutation.Driver(uv, config.load(set()), tmp_path_factory.mktemp("cr") / "driver.log")
    try:
        driver.ask({"op": "version"})
    except PytError as e:
        driver.close()
        pytest.skip(f"Cosmic Ray's environment is not available here (offline, not in uv's cache?): {e}")
    yield driver
    driver.close()


def test_every_operator_is_one_of_cosmic_rays_and_makes_its_mutant(cosmic_ray: Any, tmp_path: Path) -> None:
    """mutation.OPERATORS names Cosmic Ray's operators: a release that renames one fails here, and
    each one makes a mutant of a module that has what it changes."""
    sample = _write(tmp_path, {"sample.py": SAMPLE}) / "sample.py"
    answer = cosmic_ray.ask({"op": "list", "path": str(sample), "operators": list(mutation.OPERATORS)})
    assert "error" not in answer, answer
    assert {item[0] for item in answer["mutants"]} == set(mutation.OPERATORS)
    before = SAMPLE.splitlines()
    for name, occurrence, index, line, column, end_line, end_column, _ in answer["mutants"]:
        code = cosmic_ray.ask({"op": "mutate", "path": str(sample), "operator": name, "occurrence": occurrence}).get("code")
        assert isinstance(code, str) and code != SAMPLE, (name, occurrence)
        # the mutant made for an occurrence is the one listed for it: its first change is on the
        # listed line, in the listed span (`is` -> `is not` changes the character right after
        # it). Another order would move the mutants --diff keeps, and the lines the report names.
        after = code.splitlines()
        row = next(i for i, (x, y) in enumerate(zip(before, after, strict=False)) if x != y) + 1
        old, new = before[row - 1], after[row - 1]
        first = next((i for i, (x, y) in enumerate(zip(old, new, strict=False)) if x != y), min(len(old), len(new)))
        assert (row, end_line) == (line, line) and column <= first <= end_column + 1, (name, occurrence, line, column, end_column, new)
        if name == "core/NumberReplacer":  # select keeps index 0: the +1
            number = new[column : end_column + len(new) - len(old)]
            assert int(number) == int(old[column:end_column]) + (1 if index == 0 else -1), (occurrence, index, new)
    assert cosmic_ray.ask({"op": "mutate", "path": str(sample), "operator": "core/AddNot", "occurrence": 99}) == {"code": None}
    assert "error" in cosmic_ray.ask({"op": "list", "path": str(sample), "operators": ["core/NoSuchOperator"]})


PASS_THROUGH = """\
import subprocess


def f():
    try:
        raise KeyError("k")
    except OSError:
        return "caught"


def g():
    try:
        raise KeyError("k")
    except (OSError, subprocess.TimeoutExpired):
        return "caught"


def h(extra):
    return {**extra, "k": 1}
"""


def test_cosmic_rays_defects_that_the_runner_works_around(cosmic_ray: Any, tmp_path: Path) -> None:
    """Pins of Cosmic Ray 8.7.0's defects (CLAUDE.md 15.1): its ExceptionReplacer turns an exception
    that goes through the handler into a NameError, and fails on a dotted class in a tuple (the
    runner makes those mutants itself: own_mutant); its Pow_Mul makes invalid Python of a dict
    display (made skips it). A pin that fails means Cosmic Ray changed: see whether its
    workaround can go."""
    sample = _write(tmp_path, {"sample.py": PASS_THROUGH}) / "sample.py"
    names = [mutation.EXCEPTION_REPLACER, "core/ReplaceBinaryOperator_Pow_Mul"]
    listed = cosmic_ray.ask({"op": "list", "path": str(sample), "operators": names})["mutants"]

    def call(code: str, name: str) -> Any:
        scope: dict[str, Any] = {}
        exec(compile(code, "sample.py", "exec"), scope)
        return scope[name]()

    theirs = cosmic_ray.ask({"op": "mutate", "path": str(sample), "operator": mutation.EXCEPTION_REPLACER, "occurrence": 0})
    with pytest.raises(NameError):  # f's KeyError was to go through
        call(theirs["code"], "f")
    dotted = cosmic_ray.ask({"op": "mutate", "path": str(sample), "operator": mutation.EXCEPTION_REPLACER, "occurrence": 2})
    assert dotted["code"] is None and dotted["cannot"].startswith("AttributeError"), dotted
    kept = mutation.select("sample.py", listed, PASS_THROUGH, None)
    handlers = [m for m in kept if m.operator == mutation.EXCEPTION_REPLACER]
    assert [(m.function, PASS_THROUGH.splitlines()[m.line - 1][m.column : m.end_column]) for m in handlers] == [
        ("f", "OSError"), ("g", "OSError"), ("g", "subprocess.TimeoutExpired"),
    ]  # fmt: skip
    for m in handlers:
        ours = mutation.own_mutant(m, PASS_THROUGH)
        assert ours is not None and mutation.made({"code": ours}, PASS_THROUGH.encode(), "sample.py")[0] == NOT_RUN
        with pytest.raises(KeyError):
            call(ours, str(m.function))
    power = next(m for m in kept if m.operator == "core/ReplaceBinaryOperator_Pow_Mul")
    answer = cosmic_ray.ask({"op": "mutate", "path": str(sample), "operator": power.operator, "occurrence": power.occurrence})
    status, detail, _ = mutation.made(answer, PASS_THROUGH.encode(), "sample.py")
    assert (status, detail.split(":")[0]) == (mutation.SKIPPED, "the mutant is no valid Python"), answer


CALC = '''\
"""A toy runner module: its tests leave two boundaries and a handler untested."""


def clamp(x: int, low: int, high: int) -> int:
    if x < low:
        return low
    if x > high:
        return high
    return x


def total(items: list[int]) -> int:
    result = 0
    for item in items:
        result += item
    return result


def parse(text: str | None) -> int:
    try:
        return int(text)  # None: a TypeError, which the handler lets through
    except ValueError:
        return 0
'''
TEST_CALC = '''\
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import calc  # noqa: E402


def test_clamp() -> None:
    assert calc.clamp(5, 0, 10) == 5
    assert calc.clamp(-1, 0, 10) == 0
    assert calc.clamp(11, 0, 10) == 10


def test_total() -> None:
    assert calc.total([1, 2]) == 3


def test_parse_lets_a_type_error_through() -> None:
    with pytest.raises(TypeError):
        calc.parse(None)
'''


@needs_git
def test_a_real_run_kills_what_the_tests_check_and_finds_what_they_miss(tmp_path: Path) -> None:
    try:
        uv = proc.find_uv()
    except PytError:
        pytest.skip("uv not found")
    cfg = config.load(set())
    probe = mutation.Driver(uv, cfg, tmp_path / "probe.log")
    try:
        probe.ask({"op": "version"})
    except PytError as e:
        pytest.skip(f"Cosmic Ray's environment is not available here (offline, not in uv's cache?): {e}")
    finally:
        probe.close()
    env = _git_env(tmp_path)
    toy = _write(tmp_path / "toy", {".pytemplate/runner/__init__.py": "", ".pytemplate/runner/calc.py": CALC, ".pytemplate/tests/test_calc.py": TEST_CALC})
    for name in ("pyproject.toml", "uv.lock", ".python-version", ".gitignore"):
        shutil.copyfile(ROOT / name, toy / name)  # uv sync --locked of the workers: the project's own lock
    _git(toy, env, "init", "-q")
    _git(toy, env, "add", "-A")
    _git(toy, env, "commit", "-q", "-m", "toy")
    base = tmp_path / "base"
    report = mutation.run(cfg, Options(None, 2, False), uv, toy, base)
    got = sorted((m.line, m.operator.split("/")[-1], m.status) for m in report.mutants)
    assert got == [
        (5, "AddNot", KILLED),
        (5, "ReplaceComparisonOperator_Lt_LtE", SURVIVED),  # clamp(0, 0, 10) is never asked
        (7, "AddNot", KILLED),
        (7, "ReplaceComparisonOperator_Gt_GtE", SURVIVED),  # nor clamp(10, 0, 10)
        (13, "NumberReplacer", KILLED),
        (14, "ZeroIterationForLoop", KILLED),
        # no test asks for a ValueError: switched off, the handler is missed by no test. Cosmic Ray's
        # own mutant turned the TypeError into a NameError, and test_parse... killed it
        (22, "ExceptionReplacer", SURVIVED),
        (23, "NumberReplacer", SURVIVED),
    ], [(m.where, m.operator, m.status, m.detail) for m in report.mutants]
    assert report.baselines["runner.calc"].status == mutation.PASS and report.workers == 2 and report.cosmic_ray
    assert (toy / ".pytemplate/runner/calc.py").read_text(encoding="utf-8") == CALC  # the project itself is never touched
    survivor = next(m for m in report.mutants if m.status == SURVIVED)
    assert survivor.diff == ["-    if x < low:", "+    if x <= low:"] and survivor.function == "clamp"
    killer = next(m for m in report.mutants if m.status == KILLED)
    assert killer.detail.startswith("FAILED .pytemplate/tests/test_calc.py::")
    handler = next(m for m in report.mutants if m.operator == mutation.EXCEPTION_REPLACER)
    assert handler.diff == ["-    except ValueError:", "+    except ():"]
    assert not report.kept and sorted(p.name for p in base.iterdir()) == sorted([mutation.MARKER, "lock"])  # nothing to read
