"""Paranoid tests of render.py's core: render.apply / auto and state.json, the exit codes of
./deploy render, the managed parts of pyproject.toml, the typing profiles and the generated CI.

Everything runs in process against a sandbox (render.ROOT, STATE_FILE, PYPROJECT and outputs
monkeypatched to a tmp dir); the real generators are rendered for every preset and backend set.
"""

from __future__ import annotations

import configparser
import copy
import datetime
import hashlib
import itertools
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

from runner import cli, cmd_mode, config, envs, presets, proc, render  # noqa: E402
from runner.cmd_build import BuildRequest  # noqa: E402
from runner.config import BACKENDS, Config  # noqa: E402
from runner.editors import nvim  # noqa: E402
from runner.methods import pyz  # noqa: E402
from runner.project import PRESETS, ROOT, TEMPLATES  # noqa: E402
from runner.ui import DeployError  # noqa: E402

COMMANDS = set(cli.COMMANDS)
PRESET_NAMES = ("script", "raylib", "flet")


# --- configs -----------------------------------------------------------------------------------


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def preset_cfg(name: str = "script", extra: dict[str, Any] | None = None) -> Config:
    """The real pytemplate.toml of a preset (app myapp), with `extra` merged in."""
    text = (PRESETS / name / "files" / "pytemplate.toml").read_text("utf-8")
    data = tomllib.loads(text.replace("{{name}}", "myapp").replace("{{pkg}}", "myapp"))
    cfg: Config = config._build(Config, _merge(data, extra or {}), "")
    config.validate(cfg, COMMANDS)
    return cfg


def backend_sets() -> list[tuple[list[str], str]]:
    """Every non-empty set of backends (in the canonical order) with each of its members active."""
    out: list[tuple[list[str], str]] = []
    for n in range(1, len(BACKENDS) + 1):
        for combo in itertools.combinations(BACKENDS, n):
            out += [(list(combo), active) for active in combo]
    return out


COMBOS = [(p, s, a) for p in PRESET_NAMES for s, a in backend_sets()]


def combo_cfg(preset: str, supported: list[str], active: str, extra: dict[str, Any] | None = None) -> Config:
    return preset_cfg(preset, _merge({"backend": {"supported": supported, "active": active}}, extra or {}))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- the apply sandbox ---------------------------------------------------------------------------

GENERATED = {"gen/a.json": '// x\n{"a": 1}\n', "b.ini": "[b]\nx = 1\n", ".c": "c\n"}
OLD = 1_000_000_000  # a fixed past mtime (ns): any later write shows up


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = root / ".pytemplate" / "state.json"
        self.pyproject = root / "pyproject.toml"
        self.files = dict(GENERATED)

    def read(self, path: str) -> bytes:
        return (self.root / path).read_bytes()

    def write(self, path: str, data: str | bytes) -> None:
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_bytes(data.encode("utf-8") if isinstance(data, str) else data)

    def recorded(self) -> Any:
        return json.loads(self.state.read_text(encoding="utf-8"))["files"]

    def age(self) -> dict[str, bytes]:
        """Set every file's mtime to OLD and return the contents (see `untouched`)."""
        out: dict[str, bytes] = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                os.utime(p, ns=(OLD, OLD))
                out[p.relative_to(self.root).as_posix()] = p.read_bytes()
        return out

    def untouched(self, before: dict[str, bytes]) -> bool:
        """No file was created, deleted or written since `age` (same bytes rewritten included)."""
        now = {p.relative_to(self.root).as_posix(): p for p in self.root.rglob("*") if p.is_file()}
        return set(now) == set(before) and all(now[k].read_bytes() == v and now[k].stat().st_mtime_ns == OLD for k, v in before.items())


def pyproject_text(cfg: Config) -> str:
    """The project's pyproject.toml with its managed parts in the state `cfg` expects."""
    return render.pyproject_expected(cfg, (ROOT / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    sb = Sandbox(tmp_path)
    monkeypatch.setattr(proc, "DRY_RUN", False)
    monkeypatch.setattr(render, "ROOT", tmp_path)
    monkeypatch.setattr(render, "STATE_FILE", sb.state)
    monkeypatch.setattr(render, "PYPROJECT", sb.pyproject)
    monkeypatch.setattr(render, "outputs", lambda cfg: dict(sb.files))
    sb.state.parent.mkdir()  # as in a project: .pytemplate/ exists, state.json may not
    sb.pyproject.write_text(pyproject_text(preset_cfg()), encoding="utf-8", newline="\n")
    return sb


CFG = preset_cfg()


# --- render.apply ----------------------------------------------------------------------------------


def test_first_apply_writes_lf_files_and_records_their_hashes(box: Sandbox) -> None:
    assert render.apply(CFG) == (list(GENERATED), [])
    for path, content in GENERATED.items():
        assert box.read(path) == content.encode("utf-8")
    assert box.recorded() == {path: sha(content) for path, content in GENERATED.items()}
    raw = box.state.read_bytes()
    assert raw.endswith(b"}\n") and b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    data = json.loads(raw)
    assert list(data) == ["comment", "files"] and list(data["files"]) == sorted(GENERATED)


def test_second_apply_writes_nothing(box: Sandbox) -> None:
    render.apply(CFG)
    before = box.age()
    assert render.apply(CFG) == ([], [])
    assert render.apply(CFG, force=True) == ([], [])
    assert box.untouched(before)  # not even state.json is rewritten


@pytest.mark.parametrize("variant", ["crlf", "bom", "bom+crlf"])
def test_crlf_and_bom_checkouts_are_neither_edits_nor_rewritten(box: Sandbox, variant: str) -> None:
    # `* text=auto eol=native` + core.autocrlf gives CRLF on Windows; editors and PS 5.1 add BOMs
    render.apply(CFG)
    for path, content in GENERATED.items():
        text = content.replace("\n", "\r\n") if "crlf" in variant else content
        box.write(path, ("\ufeff" if "bom" in variant else "") + text)
    before = box.age()
    assert render.apply(CFG, check=True) == ([], [])
    assert render.apply(CFG) == ([], [])
    assert box.untouched(before)


def test_hand_edits_are_kept_reported_and_forced(box: Sandbox) -> None:
    render.apply(CFG)
    box.write("b.ini", "[b]\nx = 2  # mine\n")
    before = box.age()
    assert render.apply(CFG) == ([], ["b.ini"])
    assert render.apply(CFG, check=True) == ([], ["b.ini"])
    assert box.untouched(before)
    box.files["b.ini"] = "[b]\nx = 3\n"  # the generator changes too: still protected
    assert render.apply(CFG) == ([], ["b.ini"])
    assert box.read("b.ini") == b"[b]\nx = 2  # mine\n"
    assert render.apply(CFG, force=True) == (["b.ini"], [])
    assert box.read("b.ini") == b"[b]\nx = 3\n"
    assert box.recorded()["b.ini"] == sha("[b]\nx = 3\n")
    assert render.apply(CFG) == ([], [])


def test_a_hand_edit_that_matches_the_new_output_is_just_recorded(box: Sandbox) -> None:
    render.apply(CFG)
    box.files["b.ini"] = "[b]\nx = 3\n"
    box.write("b.ini", "[b]\r\nx = 3\r\n")  # someone already wrote what the generator now says
    assert render.apply(CFG) == ([], [])
    assert box.recorded()["b.ini"] == sha("[b]\nx = 3\n")


def test_generator_changes_update_untouched_files(box: Sandbox) -> None:
    render.apply(CFG)
    box.files["gen/a.json"] = '// x\n{"a": 2}\n'
    assert render.apply(CFG, check=True) == (["gen/a.json"], [])
    assert box.read("gen/a.json") == GENERATED["gen/a.json"].encode()  # --check wrote nothing
    assert render.apply(CFG) == (["gen/a.json"], [])
    assert box.read("gen/a.json") == b'// x\n{"a": 2}\n'
    assert box.recorded()["gen/a.json"] == sha('// x\n{"a": 2}\n')


def test_a_deleted_file_is_recreated(box: Sandbox) -> None:
    render.apply(CFG)
    (box.root / "gen" / "a.json").unlink()
    (box.root / "gen").rmdir()
    assert render.apply(CFG) == (["gen/a.json"], [])
    assert box.read("gen/a.json") == GENERATED["gen/a.json"].encode()


def test_an_unrecorded_file_that_differs_is_overwritten(box: Sandbox) -> None:
    # no state.json (first render, or it was deleted): nothing counts as hand-edited
    box.write("b.ini", "anything\n")
    assert render.apply(CFG) == (list(GENERATED), [])
    assert box.read("b.ini") == GENERATED["b.ini"].encode()


@pytest.mark.parametrize("how", ["check", "dry-run", "check+force", "dry-run+force"])
def test_check_and_dry_run_write_nothing(box: Sandbox, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    render.apply(CFG)
    (box.root / ".c").unlink()  # missing
    box.files["gen/a.json"] = "changed\n"  # outdated
    box.write("b.ini", "mine\n")  # hand-edited
    before = box.age()
    monkeypatch.setattr(proc, "DRY_RUN", how.startswith("dry-run"))
    force = how.endswith("+force")
    result = render.apply(CFG, check=how.startswith("check"), force=force)
    assert result == ((["gen/a.json", "b.ini", ".c"], []) if force else (["gen/a.json", ".c"], ["b.ini"]))
    assert box.untouched(before)


def test_diff_shows_the_generated_against_the_current_content(box: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    render.apply(CFG)
    box.write("b.ini", "[b]\r\nx = 2\r\n")
    capsys.readouterr()
    assert render.apply(CFG, check=True, show_diff=True) == ([], ["b.ini"])
    err = capsys.readouterr().err
    assert "--- b.ini (generated)" in err and "+++ b.ini (current)" in err
    assert "-x = 1" in err and "+x = 2" in err and "\r" not in err
    render.apply(CFG, check=True)
    assert "---" not in capsys.readouterr().err  # only with show_diff


def test_a_folder_in_the_way_is_a_clear_error(box: Sandbox) -> None:
    (box.root / "b.ini").mkdir()
    with pytest.raises(DeployError, match=r"b\.ini is generated, but a folder"):
        render.apply(CFG)


def test_write_failures_are_clear_errors(box: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: Path, *args: Any, **kwargs: Any) -> int:
        raise PermissionError(13, "Permission denied")

    with monkeypatch.context() as m:
        m.setattr(Path, "write_text", refuse)
        with pytest.raises(DeployError, match="cannot write the generated file gen/a.json: Permission denied"):
            render.apply(CFG)


# --- state.json ------------------------------------------------------------------------------------


def test_state_keeps_unknown_top_level_keys(box: Sandbox) -> None:
    extra = {"applied": {"backend": {"supported": ["cpython"]}}, "zzz": [1, "two", None]}
    box.write(".pytemplate/state.json", json.dumps({"files": {}, **extra}))
    render.apply(CFG)
    data = json.loads(box.state.read_text(encoding="utf-8"))
    assert list(data) == ["comment", "files", "applied", "zzz"]
    assert {k: data[k] for k in extra} == extra
    box.files["b.ini"] = "new\n"
    render.apply(CFG)  # every rewrite keeps them
    assert {k: json.loads(box.state.read_text(encoding="utf-8"))[k] for k in extra} == extra


def test_state_key_order_is_kept(box: Sandbox) -> None:
    box.write(".pytemplate/state.json", json.dumps({"applied": {"x": 1}, "files": {}, "comment": "old"}))
    render.apply(CFG)
    data = json.loads(box.state.read_text(encoding="utf-8"))
    assert list(data) == ["applied", "files", "comment"] and data["comment"] == render.STATE_COMMENT


CORRUPT_STATES = {
    "empty": b"",
    "garbage": b"garbage{",
    "array": b"[]",
    "null": b"null",
    "number": b"42",
    "string": b'"files"',
    "merge-conflict": b'<<<<<<< HEAD\n{"files": {}}\n=======\n{"files": {}}\n>>>>>>> other\n',
    "files-null": b'{"files": null}',
    "files-string": b'{"files": "abc"}',
    "files-array": b'{"files": ["a"]}',
    "files-number": b'{"files": 5}',
    "bad-hashes": b'{"files": {"b.ini": 1, "gen/a.json": ["x"], ".c": "not a sha256"}}',
    "utf-16": '{"files": {}}'.encode("utf-16"),
    "latin-1": b'{"files": {"\xe9": "x"}}',
    "binary": b"\x80\x81\xff\x00",
}


@pytest.mark.parametrize("raw", CORRUPT_STATES.values(), ids=CORRUPT_STATES.keys())
def test_corrupt_state_counts_as_empty(box: Sandbox, raw: bytes) -> None:
    for path in GENERATED:
        box.write(path, "hand edit?\n")
    box.write(".pytemplate/state.json", raw)
    assert render.apply(CFG, check=True) == (list(GENERATED), [])  # never raises; nothing is "hand-edited"
    assert render.apply(CFG) == (list(GENERATED), [])
    for path, content in GENERATED.items():
        assert box.read(path) == content.encode()
    assert box.recorded() == {path: sha(content) for path, content in GENERATED.items()}  # valid UTF-8 JSON again


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"], ids=["no-bom", "bom"])
@pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_state_with_a_bom_or_crlf_still_protects_hand_edits(box: Sandbox, bom: bytes, eol: bytes) -> None:
    render.apply(CFG)
    box.state.write_bytes(bom + box.state.read_bytes().replace(b"\n", eol))
    box.write("b.ini", "mine\n")
    assert render.apply(CFG) == ([], ["b.ini"])
    assert box.read("b.ini") == b"mine\n"


def test_stale_entries_stay_recorded(box: Sandbox) -> None:
    # A file no longer generated keeps its hash: if it comes back, a hand edit is still protected
    render.apply(CFG)
    del box.files[".c"]
    box.files["b.ini"] = "new\n"
    render.apply(CFG)
    assert box.recorded()[".c"] == sha(GENERATED[".c"])


# --- ./deploy render and render.auto ---------------------------------------------------------------


def _render(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cmd_mode.cmd_render(CFG, args)
    return code, capsys.readouterr().err


def test_render_command_exit_codes(box: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    code, err = _render(["--check"], capsys)
    assert code == 1 and "outdated: gen/a.json" in err and not (box.root / "gen").exists()
    code, err = _render([], capsys)
    assert code == 0 and "updated: gen/a.json" in err
    code, err = _render(["--check"], capsys)
    assert code == 0 and "generated files up to date" in err
    box.write("b.ini", "[b]\nx = 2\n")
    code, err = _render(["--check"], capsys)
    assert code == 1 and "hand-edited (left untouched without --force): b.ini" in err
    code, err = _render([], capsys)
    assert code == 0 and "hand-edited" in err and box.read("b.ini") == b"[b]\nx = 2\n"  # plain render only warns
    code, err = _render(["--check", "--diff"], capsys)
    assert code == 1 and "--- b.ini (generated)" in err
    code, err = _render(["--force"], capsys)
    assert code == 0 and "updated: b.ini" in err and box.read("b.ini") == GENERATED["b.ini"].encode()
    code, err = _render(["--check"], capsys)
    assert code == 0


def test_render_check_fails_on_an_outdated_pyproject(box: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    render.apply(CFG)
    box.pyproject.write_text(pyproject_text(preset_cfg(extra={"python": {"cpython": "3.13"}})), encoding="utf-8")
    code, err = _render(["--check"], capsys)
    assert code == 1 and "pyproject.toml" in err
    code, err = _render([], capsys)
    assert code == 0 and "pyproject.toml" in err  # render never touches pyproject.toml: lock does


def test_render_under_dry_run_writes_nothing(box: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    before = box.age()
    monkeypatch.setattr(proc, "DRY_RUN", True)
    code, err = _render(["--force"], capsys)
    assert code == 0 and "would update: gen/a.json" in err
    assert box.untouched(before)


def test_auto_reports_once_and_warns(box: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    render.auto(CFG)
    assert capsys.readouterr().err.splitlines() == ["render: updated gen/a.json, b.ini, .c"]
    render.auto(CFG)
    assert capsys.readouterr().err == ""  # up to date: silent
    box.write("b.ini", "mine\n")
    render.auto(CFG)
    err = capsys.readouterr().err
    assert "not overwriting hand-edited generated files: b.ini" in err and box.read("b.ini") == b"mine\n"
    render.auto(CFG, force=True)
    assert "render: updated b.ini" in capsys.readouterr().err
    box.files[".c"] = "new\n"
    monkeypatch.setattr(proc, "DRY_RUN", True)
    render.auto(CFG)
    assert "render: would update .c" in capsys.readouterr().err and box.read(".c") == b"c\n"
    monkeypatch.setattr(proc, "DRY_RUN", False)
    box.pyproject.write_text(pyproject_text(preset_cfg(extra={"python": {"cpython": "3.13"}})), encoding="utf-8")
    render.auto(CFG)
    assert "pyproject.toml" in capsys.readouterr().err  # the hint's wording belongs to `apply`


# --- pyproject.toml: the managed parts -------------------------------------------------------------

FLET_TABLES = '[tool.flet]\norg = "com.example"\nproduct = "x"\n\n[tool.flet.app]\npath = "src"\nmodule = "main"\n'
BASE = '[project]\nname = "x"\nrequires-python = ">=3.11"\ndependencies = [\n    "rich>=15",\n]\n\n[tool.uv]\n'


def preset_pyproject(preset: str) -> str:
    """The project's pyproject.toml as `init <preset>` leaves it (preset tables included)."""
    extra = str(presets.load(preset).get("pyproject", "")).replace("{{name}}", "myapp").replace("{{pkg}}", "myapp")
    return presets._set_extra_tables((ROOT / "pyproject.toml").read_text(encoding="utf-8"), extra)


@pytest.fixture
def pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "pyproject.toml"
    monkeypatch.setattr(render, "PYPROJECT", target)
    monkeypatch.setattr(proc, "DRY_RUN", False)
    return target


def _write(target: Path, text: str) -> None:
    target.write_bytes(text.encode("utf-8"))


@pytest.mark.parametrize(("preset", "supported", "active"), COMBOS)
def test_managed_parts_for_every_preset_and_backend_set(pyproject: Path, preset: str, supported: list[str], active: str) -> None:
    cfg = combo_cfg(preset, supported, active)
    text = preset_pyproject(preset)
    _write(pyproject, text)
    render.write_pyproject(cfg)
    new = pyproject.read_text(encoding="utf-8")
    data = tomllib.loads(new)
    assert data["project"]["requires-python"] == f">={cfg.min_python}"
    uv = data["tool"]["uv"]
    assert uv == tomllib.loads(render.managed_block(cfg))  # [tool.uv] holds the managed keys only
    assert ("pypy" in str(uv["environments"])) == ("pypy" in supported)
    assert uv["required-version"] == f">={envs.MIN_UV}"  # an older uv stops with uv's own clear message
    before = tomllib.loads(text)
    assert {k: v for k, v in data["tool"].items() if k != "uv"} == {k: v for k, v in before["tool"].items() if k != "uv"}
    assert {k: v for k, v in data["project"].items() if k != "requires-python"} == {
        k: v for k, v in before["project"].items() if k != "requires-python"
    }
    lines = new.splitlines()
    last = max(i for i, ln in enumerate(lines) if render.MARK_END in ln and "preset" not in ln)
    assert lines[last].startswith("python-preference = ") and lines[last].endswith("  " + render.MARK_END)  # inline, never detached
    assert render.pyproject_outdated(cfg) is False
    assert render.write_pyproject(cfg) is False and pyproject.read_text(encoding="utf-8") == new  # idempotent
    render.check_pyproject(cfg)


def test_preset_markers_are_never_taken_for_the_managed_ones(pyproject: Path) -> None:
    cfg = preset_cfg("flet")
    flet = tomllib.loads(FLET_TABLES)["tool"]["flet"]
    # The managed block deleted by hand: `lock` must restore it, not replace [tool.flet] with it
    for text in (presets._set_extra_tables(BASE, FLET_TABLES), presets._set_extra_tables(BASE, FLET_TABLES).replace("[tool.uv]\n", "") + "\n[tool.uv]\n"):
        _write(pyproject, text)
        assert render.pyproject_outdated(cfg) is True
        assert render.write_pyproject(cfg) is True
        new = pyproject.read_text(encoding="utf-8")
        data = tomllib.loads(new)
        assert data["tool"]["flet"] == flet and "environments" in data["tool"]["uv"]
        assert presets.EXTRA_BEGIN in new and presets.EXTRA_END in new
        assert render.pyproject_outdated(cfg) is False
    # Only the opening comment line survived: it still counts as the block
    whole = pyproject.read_text(encoding="utf-8")
    lines = whole.splitlines()
    begin = next(i for i, ln in enumerate(lines) if ln.startswith(render.MARK_BEGIN + ":"))
    end = next(i for i, ln in enumerate(lines) if ln.endswith("  " + render.MARK_END))
    _write(pyproject, "\n".join(lines[: begin + 1] + lines[end + 1 :]) + "\n")
    with pytest.raises(DeployError, match="closing marker"):
        render.write_pyproject(cfg)
    # The closing marker lost: an error, never a silent loss of [tool.flet]
    _write(pyproject, whole.replace("  " + render.MARK_END + "\n", "\n"))
    with pytest.raises(DeployError):
        render.write_pyproject(cfg)
    assert tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["flet"] == flet


MANAGED = (
    '[project]\nname = "x"\nrequires-python = ">=3.14"\n\n[tool.uv]\n'
    "# >>> pytemplate: generated\n"
    'environments = ["x"]\n'
    'python-preference = "only-managed"  # <<< pytemplate\n'
)
BROKEN = {
    "end-marker-removed": (MANAGED.replace("  # <<< pytemplate", ""), "closing marker"),
    "begin-marker-removed": (MANAGED.replace("# >>> pytemplate: generated\n", ""), "opening marker"),
    "end-before-begin": (
        MANAGED.replace("  # <<< pytemplate", "").replace("[tool.uv]\n", "[tool.uv]\nfoo = 1  # <<< pytemplate\n"),
        "comes before",
    ),
    "markers-removed-keys-kept": (
        MANAGED.replace("# >>> pytemplate: generated\n", "").replace("  # <<< pytemplate", ""),
        "repeat a managed key",
    ),
    "block-twice": (MANAGED + "\n[tool.other]\n# >>> pytemplate\nx = 1  # <<< pytemplate\n", "more than once"),
    "block-outside-tool-uv": (MANAGED.replace("[tool.uv]", "[tool.other]"), "not in the [tool.uv] table"),
    "table-inside-block": (
        MANAGED.replace('environments = ["x"]\n', 'environments = ["x"]\n[tool.flet]\norg = "x"\n'),
        "table header",
    ),
    "invalid-toml": ('[project]\nname = "x"\n\n[tool.uv]\nfoo = [\n', "not valid TOML"),
    "no-project-table": ('[tool.uv]\nfoo = 1\n', "[project]"),
}


@pytest.mark.parametrize(("text", "message"), BROKEN.values(), ids=BROKEN.keys())
def test_unusable_pyproject_is_a_clear_error_and_untouched(pyproject: Path, text: str, message: str) -> None:
    _write(pyproject, text)
    with pytest.raises(DeployError) as e:
        render.write_pyproject(CFG)
    assert e.value.code == 2 and "pyproject.toml" in str(e.value) and message in str(e.value), str(e.value)
    assert pyproject.read_bytes() == text.encode("utf-8")  # nothing written
    assert render.pyproject_outdated(CFG) is True  # auto, doctor and the hook warn, never crash
    with pytest.raises(DeployError):
        render.check_pyproject(CFG)  # the preflight says the same without writing


@pytest.mark.parametrize("header", ["[tool.uv]  # uv settings", "[ tool.uv ]", "[tool.uv]\t", "[tool . uv]", "  [tool.uv]"])
def test_tool_uv_header_spellings(pyproject: Path, header: str) -> None:
    _write(pyproject, f'[project]\nname = "x"\nrequires-python = ">=3.11"\n\n{header}\nfoo = 1\n')
    assert render.write_pyproject(CFG) is True
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["tool"]["uv"]["foo"] == 1 and data["tool"]["uv"]["python-preference"] == "only-managed"
    assert render.pyproject_outdated(CFG) is False and render.write_pyproject(CFG) is False


def test_missing_tool_uv_table_is_added(pyproject: Path) -> None:
    _write(pyproject, '[project]\nname = "x"\nrequires-python = ">=3.14"\n\n\n')
    assert render.write_pyproject(CFG) is True
    new = pyproject.read_text(encoding="utf-8")
    assert "\n\n\n[tool.uv]" not in new and tomllib.loads(new)["tool"]["uv"]["python-preference"] == "only-managed"
    assert render.write_pyproject(CFG) is False


def test_keys_no_longer_managed_are_removed(pyproject: Path) -> None:
    pypy = preset_cfg(extra={"backend": {"supported": ["cpython", "pypy", "mypyc"]}})
    _write(pyproject, pyproject_text(pypy))
    assert "override-dependencies" in tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["uv"]
    assert render.pyproject_outdated(CFG) is True
    assert render.write_pyproject(CFG) is True  # PyPy dropped: its cffi override and 3.11 floor go
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert "override-dependencies" not in data["tool"]["uv"] and data["project"]["requires-python"] == ">=3.14"


def taplo_like(text: str) -> str:
    """What taplo (Even Better TOML, LazyVim's toml extra) does with its defaults: 2-space array
    items, one space before a comment."""
    return text.replace("\n    ", "\n  ").replace("  # <<< pytemplate", " # <<< pytemplate")


@pytest.mark.parametrize("supported", [["cpython"], ["cpython", "pypy", "mypyc"]])
def test_formatting_is_not_a_change(pyproject: Path, supported: list[str]) -> None:
    cfg = preset_cfg("raylib", {"backend": {"supported": supported, "active": "cpython"}})
    expected = pyproject_text(cfg)
    formatted = taplo_like(expected)
    assert formatted != expected and tomllib.loads(formatted) == tomllib.loads(expected)
    _write(pyproject, formatted)
    assert render.pyproject_outdated(cfg) is False
    assert render.write_pyproject(cfg) is False and pyproject.read_text(encoding="utf-8") == formatted
    assert render.pyproject_outdated(preset_cfg("raylib", {"python": {"cpython": "3.13"}})) is True  # a real change
    _write(pyproject, formatted.replace(" # <<< pytemplate", ""))
    assert render.pyproject_outdated(cfg) is True  # the markers still matter


def test_real_taplo_formatting_is_not_a_change(pyproject: Path, tmp_path: Path) -> None:
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    cfg = preset_cfg("flet", {"backend": {"supported": ["cpython", "pypy", "mypyc"]}})
    _write(pyproject, pyproject_text(cfg))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_PROJECT", "UV_PYTHON", "VIRTUAL_ENV", "PYTEMPLATE_"))}
    argv = [uv, "tool", "run", "--offline", "--from", "taplo==0.9.3", "taplo", "fmt", str(pyproject)]
    r = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False)
    if r.returncode != 0:
        pytest.skip(f"taplo 0.9.3 is not in the uv cache: {r.stderr.strip()[-200:]}")
    formatted = pyproject.read_text(encoding="utf-8")
    assert formatted != pyproject_text(cfg)  # taplo did reformat it
    assert render.pyproject_outdated(cfg) is False
    assert render.write_pyproject(cfg) is False and pyproject.read_text(encoding="utf-8") == formatted


@pytest.mark.parametrize(
    "line",
    [
        'requires-python = ">=3.14"',
        "requires-python = '>=3.14'",
        'requires-python=">=3.14"',
        '  requires-python = ">=3.14"  # comment',
        'requires-python = """>=3.14"""',
        "requires-python = '''>=3.14'''",
        'requires-python = """\n>=3.14"""',
        '"requires-python" = ">=3.14"',
        'requires-python = "\\u003e=3.14"',
        "",
    ],
    ids=["basic", "literal", "no-spaces", "indented", "multiline-basic", "multiline-literal", "multiline-newline", "quoted-key", "escape", "missing"],
)
def test_requires_python_is_managed_in_every_toml_form(pyproject: Path, line: str) -> None:
    cfg = preset_cfg(extra={"backend": {"supported": ["cpython", "pypy"]}})
    text = f'[project]\nname = "x"\n{line}\ndependencies = []\n\n[tool.other]\nrequires-python = "keep"\n\n[tool.uv]\nfoo = 1\n'
    _write(pyproject, text)
    assert render.pyproject_outdated(cfg) is True
    assert render.write_pyproject(cfg) is True
    once = pyproject.read_text(encoding="utf-8")
    data = tomllib.loads(once)
    assert data["project"]["requires-python"] == ">=3.11"
    assert data["tool"]["other"]["requires-python"] == "keep"  # only [project]'s is managed
    assert render.pyproject_expected(cfg, once) == once and render.pyproject_outdated(cfg) is False


def test_requires_python_literal_string_is_same_meaning_when_equal(pyproject: Path) -> None:
    text = pyproject_text(CFG).replace('requires-python = ">=3.14"', "requires-python = '>=3.14'")
    _write(pyproject, text)
    assert render.pyproject_outdated(CFG) is False and render.write_pyproject(CFG) is False


def test_bom_and_crlf_pyproject(pyproject: Path) -> None:
    text = pyproject_text(CFG)
    pyproject.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))
    before = pyproject.read_bytes()
    assert render.pyproject_outdated(CFG) is False and render.write_pyproject(CFG) is False
    assert pyproject.read_bytes() == before  # the user's line endings stay
    changed = preset_cfg(extra={"python": {"cpython": "3.13"}})
    assert render.write_pyproject(changed) is True
    raw = pyproject.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw and render.pyproject_outdated(changed) is False


@pytest.mark.parametrize(
    ("content", "message"),
    [(None, "pyproject.toml not found"), (b"\xff\xfe[\x00", "not UTF-8"), (b"[project\n", "not valid TOML")],
    ids=["missing", "utf-16", "invalid"],
)
def test_unreadable_pyproject(pyproject: Path, content: bytes | None, message: str) -> None:
    if content is not None:
        pyproject.write_bytes(content)
    with pytest.raises(DeployError, match=message):
        render.write_pyproject(CFG)
    assert render.pyproject_outdated(CFG) is True


def test_user_keys_in_tool_uv_survive(pyproject: Path) -> None:
    text = pyproject_text(CFG).replace("[tool.uv]\n", '[tool.uv]\nindex-url = "https://example.invalid/simple"\n')
    _write(pyproject, text.replace("3.15", "3.16"))  # an outdated managed value
    assert render.write_pyproject(CFG) is True
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["tool"]["uv"]["index-url"] == "https://example.invalid/simple"
    assert "3.15" in data["tool"]["uv"]["environments"][0]


PYPY_CFG = preset_cfg(extra={"backend": {"supported": ["cpython", "pypy", "mypyc"]}})
CFFI = "cffi>=1.15.1; implementation_name == 'cpython'"


def _own_list(text: str, line: str) -> str:
    """`text` with a [tool.uv] line of the project's own right after the managed block."""
    end = f"  {render.MARK_END}\n"
    assert text.count(end) == 1
    return text.replace(end, f"{end}{line}\n")


def test_a_project_keeps_its_own_override_dependencies(pyproject: Path) -> None:
    """The managed block owned the whole key: with the project's own list outside the markers,
    enabling PyPy gave invalid TOML (a repeated key) and lock, mode and apply refused."""
    text = _own_list(pyproject_text(CFG), 'override-dependencies = ["pygments>=2.19"]')
    _write(pyproject, text)
    assert render.pyproject_outdated(CFG) is False and render.write_pyproject(CFG) is False
    # PyPy needs the cffi override: the project's list must hold it, and the error says so
    with pytest.raises(DeployError) as e:
        render.check_pyproject(PYPY_CFG)
    assert "override-dependencies" in str(e.value) and f'"{CFFI}"' in str(e.value) and "outside the" in str(e.value)
    assert render.pyproject_outdated(PYPY_CFG) is True
    assert pyproject.read_bytes() == text.encode("utf-8")
    # with it (spelled another way), the block leaves the key to the project
    own = 'override-dependencies = ["pygments>=2.19", "cffi >= 1.15.1 ; implementation_name==\\"cpython\\""]'
    _write(pyproject, _own_list(pyproject_text(CFG), own))
    assert render.write_pyproject(PYPY_CFG) is True
    new = pyproject.read_text(encoding="utf-8")
    uv = tomllib.loads(new)["tool"]["uv"]
    assert uv["override-dependencies"] == ["pygments>=2.19", 'cffi >= 1.15.1 ; implementation_name=="cpython"']
    assert "pypy" in str(uv["environments"]) and new.count("override-dependencies") == 1
    assert render.pyproject_outdated(PYPY_CFG) is False and render.write_pyproject(PYPY_CFG) is False
    # without PyPy again: the project's list stays as it is
    assert render.write_pyproject(CFG) is True
    assert tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["uv"]["override-dependencies"] == uv["override-dependencies"]


def test_a_repeated_additive_key_is_repaired(pyproject: Path) -> None:
    """The project added its own list while the block had the key (invalid TOML, uv refuses it
    too): the rewrite leaves the key to the project once its list holds the block's entries."""
    text = _own_list(pyproject_text(PYPY_CFG), f'override-dependencies = ["{CFFI}", "rich<16"]')
    _write(pyproject, text)
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(text)
    assert render.write_pyproject(PYPY_CFG) is True
    uv = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["uv"]
    assert uv["override-dependencies"] == [CFFI, "rich<16"]


@pytest.mark.parametrize("package", ["raylib", "raylib_sdl"])
def test_a_raylib_project_keeps_its_own_no_build_package(pyproject: Path, package: str) -> None:
    cfg = preset_cfg("raylib", {"preset": {"raylib": {"package": package}}})
    base = pyproject_text(cfg)
    _write(pyproject, _own_list(base.replace(f'no-build-package = ["{package}"]\n', ""), 'no-build-package = ["numpy"]'))
    with pytest.raises(DeployError, match=f'(?s)no-build-package .*"{package}"'):
        render.write_pyproject(cfg)
    other = package.replace("_", "-").upper()  # uv normalizes names: the same package
    _write(pyproject, _own_list(base.replace(f'no-build-package = ["{package}"]\n', ""), f'no-build-package = ["numpy", "{other}"]'))
    assert render.write_pyproject(cfg) is False  # nothing to change: the list is the project's
    assert tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["uv"]["no-build-package"] == ["numpy", other]


def test_a_rewrite_never_changes_anything_else(pyproject: Path) -> None:
    # requires-python inside a multi-line string of [project] is not the key: rather than rewrite
    # the description, write_pyproject refuses (the file is left as it is)
    text = pyproject_text(CFG).replace("[project]\n", '[project]\nnotes = """\nrequires-python = ">=2.7"\n"""\n', 1)
    _write(pyproject, text.replace('requires-python = ">=3.14"', 'requires-python = ">=3.13"'))
    with pytest.raises(DeployError, match="would also change other settings"):
        render.write_pyproject(CFG)
    assert tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["notes"] == 'requires-python = ">=2.7"\n'


def test_verify_requires_the_managed_values_in_tool_uv() -> None:
    # A safety net behind the marker checks: whatever the rewrite did, uv must find the values
    text = pyproject_text(CFG)
    with pytest.raises(DeployError, match=r"did not end up in \[tool\.uv\]"):
        render._verify(CFG, text, text.replace("< '3.15'", "< '3.99'"))
    with pytest.raises(DeployError, match="would also change other settings"):
        render._verify(CFG, text, text.replace("[tool.uv]\n", "[tool.other]\n"))
    render._verify(CFG, text, text)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), datetime.date(2026, 1, 1), {1, 2}])
def test_generated_json_never_holds_what_json_cannot(value: object) -> None:
    with pytest.raises(DeployError, match="JSON cannot represent"):
        render.jsonc({"key": [value]})
    assert render.jsonc({"key": [1.5, "x", None, True]}).endswith('\n}\n')


def test_write_pyproject_under_dry_run_reports_but_writes_nothing(pyproject: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(pyproject, BASE)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert render.write_pyproject(CFG) is True
    assert pyproject.read_text(encoding="utf-8") == BASE


# --- typing profiles ---------------------------------------------------------------------------


@pytest.fixture
def profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(TEMPLATES / "typing", tmp_path / "typing")
    monkeypatch.setattr(render, "TEMPLATES", tmp_path)
    return tmp_path / "typing"


@pytest.mark.parametrize("name", config.PROFILES)
def test_profiles_tolerate_a_bom_and_crlf(profiles: Path, name: str) -> None:
    path = profiles / f"{name}.toml"
    expected = tomllib.loads(path.read_text(encoding="utf-8"))
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes().replace(b"\n", b"\r\n"))
    assert render.load_profile(name) == expected


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("select = [\n", "not valid TOML"),
        ("blocking = 1\n", "'blocking' must be true or false"),
        ('skip_mypy = "no"\n', "'skip_mypy' must be true or false"),
        ("description = 3\n", "'description' must be a string"),
        ("mypy = 1\n", "'mypy' must be a table"),
        ('vscode = "x"\n', "'vscode' must be a table"),
        ("pyright_compiled = []\n", "'pyright_compiled' must be a table"),
        ('[ruff]\nselect = "E"\n', "'ruff.select' must be a list of strings"),
        ("[ruff]\nignore = [1]\n", "'ruff.ignore' must be a list of strings"),
        ("[ruff]\nexit_zero = 1\n", "'ruff.exit_zero' must be true or false"),
    ],
)
def test_bad_profiles_are_clear_errors(profiles: Path, content: str, message: str) -> None:
    # a wrong type would otherwise crash deep inside a generator (e.g. "x".get(...))
    (profiles / "x.toml").write_text(content, encoding="utf-8")
    with pytest.raises(DeployError) as e:
        render.load_profile("x")
    assert e.value.code == 2 and "x.toml" in str(e.value) and message in str(e.value), str(e.value)


def test_profile_not_utf8_or_missing(profiles: Path) -> None:
    (profiles / "off.toml").write_bytes(b"\xff\xfed\x00")
    with pytest.raises(DeployError, match=r"off\.toml is not UTF-8"):
        render.load_profile("off")
    with pytest.raises(DeployError, match="typing profile not found: .*nope.toml"):
        render.load_profile("nope")


@pytest.mark.parametrize("editor", config.EDITORS)
@pytest.mark.parametrize("profile", config.PROFILES)
def test_pyright_config_uses_current_rule_names(profile: str, editor: str) -> None:
    conf = render.pyright_config(preset_cfg(extra={"typing": {"editor": editor}}), profile)
    assert "reportPossiblyUnbound" not in conf  # never a pyright or basedpyright setting
    if profile == "warn":
        assert conf["reportPossiblyUnboundVariable"] == "warning"


def test_profile_rule_names_are_known_to_the_pinned_basedpyright(tmp_path: Path) -> None:
    """One config with every [pyright] key of every profile (plus the basedpyright-only ones of the
    compiled modules): the pinned basedpyright must know them all. Needs it in the uv cache."""
    from runner import cmd_dev

    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    keys: dict[str, Any] = {}
    for name in config.PROFILES:
        data = render.load_profile(name)
        keys.update(data.get("pyright", {}))
        keys.update(data.get("basedpyright_compiled", {}))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pyrightconfig.json").write_text(json.dumps({"include": ["src"], **keys}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_PROJECT", "UV_PYTHON", "VIRTUAL_ENV", "PYTEMPLATE_"))}
    argv = [uv, "tool", "run", "--offline", "--from", cmd_dev.BASEDPYRIGHT, "basedpyright", "--project", str(tmp_path / "pyrightconfig.json")]
    r = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300, check=False)
    out = r.stdout + r.stderr
    if "0 errors" not in out and "unrecognized" not in out:
        pytest.skip(f"{cmd_dev.BASEDPYRIGHT} is not in the uv cache: {out.strip()[-200:]}")
    assert "unrecognized setting" not in out, out


# --- the generated CI workflow ---------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"" and (i == 0 or line[i - 1] in " :[,"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] == " "):
            return line[:i]
    return line


def _scalar(text: str, n: int) -> Any:
    if text.startswith('"'):
        assert len(text) > 1 and text.endswith('"') and '"' not in text[1:-1].replace('\\"', ""), f"line {n}: {text}"
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if text.startswith("'"):
        assert len(text) > 1 and text.endswith("'") and "'" not in text[1:-1].replace("''", ""), f"line {n}: {text}"
        return text[1:-1].replace("''", "'")
    if text.startswith("["):
        assert text.endswith("]") and "[" not in text[1:-1] and "]" not in text[1:-1], f"line {n}: {text}"
        return [_scalar(item.strip(), n) for item in text[1:-1].split(",")] if text[1:-1].strip() else []
    assert text[0] not in "-?:,]{}#&*!|>%@`" and ": " not in text and not text.endswith(":"), f"line {n}: not a plain scalar: {text}"
    return text


def _mapping(lines: list[tuple[int, str, int]], pos: int, indent: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while pos < len(lines) and lines[pos][0] == indent:
        _, content, n = lines[pos]
        m = re.fullmatch(r"([A-Za-z0-9_-]+):(?: (.*))?", content)
        assert m, f"line {n}: not a 'key: value' line: {content}"
        key, rest = m.group(1), (m.group(2) or "").strip()
        assert key not in out, f"line {n}: duplicate key {key}"
        pos += 1
        if rest:
            out[key] = _scalar(rest, n)
        elif pos < len(lines) and lines[pos][0] > indent:
            out[key], pos = _block(lines, pos)
        else:
            out[key] = None
    return out, pos


def _block(lines: list[tuple[int, str, int]], pos: int) -> tuple[Any, int]:
    indent, content, _ = lines[pos]
    if not content.startswith("- "):
        return _mapping(lines, pos, indent)
    out: list[Any] = []
    while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("- "):
        _, content, n = lines[pos]
        item = content[2:].lstrip(" ")
        column = indent + len(content) - len(item)
        if re.match(r"[A-Za-z0-9_-]+:(?: |$)", item):  # "- key: value": a mapping at the item's column
            lines[pos] = (column, item, n)
            value, pos = _mapping(lines, pos, column)
        else:
            value, pos = _scalar(item, n), pos + 1
        out.append(value)
    return out, pos


def _literal(raw: list[str], start: int, parent: int) -> tuple[str, int]:
    """The literal block scalar (`key: |`, clip chomping) whose lines start at raw[start]: every
    line indented more than `parent` (the key line), blank ones included. Returns (value, next)."""
    end = start
    while end < len(raw) and (not raw[end].strip() or len(raw[end]) - len(raw[end].lstrip(" ")) > parent):
        assert "\t" not in raw[end] and "\r" not in raw[end], f"line {end + 1}: tab or CR"
        end += 1
    body = raw[start:end]
    while body and not body[-1].strip():
        body.pop()
    assert body, f"line {start}: empty block scalar"
    indent = len(body[0]) - len(body[0].lstrip(" "))
    assert all(not line.strip() or line[:indent] == " " * indent for line in body), f"line {start + 1}: bad block indentation"
    return "\n".join(line[indent:] for line in body) + "\n", end


def _fill(value: Any, literals: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: _fill(v, literals) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, literals) for v in value]
    return literals.get(value, value) if isinstance(value, str) else value


def parse_yaml(text: str) -> Any:
    """A strict reader of the YAML subset templates/ci.yml uses: block mappings and sequences,
    plain and quoted scalars, flow sequences, literal block scalars (`key: |`) and comments.
    Anything else (tabs, anchors, tags, folded or chomped block scalars, flow mappings, a ': ' in
    a plain scalar, duplicate keys, a bad indent) fails."""
    lines: list[tuple[int, str, int]] = []
    literals: dict[str, str] = {}
    raw = text.split("\n")
    i = 0
    while i < len(raw):
        n, line = i + 1, raw[i]
        i += 1
        assert "\t" not in line and "\r" not in line, f"line {n}: tab or CR"
        content = _strip_comment(line).rstrip()
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        if re.fullmatch(r"(- )?[A-Za-z0-9_-]+: \|", content.strip()):
            token = f"__literal_{len(literals)}__"
            literals[token], i = _literal(raw, i, indent)
            content = content[:-1] + token
        lines.append((indent, content.strip(), n))
    assert lines and lines[0][0] == 0, "the document must start at column 0"
    doc, pos = _block(lines, 0)
    assert pos == len(lines), f"line {lines[pos][2]}: bad indentation"
    return _fill(doc, literals)


def test_the_yaml_reader_is_strict() -> None:
    assert parse_yaml("a: 1\nb:\n  - x: 'y'\n    z: [p, q]\n  - w\n") == {"a": "1", "b": [{"x": "y", "z": ["p", "q"]}, "w"]}
    literal = "a:\n  b: |\n    x # kept\n\n      y\n  c: 1\n- d: |\n    z\n"
    assert parse_yaml(literal.partition("- ")[0]) == {"a": {"b": "x # kept\n\n  y\n", "c": "1"}}
    assert parse_yaml("- d: |\n    z\n\n- e\n") == [{"d": "z\n"}, "e"]
    bad_yaml = (
        "a: 1\n b: 2\n", "a: 1\na: 2\n", "a: b: c\n", "a: *x\n", "a: {b: 1}\n", "a:\n\t- x\n", "- x\na: 1\n",
        "a: >\n  x\n", "a: |-\n  x\n", "a: |\nb: 1\n", "a: |\n    x\n  y\n",  # folded, chomped, empty, dedented
    )  # fmt: skip
    for bad in bad_yaml:
        with pytest.raises(AssertionError):
            parse_yaml(bad)


@pytest.mark.parametrize(("preset", "supported", "active"), COMBOS)
def test_ci_workflow_for_every_preset_and_backend_set(preset: str, supported: list[str], active: str) -> None:
    cfg = combo_cfg(preset, supported, active)
    text = render.ci_workflow(cfg)
    assert not re.search(r"__[A-Z_]+__", text) and text.isascii() and text.endswith("\n")
    doc = parse_yaml(text)
    assert doc["on"]["push"]["branches"] == ["main", "master"]  # `git init` gives either
    assert set(doc["on"]) == {"push", "pull_request", "workflow_dispatch"}
    test = doc["jobs"]["test"]
    rows = test["strategy"]["matrix"]["include"]
    macos_pypy = not (preset == "raylib" and "pypy" in supported)  # raylib: no PyPy wheels for macOS arm64
    expected_os = ["ubuntu-latest", "windows-latest"] + (["macos-latest"] if supported != ["pypy"] or macos_pypy else [])
    assert [r["os"] for r in rows] == expected_os
    for row in rows:
        backends = row["backends"].split()
        drop = "pypy" if row["os"] == "macos-latest" and not macos_pypy else ""
        assert backends == [b for b in supported if b != drop], row
    steps = test["steps"]
    runs = [s["run"] for s in steps if "run" in s]
    assert runs[0] == "./deploy render --check"  # before anything else can render
    assert [s["uses"].partition("@")[0] for s in steps[:2]] == ["actions/checkout", "astral-sh/setup-uv"]
    apt = [s for s in steps if "apt-get" in s.get("run", "")]
    assert len(apt) == (preset == "raylib") and all(s["if"] == "runner.os == 'Linux'" for s in apt)
    build = next(r for r in runs if r.startswith("./deploy build "))
    backend = build.split()[2]
    assert backend == next((b for b in ("mypyc", "cpython") if b in supported), active)
    assert all(backend in r["backends"].split() for r in rows)  # every OS synced what it builds
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@"))
    # coupled to BuildRequest.out_name and the pyz method's output (dist/<out_name>/<name>.pyz)
    assert upload["with"]["path"] == f"dist/{BuildRequest(cfg, backend, 'pyz', Path('.')).out_name}/{cfg.app.name}.pyz"
    merge = doc["jobs"]["pyz"]
    assert merge["needs"] == "test" and merge["runs-on"] == "ubuntu-latest"
    assert f"--out dist/{cfg.app.name}.pyz" in merge["steps"][3]["run"]
    # the merged .pyz and the Windows wrapper pyz-merge writes next to it (pyz.wrapper_path)
    wrapper = pyz.wrapper_path(Path(f"dist/{cfg.app.name}.pyz")).as_posix()
    assert merge["steps"][4]["with"]["path"] == f"dist/{cfg.app.name}.pyz\n{wrapper}\n"
    uses = [s["uses"] for job in doc["jobs"].values() for s in job["steps"] if "uses" in s]
    assert all(re.fullmatch(r"[\w-]+/[\w-]+@v\d+(\.\d+\.\d+)?", u) for u in uses), uses
    assert all(re.fullmatch(r"astral-sh/setup-uv@v\d+\.\d+\.\d+", u) for u in uses if "setup-uv" in u)  # no floating tags


@pytest.mark.parametrize(("preset", "supported", "active"), COMBOS)
def test_ci_workflow_keeps_its_moving_parts_on_purpose(preset: str, supported: list[str], active: str) -> None:
    """-latest runner labels (GitHub retires pinned ones) and no uv version (setup-uv takes the
    newest that satisfies pyproject's required-version; a pinned uv cannot download newer
    Pythons). The template says why, so nobody 'fixes' either."""
    doc = parse_yaml(render.ci_workflow(combo_cfg(preset, supported, active)))
    labels = [row["os"] for row in doc["jobs"]["test"]["strategy"]["matrix"]["include"]] + [doc["jobs"]["pyz"]["runs-on"]]
    assert labels and all(label in ("ubuntu-latest", "windows-latest", "macos-latest") for label in labels), labels
    setup_uv = [s for job in doc["jobs"].values() for s in job["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert len(setup_uv) == 2 and not any("version" in s.get("with", {}) for s in setup_uv)
    header = (TEMPLATES / "ci.yml").read_text(encoding="utf-8").partition("\nname: ci")[0]
    assert "on purpose" in header and "required-version" in header and "-latest" in header


def test_ci_workflows_pass_actionlint(tmp_path: Path) -> None:
    actionlint = shutil.which("actionlint")
    if actionlint is None:
        pytest.skip("actionlint is not installed")
    files = []
    for i, (preset, supported, active) in enumerate(COMBOS):
        path = tmp_path / ".github" / "workflows" / f"ci{i}.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render.ci_workflow(combo_cfg(preset, supported, active)), encoding="utf-8", newline="\n")
        files.append(str(path))
    r = subprocess.run([actionlint, "-no-color", *files], cwd=tmp_path, capture_output=True, text=True, timeout=300, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.fixture
def ci_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copy(TEMPLATES / "ci.yml", tmp_path / "ci.yml")
    monkeypatch.setattr(render, "TEMPLATES", tmp_path)
    return tmp_path / "ci.yml"


def test_ci_template_bom_and_crlf_do_not_reach_the_workflow(ci_template: Path) -> None:
    expected = render.ci_workflow(CFG)
    lf = ci_template.read_bytes().replace(b"\r\n", b"\n")  # already CRLF in a Windows checkout
    ci_template.write_bytes(b"\xef\xbb\xbf" + lf.replace(b"\n", b"\r\n"))
    assert render.ci_workflow(CFG) == expected


def test_ci_template_linux_deps_line_may_be_indented(ci_template: Path) -> None:
    raylib = preset_cfg("raylib")
    expected = {"raylib": render.ci_workflow(raylib), "script": render.ci_workflow(CFG)}
    ci_template.write_text(ci_template.read_text(encoding="utf-8").replace("\n__LINUX_DEPS__\n", "\n      __LINUX_DEPS__  \n"), encoding="utf-8")
    assert render.ci_workflow(raylib) == expected["raylib"]
    assert render.ci_workflow(CFG) == expected["script"]  # without deps the whole line goes
    assert "apt-get" in expected["raylib"] and "apt-get" not in expected["script"]


def test_ci_template_placeholder_left_is_a_clear_error(ci_template: Path) -> None:
    ci_template.write_text(ci_template.read_text(encoding="utf-8").replace("\n__LINUX_DEPS__\n", "\n      - run: x __LINUX_DEPS__\n"), encoding="utf-8")
    with pytest.raises(DeployError, match="__LINUX_DEPS__ not replaced"):
        render.ci_workflow(CFG)


# --- every generated file, for every preset and backend set --------------------------------------

OUTPUT_COMBOS = [(p, s, a, {}) for p, s, a in COMBOS] + [
    ("script", ["cpython", "pypy", "mypyc"], "cpython", {"typing": {"editor": e, "relaxed": r}})
    for e in config.EDITORS
    for r in ("off", "warn", "strict")
]


def _check_file(path: str, text: str) -> None:
    assert text.endswith("\n") and "\r" not in text and "\ufeff" not in text and text.isascii(), path
    assert str(ROOT) not in text and ROOT.as_posix() not in text and str(Path.home()) + os.sep not in text, path
    if path.endswith(".json"):
        body = text.partition("\n")[2] if text.startswith("//") else text
        json.loads(body)
    elif path.endswith(".toml"):
        tomllib.loads(text)
    elif path.endswith(".ini"):
        configparser.ConfigParser(interpolation=None).read_string(text)
    elif path.endswith(".yml"):
        parse_yaml(text)
    elif path == nvim.LAZY_LUA:
        assert text == (TEMPLATES / "nvim" / "lazy.lua").read_text(encoding="utf-8")


@pytest.mark.parametrize(("preset", "supported", "active", "extra"), OUTPUT_COMBOS)
def test_every_output_is_clean(preset: str, supported: list[str], active: str, extra: dict[str, Any]) -> None:
    files = render.outputs(combo_cfg(preset, supported, active, extra))
    assert ".pytemplate/state.json" not in files and all(not p.startswith("/") and ".." not in p.split("/") for p in files)
    for path, text in files.items():
        _check_file(path, text)


DETERMINISM = """
import hashlib, sys, tomllib
sys.path.insert(0, sys.argv[1])
from runner import cli, config, render
digest = hashlib.sha256()
for preset in ("script", "raylib", "flet"):
    for supported in (["cpython"], ["cpython", "mypyc"], ["cpython", "pypy", "mypyc"]):
        text = open(f"{sys.argv[1]}/presets/{preset}/files/pytemplate.toml", encoding="utf-8").read()
        data = tomllib.loads(text.replace("{{name}}", "myapp").replace("{{pkg}}", "myapp"))
        data["backend"] = {"supported": supported, "active": "cpython"}
        cfg = config._build(config.Config, data, "")
        config.validate(cfg, set(cli.COMMANDS))
        for path, content in sorted(render.outputs(cfg).items()):
            digest.update(path.encode() + b"\\0" + content.encode() + b"\\0")
print(digest.hexdigest())
"""


def test_outputs_do_not_depend_on_the_hash_seed() -> None:
    digests = set()
    for seed in ("0", "1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        r = subprocess.run([sys.executable, "-c", DETERMINISM, str(ROOT / ".pytemplate")], env=env, capture_output=True, text=True, timeout=300, check=False)
        assert r.returncode == 0, r.stderr
        digests.add(r.stdout.strip())
    assert len(digests) == 1, digests
