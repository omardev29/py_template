"""`./deploy apply` / `./deploy setup` (runner/cmd_apply.py): bring the project in line with
pytemplate.toml.

Most tests build a throwaway project in tmp_path (a preset skeleton plus the pyproject.toml
`./deploy new` would write) and run the runner in-process with a fake uv that edits
pyproject.toml exactly like `uv add/remove --frozen` do. A few run the real `./deploy` in a
throwaway copy of the template; the one that re-locks skips when the package index cannot be
reached, and `test_uv_frozen_edits_only_pyproject` checks, offline, the uv behaviour the fake
imitates.

The matrix: after editing each key of pytemplate.toml, what apply does.
  app.name                  the rename flow (src/<pkg>/, imports, pytemplate.toml, pyproject.toml)
  app.preset                refused (exit 2): ./deploy new DIR --preset P
  [preset.<name>]           uv remove/add --frozen of the option-driven requirements + one uv lock
  backend.supported/python  managed pyproject parts + uv lock (+ the PyPy 3.11 precheck when new)
  hooks.pre_commit          hook installed (true) / pytemplate's own hook removed (false)
  anything else             the generated files (render.apply), as every command does
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEMPLATE_DIR))

from runner import cli, cmd_apply, cmd_dev, cmd_env, config, envs, hooks, presets, proc, render, rename  # noqa: E402
from runner import project as project_module  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import ROOT  # noqa: E402
from runner.ui import DeployError  # noqa: E402

needs_uv = pytest.mark.skipif(shutil.which("uv") is None and not os.environ.get("UV"), reason="uv not found")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")

FLET_DEPS = ["flet==1.0.1", "flet-desktop==1.0.1"]
FLET_DEV = ["flet-cli==1.0.1"]
CHANGING = ("add", "remove", "lock", "sync")


# --- a throwaway project -------------------------------------------------------------------------


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _set_array(text: str, key: str, values: Sequence[str]) -> str:
    """Rewrite the multi-line array `key = [...]` that starts a line (the pyproject layout)."""
    body = "".join(f'    "{v}",\n' for v in values)
    new, count = re.subn(rf"(?ms)^({re.escape(key)} = \[\n).*?^\]", lambda m: m.group(1) + body + "]", text, count=1)
    assert count == 1, key
    return new


def _cfg_text(text: str) -> Config:
    cfg: Config = config._build(Config, tomllib.loads(text), "")
    config.validate(cfg, set(cli.COMMANDS))
    return cfg


def _pyproject(preset: str, name: str) -> str:
    """pyproject.toml as `./deploy new NAME --preset PRESET` writes it (managed block included)."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8").replace("\r\n", "\n")
    data = tomllib.loads(text)
    deps = [d for d in data["project"]["dependencies"] if cmd_apply.req_key(d)[0] != "rich"]
    dev = list(data["dependency-groups"]["dev"])
    if preset == "script":
        deps.append("rich>=15.0.0")
    elif preset == "raylib":
        deps.append("raylib==6.0.1.0")
        dev.append("types-cffi>=2.1.0.20260827")  # uv writes a lower bound for the unpinned preset entry
    elif preset == "flet":
        deps += FLET_DEPS
        dev += FLET_DEV
    text = _set_array(_set_array(text, "dependencies", sorted(deps)), "dev", sorted(dev))
    extra = str(presets.load(preset).get("pyproject", "")).replace("{{name}}", name).replace("{{pkg}}", rename.package_of(name))
    text = presets._set_extra_tables(presets._set_project_name(text, name), extra)
    return render.pyproject_expected(_cfg_text(presets.skeleton(preset, name)["pytemplate.toml"].decode("utf-8")), text)


class Project:
    """A project in tmp_path that the runner modules point at."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, preset: str, name: str) -> None:
        self.root = root
        for rel_path, data in presets.skeleton(preset, name).items():
            path = root / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (root / "pyproject.toml").write_text(_pyproject(preset, name), encoding="utf-8", newline="\n")
        (root / ".pytemplate").mkdir(exist_ok=True)
        for module in (cmd_apply, rename, render, presets, envs, project_module):
            monkeypatch.setattr(module, "ROOT", root)
        for module in (render, presets):
            monkeypatch.setattr(module, "PYPROJECT", root / "pyproject.toml")
        monkeypatch.setattr(render, "STATE_FILE", root / ".pytemplate" / "state.json")
        monkeypatch.setattr(config, "CONFIG_FILE", root / "pytemplate.toml")

    @property
    def config_file(self) -> Path:
        return self.root / "pytemplate.toml"

    def edit(self, table: str, key: str, value: Any) -> None:
        """A hand edit of pytemplate.toml (what the user does in an editor)."""
        text = config.set_value(self.config_file.read_text(encoding="utf-8"), table, key, value)
        self.config_file.write_text(text, encoding="utf-8", newline="\n")

    def cfg(self) -> Config:
        return _cfg_text(self.config_file.read_text(encoding="utf-8"))

    def pyproject(self) -> dict[str, Any]:
        return tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))

    def snapshot(self) -> dict[str, str]:
        return {
            p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<dir>"
            for p in sorted(self.root.rglob("*"))
            if "__pycache__" not in p.parts
        }


class FakeUv:
    """envs.uv: records every call; `add/remove --frozen` edit pyproject.toml the way uv does
    (normalized names, a requirement replaced in place, a missing one refused), `lock --check`
    compares with the last `lock`, and --dry-run skips the echoed calls like proc.run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []
        self.fail: set[str] = set()  # verbs that fail (exit 1)
        self.locked = (root / "pyproject.toml").read_bytes()

    def __call__(
        self,
        env: envs.PyEnv,
        args: Sequence[str | Path],
        *,
        cwd: Path | None = None,
        extra_env: Any = None,
        check: bool = True,
        capture: bool = False,
        echo: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in args]
        self.calls.append(argv)
        if proc.DRY_RUN and echo:
            return subprocess.CompletedProcess(["uv", *argv], 0, "", "")
        code = 0
        pyproject = self.root / "pyproject.toml"
        if argv[:2] == ["lock", "--check"]:
            code = 1 if "lock" in self.fail or pyproject.read_bytes() != self.locked else 0
        elif argv[0] in self.fail:
            code = 1
        elif argv[0] in ("add", "remove"):
            self._edit(argv)
        elif argv[0] == "lock":
            self.locked = pyproject.read_bytes()
            (self.root / "uv.lock").write_text(f"# locked {hashlib.sha256(self.locked).hexdigest()}\n", encoding="utf-8")
        if check and code:
            raise proc.CommandFailed(["uv", *argv], code)
        return subprocess.CompletedProcess(["uv", *argv], code, "", "")

    def _edit(self, argv: list[str]) -> None:
        dev = "--dev" in argv
        path = self.root / "pyproject.toml"
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        current: list[str] = list(data["dependency-groups"]["dev"] if dev else data["project"]["dependencies"])
        for item in (a for a in argv[1:] if not a.startswith("--")):
            name, spec = cmd_apply.req_key(item)
            at = next((i for i, r in enumerate(current) if cmd_apply.req_key(r)[0] == name), None)
            if argv[0] == "remove":
                assert at is not None, f"uv: the dependency {name} could not be found"
                del current[at]
            elif at is None:
                current.append(name + spec)
            else:
                current[at] = name + spec
        path.write_text(_set_array(text, "dev" if dev else "dependencies", current), encoding="utf-8", newline="\n")

    def changing(self, since: int = 0) -> list[list[str]]:
        """The calls that change something (not the read-only `lock --check`)."""
        return [c for c in self.calls[since:] if c[0] in CHANGING and c[:2] != ["lock", "--check"]]


@pytest.fixture(autouse=True)
def _guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing here may touch the real repository: its git hook, its index or its .build/."""
    real_install, real_uninstall = hooks.install, hooks.uninstall

    def inside(repo: hooks.Repo) -> None:
        assert repo.project.resolve().is_relative_to(tmp_path.resolve()), f"a test touched the hook of {repo.project}"

    def install(repo: hooks.Repo, *, force: bool = False) -> str:
        inside(repo)
        return real_install(repo, force=force)

    def uninstall(repo: hooks.Repo) -> str:
        inside(repo)
        return real_uninstall(repo)

    def profile_file(cfg: Config, profile: str, kind: str) -> Path:
        out = tmp_path / "cfg" / f"{kind}-{profile}.toml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("", encoding="utf-8")
        return out

    monkeypatch.setattr(hooks, "install", install)
    monkeypatch.setattr(hooks, "uninstall", uninstall)
    monkeypatch.setattr(cmd_env, "_fix_exec_bit", lambda: None)
    monkeypatch.setattr(cmd_dev, "_profile_file", profile_file)
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(proc, "DRY_RUN", False)


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str = "script", name: str = "alpha") -> tuple[Project, FakeUv]:
    root = tmp_path / "p"
    root.mkdir()
    project = Project(root, monkeypatch, preset, name)
    uv = FakeUv(root)
    monkeypatch.setattr(envs, "uv", uv)
    return project, uv


def _run(project: Project, *args: str, command: str = "apply") -> int:
    return cmd_apply.apply(project.cfg(), list(args), command=command)


# --- requirements ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requirement", "key"),
    [
        ("flet==1.0.1", ("flet", "==1.0.1")),
        ("Flet == 1.0.1", ("flet", "==1.0.1")),
        ("raylib_sdl==6.0.1.0", ("raylib-sdl", "==6.0.1.0")),
        ("Raylib.Software >= 6 , < 7", ("raylib-software", ">=6,<7")),
        ("flet[all]==1.0.1; sys_platform != 'emscripten'", ("flet", "==1.0.1")),
        ("types-cffi", ("types-cffi", "")),
        ("  rich>=15.0.0  ", ("rich", ">=15.0.0")),
    ],
)
def test_req_key_normalizes_like_uv(requirement: str, key: tuple[str, str]) -> None:
    assert cmd_apply.req_key(requirement) == key


def test_option_dependencies_are_only_the_templated_ones() -> None:
    assert presets.option_dependencies("script", presets.default_options("script")) == ([], [])
    assert presets.option_dependencies("raylib", presets.default_options("raylib")) == (["raylib==6.0.1.0"], [])
    assert presets.option_dependencies("raylib", {"package": "raylib_sdl", "version": "6.0.1.0"}) == (["raylib_sdl==6.0.1.0"], [])
    assert presets.option_dependencies("flet", {"version": "1.0.0"}) == (["flet==1.0.0", "flet-desktop==1.0.0"], ["flet-cli==1.0.0"])
    with pytest.raises(DeployError, match=r"cannot format") as e:
        presets.option_dependencies("flet", {})
    assert e.value.code == 2


def test_read_project_tolerates_a_bom_and_reports_broken_toml(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_bytes(b'\xef\xbb\xbf[project]\nname = "x"\ndependencies = ["Rich>=13"]\n[dependency-groups]\ndev = ["pytest"]\n')
    project = cmd_apply.read_project(path)
    assert (project.name, project.deps, project.dev) == ("x", {"rich": "Rich>=13"}, {"pytest": "pytest"})
    assert not project.pypy_locked
    path.write_text("[project\n", encoding="utf-8")
    with pytest.raises(DeployError, match="not valid TOML") as e:
        cmd_apply.read_project(path)
    assert e.value.code == 2
    with pytest.raises(DeployError, match="cannot be read"):
        cmd_apply.read_project(tmp_path / "missing.toml")
    path.write_text("[tool.uv]\nenvironments = [\"implementation_name == 'pypy' and x\"]\n", encoding="utf-8")
    assert cmd_apply.read_project(path).pypy_locked
    assert cmd_apply.read_project(path).name is None
    path.write_text('project = 1\ndependency-groups = "x"\n[tool]\nuv = 2\n', encoding="utf-8")  # wrong shapes: no crash
    project = cmd_apply.read_project(path)
    assert (project.name, project.deps, project.dev, project.pypy_locked) == (None, {}, {}, False)


# --- the record -----------------------------------------------------------------------------------


RECORD = {"name": "alpha", "preset": "raylib", "dependencies": ["raylib_sdl==6.0.1.0"], "dev": []}


def test_record_round_trip_keeps_the_other_keys(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    files = {"comment": "x", "files": {".mypy.ini": "abc"}, "future": [1, 2]}
    state.write_text(json.dumps(files), encoding="utf-8")
    assert cmd_apply.save_record(RECORD, state) is True
    assert json.loads(state.read_text(encoding="utf-8")) == {**files, "applied": RECORD}
    before = state.read_bytes()
    assert cmd_apply.save_record(RECORD, state) is False  # unchanged: not even rewritten
    assert state.read_bytes() == before
    assert cmd_apply.load_record(state) == RECORD


def test_record_is_never_written_under_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert cmd_apply.save_record(RECORD, state) is True
    assert not state.exists()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        '{"applied": []}',
        '{"applied": {"name": 1, "preset": "script"}}',
        '{"applied": {"name": "a", "preset": "script", "dependencies": "x"}}',
        '{"applied": {"name": "a", "preset": "script", "dev": [1]}}',
        '{"applied": {"preset": "script"}}',
    ],
)
def test_a_malformed_record_is_ignored_and_replaced(tmp_path: Path, content: str) -> None:
    state = tmp_path / "state.json"
    state.write_text(content, encoding="utf-8")
    assert cmd_apply.load_record(state) is None
    assert cmd_apply.save_record(RECORD, state) is True
    assert cmd_apply.load_record(state) == RECORD


def test_record_with_a_bom_is_read(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_bytes(b"\xef\xbb\xbf" + json.dumps({"applied": RECORD}).encode("utf-8"))
    assert cmd_apply.load_record(state) == RECORD


def test_rename_record_changes_only_the_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmd_apply, "ROOT", tmp_path)
    (tmp_path / ".pytemplate").mkdir()
    cmd_apply.rename_record("beta", None)  # no record: rename does not invent one
    assert not (tmp_path / ".pytemplate" / "state.json").exists()
    cmd_apply.rename_record("beta", dict(RECORD))
    assert cmd_apply.load_record() == {**RECORD, "name": "beta"}


# --- which preset the project was made with ------------------------------------------------------------


def _cfg(preset: str, **options: str) -> Config:
    cfg = _cfg_text(presets.skeleton(preset, "alpha")["pytemplate.toml"].decode("utf-8"))
    if options:
        cfg.preset[preset] = dict(options)
    return cfg


def _declared(deps: list[str], dev: list[str] | None = None) -> cmd_apply.Project:
    return cmd_apply.Project({}, "alpha", {cmd_apply.req_key(r)[0]: r for r in deps}, {cmd_apply.req_key(r)[0]: r for r in dev or []})


@pytest.mark.parametrize(
    ("preset", "options", "deps", "dev", "record", "expected"),
    [
        ("script", {}, ["rich>=15.0.0"], [], None, "script"),
        ("flet", {}, FLET_DEPS, FLET_DEV, None, "flet"),
        ("raylib", {}, ["raylib==6.0.1.0"], ["types-cffi>=1"], None, "raylib"),
        # hand switch: the project still declares the old preset's dependencies
        ("flet", {}, ["rich>=15.0.0"], [], None, "script"),
        ("script", {}, FLET_DEPS, FLET_DEV, None, "flet"),
        ("raylib", {}, FLET_DEPS, FLET_DEV, None, "flet"),
        ("flet", {}, ["raylib==6.0.1.0"], [], None, "raylib"),
        ("flet", {}, ["rich>=15.0.0"], [], {"preset": "script"}, "script"),
        # an option change is not a preset change
        ("raylib", {"package": "raylib_sdl"}, ["raylib==6.0.1.0"], [], None, "raylib"),
        ("raylib", {"package": "raylib_software"}, ["raylib-sdl==6.0.1.0"], [], {"preset": "raylib", "dependencies": ["raylib_sdl==6.0.1.0"]}, "raylib"),
        ("flet", {"version": "1.0.0"}, FLET_DEPS, FLET_DEV, None, "flet"),
        # the dependencies were removed by hand: the record says it is still that preset
        ("flet", {}, [], [], {"preset": "flet"}, "flet"),
        # a new project copies the template's record (script): the dependencies win
        ("flet", {}, FLET_DEPS, FLET_DEV, {"preset": "script"}, "flet"),
        ("raylib", {}, ["raylib==6.0.1.0"], [], {"preset": "script"}, "raylib"),
        ("script", {}, ["rich>=15.0.0"], [], {"preset": "no-such-preset"}, "script"),
    ],
)
def test_applied_preset(preset: str, options: dict[str, str], deps: list[str], dev: list[str], record: dict[str, Any] | None, expected: str) -> None:
    full = None if record is None else {"name": "alpha", "dependencies": [], "dev": [], **record}
    assert cmd_apply._applied_preset(_cfg(preset, **options), _declared(deps, dev), full) == expected


# --- which requirements change ----------------------------------------------------------------------


def _applied(preset: str, deps: list[str] | None = None, dev: list[str] | None = None) -> cmd_apply.Applied:
    base = presets.option_dependencies(preset, presets.default_options(preset))
    return cmd_apply.Applied(preset, base[0] if deps is None else deps, base[1] if dev is None else dev, None, None)


def test_flet_version_change_replaces_the_three_pins() -> None:
    changes = cmd_apply.dependency_changes(_cfg("flet", version="1.0.0"), _applied("flet"), _declared(FLET_DEPS, FLET_DEV))
    assert (changes.remove, changes.remove_dev) == ([], [])
    assert changes.add == ["flet==1.0.0", "flet-desktop==1.0.0"]
    assert changes.add_dev == ["flet-cli==1.0.0"]
    assert changes.describe() == "add flet==1.0.0, flet-desktop==1.0.0, flet-cli==1.0.0 (dev)"


def test_raylib_package_switch_removes_the_old_package() -> None:
    changes = cmd_apply.dependency_changes(_cfg("raylib", package="raylib_sdl"), _applied("raylib"), _declared(["raylib==6.0.1.0"], ["types-cffi>=1"]))
    assert (changes.remove, changes.add, changes.remove_dev, changes.add_dev) == (["raylib"], ["raylib_sdl==6.0.1.0"], [], [])
    assert changes.describe() == "remove raylib; add raylib_sdl==6.0.1.0"
    # the next switch: uv wrote raylib-sdl, the record knows it came from the options
    changes = cmd_apply.dependency_changes(
        _cfg("raylib", package="raylib_software"),
        _applied("raylib", ["raylib_sdl==6.0.1.0"]),
        _declared(["raylib-sdl==6.0.1.0"]),
    )
    assert (changes.remove, changes.add) == (["raylib-sdl"], ["raylib_software==6.0.1.0"])


@pytest.mark.parametrize(
    ("preset", "deps", "dev"),
    [
        ("flet", ["Flet == 1.0.1", "flet_desktop==1.0.1; sys_platform != 'emscripten'"], ["flet-cli[extra]==1.0.1"]),
        ("raylib", ["raylib==6.0.1.0"], ["types-cffi>=2.1.0.20260827"]),  # unpinned preset entry: the user's
        ("raylib", ["raylib==6.0.1.0"], []),  # types-cffi removed by the user: not added back
        ("script", ["rich>=16"], []),  # plain preset dependencies belong to the project
        ("script", [], []),
    ],
)
def test_nothing_changes_when_the_pins_match(preset: str, deps: list[str], dev: list[str]) -> None:
    changes = cmd_apply.dependency_changes(_cfg(preset), _applied(preset), _declared(deps, dev))
    assert not changes and changes.describe() == "none"


def test_a_removed_option_dependency_comes_back() -> None:
    changes = cmd_apply.dependency_changes(_cfg("flet"), _applied("flet"), _declared(["flet==1.0.1"], []))
    assert (changes.add, changes.add_dev, changes.remove) == (["flet-desktop==1.0.1"], ["flet-cli==1.0.1"], [])


def test_the_old_name_is_removed_only_when_the_options_produced_it() -> None:
    # raylib added by the user next to raylib_sdl from the options: never touched
    changes = cmd_apply.dependency_changes(
        _cfg("raylib", package="raylib_software"), _applied("raylib", ["raylib_sdl==6.0.1.0"]), _declared(["raylib==6.0.1.0", "raylib-sdl==6.0.1.0"])
    )
    assert changes.remove == ["raylib-sdl"]


# --- apply, in-process ---------------------------------------------------------------------------------


def test_apply_flet_version_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, uv = _project(tmp_path, monkeypatch, "flet")
    project.edit("preset.flet", "version", "1.0.0")
    assert _run(project) == 0
    assert uv.changing()[:3] == [
        ["add", "--frozen", "flet==1.0.0", "flet-desktop==1.0.0"],
        ["add", "--frozen", "--dev", "flet-cli==1.0.0"],
        ["lock"],
    ]
    assert not any(c[0] in ("add", "remove") and "--frozen" not in c for c in uv.calls)  # --no-sync cannot bump flet
    assert ["sync", "--locked"] in uv.calls
    data = project.pyproject()
    assert {"flet==1.0.0", "flet-desktop==1.0.0"} <= set(data["project"]["dependencies"])
    assert "flet-cli==1.0.0" in data["dependency-groups"]["dev"]
    assert cmd_apply.load_record() == {"name": "alpha", "preset": "flet", "dependencies": ["flet==1.0.0", "flet-desktop==1.0.0"], "dev": ["flet-cli==1.0.0"]}
    err = capsys.readouterr().err
    assert "add flet==1.0.0, flet-desktop==1.0.0, flet-cli==1.0.0 (dev)" in err and "uv.lock          re-locked" in err
    # idempotent: a second run changes no file and adds, removes or locks nothing
    before, count = project.snapshot(), len(uv.calls)
    assert _run(project) == 0
    assert project.snapshot() == before
    assert [c for c in uv.changing(count) if c[0] != "sync"] == []
    assert cmd_apply.pending(project.cfg()) == []


def test_apply_raylib_package_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    project.edit("preset.raylib", "package", "raylib_sdl")
    assert render.pyproject_outdated(project.cfg())  # no-build-package follows the option
    assert _run(project) == 0
    assert uv.changing()[:3] == [["remove", "--frozen", "raylib"], ["add", "--frozen", "raylib_sdl==6.0.1.0"], ["lock"]]
    data = project.pyproject()
    assert "raylib-sdl==6.0.1.0" in data["project"]["dependencies"]
    assert not any(cmd_apply.req_key(r)[0] == "raylib" for r in data["project"]["dependencies"])
    assert data["tool"]["uv"]["no-build-package"] == ["raylib_sdl"]  # the guard covers the real dependency
    assert not render.pyproject_outdated(project.cfg())
    # and on to raylib_software: the record says raylib-sdl came from the options
    project.edit("preset.raylib", "package", "raylib_software")
    count = len(uv.calls)
    assert _run(project) == 0
    assert uv.changing(count)[:3] == [["remove", "--frozen", "raylib-sdl"], ["add", "--frozen", "raylib_software==6.0.1.0"], ["lock"]]
    assert [r for r in project.pyproject()["project"]["dependencies"] if r.startswith("raylib")] == ["raylib-software==6.0.1.0"]


def test_a_failed_lock_restores_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch, "flet")
    original = (project.root / "pyproject.toml").read_bytes()
    project.edit("preset.flet", "version", "9.9.9")  # a version that does not exist
    uv.fail.add("lock")
    with pytest.raises(DeployError) as e:
        _run(project)
    assert "pyproject.toml was restored" in str(e.value) and "./deploy apply again" in str(e.value)
    assert e.value.code == 1  # uv's exit code
    assert (project.root / "pyproject.toml").read_bytes() == original
    assert cmd_apply.load_record() is None  # nothing recorded as applied
    assert ["add", "--frozen", "flet==9.9.9", "flet-desktop==9.9.9"] in uv.calls  # it was tried
    uv.fail.clear()
    project.edit("preset.flet", "version", "1.0.1")
    count = len(uv.calls)
    assert _run(project) == 0
    assert [c for c in uv.changing(count) if c[0] != "sync"] == []


def test_a_failed_add_restores_pyproject_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    original = (project.root / "pyproject.toml").read_bytes()
    project.edit("preset.raylib", "package", "raylib_sdl")
    uv.fail.add("add")  # e.g. an option value uv cannot parse: the remove already happened
    with pytest.raises(DeployError, match="pyproject.toml was restored") as e:
        _run(project)
    assert e.value.code == 1
    assert ["remove", "--frozen", "raylib"] in uv.calls
    assert (project.root / "pyproject.toml").read_bytes() == original


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    project.edit("preset.raylib", "package", "raylib_sdl")
    project.edit("app", "name", "beta")
    before = project.snapshot()
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert _run(project) == 0
    assert project.snapshot() == before
    assert all(c[:2] == ["lock", "--check"] for c in uv.calls)
    err = capsys.readouterr().err
    assert "apply (--dry-run: nothing is written)" in err
    assert "would rename 'alpha' -> 'beta'" in err and "would move       src/alpha/ -> src/beta/" in err
    assert "would remove raylib; add raylib_sdl==6.0.1.0" in err
    assert "would rewrite the managed parts, [project] name, dependencies" in err
    assert "uv.lock          would re-lock (uv lock)" in err
    assert "would sync .venv (3.14), .venv-pypy (pypy@3.11.15)" in err


def test_dry_run_of_an_applied_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, uv = _project(tmp_path, monkeypatch, "flet")
    assert _run(project) == 0
    capsys.readouterr()
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert _run(project, command="setup") == 0
    err = capsys.readouterr().err
    for row in ("'alpha' (src/alpha/): unchanged", "flet: unchanged", "dependencies unchanged", "pyproject.toml   unchanged", "up to date (uv lock --check)", "generated files  unchanged"):
        assert row in err


def test_setup_is_the_same_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, uv = _project(tmp_path, monkeypatch, "flet")
    project.edit("preset.flet", "version", "1.0.0")
    assert cmd_env.cmd_setup(project.cfg(), []) == 0
    assert ["add", "--frozen", "--dev", "flet-cli==1.0.0"] in uv.calls
    err = capsys.readouterr().err
    assert "==> setup" in err and "done. Try: ./deploy run" in err


@pytest.mark.parametrize("command", ["apply", "setup"])
def test_unknown_arguments_are_refused_before_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    project, uv = _project(tmp_path, monkeypatch)
    before = project.snapshot()
    for bad in (["--frce"], ["cpython"], ["--force", "x"]):
        with pytest.raises(DeployError, match=f"{command}: unrecognized arguments") as e:
            _run(project, *bad, command=command)
        assert e.value.code == 2
    assert project.snapshot() == before and uv.calls == []


def test_commands_table() -> None:
    for name in ("apply", "setup"):
        command = cli.COMMANDS[name]
        assert (command.render, command.group, command.usage) == (False, "Environment", "[--force]")
    assert cli.COMMANDS["apply"].module == "cmd_apply" and cli.COMMANDS["setup"].func == "cmd_setup"
    assert cli.COMMANDS["rename"].render is False  # rename renders at the end, never before its checks
    assert "cmd_apply.doctor(cfg, check)" in inspect.getsource(cmd_env.cmd_doctor)


@pytest.mark.parametrize(
    ("table", "key", "value", "expected"),
    [
        ("typing", "relaxed", "strict", {"generated"}),  # the typing profile: render only
        ("backend", "active", "mypyc", {"generated"}),
        ("deploy.exe", "mode", "onedir", set()),  # read by `build` itself
        ("python", "cpython", "3.13", {"generated", "pyproject", "lock"}),
        ("backend", "supported", ["cpython", "pypy", "mypyc"], {"generated", "pyproject", "lock", "precheck", "sync pypy"}),
    ],
)
def test_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], table: str, key: str, value: Any, expected: set[str]) -> None:
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0  # a project in line with its pytemplate.toml
    project.edit(table, key, value)
    pyproject, count = (project.root / "pyproject.toml").read_bytes(), len(uv.calls)
    prechecks: list[int] = []  # changing uv calls made before the precheck ran
    monkeypatch.setattr(cmd_apply, "cmd_mode_precheck", lambda cfg: prechecks.append(len(uv.changing(count))))
    capsys.readouterr()
    assert _run(project) == 0
    summary = capsys.readouterr().err.split("==> summary")[1]
    done = set()
    if "generated files  updated" in summary:
        done.add("generated")
    if (project.root / "pyproject.toml").read_bytes() != pyproject:
        done.add("pyproject")
    if ["lock"] in uv.changing(count):
        done.add("lock")
    if prechecks:
        done.add("precheck")
        assert prechecks == [0]  # before anything changed the lock
    if ".venv-pypy" in summary:
        done.add("sync pypy")
    assert done == expected


def test_a_dropped_backend_leaves_a_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    (project.root / ".venv-pypy").mkdir()
    project.edit("backend", "active", "cpython")
    project.edit("backend", "supported", ["cpython", "mypyc"])
    assert _run(project) == 0
    err = capsys.readouterr().err
    assert "note: .venv-pypy is no longer used" in err and "./deploy clean --envs" in err
    assert (project.root / ".venv-pypy").is_dir()  # never deleted by apply
    assert ["sync", "--locked"] in uv.calls and "environments     synced .venv\n" in err


# --- app.name and app.preset edited by hand ------------------------------------------------------------


def _owned(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] in ("src", "tests", "typings") and "__pycache__" not in p.parts
    }


@pytest.mark.parametrize(("preset", "old", "new"), [("script", "alpha", "beta"), ("flet", "alpha", "My-Game"), ("raylib", "My-Game", "beta")])
def test_a_hand_edited_name_is_renamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str, old: str, new: str) -> None:
    project, uv = _project(tmp_path, monkeypatch, preset, old)
    project.edit("app", "name", new)
    assert cmd_apply.pending(project.cfg())[0] == (
        f"app.name = '{new}' is not applied: the package is still src/{rename.package_of(old)}/",
        f"./deploy apply  (renames '{old}' -> '{new}')",
    )
    assert _run(project) == 0
    skeleton = presets.skeleton(preset, new)
    assert _owned(project.root) == {k: v for k, v in skeleton.items() if k.split("/")[0] in ("src", "tests", "typings")}
    assert project.config_file.read_bytes() == skeleton["pytemplate.toml"]  # compile.modules... renamed too
    assert project.pyproject()["project"]["name"] == new
    assert presets.pristine(project.cfg())
    assert ["lock"] in uv.changing()  # the project name is in uv.lock
    assert cmd_apply.load_record() == {**cmd_apply.record_of(project.cfg()), "name": new}
    assert cmd_apply.pending(project.cfg()) == []
    count, before = len(uv.calls), project.snapshot()
    assert _run(project) == 0  # idempotent
    assert project.snapshot() == before and [c for c in uv.changing(count) if c[0] != "sync"] == []


def test_only_the_pyproject_name_differs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch, "flet", "alpha")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('"alpha"', '"other"'), encoding="utf-8", newline="\n")
    uv.locked = path.read_bytes()  # uv.lock was made with that name
    assert project.pyproject()["tool"]["flet"]["product"] == "other"
    assert cmd_apply.pending(project.cfg()) == [("pyproject.toml [project] name = 'other', but app.name = 'alpha'", "./deploy apply")]
    assert _run(project) == 0
    data = project.pyproject()
    assert data["project"]["name"] == "alpha" and data["tool"]["flet"]["product"] == "alpha"
    assert (project.root / "src" / "alpha").is_dir() and not (project.root / "src" / "other").exists()
    assert ["lock"] in uv.changing() and cmd_apply.pending(project.cfg()) == []


@pytest.mark.parametrize(("old", "new"), [("script", "flet"), ("flet", "raylib"), ("raylib", "script"), ("flet", "script")])
def test_a_hand_edited_preset_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str, new: str) -> None:
    project, uv = _project(tmp_path, monkeypatch, old)
    project.edit("app", "preset", new)
    before = project.snapshot()
    for command in ("apply", "setup"):
        with pytest.raises(DeployError) as e:
            _run(project, command=command)
        assert e.value.code == 2
        message = str(e.value)
        assert f"changed from '{old}' to '{new}' by hand" in message
        assert f'Put back app.preset = "{old}"' in message and f"./deploy new DIR --preset {new}" in message
    monkeypatch.setattr(proc, "DRY_RUN", True)
    with pytest.raises(DeployError, match="by hand"):
        _run(project)
    assert project.snapshot() == before and uv.calls == []
    assert cmd_apply.pending(project.cfg())[0][0] == f"app.preset = '{new}' but the project was made with the '{old}' preset"


def test_an_invalid_hand_edited_name_is_refused_before_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch)
    before = project.snapshot()
    for bad, message in (("rich", "dependency"), ("json", "standard library"), ("compression", "standard library"), ("class", "keyword")):
        project.edit("app", "name", bad)
        with pytest.raises(DeployError, match=message) as e:
            _run(project)
        assert e.value.code == 2 and str(e.value).startswith("app.name: ")
    project.edit("app", "name", "alpha")
    assert project.snapshot() == before and uv.calls == []


def test_missing_package_is_a_warning_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, uv = _project(tmp_path, monkeypatch)
    shutil.rmtree(project.root / "src" / "alpha")
    assert _run(project) == 0
    err = capsys.readouterr().err
    assert "warning: src/alpha/ does not exist" in err and "compile.modules: alpha.core not found" in err
    assert cmd_apply.pending(project.cfg())[0][0].startswith("src/alpha/ does not exist")


# --- the git hook ------------------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@needs_git
def test_the_hook_follows_pre_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, _ = _project(tmp_path, monkeypatch)
    _git(project.root, "init", "-q")
    hook = project.root / ".git" / "hooks" / "pre-commit"
    assert _run(project) == 0
    assert hook.is_file() and hooks.MARKER in hook.read_text(encoding="utf-8")
    project.edit("hooks", "pre_commit", False)
    assert cmd_apply.pending(project.cfg())[-1][0].startswith("hooks.pre_commit = false, but pytemplate's")
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert _run(project) == 0
    assert "would remove pytemplate's pre-commit hook" in capsys.readouterr().err and hook.is_file()
    monkeypatch.setattr(proc, "DRY_RUN", False)
    assert _run(project) == 0
    assert not hook.exists()
    assert "git hook         removed (hooks.pre_commit = false)" in capsys.readouterr().err
    # a hook that is not pytemplate's is never removed
    hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    assert _run(project) == 0
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert not [p for p, _ in cmd_apply.pending(project.cfg()) if "hook" in p]


# --- doctor, references, docs ---------------------------------------------------------------------------


def test_doctor_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, _ = _project(tmp_path, monkeypatch, "raylib")
    lines: list[tuple[bool | None, str]] = []

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        lines.append((passed, label))

    cmd_apply.doctor(project.cfg(), check)
    assert lines == [(True, "pytemplate.toml applied (app.name, app.preset, [preset.*], hooks.pre_commit)")]
    project.edit("preset.raylib", "version", "6.0.2.0")
    project.edit("deploy.exe", "icon", "art/app.ico")
    lines.clear()
    cmd_apply.doctor(project.cfg(), check)
    assert lines == [
        (False, "[preset.raylib] is not applied to pyproject.toml (add raylib==6.0.2.0)"),
        (None, "deploy.exe.icon = 'art/app.ico' does not exist (relative to the project root): exe and nuitka builds fail"),
    ]
    (project.root / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    lines.clear()
    cmd_apply.doctor(project.cfg(), check)  # a broken pyproject.toml is a problem line, never a traceback
    assert lines[0][0] is False and lines[0][1].startswith("pyproject.toml is not valid TOML")


def test_reference_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, _ = _project(tmp_path, monkeypatch, "flet")
    assert cmd_apply.reference_problems(project.cfg()) == []
    project.edit("app", "assets", "media")
    project.edit("compile", "modules", ["alpha.core", "alpha.gone"])
    project.edit("deploy.upx", "path", "tools/upx")
    problems = cmd_apply.reference_problems(project.cfg())
    assert [re.split(r"[ :]", p)[0] for p in problems] == ["compile.modules", "app.assets", "deploy.upx.path"]
    assert "alpha.gone" in problems[0] and "alpha.core" not in problems[0]
    (project.root / "tools").mkdir()
    (project.root / "tools" / "upx").write_text("", encoding="utf-8")
    assert len(cmd_apply.reference_problems(project.cfg())) == 2
    project.edit("backend", "supported", ["cpython"])
    project.edit("backend", "active", "cpython")
    assert len(cmd_apply.reference_problems(project.cfg())) == 1  # compile.modules only matters with mypyc


def test_render_auto_points_at_apply(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(render, "apply", lambda cfg, **kw: ([], []))
    monkeypatch.setattr(render, "pyproject_outdated", lambda cfg: True)
    render.auto(_cfg("script"))
    err = capsys.readouterr().err
    assert "./deploy apply" in err and "./deploy lock" not in err


def _table(text: str, header: str) -> str:
    return text.split(f"\n{header}\n", 1)[1].split("\n[", 1)[0]


@pytest.mark.parametrize("preset", ["script", "raylib", "flet"])
def test_config_comments_say_how_changes_are_applied(preset: str) -> None:
    text = (ROOT / ".pytemplate" / "presets" / preset / "files" / "pytemplate.toml").read_text(encoding="utf-8")
    head = text.split("\nschema = 1\n", 1)[0]
    assert "run ./deploy apply" in head and "./deploy new DIR --preset P" in head
    assert "run any ./deploy command" not in head and "./deploy lock" not in text
    assert "./deploy apply" in _table(text, "[hooks]") and "false" in _table(text, "[hooks]")
    if preset != "script":
        assert "./deploy apply" in _table(text, f"[preset.{preset}]")
    assert (ROOT / "pytemplate.toml").read_bytes() == presets.skeleton("script", "myapp")["pytemplate.toml"]


# --- the real ./deploy in a throwaway copy ----------------------------------------------------------------


def _deploy(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    drop = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return subprocess.run(
        [sys.executable, "-B", str(root / ".pytemplate" / "deploy.py"), *args],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, check=False,
    )


def _tree(root: Path) -> dict[str, str]:
    skip = (".git", ".venv", ".build", "__pycache__")
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<dir>"
        for p in sorted(root.rglob("*"))
        if not any(s in p.parts for s in skip)
    }


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    dest = tmp_path / "proj"
    dest.mkdir()
    presets.copy_template(dest)
    return dest


def _edit_copy(root: Path, table: str, key: str, value: Any) -> None:
    path = root / "pytemplate.toml"
    path.write_text(config.set_value(path.read_text(encoding="utf-8"), table, key, value), encoding="utf-8", newline="\n")


def test_real_dry_run_in_a_copy(copy: Path) -> None:
    _edit_copy(copy, "app", "name", "beta")
    _edit_copy(copy, "hooks", "pre_commit", False)
    before = _tree(copy)
    r = _deploy(copy, "--dry-run", "apply")
    assert r.returncode == 0, r.stderr
    assert "would rename 'myapp' -> 'beta'" in r.stderr and "+ from beta.core import bench" in r.stderr
    assert "git hook         not a git work tree: nothing to do" in r.stderr
    assert _tree(copy) == before, "--dry-run wrote files"
    r = _deploy(copy, "apply", "--bogus")
    assert r.returncode == 2 and "apply: unrecognized arguments: --bogus" in r.stderr
    assert _tree(copy) == before


def test_real_hand_edited_preset_is_refused(copy: Path) -> None:
    _edit_copy(copy, "app", "preset", "raylib")
    before = _tree(copy)
    for command in ("apply", "setup"):
        r = _deploy(copy, command)
        assert r.returncode == 2, r.stderr
        assert "changed from 'script' to 'raylib' by hand" in r.stderr and "./deploy new DIR --preset raylib" in r.stderr
    assert _tree(copy) == before


@needs_uv
@needs_git
def test_real_apply_after_a_hand_edited_name(copy: Path) -> None:
    _git(copy, "init", "-q")
    _git(copy, "add", "-A")
    _git(copy, "commit", "-q", "-m", "init", "--no-verify")
    _edit_copy(copy, "app", "name", "beta")  # pytemplate.toml is dirty by definition: not refused
    (copy / "src" / "notes.txt").write_text("mine\n", encoding="utf-8")
    r = _deploy(copy, "apply")
    assert r.returncode == 2 and "uncommitted changes in git (1 path(s): src/notes.txt)" in r.stderr, r.stderr
    assert "./deploy apply --force" in r.stderr and (copy / "src" / "myapp").is_dir()
    (copy / "src" / "notes.txt").unlink()
    r = _deploy(copy, "apply")
    if r.returncode != 0 and rename.needs_pypi(r.stderr):
        pytest.skip("needs PyPI: uv lock could not reach the package index")
    assert r.returncode == 0, r.stderr
    assert not (copy / "src" / "myapp").exists() and (copy / "src" / "beta" / "app.py").is_file()
    assert tomllib.loads((copy / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"] == "beta"
    assert "beta" in {p["name"] for p in tomllib.loads((copy / "uv.lock").read_text(encoding="utf-8"))["package"]}
    assert (copy / ".git" / "hooks" / "pre-commit").is_file()
    assert _deploy(copy, "render", "--check").returncode == 0
    before = _tree(copy)
    r = _deploy(copy, "apply")
    assert r.returncode == 0, r.stderr
    summary = r.stderr.split("==> summary")[1]
    assert "app.name" not in summary and "uv.lock" not in summary and "generated files" not in summary
    assert _tree(copy) == before, "a second apply changed files"


def test_uv_frozen_edits_only_pyproject(tmp_path: Path) -> None:
    """The uv behaviour apply relies on, offline: `add/remove --frozen` edit pyproject.toml in place
    (names normalized, the dev group with --dev) and never touch uv.lock."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\nrequires-python = ">=3.11"\ndependencies = [\n    "raylib==6.0.1.0",\n]\n\n'
        '[dependency-groups]\ndev = [\n    "flet-cli==1.0.1",\n]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("untouched\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_PROJECT", "VIRTUAL_ENV"))} | {"UV_OFFLINE": "1"}

    def run(*args: str) -> None:
        r = subprocess.run([uv, *args], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
        assert r.returncode == 0, r.stderr

    run("remove", "--frozen", "raylib")
    run("add", "--frozen", "Raylib_SDL == 6.0.1.0")
    run("add", "--frozen", "--dev", "flet-cli==1.0.0")
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == ["raylib-sdl==6.0.1.0"]
    assert data["dependency-groups"]["dev"] == ["flet-cli==1.0.0"]
    assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == "untouched\n"
    assert not (tmp_path / ".venv").exists()
