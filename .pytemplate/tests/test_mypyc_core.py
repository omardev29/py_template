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

from runner import cmd_mode, config, envs, imports, lintc, mypyc, proc, render, ui  # noqa: E402
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
def test_lintc_allows_module_level_file_with_a_shared_lib(tmp_path: Path, modules: list[str]) -> None:
    found = _lint(tmp_path, MODULE_LEVEL_FILE, {"compile": {"modules": modules}})
    assert not [f for f in found if "__file__" in f.message]


def test_lintc_flags_module_level_file_for_a_single_top_level_module(tmp_path: Path) -> None:
    found = _lint(tmp_path, MODULE_LEVEL_FILE, {"compile": {"modules": ["solo"]}})
    assert sorted(f.line for f in found if "__file__" in f.message) == [2, 3, 6, 9]
    assert all("relative path" in f.message and "inside a function" in f.message for f in found)


def test_lintc_single_top_level_package_is_not_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    (src / "solo").mkdir(parents=True)  # a package: its modules have dotted names, mypyc builds a shared lib
    monkeypatch.setattr(lintc, "SRC", src)
    assert not lintc.relative_file_at_import(make({"compile": {"modules": ["solo"]}}))
    assert lintc.relative_file_at_import(make({"compile": {"modules": ["other"]}}))


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
            path.write_text(content, encoding="utf-8")
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
    with pytest.raises(DeployError, match="compile.modules: src/myapp/nope.py does not exist"):
        mypyc.compiled_sources(make({"compile": {"modules": ["myapp.nope"]}}))


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


@pytest.mark.parametrize(
    ("code", "stdout", "stderr", "verbose", "hint"),
    [
        (mypyc.MYPYC_REJECTED, "myapp/core/m.py:1: error: bad", "", False, False),
        (mypyc.MYPYC_REJECTED, None, None, True, False),  # -v: nothing captured, still no hint
        (1, "", "error: command 'gcc' failed: No such file or directory", False, True),
        (1, None, None, True, True),
        (1, "myapp/core/m.py:1: error: this text no longer decides", "", False, True),
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
) -> None:
    monkeypatch.setattr(ui, "VERBOSE", verbose)
    fake_build.code, fake_build.stdout, fake_build.stderr = code, stdout, stderr
    with pytest.raises(DeployError) as err:
        mypyc.build(make({}), "dev")
    assert fake_build.captures[-1] is not verbose
    assert (mypyc.has_compiler_hint() in str(err.value)) is hint
    assert str(err.value).startswith("mypyc failed (exit code 1)") and err.value.code == 1
    if not verbose and stdout:
        assert stdout in capsys.readouterr().err  # the captured output is shown


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


def test_hidden_imports_of_an_unparsable_file_is_a_deploy_error(src_tree: Path, tmp_path: Path) -> None:
    _project(src_tree, {"myapp/__init__.py": "", "myapp/core/__init__.py": "", "myapp/core/m.py": b"import json\n\ndef f(:\n"})
    with pytest.raises(DeployError, match=r"src/myapp/core/m\.py:3: cannot parse it with the runner's Python") as err:
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
def test_mypy_ini_relaxes_compile_exclude(for_compile: bool) -> None:
    cfg = make({"compile": {"modules": ["myapp.core"], "exclude": ["myapp.core.loose", "myapp.core.sub"]}})
    ini = _ini(render.mypy_ini(cfg, "mypyc", for_compile=for_compile))
    assert COMPILED_KEYS and all(ini.getboolean("mypy-myapp.core.*", key) for key in COMPILED_KEYS)
    for section in ("mypy-myapp.core.loose.*", "mypy-myapp.core.sub.*"):  # x.* covers x itself too
        assert all(ini.getboolean(section, key) is False for key in COMPILED_KEYS)


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
