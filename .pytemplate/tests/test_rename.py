"""`./deploy rename NEW_NAME` (runner/rename.py).

The acceptance test renames every preset skeleton rendered for one name and checks that the
result is byte-identical to the skeleton rendered for the new name: exactly what
`./deploy new --name NEW` would have written. The rest covers the tricky text cases, the
safety checks and the command itself (dry run and a real run in a throwaway copy).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import tokenize
import tomllib
from pathlib import Path
from typing import Any

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_DIR))

from runner import cli, cmd_dev, cmd_env, config, envs, presets, proc, render, rename  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import ROOT  # noqa: E402
from runner.rename import Names, package_of, rewrite  # noqa: E402
from runner.ui import DeployError  # noqa: E402

needs_uv = pytest.mark.skipif(shutil.which("uv") is None and not os.environ.get("UV"), reason="uv not found")


def _pyproject(preset: str, name: str) -> str:
    """pyproject.toml as presets.init writes it: project name + the preset's extra tables."""
    base = (ROOT / "pyproject.toml").read_text(encoding="utf-8").replace("\r\n", "\n")
    extra = str(presets.load(preset).get("pyproject", "")).replace("{{name}}", name).replace("{{pkg}}", package_of(name))
    return presets._set_extra_tables(presets._set_project_name(base, name), extra)


def _write_project(root: Path, preset: str, name: str, *, crlf: bool = False) -> None:
    for rel, data in presets.skeleton(preset, name).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.replace(b"\n", b"\r\n") if crlf and rel != "pytemplate.toml" else data)
    (root / "pyproject.toml").write_text(_pyproject(preset, name), encoding="utf-8", newline="\n")


def _tree(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and rel != "pyproject.toml":
            out[rel] = path.read_bytes()
    return out


def _rename(root: Path, old: str, new: str) -> rename.Plan:
    planned = rename.plan(root, old, new)
    rename.apply_plan(root, planned)
    return planned


# --- acceptance: renaming a skeleton gives the skeleton of the new name ---------------------------

PAIRS = [
    ("alpha", "beta"),
    ("alpha", "My-Game"),
    ("alpha", "game_2"),
    ("My-Game", "alpha"),
    ("My-Game", "my_game"),  # same package: only the display name changes
    ("game_2", "My-Game"),
    ("Alpha", "beta-2"),
]


@pytest.mark.parametrize("preset", ["script", "raylib", "flet"])
@pytest.mark.parametrize(("old", "new"), PAIRS)
@pytest.mark.parametrize("crlf", [False, True], ids=["lf", "crlf"])
def test_rename_reproduces_the_skeleton_of_the_new_name(tmp_path: Path, preset: str, old: str, new: str, crlf: bool) -> None:
    _write_project(tmp_path, preset, old, crlf=crlf)
    planned = _rename(tmp_path, old, new)
    expected = presets.skeleton(preset, new)
    if crlf:  # every file keeps its own line endings (pytemplate.toml is written with LF)
        expected = {k: v if k == "pytemplate.toml" else v.replace(b"\n", b"\r\n") for k, v in expected.items()}
    assert _tree(tmp_path) == expected
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == _pyproject(preset, new)
    moved = package_of(old) != package_of(new)
    assert planned.move == ((f"src/{package_of(old)}", f"src/{package_of(new)}") if moved else None)
    assert not planned.config.kept and all(not f.result.kept for f in planned.files)


def test_the_pyproject_preset_block_is_renamed(tmp_path: Path) -> None:
    _write_project(tmp_path, "flet", "alpha")
    _rename(tmp_path, "alpha", "My-Game")
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "My-Game"
    assert data["tool"]["flet"]["product"] == data["tool"]["flet"]["company"] == "My-Game"


@pytest.mark.parametrize(("old", "new"), [("com", "beta"), ("com", "My-Game"), ("example", "beta")])
def test_the_preset_block_keeps_what_is_not_the_name(tmp_path: Path, old: str, new: str) -> None:
    """Only the values the preset writes with the name follow a rename: flet's org =
    "com.example" (a reverse domain) became "beta.example" for an app named com."""
    _write_project(tmp_path, "flet", old)
    _rename(tmp_path, old, new)
    assert _tree(tmp_path) == presets.skeleton("flet", new)
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == _pyproject("flet", new)


def test_plan_writes_nothing(tmp_path: Path) -> None:
    _write_project(tmp_path, "raylib", "alpha")
    before = {k: hashlib.sha256(v).hexdigest() for k, v in _tree(tmp_path).items()}
    planned = rename.plan(tmp_path, "alpha", "My-Game")
    assert planned.changed_files and planned.move is not None
    assert {k: hashlib.sha256(v).hexdigest() for k, v in _tree(tmp_path).items()} == before


# --- tricky text ----------------------------------------------------------------------------------

AMBIGUOUS = Names("myapp", "My-Game")  # the old name is also the package; the new one is not


def test_substrings_and_longer_names_are_not_touched() -> None:
    src = (
        "from myapp.core import run\n"
        "import myapp_extra\n"
        "myapp_extra = myapp_value = 1\n"
        'label = "my-app-2 and myapp-plugin and xmyapp"\n'
        "print('myapp')\n"
    )
    out = rewrite(src, AMBIGUOUS, python=True)
    assert out.text == (
        "from my_game.core import run\n"
        "import myapp_extra\n"
        "myapp_extra = myapp_value = 1\n"
        'label = "my-app-2 and myapp-plugin and xmyapp"\n'
        "print('My-Game')\n"
    )
    assert out.count == 2


def test_hyphenated_old_name_matches_whole_names_only() -> None:
    names = Names("my-app", "tool")
    text = 'a = "my-app-2"\nb = "my-app"\nc = "my-apps"\nfrom my_app.core import x\n'
    out = rewrite(text, names, python=True)
    assert out.text == 'a = "my-app-2"\nb = "tool"\nc = "my-apps"\nfrom tool.core import x\n'


def test_crlf_and_bom_are_kept() -> None:
    text = '\ufeff"""myapp"""\r\nfrom myapp import gfx\r\n# see src/myapp/app.py\r\n'
    out = rewrite(text, AMBIGUOUS, python=True)
    assert out.text == '\ufeff"""My-Game"""\r\nfrom my_game import gfx\r\n# see src/my_game/app.py\r\n'
    assert [n for n, _, _ in out.changes] == [1, 2, 3]


def test_cr_only_line_endings_are_line_breaks(tmp_path: Path) -> None:
    """Python runs a file with classic Mac line endings: ast counted the lines, the offsets did not
    (IndexError, an internal-error traceback)."""
    text = "import alpha.core\rX = alpha.core.run()\rdef f(alpha):\r    return alpha\r"
    out = rewrite(text, Names("alpha", "beta"), python=True)
    assert out.text == "import beta.core\rX = beta.core.run()\rdef f(alpha):\r    return alpha\r"
    assert not out.note  # tokenized as Python, not as plain text
    _write_project(tmp_path, "script", "alpha")
    (tmp_path / "tests" / "limits.py").write_bytes(b"import alpha.core.bench\rLIMIT = alpha.core.bench.SIEVE_LIMIT\r")
    _rename(tmp_path, "alpha", "beta")
    assert (tmp_path / "tests" / "limits.py").read_bytes() == b"import beta.core.bench\rLIMIT = beta.core.bench.SIEVE_LIMIT\r"


def test_code_changes_only_real_package_references() -> None:
    src = (
        "import myapp.core\n"
        "import myapp as m\n"
        "from . import myapp as local\n"
        "self.myapp = myapp.core.run()\n"
        "def f(myapp=None): return g(myapp=1)\n"
    )
    out = rewrite(src, AMBIGUOUS, python=True)
    assert out.text == (
        "import my_game.core\n"
        "import my_game as m\n"
        "from . import myapp as local\n"
        "self.myapp = my_game.core.run()\n"
        "def f(myapp=None): return g(myapp=1)\n"
    )
    assert [n for n, _ in out.kept] == [3, 4, 5]  # reported, not changed


def test_without_import_a_bare_name_is_a_variable() -> None:
    out = rewrite("from myapp.core import x\nmyapp = 3\nprint(myapp)\n", AMBIGUOUS, python=True)
    assert out.text == "from my_game.core import x\nmyapp = 3\nprint(myapp)\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('p = "dist/myapp.exe"', 'p = "dist/My-Game.exe"'),
        ('p = "dist/myapp-cpython-exe/"', 'p = "dist/My-Game-cpython-exe/"'),
        ('p = "src/myapp/core"', 'p = "src/my_game/core"'),
        ('p = "src\\\\myapp\\\\core"', 'p = "src\\\\my_game\\\\core"'),
        ('cmd = "python -m myapp"', 'cmd = "python -m my_game"'),
        ('cmd = ["python", "-m", "myapp"]', 'cmd = ["python", "-m", "my_game"]'),
        # a prefixed string after -m: the prefix letter hid the -m (python -m My-Game failed)
        ('cmd = [sys.executable, "-m", f"myapp.{mod}"]', 'cmd = [sys.executable, "-m", f"my_game.{mod}"]'),
        ('cmd = [sys.executable, "-m", f"myapp"]', 'cmd = [sys.executable, "-m", f"my_game"]'),
        ("cmd = [sys.executable, '-m', r'myapp']", "cmd = [sys.executable, '-m', r'my_game']"),
        ('cmd = ["python", "-m",\n    rb"myapp"]', 'cmd = ["python", "-m",\n    rb"my_game"]'),
        ('title = "myapp: ready"', 'title = "My-Game: ready"'),
        ('entry = "myapp:main"', 'entry = "my_game:main"'),
        ('mods = ["myapp.*"]', 'mods = ["my_game.*"]'),
        ("# the myapp package", "# the my_game package"),
        ("# Welcome to myapp.", "# Welcome to My-Game."),
        ("# x.myapp is a submodule", "# x.myapp is a submodule"),
        ("m = importlib.import_module('myapp')", "m = importlib.import_module('my_game')"),
        ("s = 'from myapp import x'", "s = 'from my_game import x'"),
        ("b = b'myapp'", "b = b'My-Game'"),
    ],
)
def test_text_occurrences_are_the_package_or_the_name(text: str, expected: str) -> None:
    assert rewrite(text + "\n", AMBIGUOUS, python=True).text == expected + "\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("m = importlib.import_module(name='myapp')", "m = importlib.import_module(name='my_game')"),
        ("m = importlib.import_module('.core', 'myapp')", "m = importlib.import_module('.core', 'my_game')"),
        ("m = importlib.import_module('.core', package='myapp')", "m = importlib.import_module('.core', package='my_game')"),
        ("s = importlib.util.find_spec('.core', 'myapp')", "s = importlib.util.find_spec('.core', 'my_game')"),
        ("f = resources.files(package='myapp')", "f = resources.files(package='my_game')"),
        ("f = resources.files(anchor='myapp')", "f = resources.files(anchor='my_game')"),
        ("t = resources.read_text('myapp', 'data.txt')", "t = resources.read_text('my_game', 'data.txt')"),
        ("t = resources.open_binary('myapp', 'img.png')", "t = resources.open_binary('my_game', 'img.png')"),
        ("d = pkgutil.get_data('myapp', 'data.txt')", "d = pkgutil.get_data('my_game', 'data.txt')"),
        ("g = runpy.run_module('myapp')", "g = runpy.run_module('my_game')"),
        ("g = runpy.run_module(mod_name='myapp', run_name='myapp')", "g = runpy.run_module(mod_name='my_game', run_name='My-Game')"),
        ("t = resources.read_text('myapp', 'about myapp')", "t = resources.read_text('my_game', 'about My-Game')"),  # the resource: text
        ("print(import_module, 'myapp')", "print(import_module, 'My-Game')"),  # not a call of it
        ("x = f(importlib.import_module('a'), 'myapp')", "x = f(importlib.import_module('a'), 'My-Game')"),  # after the call closed
        ("d = {'myapp': 1}[importlib.import_module('myapp').x]", "d = {'My-Game': 1}[importlib.import_module('my_game').x]"),
        # f-strings: one STRING token on 3.11, FSTRING_START..END from 3.12 (the display name there)
        ('m = importlib.import_module(f"myapp.{name}")', 'm = importlib.import_module(f"my_game.{name}")'),
        ('m = importlib.import_module(f"myapp")', 'm = importlib.import_module(f"my_game")'),
        ('m = importlib.import_module(name=f"myapp.{name}")', 'm = importlib.import_module(name=f"my_game.{name}")'),
        ('f = resources.files(f"myapp")', 'f = resources.files(f"my_game")'),
        ('g = runpy.run_module(f"myapp", run_name=f"myapp {x}")', 'g = runpy.run_module(f"my_game", run_name=f"My-Game {x}")'),
        ('print(f"myapp", importlib.import_module("myapp"))', 'print(f"My-Game", importlib.import_module("my_game"))'),
    ],
)
def test_module_name_arguments_of_loader_calls_get_the_package(text: str, expected: str) -> None:
    """A display name is not a module name: import_module(name=...), the package argument of
    import_module/find_spec, files(package=...), the resources functions, pkgutil.get_data and
    runpy.run_module get the package, like the first argument of import_module always did; an
    f-string argument too, on every Python (from 3.12 it got the display name)."""
    assert rewrite(text + "\n", AMBIGUOUS, python=True).text == expected + "\n"


def test_fstrings_tell_fields_from_text() -> None:
    src = 'import myapp\nprint(f"{myapp.core} {{myapp}}: myapp")\n'
    out = rewrite(src, AMBIGUOUS, python=True)
    assert out.text == 'import my_game\nprint(f"{my_game.core} {{My-Game}}: My-Game")\n'


def test_fstring_fields_as_one_token() -> None:
    # Python 3.11 tokenizes a whole f-string as ONE token: the fields are found by hand
    text = 'f"{myapp.x} {{myapp}} {d[\'k\']:{w}} myapp"'
    region = rename._Region(0, len(text), fstring=True)
    positions = [i for i in range(len(text)) if text.startswith("myapp", i)]
    assert [rename._in_fstring_field(text, region, p) for p in positions] == [True, False, False]


@pytest.mark.parametrize(
    ("old", "text", "expected", "kept"),
    [
        ("f", 'x = f"{1}"\n', 'x = f"{1}"\n', 0),  # a string prefix is syntax, never the name
        ("b", "x = b'b'\n", "x = b'tool'\n", 0),  # ... but the text after the quote is
        ("rb", 'x = rb"\\d"\n', 'x = rb"\\d"\n', 0),
        ("u", "x = u'u'\n", "x = u'tool'\n", 0),
        ("fr", 'x = fr"{1}"\n', 'x = fr"{1}"\n', 0),
        ("n", 'x = "a\\nb".split("\\n")\n', 'x = "a\\nb".split("\\n")\n', 0),  # an escape: not the name
        ("r", 'SIG = b"\\x89PNG\\r\\n\\x1a\\n"\n', 'SIG = b"\\x89PNG\\r\\n\\x1a\\n"\n', 0),
        ("x", 'SIG = b"\\x89PNG"\n', 'SIG = b"\\x89PNG"\n', 0),
        ("alpha", 'x = "src\\alpha"\n', 'x = "src\\alpha"\n', 0),  # \a is BEL: "lpha" follows it
        ("d", 'x = re.compile(r"\\d+")\n', 'x = re.compile(r"\\d+")\n', 1),  # raw: a regex escape or a path, reported
        ("alpha", 'x = r"src\\alpha"\n', 'x = r"src\\alpha"\n', 1),
        ("myapp", 'x = "src\\myapp"\n', 'x = "src\\myapp"\n', 1),  # an invalid escape: the new name could make it a real one
        # format directives of a one-letter name: syntax whatever the Python (3.12+ splits f-strings into tokens)
        ("f", 'x = f"{y:f}"\n', 'x = f"{y:f}"\n', 1),
        ("d", 'x = f"{y:>d} {z:,d}"\n', 'x = f"{y:>d} {z:,d}"\n', 1),
        ("x", 'x = f"{y:x}"\n', 'x = f"{y:x}"\n', 1),
        ("alpha", "x = f\"{d['alpha']}\"\n", "x = f\"{d['alpha']}\"\n", 1),  # a string inside a field is code
        ("d", 'x = "%d" % y\n', 'x = "%d" % y\n', 1),
        ("s", 'x = "%(k)-s %.*s" % y\n', 'x = "%(k)-s %.*s" % y\n', 1),
        ("d", 'x = "{:d} {0:,d}".format(y)\n', 'x = "{:d} {0:,d}".format(y)\n', 1),
        ("r", 'x = "{!r}".format(y)\n', 'x = "{!r}".format(y)\n', 1),
        ("d", 'x = "d: {d}"\n', 'x = "tool: {tool}"\n', 0),  # not a directive
    ],
)
@pytest.mark.filterwarnings("ignore::SyntaxWarning", "ignore::DeprecationWarning")  # "\m" is an invalid escape on purpose (3.11: a DeprecationWarning)
def test_string_prefixes_and_escapes_are_never_the_name(old: str, text: str, expected: str, kept: int) -> None:
    out = rewrite(text, Names(old, "tool"), python=True)
    assert out.text == expected and len(out.kept) == kept
    compile(out.text, "t.py", "exec")


@pytest.mark.parametrize(
    ("old", "text", "expected"),
    [
        ("n", 'x = "a\\\\n"\n', 'x = "a\\\\tool"\n'),  # an escaped backslash: the name follows a real one
        ("alpha", 'x = "src\\\\alpha"\n', 'x = "src\\\\tool"\n'),
        ("f", "# f is the app\n", "# tool is the app\n"),  # comments have no escapes
    ],
)
def test_backslash_pairs_and_comments_are_plain_text(old: str, text: str, expected: str) -> None:
    assert rewrite(text, Names(old, "tool"), python=True).text == expected


@pytest.mark.parametrize(
    ("text", "expected", "kept"),
    [
        ('[tasks.a]\ncmd = ["x", "\\n", "n"]\n', '[tasks.a]\ncmd = ["x", "\\n", "tool"]\n', 0),  # a basic string escape
        ("[tasks.a]\ncmd = ['x', '\\n']\n", "[tasks.a]\ncmd = ['x', '\\n']\n", 1),  # a literal string: kept, reported
    ],
)
def test_toml_escapes_are_never_the_name(text: str, expected: str, kept: int) -> None:
    out = rewrite(text, Names("n", "tool"), toml=True)
    assert out.text == expected and len(out.kept) == kept


def test_json_and_toml_files_of_src_and_tests_keep_their_escapes(tmp_path: Path) -> None:
    """A JSON fixture's "a\\n" once became "a\\tool" (a tab and "ool") for an app named n."""
    _write_project(tmp_path, "script", "n")
    (tmp_path / "tests" / "data.json").write_text('{"sep": "a\\n", "app": "n", "path": "C:\\\\n", "odd": "\\q\\n"}\n', encoding="utf-8", newline="\n")
    (tmp_path / "tests" / "cfg.toml").write_text("# a\\n n\nsep = \"a\\n\"\napp = \"n\"\nraw = 'a\\n'\n", encoding="utf-8", newline="\n")
    (tmp_path / "tests" / "notes.txt").write_text("a\\n n\n", encoding="utf-8", newline="\n")  # plain text: no escapes
    edits = {edit.path: edit for edit in rename.plan(tmp_path, "n", "tool").files}
    json_edit, toml_edit = edits["tests/data.json"], edits["tests/cfg.toml"]
    assert json_edit.new == b'{"sep": "a\\n", "app": "tool", "path": "C:\\\\tool", "odd": "\\q\\n"}\n'
    assert toml_edit.new == b"# a\\tool tool\nsep = \"a\\n\"\napp = \"tool\"\nraw = 'a\\n'\n"
    assert [n for n, _ in toml_edit.result.kept] == [4]  # a literal string's \n: no escape there, reported
    assert edits["tests/notes.txt"].new == b"a\\tool tool\n"


@pytest.mark.parametrize("preset", ["script", "raylib", "flet"])
@pytest.mark.parametrize("old", ["b", "f", "r", "rb", "fr", "u"])
def test_names_that_are_string_prefixes_or_escapes_keep_the_code_intact(tmp_path: Path, preset: str, old: str) -> None:
    _write_project(tmp_path, preset, old)
    _rename(tmp_path, old, "tool")
    expected = presets.skeleton(preset, "tool")
    for rel_path, data in _tree(tmp_path).items():
        if rel_path.endswith(".py"):
            compile(data, rel_path, "exec")
            assert data == expected[rel_path], rel_path


def test_the_png_signature_survives_a_rename_from_n(tmp_path: Path) -> None:
    """The flet skeleton renamed from 'n' or 'r' got a silently wrong PNG signature."""
    for old in ("n", "r"):
        root = tmp_path / old
        root.mkdir()
        _write_project(root, "flet", old)
        _rename(root, old, "tool")
        assert b'PNG_SIGNATURE: Final = b"\\x89PNG\\r\\n\\x1a\\n"' in (root / "src" / "tool" / "core" / "fractal.py").read_bytes()


def test_unparseable_python_falls_back_to_plain_text() -> None:
    out = rewrite('"""myapp\nfrom myapp.core import x\n', AMBIGUOUS, python=True)
    assert out.text == '"""My-Game\nfrom my_game.core import x\n'
    assert "plain text" in out.note


def test_distinct_old_name_and_package() -> None:
    names = Names("My-Game", "beta")
    text = 'title = "My-Game"\nfrom my_game.core import x\npath = "src/my_game/"\nMy-Game-2 = 1\n'
    assert rewrite(text, names).text == 'title = "beta"\nfrom beta.core import x\npath = "src/beta/"\nMy-Game-2 = 1\n'


# --- files, folders and pytemplate.toml -------------------------------------------------------------


def test_binary_and_cache_files_are_left_alone(tmp_path: Path) -> None:
    _write_project(tmp_path, "script", "alpha")
    blob = b"\x89PNG\r\n\x1a\n\0alpha\0"
    (tmp_path / "src" / "assets").mkdir()
    (tmp_path / "src" / "assets" / "logo.png").write_bytes(blob)
    (tmp_path / "src" / "alpha" / "latin1.txt").write_bytes("alpha caf\xe9".encode("latin-1"))
    cache = tmp_path / "src" / "alpha" / "__pycache__"
    cache.mkdir()
    (cache / "app.txt").write_text("alpha", encoding="utf-8")
    planned = _rename(tmp_path, "alpha", "beta")
    assert (tmp_path / "src" / "assets" / "logo.png").read_bytes() == blob
    assert (tmp_path / "src" / "beta" / "latin1.txt").read_bytes() == "alpha caf\xe9".encode("latin-1")
    assert (tmp_path / "src" / "beta" / "__pycache__" / "app.txt").read_text(encoding="utf-8") == "alpha"
    assert sorted(planned.binary) == ["src/alpha/latin1.txt", "src/assets/logo.png"]


def test_pytemplate_toml_changes_only_package_references(tmp_path: Path) -> None:
    _write_project(tmp_path, "script", "alpha")
    cfg_file = tmp_path / "pytemplate.toml"
    text = cfg_file.read_text(encoding="utf-8")
    text += '\n[tasks.pack]\ncmd = ["echo", "alpha", "src/alpha/core"]\n'
    cfg_file.write_text(text, encoding="utf-8", newline="\n")
    planned = _rename(tmp_path, "alpha", "My-Game")
    new = cfg_file.read_text(encoding="utf-8")
    assert 'name = "My-Game"   # executable name; the Python package is src/my_game/' in new
    assert 'cmd = ["echo", "alpha", "src/my_game/core"]' in new
    assert [line for _, line in planned.config.kept] == ['cmd = ["echo", "alpha", "src/alpha/core"]']
    assert tomllib.loads(new)["compile"]["modules"] == ["my_game.core"]


@pytest.mark.parametrize("new", ["beta", "My-Game"])
def test_pytemplate_toml_keeps_file_names_and_other_folders(tmp_path: Path, new: str) -> None:
    """Only src/<pkg>/ moves and only <pkg>.<its module> is a module name: an icon, a data file or
    a tool named like the app keeps its name (the file itself is not renamed)."""
    _write_project(tmp_path, "script", "alpha")
    cfg_file = tmp_path / "pytemplate.toml"
    text = cfg_file.read_text(encoding="utf-8").replace('icon = ""', 'icon = "assets/alpha.ico"')
    text += '\n[tasks.gen]\ncmd = ["{python}", "tools/alpha.py", "--out", "levels/alpha.json", "src/alpha/data", "-m", "alpha.core.bench"]\n'
    cfg_file.write_text(text, encoding="utf-8", newline="\n")
    planned = _rename(tmp_path, "alpha", new)
    pkg = package_of(new)
    data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["deploy"]["exe"]["icon"] == "assets/alpha.ico"
    assert data["tasks"]["gen"]["cmd"] == ["{python}", "tools/alpha.py", "--out", "levels/alpha.json", f"src/{pkg}/data", "-m", f"{pkg}.core.bench"]
    assert data["compile"]["modules"] == [f"{pkg}.core"]
    kept = [line.split(" =")[0] for _, line in planned.config.kept]
    assert kept == ["icon", "cmd"]
    assert planned.config.count == 4  # the app.name comment's src/alpha/, compile.modules, the two in cmd: never the icon


@pytest.mark.parametrize("preset", ["script", "raylib", "flet"])
@pytest.mark.parametrize("old", ["deploy", "uv", "src", "tools"])
def test_pytemplate_toml_comments_of_an_app_named_like_a_path_word(tmp_path: Path, preset: str, old: str) -> None:
    """`./deploy apply`, `uv.lock` and `src/<pkg>/` in the comments are not the package of an app
    named deploy, uv or src: the renamed pytemplate.toml is the new name's skeleton."""
    _write_project(tmp_path, preset, old)
    planned = rename.plan(tmp_path, old, "beta")
    assert planned.config.new == presets.skeleton(preset, "beta")["pytemplate.toml"].decode("utf-8")


@pytest.mark.parametrize(
    ("python", "text", "expected"),
    [
        (True, 'DATA = Path("src/src/data.json")\n# see src/src/core.py\n', 'DATA = Path("src/beta/data.json")\n# see src/beta/core.py\n'),
        (False, "see src/src/core.py, src\\src\\x and src/src/src/y\n", "see src/beta/core.py, src\\beta\\x and src/beta/src/y\n"),
    ],
)
def test_code_of_an_app_named_src_keeps_the_project_folder(python: bool, text: str, expected: str) -> None:
    """`new` refuses the name src, but a project made by hand can have it: in src/src/x the first
    src is the project's own folder and the second the package (it once became beta/beta/x)."""
    assert rewrite(text, Names("src", "beta"), python=python).text == expected


def test_root_files_that_mention_the_name_are_reported(tmp_path: Path) -> None:
    _write_project(tmp_path, "script", "alpha")
    (tmp_path / "README.md").write_text("# alpha\n", encoding="utf-8")
    (tmp_path / "NOTES.txt").write_text("nothing here\n", encoding="utf-8")
    assert rename.plan(tmp_path, "alpha", "beta").mentions == ["README.md"]


def test_missing_or_taken_package_folders(tmp_path: Path) -> None:
    _write_project(tmp_path, "script", "alpha")
    with pytest.raises(DeployError, match=r"src/other/ not found"):
        rename.plan(tmp_path, "other", "beta")
    (tmp_path / "src" / "beta").mkdir()
    with pytest.raises(DeployError, match=r"src/beta/ already exists"):
        rename.plan(tmp_path, "alpha", "beta")
    (tmp_path / "src" / "gamma.py").write_text("", encoding="utf-8")
    with pytest.raises(DeployError, match=r"src/gamma.py exists"):
        rename.plan(tmp_path, "alpha", "gamma")


def _case_insensitive(path: Path) -> bool:
    probe = path / "CaseProbe"
    probe.mkdir()
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.rmdir()


def test_case_only_folder_fix_on_a_case_insensitive_file_system(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("case-sensitive file system")
    _write_project(tmp_path, "script", "alpha")
    (tmp_path / "src" / "alpha").rename(tmp_path / "src" / "Alpha")  # e.g. made by hand
    planned = _rename(tmp_path, "alpha", "Alpha")
    assert planned.move == ("src/Alpha", "src/alpha")
    assert "alpha" in os.listdir(tmp_path / "src") and "Alpha" not in os.listdir(tmp_path / "src")
    assert (tmp_path / "src" / "alpha" / "__init__.py").read_text(encoding="utf-8") == '"""Alpha"""\n'


# --- name checks and the command --------------------------------------------------------------------


def _cfg(preset: str) -> Config:
    text = presets.skeleton(preset, "alpha")["pytemplate.toml"].decode("utf-8")
    cfg: Config = config._build(Config, tomllib.loads(text), "")
    return cfg


@pytest.mark.parametrize(
    ("preset", "name", "message"),
    [
        ("flet", "flet", "also the name of a dependency"),
        ("script", "Rich", "also the name of a dependency"),
        ("script", "1game", "may only contain"),
        ("script", "my game", "may only contain"),
        ("script", "class", "Python keyword"),
        ("script", "json", "standard library"),
    ],
)
def test_bad_new_names(preset: str, name: str, message: str) -> None:
    with pytest.raises(DeployError, match=message) as e:
        rename.check_new_name(_cfg(preset), name)
    assert e.value.code == 2


def test_good_new_names() -> None:
    for name in ("beta", "My-Game", "game_2"):
        rename.check_new_name(_cfg("script"), name)


def _deploy(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    drop = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return subprocess.run(
        [sys.executable, "-B", str(root / ".pytemplate" / "deploy.py"), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def _snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<dir>"
        for p in sorted(root.rglob("*"))
        if "__pycache__" not in p.parts
    }


@pytest.fixture(scope="module")
def project_copy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("rename")
    presets.copy_template(dest)
    return dest


@needs_uv
def test_command_dry_run_then_real_run(project_copy: Path) -> None:
    root = project_copy
    # the copy's own package (a project made with ./deploy new has its own name and preset)
    old = package_of(tomllib.loads((root / "pytemplate.toml").read_text(encoding="utf-8-sig"))["app"]["name"])
    if old == "my_game":
        pytest.skip("this project is already called my_game")
    before = _snapshot(root)
    r = _deploy(root, "--dry-run", "rename", "My-Game")
    assert r.returncode == 0, r.stderr
    assert f"would move       src/{old}/ -> src/my_game/" in r.stderr, r.stderr
    assert "uv.lock          would re-lock" in r.stderr
    assert "src/my_game/__init__.py" in r.stderr and '+ """My-Game"""' in r.stderr  # every preset's docstring
    assert _snapshot(root) == before, "--dry-run wrote files"

    r = _deploy(root, "rename", "My-Game", "--bogus")
    assert r.returncode == 2 and "unknown argument(s): --bogus" in r.stderr

    r = _deploy(root, "rename", "My-Game")
    if r.returncode != 0 and rename.needs_pypi(r.stderr):
        # The rename itself happened; only the re-lock needs the package index
        assert (root / "src" / "my_game" / "__init__.py").is_file() and not (root / "src" / old).exists()
        assert "The files are already renamed" in r.stderr and "./deploy apply" in r.stderr, r.stderr
        pytest.skip("needs PyPI: uv lock could not reach the package index (offline, blocked proxy, or UV_OFFLINE with a cold cache)")
    assert r.returncode == 0, r.stderr
    assert not (root / "src" / old).exists()
    assert (root / "src" / "my_game" / "__init__.py").is_file()
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    assert "my-game" in {p["name"] for p in lock["package"]}
    editor = (root / ".pytemplate" / "editor.json").read_text(encoding="utf-8")
    assert '"pkg": "my_game"' in editor
    check = _deploy(root, "render", "--check")
    assert check.returncode == 0, check.stderr

    r = _deploy(root, "rename", "My-Game")
    assert r.returncode == 0 and "nothing to do" in r.stderr


@needs_uv
def test_real_run_skips_without_the_index(tmp_path: Path) -> None:
    """Offline with a cold uv cache the real rename SKIPs instead of failing the suite."""
    drop = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(UV_OFFLINE="1", UV_CACHE_DIR=str(tmp_path / "cache"), PYTHONDONTWRITEBYTECODE="1")
    node = f"{Path(__file__).resolve()}::test_command_dry_run_then_real_run"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "t"), node],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False,
    )
    assert r.returncode == 0 and "1 skipped" in r.stdout and "needs PyPI" in r.stdout, r.stdout + r.stderr


# --- pytemplate.toml: module values, TOML keys, words of the schema ----------------------------------------


def _flat(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(_flat(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


@pytest.mark.parametrize("new", ["beta", "Beta", "My-Game"])
def test_module_name_keys_follow_the_package(tmp_path: Path, new: str) -> None:
    _write_project(tmp_path, "script", "alpha")
    cfg_file = tmp_path / "pytemplate.toml"
    text = cfg_file.read_text(encoding="utf-8")
    text = text.replace('modules = ["alpha.core"]', 'modules = [\n    "alpha",\n]')  # a multi-line array
    text = text.replace("[compile]", '[[typing.mypy_overrides]]\nmodule = ["alpha", "alpha.*"]\ndisallow_any_explicit = false\n\n[compile]')
    text = text.replace("[deploy.exe]", '[deploy.exe]\nhidden_imports = ["alpha"]')
    text = text.replace("[deploy.upx]", '[deploy.wheel]\nentry = "alpha:main"\n\n[deploy.upx]')
    text = text.replace("exclude = []     # extra file-name globs", 'exclude = ["alpha"]     # extra file-name globs')  # [deploy.upx]
    text += '\n[tasks.pack]\ncmd = ["echo", "alpha"]\n'
    cfg_file.write_text(text, encoding="utf-8", newline="\n")
    planned = _rename(tmp_path, "alpha", new)
    pkg = package_of(new)
    data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["compile"]["modules"] == [pkg]
    assert data["typing"]["mypy_overrides"][0]["module"] == [pkg, f"{pkg}.*"]
    assert data["deploy"]["exe"]["hidden_imports"] == [pkg]
    assert data["deploy"]["wheel"]["entry"] == f"{pkg}:main"
    assert data["deploy"]["upx"]["exclude"] == ["alpha"]  # file-name globs: another table's `exclude`
    assert data["tasks"]["pack"]["cmd"] == ["echo", "alpha"]  # task arguments are reported, not changed
    assert [line.split(" =")[0] for _, line in planned.config.kept] == ["exclude", "cmd"]


def test_module_value_lines_follow_tables_and_arrays() -> None:
    text = (
        '[compile]\nmodules = [\n  "a",  # x\n  "b",\n]\nexclude = ["c"]\nannotate = false\n'
        '[deploy.upx]\nexclude = ["d"]\n[deploy]\nexe.hidden_imports = ["e"]\n[[typing.mypy_overrides]]\nmodule = "f"\n'
    )
    assert rename.module_value_lines(text) == {2, 3, 4, 5, 6, 11, 13}


@pytest.mark.parametrize("new", ["beta", "My-Game"])
def test_bare_package_values_of_module_keys_are_renamed(new: str) -> None:
    text = (
        '[compile]\nmodules = [\n  "alpha",\n]\nexclude = ["alpha.slow"]\n[[typing.mypy_overrides]]\nmodule = "alpha"\n'
        '[deploy.exe]\nhidden_imports = ["alpha"]\n[vscode]\nbuttons = ["alpha"]\n[tasks.alpha]\ncmd = ["echo", "alpha"]\n'
    )
    pkg = package_of(new)
    data = tomllib.loads(rewrite(text, Names("alpha", new), only_pkg=True, toml=True, module_keys=rename.MODULE_KEYS).text)
    assert data["compile"]["modules"] == [pkg] and data["compile"]["exclude"] == [f"{pkg}.slow"]
    assert data["typing"]["mypy_overrides"][0]["module"] == pkg and data["deploy"]["exe"]["hidden_imports"] == [pkg]
    assert data["vscode"]["buttons"] == ["alpha"] and data["tasks"]["alpha"]["cmd"] == ["echo", "alpha"]


def test_toml_keys_and_headers_never_change() -> None:
    text = '[app]\napp = 1\n[tasks.app]\nx.app.y = 2\n[[app.x]]\nv = "src/app/core"\n'
    out = rewrite(text, Names("app", "beta"), only_pkg=True, toml=True)
    assert out.text == text.replace("src/app/core", "src/beta/core")


@pytest.mark.parametrize(
    ("preset", "old"),
    [("script", "app"), ("script", "editor"), ("script", "console"), ("script", "exe"), ("script", "check"), ("script", "hooks"),
     ("script", "compile"), ("script", "tasks"), ("script", "mode"), ("raylib", "bunnymark"), ("raylib", "python"), ("flet", "dev"), ("flet", "app")],
)
@pytest.mark.parametrize("new", ["beta", "My-Game"])
def test_names_that_are_also_config_words_keep_the_config_valid(tmp_path: Path, preset: str, old: str, new: str) -> None:
    _write_project(tmp_path, preset, old)
    planned = rename.plan(tmp_path, old, new)
    rename.validate_config(planned.config.new)  # 'unknown key beta' / vscode.buttons before the fix
    before, after = _flat(tomllib.loads(planned.config.old)), _flat(tomllib.loads(planned.config.new))
    assert before.keys() == after.keys()
    assert after["app.name"] == new and after["vscode.buttons"] == before["vscode.buttons"]
    assert after["compile.modules"] == [f"{package_of(new)}.core"]
    assert {k for k in before if before[k] != after[k]} <= {"app.name", "compile.modules", "typing.mypy_overrides", "deploy.wheel.entry"}


def test_key_paths_in_comments_of_an_app_named_like_a_table(tmp_path: Path) -> None:
    _write_project(tmp_path, "script", "app")
    old = (tmp_path / "pytemplate.toml").read_text(encoding="utf-8")
    assert "(= not app.gui)" in old and "(app.name" in old
    new = rename.plan(tmp_path, "app", "beta").config.new
    assert "(= not app.gui)" in new and "(app.name" in new and "app.preset cannot" in new
    assert 'modules = ["beta.core"]' in new and "src/beta/" in new


# --- Python code: scopes, f-strings, token families -------------------------------------------------------

SHADOWS = [
    "def main():\n    game = make()\n    game.run()\n    print(game.core.VERSION)\n",
    "def main():\n    global game\n    game = 1\n    print(game)\n",
    "def outer():\n    game = 1\n\n    def inner():\n        nonlocal game\n        game = 2\n\n    inner()\n    return game\n",
    "def main():\n    game = 1\n    game += 1\n    return f'{game}'\n",
    "def main(game=None):\n    return game\n",
    "def main(xs):\n    for game in xs:\n        print(game)\n",
    "def main(xs):\n    ys = [(game := x) for x in xs]\n    return game\n",
    "game = 3\nprint(game)\n",
    "class C:\n    game = 1\n    y = game + 1\n",
    "def main():\n    try:\n        pass\n    except OSError as game:\n        print(game)\n",
    "def main():\n    with open('x') as game:\n        print(game)\n",
    "def main():\n    game: int = 1\n    return game\n",
    "def main():\n    from other import game\n    return game\n",
    "def main():\n    import other as game\n    return game\n",
    "def game():\n    return game\n",
]


@pytest.mark.parametrize("body", SHADOWS)
def test_a_variable_named_like_the_package_is_never_half_renamed(body: str) -> None:
    out = rewrite("import game.core\n\n\n" + body, Names("game", "beta"), python=True)
    assert out.text == "import beta.core\n\n\n" + body
    compile(out.text, "t.py", "exec")  # the nonlocal case raised SyntaxError before the fix
    assert {n for n, _ in out.kept} == {n for n, line in enumerate(body.splitlines(), 4) if "game" in line}


def test_package_uses_next_to_shadowing_scopes_are_renamed() -> None:
    src = (
        "import game.core\n"
        "print(game.core.X, f'{game.core}')\n"
        "def f(game=None): return g(game=1)\n"
        "class C:\n"
        "    game = 1\n"
        "    def m(self): return game.core.Y\n"
        "def h():\n"
        "    import game.gfx\n"
        "    return game.gfx.Z\n"
        "def k(x=game.core.DEFAULT): return [game.core for _ in x]\n"
        "@game.core.deco\n"
        "def d(): pass\n"
    )
    out = rewrite(src, Names("game", "beta"), python=True)
    assert out.text == src.replace("game.", "beta.")
    assert [n for n, _ in out.kept] == [3, 5]


def test_a_local_import_of_the_package_under_a_module_rebinding() -> None:
    src = "import game.core\ngame = None\n\ndef h():\n    import game.gfx\n    return game.gfx.Z\n"
    out = rewrite(src, Names("game", "beta"), python=True)
    assert out.text == "import beta.core\ngame = None\n\ndef h():\n    import beta.gfx\n    return beta.gfx.Z\n"


def test_fstring_debug_field_of_the_bound_package() -> None:
    out = rewrite('import myapp\nx = f"{myapp=}" f"{myapp = !r}" f"{myapp=:>9}"\nf(myapp=1)\nmyapp = 2\n', Names("myapp", "beta"), python=True)
    # myapp = 2 rebinds the module name: nothing but the import changes, everything else is reported
    assert out.text.startswith("import beta\n") and [n for n, _ in out.kept] == [2, 3, 4]
    out = rewrite('import myapp\nx = f"{myapp=}" f"{myapp = !r}" f"{myapp=:>9}"\nf(myapp=1)\n', Names("myapp", "beta"), python=True)
    assert out.text == 'import beta\nx = f"{beta=}" f"{beta = !r}" f"{beta=:>9}"\nf(myapp=1)\n'
    assert [n for n, _ in out.kept] == [3]


def test_the_token_rule_without_ast(monkeypatch: pytest.MonkeyPatch) -> None:
    # Syntax newer than the runner's Python: ast fails, the token rule still works
    monkeypatch.setattr(rename, "_package_uses", lambda body, pkg: None)
    out = rewrite('import myapp\nx = f"{myapp=}" + str(myapp.core)\nf(myapp=1)\n', Names("myapp", "beta"), python=True)
    assert out.text == 'import beta\nx = f"{beta=}" + str(beta.core)\nf(myapp=1)\n'


def test_unknown_string_token_family_is_text(monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(tokenize, "FSTRING_START"):
        pytest.skip("Python 3.11: f-strings are one STRING token")
    for kind in ("START", "MIDDLE", "END"):
        monkeypatch.setitem(tokenize.tok_name, getattr(tokenize, f"FSTRING_{kind}"), f"XSTRING_{kind}")
    assert rewrite('x = f"src/myapp/core {1}"\n', AMBIGUOUS, python=True).text == 'x = f"src/my_game/core {1}"\n'


def test_tokenizer_canary() -> None:
    """A new string token family (like TSTRING in 3.14) must end in _START/_END to be treated as text."""
    starts = {n for n in tokenize.tok_name.values() if n.endswith("_START")}
    assert starts <= {"FSTRING_START", "TSTRING_START"}, starts


@pytest.mark.parametrize(
    "src",
    [
        "import os, myapp.core, sys\nmyapp.core.run()\n",
        "from myapp.core import (\n    a,\n    b,\n)\n",
        "from myapp.core \\\n    import a\n",
        "def g():\n    yield from myapp.core.items()\nimport myapp.core\n",
        "try:\n    pass\nexcept ValueError as e:\n    raise KeyError() from e\nimport myapp\nprint(myapp.x)\n",
    ],
)
def test_import_forms(src: str) -> None:
    out = rewrite(src, Names("myapp", "beta"), python=True)
    assert "myapp" not in out.text and out.text == src.replace("myapp", "beta")
    compile(out.text, "t.py", "exec")


def test_a_relative_import_of_a_sibling_named_like_the_package() -> None:
    src = "from . import myapp\nprint(myapp.x)\n"
    assert rewrite(src, Names("myapp", "beta"), python=True).text == src


# --- paths inside the package --------------------------------------------------------------------------


@pytest.mark.parametrize("new", ["beta", "My-Game"])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# see src/core/core/x.py\n", "# see src/{pkg}/core/x.py\n"),
        ('p = "src/core/core/bench.py"\n', 'p = "src/{pkg}/core/bench.py"\n'),
        ('p = "src\\\\core\\\\core\\\\bench.py"\n', 'p = "src\\\\{pkg}\\\\core\\\\bench.py"\n'),
        ('p = "src/core/core.py"\n', 'p = "src/{pkg}/core.py"\n'),
        ('p = "core/core.txt"\n', 'p = "{pkg}/core.txt"\n'),
        ('p = "src/my-core/core.py"\n', 'p = "src/my-core/{pkg}.py"\n'),  # another folder: core.py is a module path
    ],
)
def test_a_path_segment_after_the_package_is_a_submodule(text: str, expected: str, new: str) -> None:
    assert rewrite(text, Names("core", new), python=True).text == expected.format(pkg=package_of(new))


# --- other encodings, other files ------------------------------------------------------------------------


def test_python_file_with_a_coding_cookie_is_rewritten_in_its_encoding(tmp_path: Path) -> None:
    _write_project(tmp_path, "script", "alpha")
    source = ('# -*- coding: cp1252 -*-\n"""Caf' + chr(0xE9) + '"""\nfrom alpha.core import bench\n').encode("cp1252")
    (tmp_path / "src" / "alpha" / "legacy.py").write_bytes(source)
    planned = _rename(tmp_path, "alpha", "beta")
    assert (tmp_path / "src" / "beta" / "legacy.py").read_bytes() == source.replace(b"from alpha.core", b"from beta.core")
    assert "src/alpha/legacy.py" not in planned.binary and not planned.unreadable
    assert next(f for f in planned.files if f.target == "src/beta/legacy.py").encoding == "cp1252"


def test_undecodable_files_that_mention_the_old_name_are_warned_about(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_project(tmp_path, "script", "alpha")
    (tmp_path / "src" / "alpha" / "nocookie.py").write_bytes(b'"""caf\xe9"""\nfrom alpha.core import bench\n')
    (tmp_path / "src" / "alpha" / "notes.txt").write_bytes(b"see alpha caf\xe9")
    (tmp_path / "src" / "alpha" / "other.txt").write_bytes(b"caf\xe9")
    (tmp_path / "src" / "alpha" / "logo.png").write_bytes(b"\x89PNG\0alpha\0")
    planned = rename.plan(tmp_path, "alpha", "beta")
    assert planned.unreadable == ["src/alpha/nocookie.py", "src/alpha/notes.txt"]
    capsys.readouterr()
    rename.report(planned, dry=False)  # not verbose: the warning must still show
    err = capsys.readouterr().err
    assert "warning: " in err and "src/beta/nocookie.py" in err and "src/beta/notes.txt" in err
    assert "other.txt" not in err and "logo.png" not in err


def _symlink_or_skip(link: Path, target: str, *, directory: bool = False) -> None:
    try:  # native separators: Windows cannot follow a relative target written with forward slashes
        link.symlink_to(target.replace("/", os.sep), target_is_directory=directory)
    except (OSError, NotImplementedError) as e:  # Windows without the symlink privilege
        pytest.skip(f"cannot create symbolic links here: {e}")


def test_a_link_windows_cannot_follow_is_no_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """os.DirEntry.is_dir() raises for a link Windows cannot resolve (WinError 123: a relative
    target written with forward slashes): the rename plan stopped with an internal-error traceback."""
    _write_project(tmp_path, "script", "alpha")
    folder = tmp_path / "src" / "alpha"
    real = os.scandir

    class Odd:
        name = "ext"

        def is_dir(self) -> bool:
            raise OSError(22, "The filename, directory name, or volume label syntax is incorrect", str(folder / "ext"))

    def scandir(path: Any) -> Any:
        return [*real(path), Odd()] if Path(path) == folder else real(path)

    monkeypatch.setattr(rename.os, "scandir", scandir)
    assert "ext" not in rename._package_modules(tmp_path, "alpha") and "core" in rename._package_modules(tmp_path, "alpha")


def test_links_in_src_and_tests_are_reported_never_rewritten(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A linked subpackage or module is neither rewritten (its target may be shared with other
    projects) nor silently skipped: the rename warns, so its old imports do not break unnoticed."""
    root = tmp_path / "p"
    root.mkdir()
    _write_project(root, "script", "alpha")
    shared = tmp_path / "shared_ext"
    shared.mkdir()
    (shared / "helper.py").write_text("from alpha.core import bench\n", encoding="utf-8")
    (shared / "linked.py").write_text("import alpha\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "alpha" / "data").mkdir()
    _symlink_or_skip(root / "src" / "alpha" / "ext", "../../../shared_ext", directory=True)
    _symlink_or_skip(root / "src" / "alpha" / "linked.py", "../../../shared_ext/linked.py")
    _symlink_or_skip(root / "tests" / "other", "../../other", directory=True)  # mentions nothing: not listed
    _symlink_or_skip(root / "tests" / "data", "../src/alpha/data", directory=True)  # dangles once src/alpha/ moves
    planned = rename.plan(root, "alpha", "beta")
    assert planned.linked == ["src/alpha/ext/", "src/alpha/linked.py", "tests/data/"]
    assert not [f.path for f in planned.files if f.path.startswith(("src/alpha/ext", "src/alpha/linked", "tests/other"))]
    capsys.readouterr()
    rename.report(planned, dry=False)  # not verbose: the warning must still show
    err = capsys.readouterr().err
    assert "warning: " in err and "src/beta/ext/" in err and "src/beta/linked.py" in err and "tests/data/" in err
    assert "tests/other" not in err
    rename.apply_plan(root, planned)
    assert (shared / "helper.py").read_text(encoding="utf-8") == "from alpha.core import bench\n"  # never through a link
    assert (root / "src" / "beta" / "ext").is_symlink()


def test_only_links_the_move_breaks_are_reported(tmp_path: Path) -> None:
    """The name was searched in the link's target text: an absolute link through the project's
    own folder, named like the app (what `./deploy new alpha` makes), was reported as pointing
    through the old name although it kept working. What counts is whether it dangles once
    src/alpha/ moves."""
    root = tmp_path / "alpha"  # the project folder, named like the app
    root.mkdir()
    _write_project(root, "script", "alpha")
    (root / "shared").mkdir()
    (root / "shared" / "readme.txt").write_text("x\n", encoding="utf-8")
    (root / "src" / "alpha" / "data").mkdir()
    (root / "src" / "alpha" / "data" / "x.txt").write_text("x\n", encoding="utf-8")
    _symlink_or_skip(root / "tests" / "fixtures", str(root / "shared"), directory=True)  # absolute, keeps working
    _symlink_or_skip(root / "src" / "alpha" / "assets", "data", directory=True)  # moves with the package
    _symlink_or_skip(root / "tests" / "data", str(root / "src" / "alpha" / "data"), directory=True)  # dangles
    _symlink_or_skip(root / "src" / "alpha" / "back", "../alpha/data", directory=True)  # dangles: through the old name
    planned = rename.plan(root, "alpha", "beta")
    assert planned.linked == ["src/alpha/back/", "tests/data/"]
    assert rename.plan(root, "alpha", "Alpha").linked == []  # the package does not move


def test_quiet_still_lists_what_must_be_reviewed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """-q hides progress, never what the user must act on: `./deploy -q rename beta` printed
    nothing and left `return alpha.core` (kept: the module rebinds alpha) next to the renamed
    `import beta.core`, a NameError at runtime, without a word."""
    _write_project(tmp_path, "script", "alpha")
    (tmp_path / "src" / "alpha" / "extra.py").write_text(
        "import alpha.core\n\n\ndef run():\n    return alpha.core\n\n\ndef other():\n    global alpha\n    alpha = None\n", encoding="utf-8"
    )
    (tmp_path / "notes.md").write_text("Run alpha.\n", encoding="utf-8")
    planned = rename.plan(tmp_path, "alpha", "beta")
    monkeypatch.setattr(rename.ui, "QUIET", True)
    capsys.readouterr()
    rename.report(planned, dry=False)
    err = capsys.readouterr().err
    assert "left unchanged" in err and "src/beta/extra.py:5: return alpha.core" in err
    assert "notes.md also mention 'alpha'" in err
    assert "==> rename" not in err  # the progress stays hidden


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes: POSIX")
def test_a_named_pipe_is_never_read(tmp_path: Path) -> None:
    """A FIFO outside src/ and tests/ (a dev script's run/app.fifo): the search for other files
    that mention the old name opened it, and open() waits for a writer: rename, and apply after
    an app.name edit (--dry-run too), hung forever."""
    _write_project(tmp_path, "script", "alpha")
    (tmp_path / "run").mkdir()
    fifo = tmp_path / "run" / "app.fifo"
    os.mkfifo(fifo)
    (tmp_path / "notes.md").write_text("Run alpha.\n", encoding="utf-8")
    planned: list[rename.Plan] = []
    worker = threading.Thread(target=lambda: planned.append(rename.plan(tmp_path, "alpha", "beta")), daemon=True)
    worker.start()
    worker.join(30)
    if worker.is_alive():  # it waits for a writer: give it one, then fail
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(10)
        pytest.fail("rename.plan hung on a named pipe")
    assert planned[0].mentions == ["notes.md"]


def test_a_windows_junction_in_src_is_never_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """os.walk enters a junction (os.path.islink is False for it) and the files behind it were
    rewritten, outside the project; now it is a link like any other: reported, never rewritten."""
    import stat
    import types

    _write_project(tmp_path, "script", "alpha")
    junction = tmp_path / "src" / "alpha" / "shared"
    junction.mkdir()
    (junction / "helper.py").write_text("from alpha.core import bench\n", encoding="utf-8")
    real_lstat = os.lstat

    def lstat(path: Any, *a: Any, **k: Any) -> Any:
        if Path(path) == junction:
            return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_reparse_tag=cmd_env._JUNCTION)
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(cmd_env.os, "lstat", lstat)
    planned = rename.plan(tmp_path, "alpha", "beta")
    assert "src/alpha/shared/helper.py" not in [f.path for f in planned.files]
    assert planned.linked == ["src/alpha/shared/"]


def test_mentions_outside_src_and_tests_are_reported(tmp_path: Path) -> None:
    _write_project(tmp_path, "script", "alpha")
    files = {
        "scripts/gen.py": "from alpha.core import bench\n",
        "docs/usage.md": "Run `python -m alpha`.\n",
        ".github/workflows/mine.yml": "run: python -m alpha\n",
        ".github/workflows/ci.yml": "name: alpha\n",  # generated: re-rendered, never listed
        ".venv/lib/alpha.py": "alpha\n",
        "dist/alpha-cpython-pyz/notes.txt": "alpha\n",
        ".pytemplate/editor.json": '{"name": "alpha"}\n',
        ".claude/worktrees/x/src/alpha/app.py": "import alpha\n",
        "big.txt": "alpha\n" + "x" * (rename.MENTION_MAX_BYTES + 1),
    }
    for rel_path, text in files.items():
        (tmp_path / rel_path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel_path).write_text(text, encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8") + '\n[tool.pt-cov]\nsource = ["alpha"]\ninclude = ["src/alpha/*"]\n', encoding="utf-8")
    planned = rename.plan(tmp_path, "alpha", "beta", generated={".github/workflows/ci.yml"})
    assert planned.mentions == [".github/workflows/mine.yml", "docs/usage.md", "scripts/gen.py"]
    assert planned.pyproject is not None
    assert [line for _, line in planned.pyproject.kept] == ['source = ["alpha"]', 'include = ["src/alpha/*"]']
    rename.apply_plan(tmp_path, planned)
    assert (tmp_path / "scripts" / "gen.py").read_text(encoding="utf-8") == files["scripts/gen.py"]  # listed, not changed
    assert 'source = ["alpha"]' in pyproject.read_text(encoding="utf-8")


def test_rename_accepts_a_pyproject_with_a_bom(tmp_path: Path) -> None:
    _write_project(tmp_path, "flet", "alpha")
    path = tmp_path / "pyproject.toml"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    rename.apply_plan(tmp_path, rename.plan(tmp_path, "alpha", "My-Game"))
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    parsed = tomllib.loads(data.decode("utf-8"))
    assert parsed["project"]["name"] == "My-Game" and parsed["tool"]["flet"]["product"] == "My-Game"


def test_pytemplate_toml_keeps_its_bom_and_line_endings(tmp_path: Path) -> None:
    """Like config.update_file: a Windows editor's CRLF and BOM survive the rename."""
    _write_project(tmp_path, "raylib", "alpha")
    path = tmp_path / "pytemplate.toml"
    text = path.read_text(encoding="utf-8")
    path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))
    planned = _rename(tmp_path, "alpha", "My-Game")
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and data.count(b"\r\n") == data.count(b"\n")
    expected = presets.skeleton("raylib", "My-Game")["pytemplate.toml"].replace(b"\n", b"\r\n")
    assert data == b"\xef\xbb\xbf" + expected  # byte for byte what new would write, in the file's own form
    assert planned.config.bom and "\r\n" in planned.config.new


def test_a_pytemplate_toml_that_is_not_utf8_is_a_clear_error(tmp_path: Path) -> None:
    _write_project(tmp_path, "script", "alpha")
    path = tmp_path / "pytemplate.toml"
    path.write_bytes(path.read_text(encoding="utf-8").encode("utf-16"))
    with pytest.raises(DeployError, match="UTF-16") as e:
        rename.plan(tmp_path, "alpha", "beta")
    assert e.value.code == 2


PROJECT_NAME_VARIANTS = {
    "single-quotes": "[project]\nname = 'alpha'\nversion = \"0.1.0\"\n",
    "indented": "[project]\n  name=\"alpha\"\nversion = \"0.1.0\"\n",
    "table-before": "[[tool.uv.index]]\nname = \"pytorch\"\nurl = \"https://x.invalid/simple\"\n\n[project]\nname = \"alpha\"\nversion = \"0.1.0\"\n",
    "comment": "[project]  # the app\nname = \"alpha\"  # keep\nversion = \"0.1.0\"\n",
    "not-first-key": "[project]\nversion = \"0.1.0\"\nname = \"alpha\"\n",
    "crlf": "[project]\r\nname = \"alpha\"\r\nversion = \"0.1.0\"\r\n",
}


@pytest.mark.parametrize("text", list(PROJECT_NAME_VARIANTS.values()), ids=list(PROJECT_NAME_VARIANTS))
def test_set_project_name_only_touches_the_project_table(text: str) -> None:
    before = tomllib.loads(text)
    after = tomllib.loads(presets.set_project_name(text, "beta"))
    assert after["project"]["name"] == "beta"
    before["project"]["name"] = "beta"
    assert after == before  # e.g. the [[tool.uv.index]] name is untouched


@pytest.mark.parametrize("text", ['[tool.x]\nname = "alpha"\n', '[project]\nversion = "0.1.0"\n', 'project.name = "alpha"\n', "[project\n"])
def test_set_project_name_refuses_what_it_cannot_edit(text: str) -> None:
    with pytest.raises(DeployError, match=r"\[project\] name") as e:
        presets.set_project_name(text, "beta")
    assert e.value.code == 2


def test_a_crlf_pyproject_keeps_its_line_endings(tmp_path: Path) -> None:
    _write_project(tmp_path, "flet", "alpha")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_bytes(pyproject.read_bytes().replace(b"\n", b"\r\n"))  # a Windows checkout
    planned = _rename(tmp_path, "alpha", "beta")
    data = pyproject.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n") and b'name = "beta"\r\n' in data
    assert planned.pyproject is not None and [line for _, line, _ in planned.pyproject.changes][0] == 'name = "alpha"'


def test_a_file_that_cannot_be_read_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A root-owned file in tests/ (a container run), a file another program holds on Windows."""
    _write_project(tmp_path, "script", "alpha")
    locked = tmp_path / "tests" / "data.txt"
    locked.write_text("alpha\n", encoding="utf-8")
    real = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self.name == "data.txt":
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(DeployError, match=r"rename: cannot read tests/data.txt: Permission denied") as e:
        rename.plan(tmp_path, "alpha", "beta")
    assert e.value.code == 2 and "nothing was changed" in str(e.value)


def test_rename_of_a_single_quoted_project_name(tmp_path: Path) -> None:
    _write_project(tmp_path, "script", "alpha")
    path = tmp_path / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('name = "alpha"', "name = 'alpha'", 1), encoding="utf-8", newline="\n")
    _rename(tmp_path, "alpha", "beta")
    assert tomllib.loads(path.read_text(encoding="utf-8"))["project"]["name"] == "beta"


def test_a_pyproject_without_project_name_stops_the_plan(tmp_path: Path) -> None:
    _write_project(tmp_path, "script", "alpha")
    path = tmp_path / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('name = "alpha"\n', "", 1), encoding="utf-8", newline="\n")
    before = _tree(tmp_path)
    with pytest.raises(DeployError, match="nothing was changed"):
        rename.plan(tmp_path, "alpha", "beta")
    assert _tree(tmp_path) == before


# --- names: indirect dependencies, the standard library of every Python ---------------------------------


@pytest.mark.parametrize("extra", [(), ("build", "py")], ids=["lock", "lock-with-build-and-py"])
def test_every_locked_package_name_is_refused(monkeypatch: pytest.MonkeyPatch, extra: tuple[str, ...]) -> None:
    """Any refusal counts: a rule checked first may give its own reason (build is a folder of
    the project, py a Python command), and the test failed in a project that had added them."""
    cfg = _cfg("script")
    names = rename.locked_names(ROOT)
    assert {"iniconfig", "pluggy", "packaging", "pygments"} <= names  # pytest's and rich's own dependencies
    graph = presets._lock_graph()
    monkeypatch.setattr(presets, "_lock_graph", lambda lock=None: {**{k: v | set(extra) for k, v in graph.items()}, **{n: set() for n in extra}})
    monkeypatch.setattr(rename, "locked_names", lambda root: names | set(extra))
    for name in sorted(names | set(extra)):
        with pytest.raises(DeployError) as e:
            rename.check_new_name(cfg, name)
        assert e.value.code == 2
    with pytest.raises(DeployError, match=r"\(iniconfig, "):
        rename.check_new_name(cfg, "IniConfig")  # normalized like uv


@pytest.mark.parametrize("name", ["mypyc", "PyPy", "cpython"])
def test_backend_names_are_refused(name: str) -> None:
    # src/mypyc/ would shadow the compiler in the mypyc stage, and a later rename away from a
    # backend name would rewrite tests/conftest.py's `BACKEND != "mypyc"`
    with pytest.raises(DeployError, match="name of a backend") as e:
        rename.check_new_name(_cfg("script"), name)
    assert e.value.code == 2


def test_locked_names_skip_the_project_itself(tmp_path: Path) -> None:
    lock = 'version = 1\n\n[[package]]\nname = "alpha"\nversion = "0.1.0"\nsource = { virtual = "." }\n\n'
    lock += '[[package]]\nname = "Foo_Bar"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    assert rename.locked_names(tmp_path) == {"foo-bar"}
    assert rename.locked_names(tmp_path / "missing") == set()
    (tmp_path / "uv.lock").write_text("not [toml", encoding="utf-8")
    assert rename.locked_names(tmp_path) == set()
    (tmp_path / "uv.lock").write_text("package = 3\n", encoding="utf-8")
    assert rename.locked_names(tmp_path) == set()


def test_case_only_rename_of_the_project_is_not_a_lock_clash() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
    assert presets._norm_name(project) not in rename.locked_names(ROOT)


@pytest.mark.parametrize("name", ["compression", "annotationlib", "imp", "asyncore", "distutils", "sre_parse"])
def test_stdlib_names_do_not_depend_on_the_runner(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(sys, "stdlib_module_names", frozenset({"json"}))
    with pytest.raises(DeployError, match="standard library") as e:
        rename.check_new_name(_cfg("script"), name)
    assert e.value.code == 2
    assert presets.shadows_stdlib(name) and presets.shadows_stdlib("json") and not presets.shadows_stdlib("beta")


# --- git -------------------------------------------------------------------------------------------------


def _git_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    config = tmp_path / "gitconfig"
    # no background maintenance after a commit: it writes and removes .git/objects/maintenance.lock
    # while a test compares the tree (git 2.5x on macOS)
    config.write_text("[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n", encoding="utf-8")
    return env | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config), "GIT_CEILING_DIRECTORIES": str(tmp_path)}


def _git(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, env=env, capture_output=True, text=True, check=False,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not found")
def test_git_changes_no_repo_clean_dirty_subfolder_and_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _git_env(tmp_path)
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)
    repo = tmp_path / "repo"
    project = repo / "apps" / "p"
    project.mkdir(parents=True)
    assert rename.git_changes(project) is None
    _git(repo, env, "init", "-q")
    assert rename.git_changes(project) == []
    (project / "x.py").write_text("x", encoding="utf-8")
    (repo / "outside.txt").write_text("x", encoding="utf-8")
    assert rename.git_changes(project) == ["x.py"]  # relative to the project, outside files ignored
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", "x")
    _git(repo, env, "mv", "apps/p/x.py", "apps/p/y.py")
    assert rename.git_changes(project) == ["y.py"]  # a rename: the old path is not a second entry
    probe = subprocess.run(["git", "status"], cwd=project, env=env | {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}, capture_output=True, text=True, check=False)
    if probe.returncode == 0:
        pytest.skip("this git ignores GIT_TEST_ASSUME_DIFFERENT_OWNER")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    result = rename.git_changes(project)
    assert isinstance(result, str) and "dubious ownership" in result


@pytest.mark.skipif(os.name == "nt", reason="a sh script as git")
def test_git_changes_reads_git_in_english(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fake.write_text(
        '#!/bin/sh\nif [ "$LC_ALL" = C ]; then echo "fatal: not a git repository (or any of the parent directories): .git" >&2\n'
        'else echo "fatal: no es un repositorio git" >&2; fi\nexit 128\n',  # lang: allow
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("LC_ALL", "es_ES.UTF-8")
    monkeypatch.setenv("LANGUAGE", "es")
    assert rename.git_changes(tmp_path) is None


def test_dirty_tree_message() -> None:
    assert rename.dirty_tree_message(None, "rename", "x") is None
    assert rename.dirty_tree_message([], "rename", "x") is None
    many = rename.dirty_tree_message([f"f{i}" for i in range(7)], "rename", "./deploy rename b --force")
    assert many is not None and "7 path(s): f0, f1, f2, f3, f4 and 2 more" in many and "./deploy rename b --force" in many
    broken = rename.dirty_tree_message("fatal: detected dubious ownership", "rename", "x")
    assert broken is not None and broken.startswith("could not check for uncommitted changes in git (fatal: detected dubious")


# --- the command, in-process -----------------------------------------------------------------------------


@pytest.fixture
def command_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A script project named alpha that the rename command works on (no uv: the lock is stubbed)."""
    root = tmp_path / "p"
    root.mkdir()
    _write_project(root, "script", "alpha")
    (root / ".pytemplate").mkdir()
    for module in (rename, render, presets):
        monkeypatch.setattr(module, "ROOT", root)
    for module in (render, presets):
        monkeypatch.setattr(module, "PYPROJECT", root / "pyproject.toml")
    monkeypatch.setattr(render, "STATE_FILE", root / ".pytemplate" / "state.json")
    from runner import cmd_apply

    monkeypatch.setattr(cmd_apply, "ROOT", root)
    monkeypatch.setattr(cmd_env, "ensure_lock", lambda cfg: None)
    monkeypatch.setattr(rename, "tidy_before", lambda cfg, plan_, root=None: None)
    monkeypatch.setattr(proc, "DRY_RUN", False)
    for key, value in _git_env(tmp_path).items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)
    render.apply(_load(root))  # generated files as the project has them
    return root


def _load(root: Path) -> Config:
    cfg: Config = config._build(Config, tomllib.loads((root / "pytemplate.toml").read_text(encoding="utf-8")), "")
    config.validate(cfg, set(cli.COMMANDS))
    return cfg


def test_rename_after_a_hand_edit_finishes_it(command_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = command_project
    text = (root / "pytemplate.toml").read_text(encoding="utf-8")
    (root / "pytemplate.toml").write_text(config.set_value(text, "app", "name", "beta"), encoding="utf-8", newline="\n")
    assert rename.cmd_rename(_load(root), ["beta"]) == 0
    err = capsys.readouterr().err
    assert "was changed by hand" in err and "nothing to do" not in err
    assert (root / "src" / "beta").is_dir() and not (root / "src" / "alpha").exists()
    tree, skeleton = _tree(root), presets.skeleton("script", "beta")
    assert {k: tree.get(k) for k in skeleton} == skeleton  # the skeleton of the new name, pytemplate.toml included


def test_rename_after_a_hand_edit_that_keeps_the_package(command_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """alpha -> Alpha by hand keeps src/alpha/: `rename Alpha` finishes it (pyproject.toml, the
    name in text) like apply does, instead of 'nothing to do' while doctor says it is not applied."""
    from runner import cmd_apply

    root = command_project
    text = (root / "pytemplate.toml").read_text(encoding="utf-8")
    (root / "pytemplate.toml").write_text(config.set_value(text, "app", "name", "Alpha"), encoding="utf-8", newline="\n")
    assert cmd_apply.applied_name(_load(root)) == "alpha"  # doctor and the hook: not applied
    assert rename.cmd_rename(_load(root), ["Alpha"]) == 0
    err = capsys.readouterr().err
    assert "was changed by hand" in err and "nothing to do" not in err
    assert presets.project_name((root / "pyproject.toml").read_text(encoding="utf-8")) == "Alpha"
    tree, skeleton = _tree(root), presets.skeleton("script", "Alpha")
    assert {k: tree.get(k) for k in skeleton} == skeleton
    assert cmd_apply.applied_name(_load(root)) is None
    assert rename.cmd_rename(_load(root), ["Alpha"]) == 0
    assert "nothing to do" in capsys.readouterr().err


def test_rename_to_another_name_after_a_hand_edit_that_keeps_the_package(command_project: Path) -> None:
    root = command_project
    (root / "src" / "alpha" / "notes.txt").write_text("Welcome to alpha.\n", encoding="utf-8")
    text = (root / "pytemplate.toml").read_text(encoding="utf-8")
    (root / "pytemplate.toml").write_text(config.set_value(text, "app", "name", "Alpha"), encoding="utf-8", newline="\n")
    assert rename.cmd_rename(_load(root), ["My-Game"]) == 0  # from the real name, alpha: its prose is the name
    assert (root / "src" / "my_game" / "notes.txt").read_text(encoding="utf-8") == "Welcome to My-Game.\n"
    assert presets.project_name((root / "pyproject.toml").read_text(encoding="utf-8")) == "My-Game"


def test_rename_updates_only_the_projects_own_record(command_project: Path) -> None:
    from runner import cmd_apply

    state = command_project / ".pytemplate" / "state.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    foreign = {"name": "myapp", "preset": "script", "dependencies": [], "dev": []}  # the template's, copied by new
    state.write_text(json.dumps({**data, "applied": foreign}), encoding="utf-8")
    assert rename.cmd_rename(_load(command_project), ["beta"]) == 0
    assert cmd_apply.load_record() == foreign  # not adopted
    state.write_text(json.dumps({**json.loads(state.read_text(encoding="utf-8")), "applied": {**foreign, "name": "beta"}}), encoding="utf-8")
    assert rename.cmd_rename(_load(command_project), ["gamma"]) == 0
    assert cmd_apply.load_record() == {**foreign, "name": "gamma"}  # the project's own: follows the rename


def test_rename_to_a_third_name_after_a_hand_edit(command_project: Path) -> None:
    root = command_project
    text = (root / "pytemplate.toml").read_text(encoding="utf-8")
    (root / "pytemplate.toml").write_text(config.set_value(text, "app", "name", "beta"), encoding="utf-8", newline="\n")
    assert rename.cmd_rename(_load(root), ["gamma"]) == 0
    assert (root / "src" / "gamma").is_dir() and not (root / "src" / "alpha").exists() and not (root / "src" / "beta").exists()
    data = tomllib.loads((root / "pytemplate.toml").read_text(encoding="utf-8"))
    assert data["app"]["name"] == "gamma" and data["compile"]["modules"] == ["gamma.core"]


def test_same_name_with_missing_package_is_an_error(command_project: Path) -> None:
    root = command_project
    text = (root / "pytemplate.toml").read_text(encoding="utf-8")
    (root / "pytemplate.toml").write_text(config.set_value(text, "app", "name", "beta"), encoding="utf-8", newline="\n")
    pyproject = root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('name = "alpha"', 'name = "beta"', 1), encoding="utf-8")
    before = _tree(root)
    with pytest.raises(DeployError, match=r"src/beta/ not found[\s\S]*changed by hand") as e:
        rename.cmd_rename(_load(root), ["beta"])
    assert e.value.code == 2 and _tree(root) == before
    (root / "pytemplate.toml").write_text(text, encoding="utf-8", newline="\n")
    pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('name = "beta"', 'name = "alpha"', 1), encoding="utf-8")
    assert rename.cmd_rename(_load(root), ["alpha"]) == 0  # the package exists: still "nothing to do"


@pytest.mark.parametrize("new", ["helpers", "other"])
def test_rename_refuses_an_app_name_set_to_another_package(command_project: Path, new: str) -> None:
    """app.name set by hand to another package of src/ (src/helpers/): `rename helpers` said
    "nothing to do" while doctor reported it, and `rename other --force` moved src/helpers/, left
    the app in src/alpha/ and recorded the damage as applied."""
    root = command_project
    (root / "src" / "helpers").mkdir()
    (root / "src" / "helpers" / "__init__.py").write_text("", encoding="utf-8")
    text = (root / "pytemplate.toml").read_text(encoding="utf-8")
    (root / "pytemplate.toml").write_text(config.set_value(text, "app", "name", "helpers"), encoding="utf-8", newline="\n")
    before = _tree(root)
    with pytest.raises(DeployError, match=r"names src/helpers/, another package: the app is 'alpha'[\s\S]*Put back app.name = \"alpha\"") as e:
        rename.cmd_rename(_load(root), [new, "--force"])
    assert e.value.code == 2 and _tree(root) == before


def test_rename_to_the_same_name_is_not_done_while_pyproject_differs(command_project: Path) -> None:
    root = command_project
    pyproject = root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('name = "alpha"', 'name = "foo"', 1), encoding="utf-8")
    with pytest.raises(DeployError, match=r"pyproject.toml \[project\] name = 'foo'.\n  ./deploy apply writes 'alpha' there; if src/alpha/ was moved by hand, move it back to src/foo/"):
        rename.cmd_rename(_load(root), ["alpha"])
    assert rename.cmd_rename(_load(root), ["beta"]) == 0  # a real rename writes the name there too
    assert presets.project_name(pyproject.read_text(encoding="utf-8")) == "beta"


def test_dry_run_predicts_exactly_the_rerendered_files(command_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = command_project
    for new in ("Alpha", "My-Game"):  # a case-only rename keeps the package: tasks.json does not change
        cfg = _load(root)
        capsys.readouterr()
        monkeypatch.setattr(proc, "DRY_RUN", True)
        assert rename.cmd_rename(cfg, [new]) == 0
        line = next(ln for ln in capsys.readouterr().err.splitlines() if "generated files" in ln)
        predicted = set(line.split("would re-render ", 1)[1].split(", ")) if "would re-render" in line else set()
        monkeypatch.setattr(proc, "DRY_RUN", False)
        assert rename.cmd_rename(cfg, [new]) == 0
        updated = next((ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("render: updated ")), "render: updated ")
        assert predicted == set(filter(None, updated.split("render: updated ", 1)[1].split(", ")))
        if new == "Alpha":
            assert ".vscode/tasks.json" not in predicted and ".pytemplate/editor.json" in predicted


def test_dry_run_predicts_the_lock_like_the_real_run(command_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A case-only rename keeps the normalized project name, but the real run still re-locks a
    stale uv.lock (ensure_lock): the dry run asks `uv lock --check` (read-only) instead of guessing."""
    calls: list[list[str]] = []
    stale = [True]

    def fake_uv(env: Any, args: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in args])
        assert kw.get("echo") is False and kw.get("cwd") == command_project  # a query in the project, even in a dry run
        return subprocess.CompletedProcess(args, 1 if stale[0] else 0, "", "")

    monkeypatch.setattr(envs, "uv", fake_uv)
    monkeypatch.setattr(render, "pyproject_outdated", lambda cfg: False)
    monkeypatch.setattr(proc, "DRY_RUN", True)

    def lock_line(new: str) -> str:
        capsys.readouterr()
        assert rename.cmd_rename(_load(command_project), [new]) == 0
        return next(ln for ln in capsys.readouterr().err.splitlines() if ln.strip().startswith("uv.lock"))

    assert "would re-lock" in lock_line("Alpha") and calls == [["lock", "--check"]]
    stale[0] = False
    assert "would re-lock" not in lock_line("Alpha") and "up to date" in lock_line("Alpha")
    calls.clear()
    assert "would re-lock" in lock_line("beta") and calls == []  # another project name: re-locked anyway


@pytest.mark.skipif(shutil.which("git") is None, reason="git not found")
def test_dirty_tree_is_refused_forced_and_only_warned_in_a_dry_run(command_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = command_project
    env = _git_env(tmp_path)
    _git(root, env, "init", "-q")
    _git(root, env, "add", "-A")
    _git(root, env, "commit", "-q", "-m", "init")
    (root / "src" / "main.py").write_text("# edited\n", encoding="utf-8")
    render.apply(_load(root), force=True)  # generated files never count as changes
    before = _tree(root)
    with pytest.raises(DeployError, match=r"uncommitted changes in git \(1 path\(s\): src/main.py\)") as e:
        rename.cmd_rename(_load(root), ["beta"])
    assert e.value.code == 2 and "./deploy rename beta --force" in str(e.value) and _tree(root) == before
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert rename.cmd_rename(_load(root), ["beta"]) == 0
    assert "warning: uncommitted changes in git" in capsys.readouterr().err and _tree(root) == before
    monkeypatch.setattr(proc, "DRY_RUN", False)
    assert rename.cmd_rename(_load(root), ["beta", "--force"]) == 0
    assert (root / "src" / "beta").is_dir()


def test_a_failed_move_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_project(tmp_path, "script", "alpha")
    before = _tree(tmp_path)
    planned = rename.plan(tmp_path, "alpha", "beta")

    def locked(self: Path, target: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "rename", locked)
    with pytest.raises(DeployError, match="Nothing was changed") as e:
        rename.apply_plan(tmp_path, planned)
    assert e.value.code == 2 and _tree(tmp_path) == before


def _everything(root: Path) -> dict[str, bytes | None]:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


@pytest.mark.parametrize("fail_at", ["first", "second", "last"])
def test_a_failed_write_undoes_the_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: str) -> None:
    """A file that cannot be written after the move (read-only, locked by an editor) must not leave
    a half-renamed project: the written files get their bytes back and the folder moves back."""
    _write_project(tmp_path, "flet", "alpha", crlf=True)
    before = _everything(tmp_path)
    planned = rename.plan(tmp_path, "alpha", "beta")
    total = len(planned.changed_files) + 2  # + pytemplate.toml and pyproject.toml
    target = {"first": 0, "second": 1, "last": total - 1}[fail_at]
    real = Path.write_bytes
    calls: list[Path] = []

    def flaky(self: Path, data: Any) -> int:
        calls.append(self)
        if len(calls) - 1 == target:
            raise PermissionError(13, "Permission denied")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    with pytest.raises(DeployError, match=r"could not write .*Permission denied\. The rename was undone") as e:
        rename.apply_plan(tmp_path, planned)
    monkeypatch.setattr(Path, "write_bytes", real)
    assert e.value.code == 2
    assert _everything(tmp_path) == before  # byte for byte, CRLF included (no temporary file left either)
    assert calls[target].name.startswith((".pyproject.toml.", ".pytemplate.toml.")) or fail_at != "last"


def test_a_write_that_fails_midway_leaves_the_file_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disk full, a quota: the OS had written part of the new bytes when it gave up. That file must
    keep its old bytes too (the rename writes to a temporary file first), not only the ones before."""
    _write_project(tmp_path, "script", "alpha")
    (tmp_path / "tests" / "report.txt").write_text("row: alpha\n" * 2000, encoding="utf-8")
    before = _everything(tmp_path)
    planned = rename.plan(tmp_path, "alpha", "beta")
    real = Path.write_bytes

    def disk_full(self: Path, data: Any) -> int:
        if "report.txt" in self.name:
            with open(self, "wb") as f:
                f.write(data[:100])  # what reached the disk
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", disk_full)
    with pytest.raises(DeployError, match=r"could not write tests/report.txt: No space left on device\. The rename was undone"):
        rename.apply_plan(tmp_path, planned)
    monkeypatch.setattr(Path, "write_bytes", real)
    assert _everything(tmp_path) == before


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX file size limit (ulimit -f)")
def test_a_write_cut_short_by_the_file_size_limit_leaves_the_file_whole(tmp_path: Path) -> None:
    """The verifier's reproduction: `ulimit -f` stops the write of a big file in the middle."""
    _write_project(tmp_path, "script", "alpha")
    (tmp_path / "tests" / "report.txt").write_text("row: alpha\n" * 20000, encoding="utf-8")  # 220 KB > the limit
    before = _everything(tmp_path)
    code = (
        "import resource, signal, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(TEMPLATE_DIR)!r})\n"
        "from runner import rename\n"
        "from runner.ui import DeployError\n"
        "root = Path(sys.argv[1])\n"
        "planned = rename.plan(root, 'alpha', 'beta')\n"
        "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
        "resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))\n"
        "try:\n"
        "    rename.apply_plan(root, planned)\n"
        "except DeployError as e:\n"
        "    print(e)\n"
    )
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=120, check=False)
    assert "could not write tests/report.txt" in r.stdout and "The rename was undone" in r.stdout, r.stdout + r.stderr
    assert _everything(tmp_path) == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and symlinks")
def test_rewritten_files_keep_their_mode_and_links(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    _write_project(root, "script", "alpha")
    script = root / "tests" / "run.sh"
    script.write_text("#!/bin/sh\npython -m alpha\n", encoding="utf-8")
    script.chmod(0o755)
    shared = tmp_path / "shared.toml"  # pytemplate.toml kept elsewhere and linked into the project
    (root / "pytemplate.toml").rename(shared)
    (root / "pytemplate.toml").symlink_to(shared)
    _rename(root, "alpha", "beta")
    assert script.read_text(encoding="utf-8") == "#!/bin/sh\npython -m beta\n" and script.stat().st_mode & 0o777 == 0o755
    assert (root / "pytemplate.toml").is_symlink() and 'name = "beta"' in shared.read_text(encoding="utf-8")


def test_a_restore_that_fails_is_never_called_undone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_project(tmp_path, "script", "alpha")
    planned = rename.plan(tmp_path, "alpha", "beta")
    real = rename._replace_bytes
    calls: list[Path] = []

    def flaky(path: Path, data: bytes) -> None:
        calls.append(path)
        if len(calls) in (2, 3):  # the second write fails, then restoring the first one fails too
            raise PermissionError(13, "Permission denied")
        real(path, data)

    monkeypatch.setattr(rename, "_replace_bytes", flaky)
    with pytest.raises(DeployError) as e:
        rename.apply_plan(tmp_path, planned)
    first = calls[0].relative_to(tmp_path).as_posix().replace("src/beta/", "src/alpha/", 1)  # where it is now
    message = str(e.value)
    assert "The rename was undone" not in message and "NOT fully undone" in message
    assert f"{first} could not be restored" in message
    assert (tmp_path / "src" / "alpha").is_dir()  # the folder moved back all the same


def test_a_broken_pyproject_is_a_clear_error_not_a_traceback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_project(tmp_path, "script", "alpha")
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    monkeypatch.setattr(presets, "PYPROJECT", tmp_path / "pyproject.toml")
    with pytest.raises(DeployError, match="pyproject.toml is not valid TOML: .*: fix it first") as e:
        rename.check_new_name(_cfg("script"), "beta")
    assert e.value.code == 2


def test_name_checks_tolerate_a_bom_and_do_not_depend_on_the_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "pyproject.toml"
    target.write_bytes(b'\xef\xbb\xbf[project]\nname = "x"\ndependencies = ["Rich>=13"]\n[dependency-groups]\ndev = ["pytest"]\n')
    monkeypatch.setattr(presets, "PYPROJECT", target)
    assert presets._declared(None) == {"rich"} and presets._declared("dev") == {"pytest"}
    rename.check_new_name(_cfg("script"), "beta")  # a BOM (an editor, PowerShell 5.1) is fine
    with pytest.raises(DeployError, match="dependency"):
        rename.check_new_name(_cfg("script"), "rich")
    monkeypatch.setattr(sys, "stdlib_module_names", frozenset({"json"}))
    for name in ("compression", "annotationlib", "imp", "distutils"):  # `new` and `__init` too
        with pytest.raises(DeployError, match="standard library") as e:
            presets.check_name_free(None, "script", name)
        assert e.value.code == 2


def test_arguments(command_project: Path) -> None:
    cfg = _load(command_project)
    with pytest.raises(DeployError, match=r"unknown argument\(s\): --dry-run") as e:
        rename.cmd_rename(cfg, ["beta", "--dry-run"])  # a global flag after the command
    assert e.value.code == 2
    with pytest.raises(SystemExit) as exit_:
        rename.cmd_rename(cfg, ["-h"])
    assert exit_.value.code == 0


def test_the_renamed_config_is_validated_before_anything_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(DeployError, match=r"^rename: the renamed pytemplate.toml would be invalid .*nothing was changed"):
        rename.validate_config('[app]\nname = "beta"\nbogus = 1\n')
    with pytest.raises(DeployError, match="would not be valid TOML"):
        rename.validate_config("[app\n")


# --- names at random: the skeleton invariant and the round trip ----------------------------------------


def _random_names(count: int) -> list[str]:
    rng = random.Random(20260926)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out: list[str] = []
    while len(out) < count:
        name = "zq" + "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6)))
        if rng.random() < 0.5:
            name += rng.choice("-_") + "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 4)))
        if name not in out and package_of(name) not in {package_of(n) for n in out}:
            out.append(name)
    return out


@pytest.mark.parametrize("preset", ["script", "raylib", "flet"])
def test_random_names_reproduce_the_skeleton_and_round_trip(tmp_path: Path, preset: str) -> None:
    names = _random_names(6)
    for i, (a, b) in enumerate(zip(names, names[1:], strict=False)):
        root = tmp_path / f"r{i}"
        root.mkdir()
        _write_project(root, preset, a)
        original = _tree(root)
        _rename(root, a, b)
        assert _tree(root) == presets.skeleton(preset, b), (a, b)
        _rename(root, b, a)
        assert _tree(root) == original, (b, a)


def test_ruff_tidy_after_a_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Imports sorted and lines re-wrapped with the project's profile, only where the user had them so."""
    if not envs.tool_env(_cfg("script")).python.is_file():
        pytest.skip("no .venv with ruff (./deploy setup)")
    root = tmp_path / "p"
    root.mkdir()
    _write_project(root, "script", "zzz")
    monkeypatch.setattr(render, "ROOT", root)
    monkeypatch.setattr(cmd_dev, "BUILD", tmp_path / "build")
    text = (root / "pytemplate.toml").read_text(encoding="utf-8").replace('relaxed = "off"', 'relaxed = "strict"')
    (root / "pytemplate.toml").write_text(text, encoding="utf-8", newline="\n")
    cfg = _load(root)
    tests = root / "tests"
    (tests / "test_order.py").write_text("from main import _main\nfrom zzz.core import bench\n\nX = (_main, bench)\n", encoding="utf-8")
    long = "from zzz.core.bench import __name__ as bench_module_name_that_fills_the_line_up_to_a_hundred_cols\n"
    assert len(long) <= 100
    (tests / "test_wrap.py").write_text(long + "\nY = bench_module_name_that_fills_the_line_up_to_a_hundred_cols\n", encoding="utf-8")
    (tests / "test_mess.py").write_text("from zzz.core import bench\nZ=bench\n", encoding="utf-8")  # unformatted by choice
    planned = rename.plan(root, "zzz", "zzzzzzzzzzzz")
    clean = rename.tidy_before(cfg, planned, root)
    if clean is None:
        pytest.skip("ruff could not run (uv or .venv unavailable)")
    assert "tests/test_mess.py" not in clean.formatted and "tests/test_wrap.py" in clean.formatted
    assert "tests/test_mess.py" not in clean.sorted_imports and "tests/test_order.py" in clean.sorted_imports
    rename.apply_plan(root, planned)
    rename.tidy_after(rename.validate_config(planned.config.new), planned, clean, root)
    order = (tests / "test_order.py").read_text(encoding="utf-8")
    assert order.index("from main import") < order.index("from zzzzzzzzzzzz.core import")  # m < z: order kept
    wrapped = (tests / "test_wrap.py").read_text(encoding="utf-8")
    assert all(len(line) <= 100 for line in wrapped.splitlines()), wrapped
    assert (tests / "test_mess.py").read_text(encoding="utf-8") == "from zzzzzzzzzzzz.core import bench\nZ=bench\n"
