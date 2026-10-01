"""`./pyt apply` / `./pyt setup` (runner/cmd_apply.py): bring the project in line with
pytemplate.toml.

Most tests build a throwaway project in tmp_path (a preset skeleton plus the pyproject.toml
`./pyt new` would write) and run the runner in-process with a fake uv that edits
pyproject.toml exactly like `uv add/remove --frozen` do. A few run the real `./pyt` in a
throwaway copy of the template; the one that re-locks skips when the package index cannot be
reached, and `test_uv_frozen_edits_only_pyproject` checks, offline, the uv behaviour the fake
imitates.

The matrix: after editing each key of pytemplate.toml, what apply does.
  app.name                  the rename flow (src/<pkg>/, imports, pytemplate.toml, pyproject.toml)
  app.preset                refused (exit 2): ./pyt new DIR --preset P
  [preset.<name>]           uv remove/add --frozen of the option-driven requirements + one uv lock
  backend.supported/python  managed pyproject parts + uv lock (+ the PyPy 3.11 precheck when new)
  hooks.pre_commit          hook installed (true) / pytemplate's own hook removed (false)
  anything else             the generated files (render.apply), as every command does
"""

from __future__ import annotations

import errno
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

from runner import cli, cmd_apply, cmd_dev, cmd_env, config, envs, hooks, lintc, presets, proc, render, rename  # noqa: E402
from runner import project as project_module  # noqa: E402
from runner.methods import wheel  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import ROOT  # noqa: E402
from runner.ui import PytError  # noqa: E402

needs_uv = pytest.mark.skipif(shutil.which("uv") is None and not os.environ.get("UV"), reason="uv not found")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")

FLET_DEPS = ["flet==1.0.1", "flet-desktop==1.0.1"]
FLET_DEV = ["flet-cli==1.0.1"]
CHANGING = ("add", "remove", "lock", "sync")


# --- a throwaway project -------------------------------------------------------------------------


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parts(requirement: str) -> tuple[str, str, str, str]:
    """(normalized name, [extras], version specifier, marker) of a PEP 508 requirement."""
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;]*)(?:;(.*))?", requirement)
    assert m is not None, requirement
    return _norm(m.group(1)), m.group(2) or "", re.sub(r"\s+", "", m.group(3)), (m.group(4) or "").strip()


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
    """pyproject.toml as `./pyt new NAME --preset PRESET` writes it (managed block included),
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
    (normalized names, a requirement replaced in place only by one with the same marker, its
    extras kept, else appended; `remove` drops every requirement of the name, a missing one is
    refused), `lock` finds no solution for two different pins of one package in a group,
    `lock --check` compares with the last `lock`, and --dry-run skips the echoed calls like
    proc.run (test_uv_frozen_edits_only_pyproject checks uv itself)."""

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
        quiet: bool = True,
        keep_lock_mode: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in args]
        self.calls.append(argv)
        if proc.DRY_RUN and echo:
            return subprocess.CompletedProcess(["uv", *argv], 0, "", "")
        code = 0
        pyproject = self.root / "pyproject.toml"
        if argv[:2] == ["python", "find"]:  # envs.find_cpython: every CPython is installed here
            return subprocess.CompletedProcess(["uv", *argv], 0, f"/uv/cpython-{argv[-1]}/bin/python{argv[-1]}\n", "")
        if argv[:2] == ["lock", "--check"]:
            code = 1 if "lock" in self.fail or pyproject.read_bytes() != self.locked else 0
        elif argv[0] in self.fail:
            code = 1
        elif argv[0] in ("add", "remove"):
            self._edit(argv)
        elif argv[0] == "lock" and self._conflict():
            code = 1  # "No solution found": two different pins of one package
        elif argv[0] == "lock":
            self.locked = pyproject.read_bytes()
            (self.root / "uv.lock").write_text(f"# locked {hashlib.sha256(self.locked).hexdigest()}\n", encoding="utf-8")
        if check and code:
            raise proc.CommandFailed(["uv", *argv], code)
        return subprocess.CompletedProcess(["uv", *argv], code, "", "")

    def _conflict(self) -> bool:
        data = tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))
        for group in (data["project"].get("dependencies", []), data.get("dependency-groups", {}).get("dev", [])):
            pins: dict[str, set[str]] = {}
            for requirement in group:
                name, _, spec, _ = _parts(requirement)
                pins.setdefault(name, set()).add(spec)
            if any(len(specs) > 1 for specs in pins.values()):
                return True
        return False

    def _edit(self, argv: list[str]) -> None:
        dev = "--dev" in argv
        path = self.root / "pyproject.toml"
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        current: list[str] = list(data["dependency-groups"]["dev"] if dev else data["project"]["dependencies"])
        for item in (a for a in argv[1:] if not a.startswith("--")):
            name, extras, spec, marker = _parts(item)
            if argv[0] == "remove":  # every requirement of that name, whatever its marker
                assert any(_parts(r)[0] == name for r in current), f"uv: the dependency {name} could not be found"
                current = [r for r in current if _parts(r)[0] != name]
                continue
            # uv compares the markers by meaning; the same text, blanks aside, is enough here
            at = next((i for i, r in enumerate(current) if _parts(r)[0] == name and _parts(r)[3].replace(" ", "") == marker.replace(" ", "")), None)
            new = name + (_parts(current[at])[1] if at is not None else extras) + spec + (f" ; {marker}" if marker else "")
            if at is None:
                current.append(new)
            else:
                current[at] = new
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
    with pytest.raises(PytError, match=r"cannot format") as e:
        presets.option_dependencies("flet", {})
    assert e.value.code == 2


def test_read_project_tolerates_a_bom_and_reports_broken_toml(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_bytes(b'\xef\xbb\xbf[project]\nname = "x"\ndependencies = ["Rich>=13"]\n[dependency-groups]\ndev = ["pytest"]\n')
    project = cmd_apply.read_project(path)
    assert (project.name, project.deps, project.dev) == ("x", {"rich": "Rich>=13"}, {"pytest": "pytest"})
    assert not project.pypy_locked
    path.write_text("[project\n", encoding="utf-8")
    with pytest.raises(PytError, match="not valid TOML") as e:
        cmd_apply.read_project(path)
    assert e.value.code == 2
    with pytest.raises(PytError, match="cannot be read"):
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


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_a_record_outside_a_merge_conflict_is_read(tmp_path: Path, eol: str) -> None:
    """`git merge` left conflict markers in the hashes of state.json, the record intact below
    them: `./pyt apply` run before any rendering command read no record and refused with a
    false 'app.preset was changed' (render already salvages it: render._unconflicted)."""
    state = tmp_path / "state.json"
    ours = json.dumps({"comment": "x", "files": {"a": "1" * 64}, "applied": RECORD}, indent=2).split("\n")
    theirs = [line.replace("1" * 64, "2" * 64) for line in ours]
    lines: list[str] = []
    for a, b in zip(ours, theirs, strict=True):
        lines += [a] if a == b else ["<<<<<<< HEAD", a, "=======", b, ">>>>>>> other"]
    state.write_text(eol.join(lines) + eol, encoding="utf-8", newline="")
    assert cmd_apply.load_record(state) == RECORD
    other = {**RECORD, "name": "beta"}
    assert cmd_apply.save_record(other, state) is True
    # written as valid JSON without the untrusted hashes: every generated file is rendered again
    assert json.loads(state.read_text(encoding="utf-8")) == {"comment": "x", "applied": other}


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
    """`./pyt new` copies the template's state.json: its record (myapp, script) does not describe
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
        ("raylib", {}, FLET_DEPS, FLET_DEV, None, "flet"),
        ("flet", {}, ["raylib==6.0.1.0"], [], None, "raylib"),
        # ...but a script project may depend on raylib or flet: without a record their
        # requirements alone are no trace (flet's [tool.flet] or raylib's managed block is:
        # test_applied_preset_reads_every_trace)
        ("script", {}, FLET_DEPS, FLET_DEV, None, "script"),
        ("script", {}, ["rich>=15.0.0", "flet>=1.0.1"], [], None, "script"),
        ("script", {}, ["rich>=15.0.0", "raylib>=6.0.1.0"], [], None, "script"),
        ("flet", {}, ["rich>=15.0.0"], [], {"preset": "script"}, "script"),
        # an option change is not a preset change
        ("raylib", {"package": "raylib_sdl"}, ["raylib==6.0.1.0"], [], None, "raylib"),
        ("raylib", {"package": "raylib_software"}, ["raylib-sdl==6.0.1.0"], [], {"preset": "raylib", "dependencies": ["raylib_sdl==6.0.1.0"]}, "raylib"),
        ("flet", {"version": "1.0.0"}, FLET_DEPS, FLET_DEV, None, "flet"),
        # the dependencies were removed by hand: the record says it is still that preset
        ("flet", {}, [], [], {"preset": "flet"}, "flet"),
        # the record decides, whatever pyproject.toml declares (`./pyt new` writes the new
        # project's own record, so a copy of the template's never stands for it)
        ("flet", {}, FLET_DEPS, FLET_DEV, {"preset": "script"}, "script"),
        ("raylib", {}, ["raylib==6.0.1.0"], [], {"preset": "script"}, "script"),
        ("script", {}, ["rich>=15.0.0"], [], {"preset": "no-such-preset"}, "script"),
        # a script project (the record says so) that depends on raylib or flet: no preset switch
        ("script", {}, ["rich>=15.0.0", "raylib==6.0.1.0"], [], {"preset": "script"}, "script"),
        ("script", {}, ["rich>=15.0.0", *FLET_DEPS], FLET_DEV, {"preset": "script"}, "script"),
        ("script", {}, [], ["flet-cli==1.0.1"], {"preset": "script"}, "script"),
        # ...but a hand switch of that project is still one, from the recorded preset
        ("flet", {}, ["rich>=15.0.0", "raylib==6.0.1.0"], [], {"preset": "script"}, "script"),
        ("raylib", {}, ["rich>=15.0.0"], [], {"preset": "script"}, "script"),
    ],
)
def test_applied_preset(preset: str, options: dict[str, str], deps: list[str], dev: list[str], record: dict[str, Any] | None, expected: str) -> None:
    full = None if record is None else {"name": "alpha", "dependencies": [], "dev": [], **record}
    assert cmd_apply._applied_preset(_cfg(preset, **options), _declared(deps, dev), full) == expected


NO_BUILD_RAYLIB = {"tool": {"uv": {"no-build-package": ["raylib"]}}}  # the managed block of a raylib project
NO_BUILD_RAYLIB_SDL = {"tool": {"uv": {"no-build-package": ["raylib_sdl"]}}}  # ...applied with package = "raylib_sdl"
OWN_NO_BUILD_SIX = {"tool": {"uv": {"no-build-package": ["six"]}}}  # a project's own list, outside the markers


@pytest.mark.parametrize(
    ("preset", "options", "deps", "data", "block", "expected"),
    [
        # no record (a lost state.json): the package the managed block was last written with
        ("raylib", {}, ["raylib-sdl==6.0.1.0"], NO_BUILD_RAYLIB_SDL, NO_BUILD_RAYLIB_SDL, "raylib"),
        ("raylib", {"package": "raylib_software"}, ["raylib-sdl==6.0.1.0"], NO_BUILD_RAYLIB_SDL, NO_BUILD_RAYLIB_SDL, "raylib"),
        # every flet dependency removed: [tool.flet] is still there
        ("flet", {}, [], {"tool": {"flet": {"org": "com.example"}, "uv": {}}}, {}, "flet"),
        # hand switches without a record: another preset's traces
        ("script", {}, ["rich>=15.0.0"], {"tool": {"flet": {"org": "com.example"}}}, {}, "flet"),
        ("flet", {}, ["raylib-sdl==6.0.1.0"], NO_BUILD_RAYLIB_SDL, NO_BUILD_RAYLIB_SDL, "raylib"),
        ("script", {}, ["raylib-sdl==6.0.1.0"], NO_BUILD_RAYLIB_SDL, NO_BUILD_RAYLIB_SDL, "raylib"),
        ("script", {}, ["raylib==6.0.1.0"], NO_BUILD_RAYLIB, NO_BUILD_RAYLIB, "raylib"),
        ("script", {}, FLET_DEPS, {"tool": {"flet": {"org": "com.example"}}}, {}, "flet"),
        # a script project that added raylib (`./pyt add raylib`): its managed block is the script
        # preset's, so the requirement is the user's own, no preset switch (README)
        ("script", {}, ["rich>=15.0.0", "raylib>=6.0.1.0"], {"tool": {"uv": {}}}, {"tool": {"uv": {}}}, "script"),
        ("script", {}, ["raylib==6.0.1.0"], OWN_NO_BUILD_SIX, {}, "script"),
        # the managed block follows app.preset (`./pyt lock` after a hand switch writes the
        # new preset's keys): never a trace of it; putting app.preset back is accepted
        ("raylib", {}, ["rich>=15.0.0"], NO_BUILD_RAYLIB, NO_BUILD_RAYLIB, "script"),
        ("script", {}, ["rich>=15.0.0"], NO_BUILD_RAYLIB, NO_BUILD_RAYLIB, "script"),
        # no trace of any preset in pyproject.toml: only a preset without traces made it (a
        # guess, which the refusal says: raylib replaced by hand, not through [preset.raylib])
        ("raylib", {}, ["raylib-sdl==6.0.1.0"], {}, {}, "script"),
        ("raylib", {}, ["raylib-sdl==6.0.1.0"], NO_BUILD_RAYLIB, NO_BUILD_RAYLIB, "script"),
        ("flet", {}, [], {"tool": {"uv": {"environments": []}}}, {"tool": {"uv": {"environments": []}}}, "script"),
        # the project's own no-build-package list outside the markers (render._adopted): no
        # option read, whatever its length (["six"] once read as raylib's {package})
        ("raylib", {}, ["raylib-sdl==6.0.1.0"], {"tool": {"uv": {"no-build-package": ["raylib_sdl", "numpy"]}}}, {}, "script"),
        ("script", {}, ["six==1.17.0"], OWN_NO_BUILD_SIX, {}, "script"),
        ("raylib", {}, ["six==1.17.0"], OWN_NO_BUILD_SIX, {}, "script"),
    ],
)
def test_applied_preset_reads_every_trace(
    preset: str, options: dict[str, str], deps: list[str], data: dict[str, Any], block: dict[str, Any], expected: str
) -> None:
    project = cmd_apply.Project(data, "alpha", {cmd_apply.req_key(r)[0]: r for r in deps}, {}, block=block.get("tool", {}).get("uv", {}))
    assert cmd_apply._applied_preset(_cfg(preset, **options), project, None) == expected


def test_the_projects_own_no_build_package_is_no_preset_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A script project that keeps its own `no-build-package = ["six"]` in [tool.uv], outside the
    markers (render._adopted), then loses the `applied` record: the one-entry list read as raylib's
    ["{package}"], so apply refused a hand switch from raylib, doctor and the hook reported it, and
    putting app.preset = "raylib" back failed too."""
    monkeypatch.setattr(cmd_apply, "load_record", lambda: None)  # the record is lost
    cfg = _cfg("script")
    base = '[project]\nname = "alpha"\nversion = "0.1.0"\ndependencies = ["six==1.17.0"]\n\n[tool.uv]\n# our own: never build six\nno-build-package = ["six"]\n'
    path = tmp_path / "pyproject.toml"
    path.write_text(render.pyproject_expected(cfg, base), encoding="utf-8")
    project = cmd_apply.read_project(path)
    assert project.data["tool"]["uv"]["no-build-package"] == ["six"]
    assert cmd_apply._applied_preset(cfg, project, None) == "script"
    assert cmd_apply.applied_state(cfg, project).preset == "script"
    assert "no-build-package" not in project.block and project.block["python-preference"] == "only-managed"  # the block itself is read


@pytest.mark.parametrize(
    ("template", "text", "expected"),
    [
        ("{package}", "raylib_sdl", {"package": "raylib_sdl"}),
        ("{package}=={version}", "raylib==6.0.1.0", {"package": "raylib", "version": "6.0.1.0"}),
        ("{a}-{a}", "x-x", {"a": "x"}),
        ("{a}-{a}", "x-y", None),
        ("lib-{a}", "other", None),
        ("{{a}}", "{a}", {}),
        ("{a!r}", "x", None),  # a conversion or a format spec cannot be read back
        ("{a:>4}", "   x", None),
        ("{0}", "x", None),
    ],
)
def test_unformat_reads_the_options_back(template: str, text: str, expected: dict[str, str] | None) -> None:
    assert cmd_apply._unformat(template, text) == expected


@pytest.mark.parametrize("made_with", ["script", "flet"])
def test_a_hand_switch_stays_refused_after_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, made_with: str) -> None:
    """`./pyt lock` (mode and rename too) writes the managed [tool.uv] block from app.preset: after
    a hand switch to raylib it holds raylib's no-build-package. That is no trace of the preset the
    project was made with: apply stays refused, doctor names the recorded preset, and putting
    app.preset back is accepted."""
    project, _ = _project(tmp_path, monkeypatch, made_with)
    monkeypatch.setattr(cmd_env, "PYPROJECT", project.root / "pyproject.toml")
    assert _run(project) == 0  # the record: made_with
    before = project.pyproject()["project"]["dependencies"]
    project.edit("app", "preset", "raylib")
    assert cmd_env.cmd_lock(project.cfg(), []) == 0
    assert project.pyproject()["tool"]["uv"]["no-build-package"] == ["raylib"]
    with pytest.raises(PytError, match=f"changed from '{made_with}' to 'raylib'") as e:
        _run(project)
    assert e.value.code == 2 and "Put back app.preset = \"" + made_with + '"' in str(e.value)
    assert cmd_apply.pending(project.cfg())[0][0] == f"app.preset = 'raylib' but the project was made with the '{made_with}' preset"
    assert project.pyproject()["project"]["dependencies"] == before  # nothing half switched
    project.edit("app", "preset", made_with)
    assert _run(project) == 0
    assert "no-build-package" not in project.pyproject()["tool"]["uv"] and cmd_apply.pending(project.cfg()) == []


def test_a_record_names_the_preset_of_a_script_project_with_raylib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A script project that depends on raylib, whose app.preset is set to flet by hand: the
    refusal names the preset it was made with (script), not raylib (following that advice was
    then accepted as an in-place switch)."""
    project, uv = _project(tmp_path, monkeypatch, "script")
    assert _run(project) == 0
    uv(envs.tool_env(project.cfg()), ["add", "--frozen", "raylib==6.0.1.0"])
    uv.locked = (project.root / "pyproject.toml").read_bytes()
    for switched in ("flet", "raylib"):
        project.edit("app", "preset", switched)
        with pytest.raises(PytError, match=f"changed from 'script' to '{switched}'"):
            _run(project)


def test_a_script_project_may_depend_on_raylib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`./pyt add raylib` in a script project that was applied (its record says script):
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


@pytest.mark.parametrize("added", [["raylib>=6.0.1.0"], ["flet>=1.0.1"], FLET_DEPS], ids=["raylib", "flet", "flet pinned"])
def test_a_script_project_that_depends_on_raylib_or_flet_stays_one_without_a_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, added: list[str]) -> None:
    """README: a dependency you add yourself (`./pyt add raylib` in a script project) is not a
    preset switch. That held only while state.json kept the record: lost (a state.json merge
    conflict whose sides disagree on it, a deleted file), the declared raylib (or flet) was read
    as the trace of a raylib project: apply and setup refused app.preset = "script" as changed
    by hand, doctor and the pre-commit hook reported it on every commit, and putting app.preset =
    "raylib" back, as they said, switched the project to the raylib preset in place."""
    project, uv = _project(tmp_path, monkeypatch, "script")
    assert _run(project) == 0  # the record: script
    uv(envs.tool_env(project.cfg()), ["add", "--frozen", *added])
    uv(envs.tool_env(project.cfg()), ["lock"])
    state = project.root / ".pytemplate" / "state.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    del data["applied"]
    state.write_text(json.dumps(data), encoding="utf-8")
    assert cmd_apply.load_record() is None
    assert cmd_apply.pending(project.cfg(), hook=False) == []
    for command in ("apply", "setup"):
        assert _run(project, command=command) == 0
    assert set(added) <= set(project.pyproject()["project"]["dependencies"])  # never removed
    assert cmd_apply.load_record() == cmd_apply.record_of(project.cfg())  # script, recorded again


def _as_new(project: Project) -> None:
    """What `./pyt new` leaves: the new project's own record (presets.init writes it)."""
    cmd_apply.save_record(cmd_apply.record_of(project.cfg()), cmd_apply.state_file(project.root))


@pytest.mark.parametrize("added", [["raylib==6.0.1.0"], FLET_DEPS])
def test_a_new_script_project_may_depend_on_raylib_before_its_first_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, added: list[str]) -> None:
    """`./pyt new DIR --preset script`, `./pyt add raylib` (or flet), then the first
    `./pyt setup`: the project's own record (written by new) says script."""
    project, uv = _project(tmp_path, monkeypatch, "script")
    _as_new(project)
    uv(envs.tool_env(project.cfg()), ["add", "--frozen", *added])
    uv.locked = (project.root / "pyproject.toml").read_bytes()
    assert cmd_apply.pending(project.cfg()) == []
    assert _run(project, command="setup") == 0
    assert set(added) <= set(project.pyproject()["project"]["dependencies"])


def test_a_raylib_package_swapped_by_hand_before_the_first_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new raylib project whose raylib was swapped by hand for raylib_sdl: still a raylib
    project, not a script one."""
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    _as_new(project)
    uv(envs.tool_env(project.cfg()), ["remove", "--frozen", "raylib"])
    uv(envs.tool_env(project.cfg()), ["add", "--frozen", "raylib_sdl==6.0.1.0"])
    assert cmd_apply.applied_state(project.cfg(), cmd_apply.read_project()).preset == "raylib"
    # the one thing to fix is [preset.raylib] (it wins over a hand `uv add`), not app.preset
    assert cmd_apply.pending(project.cfg()) == [("[preset.raylib] is not applied to pyproject.toml (add raylib==6.0.1.0)", "./pyt apply")]
    project.edit("preset.raylib", "package", "raylib_sdl")
    assert cmd_apply.pending(project.cfg()) == []
    assert _run(project) == 0
    assert [r for r in project.pyproject()["project"]["dependencies"] if r.startswith("raylib")] == ["raylib-sdl==6.0.1.0"]


def _raylib(project: Project) -> list[str]:
    return [r for r in project.pyproject()["project"]["dependencies"] if r.startswith("raylib")]


def test_a_package_switch_after_the_record_was_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The record lost (a state.json merge conflict whose sides disagree on it, resolved by
    rendering): the managed block still names the package applied last, so the next switch
    removes it instead of keeping both raylib distributions (one import package)."""
    project, _ = _project(tmp_path, monkeypatch, "raylib")
    project.edit("preset.raylib", "package", "raylib_sdl")
    assert _run(project) == 0 and _raylib(project) == ["raylib-sdl==6.0.1.0"]
    state = project.root / ".pytemplate" / "state.json"
    for package, expected in (("raylib_software", "raylib-software==6.0.1.0"), ("raylib", "raylib==6.0.1.0")):
        state.write_text("{}", encoding="utf-8")
        project.edit("preset.raylib", "package", package)
        assert cmd_apply.applied_state(project.cfg(), cmd_apply.read_project()).preset == "raylib"
        assert cmd_apply.pending(project.cfg())[0][0].startswith("[preset.raylib] is not applied to pyproject.toml (remove raylib-")
        assert _run(project) == 0
        assert _raylib(project) == [expected]
        assert cmd_apply.pending(project.cfg()) == []


def test_a_guessed_preset_says_it_is_a_guess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No record and no trace of app.preset in pyproject.toml (raylib replaced by hand, then the
    record lost): the preset is a guess, and the refusal says how to keep app.preset."""
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    uv(envs.tool_env(project.cfg()), ["remove", "--frozen", "raylib"])
    uv(envs.tool_env(project.cfg()), ["add", "--frozen", "raylib_sdl==6.0.1.0"])
    with pytest.raises(PytError, match="changed from 'script' to 'raylib'") as e:
        _run(project)
    assert "no record of the last apply, and pyproject.toml holds no trace of the raylib preset" in str(e.value)
    assert "./pyt add raylib==6.0.1.0" in str(e.value) and "[preset.raylib]" in str(e.value)
    [(label, hint)] = cmd_apply.pending(project.cfg())
    assert label == "app.preset = 'raylib', but pyproject.toml holds no trace of that preset (and there is no record of the last apply)"
    assert "./pyt add raylib==6.0.1.0" in hint
    # following the hint: set [preset.raylib] to what pyproject.toml declares
    project.edit("preset.raylib", "package", "raylib_sdl")
    assert cmd_apply.pending(project.cfg()) == [] and _run(project) == 0


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


def test_a_marked_requirement_keeps_its_marker() -> None:
    """uv replaces a requirement only with one of the same marker: `uv add --frozen
    flet-desktop==1.0.0` appended a second flet-desktop next to the declared
    `flet-desktop==1.0.1; sys_platform != 'emscripten'`, the lock had no solution, and the version
    change could never be applied. The requirement added carries the declared marker (uv keeps the
    extras itself), and a package switch carries the marker of the package it replaces."""
    marked = "flet-desktop==1.0.1; sys_platform != 'emscripten'"
    changes = cmd_apply.dependency_changes(
        _cfg("flet", version="1.0.0"), _applied("flet"), _declared(["flet[all]==1.0.1", marked], ["flet-cli[x] == 1.0.1 ; python_version >= '3.11'"])
    )
    assert changes.add == ["flet==1.0.0", "flet-desktop==1.0.0; sys_platform != 'emscripten'"]
    assert changes.add_dev == ["flet-cli==1.0.0; python_version >= '3.11'"]
    assert changes.describe() == "add flet==1.0.0, \"flet-desktop==1.0.0; sys_platform != 'emscripten'\", \"flet-cli==1.0.0; python_version >= '3.11'\" (dev)"
    machine = "platform_machine == 'x86_64' or platform_machine == 'AMD64'"
    changes = cmd_apply.dependency_changes(_cfg("raylib", package="raylib_sdl"), _applied("raylib"), _declared([f"raylib==6.0.1.0; {machine}"]))
    assert (changes.remove, changes.add) == (["raylib"], [f"raylib_sdl==6.0.1.0; {machine}"])
    assert cmd_apply.req_marker(f"raylib==6.0.1.0 ;{machine} ") == machine and cmd_apply.req_marker("flet[all]==1.0.1") == ""


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


def _mark(project: Project, requirement: str, marker: str) -> None:
    """Give a requirement of pyproject.toml a marker by hand (uv.lock locked with it)."""
    path = project.root / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    assert f'"{requirement}",' in text
    path.write_text(text.replace(f'"{requirement}",', f'"{requirement}; {marker}",', 1), encoding="utf-8", newline="\n")


@pytest.mark.parametrize("preset", ["flet", "raylib"])
def test_apply_keeps_the_marker_of_an_option_driven_requirement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: str) -> None:
    """A [preset.*] change of a requirement that carries a marker: a version change added an
    unmarked second pin (no solution: pyproject.toml restored, never applicable, and doctor and
    the hook reported it forever), and a package switch dropped the marker without a word."""
    project, uv = _project(tmp_path, monkeypatch, preset)
    if preset == "flet":
        marker = "sys_platform != 'emscripten'"
        _mark(project, "flet-desktop==1.0.1", marker)
        _mark(project, "flet-cli==1.0.1", marker)
        project.edit("preset.flet", "version", "1.0.0")
        expected = {"flet-desktop": f"flet-desktop==1.0.0 ; {marker}", "flet-cli": f"flet-cli==1.0.0 ; {marker}", "flet": "flet==1.0.0"}
    else:
        marker = "platform_machine == 'x86_64' or platform_machine == 'AMD64'"
        _mark(project, "raylib==6.0.1.0", marker)
        project.edit("preset.raylib", "package", "raylib_sdl")
        expected = {"raylib-sdl": f"raylib-sdl==6.0.1.0 ; {marker}"}
    uv.locked = (project.root / "pyproject.toml").read_bytes()
    assert _run(project) == 0
    data = project.pyproject()
    declared = [*data["project"]["dependencies"], *data["dependency-groups"]["dev"]]
    for name, requirement in expected.items():
        assert [r for r in declared if _parts(r)[0] == name] == [requirement]
    assert not any(_parts(r)[0] == "raylib" for r in declared)
    assert cmd_apply.pending(project.cfg()) == []


def test_a_failed_lock_restores_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch, "flet")
    original = (project.root / "pyproject.toml").read_bytes()
    project.edit("preset.flet", "version", "9.9.9")  # a version that does not exist
    uv.fail.add("lock")
    with pytest.raises(PytError) as e:
        _run(project)
    assert "pyproject.toml was restored" in str(e.value) and "./pyt apply again" in str(e.value)
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
    with pytest.raises(PytError, match="pyproject.toml was restored") as e:
        _run(project)
    assert e.value.code == 1
    assert ["remove", "--frozen", "raylib"] in uv.calls
    assert (project.root / "pyproject.toml").read_bytes() == original


@pytest.mark.parametrize(
    ("extra", "why"),
    [("big = " + "9" * 5000, "Exceeds the limit"), ("deep = " + "[" * 2000 + "]" * 2000, "nested too deeply")],
    ids=["an integer of 5000 digits", "arrays nested 2000 deep"],
)
def test_a_pyproject_tomllib_cannot_read_is_one_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str, why: str) -> None:
    """tomllib raises a plain ValueError for an integer over 4300 digits and a RecursionError for
    arrays nested about a thousand deep, never its TOMLDecodeError, and the readers of
    pyproject.toml caught only that one: apply, setup, doctor (and every command, through
    render.auto), lock, mode, rename, sync, check, test mypyc and the wheel ended in an
    internal-error traceback. Each says that pyproject.toml is not valid TOML, or reads it as a
    file it cannot use, as for pytemplate.toml (config.TOML_ERRORS)."""
    project, uv = _project(tmp_path, monkeypatch, "script", "alpha")
    pyproject = project.root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8") + f"\n[tool.mine]\n{extra}\n", encoding="utf-8", newline="\n")
    cfg = project.cfg()
    with pytest.raises(PytError, match="pyproject.toml is not valid TOML") as e:
        _run(project)
    assert why in str(e.value), str(e.value)
    assert "pyproject.toml is not valid TOML" in cmd_apply.pending(cfg)[0][0]  # doctor and the hook
    assert render.pyproject_outdated(cfg) and render.managed_values(pyproject.read_text(encoding="utf-8")) is not None
    with pytest.raises(PytError, match="pyproject.toml is not valid TOML"):
        render.write_pyproject(cfg)  # lock, mode, rename (ensure_lock)
    with pytest.raises(PytError, match="pyproject.toml is not valid TOML"):
        rename._plan_pyproject(project.root, rename.Names("alpha", "beta"))
    assert presets.project_name(pyproject.read_text(encoding="utf-8")) is None
    with pytest.raises(PytError, match="pyproject.toml is not valid TOML"):
        presets.read_pyproject()
    assert envs.left_out(envs.cpython_env(cfg)) == []  # sync: uv says what is wrong
    monkeypatch.setattr(lintc, "PYPROJECT", pyproject)
    assert lintc._runtime_dependencies() == set()  # check's librt rule
    assert cmd_dev.pytest_pythonpath(project.root) == []  # test mypyc: pytest says what is wrong
    with pytest.raises(PytError, match="wheel: pyproject.toml is not valid TOML"):
        wheel._read_toml(pyproject)


def test_a_missing_project_name_is_named_with_the_line_to_add(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pyproject.toml without its [project] name line: doctor and the hook said "[project] name =
    'None', but app.name = 'alpha'" with ./pyt apply as the fix, and apply then refused with "edit
    that line by hand", a line there is not. Both name the line to add."""
    project, uv = _project(tmp_path, monkeypatch, "script", "alpha")
    assert _run(project) == 0
    pyproject = project.root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('name = "alpha"\n', "", 1), encoding="utf-8", newline="\n")
    assert "name" not in project.pyproject()["project"]
    problem, hint = cmd_apply.pending(project.cfg())[0]
    assert "None" not in problem and problem == "pyproject.toml [project] has no name (app.name = 'alpha')", problem
    assert hint == 'add name = "alpha" to the [project] table of pyproject.toml', hint
    with pytest.raises(PytError, match=r'pyproject.toml has no \[project\] name: add name = "alpha" to that table'):
        _run(project)


def test_a_failed_lock_puts_the_project_name_line_back_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only pyproject.toml [project] name differed (a copy of the template's pyproject.toml in an
    upgrade): apply wrote that line before it took the bytes it puts back when the re-lock fails,
    so a failed `uv lock` left the rewritten name next to the old uv.lock, which every `uv run
    --locked` then refused, and the message did not say pyproject.toml had changed. The line is
    put back with the rest."""
    project, uv = _project(tmp_path, monkeypatch, "script", "alpha")
    assert _run(project) == 0  # the record: alpha
    pyproject = project.root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('name = "alpha"', 'name = "myapp"', 1), encoding="utf-8", newline="\n")
    uv.locked = original = pyproject.read_bytes()  # the lock of that pyproject.toml
    uv.fail.add("lock")
    with pytest.raises(PytError, match="pyproject.toml was restored") as e:
        _run(project)
    assert "./pyt apply again" in str(e.value)
    assert pyproject.read_bytes() == original
    uv.fail.clear()
    assert _run(project) == 0 and project.pyproject()["project"]["name"] == "alpha"


def test_the_record_follows_the_lock_when_a_later_step_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The option change is locked, then `uv sync` fails: pyproject.toml and uv.lock hold the new
    package, and so must the record, or reverting the option kept both raylib distributions."""
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    assert _run(project) == 0
    project.edit("preset.raylib", "package", "raylib_software")
    uv.fail.add("sync")
    with pytest.raises(PytError, match="sync"):
        _run(project)
    assert [r for r in project.pyproject()["project"]["dependencies"] if r.startswith("raylib")] == ["raylib-software==6.0.1.0"]
    assert cmd_apply.load_record() == {"name": "alpha", "preset": "raylib", "dependencies": ["raylib_software==6.0.1.0"], "dev": []}
    uv.fail.clear()
    project.edit("preset.raylib", "package", "raylib")
    assert _run(project) == 0
    assert [r for r in project.pyproject()["project"]["dependencies"] if r.startswith("raylib")] == ["raylib==6.0.1.0"]
    assert cmd_apply.pending(project.cfg()) == []


def test_a_rename_whose_lock_fails_is_tidied_already(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ruff tidy-up of the renamed files came after the lock, the sync and render: a lock that
    failed left the rename to the next apply, which finds the names in line and tidies nothing
    (the files ruff had formatted stayed unwrapped under a longer name, and the next commit's hook
    refused them). It runs right after the rename, before any step that can fail."""
    project, uv = _project(tmp_path, monkeypatch)
    tidied: list[str] = []
    monkeypatch.setattr(rename, "tidy_before", lambda cfg, plan_, root=None: rename.Tidy(set(), set()))
    monkeypatch.setattr(rename, "tidy_after", lambda cfg, plan_, clean, root=None: tidied.append(cfg.app.name))
    project.edit("app", "name", "beta")
    uv.fail.add("lock")
    with pytest.raises(PytError, match="the app is already renamed"):
        _run(project)
    assert tidied == ["beta"]  # with the renamed configuration, before the lock failed
    uv.fail.clear()
    assert _run(project) == 0 and tidied == ["beta"]  # the apply that finishes it has nothing to tidy


def test_a_record_it_cannot_write_after_the_rename_says_the_app_is_renamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """state.json that cannot be written right after apply renamed the app (another user's, read
    only, locked on Windows): apply ended with only `error: cannot write .pytemplate/state.json`,
    the ruff tidy-up skipped and nothing said the app was renamed. It tidies first, then says the
    app is already renamed and to run apply again, which finishes the job once state.json can be
    written (the record still names the old app: no reference to it is left, so no folder moved by
    hand either)."""
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0  # the record: alpha
    tidied: list[str] = []
    monkeypatch.setattr(rename, "tidy_before", lambda cfg, plan_, root=None: rename.Tidy(set(), set()))
    monkeypatch.setattr(rename, "tidy_after", lambda cfg, plan_, clean, root=None: tidied.append(cfg.app.name))
    project.edit("app", "name", "beta")
    real = cmd_apply.save_record

    def unwritable(record: dict[str, Any], path: Path | None = None) -> bool:
        raise PytError("cannot write .pytemplate/state.json: Permission denied")

    monkeypatch.setattr(cmd_apply, "save_record", unwritable)
    count = len(uv.calls)
    with pytest.raises(PytError, match="cannot write .pytemplate/state.json") as e:
        _run(project)
    assert "the app is already renamed: fix the problem above and run ./pyt apply again" in str(e.value), str(e.value)
    assert tidied == ["beta"] and uv.changing(count) == []  # tidied; no lock, sync or render after it
    assert (project.root / "src" / "beta").is_dir() and not (project.root / "src" / "alpha").exists()
    monkeypatch.setattr(cmd_apply, "save_record", real)
    assert _run(project) == 0 and tidied == ["beta"]  # the apply that finishes it renames nothing more
    record = cmd_apply.load_record()
    assert record is not None and record["name"] == "beta" and cmd_apply.pending(project.cfg()) == []


def test_the_record_follows_a_rename_when_the_lock_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rename is done before the lock: when the lock fails, the record keeps the applied
    options under the NEW name (named after the old one it would no longer be trusted)."""
    project, uv = _project(tmp_path, monkeypatch, "raylib")
    project.edit("preset.raylib", "package", "raylib_sdl")
    assert _run(project) == 0
    project.edit("app", "name", "beta")
    uv.fail.add("lock")
    with pytest.raises(PytError, match="the app is already renamed"):
        _run(project)
    uv.fail.clear()
    assert cmd_apply.load_record() == {"name": "beta", "preset": "raylib", "dependencies": ["raylib_sdl==6.0.1.0"], "dev": []}
    project.edit("preset.raylib", "package", "raylib_software")
    assert _run(project) == 0
    assert [r for r in project.pyproject()["project"]["dependencies"] if r.startswith("raylib")] == ["raylib-software==6.0.1.0"]


@pytest.mark.parametrize("step", ["lock", "sync"])
def test_an_interrupted_apply_after_the_rename_says_how_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], step: str
) -> None:
    """A Ctrl+C or SIGTERM once apply had renamed the app (during the lock, or a later step) said
    only `error: terminated (SIGTERM)`, the project half-applied: it says how to finish, as
    `./pyt rename` does, and the files stay renamed (the record with them)."""
    project, uv = _project(tmp_path, monkeypatch)
    project.edit("app", "name", "beta")

    def interrupted(env: envs.PyEnv, args: Sequence[str | Path], **kw: Any) -> subprocess.CompletedProcess[str]:
        if [str(a) for a in args][:2] in ([step], [step, "--locked"]):
            raise proc.Interrupted(143, 15)
        return uv(env, args, **kw)

    monkeypatch.setattr(envs, "uv", interrupted)
    with pytest.raises(proc.Interrupted):
        _run(project)
    assert "the app is already renamed: run ./pyt apply to finish" in capsys.readouterr().err
    assert (project.root / "src" / "beta").is_dir() and not (project.root / "src" / "alpha").exists()
    record = cmd_apply.load_record()
    assert record is not None and record["name"] == "beta"
    # a plain apply that is interrupted says nothing of a rename
    monkeypatch.setattr(envs, "uv", interrupted)
    with pytest.raises(proc.Interrupted):
        _run(project)
    assert "already renamed" not in capsys.readouterr().err


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
        raise PytError("the code uses syntax that does not exist in Python 3.11 (see above); fix it before enabling PyPy")

    monkeypatch.setattr(cmd_apply, "cmd_mode_precheck", precheck)
    count = len(uv.calls)
    with pytest.raises(PytError) as e:
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
    with pytest.raises(PytError, match="cannot write") as e:
        cmd_apply.save_record(RECORD, state)
    assert e.value.code == 2


def test_a_pyproject_that_cannot_be_written_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch, "flet")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('"alpha"', '"other"'), encoding="utf-8", newline="\n")
    real = Path.write_bytes

    def write_bytes(self: Path, data: Any) -> int:
        if "pyproject.toml" in self.name:  # the temporary file next to it (project.write_whole)
            raise PermissionError(13, "Permission denied")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    with pytest.raises(PytError, match="cannot write pyproject.toml: Permission denied") as e:
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


def test_dry_run_of_a_rename_whose_project_name_is_set_already(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """app.name and pyproject.toml [project] name both edited by hand to the new name: the rename
    leaves pyproject.toml as it is, and the --dry-run said "would rewrite [project] name"."""
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0  # the record: alpha
    project.edit("app", "name", "delta")
    pyproject = project.root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('name = "alpha"', 'name = "delta"', 1), encoding="utf-8", newline="\n")
    before = pyproject.read_bytes()
    monkeypatch.setattr(proc, "DRY_RUN", True)
    capsys.readouterr()
    assert _run(project) == 0
    err = capsys.readouterr().err
    assert "would rename 'alpha' -> 'delta'" in err and "pyproject.toml   unchanged" in err, err
    monkeypatch.setattr(proc, "DRY_RUN", False)
    assert _run(project) == 0 and pyproject.read_bytes() == before  # what the real run writes there: nothing


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
    assert "==> setup" in err and "done. Try: ./pyt run" in err


@pytest.mark.parametrize("command", ["apply", "setup"])
def test_unknown_arguments_are_refused_before_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    project, uv = _project(tmp_path, monkeypatch)
    before = project.snapshot()
    for bad in (["--frce"], ["cpython"], ["--force", "x"]):
        with pytest.raises(PytError, match=f"{command}: unrecognized arguments") as e:
            _run(project, *bad, command=command)
        assert e.value.code == 2
    assert project.snapshot() == before and uv.calls == []


def test_commands_table() -> None:
    for name in ("apply", "setup"):
        command = cli.COMMANDS[name]
        assert (command.render, command.group, command.usage) == (False, "Environment", "[--force]")
    assert cli.COMMANDS["apply"].module == "cmd_apply" and cli.COMMANDS["setup"].func == "cmd_setup"
    assert cli.COMMANDS["rename"].render is False  # rename renders at the end, never before its checks
    assert "cmd_apply.doctor(cfg, check)" in inspect.getsource(cmd_env._project_files)  # doctor's project steps


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
    assert "note: .venv-pypy is not used by this configuration" in err and "./pyt clean --envs" in err
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
        f"./pyt apply  (renames '{old}' -> '{new}')",
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


def test_a_hand_edited_name_in_an_inline_app_table_is_renamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pytemplate.toml with `app = { name = ..., ... }` (valid TOML, which doctor and the hook
    accept): rename's set_value cannot edit an inline table, and its message asked to set the
    name by hand; once it was set, apply and rename planned the rename again and set it again,
    with the same refusal, while doctor and the hook asked for ./pyt apply on every commit. A
    name already set is left alone; `./pyt rename` from such a file says to set it by hand,
    then ./pyt apply, which renames the rest."""
    project, _ = _project(tmp_path, monkeypatch, "script", "alpha")
    text = project.config_file.read_text(encoding="utf-8")
    table = re.search(r"^\[app\]\n(?:.+\n)+", text, re.M)
    assert table is not None
    inline = 'app = { name = "alpha", preset = "script", gui = false, assets = "" }\n'
    project.config_file.write_text(text[: table.start()] + inline + text[table.end() :], encoding="utf-8", newline="\n")
    assert project.cfg().app.name == "alpha" and cmd_apply.pending(project.cfg(), hook=False) == []
    with pytest.raises(PytError, match=r'set name = "beta" there by hand, then run \./pyt apply') as e:
        rename.plan(project.root, "alpha", "beta")
    assert "inline `app = {...}` table" in str(e.value)
    project.config_file.write_text(project.config_file.read_text(encoding="utf-8").replace('name = "alpha"', 'name = "beta"', 1), encoding="utf-8", newline="\n")
    assert cmd_apply.pending(project.cfg(), hook=False)[0][0] == "app.name = 'beta' is not applied: the package is still src/alpha/"
    assert _run(project) == 0
    assert (project.root / "src" / "beta").is_dir() and not (project.root / "src" / "alpha").exists()
    assert project.config_file.read_text(encoding="utf-8").count('app = { name = "beta", preset = "script"') == 1
    assert project.pyproject()["project"]["name"] == "beta" and cmd_apply.pending(project.cfg(), hook=False) == []


@pytest.mark.parametrize(("old", "new"), [("Flet-App", "flet-app"), ("alpha", "Alpha"), ("my_app", "My-App")])
def test_a_hand_edit_of_the_names_spelling_never_says_the_package_must_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str, new: str) -> None:
    """app.name edited to another spelling of the same package (Flet-App -> flet-app): doctor
    said "app.name = 'flet-app' is not applied: the package is still src/flet_app/", the very
    folder app.name's package already is, as if the package had to move. It says the project is
    still called by the old name; apply renames it as before."""
    project, _ = _project(tmp_path, monkeypatch, "flet", old)
    project.edit("app", "name", new)
    assert cmd_apply.pending(project.cfg())[0] == (
        f"app.name = '{new}' is not applied: the project is still called '{old}'",
        f"./pyt apply  (renames '{old}' -> '{new}')",
    )
    assert _run(project) == 0
    assert project.pyproject()["project"]["name"] == new and cmd_apply.pending(project.cfg()) == []


def test_only_the_pyproject_name_differs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch, "flet", "alpha")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('"alpha"', '"other"'), encoding="utf-8", newline="\n")
    uv.locked = path.read_bytes()  # uv.lock was made with that name
    assert project.pyproject()["tool"]["flet"]["product"] == "other"
    assert cmd_apply.pending(project.cfg()) == [("pyproject.toml [project] name = 'other', but app.name = 'alpha'", "./pyt apply")]
    assert _run(project) == 0
    data = project.pyproject()
    assert data["project"]["name"] == "alpha" and data["tool"]["flet"]["product"] == "alpha"
    assert (project.root / "src" / "alpha").is_dir() and not (project.root / "src" / "other").exists()
    assert ["lock"] in uv.changing() and cmd_apply.pending(project.cfg()) == []


def test_a_pyproject_name_that_differs_only_in_spelling_is_put_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """app.name = "MyApp" (src/myapp/, the record MyApp) and only pyproject.toml [project] name
    changed to "myapp" (by hand, or a tool that lowercases it): the record says app.name is
    current, so apply puts that line back. It took "myapp" for the real name: doctor and the hook
    reported app.name as not applied, apply wanted a clean tree for a rename myapp -> MyApp that
    rewrote the user's prose, and `rename Other` started from "myapp"."""
    project, uv = _project(tmp_path, monkeypatch, "script", "MyApp")
    assert _run(project) == 0  # the record: MyApp
    prose = project.root / "tests" / "test_prose.py"
    prose.write_text("# welcome to myapp, the best app\n", encoding="utf-8")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('name = "MyApp"', 'name = "myapp"', 1), encoding="utf-8", newline="\n")
    uv.locked = path.read_bytes()
    cfg = project.cfg()
    assert cmd_apply.applied_name(cfg) is None  # what rename starts from: app.name itself
    assert cmd_apply.pending(cfg) == [("pyproject.toml [project] name = 'myapp', but app.name = 'MyApp'", "./pyt apply")]
    assert _run(project) == 0
    assert project.pyproject()["project"]["name"] == "MyApp" and cmd_apply.pending(project.cfg()) == []
    assert prose.read_text(encoding="utf-8") == "# welcome to myapp, the best app\n"  # no rename touched it


def test_both_names_edited_by_hand_are_renamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """app.name and pyproject.toml [project] name both edited to the new name: the record (the old
    name) matched neither, so apply skipped the rename, recorded the new name (the real one lost),
    warned that src/beta/ does not exist and said "applied". The record's package is still in
    src/: it is this project's record, and apply renames from it."""
    project, _ = _project(tmp_path, monkeypatch, "script", "alpha")
    assert _run(project) == 0  # the record: alpha
    project.edit("app", "name", "beta")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('name = "alpha"', 'name = "beta"', 1), encoding="utf-8", newline="\n")
    cfg = project.cfg()
    assert cmd_apply.applied_name(cfg) == "alpha"
    assert cmd_apply.pending(cfg)[0] == ("app.name = 'beta' is not applied: the package is still src/alpha/", "./pyt apply  (renames 'alpha' -> 'beta')")
    assert _run(project) == 0
    skeleton = presets.skeleton("script", "beta")
    assert _owned(project.root) == {k: v for k, v in skeleton.items() if k.split("/")[0] in ("src", "tests", "typings")}
    record = cmd_apply.load_record()
    assert record is not None and record["name"] == "beta" and cmd_apply.pending(project.cfg()) == []


@pytest.mark.parametrize("imports", ["kept", "fixed"], ids=["a plain move", "an IDE's move"])
def test_a_package_folder_moved_by_hand_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, imports: str) -> None:
    """app.name set after src/alpha/ was moved to src/beta/ by hand (an IDE's folder rename): apply
    took the "only [project] name differs" path, rewrote that line, recorded beta and said
    "applied" (doctor too), while pytemplate.toml (compile.modules, deploy.wheel.entry...) and,
    without the IDE, the imports still named alpha. It refuses before any write, as rename does,
    with the way out; moved back, apply renames it all."""
    project, uv = _project(tmp_path, monkeypatch, "flet", "alpha")
    assert _run(project) == 0  # the record: alpha
    src = project.root / "src"
    (src / "alpha").rename(src / "beta")
    if imports == "fixed":  # what the IDE's refactoring rewrites
        for path in [*src.rglob("*.py"), *(project.root / "tests").rglob("*.py")]:
            text = path.read_text(encoding="utf-8")
            path.write_text(re.sub(r"(import|from) alpha\b", r"\1 beta", text), encoding="utf-8", newline="\n")
    project.edit("app", "name", "beta")
    cfg = project.cfg()
    before, count = project.snapshot(), len(uv.calls)
    for dry in (False, True):
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        with pytest.raises(PytError, match=r"src/alpha/ was moved to src/beta/ by hand") as e:
            _run(project)
        assert e.value.code == 2 and "Move it back to src/alpha/, then ./pyt apply" in str(e.value)
    monkeypatch.setattr(proc, "DRY_RUN", False)
    assert project.snapshot() == before and uv.changing(count) == []
    problem, hint = cmd_apply.pending(cfg)[0]
    assert "src/alpha/ was moved to src/beta/ by hand" in problem and hint.startswith("move it back to src/alpha/"), (problem, hint)
    for new in ("beta", "gamma"):  # rename, too, never starts from the moved folder
        with pytest.raises(PytError, match=r"src/alpha/ was moved to src/beta/ by hand"):
            rename.cmd_rename(cfg, [new])
    assert project.snapshot() == before
    (src / "beta").rename(src / "alpha")  # the way out
    assert _run(project) == 0
    skeleton = presets.skeleton("flet", "beta")
    assert project.config_file.read_bytes() == skeleton["pytemplate.toml"]  # compile.modules, entry... renamed
    if imports == "kept":
        assert _owned(project.root) == {k: v for k, v in skeleton.items() if k.split("/")[0] in ("src", "tests", "typings")}
    assert cmd_apply.pending(project.cfg()) == []


def test_a_package_moved_by_hand_that_left_its_caches_behind_is_still_a_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """src/alpha/ copied to src/beta/, then `git rm -r src/alpha` (or a move of its .py files):
    git leaves the untracked __pycache__ folders, and src/alpha/ holding only them was taken for
    the package. apply then refused src/beta/, which held all the code, as "another package" and
    said to move or delete it, and doctor and the hook said the same. A folder that holds no
    module is no package: the move by hand is diagnosed, with the folder left behind named."""
    project, uv = _project(tmp_path, monkeypatch, "script", "alpha")
    assert _run(project) == 0  # the record: alpha
    src = project.root / "src"
    shutil.copytree(src / "alpha", src / "beta")
    for path in sorted((src / "alpha").rglob("*.py")):  # git rm -r: the tracked files go, the caches stay
        (path.parent / "__pycache__").mkdir(exist_ok=True)
        (path.parent / "__pycache__" / f"{path.stem}.cpython-314.pyc").write_bytes(b"\x00")
        path.unlink()
    project.edit("app", "name", "beta")
    cfg = project.cfg()
    assert rename.package_dir(src, "alpha") is None and rename.package_dir(src, "beta") == src / "beta"
    with pytest.raises(PytError, match=r"src/alpha/ was moved to src/beta/ by hand") as e:
        _run(project)
    assert "another package" not in str(e.value) and "move away the src/alpha/ left behind first: it holds no module" in str(e.value)
    problem, hint = cmd_apply.pending(cfg)[0]
    assert "src/alpha/ was moved to src/beta/ by hand" in problem and "move away the src/alpha/ left behind first" in hint, (problem, hint)
    shutil.rmtree(src / "alpha")
    (src / "beta").rename(src / "alpha")  # the way out
    assert _run(project) == 0 and cmd_apply.pending(project.cfg()) == []
    assert (src / "beta" / "__init__.py").is_file()


def test_a_package_folder_moved_by_hand_with_both_names_edited_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The folder moved by hand, then app.name AND pyproject.toml [project] name edited (a rename
    by hand edits both files): the record (alpha) named neither line and its package was gone, so
    it was taken for a foreign record and ignored. apply re-locked, rendered, said "applied" and
    recorded beta, the last trace of alpha, and doctor and the hook agreed, while the imports,
    compile.modules and the wheel entry still named alpha (./pyt run: No module named 'alpha').
    It is refused as with app.name alone; moved back, apply renames it all."""
    project, uv = _project(tmp_path, monkeypatch, "flet", "alpha")
    assert _run(project) == 0  # the record: alpha
    src = project.root / "src"
    (src / "alpha").rename(src / "beta")
    project.edit("app", "name", "beta")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('name = "alpha"', 'name = "beta"', 1), encoding="utf-8", newline="\n")
    cfg = project.cfg()
    assert project.pyproject()["project"]["name"] == "beta" and cmd_apply.trusted_record(cfg, "beta") is None
    before, count = project.snapshot(), len(uv.calls)
    for dry in (False, True):
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        with pytest.raises(PytError, match=r"src/alpha/ was moved to src/beta/ by hand") as e:
            _run(project)
        assert e.value.code == 2 and "Move it back to src/alpha/, then ./pyt apply" in str(e.value), str(e.value)
    monkeypatch.setattr(proc, "DRY_RUN", False)
    assert project.snapshot() == before and uv.changing(count) == []
    record = cmd_apply.load_record()
    assert record is not None and record["name"] == "alpha"  # never replaced by the new name
    problem, hint = cmd_apply.pending(cfg)[0]
    assert "src/alpha/ was moved to src/beta/ by hand" in problem and hint.startswith("move it back to src/alpha/"), (problem, hint)
    for new in ("beta", "gamma"):  # rename said "nothing to do" for beta
        with pytest.raises(PytError, match=r"src/alpha/ was moved to src/beta/ by hand"):
            rename.cmd_rename(cfg, [new])
    assert project.snapshot() == before
    (src / "beta").rename(src / "alpha")  # the way out
    assert _run(project) == 0
    skeleton = presets.skeleton("flet", "beta")
    assert project.config_file.read_bytes() == skeleton["pytemplate.toml"]  # compile.modules, entry... renamed
    assert _owned(project.root) == {k: v for k, v in skeleton.items() if k.split("/")[0] in ("src", "tests", "typings")}
    assert project.pyproject()["project"]["name"] == "beta" and cmd_apply.pending(project.cfg()) == []


@pytest.mark.parametrize(
    ("left", "text", "refers"),
    [
        (None, "", False),
        ("tests/test_old.py", "import alpha.core.fractal as fractal\n\nSIZE = fractal.SIZE\n", True),
        ("tests/test_patch.py", 'from unittest import mock\n\nTARGET = "alpha.core.fractal.render"\n\n\ndef test_render() -> None:\n    with mock.patch(TARGET):\n        pass\n', True),
        ("tests/test_prose.py", '"""Tests of beta, which replaces alpha (the alpha app is gone)."""\n\n# alpha was its name\n', False),
    ],
    ids=["no reference left", "a test still imports the old package", "a test names a module of it in a string", "only prose names it"],
)
def test_a_package_written_to_replace_the_old_one_is_no_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, left: str | None, text: str, refers: bool
) -> None:
    """src/alpha/ deleted and src/beta/ written in its place, with app.name = "beta" and no
    reference to alpha left (the imports, compile.modules, the wheel entry): apply, rename, doctor
    and the hook took it for a folder moved by hand and refused with "move it back to src/alpha/",
    which only led to another refusal (src/beta/ is another package), and the hook blocked every
    commit. With no reference left it is applied as an edit of app.name whose package is in place
    already; with one left (an import, or a module of the package named in a string, which a
    moved folder leaves too: mock.patch then failed at run time), the refusal names where it is
    and the way out of a replacement too. Prose that names the old app is no reference."""
    project, uv = _project(tmp_path, monkeypatch, "flet", "alpha")
    assert _run(project) == 0  # the record: alpha
    shutil.rmtree(project.root / "src" / "alpha")
    for rel_path, data in presets.skeleton("flet", "beta").items():  # the new package, written anew
        if rel_path.split("/")[0] in ("src", "tests") or rel_path == "pytemplate.toml":
            (project.root / rel_path).parent.mkdir(parents=True, exist_ok=True)
            (project.root / rel_path).write_bytes(data)
    if left is not None:
        (project.root / left).write_text(text, encoding="utf-8", newline="\n")
    cfg = project.cfg()
    assert cfg.app.name == "beta" and project.pyproject()["project"]["name"] == "alpha"
    if left is not None and refers:
        before = project.snapshot()
        with pytest.raises(PytError, match=r"src/alpha/ was moved to src/beta/ by hand") as e:
            _run(project)
        assert f"still name alpha: {left}" in str(e.value) and "Move it back to src/alpha/, then ./pyt apply" in str(e.value), str(e.value)
        assert "if src/beta/ replaces src/alpha/, make them name beta, then ./pyt apply" in str(e.value), str(e.value)
        problem, hint = cmd_apply.pending(cfg)[0]
        assert "src/alpha/ was moved to src/beta/ by hand" in problem and left in hint, (problem, hint)
        with pytest.raises(PytError, match=r"src/alpha/ was moved to src/beta/ by hand"):
            rename.cmd_rename(cfg, ["beta"])
        assert project.snapshot() == before
        (project.root / left).unlink()  # the way out of a replacement
    assert cmd_apply.pending(cfg) == [("pyproject.toml [project] name = 'alpha', but app.name = 'beta'", "./pyt apply")]
    with pytest.raises(PytError, match=r"the app is already called 'beta' \(src/beta/\), but pyproject.toml \[project\] name = 'alpha'"):
        rename.cmd_rename(cfg, ["beta"])  # rename names the way out: apply
    assert _run(project) == 0
    assert project.pyproject()["project"]["name"] == "beta"
    record = cmd_apply.load_record()
    assert record is not None and record["name"] == "beta" and cmd_apply.pending(project.cfg()) == []


@pytest.mark.parametrize("variable", ["UV_FROZEN", "UV_LOCKED"])
@pytest.mark.parametrize("edit", ["app.name", "[project] name", "[preset.flet] version"])
def test_a_relock_the_users_frozen_lock_refuses_is_refused_before_the_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], variable: str, edit: str
) -> None:
    """Under the user's UV_FROZEN or UV_LOCKED `uv lock` writes nothing, so ensure_lock refuses a
    needed re-lock. apply made that refusal only after it had renamed the app (or rewritten
    [project] name, or edited the dependencies): the app renamed next to a stale uv.lock, and the
    dry run said "would re-lock". It is refused before the first write, in the dry run too."""
    project, uv = _project(tmp_path, monkeypatch, "flet", "alpha")
    assert _run(project) == 0
    if edit == "app.name":
        project.edit("app", "name", "beta")
        why = "the project name changes"
    elif edit == "[project] name":  # a copy of the template's pyproject.toml (an upgrade)
        pyproject = project.root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('name = "alpha"', 'name = "myapp"', 1), encoding="utf-8", newline="\n")
        uv.locked = pyproject.read_bytes()  # the lock of that pyproject.toml
        why = "the project name changes"
    else:
        project.edit("preset.flet", "version", "1.0.0")
        why = "the dependencies change"
    monkeypatch.setenv(variable, "1")
    before, count = project.snapshot(), len(uv.calls)
    for dry in (True, False):
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        with pytest.raises(PytError, match=f"uv.lock must follow pyproject.toml \\({re.escape(why)}\\), but {variable} is set") as e:
            _run(project)
        assert e.value.code == 2
    assert project.snapshot() == before and uv.changing(count) == []
    monkeypatch.setattr(proc, "DRY_RUN", False)
    monkeypatch.delenv(variable)
    capsys.readouterr()
    assert _run(project) == 0  # unset: applied
    assert cmd_apply.pending(project.cfg()) == []


def test_the_dry_run_names_a_relock_the_users_frozen_lock_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Without a write before the lock (only the managed parts or a stale lock), ensure_lock
    itself refuses and puts pyproject.toml back: the dry run says so on its uv.lock row."""
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0
    uv.locked = b""  # uv.lock is stale
    monkeypatch.setenv("UV_FROZEN", "1")
    monkeypatch.setattr(proc, "DRY_RUN", True)
    capsys.readouterr()
    assert _run(project) == 0
    assert "would re-lock (uv lock): uv.lock is not up to date, which apply refuses while UV_FROZEN is set" in capsys.readouterr().err
    monkeypatch.setattr(proc, "DRY_RUN", False)
    before = project.snapshot()
    with pytest.raises(PytError, match="UV_FROZEN is set"):
        _run(project)
    assert project.snapshot() == before


@pytest.mark.parametrize("record", [True, False])
def test_a_name_of_another_package_in_src_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: bool) -> None:
    """app.name set by hand to the name of another package of the project (src/helpers/): apply
    rewrote only pyproject.toml [project] name and said "applied", the app still in src/alpha/.
    It refuses, as `./pyt rename helpers` does, and writes nothing."""
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
        with pytest.raises(PytError, match=r"src/helpers/ already exists and is not the app's package") as e:
            _run(project)
        assert e.value.code == 2 and 'Put back app.name = "alpha"' in str(e.value)
    assert project.snapshot() == before and uv.changing(count) == []
    problem, hint = cmd_apply.pending(project.cfg())[0]
    assert "names src/helpers/, another package: the app is 'alpha'" in problem and 'put back app.name = "alpha"' in hint
    project.edit("app", "name", "alpha")
    assert cmd_apply.pending(project.cfg()) == []


@pytest.mark.parametrize("project_name", ["helpers", "gamma"])
def test_both_names_edited_onto_another_package_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_name: str) -> None:
    """app.name and pyproject.toml [project] name both edited by hand to another package of src/
    (src/helpers/), or app.name to it and [project] name to a third name: the record (alpha, whose
    package is still in src/) matched neither line and was dropped as foreign. Apply said
    "applied" and recorded {"name": "helpers"} (the real name lost), doctor and the hook passed,
    and `rename` then planned to move src/helpers/. The record's package is in src/: it is this
    project's record, and apply, pending and rename refuse as for app.name edited alone."""
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0  # the record: alpha
    helpers = project.root / "src" / "helpers"
    helpers.mkdir()
    (helpers / "__init__.py").write_text('"""Helpers."""\n', encoding="utf-8")
    project.edit("app", "name", "helpers")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('name = "alpha"', f'name = "{project_name}"', 1), encoding="utf-8", newline="\n")
    uv.locked = path.read_bytes()
    before, count = project.snapshot(), len(uv.calls)
    for dry in (False, True):
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        with pytest.raises(PytError, match=r"src/helpers/ already exists and is not the app's package") as e:
            _run(project)
        assert e.value.code == 2 and 'Put back app.name = "alpha"' in str(e.value)
    monkeypatch.setattr(proc, "DRY_RUN", False)
    assert project.snapshot() == before and uv.changing(count) == []
    assert cmd_apply.load_record() == cmd_apply.record_of(_cfg_text(presets.skeleton("script", "alpha")["pytemplate.toml"].decode("utf-8")))
    problem, hint = cmd_apply.pending(project.cfg())[0]
    assert "names src/helpers/, another package: the app is 'alpha'" in problem and 'put back app.name = "alpha"' in hint
    with pytest.raises(PytError, match=r"names src/helpers/, another package: the app is 'alpha'") as e:
        rename.cmd_rename(project.cfg(), ["gamma", "--force"])
    assert e.value.code == 2 and project.snapshot() == before


def test_a_pyproject_name_edited_to_another_package_is_put_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pyproject.toml [project] name set by hand to another package of src/: the record says
    app.name is the app, so apply puts the pyproject.toml line back (it refused, advising to move
    the real app away, and the hook blocked every commit)."""
    project, uv = _project(tmp_path, monkeypatch)
    assert _run(project) == 0  # the record: alpha
    engine = project.root / "src" / "engine"
    engine.mkdir()
    (engine / "__init__.py").write_text('"""Engine."""\n', encoding="utf-8")
    path = project.root / "pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('name = "alpha"', 'name = "engine"', 1), encoding="utf-8", newline="\n")
    uv.locked = path.read_bytes()
    assert cmd_apply.pending(project.cfg()) == [("pyproject.toml [project] name = 'engine', but app.name = 'alpha'", "./pyt apply")]
    assert _run(project) == 0
    assert project.pyproject()["project"]["name"] == "alpha" and cmd_apply.pending(project.cfg()) == []
    assert (project.root / "src" / "alpha").is_dir() and engine.is_dir()
    # without a record either line may be the edited one: refused, and the message says both ways
    path.write_text(path.read_text(encoding="utf-8").replace('name = "alpha"', 'name = "engine"', 1), encoding="utf-8", newline="\n")
    (project.root / ".pytemplate" / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PytError, match="src/alpha/ already exists and is not the app's package") as e:
        _run(project)
    assert 'put back name = "alpha" there' in str(e.value)
    assert 'put back name = "alpha" there' in cmd_apply.pending(project.cfg())[0][1]


@pytest.mark.parametrize("record", [True, False])
@pytest.mark.parametrize(("old", "new"), [("script", "flet"), ("flet", "raylib"), ("raylib", "script"), ("flet", "script")])
def test_a_hand_edited_preset_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str, new: str, record: bool) -> None:
    project, uv = _project(tmp_path, monkeypatch, old)
    if record:
        _as_new(project)  # the record `./pyt new` writes (else: it was lost)
    project.edit("app", "preset", new)
    before = project.snapshot()
    for command in ("apply", "setup"):
        with pytest.raises(PytError) as e:
            _run(project, command=command)
        assert e.value.code == 2
        message = str(e.value)
        assert f"changed from '{old}' to '{new}' by hand" in message
        assert f'Put back app.preset = "{old}"' in message and f"./pyt new DIR --preset {new}" in message
    monkeypatch.setattr(proc, "DRY_RUN", True)
    with pytest.raises(PytError, match="by hand"):
        _run(project)
    assert project.snapshot() == before and uv.calls == []
    label = cmd_apply.pending(project.cfg())[0][0]
    if record or old != "script":
        assert label == f"app.preset = '{new}' but the project was made with the '{old}' preset"
    else:  # script leaves no trace in pyproject.toml: without a record the refusal is a guess, and says so
        assert label.startswith(f"app.preset = '{new}', but pyproject.toml holds no trace of that preset")


def test_an_invalid_hand_edited_name_is_refused_before_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, uv = _project(tmp_path, monkeypatch)
    before = project.snapshot()
    for bad, message in (("rich", "dependency"), ("json", "standard library"), ("compression", "standard library"), ("class", "keyword")):
        project.edit("app", "name", bad)
        with pytest.raises(PytError, match=message) as e:
            _run(project)
        assert e.value.code == 2 and str(e.value).startswith("app.name: ")
    project.edit("app", "name", "alpha")
    assert project.snapshot() == before and uv.calls == []


def test_a_upx_path_of_a_user_this_machine_lacks_is_a_note_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A shared pytemplate.toml with deploy.upx.path = "~builder/bin/upx": on a machine without
    that user, expanduser raises RuntimeError, and doctor (no line at all) and the end of apply
    ended in an internal-error traceback."""
    project, _ = _project(tmp_path, monkeypatch)
    project.edit("deploy.upx", "path", "~pt-no-such-user-here/bin/upx")
    problems = cmd_apply.reference_problems(project.cfg())
    assert "deploy.upx.path = '~pt-no-such-user-here/bin/upx' does not exist: builds with UPX fail" in problems
    lines: list[tuple[bool | None, str]] = []
    cmd_apply.doctor(project.cfg(), lambda passed, label, hint="": lines.append((passed, label)))
    assert (None, "deploy.upx.path = '~pt-no-such-user-here/bin/upx' does not exist: builds with UPX fail") in lines
    assert _run(project) == 0
    assert "deploy.upx.path = '~pt-no-such-user-here/bin/upx' does not exist" in capsys.readouterr().err


def test_missing_package_is_a_warning_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, uv = _project(tmp_path, monkeypatch)
    shutil.rmtree(project.root / "src" / "alpha")
    assert _run(project) == 0
    err = capsys.readouterr().err
    assert "warning: src/alpha/ does not exist" in err and "compile.modules: alpha.core not found" in err
    assert cmd_apply.pending(project.cfg())[0][0].startswith("src/alpha/ does not exist")


def test_an_entry_of_src_that_cannot_be_read_is_no_missing_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link in src/ that cannot be followed (a loop, a dead mount, a folder the user may not
    enter) made doctor say `src/alpha/ does not exist (... src/ has alpha/)`, the hook blocked every
    commit with it, and on a Python 3.11 runner Path.is_dir raised for the link (EACCES): doctor
    ended in an internal error."""
    project, _ = _project(tmp_path, monkeypatch)
    src = project.root / "src"
    try:
        (src / "loop").symlink_to("loop")
    except OSError as e:
        if sys.platform != "win32":
            raise
        print(f"no symlink here ({e})")
    cfg = project.cfg()
    assert cmd_apply.missing_package(cfg) is None and cmd_apply.pending(cfg) == []
    (src / "alpha").rename(src / "other")  # now the app package is missing for real
    (src / "data").mkdir()
    real = Path.is_dir

    def is_dir(self: Path, *args: Any, **kwargs: Any) -> bool:
        if self == src / "data":  # what Python 3.11 does for a link into a folder the user may not enter
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    missing = cmd_apply.missing_package(cfg)
    assert missing is not None and "src/alpha/ does not exist (app.name = 'alpha'; src/ has other/)" in missing[0]


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
        with pytest.raises(PytError, match="pytemplate") as e:
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
        with pytest.raises(PytError, match=r'could not set \[project\] name = "alpha"') as e:
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
    """`./pyt lock` applies the managed block but not [preset.*]: after a raylib package switch
    it moved no-build-package to raylib_sdl and kept the raylib dependency. Every hint for a
    pyproject.toml that does not match pytemplate.toml names apply."""
    from runner import cmd_mode

    monkeypatch.setattr(render, "pyproject_outdated", lambda cfg: True)
    monkeypatch.setattr(hooks, "uv_lock_check", lambda cfg: (0, ""))
    result = hooks.check_lock(_cfg("raylib"))
    assert result.passed is False and "does not match pytemplate.toml: ./pyt apply" in result.hint and "lock" not in result.hint
    monkeypatch.setattr(render, "apply", lambda cfg, **kw: ([], []))
    assert cmd_mode.cmd_render(_cfg("raylib"), ["--check"]) == 1
    assert "does not match pytemplate.toml: ./pyt apply" in capsys.readouterr().err
    assert '"pyproject.toml matches pytemplate.toml", "./pyt apply"' in inspect.getsource(cmd_env._project_files)


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
        raise PytError(f"git cannot use the repository of {project.root}:\n  fatal: detected dubious ownership in repository", 2)

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
def test_git_missing_from_path_is_said_not_taken_for_no_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """git not on PATH (GitHub Desktop, Fork and SourceTree bring their own): apply said "not a git
    work tree: nothing to do", left pytemplate's hook installed with hooks.pre_commit = false,
    and doctor called hooks.pre_commit applied."""
    project, _ = _project(tmp_path, monkeypatch)
    _git(project.root, "init", "-q")
    assert _run(project) == 0
    hook = project.root / ".git" / "hooks" / "pre-commit"
    assert hook.is_file()
    project.edit("hooks", "pre_commit", False)
    capsys.readouterr()
    which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: None if name == "git" else which(name, *a, **kw))
    assert _run(project) == 0
    err = capsys.readouterr().err
    assert "warning: git pre-commit hook not checked: git not found in PATH (put it on PATH" in err, err
    assert "git hook         not checked: git not found in PATH (see above)" in err, err
    assert hook.is_file()  # nothing could remove it, and nothing says it was
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert _run(project) == 0
    assert "git hook         not checked: git not found in PATH" in capsys.readouterr().err
    monkeypatch.setattr(proc, "DRY_RUN", False)
    lines: list[tuple[bool | None, str]] = []

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        lines.append((passed, label))

    cmd_apply.doctor(project.cfg(), check)
    hooks.doctor(project.cfg(), check, project.root)
    assert (True, "pytemplate.toml applied (app.name, app.preset, [preset.*])") in lines, lines
    assert any(passed is None and label.startswith("git not found in PATH: the pre-commit hook") for passed, label in lines), lines
    if not any((d / ".git").exists() for d in project.root.parents):  # no repository at all: no news
        shutil.rmtree(project.root / ".git")
        assert _run(project) == 0
        assert "git hook         not a git work tree: nothing to do" in capsys.readouterr().err


def test_a_rename_refuses_a_tree_git_cannot_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A .git and no git on PATH (a git GUI's own git): the rename of a hand-edited app.name went
    ahead on a tree nobody could check for uncommitted changes; it refuses as for a git failure
    (a warning in the dry run), and --force goes ahead."""
    project, _ = _project(tmp_path, monkeypatch)
    (project.root / ".git").mkdir()
    project.edit("app", "name", "beta")
    no_git = tmp_path / "bin"
    no_git.mkdir()
    monkeypatch.setenv("PATH", str(no_git))
    before = project.snapshot()
    with pytest.raises(PytError, match=r"could not check for uncommitted changes in git \(git not found in PATH\)") as e:
        _run(project)
    assert "./pyt apply --force" in str(e.value) and project.snapshot() == before
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert _run(project) == 0
    assert "warning: could not check for uncommitted changes in git (git not found in PATH)" in capsys.readouterr().err
    assert project.snapshot() == before


@needs_git
def test_apply_leaves_pytemplates_hook_in_a_tracked_linked_folder_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """hooks.pre_commit = false with pytemplate's hook in a team's tracked folder linked as
    .git/hooks: doctor said [XX] "./pyt apply", and apply deleted the tracked hook and renamed the
    team's tracked pre-commit.local over it. apply (and its --dry-run) leaves the folder alone
    and says why, and doctor counts nothing: no change waits for apply."""
    project, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))  # a core.hooksPath of the user's would win
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _git(project.root, "init", "-q")
    shared = project.root / ".githooks"
    shared.mkdir()
    ours, team = hooks.hook_script("./pyt"), "#!/bin/sh\necho team check\n"
    (shared / "pre-commit").write_text(ours, encoding="utf-8", newline="\n")
    (shared / "pre-commit.local").write_text(team, encoding="utf-8", newline="\n")
    _git(project.root, "add", ".githooks")
    _git(project.root, "commit", "-q", "--no-verify", "-m", "share the hooks")
    shutil.rmtree(project.root / ".git" / "hooks")
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(shared), str(project.root / ".git" / "hooks"))
        else:
            (project.root / ".git" / "hooks").symlink_to(Path("..") / ".githooks", target_is_directory=True)
    except (OSError, ImportError, AttributeError) as e:
        pytest.skip(f"cannot create a folder link here: {e}")
    project.edit("hooks", "pre_commit", False)
    assert not [p for p, _ in cmd_apply.pending(project.cfg()) if "hook" in p]

    def row(dry: bool) -> str:
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        capsys.readouterr()
        assert _run(project) == 0
        out = capsys.readouterr().err
        return next(line for line in out.splitlines() if line.startswith("  git hook ")).split(None, 2)[2]

    for dry in (True, False):
        assert row(dry) == (
            ".git/hooks is a link to .githooks: pytemplate's hook there left alone "
            "(git tracks .githooks/pre-commit, .githooks/pre-commit.local: the hooks of everyone who uses that folder)"
        )
    assert (shared / "pre-commit").read_text(encoding="utf-8") == ours and (shared / "pre-commit.local").read_text(encoding="utf-8") == team


@needs_git
@pytest.mark.skipif(sys.platform == "win32", reason="Git's sh reads a file's first bytes as its x bit there (#!: the hook script's own line)")
def test_apply_gives_pytemplates_hook_its_x_bit_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """git skips a hook without its x bit and every commit goes unchecked: apply answered
    "already installed" and left the mode as it was. Its --dry-run says what it does."""
    project, _ = _project(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))  # a core.hooksPath of the user's would win
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _git(project.root, "init", "-q")
    hook = project.root / ".git" / "hooks" / "pre-commit"

    def row(dry: bool) -> str:
        monkeypatch.setattr(proc, "DRY_RUN", dry)
        capsys.readouterr()
        assert _run(project) == 0
        out = capsys.readouterr().err
        return next(line for line in out.splitlines() if line.startswith("  git hook ")).split(None, 2)[2]

    assert row(False) == "installed" and os.access(hook, os.X_OK)
    hook.chmod(0o644)
    assert row(True) == "would make the pre-commit hook executable again (git skips it: .git/hooks/pre-commit is not executable)"
    assert not os.access(hook, os.X_OK)
    assert row(False) == "made executable again (git skipped it)" and os.access(hook, os.X_OK)
    assert row(False) == "already installed"


@needs_git
def test_every_hook_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """What apply does, and what its --dry-run says, for each state of the hooks folder."""
    project, _ = _project(tmp_path, monkeypatch)
    _git(project.root, "init", "-q")
    hook = project.root / ".git" / "hooks" / "pre-commit"
    ours = hooks.hook_script("./pyt")

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
        (True, "#!/bin/sh\nsh ./pyt hooks run || exit $?\n", "a hook that runs ./pyt hooks run: left alone", "a hook that runs ./pyt hooks run: left alone"),
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
            if sys.platform != "win32":
                hook.chmod(0o755)  # as install writes it, and as git runs a hook (without: git skips it)
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
    (project.root / ".githooks" / "pre-commit").write_text("#!/bin/sh\nsh ./pyt hooks run || exit $?\n", encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(project.root / ".githooks" / "pre-commit", 0o644)  # git skips it: said so
        skipped = ", but git skips it: .githooks/pre-commit is not executable (chmod +x .githooks/pre-commit)"
        assert run(True) == run(False) == f"core.hooksPath is set: .githooks/pre-commit runs ./pyt hooks run{skipped}"
        os.chmod(project.root / ".githooks" / "pre-commit", 0o755)
    assert run(True) == run(False) == "core.hooksPath is set: .githooks/pre-commit runs ./pyt hooks run"


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
        (folder / "pyt").write_text("#!/bin/sh\n", encoding="utf-8")
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
    # q's hook runs pre-commit.local (p's copy) FIRST, then q's own checks (hooks.hook_script)
    chained = "runs this project's checks from pre-commit.local, which another project's hook runs first"
    assert row(True) == row(False) == chained
    assert target.read_bytes() == q_hook and hooks.own_local(rp)
    assert not [p for p, _ in cmd_apply.pending(project.cfg()) if "hook" in p]
    project.edit("hooks", "pre_commit", False)
    assert cmd_apply.pending(project.cfg())[-1][0].startswith("hooks.pre_commit = false, but pytemplate's")
    assert row(True) == "would remove pre-commit.local, this project's checks that another project's hook runs first (hooks.pre_commit = false)"
    assert local.is_file()
    assert row(False) == "removed (hooks.pre_commit = false)"
    assert not local.exists() and target.read_bytes() == q_hook  # q's own hook is never touched
    assert not [p for p, _ in cmd_apply.pending(project.cfg()) if "hook" in p]
    project.edit("hooks", "pre_commit", True)
    assert row(False) == "another project's hook of this repository: left alone (./pyt hooks install --force runs both)"
    local.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")  # a third hook there: --force would fail
    assert row(False) == "another project's hook of this repository: left alone (pre-commit.local is taken too: ./pyt hooks status says what to do)"
    local.unlink()
    # chained again, then q goes away: its stale hook is replaced by p's, and p's copy goes
    hooks.uninstall(rq)
    hooks.install(rp)
    hooks.install(rq, force=True)
    # (another tool's hook put on top by hand: it runs no pre-commit.local, nothing is removed)
    target.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    project.edit("hooks", "pre_commit", False)
    assert row(True).startswith("another tool's hook: left alone") and row(False).startswith("another tool's hook: left alone")
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n" and hooks.own_local(rp)
    project.edit("hooks", "pre_commit", True)
    target.write_bytes(q_hook)
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
    assert "[preset.raylib] is not applied to pyproject.toml (add raylib==6.0.2.0): ./pyt apply" in result.hint


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


def test_reference_problems_see_through_a_leftover_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder left holding only __pycache__ (a module deleted, a package turned into a module)
    is no module: mypyc finds nothing there (config.compiled_paths, mypyc.compiled_sources)."""
    project, _ = _project(tmp_path, monkeypatch)
    project.edit("compile", "modules", ["alpha.core", "alpha.gone", "alpha.bench"])
    for leftover in ("gone", "bench"):
        (project.root / "src" / "alpha" / leftover / "__pycache__").mkdir(parents=True)
        (project.root / "src" / "alpha" / leftover / "__pycache__" / "m.cpython-314.pyc").write_bytes(b"")
    (project.root / "src" / "alpha" / "bench.py").write_text("X = 1\n", encoding="utf-8")
    problems = [p for p in cmd_apply.reference_problems(project.cfg()) if p.startswith("compile.modules")]
    assert problems == ["compile.modules: alpha.gone not found in src/ (mypyc builds and `test mypyc` will fail)"]
    # a namespace folder that holds modules is one
    (project.root / "src" / "alpha" / "gone" / "m.py").write_text("X = 1\n", encoding="utf-8")
    assert not [p for p in cmd_apply.reference_problems(project.cfg()) if p.startswith("compile.modules")]


@pytest.mark.parametrize("how", ["simulated", "for real"])
def test_references_in_a_folder_this_user_may_not_enter_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    """deploy.upx.path or deploy.exe.icon in another user's folder (a shared pytemplate.toml that
    names ~alice/tools/upx), app.assets or a compiled module behind a folder this user may not
    enter: Python 3.11-3.13's Path.is_file and is_dir raised PermissionError there, and doctor (its
    whole report), apply and setup (at their very end, after the sync and the render) ended in an
    internal-error traceback. Each is reported as missing. "simulated": os.stat refuses them, as
    such a folder does (the tests also run as root); "for real": folders of mode 0 (POSIX, not root)."""
    project, _ = _project(tmp_path, monkeypatch, "flet")
    private = tmp_path / "private"
    private.mkdir()
    (private / "upx").write_bytes(b"x")
    (private / "app.ico").write_bytes(b"x")
    project.edit("deploy.upx", "path", str(private / "upx"))
    project.edit("deploy.exe", "icon", str(private / "app.ico"))
    assert cmd_apply.reference_problems(project.cfg()) == []
    core = project.root / "src" / "alpha" / "core"
    expected = ["compile.modules", "deploy.exe.icon", "deploy.upx.path"]
    if how == "simulated":
        real, blocked = os.stat, [os.path.abspath(p) for p in (private, core, project.root / "src" / "assets")]

        def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            here = "" if isinstance(path, int) else os.path.abspath(os.fsdecode(path))
            if any(here == b or here.startswith(b + os.sep) for b in blocked):
                raise PermissionError(errno.EACCES, "Permission denied", os.fsdecode(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", stat)
        # os.path's checks as stat answers them: Windows' own (nt._path_isfile...) never call
        # os.stat, and the simulated folders read as plain ones there
        import genericpath

        for check in ("exists", "isfile", "isdir"):
            monkeypatch.setattr(os.path, check, getattr(genericpath, check))
        expected.insert(1, "app.assets")
        problems = cmd_apply.reference_problems(project.cfg())
    else:
        if sys.platform == "win32" or os.geteuid() == 0:
            pytest.skip("modes that keep this user out: POSIX, not root (root enters every folder)")
        for folder in (private, core):
            folder.chmod(0)
        try:
            problems = cmd_apply.reference_problems(project.cfg())
        finally:
            for folder in (private, core):
                folder.chmod(0o755)
    assert [re.split(r"[ :]", p)[0] for p in problems] == expected, problems


def test_render_auto_points_at_apply(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(render, "apply", lambda cfg, **kw: ([], []))
    monkeypatch.setattr(render, "pyproject_outdated", lambda cfg: True)
    render.auto(_cfg("script"))
    err = capsys.readouterr().err
    assert "./pyt apply" in err and "./pyt lock" not in err


def _table(text: str, header: str) -> str:
    return text.split(f"\n{header}\n", 1)[1].split("\n[", 1)[0]


@pytest.mark.parametrize("preset", ["script", "raylib", "flet"])
def test_config_comments_say_how_changes_are_applied(preset: str) -> None:
    text = (ROOT / ".pytemplate" / "presets" / preset / "files" / "pytemplate.toml").read_text(encoding="utf-8")
    head = text.split("\nschema = 1\n", 1)[0]
    assert "run ./pyt apply" in head and "./pyt new DIR --preset P" in head
    assert "run any ./pyt command" not in head and "./pyt lock" not in text
    assert "./pyt apply" in _table(text, "[hooks]") and "false" in _table(text, "[hooks]")
    if preset != "script":
        assert "./pyt apply" in _table(text, f"[preset.{preset}]")
    if (TEMPLATE_DIR / "template-repo").is_file():  # the template's root is the script preset as myapp
        root = (ROOT / "pytemplate.toml").read_bytes().replace(b"\r\n", b"\n")  # a CRLF checkout (Windows)
        assert root == presets.skeleton("script", "myapp")["pytemplate.toml"]


# --- the real ./pyt in a throwaway copy ----------------------------------------------------------------


def _pyt(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    drop = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    return subprocess.run(
        [sys.executable, "-B", str(root / ".pytemplate" / "pyt.py"), *args],
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
    """[app] of the copy: a project made with ./pyt new has its own name and preset."""
    app: dict[str, Any] = tomllib.loads((root / "pytemplate.toml").read_text(encoding="utf-8-sig"))["app"]
    return app


def _new_name(root: Path) -> str:
    """The name the copy is renamed to: beta, unless its own package is beta already (a project
    named beta or Beta: nothing was renamed, or src/beta/ stayed, and two tests failed there)."""
    return "gamma" if rename.package_of(_copy_app(root)["name"]) == "beta" else "beta"


def test_real_dry_run_in_a_copy(copy: Path) -> None:
    old, new = _copy_app(copy)["name"], _new_name(copy)
    _edit_copy(copy, "app", "name", new)
    _edit_copy(copy, "hooks", "pre_commit", False)
    before = _tree(copy)
    r = _pyt(copy, "--dry-run", "apply")
    assert r.returncode == 0, r.stderr
    assert f"would rename '{old}' -> '{new}'" in r.stderr and re.search(rf"\+ name = [\"']{new}[\"']", r.stderr), r.stderr
    init = copy / "src" / rename.package_of(old) / "__init__.py"
    if init.is_file() and init.read_text(encoding="utf-8") == f'"""{old}"""\n':  # the skeleton's docstring (a project may have its own)
        assert f'+ """{new}"""' in r.stderr
    assert "git hook         not a git work tree: nothing to do" in r.stderr
    assert _tree(copy) == before, "--dry-run wrote files"
    r = _pyt(copy, "apply", "--bogus")
    assert r.returncode == 2 and "apply: unrecognized arguments: --bogus" in r.stderr
    assert _tree(copy) == before


def test_real_hand_edited_preset_is_refused(copy: Path) -> None:
    old = _copy_app(copy)["preset"]
    new = "script" if old == "raylib" else "raylib"
    _edit_copy(copy, "app", "preset", new)
    before = _tree(copy)
    for command in ("apply", "setup"):
        r = _pyt(copy, command)
        assert r.returncode == 2, r.stderr
        assert f"changed from '{old}' to '{new}' by hand" in r.stderr and f"./pyt new DIR --preset {new}" in r.stderr
    assert _tree(copy) == before


@needs_uv
@needs_git
def test_real_apply_after_a_hand_edited_name(copy: Path) -> None:
    old, new = rename.package_of(_copy_app(copy)["name"]), _new_name(copy)
    _edit_copy(copy, "hooks", "pre_commit", True)  # the project's may be false: apply then installs no hook
    _git(copy, "init", "-q")
    _git(copy, "add", "-A")
    _git(copy, "commit", "-q", "-m", "init", "--no-verify")
    _edit_copy(copy, "app", "name", new)  # pytemplate.toml is dirty by definition: not refused
    (copy / "src" / "notes.txt").write_text("mine\n", encoding="utf-8")
    r = _pyt(copy, "apply")
    assert r.returncode == 2 and "uncommitted changes in git (1 path(s): src/notes.txt)" in r.stderr, r.stderr
    assert "./pyt apply --force" in r.stderr and (copy / "src" / old).is_dir()
    (copy / "src" / "notes.txt").unlink()
    r = _pyt(copy, "apply")
    if r.returncode != 0 and rename.needs_pypi(r.stderr):
        pytest.skip("needs PyPI: uv lock could not reach the package index")
    assert r.returncode == 0, r.stderr
    assert not (copy / "src" / old).exists() and (copy / "src" / new / "__init__.py").is_file()
    assert tomllib.loads((copy / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"] == new
    assert new in {p["name"] for p in tomllib.loads((copy / "uv.lock").read_text(encoding="utf-8"))["package"]}
    assert (copy / ".git" / "hooks" / "pre-commit").is_file()
    assert _pyt(copy, "render", "--check").returncode == 0
    before = _tree(copy)
    r = _pyt(copy, "apply")
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


def test_uv_frozen_replaces_a_marked_requirement_only_with_its_marker(tmp_path: Path) -> None:
    """The uv behaviour cmd_apply.dependency_changes relies on, offline: `add --frozen` replaces a
    requirement that has a marker only with one of the same marker (and keeps its extras), and
    appends a second requirement otherwise; `remove --frozen` drops every requirement of the name."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "p"\nversion = "0"\nrequires-python = ">=3.11"\ndependencies = [\n'
        "    \"Flet_Desktop[x] == 1.0.1; sys_platform != 'emscripten'\",\n"
        "    \"raylib==6.0.1.0; sys_platform == 'linux'\",\n    \"raylib==6.0.0.0; sys_platform == 'win32'\",\n]\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_PROJECT", "VIRTUAL_ENV"))} | {"UV_OFFLINE": "1"}

    def run(*args: str) -> list[str]:
        r = subprocess.run([uv, *args], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
        assert r.returncode == 0, r.stderr
        deps: list[str] = tomllib.loads((tmp_path / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
        return deps

    deps = run("add", "--frozen", "flet-desktop==1.0.0; sys_platform != 'emscripten'")  # what apply adds
    assert [_parts(r) for r in deps if _parts(r)[0] == "flet-desktop"] == [("flet-desktop", "[x]", "==1.0.0", "sys_platform != 'emscripten'")]
    deps = run("add", "--frozen", "flet-desktop==1.0.2")  # no marker: a second requirement
    assert sorted(_parts(r)[2] for r in deps if _parts(r)[0] == "flet-desktop") == ["==1.0.0", "==1.0.2"]
    assert not any(_parts(r)[0] == "raylib" for r in run("remove", "--frozen", "raylib"))
