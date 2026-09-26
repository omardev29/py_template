"""pytemplate.toml: the loader, every validate rule, set_value/update_file and real `mode` runs.

Run them with `./deploy selftest` (or `./deploy selftest -q .pytemplate/tests/test_config_rules.py`).
Each rule has a positive and a negative case. The `mode` runs work in a throwaway copy of this
project. The file stays ASCII: accented letters are built with chr() (language guard).
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tomllib
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_DIR))

from runner import cli, cmd_build, cmd_mode, config, presets, proc, render  # noqa: E402
from runner.config import Config, set_value, toml_value  # noqa: E402
from runner.editors import nvim, vscode  # noqa: E402
from runner.ui import DeployError  # noqa: E402

ROOT = TEMPLATE_DIR.parent
COMMANDS = set(cli.COMMANDS)
E_ACUTE = chr(0xE9)
TEMPLATE_REPO = (TEMPLATE_DIR / "template-repo").is_file()


def build(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    return cfg


def make(data: dict[str, Any], commands: set[str] | None = COMMANDS) -> Config:
    cfg = build(data)
    config.validate(cfg, commands)
    return cfg


def fails(data: dict[str, Any], message: str, commands: set[str] | None = COMMANDS) -> DeployError:
    """The config error of `data`: a DeployError (never another exception), code 2, `message` in it."""
    with pytest.raises(DeployError) as info:
        make(data, commands)
    assert message in str(info.value), str(info.value)
    assert info.value.code == 2
    return info.value


def preset_text(name: str, app: str = "myapp") -> str:
    text = (config.PRESETS / name / "files" / "pytemplate.toml").read_text(encoding="utf-8")
    return text.replace("{{name}}", app).replace("{{pkg}}", app)


def shipped_configs() -> dict[str, str]:
    """The root pytemplate.toml and every preset's (rendered with the name myapp), LF."""
    out = {"root": (ROOT / "pytemplate.toml").read_text(encoding="utf-8")}
    for p in sorted(d.name for d in config.PRESETS.iterdir() if (d / "files" / "pytemplate.toml").is_file()):
        out[p] = preset_text(p)
    return {name: text.replace("\r\n", "\n") for name, text in out.items()}


# --- the schema: every field, every wrong type ----------------------------------------------------


def _schema(cls: type[Any] = Config, prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    """(path, type) of every non-table field of the schema, tasks.t.* included."""
    for f in dataclasses.fields(cls):
        hint = typing.get_type_hints(cls)[f.name]
        path = (*prefix, f.name)
        if isinstance(hint, type) and dataclasses.is_dataclass(hint):
            yield from _schema(hint, path)
        else:
            yield path, hint
    if cls is Config:
        yield from _schema(config.TaskConfig, ("tasks", "t"))


def _tables(cls: type[Any] = Config, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    yield prefix
    for f in dataclasses.fields(cls):
        hint = typing.get_type_hints(cls)[f.name]
        if isinstance(hint, type) and dataclasses.is_dataclass(hint):
            yield from _tables(hint, (*prefix, f.name))
    if cls is Config:
        yield ("tasks", "t")


def _wrong(hint: Any) -> Any:
    origin = typing.get_origin(hint)
    if origin is list:
        return "x"
    if origin is dict:
        return ["x"]
    return {bool: "yes", int: "1", str: 1}[hint]


def _nest(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    data: Any = value
    for part in reversed(path):
        data = {part: data}
    if path[:1] == ("tasks",) and len(path) > 2 and path[2] != "cmd":
        data["tasks"]["t"]["cmd"] = ["x"]  # a task needs cmd or deps
    assert isinstance(data, dict)
    return data


SCHEMA = list(_schema())


@pytest.mark.parametrize(("path", "hint"), SCHEMA, ids=[".".join(p) for p, _ in SCHEMA])
def test_every_field_rejects_a_wrong_type(path: tuple[str, ...], hint: Any) -> None:
    fails(_nest(path, _wrong(hint)), f"'{'.'.join(path)}' must be")


ITEMS = [
    (p, h)
    for p, h in SCHEMA
    if typing.get_origin(h) in (list, dict)
    and typing.get_args(h)[-1] is not Any
    and not dataclasses.is_dataclass(typing.get_args(h)[-1])  # tasks: covered by the task fields
]


@pytest.mark.parametrize(("path", "hint"), ITEMS, ids=[".".join(p) for p, _ in ITEMS])
def test_every_list_and_table_checks_its_items(path: tuple[str, ...], hint: Any) -> None:
    item = typing.get_args(hint)[-1]
    if typing.get_origin(hint) is list:
        fails(_nest(path, [_wrong(item)]), f"'{'.'.join(path)}[0]' must be")
    else:
        fails(_nest(path, {"k": _wrong(item)}), f"'{'.'.join(path)}.k' must be")


TABLES = list(_tables())


@pytest.mark.parametrize("path", TABLES, ids=[".".join(p) or "<root>" for p in TABLES])
def test_an_unknown_key_anywhere_names_its_path(path: tuple[str, ...]) -> None:
    data = _nest((*path, "zzz"), 1)
    if path == ("tasks", "t"):
        data["tasks"]["t"]["cmd"] = ["x"]
    fails(data, f"unknown key '{'.'.join((*path, 'zzz'))}'")


def test_schema_types_come_from_the_dataclasses() -> None:
    # The old loader guessed "list of strings" from key-name suffixes: typing.mypy_overrides = [1]
    # and ["mymodule"] slipped through and crashed render with a traceback
    fails({"typing": {"mypy_overrides": [1]}}, "'typing.mypy_overrides[0]' must be of type table, not integer")
    fails({"typing": {"mypy_overrides": ["mymodule"]}}, "'typing.mypy_overrides[0]' must be of type table, not string")
    fails({"preset": {"script": 5}}, "'preset.script' must be of type table, not integer")
    fails({"backend": {"supported": ["cpython", 1]}}, "'backend.supported[1]' must be of type string, not integer")
    fails({"deploy": {"default": {"cpython": 5}}}, "'deploy.default.cpython' must be of type string, not integer")
    fails({"schema": True}, "'schema' must be of type integer, not boolean")
    fails({"python": {"cpython": 3.14}}, "'python.cpython' must be of type string, not float")
    fails({"app": {"name": datetime.date(2026, 1, 1)}}, "'app.name' must be of type string, not date")
    fails({"tasks": {"a b": {"cmd": [2]}}}, "'tasks.\"a b\".cmd[0]' must be of type string")  # quoted key in the path
    fails({"tasks": ["x"]}, "'tasks' must be a table")
    fails({"tasks": {"t": 5}}, "'tasks.t' must be a table")


@pytest.mark.parametrize(
    "data",
    [
        {"app": {"name": "my\0app"}},
        {"deploy": {"exe": {"extra_args": ["--x\0"]}}},
        {"tasks": {"t": {"cmd": ["echo", "a\0b"]}}},
        {"vscode": {"settings": {"k": ["ok", "a\0"]}}},
        {"app": {"preset": "flet"}, "preset": {"flet": {"version": "1\0"}}},
    ],
)
def test_nul_characters_are_rejected(data: dict[str, Any]) -> None:
    # They would reach subprocess argv or a path: ValueError "embedded null byte" (a traceback)
    fails(data, "NUL character")


@pytest.mark.parametrize(
    ("settings", "where"),
    [
        ({"when": datetime.date(1979, 5, 27)}, "'vscode.settings.when' is a TOML date"),
        ({"x": {"at": datetime.time(7, 32)}}, "'vscode.settings.x.at' is a TOML time"),
        ({"x": [1, datetime.datetime(1979, 5, 27, 7, 32)]}, "'vscode.settings.x[1]' is a TOML date-time"),
        ({"n": math.nan}, "only finite numbers"),
        ({"n": -math.inf}, "only finite numbers"),
    ],
)
def test_vscode_settings_must_be_json(settings: dict[str, Any], where: str) -> None:
    # json.dumps raised "Object of type date is not JSON serializable" on every render
    fails({"vscode": {"settings": settings}}, where)


def test_vscode_settings_accept_json_values() -> None:
    settings = {"editor.fontSize": 13.5, "a.b": True, "n": -3, "s": "x", "list": [1, "two", {"k": 0}], "t": {"u": {}}}
    cfg = make({"vscode": {"settings": settings}})
    rendered = json.loads(json.dumps(vscode.settings(cfg, "off")))
    assert rendered["editor.fontSize"] == 13.5 and rendered["t"] == {"u": {}} and rendered["list"] == [1, "two", {"k": 0}]


@pytest.mark.parametrize("name", ["", "A=B", "=A", "A\0B"])
@pytest.mark.parametrize("where", ["tasks.t.env", "deploy.portable.env"])
def test_environment_variable_names_the_os_cannot_hold(name: str, where: str) -> None:
    # subprocess raised ValueError ("illegal environment variable name") with a traceback
    data = _nest((*where.split("."), name), "x")
    if where.startswith("tasks"):
        data["tasks"]["t"]["cmd"] = ["x"]
    fails(data, "invalid environment variable name")


def test_environment_variable_names_that_work() -> None:
    assert make({"tasks": {"t": {"cmd": ["x"], "env": {"A_1": "x"}}}}).tasks["t"].env == {"A_1": "x"}
    assert make({"deploy": {"portable": {"env": {"PT_X": "1"}}}}).deploy.portable.env == {"PT_X": "1"}


# --- validate: one positive and one negative case per rule -----------------------------------------


def test_schema_must_match_the_runner() -> None:
    assert make({}).schema == config.SCHEMA == 1  # a missing line means the current schema
    assert make({"schema": config.SCHEMA}).schema == config.SCHEMA
    for bad in (0, 2, 99):
        fails({"schema": bad}, f"schema = {bad} is not supported by this runner")
    # Checked before the keys: a newer layout's unknown keys must not hide the real reason
    fails({"schema": 2, "future": {"x": 1}}, "schema = 2 is not supported")
    with pytest.raises(DeployError, match="schema = 7"):
        config.validate(Config(schema=7))


@pytest.mark.parametrize("name", ["myapp", "My-App_2", "a"])
def test_app_name_valid(name: str) -> None:
    assert make({"app": {"name": name}}).app.name == name


@pytest.mark.parametrize("name", ["", "1app", "my app", "app\n", "-app", "caf" + E_ACUTE, "a.b"])
def test_app_name_invalid(name: str) -> None:
    fails({"app": {"name": name}}, "'app.name' only allows")


def test_app_preset() -> None:
    assert make({"app": {"preset": "flet"}}).app.preset == "flet"
    fails({"app": {"preset": "nope"}}, "is not a preset of this template")


@pytest.mark.parametrize("assets", ["", "assets"])
def test_app_assets_on_and_off(assets: str) -> None:
    assert make({"app": {"assets": assets}}).app.assets == assets


@pytest.mark.parametrize("assets", ["data", "src/assets", "assets/", "../assets", "/tmp/assets", "C:\\x", "Assets", ".."])
def test_app_assets_only_knows_src_assets(assets: str) -> None:
    # resources.assets_dir(), portable/boot.py and pyz/__main__.py only look for src/assets/
    fails({"app": {"assets": assets}}, "app.assets")


def test_backend_supported() -> None:
    assert make({"backend": {"supported": ["mypyc", "cpython"]}}).backend.supported == ["mypyc", "cpython"]
    fails({"backend": {"supported": ["cpython", "jython"]}}, "'backend.supported' = 'jython' is not valid")
    fails({"backend": {"supported": []}}, "'backend.supported' cannot be empty")
    # Twice: duplicated VS Code tasks, `test all` ran mypyc twice, `mode -mypyc` left one copy
    fails({"backend": {"supported": ["cpython", "mypyc", "mypyc"]}}, "lists mypyc more than once")


def test_backend_active() -> None:
    assert make({"backend": {"active": "mypyc"}}).backend.active == "mypyc"
    fails({"backend": {"active": "jython"}}, "'backend.active' = 'jython' is not valid")
    fails({"backend": {"active": "pypy", "supported": ["cpython"]}}, "is not in backend.supported")


@pytest.mark.parametrize("version", ["3.14", "3.9", "3.100", "4.0"])
def test_python_cpython_valid(version: str) -> None:
    assert make({"python": {"cpython": version}}).python.cpython == version


@pytest.mark.parametrize(
    "version",
    ["3", "3.14.1", "3.14\n", " 3.14", "py3.14", "\u0663.\u0661\u0664", "\uff13.\uff11\uff14", "3.x"],
)
def test_python_cpython_invalid(version: str) -> None:
    # \d accepted Arabic-Indic and full-width digits; min_python then wrote them to pyproject
    fails({"python": {"cpython": version}}, "'python.cpython' must be a minor version")


@pytest.mark.parametrize("version", ["pypy@3.11.15", "pypy@3.12.0"])
def test_python_pypy_valid(version: str) -> None:
    assert make({"python": {"pypy": version}}).python.pypy == version


@pytest.mark.parametrize("version", ["pypy@3.11", "3.11.15", "pypy@3.11.15\n", "pypy@\u0663.11.15", "pypy", "pypy@3.11.x"])
def test_python_pypy_must_be_exact(version: str) -> None:
    error = fails({"python": {"pypy": version}}, "exact version")
    assert f'"{config.PythonConfig().pypy}"' in str(error)  # the example is the real default pin
    assert "yet" not in str(error).split()  # a fact, not a date-bound claim


@pytest.mark.parametrize(
    ("key", "good", "bad"),
    [
        ("profile", ["auto", "mypyc", "strict", "warn", "off"], "loose"),
        ("relaxed", ["off", "warn", "strict"], "mypyc"),
        ("editor", ["pylance", "basedpyright"], "pyright"),
    ],
)
def test_typing_choices(key: str, good: list[str], bad: str) -> None:
    for value in good:
        assert getattr(make({"typing": {key: value}}).typing, key) == value
    fails({"typing": {key: bad}}, f"'typing.{key}' = '{bad}' is not valid")


def test_mypyc_backend_needs_real_typing() -> None:
    for profile in ("auto", "strict", "mypyc"):
        assert make({"backend": {"active": "mypyc"}, "typing": {"profile": profile}}).typing.profile == profile
    for profile in ("warn", "off"):
        fails({"backend": {"active": "mypyc"}, "typing": {"profile": profile}}, "with the mypyc backend")


@pytest.mark.parametrize("key", ["modules", "exclude", "forbid_imports"])
def test_compile_module_names(key: str) -> None:
    assert make({"compile": {key: ["myapp.core", "_x.y1"]}})
    # forbid_imports was never checked: "flet, flet_desktop" or "flet " silently disabled the rule
    for bad in ["a b", "flet, flet_desktop", "flet ", "a.", ".a", "a..b", "1a", "", "myapp.core\n"]:
        fails({"compile": {key: [bad]}}, "invalid module in [compile]")


def test_compile_opt_level() -> None:
    for level in ("0", "1", "2", "3"):
        assert make({"compile": {"opt_level": level}}).compile.opt_level == level
    fails({"compile": {"opt_level": "4"}}, "'compile.opt_level' = '4' is not valid")


GOOD_OVERRIDES = [
    {"module": "{pkg}.ui.*", "ignore_errors": True},
    {"module": ["raylib", "raylib.*"], "disable_error_code": ["explicit-override", "no-untyped-call"], "warn_return_any": False},
    {"module": "a.*.b", "follow_imports": "skip", "some-option": 1},
]


def test_mypy_overrides_valid() -> None:
    cfg = make({"typing": {"mypy_overrides": GOOD_OVERRIDES}})
    ini = render.mypy_ini(cfg, "off")
    assert "[mypy-myapp.ui.*]\nignore_errors = True" in ini
    assert "[mypy-raylib,raylib.*]\ndisable_error_code = explicit-override, no-untyped-call" in ini


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"ignore_errors": True}, "'typing.mypy_overrides[0]' needs 'module'"),
        ({"module": "x", "strict": True}, "do not put 'strict'"),
        ({"module": 5}, "5 is not a module name or pattern"),
        ({"module": [1]}, "1 is not a module name or pattern"),
        ({"module": []}, "'typing.mypy_overrides[0].module' is empty"),
        ({"module": "a b"}, "'a b' is not a module name"),
        ({"module": "x,y"}, "'x,y' is not a module name"),
        ({"module": "x]\n[mypy"}, "is not a module name"),
        ({"module": "x", "a b": True}, "'a b' is not a mypy option name"),
        ({"module": "x", "a=b": True}, "is not a mypy option name"),
        ({"module": "x", "opt": {"a": 1}}, "'typing.mypy_overrides[0].opt' must be a boolean, a number"),
        ({"module": "x", "opt": [[1]]}, "must be a boolean, a number"),
        ({"module": "x", "opt": "a\n[mypy]\nstrict = True"}, "one-line string"),  # INI injection
        ({"module": "x", "opt": datetime.date(2026, 1, 1)}, "is a TOML date"),
    ],
)
def test_mypy_overrides_invalid(override: dict[str, Any], message: str) -> None:
    # Before: TypeError / AttributeError tracebacks in validate or render.mypy_ini
    fails({"typing": {"mypy_overrides": [GOOD_OVERRIDES[0], override]}}, message.replace("[0]", "[1]"))


def test_deploy_optimize() -> None:
    for level in (0, 1, 2):
        assert make({"deploy": {"optimize": level}}).deploy.optimize == level
    for bad in (-1, 3):
        fails({"deploy": {"optimize": bad}}, "'deploy.optimize' must be 0, 1 or 2")


def test_deploy_default_names() -> None:
    assert make({"deploy": {"default": {"cpython": "pyz"}}}).deploy.default["cpython"] == "pyz"
    fails({"deploy": {"default": {"jython": "exe"}}}, "'deploy.default' = 'jython' is not valid")
    fails({"deploy": {"default": {"cpython": "zip"}}}, "'deploy.default.cpython' = 'zip' is not valid")


@pytest.mark.parametrize("backend", config.BACKENDS)
@pytest.mark.parametrize("method", config.METHODS)
def test_deploy_default_follows_cmd_build_compat(backend: str, method: str) -> None:
    data = {"app": {"preset": "flet" if method == "flet" else "script"}, "deploy": {"default": {backend: method}}}
    reason = cmd_build.COMPAT[method].get(backend)
    if reason:  # e.g. pypy + exe: before, doctor said "all good" and the build failed later
        fails(data, f"deploy.default.{backend} = '{method}' cannot package {backend}: {reason}")
    else:
        assert make(data).deploy.default[backend] == method


def test_deploy_default_flet_needs_the_flet_preset() -> None:
    # `--dry-run build` claimed success; the real build failed after the checks
    fails({"deploy": {"default": {"cpython": "flet"}}}, "the flet method (flet build) is only for the flet preset")
    assert make({"app": {"preset": "flet"}, "deploy": {"default": {"cpython": "flet"}}}).deploy.default["cpython"] == "flet"


@pytest.mark.parametrize(
    "data",
    [
        {"deploy": {"default": {"cpython": "pyz"}}},
        {"deploy": {"default": {}}},
        tomllib.loads('[deploy.default]\ncpython = "pyz"\n'),
        {"deploy": {"default": {"mypyc": "wheel", "cpython": "portable"}}},
    ],
)
def test_partial_deploy_default_keeps_the_other_backends(data: dict[str, Any]) -> None:
    # A missing backend fell back to "exe", which PyInstaller cannot build for pypy
    cfg = make({"backend": {"active": "pypy", "supported": ["cpython", "pypy", "mypyc"]}, **data})
    assert cfg.deploy.default == {**config.DEFAULT_METHODS, **data["deploy"]["default"]}
    build_task = next(e for e in vscode.catalog(cfg) if e.args == ("build",))
    assert "'portable' method" in build_task.summary
    assert nvim.editor_data(cfg, "off")["build"]["default"]["pypy"] == "portable"
    assert Config().deploy.default == config.DEFAULT_METHODS == {"cpython": "exe", "mypyc": "exe", "pypy": "portable"}
    assert Config().deploy.default is not config.DEFAULT_METHODS  # never the shared dict


@pytest.mark.parametrize(
    ("table", "key", "good", "bad"),
    [
        ("exe", "mode", ["onefile", "onedir"], "twofile"),
        ("exe", "console", ["auto", "yes", "no"], "maybe"),
        ("portable", "runtime", ["bundled", "system"], "docker"),
        ("nuitka", "mode", ["standalone", "onefile"], "app"),
        ("upx", "level", ["1", "9", "best", "brute", "ultra-brute"], "0"),
        ("upx", "level", ["5"], "fast"),
    ],
)
def test_deploy_choices(table: str, key: str, good: list[str], bad: str) -> None:
    for value in good:
        assert getattr(getattr(make({"deploy": {table: {key: value}}}).deploy, table), key) == value
    fails({"deploy": {table: {key: bad}}}, f"'deploy.{table}.{key}' = '{bad}' is not valid")


def test_portable_env_names_are_identifiers() -> None:
    assert make({"deploy": {"portable": {"env": {"_A1": "x"}}}}).deploy.portable.env == {"_A1": "x"}
    for bad in ("A B", "1X", "A-B"):  # they become `set "K=v"` / `export K=v` lines
        fails({"deploy": {"portable": {"env": {bad: "x"}}}}, "deploy.portable.env: invalid environment variable name")


@pytest.mark.parametrize("where", [("exclude_modules",), ("exe", "hidden_imports")])
def test_deploy_module_names(where: tuple[str, ...]) -> None:
    assert make(_nest(("deploy", *where), ["PIL", "x.y_z"]))
    for bad in ["PIL\n", "a b", "a,b", "x.", ""]:
        fails(_nest(("deploy", *where), [bad]), f"invalid module in deploy.{'.'.join(where)}")


def test_preset_options_are_checked_against_preset_toml() -> None:
    cfg = make({"app": {"preset": "raylib"}, "preset": {"raylib": {"package": "raylib_sdl", "version": "6.0.1.0"}}})
    assert cfg.preset_options("raylib") == {"package": "raylib_sdl", "version": "6.0.1.0"}
    assert make({"preset": {"flet": {"version": "1.0.1"}}})  # a table for a preset not in use is harmless
    # A typo was silently ignored and the default version used
    fails({"app": {"preset": "flet"}, "preset": {"flet": {"versoin": "1.0.2"}}}, "unknown key 'preset.flet.versoin' (valid: version)")
    fails({"preset": {"script": {"x": 1}}}, "unknown key 'preset.script.x' (valid: none, the script preset has no options)")
    fails({"preset": {"flet": {"version": 1}}}, "'preset.flet.version' must be of type string, not integer")
    fails({"preset": {"raylb": {}}}, "[preset.raylb]: 'raylb' is not a preset of this template")
    fails({"preset": {"../presets/flet": {}}}, "[preset.\"../presets/flet\"]: '../presets/flet' is not a preset")


def test_preset_options_of_a_broken_preset_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("script", "p"):
        (tmp_path / name).mkdir()
    (tmp_path / "script" / "preset.toml").write_text('description = "x"\n', encoding="utf-8")
    (tmp_path / "p" / "preset.toml").write_bytes(b"[options\n")
    monkeypatch.setattr(config, "PRESETS", tmp_path)
    fails({"preset": {"p": {"x": "1"}}}, "cannot be read")


def test_tasks_rules() -> None:
    assert make({"tasks": {"my-task_1": {"cmd": ["x"], "backend": "cpython"}, "d": {"deps": ["run"]}}})
    fails({"tasks": {"Bad": {"cmd": ["x"]}}}, "'Bad'")
    fails({"tasks": {"run": {"cmd": ["x"]}}}, "clashes with")
    fails({"tasks": {"t": {}}}, "'t'")
    fails({"tasks": {"t": {"cmd": ["x"], "backend": "jython"}}}, "tasks.t.backend")


def test_vscode_buttons() -> None:
    assert make({"vscode": {"buttons": ["run", "build --method pyz", "t"]}, "tasks": {"t": {"cmd": ["x"]}}})
    fails({"vscode": {"buttons": ["nope"]}}, "vscode.buttons: 'nope' is neither")
    fails({"vscode": {"buttons": [" "]}}, "vscode.buttons")
    assert make({"vscode": {"buttons": ["nope"]}}, commands=None)  # without the command list: not checked


@pytest.mark.parametrize("name", sorted(shipped_configs()))
def test_every_shipped_config_is_valid(name: str) -> None:
    data = tomllib.loads(shipped_configs()[name])
    cfg = make(data)
    assert cfg.schema == config.SCHEMA
    assert cfg.app.assets in ("", "assets")


@pytest.mark.parametrize("name", sorted(shipped_configs()))
def test_pypy_pins_match_the_default(name: str) -> None:
    if name == "root" and not TEMPLATE_REPO:
        pytest.skip("a project may pin another PyPy on purpose")
    pin = tomllib.loads(shipped_configs()[name])["python"]["pypy"]
    assert pin == config.PythonConfig().pypy
    assert re.fullmatch(r"pypy@[0-9]+\.[0-9]+\.[0-9]+", pin)


# --- derived values ---------------------------------------------------------------------------------


def test_min_python_is_the_oldest_interpreter() -> None:
    assert make({}).min_python == "3.14"
    assert make({"backend": {"supported": ["cpython", "pypy"]}}).min_python == "3.11"
    assert make({"backend": {"active": "pypy", "supported": ["pypy"]}}).min_python == "3.11"  # tools run on CPython
    assert make({"python": {"cpython": "3.9"}}).min_python == "3.9"
    # Numbers, not strings: "3.100" < "3.11" as text
    assert make({"python": {"cpython": "3.100"}, "backend": {"supported": ["cpython", "pypy"]}}).min_python == "3.11"
    pypy312 = {"python": {"cpython": "3.11", "pypy": "pypy@3.12.1"}, "backend": {"supported": ["cpython", "pypy"]}}
    assert make(pypy312).min_python == "3.11" and make(pypy312).pypy_minor == "3.12"


def test_older_cpython_than_pypy_keeps_both_environments() -> None:
    # min_python took PyPy's 3.11 and requires-python excluded the CPython 3.10 environment
    cfg = make({"python": {"cpython": "3.10"}, "backend": {"supported": ["cpython", "pypy"]}})
    assert (cfg.min_python, cfg.pypy_minor) == ("3.10", "3.11")
    block = render.managed_block(cfg)
    assert "implementation_name == 'cpython' and python_full_version >= '3.10' and python_full_version < '3.11'" in block
    assert "implementation_name == 'pypy' and python_full_version >= '3.11' and python_full_version < '3.12'" in block
    text = '[project]\nname = "x"\nrequires-python = ">=3.11"\n\n[tool.uv]\n'
    assert 'requires-python = ">=3.10"' in render.pyproject_expected(cfg, text)


# --- toml_value -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain",
        'q"uote',
        "back\\slash",
        "tab\there",
        "new\nline",
        "cr\rlf",
        "\x00\x01\x08\x0b\x0c\x1f",
        "del\x7f",
        "caf" + E_ACUTE,
        "\u2028\u2029\u0085",
        "\U0001f600",
        "'single'",
        "# not a comment",
        "[x] = y",
        '"""',
    ],
)
def test_toml_value_round_trips_strings(value: str) -> None:
    assert tomllib.loads("k = " + toml_value(value))["k"] == value
    assert tomllib.loads("k = " + toml_value([value, value]))["k"] == [value, value]


def test_toml_value_other_types() -> None:
    assert [toml_value(True), toml_value(False), toml_value(0), toml_value(-7), toml_value([])] == ["true", "false", "0", "-7", "[]"]
    assert tomllib.loads("k = " + toml_value(["a", 1, True, ["b"]]))["k"] == ["a", 1, True, ["b"]]
    for bad in (1.5, {"a": 1}, None, ("a",)):
        with pytest.raises(TypeError):
            toml_value(bad)
    with pytest.raises(DeployError, match="lone surrogate"):
        toml_value("a\ud800b")  # TOML cannot hold it: the write would fail with UnicodeEncodeError


# --- set_value ----------------------------------------------------------------------------------------

# Verbatim `taplo fmt` 0.9.3 output (default options) for tables of the flet and script presets:
# a line longer than 80 columns (comment included) gets its array expanded, even an empty one
TAPLO = (
    "[backend]\n"
    "# Flet: Flutter draws the UI, and Flet's Python side (diffs, messages) cannot be compiled.\n"
    'active = "cpython"\n'
    "supported = [\n"
    '  "cpython",\n'
    '  "mypyc",\n'
    "] # PyPy: experimental, only for running during development\n"
    "\n"
    "[typing]\n"
    'profile = "auto"\n'
    'editor = "pylance"\n'
    "\n"
    "[deploy]\n"
    "optimize = 1 # 0 | 1 | 2: bytecode -O level; with >= 1 mypyc also strips asserts\n"
    'default = { cpython = "exe", mypyc = "exe", pypy = "portable" } # ./deploy build method\n'
    "exclude_modules = [\n"
    '  "PIL",\n'
    "] # flet imports Pillow only for RawImage: -13 MB (remove it if you use it)\n"
    "\n"
    "[deploy.pyz]\n"
    "targets = [\n"
    '  "host",\n'
    '] # add keys like "cp314-linux-x86_64" to include native binaries for other OSes\n'
    "\n"
    "[deploy.upx]\n"
    "enabled = false\n"
    'level = "best" # 1..9 | best | brute | ultra-brute (brute: much slower, a few % smaller)\n'
    "exclude = [\n"
    "] # extra file-name globs never packed (C runtime and python3*.dll never are)\n"
)
TAPLO_EDITS = [
    ("backend", "supported", ["cpython"], " # PyPy: experimental"),
    ("backend", "supported", ["cpython", "pypy", "mypyc"], " # PyPy: experimental"),
    ("backend", "active", "mypyc", None),
    ("typing", "relaxed", "warn", None),
    ("deploy", "exclude_modules", [], " # flet imports Pillow"),
    ("deploy.pyz", "targets", ["host", "cp314-linux-x86_64"], ' # add keys like "cp314'),
    ("deploy.upx", "exclude", ["libx*.so"], " # extra file-name globs"),
    ("deploy", "default", ["not", "a", "table"], " # ./deploy build method"),
]


def _expected(before: dict[str, Any], table: str, key: str, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(before)
    node = out
    for part in table.split("."):
        node = node.setdefault(part, {})
    node[key] = value
    return out


def _check_edit(text: str, table: str, key: str, value: Any) -> str:
    """set_value's result: valid TOML, only this key changed, the line endings kept."""
    out = set_value(text, table, key, value)
    assert tomllib.loads(out) == _expected(tomllib.loads(text), table, key, value)
    if "\r\n" in text:
        assert "\n" not in out.replace("\r\n", ""), "a bare LF in a CRLF file"
    return out


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
@pytest.mark.parametrize("final", [True, False], ids=["eol-at-eof", "no-eol-at-eof"])
@pytest.mark.parametrize(("table", "key", "value", "tail"), TAPLO_EDITS, ids=[f"{t}.{k}={v}" for t, k, v, _ in TAPLO_EDITS])
def test_set_value_on_taplo_output(eol: str, final: bool, table: str, key: str, value: Any, tail: str | None) -> None:
    # set_value replaced only the first line of `supported = [`: "not valid TOML" and mode was stuck
    text = TAPLO if final else TAPLO.rstrip("\n")
    text = text.replace("\n", eol)
    out = _check_edit(text, table, key, value)
    if tail:  # the whole old value went (it parses), the comment after it stayed on its line
        assert f"{key} = {toml_value(value)}{tail}" in out


def taplo_expand(text: str) -> str:
    """Every one-line array written the way taplo expands a long one (one item per line)."""

    def expand(m: re.Match[str]) -> str:
        items = tomllib.loads("v = [" + m.group("items") + "]")["v"]
        body = "".join(f"  {toml_value(i)},\n" for i in items)
        tail = f" {m.group('tail').strip()}" if m.group("tail") else ""
        return f"{m.group('key')}[\n{body}]{tail}"

    return re.sub(r"(?m)^(?P<key>[ \t]*[A-Za-z0-9_-]+[ \t]*=[ \t]*)\[(?P<items>[^\]\[\n]*)\](?P<tail>[ \t]*#[^\n]*)?$", expand, text)


MODE_EDITS = [
    ("backend", "supported", ["cpython"]),
    ("backend", "supported", ["cpython", "pypy", "mypyc"]),
    ("backend", "active", "mypyc"),
    ("typing", "profile", "mypyc"),
    ("typing", "relaxed", "strict"),
    ("typing", "editor", "basedpyright"),
    ("app", "name", "zeta"),
    ("python", "cpython", "3.13"),
    ("brand", "new", "table"),
]


@pytest.mark.parametrize("name", sorted(shipped_configs()))
@pytest.mark.parametrize("layout", ["as-is", "taplo", "taplo-crlf", "crlf-no-final-eol"])
def test_set_value_on_every_shipped_config(name: str, layout: str) -> None:
    text = shipped_configs()[name]
    if layout.startswith("taplo"):
        text = taplo_expand(text)
        assert "supported = [\n" in text  # the fixture really is multi-line
    if "crlf" in layout:
        text = text.replace("\n", "\r\n")
    if "no-final-eol" in layout:
        text = text.rstrip("\r\n")
    for table, key, value in MODE_EDITS:
        _check_edit(text, table, key, value)


EDGE_CASES = [
    ('[backend]\nsupported = [   # list\n  "cpython"]\n', "backend", "supported", ["cpython", "mypyc"]),
    ('[backend]\nsupported = ["a]",\n "b"]\n', "backend", "supported", ["c"]),
    ('[backend]\nsupported = [ # ] in a comment\n "cpython",\n]\n', "backend", "supported", ["mypyc"]),
    ('[backend]\nsupported = [\n  "cpython", # the usual one\n  "mypyc",   # compiled\n]\nactive = "cpython"\n', "backend", "supported", ["cpython"]),
    ("[x]\nnested = [[1, 2], [3,\n 4]]\nafter = 1\n", "x", "nested", [1]),
    ('[x]\ntable = { a = [1,\n 2], b = "}" }\nafter = 1\n', "x", "table", "v"),
    ('[backend]\nactive = "cpython"# the mode, no space before #\n', "backend", "active", "pypy"),
    ('[backend]\nactive="cpython"\n', "backend", "active", "pypy"),
    ('[backend]\n  active   =   "cpython"   # aligned\n', "backend", "active", "pypy"),
    ('[ backend ] # the table\nactive = "cpython"\n', "backend", "active", "pypy"),
    ('["backend"]\n"active" = "cpython"\n', "backend", "active", "pypy"),
    ("['backend']\n'active' = 'cpython' # literal\n", "backend", "active", "pypy"),
    ('backend.active = "cpython"\n', "backend", "active", "pypy"),
    ('[deploy]\ndefault.cpython = "exe"\n', "deploy.default", "cpython", "pyz"),
    ('[backend]\nactive = """\n[typing]\nrelaxed = "x"\n"""\n[typing]\nrelaxed = "off"\n', "typing", "relaxed", "warn"),
    ('[app]\nassets = """\nname = "x"\n"""\nname = "myapp"\n', "app", "name", "zeta"),
    ("[app]\nassets = '''\nname = \"x\"\n'''\nname = \"myapp\"\n", "app", "name", "zeta"),
    ('[app]\ns = """a""""\nname = "myapp"\n', "app", "name", "zeta"),
    ("[app]\ns = '''a'''''\nname = \"myapp\"\n", "app", "name", "zeta"),
    ('[app]\ns = "a \\" [x] # y"\nname = "myapp" # n\n', "app", "name", "zeta"),
    ('[app]\ns = """a \\""" b"""\nname = "myapp"\n', "app", "name", "zeta"),
    ('[typing]\nprofile = "auto"', "typing", "relaxed", "warn"),
    ('[typing]\nprofile = "auto"\neditor = "pylance"  # c', "typing", "relaxed", "warn"),
    ('[app]\nname = "x"\n[typing]', "typing", "relaxed", "warn"),
    ('[typing]\nprofile = "auto"\n\n# about the overrides\n[[typing.mypy_overrides]]\nmodule = "x"\n', "typing", "relaxed", "warn"),
    ('[[typing.mypy_overrides]]\nmodule = "x"\n\n[typing]\nprofile = "auto"\n', "typing", "relaxed", "warn"),
    ('[deploy.exe]\nmode = "onedir"\n', "deploy", "optimize", 2),
    ("", "backend", "active", "pypy"),
    ("# only a comment", "backend", "active", "pypy"),
    ("a = 1\n\n\n", "backend", "active", "pypy"),
    ("[python]\nk = 1979-05-27 07:32:00 # a date with a space\n", "python", "k", "x"),
    ("[x]\nn = +1_000 # num\nf = -inf\n", "x", "n", 5),
    ("[x]\r\nb = true\r\nc = 1\r\n", "x", "b", False),
    ("[x]\r\nb = true\r\n", "x", "new", "v"),
    ("\ta = 1\n[x]\n\tb = 2 \t# tab\n", "x", "b", 3),
]


@pytest.mark.parametrize(("text", "table", "key", "value"), EDGE_CASES)
def test_set_value_edge_layouts(text: str, table: str, key: str, value: Any) -> None:
    out = _check_edit(text, table, key, value)
    for comment in re.findall(r"#[^\n\"']*", text):  # every comment survives
        assert comment in out or key in ("supported", "nested")


SCANNED = [
    *[(f"{name}-{layout}", text) for name, text in shipped_configs().items() for layout, text in (("lf", text), ("taplo", taplo_expand(text)), ("crlf", text.replace("\n", "\r\n")))],
    ("taplo-output", TAPLO),
    *[(f"edge{i}", text) for i, (text, *_) in enumerate(EDGE_CASES)],
]


@pytest.mark.parametrize(("label", "text"), SCANNED, ids=[label for label, _ in SCANNED])
def test_scanner_spans_are_the_documents_values(label: str, text: str) -> None:
    """Every `key = value` the scanner finds: its span alone parses to the document's value."""
    data = tomllib.loads(text)
    stmts = config._statements(text)
    keys = [s for s in stmts if s.kind == "key"]
    assert len(keys) == len(re.findall(r"(?m)^[ \t]*[A-Za-z0-9_\"'-][^=\n]*=", text)) - _in_strings(text)
    for s in keys:
        if s.in_array:
            continue
        node: Any = data
        for part in s.path:
            node = node[part]
        start, stop = s.value
        assert tomllib.loads("v = " + text[start:stop])["v"] == node, (s.path, text[start:stop])
    for s in stmts:
        if s.kind == "table":
            node = data
            for part in s.path:
                node = node[part]
            assert isinstance(node, dict)


def _in_strings(text: str) -> int:
    """`key = ` look-alike lines inside multi-line strings and arrays (the scanner skips them)."""
    count = 0
    for s in config._statements(text):
        if s.kind == "key":
            inner = text[s.value[0] : s.value[1]]
            count += len(re.findall(r"(?m)^[ \t]*[A-Za-z0-9_\"'-][^=\n]*=", inner))
    return count


def test_set_value_changes_only_the_value() -> None:
    text = '# head\n[backend]\nactive = "cpython"   # the mode\nsupported = ["cpython"]\n\n[python]\ncpython = "3.14"\n'
    out = set_value(text, "backend", "active", "mypyc")
    assert out == text.replace('active = "cpython"', 'active = "mypyc"')
    assert set_value(out, "backend", "active", "cpython") == text  # and back: byte-identical
    # A key that is missing goes after the table's last key, before the next table
    added = set_value(text, "backend", "relaxed", "x")
    assert added == text.replace('supported = ["cpython"]\n', 'supported = ["cpython"]\nrelaxed = "x"\n')
    assert set_value(text, "brand", "k", 1) == text + "\n[brand]\nk = 1\n"


def test_set_value_is_idempotent() -> None:
    for text in shipped_configs().values():
        for table, key, value in MODE_EDITS:
            once = set_value(text, table, key, value)
            assert set_value(once, table, key, value) == once


@pytest.mark.parametrize(
    ("text", "table", "key"),
    [
        ('backend = { active = "cpython" }\n', "backend", "active"),  # the table is an inline table
        ('[[backend]]\nactive = "cpython"\n', "backend", "active"),  # an array of tables
        ("[backend.supported]\nx = 1\n", "backend", "supported"),  # the key is a table
        ("[backend]\nsupported.x = 1\n", "backend", "supported"),  # ... defined through dotted keys
    ],
)
def test_set_value_refuses_what_it_cannot_edit(text: str, table: str, key: str) -> None:
    with pytest.raises(DeployError, match=r"could not set .* automatically.*set it by hand") as info:
        set_value(text, table, key, "x")
    assert info.value.code == 2


def test_set_value_needs_valid_toml() -> None:
    with pytest.raises(DeployError, match="not valid TOML"):
        set_value("[backend\nactive = 1\n", "backend", "active", "x")


# --- reading pytemplate.toml ------------------------------------------------------------------------

VALID = '# the config\nschema = 1\n\n[backend]\nactive = "cpython"   # the mode\nsupported = ["cpython", "mypyc"]\n\n[typing]\nrelaxed = "off"   # off | warn | strict\n'


@pytest.fixture
def cfg_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "pytemplate.toml"
    path.write_bytes(VALID.encode("utf-8"))
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    return path


def test_load_accepts_a_bom_and_crlf(cfg_file: Path) -> None:
    lf = config.load(COMMANDS)
    cfg_file.write_bytes(b"\xef\xbb\xbf" + VALID.replace("\n", "\r\n").encode("utf-8"))
    assert config.load(COMMANDS) == lf
    assert config.read_text() == VALID.replace("\n", "\r\n")  # BOM dropped, line endings kept


LAST_LINE = VALID.count("\n") + 1  # the line appended below
NOT_UTF8 = [
    ("utf-16", (VALID + "# caf" + E_ACUTE + "\n").replace("\n", "\r\n").encode("utf-16"), "is UTF-16 text"),  # PS 5.1 > and Out-File
    ("utf-16-be-bom", ("\ufeff" + VALID).encode("utf-16-be"), "is UTF-16 text"),
    ("utf-32", VALID.encode("utf-32"), "is UTF-32 text"),
    ("utf-16-no-bom", VALID.encode("utf-16-le"), "has NUL bytes (line 1)"),
    ("cp1252", (VALID + "# caf" + E_ACUTE + "\n").encode("cp1252"), f"is not UTF-8 (byte 0xE9 on line {LAST_LINE}"),  # PS 5.1 Set-Content
    ("latin-1-bom", b"\xef\xbb\xbf" + (VALID + "x = '\xff'\n").encode("latin-1"), f"is not UTF-8 (byte 0xFF on line {LAST_LINE}"),
]


@pytest.mark.parametrize(("label", "data", "message"), NOT_UTF8, ids=[n for n, _, _ in NOT_UTF8])
def test_a_config_that_is_not_utf8_is_a_config_error(cfg_file: Path, label: str, data: bytes, message: str) -> None:
    # A UnicodeDecodeError traceback blamed the runner ("internal runner error"), exit 1
    cfg_file.write_bytes(data)
    for call in (lambda: config.load(COMMANDS), config.read_text, lambda: config.update_file([("typing", "relaxed", "warn")])):
        with pytest.raises(DeployError) as info:
            call()
        assert message in str(info.value), str(info.value)
        assert "save it as UTF-8" in str(info.value) and "-Encoding utf8" in str(info.value)
        assert info.value.code == 2
    assert cfg_file.read_bytes() == data  # nothing rewritten


def test_the_line_of_a_bad_byte_counts_from_the_text(cfg_file: Path) -> None:
    # utf-8-sig counted the error offset after the BOM: one line off
    cfg_file.write_bytes(b"\xef\xbb\xbfa = 1\nbb = 2\n\xe9\n")
    with pytest.raises(DeployError, match="on line 3"):
        config.read_text()
    cfg_file.write_bytes(b"a = 1\n\xe9\n")
    with pytest.raises(DeployError, match="on line 2"):
        config.read_text()


def test_missing_unreadable_and_invalid_configs(cfg_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file.write_bytes(b"[backend\n")
    with pytest.raises(DeployError, match="pytemplate.toml is not valid TOML"):
        config.load(COMMANDS)
    cfg_file.unlink()
    with pytest.raises(DeployError, match="pytemplate.toml not found"):
        config.load(COMMANDS)

    class Locked:
        name = "pytemplate.toml"

        def is_file(self) -> bool:
            return True

        def read_bytes(self) -> bytes:
            raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(config, "CONFIG_FILE", Locked())
    with pytest.raises(DeployError, match="cannot read pytemplate.toml: Permission denied"):
        config.load(COMMANDS)


def test_help_and_doctor_with_a_utf16_config(cfg_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg_file.write_bytes(VALID.encode("utf-16"))
    assert cli.main(["help"]) == 0  # help still works (without the custom tasks)
    out = capsys.readouterr()
    assert "BACKEND = cpython" in out.out
    assert cli.main(["doctor"]) == 2
    err = capsys.readouterr().err
    assert "is UTF-16 text" in err and "internal runner error" not in err and "Traceback" not in err


# --- update_file ------------------------------------------------------------------------------------


def test_update_file_keeps_comments_and_every_other_byte(cfg_file: Path) -> None:
    config.update_file([("backend", "active", "mypyc"), ("typing", "relaxed", "warn")])
    new = cfg_file.read_bytes().decode("utf-8")
    old_lines, new_lines = VALID.splitlines(), new.splitlines()
    changed = [(a, b) for a, b in zip(old_lines, new_lines, strict=True) if a != b]
    assert changed == [
        ('active = "cpython"   # the mode', 'active = "mypyc"   # the mode'),
        ('relaxed = "off"   # off | warn | strict', 'relaxed = "warn"   # off | warn | strict'),
    ]


def test_update_file_keeps_a_bom_and_crlf(cfg_file: Path) -> None:
    cfg_file.write_bytes(b"\xef\xbb\xbf" + VALID.replace("\n", "\r\n").encode("utf-8"))
    config.update_file([("typing", "relaxed", "warn"), ("typing", "editor", "basedpyright")])
    data = cfg_file.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and data.count(b"\xef\xbb\xbf") == 1
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert b'editor = "basedpyright"\r\n' in data  # an added key uses the file's line ending
    assert config.load(COMMANDS).typing.relaxed == "warn"


def test_update_file_writes_nothing_without_a_change(cfg_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = cfg_file.stat().st_mtime_ns
    config.update_file([])
    config.update_file([("backend", "active", "cpython")])  # already the value
    assert cfg_file.read_bytes() == VALID.encode("utf-8") and cfg_file.stat().st_mtime_ns == before
    monkeypatch.setattr(proc, "DRY_RUN", True)
    config.update_file([("backend", "active", "mypyc")])
    assert cfg_file.read_bytes() == VALID.encode("utf-8")


def test_update_file_never_writes_a_broken_file(cfg_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_file.write_bytes(b'backend = { active = "cpython" }\n')
    with pytest.raises(DeployError, match="could not set backend.active"):
        config.update_file([("typing", "relaxed", "warn"), ("backend", "active", "mypyc")])
    assert cfg_file.read_bytes() == b'backend = { active = "cpython" }\n'  # the first edit was not written either
    cfg_file.write_bytes(VALID.encode("utf-8"))
    monkeypatch.setattr(config, "set_value", lambda text, table, key, value: text + "[[broken\n")
    with pytest.raises(DeployError, match="would break the file"):
        config.update_file([("typing", "relaxed", "warn")])
    assert cfg_file.read_bytes() == VALID.encode("utf-8")


def test_update_file_round_trip_is_byte_identical(cfg_file: Path) -> None:
    config.update_file([("backend", "supported", ["cpython", "pypy", "mypyc"]), ("backend", "active", "pypy")])
    config.update_file([("backend", "supported", ["cpython", "mypyc"]), ("backend", "active", "cpython")])
    assert cfg_file.read_bytes() == VALID.encode("utf-8")


# --- mode: argument parsing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "backend", "expected"),
    [
        ("+pypy", None, ["cpython", "pypy", "mypyc"]),
        ("+pypy,", None, ["cpython", "pypy", "mypyc"]),  # a stray comma was "unknown backend ''"
        (" +pypy ", None, ["cpython", "pypy", "mypyc"]),  # a stray space was "unknown backend ' +pypy'"
        ("+ pypy", None, ["cpython", "pypy", "mypyc"]),
        ("-mypyc", None, ["cpython"]),
        ("+pypy,-mypyc", None, ["cpython", "pypy"]),
        ("-pypy", None, ["cpython", "mypyc"]),
        ("cpython,,mypyc", None, ["cpython", "mypyc"]),
        ("mypyc, cpython", None, ["cpython", "mypyc"]),
        ("pypy,pypy", None, ["pypy"]),
        ("+mypyc", "pypy", ["cpython", "pypy", "mypyc"]),  # BACKEND is added, like `mode pypy` alone
        ("cpython,pypy", "pypy", ["cpython", "pypy"]),
        ("-cpython,-mypyc", "pypy", ["pypy"]),
    ],
)
def test_supports_specs(spec: str, backend: str | None, expected: list[str]) -> None:
    assert cmd_mode._supports_after(make({}), spec, backend) == expected


@pytest.mark.parametrize(
    ("spec", "backend", "message"),
    [
        (",", None, "--supports needs a value"),
        (" , ", None, "--supports needs a value"),
        ("+pypy,cpython", None, "mixes changes (+name, -name) with plain names"),
        ("cpython,-mypyc", None, "mixes changes"),
        ("+pypi", None, "unknown backend 'pypi' in '+pypi'"),
        ("cpyton", None, "unknown backend 'cpyton'"),
        ("+pypy,-pypy", None, "pypy is both added and removed"),
        ("-cpython,-mypyc", None, "at least one backend must stay supported"),
        # BACKEND and --supports contradict each other: pypy was silently added back
        ("-pypy", "pypy", "mode pypy --supports -pypy: pypy would be the active backend, but --supports removes it"),
        ("cpython", "mypyc", "--supports leaves it out of the list"),
    ],
)
def test_bad_supports_specs(spec: str, backend: str | None, message: str) -> None:
    with pytest.raises(DeployError) as info:
        cmd_mode._supports_after(make({}), spec, backend)
    assert message in str(info.value)
    assert info.value.code == 2


@pytest.fixture
def dry(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setattr(proc, "DRY_RUN", True)
    return config.load(set())


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--typing", "strict", "--typing", "off"], "mode: --typing given more than once"),
        (["--supports", "+pypy", "--supports=-pypy"], "mode: --supports given more than once"),
        (["--editor=basedpyright", "--editor", "pylance"], "--editor given more than once"),
        (["--supports", "--typing", "strict"], "--supports needs a value"),
        (["--typ=strict"], "unknown argument(s): --typ=strict"),  # no silent abbreviations
        (["pypy", "--supports", "-pypy"], "--supports removes it"),
        (["mypyc", "--supports", "cpython"], "leaves it out of the list"),
    ],
)
def test_mode_rejects_contradictory_arguments(dry: Config, args: list[str], message: str) -> None:
    with pytest.raises(DeployError) as info:
        cmd_mode.cmd_mode(dry, args)
    assert message in str(info.value)
    assert info.value.code == 2


def test_mode_leaves_values_that_are_already_set(dry: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_mode.cmd_mode(dry, ["--editor", dry.typing.editor]) == 0
    assert "pytemplate.toml  unchanged" in capsys.readouterr().err


# --- mode: real runs in a throwaway copy of this project ----------------------------------------------

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not found")
_SCRUB = ("UV", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "p"
    dest.mkdir()
    presets.copy_template(dest)
    return dest


def _deploy(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB and not k.startswith("PYTEMPLATE_")}
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
    )


def _ok(root: Path, *args: str) -> str:
    r = _deploy(root, *args)
    assert r.returncode == 0, f"./deploy {' '.join(args)}: {r.stderr}"
    return r.stderr


def _toml(root: Path) -> dict[str, Any]:
    return tomllib.loads((root / "pytemplate.toml").read_text(encoding="utf-8-sig"))


def _generated(root: Path) -> dict[str, str]:
    """The generated files, CRLF-normalised (a Windows checkout may have CRLF, render writes LF)."""
    names = [".pytemplate/editor.json", ".pytemplate/state.json", ".vscode/extensions.json", ".vscode/tasks.json", ".mypy.ini"]
    return {n: (root / n).read_bytes().replace(b"\r\n", b"\n").decode("utf-8") for n in names}


def _editor_json(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / ".pytemplate" / "editor.json").read_text(encoding="utf-8"))
    return data


def test_mode_typing_round_trip_restores_every_byte(project: Path) -> None:
    typing_ = _toml(project)["typing"]
    if typing_.get("profile", "auto") != "auto":
        pytest.skip("the round trip needs typing.profile = auto")
    original, generated = (project / "pytemplate.toml").read_bytes(), _generated(project)
    other = next(v for v in ("strict", "warn", "off") if v != typing_.get("relaxed", "off"))
    _ok(project, "mode", "--typing", other)
    text = (project / "pytemplate.toml").read_bytes()
    changed = [(a, b) for a, b in zip(original.splitlines(), text.splitlines(), strict=True) if a != b]
    assert len(changed) == 1 and changed[0][1].startswith(f'relaxed = "{other}"'.encode())
    assert changed[0][0].split(b"#", 1)[1:] == changed[0][1].split(b"#", 1)[1:]  # the comment stayed
    assert _editor_json(project)["typing"]["profile"] == (other if _toml(project)["backend"]["active"] != "mypyc" else "mypyc")
    _ok(project, "render", "--check")
    _ok(project, "mode", "--typing", typing_.get("relaxed", "off"))
    assert (project / "pytemplate.toml").read_bytes() == original
    assert _generated(project) == generated


def test_mode_editor_round_trip(project: Path) -> None:
    current = _toml(project)["typing"].get("editor", "pylance")
    other = "basedpyright" if current == "pylance" else "pylance"
    original, generated = (project / "pytemplate.toml").read_bytes(), _generated(project)
    _ok(project, "mode", "--editor", other)
    recommended = json.loads(_generated(project)[".vscode/extensions.json"].split("\n", 1)[1])["recommendations"]
    assert ("detachhead.basedpyright" in recommended) == (other == "basedpyright")
    assert _editor_json(project)["typing"]["editor"] == other
    _ok(project, "mode", "--editor", current)
    assert (project / "pytemplate.toml").read_bytes() == original
    assert _generated(project) == generated


def test_mode_keeps_a_bom_and_crlf(project: Path) -> None:
    path = project / "pytemplate.toml"
    text = path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))
    editor = "basedpyright" if _toml(project)["typing"].get("editor", "pylance") == "pylance" else "pylance"
    _ok(project, "mode", "--editor", editor)
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and b"\n" not in data.replace(b"\r\n", b"")
    assert tomllib.loads(data.decode("utf-8-sig"))["typing"]["editor"] == editor


@needs_uv
def test_mode_supports_round_trip(project: Path) -> None:
    cfg = _toml(project)
    if cfg["backend"]["active"] == "mypyc" or "mypyc" not in cfg["backend"]["supported"]:
        pytest.skip("the round trip removes and adds back mypyc")
    files = ["pytemplate.toml", "pyproject.toml", "uv.lock"]
    original, generated = {f: (project / f).read_bytes() for f in files}, _generated(project)
    active = cfg["backend"]["active"]
    stderr = _ok(project, "--dry-run", "mode", "--supports", f"-{active}")  # writes nothing (checked below)
    assert f"note: {active} is no longer supported: the active backend becomes" in stderr
    _ok(project, "mode", "--supports", "-mypyc")
    assert "mypyc" not in _toml(project)["backend"]["supported"]
    assert "mypyc" not in _editor_json(project)["backend"]["supported"]
    _ok(project, "mode", "--supports", "+mypyc")
    assert {f: (project / f).read_bytes() for f in files} == original
    assert _generated(project) == generated


@needs_uv
def test_mode_edits_a_taplo_formatted_config(project: Path) -> None:
    # After one save in LazyVim (taplo), every `mode --supports` failed with "not valid TOML"
    path = project / "pytemplate.toml"
    text = taplo_expand(path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8"))
    assert "supported = [\n" in text
    path.write_bytes(text.encode("utf-8"))
    before = tomllib.loads(text)
    supported = before["backend"]["supported"]
    keep = [before["backend"]["active"]]
    stderr = _ok(project, "--dry-run", "mode", "--supports", ",".join(keep))
    assert f"[backend] supported = {toml_value(keep)}" in stderr
    if supported == keep:
        return
    _ok(project, "mode", "--supports", ",".join(keep))
    after = tomllib.loads(path.read_text(encoding="utf-8"))
    assert after == _expected(before, "backend", "supported", keep)
    assert re.search(r"(?m)^supported = \[[^\n]*\] #", path.read_text(encoding="utf-8"))  # its comment stayed
    _ok(project, "mode", "--supports", ",".join(supported))
    assert tomllib.loads(path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize(
    ("encoding", "message"),
    [("utf-16", "is UTF-16 text, not UTF-8"), ("cp1252", "is not UTF-8 (byte 0xE9")],
)
def test_every_command_reports_a_config_that_is_not_utf8(project: Path, encoding: str, message: str) -> None:
    path = project / "pytemplate.toml"
    text = path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8") + "# caf" + E_ACUTE + "\n"
    path.write_bytes(text.replace("\n", "\r\n").encode(encoding))
    for args in (["doctor"], ["tasks"], ["render", "--check"], ["mode", "--typing", "strict"], ["hooks", "status"]):
        r = _deploy(project, *args)
        assert r.returncode == 2, (args, r.stderr)
        assert message in r.stderr and "save it as UTF-8" in r.stderr, (args, r.stderr)
        assert "Traceback" not in r.stderr and "internal runner error" not in r.stderr, (args, r.stderr)
    r = _deploy(project, "help")
    assert r.returncode == 0 and "BACKEND = cpython" in r.stdout
