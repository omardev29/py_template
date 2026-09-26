"""Presets, `./deploy new` and its internal step `__init` (runner/presets.py, cmd_mode.cmd_new/_plan_init).

- The preset data and skeletons: every preset rendered with several names is complete, valid,
  ruff-clean under every typing profile (a fresh project must pass its own pre-commit hook),
  free of stale years; the template root is the script skeleton named myapp.
- The name rules (format, keywords, stdlib, the project's own folders, direct and indirect
  dependencies) and the per-preset tested pins (constraints.txt).
- `copy_template`: only what git tracks, never the template's own files.
- `new` and `init` in-process with uv faked: every failure puts back or removes what was written.
- Real runs of `new` for every preset (need uv and the network: skipped without them).
"""

from __future__ import annotations

import copy
import functools
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_DIR))

from runner import cli, cmd_mode, config, presets, proc, render  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import ROOT  # noqa: E402
from runner.ui import DeployError  # noqa: E402

TEMPLATE_REPO = (ROOT / ".pytemplate" / "template-repo").is_file()
template_repo = pytest.mark.skipif(not TEMPLATE_REPO, reason="an invariant of the template repository itself")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
PRESETS = presets.available()
NAMES = ["myapp", "My-Game_2", "e2e-raylib"]
TOKEN = re.compile(r"\{\{\w*\}\}|__pkg__")
# Non-ASCII folder names, written with escapes so this file stays ASCII
CAFE = "caf\N{LATIN SMALL LETTER E WITH ACUTE}"
CAFE_DECOMPOSED = "cafe\N{COMBINING ACUTE ACCENT}"
NAIVE = "na\N{LATIN SMALL LETTER I WITH DIAERESIS}ve app"
ANGSTROM = "\N{LATIN SMALL LETTER A WITH RING ABOVE}ngstr\N{LATIN SMALL LETTER O WITH DIAERESIS}m_"
CJK = "\N{CJK UNIFIED IDEOGRAPH-65E5}\N{CJK UNIFIED IDEOGRAPH-672C}"


def _write(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _snapshot(root: Path, lf: bool = False) -> dict[str, str]:
    """Every file (sha256) and folder under root, without .build/ (scratch) and caches; `lf`
    reads CRLF as LF (a Windows checkout with core.autocrlf, which the runner writes back as LF)."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] == ".build" or "__pycache__" in rel.parts:
            continue
        data = path.read_bytes() if path.is_file() else None
        if data is not None and lf:
            data = data.replace(b"\r\n", b"\n")
        out[rel.as_posix()] = "<dir>" if data is None else hashlib.sha256(data).hexdigest()
    return out


def _config(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg, set(cli.COMMANDS))
    return cfg


def _skeleton_config(preset: str, name: str) -> Config:
    return _config(tomllib.loads(presets.skeleton(preset, name)["pytemplate.toml"].decode("utf-8")))


def _child_env(tmp: Path) -> dict[str, str]:
    """The environment of a child ./deploy: no uv/venv selection, no launcher variables, no
    global or system git config."""
    drop = ("UV", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON")
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("PYTEMPLATE_")}
    gitconfig = tmp / "gitconfig"
    gitconfig.touch()
    env.update(NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1")
    return env


def _deploy(root: Path, *args: str, cwd: Path, env: dict[str, str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(root / ".pytemplate" / "deploy.py"), *args],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )


@functools.cache
def _offline_reason() -> str | None:
    """None when uv can resolve from the package index now, else why not (a skip reason)."""
    uv = shutil.which("uv")
    if uv is None:
        return "uv not found"
    argv = [uv, "pip", "compile", "--no-cache", "--quiet", "--python-version", "3.12", "--python-platform", "linux", "-"]
    try:
        r = subprocess.run(argv, input="iniconfig\n", capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "uv could not reach the package index"
    return None if r.returncode == 0 else "no network: uv cannot reach the package index"


@pytest.fixture
def network() -> None:
    reason = _offline_reason()
    if reason:
        pytest.skip(reason)


# --- the template and the preset data ---------------------------------------------------------


@template_repo
def test_template_root_is_the_script_skeleton_named_myapp() -> None:
    cfg = config.load(set(cli.COMMANDS))
    assert (cfg.app.name, cfg.app.preset) == ("myapp", "script")
    assert presets.pristine(cfg), "src/, tests/ or typings/ differ from presets/script/files rendered as myapp"
    root_config = (ROOT / "pytemplate.toml").read_bytes().replace(b"\r\n", b"\n")
    assert root_config == presets.skeleton("script", "myapp")["pytemplate.toml"]


@template_repo
def test_committed_generated_files_are_up_to_date() -> None:
    cfg = config.load(set(cli.COMMANDS))
    assert render.apply(cfg, check=True) == ([], []), "./deploy render, then commit the generated files"
    assert not render.pyproject_outdated(cfg), "./deploy lock"


def test_the_e2e_smoke_texts_are_in_the_skeletons() -> None:
    """selftest --e2e looks for these texts in the output of the built apps (e2e.SMOKE,
    e2e.COMPILED_MARK): a skeleton edit must keep them."""
    from runner import e2e

    for preset in PRESETS:
        sources = "\n".join(d.decode("utf-8") for r, d in presets.skeleton(preset, "myapp").items() if r.endswith(".py"))
        for text in (e2e.SMOKE.get(preset, ((), ""))[1], e2e.COMPILED_MARK.get(preset, "")):
            assert text in sources, f"{preset}: {text!r}"


def test_every_preset_ships_the_same_conftest() -> None:
    copies = {p: (presets.PRESETS / p / "files" / "tests" / "conftest.py").read_bytes().replace(b"\r\n", b"\n") for p in PRESETS}
    assert len(set(copies.values())) == 1, f"tests/conftest.py differs between presets: {sorted(copies)}"
    if TEMPLATE_REPO:
        assert (ROOT / "tests" / "conftest.py").read_bytes().replace(b"\r\n", b"\n") == copies["script"]


@pytest.mark.parametrize("preset", PRESETS)
@pytest.mark.parametrize("name", NAMES)
def test_skeleton_renders_every_token(preset: str, name: str) -> None:
    pkg = name.replace("-", "_").lower()
    files = presets.skeleton(preset, name)
    assert {"pytemplate.toml", "src/main.py", f"src/{pkg}/__init__.py", "tests/conftest.py"} <= set(files)
    for rel, data in files.items():
        assert not TOKEN.search(rel), rel
        text = data.decode("utf-8")
        assert not TOKEN.search(text), f"{rel}: {TOKEN.search(text)}"
        assert "\r\n" not in text, rel


@pytest.mark.parametrize("preset", PRESETS)
@pytest.mark.parametrize("name", NAMES)
def test_skeleton_config_is_valid_and_names_the_app(preset: str, name: str) -> None:
    cfg = _skeleton_config(preset, name)
    pkg = name.replace("-", "_").lower()
    assert (cfg.app.name, cfg.app.preset, cfg.pkg) == (name, preset, pkg)
    files = presets.skeleton(preset, name)
    assert f"{pkg}.core" in cfg.compile.modules
    for module in cfg.compile.modules:
        path = "src/" + module.replace(".", "/")
        assert f"{path}/__init__.py" in files or f"{path}.py" in files, module
    # [preset.<p>] of the skeleton repeats the preset.toml defaults: both are documentation
    assert cfg.preset_options(preset) == presets.load(preset).get("options", {})


@pytest.mark.parametrize("preset", PRESETS)
def test_preset_toml_is_complete(preset: str) -> None:
    data = presets.load(preset)
    assert set(data) <= set(presets.PRESET_KEYS), set(data)
    assert str(data.get("description", "")).strip()
    opts = dict(data.get("options", {}))
    values: list[str] = [*data.get("dependencies", []), *data.get("dev_dependencies", [])]
    for value in data.get("uv", {}).values():
        values += value if isinstance(value, list) else [value]
    for value in values:
        str(value).format_map(opts)  # KeyError: an {option} without a default in [options]
    extra = presets.extra_tables(preset, "My-App")
    assert not TOKEN.search(extra), extra
    tomllib.loads(extra)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"description = [\n", "is not valid TOML"),
        (b'description = "caf\xe9"\n', r"is not UTF-8 text \(byte 18\)"),
        (b'description = "x"\nversion = "1"\n', "unknown key 'version'"),
        (b'dependencies = "flet"\n', "'dependencies' must be a list of strings"),
        (b"dev_dependencies = [1]\n", "'dev_dependencies' must be a list of strings"),
        (b"description = 1\n", "'description' must be a string"),
        (b'options = "x"\n', "'options' must be a table"),
        (b"[pyproject]\n", "'pyproject' must be a string"),
    ],
)
def test_a_broken_preset_toml_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes, message: str) -> None:
    """render reads preset.toml on every run (the managed [tool.uv] block): a damaged file must
    say which file and what, never end in a traceback."""
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "preset.toml").write_bytes(raw)
    monkeypatch.setattr(presets, "PRESETS", tmp_path)
    with pytest.raises(DeployError, match=message) as e:
        presets.load("p")
    assert e.value.code == 2
    assert "preset.toml" in str(e.value)
    cfg = config._build(Config, {"app": {"name": "demo", "preset": "p"}}, "")
    with pytest.raises(DeployError, match=message):
        render.managed_block(cfg)


def test_preset_toml_may_carry_a_bom_and_crlf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An editor or PowerShell 5.1 adds a BOM: both readers of preset.toml (presets.load, and
    config's [preset.<p>] check, which cannot import presets) take it like uv and render do."""
    (tmp_path / "p").mkdir()
    text = 'description = "x"\r\ndependencies = ["a=={version}"]\r\n\r\n[options]\r\nversion = "1"\r\n'
    (tmp_path / "p" / "preset.toml").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    monkeypatch.setattr(presets, "PRESETS", tmp_path)
    assert presets.load("p") == {"description": "x", "dependencies": ["a=={version}"], "options": {"version": "1"}}
    assert config._preset_options(tmp_path / "p" / "preset.toml") == {"version": "1"}


def test_unknown_preset_lists_the_available_ones() -> None:
    with pytest.raises(DeployError, match=r"unknown preset 'nope' \(available: .*script") as e:
        presets.load("nope")
    assert e.value.code == 2


YEAR = re.compile(r"(?<![\d.])(?:19|20)\d\d(?![\d.])")
COPYRIGHT = re.compile(r"(?i)copyright|\(c\)|\N{COPYRIGHT SIGN}")


@pytest.mark.parametrize("preset", PRESETS)
def test_no_preset_hard_codes_a_year(preset: str) -> None:
    """A year baked into a preset (the flet copyright that `flet build` embeds) goes stale in
    every project created in a later year."""
    raw = (presets.PRESETS / preset / "preset.toml").read_text(encoding="utf-8")
    assert not YEAR.search(raw), f"{preset}/preset.toml: {YEAR.search(raw)}"
    bad = [
        f"{rel}: {line.strip()}"
        for rel, data in presets.skeleton(preset, "myapp").items()
        for line in data.decode("utf-8").splitlines()
        if COPYRIGHT.search(line) and YEAR.search(line)
    ]
    assert not bad, bad


@pytest.mark.skipif("flet" not in PRESETS, reason="no flet preset")
def test_flet_copyright_names_the_app() -> None:
    data = tomllib.loads(presets.extra_tables("flet", "My-Game"))
    assert data["tool"]["flet"]["copyright"] == "Copyright (C) My-Game"


@pytest.mark.skipif(importlib.util.find_spec("ruff") is None, reason="ruff is not installed here")
@pytest.mark.parametrize("preset", PRESETS)
def test_rendered_skeleton_passes_the_precommit_ruff_checks(preset: str, tmp_path: Path) -> None:
    """The pre-commit hook runs `ruff format --check` and ruff with the active profile on the
    staged files: a fresh project must be able to make its first commit (and pass `check`)."""
    failures: list[str] = []
    for name in NAMES:
        root = tmp_path / name
        _write(root, presets.skeleton(preset, name))
        data = tomllib.loads((root / "pytemplate.toml").read_text(encoding="utf-8"))
        variants = {"shipped": data}
        for label, supported in (("py311", ["cpython", "pypy", "mypyc"]), ("py314", ["cpython", "mypyc"])):
            variant = copy.deepcopy(data)
            variant["backend"] = {"active": "cpython", "supported": supported}
            variants[label] = variant
        for label, variant in variants.items():
            cfg = _config(variant)
            for i, profile in enumerate(config.PROFILES):
                conf = root / f"ruff-{label}-{profile}.toml"
                conf.write_text(render.to_toml(render.ruff_config(cfg, profile)), encoding="utf-8")
                runs = [["check", "--no-cache", "--output-format", "concise", "--config", str(conf), "src", "tests"]]
                if i == 0:  # the format settings do not depend on the profile
                    runs.append(["format", "--check", "--no-cache", "--config", str(conf), "src", "tests"])
                for args in runs:
                    r = subprocess.run([sys.executable, "-m", "ruff", *args], cwd=root, capture_output=True, text=True, timeout=120, check=False)
                    if r.returncode != 0:
                        failures.append(f"[{name} {label} {profile}] ruff {args[0]}:\n{r.stdout}{r.stderr}")
    assert not failures, "\n\n".join(failures)


# --- names ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["a", "a1", "My-App", "x_y", "A-B-C", "game2"])
def test_app_name_rule_accepts(name: str) -> None:
    assert config.APP_NAME.fullmatch(name)


@pytest.mark.parametrize("name", ["", "1a", "a-", "a_", "-a", "_a", "a b", "a.b", CAFE, "a/b"])
def test_app_name_rule_refuses(name: str) -> None:
    assert not config.APP_NAME.fullmatch(name)


@pytest.mark.parametrize(
    ("folder", "name"),
    [
        (CAFE, "cafe"),  # accent dropped
        (CAFE_DECOMPOSED, "cafe"),  # the same, decomposed
        (NAIVE, "naive-app"),
        (ANGSTROM, "angstrom"),  # no trailing '_' (uv refuses it)
        ("my.tool", "my-tool"),
        ("a  -  b", "a-b"),
        ("__x__", "x"),
        (CJK, ""),  # nothing left: `new` asks for --name
        ("2game", "2game"),  # kept, then refused by APP_NAME
    ],
)
def test_name_from_folder(folder: str, name: str) -> None:
    assert presets.name_from_folder(folder) == name
    assert name == "" or name[0].isdigit() or config.APP_NAME.fullmatch(name)


@pytest.mark.parametrize(
    ("preset", "name", "message"),
    [
        ("script", "class", "Python keyword"),
        ("script", "json", "standard library module 'json'"),
        ("script", "tests", "src/tests/ would collide with the project's own tests/"),
        ("script", "Tests", "src/tests/ would collide"),
        ("script", "typings", "typings/ (.ruff.toml"),
        ("script", "build", "build/ (.gitignore"),
        ("script", "dist", "dist/ (.gitignore"),
        ("script", "assets", "src/assets/"),
        ("script", "main", "src/main.py"),
        ("raylib", "Assets", "src/assets/"),
        ("flet", "main", "src/main.py"),
        ("flet", "flet", "also the name of a dependency of the 'flet' preset (flet"),
        ("script", "Rich", "also the name of a dependency of the 'script' preset (rich"),
        # uv refuses these (PEP 508): rename relies on this check too
        ("script", "app-", "'app-' is not a valid app name"),
        ("script", "app_", "ending with a letter or digit"),
        ("script", "1app", "starting with a letter"),
        ("script", "my app", "not a valid app name"),
        ("script", "", "not a valid app name"),
        ("script", CAFE, "not a valid app name"),
        # Windows device names: the folder cannot exist there, git cannot check it out
        ("script", "aux", "src/aux/ cannot exist on Windows"),
        ("script", "Con", "src/con/ cannot exist on Windows"),
        ("script", "NUL", "reserved device name"),
        ("script", "com1", "reserved device name"),
        ("raylib", "LPT9", "reserved device name"),
        ("script", "prn", "reserved device name"),
    ],
)
def test_check_name_free_refuses(preset: str, name: str, message: str) -> None:
    for cfg in (None, _skeleton_config("script", "myapp")):
        with pytest.raises(DeployError, match=re.escape(message)) as e:
            presets.check_name_free(cfg, preset, name)
        assert e.value.code == 2
        assert str(e.value).endswith("Choose another name with --name NAME")


@pytest.mark.parametrize("name", ["test-s", "builds", "mains", "asset", "my-dist", "typing_s", "Beta", "com10", "auxiliary", "console", "null", "a"])
def test_check_name_free_accepts_near_misses(name: str) -> None:
    presets.check_name_free(_skeleton_config("script", "myapp"), "script", name)


@pytest.mark.parametrize(("name", "message"), [("game-", "valid app name|may only contain"), ("g_", "valid app name|may only contain"), ("aux", "Windows"), ("typings", "typings/")])
def test_rename_refuses_the_names_check_name_free_refuses(name: str, message: str) -> None:
    """`./deploy rename` goes through check_name_free: a name uv refuses (game-) used to move
    src/ and rewrite the project before `uv lock` failed on it."""
    from runner import rename

    with pytest.raises(DeployError, match=message) as e:
        rename.check_new_name(config.load(set(cli.COMMANDS)), name)
    assert e.value.code == 2


def test_every_locked_package_name_is_refused() -> None:
    """uv refuses a project that depends on itself, also through a dependency of a dependency
    (rich -> pygments), and on any platform (colorama is win32 only): every name in uv.lock."""
    cfg = config.load(set(cli.COMMANDS))
    locked = presets.locked_names()
    assert locked, "uv.lock is missing or empty"
    for name in sorted(locked):
        with pytest.raises(DeployError, match="also the name of a dependency|standard library") as e:
            presets.check_name_free(cfg, cfg.app.preset, name)
        assert e.value.code == 2
    assert presets._norm_name(cfg.app.name) not in locked  # the project itself is not a clash
    presets.check_name_free(cfg, cfg.app.preset, cfg.app.name)
    presets.check_name_free(cfg, cfg.app.preset, cfg.app.name.upper())


@template_repo
@pytest.mark.parametrize(
    ("preset", "name"),
    [
        ("script", "pygments"),  # rich -> pygments
        ("script", "Markdown_It_Py"),  # normalized like uv does
        ("script", "iniconfig"),  # pytest (dev group) -> iniconfig
        ("raylib", "cffi"),  # the raylib pins: not in the template's uv.lock
        ("raylib", "types-setuptools"),
        ("flet", "httpx"),
        ("flet", "certifi"),
        ("flet", "mdurl"),  # flet-cli -> rich -> markdown-it-py -> mdurl (already locked)
    ],
)
def test_indirect_dependencies_of_every_preset_are_refused(preset: str, name: str) -> None:
    with pytest.raises(DeployError, match="also the name of a dependency"):
        presets.check_name_free(config.load(set(cli.COMMANDS)), preset, name)


# --- uv.lock and the tested pins -----------------------------------------------------------------

FAKE_LOCK = """\
version = 1
requires-python = ">=3.14"

[[package]]
name = "myapp"
version = "0.1.0"
source = { virtual = "." }
dependencies = [{ name = "rich" }]

[package.dev-dependencies]
dev = [{ name = "pytest" }]

[[package]]
name = "rich"
version = "15.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "markdown-it-py" }, { name = "Pygments" }]

[[package]]
name = "markdown-it-py"
version = "4.2.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "mdurl" }]

[package.optional-dependencies]
linkify = [{ name = "linkify-it-py" }]

[[package]]
name = "mdurl"
version = "0.1.2"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "linkify-it-py"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pygments"
version = "2.21.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pytest"
version = "9.1.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "colorama", marker = "sys_platform == 'win32'" }, { name = "pygments" }]

[[package]]
name = "colorama"
version = "0.4.6"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "orphan"
version = "1.0"
source = { registry = "https://pypi.org/simple" }
"""


def test_locked_names_walks_the_dependency_graph(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(FAKE_LOCK, encoding="utf-8")
    everything = {"rich", "markdown-it-py", "mdurl", "linkify-it-py", "pygments", "pytest", "colorama", "orphan"}
    assert presets.locked_names(lock=lock) == everything  # the project itself is not in it
    assert presets.locked_names({"rich"}, lock=lock) == {"rich", "markdown-it-py", "mdurl", "linkify-it-py", "pygments"}
    assert presets.locked_names({"pytest"}, lock=lock) == {"pytest", "colorama", "pygments"}
    assert presets.locked_names({"not-locked"}, lock=lock) == set()
    lock.write_bytes(b"\xef\xbb\xbf" + FAKE_LOCK.encode("utf-8"))  # a BOM, as uv tolerates it
    assert presets.locked_names(lock=lock) == everything
    for broken in ("not [toml", 'package = "x"', "version = 1\n"):
        lock.write_text(broken, encoding="utf-8")
        assert presets.locked_names(lock=lock) == set()
    assert presets.locked_names(lock=tmp_path / "missing.lock") == set()


def _fake_preset(root: Path, name: str, pins: str | None, deps: list[str]) -> None:
    folder = root / name
    (folder / "files" / "src").mkdir(parents=True)
    (folder / "files" / "src" / "main.py").write_text("", encoding="utf-8")
    quoted = ", ".join(f'"{d}"' for d in deps)
    (folder / "preset.toml").write_text(f'description = "x"\ndependencies = [{quoted}]\n', encoding="utf-8")
    if pins is not None:
        (folder / "constraints.txt").write_text(pins, encoding="utf-8")


def test_dependency_names_after_switching_presets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Precise without pins (the old preset's own tree is dropped), conservative with pins (their
    own dependencies are unknown: every locked package counts)."""
    _fake_preset(tmp_path / "presets", "old", None, ["rich>=15"])
    _fake_preset(tmp_path / "presets", "plain", None, ["pytest>=9"])
    _fake_preset(tmp_path / "presets", "pinned", "newpkg==1.0\n", ["newpkg==1.0"])
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "myapp"\ndependencies = ["rich>=15"]\n\n[dependency-groups]\ndev = ["pytest>=9"]\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(FAKE_LOCK, encoding="utf-8")
    monkeypatch.setattr(presets, "PRESETS", tmp_path / "presets")
    monkeypatch.setattr(presets, "PYPROJECT", tmp_path / "pyproject.toml")
    monkeypatch.setattr(presets, "LOCK", tmp_path / "uv.lock")
    cfg = config._build(Config, {"app": {"name": "myapp", "preset": "old"}}, "")
    assert presets._dependency_names(cfg, "plain") == {"pytest", "colorama", "pygments"}  # rich's tree is gone
    same = presets._dependency_names(cfg, "old")
    assert {"rich", "mdurl", "pytest", "colorama", "pygments"} <= same and "orphan" not in same
    assert presets._dependency_names(cfg, "pinned") >= {"newpkg", "mdurl", "orphan", "pytest"}
    presets.check_name_free(cfg, "plain", "mdurl")  # accepted: nothing needs it any more
    with pytest.raises(DeployError, match="dependency"):
        presets.check_name_free(cfg, "pinned", "mdurl")


def test_constraints_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(presets, "PRESETS", tmp_path)
    (tmp_path / "p").mkdir()
    assert presets.constraints("p") == {}  # no file: nothing pinned
    text = "\N{BYTE ORDER MARK}# header\r\n\r\nAnyio == 4.15.1  # a comment\r\nPython_Dateutil==2.9.0.post0\r\nx.y==1!2.0+local\r\n"
    (tmp_path / "p" / "constraints.txt").write_text(text, encoding="utf-8")
    assert presets.constraints("p") == {"anyio": "4.15.1", "python-dateutil": "2.9.0.post0", "x-y": "1!2.0+local"}
    for bad in ("anyio>=4\n", "anyio\n", "==1.0\n", "anyio==\n"):
        (tmp_path / "p" / "constraints.txt").write_text("ok==1\n" + bad, encoding="utf-8")
        with pytest.raises(DeployError, match=r"constraints\.txt:2: expected name==version"):
            presets.constraints("p")
    (tmp_path / "p" / "constraints.txt").write_bytes(b"ok==1\nb\xe9==2\n")
    with pytest.raises(DeployError, match=r"constraints\.txt is not UTF-8 text \(byte 7\): regenerate it") as e:
        presets.constraints("p")
    assert e.value.code == 2


def test_constraints_text_lists_every_locked_package(tmp_path: Path) -> None:
    project = tmp_path / "project.lock"
    project.write_text(
        FAKE_LOCK.replace('name = "myapp"', 'name = "demo"')
        + '\n[[package]]\nname = "Zeta_Pkg"\nversion = "2.0"\nsource = { registry = "https://pypi.org/simple" }\n'
        + '\n[[package]]\nname = "alpha"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
        + '\n[[package]]\nname = "forked"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
        + '\n[[package]]\nname = "forked"\nversion = "2.0"\nsource = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    text = presets.constraints_text("demo", project)
    pins = [line for line in text.splitlines() if line and not line.startswith("#")]
    # sorted; the template's packages too (rich, pytest...); the fork cannot be pinned; no project entry
    assert pins == [
        "alpha==1.0", "colorama==0.4.6", "linkify-it-py==2.0.0", "markdown-it-py==4.2.0", "mdurl==0.1.2",
        "orphan==1.0", "pygments==2.21.0", "pytest==9.1.1", "rich==15.0.0", "zeta-pkg==2.0",
    ]  # fmt: skip
    assert "CLAUDE.md" in text and text.isascii()


@pytest.mark.parametrize("preset", PRESETS)
def test_preset_pins_agree_with_the_preset(preset: str) -> None:
    """constraints.txt must be regenerated when the preset's own pins change (CLAUDE.md 11)."""
    pins = presets.constraints(preset)
    if not pins:
        return
    text = presets.constraints_path(preset).read_text(encoding="utf-8")
    listed = [presets._norm_name(line) for line in text.splitlines() if line.strip() and not line.startswith("#")]
    assert listed == sorted(set(listed)), "not sorted or duplicated: regenerate it (CLAUDE.md 11)"
    deps, dev = presets.dependencies(_skeleton_config(preset, "myapp"), preset)
    for req in (*deps, *dev):
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", req)
        if m:
            assert pins.get(presets._norm_name(m.group(1))) == m.group(2), f"{preset}: {req} vs constraints.txt"
        else:
            assert presets._norm_name(req) in pins or presets._norm_name(req) in presets.locked_names(), req


@template_repo
@pytest.mark.parametrize("preset", PRESETS)
def test_preset_pins_hold_the_whole_tested_tree(preset: str) -> None:
    """constraints.txt pins every package a project of the preset locks, at the versions the
    template tested: the name check reads it (from a project whose uv.lock lacks the preset's
    tree, it is the only place that names it), and `new` from such a project gets those
    versions. A package the template's uv.lock also has must carry the lock's version, and every
    locked package the new project keeps or needs must be there: regenerate constraints.txt
    after changing the template's lock or the preset (CLAUDE.md 11)."""
    pins = presets.constraints(preset)
    template = {presets._norm_name(e["name"]): str(e.get("version")) for e in presets._lock_entries() if not presets._is_project(e)}
    moved = sorted(f"{n}: {pins[n]} (uv.lock: {v})" for n, v in template.items() if n in pins and pins[n] != v)
    assert not moved, f"{preset}: {moved}: regenerate constraints.txt (CLAUDE.md 11)"
    cfg = config.load(set(cli.COMMANDS))
    old_deps, old_dev = presets.dependencies(cfg)
    deps, dev = presets.dependencies(_skeleton_config(preset, "myapp"), preset)
    new = {presets._norm_name(r) for r in (*deps, *dev)}
    kept = (presets._declared_anywhere() - {presets._norm_name(r) for r in (*old_deps, *old_dev)}) | new
    missing = sorted((presets.locked_names(kept) | new) - set(pins))
    assert not missing, f"{preset}: {missing} are not pinned: regenerate constraints.txt (CLAUDE.md 11)"
    if preset == cfg.app.preset:  # the template is a project of this preset: its lock is the tested set
        assert set(pins) == set(template)


# --- copy_template ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(key, raising=False)


TRACKED = {
    ".gitignore": "*.spec\nhtmlcov/\n.venv*/\n/build/\n",
    ".pytemplate/deploy.py": "# entry\n",
    ".pytemplate/template-repo": "",
    ".pytemplate/runner/x.py": "X = 1\n",
    "src/app/__init__.py": "",
    "sub/build/keep.txt": "kept: build/ is only skipped at the root\n",
    ".github/workflows/ci.yml": "name: ci\n",
    ".github/workflows/template-e2e.yml": "name: template\n",
    ".claude/settings.json": "{}\n",
    "deploy": "#!/bin/sh\n",
    "modified.txt": "old\n",
    "deleted.txt": "gone\n",
    "README.md": "# the template\n",
    "LICENSE": "MIT\n",
}
UNTRACKED = {".env": "SECRET=1\n", "notes.txt": "scratch\n", "src/app/__pycache__/m.cpython-314.pyc": "x"}
IGNORED = {"x.spec": "spec\n", "htmlcov/index.html": "<html>\n", ".venv-x/pyvenv.cfg": "home\n", "build/out.txt": "out\n"}


def _fake_template(root: Path, *, track: bool = True) -> None:
    for group in (TRACKED, UNTRACKED, IGNORED):
        for rel, text in group.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    (root / "deploy").chmod(0o755)
    _git(root, "init", "--quiet")
    if track:
        _git(root, "add", "--", *TRACKED)
    (root / "modified.txt").write_text("new\n", encoding="utf-8")
    (root / "deleted.txt").unlink()


def _files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


@needs_git
def test_copy_template_copies_only_what_git_tracks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], git_env: None) -> None:
    src = tmp_path / "template"
    _fake_template(src)
    monkeypatch.setattr(presets, "ROOT", src)
    dest = tmp_path / "a" / "new"
    presets.copy_template(dest)
    assert _files(dest) == {
        ".gitignore": TRACKED[".gitignore"],
        ".pytemplate/deploy.py": "# entry\n",
        ".pytemplate/runner/x.py": "X = 1\n",
        "src/app/__init__.py": "",
        "sub/build/keep.txt": TRACKED["sub/build/keep.txt"],
        ".github/workflows/ci.yml": "name: ci\n",
        "deploy": "#!/bin/sh\n",
        "modified.txt": "new\n",  # the working-tree content
    }
    if os.name != "nt":
        assert os.access(dest / "deploy", os.X_OK)
    err = capsys.readouterr().err
    assert "not copied (not tracked by git): .env, notes.txt" in err
    assert "x.spec" not in err and "pyc" not in err  # ignored or skipped: not worth a line


@needs_git
def test_copy_template_without_tracked_files_uses_the_skip_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], git_env: None) -> None:
    """A template git does not track (a copy inside another repository, a project never
    committed): every file but the skipped ones, as without git."""
    src = tmp_path / "template"
    _fake_template(src, track=False)
    monkeypatch.setattr(presets, "ROOT", src)
    presets.copy_template(tmp_path / "new")
    copied = set(_files(tmp_path / "new"))
    assert {".env", "notes.txt", "x.spec", "htmlcov/index.html", "sub/build/keep.txt", "modified.txt"} <= copied
    assert not {".pytemplate/template-repo", ".github/workflows/template-e2e.yml", ".claude/settings.json", "build/out.txt"} & copied
    assert not any(p.startswith((".git/", ".venv")) or "__pycache__" in p for p in copied)
    assert "git does not track" in capsys.readouterr().err


def test_copy_template_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for rel, text in {**TRACKED, **UNTRACKED}.items():
        (tmp_path / "t" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "t" / rel).write_text(text, encoding="utf-8")
    monkeypatch.setattr(presets, "ROOT", tmp_path / "t")
    monkeypatch.setattr(presets, "_git_files", lambda *args: None)
    presets.copy_template(tmp_path / "new")
    copied = set(_files(tmp_path / "new"))
    assert copied == {rel for rel in {**TRACKED, **UNTRACKED} if not presets._skipped(rel)}
    assert ".env" in copied  # without git nothing tells a local file from the template's


@pytest.mark.parametrize(
    ("stderr", "warned"),
    [
        ("fatal: detected dubious ownership in repository at '/t'\nTo add an exception...\n", True),
        ("fatal: not a git repository (or any of the parent directories): .git\n", False),
        ("", True),
    ],
)
def test_copy_template_says_when_git_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stderr: str, warned: bool) -> None:
    """Without a list from git the copy takes every file, secrets included: a git failure other
    than "not a repository" (dubious ownership on a shared or copied folder) must say so."""
    src = tmp_path / "t"
    _write(src, {".pytemplate/deploy.py": b"# entry\n", ".env": b"SECRET=1\n"})
    monkeypatch.setattr(presets, "ROOT", src)
    monkeypatch.setattr(shutil, "which", lambda name: "git")
    locales: list[str | None] = []

    def run(argv: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
        locales.append(kw["env"].get("LC_ALL"))
        return subprocess.CompletedProcess(argv, 128, "", stderr)

    monkeypatch.setattr(proc, "run", run)
    presets.copy_template(tmp_path / "new")
    err = capsys.readouterr().err
    assert ("warning: git ls-files failed" in err) is warned, err
    if stderr:
        assert ("dubious ownership" in err) is warned
    assert (tmp_path / "new" / ".env").is_file()  # the fallback: every file
    assert locales == ["C"]  # git's messages in English, whatever the user's locale


@pytest.mark.parametrize(
    ("path", "skipped"),
    [
        (".git/config", True),
        (".git", True),  # a worktree's .git file
        ("sub/.git", True),
        (".venv/x", True),
        (".venv-pypy-wsl/x", True),
        ("src/.venvfoo/x", True),
        (".build/x", True),
        ("dist/x.whl", True),
        ("src/app/__pycache__/m.pyc", True),
        (".mypy_cache/x", True),
        (".flet/x", True),
        (".pytemplate/template-repo", True),
        ("build/x", True),
        (".claude/settings.json", True),
        (".github/workflows/template-e2e.yml", True),
        (".github/workflows/ci.yml", False),
        (".github/template-x.yml", False),
        ("sub/build/x", False),
        ("sub/.claude/x", False),
        ("src/app/core/bench.py", False),
        ("CLAUDE.md", False),
        ("README.md", True),  # the template's page and license: a new project is another program
        ("LICENSE", True),
        ("docs/README.md", False),
        (".pytemplate/README.md", False),  # a project's copy of the manual travels on
        (".pytemplate/LICENSE", False),
    ],
)
def test_skipped(path: str, skipped: bool) -> None:
    assert presets._skipped(path) is skipped


def test_copy_template_refuses_a_folder_with_content(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(DeployError, match="not empty"):
        presets.copy_template(tmp_path)
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "user data"


@template_repo
@needs_git
def test_copy_of_the_real_template_is_exactly_its_tracked_files(tmp_path: Path, git_env: None) -> None:
    tracked = presets._git_files("--cached") or []
    expected = {p for p in tracked if not presets._skipped(p) and (ROOT / p).is_file()}
    dest = tmp_path / "copy"
    presets.copy_template(dest)
    copied = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    assert copied == expected
    for rel in (".pytemplate/template-repo", ".claude", ".github/workflows/template-e2e.yml"):
        assert not (dest / rel).exists(), rel
    assert (dest / "deploy").read_bytes() == (ROOT / "deploy").read_bytes()


# --- new ---------------------------------------------------------------------------------------


def _fake_copy(dest: Path) -> None:
    (dest / ".pytemplate").mkdir(parents=True, exist_ok=True)
    (dest / ".pytemplate" / "deploy.py").write_text("# copied\n", encoding="utf-8")
    (dest / "pyproject.toml").write_text("[project]\n", encoding="utf-8")


def _new_with_a_fake_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str) -> Path:
    """`presets.new` with the real copy of this template and `__init` faked (no uv, no network)."""
    calls: list[list[str]] = []

    def run(argv: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    monkeypatch.setattr(presets, "_git_init", lambda dest: None)
    dest = tmp_path / "demo"
    presets.new(dest, preset, "demo")
    assert [c[5:7] for c in calls if "__init" in c] == [["__init", preset]]
    return dest


@needs_git
@template_repo
@pytest.mark.parametrize("preset", PRESETS)
def test_new_gives_the_project_its_own_readme_and_description(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_env: None, preset: str) -> None:
    # The copy used to start with the template's README ("# myapp: multi-backend uv template") and
    # pyproject description, as if the new project were the template
    dest = _new_with_a_fake_init(tmp_path, monkeypatch, preset)
    readme = (dest / "README.md").read_text(encoding="utf-8")
    description = str(presets.load(preset)["description"])
    assert readme.startswith(f"# demo\n\n{description.rstrip('.')}. Made from [py_template](")
    assert "`.pytemplate/README.md`" in readme and "myapp" not in readme and readme.isascii()
    assert tomllib.loads((dest / "pyproject.toml").read_text(encoding="utf-8"))["project"]["description"] == description
    # The template's page and license travel under .pytemplate/: the manual of the version the
    # project was made from, and the notice the MIT license asks for with the copied runner
    assert (dest / ".pytemplate" / "README.md").read_bytes() == (ROOT / "README.md").read_bytes()
    assert (dest / ".pytemplate" / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert not (dest / "LICENSE").exists()


@needs_git
def test_new_from_a_project_keeps_the_manual_it_carries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_env: None) -> None:
    """A project (no template-repo marker) running `new`: its own README.md and LICENSE stay
    behind, its .pytemplate/README.md and LICENSE (tracked) are copied on like any file."""
    src = tmp_path / "project"
    _fake_template(src)
    (src / ".pytemplate" / "template-repo").unlink()
    (src / ".pytemplate" / "README.md").write_text("# manual\n", encoding="utf-8")
    (src / ".pytemplate" / "LICENSE").write_text("MIT (template)\n", encoding="utf-8")
    (src / "pyproject.toml").write_text('[project]\nname = "old"\ndescription = "my old project"\n', encoding="utf-8")
    _git(src, "add", "--", ".pytemplate/README.md", ".pytemplate/LICENSE", "pyproject.toml")
    monkeypatch.setattr(presets, "ROOT", src)
    monkeypatch.setattr(presets, "TEMPLATE", src / ".pytemplate")
    dest = _new_with_a_fake_init(tmp_path, monkeypatch, "script")
    assert (dest / ".pytemplate" / "README.md").read_text(encoding="utf-8") == "# manual\n"
    assert (dest / ".pytemplate" / "LICENSE").read_text(encoding="utf-8") == "MIT (template)\n"
    assert (dest / "README.md").read_text(encoding="utf-8").startswith("# demo\n")
    assert not (dest / "LICENSE").exists()  # the old project's own license is not the new one's
    assert tomllib.loads((dest / "pyproject.toml").read_text(encoding="utf-8"))["project"]["description"] == presets.load("script")["description"]


@pytest.mark.parametrize(
    "pyproject",
    [
        '[project]\nname = "old"\ndescription = """\nMy tool:\ncounts primes."""\nversion = "1"\n',
        "[project]\nname = \"old\"\ndescription = '''My tool'''  # mine\nversion = \"1\"\n",
        '[project]\r\nname = "old"\r\ndescription = "one line"\r\nversion = "1"\r\n',
        '[project]\nname = "old"\nversion = "1"\n\n[tool.x]\ndescription = "not this one"\n',  # no description yet
    ],
    ids=["basic-multi-line", "literal-multi-line", "crlf", "missing"],
)
def test_make_own_sets_the_description_whatever_its_form(tmp_path: Path, pyproject: str) -> None:
    """A triple-quoted description was half-replaced (invalid TOML: `new` then failed with a
    misleading error), and a missing one was never written although README says new writes it."""
    dest = tmp_path / "copy"
    (dest / ".pytemplate").mkdir(parents=True)
    (dest / "pyproject.toml").write_text(pyproject, encoding="utf-8", newline="")
    presets._make_own(dest, "script", "demo")
    data = tomllib.loads((dest / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"] == {"name": "old", "description": presets.load("script")["description"], "version": "1"}
    assert data.get("tool", {}).get("x", {}).get("description") in (None, "not this one")


def test_make_own_leaves_a_pyproject_without_a_project_table(tmp_path: Path) -> None:
    dest = tmp_path / "copy"
    (dest / ".pytemplate").mkdir(parents=True)
    for text in ('[tool.x]\ndescription = "keep"\n', "[project\n"):  # init names what is wrong
        (dest / "pyproject.toml").write_text(text, encoding="utf-8")
        presets._make_own(dest, "script", "demo")
        assert (dest / "pyproject.toml").read_text(encoding="utf-8") == text


def test_project_readme_without_a_manual_points_at_the_template() -> None:
    readme = presets.project_readme("demo", "script", manual=False)
    assert presets.TEMPLATE_URL in readme.split("Made from", 1)[1] and ".pytemplate/README.md" not in readme


@pytest.mark.parametrize("pre_existing", [False, True], ids=["new-folder", "empty-folder"])
def test_new_removes_the_copy_when_init_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pre_existing: bool) -> None:
    dest = tmp_path / "a" / "b" / "demo"
    if pre_existing:
        dest.mkdir(parents=True)
    calls: list[list[str]] = []

    def run(argv: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in argv])
        assert (dest / ".pytemplate" / "deploy.py").is_file(), "init ran before the copy"
        raise proc.CommandFailed([str(a) for a in argv], 1)

    monkeypatch.setattr(presets, "copy_template", _fake_copy)
    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    with pytest.raises(DeployError, match="half-made project in .* was removed") as e:
        presets.new(dest, "script", "demo")
    assert e.value.code == 1
    assert [c[5:7] for c in calls] == [["__init", "script"]]  # no git init after a failure
    if pre_existing:
        assert dest.is_dir() and not any(dest.iterdir())
    else:
        assert not (tmp_path / "a").exists()  # the parents it created go too


def test_new_cleans_up_after_ctrl_c(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(argv: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr(presets, "copy_template", _fake_copy)
    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    with pytest.raises(KeyboardInterrupt):
        presets.new(tmp_path / "demo", "script", "demo")
    assert not (tmp_path / "demo").exists()


def test_new_says_what_it_could_not_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(argv: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        raise proc.CommandFailed([str(a) for a in argv], 2)

    monkeypatch.setattr(presets, "copy_template", _fake_copy)
    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    monkeypatch.setattr(presets, "_remove", lambda path: False)
    with pytest.raises(DeployError, match="delete .*demo by hand") as e:
        presets.new(tmp_path / "demo", "script", "demo")
    assert e.value.code == 2


def test_new_keeps_the_exit_code_of_a_missing_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """uv not found (exit 3, a missing requirement) stays exit 3 after the cleanup."""

    def no_uv() -> str:
        raise DeployError("uv not found", 3)

    monkeypatch.setattr(presets, "copy_template", _fake_copy)
    monkeypatch.setattr(proc, "find_uv", no_uv)
    monkeypatch.setattr(proc, "run", lambda *a, **k: pytest.fail("ran a command"))
    with pytest.raises(DeployError, match="(?s)uv not found.*was removed") as e:
        presets.new(tmp_path / "demo", "script", "demo")
    assert e.value.code == 3
    assert not (tmp_path / "demo").exists()


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["script", "--bogus"], 2, "unknown argument(s): --bogus"),
        (["script", "extra"], 2, "unknown argument(s): extra"),
        (["script", "--name"], 2, None),  # argparse: expected one argument
        (["nope"], 2, None),  # argparse: invalid choice
        ([], 2, None),  # argparse: the preset is required
    ],
)
def test_the_internal_init_rejects_bad_arguments(dry: Config, monkeypatch: pytest.MonkeyPatch, args: list[str], code: int, message: str | None) -> None:
    """__init is outside cli.COMMANDS (so outside the every-command test of test_cli_core)."""
    monkeypatch.setattr(presets, "plan_init", lambda *a, **k: pytest.fail("planned"))
    monkeypatch.setattr(presets, "init", lambda *a, **k: pytest.fail("ran"))
    if message is None:
        with pytest.raises(SystemExit) as exit_info:
            cmd_mode.cmd_init(dry, args)
        assert exit_info.value.code == code
    else:
        with pytest.raises(DeployError, match=re.escape(message)) as e:
            cmd_mode.cmd_init(dry, args)
        assert e.value.code == code


@pytest.mark.parametrize(
    ("args", "message"),
    [(["x", "--bogus"], "unknown argument(s): --bogus"), (["x", "y"], "unknown argument(s): y"), ([], None), (["x", "--preset", "nope"], None)],
)
def test_new_rejects_bad_arguments_before_anything(dry: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], message: str | None) -> None:
    monkeypatch.chdir(tmp_path)
    if message is None:
        with pytest.raises(SystemExit) as exit_info:
            cmd_mode.cmd_new(dry, args)
        assert exit_info.value.code == 2
    else:
        with pytest.raises(DeployError, match=re.escape(message)) as e:
            cmd_mode.cmd_new(dry, args)
        assert e.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_new_never_touches_a_folder_with_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "run", lambda *a, **k: pytest.fail("ran a command"))
    monkeypatch.setattr(presets, "copy_template", lambda dest: pytest.fail("copied"))
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "keep.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(DeployError, match="not empty"):
        presets.new(tmp_path / "p", "script", "demo")
    (tmp_path / "f").write_text("a file", encoding="utf-8")
    with pytest.raises(DeployError, match="not a folder"):
        presets.new(tmp_path / "f", "script", "demo")
    for name, folder in (("app-", "x"), (None, CJK), (None, "_")):
        with pytest.raises(DeployError, match="--name NAME"):
            presets.new(tmp_path / folder, "script", name)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f", "p"]
    monkeypatch.setattr(presets, "ROOT", tmp_path.resolve())
    with pytest.raises(DeployError, match="inside this template"):
        presets.new(tmp_path / "sub" / "demo", "script", "demo")
    assert not (tmp_path / "sub").exists()
    assert (tmp_path / "p" / "keep.txt").read_text(encoding="utf-8") == "user data"


@needs_git
def test_git_init_makes_a_main_branch(tmp_path: Path, git_env: None) -> None:
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=tmp_path, capture_output=True, text=True, check=False)
    if inside.stdout.strip() == "true":
        pytest.skip("the temporary folder is inside a git work tree")
    dest = tmp_path / "proj"
    dest.mkdir()
    for script in ("deploy", "deploy.ps1"):
        (dest / script).write_text("#!/bin/sh\n", encoding="utf-8")
    presets._git_init(dest)
    assert _git(dest, "symbolic-ref", "HEAD").strip() == "refs/heads/main"
    modes = {line.split()[3]: line.split()[0] for line in _git(dest, "ls-files", "-s").splitlines()}
    assert modes == {"deploy": "100755", "deploy.ps1": "100755"}
    inner = dest / "sub"  # inside a work tree now: no nested repository
    inner.mkdir()
    presets._git_init(inner)
    assert not (inner / ".git").exists()


def test_git_init_falls_back_without_b(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git < 2.28 has no `init -b`: a plain init, then HEAD -> refs/heads/main."""
    calls: list[list[str]] = []

    def run(argv: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv][1:]
        calls.append(args)
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")
        code = 129 if args[:4] == ["init", "--quiet", "-b", "main"] else 0
        return subprocess.CompletedProcess(argv, code, "", "error: unknown switch `b'" if code else "")

    monkeypatch.setattr(shutil, "which", lambda name: "git")
    monkeypatch.setattr(proc, "run", run)
    presets._git_init(tmp_path)
    assert calls[1:] == [
        ["init", "--quiet", "-b", "main"],
        ["init", "--quiet"],
        ["symbolic-ref", "HEAD", "refs/heads/main"],
        ["add", "--chmod=+x", "deploy", "deploy.ps1"],
    ]


@pytest.fixture
def dry(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setattr(proc, "DRY_RUN", True)
    monkeypatch.delenv("PYTEMPLATE_CALLER_CWD", raising=False)
    return config.load(set())


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["x", "--name", "app-"], "--name NAME"),
        (["x", "--name", "app_"], "ending with a letter or digit"),
        (["x", "--name", "1app"], "--name NAME"),
        ([CJK], "--name NAME"),  # nothing of the folder name is usable
        (["x", "--name", "tests"], "would collide with the project's own tests/"),
        (["x", "--name", "Main"], "src/main.py"),
        (["x", "--name", "dist"], "dist/"),
        (["x", "--preset", "raylib", "--name", "assets"], "src/assets/"),
        (["x", "--name", "class"], "Python keyword"),
        (["json"], "standard library"),
        (["pytest"], "also the name of a dependency"),
    ],
)
def test_cmd_new_refuses_before_copying(dry: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], message: str) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DeployError, match=re.escape(message)) as e:
        cmd_mode.cmd_new(dry, args)
    assert e.value.code == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(("folder", "name"), [(CAFE, "cafe"), (NAIVE, "naive-app"), ("my.tool_", "my-tool")])
def test_cmd_new_derives_a_valid_name_from_the_folder(dry: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], folder: str, name: str) -> None:
    monkeypatch.chdir(tmp_path)
    assert cmd_mode.cmd_new(dry, [folder]) == 0
    err = capsys.readouterr().err
    assert re.search(rf"^  name +{re.escape(name)} ", err, re.M), err
    assert "git init -b main" in err
    assert list(tmp_path.iterdir()) == []


def test_cmd_new_dry_run_mentions_the_pins(dry: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    preset = next((p for p in PRESETS if presets.constraints(p)), None)
    if preset is None:
        pytest.skip("no preset has pins")
    monkeypatch.chdir(tmp_path)
    assert cmd_mode.cmd_new(dry, ["x", "--preset", preset]) == 0
    assert f"pins    {len(presets.constraints(preset))} packages" in capsys.readouterr().err


# --- init, in-process with uv faked -----------------------------------------------------------------

FAKE_PYPROJECT = """\
[project]
name = "myapp"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
    "mypy-extensions>=1.1.0",
    "rich>=15.0.0",
]

[dependency-groups]
dev = [
    "pytest>=9.0.0",
]

[tool.uv]
"""


@dataclass
class Fake:
    root: Path
    cfg: Config
    calls: list[list[str]]
    rendered: list[Config]


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    """A throwaway project with the script skeleton named myapp that presets.* works on; uv is
    faked (every call succeeds and is recorded) and render.apply only records its config."""
    root = tmp_path / "proj"
    _write(root, presets.skeleton("script", "myapp"))
    (root / "pyproject.toml").write_text(FAKE_PYPROJECT, encoding="utf-8")
    (root / "uv.lock").write_text(FAKE_LOCK, encoding="utf-8")
    for name, value in (("ROOT", root), ("PYPROJECT", root / "pyproject.toml"), ("LOCK", root / "uv.lock"), ("BUILD", root / ".build")):
        monkeypatch.setattr(presets, name, value)
    monkeypatch.setattr(render, "PYPROJECT", root / "pyproject.toml")  # render.check_pyproject
    calls: list[list[str]] = []
    rendered: list[Config] = []

    def run(argv: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    monkeypatch.setattr(render, "apply", lambda cfg, **_: rendered.append(cfg) or ([], []))
    return Fake(root, _skeleton_config("script", "myapp"), calls, rendered)


def _left_aside(root: Path) -> list[str]:
    return [p.name for p in root.iterdir() if p.name.startswith(".pytemplate-init-")]


@pytest.mark.parametrize("preset", PRESETS)
def test_init_converts_the_project(fake: Fake, preset: str) -> None:
    presets.init(fake.cfg, preset, "Other-Name", force=False)
    expected = presets.skeleton(preset, "Other-Name")
    owned = {p.relative_to(fake.root).as_posix() for d in presets.OWNED_DIRS for p in (fake.root / d).rglob("*") if p.is_file()}
    assert owned == {k for k in expected if k.split("/")[0] in presets.OWNED_DIRS}  # the old skeleton is gone
    for rel, data in expected.items():
        assert (fake.root / rel).read_bytes() == data, rel
    assert not _left_aside(fake.root)
    text = (fake.root / "pyproject.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["project"]["name"] == "Other-Name"
    extra = tomllib.loads(presets.extra_tables(preset, "Other-Name"))
    assert {k: data["tool"][k] for k in extra.get("tool", {})} == extra.get("tool", {})  # the preset tables
    assert "environments" in data["tool"]["uv"]  # the managed block
    assert render.pyproject_expected(fake.rendered[0], text) == text  # nothing outdated afterwards
    plan = fake.rendered[0]
    assert (plan.app.name, plan.app.preset) == ("Other-Name", preset)
    uv = [c[1:] for c in fake.calls]
    assert uv[-1] == ["lock"]
    assert all("--frozen" in c for c in uv if c[0] == "remove")
    assert all("--no-sync" in c for c in uv if c[0] == "add")
    # the pins of the packages uv.lock does not have yet (the ones it has keep their versions)
    pins = {n: v for n, v in presets.constraints(preset).items() if n not in presets.locked_names(lock=presets.LOCK)}
    if pins:
        adds = [c for c in uv if c[0] == "add"]
        assert adds and all("--constraints" in c for c in adds)
        pinned = Path(adds[0][adds[0].index("--constraints") + 1])
        assert pinned.read_text(encoding="utf-8").splitlines() == [f"{n}=={v}" for n, v in sorted(pins.items())]


def test_init_from_a_flet_project_of_another_version_removes_its_pins_first(fake: Fake) -> None:
    """A flet project whose [preset.flet] version = "1.0.0" is applied: `new --preset flet` ran a
    resolving `uv add flet==1.0.1 flet-desktop==1.0.1` while the dev group still pinned
    flet-cli==1.0.0 (which pins flet==1.0.0): no solution, and new failed. Every pin the new
    preset adds in another form now leaves pyproject.toml first (uv remove --frozen)."""
    data = tomllib.loads(presets.skeleton("flet", "myapp")["pytemplate.toml"].decode("utf-8"))
    data["preset"] = {"flet": {"version": "1.0.0"}}
    cfg = _config(data)
    pyproject = FAKE_PYPROJECT.replace('"rich>=15.0.0"', '"flet==1.0.0",\n    "flet-desktop == 1.0.0"').replace('"pytest>=9.0.0",', '"pytest>=9.0.0",\n    "flet-cli==1.0.0",')
    (fake.root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    version = presets.default_options("flet")["version"]
    assert version != "1.0.0"
    plan = presets.plan_init(cfg, "flet", "Other", force=True)
    assert plan.drop == ["flet==1.0.0", "flet-desktop == 1.0.0"] and plan.drop_dev == ["flet-cli==1.0.0"]
    presets.init(cfg, "flet", "Other", force=True)
    uv = [c[1:] for c in fake.calls]
    assert uv[0] == ["remove", "--frozen", "flet", "flet-desktop"]
    assert uv[1] == ["remove", "--frozen", "--dev", "flet-cli"]
    assert uv[2][:2] == ["add", "--no-sync"] and uv[2][-2:] == [f"flet=={version}", f"flet-desktop=={version}"]
    assert uv[3][:3] == ["add", "--no-sync", "--dev"] and uv[3][-1] == f"flet-cli=={version}"
    assert uv[4] == ["lock"]


def test_dropped_keeps_what_the_new_preset_adds_unchanged() -> None:
    added = ["flet==1.0.1", "flet-desktop==1.0.1", "flet-cli==1.0.1"]
    declared = ["mypy-extensions>=1.1.0", "Flet == 1.0.1", "flet_desktop==1.0.0", "rich>=15"]
    # rich: the old preset's, gone; flet_desktop: another pin; Flet == 1.0.1: the same requirement
    assert presets._dropped(["rich>=15"], added[:2], added, declared) == ["flet_desktop==1.0.0", "rich>=15"]
    assert presets._dropped(["rich>=15"], ["rich>=15"], [], declared) == []  # the same preset: nothing
    assert presets._dropped([], [], added, ["pytest>=9", "flet-cli==1.0.0; sys_platform != 'emscripten'"]) == [
        "flet-cli==1.0.0; sys_platform != 'emscripten'"
    ]


def test_init_removes_the_old_preset_first_and_adds_the_new_one(fake: Fake) -> None:
    presets.init(fake.cfg, "raylib", None, force=False)
    uv = [c[1:] for c in fake.calls]
    assert uv[0] == ["remove", "--frozen", "rich"]
    assert uv[1][:2] == ["add", "--no-sync"] and uv[1][-1] == "raylib==6.0.1.0"
    assert uv[2][:3] == ["add", "--no-sync", "--dev"] and uv[2][-1] == "types-cffi"
    assert uv[3] == ["lock"]


@pytest.mark.parametrize("failing", ["remove", "add", "lock"])
def test_init_puts_everything_back_when_uv_fails(fake: Fake, monkeypatch: pytest.MonkeyPatch, failing: str) -> None:
    """Offline, a uv error, no solution: the project stays on the old preset, as it was, so the
    next attempt starts from it (and removes its dependencies)."""
    before = _snapshot(fake.root)

    def run(argv: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        args = [str(a) for a in argv]
        (fake.root / "uv.lock").write_text("written by uv", encoding="utf-8")  # uv got that far
        if args[1] == failing:
            raise proc.CommandFailed(args, 1)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(proc, "run", run)
    with pytest.raises(proc.CommandFailed):
        presets.init(fake.cfg, "flet", None, force=False)
    assert _snapshot(fake.root) == before
    assert not fake.rendered


def test_init_puts_everything_back_after_ctrl_c(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _snapshot(fake.root)

    def run(argv: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr(proc, "run", run)
    with pytest.raises(KeyboardInterrupt):
        presets.init(fake.cfg, "raylib", None, force=False)
    assert _snapshot(fake.root) == before


def test_init_puts_everything_back_when_a_folder_cannot_be_moved(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: a file in tests/ held open by another program (or a shell in it) makes the
    rename fail before anything under src/, tests/ or typings/ changed."""
    before = _snapshot(fake.root)
    real = Path.rename

    def rename(self: Path, target: Any) -> Path:
        if self == fake.root / "tests":
            raise PermissionError(13, "Permission denied")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(DeployError, match="cannot move tests/ aside .*close the programs"):
        presets.init(fake.cfg, "raylib", None, force=False)
    assert _snapshot(fake.root) == before
    assert not _left_aside(fake.root)


def test_init_puts_everything_back_when_writing_fails(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _snapshot(fake.root)
    real = Path.write_bytes

    def write_bytes(self: Path, data: Any) -> int:
        if self.name == "world.py":
            raise OSError(28, "No space left on device")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    with pytest.raises(DeployError, match="init could not write: No space left") as e:  # not "internal runner error"
        presets.init(fake.cfg, "raylib", None, force=False)
    assert e.value.code == 2
    assert _snapshot(fake.root) == before
    assert not _left_aside(fake.root)


def test_init_reports_what_it_could_not_put_back(fake: Fake, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    real = Path.write_bytes

    def write_bytes(self: Path, data: Any) -> int:
        if self.name == "world.py":
            raise OSError(28, "No space left on device")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    monkeypatch.setattr(presets, "_remove", lambda path: False)
    with pytest.raises(DeployError, match="No space left"):
        presets.init(fake.cfg, "raylib", None, force=False)
    assert "could not put back: src/ (partly written)" in capsys.readouterr().err


def test_init_with_a_name_the_file_system_refuses_puts_everything_back(fake: Fake) -> None:
    """A huge name passes the name rules, but no file system takes a 300-character folder name:
    a clear error naming the file, and the project as it was (never an internal-error traceback)."""
    name = "a" * 300
    before = _snapshot(fake.root)
    with pytest.raises(DeployError, match=r"init could not write .*src[/\\]a{300}\b") as e:
        presets.init(fake.cfg, "script", name, force=True)
    assert e.value.code == 2
    assert _snapshot(fake.root) == before
    assert not _left_aside(fake.root)


def test_init_refuses_a_changed_tree_without_force(fake: Fake) -> None:
    (fake.root / "src" / "myapp" / "app.py").write_text("# my code\n", encoding="utf-8")
    before = _snapshot(fake.root)
    with pytest.raises(DeployError, match="__init raylib --force"):
        presets.init(fake.cfg, "raylib", None, force=False)
    assert _snapshot(fake.root) == before and fake.calls == []


@pytest.mark.parametrize(
    "extra",
    ['\n[tool.flet]\norg = "mine"\n', f'\n{presets.EXTRA_BEGIN}\n[tool.flet]\norg = "mine"\n'],
    ids=["outside-the-markers", "end-marker-missing"],
)
def test_init_refuses_a_preset_table_outside_the_markers(fake: Fake, extra: str) -> None:
    pyproject = fake.root / "pyproject.toml"
    pyproject.write_text(FAKE_PYPROJECT + extra, encoding="utf-8")
    before = _snapshot(fake.root)
    for attempt in (lambda: presets.init(fake.cfg, "flet", None, force=True), lambda: presets.plan_init(fake.cfg, "flet", None, force=True)):
        with pytest.raises(DeployError, match="pytemplate-preset") as e:
            attempt()
        assert e.value.code == 2
    assert _snapshot(fake.root) == before and fake.calls == []


def test_init_twice_keeps_one_preset_block(fake: Fake) -> None:
    presets.init(fake.cfg, "flet", None, force=False)
    cfg = _skeleton_config("flet", "myapp")
    presets.init(cfg, "flet", "B", force=True)
    text = (fake.root / "pyproject.toml").read_text(encoding="utf-8")
    assert text.count(presets.EXTRA_BEGIN) == 1 and text.count(presets.EXTRA_END) == 1
    assert tomllib.loads(text)["tool"]["flet"]["product"] == "B"


def test_init_keeps_the_preset_tables_without_the_managed_markers(fake: Fake) -> None:
    """'# >>> pytemplate' is a prefix of '# >>> pytemplate-preset': with the managed markers
    gone, the preset block must not be taken for the managed block (and replaced by it)."""
    pyproject = fake.root / "pyproject.toml"
    old_block = presets._set_extra_tables("", presets.extra_tables("flet", "Old"))
    pyproject.write_text(FAKE_PYPROJECT + old_block, encoding="utf-8")
    text = presets.plan_init(fake.cfg, "flet", "New", force=True).pyproject
    data = tomllib.loads(text)
    assert data["tool"]["flet"]["product"] == "New" and data["tool"]["flet"]["app"]["module"] == "main"
    assert "environments" in data["tool"]["uv"] and "environments" not in data["tool"]["flet"]["app"]
    assert text.count(presets.EXTRA_BEGIN) == 1


def test_init_reads_a_pyproject_with_a_bom_and_crlf(fake: Fake) -> None:
    pyproject = fake.root / "pyproject.toml"
    pyproject.write_bytes(b"\xef\xbb\xbf" + FAKE_PYPROJECT.replace("\n", "\r\n").encode("utf-8"))
    assert presets._declared(None) == {"mypy-extensions", "rich"}
    assert presets._declared("dev") == {"pytest"}
    presets.init(fake.cfg, "flet", None, force=False)
    data = pyproject.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r\n" not in data
    assert tomllib.loads(data.decode("utf-8"))["tool"]["flet"]["product"] == "myapp"


@pytest.mark.parametrize(
    ("pyproject", "message"),
    [
        # uv would reject the duplicate key: render's own message says which key and why
        (FAKE_PYPROJECT + 'python-preference = "system"\n', "repeat a managed key"),
        (FAKE_PYPROJECT + "# >>> pytemplate: generated\n", "block managed by pytemplate is broken"),
        (FAKE_PYPROJECT.replace("[project]", "[tool.other]"), r"is the \[project\] table missing"),
        (FAKE_PYPROJECT.replace('name = "myapp"\n', ""), r"\[project\] table has no name"),
    ],
    ids=["managed-key-repeated", "managed-marker-damaged", "no-project-table", "no-project-name"],
)
def test_init_checks_the_pyproject_rewrite_before_writing(fake: Fake, pyproject: str, message: str) -> None:
    """render.check_pyproject and pyproject_after_init run in plan_init: a pyproject.toml that
    cannot be rewritten stops init (and its dry run) before any file or uv.lock changes."""
    (fake.root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    before = _snapshot(fake.root)
    for attempt in (lambda: presets.plan_init(fake.cfg, "raylib", None, force=False), lambda: presets.init(fake.cfg, "raylib", None, force=False)):
        with pytest.raises(DeployError, match=message) as e:
            attempt()
        assert e.value.code == 2
    assert _snapshot(fake.root) == before and fake.calls == [] and not fake.rendered


def test_a_preset_with_broken_pyproject_tables_is_named(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(presets, "extra_tables", lambda preset, name: "[tool.flet\norg = 1\n")
    with pytest.raises(DeployError, match=r"presets/flet/preset\.toml: the pyproject tables are not valid TOML") as e:
        presets.pyproject_after_init(_skeleton_config("flet", "myapp"), "flet", "myapp")
    assert e.value.code == 2


SAME = "<unchanged>"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('[project]\nname = "old"\nversion = "1"\n', '[project]\nname = "new"\nversion = "1"\n'),
        ("[project]\nname = 'old'  # the app\n", '[project]\nname = "new"  # the app\n'),
        ('[project]\r\nname = "old"\r\nversion = "1"\r\n', '[project]\r\nname = "new"\r\nversion = "1"\r\n'),  # rename keeps CRLF
        ('[project]\nname = "o\\"ld"\n', '[project]\nname = "new"\n'),
        ('[[tool.uv.index]]\nname = "pypi"\n\n[project]\n"name" = "old"\n', '[[tool.uv.index]]\nname = "pypi"\n\n[project]\n"name" = "new"\n'),
        ('[ project ]  # the app\nversion = "1"\n  name = "old"\n[tool.x]\nname = "keep"\n', '[ project ]  # the app\nversion = "1"\n  name = "new"\n[tool.x]\nname = "keep"\n'),
        ('[project]\nversion = "1"\n\n[tool.x]\nname = "keep"\n', SAME),  # [project] has no name
        ('name = "top"\n\n[tool.x]\nname = "keep"\n', SAME),  # no [project] at all
        ('[project]\nnamespace = "x"\nname = "old"\n', '[project]\nnamespace = "x"\nname = "new"\n'),
        # a multi-line string is never half-replaced (it was `name = "new""old"""`): callers check
        ('[project]\nname = """old"""\n', SAME),
        ("[project]\nname = '''old'''\n", SAME),
        ('[project]\nname = ""\n', '[project]\nname = "new"\n'),
    ],
)
def test_set_project_name_only_touches_the_project_table(text: str, expected: str) -> None:
    assert presets._set_project_name(text, "new") == (text if expected == SAME else expected)


@pytest.mark.parametrize(
    "text",
    [
        f'x = 1\n{presets.EXTRA_BEGIN}\n[tool.a]\ny = 2\n',  # the closing marker is gone
        f'x = 1\n[tool.a]\ny = 2\n{presets.EXTRA_END}\n',  # the opening marker is gone
        f"{presets.EXTRA_END}\n{presets.EXTRA_BEGIN}\n",  # swapped
        f"{presets.EXTRA_BEGIN}\n{presets.EXTRA_END}\n{presets.EXTRA_BEGIN}\n{presets.EXTRA_END}\n",  # twice
    ],
)
def test_damaged_preset_markers_are_refused(text: str) -> None:
    """A lone marker used to append a second block (duplicate tables) or leave the old one."""
    for extra in ("", "[tool.b]\nz = 3\n"):
        with pytest.raises(DeployError, match="pytemplate-preset") as e:
            presets._set_extra_tables(text, extra)
        assert e.value.code == 2


def test_set_extra_tables_replaces_only_the_block() -> None:
    """Lines split on \\n only: a U+2028 or U+0085 inside a TOML string is not a line break
    (str.splitlines would cut the string in two and rejoin it with \\n: invalid TOML)."""
    head = '[project]\nname = "a"\ndescription = "one\N{LINE SEPARATOR}two\x85three"\n'
    block = f"\n{presets.EXTRA_BEGIN}\n[tool.a]\nx = 1\n{presets.EXTRA_END}\n"
    once = presets._set_extra_tables(head, "[tool.a]\nx = 1\n")
    assert once == head + block
    assert presets._set_extra_tables(once, "[tool.a]\nx = 1\n") == once  # idempotent
    assert presets._set_extra_tables(once, "") == head
    assert presets._set_extra_tables(once + "\n\n", "[tool.b]\ny = 2\n") == head + block.replace("[tool.a]\nx = 1", "[tool.b]\ny = 2")
    assert tomllib.loads(once)["project"]["description"] == "one\N{LINE SEPARATOR}two\x85three"
    assert presets._set_extra_tables("", "[tool.a]\nx = 1\n") == block


def test_a_broken_pyproject_is_a_clear_error(fake: Fake) -> None:
    (fake.root / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    with pytest.raises(DeployError, match="pyproject.toml is not valid TOML") as e:
        presets.check_name_free(fake.cfg, "script", "demo")
    assert e.value.code == 2


@pytest.mark.parametrize("name", ["app-", "app_", "1app", "my app", CAFE])
def test_init_refuses_names_uv_refuses(fake: Fake, name: str) -> None:
    before = _snapshot(fake.root)
    with pytest.raises(DeployError, match="not a valid app name"):
        presets.init(fake.cfg, "raylib", name, force=True)
    assert _snapshot(fake.root) == before and fake.calls == []


def test_init_pins_only_what_the_lock_does_not_have(fake: Fake) -> None:
    """A package the project already locks keeps its version (a project made from another
    project); the preset's pins only decide the packages new to uv.lock."""
    pins = {n: v for n, v in presets.constraints("raylib").items() if n not in presets.locked_names()}
    if not pins:
        pytest.skip("the raylib preset has no pins beyond this uv.lock")
    first = sorted(pins)[0]
    lock = fake.root / "uv.lock"
    lock.write_text(lock.read_text(encoding="utf-8") + f'\n[[package]]\nname = "{first}"\nversion = "0.0.1"\nsource = {{ registry = "https://pypi.org/simple" }}\n', encoding="utf-8")
    plan = presets.plan_init(fake.cfg, "raylib", None, force=False)
    assert plan.pins == {n: v for n, v in pins.items() if n != first}
    assert not set(plan.pins) & presets.locked_names()  # rich, pytest... (FAKE_LOCK) keep theirs too


# A raylib project's pyproject.toml and uv.lock: none of the script preset's tree (rich,
# markdown-it-py, mdurl), which the flet preset needs too (flet-cli -> rich)
RAYLIB_PYPROJECT = FAKE_PYPROJECT.replace('"rich>=15.0.0"', '"raylib==6.0.1.0"').replace('"pytest>=9.0.0",', '"pytest>=9.0.0",\n    "types-cffi",')
RAYLIB_LOCK = """\
version = 1
requires-python = ">=3.14"

[[package]]
name = "myapp"
version = "0.1.0"
source = { virtual = "." }
dependencies = [{ name = "raylib" }]

[package.dev-dependencies]
dev = [{ name = "pytest" }, { name = "types-cffi" }]

[[package]]
name = "raylib"
version = "6.0.1.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "cffi" }]

[[package]]
name = "cffi"
version = "2.1.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "pycparser" }]

[[package]]
name = "pycparser"
version = "3.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "types-cffi"
version = "2.1.0.20260827"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "pytest"
version = "9.1.1"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "pygments" }]

[[package]]
name = "pygments"
version = "2.21.0"
source = { registry = "https://pypi.org/simple" }
"""


def _as_raylib_project(fake: Fake) -> Config:
    (fake.root / "pyproject.toml").write_text(RAYLIB_PYPROJECT, encoding="utf-8")
    (fake.root / "uv.lock").write_text(RAYLIB_LOCK, encoding="utf-8")
    return _skeleton_config("raylib", "myapp")


@pytest.mark.parametrize(("preset", "name"), [("script", "mdurl"), ("script", "Markdown-It-Py"), ("script", "rich"), ("flet", "mdurl"), ("flet", "rich")])
def test_new_from_a_project_without_the_presets_tree_refuses_its_names(fake: Fake, preset: str, name: str) -> None:
    """From a raylib project (its uv.lock has no rich), `new --preset script --name mdurl` was
    accepted: uv then resolved markdown-it-py's mdurl to the project itself, and the library was
    missing from uv.lock, .venv and every build. The pins name the preset's whole tested tree."""
    cfg = _as_raylib_project(fake)
    with pytest.raises(DeployError, match="also the name of a dependency") as e:
        presets.check_name_free(cfg, preset, name)
    assert e.value.code == 2
    presets.check_name_free(cfg, preset, "demo")


def test_new_from_another_project_gets_the_tested_versions(fake: Fake) -> None:
    """`new --preset script` from a raylib project resolved rich, markdown-it-py and mdurl to
    the newest release of the day: the pins must reach every package the source lock lacks."""
    cfg = _as_raylib_project(fake)
    plan = presets.plan_init(cfg, "script", "demo", force=True)
    tested = presets.constraints("script")
    assert {"rich", "markdown-it-py", "mdurl"} <= set(plan.pins)
    assert plan.pins == {n: v for n, v in tested.items() if n not in presets.locked_names()}
    assert "pytest" not in plan.pins  # the source project's own version stays
    presets.init(cfg, "script", "demo", force=True)
    adds = [c for c in fake.calls if c[1] == "add"]
    assert adds and all("--constraints" in c for c in adds)


def _self_dependent_lock(name: str) -> str:
    """The uv.lock uv (0.12) writes when a dependency of a dependency has the project's name."""
    return (
        f'version = 1\n\n[[package]]\nname = "markdown-it-py"\nversion = "4.2.0"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
        f'dependencies = [{{ name = "{name}" }}]\n\n[[package]]\nname = "{name}"\nversion = "0.1.0"\nsource = {{ virtual = "." }}\n'
        'dependencies = [{ name = "markdown-it-py" }]\n'
    )


def test_self_dependents(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(_self_dependent_lock("mdurl"), encoding="utf-8")
    assert presets._self_dependents("mdurl", lock) == ["markdown-it-py"]
    assert presets._self_dependents("MDURL", lock) == ["markdown-it-py"]
    assert presets._self_dependents("other", lock) == []  # not the project's name
    lock.write_text(FAKE_LOCK, encoding="utf-8")
    assert presets._self_dependents("myapp", lock) == []
    assert presets._self_dependents("mdurl", lock) == []  # the real library, not the project


def test_init_refuses_a_lock_that_resolves_a_dependency_to_the_project(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    """The last guard, for what the name check cannot know (a fork left out of the pins, a
    source lock with other versions): uv resolved a dependency to the project and said nothing."""
    before = _snapshot(fake.root)

    def run(argv: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        if str(argv[1]) == "lock":
            (fake.root / "uv.lock").write_text(_self_dependent_lock("Demo"), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(presets, "check_name_free", lambda *a: None)
    with pytest.raises(DeployError, match=r"(?s)'Demo' is also the name of a package .*markdown-it-py depends on demo.*--name NAME") as e:
        presets.init(fake.cfg, "script", "Demo", force=True)
    assert e.value.code == 2
    assert _snapshot(fake.root) == before and not fake.rendered and not _left_aside(fake.root)


def test_plan_init_writes_nothing_and_the_dry_run_prints_it(fake: Fake, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cmd_mode, "ROOT", fake.root)
    before = _snapshot(fake.root)
    cmd_mode._plan_init(fake.cfg, "flet", "Other", force=True)
    err = capsys.readouterr().err
    assert "init flet (" in err and "as 'Other'" in err
    assert "remove rich>=15.0.0; add flet==1.0.1, flet-desktop==1.0.1" in err
    assert "+ src/other/ui/app.py" in err and "- src/myapp/core/bench.py" in err
    if presets.constraints("flet"):
        assert "versions:" in err and "constraints.txt" in err
    assert _snapshot(fake.root) == before and fake.calls == [] and not fake.rendered


def test_undo_rollback_reports_a_folder_it_cannot_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(presets, "ROOT", tmp_path)
    (tmp_path / "aside" / "src").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    undo = presets._Undo()
    undo.aside, undo.existed, undo.moved = tmp_path / "aside", {"src"}, ["src"]
    real = Path.rename

    def rename(self: Path, target: Any) -> Path:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "rename", rename)
    left = undo.rollback()
    monkeypatch.setattr(Path, "rename", real)
    assert left == ["src/ (the original is in " + presets.rel(tmp_path / "aside" / "src") + ")"]
    assert (tmp_path / "aside" / "src").is_dir()  # the original is never deleted


def test_remove_deletes_read_only_entries(tmp_path: Path) -> None:
    tree = tmp_path / "t"
    (tree / "sub").mkdir(parents=True)
    for path in (tree / "a.txt", tree / "sub" / "b.txt"):
        path.write_text("x", encoding="utf-8")
        path.chmod(0o444)
    assert presets._remove(tree) and not tree.exists()
    single = tmp_path / "ro.txt"
    single.write_text("x", encoding="utf-8")
    single.chmod(0o444)
    assert presets._remove(single) and not single.exists()
    assert presets._remove(tmp_path / "missing")


# --- the raylib stub generator ----------------------------------------------------------------------

FAKE_RAYLIB = '''\
"""A fake raylib: GLFWcursor is opaque and reading its .fields aborts, like cffi 2.x builds
that keep their C asserts (the Linux wheels)."""
import os

__version__ = "0.0-fake"


class _T:
    def __init__(self, kind, cname, fields=None, opaque=False):
        self.kind, self.cname, self._fields, self.opaque = kind, cname, fields, opaque

    @property
    def fields(self):
        if self.opaque:
            os.abort()
        return self._fields


class _F:
    def __init__(self, t):
        self.type = t


class _Fn:
    kind = "function"

    def __init__(self, result, args):
        self.result, self.args = result, args


class _FFI:
    class error(Exception):
        pass

    TYPES = {
        "Color": _T("struct", "Color", [("r", _F(_T("primitive", "unsigned char")))]),
        "GLFWcursor": _T("struct", "GLFWcursor", opaque=True),
    }

    def typeof(self, x):
        if isinstance(x, str):
            if x not in self.TYPES:
                raise TypeError(x)
            return self.TYPES[x]
        return x

    def sizeof(self, t):
        if t.opaque:
            raise self.error("don't know the size of ctype")
        return 4


ffi = _FFI()


class rl:
    DrawText = _Fn(_T("pointer", "char *"), [_T("pointer", "char *"), _T("primitive", "int")])
'''

FAKE_STUB = """\
import _cffi_backend
from warnings import deprecated

class Color:
    r: bytes
class GLFWcursor:
    pass
def DrawText(posx: bytes, x: bytes) -> bytes:
    ...
"""


def test_raylib_stubs_skips_opaque_structs(tmp_path: Path) -> None:
    """cffi 2.x aborts the process (C assert, exit 134) on `.fields` of an opaque struct: the
    generator must test the size first. Also: a parameter `x` must not rewrite `posx`."""
    fake = tmp_path / "fake" / "raylib"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text(FAKE_RAYLIB, encoding="utf-8")
    (fake / "__init__.pyi").write_text(FAKE_STUB, encoding="utf-8")
    out = tmp_path / "out" / "raylib.pyi"
    tool = presets.PRESETS / "raylib" / "tools" / "raylib_stubs.py"
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "fake")}
    r = subprocess.run([sys.executable, str(tool), str(out)], env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "corrected 1 return types, 1 fields and 1 parameters" in r.stdout
    text = out.read_text(encoding="utf-8")
    assert "class Color:\n    r: int\n" in text
    assert "class GLFWcursor:\n    pass\n" in text
    assert "def DrawText(posx: bytes, x: int) -> CData:" in text
    assert "from typing_extensions import deprecated" in text and "from _cffi_backend import _CDataBase as CData" in text


def test_raylib_stubs_regenerates_the_committed_stub(tmp_path: Path, network: None) -> None:
    """The real generator on the pinned raylib and cffi gives the stub the preset ships."""
    pins = presets.constraints("raylib")
    if "raylib" not in pins or "cffi" not in pins:
        pytest.skip("the raylib preset pins no raylib/cffi")
    uv = shutil.which("uv")
    assert uv
    out = tmp_path / "raylib.pyi"
    tool = presets.PRESETS / "raylib" / "tools" / "raylib_stubs.py"
    argv = [uv, "run", "--quiet", "--no-project", "--python", "3.14", "--with", f"raylib=={pins['raylib']}", "--with", f"cffi=={pins['cffi']}", "python", str(tool), str(out)]
    r = subprocess.run(argv, cwd=tmp_path, env=_child_env(tmp_path), capture_output=True, text=True, timeout=600, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    committed = presets.PRESETS / "raylib" / "files" / "typings" / "raylib" / "__init__.pyi"
    assert out.read_bytes() == committed.read_bytes().replace(b"\r\n", b"\n")


# --- real runs (uv + network) ---------------------------------------------------------------------


@pytest.mark.parametrize("preset", PRESETS)
def test_new_creates_a_working_project(preset: str, tmp_path: Path, network: None) -> None:
    env = _child_env(tmp_path)
    name = f"pt-{preset}"
    dest = tmp_path / "new" / name  # the parent does not exist yet
    r = _deploy(ROOT, "new", str(dest), "--preset", preset, cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr[-4000:]

    for rel, data in presets.skeleton(preset, name).items():  # the skeleton, byte for byte
        assert (dest / rel).read_bytes() == data, rel
    for rel in (".pytemplate/template-repo", ".claude", ".venv"):
        assert not (dest / rel).exists(), rel
    assert not list((dest / ".github" / "workflows").glob("template-*"))

    project = tomllib.loads((dest / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == name
    assert "constraint-dependencies" not in project["tool"]["uv"]  # the pins were a one-off
    locked = {presets._norm_name(e["name"]): e["version"] for e in presets._lock_entries(dest / "uv.lock") if not presets._is_project(e)}
    pins = presets.constraints(preset)
    # what this project locks keeps its version (init pins only the packages it lacks)
    source = {presets._norm_name(e["name"]): e["version"] for e in presets._lock_entries() if not presets._is_project(e)}
    expected = {n: source.get(n, v) for n, v in pins.items()}
    assert {n: locked.get(n) for n in pins} == expected, "the new project does not lock the tested versions"
    if TEMPLATE_REPO:
        fresh = sorted(set(locked) - presets.locked_names() - set(pins))
        assert not fresh, (
            f"{preset}: {fresh} were resolved on the day, not pinned: regenerate constraints.txt (CLAUDE.md 11):\n"
            + presets.constraints_text(preset, dest / "uv.lock")
        )

    check = subprocess.run([shutil.which("uv") or "uv", "lock", "--check"], cwd=dest, env=env, capture_output=True, text=True, timeout=300, check=False)
    assert check.returncode == 0, check.stderr
    render_check = _deploy(dest, "render", "--check", cwd=dest, env=env)
    assert render_check.returncode == 0, render_check.stderr

    if shutil.which("git"):
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
        if inside.stdout.strip() != "true":
            assert subprocess.run(["git", "symbolic-ref", "HEAD"], cwd=dest, env=env, capture_output=True, text=True, check=True).stdout.strip() == "refs/heads/main"
            staged = subprocess.run(["git", "ls-files", "-s", "deploy", "deploy.ps1"], cwd=dest, env=env, capture_output=True, text=True, check=True).stdout
            assert [line.split()[0] for line in staged.splitlines()] == ["100755", "100755"]


def test_new_into_a_folder_with_a_space_and_an_accent(tmp_path: Path, network: None) -> None:
    """The folder name gives the app name (accent dropped: cafe); the paths, with a space and a
    non-ASCII character, reach uv, git and the copy's own runner intact."""
    env = _child_env(tmp_path)
    parent = tmp_path / "my projects"
    parent.mkdir()
    r = _deploy(ROOT, "new", CAFE, cwd=parent, env={**env, "PYTEMPLATE_CALLER_CWD": str(parent)})
    assert r.returncode == 0, r.stderr[-4000:]
    dest = parent / CAFE
    assert (dest / "src" / "cafe" / "__init__.py").is_file()
    assert tomllib.loads((dest / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"] == "cafe"
    assert tomllib.loads((dest / "pytemplate.toml").read_text(encoding="utf-8"))["app"]["name"] == "cafe"
    check = _deploy(dest, "render", "--check", cwd=dest, env=env)
    assert check.returncode == 0, check.stderr


def test_init_pins_steer_the_resolution(tmp_path: Path, network: None, git_env: None) -> None:
    """An older pinned version wins over the newest one: the pins are really used."""
    pins = presets.constraints("raylib")
    if pins.get("pycparser") != "3.0":
        pytest.skip("this check expects the raylib preset to pin pycparser 3.0")
    env = _child_env(tmp_path)
    copy_root = tmp_path / "copy"
    presets.copy_template(copy_root)
    if config.load(set(cli.COMMANDS)).app.preset == "raylib":  # a raylib project: from another preset first
        r = _deploy(copy_root, "__init", "script", "--force", cwd=copy_root, env=env)
        assert r.returncode == 0, r.stderr[-4000:]
    constraints = copy_root / ".pytemplate" / "presets" / "raylib" / "constraints.txt"
    constraints.write_text(constraints.read_text(encoding="utf-8").replace("pycparser==3.0", "pycparser==2.22"), encoding="utf-8")
    r = _deploy(copy_root, "__init", "raylib", cwd=copy_root, env=env)
    assert r.returncode == 0, r.stderr[-4000:]
    locked = {e["name"]: e["version"] for e in presets._lock_entries(copy_root / "uv.lock")}
    assert locked["pycparser"] == "2.22" and locked["raylib"] == pins["raylib"]
    assert "pycparser" not in (copy_root / "pyproject.toml").read_text(encoding="utf-8")


def test_init_round_trip_through_every_preset_is_byte_identical(tmp_path: Path, network: None, git_env: None) -> None:
    """current -> every other preset -> current gives back the same bytes: every init removes
    the previous preset's dependencies, tables and files (and the template root is exactly what
    `__init script --name myapp --force` writes)."""
    cfg = config.load(set(cli.COMMANDS))
    if not presets.pristine(cfg):
        pytest.skip("src/, tests/ or typings/ are not the pristine skeleton of the current preset")
    env = _child_env(tmp_path)
    copy_root = tmp_path / "copy"
    presets.copy_template(copy_root)
    before = _snapshot(copy_root, lf=True)
    for preset in [*(p for p in PRESETS if p != cfg.app.preset), cfg.app.preset]:
        r = _deploy(copy_root, "__init", preset, "--force", cwd=copy_root, env=env)
        assert r.returncode == 0, f"init {preset}:\n{r.stderr[-4000:]}"
    after = _snapshot(copy_root, lf=True)
    assert sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k)) == []


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not found")
def test_init_that_fails_in_uv_changes_nothing(tmp_path: Path, git_env: None) -> None:
    """No network and an empty uv cache: `__init flet` (raylib in a flet project) fails in `uv add`
    and the project is exactly as before (a later attempt starts from the same preset again)."""
    env = _child_env(tmp_path)
    env.update(UV_OFFLINE="1", UV_CACHE_DIR=str(tmp_path / "empty-cache"))
    copy_root = tmp_path / "copy"
    presets.copy_template(copy_root)
    before = _snapshot(copy_root)
    target = "raylib" if config.load(set(cli.COMMANDS)).app.preset == "flet" else "flet"
    r = _deploy(copy_root, "__init", target, cwd=copy_root, env=env)
    assert r.returncode != 0
    assert "init failed: every file is back as it was" in r.stderr
    assert _snapshot(copy_root) == before
