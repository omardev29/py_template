"""Tests for runner/shells.py: quoting per shell family, PTPROBE parsing, shell discovery, the
shell-setup snippets, the launcher checks of doctor, the report and two quick real probes.

The full shell x test matrix is `./pyt selftest --shells`, not pytest.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config, shells  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import IS_WINDOWS, ROOT  # noqa: E402
from runner.ui import PytError  # noqa: E402

ALL_ARGS = shells.BASE_ARGS + shells.EXTRA_ARGS


def make(data: dict[str, object]) -> Config:
    cfg: Config = config._build(Config, data, "")
    return cfg


def posix_sh() -> list[str] | None:
    """A POSIX sh to run snippets in (Git's or MSYS2's on Windows), or None."""
    if not IS_WINDOWS:
        return ["/bin/sh"] if Path("/bin/sh").is_file() else None
    for sh in shells.discover(distros=lambda wsl: []):
        if sh.name in ("git-sh", "msys2-dash", "git-dash"):
            return [sh.argv[0]]
    return None


# --- quoting ---------------------------------------------------------------------------------------


def test_sh_quote() -> None:
    assert shells.sh_quote("") == "''"
    assert shells.sh_quote("a'b") == "'a'\\''b'"
    assert shells.sh_quote("$HOME *") == "'$HOME *'"


def test_cmd_quote() -> None:
    assert shells.cmd_quote("plain") == "plain"
    assert shells.cmd_quote("") == '""'
    assert shells.cmd_quote("with space") == '"with space"'
    assert shells.cmd_quote("tail\\") == "tail\\"
    assert shells.cmd_quote("sp tr\\") == '"sp tr\\\\"'
    assert shells.cmd_quote("a&b") == '"a&b"'
    for bad in ("100%", "bang!", 'q"x', "a^b"):
        with pytest.raises(ValueError):
            shells.cmd_quote(bad)
    assert all(shells.cmd_quote(a) for a in shells.BASE_ARGS)


def test_powershell_quote_and_encoding() -> None:
    assert shells.ps_quote("a'b") == "'a''b'"
    assert shells.ps_quote("") == "''"
    code = "& './pyt' 'x'\nexit $LASTEXITCODE\n"
    assert base64.b64decode(shells.ps_encoded(code)).decode("utf-16-le") == code


def test_fish_and_nu_quote() -> None:
    assert shells.fish_quote("a'b\\c") == "'a\\'b\\\\c'"
    assert shells.nu_quote("a'#b") == "r##'a'#b'##"
    assert shells.nu_quote("plain") == "r#'plain'#"


def test_argsets() -> None:
    cmd = shells.Shell("cmd", "cmd", ("cmd.exe", "/d", "/s", "/c"))
    posix = shells.Shell("sh", "posix", ("/bin/sh",))
    assert shells.argset(cmd) == shells.BASE_ARGS
    assert shells.argset(posix) == ALL_ARGS
    assert "" in shells.BASE_ARGS and "tail\\" in shells.BASE_ARGS and "--" in shells.EXTRA_ARGS


def test_command_text_per_family(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    posix = shells.Shell("git-dash", "posix", ("dash.exe",), interp=("C:/g/dash.exe",), mixed=True)
    assert shells.command_text(posix, project, "root", ["a b"]) == "'C:/g/dash.exe' ./pyt 'a b'"
    assert shells.command_text(posix, project, "sub", []).endswith("../pyt")
    assert shells.command_text(posix, project, "abs", []).endswith(shells.sh_quote(str(project / "pyt").replace("\\", "/")))
    assert shells.command_text(posix, project, "root", [], "/usr/bin:/bin").startswith("PATH=/usr/bin:/bin; export PATH; ")

    cmd = shells.Shell("cmd", "cmd", ("cmd.exe",))
    assert shells.command_text(cmd, project, "root", ["with space", ""]) == '.\\pyt "with space" ""'
    assert shells.command_text(cmd, project, "abs", []) == f'"{project / "pyt"}"'

    ps = shells.Shell("pwsh", "powershell", ("pwsh",))
    text = shells.command_text(ps, project, "root", ["--", "a'b"])
    word = "./pyt" if IS_WINDOWS else "./pyt.ps1"  # on Windows ./pyt resolves to pyt.ps1
    assert f"& '{word}' '--' 'a''b'" in text and text.rstrip().endswith("exit $LASTEXITCODE")
    assert shells.ps_quote(str(project / "pyt.ps1")) in shells.command_text(ps, project, "abs", [])

    xonsh = shells.Shell("xonsh", "xonsh", ("xonsh", "--no-rc"))
    text = shells.command_text(xonsh, project, "root", ["\u00fcn", 'q"x'])
    assert "![./pyt @(['\\xfcn', 'q\"x'])]" in text and "except subprocess.CalledProcessError" in text
    assert "XONSH_SUBPROC" not in text and "RAISE" not in text  # no setting whose name xonsh changes
    assert text.isascii()

    wsl = shells.Shell("wsl-u", "wsl", ("wsl.exe", "-d", "U"))
    assert "$(wslpath -u " in shells.command_text(wsl, project, "abs", [])


@pytest.mark.skipif(IS_WINDOWS, reason="a POSIX stand-in launcher")
def test_xonsh_probe_exit_code_ignores_raise_settings(tmp_path: Path) -> None:
    """The probe returns the child's code with every raise-error setting of any xonsh turned on
    (0.24 raises CalledProcessError from a failing ![...]; 0.18 did not): reading the setting's
    name was what broke when xonsh renamed it."""
    xonsh = shutil.which("xonsh")
    if not xonsh:
        pytest.skip("xonsh not installed")
    project = tmp_path / "proj"
    project.mkdir()
    launcher = project / "pyt"
    launcher.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8", newline="\n")
    launcher.chmod(0o755)
    body = shells.command_text(shells.Shell("xonsh", "xonsh", (xonsh,)), project, "abs", [])
    # as if xonsh renamed its settings again: a `$NAME = ...` line of the probe would set nothing
    body = "".join(line for line in body.splitlines(keepends=True) if not line.startswith("$"))
    forced = "$XONSH_SUBPROC_CMD_RAISE_ERROR = True\n$XONSH_SUBPROC_RAISE_ERROR = True\n$RAISE_SUBPROC_ERROR = True\n"
    r = subprocess.run([xonsh, "--no-rc", "-c", forced + body], cwd=project, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 7, r.stdout + r.stderr


def test_invocation(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    posix = shells.Shell("sh", "posix", ("/bin/sh",))
    inv = shells.invocation(posix, project, "root", ["__probe", "0", "0", "back\\slash"], cwd=project, scripts=tmp_path, tag="t")
    assert inv.argv == ["/bin/sh", "-c", 'eval "$PTCMD"']  # never the command itself: Cygwin mangles argv
    assert inv.env["PTCMD"] == "./pyt '__probe' '0' '0' 'back\\slash'"

    script = shells.Shell("niubash-shx", "posix", ("niu",), mode="script", mixed=True)
    inv = shells.invocation(script, project, "sub", ["__probe"], cwd=project, scripts=tmp_path, tag="s")
    assert inv.script is not None and inv.argv == ["niu", str(inv.script)]
    assert inv.script.read_bytes() == b"../pyt '__probe'\n"

    cmd = shells.Shell("cmd", "cmd", ("C:\\Windows\\system32\\cmd.exe", "/d", "/s", "/c"))
    inv = shells.invocation(cmd, project, "root", ["__probe", "with space"], cwd=project, scripts=tmp_path, tag="c")
    assert inv.argv == 'C:\\Windows\\system32\\cmd.exe /d /s /c ".\\pyt __probe "with space""'

    wsl = shells.Shell("wsl-u", "wsl", ("wsl.exe", "-d", "U"))
    inv = shells.invocation(wsl, project, "root", ["x"], cwd=tmp_path, scripts=tmp_path, tag="w")
    assert inv.argv[:5] == ["wsl.exe", "-d", "U", "--cd", str(tmp_path)] and "PTCMD/u" in inv.env["WSLENV"]


def test_sh_quoting_round_trips_through_a_real_shell() -> None:
    sh = posix_sh()
    if sh is None:
        pytest.skip("no POSIX sh here")
    env = dict(os.environ, PTCMD="printf '<%s>\\n' " + " ".join(map(shells.sh_quote, ALL_ARGS)))
    r = subprocess.run([*sh, "-c", 'eval "$PTCMD"'], env=env, capture_output=True, timeout=60, check=True)
    assert r.stdout.decode("utf-8").splitlines() == [f"<{a}>" for a in ALL_ARGS]


# --- PTPROBE -----------------------------------------------------------------------------------------


def test_parse_probe() -> None:
    data = {"argv": ["a b", ""], "root": "C:\\x"}
    line = "PTPROBE" + json.dumps(data)
    assert shells.parse_probe(f"banner from a login profile\r\n{line}\r\nmore\n") == data
    assert shells.parse_probe("\x1b[0m" + line) == data
    assert shells.parse_probe("PTPROBE{not json\n" + line) == data
    assert shells.parse_probe("nothing here") is None
    assert shells.parse_probe('PTPROBE["a list"]') is None


def test_probe_prints_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert shells.probe(["37", "0", "a b", ""]) == 37
    data = shells.parse_probe(capsys.readouterr().out)
    assert data is not None and data["argv"] == ["a b", ""] and Path(str(data["root"])) == ROOT


# --- discovery ---------------------------------------------------------------------------------------


def _touch(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return str(path)


def test_discover_windows_with_fake_folders(tmp_path: Path) -> None:
    sysroot = tmp_path / "Windows"
    _touch(sysroot / "System32" / "cmd.exe")
    _touch(sysroot / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    msys = tmp_path / "msys64"
    for exe in ("bash.exe", "dash.exe", "msys-2.0.dll"):
        _touch(msys / "usr" / "bin" / exe)
    (msys / "ucrt64").mkdir()
    git = tmp_path / "scoop" / "apps" / "git" / "current"
    for exe in ("usr/bin/sh.exe", "usr/bin/bash.exe", "usr/bin/dash.exe", "bin/bash.exe", "cmd/git.exe"):
        _touch(git / exe)
    niu = _touch(tmp_path / "Local" / "Programs" / "Niubash" / "niu.exe")
    busybox = _touch(tmp_path / "bin" / "busybox.exe")
    env = {
        "SystemRoot": str(sysroot), "ComSpec": str(sysroot / "System32" / "cmd.exe"), "MSYS2_ROOT": str(msys),
        "USERPROFILE": str(tmp_path), "LOCALAPPDATA": str(tmp_path / "Local"), "PATH": "",
    }
    found = {"git": str(git / "cmd" / "git.exe"), "busybox": busybox, "wsl": "wsl.exe", "xonsh": "xonsh.exe"}
    got = shells.discover(env, windows=True, which=found.get, standard=False, distros=lambda wsl: ["Ubuntu-24.04"])
    names = [s.name for s in got]
    assert names == [
        "cmd", "powershell", "xonsh", "niubash", "niubash-shx", "git-bash", "git-sh", "git-dash",
        "msys2-msys", "msys2-ucrt64", "msys2-shx", "msys2-dash", "busybox", "wsl-ubuntu-24.04",
    ]
    by = {s.name: s for s in got}
    assert by["niubash"].argv == (niu,) and by["niubash-shx"].mode == "script"
    assert dict(by["msys2-ucrt64"].env) == {"MSYSTEM": "UCRT64", "CHERE_INVOKING": "1", "MSYS2_PATH_TYPE": "minimal"}
    assert by["msys2-ucrt64"].argv[1] == "--login" and by["msys2-shx"].mode == "script"
    assert dict(by["msys2-shx"].env)["MSYSTEM"] == "MINGW64"
    assert by["git-dash"].interp == (str(git / "usr" / "bin" / "dash.exe").replace("\\", "/"),)
    assert by["wsl-ubuntu-24.04"].argv == ("wsl.exe", "-d", "Ubuntu-24.04")
    assert all(s.mixed for s in got if s.family == "posix")


def test_a_second_install_keeps_its_family_name(tmp_path: Path) -> None:
    """Two MSYS2 roots and two Git installs: the second one's shells are msys2-2-*, git-2-*, so
    the family NAME (`selftest --shells msys2`, `git`) selects them too (msys22-* did not)."""
    msys_roots = [tmp_path / "msys64", tmp_path / "scoop" / "apps" / "msys2" / "current"]
    for root in msys_roots:
        for exe in ("bash.exe", "dash.exe", "msys-2.0.dll"):
            _touch(root / "usr" / "bin" / exe)
    git_roots = [tmp_path / "scoop" / "apps" / "git" / "current", tmp_path / "Git"]
    for root in git_roots:
        for exe in ("usr/bin/sh.exe", "usr/bin/bash.exe", "bin/bash.exe", "cmd/git.exe"):
            _touch(root / exe)
    for cyg in ("cyg1", "cyg2"):
        _touch(tmp_path / cyg / "bin" / "bash.exe")
    env = {"MSYS2_ROOT": str(msys_roots[0]), "USERPROFILE": str(tmp_path), "SystemRoot": str(tmp_path / "Windows"), "PATH": ""}
    found = {"git": str(git_roots[1] / "cmd" / "git.exe")}
    got = shells.discover(env, windows=True, which=found.get, standard=False, distros=lambda wsl: [])
    names = [s.name for s in got]
    assert {"msys2-msys", "msys2-shx", "msys2-2-msys", "msys2-2-shx", "git-bash", "git-2-bash", "git-2-sh"} <= set(names), names
    msys2 = [s.name for s in shells.select(got, ["msys2"])]
    assert "msys2-2-msys" in msys2 and "msys2-msys" in msys2
    assert [s.name for s in shells.select(got, ["git"])] == ["git-bash", "git-sh", "git-2-bash", "git-2-sh"]
    assert [s.name for s in shells.select(got, ["git-2"])] == ["git-2-bash", "git-2-sh"]
    # Cygwin: the root is named by CYGWIN_ROOT only (plus C:\cygwin64 and C:\cygwin as standard)
    cyg = shells.discover({**env, "CYGWIN_ROOT": str(tmp_path / "cyg1")}, windows=True, which=found.get, standard=False, distros=lambda wsl: [])
    assert "cygwin" in [s.name for s in cyg]


def test_discover_posix_with_fake_which(tmp_path: Path) -> None:
    found = {"bash": _touch(tmp_path / "bash"), "zsh": _touch(tmp_path / "zsh"), "busybox": _touch(tmp_path / "busybox")}
    got = {s.name: s for s in shells.discover({}, windows=False, which=found.get)}
    assert got["bash"].interp == (found["bash"],) and got["bash"].argv == (found["bash"], "--norc", "--noprofile")
    assert got["zsh"].argv[1] == "-f"
    assert got["busybox"].interp == (found["busybox"], "sh")
    assert "dash" not in got and "pwsh" not in got


def test_describe_names_every_word_the_launcher_runs_with(tmp_path: Path) -> None:
    """--list and the --json report said `busybox ./pyt` while the probes run `busybox sh ./pyt`."""
    found = {"busybox": _touch(tmp_path / "busybox"), "dash": _touch(tmp_path / "dash")}
    got = {s.name: s for s in shells.discover({}, windows=False, which=found.get)}
    assert "launcher run as `busybox sh ./pyt`" in got["busybox"].describe()
    assert "launcher run as `dash ./pyt`" in got["dash"].describe()


def test_select() -> None:
    found = [shells.Shell(n, "posix", ("x",)) for n in ("cmd", "msys2-msys", "msys2-ucrt64", "niubash", "niubash-shx")]
    assert [s.name for s in shells.select(found, ["msys2"])] == ["msys2-msys", "msys2-ucrt64"]
    assert [s.name for s in shells.select(found, ["niubash-shx", "cmd"])] == ["cmd", "niubash-shx"]
    assert len(shells.select(found, [])) == 5
    with pytest.raises(PytError, match="not found here: fish"):
        shells.select(found, ["fish"])


def test_child_env_drops_what_uv_run_added() -> None:
    env = shells.child_env({"UV": "x", "VIRTUAL_ENV": "y", "PYTEMPLATE_LAUNCHER": "cmd", "PWD": "C:/stale", "UV_RUN_RECURSION_DEPTH": "1", "KEEP": "1"})
    assert env == {"KEEP": "1"}


def test_uv_standard_dirs() -> None:
    win = shells.uv_standard_dirs({"USERPROFILE": "C:\\U", "LOCALAPPDATA": "C:\\U\\L", "SCOOP": "D:\\s"}, windows=True)
    assert Path("C:\\U") / ".local/bin" in win and Path("C:\\U\\L") / "Microsoft" / "WinGet" / "Links" in win
    assert Path("D:\\s") / "shims" in win
    posix = shells.uv_standard_dirs({"HOME": "/h"}, windows=False)
    assert Path("/h/.local/bin") in posix and Path("/opt/homebrew/bin") in posix


# --- shell-setup -------------------------------------------------------------------------------------


def test_snippets_are_ascii_and_say_where_to_paste(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The niubash and msys2 headers name real files (NIU_ENV, the MSYS2 home): keep them ASCII here.
    for key, value in (("NIU_ENV", "C:/Users/me/.niu_env"), ("USERNAME", "me"), ("USERPROFILE", str(tmp_path)), ("MSYS2_ROOT", ""), ("SCOOP", "")):
        monkeypatch.setenv(key, value)
    cfg = make({"tasks": {"gen": {"cmd": ["python", "gen.py"]}}})
    for shell in shells.SETUP_SHELLS:
        text = shells.snippet(shell, cfg)
        assert text.isascii(), shell
        assert "\r" not in text and text.endswith("\n")
        assert "pyt" in text and ("Paste" in text or "Save as" in text), shell
    with pytest.raises(PytError, match="unknown shell"):
        shells.snippet("tcsh")


def test_a_snippet_appended_to_an_rc_file_never_joins_its_last_line(tmp_path: Path) -> None:
    """`./pyt shell-setup bash >> ~/.bashrc` on an rc file without a final newline (VS Code and
    Notepad save them so): the snippet's first comment joined the user's last line, its backticks
    ran `pyt` and every new shell printed errors."""
    for shell in shells.SETUP_SHELLS:
        assert shells.snippet(shell).startswith("\n"), shell
    sh = posix_sh()
    if sh is None:
        pytest.skip("no POSIX sh here")
    rc = tmp_path / "rc"
    rc.write_bytes(b"PT_KEEP=kept" + shells.snippet("bash").encode("ascii"))
    r = subprocess.run([*sh, "-c", '. "$1" && printf "%s|" "$PT_KEEP" && command -v pyt', "sh", str(rc)], capture_output=True, text=True, timeout=60, check=False)
    assert (r.stdout, r.stderr) == ("kept|pyt\n", ""), (r.stdout, r.stderr)


def test_snippets_stay_ascii_with_non_ascii_user_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The niubash and msys2 headers name the user's files: a NIU_ENV or user name with an accent
    must not put non-ASCII bytes into the snippet (it is appended to rc files)."""
    root = tmp_path / "msys64"
    (root / "usr" / "bin").mkdir(parents=True)
    for name in ("bash.exe", "msys-2.0.dll"):
        (root / "usr" / "bin" / name).write_bytes(b"")
    user = "Jos\u00e9"
    for key, value in (("NIU_ENV", f"/home/{user}/niu.env"), ("USERNAME", user), ("MSYS2_ROOT", str(root)), ("SCOOP", "")):
        monkeypatch.setenv(key, value)
    for shell in ("niubash", "msys2"):
        text = shells.snippet(shell)
        assert text.isascii(), text
    assert "$NIU_ENV" in shells.snippet("niubash")
    monkeypatch.setenv("USERNAME", "me")  # an ASCII path is still named in full
    assert str(root / "home" / "me" / ".bashrc") in shells.snippet("msys2")


def test_xonsh_snippet_words_and_syntax() -> None:
    cfg = make({"tasks": {"gen": {"cmd": ["python", "gen.py"]}}})
    text = shells.snippet("xonsh", cfg)
    namespace: dict[str, object] = {}
    # Plain Python once the one xonsh-only expression (the environment) is stubbed out.
    exec(compile(text.split("\n\nif hasattr(aliases")[0].replace("${...}", "{}"), "snippet", "exec"), namespace)
    words = namespace["_PT_WORDS"]
    choices = namespace["_PT_CHOICES"]
    flags = namespace["_PT_FLAGS"]
    assert isinstance(words, list) and isinstance(choices, dict) and isinstance(flags, dict)
    assert {"selftest", "pyz-merge", "shell-setup", "gen", "--dry-run"} <= set(words)
    assert choices["sync"] == ["cpython", "pypy", "mypyc", "all"]
    assert "--shells" in choices["selftest"] and "niubash" in choices["shell-setup"]
    assert "--method" in flags["build"]
    compile(text.replace("${...}", "{}"), "snippet", "exec")
    assert "add_one_completer" in text and 'aliases["pyt"]' in text


def _xonsh_words(cfg: Config | None) -> list[object]:
    text = shells.snippet("xonsh", cfg)
    namespace: dict[str, object] = {}
    exec(compile(text.split("\n\nif hasattr(aliases")[0].replace("${...}", "{}"), "snippet", "exec"), namespace)
    words = namespace["_PT_WORDS"]
    assert isinstance(words, list)
    return words


def _xonsh_completer(monkeypatch: pytest.MonkeyPatch, cfg: Config | None = None) -> Any:
    """The snippet's completer, run as plain Python: xonsh's completer modules, `aliases` and
    ${...} are stubbed, the rest of the snippet is the real one."""
    import types

    registered: dict[str, Any] = {}
    completer_mod = types.ModuleType("xonsh.completers.completer")
    completer_mod.add_one_completer = lambda name, func, loc: registered.update({name: func})  # type: ignore[attr-defined]
    tools_mod = types.ModuleType("xonsh.completers.tools")
    tools_mod.contextual_command_completer = lambda func: func  # type: ignore[attr-defined]
    for name, module in (("xonsh", types.ModuleType("xonsh")), ("xonsh.completers", types.ModuleType("xonsh.completers")),
                         ("xonsh.completers.completer", completer_mod), ("xonsh.completers.tools", tools_mod)):  # fmt: skip
        monkeypatch.setitem(sys.modules, name, module)

    class Aliases(dict[str, Any]):
        @staticmethod
        def return_command(func: Any) -> Any:
            return func

    text = shells.snippet("xonsh", cfg).replace("${...}", "{}")
    exec(compile(text, "snippet", "exec"), {"aliases": Aliases()})
    return registered["pyt"]


def _complete(completer: Any, line: str) -> list[str]:
    import types

    words = line.split(" ")
    command = types.SimpleNamespace(
        args=[types.SimpleNamespace(value=w) for w in words[:-1]], arg_index=len(words) - 1, prefix=words[-1]
    )
    return sorted(completer(command) or [])


def test_xonsh_completer_after_hooks_help_and_global_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """hooks' subcommands (a nested [--force] in its usage), command names after `help` or `-h`,
    and the command after global flags (`pyt -v --dry-run test <TAB>`)."""
    cfg = make({"tasks": {"gen": {"cmd": ["python", "gen.py"]}, "gen_docs": {"cmd": ["python", "docs.py"]}}})
    first, choices, _ = shells.completion_words(cfg)
    assert choices["hooks"] == ["install", "uninstall", "run", "status"]
    assert {"build", "test", "gen", "gen_docs"} <= set(choices["help"])  # task names may hold "_"
    assert set(first) - {"-v", "-q", "--dry-run", "--no-render"} <= set(choices["help"])
    complete = _xonsh_completer(monkeypatch, cfg)
    assert _complete(complete, "pyt hooks ") == ["install", "run", "status", "uninstall"]
    assert _complete(complete, "pyt hooks install --") == ["--force"]
    assert "build" in _complete(complete, "pyt help ") and _complete(complete, "pyt help g") == ["gen", "gen_docs"]
    assert "build" in _complete(complete, "pyt -h b")
    assert _complete(complete, "pyt -v --dry-run te") == ["test"]
    assert {"all", "cpython"} <= set(_complete(complete, "pyt -q test "))
    assert _complete(complete, "pyt --no-render build --me") == ["--method"]
    assert "test" in _complete(complete, "pyt te") and _complete(complete, "pyt test cpython x") == []
    # the skipped options are exactly the global options the runner takes before a command
    from runner import cli, proc, ui

    for module, attr in ((ui, "VERBOSE"), (ui, "QUIET"), (proc, "DRY_RUN")):
        monkeypatch.setattr(module, attr, getattr(module, attr))  # restored afterwards
    monkeypatch.setitem(cli._OPTS, "no_render", False)
    for flag in shells.GLOBAL_OPTIONS:
        assert cli._parse_globals([flag, "x"])[-1] == "x", flag
    with pytest.raises(PytError):
        cli._parse_globals(["--nope", "x"])


def test_xonsh_completion_follows_cli_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """The completion words come from cli.COMMANDS when the snippet is printed: a new command
    (`apply`, the same operation as `setup`) is offered without touching shells.py."""
    from runner import cli

    words = _xonsh_words(None)
    assert set(cli.COMMANDS) <= set(words), set(cli.COMMANDS) - set(words)
    assert not [w for w in words if isinstance(w, str) and w.startswith("__")], "internal routes are never offered"
    commands = dict(cli.COMMANDS)
    commands["apply"] = dataclasses.replace(cli.COMMANDS["setup"], summary="Apply every pytemplate.toml change")
    monkeypatch.setattr(cli, "COMMANDS", commands)
    assert "apply" in _xonsh_words(make({})) and "setup" in _xonsh_words(make({}))


def test_guess_shell() -> None:
    assert shells.guess_shell({"PYTEMPLATE_LAUNCHER": "ps1:Core:7.6"}) == "pwsh"
    assert shells.guess_shell({"PYTEMPLATE_LAUNCHER": "sh:niubash"}) == "niubash"
    assert shells.guess_shell({"PYTEMPLATE_LAUNCHER": "sh:bash:msys"}) == "bash"
    assert shells.guess_shell({"PYTEMPLATE_LAUNCHER": "cmd", "XONSH_VERSION": "0.24"}) == "xonsh"
    assert shells.guess_shell({"SHELL": "/usr/bin/fish"}) == "fish"
    assert shells.guess_shell({"PYTEMPLATE_LAUNCHER": "cmd"}) is None


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"PYTEMPLATE_LAUNCHER": "sh:bash", "SHELL": "/bin/zsh"}, "zsh"),  # macOS: /bin/sh is bash, zsh logs in
        ({"PYTEMPLATE_LAUNCHER": "sh:bash", "SHELL": "/opt/homebrew/bin/fish"}, "fish"),
        ({"PYTEMPLATE_LAUNCHER": "sh:bash", "SHELL": "/usr/bin/nu"}, "nu"),
        ({"PYTEMPLATE_LAUNCHER": "sh:bash", "SHELL": "/bin/bash"}, "bash"),
        ({"PYTEMPLATE_LAUNCHER": "sh:bash", "XONSH_VERSION": "0.19", "SHELL": "/bin/bash"}, "xonsh"),
        ({"PYTEMPLATE_LAUNCHER": "sh:zsh", "SHELL": "/usr/bin/fish"}, "fish"),
        ({"PYTEMPLATE_LAUNCHER": "sh:bash:msys", "SHELL": "/usr/bin/zsh"}, "zsh"),
        ({"PYTEMPLATE_LAUNCHER": "sh:bash"}, "bash"),  # Git Bash/MSYS2 without SHELL
        ({"PYTEMPLATE_LAUNCHER": "sh:zsh"}, "zsh"),
        ({"PYTEMPLATE_LAUNCHER": "sh:bash", "SHELL": "/usr/local/bin/pwsh"}, "pwsh"),  # pwsh logs in (macOS, Fedora)
        ({"PYTEMPLATE_LAUNCHER": "sh", "SHELL": "C:\\Program Files\\PowerShell\\7\\pwsh.exe"}, "pwsh"),
        ({"PYTEMPLATE_LAUNCHER": "nu"}, "nu"),  # the shell-setup nu function called the runner
        ({"PYTEMPLATE_LAUNCHER": "nu", "SHELL": "/bin/bash"}, "nu"),
        ({"PYTEMPLATE_LAUNCHER": "sh:bash", "SHELL": "C:\\msys64\\usr\\bin\\zsh.exe"}, "zsh"),
        ({"PYTEMPLATE_LAUNCHER": "sh:niubash", "SHELL": "/bin/zsh"}, "niubash"),  # niubash runs it in-process
        ({"PYTEMPLATE_LAUNCHER": "ps1:Core:7.6", "SHELL": "/bin/zsh"}, "pwsh"),
        ({}, None),
    ],
)
def test_guess_shell_prefers_the_login_shell_over_the_sh_interpreter(env: dict[str, str], expected: str | None) -> None:
    """sh:bash/sh:zsh only name what runs #!/bin/sh (bash on macOS, Fedora, Arch), not the user's shell."""
    assert shells.guess_shell(env) == expected


def test_snippets_keep_the_launcher_contract() -> None:
    pwsh = shells.snippet("pwsh")
    assert "if ($MyInvocation.ExpectingInput) { $input | & $ps1 @args } else { & $ps1 @args }" in pwsh
    nu = shells.snippet("nu")
    assert "PYTEMPLATE_LAUNCHER: 'nu'" in nu and "UV_PYTHON: ''" in nu
    assert "PYTHONHOME: ''" in nu and "PYTHONPATH: ''" in nu and "UV_WORKING_DIR: '.'" in nu
    assert "^$uv run --quiet --script $script ...$rest" in nu
    # Windows: only a real uv.exe (a uv.cmd/uv.bat shim would go through cmd.exe), else the launcher
    assert "let uv = if $windows { 'uv.exe' } else { 'uv' }" in nu and "pyt.cmd" in nu
    xonsh = shells.snippet("xonsh")
    assert '[uv, "run", "--quiet", "--script", str(script), *args]' in xonsh


def test_xonsh_snippet_says_which_variables_it_keeps() -> None:
    """The xonsh alias hands uv only an argument list, so it keeps the four variables the
    launchers remove: its header names every one (a PYTHONHOME stops Python before the runner's
    own version check), and the manual says the alias is the exception."""
    header = " ".join(line[1:].strip() for line in shells.snippet("xonsh").splitlines() if line.startswith("#"))
    for name in ("UV_PYTHON", "PYTHONHOME", "PYTHONPATH", "UV_WORKING_DIR"):
        assert name in header, name
    manual = (ROOT / "README.md").read_text(encoding="utf-8") if (ROOT / "README.md").is_file() else ""
    if "The runner ignores an activated virtual environment" in manual:
        paragraph = manual.split("The runner ignores an activated virtual environment", 1)[1].split("\n\n", 1)[0]
        assert "xonsh alias" in paragraph, paragraph


@pytest.mark.parametrize("windows", [True, False])
def test_xonsh_snippet_takes_only_a_real_uv_exe_on_windows(windows: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows shutil.which('uv') tries every PATHEXT in each folder, so a uv.cmd shim in an
    earlier folder won and cmd.exe parsed the arguments again: the alias asks for uv.exe (like
    the launchers and the Neovim plugin) and falls back to pyt.cmd without one."""
    import types

    text = shells.snippet("xonsh").split("\n\nif hasattr(aliases")[0].replace("${...}", "_PT_ENV")
    asked: list[str] = []

    def which(name: str, path: str | None = None) -> str | None:
        asked.append(name)
        return None if found is None else found

    namespace: dict[str, object] = {"_PT_ENV": {"PATH": ["C:\\shims", "C:\\bin"]}}
    exec(compile(text, "snippet", "exec"), namespace)
    uid = os.getuid() if hasattr(os, "getuid") else 0
    namespace["_pt_os"] = types.SimpleNamespace(name="nt" if windows else "posix", pathsep=";" if windows else ":", getuid=lambda: uid)
    namespace["_pt_shutil"] = types.SimpleNamespace(which=which)
    monkeypatch.chdir(ROOT)
    argv_of = namespace["_pt_pyt_argv"]
    assert callable(argv_of)
    found: str | None = "C:\\bin\\uv.exe" if windows else "/usr/bin/uv"
    argv, why = argv_of(["x"])
    assert asked == ["uv.exe" if windows else "uv"] and argv[:2] == [found, "run"] and not why, (asked, argv)
    found = None
    argv, why = argv_of(["x"])
    assert argv == [str(ROOT / ("pyt.cmd" if windows else "pyt")), "x"], argv


# --- the snippets, executed in their own shells (skipped where a shell is missing) -----------------


def _snippet_file(tmp_path: Path, shell: str, suffix: str) -> Path:
    path = tmp_path / f"snippet{suffix}"
    path.write_bytes(shells.snippet(shell).encode("ascii"))
    return path


def _snippet_run(argv: list[str], cwd: Path, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env={**shells.child_env(), **(extra or {})}, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180, stdin=subprocess.DEVNULL, check=False,
    )  # fmt: skip


def _python_traps(tmp: Path) -> dict[str, str]:
    """A caller's PYTHONHOME without a stdlib, a PYTHONPATH that shadows tomllib and a
    UV_WORKING_DIR elsewhere: the functions that run uv directly (nu) keep them from the runner
    like the launchers do."""
    shadow = tmp / "shadow"
    shadow.mkdir(exist_ok=True)
    (shadow / "tomllib.py").write_text('raise SystemExit("shadowed tomllib")\n', encoding="utf-8")
    (tmp / "elsewhere").mkdir(exist_ok=True)
    return {"PYTHONHOME": str(tmp / "no-home"), "PYTHONPATH": str(shadow), "UV_WORKING_DIR": str(tmp / "elsewhere")}


def _probe_lines(stdout: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for line in stdout.splitlines():
        data = shells.parse_probe(line)
        if data is not None:
            found.append(data)
    return found


def _sub() -> Path:
    return ROOT / "src" if (ROOT / "src").is_dir() else ROOT / ".pytemplate"


def _assert_probe(p: dict[str, object], argv: list[str], cwd: Path) -> None:
    assert p["argv"] == argv, p
    assert shells.same_path(p["root"], ROOT), p
    assert shells.same_path(p["caller_cwd"], cwd), p


def _away(tmp_path: Path) -> Path:
    away = tmp_path / "away"
    away.mkdir(exist_ok=True)
    return away


def _version(argv: list[str]) -> tuple[int, ...]:
    """The first dotted version a `--version` prints (`fish, version 3.7.0`, `xonsh/0.24.2`,
    nu's `0.99.1`); () when there is none."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ()
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", r.stdout + r.stderr)
    return tuple(int(n) for n in m.groups() if n is not None) if m else ()


def _shell_for_snippet(name: str, oldest: tuple[int, ...]) -> str:
    """The shell `name`, or a skip: missing, or older than the oldest its snippet supports (the
    header says which: a distribution's older one must skip, not fail, CLAUDE.md 13.1)."""
    exe = shutil.which(name)
    if not exe:
        pytest.skip(f"{name} not installed")
    found = _version([exe, "--version"])
    if found < oldest:
        pytest.skip(f"{name} {'.'.join(map(str, found)) or '?'} is older than the snippet's {'.'.join(map(str, oldest))}")
    return exe


def test_snippet_headers_name_the_oldest_shell_the_tests_accept() -> None:
    """The versions the snippet tests skip below are the ones the snippets promise."""
    assert "(fish 3.0 or later)" in shells.snippet("fish")
    assert "(xonsh 0.14 or later)" in shells.snippet("xonsh")
    assert "def --wrapped" in shells.snippet("nu")  # nushell 0.87 brought it


def test_fish_snippet_runs(tmp_path: Path) -> None:
    if IS_WINDOWS:
        pytest.skip("fish runs in MSYS2/Cygwin on Windows")
    fish = _shell_for_snippet("fish", (3, 0))
    snip = _snippet_file(tmp_path, "fish", ".fish")
    sub = _sub()
    q = shells.fish_quote
    code = (
        f"source {q(str(snip))}; cd {q(str(sub))}; pyt __probe 7 0 'a b' '' '$HOME'; echo RC=$status; "
        f"cd {q(str(_away(tmp_path)))}; pyt x; echo RC2=$status"
    )
    r = _snippet_run([fish, "--no-config", "-c", code], tmp_path)
    probes = _probe_lines(r.stdout)
    assert len(probes) == 1, r.stdout + r.stderr
    _assert_probe(probes[0], ["a b", "", "$HOME"], sub)
    assert "RC=7" in r.stdout and "RC2=2" in r.stdout and "no .pytemplate" in r.stderr, r.stdout + r.stderr


def test_fish_snippet_needs_no_path_builtin(tmp_path: Path) -> None:
    """fish before 3.5 (Ubuntu 22.04's 3.3, Debian 11's 3.1) has no `path` builtin: the walk-up
    never left the first folder, and `pyt` from src/ said there was no project. A `path` that
    fails stands in for such a fish."""
    if IS_WINDOWS:
        pytest.skip("fish runs in MSYS2/Cygwin on Windows")
    fish = _shell_for_snippet("fish", (3, 0))
    snip = _snippet_file(tmp_path, "fish", ".fish")
    sub = _sub()
    q = shells.fish_quote
    code = (
        "function path; echo 'fish: Unknown command: path' >&2; return 127; end; "
        f"source {q(str(snip))}; cd {q(str(sub))}; pyt __probe 7 0 x; echo RC=$status"
    )
    r = _snippet_run([fish, "--no-config", "-c", code], tmp_path)
    probes = _probe_lines(r.stdout)
    assert len(probes) == 1 and "RC=7" in r.stdout, r.stdout + r.stderr
    _assert_probe(probes[0], ["x"], sub)


def test_pwsh_snippet_runs(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed")
    snip = tmp_path / "snippet.ps1"  # dot-sourcing needs the .ps1 extension
    snip.write_bytes(shells.snippet("pwsh").encode("ascii"))
    sub = _sub()
    code = "\n".join([
        f". {shells.ps_quote(str(snip))}",
        f"Set-Location -LiteralPath {shells.ps_quote(str(sub))}",
        "pyt __probe 7 0 'a b' '' '*' -X:utf8",
        "'RC=' + $LASTEXITCODE",
        "'ping', 'two' | pyt __probe 0 1 piped",
        f"Set-Location -LiteralPath {shells.ps_quote(str(_away(tmp_path)))}",
        "pyt x",
        "'RC2=' + $LASTEXITCODE",
        "exit 0",
    ])  # fmt: skip
    r = _snippet_run([pwsh, "-NoProfile", "-NonInteractive", "-EncodedCommand", shells.ps_encoded(code)], tmp_path, _python_traps(tmp_path))
    probes = _probe_lines(r.stdout)
    assert len(probes) == 2, r.stdout + r.stderr
    _assert_probe(probes[0], ["a b", "", "*", "-X:utf8"], sub)
    _assert_probe(probes[1], ["piped"], sub)
    assert probes[1]["stdin"] == "ping", probes[1]
    assert "RC=7" in r.stdout and "RC2=2" in r.stdout, r.stdout + r.stderr


def test_xonsh_snippet_runs_and_completes(tmp_path: Path) -> None:
    xonsh = _shell_for_snippet("xonsh", (0, 14))
    cfg = make({"tasks": {"gen": {"cmd": ["python", "gen.py"]}}})
    snip = tmp_path / "snippet.xsh"
    snip.write_bytes(shells.snippet("xonsh", cfg).encode("ascii"))
    sub = _sub()
    code = "\n".join([
        "$XONSH_SUBPROC_CMD_RAISE_ERROR = False",
        "$XONSH_SUBPROC_RAISE_ERROR = False",
        f"source {ascii(str(snip))}",
        f"cd {ascii(str(sub))}",
        "_pt_r = ![pyt __probe 7 0 'a b' '']",
        "print('RC=' + str(_pt_r.returncode))",
        "from xonsh.completer import Completer",
        "for _pt_line in ('pyt te', 'pyt test ', 'pyt build --'):",
        "    _pt_c, _ = Completer().complete(_pt_line.split(' ')[-1], _pt_line, len(_pt_line) - len(_pt_line.split(' ')[-1]), len(_pt_line), {}, multiline_text=_pt_line, cursor_index=len(_pt_line))",
        "    print('COMP ' + _pt_line + ' => ' + ' '.join(sorted(str(c) for c in _pt_c)))",
        f"cd {ascii(str(_away(tmp_path)))}",
        "_pt_r = ![pyt x]",
        "print('RC2=' + str(_pt_r.returncode))",
    ])  # fmt: skip
    r = _snippet_run([xonsh, "--no-rc", "-c", code], tmp_path)
    probes = _probe_lines(r.stdout)
    assert len(probes) == 1, r.stdout + r.stderr
    _assert_probe(probes[0], ["a b", ""], sub)
    assert "RC=7" in r.stdout and "RC2=2" in r.stdout, r.stdout + r.stderr
    comps = {line.split(" => ")[0][5:]: line.split(" => ")[1].split() for line in r.stdout.splitlines() if line.startswith("COMP ")}
    assert "test" in comps["pyt te"] and "gen" not in comps["pyt te"], comps
    assert {"all", "cpython", "mypyc", "pypy"} <= set(comps["pyt test "]), comps
    assert "--method" in comps["pyt build --"], comps


def test_nu_snippet_runs(tmp_path: Path) -> None:
    """Not run on the machines the template was developed on (no nushell): skipped without nu."""
    nu = _shell_for_snippet("nu", (0, 87))
    snip = _snippet_file(tmp_path, "nu", ".nu")
    sub = _sub()
    run = [nu, "--no-config-file", "-c", f"source {shells.nu_quote(str(snip))}; cd {shells.nu_quote(str(sub))}; pyt __probe 7 0 'a b' ''"]
    r = _snippet_run(run, tmp_path, _python_traps(tmp_path))
    probes = _probe_lines(r.stdout)
    assert len(probes) == 1 and r.returncode == 7, r.stdout + r.stderr
    _assert_probe(probes[0], ["a b", ""], sub)
    assert probes[0]["launcher"] == "nu"
    r = _snippet_run([nu, "--no-config-file", "-c", f"source {shells.nu_quote(str(snip))}; cd {shells.nu_quote(str(_away(tmp_path)))}; pyt x"], tmp_path)
    assert r.returncode != 0 and "no .pytemplate" in r.stdout + r.stderr, r.stdout + r.stderr
    if IS_WINDOWS or any(Path(d, "uv").exists() for d in ("/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin")):
        return  # uv cannot be hidden from the launcher here
    # no uv on PATH: the launcher (it searches the install folders, then prints how to install uv)
    home = tmp_path / "home"
    home.mkdir()
    code = f"source {shells.nu_quote(str(snip))}; cd {shells.nu_quote(str(sub))}; pyt __probe 3 0 x"
    r = subprocess.run([nu, "--no-config-file", "-c", code], env={"PATH": "/usr/bin:/bin", "HOME": str(home), "CI": "1"}, capture_output=True, text=True, timeout=180, check=False)
    assert r.returncode == 127 and "uv not found" in r.stderr, r.stdout + r.stderr


@pytest.mark.parametrize("pwd", ["C:/no/such/place", "C:", "C:/", "C:\\no\\such", "/", "//srv/share/x", "/no/such/place"])
def test_posix_function_stops_at_the_top(pwd: str) -> None:
    sh = posix_sh()
    if sh is None:
        pytest.skip("no POSIX sh here")
    code = shells.POSIX_FUNCTION + f"\nPWD={shells.sh_quote(pwd)}\npyt x\nprintf 'rc=%s\\n' \"$?\"\n"
    r = subprocess.run([*sh, "-c", 'eval "$PTCMD"'], env=dict(os.environ, PTCMD=code), capture_output=True, timeout=30)
    assert b"rc=2" in r.stdout, r.stderr


def test_posix_function_runs_the_enclosing_launcher(tmp_path: Path) -> None:
    sh = posix_sh()
    if sh is None:
        pytest.skip("no POSIX sh here")
    _touch(tmp_path / "proj" / ".pytemplate" / "pyt.py")
    nested = tmp_path / "proj" / "src" / "pkg"
    nested.mkdir(parents=True)
    launcher = tmp_path / "proj" / "pyt"
    launcher.write_bytes(b"#!/bin/sh\nprintf '<%s>\\n' \"$@\"\n")
    launcher.chmod(0o755)
    mixed = str(nested).replace("\\", "/")
    code = shells.POSIX_FUNCTION + f"\nPWD={shells.sh_quote(mixed)}\npyt 'a b' ''\n"
    r = subprocess.run([*sh, "-c", 'eval "$PTCMD"'], env=dict(os.environ, PTCMD=code), capture_output=True, timeout=30, cwd=nested)
    assert r.stdout.decode().splitlines() == ["<a b>", "<>"], r.stderr


# --- doctor ------------------------------------------------------------------------------------------


def test_doctor_names_who_runs_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first launcher line: the value, or who leaves it unset (the xonsh alias, uv by hand;
    never the nu function, which sets PYTEMPLATE_LAUNCHER=nu)."""
    lines: list[tuple[bool | None, str, str]] = []
    monkeypatch.setattr(shells, "_git_modes", lambda names: {})
    monkeypatch.delenv("PYTEMPLATE_LAUNCHER", raising=False)
    shells._check_launchers(lambda ok, label, hint: lines.append((ok, label, hint)))
    ok, label, hint = lines[0]
    assert ok is None and "unknown" in label and "xonsh" in hint and not re.search(r"\bnu\b", hint), hint
    lines.clear()
    monkeypatch.setenv("PYTEMPLATE_LAUNCHER", "nu")
    shells._check_launchers(lambda ok, label, hint: lines.append((ok, label, hint)))
    assert lines[0][1] == "this run was started by: nu"


@pytest.mark.parametrize(("windows", "wsl"), [(False, False), (False, True), (True, False)])
def test_doctor_prints_the_shell_step_only_with_something_under_it(
    windows: bool, wsl: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On plain Linux and macOS `==> shell` was followed at once by the next step's header."""
    monkeypatch.setattr(shells, "IS_WINDOWS", windows)
    monkeypatch.setattr(shells, "IS_WSL", wsl)
    monkeypatch.setattr(shells, "_git_modes", lambda names: {})
    monkeypatch.setattr(shells, "_ps_policies", lambda: [])
    lines: list[str] = []
    shells.doctor(lambda ok, label, hint: lines.append(label))
    err = capsys.readouterr().err
    if windows or wsl:
        assert "==> shell" in err
    else:
        assert "==> shell" not in err and "==> launchers" in err, err
    assert ("WSL on a Windows checkout" in " ".join(lines)) == wsl


@pytest.mark.parametrize(
    ("launcher", "expected"),
    [
        (None, [None, True]),  # uv run by hand, the xonsh alias
        ("cmd", [None, True]),
        ("sh:bash:msys", [None, True]),
        ("ps1:Core:7.6", [None, True]),
        ("ps1:Desktop:5.1", [False, True]),
    ],
)
def test_doctor_counts_an_execution_policy_only_for_the_powershell_in_use(
    launcher: str | None, expected: list[bool | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows PowerShell 5.1 is Restricted by default on client Windows: doctor exited 1 there for
    every user of cmd, Git Bash, xonsh or PowerShell 7, and the hint's own way out (.\\pyt.cmd)
    never cleared it. Only the PowerShell that started this run counts; the other is a note."""
    monkeypatch.setattr(shells, "IS_WINDOWS", True)
    monkeypatch.setattr(shells, "IS_WSL", False)
    monkeypatch.setattr(shells, "_git_modes", lambda names: {})
    policies = [("Windows PowerShell 5.1", "Desktop", "Restricted"), ("PowerShell 7", "Core", "RemoteSigned")]
    monkeypatch.setattr(shells, "_ps_policies", lambda: policies)
    if launcher is None:
        monkeypatch.delenv("PYTEMPLATE_LAUNCHER", raising=False)
    else:
        monkeypatch.setenv("PYTEMPLATE_LAUNCHER", launcher)
    lines: list[tuple[bool | None, str, str]] = []
    shells.doctor(lambda ok, label, hint: lines.append((ok, label, hint)))
    policy = [(ok, label, hint) for ok, label, hint in lines if "ExecutionPolicy" in label]
    assert [ok for ok, _, _ in policy] == expected, policy
    assert "Set-ExecutionPolicy" in policy[0][2] and "pyt.cmd" in policy[0][2]


def test_ps_policies_name_the_edition_pyt_ps1_reports() -> None:
    """The editions doctor compares with PYTEMPLATE_LAUNCHER are $PSVersionTable.PSEdition's."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed")
    got = {label: edition for label, edition, _ in shells._ps_policies()}
    r = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSEdition"], capture_output=True, text=True, timeout=120, check=False)
    assert got["PowerShell 7"] == r.stdout.strip() == "Core", (got, r.stdout, r.stderr)


def test_launcher_problems() -> None:
    good_sh = b"#!/bin/sh\nexec uv run\n"
    assert shells.launcher_problems("pyt", good_sh, "100755") == []
    assert shells.launcher_problems("pyt", good_sh, None) == []
    problems = dict(shells.launcher_problems("pyt", b"#!/usr/bin/env bash\r\nx\r\n", "100644"))
    assert set(problems) == {"CRLF line endings", "the first line is not #!/bin/sh", "git mode 100644"}
    assert problems["git mode 100644"] == "git update-index --chmod=+x pyt"
    assert "git add --renormalize ." in problems["CRLF line endings"]
    assert shells.launcher_problems("pyt.cmd", b"@echo off\r\nexit /b 0\r\n", None) == []
    assert [p for p, _ in shells.launcher_problems("pyt.cmd", b"@echo off\nexit\r\n", None)] == ["not CRLF (labels and goto break with LF)"]
    assert shells.launcher_problems("pyt.ps1", b"#!/usr/bin/env pwsh\n", "100755") == []
    bom = [p for p, _ in shells.launcher_problems("pyt.ps1", b"\xef\xbb\xbf# x\n", "100755")]
    assert bom == ["non-ASCII bytes (a UTF-8 BOM)"]


def test_real_launchers_pass_the_content_checks() -> None:
    for name in ("pyt", "pyt.cmd", "pyt.ps1"):
        problems = shells.launcher_problems(name, (ROOT / name).read_bytes(), None)
        assert problems == [], (name, problems)


# --- report ------------------------------------------------------------------------------------------


def test_report_json_and_table() -> None:
    found = [shells.Shell("cmd", "cmd", ("cmd.exe",)), shells.Shell("niubash-shx", "posix", ("niu",), mode="script")]
    results = [
        shells.Result("cmd", "T1", "pass", 120, "", "cmd"),
        shells.Result("cmd", "T4", "n/a"),
        shells.Result("cmd", "T7", "skip", 5, "registry"),
        shells.Result("niubash-shx", "T1", "fail", 300, "exit 2 (expected 0)"),
        shells.Result("niubash-shx", "T4", "pass", 200, "", "sh:niubash"),
    ]
    data = shells.report_json(Path("C:/p"), found, results, 12.34)
    assert data["summary"] == {"pass": 2, "fail": 1, "skip": 1}
    assert json.loads(json.dumps(data)) == data
    assert [s["launcher"] for s in data["shells"]] == ["cmd", "sh:niubash"]  # type: ignore[index, union-attr]
    first = data["results"][0]  # type: ignore[index]
    assert set(first) == {"shell", "test", "name", "status", "ms", "detail"}
    lines = shells.table(found, results, ["T1", "T4", "T7"])
    assert lines[0].split() == ["shell", "T1", "argv", "T4", "shx", "T7", "hints", "launcher"]
    assert lines[1].split() == ["cmd", "ok", "120", "-", "skip", "cmd"]
    assert lines[2].split() == ["niubash-shx", "FAIL", "300", "ok", "200", "-", "sh:niubash"]


def test_parse_options() -> None:
    opts = shells.parse_options(["msys2,cmd", "niubash", "--tests", "t1,T3", "--jobs=2", "--timeout", "5", "--json", "--keep"])
    assert opts.names == ["msys2", "cmd", "niubash"] and opts.tests == ["T1", "T3"]
    assert (opts.jobs, opts.timeout, opts.as_json, opts.keep, opts.list_only) == (2, 5.0, True, True, False)
    assert shells.parse_options(["--project", "x"]).project == "x"
    for bad in (["--tests", "T9"], ["--jobs", "0"], ["--nope"], ["--project"]):
        with pytest.raises(PytError):
            shells.parse_options(bad)


@pytest.mark.parametrize("key", ["--jobs", "-j", "--timeout"])
@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1e400", "NaN"])
def test_a_number_that_is_not_finite_is_a_usage_error(key: str, raw: str) -> None:
    """`--jobs nan` (or inf, 1e400) passed float() and ended in an internal-error traceback."""
    with pytest.raises(PytError, match="not a finite number") as err:
        shells.parse_options([key, raw])
    assert err.value.code == 2


def test_a_timeout_the_waits_cannot_hold_is_a_usage_error() -> None:
    """Windows waits take a 32-bit count of milliseconds: a bigger timeout raised OverflowError."""
    assert shells.parse_options(["--timeout", str(shells.MAX_TIMEOUT)]).timeout == shells.MAX_TIMEOUT
    with pytest.raises(PytError, match="at most"):
        shells.parse_options(["--timeout", "5e6"])


@pytest.mark.parametrize("args", [["--tests", ","], ["--tests=, ,"], ["sh", "--tests", " "]])
def test_a_test_list_that_names_no_test_is_a_usage_error(args: list[str]) -> None:
    """`--tests ,` left no test and the suite reported `ok ... 0 passed, 0 failed`, exit 0."""
    with pytest.raises(PytError, match="names no test") as err:
        shells.parse_options(args)
    assert err.value.code == 2


# --- quick real probes (the full matrix is ./pyt selftest --shells) --------------------------------


def _context(tmp_path: Path) -> shells.Context:
    for d in ("out", "scripts", "xonsh-shell-kit", "nouv", "away"):
        (tmp_path / d).mkdir()
    return shells.Context(project=ROOT, sub=ROOT / ".pytemplate", tmp=tmp_path, away=tmp_path / "away", timeout=120, env=shells.child_env())


def _real_shell(names: tuple[str, ...]) -> shells.Shell:
    found = {s.name: s for s in shells.discover(distros=lambda wsl: [])}
    for name in names:
        if name in found:
            return found[name]
    pytest.skip(f"none of {names} here")


def test_real_probe_argv_through_sh(tmp_path: Path) -> None:
    sh = _real_shell(("git-sh", "msys2-shx") if IS_WINDOWS else ("sh",))
    result = shells.run_test(_context(tmp_path), sh, "T1")
    assert result.status == "pass", result.detail


@pytest.mark.skipif(IS_WINDOWS, reason="a POSIX stand-in for wsl.exe")
def test_a_wsl_distribution_without_its_own_uv_is_skipped(tmp_path: Path) -> None:
    """On Windows every WSL distribution is a shell of selftest --shells; in one without a uv of
    its own (installed for other work) the Linux launcher printed the install hints and exited
    127, and every test FAILed (exit 1). It is SKIP with the reason; one with uv is tested."""
    no_uv = tmp_path / "wsl-no-uv"
    no_uv.write_text("#!/bin/sh\nprintf '%s\\n' 'pyt: uv not found (https://docs.astral.sh/uv/).' >&2\nexit 127\n", encoding="utf-8")
    # `wsl -d NAME --cd DIR -e sh -c CMD`: run it here, where uv is
    with_uv = tmp_path / "wsl-uv"
    with_uv.write_text('#!/bin/sh\nshift 2\nif [ "$1" = --cd ]; then cd "$2" || exit 9; shift 2; fi\nif [ "$1" = -e ]; then shift; fi\nexec "$@"\n', encoding="utf-8")
    for fake in (no_uv, with_uv):
        fake.chmod(0o755)
    ctx = _context(tmp_path)
    skipped = shells.run_shell(ctx, shells.Shell("wsl-ubuntu", "wsl", (str(no_uv), "-d", "Ubuntu"), note="WSL Ubuntu"), list(shells.TESTS))
    assert [r.status for r in skipped] == ["skip"] * len(shells.TESTS), skipped
    assert "no uv inside WSL Ubuntu" in skipped[0].detail
    tested = shells.run_shell(ctx, shells.Shell("wsl-debian", "wsl", (str(with_uv), "-d", "Debian"), note="WSL Debian"), ["T2"])
    assert [(r.test, r.status) for r in tested] == [("T2", "pass")], tested


@pytest.mark.skipif(not IS_WINDOWS, reason="cmd.exe is Windows only")
def test_real_probe_exit_code_through_cmd(tmp_path: Path) -> None:
    result = shells.run_test(_context(tmp_path), _real_shell(("cmd",)), "T2")
    assert result.status == "pass", result.detail


@pytest.mark.parametrize("test", ["T1", "T6"])
def test_real_probe_through_pwsh(tmp_path: Path, test: str) -> None:
    """T1 carries PS_ARGS (~, typographic quotes with a payload) through the Core hand-over."""
    assert "‘q’" in shells.PS_ARGS
    result = shells.run_test(_context(tmp_path), _real_shell(("pwsh",)), test)
    assert result.status == "pass", result.detail


def test_probe_reads_stdin_bytes(tmp_path: Path) -> None:
    """A byte that is not UTF-8 arrives as \\udcXX (raw), whatever the locale: PowerShell's text
    re-encoding (U+FFFD) is then told apart from a byte-exact stdin."""
    code = "import sys; sys.path.insert(0, sys.argv[1]); from runner import shells; raise SystemExit(shells.probe(['0', '1']))"
    # A strict text stdin (macOS en_US.UTF-8, Windows code pages) would raise on \xe9.
    r = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / ".pytemplate")], input=b"caf\xe9\r\nnext\n",
        capture_output=True, timeout=60, check=False, env={**shells.child_env(), "PYTHONIOENCODING": "utf-8:strict"},
    )  # fmt: skip
    data = shells.parse_probe(r.stdout.decode("ascii"))
    assert data is not None and data["stdin"] == "caf\udce9", (r.stdout, r.stderr)


def _planted_project(tmp_path: Path) -> Path:
    """A folder another user (nobody) owns and everybody can write, holding their own
    .pytemplate/pyt.py and launchers: what anyone can make of /tmp/.pytemplate."""
    import pwd

    nobody = pwd.getpwnam("nobody")
    shared = tmp_path / "shared"
    (shared / ".pytemplate").mkdir(parents=True)
    (shared / ".pytemplate" / "pyt.py").write_text("print('PWNED by the planted pyt.py')\n", encoding="utf-8")
    (shared / "pyt").write_text("#!/bin/sh\necho 'PWNED by the planted pyt'\n", encoding="utf-8")
    (shared / "pyt.ps1").write_text("Write-Output 'PWNED by the planted pyt.ps1'\n", encoding="utf-8")
    for path in (shared, shared / ".pytemplate", *shared.rglob("*")):
        os.chown(path, nobody.pw_uid, nobody.pw_gid)
    shared.chmod(0o777)
    victim = shared / "victim"  # the user's own folder below it
    victim.mkdir()
    return victim


@pytest.mark.skipif(IS_WINDOWS or not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to make files another user owns")
def test_snippets_never_run_another_users_project(tmp_path: Path) -> None:
    """The shell-setup functions walk up from the current folder: from a folder of the user's
    own below /tmp they ran the /tmp/.pytemplate/pyt.py (or ./pyt) another user had
    planted there, as this user. A project another user owns is refused, with how to run it."""
    victim = _planted_project(tmp_path)
    runs: dict[str, list[str]] = {}
    for shell in ("bash", "zsh", "dash"):
        if shutil.which(shell):
            snip = _snippet_file(tmp_path, "bash", ".sh")
            runs[shell] = [shell, "-c", f". {shlex.quote(str(snip))}; pyt x; echo RC=$?"]
    if shutil.which("fish"):
        snip = _snippet_file(tmp_path, "fish", ".fish")
        runs["fish"] = ["fish", "--no-config", "-c", f"source {shells.fish_quote(str(snip))}; pyt x; echo RC=$status"]
    if shutil.which("pwsh"):
        snip = tmp_path / "snippet.ps1"
        snip.write_bytes(shells.snippet("pwsh").encode("ascii"))
        runs["pwsh"] = ["pwsh", "-NoProfile", "-NonInteractive", "-Command", f". {shells.ps_quote(str(snip))}; pyt x; 'RC=' + $LASTEXITCODE"]
    assert runs
    for shell, argv in runs.items():
        r = _snippet_run(argv, victim)
        out = r.stdout + r.stderr
        assert "PWNED" not in out and "RC=2" in r.stdout and "is not yours" in r.stderr, (shell, out)
