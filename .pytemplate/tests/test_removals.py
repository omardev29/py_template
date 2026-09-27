"""What the removals leave behind: no CPython JIT support, no public `init`, no `shell-setup`.

- `python.jit` / `python.jit_interpreter` are unknown keys: the loader's normal error (exit 2).
- Nothing the runner generates or starts sets PYTHON_JIT or points at a .venv-jit environment.
- `./pyt init ...` exits 2 with a hint (./pyt new DIR --preset P). The preset step lives on
  as the internal route `__init`: `./pyt new` runs it in the fresh copy, and the template
  maintainer regenerates the template root with it (./pyt __init script --name myapp --force).
- `shell-setup` (a pyt function or alias per shell) is an unknown command, listed nowhere:
  `pyt install` puts the launchers themselves on PATH.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cli, cmd_mode, config, envs, presets, proc, project, rename, render, shells, tasks, ui  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.editors import nvim, vscode  # noqa: E402
from runner.methods import portable, pyz  # noqa: E402
from runner.project import ENV_SUFFIX, PRESETS, ROOT, TEMPLATE  # noqa: E402
from runner.ui import PytError  # noqa: E402

HINT = "./pyt new DIR --preset P"
JIT_KEYS = ("jit", "jit_interpreter")
# Each preset's real pytemplate.toml, as `new` renders it (the root is the script one as "myapp")
PRESET_NAMES = sorted(p.name for p in PRESETS.iterdir() if (p / "preset.toml").is_file())

needs_uv = pytest.mark.skipif(shutil.which("uv") is None and not os.environ.get("UV"), reason="uv not found")


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg, set(cli.COMMANDS))
    return cfg


def preset_config(name: str) -> Config:
    text = (PRESETS / name / "files" / "pytemplate.toml").read_text(encoding="utf-8")
    return make(tomllib.loads(text.replace("{{name}}", "myapp").replace("{{pkg}}", "myapp")))


def configs() -> dict[str, Config]:
    """Every preset plus the modes that used to involve the JIT (PyPy supported, mypyc active)."""
    out = {name: preset_config(name) for name in PRESET_NAMES}
    out["pypy-supported"] = make({"backend": {"supported": ["cpython", "pypy", "mypyc"]}})
    out["mypyc-active"] = make({"backend": {"active": "mypyc"}})
    out["cpython-only"] = make({"backend": {"supported": ["cpython"]}})
    return out


@pytest.fixture
def cli_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """cli.main sets these globals from the global flags: restore them after each test."""
    monkeypatch.setattr(proc, "DRY_RUN", False)
    monkeypatch.setattr(ui, "VERBOSE", False)
    monkeypatch.setattr(ui, "QUIET", False)
    monkeypatch.setitem(cli._OPTS, "no_render", False)


def child_env(**extra: str) -> dict[str, str]:
    """A ./pyt child: no UV/VIRTUAL_ENV/UV_PROJECT_ENVIRONMENT/UV_PYTHON/PYTEMPLATE_*, no git config.

    The uv that runs this suite stays first on PATH, so the child uses the same one.
    """
    drop = {"UV", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON"}
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith(("PYTEMPLATE_", "GIT_"))}
    uv = os.environ.get("UV")
    if uv:
        env["PATH"] = os.pathsep.join([str(Path(uv).parent), env.get("PATH", "")])
    # git maps "/dev/null" to NUL on Windows too
    env.update(NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    env.update(extra)
    return env


def pyt(root: Path, *args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(root / ".pytemplate" / "pyt.py"), *args],
        cwd=cwd or root,
        env=env or child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def snapshot(root: Path, *, eol: bool = True) -> dict[str, str]:
    """Every file (sha256) under root, outside caches: any write shows up as a difference.

    eol=False compares modulo CRLF/LF: a Windows checkout (core.autocrlf=true) has CRLF files,
    which the runner rewrites with LF (the same content: CLAUDE.md section 14).
    """
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes() if eol else p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts
    }


# --- CPython JIT: the keys are gone -------------------------------------------------------------


@pytest.mark.parametrize(("key", "value"), [("jit", True), ("jit", False), ("jit_interpreter", "/usr/bin/python3.14")])
def test_jit_keys_are_unknown_keys(key: str, value: object) -> None:
    with pytest.raises(PytError) as e:
        make({"python": {key: value}})
    assert f"unknown key 'python.{key}'" in str(e.value)
    assert "(valid: cpython, pypy)" in str(e.value)  # the loader's normal message, no special case
    assert e.value.code == 2


def test_a_pytemplate_toml_that_still_has_jit_fails_to_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_state: None, capsys: pytest.CaptureFixture[str]) -> None:
    """A project whose pytemplate.toml kept `jit = false` fails on every command, like any typo."""
    text = (ROOT / "pytemplate.toml").read_text(encoding="utf-8")
    assert "\njit" not in text
    old = text.replace('pypy = "pypy@3.11.15"', 'pypy = "pypy@3.11.15"\njit = false             # old key', 1)
    assert old != text
    path = tmp_path / "pytemplate.toml"
    path.write_text(old, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    with pytest.raises(PytError, match=r"unknown key 'python\.jit'") as e:
        config.load(set(cli.COMMANDS))
    assert e.value.code == 2
    assert cli.main(["mode"]) == 2
    assert "unknown key 'python.jit'" in capsys.readouterr().err


@pytest.mark.parametrize("path", ["pytemplate.toml", *(f".pytemplate/presets/{p}/files/pytemplate.toml" for p in PRESET_NAMES)])
def test_no_pytemplate_toml_mentions_the_jit_keys(path: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    data = tomllib.loads(text.replace("{{name}}", "myapp").replace("{{pkg}}", "myapp"))
    assert set(data["python"]) <= {"cpython", "pypy"}
    assert not re.search(r"(?m)^\s*jit(_interpreter)?\s*=", text)
    assert "python.org" not in text and "PYTHON_JIT" not in text
    assert "./pyt init" not in text


@pytest.mark.parametrize("args", [["--jit", "on"], ["--jit", "off"], ["mypyc", "--jit=on"], ["--jit=off"]])
def test_mode_has_no_jit_option(args: list[str], monkeypatch: pytest.MonkeyPatch, cli_state: None, capsys: pytest.CaptureFixture[str]) -> None:
    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("mode must not write pytemplate.toml")

    monkeypatch.setattr(config, "update_file", never)
    # "--jit on": argparse reads "on" as the BACKEND (invalid choice); "--jit=on": unknown argument
    assert cli.main(["--no-render", "mode", *args]) == 2
    err = capsys.readouterr().err
    assert "unknown argument(s): --jit" in err or "invalid choice" in err, err


def test_mode_and_doctor_texts_do_not_mention_the_jit(cli_state: None, capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("mode", "doctor"):
        command = cli.COMMANDS[name]
        assert "jit" not in f"{command.usage} {command.summary}".lower(), name
    assert cmd_mode.cmd_mode(make({}), []) == 0  # no arguments: describe the mode
    assert "jit" not in capsys.readouterr().err.lower()


def test_envs_have_no_jit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not hasattr(envs, "jit_env") and not hasattr(envs, "find_jit_interpreter")
    for cfg in configs().values():
        for backend in cfg.backend.supported:
            env = envs.runtime_env(cfg, backend)
            expected = envs.pypy_env(cfg) if backend == "pypy" else envs.cpython_env(cfg)
            assert env == expected and env.dir.name in (f".venv{ENV_SUFFIX}", f".venv-pypy{ENV_SUFFIX}")
            assert env.preference == "only-managed"
    # The runner no longer sets PYTHON_JIT: the user's own value (if any) passes through untouched
    env = envs.cpython_env(make({}))
    monkeypatch.delenv("PYTHON_JIT", raising=False)
    assert "PYTHON_JIT" not in envs.env_vars(env)
    monkeypatch.setenv("PYTHON_JIT", "1")
    assert envs.env_vars(env)["PYTHON_JIT"] == "1"


def test_generated_launchers_do_not_set_python_jit(tmp_path: Path) -> None:
    for cfg in configs().values():
        for backend in ("cpython", "pypy"):
            texts = [
                pyz._wrapper_cmd(cfg, backend, "myapp.pyz"),
                portable.cmd_launcher(cfg, backend, tmp_path, None),  # runtime = "system"
                portable.sh_launcher(cfg, backend, tmp_path, None),
                portable.cmd_launcher(cfg, backend, tmp_path, tmp_path / "runtime" / "python.exe"),  # bundled
                portable.sh_launcher(cfg, backend, tmp_path, tmp_path / "runtime" / "bin" / "python3"),
            ]
            for text in texts:
                assert "PYTHON_JIT" not in text and "jit" not in text.lower(), text
                assert "PYTHONUTF8=1" in text  # the env lines are still there


def test_generated_files_never_mention_the_jit() -> None:
    """launch.json, tasks.json, editor.json, ci.yml...: no JIT config, no .venv-jit, no PYTHON_JIT."""
    for name, cfg in configs().items():
        for path, content in render.outputs(cfg).items():
            assert "jit" not in content.lower(), f"{name}: {path}"
        launch = vscode.launch(cfg)
        assert not any("JIT" in c["name"] for c in launch["configurations"])
        stages = [c for c in launch["configurations"] if c["name"].startswith("Run mypyc stage")]
        for stage in stages:
            assert stage["python"] == "${workspaceFolder}/.venv/bin/python"
            assert stage["windows"] == {"python": "${workspaceFolder}/.venv/Scripts/python.exe"}
            assert stage["env"] == {"PYTHONUTF8": "1", "PYTEMPLATE_BACKEND": "mypyc"}
        data = nvim.editor_data(cfg, cfg.profile_for())
        assert data["envs"] == {"tools": ".venv", "cpython": ".venv", "mypyc": ".venv", "pypy": ".venv-pypy"}


def test_committed_generated_files_have_no_jit() -> None:
    for path in (".pytemplate/editor.json", ".vscode/launch.json", ".vscode/tasks.json", ".github/workflows/ci.yml"):
        file = ROOT / path
        if file.is_file():
            assert "jit" not in file.read_text(encoding="utf-8").lower(), path


# --- init: not a public command ------------------------------------------------------------------


def test_init_is_not_listed_anywhere(capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("init", "__init"):
        assert name not in cli.COMMANDS
    assert cli.cmd_help(None, []) == 0
    listed = capsys.readouterr().out
    assert not re.search(r"(?m)^\s+_*init\b", listed), listed
    assert "./pyt init" not in listed
    # editor.json (VS Code/Neovim task lists and pickers), VS Code's tasks
    cfg = make({})
    assert {"init", "__init"}.isdisjoint(c["name"] for c in nvim.commands())
    labels = [t["label"] for t in vscode.tasks(cfg)["tasks"]]
    assert not any(re.fullmatch(r"pyt: _*init\b.*", label) for label in labels), labels
    committed = json.loads((ROOT / ".pytemplate" / "editor.json").read_text(encoding="utf-8"))
    assert {"init", "__init"}.isdisjoint(c["name"] for c in committed["commands"])
    # The Neovim plugin's per-command metadata (refresh after it, open its output) went too
    lua = (TEMPLATE / "nvim" / "lua" / "pytemplate" / "tasks.lua").read_text(encoding="utf-8")
    meta = lua.split("M.META = {", 1)[1].split("\n}\n", 1)[0]
    assert not re.search(r"(?m)^\s*_*init\s*=", meta)


def test_the_internal_route_is_the_old_init(capsys: pytest.CaptureFixture[str]) -> None:
    command = cli.INTERNAL["__init"]
    func = getattr(importlib.import_module(f"runner.{command.module}"), command.func)
    assert func is cmd_mode.cmd_init
    # unlike the old public command it never renders first: it renders with --force at its end, and
    # rendering the copy first made `new` warn about the source project's hand-edited files
    assert not command.render
    assert set(cli.INTERNAL).isdisjoint(cli.COMMANDS)
    # A [tasks] name can never shadow an internal route: task names start with a letter
    for name in cli.INTERNAL:
        with pytest.raises(PytError, match="invalid task name"):
            make({"tasks": {name: {"cmd": ["x"]}}})
    # `help __init` does not document it (internal: like any unknown name, exit 2), and
    # `__init -h` is argparse's help
    with pytest.raises(PytError, match="unknown command: __init"):
        cli.cmd_help(None, ["__init"])
    assert "__init" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cmd_mode.cmd_init(make({}), ["-h"])
    assert e.value.code == 0
    assert "./pyt __init" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["init"],
        ["init", "flet"],
        ["init", "script", "--name", "other", "--force"],
        ["--dry-run", "init", "raylib"],
        ["-q", "init", "--help"],
    ],
)
def test_pyt_init_exits_2_with_a_hint(argv: list[str], monkeypatch: pytest.MonkeyPatch, cli_state: None, capsys: pytest.CaptureFixture[str]) -> None:
    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("init must not run")

    monkeypatch.setattr(presets, "init", never)
    monkeypatch.setattr(cmd_mode, "cmd_init", never)
    monkeypatch.setattr(render, "auto", never)  # refused before the generated files are touched
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "init is no longer a ./pyt command" in err
    assert HINT in err
    assert "unknown command" not in err


def test_a_task_named_init_is_allowed(monkeypatch: pytest.MonkeyPatch, cli_state: None) -> None:
    """`init` is no longer reserved: a [tasks] entry of that name runs instead of the hint."""
    cfg = make({"tasks": {"init": {"cmd": ["python", "-c", "pass"]}}})  # validated with COMMANDS
    ran: list[tuple[str, list[str]]] = []

    def run_task(_cfg: Config, name: str, args: list[str], _dispatch: object) -> int:
        ran.append((name, args))
        return 0

    monkeypatch.setattr(config, "load", lambda *_a: cfg)
    monkeypatch.setattr(tasks, "run_task", run_task)
    monkeypatch.setitem(cli._OPTS, "no_render", True)
    assert cli.dispatch(["init", "a"]) == 0
    assert ran == [("init", ["a"])]


def test_new_runs_the_internal_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """presets.new runs `pyt.py --no-render __init PRESET --name N --force` in the copy: a routed name."""
    calls: list[list[str]] = []

    def fake_run(argv: list[Any], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(presets, "copy_template", lambda dest: dest.mkdir(parents=True))
    monkeypatch.setattr(presets.proc, "run", fake_run)
    monkeypatch.setattr(presets.proc, "find_uv", lambda: "uv")
    monkeypatch.setattr(presets.shutil, "which", lambda _name: None)  # no git step
    dest = tmp_path / "demo"
    presets.new(dest, "raylib", None)
    (argv,) = calls
    assert argv[:4] == ["uv", "run", "--quiet", "--script"]
    assert Path(argv[4]) == dest.resolve() / ".pytemplate" / "pyt.py"
    assert argv[5:] == ["--no-render", "__init", "raylib", "--name", "demo", "--force"]  # init renders itself
    command = cli.INTERNAL[argv[6]]
    assert getattr(importlib.import_module(f"runner.{command.module}"), command.func) is cmd_mode.cmd_init


def test_new_dry_run_names_the_internal_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(proc, "DRY_RUN", True)
    monkeypatch.delenv("PYTEMPLATE_CALLER_CWD", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cmd_mode.cmd_new(config.load(set(cli.COMMANDS)), ["p1", "--preset", "flet", "--name", "demo"]) == 0
    err = capsys.readouterr().err
    assert "./pyt __init flet --name demo --force" in err
    assert "./pyt init" not in err
    assert not (tmp_path / "p1").exists()


def test_preset_hint_in_validate_names_new() -> None:
    with pytest.raises(PytError) as e:
        make({"app": {"preset": "nope"}})
    assert HINT in str(e.value) and "./pyt init" not in str(e.value)
    assert e.value.code == 2


# --- shell-setup: gone (`pyt install` puts the launchers themselves on PATH) ----------------------


def test_shell_setup_is_listed_nowhere() -> None:
    assert "shell-setup" not in cli.COMMANDS and "shell-setup" not in cli.INTERNAL
    # its snippets and their helpers went with it (selftest --shells, __probe and doctor stay)
    for name in ("cmd_shell_setup", "snippet", "guess_shell", "xonsh_snippet", "completion_words", "SETUP_SHELLS"):
        assert not hasattr(shells, name), f"shells.{name} is left over from shell-setup"
    # editor.json (VS Code/Neovim task lists and pickers), VS Code's tasks, the plugin's metadata
    assert "shell-setup" not in {c["name"] for c in nvim.commands()}
    committed = json.loads((ROOT / ".pytemplate" / "editor.json").read_text(encoding="utf-8"))
    assert "shell-setup" not in {c["name"] for c in committed["commands"]}
    labels = [t["label"] for t in vscode.tasks(make({}))["tasks"]]
    assert not any("shell-setup" in label for label in labels), labels
    assert "shell-setup" not in (TEMPLATE / "nvim" / "lua" / "pytemplate" / "tasks.lua").read_text(encoding="utf-8")


@pytest.mark.parametrize("outside", [False, True], ids=["project", "outside"])
def test_shell_setup_says_what_replaced_it(outside: bool, monkeypatch: pytest.MonkeyPatch, cli_state: None, capsys: pytest.CaptureFixture[str]) -> None:
    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("nothing may run or render")

    monkeypatch.setattr(project, "GLOBAL", outside)
    monkeypatch.setattr(config, "load", lambda *_a: make({}))
    monkeypatch.setattr(render, "auto", never)
    monkeypatch.setattr(tasks, "run_task", never)
    assert cli.main(["shell-setup", "bash"]) == 2
    prog = "pyt" if outside else "./pyt"
    assert f"shell-setup is no longer a {prog} command: `pyt install` puts the launchers themselves on PATH" in capsys.readouterr().err


# --- the real thing, in throwaway copies ---------------------------------------------------------


@needs_uv
def test_new_creates_a_project_through_the_internal_route(tmp_path: Path) -> None:
    dest = tmp_path / "demo"
    r = pyt(ROOT, "new", str(dest), "--preset", "script", cwd=tmp_path, env=child_env(GIT_CEILING_DIRECTORIES=str(tmp_path)))
    if r.returncode != 0 and any(marker in r.stderr for marker in rename.PYPI_UNREACHABLE):
        pytest.skip("needs PyPI: `new` adds the preset's requirements with uv")
    assert r.returncode == 0, r.stderr
    assert re.search(r"pyt\.py --no-render __init script --name demo --force", r.stderr), r.stderr
    assert (dest / "src" / "demo" / "__init__.py").is_file() and not (dest / "src" / "myapp").exists()
    text = (dest / "pytemplate.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["app"]["name"] == "demo"
    assert not (dest / ".pytemplate" / "template-repo").exists()
    if shutil.which("git"):
        assert (dest / ".git").is_dir()
    check = pyt(dest, "render", "--check")
    assert check.returncode == 0, check.stderr
    # The new project's own runner refuses the old command with the same hint
    before = snapshot(dest)
    refused = pyt(dest, "init", "flet")
    assert refused.returncode == 2 and HINT in refused.stderr, refused.stderr
    assert snapshot(dest) == before


@needs_uv
@pytest.mark.skipif(not (TEMPLATE / "template-repo").is_file(), reason="about the template repository (a project made with ./pyt new has its own name)")
def test_maintainer_route_regenerates_the_root_pristine(tmp_path: Path) -> None:
    """The template root IS the script preset as "myapp": regenerating it changes no byte."""
    root_cfg = config.load(set(cli.COMMANDS))
    assert root_cfg.app.preset == "script" and root_cfg.app.name == "myapp"
    assert presets.pristine(root_cfg)
    copy = tmp_path / "t"
    copy.mkdir()
    presets.copy_template(copy)
    before = snapshot(copy)

    dry = pyt(copy, "--dry-run", "__init", "script", "--name", "myapp")
    assert dry.returncode == 0, dry.stderr
    marks = [ln for ln in dry.stderr.splitlines() if re.match(r"\s{4}[-+~] ", ln)]
    assert "identical):" in dry.stderr and not marks, dry.stderr
    assert snapshot(copy) == before

    before = snapshot(copy, eol=False)
    real = pyt(copy, "__init", "script", "--name", "myapp", "--force")
    assert real.returncode == 0, real.stderr
    after = snapshot(copy, eol=False)
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert not changed, changed

    # Without --force it still refuses an edited src/, and the hint names the internal route
    app = copy / "src" / "myapp" / "app.py"
    app.write_text(app.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    refused = pyt(copy, "__init", "script")
    assert refused.returncode == 2, refused.stderr
    assert "__init script --force" in refused.stderr and "./pyt init" not in refused.stderr
