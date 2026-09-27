"""Tests for ./pyt nvim (cmd_nvim) and the selftest --nvim harness (nvimtest)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_nvim, nvimtest, proc, project  # noqa: E402
from runner.ui import PytError  # noqa: E402

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
        assert cmd_nvim.trust_status(db, file, windows=False, macos=False).state == "untrusted"
    # an exact entry wins over a case-insensitive one
    db.write_text(f"{'0' * 64} {real.upper()}\n{digest} {real}\n", encoding="utf-8")
    assert cmd_nvim.trust_status(db, file, windows=True).state == "trusted"


def test_trust_macos_paths_ignore_case_and_unicode_form(tmp_path: Path) -> None:
    """macOS volumes are case- and normalization-insensitive by default. Neovim keys the trust
    database with realpath(3), which returns the on-disk spelling (`MyGame`, NFD accents);
    Python's os.path.realpath keeps what was typed (`cd ~/projects/mygame`, NFC): a trusted
    .lazy.lua read as untrusted, and `nvim trust` failed after Neovim reported success."""
    project = tmp_path / "caf\u00e9 Game"  # typed: NFC, this case
    project.mkdir()
    file = project / ".lazy.lua"
    file.write_bytes(b"return {}\n")
    digest = hashlib.sha256(b"return {}\n").hexdigest()
    real = os.path.realpath(file)
    on_disk = real.replace("caf\u00e9 Game", "CAFE\u0301 GAME")  # Neovim's key: another case, NFD
    db = tmp_path / "trust"
    db.write_text(f"{digest} {on_disk}\n", encoding="utf-8")
    assert cmd_nvim.trust_status(db, file, windows=False, macos=True).state == "trusted"
    assert cmd_nvim.trust_status(db, file, windows=False, macos=False).state == "untrusted"  # Linux
    db.write_text(f"! {on_disk}\n", encoding="utf-8")
    assert cmd_nvim.trust_status(db, file, windows=False, macos=True).state == "denied"


def test_same_path() -> None:
    assert cmd_nvim.same_path("C:\\Users\\Me\\p\\.lazy.lua", "c:/users/me/P/.lazy.lua", windows=True)
    assert not cmd_nvim.same_path("/home/Me/.lazy.lua", "/home/me/.lazy.lua", windows=False, macos=False)
    assert cmd_nvim.same_path("/Users/Me/Caf\u00e9/.lazy.lua", "/Users/me/cafe\u0301/.lazy.lua", windows=False, macos=True)
    assert not cmd_nvim.same_path("/Users/me/a/.lazy.lua", "/Users/me/b/.lazy.lua", windows=False, macos=True)


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
    with pytest.raises(PytError, match="JSON object"):
        cmd_nvim.enable_extras(path)
    path.write_text('{"extras": "lang.python"}', encoding="utf-8")
    with pytest.raises(PytError, match="not a list"):
        cmd_nvim.enable_extras(path)


def test_extras_in_a_config_that_cannot_be_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A read-only config (a Nix store, another user's file, chattr +i) ended `nvim extras` in an
    # internal-error traceback, and a failed write of lazyvim.json left its .bak behind
    path = tmp_path / "lazyvim.json"
    path.write_text(FRESH_LAZYVIM_JSON, encoding="utf-8", newline="\n")

    def denied(target: Path, data: bytes) -> None:
        raise PermissionError(1, "Operation not permitted", str(target))

    monkeypatch.setattr(cmd_nvim, "write_whole", denied)
    with pytest.raises(PytError, match=r"cannot write .*lazyvim.json: Operation not permitted\. Enable the extras by hand .*lang\.python") as e:
        cmd_nvim.enable_extras(path, stamp="s")
    assert e.value.code == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lazyvim.json"]  # no backup left behind
    assert path.read_text(encoding="utf-8") == FRESH_LAZYVIM_JSON
    real_write_bytes = Path.write_bytes

    def no_backup(self: Path, data: bytes) -> int:
        if self.name.endswith(".bak"):
            raise PermissionError(13, "Permission denied", str(self))
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", no_backup)
    with pytest.raises(PytError, match=r"cannot write .*\.bak: Permission denied"):
        cmd_nvim.enable_extras(path, stamp="s")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lazyvim.json"]


def test_extras_keep_a_linked_lazyvim_json_a_link(tmp_path: Path) -> None:
    # dotfiles managers link lazyvim.json into a repository: the file behind the link changes
    if sys.platform == "win32":
        pytest.skip("symlinks need a privilege on Windows")
    real = tmp_path / "dotfiles" / "lazyvim.json"
    real.parent.mkdir()
    real.write_text(FRESH_LAZYVIM_JSON, encoding="utf-8", newline="\n")
    config = tmp_path / "nvim"
    config.mkdir()
    (config / "lazyvim.json").symlink_to(real)
    cmd_nvim.enable_extras(config / "lazyvim.json", stamp="s")
    assert (config / "lazyvim.json").is_symlink() and json.loads(real.read_text(encoding="utf-8"))["extras"] == list(cmd_nvim.EXTRAS)


def test_bootstrap_says_what_to_do_when_the_starter_git_cannot_be_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nv = cmd_nvim.Nvim("nvim", (0, 12, 5), tmp_path / "c", tmp_path / "d", tmp_path / "s", tmp_path / "k")
    monkeypatch.setattr(cmd_nvim, "which", lambda name: "/usr/bin/git")

    def clone(argv: list[object], **_: object) -> subprocess.CompletedProcess[str]:
        (nv.config / ".git").mkdir(parents=True)
        return subprocess.CompletedProcess([str(a) for a in argv], 0, "", "")

    def stuck(path: Path) -> None:
        raise PermissionError(1, "Operation not permitted", str(path / "objects"))

    monkeypatch.setattr(cmd_nvim.proc, "run", clone)
    monkeypatch.setattr(cmd_nvim, "remove_tree", stuck)
    with pytest.raises(PytError, match=r"the starter is in .*, but its \.git could not be removed .*delete it by hand") as e:
        cmd_nvim.cmd_bootstrap(nv)
    assert e.value.code == 3


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


# lua/config/lazy.lua of the LazyVim starter (its spec part)
STARTER_LAZY_LUA = """require("lazy").setup({
  spec = {
    -- add LazyVim and import its plugins
    { "LazyVim/LazyVim", import = "lazyvim.plugins" },
    -- import/override with your plugins
    { import = "plugins" },
  },
})
"""


def test_lazyvim_is_told_from_the_config_not_from_a_file_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def nvim(name: str) -> cmd_nvim.Nvim:
        home = tmp_path / name
        return cmd_nvim.Nvim("nvim", (0, 12, 5), home / "c", home / "d", home / "s", home / "k")

    # lazy.nvim's own Structured Setup without LazyVim passed as LazyVim (lua/config/lazy.lua),
    # and doctor, sync and bootstrap then promised an integration that needs LazyVim's extras
    plain = nvim("plain")
    (plain.config / "lua" / "config").mkdir(parents=True)
    (plain.config / "init.lua").write_text('require("config.lazy")\n', encoding="utf-8")
    (plain.config / "lua" / "config" / "lazy.lua").write_text(
        'require("lazy").setup({ spec = { { import = "plugins" } } })\n'
        '-- { "LazyVim/LazyVim", import = "lazyvim.plugins" },\n'
        '--[[\n{ "LazyVim/LazyVim" }\n]]\n',
        encoding="utf-8",
    )
    (plain.data / "lazy" / "lazy.nvim").mkdir(parents=True)
    (plain.config / "lazy-lock.json").write_text('{"lazy.nvim": {"branch": "main", "commit": "x"}}', encoding="utf-8")
    assert not plain.lazyvim_installed()
    with pytest.raises(PytError, match="LazyVim is not this Neovim's config .*bootstrap .*lazyvim.github.io") as e:
        cmd_nvim.cmd_sync(plain)
    assert e.value.code == 3
    assert cmd_nvim.cmd_bootstrap(plain) == 0 and "It is not a LazyVim config" in capsys.readouterr().err
    # LazyVim in one init.lua with its own lazy.nvim root was refused as "not found"
    custom = nvim("custom")
    custom.config.mkdir(parents=True)
    (custom.config / "init.lua").write_text(
        "require('lazy').setup({ root = vim.fn.stdpath('data') .. '/plugins', spec = { { 'LazyVim/LazyVim', import = 'lazyvim.plugins' } } })\n",
        encoding="utf-8",
    )
    assert custom.lazyvim_installed()
    # once started, the lockfile names it (a spec kept elsewhere: vim.g.lazyvim_json, a module)
    locked = nvim("locked")
    locked.config.mkdir(parents=True)
    (locked.config / "lazy-lock.json").write_text('{"LazyVim": {"branch": "main", "commit": "x"}}', encoding="utf-8")
    assert locked.lazyvim_installed()
    # the starter as bootstrap clones it, before its first start
    starter = nvim("starter")
    (starter.config / "lua" / "config").mkdir(parents=True)
    (starter.config / "lua" / "config" / "lazy.lua").write_text(STARTER_LAZY_LUA, encoding="utf-8")
    assert starter.lazyvim_installed()


def test_nvim_doctor_flags_a_config_without_lazyvim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _doctor(tmp_path, monkeypatch, capsys, lazy_lua='require("lazy").setup({ spec = { { import = "plugins" } } })\n')
    assert code == 1 and "[XX] LazyVim not found" in out and "Start Neovim once" not in out, out


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
        "\nok   pytemplate.nvim loaded from .lazy.lua\r\n"
        "\nFAIL overseer templates\n"
        "...smoke.lua:12: duplicate pyt: run\n"
        "stack traceback:\n"
        "\t[C]: in function 'error'\n"
        "\nSKIP mypy lint (profile off)\n"
        "\nok   parser\n"
        "random noise\n"
        "\nDONE 4\n"
    )
    s = nvimtest.parse_smoke(out)
    assert s.passed == ["pytemplate.nvim loaded from .lazy.lua", "parser"]
    assert s.skipped == ["mypy lint (profile off)"]
    assert [name for name, _ in s.failed] == ["overseer templates"]
    assert s.failed[0][1].splitlines()[0] == "...smoke.lua:12: duplicate pyt: run"
    assert "stack traceback:" in s.failed[0][1] and not s.failed[0][1].endswith("\n")
    assert s.other == ["random noise"]
    assert (s.total, s.expected, s.complete) == (4, 4, True)
    assert nvimtest.parse_smoke("").total == 0


def test_parse_smoke_requires_done() -> None:
    """A result glued onto leaked output (no newline before it) is lost: the DONE count shows it."""
    lost = nvimtest.parse_smoke("ok   a\ngarbage from a pty ok   x\nok   y\nDONE 3\n")
    assert (lost.passed, lost.total, lost.expected, lost.complete) == (["a", "y"], 2, 3, False)
    assert lost.other == ["garbage from a pty ok   x"]
    assert not nvimtest.Row("script", smoke=lost, code=0).ok
    assert "3 checks but 2 result lines" in nvimtest.smoke_problem(lost) and "garbage" in nvimtest.smoke_problem(lost)
    unfinished = nvimtest.parse_smoke("ok   a\n")  # killed, or a watchdog: no DONE
    assert not unfinished.complete and "no DONE line" in nvimtest.smoke_problem(unfinished)


def test_smoke_problem_wants_a_typing_profile() -> None:
    off = nvimtest.parse_smoke("ok   mypy diagnostics (profile off)\nDONE 1\n")
    assert f"--typing {nvimtest.SMOKE_TYPING}" in nvimtest.smoke_problem(off)
    missing = nvimtest.parse_smoke("ok   a\nDONE 1\n")
    assert "did not run with a typing profile" in nvimtest.smoke_problem(missing)
    good = nvimtest.parse_smoke("ok   a\nok   mypy diagnostics (profile strict)\nDONE 2\n")
    assert nvimtest.smoke_problem(good) == ""


def test_row_ok() -> None:
    good = nvimtest.Row("script", smoke=nvimtest.parse_smoke("ok   a\nDONE 1\n"), code=0)
    assert good.ok
    assert not nvimtest.Row("script", smoke=nvimtest.parse_smoke("ok   a\n"), code=0).ok  # no DONE
    assert not nvimtest.Row("script", smoke=nvimtest.parse_smoke("ok   a\nDONE 1\n"), code=1).ok
    assert not nvimtest.Row("script", smoke=nvimtest.parse_smoke("FAIL a\nDONE 1\n"), code=0).ok
    assert not nvimtest.Row("script", smoke=nvimtest.parse_smoke("DONE 0\n"), code=0).ok  # no result lines
    assert not nvimtest.Row("script", smoke=nvimtest.parse_smoke("ok   a\nDONE 1\n"), code=0, error="boom").ok


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
    assert renv["XDG_CACHE_HOME"] == "/home/me/.cache"  # uv's own caches keep working for ./pyt

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
    assert d.name in ("nvim", project.scratch_name("pt-nvim")) and len(str(d)) < 80


def test_prepare_dir_refuses_foreign_dirs(tmp_path: Path) -> None:
    (tmp_path / "mine.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PytError, match="not created by selftest"):
        nvimtest._prepare_dir(nvimtest.Layout(tmp_path))
    with pytest.raises(PytError, match="outside the template"):
        nvimtest._prepare_dir(nvimtest.Layout(Path(__file__).resolve().parents[2] / ".build" / "nvim"))
    fresh = nvimtest.Layout(tmp_path / "work")
    nvimtest._prepare_dir(fresh)
    nvimtest._prepare_dir(fresh)  # reusable: it carries the marker
    assert (fresh.base / nvimtest.DIR_MARKER).is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owners and modes (Windows %TEMP% is per user)")
def test_prepare_dir_refuses_a_dir_another_user_can_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The isolated LazyVim runs from --dir: a /tmp/pt-nvim made by another user, or one every
    user can write, let that user plant code there. The default is per user and made 0700."""
    assert nvimtest.default_dir().name == project.scratch_name("pt-nvim") != "pt-nvim"
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(PytError, match="written by every user"):
        nvimtest._prepare_dir(nvimtest.Layout(shared))
    shared.chmod(0o700)
    real = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real + 1)
    with pytest.raises(PytError, match="belongs to another user"):
        nvimtest._prepare_dir(nvimtest.Layout(shared))
    assert not (shared / nvimtest.DIR_MARKER).exists()
    monkeypatch.setattr(os, "getuid", lambda: real)
    fresh = nvimtest.Layout(tmp_path / "fresh" / "dir")
    nvimtest._prepare_dir(fresh)
    assert fresh.base.stat().st_mode & 0o777 == 0o700


def test_prepare_dir_refuses_a_file(tmp_path: Path) -> None:
    """A --dir that names a file (a typo, a log) is a usage error, not an internal one."""
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    for base in (afile, afile / "sub"):
        with pytest.raises(PytError, match="is not a folder|cannot create") as e:
            nvimtest._prepare_dir(nvimtest.Layout(base))
        assert e.value.code == 2 and str(base) in str(e.value)
    assert afile.read_text(encoding="utf-8") == "x"


def test_a_failed_step_is_a_suite_fail(tmp_path: Path) -> None:
    """A failed clone, Lazy! install or restore fails the suite (exit 1), not the usage (2)."""
    log = tmp_path / "logs" / "clone.log"
    argv = [sys.executable, "-c", "import sys; print('fatal: unable to access'); sys.exit(128)"]
    with pytest.raises(PytError, match=r"clone the LazyVim starter: exit code 128 \(log: .*clone\.log\)\n.*fatal: unable to access") as e:
        nvimtest._step(argv, cwd=tmp_path, env=dict(os.environ), log=log, timeout=60, what="clone the LazyVim starter")
    assert e.value.code == 1


def test_a_tree_that_cannot_be_removed_is_a_suite_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def remove_tree(path: Path) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(cmd_nvim, "remove_tree", remove_tree)
    with pytest.raises(PytError, match=r"(?s)cannot remove .*still using it") as e:
        nvimtest._remove(tmp_path / "x")
    assert e.value.code == 1


# --- real Neovim (skipped when it is not installed) ---------------------------------------------------


@pytest.mark.skipif(shutil.which("nvim") is None, reason="nvim not in PATH")
def test_real_nvim_query_and_trust(tmp_path: Path) -> None:
    layout = nvimtest.Layout(tmp_path / "w")
    env = nvimtest.nvim_env(layout, dict(os.environ))
    nv = cmd_nvim.query(shutil.which("nvim"), env=env)  # any Neovim: an old one is reported as such
    assert nv is not None
    nvimtest._check_isolated(nv, layout)  # config/data/state/cache all inside the throwaway tree
    if nv.version < (0, 9, 0):
        pytest.skip(f"Neovim {nv.version_text}: vim.secure (the trust database) came with 0.9")
    file, real, digest = _lazy_lua(tmp_path)
    assert cmd_nvim.trust_status(nv.trust_db, file).state == "untrusted"
    data = cmd_nvim.trust_file(nv.exe, file, env=env, cwd=file.parent)  # path form on 0.12+, buffer before
    assert data["ok"] is True
    status = cmd_nvim.trust_status(nv.trust_db, file)
    assert (status.state, status.sha256, status.path) == ("trusted", digest, real)
    assert not (file.parent / "nvim.log").exists() and not Path("nvim.log").exists()


# What older Neovims answer, on the Neovim at hand: before 0.10 vim.version() is a plain table
# (tostring gives "table: 0x..."), before 0.8 stdpath('state') is an error (E6100).
OLD_API = {
    "0.9.5": "vim.version = function() return { major = 0, minor = 9, patch = 5, api_level = 11, api_prerelease = false } end",
    "0.7.2": "vim.version = function() return { major = 0, minor = 7, patch = 2, api_level = 9, api_prerelease = false } end; "
    "local sp = vim.fn.stdpath; vim.fn.stdpath = function(what) if what == 'state' then error('E6100: state is not a valid stdpath') end return sp(what) end",
}


@pytest.mark.skipif(shutil.which("nvim") is None, reason="nvim not in PATH")
@pytest.mark.parametrize("old", sorted(OLD_API))
def test_query_reports_an_old_neovim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str) -> None:
    """An old distro Neovim on PATH (Ubuntu 24.04: 0.9.5, Debian 12: 0.7.2) must be reported as too
    old by nvim doctor and skipped by selftest --nvim, not break the query (exit 3)."""
    monkeypatch.setattr(cmd_nvim, "QUERY_LUA", "lua " + OLD_API[old] + "; " + cmd_nvim.QUERY_LUA.removeprefix("lua "))
    layout = nvimtest.Layout(tmp_path / "w")
    nv = cmd_nvim.query(shutil.which("nvim"), env=nvimtest.nvim_env(layout, dict(os.environ)))
    assert nv is not None and nv.version_text == old and nv.version < cmd_nvim.MIN_LAZYVIM
    nvimtest._check_isolated(nv, layout)
    if old == "0.7.2":
        assert nv.state == nv.data, "no state dir before 0.8: the data dir held what it holds now"


def test_an_nvim_that_cannot_run_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ./pyt doctor ended in an internal-error traceback, without its verdict, when the nvim
    # on PATH has its x bit but cannot be executed: another architecture, a truncated download
    fake = tmp_path / ("nvim.exe" if sys.platform == "win32" else "nvim")
    fake.write_bytes(b"\x7fELF\x02\x01\x01\x00garbage")
    fake.chmod(0o755)
    with pytest.raises(PytError, match="cannot run .*nvim") as e:
        cmd_nvim.headless(str(fake), cmd_nvim.QUERY_LUA)
    assert e.value.code == 3
    monkeypatch.setattr(cmd_nvim, "find_nvim", lambda: str(fake))
    lines: list[tuple[bool | None, str]] = []
    cmd_nvim.doctor(lambda passed, label, hint="": lines.append((passed, label)))
    assert len(lines) == 1 and lines[0][0] is None and "cannot run" in lines[0][1], lines  # a [--] note


def test_query_lua_needs_no_new_api() -> None:
    assert "tostring(vim.version())" not in cmd_nvim.QUERY_LUA and "pcall(vim.fn.stdpath, 'state')" in cmd_nvim.QUERY_LUA
    assert '"' not in cmd_nvim.QUERY_LUA, "the -c snippet crosses the Windows command line"


# --- timeouts kill the whole tree -----------------------------------------------------------------


def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, AttributeError):
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows uses taskkill /T")
def test_run_logged_timeout_kills_the_whole_tree(tmp_path: Path) -> None:
    """git, Mason, uv, debugpy... started by a hung Neovim must not outlive the timeout."""
    argv = ["sh", "-c", "sleep 30 & echo $! > pid; wait"]
    start = time.monotonic()
    code = nvimtest._run_logged(argv, cwd=tmp_path, env=dict(os.environ), log=tmp_path / "x.log", timeout=1)
    assert code is None and time.monotonic() - start < 20
    pid = int((tmp_path / "pid").read_text(encoding="ascii"))
    try:
        assert _gone(pid), f"grandchild {pid} survived the timeout"
    finally:
        _kill(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups; Windows uses taskkill /T")
def test_run_logged_ctrl_c_kills_the_whole_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In its own session the child no longer gets the terminal's Ctrl+C: the harness kills it."""
    real_wait = subprocess.Popen.wait
    calls: list[float | None] = []

    def wait(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        calls.append(timeout)
        if len(calls) == 1:
            deadline = time.monotonic() + 10
            while not (tmp_path / "pid").is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    with pytest.raises(KeyboardInterrupt):
        nvimtest._run_logged(["sh", "-c", "sleep 30 & echo $! > pid; wait"], cwd=tmp_path, env=dict(os.environ), log=tmp_path / "x.log", timeout=60)
    pid = int((tmp_path / "pid").read_text(encoding="ascii"))
    try:
        assert _gone(pid), f"grandchild {pid} survived Ctrl+C"
    finally:
        _kill(pid)


# --- ./pyt nvim sync: install only, never an update or a clean ---------------------------------------


def _nvim_in(tmp_path: Path) -> cmd_nvim.Nvim:
    nv = cmd_nvim.Nvim("nvim", (0, 12, 5), tmp_path / "c", tmp_path / "d", tmp_path / "s", tmp_path / "k")
    (tmp_path / "d" / "lazy" / "LazyVim").mkdir(parents=True)
    return nv


ALL_INSTALLED = '{"lazy": true, "missing": [], "failed": []}'


def _record_runs(monkeypatch: pytest.MonkeyPatch, report: str | None = ALL_INSTALLED) -> list[dict[str, object]]:
    """Fake Neovim: records the run and writes `report` where the plugin check would (None: nothing)."""
    runs: list[dict[str, object]] = []

    def run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        runs.append({"argv": list(argv), **kw})
        env = kw.get("env")
        if report is not None and isinstance(env, dict) and env.get("PT_NVIM_RESULT"):
            Path(env["PT_NVIM_RESULT"]).write_text(report, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cmd_nvim.subprocess, "run", run)
    return runs


def _trusted_nvim(tmp_path: Path) -> cmd_nvim.Nvim:
    nv = _nvim_in(tmp_path)
    nv.state.mkdir()
    lazy_lua = cmd_nvim.LAZY_LUA
    nv.trust_db.write_text(f"{hashlib.sha256(lazy_lua.read_bytes()).hexdigest()} {os.path.realpath(lazy_lua)}\n", encoding="utf-8")
    return nv


def test_nvim_sync_installs_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nv = _trusted_nvim(tmp_path)
    runs = _record_runs(monkeypatch)
    assert cmd_nvim.cmd_sync(nv) == 0
    assert len(runs) == 1
    argv = [str(a) for a in runs[0]["argv"]]  # type: ignore[attr-defined]
    assert "+Lazy! install" in argv and not [a for a in argv if any(w in a for w in ("sync", "update", "clean", "restore"))]
    assert argv.index("+Lazy! install") < argv.index("+lua dofile(vim.env.PT_NVIM_CHECK)") < argv.index("+qa")
    assert not [a for a in argv if '"' in a], "the arguments cross the Windows command line"
    assert runs[0]["cwd"] == cmd_nvim.ROOT
    env = runs[0]["env"]
    assert isinstance(env, dict) and env.get("NVIM_LOG_FILE"), "without NVIM_LOG_FILE Neovim may drop nvim.log in the project"


@pytest.mark.parametrize(
    ("report", "code", "message"),
    [
        ('{"lazy": true, "missing": ["overseer.nvim", "neotest"], "failed": []}', 1, "could not install: neotest, overseer.nvim"),
        ('{"lazy": false, "missing": [], "failed": []}', 3, "lazy.nvim did not start"),
        (None, 1, "did not say which plugins are installed"),
        ("not json", 1, "did not say which plugins are installed"),
    ],
)
def test_nvim_sync_fails_when_a_plugin_is_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: str | None, code: int, message: str) -> None:
    """Headless Neovim exits 0 after a failed clone ("Too many rounds of missing plugins") or
    without lazy.nvim at all (E492: Not an editor command: Lazy! install): the check after
    `Lazy! install` decides, never the exit code alone."""
    nv = _trusted_nvim(tmp_path)
    _record_runs(monkeypatch, report)
    with pytest.raises(PytError, match=re.escape(message)) as e:
        cmd_nvim.cmd_sync(nv)
    assert e.value.code == code


def test_nvim_sync_warns_about_plugins_with_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    nv = _trusted_nvim(tmp_path)
    _record_runs(monkeypatch, '{"lazy": true, "missing": {}, "failed": ["nvim-treesitter"]}')
    assert cmd_nvim.cmd_sync(nv) == 0
    err = capsys.readouterr().err
    assert "warning: lazy.nvim reported errors for: nvim-treesitter" in err and "plugins installed" in err


FAKE_LAZY = r"""
-- a stand-in for lazy.nvim: its plugin table, has_errors() and the :Lazy command
package.preload["lazy.core.config"] = function()
  return { plugins = {
    good = { _ = { installed = true } },
    flaky = { _ = { installed = true } },
    PT_BROKEN = { _ = { installed = false } },
  } }
end
package.preload["lazy.core.plugin"] = function()
  return { has_errors = function(p) return p == require("lazy.core.config").plugins.flaky end }
end
if vim.env.PT_WITH_LAZY == "1" then
  -- an Ex command, not nvim_create_user_command: that API came with 0.7, and an older Neovim
  -- (Ubuntu 22.04 ships 0.6.1) must run this test like any other
  vim.cmd("command! -bang -nargs=* Lazy :")
end
"""


@pytest.mark.skipif(shutil.which("nvim") is None, reason="nvim not in PATH")
@pytest.mark.parametrize("case", ["broken", "all installed", "no lazy.nvim"])
def test_real_nvim_sync_checks_the_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str) -> None:
    """The plugin check runs in a real Neovim after `Lazy! install` (a fake lazy.nvim config)."""
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("NVIM_APPNAME", raising=False)
    monkeypatch.setenv("PT_WITH_LAZY", "0" if case == "no lazy.nvim" else "1")
    init = tmp_path / "xdg_config_home" / "nvim" / "init.lua"
    init.parent.mkdir(parents=True)
    init.write_text(FAKE_LAZY.replace('PT_BROKEN = { _ = { installed = false } },', "" if case == "all installed" else "broken = {},"), encoding="utf-8")
    exe = shutil.which("nvim")
    assert exe
    nv = cmd_nvim.Nvim(exe, (0, 12, 5), init.parent, tmp_path / "d", tmp_path / "s", tmp_path / "k")
    (nv.data / "lazy" / "LazyVim").mkdir(parents=True)
    monkeypatch.setattr(cmd_nvim, "trust_status", lambda db, f: cmd_nvim.Trust("trusted", str(f), "x", "x"))
    if case == "all installed":
        assert cmd_nvim.cmd_sync(nv) == 0
        assert "lazy.nvim reported errors for: flaky" in capsys.readouterr().err
        return
    with pytest.raises(PytError) as e:
        cmd_nvim.cmd_sync(nv)
    if case == "broken":
        assert e.value.code == 1 and "could not install: broken" in str(e.value) and "good" not in str(e.value)
    else:
        assert e.value.code == 3 and "lazy.nvim did not start" in str(e.value)


@pytest.mark.parametrize("state", ["untrusted", "changed", "denied"])
def test_nvim_sync_refuses_an_untrusted_lazy_lua(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    """Untrusted, Neovim would ask (it never returns headless); from another folder the old code
    cleaned the project's plugins out of the user's config."""
    nv = _nvim_in(tmp_path)
    nv.state.mkdir()
    real = os.path.realpath(cmd_nvim.LAZY_LUA)
    nv.trust_db.write_text({"untrusted": "", "changed": f"{'0' * 64} {real}\n", "denied": f"! {real}\n"}[state], encoding="utf-8")
    assert cmd_nvim.trust_status(nv.trust_db, cmd_nvim.LAZY_LUA).state == state
    runs = _record_runs(monkeypatch)
    with pytest.raises(PytError, match="nvim trust") as e:
        cmd_nvim.cmd_sync(nv)
    assert e.value.code == 3 and runs == []


def test_nvim_sync_needs_lazyvim(tmp_path: Path) -> None:
    nv = cmd_nvim.Nvim("nvim", (0, 12, 5), tmp_path / "c", tmp_path / "d", tmp_path / "s", tmp_path / "k")
    with pytest.raises(PytError, match="bootstrap") as e:
        cmd_nvim.cmd_sync(nv)
    assert e.value.code == 3


# --- selftest --nvim: pinned starter and plugins ----------------------------------------------------


LOCKED = {
    "LazyVim": {"branch": "main", "commit": "a" * 40},
    "nvim-treesitter": {"branch": "main", "commit": "c" * 40},  # a plugin LazyVim's own specs name
    "overseer.nvim": {"branch": "master", "commit": "b" * 40},  # a plugin only .lazy.lua's extras add
}
NEWEST = "f" * 40  # what upstream has now


class Base:
    """prepare_base with every external step faked: records argv, mimics git and lazy.nvim.

    The lazy.nvim fake follows what lazy/core/loader.lua and lazy/manage/lock.lua do: on a fresh
    config the startup install runs in rounds, LazyVim first (at its locked commit); then lazy
    rewrites the lock, on disk and in memory, with the plugins its spec named so far, so the
    plugins LazyVim's specs add come at their newest commits. A restore moves the installed
    plugins to the commits of the lock in memory; a project run adds the extras' plugins."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock: bool, version: tuple[int, int, int] = (0, 12, 5)) -> None:
        self.layout = nvimtest.Layout(tmp_path / "w")
        self.layout.logs.mkdir(parents=True)
        self.lock = tmp_path / "lazy-lock.json"
        if lock:
            self.lock.write_text(json.dumps(LOCKED, indent=2), encoding="utf-8")
        monkeypatch.setattr(nvimtest, "LOCK", self.lock)
        home = self.layout.home
        self.nv = cmd_nvim.Nvim("nvim", version, home("XDG_CONFIG_HOME") / "nvim", home("XDG_DATA_HOME") / "nvim", home("XDG_STATE_HOME") / "nvim", home("XDG_CACHE_HOME") / "nvim")
        monkeypatch.setattr(cmd_nvim, "query", lambda exe=None, env=None: self.nv)
        self.steps: list[list[str]] = []
        self.lock_at_lazy: list[dict[str, object]] = []
        self.head = cmd_nvim.STARTER_REV if lock else "0123456789abcdef0123456789abcdef01234567"
        self.installed: dict[str, str] = {}  # plugin -> the commit its folder holds
        self.after: list[dict[str, str]] = []  # `installed` after each Neovim run
        self.stuck: set[str] = set()  # plugins that stay at the newest commit (a checkout that fails)
        monkeypatch.setattr(nvimtest, "_step", self.step)

    def step(self, argv: list[str | Path], **kwargs: object) -> None:
        args = [str(a) for a in argv]
        self.steps.append(args)
        if "clone" in args:
            (Path(args[-1]) / ".git").mkdir(parents=True)
        if "rev-parse" in args:
            # git's answer, only while the clone still has its .git
            assert (self.nv.config / ".git").is_dir(), "the starter commit is read before .git goes"
            log = kwargs["log"]
            assert isinstance(log, Path)
            log.write_text(f"{self.head}\n", encoding="ascii")
        action = next((a[len("+Lazy! ") :] for a in args if a.startswith("+Lazy! ")), None)
        if action:
            cwd = kwargs["cwd"]
            assert isinstance(cwd, Path)
            self.lazy(action, project=cwd != self.layout.base)

    def lazy(self, action: str, project: bool) -> None:
        path = self.nv.config / "lazy-lock.json"
        lock: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        self.lock_at_lazy.append(dict(lock))
        spec = ["LazyVim", "nvim-treesitter", *(["overseer.nvim"] if project else [])]

        def put(name: str, commit: str | None) -> None:
            self.installed[name] = NEWEST if name in self.stuck else (commit or NEWEST)

        if "LazyVim" not in self.installed:  # first round: only LazyVim is in the spec yet
            put("LazyVim", lock.get("LazyVim", {}).get("commit"))
            lock = {k: v for k, v in lock.items() if k == "LazyVim"}
        for name in spec:  # the startup install of the missing plugins, with the lock in memory
            if name not in self.installed:
                put(name, lock.get(name, {}).get("commit"))
        if action == "restore":
            for name in spec:
                if name in lock and name not in self.stuck:
                    self.installed[name] = lock[name]["commit"]
        elif action in ("sync", "update"):
            for name in spec:
                self.installed[name] = NEWEST
        self.after.append(dict(self.installed))
        (self.nv.data / "lazy" / "LazyVim").mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({n: {"branch": "main", "commit": self.installed[n]} for n in spec}), encoding="utf-8")

    def run(self, fresh: bool = False) -> float | None:
        nv, seconds = nvimtest.prepare_base(self.layout, "nvim", {}, fresh=fresh)
        assert nv is self.nv
        return seconds


def test_prepare_base_pins_the_starter_and_restores_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = Base(tmp_path, monkeypatch, lock=True)
    assert base.run() is not None
    clone = next(s for s in base.steps if "clone" in s)
    assert "--depth" not in clone, "a pinned commit needs history"
    assert ["checkout", cmd_nvim.STARTER_REV] == [s for s in base.steps if "checkout" in s][0][-2:]
    lazy = [a for s in base.steps for a in s if a.startswith("+Lazy! ")]
    assert lazy == ["+Lazy! install", "+Lazy! restore"], "the pinned base never syncs to the newest"
    assert base.lock_at_lazy == [LOCKED, LOCKED], "the pinned lock goes back before each run"
    # the first run's second round took LazyVim's plugins at their newest commits: only a run
    # that starts with everything installed restores them
    assert base.after[0] == {"LazyVim": "a" * 40, "nvim-treesitter": NEWEST}
    assert base.installed == {"LazyVim": "a" * 40, "nvim-treesitter": "c" * 40}
    marker = json.loads(base.layout.marker.read_text(encoding="utf-8"))
    assert marker["rev"] == cmd_nvim.STARTER_REV and marker["lock"] == hashlib.sha256(base.lock.read_bytes()).hexdigest()
    assert marker["nvim"] == "0.12.5" and not (base.nv.config / ".git").exists()
    assert marker["commit"] == cmd_nvim.STARTER_REV, "the commit the checkout really holds"
    base.steps.clear()
    assert base.run() is None and base.steps == [], "same pins, same Neovim: reused"


def test_prepare_base_without_the_lock_takes_the_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = Base(tmp_path, monkeypatch, lock=False)
    base.run()
    clone = next(s for s in base.steps if "clone" in s)
    assert "--depth" in clone and not [s for s in base.steps if "checkout" in s]
    assert [a for s in base.steps for a in s if a.startswith("+Lazy! ")] == ["+Lazy! sync"]
    assert set(base.installed.values()) == {NEWEST}
    marker = json.loads(base.layout.marker.read_text(encoding="utf-8"))
    assert marker["rev"] == "HEAD"
    assert marker["commit"] == base.head, "the newest starter commit is recorded: the next STARTER_REV"
    order = [next(i for i, s in enumerate(base.steps) if word in s) for word in ("clone", "rev-parse")]
    assert order == sorted(order) and order[1] < next(i for i, s in enumerate(base.steps) if "+Lazy! sync" in s)


def test_prepare_base_without_the_lock_is_never_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin refresh (no lock) must take the latest of everything: a base from an earlier
    run without the lock holds the commits of that day, and record_pins would report them as
    the new pins."""
    base = Base(tmp_path, monkeypatch, lock=False)
    assert base.run() is not None
    old = base.head
    base.head = "fedcba9876543210fedcba9876543210fedcba98"  # upstream moved on since
    base.steps.clear()
    assert base.run() is not None, "an unpinned base is installed again"
    assert any("clone" in s for s in base.steps) and [a for s in base.steps for a in s if a.startswith("+Lazy! ")] == ["+Lazy! sync"]
    marker = json.loads(base.layout.marker.read_text(encoding="utf-8"))
    assert marker["commit"] == base.head != old
    assert base.head in nvimtest.record_pins(base.layout, base.nv)
    assert (base.layout.logs / "starter-commit.txt").read_text(encoding="ascii") == base.head + "\n"


def test_prepare_base_fails_when_a_pin_does_not_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin left off its locked commit would make the pinned run test something else."""
    base = Base(tmp_path, monkeypatch, lock=True)
    base.stuck = {"nvim-treesitter"}
    with pytest.raises(PytError, match=r"nvim-treesitter at f{12}, pinned c{12}") as e:
        base.run()
    assert "base-restore.log" in str(e.value)
    assert e.value.code == 1, "a FAIL of the suite, not a usage error"
    assert not base.layout.marker.is_file(), "a base that missed its pins is never reused"


def test_prepare_base_fails_when_lazyvim_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = Base(tmp_path, monkeypatch, lock=True)
    real = base.lazy

    def lazy(action: str, project: bool) -> None:
        real(action, project)
        cmd_nvim.remove_tree(base.nv.data / "lazy" / "LazyVim")  # Lazy! exited 0 but installed nothing

    monkeypatch.setattr(base, "lazy", lazy)
    with pytest.raises(PytError, match="LazyVim was not installed") as e:
        base.run()
    assert e.value.code == 1 and not base.layout.marker.is_file()


@pytest.mark.parametrize(
    ("resolved", "expected"),
    [
        ({"LazyVim": {"branch": "main", "commit": "a" * 40}}, []),  # a subset: fine
        ({**LOCKED, "extra.nvim": {"branch": "main", "commit": "e" * 40}}, []),  # not in the lock: not compared
        ({"LazyVim": {"branch": "main", "commit": "d" * 40}}, ["LazyVim at dddddddddddd, pinned aaaaaaaaaaaa"]),
        ({"LazyVim": {"branch": "main"}, "overseer.nvim": "x"}, ["LazyVim at ?, pinned aaaaaaaaaaaa", "overseer.nvim at ?, pinned bbbbbbbbbbbb"]),
    ],
)
def test_lock_drift(tmp_path: Path, resolved: dict[str, object], expected: list[str]) -> None:
    lock, got = tmp_path / "lock.json", tmp_path / "resolved.json"
    lock.write_text(json.dumps(LOCKED), encoding="utf-8")
    got.write_text(json.dumps(resolved), encoding="utf-8")
    assert nvimtest.lock_drift(lock, got) == expected


def test_lock_drift_on_unreadable_files(tmp_path: Path) -> None:
    lock, got = tmp_path / "lock.json", tmp_path / "resolved.json"
    lock.write_text(json.dumps(LOCKED), encoding="utf-8")
    assert nvimtest.lock_drift(lock, got) and "cannot compare" in nvimtest.lock_drift(lock, got)[0]  # missing
    got.write_text("{not json", encoding="utf-8")
    assert "cannot compare" in nvimtest.lock_drift(lock, got)[0]
    got.write_text("[1, 2]", encoding="utf-8")
    assert nvimtest.lock_drift(lock, got) == [f"{got} is not a lazy-lock.json"]


@pytest.mark.parametrize("pinned", [True, False])
def test_record_pins_keeps_what_the_run_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: bool) -> None:
    """The logs (CI's artifact) get the resolved lazy-lock.json and the starter commit: after a
    green run without the lock they are the new pins."""
    base = Base(tmp_path, monkeypatch, lock=pinned)
    base.run()
    (base.nv.config / "lazy-lock.json").write_text('{"LazyVim": {"branch": "main", "commit": "x"}}\n', encoding="utf-8")
    text = nvimtest.record_pins(base.layout, base.nv)
    logs = base.layout.logs
    assert (logs / "lazy-lock.json").read_text(encoding="utf-8") == (base.nv.config / "lazy-lock.json").read_text(encoding="utf-8")
    assert (logs / "starter-commit.txt").read_text(encoding="ascii") == base.head + "\n"
    if pinned:
        assert text.startswith(f"starter {cmd_nvim.STARTER_REV[:12]}, plugins of "), text
    else:
        assert text.startswith("the latest (no ") and base.head in text, text
    # a base made before the commit was recorded (or a hand-edited base.json): no guessing
    marker = json.loads(base.layout.marker.read_text(encoding="utf-8"))
    marker["commit"] = "HEAD; rm -rf /"
    base.layout.marker.write_text(json.dumps(marker), encoding="utf-8")
    (logs / "starter-commit.txt").unlink()
    text = nvimtest.record_pins(base.layout, base.nv)
    assert not (logs / "starter-commit.txt").exists() and "rm -rf" not in text


@pytest.mark.parametrize("change", ["nvim", "lock"])
def test_prepare_base_is_rebuilt_when_neovim_or_the_pins_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    base = Base(tmp_path, monkeypatch, lock=True)
    base.run()
    if change == "nvim":
        base.nv = cmd_nvim.Nvim("nvim", (0, 13, 0), base.nv.config, base.nv.data, base.nv.state, base.nv.cache)
    else:
        base.lock.write_text(json.dumps({**LOCKED, "neotest": {"commit": "c" * 40}}), encoding="utf-8")
    base.steps.clear()
    assert base.run() is not None and any("clone" in s for s in base.steps), f"a {change} change must reinstall the base"
    base.layout.marker.write_text("{not json", encoding="utf-8")
    base.steps.clear()
    assert base.run() is not None, "an unreadable base.json counts as a different base"


def test_the_shipped_pins_are_complete() -> None:
    """The lock pins LazyVim, lazy.nvim, every plugin .lazy.lua configures and the ones its extras
    bring (neotest-python): an unpinned one would be installed at its newest commit."""
    assert re.fullmatch(r"[0-9a-f]{40}", cmd_nvim.STARTER_REV)
    if not nvimtest.LOCK.is_file():
        pytest.skip("no pinned lazy-lock.json (a run without it takes the latest of everything)")
    lock = json.loads(nvimtest.LOCK.read_text(encoding="utf-8"))
    assert isinstance(lock, dict)
    configured = re.findall(r'\{ "[\w.-]+/([\w.-]+)", optional = true', (cmd_nvim.ROOT / ".pytemplate" / "templates" / "nvim" / "lazy.lua").read_text(encoding="utf-8"))
    assert len(configured) >= 8, configured
    for name in ("LazyVim", "lazy.nvim", "neotest-python", *configured):
        entry = lock.get(name)
        assert isinstance(entry, dict) and re.fullmatch(r"[0-9a-f]{40}", str(entry.get("commit", ""))), name
    assert all(isinstance(v, dict) and set(v) == {"branch", "commit"} for v in lock.values()), "lazy.nvim's own lock format"
    assert nvimtest.LOCK.read_bytes().isascii()


def _preset_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stuck: set[str]) -> tuple[Base, nvimtest.Row]:
    """run_preset after a pinned base, with ./pyt, the trust and the smoke run faked."""
    base = Base(tmp_path, monkeypatch, lock=True)
    base.run()
    base.steps.clear()
    base.lock_at_lazy.clear()
    base.stuck = stuck
    monkeypatch.setattr(cmd_nvim, "trust_file", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(cmd_nvim, "trust_status", lambda db, f: cmd_nvim.Trust("trusted", str(f), "x", "x"))
    monkeypatch.setattr(proc, "find_uv", lambda: "uv")
    smoke_out = "\nok   a\n\nok   mypy diagnostics (profile strict)\n\nDONE 2\n"

    def run_smoke(nv: cmd_nvim.Nvim, proj: Path, env: dict[str, str], log: Path, err_log: Path, timeout: float) -> int:
        assert env["PT_ROOT"] == str(proj)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(smoke_out, encoding="utf-8")
        err_log.write_text("", encoding="utf-8")
        return 0

    monkeypatch.setattr(nvimtest, "run_smoke", run_smoke)
    return base, nvimtest.run_preset("script", base.layout, base.nv, renv={}, venv={}, timeout=60)


def test_run_preset_uses_a_typing_profile_and_the_locked_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, row = _preset_run(tmp_path, monkeypatch, stuck=set())
    assert row.ok, row
    launch = [s for s in base.steps if s[:3] == ["uv", "run", "--quiet"] and s[3].startswith("--python=") and s[4:7] == ["--python-preference", "managed", "--script"]]
    runs = [s[8:] for s in launch]
    assert [d[0] for d in runs] == ["new", "sync", "mode"], runs
    assert runs[-1] == ["mode", "--typing", nvimtest.SMOKE_TYPING], runs
    assert [a for s in base.steps for a in s if a.startswith("+Lazy! ")] == ["+Lazy! install"]
    assert base.lock_at_lazy == [LOCKED], "the lock (pruned by the base run) goes back before Lazy! install"
    assert base.installed["overseer.nvim"] == "b" * 40, "the extras' plugins come at their locked commits"


def test_run_preset_fails_when_the_extras_miss_their_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, row = _preset_run(tmp_path, monkeypatch, stuck={"overseer.nvim"})
    assert not row.ok and row.smoke is None, row
    assert "overseer.nvim at ffffffffffff, pinned bbbbbbbbbbbb" in row.error and "lazy-install.log" in row.error


# --- nvim doctor ------------------------------------------------------------------------------------

EVERY_TOOL = {name: f"/usr/bin/{name}" for name in ("git", "curl", "tar", "rg", "fd", "tree-sitter", "python3", "python", "node")}


def _doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    tools: dict[str, str] = EVERY_TOOL,
    cc: str | None = "/usr/bin/gcc",
    lazyvim_json: str | None = None,
    lazy_lua: str = STARTER_LAZY_LUA,
) -> tuple[int, str]:
    """nvim doctor with Neovim, the tools, the trust and the project's .venv faked."""
    from types import SimpleNamespace

    from runner import config
    from runner.config import Config

    nv = cmd_nvim.Nvim("nvim", (0, 12, 5), tmp_path / "c", tmp_path / "d", tmp_path / "s", tmp_path / "k")
    (nv.config / "lua" / "config").mkdir(parents=True)
    (nv.config / "lua" / "config" / "lazy.lua").write_text(lazy_lua, encoding="utf-8")
    if lazyvim_json is None:
        lazyvim_json = json.dumps({"extras": list(cmd_nvim.EXTRAS), "version": 8})
    if lazyvim_json:
        nv.lazyvim_json.write_text(lazyvim_json, encoding="utf-8")
    monkeypatch.setattr(cmd_nvim, "find_nvim", lambda: "nvim")
    monkeypatch.setattr(cmd_nvim, "query", lambda exe=None, env=None: nv)
    monkeypatch.setattr(cmd_nvim, "which", lambda name: tools.get(name))
    monkeypatch.setattr(cmd_nvim, "c_compiler", lambda: cc)
    monkeypatch.setattr(cmd_nvim, "trust_status", lambda db, f: cmd_nvim.Trust("trusted", str(f), "x", "x"))
    monkeypatch.setattr(cmd_nvim.proc, "find_uv", lambda: "/opt/uv/bin/uv")
    monkeypatch.setattr(cmd_nvim.envs, "tool_env", lambda cfg: SimpleNamespace(dir=tmp_path))
    monkeypatch.setattr(cmd_nvim, "_venv_exe", lambda env_dir, name: Path(__file__))
    monkeypatch.setattr(cmd_nvim, "_has_package", lambda env_dir, package: True)
    cfg: Config = config._build(Config, {}, "")
    code = cmd_nvim.cmd_doctor(cfg)
    return code, capsys.readouterr().err


def test_nvim_doctor_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _doctor(tmp_path, monkeypatch, capsys)
    assert code == 0 and "Neovim integration ready" in out, out
    # the plugin runs ./pyt (and basedpyright: uv tool run) with the uv it finds itself, never uvx
    assert "[ok] uv: /opt/uv/bin/uv" in out and "uvx" not in out, out


def test_nvim_doctor_needs_fd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """venv-selector (LazyVim's lang.python extra, which .lazy.lua imports) raises an error on the
    first Python buffer of every session without fd: not an optional "faster pickers" tool."""
    tools = {k: v for k, v in EVERY_TOOL.items() if k != "fd"}
    code, out = _doctor(tmp_path, monkeypatch, capsys, tools=tools)
    assert code == 1 and "[XX] fd not found" in out and "venv-selector" in out, out
    assert re.search(r"install.*fd", out), "an install hint"
    code, out = _doctor(tmp_path / "debian", monkeypatch, capsys, tools={**tools, "fdfind": "/usr/bin/fdfind"})
    assert code == 0 and "[ok] fd: /usr/bin/fdfind" in out, out


@pytest.mark.parametrize(("os_name", "windows", "macos"), [("windows", True, False), ("macos", False, True), ("linux", False, False)])
def test_nvim_doctor_says_how_to_install_every_required_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], os_name: str, windows: bool, macos: bool
) -> None:
    """The manual promises an install command for every required tool: git, curl and tar had
    an empty hint (only fd and the C compiler had one), on every OS."""
    assert [names[0] for names, required, _why, _hint in cmd_nvim.TOOLS if required] == ["git", "curl", "tar", "fd"]
    monkeypatch.setattr(cmd_nvim, "IS_WINDOWS", windows)
    monkeypatch.setattr(cmd_nvim, "IS_MACOS", macos)
    command = {"windows": "winget", "macos": "brew", "linux": "apt"}[os_name]
    code, out = _doctor(tmp_path, monkeypatch, capsys, tools={}, cc=None)
    lines = out.splitlines()
    assert code == 1
    for label in ("git not found", "curl not found", "tar not found", "fd not found", "no C compiler"):
        at = next(i for i, line in enumerate(lines) if label in line)
        assert "install it: " in lines[at + 1] and len(lines[at + 1].split("install it: ", 1)[1]) > 5, (label, lines[at + 1])
    assert command in "\n".join(line for line in lines if "install it: " in line), out


def test_nvim_doctor_needs_a_c_compiler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """LazyVim lists a C compiler among its requirements: nvim-treesitter builds its parsers."""
    code, out = _doctor(tmp_path, monkeypatch, capsys, cc=None)
    assert code == 1 and "[XX] no C compiler" in out and "nvim-treesitter" in out, out


def test_nvim_doctor_tells_an_invalid_lazyvim_json_from_a_missing_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _doctor(tmp_path, monkeypatch, capsys, lazyvim_json='{ "extras": [ "a", ] }')
    assert "cannot read it as JSON" in out and "not found" not in out, out
    assert code == 0, "a note: LazyVim ignores it, .lazy.lua imports the extras anyway"
    code, out = _doctor(tmp_path / "fresh", monkeypatch, capsys, lazyvim_json="")
    assert "lazyvim.json not found" in out and code == 0, out
    with pytest.raises(PytError, match="cannot read it as JSON"):
        cmd_nvim.missing_extras(tmp_path / "c" / "lazyvim.json")


def test_extras_entries_that_are_no_module_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # A hand-written {...} or [...] entry in lazyvim.json's extras made set() raise TypeError:
    # nvim doctor ended in an internal-error traceback before its tools and project sections
    path = tmp_path / "lazyvim.json"
    mine = [cmd_nvim.EXTRAS[0], {"import": "x"}, ["y"]]
    path.write_text(json.dumps({"extras": mine, "version": 8}), encoding="utf-8")
    assert cmd_nvim.missing_extras(path) == list(cmd_nvim.EXTRAS[1:])
    added, _ = cmd_nvim.enable_extras(path, stamp="s")
    assert added == list(cmd_nvim.EXTRAS[1:])
    assert json.loads(path.read_text(encoding="utf-8"))["extras"][:3] == mine  # the user's entries stay
    code, out = _doctor(tmp_path / "doctor", monkeypatch, capsys, lazyvim_json=json.dumps({"extras": ["a", {"x": 1}]}))
    assert code == 0 and "extras not enabled in lazyvim.json" in out and "Neovim integration ready" in out, out


def test_c_compiler_skips_the_macos_shims_without_developer_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from runner import cmd_env

    paths = {"gcc": "/usr/bin/gcc", "cc": "/usr/bin/cc", "clang": "/opt/homebrew/opt/llvm/bin/clang"}
    monkeypatch.delenv("CC", raising=False)
    monkeypatch.setattr(cmd_nvim, "which", lambda name: paths.get(name))
    monkeypatch.setattr(cmd_nvim, "IS_WINDOWS", False)
    monkeypatch.setattr(cmd_nvim, "IS_MACOS", True)
    monkeypatch.setattr(cmd_env, "_xcode_problem", lambda: "no developer tools (xcode-select -p fails)")
    assert cmd_nvim.c_compiler() == "/opt/homebrew/opt/llvm/bin/clang"  # the shims are skipped
    del paths["clang"]
    assert cmd_nvim.c_compiler() is None
    monkeypatch.setattr(cmd_env, "_xcode_problem", lambda: None)  # developer tools installed
    assert cmd_nvim.c_compiler() == "/usr/bin/gcc"
    monkeypatch.setattr(cmd_nvim, "IS_MACOS", False)  # Linux: /usr/bin is a real compiler
    monkeypatch.setattr(cmd_env, "_xcode_problem", lambda: "never asked")
    assert cmd_nvim.c_compiler() == "/usr/bin/gcc"
