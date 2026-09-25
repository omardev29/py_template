"""Tests for runner/shells.py: quoting per shell family, PTPROBE parsing, shell discovery, the
shell-setup snippets, the launcher checks of doctor, the report and two quick real probes.

The full shell x test matrix is `./deploy selftest --shells`, not pytest.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config, shells  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import IS_WINDOWS, ROOT  # noqa: E402
from runner.ui import DeployError  # noqa: E402

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
    code = "& './deploy' 'x'\nexit $LASTEXITCODE\n"
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
    assert shells.command_text(posix, project, "root", ["a b"]) == "'C:/g/dash.exe' ./deploy 'a b'"
    assert shells.command_text(posix, project, "sub", []).endswith("../deploy")
    assert shells.command_text(posix, project, "abs", []).endswith(shells.sh_quote(str(project / "deploy").replace("\\", "/")))
    assert shells.command_text(posix, project, "root", [], "/usr/bin:/bin").startswith("PATH=/usr/bin:/bin; export PATH; ")

    cmd = shells.Shell("cmd", "cmd", ("cmd.exe",))
    assert shells.command_text(cmd, project, "root", ["with space", ""]) == '.\\deploy "with space" ""'
    assert shells.command_text(cmd, project, "abs", []) == f'"{project / "deploy"}"'

    ps = shells.Shell("pwsh", "powershell", ("pwsh",))
    text = shells.command_text(ps, project, "root", ["--", "a'b"])
    word = "./deploy" if IS_WINDOWS else "./deploy.ps1"  # on Windows ./deploy resolves to deploy.ps1
    assert f"& '{word}' '--' 'a''b'" in text and text.rstrip().endswith("exit $LASTEXITCODE")
    assert shells.ps_quote(str(project / "deploy.ps1")) in shells.command_text(ps, project, "abs", [])

    xonsh = shells.Shell("xonsh", "xonsh", ("xonsh", "--no-rc"))
    text = shells.command_text(xonsh, project, "root", ["\u00fcn", 'q"x'])
    assert "![./deploy @(['\\xfcn', 'q\"x'])]" in text and "sys.exit(_pt_r.returncode)" in text
    assert text.isascii()

    wsl = shells.Shell("wsl-u", "wsl", ("wsl.exe", "-d", "U"))
    assert "$(wslpath -u " in shells.command_text(wsl, project, "abs", [])


def test_invocation(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    posix = shells.Shell("sh", "posix", ("/bin/sh",))
    inv = shells.invocation(posix, project, "root", ["__probe", "0", "0", "back\\slash"], cwd=project, scripts=tmp_path, tag="t")
    assert inv.argv == ["/bin/sh", "-c", 'eval "$PTCMD"']  # never the command itself: Cygwin mangles argv
    assert inv.env["PTCMD"] == "./deploy '__probe' '0' '0' 'back\\slash'"

    script = shells.Shell("niubash-shx", "posix", ("niu",), mode="script", mixed=True)
    inv = shells.invocation(script, project, "sub", ["__probe"], cwd=project, scripts=tmp_path, tag="s")
    assert inv.script is not None and inv.argv == ["niu", str(inv.script)]
    assert inv.script.read_bytes() == b"../deploy '__probe'\n"

    cmd = shells.Shell("cmd", "cmd", ("C:\\Windows\\system32\\cmd.exe", "/d", "/s", "/c"))
    inv = shells.invocation(cmd, project, "root", ["__probe", "with space"], cwd=project, scripts=tmp_path, tag="c")
    assert inv.argv == 'C:\\Windows\\system32\\cmd.exe /d /s /c ".\\deploy __probe "with space""'

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


def test_discover_posix_with_fake_which(tmp_path: Path) -> None:
    found = {"bash": _touch(tmp_path / "bash"), "zsh": _touch(tmp_path / "zsh"), "busybox": _touch(tmp_path / "busybox")}
    got = {s.name: s for s in shells.discover({}, windows=False, which=found.get)}
    assert got["bash"].interp == (found["bash"],) and got["bash"].argv == (found["bash"], "--norc", "--noprofile")
    assert got["zsh"].argv[1] == "-f"
    assert got["busybox"].interp == (found["busybox"], "sh")
    assert "dash" not in got and "pwsh" not in got


def test_select() -> None:
    found = [shells.Shell(n, "posix", ("x",)) for n in ("cmd", "msys2-msys", "msys2-ucrt64", "niubash", "niubash-shx")]
    assert [s.name for s in shells.select(found, ["msys2"])] == ["msys2-msys", "msys2-ucrt64"]
    assert [s.name for s in shells.select(found, ["niubash-shx", "cmd"])] == ["cmd", "niubash-shx"]
    assert len(shells.select(found, [])) == 5
    with pytest.raises(DeployError, match="not found here: fish"):
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
        assert "deploy" in text and ("Paste" in text or "Save as" in text), shell
    with pytest.raises(DeployError, match="unknown shell"):
        shells.snippet("tcsh")


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
    assert "add_one_completer" in text and 'aliases["deploy"]' in text


def test_guess_shell() -> None:
    assert shells.guess_shell({"PYTEMPLATE_LAUNCHER": "ps1:Core:7.6"}) == "pwsh"
    assert shells.guess_shell({"PYTEMPLATE_LAUNCHER": "sh:niubash"}) == "niubash"
    assert shells.guess_shell({"PYTEMPLATE_LAUNCHER": "sh:bash:msys"}) == "bash"
    assert shells.guess_shell({"PYTEMPLATE_LAUNCHER": "cmd", "XONSH_VERSION": "0.24"}) == "xonsh"
    assert shells.guess_shell({"SHELL": "/usr/bin/fish"}) == "fish"
    assert shells.guess_shell({"PYTEMPLATE_LAUNCHER": "cmd"}) is None


@pytest.mark.parametrize("pwd", ["C:/no/such/place", "C:", "C:/", "C:\\no\\such", "/", "//srv/share/x", "/no/such/place"])
def test_posix_function_stops_at_the_top(pwd: str) -> None:
    sh = posix_sh()
    if sh is None:
        pytest.skip("no POSIX sh here")
    code = shells.POSIX_FUNCTION + f"\nPWD={shells.sh_quote(pwd)}\ndeploy x\nprintf 'rc=%s\\n' \"$?\"\n"
    r = subprocess.run([*sh, "-c", 'eval "$PTCMD"'], env=dict(os.environ, PTCMD=code), capture_output=True, timeout=30)
    assert b"rc=2" in r.stdout, r.stderr


def test_posix_function_runs_the_enclosing_launcher(tmp_path: Path) -> None:
    sh = posix_sh()
    if sh is None:
        pytest.skip("no POSIX sh here")
    _touch(tmp_path / "proj" / ".pytemplate" / "deploy.py")
    nested = tmp_path / "proj" / "src" / "pkg"
    nested.mkdir(parents=True)
    launcher = tmp_path / "proj" / "deploy"
    launcher.write_bytes(b"#!/bin/sh\nprintf '<%s>\\n' \"$@\"\n")
    launcher.chmod(0o755)
    mixed = str(nested).replace("\\", "/")
    code = shells.POSIX_FUNCTION + f"\nPWD={shells.sh_quote(mixed)}\ndeploy 'a b' ''\n"
    r = subprocess.run([*sh, "-c", 'eval "$PTCMD"'], env=dict(os.environ, PTCMD=code), capture_output=True, timeout=30, cwd=nested)
    assert r.stdout.decode().splitlines() == ["<a b>", "<>"], r.stderr


# --- doctor ------------------------------------------------------------------------------------------


def test_launcher_problems() -> None:
    good_sh = b"#!/bin/sh\nexec uv run\n"
    assert shells.launcher_problems("deploy", good_sh, "100755") == []
    assert shells.launcher_problems("deploy", good_sh, None) == []
    problems = dict(shells.launcher_problems("deploy", b"#!/usr/bin/env bash\r\nx\r\n", "100644"))
    assert set(problems) == {"CRLF line endings", "the first line is not #!/bin/sh", "git mode 100644"}
    assert problems["git mode 100644"] == "git update-index --chmod=+x deploy"
    assert "git add --renormalize ." in problems["CRLF line endings"]
    assert shells.launcher_problems("deploy.cmd", b"@echo off\r\nexit /b 0\r\n", None) == []
    assert [p for p, _ in shells.launcher_problems("deploy.cmd", b"@echo off\nexit\r\n", None)] == ["not CRLF (labels and goto break with LF)"]
    assert shells.launcher_problems("deploy.ps1", b"#!/usr/bin/env pwsh\n", "100755") == []
    bom = [p for p, _ in shells.launcher_problems("deploy.ps1", b"\xef\xbb\xbf# x\n", "100755")]
    assert bom == ["non-ASCII bytes (a UTF-8 BOM)"]


def test_real_launchers_pass_the_content_checks() -> None:
    for name in ("deploy", "deploy.cmd", "deploy.ps1"):
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
        with pytest.raises(DeployError):
            shells.parse_options(bad)


# --- quick real probes (the full matrix is ./deploy selftest --shells) --------------------------------


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


@pytest.mark.skipif(not IS_WINDOWS, reason="cmd.exe is Windows only")
def test_real_probe_exit_code_through_cmd(tmp_path: Path) -> None:
    result = shells.run_test(_context(tmp_path), _real_shell(("cmd",)), "T2")
    assert result.status == "pass", result.detail
