"""Tests for runner/mutation.py: `./pyt selftest --mutation [--diff BASE] [--jobs N] [--json]`.

The pure parts (options, which tests a module gets and in what order, the lines a diff changed,
where no mutant is made, what a test run's output means, the report) on made-up inputs; the
Cosmic Ray side through a fake driver; the workers' processes (time limits, a stop, the threads)
with real pytest runs of tiny test files; and one real run, Cosmic Ray included, on a toy project
of one module (skipped when Cosmic Ray's environment cannot be made: offline, not in uv's cache).
"""

from __future__ import annotations

import _thread
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config, mutation, nvimtest, presets, proc  # noqa: E402
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


# The suite's own pytest settings, which every copy of a project holds (Runs.run passes them)
SUITE_INI = {f"{mutation.TESTS}/pytest.ini": (Path(__file__).parent / "pytest.ini").read_text(encoding="utf-8")}


# --- options ------------------------------------------------------------------------------------


def test_options() -> None:
    assert mutation.parse_args([]) == Options(diff=None, jobs=mutation.default_jobs(), as_json=False)
    assert mutation.parse_args(["--diff", "origin/main", "--jobs", "3", "--json"]) == Options("origin/main", 3, True)
    assert mutation.parse_args(["--jobs", "1"]).jobs == 1  # one worker is a number of workers
    assert 1 <= mutation.default_jobs() <= 8


@pytest.mark.parametrize(("cpus", "jobs"), [(None, 1), (1, 1), (3, 1), (4, 2), (7, 3), (16, 8), (64, 8)])
def test_default_jobs_are_half_the_cpus_and_at_most_8(monkeypatch: pytest.MonkeyPatch, cpus: int | None, jobs: int) -> None:
    """A whole number: the workers are made with range(jobs). os.cpu_count() may not know (None)."""
    monkeypatch.setattr(mutation.os, "cpu_count", lambda: cpus)
    assert mutation.default_jobs() == jobs and type(mutation.default_jobs()) is int


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


def _nul_as_up_to_python_3_11_3(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parser of Python 3.11.3 and older: a ValueError for a NUL byte, a SyntaxError from 3.11.4
    on (uv installs the newest 3.11). The runner started by hand on a Python of the system meets
    it: Debian 12 has 3.11.2."""
    import ast

    real_parse, real_compile = ast.parse, compile

    def refuse_nul(source: Any) -> None:
        if (b"\0" if isinstance(source, bytes) else "\0") in source:
            raise ValueError("source code string cannot contain null bytes")

    def parse(source: Any, *args: Any, **kwargs: Any) -> Any:
        refuse_nul(source)
        return real_parse(source, *args, **kwargs)

    def old_compile(source: Any, *args: Any, **kwargs: Any) -> Any:
        refuse_nul(source)
        return real_compile(source, *args, **kwargs)

    monkeypatch.setattr(mutation.ast, "parse", parse)
    monkeypatch.setattr(mutation, "compile", old_compile, raising=False)


@pytest.mark.parametrize("old_parser", [False, True])
def test_test_map_finds_every_import_and_counts_the_mentions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_parser: bool) -> None:
    if old_parser:
        _nul_as_up_to_python_3_11_3(monkeypatch)
    _write(tmp_path, {
        ".pytemplate/tests/test_a.py": (
            "from runner import alpha, beta as b\nfrom runner.gamma import thing\nimport runner.delta\n"
            "def test_x():\n    alpha.f(); alpha.g(); b.h(); thing(); runner.delta.z()\n"
            "    from runner.methods import common\n    common.x()\n"
        ),
        ".pytemplate/tests/test_b.py": "from runner import alpha\n" + "alpha.f()\n" * 9,
        # pytest says what is wrong with them; the files after them still count
        ".pytemplate/tests/test_0broken.py": "from runner import alpha\ndef (\n",
        ".pytemplate/tests/test_0nul.py": "from runner import alpha\nx = 1\0\n",  # a NUL byte: the parser refuses it
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
        '<testcase classname=".pytemplate.tests.test_b" name="t0" time="bad"/>'  # no time: the cases after it still count
        '<testcase classname=".pytemplate.tests.test_a" name="t1" time="1.5"/>'
        '<testcase classname=".pytemplate.tests.test_a.TestK" name="t2" time="0.5"/>'
        '<testcase classname=".pytemplate.tests.test_b" name="t3" time="2"/>'
        '<testcase classname=".pytemplate.tests.test_ab" name="t4" time="7"/>'
        '<testcase classname="elsewhere" name="t5" time="9"/>'
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
def test_diff_in_a_folder_git_cannot_read_says_why(tmp_path: Path) -> None:
    """--diff in a folder that is no repository (or one git refuses: dubious ownership) said the
    BASE "names no commit here (fetch it first)", which cannot help (A10-06): git's own reason,
    exit 3, as the run without --diff gives it."""
    root = _write(tmp_path / "p", {f"{mutation.SCOPE}/a.py": "x = 1\n"})
    env = {**_git_env(tmp_path), "GIT_CEILING_DIRECTORIES": str(tmp_path)}
    with pytest.raises(PytError, match="not a git repository") as e:
        mutation.changed_lines(root, "origin/main", env)
    assert e.value.code == 3
    assert "fetch" not in str(e.value)


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


@needs_git
def test_a_git_that_fails_or_cannot_run_is_a_missing_requirement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 3 with git's own reason, or its exit code when it gives none; check=False gives the
    result back instead."""
    env = {**_git_env(tmp_path), "GIT_CEILING_DIRECTORIES": str(tmp_path)}
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    with pytest.raises(PytError, match="git rev-parse HEAD failed in .*nowhere: fatal: not a git repository") as e:
        mutation._git(nowhere, env, "rev-parse", "HEAD")
    assert e.value.code == 3
    assert mutation._git(nowhere, env, "rev-parse", "HEAD", check=False).returncode == 128
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, env, "init", "-q")
    with pytest.raises(PytError, match="git rev-parse --verify failed in .*repo: exit code 1$"):  # --quiet: not a word
        mutation._git(repo, env, "rev-parse", "--verify", "--quiet", "nope")
    broken = _write(tmp_path, {"git-that-cannot-start": "not a program\n"}) / "git-that-cannot-start"
    monkeypatch.setattr(mutation.shutil, "which", lambda name, *a, **k: str(broken))
    with pytest.raises(PytError, match="git rev-parse --verify did not run in") as e:  # its first two words, as for a failure
        mutation._git(repo, env, "rev-parse", "--verify", "HEAD")
    assert e.value.code == 3

    def hangs(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])  # the time _git gives git

    monkeypatch.setattr(mutation.shutil, "which", lambda name, *a, **k: "git")
    monkeypatch.setattr(mutation.subprocess, "run", hangs)
    with pytest.raises(PytError, match="git rev-parse HEAD did not run in .*timed out after 600 seconds") as e:
        mutation._git(repo, env, "rev-parse", "HEAD")
    assert e.value.code == 3
    monkeypatch.setattr(mutation.shutil, "which", lambda name, *a, **k: None)
    with pytest.raises(PytError, match="needs git") as e:
        mutation._git(repo, env, "rev-parse", "HEAD")
    assert e.value.code == 3


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
        _entry("core/ReplaceBinaryOperator_BitOr_BitAnd", 2, 0, _at(11, "int"), 3, "f"),  # at an annotation's first character
        _entry("core/NumberReplacer", 2, 0, _at(11, "1"), 1, "f"),  # kept: a default value
        _entry("core/ReplaceBinaryOperator_BitOr_BitAnd", 1, 0, _at(11, "|", 1), 1, "f"),  # the return annotation
        _entry("core/ReplaceBinaryOperator_Add_Sub", 0, 0, _at(14, "+"), 1, "f"),  # its line has the pragma
        _entry("core/ReplaceComparisonOperator_Is_IsNot", 0, 0, _at(12, "is"), 2, "f"),  # kept: a skipped one ends nothing
    ]
    rel = ".pytemplate/runner/m.py"
    kept = mutation.select(rel, listed, SOURCE, None)
    assert [(m.operator.split("/")[-1], m.line, m.occurrence) for m in kept] == [
        ("NumberReplacer", 8, 0), ("NumberReplacer", 11, 2), ("ReplaceComparisonOperator_Is_IsNot", 12, 0),
    ]  # fmt: skip
    assert kept[2].function == "f" and kept[0].function is None and kept[2].module == "runner.m" and kept[2].where == f"{rel}:12"
    assert [m.line for m in mutation.select(rel, listed, SOURCE, {12, 30})] == [12]  # --diff: the changed lines only
    assert mutation.select(rel, listed, SOURCE, set()) == []
    assert mutation.select(rel, listed, SOURCE.removesuffix("\n"), None) == kept  # the pragma on a last line without a line break
    first = [_entry("core/NumberReplacer", 0, 0, (1, 4)), _entry("core/NumberReplacer", 1, 0, (2, 4))]
    assert [m.line for m in mutation.select(rel, first, "x = 1  # pragma: no mutate\ny = 2\n", None)] == [2]  # on the first line too


BLOCKS = """\
import typing
from typing import TYPE_CHECKING

if typing.TYPE_CHECKING:
    X = 1
    Y = (
        2 + 3
    )
if TYPE_CHECKING:
    Z = 4 + 5
if options.verbose:
    W = 6


def g(
    a: Literal[
        7,
    ] = 8,
) -> None:
    pass
"""


def test_select_skips_whole_blocks_and_annotations_over_several_lines() -> None:
    """`if typing.TYPE_CHECKING:` is one too, and a block or an annotation ends where its last
    line does (not where its last statement starts, nor at the start of that line); another
    attribute is a plain condition."""
    lines = BLOCKS.splitlines()

    def at(line: int, text: str) -> tuple[int, int]:
        return line, lines[line - 1].index(text)

    listed = [
        _entry("core/NumberReplacer", 0, 0, at(5, "1")), _entry("core/NumberReplacer", 1, 0, at(7, "2")),
        _entry("core/ReplaceBinaryOperator_Add_Sub", 0, 0, at(7, "+")), _entry("core/NumberReplacer", 2, 0, at(10, "5")),
        _entry("core/AddNot", 0, 0, at(11, "options.verbose"), 15), _entry("core/NumberReplacer", 3, 0, at(12, "6")),
        _entry("core/NumberReplacer", 4, 0, at(17, "7"), 1, "g"), _entry("core/NumberReplacer", 5, 0, at(18, "8"), 1, "g"),
    ]  # fmt: skip
    kept = mutation.select(".pytemplate/runner/m.py", listed, BLOCKS, None)
    assert [(m.line, m.operator.split("/")[-1]) for m in kept] == [(11, "AddNot"), (12, "NumberReplacer"), (18, "NumberReplacer")]


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
    assert _classes("try:\n    pass\nexcept OSError: pass") == ["OSError"]  # on the last line, without a line break


def test_a_class_written_over_two_lines_is_one_span() -> None:
    source = "try:\n    pass\nexcept (OSError, subprocess\n        .TimeoutExpired):\n    pass\n"
    assert mutation.handler_classes(source) == {((3, 8), (3, 15)): "*()", ((3, 17), (4, 23)): "*()"}
    m = Mutant("m.py", mutation.EXCEPTION_REPLACER, 1, 3, 17, 4, 23, None)
    assert mutation.own_mutant(m, source) == "try:\n    pass\nexcept (OSError, *()):\n    pass\n"


@pytest.mark.parametrize(
    ("text", "parenthesized"),
    [
        ("(A, B)", True), ("(A,)", True), ("((A), (B))", True), ("(A, (B))", True), ("(A, b.C)", True),
        ("(\n    A,  # the first\n    B,  # the last\n)", True),
        ("(A), (B)", False), ("(A), B", False), ("A, (B)", False), ("AB, CD", False), ("A, B", False),
    ],
)  # fmt: skip
def test_parenthesized_is_a_tuple_in_parentheses_of_its_own(text: str, parenthesized: bool) -> None:
    assert mutation._parenthesized(text) is parenthesized


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
    called = HANDLERS.splitlines()[10].index("subprocess.CalledProcessError")
    listed = [
        _entry("core/ExceptionReplacer", 0, 0, (9, line.index("OSError")), 7, "catch"),
        _entry("core/ExceptionReplacer", 1, 0, (9, at + len("subprocess")), 1, "catch"),  # its dot
        _entry("core/ExceptionReplacer", 2, 0, (9, at), 10, "catch"),  # the same class again
        _entry("core/ExceptionReplacer", 3, 0, (11, called), 10, "catch"),  # the next one
    ]
    kept = mutation.select(".pytemplate/runner/m.py", listed, HANDLERS, None)
    assert [(m.line, m.column, m.end_line, m.end_column, m.occurrence) for m in kept] == [
        (9, line.index("OSError"), 9, line.index("OSError") + 7, 0), (9, at, 9, at + len("subprocess.TimeoutExpired"), 1),
        (11, called, 11, called + len("subprocess.CalledProcessError"), 3),
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


def test_made_takes_the_mutant_of_a_module_with_a_bom() -> None:
    """Python imports a module that starts with a UTF-8 BOM, and Cosmic Ray's mutant of one keeps
    it (U+FEFF first), which compile() refuses in a str: every such mutant read as "no valid
    Python" (A10-05). made compiles the bytes Python would read."""
    status, detail, code = mutation.made({"code": "﻿x = 2\n"}, b"\xef\xbb\xbfx = 1\n", "m.py")
    assert (status, detail, code) == (NOT_RUN, "", "﻿x = 2\n")


def test_made_on_a_python_that_refuses_a_nul_byte_with_a_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _nul_as_up_to_python_3_11_3(monkeypatch)
    assert mutation.made({"code": "x = 1\0\n"}, b"x = 1\n", "m.py") == (
        mutation.SKIPPED, "the mutant is no valid Python: source code string cannot contain null bytes", "x = 1\0\n",
    )  # fmt: skip


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
    before, after = "".join(f"a{i}\n" for i in range(10)), "".join(f"b{i}\n" for i in range(10))
    assert mutation.mutant_diff(before, after) == [f"-a{i}" for i in range(10)] + ["+b0", "+b1"]  # 12 lines at most in the report


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
        # a KeyboardInterrupt of the tests' own (a mutant switched a signal handler off): pytest
        # stops with 2 and counts no failure, and the summary may be "no tests ran"
        (2, f"{'.' * 80}\n{'!' * 30} KeyboardInterrupt {'!' * 30}\n/w0/t.py:968: KeyboardInterrupt\n(to show a full traceback on KeyboardInterrupt use --full-trace)\n80 passed in 14.39s\n", KILLED, "a KeyboardInterrupt ended the tests: /w0/t.py:968: KeyboardInterrupt"),
        (2, f"\n{'!' * 30} KeyboardInterrupt {'!' * 30}\n/w0/t.py:4: KeyboardInterrupt\nno tests ran in 0.28s\n", KILLED, "a KeyboardInterrupt ended the tests: /w0/t.py:4: KeyboardInterrupt"),
        (1, f"{'!' * 30} KeyboardInterrupt {'!' * 30}\n3 passed in 1.00s\n", mutation.ERROR, "exit code 1: 3 passed in 1.00s"),  # only with pytest's 2
    ],
)
def test_classify(code: int, output: str, status: str, detail: str) -> None:
    assert mutation.classify(code, output) == (status, detail)


def test_classify_a_run_that_was_ended() -> None:
    assert mutation.classify(None, "...") == (TIMEOUT, "")
    assert mutation.classify(None, "...", stopped=True) == (NOT_RUN, "interrupted")
    # once the suite was stopped (stop() killed the run), what the run gave back proves nothing
    stopped_by_ctrl_c = "!!! KeyboardInterrupt !!!\n3 passed in 1.00s\n"
    assert mutation.classify(2, stopped_by_ctrl_c, stopped=True) == (NOT_RUN, "interrupted")
    assert mutation.classify(1, "FAILED t.py::test_x\n1 failed in 1.00s\n", stopped=True) == (NOT_RUN, "interrupted")


def test_a_detail_is_cut_to_200_characters() -> None:
    """The line of the failing test, of the place a KeyboardInterrupt ended the tests, or the last
    one of a crash: one line of the report each, however long (a test id with a long parameter).
    The place keeps its end, the file and line: pytest names the file by its absolute path, whose
    first 200 characters under a long folder were folders only (A10-04)."""
    long = "x" * 300
    failed = f"FAILED t.py::test_x[{long}] - assert 0"
    assert mutation.classify(1, f"{failed}\n1 failed in 1.00s\n") == (KILLED, failed[:200])
    where, banner = f"/w0/{long}/test_x.py:4: KeyboardInterrupt", f"{'!' * 30} KeyboardInterrupt {'!' * 30}"
    status, detail = mutation.classify(2, f"{banner}\n{where}\n1 passed in 1.00s\n")
    assert (status, detail) == (KILLED, f"a KeyboardInterrupt ended the tests: ...{where[-197:]}")
    assert detail.endswith("/test_x.py:4: KeyboardInterrupt") and len(detail.split(": ", 1)[1]) == 200
    short = "/w0/t/test_x.py:4: KeyboardInterrupt"
    assert mutation.classify(2, f"{banner}\n{short}\n1 passed in 1.00s\n") == (KILLED, f"a KeyboardInterrupt ended the tests: {short}")
    assert mutation.classify(0, f"{long}\n") == (mutation.ERROR, f"exit code 0 without pytest's summary line: {long[:200]}")


def test_a_run_with_no_summary_reports_pytests_error_line() -> None:
    """A pytest usage error (exit 4: an unknown option) prints its `pytest: error:` line and then
    the `rootdir:` line, which says nothing. classify names the error line, not the last one."""
    out = "ERROR: usage: pytest [options] [file_or_dir]\npytest: error: unrecognized arguments: --hypothesis-seed=0\n\nrootdir: /home/me/proj\n"
    assert mutation.classify(4, out) == (mutation.ERROR, "exit code 4 without pytest's summary line: pytest: error: unrecognized arguments: --hypothesis-seed=0")


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
    elif request["op"] == "long":
        print("y" * 300, flush=True)
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
        with pytest.raises(PytError, match=f"answered '{'y' * 200}'  \\(log: ") as e:  # one line of the error, however long
            driver.ask({"op": "long"})
        assert e.value.code == 3
        with pytest.raises(PytError, match=r"stopped \(exit code 7\): fake driver: stopping") as e:
            driver.ask({"op": "die"})
        assert e.value.code == 3
    finally:
        driver.close()


def test_driver_closes_cleanly_and_a_driver_that_stopped_stays_an_error(tmp_path: Path) -> None:
    """close() ends Cosmic Ray's side by closing its input (it is never left to be killed after
    30 s); a worker that asks after the side died (a broken pipe) or after close() (a closed
    file) gets the same PytError as the first one."""
    driver = _driver(tmp_path)
    assert driver.ask({"op": "version"}) == {"version": "9.9.9"}
    start = time.monotonic()
    driver.close()
    assert driver._child.returncode == 0 and time.monotonic() - start < 20
    with pytest.raises(PytError, match=r"stopped \(exit code 0\)") as e:
        driver.ask({"op": "version"})
    assert e.value.code == 3
    driver = _driver(tmp_path)
    try:
        for _ in range(2):  # the second one writes into a pipe nobody reads
            with pytest.raises(PytError, match=r"stopped \(exit code 7\): fake driver: stopping") as e:
                driver.ask({"op": "die"})
            assert e.value.code == 3
    finally:
        driver.close()


def test_driver_close_ends_one_that_died_or_hangs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    driver = _driver(tmp_path)
    driver._child.kill()
    driver._child.wait()
    assert driver._child.stdin is not None
    driver._child.stdin.write("{}")  # buffered: close() flushes it into a pipe nobody reads
    driver.close()
    hangs = mutation.Driver("uv", _cfg(), tmp_path / "hangs.log", argv=[sys.executable, "-c", "import time; time.sleep(600)"])
    real = hangs._child.wait
    waits: list[float | None] = []

    def wait(timeout: float | None = None) -> int:
        waits.append(timeout)
        if timeout is not None:
            raise subprocess.TimeoutExpired("driver", timeout)  # it did not end within 30 s of its input closing
        return real()

    monkeypatch.setattr(hangs._child, "wait", wait)
    try:
        hangs.close()
    finally:
        if hangs._child.poll() is None:  # close() failed: never leave it running
            hangs._child.kill()
            real()
    assert hangs._child.returncode is not None and waits == [30, None]  # 30 s after its input closed, then killed


def test_driver_log_tail(tmp_path: Path) -> None:
    """The last line of Cosmic Ray's log goes into the error of a side that stopped."""
    driver = mutation.Driver.__new__(mutation.Driver)
    driver.log = tmp_path / "gone.log"
    assert driver._tail() == "no log"
    driver.log = _write(tmp_path, {"empty.log": "\n  \n"}) / "empty.log"
    assert driver._tail() == "no output"
    driver.log = _write(tmp_path, {"cr.log": "first\n  Traceback: the last one  \n\n"}) / "cr.log"
    assert driver._tail() == "Traceback: the last one"
    driver.log = _write(tmp_path, {"long.log": "t" * 400 + "\n"}) / "long.log"
    assert driver._tail() == "t" * 300  # one line of the error, however long


def test_driver_that_cannot_start(tmp_path: Path) -> None:
    with pytest.raises(PytError, match="cannot start uv") as e:
        mutation.Driver("uv", _cfg(), tmp_path / "driver.log", argv=[str(tmp_path / "missing")])
    assert e.value.code == 3


@pytest.mark.parametrize("old_parser", [False, True])
def test_list_mutants_turns_a_bad_answer_into_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_parser: bool) -> None:
    if old_parser:
        _nul_as_up_to_python_3_11_3(monkeypatch)

    class Fake:
        def __init__(self, answer: dict[str, Any]) -> None:
            self.answer = answer

        def ask(self, request: dict[str, Any]) -> dict[str, Any]:
            return self.answer

    _write(tmp_path, {"m.py": "x = 1\n", "bad.py": "def (\n"})
    snap = tmp_path / "snap"
    for answer in ({"error": "boom"}, {"mutants": "no list"}):
        with pytest.raises(PytError, match="could not list the mutants of m.py") as e:
            mutation.list_mutants(Fake(answer), tmp_path, ["m.py"], None, snap)  # type: ignore[arg-type]
        assert e.value.code == 1  # an error of the run, not a missing requirement
    (tmp_path / "latin1.py").write_bytes(b"x = '\xe9'\n")  # not UTF-8
    (tmp_path / "nul.py").write_bytes(b"x = 1\0\n")  # a NUL byte: the parser refuses it
    for name in ("bad.py", "latin1.py", "nul.py"):
        with pytest.raises(PytError, match=f"{name} is no Python the runner can read") as e:
            mutation.list_mutants(Fake({"mutants": []}), tmp_path, [name], None, snap)  # type: ignore[arg-type]
        assert e.value.code == 1
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


@contextmanager
def _umask(mask: int) -> Iterator[None]:
    """The umask for the block (the process's): a mode a test reads is not the user's choice."""
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


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
    with _umask(0o022):
        mutation.prepare_base(base, root)
    if sys.platform != "win32":  # the user's own: the workers run code from it
        assert stat.S_IMODE(base.stat().st_mode) == 0o700
    mutation.prepare_base(base, root)  # its own marker: reused
    assert (base / mutation.MARKER).is_file()
    mutation.prepare_base(tmp_path / "pt" / "mut", root)  # its parent made too (Windows' default: %TEMP%\pt\mut)
    assert (tmp_path / "pt" / "mut" / mutation.MARKER).is_file()


def test_prepare_base_refuses_a_base_git_cannot_be_kept_inside(tmp_path: Path) -> None:
    """The workers' git is kept inside the base (e2e.child_env: its parent is the ceiling), which
    a parent path holding os.pathsep (a TMPDIR below `a:b`) cannot be: refused before anything
    is made."""
    root = tmp_path / "project"
    root.mkdir()
    base = tmp_path / f"a{os.pathsep}b" / "mut"
    with pytest.raises(PytError, match="GIT_CEILING_DIRECTORIES") as e:
        mutation.prepare_base(base, root)
    assert e.value.code == 2 and not base.parent.exists()


def test_default_base_is_short_and_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    tmp = Path(tempfile.gettempdir())
    monkeypatch.setattr(mutation, "IS_WINDOWS", True)
    assert mutation.default_base() == tmp / "pt" / "mut"  # MAX_PATH
    monkeypatch.setattr(mutation, "IS_WINDOWS", False)
    assert mutation.default_base() == tmp / mutation.scratch_name("pt-mutation")
    if sys.platform != "win32":  # a shared /tmp: another user's base is never this one's
        assert mutation.default_base().name == f"pt-mutation-{os.getuid()}"


def test_one_run_at_a_time_per_base(tmp_path: Path) -> None:
    with _umask(0o022), mutation.base_lock(tmp_path):
        with pytest.raises(PytError, match="another run is using"):
            with mutation.base_lock(tmp_path):
                pass
    if sys.platform != "win32":
        assert stat.S_IMODE((tmp_path / "lock").stat().st_mode) == 0o600  # the user's own, as the base
    with mutation.base_lock(tmp_path):  # released
        pass


def test_a_base_whose_file_system_takes_no_lock_is_named_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mutation.base_lock is project.base_lock's copy: a lock call that fails for another reason
    than another holder (ENOSYS, a file system without locks) said "another run is using" (A10-06)."""

    def refuse(*_args: object) -> None:
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))

    if sys.platform == "win32":
        import msvcrt

        monkeypatch.setattr(msvcrt, "locking", refuse)
    else:
        import fcntl

        monkeypatch.setattr(fcntl, "flock", refuse)
    with pytest.raises(PytError) as e:
        with mutation.base_lock(tmp_path):
            pass
    assert str(e.value).startswith(f"selftest --mutation: cannot lock {tmp_path / 'lock'}: ") and "another run" not in str(e.value)


def test_worker_env_moves_home_and_temp_but_keeps_uv(tmp_path: Path) -> None:
    base_env = {
        "PATH": os.pathsep.join(["/usr/bin", "/bin"]), "HOME": "/home/me", "XDG_DATA_HOME": "/home/me/.data", "KEEP": "1",
        "PYTEST_ADDOPTS": "-n auto --lf", "PYTEST_PLUGINS": "mine",  # they would change what every run means
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",  # drops Hypothesis's --hypothesis-seed: every run would exit 4
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


def test_the_driver_and_the_workers_never_get_the_users_lock_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cosmic Ray's side starts with `uv run --locked --script`, which uv 0.10.12 to 0.12.8
    refused next to the user's UV_FROZEN (exit 2), and a worker's pytest gets what `./pyt
    selftest` gives it: no UV_FROZEN nor UV_LOCKED (envs.LOCK_MODE)."""
    for name in ("UV_FROZEN", "UV_LOCKED"):
        monkeypatch.setenv(name, "1")
    code = "import json, os, sys; sys.stdin.readline(); print(json.dumps({'seen': [k for k in ('UV_FROZEN', 'UV_LOCKED') if k in os.environ]}), flush=True)"
    driver = mutation.Driver("uv", _cfg(), tmp_path / "driver.log", argv=[sys.executable, "-c", code])
    try:
        assert driver.ask({"op": "version"}) == {"seen": []}
    finally:
        driver.close()
    base_env = {"PATH": os.pathsep.join(["/usr/bin", "/bin"]), "UV_FROZEN": "1", "UV_LOCKED": "1", "KEEP": "1"}
    env = mutation.worker_env(tmp_path / "w0", tmp_path / "h0", tmp_path / "t0", base_env, {}, _cfg(), "/opt/uv")
    assert "UV_FROZEN" not in env and "UV_LOCKED" not in env and env["KEEP"] == "1"


def test_every_write_of_a_module_gets_a_time_of_its_own(tmp_path: Path) -> None:
    """A .pyc records the whole second and the size of its source: two versions of the same size
    written in one second would share it."""
    path = tmp_path / "m.py"
    times = []
    for data in (b"x = 1\n", b"x = 2\n", b"x = 1\n"):
        mutation.write_module(path, data)
        times.append(int(path.stat().st_mtime))
        assert path.stat().st_mtime_ns % 10**9 == 0  # a whole second, as a .pyc keeps it
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


def test_a_copy_whose_sync_fails_says_why_even_under_q(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    """`./pyt -q selftest --mutation` added its own --quiet to the sync's: `uv -qq` fails without
    a word (a worker that could not be made, and no reason). The uv of this run: the CI runs the
    suite with the newest uv and with the oldest the project takes."""
    try:
        proc.find_uv()
    except PytError:
        pytest.skip("uv not found")
    copy = _write(tmp_path / "copy", {"pyproject.toml": '[project]\nname = "toy"\nversion = "0"\nrequires-python = ">=3.11"\ndependencies = []\n'})
    (copy / "uv.lock").write_text('version = 1\nrequires-python = ">=3.9"\n', encoding="utf-8")  # stale: another requires-python
    monkeypatch.setattr(mutation.ui, "QUIET", True)
    monkeypatch.setattr(mutation.envs, "left_out", lambda env: [])
    venv = mutation.envs.PyEnv("cpython", copy / ".venv", _cfg().python.cpython, "only-managed")
    with pytest.raises(proc.CommandFailed):
        mutation.sync_copy(venv, copy)
    assert "--locked" in capfd.readouterr().err  # uv's own error: the lock needs to be updated


OLD_UV = """\
import sys

if sys.argv[1:] == ["--version"]:
    print("uv 0.10.12 (as it answers)")
elif any(a in ("--quiet", "-q", "-qq") for a in sys.argv[1:]):
    sys.exit(1)  # a stale lock under --quiet: exit 1, and no word
else:
    print("Resolved 1 package in 3ms", file=sys.stderr)
    print("The lockfile at `uv.lock` needs to be updated, but `--locked` was provided.", file=sys.stderr)
    sys.exit(1)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the fake uv is a #! script")
def test_a_copy_whose_sync_fails_says_why_with_the_oldest_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    """uv 0.10.12, the oldest the project takes, says nothing about a stale lock under one
    --quiet (as this fake does): the sync runs without it, its output captured and shown when it
    fails, with -q too."""
    fake = tmp_path / "uv"
    fake.write_text(f"#!{sys.executable}\n{OLD_UV}", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("UV", str(fake))
    monkeypatch.setattr(mutation.ui, "QUIET", True)
    monkeypatch.setattr(mutation.envs, "left_out", lambda env: [])
    copy = tmp_path / "copy"
    copy.mkdir()
    venv = mutation.envs.PyEnv("cpython", copy / ".venv", _cfg().python.cpython, "only-managed")
    with pytest.raises(proc.CommandFailed):
        mutation.sync_copy(venv, copy)
    assert "`--locked` was provided" in capfd.readouterr().err


@needs_git
def test_make_copy_is_a_repository_of_the_listed_files(tmp_path: Path) -> None:
    env = _git_env(tmp_path)
    root = _write(tmp_path / "p", {
        "a.py": "x\n", "sub/b.txt": "y\n", "deep/er/c.txt": "w\n", ".gitignore": "ignored/\n", "ignored/c.txt": "z\n",
        "module/x.txt": "a submodule's file\n",
    })  # fmt: skip
    _git(root, env, "init", "-q")
    _git(root, env, "add", "a.py", ".gitignore")
    _write(root, {"a.py": "changed\n"})  # the working tree's content, not the commit's
    files = mutation.listed_files(root, env)
    assert files == [".gitignore", "a.py", "deep/er/c.txt", "module/x.txt", "sub/b.txt"]  # untracked but not ignored counts
    copy = tmp_path / "copy"
    # deleted from the working tree, and a submodule (git lists its folder): left out, and the rest still copied
    mutation.make_copy(root, copy, ["deleted.py", "module", *(f for f in files if not f.startswith("module/"))], env)
    assert (copy / "a.py").read_text(encoding="utf-8") == "changed\n" and not (copy / "ignored").exists()
    assert (copy / "deep" / "er" / "c.txt").read_text(encoding="utf-8") == "w\n" and not (copy / "module").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=copy, env=env, capture_output=True, text=True, check=True).stdout
    assert status == ""  # every file committed


@needs_git
def test_a_submodule_and_a_nested_repository_reach_the_copy_as_folders(tmp_path: Path) -> None:
    """git lists a submodule, and a repository nested in the project, as one folder, which
    make_copy left out: a local library kept in a submodule (`./pyt add ./libs/mylib`) was
    missing from every worker, whose `uv sync --locked` failed and stopped the run (A10-02).
    Their own files are listed in their place, as `new` copies a submodule, each repository's
    ignores its own, and the copy commits them as files."""
    env = _git_env(tmp_path)
    lib = _write(tmp_path / "lib", {
        "pyproject.toml": '[project]\nname = "mylib"\nversion = "0.1"\n', "src/mylib/__init__.py": "VALUE = 1\n",
        ".gitignore": "build/\n",
    })  # fmt: skip
    _git(lib, env, "init", "-q")
    _git(lib, env, "add", "-A")
    _git(lib, env, "commit", "-qm", "lib")
    root = _write(tmp_path / "p", {"a.py": "x\n"})
    _git(root, env, "init", "-q")
    _git(root, env, "add", "-A")
    _git(root, env, "commit", "-qm", "p")
    _git(root, env, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "libs/mylib")
    _write(root, {"libs/mylib/build/x.o": "junk\n", "libs/mylib/notes.txt": "n\n", "libs/inner/b.txt": "b\n"})
    _git(root / "libs" / "inner", env, "init", "-q")
    files = mutation.listed_files(root, env)
    assert {"libs/mylib/pyproject.toml", "libs/mylib/src/mylib/__init__.py", "libs/mylib/notes.txt", "libs/inner/b.txt"} <= set(files), files
    assert not [f for f in files if f.rstrip("/") in ("libs/mylib", "libs/inner") or "/build/" in f or ".git" in f.split("/")], files
    copy = tmp_path / "copy"
    mutation.make_copy(root, copy, files, env)
    assert (copy / "libs" / "mylib" / "src" / "mylib" / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (copy / "libs" / "inner" / "b.txt").is_file() and not (copy / "libs" / "mylib" / ".git").exists()
    index = subprocess.run(["git", "ls-files", "-s"], cwd=copy, env=env, capture_output=True, text=True, check=True).stdout
    assert "160000" not in index and "libs/mylib/pyproject.toml" in index and "libs/inner/b.txt" in index, index


@needs_git
def test_make_copy_keeps_the_executables_of_the_projects_index(tmp_path: Path) -> None:
    """Git for Windows' `git init` writes core.filemode = false (NTFS keeps no x bit), and `git
    add` then records every new file as 100644: a worker's pyt and pyt.ps1 lost the 100755 of the
    project's index, which test_launcher_sh and test_launcher_win read, and the baselines of
    runner.project and runner.cmd_install failed on Windows. Simulated here with that setting on
    the copy's git (only the real Windows runner proves what its `git init` writes)."""
    env = _git_env(tmp_path)
    root = _write(tmp_path / "p", {"pyt": "#!/bin/sh\n", "pyt.ps1": "#!/usr/bin/env pwsh\n", "a.py": "x\n", "tools/new.sh": "#!/bin/sh\n"})
    _git(root, env, "init", "-q")
    _git(root, env, "add", "pyt", "pyt.ps1", "a.py")
    _git(root, env, "update-index", "--chmod=+x", "pyt", "pyt.ps1")  # tools/new.sh: untracked, as the working tree has it
    windows = {**env, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.filemode", "GIT_CONFIG_VALUE_0": "false"}
    copy = tmp_path / "copy"
    mutation.make_copy(root, copy, mutation.listed_files(root, env), windows)
    staged = subprocess.run(["git", "ls-files", "-s"], cwd=copy, env=windows, capture_output=True, text=True, check=True).stdout
    modes = {line.split("\t")[1]: line.split()[0] for line in staged.splitlines()}
    assert modes == {"a.py": "100644", "pyt": "100755", "pyt.ps1": "100755", "tools/new.sh": "100644"}
    assert sorted(mutation.executables(root, env)) == ["pyt", "pyt.ps1"]
    plain = tmp_path / "plain"
    plain.mkdir()
    assert mutation.executables(plain, {**env, "GIT_CEILING_DIRECTORIES": str(tmp_path)}) == []  # no repository: nothing to mark


@needs_git
def test_make_copy_takes_an_executable_the_project_tracks_past_its_gitignore(tmp_path: Path) -> None:
    """A vendored native library is `git add -f`ed past the .gitignore (*.so), often with its x
    bit: the copy's `git add -A` leaves it out, and marking it executable in the copy's index
    (`git update-index --chmod=+x`) failed with "cannot add to the index", which stopped every
    worker of selftest --mutation. Only what the copy's index holds is marked."""
    env = _git_env(tmp_path)
    root = _write(tmp_path / "p", {".gitignore": "*.so\n", "pyt": "#!/bin/sh\n", "src/libfoo.so": "ELF\n"})
    _git(root, env, "init", "-q")
    _git(root, env, "add", ".gitignore", "pyt")
    _git(root, env, "add", "-f", "src/libfoo.so")
    _git(root, env, "update-index", "--chmod=+x", "pyt", "src/libfoo.so")
    assert sorted(mutation.executables(root, env)) == ["pyt", "src/libfoo.so"]
    windows = {**env, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.filemode", "GIT_CONFIG_VALUE_0": "false"}
    copy = tmp_path / "copy"
    mutation.make_copy(root, copy, mutation.listed_files(root, env), windows)
    staged = subprocess.run(["git", "ls-files", "-s"], cwd=copy, env=windows, capture_output=True, text=True, check=True).stdout
    assert {line.split("\t")[1]: line.split()[0] for line in staged.splitlines()} == {".gitignore": "100644", "pyt": "100755"}
    assert (copy / "src" / "libfoo.so").read_text(encoding="utf-8") == "ELF\n"  # in the copy, as in the project's working tree


def _symlinks(root: Path) -> None:
    try:
        os.symlink("a.py", root / "link.py")
        os.symlink("sub", root / "linkdir", target_is_directory=True)
    except OSError as e:  # Windows without the right to make links
        pytest.skip(f"cannot make a symbolic link here: {e}")


@needs_git
def test_make_copy_keeps_links_as_links(tmp_path: Path) -> None:
    env = _git_env(tmp_path)
    root = _write(tmp_path / "p", {"a.py": "x\n", "sub/b.txt": "y\n"})
    _symlinks(root)
    _git(root, env, "init", "-q")
    files = ["a.py", "link.py", "linkdir", "sub/b.txt"]
    if sys.platform != "win32":  # Git for Windows may read links as it reads core.symlinks
        assert mutation.listed_files(root, env) == files  # a link to a folder is one file for git
    copy = tmp_path / "copy"
    mutation.make_copy(root, copy, files, env)
    assert os.readlink(copy / "link.py") == "a.py" and os.readlink(copy / "linkdir") == "sub"


@needs_git
def test_make_copy_names_what_lies_outside_the_project_from_the_copy(tmp_path: Path) -> None:
    """A library next to the project (`./pyt add ../mylib`: pyproject.toml and uv.lock name it
    from the project) and a link that leaves the project named other folders from <base>/w<i>:
    every worker's `uv sync --locked` failed ("Distribution not found at <base>/mylib") and the
    run stopped. The copy names them from its own folder, both files alike; what lies inside
    the project (a library, a link) stays as it is, and the project itself is never touched."""
    env = _git_env(tmp_path)
    side = tmp_path / "side"
    _write(side / "mylib", {"pyproject.toml": '[project]\nname = "mylib"\n'})
    _write(side / "shared", {"logo.txt": "logo\n"})
    pyproject = '[project]\nname = "proj"\ndependencies = ["inner", "mylib"]\n\n[tool.uv.sources]\nmylib = { path = "../mylib" }\ninner = { path = "libs/inner" }\n'
    lock = (
        'version = 1\n\n[[package]]\nname = "inner"\nversion = "0.1.0"\nsource = { directory = "libs/inner" }\n\n'
        '[[package]]\nname = "mylib"\nversion = "0.1.0"\nsource = { directory = "../mylib" }\n\n'
        '[[package]]\nname = "proj"\nversion = "0.1.0"\nsource = { virtual = "." }\n\n[package.metadata]\n'
        'requires-dist = [\n    { name = "inner", directory = "libs/inner" },\n    { name = "mylib", directory = "../mylib" },\n]\n'
    )
    root = _write(side / "proj", {"pyproject.toml": pyproject, "uv.lock": lock, "libs/inner/pyproject.toml": '[project]\nname = "inner"\n', "src/a.py": "x\n"})
    try:
        os.symlink(os.path.join("..", "shared"), root / "assets", target_is_directory=True)
        os.symlink("src", root / "code", target_is_directory=True)
    except OSError as e:  # Windows without the right to make links
        pytest.skip(f"cannot make a symbolic link here: {e}")
    _git(root, env, "init", "-q")
    copy = tmp_path / "base" / "w0"
    mutation.make_copy(root, copy, mutation.listed_files(root, env), env)
    text = (copy / "pyproject.toml").read_text(encoding="utf-8")
    assert text == pyproject.replace('"../mylib"', '"../../side/mylib"')
    assert (copy / "uv.lock").read_text(encoding="utf-8") == lock.replace('"../mylib"', '"../../side/mylib"')
    assert (copy / "assets" / "logo.txt").read_text(encoding="utf-8") == "logo\n" and os.readlink(copy / "code") == "src"
    assert (root / "pyproject.toml").read_text(encoding="utf-8") == pyproject and os.readlink(root / "assets") == os.path.join("..", "shared")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=copy, env=env, capture_output=True, text=True, check=True).stdout
    assert status == ""  # what the copy names is what it committed


@needs_git
def test_make_copy_copies_what_a_link_names_where_links_cannot_be_made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows without the right to make links: the file or the folder a link names takes its
    place (a folder was copied as a file: an error). Any other file that cannot be copied stops
    the copy."""
    env = _git_env(tmp_path)
    root = _write(tmp_path / "p", {"a.py": "x\n", "sub/b.txt": "y\n"})
    _symlinks(root)
    real = shutil.copy2

    def copy2(src: Any, dst: Any, *, follow_symlinks: bool = True) -> Any:
        if Path(src).is_symlink() and not follow_symlinks:
            raise OSError("A required privilege is not held by the client")
        if Path(src).name == "locked.py":
            raise PermissionError(f"in use by another process: {src}")
        return real(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(mutation.shutil, "copy2", copy2)
    copy = tmp_path / "copy"
    mutation.make_copy(root, copy, ["a.py", "link.py", "linkdir", "sub/b.txt"], env)
    assert not (copy / "link.py").is_symlink() and (copy / "link.py").read_text(encoding="utf-8") == "x\n"
    assert not (copy / "linkdir").is_symlink() and (copy / "linkdir" / "b.txt").read_text(encoding="utf-8") == "y\n"
    _write(root, {"locked.py": "y\n"})
    with pytest.raises(PermissionError, match="in use"):
        mutation.make_copy(root, tmp_path / "copy2", ["a.py", "locked.py"], env)


# --- running tests: time limits, a stop, the threads --------------------------------------------------


def _worker(tmp_path: Path, tests: dict[str, str]) -> Worker:
    copy = _write(tmp_path / "copy", {**SUITE_INI, **tests})
    (tmp_path / "pytest").mkdir()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return Worker(0, copy, Path(sys.executable), tmp_path / "pytest", tmp_path / "w0.log", env)


def test_run_reports_pytests_result(tmp_path: Path) -> None:
    worker = _worker(tmp_path, {"test_ok.py": "def test_ok():\n    assert True\n", "test_bad.py": "def test_bad():\n    assert 1 == 2\n"})
    runs = Runs()
    start = time.perf_counter()
    code, output, seconds = runs.run(worker, ["test_ok.py"], 120, junit=tmp_path / "junit.xml")
    assert mutation.classify(code, output) == (SURVIVED, "") and 0 < seconds <= time.perf_counter() - start
    assert mutation.junit_seconds(tmp_path / "junit.xml", ["test_ok.py"]).keys() == {"test_ok.py"}
    code, output, _ = runs.run(worker, ["test_ok.py", "test_bad.py"], 120)
    assert mutation.classify(code, output)[0] == KILLED


def test_a_leftover_in_the_basetemp_never_fails_the_next_run(tmp_path: Path, unprivileged_python: Path) -> None:
    """A run that its time limit or a stop killed, or a test that a mutant made fail before it
    put a folder's mode back, leaves an unreadable folder in the worker's --basetemp, which
    pytest's own cleanup cannot remove: the next run errored at setup (FileExistsError), a kill
    for classify, of a mutant that survives (A10-01). Runs.run empties the basetemp first."""
    worker = _worker(tmp_path, {"test_ok.py": "def test_ok(tmp_path):\n    assert tmp_path.is_dir()\n"})
    worker = Worker(worker.index, worker.copy, unprivileged_python, worker.tmp, worker.log, worker.env)
    left = worker.tmp / "test_killed_run0" / "locked"
    left.mkdir(parents=True)
    (left / "f.txt").write_text("x", encoding="utf-8")
    left.chmod(0)
    try:
        code, output, _ = Runs().run(worker, ["test_ok.py"], 120)
    finally:
        if left.exists():
            left.chmod(0o700)
    assert mutation.classify(code, output) == (SURVIVED, ""), output


def test_the_cleanup_never_loses_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The workers' folders and the logs go in run()'s finally: whatever stops their removal is a
    warning, never an error that loses the report of hours of runs (rmtree raised TypeError on a
    folder a test left unreadable, A10-01)."""
    for name in ("w0", "t0", "logs"):
        (tmp_path / name).mkdir()

    def broken(path: Path) -> None:
        raise TypeError("open() missing required argument 'flags' (pos 2)")

    monkeypatch.setattr(mutation, "rmtree", broken)
    mutation._remove_workers(tmp_path)
    mutation._remove_logs(tmp_path)
    err = capsys.readouterr().err
    assert "could not remove the workers' folders" in err and "could not remove" in err.split("workers' folders", 1)[1], err


def test_run_uses_the_suites_own_pytest_settings(tmp_path: Path) -> None:
    """The copy is the project: its pyproject.toml gives its app's tests their settings (a
    coverage gate: every baseline failed, here an unknown option, exit 4) and so does its root
    conftest.py. Runs.run passes the suite's own (-c); the test ids and the JUnit classnames
    junit_seconds reads stay the project's (.pytemplate/tests/...)."""
    rel = f"{mutation.TESTS}/test_ok.py"
    worker = _worker(tmp_path, {
        rel: "def test_ok():\n    assert True\n",
        "pyproject.toml": '[tool.pytest.ini_options]\naddopts = ["--cov=src", "--cov-fail-under=50"]\npython_files = ["*_check.py"]\n',
        "conftest.py": 'raise RuntimeError("the project conftest reached the suite")\n',
    })  # fmt: skip
    code, output, _ = Runs().run(worker, [rel], 120, junit=tmp_path / "junit.xml")
    assert mutation.classify(code, output) == (SURVIVED, ""), output
    assert mutation.junit_seconds(tmp_path / "junit.xml", [rel]).keys() == {rel}


@pytest.mark.usefixtures("default_signals")
def test_a_test_run_that_a_keyboard_interrupt_ends_is_a_kill(tmp_path: Path) -> None:
    """For real: a test that sends itself SIGINT (a mutant switched off the handler it counted
    on), first or after others. The runs were not stopped: the tests' own interrupt. The copy
    lies deep enough that pytest's "path:line: KeyboardInterrupt" line passes 200 characters
    whatever TMPDIR is: the detail kept its first 200 (folders) and lost the file and line, and
    this test failed under a long TMPDIR (A10-04). It stays under Windows' 260 for the file."""
    deep = tmp_path / ("d" * max(1, 170 - len(str(tmp_path)) - 1))
    deep.mkdir()
    worker = _worker(deep, {
        "test_first.py": "import signal\n\ndef test_sig():\n    signal.raise_signal(signal.SIGINT)\n",
        "test_later.py": "def test_ok():\n    pass\n\ndef test_ki():\n    raise KeyboardInterrupt\n",
    })  # fmt: skip
    runs = Runs()
    for name, line in (("test_first.py", 4), ("test_later.py", 5)):
        code, output, _ = runs.run(worker, [name], 120)
        status, detail = mutation.classify(code, output, stopped=runs.stopped.is_set())
        assert (code, status) == (2, KILLED) and detail.endswith(f"{name}:{line}: KeyboardInterrupt"), output


def test_the_terminals_ctrl_c_never_reaches_a_test_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run has a session of its own on POSIX and a process group of its own on Windows, which
    ignores the console's Ctrl+C: the Ctrl+C reaches the runner alone, whose stop() ends the run.
    One that reached pytest ended it with the KeyboardInterrupt banner and exit code 2, which
    reads as a kill when the runner's own interrupt came a moment later."""
    worker = _worker(tmp_path, {"test_ok.py": "def test_ok():\n    pass\n"})
    seen: dict[str, Any] = {}
    real = subprocess.Popen

    def popen(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(mutation.subprocess, "Popen", popen)
    assert mutation.classify(*Runs().run(worker, ["test_ok.py"], 120)[:2]) == (SURVIVED, "")
    if sys.platform == "win32":
        assert seen["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP and not seen["start_new_session"]
    else:
        assert seen["start_new_session"] is True and seen["creationflags"] == 0


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
    assert runs.run(worker, ["test_slow.py"], 600) == (None, "", 0.0)  # stopped: nothing starts any more, nor takes any time


def test_stop_ends_the_running_runs_itself(tmp_path: Path) -> None:
    """stop() kills every run there and then (an interrupt: the terminal is waiting), before the
    run's own loop sees the stop."""
    worker = _worker(tmp_path, {"test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(120)\n"})
    runs = Runs()
    got: list[tuple[int | None, str, float]] = []
    thread = threading.Thread(target=lambda: got.append(runs.run(worker, ["test_slow.py"], 600)))
    thread.start()
    deadline = time.monotonic() + 60
    while not runs._children and time.monotonic() < deadline:
        time.sleep(0.01)
    child = runs._children[worker.index]
    runs.stop()
    assert child.poll() is not None
    thread.join(60)
    assert got[0][0] is None


def test_a_run_that_fails_while_it_waits_leaves_no_test_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever goes wrong while a run waits for pytest, pytest does not go on alone."""
    worker = _worker(tmp_path, {"test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(120)\n"})
    started: list[subprocess.Popen[bytes]] = []

    class Popen(subprocess.Popen[bytes]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            started.append(self)

        def wait(self, timeout: float | None = None) -> int:
            if timeout == 0.5:  # the run's own wait
                raise RuntimeError("boom")
            return super().wait(timeout)

    monkeypatch.setattr(mutation.subprocess, "Popen", Popen)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            Runs().run(worker, ["test_slow.py"], 600)
        assert started[0].poll() is not None
    finally:
        for child in started:
            if child.poll() is None:
                child.kill()
                subprocess.Popen.wait(child)


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
def test_descendants_cross_sessions(tree: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    child, pids = tree
    if sys.platform.startswith("linux"):  # /proc answers, never ps
        monkeypatch.setattr(mutation.shutil, "which", lambda name, *a, **k: None)
    assert set(pids) <= set(mutation.descendants(child.pid))
    assert child.pid not in mutation.descendants(child.pid)


def _no_proc(monkeypatch: pytest.MonkeyPatch, first: str | None = None) -> None:
    """/proc cannot be listed, or (`first`) lists a process that ends before it is read first."""
    real = os.scandir

    def scandir(path: Any = ".") -> Any:
        if str(path) != "/proc":
            return real(path)
        if first is None:
            raise PermissionError("[Errno 13] Permission denied: '/proc'")
        return [SimpleNamespace(name=first), *real(path)]

    monkeypatch.setattr(mutation.os, "scandir", scandir)


@posix
@pytest.mark.skipif(shutil.which("ps") is None, reason="ps is not installed")
def test_descendants_through_ps(tree: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Where there is no /proc (macOS, the BSDs), ps answers."""
    child, pids = tree
    monkeypatch.setattr(mutation.sys, "platform", "darwin")
    _no_proc(monkeypatch)
    found = mutation.descendants(child.pid)
    monkeypatch.undo()
    assert set(pids) <= set(found)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc of Linux")
def test_descendants_through_proc_pass_over_what_they_cannot_read(tree: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    child, pids = tree
    _no_proc(monkeypatch, first="4194305")  # above pid_max: a process that ended between the listing and the reading
    found = mutation.descendants(child.pid)
    _no_proc(monkeypatch)
    none = mutation.descendants(child.pid)
    monkeypatch.undo()
    assert set(pids) <= set(found) and none == []


def _ps(monkeypatch: pytest.MonkeyPatch, out: bytes = b"", code: int = 0, error: BaseException | None = None) -> None:
    """descendants' ps (macOS, the BSDs), faked: what it prints and how it ends."""

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert argv[1:] == ["-A", "-o", "pid=", "-o", "ppid="] and kwargs["timeout"] == 30  # a ps that hangs never holds the kill
        if error is not None:
            raise error
        done = subprocess.CompletedProcess(argv, code, out, b"")
        if kwargs.get("check"):
            done.check_returncode()
        return done

    monkeypatch.setattr(mutation.sys, "platform", "darwin")
    monkeypatch.setattr(mutation.shutil, "which", lambda name, *a, **k: "/bin/ps")
    monkeypatch.setattr(mutation.subprocess, "run", run)


def test_descendants_read_ps_line_by_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each line is a pid and its parent's; any other line is passed over. A pid met twice is one
    process, and one that comes back to where the walk began (a pid reused meanwhile) ends it."""
    lines = b"  100     1\n  200   100\n  300   200\n  300   200\n    x   100\n  400\n  500   300 extra\n  600   999\n"
    _ps(monkeypatch, lines, code=1)  # what ps could read, even when it says some process escaped it
    assert mutation.descendants(100) == [200, 300]
    _ps(monkeypatch, b"200 100\n300 200\n100 300\n")
    assert mutation.descendants(100) == [200, 300]
    for error in (OSError("ps: cannot run"), subprocess.TimeoutExpired("ps", 30)):
        _ps(monkeypatch, error=error)
        assert mutation.descendants(100) == []
    monkeypatch.setattr(mutation.shutil, "which", lambda name, *a, **k: None)
    assert mutation.descendants(100) == []  # no ps: none found


@posix
def test_kill_run_passes_over_a_process_that_ended_meanwhile(tree: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    child, pids = tree
    monkeypatch.setattr(mutation, "descendants", lambda pid: [2**31 - 1, *pids])  # no such process: it ended
    mutation.kill_run(child)
    assert child.poll() is not None and _stopped(pids)


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


def test_run_all_gives_every_item_to_one_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, int]] = []
    crashed: list[Any] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: crashed.append(args.exc_type))

    def handle(worker: Worker, item: int) -> None:
        time.sleep(0.01)
        seen.append((worker.index, item))

    mutation.run_all(_workers(3, tmp_path), list(range(20)), handle, Runs())
    assert sorted(item for _, item in seen) == list(range(20)) and len({w for w, _ in seen}) > 1
    assert crashed == []  # a worker that finds nothing left to do ends quietly


def test_run_all_stops_at_the_first_failure(tmp_path: Path) -> None:
    runs = Runs()

    def handle(worker: Worker, item: int) -> None:
        if item == 3:
            raise ValueError("boom")
        time.sleep(0.05)

    with pytest.raises(ValueError, match="boom"):
        mutation.run_all(_workers(2, tmp_path), list(range(100)), handle, runs)
    assert runs.stopped.is_set()


@pytest.mark.usefixtures("default_signals")
def test_run_all_goes_on_as_an_interrupt(tmp_path: Path) -> None:
    """Ctrl+C reaches the main thread only: the runs stop, the workers end (a mutant written into
    a copy gets its module's bytes back), and then the interrupt goes on."""
    runs = Runs()
    done: list[int] = []
    busy: set[int] = set()

    def handle(worker: Worker, item: int) -> None:
        busy.add(item)
        if item == 0:
            _thread.interrupt_main()
        time.sleep(0.2)
        done.append(item)
        busy.discard(item)

    with pytest.raises(KeyboardInterrupt):
        mutation.run_all(_workers(2, tmp_path), list(range(50)), handle, runs)
    assert runs.stopped.is_set() and len(done) < 50 and busy == set()


def _fake_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """_test's workers without git, uv or a real environment: a copy holds the modules only."""

    def copy(root: Path, dest: Path, files: Sequence[str], env: Mapping[str, str], contents: Mapping[str, bytes] | None = None) -> None:
        for name, data in (contents or {}).items():
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            (dest / name).write_bytes(data)

    monkeypatch.setattr(mutation, "child_env", lambda base: dict(os.environ))
    monkeypatch.setattr(nvimtest, "uv_dirs", lambda env: {})
    monkeypatch.setattr(mutation, "listed_files", lambda root, env: [])
    monkeypatch.setattr(mutation, "make_copy", copy)
    monkeypatch.setattr(mutation, "sync_copy", lambda venv, copy: None)
    monkeypatch.setattr(mutation, "worker_env", lambda *a: {})


def test_each_mutant_runs_in_a_workers_copy_within_its_baselines_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_test with the copies, uv, Cosmic Ray and pytest faked: each mutant runs in a worker's
    copy of its module, within TIMEOUT_FACTOR x its baseline + TIMEOUT_EXTRA, and the copy gets
    the module's own bytes back; a mutant that is no valid Python never runs; only an error keeps
    its log."""
    rel = ".pytemplate/runner/calc.py"
    original = "def f(x):\n    return x + 1\n"
    answers = {  # Cosmic Ray's mutant of each occurrence: its comment says what its tests make of it
        0: "def f(x):\n    return x - 1  # killed\n",
        1: "def f(x):\n    return x + 2  # survived\n",
        2: "def f(x):\n    return x * 1  # crashed\n",
        3: "def f(x):\n    return x +\n",  # no valid Python: never run
        4: "def f(x):\n    return x // 1  # slow\n",
    }
    outputs: dict[str, tuple[int | None, str]] = {
        "killed": (1, "FAILED t.py::test_f - assert 0 == 2\n1 failed in 0.10s\n"),
        "survived": (0, "3 passed in 0.10s\n"),
        "crashed": (1, "Fatal Python error: Segmentation fault\n"),
        "slow": (None, ""),
    }
    ran: list[tuple[str, float]] = []
    base = tmp_path / "base"
    (base / "logs").mkdir(parents=True)

    class FakeDriver(mutation.Driver):
        def __init__(self) -> None:  # no Cosmic Ray
            pass

        def ask(self, request: Mapping[str, Any]) -> dict[str, Any]:
            return {"code": answers[int(request["occurrence"])]}

    def run(self: Runs, worker: Worker, tests: Sequence[str], timeout: float, junit: Path | None = None) -> tuple[int | None, str, float]:
        text = (worker.copy / rel).read_text(encoding="utf-8")
        ran.append((text, timeout))
        code, output = next((outputs[k] for k in outputs if f"# {k}" in text), (0, "3 passed in 5.00s\n"))
        worker.log.write_text(output, encoding="utf-8")
        return code, output, 5.0 if text == original else 0.1

    _fake_workers(monkeypatch)
    monkeypatch.setattr(Runs, "run", run)
    todo = [Mutant(rel, "core/AddNot", i, 2, 11, 2, 16, "f") for i in answers]
    report = Report(list(todo), {}, base)
    tests = {"runner.calc": {".pytemplate/tests/test_calc.py": 1}}
    mutation._test(_cfg(), Options(None, 2, False), "uv", ROOT, base, tests, todo, {rel: original.encode()}, base / "snapshot", FakeDriver(), report)
    by = {m.occurrence: m for m in report.mutants}
    assert {i: m.status for i, m in by.items()} == {0: KILLED, 1: SURVIVED, 2: mutation.ERROR, 3: mutation.SKIPPED, 4: TIMEOUT}
    assert report.workers == 2 and report.baselines["runner.calc"].status == mutation.PASS
    limit = 2 * 5.0 + 60.0  # twice the baseline's time, plus a minute (CLAUDE.md 13.1)
    assert sorted(t for text, t in ran if text != original) == [limit] * 4 and answers[3] not in [text for text, _ in ran]
    assert [t for text, t in ran if text == original] == [mutation.BASELINE_TIMEOUT]
    assert by[4].detail == f"no result after {limit:.0f} s" and by[3].detail.startswith("the mutant is no valid Python")
    assert by[0].seconds == 0.1 and by[3].seconds == 0.0  # what its run took; one that never ran took no time
    saved = base / "logs" / "error-1.log"
    assert by[2].detail.endswith(f"  (log: {saved})") and saved.read_text(encoding="utf-8") == outputs["crashed"][1]
    assert sorted(p.name for p in (base / "logs").glob("error-*")) == ["error-1.log"]
    assert not any("(log:" in by[i].detail for i in (0, 1, 3, 4))
    assert [(base / f"w{i}" / rel).read_text(encoding="utf-8") for i in range(2)] == [original] * 2  # the module's own bytes back


def test_a_failed_baseline_keeps_its_mutants_from_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """_test with a module whose tests fail without a mutant, one whose tests never end and one
    whose tests pass: the first two FAIL, with pytest's word or the time limit, and keep their
    log; their mutants never run and say why; the passing module's do, each with a progress line.
    The workers' folders start empty, whatever a killed run left there."""
    files = {name: f".pytemplate/runner/{name}.py" for name in ("bad", "slow", "good")}
    originals = {rel: f"def {name}():\n    return 1\n".encode() for name, rel in files.items()}
    baselines: dict[str, tuple[int | None, str]] = {
        "bad": (1, "FAILED .pytemplate/tests/test_bad.py::test_x - assert 0\n1 failed in 0.10s\n"),
        "slow": (None, "...."),
        "good": (0, "3 passed in 0.10s\n"),
    }

    class FakeDriver(mutation.Driver):
        def __init__(self) -> None:  # no Cosmic Ray
            pass

        def ask(self, request: Mapping[str, Any]) -> dict[str, Any]:
            return {"code": f"def good():\n    return 2  # {'killed' if request['occurrence'] == 0 else 'survived'}\n"}

    def run(self: Runs, worker: Worker, tests: Sequence[str], timeout: float, junit: Path | None = None) -> tuple[int | None, str, float]:
        (name,) = [n for n in files if list(tests) == [f".pytemplate/tests/test_{n}.py"]]
        text = (worker.copy / files[name]).read_bytes()
        if text == originals[files[name]]:
            code, output = baselines[name]
        else:
            code, output = (1, "FAILED t.py::test_good\n1 failed in 0.10s\n") if b"# killed" in text else (0, "3 passed in 0.10s\n")
        worker.log.write_text(output, encoding="utf-8")
        return code, output, 1.0

    _fake_workers(monkeypatch)
    monkeypatch.setattr(Runs, "run", run)
    monkeypatch.setattr(mutation.ui, "QUIET", False)
    base = _write(tmp_path / "base", {"logs/cosmic-ray.log": "", "w0/stale.py": "a killed run's\n", "h0/.cache/x": "", "t1/tmp/x": ""})
    todo = [Mutant(files[name], "core/NumberReplacer", occurrence, 2, 11, 2, 12, name) for name, occurrence in (("bad", 0), ("slow", 0), ("good", 0), ("good", 1))]
    report = Report(list(todo), {}, base)
    tests = {f"runner.{name}": {f".pytemplate/tests/test_{name}.py": 1} for name in files}
    mutation._test(_cfg(), Options(None, 2, False), "uv", ROOT, base, tests, todo, originals, base / "snapshot", FakeDriver(), report)
    b = report.baselines
    assert (b["runner.bad"].status, b["runner.bad"].detail) == (mutation.FAIL, "FAILED .pytemplate/tests/test_bad.py::test_x - assert 0")
    assert b["runner.bad"].log == str(base / "logs" / "baseline-runner.bad.log") and Path(b["runner.bad"].log).read_text(encoding="utf-8") == baselines["bad"][1]
    assert (b["runner.slow"].status, b["runner.slow"].detail) == (mutation.FAIL, "no result after 3600 s")
    assert (b["runner.good"].status, b["runner.good"].log) == (mutation.PASS, "") and not (base / "logs" / "baseline-runner.good.log").exists()
    assert [(m.status, m.detail) for m in todo[:2]] == [(NOT_RUN, "its module's tests fail without a mutant")] * 2
    assert [m.status for m in todo[2:]] == [KILLED, SURVIVED]
    progress = [line for line in capsys.readouterr().err.splitlines() if line.startswith(("[1/2] ", "[2/2] "))]
    assert sorted(line[:6] for line in progress) == ["[1/2] ", "[2/2] "]
    assert sorted(line[6:] for line in progress) == [f"{status:<8} {files['good']}:2 NumberReplacer (1.0 s)" for status in ("killed", "survived")]
    assert not (base / "w0" / "stale.py").exists() and not (base / "h0" / ".cache").exists()
    assert all((base / d).is_dir() for d in ("h0", "h1", "t0/tmp", "t0/pytest", "t1/tmp", "t1/pytest"))
    assert (base / "h0" / "AppData" / "Local").is_dir() is mutation.IS_WINDOWS  # LOCALAPPDATA and APPDATA of the Windows workers


def test_a_baseline_that_never_ran_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One worker, and the first module's baseline stops the run (its uv went away): the error
    goes on, and the report says that no baseline ran, nor took any time."""
    files = {name: f".pytemplate/runner/{name}.py" for name in ("a", "b")}

    def run(self: Runs, worker: Worker, tests: Sequence[str], timeout: float, junit: Path | None = None) -> tuple[int | None, str, float]:
        raise PytError("uv went away", 3)

    _fake_workers(monkeypatch)
    monkeypatch.setattr(Runs, "run", run)
    base = tmp_path / "base"
    (base / "logs").mkdir(parents=True)
    todo = [Mutant(rel, "core/NumberReplacer", 0, 1, 4, 1, 5, None) for rel in files.values()]
    report = Report(list(todo), {}, base)
    tests = {f"runner.{name}": {f".pytemplate/tests/test_{name}.py": 1} for name in files}
    originals = {rel: b"x = 1\n" for rel in files.values()}
    with pytest.raises(PytError, match="uv went away"):
        mutation._test(_cfg(), Options(None, 1, False), "uv", ROOT, base, tests, todo, originals, base / "snapshot", None, report)  # type: ignore[arg-type]
    baselines = report.as_json(Options(None, 1, False))["baselines"]
    assert [(b["module"], b["status"], b["seconds"]) for b in baselines] == [("runner.a", NOT_RUN, 0.0), ("runner.b", NOT_RUN, 0.0)]


def test_run_locked_starts_clean_and_says_what_it_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The logs and the snapshot a killed run left go first; the mutants of a module no test file
    imports are untested (never given to the workers); the step line names the --diff base, and
    a run without mutants says why."""
    rel, lonely = ".pytemplate/runner/calc.py", ".pytemplate/runner/lonely.py"
    closed: list[bool] = []
    tested: list[list[Mutant]] = []
    listed: list[Mutant] = []

    class FakeDriver:
        def __init__(self, uv: str, cfg: Config, log: Path) -> None:  # no Cosmic Ray
            pass

        def ask(self, request: Mapping[str, Any]) -> dict[str, Any]:
            return {"version": "8.7.0"}

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(mutation, "Driver", FakeDriver)
    monkeypatch.setattr(mutation, "list_mutants", lambda driver, root, files, changed, snapshot: (list(listed), {}))
    monkeypatch.setattr(mutation, "_test", lambda *args: tested.append(list(args[6])))
    monkeypatch.setattr(mutation.ui, "QUIET", False)
    base = _write(tmp_path / "base", {"logs/old.log": "a killed run's\n", "snapshot/.pytemplate/runner/calc.py": "x = 0\n"})
    tests = {"runner.calc": {".pytemplate/tests/test_calc.py": 1}}

    def run(diff: str | None) -> Report:
        report = Report([], {}, base)
        mutation._run_locked(_cfg(), Options(diff, 1, False), "uv", ROOT, base, [rel, lonely], None, tests, report)
        return report

    listed[:] = [Mutant(rel, "core/AddNot", 0, 1, 4, 1, 5, None), Mutant(lonely, "core/AddNot", 0, 1, 4, 1, 5, None), Mutant(rel, "core/AddNot", 1, 2, 4, 2, 5, None)]
    report = run("HEAD")
    assert (base / "logs").is_dir() and not (base / "logs" / "old.log").exists() and not (base / "snapshot").exists()
    assert [(m.file, m.status, m.detail) for m in report.mutants] == [
        (rel, NOT_RUN, ""), (lonely, mutation.UNTESTED, "no test file imports this module"), (rel, NOT_RUN, ""),
    ]  # fmt: skip
    assert [[m.occurrence for m in todo] for todo in tested] == [[0, 1]] and closed == [True]
    assert report.note == "" and report.cosmic_ray == "8.7.0"
    assert f"==> selftest --mutation: 3 mutants in 2 files changed since HEAD, Cosmic Ray 8.7.0, in {base}" in capsys.readouterr().err
    listed.clear()
    for diff, note in (("HEAD", "the changed lines hold nothing Cosmic Ray mutates here"), (None, "no mutant in the runner")):
        report = run(diff)
        assert report.mutants == [] and report.note == note and len(tested) == 1
        step = [line for line in capsys.readouterr().err.splitlines() if "selftest --mutation:" in line]
        assert step == [f"==> selftest --mutation: 0 mutants in 2 files{' changed since HEAD' if diff else ''}, Cosmic Ray 8.7.0, in {base}"]


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
    assert data["ok"] is True and data["failed"] is False and data["score"] == 0.75 and data["options"] == {"diff": "origin/main", "jobs": 2}
    assert data["mutants"][3]["status"] == SURVIVED and data["mutants"][3]["module"] == "runner.m" and data["mutants"][3]["line"] == 13
    json.dumps(data)


def test_print_report_shows_the_survivors_with_their_change(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mutation.print_report(_report(tmp_path, [KILLED, SURVIVED, SURVIVED]))
    err = capsys.readouterr().err
    assert "score: 33.3% (1 of 3 judged mutants killed)" in err and "survived (2):" in err
    assert ".pytemplate/runner/m.py:11 in f: AddNot\n" in err and "      +    if not x12:" in err  # no detail: nothing after it
    mutation.print_report(Report([], {}, tmp_path, note="no runner line changed since origin/main"))
    assert "no mutant to test: no runner line changed since origin/main" in capsys.readouterr().err


def test_print_report_counts_each_file_the_timeouts_and_the_failed_baselines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = _report(tmp_path, [KILLED, TIMEOUT, SURVIVED, SURVIVED, mutation.ERROR])
    report.mutants[4].detail = "exit code 3: INTERNALERROR>"
    report.mutants.append(Mutant(".pytemplate/runner/cli.py", "core/NumberReplacer", 0, 7, 4, 7, 5, None, KILLED))
    report.baselines["runner.b"] = Baseline("runner.b", ["tb.py"], mutation.FAIL, 1.0, "FAILED tb.py::test_x - assert 0", "/base/logs/baseline-runner.b.log")
    mutation.print_report(report)
    lines = capsys.readouterr().err.splitlines()
    assert [line.split() for line in lines if line.startswith("  file") or line.startswith("  .pytemplate/")][:3] == [
        ["file", "killed", "timeout", "survived", "error"], [".pytemplate/runner/cli.py", "1", "0", "0", "0"],
        [".pytemplate/runner/m.py", "1", "1", "2", "1"],
    ]  # fmt: skip
    assert "  score: 60.0% (3 of 5 judged mutants killed), 6 mutants in 1m15s, 2 workers" in lines  # a timeout is a kill
    failed = [line for line in lines if "baseline FAILED" in line]
    assert failed == ["  baseline FAILED: the tests of runner.b fail without a mutant: FAILED tb.py::test_x - assert 0  (log: /base/logs/baseline-runner.b.log)"]
    assert "errors (1):" in lines and "  .pytemplate/runner/m.py:14 in f: AddNot  (exit code 3: INTERNALERROR>)" in lines


@pytest.mark.parametrize(
    ("statuses", "baseline", "interrupted", "code"),
    [
        ([KILLED, SURVIVED], mutation.PASS, False, 0),  # survivors are the report, not a failure
        ([KILLED, mutation.ERROR], mutation.PASS, False, 1),
        ([NOT_RUN], mutation.FAIL, False, 1),
        ([KILLED, NOT_RUN], mutation.PASS, True, 130),
        # interrupted (CI's timeout) after a baseline failed: 130, and the report's `failed` says it
        ([NOT_RUN], mutation.FAIL, True, 130),
    ],
)
def test_selftest_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], statuses: list[str], baseline: str, interrupted: bool, code: int
) -> None:
    report = _report(tmp_path, statuses, baseline)
    report.interrupted = interrupted
    report.kept = code != 0  # as run() decides it
    monkeypatch.setattr(mutation, "run", lambda *a: report)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    assert mutation.selftest(_cfg(), ["--json"]) == code
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert data["ok"] is (code == 0) and data["interrupted"] is interrupted
    assert data["failed"] is (baseline == mutation.FAIL or mutation.ERROR in statuses)
    assert (f"logs kept for inspection: {tmp_path / 'logs'}" in err) is report.kept


def test_selftest_final_line_names_untested_mutants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A mutant of a module no test file imports is never run and never judged: the closing line
    must say so (exit 0: survivors and untested ones are the report, not a failure), never the
    false 'every mutant judged'; with none untested it says 'every mutant judged'."""
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    monkeypatch.setattr(mutation, "run", lambda *a: _report(tmp_path, [KILLED, SURVIVED, mutation.UNTESTED, mutation.UNTESTED]))
    assert mutation.selftest(_cfg(), []) == 0
    err = capsys.readouterr().err
    assert "2 untested (no test file imports their module)" in err and "every mutant judged" not in err, err
    monkeypatch.setattr(mutation, "run", lambda *a: _report(tmp_path, [KILLED, SURVIVED]))
    assert mutation.selftest(_cfg(), []) == 0
    assert "every mutant judged" in capsys.readouterr().err


@pytest.mark.parametrize(("statuses", "said"), [([], "no mutant judged"), ([mutation.SKIPPED, mutation.SKIPPED], "no mutant judged (2 skipped)")])
def test_selftest_final_line_says_when_no_mutant_was_judged(
    statuses: list[str], said: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No runner line changed since --diff's BASE (no mutant), or every mutant skipped: the report
    said "no mutant to test" and the closing line then claimed "every mutant judged" (A10-08)."""
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    report = _report(tmp_path, statuses)
    report.note = "no runner line changed since HEAD"
    monkeypatch.setattr(mutation, "run", lambda *a: report)
    assert mutation.selftest(_cfg(), []) == 0
    err = capsys.readouterr().err
    assert f"selftest --mutation: {said}\n" in err and "every mutant judged" not in err, err


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
    assert data["ok"] is False and data["failed"] is True and data["error"] == "PytError: uv sync failed in the copy" and "survived (1):" in err


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


def test_an_interrupt_keeps_the_report_of_what_ran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Ctrl+C (or a SIGTERM, a SIGHUP) in the middle of the run: the report of what ran comes
    back and says interrupted and how long the run took, the workers' folders go, the logs stay."""
    base = tmp_path / "base"

    def interrupted(cfg: Config, opts: Options, uv: str, root: Path, base: Path, files: Any, changed: Any, tests: Any, report: Report) -> None:
        (base / "logs").mkdir()
        (base / "w0" / ".venv").mkdir(parents=True)
        report.mutants = [Mutant(".pytemplate/runner/calc.py", "core/AddNot", 0, 1, 4, 1, 5, None, KILLED)]
        raise KeyboardInterrupt

    monkeypatch.setattr(mutation, "_run_locked", interrupted)
    start = time.perf_counter()
    report = mutation.run(_cfg(), Options(None, 1, False), "uv", _toy(tmp_path), base)
    took = time.perf_counter() - start
    assert report.interrupted and report.kept and report.error is None and not report.failed()
    assert len(report.mutants) == 1 and sorted(p.name for p in base.iterdir()) == sorted([mutation.MARKER, "lock", "logs"])
    assert 0 <= report.seconds <= took


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
    before = signal.signal(signal.SIGINT, signal.default_int_handler)  # as a terminal has it (a background job ignores it)
    try:
        report = mutation.run(_cfg(), Options(None, 1, False), "uv", _toy(tmp_path), base)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL if before is None else before)
    assert removed == [True] and report.interrupted and report.kept and (base / "logs").is_dir()


@pytest.mark.parametrize("name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_deferred_interrupts_wait_for_the_block_and_put_the_handlers_back(name: str) -> None:
    """The handler before the block is one of the test's own: the one an earlier test left may be
    the default, which a restore that always put the default back matched (an earlier test that
    ran such a mutant through selftest() left it there)."""
    import signal

    number = getattr(signal, name, None)
    if number is None:
        pytest.skip(f"no {name} here")

    def mine(signum: int, frame: Any) -> None:
        raise AssertionError(f"{name} reached the handler of before the block")

    before = signal.signal(number, mine)
    try:
        with mutation.deferred_interrupts() as got:
            signal.raise_signal(number)
            time.sleep(0.01)  # a signal's Python handler runs between two bytecodes
        assert got == [number] and signal.getsignal(number) is mine
    finally:
        signal.signal(number, signal.SIG_DFL if before is None else before)


@pytest.mark.parametrize("name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_deferred_interrupts_leave_an_ignored_signal_ignored(name: str) -> None:
    """A run started with a signal ignored (SIGHUP under nohup, which uv hands the runner; SIGINT
    in a background job) goes on through it: recorded during the cleanup or the report, a
    hang-up made that run `interrupted` (130)."""
    import signal

    number = getattr(signal, name, None)
    if number is None:
        pytest.skip(f"no {name} here")
    before = signal.signal(number, signal.SIG_IGN)
    try:
        with mutation.deferred_interrupts() as got:
            assert signal.getsignal(number) is signal.SIG_IGN
            signal.raise_signal(number)
            time.sleep(0.01)
        assert got == [] and signal.getsignal(number) is signal.SIG_IGN
    finally:
        signal.signal(number, signal.SIG_DFL if before is None else before)


def test_nothing_changed_starts_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutation, "changed_lines", lambda *a: {".pytemplate/runner/not_there.py": {1}})
    monkeypatch.setattr(mutation, "Driver", None)  # never started
    base = tmp_path / "base"
    report = mutation.run(_cfg(), Options("HEAD", 1, False), "uv", ROOT, base)
    assert report.mutants == [] and report.note == "no runner line changed since HEAD" and not base.exists()
    assert report.kept is False and report.seconds == 0.0 and report.workers == 0  # no logs to point at, no time, no worker


def test_a_cleanup_that_fails_is_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A folder in use (Windows: an antivirus scan, a process a test left) never loses the report."""
    base = tmp_path / "base"
    for d in ("w0", "logs"):
        (base / d).mkdir(parents=True)

    def in_use(path: Path) -> None:
        raise PermissionError(f"in use: {path.name}")

    monkeypatch.setattr(mutation, "rmtree", in_use)
    mutation._remove_workers(base)
    mutation._remove_logs(base)
    err = capsys.readouterr().err
    assert f"warning: could not remove the workers' folders in {base}: in use: w0" in err
    assert f"warning: could not remove {base / 'logs'}: in use: logs" in err


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


def test_a_module_with_a_bom_is_mutated_like_any_other(cosmic_ray: Any, tmp_path: Path) -> None:
    """A runner module saved with a UTF-8 BOM (a Windows editor, PowerShell 5.1), which Python
    imports: list_mutants decoded it with the BOM, and ast refused U+FEFF, so the whole run
    stopped with "is no Python the runner can read" (A10-05). Its mutants are those of the same
    module without the BOM (Cosmic Ray, through parso, leaves the BOM out of line 1's columns, as
    ast does without it), each made and judged alike, the BOM kept."""
    rel = f"{mutation.SCOPE}/calc.py"
    body = "x = 1 + 2\n" + PASS_THROUGH
    found: dict[bool, list[tuple[Any, ...]]] = {}
    for bom in (False, True):
        root = tmp_path / ("bom" if bom else "plain")
        (root / rel).parent.mkdir(parents=True)
        (root / rel).write_bytes((b"\xef\xbb\xbf" if bom else b"") + body.encode("utf-8"))
        mutants, originals = mutation.list_mutants(cosmic_ray, root, [rel], None, root / "snapshot")
        made = []
        for m in mutants:
            status, _, code = mutation.make_mutant(m, originals[rel], root / "snapshot", cosmic_ray)
            assert code is None or code.startswith("﻿") == bom, (m, code)
            made.append((m.operator, m.occurrence, m.line, m.column, m.end_line, m.end_column, status, code and code.removeprefix("﻿")))
        found[bom] = made
    assert found[True] == found[False] and len(found[False]) > 5
    assert NOT_RUN in {status for *_, status, _ in found[True]}, found[True]  # mutants to test, not "no valid Python"


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
    toy = _write(tmp_path / "toy", {".pytemplate/runner/__init__.py": "", ".pytemplate/runner/calc.py": CALC, ".pytemplate/tests/test_calc.py": TEST_CALC, **SUITE_INI})
    for name in ("pyproject.toml", "uv.lock", ".python-version", ".gitignore"):
        shutil.copyfile(ROOT / name, toy / name)  # uv sync --locked of the workers: the project's own lock
    # A project with local libraries (CLAUDE.md 10: ./pyt add ./libs/x, ./wheels/x.whl or ../x)
    # names them from the project in pyproject.toml and uv.lock. A real run's workers get those
    # inside it from make_copy (git ls-files) and name those outside it from their own folder: the
    # toy gets the first, a folder or a file (a local wheel was left out, and every mutant stayed
    # "not run"), and names the others from its folder. The template's own lock has none.
    import tomllib

    presets.rebase_local_sources(ROOT, toy)
    real_root = os.path.realpath(ROOT)
    for pkg in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8")).get("package", []):
        source = pkg.get("source") or {}
        rel = next((source[k] for k in ("directory", "editable", "virtual", "path") if isinstance(source.get(k), str)), None)
        if rel is None or os.path.isabs(rel):
            continue  # a package of an index, or a library named by its absolute path
        local = os.path.normpath(os.path.join(real_root, rel))
        if not local.startswith(real_root.rstrip(os.sep) + os.sep):
            continue  # the project itself, or outside it (named from the toy above)
        if os.path.isdir(local):
            shutil.copytree(
                local, toy / rel, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".venv*", ".build", "dist", "__pycache__", ".git", ".flet"),
            )  # fmt: skip
        elif os.path.isfile(local):
            (toy / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, toy / rel)
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
