"""Tests for ./deploy nvim (cmd_nvim) and the selftest --nvim harness (nvimtest)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_nvim, nvimtest  # noqa: E402
from runner.ui import DeployError  # noqa: E402

# lazyvim.json exactly as LazyVim 16 writes it on first start (util/json.lua: sorted keys,
# 2-space indent, an empty list as "[\n\n  ]", no final newline)
FRESH_LAZYVIM_JSON = '{\n  "extras": [\n\n  ],\n  "install_version": 8,\n  "news": {\n    "NEWS.md": "11866"\n  },\n  "version": 8\n}'


# --- versions and headless output --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.12.5+v0.12.5", (0, 12, 5)),  # tostring(vim.version()) of a release build
        ("NVIM v0.11.2", (0, 11, 2)),
        ("0.12.0-dev-1234+gabcdef", (0, 12, 0)),
        ("v1.0.0", (1, 0, 0)),
        ("nightly", None),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int, int] | None) -> None:
    assert cmd_nvim.parse_version(text) == expected


def test_version_thresholds() -> None:
    assert (0, 11, 1) < cmd_nvim.MIN_LAZYVIM <= (0, 11, 2)
    assert (0, 11, 9) < cmd_nvim.MIN_PATH_TRUST <= (0, 12, 0)


def test_parse_marker_skips_noise() -> None:
    out = 'some message\nPTNVIM{not json\n  PTNVIM{"config": "C:\\\\x\\\\nvim", "ok": true}\r\nPTNVIM{"second": 1}\n'
    assert cmd_nvim.parse_marker(out) == {"config": "C:\\x\\nvim", "ok": True}
    assert cmd_nvim.parse_marker("nothing here") is None


# --- trust database ------------------------------------------------------------------------------


def _lazy_lua(tmp_path: Path, content: bytes = b"return {}\n") -> tuple[Path, str, str]:
    project = tmp_path / "My Project"
    project.mkdir()
    file = project / ".lazy.lua"
    file.write_bytes(content)
    return file, os.path.realpath(file), hashlib.sha256(content).hexdigest()


def test_read_trust_db_formats(tmp_path: Path) -> None:
    db = tmp_path / "trust"
    # Neovim writes it in text mode: CRLF on Windows. Paths may contain spaces.
    db.write_bytes(b"abc123 C:\\Users\\me\\My Project\\.lazy.lua\r\n! /home/me/x/.nvim.lua\r\ngarbage\r\n\r\n")
    assert cmd_nvim.read_trust_db(db) == [
        ("abc123", "C:\\Users\\me\\My Project\\.lazy.lua"),
        ("!", "/home/me/x/.nvim.lua"),
    ]
    assert cmd_nvim.read_trust_db(tmp_path / "missing") == []


def test_trust_states(tmp_path: Path) -> None:
    file, real, digest = _lazy_lua(tmp_path)
    db = tmp_path / "state" / "trust"
    assert cmd_nvim.trust_status(db, file).state == "untrusted"  # no database at all
    db.parent.mkdir()
    db.write_text(f"{'0' * 64} /elsewhere/.lazy.lua\n{digest} {real}\n", encoding="utf-8")
    status = cmd_nvim.trust_status(db, file)
    assert (status.state, status.path, status.sha256, status.recorded) == ("trusted", real, digest, digest)
    db.write_text(f"{'f' * 64} {real}\n", encoding="utf-8")
    assert cmd_nvim.trust_status(db, file).state == "changed"
    db.write_text(f"! {real}\n", encoding="utf-8")
    assert cmd_nvim.trust_status(db, file).state == "denied"
    missing = cmd_nvim.trust_status(db, file.parent / "nope.lua")
    assert (missing.state, missing.sha256) == ("missing", "")


def test_trust_hash_is_over_raw_bytes(tmp_path: Path) -> None:
    file, real, digest = _lazy_lua(tmp_path, b"return {}\n")
    db = tmp_path / "trust"
    db.write_text(f"{digest} {real}\n", encoding="utf-8")
    file.write_bytes(b"return {}\r\n")  # a CRLF checkout breaks the trust
    assert cmd_nvim.trust_status(db, file).state == "changed"


def test_trust_windows_paths_are_case_insensitive(tmp_path: Path) -> None:
    file, real, digest = _lazy_lua(tmp_path)
    db = tmp_path / "trust"
    db.write_text(f"{digest} {real.upper().replace('/', chr(92))}\n", encoding="utf-8")
    assert cmd_nvim.trust_status(db, file, windows=True).state == "trusted"
    if real.upper() != real:
        assert cmd_nvim.trust_status(db, file, windows=False).state == "untrusted"
    # an exact entry wins over a case-insensitive one
    db.write_text(f"{'0' * 64} {real.upper()}\n{digest} {real}\n", encoding="utf-8")
    assert cmd_nvim.trust_status(db, file, windows=True).state == "trusted"


def test_same_path() -> None:
    assert cmd_nvim.same_path("C:\\Users\\Me\\p\\.lazy.lua", "c:/users/me/P/.lazy.lua", windows=True)
    assert not cmd_nvim.same_path("/home/Me/.lazy.lua", "/home/me/.lazy.lua", windows=False)


# --- lazyvim.json ----------------------------------------------------------------------------------


def test_extras_merge_backup_shape_and_idempotence(tmp_path: Path) -> None:
    path = tmp_path / "lazyvim.json"
    path.write_text(FRESH_LAZYVIM_JSON, encoding="utf-8", newline="\n")
    assert cmd_nvim.missing_extras(path) == list(cmd_nvim.EXTRAS)

    added, backup = cmd_nvim.enable_extras(path, stamp="20260925-120000")
    assert added == list(cmd_nvim.EXTRAS)
    assert backup == tmp_path / "lazyvim.json.20260925-120000.bak"
    assert backup.read_text(encoding="utf-8") == FRESH_LAZYVIM_JSON  # untouched copy
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert list(data) == ["extras", "install_version", "news", "version"]  # sorted like LazyVim
    assert (data["version"], data["install_version"], data["news"]) == (8, 8, {"NEWS.md": "11866"})
    assert data["extras"] == list(cmd_nvim.EXTRAS)
    assert text.startswith('{\n  "extras": [\n    "lazyvim.plugins.extras.lang.python",\n')
    assert not text.endswith("\n") and "\r" not in text
    assert cmd_nvim.missing_extras(path) == []

    # second run: nothing to add, nothing written, no new backup
    added, backup = cmd_nvim.enable_extras(path, stamp="20260925-120001")
    assert (added, backup) == ([], None)
    assert path.read_text(encoding="utf-8") == text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lazyvim.json", "lazyvim.json.20260925-120000.bak"]


def test_extras_keep_the_users_own(tmp_path: Path) -> None:
    path = tmp_path / "lazyvim.json"
    mine = ["lazyvim.plugins.extras.lang.rust", "lazyvim.plugins.extras.dap.core"]
    path.write_text(json.dumps({"extras": mine, "version": 8, "custom": True}), encoding="utf-8")
    added, backup = cmd_nvim.enable_extras(path, stamp="s")
    assert "lazyvim.plugins.extras.dap.core" not in added and len(added) == len(cmd_nvim.EXTRAS) - 1
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["extras"][:2] == mine and data["custom"] is True
    # same stamp again (after removing one extra): the first backup is not overwritten
    data["extras"].remove("lazyvim.plugins.extras.lang.toml")
    path.write_text(json.dumps(data), encoding="utf-8")
    _, second = cmd_nvim.enable_extras(path, stamp="s")
    assert backup is not None and second is not None and second != backup and backup.is_file()


def test_extras_dry_run_and_bad_files(tmp_path: Path) -> None:
    path = tmp_path / "lazyvim.json"
    path.write_text(FRESH_LAZYVIM_JSON, encoding="utf-8")
    added, backup = cmd_nvim.enable_extras(path, dry_run=True)
    assert len(added) == len(cmd_nvim.EXTRAS) and backup is None
    assert path.read_text(encoding="utf-8") == FRESH_LAZYVIM_JSON
    assert cmd_nvim.missing_extras(tmp_path / "absent.json") is None
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(DeployError, match="JSON object"):
        cmd_nvim.enable_extras(path)
    path.write_text('{"extras": "lang.python"}', encoding="utf-8")
    with pytest.raises(DeployError, match="not a list"):
        cmd_nvim.enable_extras(path)


def test_local_spec_off(tmp_path: Path) -> None:
    config = tmp_path / "nvim"
    (config / "lua" / "config").mkdir(parents=True)
    (config / "init.lua").write_text('require("config.lazy")\n', encoding="utf-8")
    lazy = config / "lua" / "config" / "lazy.lua"
    lazy.write_text("require('lazy').setup({\n  -- local_spec = false,\n  spec = {},\n})\n", encoding="utf-8")
    assert cmd_nvim.local_spec_off(config) == []  # commented out
    lazy.write_text("require('lazy').setup({ local_spec=false, spec = {} })\n", encoding="utf-8")
    assert cmd_nvim.local_spec_off(config) == [lazy]
    assert cmd_nvim.local_spec_off(tmp_path / "absent") == []


def test_lazyvim_installed(tmp_path: Path) -> None:
    nv = cmd_nvim.Nvim("nvim", (0, 12, 5), tmp_path / "c", tmp_path / "d", tmp_path / "s", tmp_path / "k")
    assert not nv.lazyvim_installed()
    (tmp_path / "d" / "lazy" / "LazyVim").mkdir(parents=True)
    assert nv.lazyvim_installed()
    assert nv.trust_db == tmp_path / "s" / "trust" and nv.lazyvim_json == tmp_path / "c" / "lazyvim.json"


def test_remove_tree_read_only(tmp_path: Path) -> None:
    obj = tmp_path / "repo" / ".git" / "objects" / "ab" / "cdef"
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b"x")
    obj.chmod(0o444)  # git objects are read-only: a plain rmtree fails on Windows
    cmd_nvim.remove_tree(tmp_path / "repo")
    assert not (tmp_path / "repo").exists()
    cmd_nvim.remove_tree(tmp_path / "repo")  # missing: no error


# --- selftest --nvim harness ------------------------------------------------------------------------


def test_parse_smoke() -> None:
    out = (
        "ok   pytemplate.nvim loaded from .lazy.lua\r\n"
        "FAIL overseer templates\n"
        "...smoke.lua:12: duplicate deploy: run\n"
        "stack traceback:\n"
        "\t[C]: in function 'error'\n"
        "SKIP mypy lint (profile off)\n"
        "ok   parser\n"
        "random noise\n"
    )
    s = nvimtest.parse_smoke(out)
    assert s.passed == ["pytemplate.nvim loaded from .lazy.lua", "parser"]
    assert s.skipped == ["mypy lint (profile off)"]
    assert [name for name, _ in s.failed] == ["overseer templates"]
    assert s.failed[0][1].splitlines()[0] == "...smoke.lua:12: duplicate deploy: run"
    assert "stack traceback:" in s.failed[0][1]
    assert s.other == ["random noise"]
    assert s.total == 4
    assert nvimtest.parse_smoke("").total == 0


def test_row_ok() -> None:
    good = nvimtest.Row("script", smoke=nvimtest.parse_smoke("ok   a\n"), code=0)
    assert good.ok
    assert not nvimtest.Row("script", smoke=nvimtest.parse_smoke("ok   a\n"), code=1).ok
    assert not nvimtest.Row("script", smoke=nvimtest.parse_smoke("FAIL a\n"), code=0).ok
    assert not nvimtest.Row("script", smoke=nvimtest.parse_smoke(""), code=0).ok  # no result lines
    assert not nvimtest.Row("script", smoke=nvimtest.parse_smoke("ok   a\n"), code=0, error="boom").ok


def test_env_isolation(tmp_path: Path) -> None:
    user = {
        "PATH": os.environ.get("PATH", ""),
        "XDG_CONFIG_HOME": "/home/me/.config",
        "XDG_DATA_HOME": "/home/me/.local/share",
        "XDG_STATE_HOME": "/home/me/.local/state",
        "XDG_CACHE_HOME": "/home/me/.cache",
        "XDG_CONFIG_DIRS": "/etc/xdg:/home/me/dots",
        "NVIM_APPNAME": "mynvim",
        "NVIM_LOG_FILE": "/home/me/nvim.log",
        "NVIM": "/run/nvim.sock",
        "VIMINIT": "lua require('mine')",
        "MYVIMRC": "/home/me/.config/nvim/init.lua",
        "VIRTUAL_ENV": "/home/me/proj/.venv",
        "UV": "/usr/bin/uv",
        "UV_PROJECT_ENVIRONMENT": "/x",
        "PYTEMPLATE_CALLER_CWD": "/home/me/proj",
        "PYTEMPLATE_LAUNCHER": "sh",
        "UV_CACHE_DIR": "/home/me/.cache/uv",
        "HOME": "/home/me",
    }
    layout = nvimtest.Layout(tmp_path / "w")
    renv = nvimtest.runner_env(user)
    assert not {"VIRTUAL_ENV", "UV", "UV_PROJECT_ENVIRONMENT", "PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER"} & set(renv)
    assert renv["XDG_CACHE_HOME"] == "/home/me/.cache"  # uv's own caches keep working for ./deploy

    env = nvimtest.nvim_env(layout, user, {"UV_CACHE_DIR": "/ignored", "UV_PYTHON_INSTALL_DIR": "/home/me/.local/share/uv/python"})
    for key in nvimtest.XDG_HOMES:
        assert Path(env[key]).is_relative_to(layout.xdg), key
    assert env["NVIM_LOG_FILE"] == str(layout.nvim_log)
    for key in ("NVIM_APPNAME", "NVIM", "VIMINIT", "MYVIMRC", "XDG_CONFIG_DIRS", "VIRTUAL_ENV", "UV", "PYTEMPLATE_CALLER_CWD"):
        assert key not in env, key
    assert env["UV_CACHE_DIR"] == "/home/me/.cache/uv"  # the user's own value wins over `keep`
    assert env["UV_PYTHON_INSTALL_DIR"] == "/home/me/.local/share/uv/python"
    home_values = {user[k] for k in user if k.startswith(("XDG_", "NVIM", "VIMINIT", "MYVIMRC"))}
    assert not home_values & set(env.values())
    assert len({env[k] for k in nvimtest.XDG_HOMES}) == 4


def test_default_dir_is_short() -> None:
    d = nvimtest.default_dir()
    assert d.name in ("nvim", "pt-nvim") and len(str(d)) < 80


def test_prepare_dir_refuses_foreign_dirs(tmp_path: Path) -> None:
    (tmp_path / "mine.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DeployError, match="not created by selftest"):
        nvimtest._prepare_dir(nvimtest.Layout(tmp_path))
    with pytest.raises(DeployError, match="outside the template"):
        nvimtest._prepare_dir(nvimtest.Layout(Path(__file__).resolve().parents[2] / ".build" / "nvim"))
    fresh = nvimtest.Layout(tmp_path / "work")
    nvimtest._prepare_dir(fresh)
    nvimtest._prepare_dir(fresh)  # reusable: it carries the marker
    assert (fresh.base / nvimtest.DIR_MARKER).is_file()


# --- real Neovim (skipped when it is not installed) ---------------------------------------------------


@pytest.mark.skipif(shutil.which("nvim") is None, reason="nvim not in PATH")
def test_real_nvim_query_and_trust(tmp_path: Path) -> None:
    layout = nvimtest.Layout(tmp_path / "w")
    env = nvimtest.nvim_env(layout, dict(os.environ))
    nv = cmd_nvim.query(shutil.which("nvim"), env=env)
    assert nv is not None and nv.version >= (0, 9, 0)
    nvimtest._check_isolated(nv, layout)  # config/data/state/cache all inside the throwaway tree
    file, real, digest = _lazy_lua(tmp_path)
    assert cmd_nvim.trust_status(nv.trust_db, file).state == "untrusted"
    data = cmd_nvim.trust_file(nv.exe, file, env=env, cwd=file.parent)  # path form on 0.12+, buffer before
    assert data["ok"] is True
    status = cmd_nvim.trust_status(nv.trust_db, file)
    assert (status.state, status.sha256, status.path) == ("trusted", digest, real)
    assert not (file.parent / "nvim.log").exists() and not Path("nvim.log").exists()
