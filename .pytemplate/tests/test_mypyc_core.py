"""Tests of the mypyc area: the PyPy 3.11 precheck, lintc, imports, the mypyc stage
(mypyc.py + tools/mypyc_build.py), the compile-module sections of the generated type-checker
configs and the wheel method.

Most tests run in-process with `proc.run` / `envs.uv` replaced by fakes. The few that need the
real tools (mypy, mypyc and a C compiler in .venv) skip cleanly without them.
"""

from __future__ import annotations

import ast
import configparser
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import tomllib
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_env, cmd_mode, config, envs, imports, lintc, mypyc, proc, render, ui  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import ENV_SUFFIX, PRESETS, ROOT, TOOLS, venv_python  # noqa: E402
from runner.ui import DeployError  # noqa: E402

TOOL_PYTHON = venv_python(ROOT / f".venv{ENV_SUFFIX}")
needs_venv = pytest.mark.skipif(not TOOL_PYTHON.is_file(), reason="needs .venv (./deploy setup)")
EXT = importlib.machinery.EXTENSION_SUFFIXES[0]  # this interpreter's own extension suffix
LINUX_EXT = ".cpython-314-x86_64-linux-gnu.so"


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


def _has_mypyc() -> bool:
    try:
        return importlib.util.find_spec("mypyc.build") is not None  # selftest runs in .venv (mypy)
    except ImportError:
        return False


def _has_c_compiler() -> bool:
    if os.name == "nt":
        return shutil.which("cl") is not None or proc.vs_installer_dir() is not None
    cc = (sysconfig.get_config_var("CC") or "cc").split()[0]
    return shutil.which(cc) is not None or shutil.which("cc") is not None


needs_compiler = pytest.mark.skipif(
    not (_has_mypyc() and _has_c_compiler()), reason="needs mypyc (run through ./deploy selftest) and a C compiler"
)


@pytest.fixture(autouse=True)
def _quiet_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """No leftovers from other tests: real runs, normal verbosity."""
    monkeypatch.setattr(proc, "DRY_RUN", False)
    monkeypatch.setattr(ui, "VERBOSE", False)
    monkeypatch.setattr(ui, "QUIET", False)


def _done(argv: list[str], code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


# --- 1. the PyPy precheck (cmd_mode._precheck_py311) ------------------------------------------------


class FakeTools:
    """Replaces envs.sync / envs.uv in cmd_mode: records the calls, answers per tool."""

    def __init__(self, *, ruff: int = 0, mypy: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.ruff = ruff
        self.mypy = mypy or {}

    def sync(self, env: envs.PyEnv, **_kw: Any) -> None:
        self.calls.append(["sync", str(env.dir)])

    def uv(self, _env: envs.PyEnv, args: list[Any], **_kw: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in args]
        self.calls.append(argv)
        if "ruff" in argv:
            return _done(argv, self.ruff)
        version = argv[argv.index("--python-version") + 1]
        code, out = self.mypy.get(version, (0, ""))
        return _done(argv, code, out)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeTools:
        monkeypatch.setattr(cmd_mode.envs, "sync", self.sync)
        monkeypatch.setattr(cmd_mode.envs, "uv", self.uv)
        return self


@pytest.fixture
def fake_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tools environment whose interpreter 'exists' (the dry-run precheck needs one)."""
    venv = tmp_path / "venv"
    python = venv_python(venv)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    real = envs.tool_env

    def tool_env(cfg: Config) -> envs.PyEnv:
        env = real(cfg)
        return envs.PyEnv(env.key, venv, env.request, env.preference)

    monkeypatch.setattr(cmd_mode.envs, "tool_env", tool_env)
    return venv


def test_precheck_syncs_first_and_never_reads_the_project_mypy_ini(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = FakeTools().install(monkeypatch)
    cfg = make({})
    cmd_mode._precheck_py311(cfg)
    assert tools.calls[0] == ["sync", str(envs.tool_env(cfg).dir)]  # before any tool runs
    assert len([c for c in tools.calls if c[0] == "sync"]) == 1
    runs = tools.calls[1:]
    assert [c[:3] for c in runs] == [["run", "--locked", "--no-sync"]] * 3  # ruff, mypy 3.11, mypy cpython
    mypy = [c for c in runs if "mypy" in c]
    assert [c[c.index("--python-version") + 1] for c in mypy] == ["3.11", cfg.python.cpython]
    for argv in mypy:
        # an empty --config-file: the root .mypy.ini (ignore_errors = True under "off") is never read
        assert "--config-file=" in argv and "--check-untyped-defs" in argv
        assert not any(a.endswith((".ini", ".toml")) for a in argv)


def test_precheck_checks_the_python_of_the_pinned_pypy(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """It hard-coded 3.11: a project that moved to pypy@3.12.x (README's plan once raylib ships PyPy
    8 wheels) could not enable PyPy with 3.12 code (PEP 695 generics)."""
    tools = FakeTools().install(monkeypatch)
    cfg = make({"python": {"pypy": "pypy@3.12.14"}})
    cmd_mode._precheck_py311(cfg)
    ruff = next(c for c in tools.calls if "ruff" in c)
    assert ruff[ruff.index("--target-version") + 1] == "py312"
    mypy = [c for c in tools.calls if "mypy" in c]
    assert [c[c.index("--python-version") + 1] for c in mypy] == ["3.12", cfg.python.cpython]
    err = capsys.readouterr().err
    assert "valid on Python 3.12 (required by PyPy)" in err and "ok the code is valid on Python 3.12" in err
    FakeTools(ruff=1).install(monkeypatch)
    with pytest.raises(DeployError, match=r"syntax that does not exist in Python 3\.12"):
        cmd_mode._precheck_py311(cfg)


def test_precheck_dry_run_never_syncs(monkeypatch: pytest.MonkeyPatch, fake_venv: Path) -> None:
    tools = FakeTools().install(monkeypatch)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    cmd_mode._precheck_py311(make({}))
    assert tools.calls and all(c[0] != "sync" for c in tools.calls)
    assert all(c[:3] == ["run", "--locked", "--no-sync"] for c in tools.calls)


def test_precheck_dry_run_without_venv_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tools = FakeTools().install(monkeypatch)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    monkeypatch.setattr(cmd_mode.envs, "tool_env", lambda cfg: envs.PyEnv("cpython", tmp_path / "none", "3.14", "only-managed"))
    cmd_mode._precheck_py311(make({}))
    assert tools.calls == []


@pytest.mark.parametrize("dry", [False, True])
def test_precheck_ruff_that_cannot_run_is_not_a_syntax_error(monkeypatch: pytest.MonkeyPatch, fake_venv: Path, dry: bool) -> None:
    FakeTools(ruff=2).install(monkeypatch)
    monkeypatch.setattr(proc, "DRY_RUN", dry)
    with pytest.raises(DeployError, match=r"could not run ruff .*exit code 2") as err:
        cmd_mode._precheck_py311(make({}))
    assert "syntax" not in str(err.value)


def test_precheck_ruff_findings_are_syntax_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = FakeTools(ruff=1).install(monkeypatch)
    with pytest.raises(DeployError, match="syntax that does not exist in Python 3.11"):
        cmd_mode._precheck_py311(make({}))
    assert not [c for c in tools.calls if "mypy" in c]  # stops at the syntax step


@pytest.mark.parametrize("version", ["3.11", "3.14"])
def test_precheck_mypy_that_aborts_never_passes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], version: str) -> None:
    same = 'tests/__init__.py: error: Duplicate module named "tests"'
    FakeTools(mypy={version: (2, same)}).install(monkeypatch)
    with pytest.raises(DeployError, match=f"mypy could not check the code as Python {version} .exit code 2"):
        cmd_mode._precheck_py311(make({}))
    assert "Duplicate module" in capsys.readouterr().err  # the reason is shown


def test_precheck_reports_only_the_errors_new_at_311(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    both = "src/myapp/a.py:3: error: Incompatible types in assignment"
    new = 'src/myapp/b.py:1: error: Module "typing" has no attribute "override"  [attr-defined]'
    FakeTools(mypy={"3.11": (1, f"{both}\n{new}\n"), "3.14": (1, both + "\n")}).install(monkeypatch)
    with pytest.raises(DeployError, match="APIs that do not exist in Python 3.11"):
        cmd_mode._precheck_py311(make({}))
    err = capsys.readouterr().err
    assert f"error: {new}" in err and "Incompatible types" not in err
    # the same errors at both versions: nothing new, the precheck passes
    FakeTools(mypy={"3.11": (1, both), "3.14": (1, both)}).install(monkeypatch)
    cmd_mode._precheck_py311(make({}))
    assert "ok the code is valid on Python 3.11" in capsys.readouterr().err


def test_precheck_checks_only_the_code_folders_that_hold_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """tests/ left with only __pycache__ (the tests removed with `git rm`): mypy stopped with "There
    are no .py[i] files in directory 'tests'" and PyPy could not be enabled, while ./deploy check
    passed (.mypy.ini's `files` leaves such a folder out, render._holds_python)."""
    src, tests = tmp_path / "src", tmp_path / "tests"
    (src / "app").mkdir(parents=True)
    (src / "app" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tests / "__pycache__").mkdir(parents=True)
    (tests / "__pycache__" / "test_a.cpython-314.pyc").write_bytes(b"")
    monkeypatch.setattr(cmd_mode, "code_dirs", lambda: [str(src), str(tests)])
    tools = FakeTools().install(monkeypatch)
    cmd_mode._precheck_py311(make({}))
    runs = [c for c in tools.calls if c[0] != "sync"]
    assert len(runs) == 3 and all(c[-1] == str(src) and str(tests) not in c for c in runs), runs
    # no Python code at all: nothing to check (ruff without a path would check the whole project)
    shutil.rmtree(src)
    tools = FakeTools().install(monkeypatch)
    cmd_mode._precheck_py311(make({}))
    assert [c for c in tools.calls if c[0] != "sync"] == []
    assert "no Python code in src/ or tests/: nothing to check for Python 3.11" in capsys.readouterr().err


@needs_venv
def test_precheck_real_mypy_with_a_test_folder_without_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src, tests = tmp_path / "src", tmp_path / "tests"
    src.mkdir()
    (src / "a.py").write_text("x: int = 1\n", encoding="utf-8")
    (tests / "__pycache__").mkdir(parents=True)
    monkeypatch.setattr(cmd_mode, "code_dirs", lambda: [str(src), str(tests)])
    monkeypatch.setattr(proc, "DRY_RUN", True)  # uv run --no-sync: .venv is never touched
    cmd_mode._precheck_py311(make({}))
    assert "ok the code is valid on Python 3.11" in capsys.readouterr().err


NEW_APIS = (
    "import itertools\n"
    "from typing import override\n\n\n"
    "class Base:\n    def f(self) -> int:\n        return 1\n\n\n"
    "class Child(Base):\n    @override\n    def f(self) -> int:\n        return 2\n\n\n"
    "def pairs(xs):\n    return list(itertools.batched(xs, 2))\n"  # unannotated: needs --check-untyped-defs
)


@needs_venv
def test_precheck_real_mypy_catches_new_apis_with_the_off_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The project's own .mypy.ini is the "off" profile (ignore_errors = True): it must not matter
    assert "ignore_errors = True" in (ROOT / ".mypy.ini").read_text(encoding="utf-8")
    code = tmp_path / "code"
    code.mkdir()
    (code / "newapi.py").write_text(NEW_APIS, encoding="utf-8")
    monkeypatch.setattr(cmd_mode, "code_dirs", lambda: [str(code)])
    monkeypatch.setattr(proc, "DRY_RUN", True)  # uv run --no-sync: .venv is never touched
    with pytest.raises(DeployError, match="APIs that do not exist in Python 3.11"):
        cmd_mode._precheck_py311(make({}))
    err = capsys.readouterr().err
    assert 'Module "typing" has no attribute "override"' in err
    assert err.count('has no attribute "batched"') == 1  # inside an unannotated function
    (code / "newapi.py").write_text(
        NEW_APIS.replace("from typing import override", "from typing_extensions import override").replace(
            "itertools.batched(xs, 2)", "zip(xs, xs)"
        ),
        encoding="utf-8",
    )
    cmd_mode._precheck_py311(make({}))
    assert "ok the code is valid on Python 3.11" in capsys.readouterr().err


# --- 2. lintc: the rules for compiled modules ----------------------------------------------------


def _lint(tmp_path: Path, source: str | bytes, data: dict[str, Any] | None = None) -> list[lintc.Finding]:
    mod = tmp_path / "m.py"
    if isinstance(source, bytes):
        mod.write_bytes(source)
    else:
        mod.write_text(source, encoding="utf-8")
    return lintc.lint_file(make(data or {}), mod)


@pytest.mark.parametrize(
    ("source", "line"),
    [
        (b"def ok() -> None: ...\n\n\ndef f(:\n", 4),  # a syntax error
        (b"def f() -> str:\n    return 'a' +\n", 2),
        (b"x = 1\x00\n", 1),  # a NUL byte
        (b"x = '\xff'\n", 1),  # not UTF-8
    ],
)
def test_lintc_reports_an_unparsable_file(tmp_path: Path, source: bytes, line: int) -> None:
    found = _lint(tmp_path, source)
    assert [f.line for f in found] == [line]
    assert "cannot parse" in found[0].message and "skipped this file" in found[0].message
    assert f"Python {sys.version_info.major}.{sys.version_info.minor}" in found[0].message
    # the `path:N: msg` shape that RULES_RE and the Neovim parser read
    assert str(lintc.Finding(ROOT / "src" / "m.py", line, found[0].message)).startswith(f"src/m.py:{line}: cannot parse")


def test_lintc_lint_sorts_and_survives_an_unparsable_file(tmp_path: Path) -> None:
    good, bad = tmp_path / "a.py", tmp_path / "b.py"
    good.write_text("from functools import cache\n\n@cache\nclass A: ...\n", encoding="utf-8")
    bad.write_text("def f(:\n", encoding="utf-8")
    found = lintc.lint(make({}), [bad, good])
    assert [(f.path.name, f.line) for f in found] == [("a.py", 4), ("b.py", 1)]  # the `class` line


# Every case was compiled with the locked mypyc (2.3.1) to check whether the class is native
NATIVE_CASES = [
    ("import dataclasses\n@dataclasses.dataclass(frozen=True)", True),
    ("from dataclasses import dataclass\n@dataclass", True),
    ("from dataclasses import dataclass as dc\n@dc", True),
    ("from dataclasses import *\n@dataclass", True),
    ("import attr\n@attr.s(auto_attribs=True)", True),
    ("from attr import s as ss\n@ss(auto_attribs=True)", True),
    ("import attr\n@attr.attrs(auto_attribs=True)", True),
    ("from typing import final\n@final", True),
    ("import typing as t\n@t.final", True),
    ("import typing_extensions\n@typing_extensions.final", True),
    ("from mypy_extensions import trait\n@trait", True),
    ("from mypy_extensions import mypyc_attr\n@mypyc_attr(allow_interpreted_subclasses=True)", True),
    ("from mypy_extensions import mypyc_attr\n@mypyc_attr(native_class=False)", True),  # explicitly non-native: fine
    ("import attrs\n@attrs.define", False),
    ("from attrs import define\n@define", False),
    ("from attrs import frozen\n@frozen", False),
    ("from attrs import mutable\n@mutable", False),
    ("import attr\n@attr.define", False),
    ("import attr\n@attr.dataclass", False),
    ("import attr\n@attr.attributes(auto_attribs=True)", False),
    ("from pydantic.dataclasses import dataclass\n@dataclass", False),
    ("from .util import final\n@final", False),  # a local `final` is not typing.final
    ("from functools import total_ordering\n@total_ordering", False),
    ("from functools import cache\n@cache", False),
    ("registry = {}\n@registry.get('x')", False),
]


@pytest.mark.parametrize(("head", "native"), NATIVE_CASES)
def test_lintc_native_decorators_match_mypyc(tmp_path: Path, head: str, native: bool) -> None:
    found = _lint(tmp_path, head + "\nclass P:\n    x: int = 0\n")
    assert (found == []) is native, [f.message for f in found]
    if not native:
        assert len(found) == 1 and "class 'P' uses @" in found[0].message and "regular (slow) Python class" in found[0].message


SCOPED_CASES = [
    # A function's own import never decides a module-level decorator (the last import of the
    # whole file won: a false error under the mypyc profile, or a slow class not reported)
    ("from dataclasses import dataclass\n\n\ndef f():\n    from mylib import dataclass\n    return dataclass\n\n\n@dataclass\nclass P:\n    x: int = 0\n", True),
    ("from attrs import define as dataclass\n\n\ndef f():\n    from dataclasses import dataclass\n    return dataclass\n\n\n@dataclass\nclass P:\n    x: int = 0\n", False),
    ("from dataclasses import dataclass\n\n\nclass C:\n    from mylib import dataclass\n\n\n@dataclass\nclass P:\n    x: int = 0\n", True),
    # module level includes the blocks of if/try/with
    ("try:\n    from dataclasses import dataclass\nexcept ImportError:\n    raise\n\n\n@dataclass\nclass P:\n    x: int = 0\n", True),
    ("import sys\nif sys.version_info >= (3, 11):\n    from attrs import define as dataclass\n\n\n@dataclass\nclass P:\n    x: int = 0\n", False),
]


@pytest.mark.parametrize(("source", "native"), SCOPED_CASES)
def test_lintc_resolves_decorators_in_the_scope_of_the_class(tmp_path: Path, source: str, native: bool) -> None:
    found = [f for f in _lint(tmp_path, source) if "class 'P'" in f.message]
    assert (found == []) is native, [f.message for f in found]


def test_lintc_a_class_in_a_function_sees_that_functions_imports(tmp_path: Path) -> None:
    source = "def f():\n    from dataclasses import dataclass\n\n    @dataclass\n    class P:\n        x: int = 0\n\n    return P\n"
    found = _lint(tmp_path, source)
    assert [("uses @" in f.message, "inside a function" in f.message) for f in found] == [(False, True)]


@pytest.mark.parametrize(("last", "native"), [("from dataclasses import dataclass", True), ("from attrs import define as dataclass", False)])
def test_lintc_reads_a_long_elif_chain(tmp_path: Path, last: str, native: bool) -> None:
    """The blocks of a scope were walked recursively, one level per `elif`: a generated dispatch
    table of ~1000 branches crashed `check`, `build` and the pre-commit hook with an internal
    runner error (RecursionError). The import in the chain's last `else` still decides."""
    branches = "".join(f"elif sys.argv[0] == '{i}':\n    pass\n" for i in range(1, 3000))
    source = f"import sys\n\nif sys.argv[0] == '0':\n    pass\n{branches}else:\n    {last}\n\n\n@dataclass\nclass P:\n    x: int = 0\n"
    found = _lint(tmp_path, source)
    assert (found == []) is native, [f.message for f in found]


def test_lintc_reports_a_source_nested_too_deeply_for_the_compiler(tmp_path: Path) -> None:
    # ast.parse itself raises RecursionError ("during ast construction", "Stack overflow ...
    # during compilation"): one finding, like a syntax error, never an internal error
    found = _lint(tmp_path, "x = " + " + ".join(["1"] * 100_000) + "\n")
    assert len(found) == 1 and "cannot parse" in found[0].message and "skipped this file" in found[0].message


# mypyc 2.3.1 compiles these as regular Python classes without a word (is_implicit_extension_class:
# a metaclass other than ABCMeta, TypedDict, NamedTuple); every case was compiled to check it
METACLASS_CASES = [
    ("from enum import Enum\n\n\nclass P(Enum):\n    A = 1\n", "is an Enum (metaclass EnumMeta)"),
    ("import enum\n\n\nclass P(enum.IntFlag):\n    A = 1\n", "is an Enum (metaclass EnumMeta)"),
    ("from enum import *\n\n\nclass P(StrEnum):\n    A = 'a'\n", "is an Enum (metaclass EnumMeta)"),
    ("from typing import NamedTuple\n\n\nclass P(NamedTuple):\n    x: int\n", "is a NamedTuple"),
    ("from typing_extensions import TypedDict\n\n\nclass P(TypedDict):\n    x: int\n", "is a TypedDict"),
    ("from .meta import Meta\n\n\nclass P(metaclass=Meta):\n    x: int = 0\n", "has the metaclass Meta"),
    ("from .meta import Meta\n\n\nclass B(metaclass=Meta):\n    x: int = 0\n\n\nclass P(B):\n    y: int = 0\n", "inherits from 'B', which has the metaclass Meta"),
    ("from abc import ABC\n\n\nclass P(ABC):\n    x: int = 0\n", None),
    ("import abc\n\n\nclass P(metaclass=abc.ABCMeta):\n    x: int = 0\n", None),
    ("from typing import Generic, TypeVar\n\nT = TypeVar('T')\n\n\nclass P(Generic[T]):\n    x: int = 0\n", None),
    ("from enum import Enum\nfrom mypy_extensions import mypyc_attr\n\n\n@mypyc_attr(native_class=False)\nclass P(Enum):\n    A = 1\n", None),
    ("from .mylib import Enum\n\n\nclass P(Enum):\n    A = 1\n", None),  # not the stdlib's Enum
]


@pytest.mark.parametrize(("source", "kind"), METACLASS_CASES)
def test_lintc_flags_classes_mypyc_compiles_as_python_classes_for_their_metaclass(tmp_path: Path, source: str, kind: str | None) -> None:
    """Only the decorators were checked: an Enum, a NamedTuple, a TypedDict or a class with its
    own metaclass silently became a slow Python class under mypyc, with no finding."""
    found = [f for f in _lint(tmp_path, source) if "class 'P'" in f.message]
    expected = [] if kind is None else [f"class 'P' {kind}: mypyc compiles it as a regular (slow) Python class"]
    assert [f.message.split("Python class")[0] + "Python class" for f in found] == expected
    # an Enum, a NamedTuple or a TypedDict works compiled, only slower: a note that never blocks;
    # a metaclass of the user's own is the surprise the rule is for, like a foreign decorator
    assert [f.note for f in found] == ([] if kind is None else ["has the metaclass" not in kind])


@needs_venv
def test_lintc_native_metaclasses_follow_the_locked_mypyc() -> None:
    """A mypy bump that changes what mypyc accepts as the metaclass of a native class, or drops
    its TypedDict and NamedTuple exceptions, must fail selftest."""
    code = (
        "import importlib.util, pathlib\n"
        "origin = pathlib.Path(importlib.util.find_spec('mypyc.irbuild.util').origin)\n"
        "print((origin.parent / 'util.py').read_text(encoding='utf-8'))\n"
    )
    text = subprocess.run([str(TOOL_PYTHON), "-I", "-c", code], capture_output=True, text=True, check=True).stdout
    function = next(n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef) and n.name == "is_implicit_extension_class")
    tuples = [
        {e.value for e in n.comparators[0].elts if isinstance(e, ast.Constant)}
        for n in ast.walk(function)
        if isinstance(n, ast.Compare) and isinstance(n.ops[0], ast.NotIn) and isinstance(n.comparators[0], ast.Tuple)
    ]
    assert tuples == [set(lintc.NATIVE_METACLASSES)]
    source = ast.unparse(function)
    assert "typeddict_type" in source and "is_named_tuple" in source


@needs_venv
def test_lintc_native_decorators_follow_the_locked_mypyc() -> None:
    """A mypy bump that changes mypyc's list of native decorators must fail selftest."""
    code = (  # mypy is itself compiled: read util.py, shipped next to the extension
        "import importlib.util, pathlib\n"
        "from mypyc.irbuild import util\n"
        "from mypy.types import FINAL_DECORATOR_NAMES\n"
        "names = {*util.DATACLASS_DECORATORS, *FINAL_DECORATOR_NAMES}\n"
        "origin = pathlib.Path(importlib.util.find_spec('mypyc.irbuild.util').origin)\n"
        "text = (origin.parent / 'util.py').read_text(encoding='utf-8')\n"
        "names |= {n for n in ('mypy_extensions.trait', 'mypy_extensions.mypyc_attr') if repr(n).replace(\"'\", '\"') in text}\n"
        "print(' '.join(sorted(names)))\n"
    )
    r = subprocess.run([str(TOOL_PYTHON), "-I", "-c", code], capture_output=True, text=True, check=True)
    assert set(r.stdout.split()) == set(lintc.NATIVE_CLASS_DECORATORS)


MODULE_LEVEL_FILE = (
    "from pathlib import Path\n"
    "HERE = __file__\n"  # 2
    "DATA = Path(__file__).parent / 'data'\n"  # 3
    "def inside() -> str:\n"
    "    return __file__\n"  # a function body: runs later, with the real path
    "def default(p: str = __file__) -> str:\n"  # 6: a default value runs at import
    "    return p\n"
    "class Holder:\n"
    "    where = __file__\n"  # 9: a class body runs at import
    "    def get(self) -> str:\n"
    "        return __file__\n"
    "later = lambda: __file__\n"  # a lambda body: later
)


@pytest.mark.parametrize("modules", [["myapp.core"], ["myapp.core.bench"], ["solo", "myapp.core"], ["myapp"]])
def test_lintc_allows_module_level_file_with_a_shared_lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, modules: list[str]) -> None:
    (tmp_path / "src" / "myapp").mkdir(parents=True)  # a package, whatever this project's is called
    monkeypatch.setattr(lintc, "SRC", tmp_path / "src")
    monkeypatch.setattr(config, "SRC", tmp_path / "src")
    found = _lint(tmp_path, MODULE_LEVEL_FILE, {"compile": {"modules": modules}})
    assert not [f for f in found if "__file__" in f.message]


def test_lintc_flags_module_level_file_for_a_single_top_level_module(tmp_path: Path) -> None:
    found = _lint(tmp_path, MODULE_LEVEL_FILE, {"compile": {"modules": ["solo"]}})
    assert sorted(f.line for f in found if "__file__" in f.message) == [2, 3, 6, 9]
    assert all("relative path" in f.message and "inside a function" in f.message for f in found)


def test_lintc_single_top_level_package_is_not_relative(src_tree: Path) -> None:
    (src_tree / "solo").mkdir()  # a package: its modules have dotted names, mypyc builds a shared lib
    assert not lintc.relative_file_at_import(make({"compile": {"modules": ["solo"]}}))
    assert lintc.relative_file_at_import(make({"compile": {"modules": ["other"]}}))


def test_lintc_flags_a_lone_module_next_to_a_leftover_folder(src_tree: Path) -> None:
    """A package turned into a module leaves solo/__pycache__/ behind: Python (and mypyc, through
    config.compiled_paths) takes solo.py, alone, with no shared lib, so the rule must still fire."""
    _project(src_tree, {"solo.py": MODULE_LEVEL_FILE, "solo/__pycache__/solo.cpython-314.pyc": b""})
    cfg = make({"compile": {"modules": ["solo"]}})
    assert config.compiled_paths(cfg) == ["solo.py"]
    assert lintc.relative_file_at_import(cfg)
    found = lintc.lint(cfg, mypyc.compiled_sources(cfg))
    assert sorted(f.line for f in found if "__file__" in f.message) == [2, 3, 6, 9]
    # a real package of that name wins over solo.py, as in Python: dotted modules, a shared lib
    _project(src_tree, {"solo/__init__.py": "", "solo/m.py": MODULE_LEVEL_FILE})
    assert not lintc.relative_file_at_import(cfg)


@pytest.mark.parametrize(
    ("source", "forbid", "expected"),
    [
        # one finding per class, from its nearest enclosing class or function
        ("class A:\n    class B:\n        class C: ...\n", [], [(2, "nested class 'B'"), (3, "nested class 'C'")]),
        ("class K:\n    def m(self) -> None:\n        class L: ...\n", [], [(3, "class 'L' defined inside a function")]),
        ("def f() -> None:\n    def g() -> None:\n        class L: ...\n", [], [(3, "class 'L' defined inside a function")]),
        ("class A:\n    if True:\n        class B: ...\n", [], [(3, "nested class 'B'")]),
        ("async def f() -> None:\n    class L: ...\n", [], [(2, "class 'L' defined inside a function")]),
        # forbid_imports: dotted names through `from a import b`; relative imports are the app's modules
        ("import flet as ft\n", ["flet"], [(1, "import of 'flet' is forbidden")]),
        ("import flet.controls\n", ["flet"], [(1, "import of 'flet.controls' is forbidden")]),
        ("from flet import Page, Text\n", ["flet"], [(1, "import of 'flet' is forbidden")]),  # once per statement
        ("from a import b\n", ["a.b"], [(1, "import of 'a.b' is forbidden")]),
        ("from a.b import c\n", ["a.b"], [(1, "import of 'a.b' is forbidden")]),
        ("from a import c\n", ["a.b"], []),
        ("import fletcher\n", ["flet"], []),
        ("from .flet import helper\n", ["flet"], []),
        ("from . import flet\n", ["flet"], []),
        ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import flet\n", ["flet"], []),
        ("import typing\nif typing.TYPE_CHECKING:\n    pass\nelse:\n    import flet\n", ["flet"], [(5, "import of 'flet'")]),
        # `if __name__ == "__main__"` in either order; other comparisons are fine
        ("if __name__ != 'x':\n    pass\n", [], []),
        ("if __name__ == 'x':\n    pass\n", [], []),
        ("if '__main__' == __name__:\n    pass\n", [], [(1, "__main__")]),
        ("if __name__ == '__main__':\n    pass\n", [], [(1, "__main__")]),
        ("def f() -> None:\n    if __name__ == '__main__':\n        pass\n", [], []),
    ],
)
def test_lintc_reports_each_problem_once(tmp_path: Path, source: str, forbid: list[str], expected: list[tuple[int, str]]) -> None:
    found = _lint(tmp_path, source, {"compile": {"forbid_imports": forbid}})
    got = sorted((f.line, f.message) for f in found)
    assert len(got) == len(expected), got
    for (line, msg), (want_line, want) in zip(got, sorted(expected), strict=True):
        assert line == want_line and want in msg, got


@pytest.mark.skipif(not hasattr(ast, "TemplateStr"), reason="t-strings need Python 3.14")
def test_lintc_flags_t_strings(tmp_path: Path) -> None:
    found = _lint(tmp_path, "name = 'x'\ngreeting = t'hello {name}'\n")
    assert [(f.line, f.message) for f in found] == [(2, "t-strings: mypyc does not support them")]


@pytest.mark.parametrize(
    ("deps", "pypy", "expected"),
    [
        ([], False, "dev group"),
        (["rich>=15"], False, "dev group"),
        (["librt>=0.15.0 ; implementation_name == 'cpython'"], False, None),
        (["LibRT==0.15"], False, None),  # names are normalised
        (["librt_extra>=1"], False, "dev group"),  # another package
        ([], True, "does not exist on PyPy"),
        (["librt>=0.15"], True, "does not exist on PyPy"),
    ],
)
def test_lintc_librt_needs_a_runtime_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deps: list[str], pypy: bool, expected: str | None
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f"[project]\nname = 'x'\nversion = '1'\ndependencies = {json.dumps(deps)}\n", encoding="utf-8")
    monkeypatch.setattr(lintc, "PYPROJECT", pyproject)
    supported = ["cpython", "pypy", "mypyc"] if pypy else ["cpython", "mypyc"]
    source = "from librt.base64 import b64encode\nimport librt\nfrom .librt import x\n"
    found = _lint(tmp_path, source, {"backend": {"supported": supported}})
    messages = [f.message for f in found]
    if expected is None:
        assert messages == []
    else:
        assert len(messages) == 2 and all(expected in m for m in messages), messages
        assert sorted(f.line for f in found) == [1, 2]  # never the local `.librt`
        if not pypy:
            assert all("./deploy add librt --cpython-only" in m for m in messages)


@pytest.mark.parametrize("text", ["", "not toml [", "[project]\ndependencies = 'librt'\n", "[project]\ndependencies = [1, 2]\n"])
def test_lintc_librt_rule_survives_a_broken_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(text, encoding="utf-8")
    monkeypatch.setattr(lintc, "PYPROJECT", pyproject)
    assert ["dev group" in f.message for f in _lint(tmp_path, "import librt\n")] == [True]


def _preset_config(preset: str) -> Config:
    text = (PRESETS / preset / "files" / "pytemplate.toml").read_text(encoding="utf-8")
    cfg: Config = config._build(Config, tomllib.loads(text.replace("{{name}}", "myapp").replace("{{pkg}}", "myapp")), "")
    config.validate(cfg)
    return cfg


PRESET_NAMES = sorted(p.name for p in PRESETS.iterdir() if (p / "preset.toml").is_file())


@pytest.mark.parametrize("preset", PRESET_NAMES)
def test_lintc_default_presets_are_clean(preset: str) -> None:
    """The shipped compiled cores pass every rule, with each preset's own [compile]."""
    cfg = _preset_config(preset)
    core = PRESETS / preset / "files" / "src" / "__pkg__" / "core"
    files = sorted(core.rglob("*.py"))
    assert files
    for path in files:
        assert lintc.lint_file(cfg, path) == [], (preset, path.name)


# --- 3. imports ----------------------------------------------------------------------------------


def _project(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel_path, content in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")  # the same bytes on every OS
    return root


def test_imports_table(tmp_path: Path) -> None:
    src = _project(
        tmp_path / "src",
        {
            "app/__init__.py": "",
            "app/util.py": "",
            "app/data/__init__.py": "",
            "app/native/_fast" + LINUX_EXT: b"",
            "app/core/__init__.py": "",
            "app/core/sib.py": "",
            "app/core/deep/__init__.py": "",
            "app/core/deep/m.py": (
                "from __future__ import annotations\n"
                "import json, os.path\n"
                "from typing import TYPE_CHECKING, Final\n"
                "from . import x_missing\n"  # level 1: app.core.deep (the package); x_missing is no module
                "from .. import sib\n"  # level 2: app.core + local submodule app.core.sib
                "from ... import util, data\n"  # level 3: app + app.util + app.data (a package)
                "from .... import beyond\n"  # beyond the top: Python refuses it, skipped
                "from ....beyond import nothing\n"
                "from app.native import _fast\n"  # a local extension module
                "from html import parser\n"
                "from os import *\n"
                "if TYPE_CHECKING:\n    import raylib\nelse:\n    import zlib\n"
                "if not TYPE_CHECKING:\n    import struct\nelse:\n    import typed_only\n"
            ),
        },
    )
    module = src / "app" / "core" / "deep" / "m.py"
    candidates: set[str] = set()
    found = imports.imports_of(module, "app.core.deep.m", src, candidates)
    assert found == {
        "json", "os.path", "typing", "app.core.deep", "app.core", "app.core.sib", "app", "app.util", "app.data",
        "app.native", "app.native._fast", "html", "os", "zlib", "struct",
    }  # fmt: skip
    # non-local `from X import a`: candidates for the caller to resolve (typing.Final is not a module)
    assert candidates == {"typing.TYPE_CHECKING", "typing.Final", "html.parser"}
    assert imports.imports_of(module, "app.core.deep.m", src) == found  # candidates are optional


def test_imports_parse_honours_a_bom_and_crlf(tmp_path: Path) -> None:
    mod = tmp_path / "m.py"
    mod.write_bytes(b"\xef\xbb\xbfimport json\r\nfrom html import parser\r\n")
    assert imports.imports_of(mod, "m", tmp_path) == {"json", "html"}


def test_imports_of_a_package_init_and_a_top_level_module(tmp_path: Path) -> None:
    src = _project(
        tmp_path, {"pkg/__init__.py": "from . import sub\nfrom .sub import x\n", "pkg/sub.py": "", "top.py": "from . import y\nfrom .y import z\n"}
    )
    assert imports.imports_of(src / "pkg" / "__init__.py", "pkg", src) == {"pkg", "pkg.sub"}
    assert imports.imports_of(src / "top.py", "top", src) == set()  # no parent package: Python refuses it


def test_local_module_and_is_local(tmp_path: Path) -> None:
    src = _project(
        tmp_path, {"a/__init__.py": "", "a/b.py": "", "ns/x.txt": "", "top.py": "", "v/_c" + LINUX_EXT: b"", "v/lib.so.1": b""}
    )
    assert imports.local_module(src, "a") and imports.local_module(src, "a.b") and imports.local_module(src, "top")
    assert imports.local_module(src, "ns")  # a namespace package
    assert imports.local_module(src, "v._c")  # an extension module
    assert not imports.local_module(src, "v.lib")  # lib.so.1 is not an extension module
    assert not imports.local_module(src, "a.c") and not imports.local_module(src, "a..b") and not imports.local_module(src, "")
    assert imports.is_local(src, "a.anything") and not imports.is_local(src, "json")


def test_parse_error_names_the_runner_python() -> None:
    line, msg = imports.parse_error(SyntaxError("invalid syntax", ("m.py", 7, 1, "x")))
    assert line == 7 and msg.endswith("invalid syntax")
    assert f"runner's Python {sys.version_info.major}.{sys.version_info.minor}" in msg
    line, msg = imports.parse_error(ValueError("source code string cannot contain null bytes"))
    assert line == 1 and "null bytes" in msg
    assert imports.parse_error(SyntaxError())[0] == 1  # no line number


# --- 4. [compile]: validation and the compiled sources ---------------------------------------------


@pytest.mark.parametrize(
    ("compile_", "message"),
    [
        ({"modules": ["myapp.core", "myapp.core.bench"]}, "'myapp.core.bench' is inside 'myapp.core'"),
        ({"modules": ["myapp.core.bench", "myapp.core"]}, "'myapp.core.bench' is inside 'myapp.core'"),
        ({"modules": ["myapp", "myapp.core"]}, "is inside 'myapp'"),
        ({"modules": ["myapp.core", "myapp.core"]}, "lists 'myapp.core' twice"),
        ({"exclude": ["myapp.core"]}, "whole compile.modules entry"),
        ({"exclude": ["myapp.ui"]}, "not inside compile.modules"),
        ({"exclude": ["myapp.coreutils.x"]}, "not inside compile.modules"),
        ({"modules": ["solo"], "exclude": ["solo"]}, "whole compile.modules entry"),
        ({"forbid_imports": ["flet "]}, r"invalid module in \[compile\]"),
        ({"forbid_imports": ["flet, flet_desktop"]}, r"invalid module in \[compile\]"),
        ({"forbid_imports": ["flet\n"]}, r"invalid module in \[compile\]"),
        ({"forbid_imports": [""]}, r"invalid module in \[compile\]"),
        ({"no_semantic_interposition": "yes"}, r"'compile\.no_semantic_interposition' must be of type boolean"),
        ({"no_semantic_interposition": 1}, r"'compile\.no_semantic_interposition' must be of type boolean"),
    ],
)
def test_validate_rejects_compile_mistakes(compile_: dict[str, Any], message: str) -> None:
    with pytest.raises(DeployError, match=message) as err:
        make({"compile": compile_})
    assert err.value.code == 2


@pytest.mark.parametrize(
    "compile_",
    [
        {"modules": ["myapp.core", "myapp.coreutils"]},  # a common prefix is not nesting
        {"modules": ["myapp.core"], "exclude": ["myapp.core.slow", "myapp.core.sub.x"]},
        {"modules": ["a", "b.c"], "exclude": ["b.c.d"], "forbid_imports": ["flet", "a.b_c"]},
        {"modules": ["myapp.core"], "no_semantic_interposition": False},
    ],
)
def test_validate_accepts_compile_settings(compile_: dict[str, Any]) -> None:
    assert make({"compile": compile_}).compile.modules == compile_["modules"]


@pytest.mark.parametrize("preset", PRESET_NAMES)
def test_shipped_configs_pass_the_compile_rules(preset: str) -> None:
    cfg = _preset_config(preset)
    config.load()  # the template's own pytemplate.toml
    # Every project shows the C-flag option, on (the default)
    raw = tomllib.loads((PRESETS / preset / "files" / "pytemplate.toml").read_text(encoding="utf-8"))
    assert raw["compile"]["no_semantic_interposition"] is True
    assert cfg.compile.no_semantic_interposition is True


@pytest.fixture
def src_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp src/ that mypyc.py and config.compiled_paths use instead of the project's."""
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(mypyc, "SRC", src)
    monkeypatch.setattr(config, "SRC", src)
    monkeypatch.setattr(lintc, "SRC", src)
    return src


CORE_TREE = {
    "myapp/__init__.py": "",
    "myapp/core/__init__.py": "",
    "myapp/core/a.py": "X = 1\n",
    "myapp/core/sub/__init__.py": "",
    "myapp/core/sub/m.py": "X = 1\n",
    "myapp/core/sub/n.py": "X = 1\n",
    "myapp/core/subx/__init__.py": "",
    "myapp/core/subx/k.py": "X = 1\n",
    "myapp/core/empty/__init__.py": "",
    "myapp/core/__pycache__/a.cpython-314.py": "",  # never a source
}


def test_compile_exclude_takes_modules_and_subpackages(src_tree: Path) -> None:
    _project(src_tree, CORE_TREE)

    def modules(exclude: list[str]) -> list[str]:
        return mypyc.compiled_modules(make({"compile": {"exclude": exclude}}))

    everything = ["myapp.core.a", "myapp.core.sub.m", "myapp.core.sub.n", "myapp.core.subx.k"]
    assert modules([]) == everything
    assert modules(["myapp.core.sub.m"]) == ["myapp.core.a", "myapp.core.sub.n", "myapp.core.subx.k"]
    # a subpackage removes all its modules, and only them (not the sibling 'subx')
    assert modules(["myapp.core.sub"]) == ["myapp.core.a", "myapp.core.subx.k"]
    assert modules(["myapp.core.empty"]) == everything  # exists: harmless
    for bad in (["myapp.core.nothere"], ["myapp.core.su"], ["myapp.core.a.b"], ["myapp.core.sub.m", "myapp.core.typo"]):
        with pytest.raises(DeployError, match="matches no module in compile.modules") as err:
            modules(bad)
        assert repr(bad[-1]) in str(err.value) and err.value.code == 2
    with pytest.raises(DeployError, match="no .py file to compile"):
        modules(["myapp.core.a", "myapp.core.sub", "myapp.core.subx"])


def test_compiled_sources_of_modules_files_and_missing_entries(src_tree: Path) -> None:
    _project(src_tree, {**CORE_TREE, "solo.py": "X = 1\n"})
    cfg = make({"compile": {"modules": ["myapp.core.sub", "solo", "myapp.core.a"]}})
    assert mypyc.compiled_modules(cfg) == ["myapp.core.sub.m", "myapp.core.sub.n", "solo", "myapp.core.a"]
    with pytest.raises(DeployError, match="compile.modules: neither src/myapp/nope.py nor src/myapp/nope/ exists"):
        mypyc.compiled_sources(make({"compile": {"modules": ["myapp.nope"]}}))


def test_a_module_file_wins_over_a_folder_that_is_no_package(src_tree: Path) -> None:
    """Python imports bench.py when bench/ has no __init__.py (a package turned into a module
    leaves bench/__pycache__/ behind): mypyc compiles that file, never the empty folder."""
    _project(
        src_tree,
        {
            **CORE_TREE,
            "myapp/core/bench.py": "X = 1\n",
            "myapp/core/bench/__pycache__/old.cpython-314.pyc": b"",
            "myapp/core/data/notes.txt": "",
        },
    )
    cfg = make({"compile": {"modules": ["myapp.core.bench", "myapp.core.a"]}})
    assert config.compiled_paths(cfg) == ["myapp/core/bench.py", "myapp/core/a.py"]
    assert mypyc.compiled_modules(cfg) == ["myapp.core.bench", "myapp.core.a"]
    # a real package (with __init__.py) still wins over a module file of the same name, as in Python
    _project(src_tree, {"myapp/core/bench/__init__.py": "", "myapp/core/bench/k.py": "X = 1\n"})
    assert config.compiled_paths(cfg) == ["myapp/core/bench", "myapp/core/a.py"]
    assert mypyc.compiled_modules(cfg) == ["myapp.core.bench.k", "myapp.core.a"]
    # a namespace package (a folder of modules, no __init__.py and no bench.py) is a folder
    (src_tree / "myapp/core/bench.py").unlink()
    (src_tree / "myapp/core/bench/__init__.py").unlink()
    assert config.compiled_paths(cfg) == ["myapp/core/bench", "myapp/core/a.py"]
    assert mypyc.compiled_modules(cfg) == ["myapp.core.bench.k", "myapp.core.a"]
    # a folder that holds no module and no bench.py: nothing to compile, said as such
    with pytest.raises(DeployError, match="neither src/myapp/core/data.py nor src/myapp/core/data/ holds a module to compile"):
        mypyc.compiled_sources(make({"compile": {"modules": ["myapp.core.data"]}}))


def test_compiled_sources_follow_a_symlinked_subpackage(src_tree: Path, tmp_path: Path) -> None:
    _project(src_tree, CORE_TREE)
    shared = _project(tmp_path / "shared", {"__init__.py": "", "z.py": "X = 1\n"})
    try:
        os.symlink(shared, src_tree / "myapp" / "core" / "linked", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")
    assert "myapp.core.linked.z" in mypyc.compiled_modules(make({}))


def test_hook_reports_an_unparsable_module_and_a_bad_exclude(src_tree: Path) -> None:
    """Through the pre-commit hook's check with the real lintc: a failed check, never a crash."""
    from runner import hooks

    _project(src_tree, {"myapp/__init__.py": "", "myapp/core/__init__.py": "", "myapp/core/m.py": "def f() -> str:\n    return 'a' +\n"})
    staged = {"src/myapp/core/m.py"}
    strict = hooks.check_mypyc(make({"backend": {"active": "mypyc"}}), src_tree.parent, staged)
    assert strict.passed is False and len(strict.errors) == 1
    assert strict.errors[0].startswith("src/myapp/core/m.py:2: cannot parse it with the runner's Python")
    relaxed = hooks.check_mypyc(make({}), src_tree.parent, staged)
    assert relaxed.passed is True and len(relaxed.warnings) == 1
    bad = hooks.check_mypyc(make({"compile": {"exclude": ["myapp.core.nope"]}}), src_tree.parent, staged)
    assert bad.passed is False and "matches no module" in bad.hint


def test_a_note_of_the_mypyc_rules_never_blocks(src_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An Enum in compiled code works (mypyc compiles it as a regular Python class): under the
    mypyc profile it made the pre-commit hook and `./deploy check` fail, where a warning says
    all there is to say. A metaclass of the user's own still blocks there."""
    from runner import cmd_dev, hooks

    enum = "from enum import Enum\n\n\nclass Color(Enum):\n    RED = 1\n"
    _project(src_tree, {"myapp/__init__.py": "", "myapp/core/__init__.py": "", "myapp/core/m.py": enum})
    staged = {"src/myapp/core/m.py"}
    cfg = make({"backend": {"active": "mypyc"}})
    note = hooks.check_mypyc(cfg, src_tree.parent, staged)
    assert note.passed is True and not note.errors and len(note.warnings) == 1 and "a note" in note.warnings[0]
    (src_tree / "myapp" / "core" / "m.py").write_text("class Color(metaclass=type(int)):\n    red = 1\n", encoding="utf-8")
    blocked = hooks.check_mypyc(cfg, src_tree.parent, staged)
    assert blocked.passed is False and len(blocked.errors) == 1 and "has the metaclass" in blocked.errors[0]
    (src_tree / "myapp" / "core" / "m.py").write_text(enum, encoding="utf-8")
    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(cmd_dev.ui, "error", lambda msg: lines.append(("error", msg)))
    monkeypatch.setattr(cmd_dev.ui, "warn", lambda msg: lines.append(("warn", msg)))
    monkeypatch.setattr(cmd_dev, "_profile_file", lambda cfg, profile, kind: src_tree / f"{kind}.cfg")
    monkeypatch.setattr(cmd_dev.envs, "uv_run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(cmd_dev, "_run_mypy", lambda cfg, profile, blocking: True)
    assert cmd_dev.run_checks(cfg, "mypyc") is True
    assert [kind for kind, _ in lines] == ["warn"]


# --- 5. sync_tree ----------------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in root.rglob("*")
        if "__pycache__" not in p.parts
    }


def test_sync_tree_follows_symlinked_dirs(tmp_path: Path) -> None:
    """A symlinked folder in src/ (shared assets, a linked subpackage) is copied with its files."""
    shared, src, dst = tmp_path / "shared", tmp_path / "proj" / "src", tmp_path / "dst"
    _project(shared, {"img.png": b"png", "sub/a.txt": b"a"})
    _project(src, {"main.py": b"pass\n"})
    try:
        os.symlink(shared, src / "assets", target_is_directory=True)
        os.symlink(shared, src / "again", target_is_directory=True)  # two links to one folder
        os.symlink(src, shared / "sub" / "loop", target_is_directory=True)  # a cycle back to src
        os.symlink(src.parent, src / "up", target_is_directory=True)  # a cycle through a parent
        os.symlink(shared / "img.png", src / "linked.png")  # a linked file
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")
    changed = mypyc.sync_tree(src, dst)
    for top in ("assets", "again"):
        assert (dst / top / "img.png").read_bytes() == b"png"
        assert (dst / top / "sub" / "a.txt").read_bytes() == b"a"
        assert not (dst / top).is_symlink()
    assert (dst / "linked.png").read_bytes() == b"png" and not (dst / "linked.png").is_symlink()
    # a link back to a folder being copied is skipped (no endless recursion)
    assert not (dst / "up" / "src").exists() and not (dst / "assets" / "sub" / "loop").exists()
    assert changed == 6 == sum(1 for p in dst.rglob("*") if p.is_file())
    assert mypyc.sync_tree(src, dst) == 0
    (shared / "img.png").unlink()
    assert mypyc.sync_tree(src, dst) > 0
    assert not (dst / "assets" / "img.png").exists() and (dst / "assets" / "sub" / "a.txt").exists()


def test_sync_tree_warns_about_a_broken_symlink(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src, dst = _project(tmp_path / "src", {"a.py": "x = 1\n"}), tmp_path / "dst"
    try:
        os.symlink(tmp_path / "nowhere", src / "gone.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")
    assert mypyc.sync_tree(src, dst) == 1
    assert "gone.txt: broken symbolic link, not copied" in capsys.readouterr().err
    assert _snapshot(dst) == {"a.py": b"x = 1\n"}


def test_sync_tree_handles_type_changes_and_removed_packages(tmp_path: Path) -> None:
    src, dst = _project(tmp_path / "src", {"pkg/__init__.py": ""}), tmp_path / "dst"
    data = src / "pkg" / "data"
    data.write_text("file", encoding="utf-8")
    mypyc.sync_tree(src, dst)
    data.unlink()
    _project(data, {"x.txt": "x"})  # a file became a folder (was FileExistsError)
    mypyc.sync_tree(src, dst)
    assert _snapshot(dst) == _snapshot(src)
    shutil.rmtree(data)
    data.write_text("file again", encoding="utf-8")  # a folder became a file (was an empty folder forever)
    mypyc.sync_tree(src, dst)
    assert _snapshot(dst) == _snapshot(src)
    assert mypyc.sync_tree(src, dst) == 0  # stable
    _project(src, {"pkg/old/__init__.py": "X = 1\n", "pkg/old/deep/__init__.py": ""})
    mypyc.sync_tree(src, dst)
    for folder in ("pkg/old", "pkg/old/deep"):
        _project(dst, {f"{folder}/__pycache__/__init__.cpython-314.pyc": b"pyc"})
    shutil.rmtree(src / "pkg" / "old")
    mypyc.sync_tree(src, dst)
    assert not (dst / "pkg" / "old").exists()  # no namespace-package ghost left with its caches
    _project(dst, {"pkg/__pycache__/__init__.cpython-314.pyc": b"pyc"})
    mypyc.sync_tree(src, dst)
    assert (dst / "pkg" / "__pycache__").is_dir()  # the caches of live folders are kept


def test_sync_tree_keeps_user_native_files_and_skips_mypyc_outputs(tmp_path: Path) -> None:
    natives = ["libfoo.so", "libbar.so.1", "foo.dll", "foo.dylib", "_vend" + LINUX_EXT, "_v.cp314-win_amd64.pyd"]
    src = _project(
        tmp_path / "src",
        {
            **{f"pkg/native/{n}": b"native" for n in natives},
            "pkg/core/m.py": "X = 1\n",
            "pkg/core/m" + LINUX_EXT: b"stray",  # a stray in-place build of a compiled module
            "pkg__mypyc" + LINUX_EXT: b"stray",  # a stray shared lib
            "pkg/core/n__mypyc.cp314-win_amd64.pyd": b"stray",  # a stray per-module lib (separate = true)
        },
    )
    dst = tmp_path / "dst"
    mypyc.sync_tree(src, dst, owned=["pkg.core.m"])
    assert sorted(p.name for p in (dst / "pkg" / "native").iterdir()) == sorted(natives)
    assert not (dst / "pkg" / "core" / ("m" + LINUX_EXT)).exists() and not (dst / ("pkg__mypyc" + LINUX_EXT)).exists()
    assert not (dst / "pkg" / "core" / "n__mypyc.cp314-win_amd64.pyd").exists()
    # the stage's own build output survives the sync; a user file deleted from src goes
    (dst / "pkg" / "core" / ("m" + LINUX_EXT)).write_bytes(b"built")
    (src / "pkg" / "native" / "libfoo.so").unlink()
    assert mypyc.sync_tree(src, dst, owned=["pkg.core.m"]) == 1
    assert not (dst / "pkg" / "native" / "libfoo.so").exists()
    assert (dst / "pkg" / "core" / ("m" + LINUX_EXT)).read_bytes() == b"built"


def test_sync_tree_payload_never_takes_a_stray_build_without_its_shared_lib(tmp_path: Path) -> None:
    """The cpython/pypy payload (owned=()): a stray compiled copy of a module next to its .py
    is a build output, not app content (its shared lib would be missing); a vendored one is kept."""
    src = _project(
        tmp_path / "src",
        {"pkg/x.py": "X = 1\n", "pkg/x" + LINUX_EXT: b"stray", "pkg__mypyc" + LINUX_EXT: b"stray", "pkg/libz" + LINUX_EXT: b"z"},
    )
    dst = tmp_path / "payload"
    mypyc.sync_tree(src, dst)
    assert _snapshot(dst) == {"pkg": None, "pkg/x.py": b"X = 1\n", "pkg/libz" + LINUX_EXT: b"z"}


def _writable(root: Path) -> list[str]:
    """What below `root` (itself included) is not owner-writable."""
    return sorted(p.relative_to(root).as_posix() for p in [root, *root.rglob("*")] if not p.stat().st_mode & stat.S_IWUSR)


def test_sync_tree_copies_of_read_only_files_stay_replaceable(tmp_path: Path) -> None:
    # A read-only file of src/ (a Perforce checkout, a link into the Nix store): copy2 kept its
    # mode, and once the file changed the next build could not replace the copy ("cannot write
    # .build/payload/...: Permission denied"; Windows could not delete one either)
    src = _project(tmp_path / "src", {"pkg/NOTICE.txt": "version 1", "pkg/gone.txt": "x", "pkg/same.txt": "s"})
    for f in (src / "pkg").iterdir():
        f.chmod(0o444)
    dst = tmp_path / "dst"
    mypyc.sync_tree(src, dst)
    assert _writable(dst) == []
    # Read-only copies an older ./deploy left: replaced, deleted, or made writable in place
    for f in (dst / "pkg").iterdir():
        f.chmod(0o444)
    notice = src / "pkg" / "NOTICE.txt"
    notice.chmod(0o644)
    notice.write_text("version 2, longer", encoding="utf-8")
    notice.chmod(0o444)
    (src / "pkg" / "gone.txt").chmod(0o644)  # Windows deletes no read-only file either
    (src / "pkg" / "gone.txt").unlink()
    assert mypyc.sync_tree(src, dst) == 2
    assert (dst / "pkg" / "NOTICE.txt").read_text(encoding="utf-8") == "version 2, longer"
    assert not (dst / "pkg" / "gone.txt").exists()
    assert _writable(dst) == []  # same.txt, unchanged, too


def test_remove_tree_removes_read_only_copies_and_only_a_link(tmp_path: Path) -> None:
    tree = _project(tmp_path / "t", {"a/b.txt": "x", "a/c/d.txt": "y"})
    for path in (tree / "a" / "b.txt", tree / "a" / "c" / "d.txt"):
        path.chmod(0o444)
    for folder in (tree / "a" / "c", tree / "a"):
        folder.chmod(0o555)  # copytree copies a folder's mode too: POSIX empties no read-only folder
    mypyc.remove_tree(tree)
    assert not tree.exists()
    target = _project(tmp_path / "target", {"keep.txt": "k"})
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("no symbolic links here")
    mypyc.remove_tree(link)
    assert not os.path.lexists(link) and (target / "keep.txt").is_file()


def test_the_wheel_copies_read_only_sources_writable(wheel_project: Path, monkeypatch: pytest.MonkeyPatch, src_tree: Path) -> None:
    # copytree from src/ kept read-only files AND folders: build_ext --inplace writes next to the
    # sources, and the next build could not delete the work folder
    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    def fake_uv(env: envs.PyEnv, args: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in args]
        out = Path(argv[argv.index("--out-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "pkg-0.1.0-py3-none-any.whl").write_bytes(b"")
        return _done(argv)

    monkeypatch.setattr(wheel.envs, "sync", lambda env, **kw: None)
    monkeypatch.setattr(wheel.envs, "uv", fake_uv)
    read_only = [src_tree / "pkg" / "data" / "x.json", src_tree / "assets" / "img.txt", src_tree / "pkg" / "data", src_tree / "assets"]
    try:
        for path in read_only:
            path.chmod(0o555 if path.is_dir() else 0o444)
        for _ in range(2):  # the second build deletes the first one's work folder
            wheel.build(BuildRequest(_wheel_cfg(), "mypyc", "wheel", src_tree))
            assert _writable(wheel_project / ".build" / "wheel" / "mypyc" / "src") == []
    finally:
        for path in read_only:
            path.chmod(0o755 if path.is_dir() else 0o644)


# --- 6. stale extensions -------------------------------------------------------------------------


def _stage_with(stage: Path, names: list[str]) -> None:
    shutil.rmtree(stage, ignore_errors=True)
    for name in names:
        (stage / name).parent.mkdir(parents=True, exist_ok=True)
        (stage / name).write_bytes(b"x")


def _left(stage: Path) -> list[str]:
    return sorted(p.relative_to(stage).as_posix() for p in mypyc.extension_files(stage))


def test_remove_stale_extensions_drops_other_python_and_unused_shared_libs(tmp_path: Path) -> None:
    stage, src = tmp_path / "stage", tmp_path / "src"
    src.mkdir()
    lin = "cpython-313-x86_64-linux-gnu.so"
    keep = [f"myapp/core/bench.{lin}", "myapp/core/bench.cpython-313-darwin.so", "myapp/core/bench.cp313-win_amd64.pyd"]
    files = [
        *keep,
        "myapp/core/bench.cpython-314-x86_64-linux-gnu.so",  # built before python.cpython changed: OLD code
        "myapp/core/bench.cp314-win_amd64.pyd",
        "myapp/core/bench.cpython-313t-x86_64-linux-gnu.so",  # a free-threaded build: another ABI
        f"myapp/core/gone.{lin}",  # a module no longer compiled
        f"myapp/core/bench__mypyc.{lin}",  # the shared lib of compile.separate = true
        f"myapp__mypyc.{lin}",  # the shared lib of compile.separate = false
        "myapp__mypyc.cpython-314-x86_64-linux-gnu.so",
        "myapp/core/__pycache__/bench.cpython-313.so",  # never looked at
    ]
    _stage_with(stage, files)
    mypyc.remove_stale_extensions(stage, ["myapp.core.bench"], "myapp", python="3.13", src=src)
    assert _left(stage) == sorted([*keep, f"myapp__mypyc.{lin}"])
    assert (stage / "myapp/core/__pycache__/bench.cpython-313.so").exists()
    _stage_with(stage, files)
    mypyc.remove_stale_extensions(stage, ["myapp.core.bench"], "myapp", python="3.13", separate=True, src=src)
    assert _left(stage) == sorted([*keep, f"myapp/core/bench__mypyc.{lin}"])
    # idempotent
    mypyc.remove_stale_extensions(stage, ["myapp.core.bench"], "myapp", python="3.13", separate=True, src=src)
    assert _left(stage) == sorted([*keep, f"myapp/core/bench__mypyc.{lin}"])


def test_remove_stale_extensions_keeps_the_apps_own_native_files(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    user = ["pkg/native/libfoo.so", "pkg/native/_v.cpython-312-x86_64-linux-gnu.so"]
    src = _project(tmp_path / "src", {n: b"native" for n in user})
    _project(src, {"pkg/core/m.cpython-312-x86_64-linux-gnu.so": b"stray"})  # a stray build output is never exempt
    _stage_with(stage, [*user, "pkg/core/m.cpython-312-x86_64-linux-gnu.so", "pkg/core/old" + LINUX_EXT])
    mypyc.remove_stale_extensions(stage, ["pkg.core.m"], "pkg", python="3.14", src=src)
    assert _left(stage) == sorted(user)


def test_other_python_reads_every_platform_spelling() -> None:
    def other(name: str, python: str = "3.14") -> bool:
        return mypyc._other_python(Path(name), python)

    assert not other("m.cpython-314-x86_64-linux-gnu.so") and not other("m.cp314-win_amd64.pyd")
    assert not other("m.cpython-314-darwin.so") and not other("m.so") and not other("m.abi3.so") and not other("m.pyd")
    assert other("m.cpython-313-x86_64-linux-gnu.so") and other("m.cp315-win_arm64.pyd") and other("m.cpython-314t-darwin.so")
    assert other("m.cpython-3140-x.so") and not other("m.cpython-3140-x.so", "3.140")
    assert not other("cp313.cpython-314-darwin.so")  # a module named like a tag


# --- 7. mypyc.build with a fake compiler ----------------------------------------------------------


class FakeCompiler:
    """Replaces proc.run in mypyc.build: reads spec.json and acts like tools/mypyc_build.py.

    It writes `<module><suffix>` for each compiled file unless it is already there (the
    incremental case), or always with `force`; `code` makes it fail instead.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, suffix: str = LINUX_EXT) -> None:
        self.specs: list[dict[str, Any]] = []
        self.captures: list[bool] = []
        self.code = 0
        self.stdout: str | None = ""
        self.stderr: str | None = ""
        self.skip: set[str] = set()  # modules it "forgets" to build
        self.suffix = suffix
        monkeypatch.setattr(mypyc.proc, "run", self.run)
        monkeypatch.setattr(mypyc.proc, "find_uv", lambda: "uv")

    def run(self, argv: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        spec = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        self.specs.append(spec)
        self.captures.append(kw.get("capture", False))
        if self.code:
            return _done([str(a) for a in argv], self.code, self.stdout, self.stderr)  # type: ignore[arg-type]
        if spec["compile"]:
            for f in spec["files"]:
                ext = Path(spec["stage"]) / (f[:-3] + self.suffix)
                if f[:-3].replace("/", ".") not in self.skip and (spec["force"] or not ext.exists()):
                    ext.write_bytes(spec["opt_level"].encode())
            if not spec["separate"]:
                (Path(spec["stage"]) / (spec["group"] + "__mypyc" + self.suffix)).write_bytes(b"lib")
        return _done([str(a) for a in argv])

    @property
    def force(self) -> bool:
        return bool(self.specs[-1]["force"])


@pytest.fixture
def fake_build(src_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCompiler:
    _project(src_tree, {"main.py": "", "myapp/__init__.py": "", "myapp/core/__init__.py": "", "myapp/core/m.py": "X = 1\n"})
    monkeypatch.setattr(mypyc, "BUILD", tmp_path / ".build")
    monkeypatch.setattr(mypyc, "ANNOTATE_HTML", tmp_path / ".build" / "reports" / "mypyc-annotate.html")
    return FakeCompiler(monkeypatch)


def test_build_forces_a_rebuild_when_compile_options_change(fake_build: FakeCompiler) -> None:
    o3, o0 = make({}), make({"compile": {"opt_level": "0"}})
    for name in ("dev", "release"):
        mypyc.build(o3, name)
        assert fake_build.force  # no record yet: one full build
        mypyc.build(o3, name)
        assert not fake_build.force  # unchanged options stay incremental
    mypyc.build(o0, "release")
    assert fake_build.force
    stage = mypyc.profile(o3, "release").stage
    assert (stage / "myapp" / "core" / ("m" + LINUX_EXT)).read_bytes() == b"0"  # really rebuilt
    mypyc.build(o0, "release")
    assert not fake_build.force
    mypyc.build(o0, "dev")
    assert fake_build.force  # each profile has its own record
    # ./deploy report (compile_c=False) neither forces nor records
    mypyc.build(o3, "dev", compile_c=False)
    assert not fake_build.force
    mypyc.build(o3, "dev")
    assert fake_build.force
    # a failed build leaves no record: the next one forces again
    fake_build.code = 1
    with pytest.raises(DeployError):
        mypyc.build(o0, "release")
    fake_build.code = 0
    mypyc.build(o0, "release")
    assert fake_build.force


@pytest.mark.parametrize(
    "change",
    [
        {"deploy": {"optimize": 0}},  # strip_asserts of the release profile
        {"compile": {"multi_file": True}},
        {"compile": {"separate": True}},
        {"compile": {"strict_dunder_typing": True}},
        {"compile": {"no_semantic_interposition": False}},  # a C flag only: the C files stay the same
    ],
)
def test_build_forces_a_rebuild_for_every_binary_option(fake_build: FakeCompiler, change: dict[str, Any]) -> None:
    mypyc.build(make({}), "release")
    mypyc.build(make({}), "release")
    assert not fake_build.force
    mypyc.build(make(change), "release")
    assert fake_build.force
    mypyc.build(make(change), "release")
    assert not fake_build.force


def test_a_forced_build_starts_without_the_cached_ir_and_c(fake_build: FakeCompiler) -> None:
    """compile.separate = true makes mypyc incremental: it reused its cached IR and the C of the
    last run, and strip_asserts is no part of mypy's cache key, so deploy.optimize 0 -> 1 kept
    the asserts in the release binary (verified with a real compile). A forced build removes
    both first; an unchanged one keeps them (incremental builds stay fast)."""
    cfg = make({"compile": {"separate": True}, "deploy": {"optimize": 0}})
    mypyc.build(cfg, "release")
    cache = mypyc.profile(cfg, "release").dir
    for sub in ("mypy_cache", "c"):
        (cache / sub).mkdir(exist_ok=True)
        (cache / sub / "stale").write_text("from the last run", encoding="utf-8")
    mypyc.build(cfg, "release")
    assert not fake_build.force and (cache / "mypy_cache" / "stale").exists() and (cache / "c" / "stale").exists()
    mypyc.build(make({"compile": {"separate": True}, "deploy": {"optimize": 1}}), "release")
    assert fake_build.force and fake_build.specs[-1]["strip_asserts"] is True
    assert not (cache / "mypy_cache").exists() and not (cache / "c").exists()


def test_build_forces_a_rebuild_when_the_compiler_environment_changes(fake_build: FakeCompiler, monkeypatch: pytest.MonkeyPatch) -> None:
    """CC/CFLAGS/... change the binaries, not the C: like opt_level, a change forces a rebuild."""
    for name in mypyc.COMPILER_ENV:
        monkeypatch.delenv(name, raising=False)
    cfg = make({})
    mypyc.build(cfg, "dev")
    mypyc.build(cfg, "dev")
    assert not fake_build.force
    monkeypatch.setenv("CFLAGS", "-O2 -march=native")
    mypyc.build(cfg, "dev")
    assert fake_build.force
    mypyc.build(cfg, "dev")
    assert not fake_build.force
    record = json.loads((mypyc.profile(cfg, "dev").dir / mypyc.COMPILED_STAMP).read_text(encoding="utf-8"))
    assert record["env"] == {"CFLAGS": "-O2 -march=native"}
    assert "env" not in fake_build.specs[-1]  # recorded, not an input of tools/mypyc_build.py
    monkeypatch.setenv("PT_SOMETHING_ELSE", "1")  # not a compiler variable
    mypyc.build(cfg, "dev")
    assert not fake_build.force
    monkeypatch.setenv("CC", "clang")
    mypyc.build(cfg, "dev")
    assert fake_build.force
    monkeypatch.delenv("CFLAGS")
    mypyc.build(cfg, "dev")
    assert fake_build.force  # removed is a change too


def test_build_does_not_force_for_annotate_or_new_modules(fake_build: FakeCompiler, src_tree: Path) -> None:
    mypyc.build(make({}), "dev")
    mypyc.build(make({"compile": {"annotate": True}}), "dev")
    assert not fake_build.force
    (src_tree / "myapp" / "core" / "n.py").write_text("Y = 2\n", encoding="utf-8")
    mypyc.build(make({}), "dev")
    assert not fake_build.force and fake_build.specs[-1]["files"] == ["myapp/core/m.py", "myapp/core/n.py"]


def test_build_dry_run_keeps_the_record(fake_build: FakeCompiler, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({})
    mypyc.build(cfg, "dev")
    stamp = mypyc.profile(cfg, "dev").dir / mypyc.COMPILED_STAMP
    before = stamp.read_bytes()
    monkeypatch.setattr(proc, "DRY_RUN", True)
    mypyc.build(make({"compile": {"opt_level": "0"}}), "dev")
    assert stamp.read_bytes() == before


def test_build_spec_matches_what_the_tool_reads(fake_build: FakeCompiler) -> None:
    """Every spec key tools/mypyc_build.py reads must be written by mypyc.build."""
    mypyc.build(make({}), "dev")
    tree = ast.parse((TOOLS / "mypyc_build.py").read_text(encoding="utf-8"))
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "spec":
            if isinstance(node.slice, ast.Constant):
                read.add(str(node.slice.value))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "spec" and isinstance(node.args[0], ast.Constant):
                read.add(str(node.args[0].value))
    assert "force" in read and "compile" in read
    assert read <= set(fake_build.specs[-1])
    spec = fake_build.specs[-1]
    assert (spec["c_dir"], spec["build_temp"], spec["build_lib"]) == ("../c", "../obj", "../lib")  # short paths (MAX_PATH)
    assert spec["files"] == ["myapp/core/m.py"] and spec["group"] == "myapp"
    assert spec["no_semantic_interposition"] is True and spec["opt_level"] == "3"  # the defaults


def test_build_removes_stale_extensions_before_the_sync(fake_build: FakeCompiler, src_tree: Path) -> None:
    cfg = make({})
    stage = mypyc.profile(cfg, "dev").stage
    mypyc.build(cfg, "dev")
    # a compiled subpackage that is deleted: its extension and then its folder go
    _project(src_tree, {"myapp/core/sub/__init__.py": "", "myapp/core/sub/k.py": "Z = 3\n"})
    mypyc.build(cfg, "dev")
    assert (stage / "myapp" / "core" / "sub" / ("k" + LINUX_EXT)).exists()
    shutil.rmtree(src_tree / "myapp" / "core" / "sub")
    mypyc.build(cfg, "dev")
    assert not (stage / "myapp" / "core" / "sub").exists()
    # python.cpython changed: the old binaries go, never shipped next to the new ones
    fake_build.suffix = ".cpython-315-x86_64-linux-gnu.so"
    mypyc.build(make({"python": {"cpython": "3.15"}}), "dev")
    assert _left(stage) == ["myapp/core/m.cpython-315-x86_64-linux-gnu.so", "myapp__mypyc.cpython-315-x86_64-linux-gnu.so"]


def test_build_leaves_a_vendored_native_file_alone(
    fake_build: FakeCompiler, src_tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(src_tree, {"myapp/native/libfoo.so": b"native", "myapp/native/_v.cpython-313-x86_64-linux-gnu.so": b"v"})
    cfg = make({})
    stage = mypyc.profile(cfg, "dev").stage
    mypyc.build(cfg, "dev")
    assert (stage / "myapp" / "native" / "_v.cpython-313-x86_64-linux-gnu.so").read_bytes() == b"v"
    monkeypatch.setattr(ui, "VERBOSE", True)
    capsys.readouterr()
    mypyc.build(cfg, "dev")  # neither deleted ("no longer compiled", "another Python") nor copied again
    err = capsys.readouterr().err
    assert "stage: 0 file(s) updated" in err and "native" not in err
    (src_tree / "myapp" / "native" / "libfoo.so").unlink()
    mypyc.build(cfg, "dev")
    assert not (stage / "myapp" / "native" / "libfoo.so").exists()


def test_build_with_separate_keeps_the_per_module_libs(fake_build: FakeCompiler) -> None:
    """compile.separate = true: <module>__mypyc libs are wanted (they were deleted on every build)."""
    cfg = make({"compile": {"separate": True}})
    stage = mypyc.profile(cfg, "dev").stage
    mypyc.build(make({}), "dev")  # first with the shared lib
    (stage / "myapp" / "core" / ("m__mypyc" + LINUX_EXT)).write_bytes(b"lib")  # what separate = true builds
    mypyc.build(cfg, "dev")
    assert _left(stage) == [f"myapp/core/m{LINUX_EXT}", f"myapp/core/m__mypyc{LINUX_EXT}"]  # the old group lib is gone


def test_build_fails_when_an_extension_is_missing(fake_build: FakeCompiler, src_tree: Path) -> None:
    cfg = make({})
    stamp = mypyc.profile(cfg, "dev").dir / mypyc.COMPILED_STAMP
    mypyc.build(cfg, "dev")
    assert stamp.is_file()
    (src_tree / "myapp" / "core" / "n.py").write_text("Y = 2\n", encoding="utf-8")
    fake_build.skip = {"myapp.core.n"}
    with pytest.raises(DeployError, match=r"mypyc did not generate an extension for: myapp\.core\.n$"):
        mypyc.build(cfg, "dev")
    assert not stamp.exists()  # the next build is forced


def test_compiler_hint_names_the_msvc_tools_of_the_venv_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mypyc.os, "name", "nt")
    assert "Component.VC.Tools.x86.x64 " in mypyc.has_compiler_hint()
    assert "Component.VC.Tools.x86.x64 " in mypyc.has_compiler_hint("win-amd64")
    arm = mypyc.has_compiler_hint("win-arm64")
    assert "Component.VC.Tools.ARM64 " in arm and "x86.x64" not in arm
    monkeypatch.setattr(mypyc.os, "name", "posix")
    assert "gcc/clang" in mypyc.has_compiler_hint("win-arm64")


STALE_LOCK = "error: The lockfile at `uv.lock` needs to be updated, but `--locked` was provided."


@pytest.mark.parametrize(
    ("code", "stdout", "stderr", "verbose", "hint", "exit_code"),
    [
        (mypyc.MYPYC_REJECTED, "myapp/core/m.py:1: error: bad", "", False, False, 1),
        (mypyc.MYPYC_REJECTED, None, None, True, False, 1),  # -v: nothing captured, still no hint
        (mypyc.C_BUILD_FAILED, "", "error: command 'gcc' failed: No such file or directory", False, True, 1),
        (mypyc.C_BUILD_FAILED, None, None, True, True, 1),
        (mypyc.C_BUILD_FAILED, "myapp/core/m.py:1: error: this text no longer decides", "", False, True, 1),
        # uv failed before mypyc ran (a stale uv.lock): it got "mypyc needs a C compiler" on a
        # machine with gcc
        (1, "", STALE_LOCK, False, False, 1),
        (2, "", "error: No interpreter found", False, False, 2),
    ],
)
def test_build_compiler_hint_only_when_the_c_step_failed(
    fake_build: FakeCompiler,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
    stdout: str | None,
    stderr: str | None,
    verbose: bool,
    hint: bool,
    exit_code: int,
) -> None:
    monkeypatch.setattr(ui, "VERBOSE", verbose)
    fake_build.code, fake_build.stdout, fake_build.stderr = code, stdout, stderr
    with pytest.raises(DeployError) as err:
        mypyc.build(make({}), "dev")
    assert fake_build.captures[-1] is not verbose
    assert (mypyc.has_compiler_hint() in str(err.value)) is hint
    assert str(err.value).startswith(f"mypyc failed (exit code {exit_code})") and err.value.code == exit_code
    if not verbose and stdout:
        assert stdout in capsys.readouterr().err  # the captured output is shown


def test_build_a_compiler_that_cannot_start_is_a_missing_requirement(fake_build: FakeCompiler) -> None:
    """README and CLAUDE.md 5.3: a missing compiler exits 3. `./deploy compile` without `cc`
    (or with CC naming a missing program) exited 1, like a failed compile."""
    fake_build.code = mypyc.COMPILER_MISSING
    with pytest.raises(DeployError) as err:
        mypyc.build(make({}), "dev")
    assert err.value.code == 3
    assert "the C compiler cannot start" in str(err.value) and mypyc.has_compiler_hint() in str(err.value)
    assert not (mypyc.profile(make({}), "dev").dir / mypyc.COMPILED_STAMP).exists()  # the next build is forced


def test_build_compiler_hint_names_the_tools_of_the_venv_platform(fake_build: FakeCompiler, monkeypatch: pytest.MonkeyPatch) -> None:
    # A win-arm64 .venv got the x86/x64 MSVC tools in the hint: build called has_compiler_hint()
    platforms: list[str] = []
    monkeypatch.setattr(mypyc, "IS_WINDOWS", True)
    monkeypatch.setattr(mypyc.envs, "interpreter_info", lambda python: {"platform": "win-arm64"})
    monkeypatch.setattr(mypyc, "has_compiler_hint", lambda platform="win-amd64": platforms.append(platform) or "HINT")
    fake_build.code = mypyc.C_BUILD_FAILED
    with pytest.raises(DeployError, match="HINT"):
        mypyc.build(make({}), "dev")
    assert platforms == ["win-arm64"]


# --- 8. tools/mypyc_build.py -----------------------------------------------------------------------


def _load_build_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("pt_mypyc_build", TOOLS / "mypyc_build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec_file(tmp_path: Path, **extra: Any) -> Path:
    spec = {
        "stage": str(tmp_path),
        "config": "mypy.ini",
        "cache_dir": "cache",
        "annotate": "",
        "files": ["m.py"],
        "opt_level": "3",
        "no_semantic_interposition": True,
        "debug_level": "1",
        "strip_asserts": False,
        "multi_file": False,
        "separate": False,
        "strict_dunder_typing": False,
        "group": "g",
        "c_dir": "../c",
        "build_temp": "../obj",
        "build_lib": "../lib",
        "compile": True,
        **extra,
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


class FakeExtension:
    """What mypycify returns: setuptools Extensions; only extra_compile_args matters here."""

    def __init__(self, name: str, args: list[str]) -> None:
        self.name = name
        self.extra_compile_args = args


def _run_build_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, compiler: str = "unix", **spec_extra: Any
) -> tuple[types.ModuleType, list[FakeExtension], list[dict[str, Any]]]:
    """Run tools/mypyc_build.py main() with a fake mypycify (two extensions that SHARE one list
    of flags, as mypycify builds them), a fake setup() and the given compiler type."""
    shared = ["-O3", "-Werror"]
    extensions = [FakeExtension("g__mypyc", shared), FakeExtension("m", shared)]
    setups: list[dict[str, Any]] = []
    fake_build_mod = types.ModuleType("mypyc.build")
    fake_build_mod.mypycify = lambda args, **kw: extensions  # type: ignore[attr-defined]
    fake_setuptools = types.ModuleType("setuptools")
    fake_setuptools.setup = lambda **kw: setups.append(kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mypyc", types.ModuleType("mypyc"))
    monkeypatch.setitem(sys.modules, "mypyc.build", fake_build_mod)
    monkeypatch.setitem(sys.modules, "setuptools", fake_setuptools)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mypyc_build.py", str(_spec_file(tmp_path, **spec_extra))])
    module = _load_build_script()

    def compiler_type() -> str:
        if compiler == "none":
            raise AssertionError("the compiler was looked up although nothing is compiled")
        return compiler

    monkeypatch.setattr(module, "compiler_type", compiler_type)
    assert module.main() == 0
    return module, extensions, setups


@pytest.mark.parametrize("force", [False, True, None])
def test_build_script_passes_force_to_build_ext(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool | None) -> None:
    extra = {} if force is None else {"force": force}  # None: a spec.json written by an older runner
    _, _, setups = _run_build_script(tmp_path, monkeypatch, **extra)
    args = setups[-1]["script_args"]
    assert args[: args.index("build_ext") + 1] == ["--quiet", "build_ext"]
    assert args.count("--force") == (1 if force else 0)


@pytest.mark.parametrize(
    ("raised", "stderr"), [(SystemExit(1), ""), (SystemExit("error: setuptools not installed"), "setuptools not installed"), (RuntimeError("boom"), "boom")]
)
def test_build_script_reports_rejected_code_with_its_own_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raised: BaseException, stderr: str
) -> None:
    def mypycify(args: list[str], **kw: Any) -> list[str]:
        raise raised

    fake_build_mod = types.ModuleType("mypyc.build")
    fake_build_mod.mypycify = mypycify  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mypyc", types.ModuleType("mypyc"))
    monkeypatch.setitem(sys.modules, "mypyc.build", fake_build_mod)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mypyc_build.py", str(_spec_file(tmp_path))])
    module = _load_build_script()
    assert module.main() == module.MYPYC_REJECTED == mypyc.MYPYC_REJECTED
    assert stderr in capsys.readouterr().err


@pytest.mark.parametrize("problem", ["the C compiler command 'clang-99 -fPIC' cannot start: clang-99 was not found", None])
def test_build_script_tells_a_missing_compiler_from_a_failed_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], problem: str | None
) -> None:
    """setuptools turns every failure into SystemExit("error: ..."): only when the compiler
    cannot start does the script exit COMPILER_MISSING; a real compile error is C_BUILD_FAILED."""
    module, _, _ = _run_build_script(tmp_path, monkeypatch)
    failure = SystemExit("error: [Errno 2] No such file or directory: 'clang-99'")

    def failing_setup(**kw: Any) -> None:
        raise failure

    sys.modules["setuptools"].setup = failing_setup  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "missing_compiler", lambda: problem)
    if problem is None:
        assert module.main() == module.C_BUILD_FAILED
        assert "No such file or directory: 'clang-99'" in capsys.readouterr().err
        return
    assert module.main() == module.COMPILER_MISSING == mypyc.COMPILER_MISSING
    err = capsys.readouterr().err
    assert "No such file or directory: 'clang-99'" in err and f"error: {problem}" in err


@pytest.mark.skipif(os.name == "nt", reason="CC is for gcc/clang: MSVC is found by vswhere")
@pytest.mark.parametrize("cc", ["/nonexistent/clang-99", "ccache-that-is-not-there gcc"])
def test_build_script_finds_a_cc_that_does_not_exist(monkeypatch: pytest.MonkeyPatch, cc: str) -> None:
    pytest.importorskip("setuptools")  # the script runs where mypyc imported setuptools (.venv)
    monkeypatch.setenv("CC", cc)
    problem = _load_build_script().missing_compiler()
    assert problem is not None and cc.split()[0] in problem


@pytest.mark.skipif(os.name == "nt", reason="CC is for gcc/clang: MSVC is found by vswhere")
@pytest.mark.parametrize(("platform", "xcode", "shim"), [("darwin", "no Xcode Command Line Tools", True), ("darwin", None, False), ("linux", "never asked", False)])
def test_build_script_finds_the_macos_compiler_shims_without_developer_tools(
    monkeypatch: pytest.MonkeyPatch, platform: str, xcode: str | None, shim: bool
) -> None:
    """A Mac without the Command Line Tools still has /usr/bin/cc and clang: xcrun shims that
    exist (so shutil.which finds them) and fail, which left the usual missing compiler of a Mac
    at exit 1 instead of 3. On macOS a compiler in /usr/bin is asked about the developer folder,
    as doctor does (cmd_env._xcode_problem)."""
    pytest.importorskip("setuptools")  # the script runs where mypyc imported setuptools (.venv)
    monkeypatch.setenv("CC", "/usr/bin/env")  # a program that exists in /usr/bin everywhere
    monkeypatch.delenv("LDSHARED", raising=False)
    module = _load_build_script()
    asked: list[bool] = []

    def xcode_problem() -> str | None:
        asked.append(True)
        return xcode

    monkeypatch.setattr(module, "xcode_problem", xcode_problem)
    problem = module.missing_compiler(platform)
    if shim:
        assert problem is not None and "/usr/bin/env is an Xcode shim: no Xcode Command Line Tools" in problem
    else:
        assert problem is None
    assert asked == ([] if platform == "linux" else [True])  # once for the compiler and the linker


def test_build_script_reads_the_developer_folder_like_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_build_script()
    assert module.XCODE_CLANG == cmd_env.XCODE_CLANG  # mirrored: the script cannot import the runner
    answer: list[subprocess.CompletedProcess[str] | OSError] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["/usr/bin/xcode-select", "-p"]
        result = answer[-1]
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(module, "subprocess", types.SimpleNamespace(run=run))
    answer.append(FileNotFoundError("xcode-select"))
    assert module.xcode_problem() == "no xcode-select"
    answer.append(subprocess.CompletedProcess([], 2, "", "xcode-select: error: unable to get active developer directory"))
    assert module.xcode_problem() == "no Xcode Command Line Tools"
    answer.append(subprocess.CompletedProcess([], 0, f"{tmp_path}\n", ""))
    assert "has no clang" in (module.xcode_problem() or "")
    (tmp_path / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "usr" / "bin" / "clang").write_text("", encoding="utf-8")
    assert module.xcode_problem() is None


@needs_venv
@pytest.mark.skipif(not _has_mypyc() or os.name == "nt", reason="needs mypyc (run through ./deploy selftest); CC is not MSVC")
def test_real_compile_with_a_missing_cc_is_a_missing_requirement(src_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured case: CC=clang-99 -> `No such file or directory: 'clang-99'` and exit 1."""
    _project(src_tree, {"main.py": "", "pkg/__init__.py": "", "pkg/core/__init__.py": "", "pkg/core/m.py": "X = 1\n"})
    monkeypatch.setattr(mypyc, "BUILD", tmp_path / ".build")
    monkeypatch.setenv("CC", "/nonexistent/clang-99")
    with pytest.raises(DeployError) as err:
        mypyc.build(make({"app": {"name": "pkg"}, "compile": {"modules": ["pkg.core"]}}), "dev")
    assert err.value.code == 3 and "the C compiler cannot start" in str(err.value)


@pytest.mark.parametrize(
    ("raised", "stderr"), [(SystemExit("error: command 'gcc' failed: No such file or directory"), "command 'gcc' failed"), (RuntimeError("boom"), "boom")]
)
def test_build_script_reports_a_failed_c_build_with_its_own_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raised: BaseException, stderr: str
) -> None:
    # setuptools' SystemExit("error: ...") left the script with exit 1, the code `uv run --locked`
    # itself fails with (a stale uv.lock): both got "mypyc needs a C compiler"
    def setup(**kw: Any) -> None:
        raise raised

    fake_build_mod = types.ModuleType("mypyc.build")
    fake_build_mod.mypycify = lambda args, **kw: [FakeExtension("m", [])]  # type: ignore[attr-defined]
    fake_setuptools = types.ModuleType("setuptools")
    fake_setuptools.setup = setup  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mypyc", types.ModuleType("mypyc"))
    monkeypatch.setitem(sys.modules, "mypyc.build", fake_build_mod)
    monkeypatch.setitem(sys.modules, "setuptools", fake_setuptools)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mypyc_build.py", str(_spec_file(tmp_path))])
    module = _load_build_script()
    monkeypatch.setattr(module, "compiler_type", lambda: "unix")
    monkeypatch.setattr(module, "missing_compiler", lambda: None)
    assert module.main() == module.C_BUILD_FAILED == mypyc.C_BUILD_FAILED
    codes = {0, 1, 2, module.MYPYC_REJECTED, module.COMPILER_MISSING}
    assert module.C_BUILD_FAILED not in codes  # never a code uv or Python uses, nor another outcome
    assert stderr in capsys.readouterr().err


@pytest.mark.skipif(not _has_mypyc(), reason="needs mypyc (run through ./deploy selftest)")
def test_build_script_exits_with_rejected_on_type_errors(tmp_path: Path) -> None:
    """The real mypyc: a type error is MYPYC_REJECTED (no compiler involved), valid code is 0."""
    stage = tmp_path / "stage"
    stage.mkdir()
    (tmp_path / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    (stage / "m.py").write_text("def f(x: int) -> str:\n    return x\n", encoding="utf-8")
    spec = _spec_file(stage, config=str(tmp_path / "mypy.ini"), cache_dir=str(tmp_path / "cache"), compile=False)
    env = {**proc.base_env(), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, str(TOOLS / "mypyc_build.py"), str(spec)], capture_output=True, text=True, env=env, check=False)
    assert r.returncode == mypyc.MYPYC_REJECTED, r.stdout + r.stderr
    assert "m.py:2: error: Incompatible return value type" in r.stdout
    (stage / "m.py").write_text("def f(x: int) -> int:\n    return x\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(TOOLS / "mypyc_build.py"), str(spec)], capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


# --- 9. hidden imports and the exe stage ----------------------------------------------------------

OTHER_OS = "fcntl" if os.name == "nt" else "winreg"
HIDDEN_SOURCE = (
    "import json\n"
    "import os.path\n"
    "import sys\n"
    "from html import parser\n"  # html/__init__ never imports html.parser
    "from xml.etree import ElementTree\n"
    "from typing import Final\n"  # a name, not a module
    "from os import *\n"
    "from . import util\n"
    "from .util import X\n"
    "from app.core.util import X as Y\n"
    "import app.gone\n"  # a local module that does not exist
    f"if sys.platform == 'never':\n    import {OTHER_OS}\n"  # absent on this OS: Nuitka would abort
    "try:\n    import not_installed_xyz\nexcept ImportError:\n    pass\n"
    "from this import s\n"  # importing `this` prints: the marker line must still be found
    "if False:\n    import zipapp\n"
)


@pytest.fixture
def hidden_project(src_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path, list[list[str]]]:
    _project(
        src_tree,
        {"app/__init__.py": "", "app/core/__init__.py": "", "app/core/util.py": "X = 1\n", "app/core/m.py": HIDDEN_SOURCE},
    )
    stage = tmp_path / "stage"
    shutil.copytree(src_tree, stage)
    _project(stage, {"app/core/m" + EXT: b"", "app/core/util" + EXT: b"", "app__mypyc" + EXT: b"", "app/native/libfoo.so": b""})
    calls: list[list[str]] = []

    def fake_uv(_env: envs.PyEnv, args: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in args]
        assert argv[:3] == ["run", "--locked", "python"] and kw.get("echo") is False
        calls.append(argv)
        r = subprocess.run([sys.executable, *argv[3:]], capture_output=True, text=True, check=False)
        return _done(argv, r.returncode, r.stdout, r.stderr)

    monkeypatch.setattr(mypyc.envs, "uv", fake_uv)
    cfg: Config = config._build(Config, {"app": {"name": "app"}, "compile": {"modules": ["app.core"]}}, "")
    config.validate(cfg)
    return cfg, stage, calls


def test_hidden_imports_resolve_what_the_binaries_import(hidden_project: tuple[Config, Path, list[list[str]]]) -> None:
    cfg, stage, calls = hidden_project
    hidden = set(mypyc.hidden_imports(cfg, stage))
    assert {"app.core.m", "app.core.util", "app__mypyc", "app.core"} <= hidden  # compiled, shared lib, package
    assert {"json", "os.path", "os", "html", "html.parser", "xml.etree", "xml.etree.ElementTree", "typing"} <= hidden
    assert {"this", "zipapp"} <= hidden
    assert not {"sys", "typing.Final", "os.*", "app.core.util.X", "app.gone", OTHER_OS, "not_installed_xyz", "this.s"} & hidden
    assert "app.native.libfoo" not in hidden  # a vendored native file is not an import
    assert len(calls) == 1 and not any("app." in a for a in calls[0][5:])  # the app's own names are never imported


def test_hidden_imports_keep_everything_when_the_check_cannot_run(
    hidden_project: tuple[Config, Path, list[list[str]]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, stage, _ = hidden_project
    monkeypatch.setattr(mypyc.envs, "uv", lambda env, args, **kw: _done([], 2, "", "error: Failed to spawn: `python`"))
    hidden = set(mypyc.hidden_imports(cfg, stage))
    assert "could not check the imports" in capsys.readouterr().err
    assert {"not_installed_xyz", OTHER_OS, "sys", "html", "app.core.m", "app__mypyc"} <= hidden
    assert "html.parser" not in hidden  # candidates need the check


@pytest.mark.parametrize(
    ("source", "line"),
    [
        (b"import json\n\ndef f(:\n", 3),
        ("x = " + " + ".join(["1"] * 100_000) + "\n", 1),  # too deep for the compiler's stack: RecursionError
    ],
    ids=["syntax-error", "too-deep"],
)
def test_hidden_imports_of_an_unparsable_file_is_a_deploy_error(src_tree: Path, tmp_path: Path, source: str | bytes, line: int) -> None:
    _project(src_tree, {"myapp/__init__.py": "", "myapp/core/__init__.py": "", "myapp/core/m.py": source})
    with pytest.raises(DeployError, match=rf"src[/\\]myapp[/\\]core[/\\]m\.py:{line}: cannot parse it with the runner's Python") as err:
        mypyc.hidden_imports(make({}), tmp_path / "stage")
    assert err.value.code == 2


def test_exe_stage_removes_only_the_compiled_sources(src_tree: Path, tmp_path: Path) -> None:
    _project(src_tree, {"main.py": "", "myapp/__init__.py": "", "myapp/ui.py": "", "myapp/core/__init__.py": "", "myapp/core/m.py": ""})
    stage = tmp_path / "stage"
    shutil.copytree(src_tree, stage)
    _project(stage, {"myapp/core/m" + EXT: b"bin", "myapp/core/__pycache__/m.cpython-314.pyc": b"", ".mypy_cache/x": b""})
    dest = tmp_path / "exe"
    _project(dest, {"leftover.txt": "old"})
    assert mypyc.exe_stage(make({}), stage, dest) == dest
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == sorted(
        ["main.py", "myapp/__init__.py", "myapp/ui.py", "myapp/core/__init__.py", "myapp/core/m" + EXT]
    )


def test_runtime_env_vars_name_the_compiled_modules(src_tree: Path) -> None:
    _project(src_tree, CORE_TREE)
    env = mypyc.runtime_env_vars(make({"compile": {"exclude": ["myapp.core.sub"]}}))
    assert env == {"PYTEMPLATE_BACKEND": "mypyc", "PYTEMPLATE_COMPILED": "myapp.core.a,myapp.core.subx.k"}


# --- 10. real compiles (a C compiler and mypyc in .venv) ----------------------------------------


def _import_from(stage: Path, code: str, cwd: Path) -> str:
    env = {**proc.base_env(), "PYTHONPATH": str(stage), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([str(TOOL_PYTHON), "-c", code], cwd=cwd, env=env, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


FILE_PROBE = "HERE = __file__\n\n\ndef inside() -> str:\n    return __file__\n"


@needs_venv
@needs_compiler
def test_real_compile_roundtrip(src_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tmp project compiled by the real pipeline: extensions load, `__file__` is the real
    path in a package, the stage stays incremental, and an opt_level change rebuilds."""
    _project(
        src_tree,
        {
            "main.py": "",
            "pkg/__init__.py": "",
            "pkg/core/__init__.py": "",
            "pkg/core/m.py": "from typing import Final\n\nLIMIT: Final = 7\n" + FILE_PROBE,
            "pkg/core/n.py": "from pkg.core.m import LIMIT\n\n\ndef twice() -> int:\n    return LIMIT * 2\n",
        },
    )
    monkeypatch.setattr(mypyc, "BUILD", tmp_path / ".build")
    cfg = make({"app": {"name": "pkg"}, "compile": {"modules": ["pkg.core"]}})
    stage = mypyc.build(cfg, "dev")
    ext = next(p for p in mypyc.extension_files(stage) if p.name.startswith("m."))
    out = _import_from(stage, "import pkg.core.m as m, pkg.core.n as n; print(m.__file__); print(m.HERE); print(n.twice())", tmp_path)
    file, here, twice = out.splitlines()
    assert file == here == str(ext) and twice == "14"  # module-level __file__: the real path (shared lib)
    spec = mypyc.profile(cfg, "dev").dir / "spec.json"
    lib = next(p for p in mypyc.extension_files(stage) if p.name.startswith("pkg__mypyc."))
    mypyc.build(cfg, "dev")  # mypyc dates a new C file 1 s ahead: a fast first build is redone once
    assert json.loads(spec.read_text(encoding="utf-8"))["force"] is False
    mtime = ext.stat().st_mtime_ns
    mypyc.build(cfg, "dev")
    assert ext.stat().st_mtime_ns == mtime  # incremental: nothing rebuilt
    size = lib.stat().st_size
    mypyc.build(make({"app": {"name": "pkg"}, "compile": {"modules": ["pkg.core"], "opt_level": "0"}}), "dev")
    assert lib.stat().st_size != size  # -O0 really rebuilt the shared lib


@needs_venv
@needs_compiler
def test_real_compile_names_namespace_modules_as_python_imports_them(src_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A top-level namespace folder in compile.modules, and an app package without __init__.py
    (PEP 420): mypy named each module from the highest folder holding an __init__.py
    (nsx/fast.py -> fast, pkg/core/bench.py -> core.bench): "mypyc did not generate an extension
    for: nsx.fast", and setuptools could not create core/bench.<ext> (blamed on the C compiler)."""
    _project(
        src_tree,
        {
            "main.py": "",
            "pkg/core/__init__.py": "",
            "pkg/core/bench.py": "def twice(x: int) -> int:\n    return 2 * x\n",
            "nsx/fast.py": "def three() -> int:\n    return 3\n",
        },
    )
    monkeypatch.setattr(mypyc, "BUILD", tmp_path / ".build")
    cfg = make({"app": {"name": "pkg"}, "compile": {"modules": ["pkg.core", "nsx"]}})
    stage = mypyc.build(cfg, "dev")
    stems = sorted(p.relative_to(stage).as_posix().split(".")[0] for p in mypyc.extension_files(stage))
    assert stems == ["nsx/fast", "pkg/core/bench", "pkg__mypyc"]
    code = "import pkg.core.bench as b, nsx.fast as f; print(b.twice(f.three()), type(f.three).__name__)"
    assert _import_from(stage, code, tmp_path) == "6 builtin_function_or_method"  # compiled, not the .py


@needs_venv
@needs_compiler
def test_real_compile_separate_names_one_lib_per_module(src_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compile.separate = true: mypyc builds `<module>__mypyc` next to each shim (the names
    remove_stale_extensions keeps); a second build deletes none of them."""
    _project(src_tree, {"main.py": "", "pkg/__init__.py": "", "pkg/core/__init__.py": "", "pkg/core/m.py": "X = 1\n", "pkg/core/n.py": "Y = 2\n"})
    monkeypatch.setattr(mypyc, "BUILD", tmp_path / ".build")
    cfg = make({"app": {"name": "pkg"}, "compile": {"modules": ["pkg.core"], "separate": True}})
    stage = mypyc.build(cfg, "dev")
    stems = sorted(p.relative_to(stage).as_posix().split(".")[0] for p in mypyc.extension_files(stage))
    assert stems == ["pkg/core/m", "pkg/core/m__mypyc", "pkg/core/n", "pkg/core/n__mypyc"]
    before = {p: p.stat().st_mtime_ns for p in mypyc.extension_files(stage)}
    mypyc.build(cfg, "dev")
    assert set(mypyc.extension_files(stage)) == set(before)
    assert _import_from(stage, "import pkg.core.m as m, pkg.core.n as n; print(m.X + n.Y)", tmp_path) == "3"


@needs_venv
@needs_compiler
def test_real_compile_single_top_level_module_sees_a_relative_file(src_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the case lintc's module-level `__file__` rule exists for. If this starts failing
    after a mypy bump, mypyc fixed it: drop the rule (lintc.relative_file_at_import)."""
    _project(src_tree, {"main.py": "", "solo.py": FILE_PROBE})
    monkeypatch.setattr(mypyc, "BUILD", tmp_path / ".build")
    cfg = make({"app": {"name": "solo-app"}, "compile": {"modules": ["solo"]}})
    assert lintc.relative_file_at_import(cfg)
    stage = mypyc.build(cfg, "dev")
    here, inside = _import_from(stage, "import solo; print(solo.HERE); print(solo.inside())", tmp_path).splitlines()
    assert not os.path.isabs(here) and here.startswith("solo.")
    assert os.path.isabs(inside) and Path(inside).parent == stage


# --- 11. the compiled-module sections of the type-checker configs --------------------------------


def _ini(text: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser()  # what mypy uses: it refuses a repeated section
    parser.read_string(text)
    return parser


COMPILED_KEYS = list(render.load_profile("mypyc")["mypy_compiled"])


@pytest.mark.parametrize("for_compile", [False, True])
def test_mypy_ini_relaxes_compile_exclude(for_compile: bool, tmp_path: Path) -> None:
    cfg = make({"compile": {"modules": ["myapp.core"], "exclude": ["myapp.core.loose", "myapp.core.sub"]}})
    ini = _ini(render.mypy_ini(cfg, "mypyc", for_compile=tmp_path if for_compile else None))
    assert COMPILED_KEYS and all(ini.getboolean("mypy-myapp.core.*", key) for key in COMPILED_KEYS)
    for section in ("mypy-myapp.core.loose.*", "mypy-myapp.core.sub.*"):  # x.* covers x itself too
        assert all(ini.getboolean(section, key) is False for key in COMPILED_KEYS)


PROJECT_FOLDERS = ["a,b"] + ([] if os.name == "nt" else ["a:b"])  # mypy splits mypy_path on both


@needs_venv
@pytest.mark.parametrize("folder", PROJECT_FOLDERS)
def test_compile_mypy_ini_finds_typings_in_any_project_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str) -> None:
    """The compile-time mypy.ini held typings/ as an absolute path, which mypy splits on ',' and
    ':': from a project folder with a comma mypyc lost the project's stubs (the raylib preset's)."""
    root = _project(
        tmp_path / folder / "proj",
        {
            "typings/fastlib/__init__.pyi": "def f(x: int) -> int: ...\n",
            ".build/mypyc-dev/stage/usefast.py": "import fastlib\n\n\ndef g(x: int) -> int:\n    return fastlib.f(x)\n",
        },
    )
    monkeypatch.setattr(render, "ROOT", root)
    ini_dir = root / ".build" / "mypyc-dev"
    text = render.mypy_ini(make({}), "mypyc", for_compile=ini_dir)
    assert "mypy_path = $MYPY_CONFIG_FILE_DIR/../../typings\n" in text
    (ini_dir / "mypy.ini").write_text(text, encoding="utf-8")
    argv = [str(TOOL_PYTHON), "-m", "mypy", "--config-file", str(ini_dir / "mypy.ini"), "--no-incremental", "usefast.py"]
    r = subprocess.run(argv, cwd=ini_dir / "stage", env=proc.base_env(), capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr  # mypyc runs mypy the same way: from the stage


@pytest.mark.parametrize("supported", [["cpython", "mypyc"], ["cpython", "pypy", "mypyc"]])
def test_mypy_ini_checks_as_min_python_while_pypy_is_supported(supported: list[str], tmp_path: Path) -> None:
    """VS Code's mypy extension reads .mypy.ini and passes no --python-version: without
    python_version there it checked as the .venv's Python and missed the 3.11 API errors that
    `./deploy check` (render.mypy_cli_args) and the Neovim linter report."""
    cfg = make({"backend": {"supported": supported}})
    expected = cfg.min_python if cfg.pypy_enabled else None
    assert (expected == "3.11") is ("pypy" in supported)
    for profile in ("strict", "mypyc", "warn", "off"):
        assert _ini(render.mypy_ini(cfg, profile)).get("mypy", "python_version", fallback=None) == expected
    # mypyc compiles for the interpreter it runs on: the compile-time ini never pins another one
    assert _ini(render.mypy_ini(cfg, "mypyc", for_compile=tmp_path)).get("mypy", "python_version", fallback=None) is None


@needs_venv
def test_mypy_reading_the_generated_ini_alone_flags_what_pypy_lacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the extension runs: mypy from the project folder, the .venv's interpreter, no
    arguments. It must find the 3.12+ API, with no python3.11 on PATH and the .venv's packages."""
    root = _project(
        tmp_path,
        {"src/myapp/__init__.py": "", "src/myapp/ov.py": "import pytest\nfrom typing import override\n", "tests/__init__.py": ""},
    )
    monkeypatch.setattr(render, "ROOT", root)
    (root / ".mypy.ini").write_text(render.mypy_ini(make({"backend": {"supported": ["cpython", "pypy"]}}), "strict"), encoding="utf-8")
    (tmp_path / "empty").mkdir()
    env = {**proc.base_env(), "PATH": str(tmp_path / "empty")}
    r = subprocess.run([str(TOOL_PYTHON), "-m", "mypy", "--no-incremental"], cwd=root, env=env, capture_output=True, text=True, check=False)
    assert r.returncode == 1 and 'Module "typing" has no attribute "override"' in r.stdout, r.stdout + r.stderr
    assert "pytest" not in r.stdout  # the .venv's packages, not another environment's


def test_mypy_ini_without_compiled_rules_has_no_exclude_sections() -> None:
    cfg = make({"compile": {"exclude": ["myapp.core.loose"]}})
    assert [s for s in _ini(render.mypy_ini(cfg, "strict")).sections() if s != "mypy"] == []


def test_mypy_ini_merges_overrides_with_the_generated_sections() -> None:
    cfg = make(
        {
            "compile": {"modules": ["myapp.core"], "exclude": ["myapp.core.loose"]},
            "typing": {
                "mypy_overrides": [
                    {"module": "{pkg}.core.*", "warn_return_any": False},
                    {"module": ["{pkg}.core.loose.*", "raylib", "raylib.*"], "ignore_missing_imports": True},
                ]
            },
        }
    )
    ini = _ini(render.mypy_ini(cfg, "mypyc"))  # a repeated section made mypy abort
    core = ini["mypy-myapp.core.*"]
    assert core.getboolean("disallow_any_expr") is True and core.getboolean("warn_return_any") is False
    loose = ini["mypy-myapp.core.loose.*"]
    assert loose.getboolean("ignore_missing_imports") is True and loose.getboolean("disallow_any_explicit") is False
    for name in ("mypy-raylib", "mypy-raylib.*"):  # a list: one section per pattern, same options
        assert dict(ini[name]) == {"ignore_missing_imports": "True"}


@needs_venv
def test_mypy_with_the_generated_ini_accepts_any_in_an_excluded_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(
        tmp_path,
        {
            "src/myapp/__init__.py": "",
            "src/myapp/core/__init__.py": "",
            "src/myapp/core/loose.py": "from typing import Any\n\n\ndef g(x: Any) -> Any:\n    return x\n",
            "src/myapp/core/uses.py": "from myapp.core.loose import g\n\n\ndef h(n: int) -> int:\n    return n\n",
            "tests/__init__.py": "",
        },
    )
    monkeypatch.setattr(render, "ROOT", root)
    ini = root / "mypy.ini"
    ini.write_text(render.mypy_ini(make({"compile": {"exclude": ["myapp.core.loose"]}}), "mypyc"), encoding="utf-8")

    def mypy() -> subprocess.CompletedProcess[str]:
        argv = [str(TOOL_PYTHON), "-m", "mypy", "--config-file", str(ini), "--no-incremental"]
        return subprocess.run(argv, cwd=root, env=proc.base_env(), capture_output=True, text=True, check=False)

    r = mypy()
    assert r.returncode == 0, r.stdout + r.stderr
    (root / "src/myapp/core/uses.py").write_text("from myapp.core.loose import g\n\n\ndef h(n: int) -> int:\n    return int(g(n))\n", encoding="utf-8")
    r = mypy()  # the compiled modules stay strict
    assert r.returncode == 1 and 'uses.py:5: error: Expression has type "Any"' in r.stdout, r.stdout


@pytest.fixture
def pyright_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _project(
        tmp_path,
        {
            "src/myapp/__init__.py": "",
            "src/myapp/core/__init__.py": "",
            "src/myapp/core/bench.py": "",
            "src/myapp/core/loose.py": "",
            "src/myapp/core/notes.txt": "",
            "src/myapp/core/sub/__init__.py": "",
            "src/myapp/core/sub/m.py": "",
            "src/myapp/core/other/__init__.py": "",
            "src/myapp/core/__pycache__/bench.cpython-314.pyc": "",
            "tests/__init__.py": "",
        },
    )
    monkeypatch.setattr(render, "ROOT", root)
    monkeypatch.setattr(config, "SRC", root / "src")
    return root


def test_pyright_config_leaves_compile_exclude_out_of_the_compiled_rules(pyright_tree: Path) -> None:
    cfg = make({"typing": {"editor": "basedpyright"}, "compile": {"exclude": ["myapp.core.loose", "myapp.core.sub.m"]}})
    conf = render.pyright_config(cfg, "mypyc")
    assert conf["strict"] == [
        "src/myapp/core/__init__.py", "src/myapp/core/bench.py", "src/myapp/core/other", "src/myapp/core/sub/__init__.py",
    ]  # fmt: skip
    # basedpyright: the first environment that matches wins, so the excluded files come first
    envs_ = conf["executionEnvironments"]
    assert [e["root"] for e in envs_] == ["src/myapp/core/loose.py", "src/myapp/core/sub/m.py", *conf["strict"]]
    assert all("reportAny" not in e for e in envs_[:2]) and all(e.get("reportAny") == "error" for e in envs_[2:])
    absolute = render.pyright_config(cfg, "mypyc", absolute=True)
    assert absolute["strict"][0] == (pyright_tree / "src/myapp/core/__init__.py").as_posix()


def test_pyright_strict_list_never_names_a_folder_without_python(pyright_tree: Path) -> None:
    """A subpackage deleted with `git rm -r` leaves its ignored __pycache__ behind: listed, it made
    the committed pyrightconfig.json differ from a fresh clone's (CI's `render --check` failed)."""
    cfg = make({"typing": {"editor": "basedpyright"}, "compile": {"exclude": ["myapp.core.loose", "myapp.core.sub.m"]}})
    before = render.pyright_config(cfg, "mypyc")
    _project(
        pyright_tree,
        {
            "src/myapp/core/old/__pycache__/m.cpython-314.pyc": b"",
            "src/myapp/core/old/deeper/__pycache__/n.cpython-314.pyc": b"",
            "src/myapp/core/.hidden/h.py": "",
            "src/myapp/core/data/table.json": "{}",
        },
    )
    (pyright_tree / "src/myapp/core/empty").mkdir()
    after = render.pyright_config(cfg, "mypyc")
    assert after["strict"] == before["strict"] and after["executionEnvironments"] == before["executionEnvironments"]
    _project(pyright_tree, {"src/myapp/core/old/deeper/stub.pyi": ""})  # a folder that holds code counts
    assert "src/myapp/core/old" in render.pyright_config(cfg, "mypyc")["strict"]


def test_pyright_environments_never_name_a_leftover_folder(pyright_tree: Path) -> None:
    """The excluded module loose.py and the compiled module bench.py each next to a folder left
    holding only __pycache__ (a package turned into a module): basedpyright's
    executionEnvironments and the strict list stay what a fresh clone renders."""
    for modules, exclude in ((["myapp.core"], ["myapp.core.loose"]), (["myapp.core.bench"], [])):
        cfg = make({"typing": {"editor": "basedpyright"}, "compile": {"modules": modules, "exclude": exclude}})
        before = render.pyright_config(cfg, "mypyc")
        for leftover in ("loose", "bench"):
            _project(pyright_tree, {f"src/myapp/core/{leftover}/__pycache__/x.cpython-314.pyc": b""})
        after = render.pyright_config(cfg, "mypyc")
        assert after["executionEnvironments"] == before["executionEnvironments"]
        assert after["strict"] == before["strict"]
        for leftover in ("loose", "bench"):
            shutil.rmtree(pyright_tree / "src/myapp/core" / leftover)
    # a compiled module that is gone, its folder left behind: what a clone without the folder renders
    cfg = make({"typing": {"editor": "basedpyright"}, "compile": {"modules": ["myapp.core.gone"]}})
    clone = render.pyright_config(cfg, "mypyc")
    _project(pyright_tree, {"src/myapp/core/gone/__pycache__/x.cpython-314.pyc": b""})
    assert render.pyright_config(cfg, "mypyc") == clone


def test_a_tests_folder_without_python_is_no_code_folder(pyright_tree: Path) -> None:
    """The same for a tests/ folder left holding only __pycache__ (.mypy.ini files, pyright include)."""
    shutil.rmtree(pyright_tree / "tests")
    _project(pyright_tree, {"tests/__pycache__/test_x.cpython-314-pytest-9.1.1.pyc": b""})
    assert "\nfiles = src\n" in render.mypy_ini(make({}), "strict")
    assert render.pyright_config(make({}), "strict")["include"] == ["src"]
    _project(pyright_tree, {"tests/test_x.py": ""})
    assert "\nfiles = src, tests\n" in render.mypy_ini(make({}), "strict")
    assert render.pyright_config(make({}), "strict")["include"] == ["src", "tests"]


def test_a_typings_folder_without_stubs_is_no_stub_folder(pyright_tree: Path) -> None:
    """typings/ emptied by hand (`rm -r typings/raylib`: git removes no folder it did not
    delete) is not in a fresh clone: mypy_path and stubPath must not name it."""
    before = (render.mypy_ini(make({}), "strict"), render.pyright_config(make({}), "strict"))
    (pyright_tree / "typings" / "raylib").mkdir(parents=True)
    assert render.typings_dir() is None
    assert (render.mypy_ini(make({}), "strict"), render.pyright_config(make({}), "strict")) == before
    _project(pyright_tree, {"typings/raylib/__init__.pyi": ""})
    assert render.typings_dir() == pyright_tree / "typings"
    assert "\nmypy_path = src, typings\n" in render.mypy_ini(make({}), "strict")
    assert render.pyright_config(make({}), "strict")["stubPath"] == "typings"


def test_pyright_config_without_exclude_is_unchanged(pyright_tree: Path) -> None:
    conf = render.pyright_config(make({"typing": {"editor": "basedpyright"}}), "mypyc")
    assert conf["strict"] == ["src/myapp/core"]
    assert [e["root"] for e in conf["executionEnvironments"]] == ["src/myapp/core"]
    whole = render.pyright_config(make({"compile": {"exclude": ["myapp.core.sub"]}}), "mypyc")
    assert "src/myapp/core/sub" not in whole["strict"] and "src/myapp/core/sub/m.py" not in whole["strict"]


# --- 12. the wheel method -------------------------------------------------------------------------


def _locked(name: str) -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return next(str(p["version"]) for p in lock["package"] if p["name"] == name)


@pytest.fixture
def wheel_project(src_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp project for methods.wheel: src/pkg (compiled core + data files), pyproject, uv.lock."""
    from runner.methods import wheel

    _project(
        src_tree,
        {
            "main.py": "",
            "pkg/__init__.py": "",
            "pkg/app.py": "def main() -> None:\n    print('hi')\n",
            "pkg/py.typed": "",
            "pkg/data/x.json": "{}",
            "pkg/native/libfoo.so": b"\x7fELF",  # a vendored native library travels with the package
            "pkg/core/__init__.py": "",
            "pkg/core/m.py": "from rich.text import Text\n\n\ndef plain(text: str) -> str:\n    return Text(text).plain\n",
            "pkg/core/m" + LINUX_EXT: b"stray",  # a stray in-place build: never packaged
            "pkg/core/m__mypyc" + LINUX_EXT: b"stray",
            "pkg__mypyc" + LINUX_EXT: b"stray",
            "pkg/__pycache__/app.cpython-314.pyc": b"",
            "assets/img.txt": "img",
        },
    )
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\nversion = "0.1.0"\ndescription = "Say \\"hi\\" \\\\ caf\\u00e9"\n'
        'requires-python = ">=3.14"\ndependencies = ["rich>=15"]\n',
        encoding="utf-8",
    )
    shutil.copy2(ROOT / "uv.lock", root / "uv.lock")
    monkeypatch.setattr(wheel, "SRC", src_tree)
    monkeypatch.setattr(wheel, "BUILD", tmp_path / ".build")
    monkeypatch.setattr(wheel, "PYPROJECT", root / "pyproject.toml")
    monkeypatch.setattr(wheel, "dist_path", lambda req, suffix="": tmp_path / "dist" / (req.out_name + suffix))
    return tmp_path


def _wheel_cfg(**extra: Any) -> Config:
    return make({"app": {"name": "pkg", "assets": "assets", **extra}, "compile": {"modules": ["pkg.core"]}})


@pytest.mark.parametrize("backend", ["cpython", "mypyc", "pypy"])
def test_wheel_builds_in_the_locked_tools_env(wheel_project: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    calls: list[list[str]] = []

    def fake_uv(env: envs.PyEnv, args: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in args]
        calls.append(argv)
        assert kw.get("extra_env") == {"VSLANG": "1033"}
        out = Path(argv[argv.index("--out-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "pkg-0.1.0-py3-none-any.whl").write_bytes(b"")
        return _done(argv)

    monkeypatch.setattr(wheel.envs, "sync", lambda env, **kw: calls.append(["sync", str(env.dir)]))
    monkeypatch.setattr(wheel.envs, "uv", fake_uv)
    cfg = _wheel_cfg()
    supported = {"supported": ["cpython", "pypy", "mypyc"]} if backend == "pypy" else {}
    if supported:
        cfg = make({"app": {"name": "pkg", "assets": "assets"}, "backend": supported, "compile": {"modules": ["pkg.core"]}})
    result = wheel.build(BuildRequest(cfg, backend, "wheel", wheel_project / "src"))
    assert result.name.endswith(".whl")
    tool = envs.tool_env(cfg)
    assert calls[0] == ["sync", str(tool.dir)]  # synced before building
    build = calls[1]
    assert build[:2] == ["build", "--wheel"] and "--no-build-isolation" in build
    assert build[build.index("--python") + 1] == str(tool.python)  # never uv's own pick (./.venv, WSL)
    work = wheel_project / ".build" / "wheel" / backend
    assert build[-1] == str(work)
    assert (work / "setup.py").is_file() is (backend == "mypyc") and (work / "mypy.ini").is_file() is (backend == "mypyc")


def test_wheel_keeps_the_previous_wheel_when_the_sync_fails(wheel_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The previous dist/ folder went first, then `uv sync --locked` failed (a stale uv.lock, a
    # package not in the cache offline): no wheel at all was left
    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    previous = wheel_project / "dist" / "pkg-cpython-wheel"
    previous.mkdir(parents=True)
    (previous / "pkg-0.1.0-py3-none-any.whl").write_bytes(b"old wheel")

    def sync(env: envs.PyEnv, **kw: Any) -> None:
        raise proc.CommandFailed(["uv", "sync", "--locked"], 1)

    monkeypatch.setattr(wheel.envs, "sync", sync)
    monkeypatch.setattr(wheel.envs, "uv", lambda *a, **k: pytest.fail("no uv build after a failed sync"))
    with pytest.raises(proc.CommandFailed):
        wheel.build(BuildRequest(_wheel_cfg(), "cpython", "wheel", wheel_project / "src"))
    assert (previous / "pkg-0.1.0-py3-none-any.whl").read_bytes() == b"old wheel"


def test_wheel_pyproject_is_exact_and_ships_the_package_data(wheel_project: Path) -> None:
    from runner.methods import wheel

    for compiled in (False, True):
        data = tomllib.loads(wheel._pyproject(_wheel_cfg(), compiled))
        requires = data["build-system"]["requires"]
        assert requires == [f"setuptools=={_locked('setuptools')}", *([f"mypy=={_locked('mypy')}"] if compiled else [])]
        assert data["project"]["description"] == 'Say "hi" \\ caf' + chr(0xE9)  # quotes, a backslash, non-ASCII
        assert data["project"]["dependencies"] == ["rich>=15"]
        assert data["project"]["scripts"] == {"pkg": "pkg.app:main"} and "gui-scripts" not in data["project"]
        assert data["tool"]["setuptools"]["package-data"] == {"pkg": ["**/*"]}
    gui = tomllib.loads(wheel._pyproject(_wheel_cfg(gui=True), False))
    assert gui["project"]["gui-scripts"] == {"pkg": "pkg.app:main"} and "scripts" not in gui["project"]
    entry = tomllib.loads(wheel._pyproject(make({"app": {"name": "pkg"}, "deploy": {"wheel": {"entry": "pkg.ui:run"}}}), False))
    assert entry["project"]["scripts"] == {"pkg": "pkg.ui:run"}


def test_wheel_names_a_missing_lock_entry(wheel_project: Path) -> None:
    from runner.methods import wheel

    lock = wheel.PYPROJECT.parent / "uv.lock"
    lock.write_text('version = 1\n[[package]]\nname = "mypy"\nversion = "2.3.1"\n', encoding="utf-8")
    with pytest.raises(DeployError, match=r"setuptools is not in uv.lock.*\./deploy add setuptools --dev --cpython-only"):
        wheel._pyproject(_wheel_cfg(), False)


def test_wheel_copies_the_package_files(wheel_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    def fake_uv(env: envs.PyEnv, args: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        out = Path(str(args[args.index("--out-dir") + 1]))
        out.mkdir(parents=True, exist_ok=True)
        (out / "pkg-0.1.0-py3-none-any.whl").write_bytes(b"")
        return _done([])

    monkeypatch.setattr(wheel.envs, "sync", lambda env, **kw: None)
    monkeypatch.setattr(wheel.envs, "uv", fake_uv)
    wheel.build(BuildRequest(_wheel_cfg(), "cpython", "wheel", wheel_project / "src"))
    pkg = wheel_project / ".build" / "wheel" / "cpython" / "src" / "pkg"
    files = sorted(p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file())
    assert files == [
        "__init__.py", "app.py", "assets/img.txt", "core/__init__.py", "core/m.py", "data/x.json", "native/libfoo.so", "py.typed",
    ]  # fmt: skip


def _top_level_cfg() -> Config:
    """compile.modules naming a lone top-level module and another top-level package of src/."""
    return make({"app": {"name": "pkg", "assets": "assets"}, "compile": {"modules": ["pkg.core", "fastbench", "other.core"]}})


def _add_top_level_modules(src: Path) -> None:
    _project(
        src,
        {
            "fastbench.py": "def twice(n: int) -> int:\n    return 2 * n\n",
            "other/__init__.py": "",
            "other/core/__init__.py": "",
            "other/core/calc.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
            "other/core/calc" + LINUX_EXT: b"stray",  # a stray in-place build: never packaged
        },
    )


@pytest.mark.parametrize("backend", ["cpython", "mypyc"])
def test_wheel_carries_compiled_modules_outside_the_package(wheel_project: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    # compile.modules = ["fastbench"] (a module of src/): the build project held only src/pkg/, so
    # mypycify stopped with "Cannot read file 'src/fastbench.py'" and a cpython wheel left it out
    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    def fake_uv(env: envs.PyEnv, args: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        out = Path(str(args[args.index("--out-dir") + 1]))
        out.mkdir(parents=True, exist_ok=True)
        (out / "pkg-0.1.0-py3-none-any.whl").write_bytes(b"")
        return _done([])

    _add_top_level_modules(wheel_project / "src")
    monkeypatch.setattr(wheel.envs, "sync", lambda env, **kw: None)
    monkeypatch.setattr(wheel.envs, "uv", fake_uv)
    cfg = _top_level_cfg()
    wheel.build(BuildRequest(cfg, backend, "wheel", wheel_project / "src"))
    work = wheel_project / ".build" / "wheel" / backend
    files = sorted(p.relative_to(work / "src").as_posix() for p in (work / "src").rglob("*") if p.is_file() and not p.is_relative_to(work / "src" / "pkg"))
    assert files == ["fastbench.py", "other/__init__.py", "other/core/__init__.py", "other/core/calc.py"]
    data = tomllib.loads((work / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["setuptools"]["py-modules"] == ["fastbench"]
    assert data["tool"]["setuptools"]["package-data"] == {"pkg": ["**/*"], "other": ["**/*"]}
    if backend == "mypyc":  # every file mypycify gets exists in the build project
        listed = re.findall(r"'(src/[^']+\.py)'", (work / "setup.py").read_text(encoding="utf-8"))
        assert "src/fastbench.py" in listed and all((work / f).is_file() for f in listed)
    else:  # a compile.modules entry that names nothing: a cpython wheel is built as before
        missing = make({"app": {"name": "pkg"}, "compile": {"modules": ["pkg.core", "gone"]}})
        wheel.build(BuildRequest(missing, backend, "wheel", wheel_project / "src"))
        assert "py-modules" not in (work / "pyproject.toml").read_text(encoding="utf-8")


@needs_venv
def test_real_pure_wheel_holds_a_top_level_module(wheel_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    _add_top_level_modules(wheel_project / "src")
    monkeypatch.setattr(wheel.envs, "sync", lambda env, **kw: None)
    names = _wheel_names(wheel.build(BuildRequest(_top_level_cfg(), "cpython", "wheel", wheel_project / "src")))
    assert {"fastbench.py", "other/__init__.py", "other/core/calc.py", "pkg/app.py"} <= set(names)
    assert not [n for n in names if n.endswith(LINUX_EXT)]


@needs_venv
@needs_compiler
def test_real_mypyc_wheel_compiles_a_top_level_module(wheel_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import zipfile

    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    _add_top_level_modules(wheel_project / "src")
    monkeypatch.setattr(wheel.envs, "sync", lambda env, **kw: None)
    cfg = make({"app": {"name": "pkg"}, "compile": {"modules": ["fastbench", "other.core"]}})
    built = wheel.build(BuildRequest(cfg, "mypyc", "wheel", wheel_project / "src"))
    site = wheel_project / "site"
    with zipfile.ZipFile(built) as z:
        z.extractall(site)
    out = _import_from(site, "import fastbench, other.core.calc as c; print(fastbench.__file__); print(fastbench.twice(c.add(1, 2)))", wheel_project)
    file, result = out.splitlines()
    assert file.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)) and result == "6"


@needs_venv
@needs_compiler
def test_real_mypyc_wheel_compiles_a_namespace_folder(wheel_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # nsx/ (no __init__.py) in compile.modules: the wheel's setup.py named nsx/fast.py "fast",
    # a top-level module the wheel does not have where nsx.fast is imported
    import zipfile

    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    _project(wheel_project / "src", {"nsx/fast.py": "def three() -> int:\n    return 3\n"})
    monkeypatch.setattr(wheel.envs, "sync", lambda env, **kw: None)
    cfg = make({"app": {"name": "pkg"}, "compile": {"modules": ["nsx"]}})
    built = wheel.build(BuildRequest(cfg, "mypyc", "wheel", wheel_project / "src"))
    site = wheel_project / "site"
    with zipfile.ZipFile(built) as z:
        assert [n for n in z.namelist() if n.startswith("nsx/fast.") and n.endswith((".so", ".pyd"))], z.namelist()
        z.extractall(site)
    out = _import_from(site, "import nsx.fast as f; print(f.three(), type(f.three).__name__)", wheel_project)
    assert out == "3 builtin_function_or_method"


def _wheel_names(wheel_file: Path) -> list[str]:
    import zipfile

    with zipfile.ZipFile(wheel_file) as z:
        return z.namelist()


@needs_venv
def test_real_pure_wheel(wheel_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv build without isolation in .venv: works offline, ships the data files, gui-scripts."""
    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    monkeypatch.setattr(wheel.envs, "sync", lambda env, **kw: None)  # selftest's .venv is synced
    built = wheel.build(BuildRequest(_wheel_cfg(gui=True), "cpython", "wheel", wheel_project / "src"))
    assert built.name == "pkg-0.1.0-py3-none-any.whl"
    names = _wheel_names(built)
    for name in ("pkg/data/x.json", "pkg/py.typed", "pkg/native/libfoo.so", "pkg/assets/img.txt", "pkg/core/m.py"):
        assert name in names
    assert not [n for n in names if n.endswith(LINUX_EXT) or "__pycache__" in n]
    import zipfile

    with zipfile.ZipFile(built) as z:
        entry_points = z.read("pkg-0.1.0.dist-info/entry_points.txt").decode()
    assert "[gui_scripts]" in entry_points and "pkg = pkg.app:main" in entry_points


@needs_venv
@needs_compiler
def test_real_mypyc_wheel_compiles_code_that_imports_a_dependency(wheel_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The compiled core imports rich (a project dependency): the isolated build env had only
    setuptools + mypy, so mypycify failed with import-not-found."""
    import zipfile

    if subprocess.run([str(TOOL_PYTHON), "-I", "-c", "import rich"], capture_output=True, check=False).returncode != 0:
        pytest.skip("imports rich (the script preset's dependency), which this project's .venv does not have")
    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    monkeypatch.setattr(wheel.envs, "sync", lambda env, **kw: None)
    built = wheel.build(BuildRequest(_wheel_cfg(), "mypyc", "wheel", wheel_project / "src"))
    assert "-cp3" in built.name and not built.name.endswith("-none-any.whl")
    site = wheel_project / "site"
    with zipfile.ZipFile(built) as z:
        compiled = [i for i in z.infolist() if i.filename.startswith("pkg/core/m.") and i.filename.endswith((".so", ".pyd"))]
        assert len(compiled) == 1 and compiled[0].file_size > 1000  # the real build, not the stray file
        z.extractall(site)
    out = _import_from(site, "import pkg.core.m as m; print(m.__file__); print(m.plain('hi'))", wheel_project)
    file, said = out.splitlines()
    assert file.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)) and said == "hi"



# --- 13. the C flags added to mypyc's own (tools/mypyc_build.py and the wheel's setup.py) --------

CFLAG_CASES = [
    ("unix", "linux", True, ["-fno-strict-overflow", "-fno-semantic-interposition"]),
    ("unix", "linux", False, ["-fno-strict-overflow"]),
    ("unix", "darwin", True, ["-fno-strict-overflow"]),  # Apple clang: semantic interposition not verified
    ("unix", "cygwin", True, ["-fno-strict-overflow"]),
    ("msvc", "win32", True, []),
    ("msvc", "win32", False, []),
    ("mingw32", "win32", True, []),
]


@pytest.mark.parametrize(("compiler", "platform", "nsi", "flags"), CFLAG_CASES)
def test_extra_cflags(compiler: str, platform: str, nsi: bool, flags: list[str]) -> None:
    assert _load_build_script().extra_cflags(compiler, platform, nsi) == flags


@pytest.mark.parametrize("nsi", [True, False])
def test_build_script_adds_the_flags_to_every_extension_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nsi: bool) -> None:
    """mypycify hands ONE list object to every extension: appending to it would repeat the
    flags once per extension (and leak into the next build in the same process)."""
    module, extensions, setups = _run_build_script(tmp_path, monkeypatch, no_semantic_interposition=nsi)
    flags = module.extra_cflags("unix", sys.platform, nsi)
    assert "-fno-strict-overflow" in flags and ("-fno-semantic-interposition" in flags) is (nsi and sys.platform == "linux")
    for ext in extensions:
        assert ext.extra_compile_args == ["-O3", "-Werror", *flags]  # after mypyc's own, once
    assert extensions[0].extra_compile_args is not extensions[1].extra_compile_args
    assert setups[-1]["ext_modules"] is extensions


def test_build_script_adds_nothing_for_msvc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, extensions, setups = _run_build_script(tmp_path, monkeypatch, compiler="msvc")
    assert [e.extra_compile_args for e in extensions] == [["-O3", "-Werror"]] * 2 and setups


def test_build_script_without_compile_never_looks_for_a_compiler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """./deploy report (spec compile = false) needs no C compiler: none is looked up."""
    _, extensions, setups = _run_build_script(tmp_path, monkeypatch, compiler="none", compile=False)
    assert setups == [] and extensions[0].extra_compile_args == ["-O3", "-Werror"]


@needs_venv
def test_build_script_compiler_type_is_setuptools_own(tmp_path: Path) -> None:
    """compiler_type() in the tools env (real setuptools): what mypycify reads for its flags."""
    code = (
        "import importlib.util, sys\n"
        "import setuptools\n"  # what mypyc.build imports first
        f"spec = importlib.util.spec_from_file_location('b', {str(TOOLS / 'mypyc_build.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print(m.compiler_type())\n"
    )
    r = subprocess.run([str(TOOL_PYTHON), "-c", code], cwd=tmp_path, env=proc.base_env(), capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == ("msvc" if os.name == "nt" else "unix")


def _fake_modules(monkeypatch: pytest.MonkeyPatch, compiler: str, extensions: list[FakeExtension], setups: list[dict[str, Any]]) -> None:
    """mypyc.build, setuptools and distutils (setuptools' copy) as the generated setup.py uses them."""
    fake_build_mod = types.ModuleType("mypyc.build")
    fake_build_mod.mypycify = lambda args, **kw: extensions  # type: ignore[attr-defined]
    fake_setuptools = types.ModuleType("setuptools")
    fake_setuptools.setup = lambda **kw: setups.append(kw)  # type: ignore[attr-defined]
    fake_ccompiler = types.ModuleType("distutils.ccompiler")
    fake_ccompiler.new_compiler = lambda: types.SimpleNamespace(compiler_type=compiler)  # type: ignore[attr-defined]
    fake_sysconfig = types.ModuleType("distutils.sysconfig")
    fake_sysconfig.customize_compiler = lambda c: None  # type: ignore[attr-defined]
    fake_distutils = types.ModuleType("distutils")
    fake_distutils.ccompiler = fake_ccompiler  # type: ignore[attr-defined]
    fake_distutils.sysconfig = fake_sysconfig  # type: ignore[attr-defined]
    for name, module in {
        "mypyc": types.ModuleType("mypyc"),
        "mypyc.build": fake_build_mod,
        "setuptools": fake_setuptools,
        "distutils": fake_distutils,
        "distutils.ccompiler": fake_ccompiler,
        "distutils.sysconfig": fake_sysconfig,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.mark.parametrize(("compiler", "platform", "nsi", "flags"), CFLAG_CASES)
def test_wheel_setup_py_adds_the_same_flags_as_the_stage(
    wheel_project: Path, monkeypatch: pytest.MonkeyPatch, compiler: str, platform: str, nsi: bool, flags: list[str]
) -> None:
    """The wheel's generated setup.py mirrors tools/mypyc_build.py (both compile the same code)."""
    from runner.methods import wheel

    code = wheel.setup_py(make({"app": {"name": "pkg"}, "compile": {"modules": ["pkg.core"], "no_semantic_interposition": nsi}}))
    shared = ["-O3"]
    extensions = [FakeExtension("pkg__mypyc", shared), FakeExtension("pkg.core.m", shared)]
    setups: list[dict[str, Any]] = []
    _fake_modules(monkeypatch, compiler, extensions, setups)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", platform)
        exec(compile(code, "setup.py", "exec"), {"__name__": "__main__"})
    assert flags == _load_build_script().extra_cflags(compiler, platform, nsi)
    assert [e.extra_compile_args for e in extensions] == [["-O3", *flags]] * 2
    assert extensions[0].extra_compile_args is not extensions[1].extra_compile_args
    assert setups == [{"ext_modules": extensions}]


INLINE_PROBE = """\
from mypy_extensions import i64


def pt_callee(x: i64) -> i64:
    return x + 1


def pt_caller(n: i64) -> i64:
    total: i64 = 0
    i: i64 = 0
    while i < n:
        total = pt_callee(total)
        i += 1
    return total


def pt_wrap(x: i64) -> i64:
    return x + 1
"""


@pytest.fixture
def logging_cc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[list[str]]]:
    """CC = a sh wrapper that logs every command line, and a user CFLAGS (it REPLACES Python's
    own flags in setuptools). Returns a function that reads (and resets) the compile lines."""
    if os.name == "nt":
        pytest.skip("the logging compiler wrapper is a sh script")
    log = tmp_path / "cc.log"
    wrapper = tmp_path / "cc.sh"
    wrapper.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$PT_CC_LOG\"\nexec $PT_REAL_CC \"$@\"\n", encoding="utf-8")
    monkeypatch.setenv("PT_REAL_CC", os.environ.get("CC") or sysconfig.get_config_var("CC") or "cc")
    monkeypatch.setenv("PT_CC_LOG", str(log))
    monkeypatch.setenv("CC", f"sh {shlex.quote(str(wrapper))}")
    monkeypatch.setenv("CFLAGS", "-DPT_USER_CFLAGS=1")

    def compiles() -> list[list[str]]:
        lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        log.unlink(missing_ok=True)
        return [argv for argv in (ln.split() for ln in lines) if "-c" in argv]

    return compiles


def _check_flags(compiles: list[list[str]], nsi: bool) -> None:
    assert compiles, "no compile command went through the wrapper"
    for argv in compiles:
        assert "-DPT_USER_CFLAGS=1" in argv  # the user's CFLAGS stay
        assert "-fno-strict-overflow" in argv  # although they replaced Python's own
        assert ("-fno-semantic-interposition" in argv) is (nsi and sys.platform == "linux")


def _caller_calls_callee(objdump: str, lib: Path) -> bool:
    out = subprocess.run([objdump, "-d", "--no-show-raw-insn", str(lib)], capture_output=True, text=True, check=True).stdout
    block = re.search(r"<CPyDef_\w*pt_caller>:\n(.*?)(?:\n\n|\Z)", out, re.DOTALL)
    assert block, f"CPyDef_*pt_caller not found in the disassembly of {lib.name}"
    return "pt_callee" in block.group(1)


def _is_gcc(cc: str) -> bool:
    r = subprocess.run([*shlex.split(cc), "--version"], capture_output=True, text=True, check=False)
    return "Free Software Foundation" in r.stdout


@needs_venv
@needs_compiler
def test_real_compile_adds_the_c_flags_and_inlines_compiled_calls(
    src_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logging_cc: Callable[[], list[list[str]]]
) -> None:
    """The flags reach the C compiler next to a user's CFLAGS; compiled i64 arithmetic wraps
    silently; on Linux, with -fno-semantic-interposition the call between two compiled
    functions is inlined (gcc then folds the whole loop) and without it gcc keeps a real call
    through the PLT; switching the option really rebuilds (only a C flag changed)."""
    _project(src_tree, {"main.py": "", "pkg/__init__.py": "", "pkg/core/__init__.py": "", "pkg/core/m.py": INLINE_PROBE})
    monkeypatch.setattr(mypyc, "BUILD", tmp_path / ".build")
    cfg = make({"app": {"name": "pkg"}, "compile": {"modules": ["pkg.core"]}})
    assert cfg.compile.no_semantic_interposition is True  # the default
    stage = mypyc.build(cfg, "dev")
    _check_flags(logging_cc(), nsi=True)
    out = _import_from(stage, "import pkg.core.m as m; print(m.pt_caller(1000)); print(m.pt_wrap(2**63 - 1))", tmp_path)
    assert out.splitlines() == ["1000", str(-(2**63))]  # interpreted, pt_wrap gives 2**63
    lib = next(p for p in mypyc.extension_files(stage) if p.name.startswith("pkg__mypyc."))
    objdump = shutil.which("objdump") if sys.platform == "linux" else None
    if objdump:
        assert not _caller_calls_callee(objdump, lib)
    mypyc.build(make({"app": {"name": "pkg"}, "compile": {"modules": ["pkg.core"], "no_semantic_interposition": False}}), "dev")
    _check_flags(logging_cc(), nsi=False)
    if objdump and _is_gcc(os.environ["PT_REAL_CC"]):
        # Pins gcc's behaviour: if this fails, gcc inlines these calls by itself and the option is moot
        assert _caller_calls_callee(objdump, lib)


@needs_venv
@needs_compiler
def test_real_mypyc_wheel_gets_the_same_c_flags(
    wheel_project: Path, monkeypatch: pytest.MonkeyPatch, logging_cc: Callable[[], list[list[str]]]
) -> None:
    from runner.cmd_build import BuildRequest
    from runner.methods import wheel

    _project(wheel_project / "src", {"pkg/core/m.py": INLINE_PROBE})
    monkeypatch.setattr(wheel.envs, "sync", lambda env, **kw: None)
    wheel.build(BuildRequest(_wheel_cfg(), "mypyc", "wheel", wheel_project / "src"))
    _check_flags(logging_cc(), nsi=True)
