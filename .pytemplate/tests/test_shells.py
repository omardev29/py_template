"""Tests for runner/shells.py: quoting per shell family, PTPROBE parsing, shell discovery, the
launcher and shell checks of doctor, the report and two quick real probes.

The full shell x test matrix is `./pyt selftest --shells`, not pytest.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import shells  # noqa: E402
from runner.project import IS_WINDOWS, ROOT  # noqa: E402
from runner.ui import PytError  # noqa: E402

ALL_ARGS = shells.BASE_ARGS + shells.EXTRA_ARGS


def posix_sh() -> list[str] | None:
    """A POSIX sh to run code in (Git's or MSYS2's on Windows), or None."""
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
    env = shells.child_env(
        {"UV": "x", "VIRTUAL_ENV": "y", "PYTEMPLATE_LAUNCHER": "cmd", "PYTEMPLATE_GLOBAL": "1", "PWD": "C:/stale", "UV_RUN_RECURSION_DEPTH": "1", "KEEP": "1"}
    )
    assert env == {"KEEP": "1"}  # a probed launcher never inherits this runner's global mode


def test_uv_standard_dirs() -> None:
    win = shells.uv_standard_dirs({"USERPROFILE": "C:\\U", "LOCALAPPDATA": "C:\\U\\L", "SCOOP": "D:\\s"}, windows=True)
    assert Path("C:\\U") / ".local/bin" in win and Path("C:\\U\\L") / "Microsoft" / "WinGet" / "Links" in win
    assert Path("D:\\s") / "shims" in win
    posix = shells.uv_standard_dirs({"HOME": "/h"}, windows=False)
    assert Path("/h/.local/bin") in posix and Path("/opt/homebrew/bin") in posix


# --- doctor ------------------------------------------------------------------------------------------


def test_doctor_names_who_runs_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first launcher line: the value, or who leaves it unset (an older launcher, uv by
    hand; the shell-setup xonsh alias, which set none, is gone)."""
    lines: list[tuple[bool | None, str, str]] = []
    monkeypatch.setattr(shells, "_git_modes", lambda names: {})
    monkeypatch.delenv("PYTEMPLATE_LAUNCHER", raising=False)
    shells._check_launchers(lambda ok, label, hint: lines.append((ok, label, hint)))
    ok, label, hint = lines[0]
    assert ok is None and "unknown" in label and "uv run by hand" in hint and "xonsh" not in hint, hint
    lines.clear()
    monkeypatch.setenv("PYTEMPLATE_LAUNCHER", "sh:bash")
    shells._check_launchers(lambda ok, label, hint: lines.append((ok, label, hint)))
    assert lines[0][1] == "this run was started by: sh:bash"


@pytest.mark.parametrize(("windows", "wsl"), [(False, False), (False, True), (True, False)])
def test_doctor_outside_a_project_checks_no_launcher_file(
    windows: bool, wsl: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Global mode (doctor outside a project): the launcher that started the run and the shell
    (Windows' bash stub and execution policies), never the files of the installed template, which
    are no project's launchers, nor the WSL note about a project's checkout."""
    monkeypatch.setattr(shells, "IS_WINDOWS", windows)
    monkeypatch.setattr(shells, "IS_WSL", wsl)
    monkeypatch.setattr(shells, "_git_modes", lambda names: pytest.fail("a launcher file was checked"))
    monkeypatch.setattr(shells, "_ps_policies", lambda: [("PowerShell 7", "Core", "RemoteSigned")])
    monkeypatch.setenv("PYTEMPLATE_LAUNCHER", "sh")
    lines: list[str] = []
    shells.doctor(lambda ok, label, hint: lines.append(label), in_project=False)
    err = capsys.readouterr().err
    assert lines[0] == "this run was started by: sh"
    assert not any(name in label for label in lines for name in ("pyt.cmd", "pyt.ps1", "#!/bin/sh")), lines
    assert ("PowerShell 7: ExecutionPolicy = RemoteSigned" in lines) == windows
    assert ("==> shell" in err) == windows and not any("WSL" in label for label in lines), (err, lines)


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
        (None, [None, True]),  # uv run by hand
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
