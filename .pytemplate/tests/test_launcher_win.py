"""The launchers pyt.cmd and pyt.ps1 (run them with `./pyt selftest`).

Static rules are checked everywhere. The behavioural tests call the hidden runner command
`__probe EXIT STDIN(0|1) ARGS...` through each launcher, which prints one PTPROBE{json} line
(argv, caller cwd, launcher, stdin, root) and exits with EXIT. pyt.cmd needs Windows;
pyt.ps1 runs wherever PowerShell 7 (pwsh) is installed, Linux and macOS included (the same
Core hand-over), and Windows PowerShell 5.1 is added on Windows. A cmd start costs about 0.3 s
and a PowerShell one 1-2 s, so each PowerShell process checks several things.
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
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_install  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CMD = ROOT / "pyt.cmd"
PS1 = ROOT / "pyt.ps1"
# A folder below the root to call the launchers from (src/ may be missing in a new project).
SUB = ROOT / "src" if (ROOT / "src").is_dir() else ROOT / ".pytemplate"
IS_WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows launchers")
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="exec bits and fake #!/bin/sh programs: Linux/macOS")
PS_NAMES = ["pwsh", "powershell"]

NON_ASCII = "\u00e9\u00f1"
# cmd parses its own command line: no % ! " ^ & | < > in these (see the pyt.cmd header).
CMD_ARGS = ["plain", "a b", "", "tr\\", "sp tr\\", NON_ASCII, "--flag=x", "-v", "--"]
# PowerShell also keeps quotes (the typographic single quotes are quotes for PowerShell too), $,
# * and commas. A bare -- is removed by PowerShell itself, so the session scripts pass it quoted
# ('--'), and -v bare (a parameter token for PowerShell).
PS_ARGS = [
    *CMD_ARGS, 'q"x', "a'b", "*", "$HOME", 'x "y z', 'a\\"b c', '"', "%PATH%", "a,b", "$(1)",
    "\u2018q\u2019", "\u201a; Write-Output PWNED; \u201b",
]  # fmt: skip
QUOTES = ("'", "\u2018", "\u2019", "\u201a", "\u201b")  # what PowerShell reads as a single quote
# What the launchers keep from the runner's start: the caller's interpreter choice, a PYTHONHOME or
# PYTHONPATH that breaks the runner's Python, the folder uv would move to, and the flags uv refuses
# next to the launchers' --python-preference.
CLEARED = {"UV_PYTHON", "PYTHONHOME", "PYTHONPATH", "UV_WORKING_DIR", "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON"}


def _ps_literal(s: str) -> str:
    """A PowerShell single-quoted literal (every kind of single quote doubled, like shells.ps_quote)."""
    for q in QUOTES:
        s = s.replace(q, q + q)
    return "'" + s + "'"


# A profile function of the kind users write around the launcher: it forwards its words with @args
# (and pipeline input like a native call). pyt.ps1 reads the words typed where that caller was
# called. (`./pyt shell-setup pwsh` used to print one like it: `pyt install` replaced it.)
PWSH_WRAPPER = (
    "function pyt {\n"
    f"    $ps1 = {_ps_literal(str(PS1))}\n"
    "    if ($MyInvocation.ExpectingInput) { $input | & $ps1 @args } else { & $ps1 @args }\n"
    "}"
)


def _text_lines(path: Path) -> list[str]:
    return path.read_bytes().decode("ascii").splitlines()


def _code_lines_cmd() -> list[str]:
    return [line.strip() for line in _text_lines(CMD) if line.strip() and not re.match(r"(?i)rem\b|::", line.strip())]


# --- pyt.cmd: static ------------------------------------------------------------------------


def test_cmd_is_ascii_with_crlf() -> None:
    data = CMD.read_bytes()
    assert data.isascii(), "pyt.cmd must be ASCII (cmd reads it in the OEM code page)"
    assert data.count(b"\n") == data.count(b"\r\n") > 0, "pyt.cmd needs CRLF line endings (labels break with LF)"


def test_cmd_has_no_blocks_and_no_delayed_expansion() -> None:
    for line in _code_lines_cmd():
        assert not line.endswith("("), f"( ) block in pyt.cmd: {line}"
        assert not re.search(r"\)\s*else\b", line, re.IGNORECASE), f"else block in pyt.cmd: {line}"
    text = CMD.read_bytes().decode("ascii")
    assert re.search(r"(?im)^setlocal\b.*\bDisableDelayedExpansion\b", text)
    assert not re.search(r"(?i)\bEnableDelayedExpansion\b", text)


def test_cmd_forwards_arguments_only_on_the_uv_line() -> None:
    lines = [line for line in _text_lines(CMD) if "%*" in line]
    assert len(lines) == 1 and 'run --quiet "--python=%PT_PY%" --python-preference %PT_PREF% --script' in lines[0], lines
    assert not [line for line in _text_lines(CMD) if re.match(r"(?i)\s*rem\b", line) and "%" in line], (
        "cmd expands % even on rem lines"
    )


def test_cmd_keeps_the_registry_path_out_of_call_arguments() -> None:
    """A quoted registry entry ("C:\\Program Files\\x") would split `call :x "%%B"` at its blank."""
    code = _code_lines_cmd()
    assert not [line for line in code if re.match(r'(?i)call\s+:uv_in_list\s+\S', line)], code
    assert any(line.lower().startswith('set "pt_list=%pt_list:"=%"') for line in code), "quotes are not removed"
    uv_line = next(line for line in code if "%*" in line)
    before = code[: code.index(uv_line)]
    for name in sorted(CLEARED):  # they must not choose, break or move the runner's Python
        assert f'set "{name}="' in before, name


def test_cmd_echo_in_a_for_f_command_has_no_redirection() -> None:
    """cmd's echo prints the blank before a redirection: `echo "LIST" 2>nul` in a for /f command
    gave `"LIST" `, whose closing quote %%~L then kept, and the FOR set built from the list had an
    unbalanced quote: ": was unexpected at this time.", exit 255, for everyone whose uv is not on
    the console's PATH (no uv from the registry, no install hints). Windows-only behaviour, so
    the rule is static (test_cmd_registry_path_with_quoted_entries runs it on Windows)."""
    commands = [m.group(1) for line in _code_lines_cmd() for m in re.finditer(r"(?i)\bin\s*\('(echo\b[^']*)'\)", line)]
    assert commands, "the registry list is no longer expanded by an echo in a for /f command"
    assert not [c for c in commands if ">" in c], commands


def test_cmd_takes_the_exit_code_on_the_line_after_uv() -> None:
    """Nothing follows the argument list on the uv line: an argument with an odd number of double
    quotes (a `"` CreateProcess escapes as `\\"`) swallows the rest of that line, which then
    reached uv as arguments. The exit code is taken on the next line: a bare `exit /b` after uv
    on the same line gave every cmd /c caller (VS Code tasks, Python, xonsh, nushell) exit 0.
    cmd opens the file again for that line, so pyt.cmd names itself for the runner, whose
    uninstall and install never delete or replace it under cmd (cmd_install.run_by_cmd)."""
    code = _code_lines_cmd()
    uv_line = next(line for line in code if "%*" in line)
    assert uv_line.endswith("%*"), uv_line
    assert code[code.index(uv_line) + 1] == "exit /b %ERRORLEVEL%"
    assert 'set "PYTEMPLATE_LAUNCHER_FILE=%~f0"' in code


# --- pyt.ps1: static ------------------------------------------------------------------------


def test_ps1_is_ascii_lf_without_bom() -> None:
    data = PS1.read_bytes()
    assert data.isascii(), "pyt.ps1 must be ASCII (no BOM: xonsh and Unix kernels read the shebang)"
    assert b"\r" not in data, "pyt.ps1 needs LF line endings"
    assert data.startswith(b"#!/usr/bin/env pwsh\n")


def test_ps1_has_no_param_block_and_leaves_path_alone() -> None:
    text = PS1.read_text(encoding="ascii")
    assert not re.search(r"(?im)^\s*(param\s*\(|\[CmdletBinding)", text), "a param() block turns -v, -h, -q into parameters"
    assert not re.search(r"(?i)\$env:path\s*\+?=(?!=)", text)
    assert not re.search(r"(?i)SetEnvironmentVariable\(\s*['\"]path['\"]|(Set|New|Remove)-Item\s+\S*env:path\b", text)


def test_every_launcher_starts_the_runner_on_the_same_python() -> None:
    """pyt, pyt.cmd, pyt.ps1 and the Neovim plugin start the runner the same way (CLAUDE.md 4.1):
    `uv run --python=REQUEST --python-preference PREF --script`: while the project has an
    environment (.venv, or .venv-wsl on POSIX) REQUEST is empty (no request: uv follows
    .python-version) and PREF only-managed, else ">=3.11" and managed (project.launcher_python;
    test_launcher_sh runs pyt and pyt.ps1 against it). One word, --python=: Windows PowerShell 5.1
    drops an empty argument."""
    sh = (ROOT / "pyt").read_text(encoding="ascii")
    cmd = CMD.read_bytes().decode("ascii")
    ps1 = PS1.read_text(encoding="ascii")
    lua = (ROOT / ".pytemplate" / "nvim" / "lua" / "pytemplate" / "init.lua").read_text(encoding="utf-8")
    for name, text in (("pyt", sh), ("pyt.cmd", cmd), ("pyt.ps1", ps1), ("init.lua", lua)):
        assert ">=3.11" in text and "--python-preference" in text and "only-managed" in text, name
    assert "_pt_py='>=3.11'" in sh and "_pt_pref=managed" in sh and "_pt_pref=only-managed" in sh
    assert '"--python=$_pt_py" --python-preference "$_pt_pref" --script' in sh
    assert ".venv/Scripts/python.exe" in sh and ".venv-wsl/bin/python" in sh and ".venv/bin/python" in sh
    assert 'set "PT_PY=>=3.11"' in cmd and 'if exist "%PT_ROOT%.venv\\Scripts\\python.exe" set "PT_PY="\r\n' in cmd
    assert 'set "PT_PREF=managed"' in cmd and 'if exist "%PT_ROOT%.venv\\Scripts\\python.exe" set "PT_PREF=only-managed"' in cmd
    assert "'.venv\\Scripts\\python.exe'" in ps1 and "'.venv-wsl/bin/python', '.venv/bin/python'" in ps1
    assert "'--python-preference', $preference" in ps1 and ps1.count('"--python=$python"') == 3
    assert "$python = ''; $preference = 'only-managed'" in ps1 and ps1.count("--python-preference $preference") == 2
    assert '"--python=" .. (env and "" or ">=3.11")' in lua and 'env and "only-managed" or "managed"' in lua


def test_ps1_restores_every_variable_it_sets() -> None:
    text = PS1.read_text(encoding="ascii")
    assigned = {m.upper() for m in re.findall(r"(?i)\$env:(\w+)\s*=(?!=)", text)}
    removed = {m.upper() for line in re.findall(r"(?im)^\s*Remove-Item\s+-LiteralPath\s+(Env:.*)$", text) for m in re.findall(r"Env:(\w+)", line)}
    names = re.search(r"(?m)^\$names = (.+)$", text)
    assert names, "pyt.ps1 lists the variables it restores in `$names = ...`"
    restored = {m.upper() for m in re.findall(r"'(\w+)'", names[1])}
    assert assigned and assigned <= restored, f"set but not restored: {assigned - restored}"
    assert removed == CLEARED and removed <= restored, f"removed but not restored: {removed - restored}"


def test_ps1_never_names_the_pipeline_variable() -> None:
    """A script whose text uses the automatic pipeline variable makes `pwsh -File` (and the shebang
    route) read a redirected stdin as text lines (IsUsingDollarInput): the bytes would change."""
    assert "$input" not in PS1.read_text(encoding="ascii").lower()


def test_ps1_is_executable_in_git() -> None:
    git = shutil.which("git")
    if not git:
        pytest.skip("git not installed")
    r = subprocess.run([git, "ls-files", "-s", "--", "pyt.ps1"], cwd=ROOT, capture_output=True, text=True, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("pyt.ps1 is not tracked by git here")
    assert r.stdout.split()[0] == "100755", "git update-index --chmod=+x pyt.ps1 (for ./pyt.ps1 on Linux/macOS)"


def _powershells() -> list[str]:
    return [exe for exe in (shutil.which("pwsh"), shutil.which("powershell") if IS_WINDOWS else None) if exe]


def _ps_exe(name: str) -> str:
    """pwsh on every OS, Windows PowerShell 5.1 only on Windows; skip when it is not installed."""
    exe = shutil.which(name) if name == "pwsh" or IS_WINDOWS else None
    if not exe:
        pytest.skip(f"{name} not installed")
    return exe


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_parses(name: str) -> None:
    exe = _ps_exe(name)
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


# --- behaviour ---------------------------------------------------------------------------------


def _clean_env(**changes: str | None) -> dict[str, str]:
    """This environment without what `uv run` (pytest's parent) sets, so the launchers search for uv."""
    drop = {"UV", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "UV_PYTHON_PREFERENCE", "UV_RUN_RECURSION_DEPTH"}
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
        text=True, encoding="utf-8", errors="replace", timeout=180, check=False,
    )


def _session(exe: str, body: str, cwd: Path = ROOT, env: dict[str, str] | None = None, stdin: str = "") -> subprocess.CompletedProcess[str]:
    """Run PowerShell code in one session (a caller typing in its own shell)."""
    return _run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", _encoded(body)], cwd, env, stdin)


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


INSTALL_HINT = "To run pyt outside a project, install it"


def _nothing_installed(tmp: Path) -> dict[str, str]:
    """Where the launchers look for the installed template (pyt install), moved to an empty
    folder: a real installed pyt of the user never answers a test that expects no project."""
    return {"XDG_DATA_HOME": str(tmp / "no-data"), "LOCALAPPDATA": str(tmp / "no-data")}


def _runner_copy(dest: Path) -> Path:
    """.pytemplate/pyt.py and the runner (enough for __probe) in `dest`."""
    (dest / ".pytemplate").mkdir(parents=True)
    shutil.copyfile(ROOT / ".pytemplate" / "pyt.py", dest / ".pytemplate" / "pyt.py")
    shutil.copytree(ROOT / ".pytemplate" / "runner", dest / ".pytemplate" / "runner", ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def _outside(tmp: Path, launcher: Path) -> tuple[Path, Path]:
    """A copy of `launcher` in a bin folder (where pyt install puts it), and a folder outside
    any project to run it from."""
    (tmp / "bin").mkdir(exist_ok=True)
    copy = tmp / "bin" / launcher.name
    shutil.copyfile(launcher, copy)
    (tmp / "away").mkdir(exist_ok=True)
    return copy, tmp / "away"


def _uv_dirs() -> dict[str, str]:
    """uv's cache and Python folders as this environment resolves them: with LOCALAPPDATA or
    XDG_DATA_HOME moved, uv would start from empty ones (and download CPython)."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    found: dict[str, str] = {}
    for key, args in (("UV_CACHE_DIR", ["cache", "dir"]), ("UV_PYTHON_INSTALL_DIR", ["python", "dir"])):
        r = subprocess.run([uv, *args], env=_clean_env(), capture_output=True, text=True, timeout=60, check=False)
        lines = r.stdout.strip().splitlines()
        if r.returncode != 0 or not lines:
            pytest.skip(f"uv {' '.join(args)} failed: {r.stderr.strip()}")
        found[key] = lines[-1]
    return found


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
    if IS_WINDOWS:
        return _hidden_env(tmp, str(tmp / "bin"))
    return {"PATH": str(tmp / "bin"), "HOME": str(tmp)}


def _assert_hints(r: subprocess.CompletedProcess[str]) -> None:
    assert r.returncode == 127, (r.returncode, r.stdout, r.stderr)
    assert "PTPROBE" not in r.stdout
    if IS_WINDOWS:
        for hint in ("irm https://astral.sh/uv/install.ps1 | iex", "winget install --id=astral-sh.uv -e", "scoop install main/uv"):
            assert hint in r.stderr, r.stderr
        assert "curl" not in r.stderr
    else:
        for hint in ("curl -LsSf https://astral.sh/uv/install.sh | sh", "brew install uv", "pipx install uv"):
            assert hint in r.stderr, r.stderr
        assert "winget" not in r.stderr


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


# --- pyt.cmd (Windows) ------------------------------------------------------------------------


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
    copy = tmp_path / "pyt.cmd"
    shutil.copyfile(CMD, copy)
    r = _run([str(copy), "__probe", "0", "0", "w"], SUB)
    assert r.returncode == 0, r.stderr
    _check(_probes(r)[0], SUB, "cmd", ["w"])
    r = _run([str(copy), "__probe", "0", "0"], tmp_path, _clean_env(**_nothing_installed(tmp_path)))
    assert r.returncode == 2 and "no .pytemplate" in r.stderr and INSTALL_HINT in r.stderr, (r.returncode, r.stderr)


@windows_only
def test_cmd_never_hands_a_project_the_global_mode() -> None:
    """A PYTEMPLATE_GLOBAL=1 of the caller (a stale export) never turns a project's runner into
    the installed template's global mode."""
    r = _run([str(CMD), "__probe", "0", "0", "g"], ROOT, {**_clean_env(), "PYTEMPLATE_GLOBAL": "1"})
    assert r.returncode == 0, r.stderr
    p = _probes(r)[0]
    _check(p, ROOT, "cmd", ["g"])
    assert p["global"] == "", p


@windows_only
def test_cmd_outside_a_project_runs_the_installed_template(tmp_path: Path) -> None:
    """No project: the installed template (pyt install) in %LOCALAPPDATA%\\pytemplate\\template,
    else below %USERPROFILE%\\AppData\\Local, in its global mode; with neither variable no folder
    is named (never one below the current drive root, where any user may create folders)."""
    installed = _runner_copy(tmp_path / "local" / "pytemplate" / "template")
    home = tmp_path / "home"
    by_profile = _runner_copy(home / "AppData" / "Local" / "pytemplate" / "template")
    launcher, away = _outside(tmp_path, CMD)
    uv = _uv_dirs()
    for env, root in (
        (_clean_env(LOCALAPPDATA=str(tmp_path / "local"), **uv), installed),
        (_clean_env(LOCALAPPDATA=None, USERPROFILE=str(home), **uv), by_profile),
    ):
        r = _run([str(launcher), "__probe", "5", "0", "x"], away, env)
        assert r.returncode == 5, (r.stdout, r.stderr)
        p = _probes(r)[0]
        assert _same(p["root"], root) and p["global"] == "1" and p["launcher"] == "cmd" and p["argv"] == ["x"], p
        assert _same(p["caller_cwd"], away), p
    r = _run([str(launcher), "__probe", "5", "0", "x"], away, _clean_env(LOCALAPPDATA=None, USERPROFILE=None))
    assert r.returncode == 2 and INSTALL_HINT in r.stderr and "PTPROBE" not in r.stdout, (r.stdout, r.stderr)


# What the runner does while cmd runs pyt.cmd, in `pyt uninstall` and `pyt install`
# A runner that does to the pyt.cmd running it what pyt uninstall does (PT_CHANGE=retired: puts
# cmd_install.self_deleting in its place, PT_STAND_IN) or what nothing may do (deleted), and
# says whether pyt.cmd named itself (PYTEMPLATE_LAUNCHER_FILE) before the change
REWRITE_LAUNCHER = """import os, sys
from pathlib import Path
target = Path(os.environ["PT_TARGET"])
try:
    named = os.path.samefile(os.environ["PYTEMPLATE_LAUNCHER_FILE"], target)
except (KeyError, OSError):
    named = False
print("NAMED=" + ("yes" if named else "no"))
if os.environ["PT_CHANGE"] == "deleted":
    target.unlink()
else:
    target.write_bytes(Path(os.environ["PT_STAND_IN"]).read_bytes())
sys.exit(7)
"""


@windows_only
@pytest.mark.parametrize("change", ["retired", "deleted"])
def test_cmd_goes_on_reading_what_uninstall_leaves(change: str, tmp_path: Path) -> None:
    """cmd reads a batch file one line at a time, opening it again by name: a pyt.cmd deleted
    while it ran made cmd say "The batch file cannot be found." (exit 1). pyt uninstall leaves
    cmd_install.self_deleting in its place instead, and cmd reads on there: the file deletes
    itself and the exit code stays the runner's. pyt.cmd names itself for the runner."""
    entry = tmp_path / "local" / "pytemplate" / "template" / ".pytemplate" / "pyt.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(REWRITE_LAUNCHER, encoding="utf-8", newline="\n")
    launcher, away = _outside(tmp_path, CMD)
    stand_in = tmp_path / "stand-in.cmd"
    data = cmd_install.self_deleting(launcher.read_bytes())
    assert data is not None
    stand_in.write_bytes(data)
    env = _clean_env(LOCALAPPDATA=str(tmp_path / "local"), PT_TARGET=str(launcher), PT_CHANGE=change, PT_STAND_IN=str(stand_in), **_uv_dirs())
    r = _run([str(launcher), "x"], away, env)
    out = r.stdout + r.stderr
    assert "NAMED=yes" in r.stdout.splitlines(), out
    if change == "deleted":  # what the stand-in avoids (a canary: cmd reads the file again)
        assert "cannot be found" in out, (r.returncode, out)
        return
    assert r.returncode == 7 and "cannot be found" not in out, (r.returncode, out)
    assert not launcher.exists()


@windows_only
def test_cmd_finds_uv_outside_path(tmp_path: Path) -> None:
    _check_uv_outside_path([str(CMD)], tmp_path, "cmd")


@windows_only
def test_cmd_prints_install_hints_without_uv(tmp_path: Path) -> None:
    _assert_hints(_run([str(CMD), "__probe", "0", "0"], ROOT, _no_uv_env(tmp_path)))


@windows_only
def test_cmd_hands_the_runner_only_its_own_variables(tmp_path: Path) -> None:
    project = tmp_path / "p"
    (project / ".pytemplate").mkdir(parents=True)
    (project / ".pytemplate" / "pyt.py").write_text(
        "import json, os\nprint('ENV' + json.dumps({k.upper(): v for k, v in os.environ.items() if k.upper().startswith(('PT_', 'PYTEMPLATE_'))}))\n",
        encoding="utf-8", newline="\n",
    )
    shutil.copyfile(CMD, project / "pyt.cmd")
    env = _clean_env()
    before = {k.upper() for k in env if k.upper().startswith("PT_")}
    r = _run([str(project / "pyt.cmd")], project, env)
    seen = [json.loads(line[3:]) for line in r.stdout.splitlines() if line.startswith("ENV")]
    assert r.returncode == 0 and len(seen) == 1, r.stdout + r.stderr
    assert set(seen[0]) == before | {"PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER", "PYTEMPLATE_LAUNCHER_FILE"}
    assert seen[0]["PYTEMPLATE_LAUNCHER"] == "cmd" and _same(seen[0]["PYTEMPLATE_CALLER_CWD"], project)
    assert _same(seen[0]["PYTEMPLATE_LAUNCHER_FILE"], project / "pyt.cmd")


@windows_only
def test_cmd_clears_the_callers_uv_python(tmp_path: Path) -> None:
    """A UV_PYTHON (here a missing interpreter, which uv would refuse) never picks the runner's
    Python; a PYTHONHOME or PYTHONPATH never breaks it, a UV_WORKING_DIR never moves it, and a
    UV_MANAGED_PYTHON or UV_NO_MANAGED_PYTHON never stops uv (refused next to --python-preference)."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "tomllib.py").write_text('raise SystemExit("shadowed tomllib")\n', encoding="utf-8")
    (tmp_path / "elsewhere").mkdir()
    env = _clean_env(
        UV_PYTHON=str(tmp_path / "no" / "python.exe"), PYTHONHOME=str(tmp_path / "no-home"), PYTHONPATH=str(shadow),
        UV_WORKING_DIR=str(tmp_path / "elsewhere"), UV_MANAGED_PYTHON="1", UV_NO_MANAGED_PYTHON="1",
    )  # fmt: skip
    r = _run([str(CMD), "__probe", "0", "0", "x"], SUB, env)
    assert r.returncode == 0, r.stdout + r.stderr
    _check(_probes(r)[0], SUB, "cmd", ["x"])


def _fake_reg(tmp: Path, value: str) -> Path:
    """A reg.cmd that prints a `reg query` answer (CRLF) with `value` as the Path: PATH = only
    this folder, so for /f runs it and the real registry is never read."""
    fake = tmp / "fake"
    fake.mkdir(exist_ok=True)
    (fake / "reg.cmd").write_bytes(b'@type "%~dp0reg.txt"\r\n')
    (fake / "reg.txt").write_bytes(f"\r\nHKEY_CURRENT_USER\\Environment\r\n    Path    REG_EXPAND_SZ    {value}\r\n\r\n".encode("ascii"))
    return fake


@windows_only
def test_cmd_registry_path_with_quoted_entries(tmp_path: Path) -> None:
    """Quoted registry entries ("C:\\Program Files\\x") neither split the list nor hide uv."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    qdir = tmp_path / "q dir"
    qdir.mkdir()
    shutil.copyfile(uv, qdir / "uv.exe")
    fake = _fake_reg(tmp_path, '"C:\\no such dir\\x";"%PT_Q%";C:\\after')
    env = {**_hidden_env(tmp_path, str(fake)), "PT_Q": str(qdir)}
    r = _run([str(CMD), "__probe", "0", "0", "q"], ROOT, env)
    assert r.returncode == 0, r.stdout + r.stderr
    _check(_probes(r)[0], ROOT, "cmd", ["q"])
    (fake / "reg.txt").write_bytes(b'\r\nHKEY_CURRENT_USER\\Environment\r\n    Path    REG_EXPAND_SZ    "C:\\Program Files (x86)\\nope";C:\\x\r\n\r\n')
    _assert_hints(_run([str(CMD), "__probe", "0", "0"], ROOT, _hidden_env(tmp_path, str(fake))))


@windows_only
def test_cmd_finds_uv_in_a_plain_registry_entry(tmp_path: Path) -> None:
    """The registry fallback on its own: a plain absolute entry, no quotes, no variable; then a
    variable whose value brings quotes of its own (a quoted JAVA_HOME) into a folder with blanks
    and parentheses. The echo that expands the list carried a redirection, whose blank kept the
    closing quote: every registry Path broke the FOR set built from it (": was unexpected at this
    time.", exit 255), and neither uv nor the install hints were ever reached."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    plain = tmp_path / "plain"
    plain.mkdir()
    shutil.copyfile(uv, plain / "uv.exe")
    fake = _fake_reg(tmp_path, str(plain))
    env = _hidden_env(tmp_path, str(fake))
    r = _run([str(CMD), "__probe", "0", "0", "p"], ROOT, env)
    assert r.returncode == 0, r.stdout + r.stderr
    _check(_probes(r)[0], ROOT, "cmd", ["p"])
    quoted = tmp_path / "q dir (x86)"
    quoted.mkdir()
    shutil.copyfile(uv, quoted / "uv.exe")
    (fake / "reg.txt").write_bytes(b"\r\nHKEY_CURRENT_USER\\Environment\r\n    Path    REG_EXPAND_SZ    C:\\nope;%PT_QV%\r\n\r\n")
    r = _run([str(CMD), "__probe", "0", "0", "q"], ROOT, {**env, "PT_QV": f'"{quoted}"'})
    assert r.returncode == 0, r.stdout + r.stderr
    _check(_probes(r)[0], ROOT, "cmd", ["q"])


@windows_only
@pytest.mark.parametrize("entry", ["%PT_UNDEFINED%uvdir", "%PT_UNDEFINED%\\uvdir", "uvdir", ".\\uvdir"])
def test_cmd_never_takes_uv_from_a_registry_entry_that_is_not_absolute(entry: str, tmp_path: Path) -> None:
    """`call set`, in a batch file, removed a variable that is not defined: an entry of an
    undefined JAVA_HOME, %JAVA_HOME%\\bin, became \\bin, a folder of the drive root any user
    may create, and pyt.cmd ran the uv.exe there (here uvdir, below the current folder, and
    \\uvdir, below the drive root, which the test never creates). Windows keeps such an entry as
    it is, and so does pyt.cmd now; an entry that is not absolute is never probed."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    work = tmp_path / "work"
    (work / "uvdir").mkdir(parents=True)
    shutil.copyfile(uv, work / "uvdir" / "uv.exe")
    fake = _fake_reg(tmp_path, f"{entry};C:\\nope")
    env = _hidden_env(tmp_path, str(fake))
    env.pop("PT_UNDEFINED", None)
    _assert_hints(_run([str(CMD), "__probe", "0", "0"], work, env))
    # An absolute entry of the same folder is found: only the rule keeps uv out above.
    (fake / "reg.txt").write_bytes(f"\r\nHKEY_CURRENT_USER\\Environment\r\n    Path    REG_EXPAND_SZ    %PT_UNDEFINED%x;{work / 'uvdir'}\r\n\r\n".encode("ascii"))
    r = _run([str(CMD), "__probe", "0", "0", "u"], work, env)
    assert r.returncode == 0, r.stdout + r.stderr
    _check(_probes(r)[0], work, "cmd", ["u"])


# --- pyt.ps1 (pwsh everywhere, Windows PowerShell 5.1 on Windows) ---------------------------------


def _ps_session(launcher: str, legacy: bool) -> str:
    args = " ".join(a if a == "-v" else _ps_literal(a) for a in PS_ARGS)
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


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_reached_through_a_symlink_finds_its_project(name: str, tmp_path: Path) -> None:
    """A link to pyt.ps1 in a folder on PATH (~/bin/mypyt.ps1 -> proj/pyt.ps1), run from
    outside the project: $PSScriptRoot is the link's folder, and the launcher said there was no
    .pytemplate/pyt.py next to it (exit 2). The link is followed to the file it names."""
    exe = _ps_exe(name)
    bindir, chain = tmp_path / "bin", tmp_path / "chain"
    links = {"absolute": bindir / "mypyt.ps1", "relative, to a link": chain / "rel.ps1"}
    try:
        for folder in (bindir, chain):
            folder.mkdir()
        links["absolute"].symlink_to(PS1)
        links["relative, to a link"].symlink_to(Path("..") / "bin" / "mypyt.ps1")
        if not IS_WINDOWS:  # a relative target seen through a symlinked folder: the kernel's '..'
            tools = tmp_path / "tools" / "bin"
            tools.mkdir(parents=True)
            (tmp_path / "tools" / "proj").symlink_to(ROOT, target_is_directory=True)
            (tools / "mypyt.ps1").symlink_to(Path("..") / "proj" / "pyt.ps1")
            (tmp_path / "home").mkdir()
            (tmp_path / "home" / "bin").symlink_to(tools, target_is_directory=True)
            links["relative, in a linked folder"] = tmp_path / "home" / "bin" / "mypyt.ps1"
            # The link's folder reached /bin/sh unquoted, which PowerShell 7 globs: b[0-9] read as b1.
            (tmp_path / "home" / "b[0-9]").symlink_to(tools, target_is_directory=True)
            (tmp_path / "home" / "b1").mkdir()
            links["relative, in a linked folder named like a glob"] = tmp_path / "home" / "b[0-9]" / "mypyt.ps1"
    except OSError as e:  # Windows without Developer Mode or admin rights
        pytest.skip(f"cannot create a symlink here: {e}")
    away = tmp_path / "away"
    away.mkdir()
    for how, link in links.items():
        r = _session(exe, f"& {_ps_literal(str(link))} __probe 5 0 x\nexit $LASTEXITCODE", away)
        assert r.returncode == 5, (how, r.stdout, r.stderr)
        _check(_probes(r)[0], away, "ps1:", ["x"])


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_round_trip_in_a_session(name: str) -> None:
    exe = _ps_exe(name)
    legacy = name == "pwsh"  # also with $PSNativeCommandArgumentPassing = 'Legacy' (5.1 always is)
    launcher = os.path.relpath(PS1, SUB)
    body = _ps_session(launcher if launcher.startswith(".") else ".\\" + launcher, legacy)
    r = _session(exe, body, SUB)
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
    assert not [ln for ln in r.stdout.splitlines() if "PWNED" in ln and not ln.startswith("PTPROBE")], r.stdout


def _injection_args() -> list[str]:
    """Arguments that would run code if the Core hand-over (Invoke-Expression of single-quoted
    words) ever quoted them wrong, plus the characters PowerShell or a command line treat specially."""
    payloads = [f"{q}; Write-Output PWNED; {q}" for q in QUOTES] + [f"x{q}+(Write-Output PWNED)+{q}" for q in QUOTES]
    return [
        *QUOTES, "''", "a'b", "\u2018\u2019\u201a\u201b", *payloads,
        "$(Write-Output PWNED)", "${env:PATH}", "`$x", "``", "a`nb", "a\nb", "a\r\nb", "tab\there",
        "\u2028", "\u2029", "\u0085", "\u200b", "\u00a0", "\U0001f600",
        "; Write-Output PWNED", "| Write-Output PWNED", "& Write-Output PWNED", "&& Write-Output PWNED",
        "@args", "-Command", "-v", "[a]", "{", "}", "(", ")", "#", "<#", "#>", "$null", "$true", "1,2", "@(1)",
        "-", "---", "--x", "", "", '"', '""', '\\"', "tail\\", "tail\\\\", 'a\\\\\\"b', "*", "?", "~", "~/x", "%PATH%",
        "x" * (8000 if IS_WINDOWS else 100000),
    ]  # fmt: skip


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_hand_over_is_injection_safe(name: str, tmp_path: Path) -> None:
    exe = _ps_exe(name)
    args = _injection_args()
    data = tmp_path / "args.json"
    data.write_text(json.dumps(args), encoding="utf-8")
    ps1 = _ps_literal(str(PS1))
    body = (
        f"$l = @(Get-Content -Raw -Encoding UTF8 -LiteralPath {_ps_literal(str(data))} | ConvertFrom-Json)\n"
        f"& {ps1} __probe 0 0 @l\n'RC=' + $LASTEXITCODE\n"
    )
    if name == "pwsh":  # the legacy pre-quoting path (5.1 always takes it)
        body += f"$PSNativeCommandArgumentPassing = 'Legacy'\n& {ps1} __probe 0 0 @l\n'RC=' + $LASTEXITCODE\n"
    r = _session(exe, body + "exit 0\n")
    probes = _probes(r)
    assert len(probes) == (2 if name == "pwsh" else 1), r.stdout + r.stderr
    for p in probes:
        got = p["argv"]
        assert isinstance(got, list) and len(got) == len(args), (len(got) if isinstance(got, list) else got, len(args))
        assert [(i, a) for i, (a, g) in enumerate(zip(args, got, strict=True)) if a != g] == []
    leaked = [ln for ln in (r.stdout + r.stderr).splitlines() if "PWNED" in ln and not ln.startswith("PTPROBE")]
    assert not leaked, leaked
    assert [ln for ln in r.stdout.splitlines() if ln.startswith("RC=")] == ["RC=0"] * len(probes)


# Any word an argv can hold (Unicode without NUL), mixed with the ones known to be hard.
PS_WORDS = st.one_of(st.sampled_from([*PS_ARGS, *QUOTES, "--%"]), st.text(st.characters(codec="utf-8", exclude_characters="\x00")))


@pytest.mark.parametrize("name", PS_NAMES)
@settings(max_examples=max(2, settings.default.max_examples // 50))  # a PowerShell start each
@given(args=st.lists(PS_WORDS.filter(lambda a: a != "--"), min_size=1, max_size=50))  # PowerShell removes a bare --
def test_ps1_hands_any_arguments_to_the_runner_unchanged(name: str, args: list[str]) -> None:
    """Splatted words at random reach the runner as they are, through the Core hand-over and the
    legacy pre-quoting alike (pwsh with $PSNativeCommandArgumentPassing = 'Legacy'; 5.1 always)."""
    exe = _ps_exe(name)
    with tempfile.TemporaryDirectory() as tmp:  # not tmp_path: one file per example
        data = Path(tmp) / "args.json"
        data.write_text(json.dumps(args), encoding="utf-8")
        call = f"& {_ps_literal(str(PS1))} __probe 0 0 @l\n'RC=' + $LASTEXITCODE\n"
        body = f"$l = @(Get-Content -Raw -Encoding UTF8 -LiteralPath {_ps_literal(str(data))} | ConvertFrom-Json)\n{call}"
        if name == "pwsh":
            body += f"$PSNativeCommandArgumentPassing = 'Legacy'\n{call}"
        r = _session(exe, body + "exit 0\n")
    runs = 2 if name == "pwsh" else 1
    assert [p["argv"] for p in _probes(r)] == [args] * runs, r.stdout + r.stderr
    assert [ln for ln in r.stdout.splitlines() if ln.startswith("RC=")] == ["RC=0"] * runs


STOP_PARSING_ARGS = ["x", "--%", "a b", "%PATH%", "$HOME", "", 'q"x', "*", "~"]


@pytest.mark.parametrize("mode", ["", "Standard", "Windows", "Legacy"])
def test_ps1_passes_a_literal_stop_parsing_token(mode: str) -> None:
    """PowerShell 7.3+ takes any native argument equal to --% for its stop-parsing token (then splits
    and %VAR%-expands the rest): the launcher passes it like any other word."""
    exe = _ps_exe("pwsh")
    args = " ".join(_ps_literal(a) for a in STOP_PARSING_ARGS)
    body = "\n".join([
        f"$PSNativeCommandArgumentPassing = '{mode}'" if mode else "",
        "$before = [string]$PSNativeCommandArgumentPassing",
        f"& {_ps_literal(str(PS1))} __probe 0 0 {args}",
        "'RC=' + $LASTEXITCODE",
        "if ([string]$PSNativeCommandArgumentPassing -ne $before) { 'LEAKED=' + $PSNativeCommandArgumentPassing }",
        "exit 0",
    ])  # fmt: skip
    r = _session(exe, body)
    assert r.returncode == 0 and "RC=0" in r.stdout, r.stdout + r.stderr
    assert _probes(r)[0]["argv"] == STOP_PARSING_ARGS
    assert "LEAKED=" not in r.stdout


COLON_TYPED = "-X:utf8 -W:ignore::DeprecationWarning -m:x -X:\"a b\" -X:a,b '-X:' utf8 -X utf8 --add-data:x --d=a:b"
COLON_ARGV = ["-X:utf8", "-W:ignore::DeprecationWarning", "-m:x", "-X:a b", "-X:a,b", "-X:", "utf8", "-X", "utf8", "--add-data:x", "--d=a:b"]


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_keeps_typed_colon_arguments_whole(name: str) -> None:
    """A typed -X:v reaches a script split in two ('-X:' plus v): the launcher joins it again, as
    PowerShell does for a native program. A quoted '-X:' and a colon-less -X stay as typed."""
    exe = _ps_exe(name)
    body = f"& ./pyt.ps1 __probe 0 0 {COLON_TYPED}\n"
    if name == "pwsh":  # also the legacy pre-quoting path that 5.1 always takes
        body += f"$PSNativeCommandArgumentPassing = 'Legacy'\n& ./pyt.ps1 __probe 0 0 {COLON_TYPED}\n"
    r = _session(exe, body + "exit 0\n")
    assert r.returncode == 0, r.stdout + r.stderr
    probes = _probes(r)
    assert len(probes) == (2 if name == "pwsh" else 1), r.stdout
    for p in probes:
        assert p["argv"] == COLON_ARGV


COMMA_TYPED = "mode cpython --supports cpython,mypyc --opt=x,y a,b 'q,r' c, d +pypy,-mypyc --tests T1,T2 z"
COMMA_ARGV = ["mode", "cpython", "--supports", "cpython,mypyc", "--opt=x,y", "a,b", "q,r", "c,d", "+pypy,-mypyc", "--tests", "T1,T2", "z"]


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_keeps_typed_comma_lists_whole(name: str) -> None:
    """PowerShell hands a script a typed comma list (cpython,mypyc) as an array: the launcher joins
    it again, as PowerShell does for a native program, so the documented `--supports cpython,mypyc`
    and `--tests T1,T2` reach the runner as one argument. Also through a wrapper function that
    forwards them with @args."""
    exe = _ps_exe(name)
    body = f"& ./pyt.ps1 __probe 0 0 {COMMA_TYPED}\n{PWSH_WRAPPER}\nSet-Location {_ps_literal(str(SUB))}\npyt __probe 0 0 {COMMA_TYPED}\n"
    if name == "pwsh":  # also the legacy pre-quoting path that 5.1 always takes
        body += f"$PSNativeCommandArgumentPassing = 'Legacy'\n& {_ps_literal(str(PS1))} __probe 0 0 {COMMA_TYPED}\n"
    r = _session(exe, body + "exit 0\n")
    assert r.returncode == 0, r.stdout + r.stderr
    probes = _probes(r)
    assert len(probes) == (3 if name == "pwsh" else 2), r.stdout + r.stderr
    for p in probes:
        assert p["argv"] == COMMA_ARGV


# An array VALUE reaches a script exactly like a typed list (one array), but a native program gets
# its items as separate arguments: the launcher reads the text of the call to tell them apart. A
# List[string] is never a typed list.
VALUES_SETUP = "$files = 'a.py','b 2.py'; $list = New-Object 'Collections.Generic.List[string]'; $list.Add('l 1'); $list.Add('l2')"
VALUES_TYPED = "run a,b $files @files ('p','q') $list -X:c,d z"
VALUES_ARGV = ["run", "a,b", "a.py", "b 2.py", "a.py", "b 2.py", "p", "q", "l 1", "l2", "-X:c,d", "z"]
# A wrapper that splats a copy of $args: its arrays are values, split as for a native program
# (the typed a,b too, which reached the wrapper as one array)
COPY_ARGV = ["run", "a", "b", "a.py", "b 2.py", "a.py", "b 2.py", "p", "q", "l 1", "l2", "-X:c,d", "z"]
# ... where a native call even drops the typed -X: (PowerShell 7.6): the launcher keeps it
NATIVE_COPY_ARGV = [*COPY_ARGV[:-2], "c", "d", "z"]


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_passes_array_values_like_a_native_call(name: str) -> None:
    """`./pyt run $files` gives the app the items of $files as separate arguments, like a direct
    native call in the same session, while a typed a,b stays one argument; also when the call is
    forwarded with @args (a profile function, a wrapper that adds words of its own), and with
    legacy argument passing. A wrapper that splats a copy of $args gets what a native program
    gets through the same wrapper: the arrays' items one by one."""
    exe = _ps_exe(name)
    uv =os.environ.get("UV") or shutil.which("uv")
    assert uv
    ps1 = _ps_literal(str(PS1))
    direct = f"& {_ps_literal(uv)} run --quiet --script {_ps_literal(str(ROOT / '.pytemplate' / 'pyt.py'))}"
    body = "\n".join([
        VALUES_SETUP,
        f"{direct} __probe 0 0 {VALUES_TYPED}",
        f"& {ps1} __probe 0 0 {VALUES_TYPED}",
        PWSH_WRAPPER,
        f"Set-Location {_ps_literal(str(SUB))}",
        f"pyt __probe 0 0 {VALUES_TYPED}",
        f"function drun {{ & {ps1} __probe 0 0 @args }}",
        f"function outer {{ drun @args }}",
        f"outer {VALUES_TYPED}",
        f"function unread {{ $rest = $args; & {ps1} __probe 0 0 @rest }}",
        f"unread {VALUES_TYPED}",
        f"function unreadn {{ $rest = $args; {direct} __probe 0 0 @rest }}",
        f"unreadn {VALUES_TYPED}",
    ])  # fmt: skip
    if name == "pwsh":  # also the legacy pre-quoting path that 5.1 always takes
        body += f"\n$PSNativeCommandArgumentPassing = 'Legacy'\n& {ps1} __probe 0 0 {VALUES_TYPED}"
    r = _session(exe, body + "\nexit 0\n")
    assert r.returncode == 0, r.stdout + r.stderr
    probes = [p["argv"] for p in _probes(r)]
    assert len(probes) == (7 if name == "pwsh" else 6), r.stdout + r.stderr
    assert probes[0] == VALUES_ARGV, "a direct native call no longer passes these as expected"
    assert probes[1:4] == [VALUES_ARGV] * 3
    assert probes[4] == COPY_ARGV
    if name == "pwsh":  # the native call through the same wrapper, as measured with 7.6
        assert probes[5] == NATIVE_COPY_ARGV, "a native call through a wrapper's copy of $args no longer passes these as expected"
    if name == "pwsh":
        assert probes[6] == VALUES_ARGV


# A $null argument (an unset $env:X, an optional variable), the $null items of an array and a -X:
# whose value is $null never reach a native program; an empty string does (PowerShell 7.3+), and
# so does a List[string]'s null item, as an empty string.
NULLS_SETUP = "$none = $null; $withnull = 'a', $null, 'b'; $empty = ''; $list = New-Object 'Collections.Generic.List[string]'; $list.Add('l'); $list.Add($null)"
NULLS_TYPED = "x $none $env:PT_NOT_SET_ANYWHERE $withnull @withnull $empty $list -X:$none y"
NULLS_ARGV = ["x", "a", "b", "a", "b", "", "l", "", "y"]


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_drops_null_arguments_like_a_native_call(name: str) -> None:
    """`./pyt build $backend` with $backend unset gave the runner an empty argument ("unknown
    backend ''"), and `./pyt check $env:UNSET` "unrecognized arguments": a native call drops a
    $null. Also through a wrapper function, and with legacy argument passing (whose native
    calls drop empty strings as well: the launcher keeps those, as PowerShell 7.3+ does)."""
    exe = _ps_exe(name)
    uv =os.environ.get("UV") or shutil.which("uv")
    assert uv
    ps1 = _ps_literal(str(PS1))
    direct = f"& {_ps_literal(uv)} run --quiet --script {_ps_literal(str(ROOT / '.pytemplate' / 'pyt.py'))}"
    body = "\n".join([
        "Remove-Item Env:PT_NOT_SET_ANYWHERE -ErrorAction Ignore",
        NULLS_SETUP,
        f"{direct} __probe 0 0 {NULLS_TYPED}",
        f"& {ps1} __probe 0 0 {NULLS_TYPED}",
        PWSH_WRAPPER,
        f"Set-Location {_ps_literal(str(SUB))}",
        f"pyt __probe 0 0 {NULLS_TYPED}",
        "$PSNativeCommandArgumentPassing = 'Legacy'",
        f"& {ps1} __probe 0 0 {NULLS_TYPED}",
    ])  # fmt: skip
    r = _session(exe, body + "\nexit 0\n")
    assert r.returncode == 0, r.stdout + r.stderr
    probes = [p["argv"] for p in _probes(r)]
    assert len(probes) == 4, r.stdout + r.stderr
    if name == "pwsh":  # 5.1's native calls drop the empty strings too
        assert probes[0] == NULLS_ARGV, "a direct native call no longer passes these as expected"
    assert probes[1:] == [NULLS_ARGV] * 3, probes


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_forwards_pipeline_input_and_keeps_raw_stdin(name: str) -> None:
    """'x' | ./pyt.ps1 run gives uv the pipeline, like a native call; without a pipeline uv keeps
    the process stdin; @() gives it EOF. Exit codes still come through."""
    exe = _ps_exe(name)
    ps1 = _ps_literal(str(PS1))
    uv = os.environ.get("UV") or shutil.which("uv")
    assert uv
    direct = f"& {_ps_literal(uv)} run --quiet --script {_ps_literal(str(ROOT / '.pytemplate' / 'pyt.py'))}"
    body = "\n".join([
        "$OutputEncoding = New-Object System.Text.UTF8Encoding $false",
        f"& {ps1} __probe 0 1",
        f"'ping','two' | & {ps1} __probe 3 1",
        "'RC=' + $LASTEXITCODE",
        f"'ping','two' | {direct} __probe 0 1",
        f"@() | & {ps1} __probe 0 1",
        f"@() | {direct} __probe 0 1",
        "exit 0",
    ])  # fmt: skip
    r = _session(exe, body, stdin="WRONG\n")
    got = [p["stdin"] for p in _probes(r)]
    assert len(got) == 5 and got[0] == "WRONG", r.stdout + r.stderr
    # the pipelines reach uv exactly as they reach a direct native call...
    assert got[1] == got[2] and got[3] == got[4], r.stdout + r.stderr
    # ...where Windows PowerShell 5.1 (GitHub's runners) puts a BOM in front of the text, even of an
    # empty pipeline and whatever $OutputEncoding says; PowerShell 7 hands the text over as is
    bom = "\ufeff" if name == "powershell" else ""
    assert [got[1].removeprefix(bom), got[3].removeprefix(bom)] == ["ping", ""], r.stdout + r.stderr
    assert "RC=3" in r.stdout
    if name != "pwsh":
        return  # how Windows PowerShell 5.1 -File treats a redirected stdin is not asserted here
    # pwsh -File (and the shebang route): the raw bytes of a redirected stdin reach uv. A script
    # naming the pipeline variable would get them as re-encoded text lines (U+FFFD for \xe9).
    raw = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS1), "__probe", "0", "1"],
        cwd=ROOT, env=_clean_env(), input=b"caf\xe9\r\nlast", capture_output=True, timeout=180, check=False,
    )  # fmt: skip
    got = [json.loads(ln[len(b"PTPROBE"):]) for ln in raw.stdout.splitlines() if ln.startswith(b"PTPROBE")]
    assert got and got[0]["stdin"] == "caf\udce9", (raw.stdout, raw.stderr)


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_clears_the_callers_uv_python_and_restores_it(name: str, tmp_path: Path) -> None:
    """A UV_PYTHON (here a missing interpreter, which uv would refuse) never picks the runner's
    Python; the caller's session keeps its own value, or none."""
    exe = _ps_exe(name)
    ps1 = _ps_literal(str(PS1))
    missing = str(tmp_path / "no" / "python")
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "tomllib.py").write_text('raise SystemExit("shadowed tomllib")\n', encoding="utf-8")
    (tmp_path / "elsewhere").mkdir()
    values = {
        "UV_PYTHON": missing, "PYTHONHOME": str(tmp_path / "no-home"), "PYTHONPATH": str(shadow), "UV_WORKING_DIR": str(tmp_path / "elsewhere"),
        "UV_MANAGED_PYTHON": "1", "UV_NO_MANAGED_PYTHON": "1",
    }  # fmt: skip
    assert set(values) == CLEARED
    body = "\n".join([
        *[f"$env:{k} = {_ps_literal(v)}" for k, v in values.items()],
        f"& {ps1} __probe 0 0 x",
        "'RC=' + $LASTEXITCODE",
        *[f"'KEPT={k}=' + $env:{k}" for k in values],
        *[f"Remove-Item Env:{k}" for k in values],
        f"& {ps1} __probe 0 0 y",
        *[f"'EXISTS={k}=' + (Test-Path Env:{k})" for k in values],
        "exit 0",
    ])  # fmt: skip
    r = _session(exe, body, SUB)
    probes = _probes(r)
    assert [p["argv"] for p in probes] == [["x"], ["y"]], r.stdout + r.stderr
    for p in probes:
        _check(p, SUB, "ps1:")  # in the caller's folder, not UV_WORKING_DIR
    assert "RC=0" in r.stdout, r.stdout
    for k, v in values.items():
        assert f"KEPT={k}={v}" in r.stdout and f"EXISTS={k}=False" in r.stdout, r.stdout


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_constrained_language_gives_one_clear_error(name: str) -> None:
    """AppLocker/WDAC policies run unsigned scripts in ConstrainedLanguage mode, which blocks the
    launcher's .NET calls: one error that names pyt.cmd, exit 126, no cascade."""
    exe = _ps_exe(name)
    body = "\n".join([
        "$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'",
        f"$out = & {_ps_literal(str(PS1))} __probe 0 0 q 2>&1",
        "'RC=' + $LASTEXITCODE",
        "foreach ($o in $out) { 'OUT=' + $o }",
        "exit 0",
    ])  # fmt: skip
    r = _session(exe, body)
    out = [ln for ln in r.stdout.splitlines() if ln.startswith("OUT=")]
    assert "RC=126" in r.stdout, r.stdout + r.stderr
    assert len(out) == 1 and "ConstrainedLanguage" in out[0] and "pyt.cmd" in out[0], r.stdout
    assert "PTPROBE" not in r.stdout and "Cannot invoke method" not in r.stdout + r.stderr


def _any_powershell() -> str:
    exes = _powershells()
    if not exes:
        pytest.skip("no PowerShell installed")
    return exes[0]


def test_ps1_walks_up_and_passes_file_arguments(tmp_path: Path) -> None:
    exe = _any_powershell()
    copy = tmp_path / "pyt.ps1"
    shutil.copyfile(PS1, copy)
    base = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(copy), "__probe"]
    r = _run([*base, "5", "0", "a b", "", "tr\\"], SUB)
    assert r.returncode == 5, r.stderr
    _check(_probes(r)[0], SUB, "ps1:", ["a b", "", "tr\\"])
    r = _run([*base, "0", "0"], tmp_path, _clean_env(**_nothing_installed(tmp_path)))
    assert r.returncode == 2 and "no .pytemplate" in r.stderr and INSTALL_HINT in r.stderr, (r.returncode, r.stderr)


@pytest.mark.parametrize("name", PS_NAMES)
def test_ps1_outside_a_project_runs_the_installed_template(name: str, tmp_path: Path) -> None:
    """No project: the installed template (pyt install; %LOCALAPPDATA% on Windows, an absolute
    XDG_DATA_HOME elsewhere) runs in its global mode. Inside a project made before the launchers
    were renamed, its .pytemplate/deploy.py runs, never in global mode. The caller's own
    PYTEMPLATE_GLOBAL (a stale 1) comes back after each run, or stays absent."""
    exe = _ps_exe(name)
    data = tmp_path / "data"
    installed = _runner_copy(data / "pytemplate" / "template")
    old = _runner_copy(tmp_path / "old")
    (old / ".pytemplate" / "pyt.py").rename(old / ".pytemplate" / "deploy.py")
    (old / "src").mkdir()
    launcher, away = _outside(tmp_path, PS1)
    ps1 = _ps_literal(str(launcher))
    body = "\n".join([
        "$env:PYTEMPLATE_GLOBAL = '1'",  # a stale export: never for a project's runner
        f"& {ps1} __probe 5 0 x",
        "'RC=' + $LASTEXITCODE",
        f"Set-Location -LiteralPath {_ps_literal(str(old / 'src'))}",
        f"& {ps1} __probe 6 0 y",
        "'RC=' + $LASTEXITCODE",
        "'AFTER=' + $env:PYTEMPLATE_GLOBAL",
        "Remove-Item Env:PYTEMPLATE_GLOBAL",
        f"Set-Location -LiteralPath {_ps_literal(str(away))}",
        f"& {ps1} __probe 7 0 z",
        "'EXISTS=' + (Test-Path Env:PYTEMPLATE_GLOBAL)",
        "exit 0",
    ])  # fmt: skip
    r = _session(exe, body, away, _clean_env(XDG_DATA_HOME=str(data), LOCALAPPDATA=str(data), **_uv_dirs()))
    probes = _probes(r)
    assert [p["argv"] for p in probes] == [["x"], ["y"], ["z"]], r.stdout + r.stderr
    assert _same(probes[0]["root"], installed) and probes[0]["global"] == "1", probes[0]
    assert _same(probes[0]["caller_cwd"], away) and str(probes[0]["launcher"]).startswith("ps1:"), probes[0]
    assert _same(probes[1]["root"], old) and probes[1]["global"] == "", probes[1]
    assert _same(probes[2]["root"], installed) and probes[2]["global"] == "1", probes[2]
    lines = r.stdout.splitlines()
    assert [ln for ln in lines if ln.startswith("RC=")] == ["RC=5", "RC=6"], r.stdout
    assert "AFTER=1" in lines and "EXISTS=False" in lines, r.stdout
    empty = tmp_path / "empty"
    r = _session(exe, f"& {ps1} __probe 5 0 x\nexit $LASTEXITCODE", away, _clean_env(**_nothing_installed(empty)))
    assert r.returncode == 2 and INSTALL_HINT in r.stderr and "PTPROBE" not in r.stdout, (r.stdout, r.stderr)


@windows_only
def test_ps1_finds_uv_outside_path(tmp_path: Path) -> None:
    exe = _any_powershell()
    _check_uv_outside_path([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS1)], tmp_path, "ps1:")


def test_ps1_prints_install_hints_without_uv(tmp_path: Path) -> None:
    exe = _any_powershell()
    base = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    lines = PS1.read_text(encoding="ascii").splitlines()
    if IS_WINDOWS:
        # The registry PATH cannot be hidden from .NET without touching the registry: drop those
        # two lines from a copy (the copy finds the project by walking up from ROOT).
        registry = [line for line in lines if "GetEnvironmentVariable('Path'" in line]
        assert len(registry) == 2
        lines = [line for line in lines if line not in registry]
    elif any(Path(d, "uv").exists() for d in ("/opt/homebrew/bin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin")):
        pytest.skip("uv is installed in a system folder the launcher always searches")
    copy = tmp_path / "nouv" / "pyt.ps1"
    copy.parent.mkdir()
    copy.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    _assert_hints(_run([*base, str(copy), "__probe", "0", "0"], ROOT, _no_uv_env(tmp_path)))


@posix_only
def test_ps1_uv_that_cannot_start_gives_one_line(tmp_path: Path) -> None:
    """A uv that exists with its x bit but cannot run (a broken download): exit 126 and one line
    `pyt: cannot run <uv>: <reason>`, without the Invoke-Expression position text."""
    exe = _ps_exe("pwsh")
    broken = tmp_path / "uv"
    broken.write_bytes(b"\x00\x01garbage, not a program\n")
    broken.chmod(0o755)
    r = _run([exe, "-NoProfile", "-NonInteractive", "-File", str(PS1), "__probe", "0", "0"], ROOT, _clean_env(UV=str(broken), CI="1"))
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert r.returncode == 126, (r.returncode, r.stdout, r.stderr)
    assert len(lines) == 1 and lines[0].startswith(f"pyt: cannot run {broken}: "), r.stderr
    assert "At line:" not in r.stderr and "char:" not in r.stderr, r.stderr


@posix_only
def test_ps1_skips_a_uv_without_exec_bit(tmp_path: Path) -> None:
    """Like `test -x` in ./pyt: a uv without x bit ($UV, PATH, an install folder) is skipped."""
    exe = _ps_exe("pwsh")
    home = tmp_path / "home"
    broken = [tmp_path / "path" / "uv", home / ".local" / "bin" / "uv"]
    good = home / ".cargo" / "bin" / "uv"
    for f in [*broken, good]:
        f.parent.mkdir(parents=True)
        f.write_text('#!/bin/sh\necho "FAKE $0"\nexit 7\n', encoding="ascii", newline="\n")
        f.chmod(0o644 if f in broken else 0o755)
    drop = ("UV_INSTALL_DIR", "XDG_BIN_HOME", "XDG_DATA_HOME", "CARGO_HOME")
    env = _clean_env(HOME=str(home), PATH=f"{broken[0].parent}:/usr/bin:/bin", UV=str(broken[0]), CI="1", **dict.fromkeys(drop))
    r = _run([exe, "-NoProfile", "-NonInteractive", "-File", str(PS1), "x"], ROOT, env)
    assert (r.returncode, r.stdout.strip()) == (7, f"FAKE {good}"), (r.returncode, r.stdout, r.stderr)


@posix_only
def test_ps1_skips_a_uv_link_whose_target_is_gone(tmp_path: Path) -> None:
    """File.Exists is true for a symbolic link whose target is gone (an uninstalled uv's link:
    pipx, Homebrew, a hand-made one), and the x-bit check that then failed counted as passed: a
    stale link ($UV, first on PATH, in ~/.local/bin) won, and the hand-over stopped with exit
    126 where ./pyt goes on to the next uv. A uv must open."""
    exe = _ps_exe("pwsh")
    home = tmp_path / "home"
    stale = [tmp_path / "path" / "uv", home / ".local" / "bin" / "uv"]
    good = home / ".cargo" / "bin" / "uv"
    for f in [*stale, good]:
        f.parent.mkdir(parents=True)
    for f in stale:
        f.symlink_to(tmp_path / "gone" / "uv")
    good.write_text('#!/bin/sh\necho "FAKE $0"\nexit 7\n', encoding="ascii", newline="\n")
    good.chmod(0o755)
    drop = ("UV_INSTALL_DIR", "XDG_BIN_HOME", "XDG_DATA_HOME", "CARGO_HOME")
    env = _clean_env(HOME=str(home), PATH=f"{stale[0].parent}:/usr/bin:/bin", UV=str(stale[0]), CI="1", **dict.fromkeys(drop))
    r = _run([exe, "-NoProfile", "-NonInteractive", "-File", str(PS1), "x"], ROOT, env)
    assert (r.returncode, r.stdout.strip()) == (7, f"FAKE {good}"), (r.returncode, r.stdout, r.stderr)


def _stale_uv_link(tmp: Path) -> Path:
    """A uv.exe link whose target is gone, in a folder of its own (skips where Windows refuses
    to make a symbolic link: no Developer Mode and no administrator)."""
    stale = tmp / "stale"
    stale.mkdir()
    try:
        (stale / "uv.exe").symlink_to(tmp / "gone" / "uv.exe")
    except OSError as e:
        pytest.skip(f"cannot make a symbolic link here: {e}")
    return stale / "uv.exe"


@windows_only
@pytest.mark.parametrize("launcher", ["cmd", *PS_NAMES])
def test_a_uv_link_whose_target_is_gone_is_skipped_on_windows(launcher: str, tmp_path: Path) -> None:
    """A link left in WinGet's Links folder by an uninstalled uv passes `if exist` (pyt.cmd: UV
    and the PATH lookup too) and File.Exists (pyt.ps1): it won over the uv of an install folder,
    and the run failed. A uv must open."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    stale = _stale_uv_link(tmp_path)
    good = tmp_path / ".local" / "bin"
    good.mkdir(parents=True)
    shutil.copyfile(uv, good / "uv.exe")
    system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    env = {**_hidden_env(tmp_path, f"{stale.parent};{system}"), "UV": str(stale)}  # USERPROFILE: tmp_path
    if launcher == "cmd":
        r = _run([str(CMD), "__probe", "0", "0", "s"], ROOT, env)
    else:
        argv = [_ps_exe(launcher), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS1), "__probe", "0", "0", "s"]
        r = _run(argv, ROOT, env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    _check(_probes(r)[0], ROOT, launcher if launcher == "cmd" else "ps1:", ["s"])
