"""`./deploy rename NEW_NAME` (runner/rename.py).

The acceptance test renames every preset skeleton rendered for one name and checks that the
result is byte-identical to the skeleton rendered for the new name: exactly what
`./deploy new --name NEW` would have written. The rest covers the tricky text cases, the
safety checks and the command itself (dry run and a real run in a throwaway copy).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_DIR))

from runner import config, presets, rename  # noqa: E402
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
    before = _snapshot(root)
    r = _deploy(root, "--dry-run", "rename", "My-Game")
    assert r.returncode == 0, r.stderr
    assert "would move       src/myapp/ -> src/my_game/" in r.stderr, r.stderr
    assert "uv.lock          would re-lock" in r.stderr
    assert "+ from my_game.core import bench" in r.stderr
    assert _snapshot(root) == before, "--dry-run wrote files"

    r = _deploy(root, "rename", "My-Game", "--bogus")
    assert r.returncode == 2 and "unknown argument(s): --bogus" in r.stderr

    r = _deploy(root, "rename", "My-Game")
    assert r.returncode == 0, r.stderr
    assert not (root / "src" / "myapp").exists()
    assert (root / "src" / "my_game" / "app.py").is_file()
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    assert "my-game" in {p["name"] for p in lock["package"]}
    editor = (root / ".pytemplate" / "editor.json").read_text(encoding="utf-8")
    assert '"pkg": "my_game"' in editor
    check = _deploy(root, "render", "--check")
    assert check.returncode == 0, check.stderr

    r = _deploy(root, "rename", "My-Game")
    assert r.returncode == 0 and "nothing to do" in r.stderr
