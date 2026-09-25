"""Tests for the POSIX launcher ./deploy (run them with `./deploy selftest`).

Three layers:
  * static checks that run everywhere: shebang, LF, ASCII, git mode 100755 and a lint of the
    rules the launcher header lists (niubash hygiene, POSIX sh only);
  * `-n` syntax checks with every sh-like shell found (skipped when missing);
  * a few behavioural round-trips through `./deploy __probe EXIT STDIN ARGS...` per shell found:
    argv byte-exact, exit code, root discovery from src/ and from outside the project.
The exhaustive shell x scenario matrix is `./deploy selftest --shells`; keep this file quick.

On Windows the shells are looked up by absolute path (Git for Windows, MSYS2, niubash): a bare
`bash` there is usually the WSL launcher. niubash has no working `-n` (its `bash -n` accepts
anything), so it is only covered by the behavioural tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
LAUNCHER = ROOT / "deploy"
IS_WINDOWS = os.name == "nt"

# Arguments that must reach the runner unchanged. Passed to the shells as POSIX-quoted text (in
# an environment variable or a script file): a Windows parent handing argv straight to an
# MSYS/Cygwin program goes through Cygwin's own command-line parser, which is not MSVC-compatible.
ARGS = ["a b", "", 'q"x', "back\\slash", "tail\\", "\u00f1", "--flag=x", "-v", "*", "$HOME", "a'b", "--"]
NO_ROOT = "deploy: no .pytemplate/deploy.py next to this launcher"


# --- shells -------------------------------------------------------------------------------------


def _first_file(candidates: list[Path | None]) -> Path | None:
    for c in candidates:
        if c is not None and c.is_file():
            return c
    return None


def _git_root() -> Path | None:
    roots: list[Path] = []
    git = shutil.which("git")
    if git:
        exe = Path(git).resolve()
        roots += [exe.parent.parent, exe.parent.parent.parent]  # <root>/cmd/git.exe, <root>/mingw64/bin/git.exe
    for var in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            roots += [Path(base) / "Git", Path(base) / "Programs" / "Git"]
    roots.append(Path.home() / "scoop" / "apps" / "git" / "current")
    return next((r for r in roots if (r / "bin" / "sh.exe").is_file()), None)


def _msys2_root() -> Path | None:
    roots = [Path(os.environ[v]) for v in ("MSYS2_ROOT",) if os.environ.get(v)]
    roots += [Path("C:/msys64"), Path.home() / "scoop" / "apps" / "msys2" / "current"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs" / "XonshShell" / "msys2")
    return next((r for r in roots if (r / "usr" / "bin" / "bash.exe").is_file()), None)


def _niubash() -> Path | None:
    found = shutil.which("niu")
    local = os.environ.get("LOCALAPPDATA")
    return _first_file([Path(found) if found else None, Path(local) / "Programs" / "Niubash" / "niu.exe" if local else None])


def _posix_shell(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found) if found and not IS_WINDOWS else None


GIT = _git_root() if IS_WINDOWS else None
MSYS2 = _msys2_root() if IS_WINDOWS else None
NIU = _niubash() if IS_WINDOWS else None


def _git(rel: str) -> Path | None:
    return _first_file([GIT / rel]) if GIT else None


def _msys2(rel: str) -> Path | None:
    return _first_file([MSYS2 / rel]) if MSYS2 else None


def _clean_env(**extra: str) -> dict[str, str]:
    """The caller's environment without what `uv run` (pytest's parent) exports."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("UV", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PTCMD", "SHX_CWD_FILE") and not k.startswith("PYTEMPLATE_")
    }
    env.update(extra)
    return env


def q(s: str) -> str:
    """POSIX single-quoting."""
    return "'" + s.replace("'", "'\\''") + "'"


def probe_cmd(launcher: str, code: int, args: list[str] = ARGS, stdin: bool = False) -> str:
    return " ".join([launcher, "__probe", str(code), "1" if stdin else "0", *map(q, args)])


class Run:
    def __init__(self, argv: list[str | Path], cwd: Path, env: dict[str, str], stdin: bytes | None = None) -> None:
        r = subprocess.run(
            [str(a) for a in argv], cwd=cwd, env=env, capture_output=True, timeout=180,
            input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
        )
        self.rc = r.returncode
        self.out = r.stdout.decode("utf-8", "replace")
        self.err = r.stderr.decode("utf-8", "replace")
        self.probe: dict[str, object] = {}
        for line in self.out.splitlines():
            if line.startswith("PTPROBE"):
                self.probe = json.loads(line[len("PTPROBE") :])

    def check(self, code: int, cwd: Path, launcher: str | None = None, args: list[str] = ARGS) -> None:
        where = f"stdout={self.out!r} stderr={self.err!r}"
        assert self.probe, where
        assert self.rc == code, where
        assert self.probe["argv"] == args, where
        assert os.path.normcase(str(self.probe["root"])) == os.path.normcase(str(ROOT)), where
        raw = str(self.probe["caller_cwd_raw"])
        assert os.path.normcase(os.path.normpath(raw)) == os.path.normcase(str(cwd)), where
        assert os.path.normcase(str(self.probe["caller_cwd"])) == os.path.normcase(str(cwd)), where
        if IS_WINDOWS:
            assert re.match(r"[A-Z]:\\", raw), f"caller cwd not in C:\\ form: {raw!r}"
        if launcher is not None:
            assert self.probe["launcher"] == launcher, where


# --- static checks --------------------------------------------------------------------------------

# (regex over code with comments and quoted text blanked out, why)
LINT_RULES = [
    (r"\[\[", "[[ ]] is not POSIX"),
    (r"(^|[\s;&|(])local\s", "local is not POSIX"),
    (r"\$\{[^}]*//", "${v//a/b} is not POSIX"),
    (r"\$'", "$'...' is not POSIX"),
    (r"(^|[\s;&|(])function\s", "the function keyword is not POSIX"),
    (r"(\|\||&&)\s*exit\b", "niubash ignores exit after || and &&: use if/case"),
    (r"\{[^}]*\bexit\b", "niubash ignores exit inside { ...; }"),
    (r"(^|[\s;&|(])set\s+-[A-Za-z]*[eu]", "no set -e / set -u (bash 3.2, niubash in-process)"),
    (r"(^|[\s;&|(])set\s+-o\s+(errexit|nounset)", "no set -e / set -u (bash 3.2, niubash in-process)"),
    (r"(^|[\s;&|(])cd(\s|$)", "the launcher never changes directory (niubash would move the caller)"),
]
PREFIX_ONLY = {"IFS", "MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL"}  # only as `NAME=value command`
EXPORTED = {"PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER"}
FUNCTION = re.compile(r"^\s*([A-Za-z_]\w*)\s*\(\)\s*\{")


def _code_lines(text: str) -> list[tuple[int, str]]:
    """(line number, code with comments and quoted text blanked out)."""
    lines: list[tuple[int, str]] = []
    for number, line in enumerate(text.split("\n"), 1):
        out: list[str] = []
        quote = ""
        i = 0
        while i < len(line):
            ch = line[i]
            if quote:
                if ch == quote:
                    quote = ""
                    out.append(ch)
                elif ch == "\\" and quote == '"':
                    out.append("  ")
                    i += 1
                else:
                    out.append(" ")
            elif ch == "\\":
                out.append(line[i : i + 2])
                i += 1
            elif ch in "'\"":
                quote = ch
                out.append(ch)
            elif ch == "#" and (i == 0 or line[i - 1] in " \t;"):
                break
            else:
                out.append(ch)
            i += 1
        lines.append((number, "".join(out)))
    return lines


def _raw_problems(text: str) -> list[str]:
    bad: list[str] = []
    for n, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue
        if re.match(r"^\s*\w+\s*\(\)\s*\{\s*#", line):
            bad.append(f"{n}: comment on a function header line (niubash syntax error)")
        if re.search(r"(^|[\s;&|(])echo(\s|$)", line) and "\\" in line:
            bad.append(f"{n}: echo with a backslash (dash interprets it): use printf '%s\\n'")
        # "..$(cmd "$x").." keeps the inner quotes in niubash: assign the substitution first.
        in_dq = False
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "'" and not in_dq:
                end = line.find("'", i + 1)
                i = len(line) if end < 0 else end + 1
                continue
            if ch == '"':
                in_dq = not in_dq
            elif in_dq and line.startswith("$(", i):
                close = line.find(")", i)
                if '"' in line[i : close if close > 0 else len(line)]:
                    bad.append(f'{n}: "..$(cmd "$x").." (niubash keeps the inner quotes)')
                    break
            i += 1
    return bad


def _assigned(code: list[tuple[int, str]], bad: list[str]) -> dict[str, int]:
    """Every shell variable the launcher sets -> first line."""
    found: dict[str, int] = {}
    for n, line in code:
        for m in re.finditer(r"(?:^|[\s;&|()])([A-Za-z_]\w*)=", line):
            name = m.group(1)
            if name in PREFIX_ONLY:
                if not re.search(rf"\b{name}=\S*\s+\S", line):
                    bad.append(f"{n}: {name} may only prefix a command")
                continue
            found.setdefault(name, n)
        for m in re.finditer(r"(?:^|[\s;])for\s+([A-Za-z_]\w*)\s+in\b", line):
            found.setdefault(m.group(1), n)
        m = re.search(r"(?:^|[\s;])read\s+(?:-r\s+)?([A-Za-z_][\w ]*)", line)
        if m:
            for name in m.group(1).split():
                found.setdefault(name, n)
    return found


def _names(text: str) -> set[str]:
    return {name for line in text.replace("\\\n", " ").split("\n") for name in line.split()[1:]}


def lint(text: str) -> list[str]:
    """Every violation of the rules in the launcher's header comment, as `line: why`."""
    code = _code_lines(text)
    bad = [f"{n}: {why}" for pattern, why in LINT_RULES for n, line in code if re.search(pattern, line)]
    bad += _raw_problems(text)
    inside = False
    for n, line in code:
        if FUNCTION.match(line):
            inside = True
        elif re.match(r"^\}\s*$", line):
            inside = False
        elif inside and re.search(r"(^|[\s;&|(])exit\b", line):
            bad.append(f"{n}: exit inside a function (return instead)")
    # niubash runs the launcher inside the caller's shell: every name it defines leaks there.
    functions = {m.group(1): n for n, line in code if (m := FUNCTION.match(line))}
    variables = _assigned(code, bad)
    for name, n in {**functions, **variables}.items():
        if not name.startswith("_pt_") and name not in EXPORTED:
            bad.append(f"{n}: {name} without the _pt_ prefix")
    cut = text.rfind('\nexec "$@"')
    if cut < 0:
        return [*bad, 'the launcher must end with exec "$@"']
    cleanup = text.find("\nunset -f ")
    if not 0 <= cleanup < cut:
        return [*bad, 'the helpers must be removed with unset -f before exec "$@"']
    statements = [s for s in text[cleanup:cut].replace("\\\n", " ").split("\n") if re.match(r"\s*unset\s", s)]
    unset_f = _names("\n".join(s.replace("unset -f", "unset", 1) for s in statements if re.match(r"\s*unset\s+-f\s", s)))
    unset_v = _names("\n".join(s for s in statements if not re.match(r"\s*unset\s+-f\s", s)))
    bad += [f"{n}: function {name} is not unset before exec" for name, n in functions.items() if name not in unset_f]
    bad += [f"{n}: {name} is not unset before exec" for name, n in variables.items() if name.startswith("_pt_") and name not in unset_v]
    rest = re.sub(r"^\s*unset\s.*$", "", text[cleanup:].replace("\\\n", " "), flags=re.M)
    if "_pt_" in rest:
        bad.append("a _pt_ name is used after the cleanup started (unset -f ...)")
    return bad


def test_shebang_lf_ascii() -> None:
    data = LAUNCHER.read_bytes()
    assert data.startswith(b"#!/bin/sh\n"), "first line must be exactly #!/bin/sh"
    assert b"\r" not in data, "deploy must have LF line endings (.gitattributes: deploy text eol=lf)"
    assert all(b < 128 for b in data), "deploy must be pure ASCII"
    assert data.endswith(b"\n")


def test_git_mode_is_executable() -> None:
    git = shutil.which("git")
    if not git:
        pytest.skip("git not found")
    r = subprocess.run([git, "ls-files", "-s", "--", "deploy"], cwd=ROOT, capture_output=True, text=True, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("not a git checkout (or deploy is not tracked)")
    assert r.stdout.split()[0] == "100755", "run: git update-index --chmod=+x deploy"


def test_lint() -> None:
    problems = lint(LAUNCHER.read_bytes().decode("ascii"))
    assert not problems, "\n".join(problems)


_OK_TAIL = '\nunset -f _pt_ok\nunset _pt_v\nexec "$@"\n'


@pytest.mark.parametrize(
    ("snippet", "why"),
    [
        ("[[ -n $x ]]", "[[ ]]"),
        ("_pt_ok() {\n    local _pt_v\n}", "local is not POSIX"),
        ("_pt_v=${_pt_v//a/b}", "${v//a/b}"),
        ("_pt_v=$'a'", "$'...'"),
        ("function _pt_ok {\n    :\n}", "function keyword"),
        ("[ -f x ] || exit 2", "after || and &&"),
        ("{ :; exit 2; }", "inside { ...; }"),
        ("set -eu", "set -e"),
        ("cd /tmp", "never changes directory"),
        ("_pt_ok() { # why\n    :\n}", "comment on a function header"),
        ("echo 'C:\\x'", "echo with a backslash"),
        ('_pt_v="a $(cmd "$_pt_v") b"', "inner quotes"),
        ("_pt_ok() {\n    exit 1\n}", "exit inside a function"),
        ("x=1", "x without the _pt_ prefix"),
        ("f() {\n    :\n}", "f without the _pt_ prefix"),
        ("_pt_w=1", "_pt_w is not unset"),
        ("_pt_f() {\n    :\n}", "function _pt_f is not unset"),
        ("IFS=:", "IFS may only prefix a command"),
    ],
)
def test_lint_detects(snippet: str, why: str) -> None:
    problems = lint("#!/bin/sh\n_pt_ok() {\n    :\n}\n_pt_v=1\n" + snippet + "\n" + _OK_TAIL)
    assert any(why in p for p in problems), problems


def test_lint_accepts_a_clean_launcher() -> None:
    assert lint("#!/bin/sh\n_pt_ok() {\n    return 0\n}\n_pt_v=$(printf '%s' \"$1\")\nIFS= read -r _pt_v\n" + _OK_TAIL) == []


# --- syntax (-n) with every shell found ---------------------------------------------------------

SYNTAX_SHELLS: list[tuple[str, Path | None, list[str]]] = [
    ("git-sh", _git("bin/sh.exe"), ["-n"]),
    ("git-bash-posix", _git("bin/bash.exe"), ["--posix", "-n"]),
    ("git-dash", _git("usr/bin/dash.exe"), ["-n"]),
    ("msys2-bash-posix", _msys2("usr/bin/bash.exe"), ["--posix", "-n"]),
    ("msys2-dash", _msys2("usr/bin/dash.exe"), ["-n"]),
    ("sh", Path("/bin/sh") if not IS_WINDOWS and Path("/bin/sh").is_file() else None, ["-n"]),
    ("dash", _posix_shell("dash"), ["-n"]),
    ("bash-posix", _posix_shell("bash"), ["--posix", "-n"]),
    ("zsh", _posix_shell("zsh"), ["-n"]),
    ("ksh", _posix_shell("ksh"), ["-n"]),
    ("mksh", _posix_shell("mksh"), ["-n"]),
    ("busybox", _posix_shell("busybox"), ["sh", "-n"]),
]


@pytest.mark.parametrize(("name", "shell", "flags"), SYNTAX_SHELLS, ids=[s[0] for s in SYNTAX_SHELLS])
def test_syntax(name: str, shell: Path | None, flags: list[str]) -> None:
    if shell is None:
        pytest.skip(f"{name} not found")
    r = subprocess.run([str(shell), *flags, "deploy"], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr


# --- behaviour: __probe round-trips --------------------------------------------------------------

needs_windows = pytest.mark.skipif(not IS_WINDOWS, reason="Windows shells")
needs_posix = pytest.mark.skipif(IS_WINDOWS, reason="POSIX shells")


def _need(path: Path | None, what: str) -> Path:
    if path is None:
        pytest.skip(f"{what} not found")
    return path


@needs_windows
def test_git_sh(tmp_path: Path) -> None:
    sh = _need(_git("bin/sh.exe"), "Git for Windows sh.exe")
    Run([sh, "-c", 'eval "$PTCMD"'], ROOT, _clean_env(PTCMD=probe_cmd("./deploy", 3))).check(3, ROOT, "sh:bash:msys")
    # absolute launcher path from outside the project; %TEMP% is /tmp in Git Bash (cygpath branch)
    posix = "/" + ROOT.as_posix()[0].lower() + ROOT.as_posix()[2:] + "/deploy"
    Run([sh, "-c", 'eval "$PTCMD"'], tmp_path, _clean_env(PTCMD=probe_cmd(q(posix), 5))).check(5, tmp_path, "sh:bash:msys")


@needs_windows
def test_git_dash_runs_the_launcher() -> None:
    sh = _need(_git("bin/sh.exe"), "Git for Windows sh.exe")
    _need(_git("usr/bin/dash.exe"), "Git for Windows dash.exe")
    run = Run([sh, "-c", 'eval "$PTCMD"'], SRC, _clean_env(PTCMD=probe_cmd("dash ../deploy", 6, stdin=True)), stdin=b"ping\n")
    run.check(6, SRC, "sh:msys")
    assert run.probe["stdin"] == "ping"


@needs_windows
def test_msys2_login_minimal_path() -> None:
    """MSYS2 login shell with the minimal PATH (uv is not on it): the install folders are searched."""
    bash = _need(_msys2("usr/bin/bash.exe"), "MSYS2 bash.exe")
    env = _clean_env(MSYSTEM="UCRT64", CHERE_INVOKING="1", MSYS2_PATH_TYPE="minimal", PTCMD=probe_cmd("./deploy", 3))
    Run([bash, "-lc", 'eval "$PTCMD"'], ROOT, env).check(3, ROOT, "sh:bash:msys")


def _shx_script(directory: Path, payload: str) -> Path:
    """What xonsh-shell-kit writes for a `! line` (the script's $0 is not the launcher)."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "bang-1-1.sh"
    trailer = ["__shx_rc=$?", 'if [ -n "${SHX_CWD_FILE:-}" ]; then pwd > "$SHX_CWD_FILE" 2>/dev/null; fi', "exit $__shx_rc"]
    script.write_text("\n".join([payload, "", *trailer]) + "\n", encoding="utf-8", newline="\n")
    return script


@needs_windows
def test_msys2_non_login_script(tmp_path: Path) -> None:
    """The xonsh-shell-kit `!m` route: non-login MSYS2 bash running a temp script."""
    bash = _need(_msys2("usr/bin/bash.exe"), "MSYS2 bash.exe")
    script = _shx_script(tmp_path / "xonsh-shell-kit", probe_cmd("../deploy", 4))
    Run([bash, script], SRC, _clean_env(MSYSTEM="MINGW64", CHERE_INVOKING="1")).check(4, SRC, "sh:bash:msys")


@needs_windows
def test_niubash_c() -> None:
    niu = _need(NIU, "niubash (niu.exe)")
    Run([niu, "-c", probe_cmd("../deploy", 3)], SRC, _clean_env()).check(3, SRC, "sh:niubash")


@needs_windows
def test_niubash_script_shx_route(tmp_path: Path) -> None:
    """The original bug: niubash runs ./deploy in-process with $0 = the temp script."""
    niu = _need(NIU, "niubash (niu.exe)")
    script = _shx_script(tmp_path / "xonsh-shell-kit", probe_cmd("./deploy", 3))
    cwd_file = tmp_path / "shx.cwd"
    Run([niu, script], ROOT, _clean_env(SHX_CWD_FILE=str(cwd_file))).check(3, ROOT, "sh:niubash")
    after = cwd_file.read_text(encoding="utf-8").strip()
    assert os.path.normcase(os.path.normpath(after)) == os.path.normcase(str(ROOT)), "the launcher moved the caller"


@needs_windows
def test_niubash_leaves_nothing_behind() -> None:
    niu = _need(NIU, "niubash (niu.exe)")
    cmd = (
        probe_cmd("./deploy", 0, ["x"]) + "; printf 'rc=%s\\n' \"$?\"; set | grep -E '^(_pt_|PYTEMPLATE_)'; "
        "typeset -f 2>/dev/null | grep -c _pt_; printf 'pwd=%s\\n' \"$PWD\""
    )
    run = Run([niu, "-c", cmd], ROOT, _clean_env())
    lines = [line for line in run.out.splitlines() if not line.startswith("PTPROBE")]
    assert run.probe and lines[0] == "rc=0", run.out + run.err
    assert lines[1:-1] == ["0"], f"leaked into the niubash session: {lines[1:-1]}"
    assert os.path.normcase(os.path.normpath(lines[-1][4:])) == os.path.normcase(str(ROOT))


@needs_posix
@pytest.mark.parametrize("name", ["sh", "dash", "bash", "zsh", "ksh", "mksh", "busybox"])
def test_posix_shell_runs_the_launcher(name: str) -> None:
    shell = Path("/bin/sh") if name == "sh" else _posix_shell(name)
    shell = _need(shell if shell and shell.is_file() else None, name)
    argv: list[str | Path] = [shell, "sh"] if name == "busybox" else [shell]
    Run([*argv, "../deploy", "__probe", "5", "0", *ARGS], SRC, _clean_env()).check(5, SRC)


@needs_posix
def test_posix_executable_from_root_with_stdin() -> None:
    run = Run(["/bin/sh", "-c", 'eval "$PTCMD"'], ROOT, _clean_env(PTCMD=probe_cmd("./deploy", 3, stdin=True)), stdin=b"ping\n")
    run.check(3, ROOT)
    assert run.probe["stdin"] == "ping"


def _outside_shell() -> list[str | Path] | None:
    if not IS_WINDOWS:
        return ["/bin/sh"]
    dash = _git("usr/bin/dash.exe") or _msys2("usr/bin/dash.exe")
    if dash:
        return [dash]
    return [NIU, "-c", "./deploy help; exit $?"] if NIU else None


def test_outside_any_project(tmp_path: Path) -> None:
    shell = _outside_shell()
    if shell is None:
        pytest.skip("no sh-like shell found")
    shutil.copyfile(LAUNCHER, tmp_path / "deploy")
    argv = shell if len(shell) > 1 else [*shell, "deploy", "help"]
    run = Run(argv, tmp_path, _clean_env())
    assert run.rc == 2 and NO_ROOT in run.err, run.out + run.err


def test_no_uv_anywhere(tmp_path: Path) -> None:
    """Exit 127 with install hints; on Windows they never suggest curl."""
    if IS_WINDOWS:
        shell = _git("usr/bin/dash.exe") or _msys2("usr/bin/dash.exe")
        if shell is None:
            pytest.skip("no dash.exe found")
        bogus = str(tmp_path / "none")
        env = _clean_env(PATH=bogus, USERPROFILE=bogus, HOME=bogus, LOCALAPPDATA=bogus, ProgramFiles=bogus, ProgramData=bogus, SCOOP=bogus, SCOOP_GLOBAL=bogus, ChocolateyInstall=bogus)
    else:
        shell = Path("/bin/sh")
        if any(Path(d, "uv").exists() for d in ("/opt/homebrew/bin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin")):
            pytest.skip("uv is installed in a system folder the launcher always searches")
        env = {"PATH": str(tmp_path / "none"), "HOME": str(tmp_path)}
    run = Run([shell, "deploy", "help"], ROOT, env)
    assert run.rc == 127, run.out + run.err
    assert "uv not found" in run.err
    if IS_WINDOWS:
        assert "winget" in run.err and "curl" not in run.err
    else:
        assert "curl" in run.err
