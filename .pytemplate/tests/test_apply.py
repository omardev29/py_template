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


def _preset_requirements() -> set[str]:
    """Names of the requirements a preset adds to the shared pyproject.toml: every preset's with
    its default options, and this project's own preset with its [preset.*] options."""
    own = config.load(set(cli.COMMANDS))
    names: set[str] = set()
    for preset in presets.available():
        deps, dev = presets.dependencies(own, preset)
        names |= {cmd_apply.req_key(r)[0] for r in (*deps, *dev)}
    return names


def _pyproject(preset: str, name: str) -> str:
    """pyproject.toml as `./deploy new NAME --preset PRESET` writes it (managed block included),
    from this project's pyproject.toml without the requirements its own preset added."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8").replace("\r\n", "\n")
    data = tomllib.loads(text)
    added = _preset_requirements()
    deps = [d for d in data["project"]["dependencies"] if cmd_apply.req_key(d)[0] not in added]
    dev = [d for d in data["dependency-groups"]["dev"] if cmd_apply.req_key(d)[0] not in added]
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
    text = presets._set_extra_tables(presets.set_project_name(text, name), extra)
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
        for module in (cmd_apply, cmd_env, rename, render, presets, envs, project_module):
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
    config = tmp_path / "gitconfig"
    # no background maintenance after a commit: it writes and removes .git/objects/maintenance.lock
    # while a test compares snapshots (git 2.5x on macOS)
    config.write_text("[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
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


def test_a_foreign_record_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`./deploy new` copies the template's state.json: its record (myapp, script) does not describe
    the new project and must not steer apply (only a record named like the project counts)."""
    project, uv = _project(tmp_path, monkeypatch, "flet")
    state = project.root / ".pytemplate" / "state.json"
    foreign = {"name": "myapp", "preset": "flet", "dependencies": ["flet==0.9.0", "flet-desktop==0.9.0"], "dev": ["flet-cli==0.9.0"]}
    state.write_text(json.dumps({"files": {}, "applied": foreign}), encoding="utf-8")
    cfg = project.cfg()
    assert cmd_apply.trusted_record(cfg, "alpha") is None and cmd_apply.project_record(cfg) is None
    assert cmd_apply.trusted_record(cfg, "myapp") == foreign  # pyproject.toml still says myapp: it is ours
    applied = cmd_apply.applied_state(cfg, cmd_apply.read_project())
    assert (applied.record, applied.dependencies, applied.renamed_from) == (None, FLET_DEPS, None)
    assert _run(project) == 0
    assert [c for c in uv.changing() if c[0] in ("add", "remove")] == []  # nothing to change
    assert cmd_apply.load_record() == cmd_apply.record_of(cfg)  # replaced by the project's own


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
        # a script project (the record says so) that depends on raylib or flet: no preset switch
        ("script", {}, ["rich>=15.0.0", "raylib==6.0.1.0"], [], {"preset": "script"}, "script"),
        ("script", {}, ["rich>=15.0.0", *FLET_DEPS], FLET_DEV, {"preset": "script"}, "script"),
        ("script", {}, [], ["flet-cli==1.0.1"], {"preset": "script"}, "script"),
        # ...but a hand switch of that project is still one
        ("flet", {}, ["rich>=15.0.0", "raylib==6.0.1.0"], [], {"preset": "script"}, "raylib"),
        ("raylib", {}, ["rich>=15.0.0"], [], {"preset": "script"}, "script"),
    ],
)
def test_applied_preset(preset: str, options: dict[str, str], deps: list[str], dev: list[str], record: dict[str, Any] | None, expected: str) -> None:
    full = None if record is None else {"name": "alpha", "dependencies": [], "dev": [], **record}
    assert cmd_apply._applied_preset(_cfg(preset, **options), _declared(deps, dev), full) == expected


NO_BUILD_RAYLIB = {"tool": {"uv": {"no-build-package": ["raylib"]}}}  # the managed block of a raylib project


@pytest.mark.parametrize(
    ("preset", "options", "deps", "data", "expected"),
    [
        # no record (a fresh project, or a lost state.json): raylib swapped by hand for another package
        ("raylib", {}, ["raylib-sdl==6.0.1.0"], NO_BUILD_RAYLIB, "raylib"),
        ("raylib", {"package": "raylib_software"}, ["raylib-sdl==6.0.1.0"], NO_BUILD_RAYLIB, "raylib"),
        # every flet dependency removed: [tool.flet] is still there
        ("flet", {}, [], {"tool": {"flet": {"org": "com.example"}, "uv": {}}}, "flet"),
        # hand switches without a record: another preset's traces
        ("script", {}, ["rich>=15.0.0"], {"tool": {"flet": {"org": "com.example"}}}, "flet"),
        ("flet", {}, ["raylib-sdl==6.0.1.0"], NO_BUILD_RAYLIB, "raylib"),
        # no trace of any preset in pyproject.toml: only a preset without traces made it
        ("raylib", {}, ["raylib-sdl==6.0.1.0"], {}, "script"),
        ("flet", {}, [], {"tool": {"uv": {"environments": []}}}, "script"),
    ],
)
def test_applied_preset_reads_every_trace(preset: str, options: dict[str, str], deps: list[str], data: dict[str, Any], expected: str) -> None:
    project = cmd_apply.Project(data, "alpha", {cmd_apply.req_key(r)[0]: r for r in deps}, {})
    assert cmd_apply._applied_preset(_cfg(preset, **options), project, None) == expected


def test_a_script_project_may_depend_on_raylib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`./deploy add raylib` in a script project that was applied (its record says script):
    apply, setup and doctor take it for what it is, not for a hand-switched raylib project."""
    project, uv = _project(tmp_path, monkeypatch, "script")
    assert _run(project) == 0  # the record: script
    uv(envs.tool_env(project.cfg()), ["add", "--frozen", "raylib==6.0.1.0"])
    uv.locked = (project.root / "pyproject.toml").read_bytes()
    for command in ("apply", "setup"):
        assert _run(project, command=command) == 0
    assert "raylib==6.0.1.0" in project.pyproject()["project"]["dependencies"]  # never removed
    assert cmd_apply.pending(project.cfg()) == []
    assert cmd_apply.load_record() == cmd_apply.record_of(project.cfg())


def test_a_raylib_package_swapped_by_hand_before_the_first_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh raylib project (no record of its own) whose raylib was swapped by hand for
    raylib_sdl, or whose record was lost: still a raylib project, not a script one."""
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    uv(envs.tool_env(project.cfg()), ["remove", "--frozen", "raylib"])
    uv(envs.tool_env(project.cfg()), ["add", "--frozen", "raylib_sdl==6.0.1.0"])
    assert cmd_apply.applied_state(project.cfg(), cmd_apply.read_project()).preset == "raylib"
    # the one thing to fix is [preset.raylib] (it wins over a hand `uv add`), not app.preset
    assert cmd_apply.pending(project.cfg()) == [("[preset.raylib] is not applied to pyproject.toml (add raylib==6.0.1.0)", "./deploy apply")]
    project.edit("preset.raylib", "package", "raylib_sdl")
    assert cmd_apply.pending(project.cfg()) == []
    assert _run(project) == 0
    assert [r for r in project.pyproject()["project"]["dependencies"] if r.startswith("raylib")] == ["raylib-sdl==6.0.1.0"]
    # the record lost (a state.json merge conflict resolved by rendering), then the next switch
    (project.root / ".pytemplate" / "state.json").write_text("{}", encoding="utf-8")
    project.edit("preset.raylib", "package", "raylib_software")
    assert cmd_apply.applied_state(project.cfg(), cmd_apply.read_project()).preset == "raylib"
    assert _run(project) == 0


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
    assert ["sync", "--locked", "--all-groups"] in uv.calls  # envs.sync: every dependency group
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


def test_the_record_follows_the_lock_when_a_later_step_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The option change is locked, then `uv sync` fails: pyproject.toml and uv.lock hold the new
    package, and so must the record, or reverting the option kept both raylib distributions."""
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    assert _run(project) == 0
    project.edit("preset.raylib", "package", "raylib_software")
    uv.fail.add("sync")
    with pytest.raises(DeployError, match="sync"):
        _run(project)
    assert [r for r in project.pyproject()["project"]["dependencies"] if r.startswith("raylib")] == ["raylib-software==6.0.1.0"]
    assert cmd_apply.load_record() == {"name": "alpha", "preset": "raylib", "dependencies": ["raylib_software==6.0.1.0"], "dev": []}
    uv.fail.clear()
    project.edit("preset.raylib", "package", "raylib")
    assert _run(project) == 0
    assert [r for r in project.pyproject()["project"]["dependencies"] if r.startswith("raylib")] == ["raylib==6.0.1.0"]
    assert cmd_apply.pending(project.cfg()) == []


def test_the_record_follows_a_rename_when_the_lock_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rename is done before the lock: when the lock fails, the record keeps the applied
    options under the NEW name (named after the old one it would no longer be trusted)."""
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    project.edit("preset.raylib", "package", "raylib_sdl")
    assert _run(project) == 0
    project.edit("app", "name", "beta")
    uv.fail.add("lock")
    with pytest.raises(DeployError, match="the app is already renamed"):
        _run(project)
    uv.fail.clear()
    assert cmd_apply.load_record() == {"name": "beta", "preset": "raylib", "dependencies": ["raylib_sdl==6.0.1.0"], "dev": []}
    project.edit("preset.raylib", "package", "raylib_software")
    assert _run(project) == 0
    assert [r for r in project.pyproject()["project"]["dependencies"] if r.startswith("raylib")] == ["raylib-software==6.0.1.0"]


def test_pypy_and_a_python_change_in_one_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Python 3.11 check syncs the tools environment (`uv sync --locked`) with the NEW
    configuration: it runs once pyproject.toml and uv.lock follow it (with a python.cpython change
    in the same edit, uv refused the old lock and apply stopped)."""
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0
    project.edit("backend", "supported", ["cpython", "pypy", "mypyc"])
    project.edit("python", "cpython", "3.13")
    seen: list[tuple[bool, int]] = []

    def precheck(cfg: Config) -> None:
        seen.append((render.pyproject_outdated(cfg), envs.uv(envs.tool_env(cfg), ["lock", "--check"], check=False, echo=False).returncode))

    monkeypatch.setattr(cmd_apply, "cmd_mode_precheck", precheck)
    assert _run(project) == 0
    assert seen == [(False, 0)]  # pyproject.toml follows the configuration, and uv.lock follows it
    assert cmd_apply.read_project().pypy_locked


def test_a_failed_python_311_check_restores_pyproject_and_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0
    (project.root / "uv.lock").write_text("# locked\n", encoding="utf-8")
    record = cmd_apply.load_record()
    before = {n: (project.root / n).read_bytes() for n in ("pyproject.toml", "uv.lock")}
    project.edit("backend", "supported", ["cpython", "pypy", "mypyc"])

    def precheck(cfg: Config) -> None:
        raise DeployError("the code uses syntax that does not exist in Python 3.11 (see above); fix it before enabling PyPy")

    monkeypatch.setattr(cmd_apply, "cmd_mode_precheck", precheck)
    count = len(uv.calls)
    with pytest.raises(DeployError) as e:
        _run(project)
    assert "pyproject.toml and uv.lock were restored" in str(e.value) and e.value.code == 2
    assert {n: (project.root / n).read_bytes() for n in ("pyproject.toml", "uv.lock")} == before
    assert ["lock"] in uv.changing(count) and not [c for c in uv.changing(count) if c[0] == "sync"]
    assert cmd_apply.load_record() == record  # nothing recorded as applied


def test_dry_run_with_pypy_new_and_a_stale_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """--dry-run runs the Python 3.11 check read-only (`uv run --locked --no-sync`), which a stale
    uv.lock alone would fail: then it says the check waits for the re-lock instead of failing."""
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0
    project.edit("backend", "supported", ["cpython", "pypy", "mypyc"])
    uv(envs.tool_env(project.cfg()), ["add", "--frozen", "idna>=3"])  # by hand, not locked
    ran: list[Config] = []
    monkeypatch.setattr(cmd_apply, "cmd_mode_precheck", ran.append)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    capsys.readouterr()
    assert _run(project) == 0
    err = capsys.readouterr().err
    assert ran == [] and "the Python 3.11 check (PyPy is new) is not run" in err and "uv.lock          would re-lock" in err
    uv.locked = (project.root / "pyproject.toml").read_bytes()  # the lock follows pyproject.toml
    assert _run(project) == 0
    assert len(ran) == 1


def test_a_state_file_that_cannot_be_written_is_a_clear_error(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.mkdir()  # a folder in the way: unwritable on every OS, even for root
    with pytest.raises(DeployError, match="cannot write") as e:
        cmd_apply.save_record(RECORD, state)
    assert e.value.code == 2


def test_a_pyproject_that_cannot_be_written_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch, "flet")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('"alpha"', '"other"'), encoding="utf-8", newline="\n")
    real = Path.write_text

    def write_text(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name == "pyproject.toml":
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    with pytest.raises(DeployError, match="cannot write pyproject.toml: Permission denied") as e:
        _run(project)
    assert e.value.code == 2


def test_dry_run_of_a_name_that_normalizes_the_same(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """uv.lock holds the normalized project name: alpha -> Alpha re-locks nothing, and the
    --dry-run says so (as rename's does)."""
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0
    project.edit("app", "name", "Alpha")
    monkeypatch.setattr(proc, "DRY_RUN", True)
    capsys.readouterr()
    assert _run(project) == 0
    err = capsys.readouterr().err
    assert "would rewrite [project] name" in err and "uv.lock          up to date (uv lock --check)" in err
    project.edit("app", "name", "beta")
    assert _run(project) == 0
    assert "uv.lock          would re-lock (uv lock)" in capsys.readouterr().err


def test_dry_run_of_a_rename_checks_the_references_where_they_are(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """compile.modules names the renamed package (zed.core): before the move it lives in
    src/alpha/, so the --dry-run must not warn that it is missing; a module that is really
    missing still warns."""
    project, uv = _project(tmp_path, monkeypatch)
    project.edit("compile", "modules", ["alpha.core", "alpha.gone"])
    project.edit("app", "name", "zed")
    monkeypatch.setattr(proc, "DRY_RUN", True)
    capsys.readouterr()
    assert _run(project) == 0
    warnings = [line for line in capsys.readouterr().err.splitlines() if line.startswith("warning: compile.modules")]
    assert warnings == ["warning: compile.modules: zed.gone not found in src/ (mypyc builds and `test mypyc` will fail)"]


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
    prechecks: list[list[list[str]]] = []  # changing uv calls made before the precheck ran
    monkeypatch.setattr(cmd_apply, "cmd_mode_precheck", lambda cfg: prechecks.append(uv.changing(count)))
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
        assert prechecks == [[["lock"]]]  # once the lock follows the configuration, before any sync
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
    assert "note: .venv-pypy is not used by this configuration" in err and "./deploy clean --envs" in err
    assert (project.root / ".venv-pypy").is_dir()  # never deleted by apply
    assert ["sync", "--locked", "--all-groups"] in uv.calls and "environments     synced .venv\n" in err


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


@pytest.mark.parametrize("record", [True, False])
def test_a_name_of_another_package_in_src_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: bool) -> None:
    """app.name set by hand to the name of another package of the project (src/helpers/): apply
    rewrote only pyproject.toml [project] name and said "applied", the app still in src/alpha/.
    It refuses, as `./deploy rename helpers` does, and writes nothing."""
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0
    if not record:  # pyproject.toml [project] name alone says what the app is
        (project.root / ".pytemplate" / "state.json").write_text("{}", encoding="utf-8")
    helpers = project.root / "src" / "helpers"
    helpers.mkdir()
    (helpers / "__init__.py").write_text('"""Helpers."""\n', encoding="utf-8")
    project.edit("app", "name", "helpers")
    before, count = project.snapshot(), len(uv.calls)
    for dry in (False, True):
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        with pytest.raises(DeployError, match=r"src/helpers/ already exists and is not the app's package") as e:
            _run(project)
        assert e.value.code == 2 and 'Put back app.name = "alpha"' in str(e.value)
    assert project.snapshot() == before and uv.changing(count) == []
    problem, hint = cmd_apply.pending(project.cfg())[0]
    assert "names src/helpers/, another package: the app is 'alpha'" in problem and 'put back app.name = "alpha"' in hint
    project.edit("app", "name", "alpha")
    assert cmd_apply.pending(project.cfg()) == []


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


def test_a_broken_managed_block_is_refused_before_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # render.check_pyproject: the preflight runs before the rename, the uv edits and the lock
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace("  # <<< pytemplate", ""), encoding="utf-8", newline="\n")
    project.edit("app", "name", "beta")
    project.edit("preset.raylib", "package", "raylib_sdl")
    before = project.snapshot()
    for dry in (False, True):
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        with pytest.raises(DeployError, match="pytemplate") as e:
            _run(project)
        assert e.value.code == 2
    assert project.snapshot() == before and uv.calls == []
    assert (project.root / "src" / "alpha").is_dir()


def test_a_project_name_that_cannot_be_set_is_refused_before_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch, "flet")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('name = "alpha"\n', "", 1), encoding="utf-8", newline="\n")
    project.edit("preset.flet", "version", "1.0.0")
    before = project.snapshot()
    for dry in (True, False):  # planned: --dry-run says it too, and the real run writes nothing
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        with pytest.raises(DeployError, match=r'could not set \[project\] name = "alpha"') as e:
            _run(project)
        assert e.value.code == 2
    assert project.snapshot() == before and uv.calls == []


def test_unused_environments_are_named_never_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, _ = _project(tmp_path, monkeypatch, "raylib")  # supports pypy
    suffix = project_module.ENV_SUFFIX  # WSL on /mnt: the -wsl environments are this side's
    for name in (".venv", ".venv-pypy", ".venv-jit", ".venv-other-side" + ("" if suffix else "-wsl")):
        (project.root / (name + suffix if name != ".venv-other-side-wsl" else name)).mkdir()
    (project.root / ".venv-file").write_text("not an environment", encoding="utf-8")
    assert [p.name for p in cmd_apply.unused_envs(project.cfg())] == [".venv-jit" + suffix]
    project.edit("backend", "active", "cpython")
    project.edit("backend", "supported", ["cpython", "mypyc"])
    assert [p.name for p in cmd_apply.unused_envs(project.cfg())] == [".venv-jit" + suffix, ".venv-pypy" + suffix]
    capsys.readouterr()
    assert _run(project) == 0
    err = capsys.readouterr().err
    assert f"note: .venv-jit{suffix}, .venv-pypy{suffix} are not used by this configuration" in err
    assert (project.root / (".venv-jit" + suffix)).is_dir() and (project.root / (".venv-pypy" + suffix)).is_dir()


def test_the_mismatch_hints_name_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`./deploy lock` applies the managed block but not [preset.*]: after a raylib package switch
    it moved no-build-package to raylib_sdl and kept the raylib dependency. Every hint for a
    pyproject.toml that does not match pytemplate.toml names apply."""
    from runner import cmd_mode

    monkeypatch.setattr(render, "pyproject_outdated", lambda cfg: True)
    monkeypatch.setattr(hooks, "uv_lock_check", lambda cfg: (0, ""))
    result = hooks.check_lock(_cfg("raylib"))
    assert result.passed is False and "does not match pytemplate.toml: ./deploy apply" in result.hint and "lock" not in result.hint
    monkeypatch.setattr(render, "apply", lambda cfg, **kw: ([], []))
    assert cmd_mode.cmd_render(_cfg("raylib"), ["--check"]) == 1
    assert "does not match pytemplate.toml: ./deploy apply" in capsys.readouterr().err
    assert '"pyproject.toml matches pytemplate.toml", "./deploy apply"' in inspect.getsource(cmd_env.cmd_doctor)


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


def test_git_refusing_the_repository_is_said_not_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, _ = _project(tmp_path, monkeypatch)

    def refuse(*args: Any, **kwargs: Any) -> hooks.Repo:
        raise DeployError(f"git cannot use the repository of {project.root}:\n  fatal: detected dubious ownership in repository", 2)

    monkeypatch.setattr(hooks, "find_repo", refuse)
    assert _run(project) == 0
    err = capsys.readouterr().err
    assert "warning: git pre-commit hook not checked: git cannot use the repository" in err and "dubious ownership" in err
    assert "git hook         not checked: git refuses the repository" in err
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert _run(project) == 0
    assert "git hook         not checked: git refuses the repository (git cannot use" in capsys.readouterr().err
    assert not [p for p, _ in cmd_apply.pending(project.cfg()) if "hook" in p]


@needs_git
def test_every_hook_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """What apply does, and what its --dry-run says, for each state of the hooks folder."""
    project, _ = _project(tmp_path, monkeypatch)
    _git(project.root, "init", "-q")
    hook = project.root / ".git" / "hooks" / "pre-commit"
    ours = hooks.hook_script("./deploy")

    def run(dry: bool) -> str:
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        capsys.readouterr()
        assert _run(project) == 0
        out = capsys.readouterr().err
        return next(line for line in out.splitlines() if line.startswith("  git hook ")).split(None, 2)[2]

    cases = [  # (hooks.pre_commit, hook content or None, dry-run row, real-run row)
        (True, None, "would install the pre-commit hook", "installed"),
        (True, ours, "installed", "already installed"),
        (True, ours.replace("hooks run", "hooks run  "), "would update the pre-commit hook", "updated"),
        (True, "#!/bin/sh\necho mine\n", "another tool's hook: left alone", "another tool's hook: left alone"),
        (True, "#!/bin/sh\nsh ./deploy hooks run || exit $?\n", "a hook that runs ./deploy hooks run: left alone", "a hook that runs ./deploy hooks run: left alone"),
        (False, ours, "would remove pytemplate's pre-commit hook", "removed (hooks.pre_commit = false)"),
        (False, None, "not installed (hooks.pre_commit = false)", "not installed (hooks.pre_commit = false)"),
        (False, "#!/bin/sh\necho mine\n", "another tool's hook: left alone", "another tool's hook: left alone"),
    ]
    for pre_commit, content, planned, done in cases:
        project.edit("hooks", "pre_commit", pre_commit)
        hook.unlink(missing_ok=True)
        if content is not None:
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text(content, encoding="utf-8")
        assert run(True).startswith(planned), (pre_commit, content)
        assert hook.exists() == (content is not None)  # --dry-run: untouched
        assert run(False).startswith(done), (pre_commit, content)
        if content is not None and "echo mine" in content:
            assert hook.read_text(encoding="utf-8") == content  # never someone else's
    project.edit("hooks", "pre_commit", True)
    hook.unlink(missing_ok=True)
    _git(project.root, "config", "core.hooksPath", ".githooks")
    assert run(True).startswith("core.hooksPath is set: nothing installed")
    assert run(False).startswith("core.hooksPath is set: nothing installed") and not hook.exists()
    # that folder's hook already runs the checks: said so, not "nothing installed"
    (project.root / ".githooks").mkdir()
    (project.root / ".githooks" / "pre-commit").write_text("#!/bin/sh\nsh ./deploy hooks run || exit $?\n", encoding="utf-8")
    assert run(True) == run(False) == "core.hooksPath is set: .githooks/pre-commit runs ./deploy hooks run"


@needs_git
def test_apply_and_a_chained_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Two projects in one repository: q's `hooks install --force` chained p's hook as
    pre-commit.local. apply in p says p's checks run (no --force advice, which would fail),
    removes that copy with hooks.pre_commit = false, and drops it once q's hook is gone (p's
    checks never run twice)."""
    project, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))  # the repository is tmp_path itself
    _git(tmp_path, "init", "-q")
    q = tmp_path / "q"
    q.mkdir()
    for folder in (project.root, q):  # live projects: their launchers exist
        (folder / "deploy").write_text("#!/bin/sh\n", encoding="utf-8")
    rp, rq = (hooks.find_repo(d, environ={}, cwd=tmp_path) for d in (project.root, q))
    target, local = tmp_path / ".git" / "hooks" / "pre-commit", tmp_path / ".git" / "hooks" / "pre-commit.local"

    def row(dry: bool) -> str:
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        capsys.readouterr()
        assert _run(project) == 0
        out = capsys.readouterr().err
        return next(line for line in out.splitlines() if line.startswith("  git hook ")).split(None, 2)[2]

    hooks.install(rp)
    hooks.install(rq, force=True)
    q_hook = target.read_bytes()
    chained = "runs this project's checks (pre-commit.local) after another project's hook"
    assert row(True) == row(False) == chained
    assert target.read_bytes() == q_hook and hooks.own_local(rp)
    assert not [p for p, _ in cmd_apply.pending(project.cfg()) if "hook" in p]
    project.edit("hooks", "pre_commit", False)
    assert cmd_apply.pending(project.cfg())[-1][0].startswith("hooks.pre_commit = false, but pytemplate's")
    assert row(True).startswith("would remove pre-commit.local") and local.is_file()
    assert row(False) == "removed (hooks.pre_commit = false)"
    assert not local.exists() and target.read_bytes() == q_hook  # q's own hook is never touched
    assert not [p for p, _ in cmd_apply.pending(project.cfg()) if "hook" in p]
    project.edit("hooks", "pre_commit", True)
    assert row(False) == "another project's hook of this repository: left alone (./deploy hooks install --force runs both)"
    local.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")  # a third hook there: --force would fail
    assert row(False) == "another project's hook of this repository: left alone (pre-commit.local is taken too: ./deploy hooks status says what to do)"
    local.unlink()
    # chained again, then q goes away: its stale hook is replaced by p's, and p's copy goes
    hooks.uninstall(rq)
    hooks.install(rp)
    hooks.install(rq, force=True)
    shutil.rmtree(q)
    assert row(True) == "would update the pre-commit hook"
    assert row(False) == "updated (and removed pre-commit.local, a copy of this project's hook)"
    assert target.read_bytes() == hooks.hook_script(rp.launcher).encode("ascii") and not local.exists()


@needs_git
def test_a_project_ignored_by_its_enclosing_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))  # the enclosing repository is tmp_path itself
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("p/\n", encoding="utf-8")
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert _run(project) == 0
    assert "not installed: the enclosing git repository ignores this project" in capsys.readouterr().err
    monkeypatch.setattr(proc, "DRY_RUN", False)
    assert _run(project) == 0
    assert "git hook         not installed (see above)" in capsys.readouterr().err
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()


# --- doctor, references, docs ---------------------------------------------------------------------------


def test_doctor_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, _ = _project(tmp_path, monkeypatch, "raylib")
    lines: list[tuple[bool | None, str]] = []

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        lines.append((passed, label))

    cmd_apply.doctor(project.cfg(), check)
    assert lines == [(True, "pytemplate.toml applied (app.name, app.preset, [preset.*], hooks.pre_commit)")]
    real_state, calls = cmd_apply._hook_state, []
    monkeypatch.setattr(cmd_apply, "_hook_state", lambda cfg: calls.append(1) or "installed")
    project.edit("hooks", "pre_commit", False)
    assert cmd_apply.pending(project.cfg(), hook=False) == [] and calls == []  # no git process for a quick check
    assert cmd_apply.pending(project.cfg())[0][0].startswith("hooks.pre_commit = false") and calls == [1]
    monkeypatch.setattr(cmd_apply, "_hook_state", real_state)
    project.edit("hooks", "pre_commit", True)
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


def test_the_hook_blocks_a_commit_of_changes_apply_has_not_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A [preset.*] edit leaves the managed parts of pyproject.toml alone, so only
    cmd_apply.pending sees it: the hook's lock check must report it too."""
    project, _ = _project(tmp_path, monkeypatch, "raylib")
    monkeypatch.setattr(hooks, "uv_lock_check", lambda cfg: (0, ""))  # the lock itself is not the point here
    assert hooks.check_lock(project.cfg()).passed is True
    project.edit("preset.raylib", "version", "6.0.2.0")
    assert not render.pyproject_outdated(project.cfg())
    result = hooks.check_lock(project.cfg())
    assert result.passed is False
    assert "[preset.raylib] is not applied to pyproject.toml (add raylib==6.0.2.0): ./deploy apply" in result.hint


def test_reference_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, _ = _project(tmp_path, monkeypatch, "flet")
    assert cmd_apply.reference_problems(project.cfg()) == []
    shutil.rmtree(project.root / "src" / "assets")
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
    if (TEMPLATE_DIR / "template-repo").is_file():  # the template's root is the script preset as myapp
        root = (ROOT / "pytemplate.toml").read_bytes().replace(b"\r\n", b"\n")  # a CRLF checkout (Windows)
        assert root == presets.skeleton("script", "myapp")["pytemplate.toml"]


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


def _copy_app(root: Path) -> dict[str, Any]:
    """[app] of the copy: a project made with ./deploy new has its own name and preset."""
    app: dict[str, Any] = tomllib.loads((root / "pytemplate.toml").read_text(encoding="utf-8-sig"))["app"]
    return app


def test_real_dry_run_in_a_copy(copy: Path) -> None:
    old = _copy_app(copy)["name"]
    _edit_copy(copy, "app", "name", "beta")
    _edit_copy(copy, "hooks", "pre_commit", False)
    before = _tree(copy)
    r = _deploy(copy, "--dry-run", "apply")
    assert r.returncode == 0, r.stderr
    assert f"would rename '{old}' -> 'beta'" in r.stderr and '+ """beta"""' in r.stderr  # every preset's docstring
    assert "git hook         not a git work tree: nothing to do" in r.stderr
    assert _tree(copy) == before, "--dry-run wrote files"
    r = _deploy(copy, "apply", "--bogus")
    assert r.returncode == 2 and "apply: unrecognized arguments: --bogus" in r.stderr
    assert _tree(copy) == before


def test_real_hand_edited_preset_is_refused(copy: Path) -> None:
    old = _copy_app(copy)["preset"]
    new = "script" if old == "raylib" else "raylib"
    _edit_copy(copy, "app", "preset", new)
    before = _tree(copy)
    for command in ("apply", "setup"):
        r = _deploy(copy, command)
        assert r.returncode == 2, r.stderr
        assert f"changed from '{old}' to '{new}' by hand" in r.stderr and f"./deploy new DIR --preset {new}" in r.stderr
    assert _tree(copy) == before


@needs_uv
@needs_git
def test_real_apply_after_a_hand_edited_name(copy: Path) -> None:
    old = rename.package_of(_copy_app(copy)["name"])
    _git(copy, "init", "-q")
    _git(copy, "add", "-A")
    _git(copy, "commit", "-q", "-m", "init", "--no-verify")
    _edit_copy(copy, "app", "name", "beta")  # pytemplate.toml is dirty by definition: not refused
    (copy / "src" / "notes.txt").write_text("mine\n", encoding="utf-8")
    r = _deploy(copy, "apply")
    assert r.returncode == 2 and "uncommitted changes in git (1 path(s): src/notes.txt)" in r.stderr, r.stderr
    assert "./deploy apply --force" in r.stderr and (copy / "src" / old).is_dir()
    (copy / "src" / "notes.txt").unlink()
    r = _deploy(copy, "apply")
    if r.returncode != 0 and rename.needs_pypi(r.stderr):
        pytest.skip("needs PyPI: uv lock could not reach the package index")
    assert r.returncode == 0, r.stderr
    assert not (copy / "src" / old).exists() and (copy / "src" / "beta" / "__init__.py").is_file()
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
