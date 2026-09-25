"""The Windows launchers deploy.cmd and deploy.ps1 (run them with `./deploy selftest`).

Static rules are checked everywhere. The behavioural tests run only on Windows: they call the
hidden runner command `__probe EXIT STDIN(0|1) ARGS...` through each launcher, which prints one
PTPROBE{json} line (argv, caller cwd, launcher, stdin, root) and exits with EXIT. A cmd start
costs about 0.3 s and a PowerShell one 1-2 s, so each PowerShell process checks several things.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CMD = ROOT / "deploy.cmd"
PS1 = ROOT / "deploy.ps1"
# A folder below the root to call the launchers from (src/ may be missing in a new project).
SUB = ROOT / "src" if (ROOT / "src").is_dir() else ROOT / ".pytemplate"
IS_WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows launchers")

NON_ASCII = "\u00e9\u00f1"
# cmd parses its own command line: no % ! " ^ & | < > in these (see the deploy.cmd header).
CMD_ARGS = ["plain", "a b", "", "tr\\", "sp tr\\", NON_ASCII, "--flag=x", "-v", "--"]
# PowerShell also keeps quotes, $, * and commas. A bare -- is removed by PowerShell itself, so
# the session scripts pass it quoted ('--'), and -v bare (a parameter token for PowerShell).
PS_ARGS = [*CMD_ARGS, 'q"x', "a'b", "*", "$HOME", 'x "y z', 'a\\"b c', '"', "%PATH%", "a,b", "$(1)"]


def _text_lines(path: Path) -> list[str]:
    return path.read_bytes().decode("ascii").splitlines()


def _code_lines_cmd() -> list[str]:
    return [line.strip() for line in _text_lines(CMD) if line.strip() and not re.match(r"(?i)rem\b|::", line.strip())]


# --- deploy.cmd: static ------------------------------------------------------------------------


def test_cmd_is_ascii_with_crlf() -> None:
    data = CMD.read_bytes()
    assert data.isascii(), "deploy.cmd must be ASCII (cmd reads it in the OEM code page)"
    assert data.count(b"\n") == data.count(b"\r\n") > 0, "deploy.cmd needs CRLF line endings (labels break with LF)"


def test_cmd_has_no_blocks_and_no_delayed_expansion() -> None:
    for line in _code_lines_cmd():
        assert not line.endswith("("), f"( ) block in deploy.cmd: {line}"
        assert not re.search(r"\)\s*else\b", line, re.IGNORECASE), f"else block in deploy.cmd: {line}"
    text = CMD.read_bytes().decode("ascii")
    assert re.search(r"(?im)^setlocal\b.*\bDisableDelayedExpansion\b", text)
    assert not re.search(r"(?i)\bEnableDelayedExpansion\b", text)


def test_cmd_forwards_arguments_only_on_the_uv_line() -> None:
    lines = [line for line in _text_lines(CMD) if "%*" in line]
    assert len(lines) == 1 and "run --quiet --script" in lines[0], lines
    assert not [line for line in _text_lines(CMD) if re.match(r"(?i)\s*rem\b", line) and "%" in line], (
        "cmd expands % even on rem lines"
    )


# --- deploy.ps1: static ------------------------------------------------------------------------


def test_ps1_is_ascii_lf_without_bom() -> None:
    data = PS1.read_bytes()
    assert data.isascii(), "deploy.ps1 must be ASCII (no BOM: xonsh and Unix kernels read the shebang)"
    assert b"\r" not in data, "deploy.ps1 needs LF line endings"
    assert data.startswith(b"#!/usr/bin/env pwsh\n")


def test_ps1_has_no_param_block_and_leaves_path_alone() -> None:
    text = PS1.read_text(encoding="ascii")
    assert not re.search(r"(?im)^\s*(param\s*\(|\[CmdletBinding)", text), "a param() block turns -v, -h, -q into parameters"
    assert not re.search(r"(?i)\$env:path\s*\+?=(?!=)", text)
    assert not re.search(r"(?i)SetEnvironmentVariable\(\s*['\"]path['\"]|(Set|New|Remove)-Item\s+\S*env:path\b", text)


def test_ps1_restores_every_variable_it_sets() -> None:
    text = PS1.read_text(encoding="ascii")
    assigned = {m.upper() for m in re.findall(r"(?i)\$env:(\w+)\s*=(?!=)", text)}
    names = re.search(r"(?m)^\$names = (.+)$", text)
    assert names, "deploy.ps1 lists the variables it restores in `$names = ...`"
    restored = {m.upper() for m in re.findall(r"'(\w+)'", names[1])}
    assert assigned and assigned <= restored, f"set but not restored: {assigned - restored}"


def test_ps1_is_executable_in_git() -> None:
    git = shutil.which("git")
    if not git:
        pytest.skip("git not installed")
    r = subprocess.run([git, "ls-files", "-s", "--", "deploy.ps1"], cwd=ROOT, capture_output=True, text=True, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("deploy.ps1 is not tracked by git here")
    assert r.stdout.split()[0] == "100755", "git update-index --chmod=+x deploy.ps1 (for ./deploy.ps1 on Linux/macOS)"


def _powershells() -> list[str]:
    return [exe for exe in (shutil.which("pwsh"), shutil.which("powershell") if IS_WINDOWS else None) if exe]


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


@pytest.mark.parametrize("name", ["pwsh", "powershell"])
def test_ps1_parses(name: str) -> None:
    exe = shutil.which(name) if name == "pwsh" or IS_WINDOWS else None
    if not exe:
        pytest.skip(f"{name} not installed")
    script = (
        "$e = $null; $t = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseFile($env:PT_TEST_PS1, [ref]$t, [ref]$e)\n"
        "foreach ($x in $e) { [Console]::Out.WriteLine($x.ToString()) }\n"
        "exit @($e).Count\n"
    )
    r = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded(script)],
        env={**os.environ, "PT_TEST_PS1": str(PS1)}, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr


# --- behaviour (Windows) -----------------------------------------------------------------------


def _clean_env(**changes: str | None) -> dict[str, str]:
    """This environment without what `uv run` (pytest's parent) sets, so the launchers search for uv."""
    drop = {"UV", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_RUN_RECURSION_DEPTH"}
    env = {k: v for k, v in os.environ.items() if k.upper() not in drop and not k.upper().startswith("PYTEMPLATE_")}
    for key, value in changes.items():
        for k in [k for k in env if k.upper() == key.upper()]:
            del env[k]
        if value is not None:
            env[key] = value
    return env


def _run(argv: list[str] | str, cwd: Path, env: dict[str, str] | None = None, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=env if env is not None else _clean_env(), input=stdin, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
    )


def _probes(r: subprocess.CompletedProcess[str]) -> list[dict[str, object]]:
    found = [json.loads(line[len("PTPROBE"):]) for line in r.stdout.splitlines() if line.startswith("PTPROBE")]
    assert found, f"no PTPROBE line (exit {r.returncode}): {r.stdout!r} {r.stderr!r}"
    return found


def _same(a: object, b: Path) -> bool:
    try:
        return os.path.samefile(str(a), b)
    except OSError:
        return False


def _check(p: dict[str, object], cwd: Path, launcher: str, argv: list[str] | None = None) -> None:
    if argv is not None:
        assert p["argv"] == argv
    assert _same(p["root"], ROOT), p
    assert _same(p["caller_cwd"], cwd) and _same(p["caller_cwd_raw"] or "", cwd), p
    assert str(p["launcher"]).startswith(launcher), p


def _minimal_path() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return f"{root}\\System32;{root}"


def _hidden_env(tmp: Path, path: str) -> dict[str, str]:
    """Every uv install folder moved into tmp, and PATH = path. uv keeps its real cache."""
    uv = os.environ.get("UV") or shutil.which("uv")
    cache = subprocess.run([uv, "cache", "dir"], capture_output=True, text=True, check=False).stdout.strip() if uv else ""
    return _clean_env(
        PATH=path, USERPROFILE=str(tmp), LOCALAPPDATA=str(tmp), ProgramFiles=str(tmp), ProgramData=str(tmp),
        SCOOP=None, SCOOP_GLOBAL=None, ChocolateyInstall=None, CARGO_HOME=None, UV_INSTALL_DIR=None,
        XDG_BIN_HOME=None, XDG_DATA_HOME=None, UV_CACHE_DIR=cache or None,
    )


def _no_uv_env(tmp: Path) -> dict[str, str]:
    """No uv anywhere: install folders moved away and an empty PATH (so no reg.exe either)."""
    (tmp / "bin").mkdir(exist_ok=True)
    return _hidden_env(tmp, str(tmp / "bin"))


def _assert_hints(r: subprocess.CompletedProcess[str]) -> None:
    assert r.returncode == 127, (r.returncode, r.stdout, r.stderr)
    assert "PTPROBE" not in r.stdout
    for hint in ("irm https://astral.sh/uv/install.ps1 | iex", "winget install --id=astral-sh.uv -e", "scoop install main/uv"):
        assert hint in r.stderr, r.stderr
    assert "curl" not in r.stderr


def _has_uv(dirs: list[str]) -> bool:
    return any(os.path.isfile(os.path.join(d, "uv.exe")) for d in dirs if d)


def _registry_path(env: dict[str, str]) -> list[str]:
    """The user and machine PATH stored in the registry, %VARS% expanded with env."""
    import winreg

    upper = {k.upper(): v for k, v in env.items()}
    keys = (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    dirs: list[str] = []
    for hive, key in keys:
        try:
            with winreg.OpenKey(hive, key) as k:
                value = str(winreg.QueryValueEx(k, "Path")[0])
        except OSError:
            continue
        value = re.sub(r"%([^%;]+)%", lambda m: upper.get(m[1].upper(), m[0]), value)
        dirs += [d.strip().strip('"') for d in value.split(";")]
    return dirs


def _uv_outside_path() -> bool:
    """True when uv sits where the launchers look beyond PATH (install folders, registry PATH)."""
    home, lad = os.environ.get("USERPROFILE", ""), os.environ.get("LOCALAPPDATA", "")
    dirs = [os.path.join(home, ".local", "bin"), os.path.join(home, ".cargo", "bin"), os.path.join(home, "scoop", "shims")]
    dirs += [os.path.join(lad, "Microsoft", "WinGet", "Links"), *glob.glob(os.path.join(lad, "Microsoft", "WinGet", "Packages", "astral-sh.uv_*"))]
    return _has_uv(dirs) or _has_uv(_registry_path(dict(os.environ)))


def _check_uv_outside_path(launcher: list[str], tmp: Path, prefix: str) -> None:
    """uv found with PATH = System32 only; then through the registry PATH alone when that is where it is."""
    if not _uv_outside_path():
        pytest.skip("uv is only reachable through this process's PATH")
    r = _run([*launcher, "__probe", "0", "0", "m"], ROOT, _clean_env(PATH=_minimal_path()))
    assert r.returncode == 0, r.stderr
    _check(_probes(r)[0], ROOT, prefix, ["m"])
    env = _hidden_env(tmp, _minimal_path())
    if not _has_uv(_registry_path(env)):
        pytest.skip("uv is not in the registry PATH")
    r = _run([*launcher, "__probe", "0", "0", "r"], ROOT, env)
    assert r.returncode == 0, r.stderr
    _check(_probes(r)[0], ROOT, prefix, ["r"])


@windows_only
def test_cmd_round_trip_exit_code_and_stdin() -> None:
    # CreateProcess on the .cmd itself, as Python, xonsh and VS Code "process" tasks do.
    r = _run([str(CMD), "__probe", "0", "0", *CMD_ARGS], ROOT)
    assert r.returncode == 0, r.stderr
    _check(_probes(r)[0], ROOT, "cmd", CMD_ARGS)
    r = _run([str(CMD), "__probe", "37", "1"], ROOT, stdin="ping\n")
    assert r.returncode == 37 and _probes(r)[0]["stdin"] == "ping", r.stderr


@windows_only
def test_cmd_from_a_subfolder_through_cmd(tmp_path: Path) -> None:
    # A command line typed in cmd (a string: list2cmdline would escape the inner quotes).
    comspec = os.environ.get("ComSpec", "cmd.exe")
    r = _run(f'"{comspec}" /d /s /c "{os.path.relpath(CMD, SUB)} __probe 0 0 "a b" "" x"', SUB)
    assert r.returncode == 0, r.stderr
    _check(_probes(r)[0], SUB, "cmd", ["a b", "", "x"])
    r = _run([str(CMD), "__probe", "4", "0", "t"], tmp_path)  # absolute path from outside the project
    assert r.returncode == 4
    _check(_probes(r)[0], tmp_path, "cmd", ["t"])


@windows_only
def test_cmd_walks_up_from_the_current_folder(tmp_path: Path) -> None:
    copy = tmp_path / "deploy.cmd"
    shutil.copyfile(CMD, copy)
    r = _run([str(copy), "__probe", "0", "0", "w"], SUB)
    assert r.returncode == 0, r.stderr
    _check(_probes(r)[0], SUB, "cmd", ["w"])
    r = _run([str(copy), "__probe", "0", "0"], tmp_path)
    assert r.returncode == 2 and "no .pytemplate" in r.stderr, (r.returncode, r.stderr)


@windows_only
def test_cmd_finds_uv_outside_path(tmp_path: Path) -> None:
    _check_uv_outside_path([str(CMD)], tmp_path, "cmd")


@windows_only
def test_cmd_prints_install_hints_without_uv(tmp_path: Path) -> None:
    _assert_hints(_run([str(CMD), "__probe", "0", "0"], ROOT, _no_uv_env(tmp_path)))


@windows_only
def test_cmd_hands_the_runner_only_its_two_variables(tmp_path: Path) -> None:
    project = tmp_path / "p"
    (project / ".pytemplate").mkdir(parents=True)
    (project / ".pytemplate" / "deploy.py").write_text(
        "import json, os\nprint('ENV' + json.dumps({k.upper(): v for k, v in os.environ.items() if k.upper().startswith(('PT_', 'PYTEMPLATE_'))}))\n",
        encoding="utf-8", newline="\n",
    )
    shutil.copyfile(CMD, project / "deploy.cmd")
    env = _clean_env()
    before = {k.upper() for k in env if k.upper().startswith("PT_")}
    r = _run([str(project / "deploy.cmd")], project, env)
    seen = [json.loads(line[3:]) for line in r.stdout.splitlines() if line.startswith("ENV")]
    assert r.returncode == 0 and len(seen) == 1, r.stdout + r.stderr
    assert set(seen[0]) == before | {"PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER"}
    assert seen[0]["PYTEMPLATE_LAUNCHER"] == "cmd" and _same(seen[0]["PYTEMPLATE_CALLER_CWD"], project)


def _ps_session(launcher: str, legacy: bool) -> str:
    args = " ".join(a if a == "-v" else "'" + a.replace("'", "''") + "'" for a in PS_ARGS)
    lines = [
        # A demanding caller session: the launcher must still return the runner's exit code,
        # without an error record. No progress records: 5.1 prints them as CLIXML on stderr.
        "$ProgressPreference = 'SilentlyContinue'",
        "Set-StrictMode -Version Latest; $ErrorActionPreference = 'Stop'; $PSNativeCommandUseErrorActionPreference = $true",
        "$env:PYTEMPLATE_LAUNCHER = 'before'",
        "if (Test-Path Env:PYTEMPLATE_CALLER_CWD) { Remove-Item Env:PYTEMPLATE_CALLER_CWD }",
        f"& {launcher} __probe 0 0 {args}",
        "'RC=' + $LASTEXITCODE",
        f"& {launcher} __probe 37 0",
        "'RC=' + $LASTEXITCODE",
        # 5.1 turns redirected native stderr into error records: with 'Stop' they would throw.
        f"$null = & {launcher} no-such-command 2>&1",
        "'RC=' + $LASTEXITCODE",
    ]
    if legacy:
        lines += ["$PSNativeCommandArgumentPassing = 'Legacy'", f"& {launcher} __probe 0 0 {args}", "'RC=' + $LASTEXITCODE"]
    lines += [
        "'AFTER=' + (ConvertTo-Json -Compress @($env:PYTEMPLATE_LAUNCHER, [Environment]::GetEnvironmentVariable('PYTEMPLATE_CALLER_CWD')))",
        "exit 0",
    ]
    return "\n".join(lines)


@windows_only
@pytest.mark.parametrize("name", ["pwsh", "powershell"])
def test_ps1_round_trip_in_a_session(name: str) -> None:
    exe = shutil.which(name)
    if not exe:
        pytest.skip(f"{name} not installed")
    legacy = name == "pwsh"  # also with $PSNativeCommandArgumentPassing = 'Legacy' (5.1 always is)
    launcher = os.path.relpath(PS1, SUB)
    body = _ps_session(launcher if launcher.startswith(".") else ".\\" + launcher, legacy)
    r = _run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", _encoded(body)], SUB)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stderr.strip() == "", r.stderr
    probes = _probes(r)
    assert len(probes) == (3 if legacy else 2), r.stdout
    prefix = "ps1:Core:" if name == "pwsh" else "ps1:Desktop:5."
    for p in probes:
        _check(p, SUB, prefix)
    assert probes[0]["argv"] == PS_ARGS
    assert probes[1]["argv"] == []
    if legacy:
        assert probes[2]["argv"] == PS_ARGS
    rcs = [line for line in r.stdout.splitlines() if line.startswith("RC=")]
    assert rcs == ["RC=0", "RC=37", "RC=2"] + (["RC=0"] if legacy else []), rcs
    after = [json.loads(line[len("AFTER="):]) for line in r.stdout.splitlines() if line.startswith("AFTER=")]
    assert after == [["before", None]], f"the caller's environment was not restored: {after}"


def _any_powershell() -> str:
    exes = _powershells()
    if not exes:
        pytest.skip("no PowerShell installed")
    return exes[0]


@windows_only
def test_ps1_walks_up_and_passes_file_arguments(tmp_path: Path) -> None:
    exe = _any_powershell()
    copy = tmp_path / "deploy.ps1"
    shutil.copyfile(PS1, copy)
    base = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(copy), "__probe"]
    r = _run([*base, "5", "0", "a b", "", "tr\\"], SUB)
    assert r.returncode == 5, r.stderr
    _check(_probes(r)[0], SUB, "ps1:", ["a b", "", "tr\\"])
    r = _run([*base, "0", "0"], tmp_path)
    assert r.returncode == 2 and "no .pytemplate" in r.stderr, (r.returncode, r.stderr)


@windows_only
def test_ps1_finds_uv_outside_path(tmp_path: Path) -> None:
    exe = _any_powershell()
    _check_uv_outside_path([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS1)], tmp_path, "ps1:")


@windows_only
def test_ps1_prints_install_hints_without_uv(tmp_path: Path) -> None:
    exe = _any_powershell()
    base = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    # The registry PATH cannot be hidden from .NET without touching the registry: drop those
    # two lines from a copy (the copy finds the project by walking up from ROOT).
    lines = PS1.read_text(encoding="ascii").splitlines()
    registry = [line for line in lines if "GetEnvironmentVariable('Path'" in line]
    assert len(registry) == 2
    copy = tmp_path / "nouv" / "deploy.ps1"
    copy.parent.mkdir()
    copy.write_text("\n".join(line for line in lines if line not in registry) + "\n", encoding="ascii", newline="\n")
    _assert_hints(_run([*base, str(copy), "__probe", "0", "0"], ROOT, _no_uv_env(tmp_path)))
