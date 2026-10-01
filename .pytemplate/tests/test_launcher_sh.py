"""Tests for the POSIX launcher ./pyt (run them with `./pyt selftest`).

Three layers:
  * static checks that run everywhere: shebang, LF, ASCII, git mode 100755 and a lint of the
    rules the launcher header lists (niubash hygiene, POSIX sh only);
  * `-n` syntax checks with every sh-like shell found (skipped when missing);
  * a few behavioural round-trips through `./pyt __probe EXIT STDIN ARGS...` per shell found:
    argv byte-exact, exit code, root discovery from src/ and from outside the project.
The exhaustive shell x scenario matrix is `./pyt selftest --shells`; keep this file quick.

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
import sys
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
LAUNCHER = ROOT / "pyt"
IS_WINDOWS = os.name == "nt"

# Arguments that must reach the runner unchanged. Passed to the shells as POSIX-quoted text (in
# an environment variable or a script file): a Windows parent handing argv straight to an
# MSYS/Cygwin program goes through Cygwin's own command-line parser, which is not MSVC-compatible.
ARGS = ["a b", "", 'q"x', "back\\slash", "tail\\", "\u00f1", "--flag=x", "-v", "*", "$HOME", "a'b", "--"]
NO_ROOT = "pyt: no .pytemplate/pyt.py next to this launcher"
INSTALL_HINT = "To run pyt outside a project, install it: ./pyt install in a clone of the template"


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


DROP_ENV = (
    "UV", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "UV_PYTHON_PREFERENCE", "UV_RUN_RECURSION_DEPTH",
    "PTCMD", "SHX_CWD_FILE", "PT_CODE",
)  # fmt: skip


def _clean_env(**extra: str) -> dict[str, str]:
    """The caller's environment without what `uv run` (pytest's parent) exports."""
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV and not k.startswith("PYTEMPLATE_")}
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
    # `x=$(cmd)` takes cmd's status: a caller's set -e (sh -e pyt, niubash) would stop there.
    # (An arithmetic expansion, _pt_n=$((_pt_n + 1)), runs no command.)
    (r"^(?!.*\|\|).*\b_pt_\w+=\$\((?!\()", "a command substitution needs `|| _pt_x=` (a caller's set -e)"),
]
PREFIX_ONLY = {
    "IFS", "MSYS_NO_PATHCONV", "MSYS2_ARG_CONV_EXCL", "UV_PYTHON", "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON", "PYTHONHOME", "PYTHONPATH", "UV_WORKING_DIR",
}  # only as `NAME=value command`
EXPORTED = {"PYTEMPLATE_CALLER_CWD", "PYTEMPLATE_LAUNCHER", "PYTEMPLATE_GLOBAL"}
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
    # Errors too: an exit before the cleanup leaves every _pt_ name in a niubash session.
    first_cleanup_line = text[: cleanup + 1].count("\n") + 1
    bad += [f"{n}: exit before the cleanup (unset -f ...)" for n, line in code if n < first_cleanup_line and re.search(r"(^|[\s;&|(])exit\b", line)]
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
    assert b"\r" not in data, "pyt must have LF line endings (.gitattributes: pyt text eol=lf)"
    assert all(b < 128 for b in data), "pyt must be pure ASCII"
    assert data.endswith(b"\n")


def test_git_mode_is_executable() -> None:
    git = shutil.which("git")
    if not git:
        pytest.skip("git not found")
    r = subprocess.run([git, "ls-files", "-s", "--", "pyt"], cwd=ROOT, capture_output=True, text=True, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("not a git checkout (or pyt is not tracked)")
    assert r.stdout.split()[0] == "100755", "run: git update-index --chmod=+x pyt"


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
        ("UV_PYTHON=", "UV_PYTHON may only prefix a command"),
        ("_pt_v=$(uname -s 2>/dev/null)", "needs `|| _pt_x=`"),
        ("if [ -z \"$_pt_v\" ]; then\n    exit 2\nfi", "exit before the cleanup"),
    ],
)
def test_lint_detects(snippet: str, why: str) -> None:
    problems = lint("#!/bin/sh\n_pt_ok() {\n    :\n}\n_pt_v=1\n" + snippet + "\n" + _OK_TAIL)
    assert any(why in p for p in problems), problems


def test_lint_accepts_a_clean_launcher() -> None:
    code = "#!/bin/sh\n_pt_ok() {\n    return 0\n}\n_pt_v=$(printf '%s' \"$1\") || _pt_v=\nIFS= read -r _pt_v || :\n_pt_v=$((1 + 1))\n"
    tail = "\nunset -f _pt_ok\nunset _pt_v\nif [ \"$#\" -eq 1 ]; then\n    exit \"$1\"\nfi\nUV_PYTHON='' \"$@\"\nexec \"$@\"\n"
    assert lint(code + tail) == []


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
    r = subprocess.run([str(shell), *flags, "pyt"], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
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
    Run([sh, "-c", 'eval "$PTCMD"'], ROOT, _clean_env(PTCMD=probe_cmd("./pyt", 3))).check(3, ROOT, "sh:bash:msys")
    # absolute launcher path from outside the project; %TEMP% is /tmp in Git Bash (cygpath branch)
    posix = "/" + ROOT.as_posix()[0].lower() + ROOT.as_posix()[2:] + "/pyt"
    Run([sh, "-c", 'eval "$PTCMD"'], tmp_path, _clean_env(PTCMD=probe_cmd(q(posix), 5))).check(5, tmp_path, "sh:bash:msys")


@needs_windows
def test_git_dash_runs_the_launcher() -> None:
    sh = _need(_git("bin/sh.exe"), "Git for Windows sh.exe")
    _need(_git("usr/bin/dash.exe"), "Git for Windows dash.exe")
    run = Run([sh, "-c", 'eval "$PTCMD"'], SRC, _clean_env(PTCMD=probe_cmd("dash ../pyt", 6, stdin=True)), stdin=b"ping\n")
    run.check(6, SRC, "sh:msys")
    assert run.probe["stdin"] == "ping"


@needs_windows
def test_msys2_login_minimal_path() -> None:
    """MSYS2 login shell with the minimal PATH (uv is not on it): the install folders are searched."""
    bash = _need(_msys2("usr/bin/bash.exe"), "MSYS2 bash.exe")
    env = _clean_env(MSYSTEM="UCRT64", CHERE_INVOKING="1", MSYS2_PATH_TYPE="minimal", PTCMD=probe_cmd("./pyt", 3))
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
    script = _shx_script(tmp_path / "xonsh-shell-kit", probe_cmd("../pyt", 4))
    Run([bash, script], SRC, _clean_env(MSYSTEM="MINGW64", CHERE_INVOKING="1")).check(4, SRC, "sh:bash:msys")


@needs_windows
def test_niubash_c() -> None:
    niu = _need(NIU, "niubash (niu.exe)")
    Run([niu, "-c", probe_cmd("../pyt", 3)], SRC, _clean_env()).check(3, SRC, "sh:niubash")


@needs_windows
def test_niubash_script_shx_route(tmp_path: Path) -> None:
    """The original bug: niubash runs ./pyt in-process with $0 = the temp script."""
    niu = _need(NIU, "niubash (niu.exe)")
    script = _shx_script(tmp_path / "xonsh-shell-kit", probe_cmd("./pyt", 3))
    cwd_file = tmp_path / "shx.cwd"
    Run([niu, script], ROOT, _clean_env(SHX_CWD_FILE=str(cwd_file))).check(3, ROOT, "sh:niubash")
    after = cwd_file.read_text(encoding="utf-8").strip()
    assert os.path.normcase(os.path.normpath(after)) == os.path.normcase(str(ROOT)), "the launcher moved the caller"


@needs_windows
def test_niubash_leaves_nothing_behind() -> None:
    niu = _need(NIU, "niubash (niu.exe)")
    cmd = (
        probe_cmd("./pyt", 0, ["x"]) + "; printf 'rc=%s\\n' \"$?\"; set | grep -E '^(_pt_|PYTEMPLATE_)'; "
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
    Run([*argv, "../pyt", "__probe", "5", "0", *ARGS], SRC, _clean_env()).check(5, SRC)


# Any word an argv can hold (Unicode without NUL), mixed with the ones known to be hard.
ANY_ARGS = st.lists(st.one_of(st.sampled_from(ARGS), st.text(st.characters(codec="utf-8", exclude_characters="\x00"))), max_size=50)


@needs_posix
@settings(max_examples=max(3, settings.default.max_examples // 20))  # a runner start each
@given(ANY_ARGS)
def test_any_arguments_reach_the_runner_unchanged(args: list[str]) -> None:
    """Words at random reach the runner as they are, however many."""
    Run(["/bin/sh", "../pyt", "__probe", "5", "0", *args], SRC, _clean_env()).check(5, SRC, args=args)


@needs_posix
def test_posix_executable_from_root_with_stdin() -> None:
    run = Run(["/bin/sh", "-c", 'eval "$PTCMD"'], ROOT, _clean_env(PTCMD=probe_cmd("./pyt", 3, stdin=True)), stdin=b"ping\n")
    run.check(3, ROOT)
    assert run.probe["stdin"] == "ping"


def _outside_shell() -> list[str | Path] | None:
    if not IS_WINDOWS:
        return ["/bin/sh"]
    dash = _git("usr/bin/dash.exe") or _msys2("usr/bin/dash.exe")
    if dash:
        return [dash]
    return [NIU, "-c", "./pyt help; exit $?"] if NIU else None


def _nothing_installed(tmp: Path) -> dict[str, str]:
    """Where the launchers look for the installed template (pyt install), moved to an empty
    folder: a real installed pyt of the user never answers a test that expects no project."""
    return {"XDG_DATA_HOME": str(tmp / "no-data"), "LOCALAPPDATA": str(tmp / "no-data")}


def test_outside_any_project(tmp_path: Path) -> None:
    """No project and no installed template: exit 2, and how to install pyt."""
    shell = _outside_shell()
    if shell is None:
        pytest.skip("no sh-like shell found")
    shutil.copyfile(LAUNCHER, tmp_path / "pyt")
    argv = shell if len(shell) > 1 else [*shell, "pyt", "help"]
    run = Run(argv, tmp_path, _clean_env(**_nothing_installed(tmp_path)))
    assert run.rc == 2 and NO_ROOT in run.err and INSTALL_HINT in run.err, run.out + run.err


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
    run = Run([shell, "pyt", "help"], ROOT, env)
    assert run.rc == 127, run.out + run.err
    assert "uv not found" in run.err
    if IS_WINDOWS:
        assert "winget" in run.err and "curl" not in run.err
    else:
        assert "curl" in run.err


# --- a caller's set -e / set -u ------------------------------------------------------------------

POSIX_SHELLS = ["sh", "dash", "bash", "zsh", "ksh", "mksh", "yash", "busybox"]
SYSTEM_UV_DIRS = ("/usr/bin", "/bin", "/opt/homebrew/bin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin")


def _shell_argv(name: str) -> list[str | Path]:
    """argv that runs a script with the POSIX shell `name` (skips when it is not installed)."""
    shell = Path("/bin/sh") if name == "sh" else _posix_shell(name)
    shell = _need(shell if shell and shell.is_file() else None, name)
    return [shell, "sh"] if name == "busybox" else [shell]


def _system_uv() -> str | None:
    """A system folder with a uv in it: the launchers always search those, so 'no uv' cannot be set up."""
    return next((d for d in SYSTEM_UV_DIRS if Path(d, "uv").exists()), None)


def _no_uv_env(home: Path, **extra: str) -> dict[str, str]:
    """An environment built from scratch where no uv can be found, with the caller's locale like
    every other case: in the C locale yash cannot read a path that is not ASCII (a project in
    `My Game e-acute`), prints `failed to set $PWD` and runs nothing."""
    locale = {k: v for k, v in os.environ.items() if k in ("LANG", "LC_ALL", "LC_CTYPE")}
    # PATH keeps /usr/bin:/bin: yash runs `[` only when it is found on PATH.
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), **locale, **extra}


@needs_posix
@pytest.mark.parametrize("case", ["uv-off-path", "stale-UV", "no-uv"])
@pytest.mark.parametrize("name", POSIX_SHELLS)
def test_launcher_survives_caller_errexit(name: str, case: str, tmp_path: Path) -> None:
    """`sh -eu pyt` (and niubash with set -e, in-process) must still reach every fallback."""
    argv = [*_shell_argv(name), "-eu"]
    if case == "no-uv":
        if _system_uv():
            pytest.skip("uv is installed in a system folder the launcher always searches")
        run = Run([*argv, "pyt", "help"], ROOT, _no_uv_env(tmp_path))
        assert run.rc == 127 and "uv not found" in run.err and "curl" in run.err, run.out + run.err
        return
    uv = shutil.which("uv", path=_clean_env().get("PATH"))
    if uv is None:
        pytest.skip("uv not on PATH")
    if case == "uv-off-path":
        if any(Path(d, "uv").exists() for d in ("/usr/bin", "/bin")):
            pytest.skip("uv is in /usr/bin or /bin")
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "uv").symlink_to(uv)
        env = _clean_env(PATH="/usr/bin:/bin", XDG_BIN_HOME=str(tmp_path / "bin"))
    else:
        env = _clean_env(UV=str(tmp_path / "none" / "uv"))
    Run([*argv, "pyt", "__probe", "0", "0", *ARGS], ROOT, env).check(0, ROOT)


# --- paths: symlinks and backslashes on POSIX -------------------------------------------------------


@needs_posix
@pytest.mark.parametrize("via", ["exec", "sh"])
def test_relative_launcher_from_a_symlinked_subfolder(tmp_path: Path, via: str) -> None:
    """`../pyt` from a symlink to src/: $PWD is logical, but the kernel found ../pyt physically."""
    if via == "exec" and not os.access(LAUNCHER, os.X_OK):
        pytest.skip("pyt has no exec bit in this checkout")
    link = tmp_path / "link"
    link.symlink_to(SRC, target_is_directory=True)
    launcher = "../pyt" if via == "exec" else "sh ../pyt"
    # cd inside the shell keeps $PWD logical (a subprocess cwd would be physical).
    code = f"cd {q(str(link))} && " + probe_cmd(launcher, 6, ["x", "a b"])
    run = Run(["/bin/sh", "-c", 'eval "$PTCMD"'], tmp_path, _clean_env(PTCMD=code))
    assert run.probe and run.rc == 6, run.out + run.err
    assert run.probe["argv"] == ["x", "a b"]
    assert Path(str(run.probe["root"])) == ROOT
    assert run.probe["caller_cwd_raw"] == str(link)
    assert run.probe["caller_cwd"] == str(link)


def _launcher_links(tmp_path: Path) -> dict[str, Path]:
    """Links to ./pyt from folders outside the project: an absolute one in a bin folder, a
    relative one to that link, and a relative one seen through a symlinked folder (~/bin ->
    /opt/tools/bin: its '..' is the physical folder's, as the kernel resolves it)."""
    bindir, chain, tools, home = tmp_path / "bin", tmp_path / "chain", tmp_path / "tools" / "bin", tmp_path / "home"
    for folder in (bindir, chain, tools, home):
        folder.mkdir(parents=True)
    (bindir / "mypyt").symlink_to(LAUNCHER)
    (chain / "rel").symlink_to(Path("..") / "bin" / "mypyt")
    (tmp_path / "tools" / "proj").symlink_to(ROOT, target_is_directory=True)
    (tools / "mypyt").symlink_to(Path("..") / "proj" / "pyt")
    (home / "bin").symlink_to(tools, target_is_directory=True)
    return {"absolute": bindir / "mypyt", "relative, to a link": chain / "rel", "relative, in a linked folder": home / "bin" / "mypyt"}


@needs_posix
@pytest.mark.parametrize("name", POSIX_SHELLS)
def test_a_launcher_reached_through_a_symlink_finds_its_project(name: str, tmp_path: Path) -> None:
    """A link to ./pyt in a folder on PATH (~/bin/mypyt -> proj/pyt), run from outside the
    project, said there was no .pytemplate/pyt.py next to this launcher (exit 2): the folder of
    the link was taken for the launcher's. The link is followed to the file it names."""
    argv = _shell_argv(name)
    away = tmp_path / "away"
    away.mkdir()
    links = _launcher_links(tmp_path)
    runs = {how: Run([*argv, link, "__probe", "5", "0", *ARGS], away, _clean_env()) for how, link in links.items()}
    if name == "sh" and os.access(LAUNCHER, os.X_OK):
        runs["executed"] = Run([links["absolute"], "__probe", "5", "0", *ARGS], away, _clean_env())
    for how, run in runs.items():
        try:
            run.check(5, away)
        except AssertionError as e:
            raise AssertionError(f"{how}: {e}") from None


def _through_a_link(tmp: Path, name: str, project: Path, env: dict[str, str], link_name: str = "game-src") -> tuple[Run, Path]:
    """The launcher of a bin folder (where pyt install puts it), run by the shell `name` (or
    pwsh) from `<tmp>/game-src/pkg`, where game-src is a symlink to the project's src/: a folder
    that physically lies in the project, and whose logical parents never reach it."""
    (project / "src" / "pkg").mkdir(exist_ok=True)
    link = tmp / link_name
    link.symlink_to(project / "src", target_is_directory=True)
    cwd = link / "pkg"
    (tmp / "bin").mkdir()
    if name == "pwsh":
        ps1 = tmp / "bin" / "pyt.ps1"
        shutil.copyfile(ROOT / "pyt.ps1", ps1)
        here, script = (str(p).replace("'", "''") for p in (cwd, ps1))
        # Set-Location keeps the location logical, as a user's cd does.
        code = f"Set-Location -LiteralPath '{here}'; & '{script}' __probe 5 0 x 'a b'; exit $LASTEXITCODE"
        return Run([_pwsh(), "-NoProfile", "-NonInteractive", "-EncodedCommand", _ps_encoded(code)], tmp, env), cwd
    launcher = tmp / "bin" / "pyt"
    shutil.copyfile(LAUNCHER, launcher)
    shell = " ".join(q(str(a)) for a in _shell_argv(name))
    # cd in the calling shell keeps $PWD logical (a subprocess cwd would be physical), and the
    # shell `name` takes it from the environment, as it does from a user's terminal.
    code = f"cd {q(str(cwd))} && export PWD && {shell} {q(str(launcher))} __probe 5 0 x 'a b'"
    return Run(["/bin/sh", "-c", 'eval "$PTCMD"'], tmp, {**env, "PTCMD": code}), cwd


@needs_posix
@pytest.mark.parametrize("name", [*POSIX_SHELLS, "pwsh"])
def test_a_folder_reached_through_a_symlink_into_a_project_finds_it(name: str, tmp_path: Path) -> None:
    """The installed pyt (its own folder holds no project) walked up the logical $PWD only: from
    ~/game-src -> ~/code/game/src no logical parent holds the project, and it said there was
    none (exit 2), or ran the installed template, whose commands need a project, where git finds
    the repository. When the logical walk finds nothing, it walks up from the physical folder."""
    project = _copy_project(tmp_path / "code" / "game")
    run, cwd = _through_a_link(tmp_path, name, project, _clean_env(**_nothing_installed(tmp_path)))
    where = f"stdout={run.out!r} stderr={run.err!r}"
    assert run.rc == 5 and run.probe and run.probe["argv"] == ["x", "a b"], where
    assert Path(str(run.probe["root"])).resolve() == project.resolve(), where
    assert run.probe["global"] == "", where
    assert run.probe["caller_cwd_raw"] == str(cwd) and run.probe["caller_cwd"] == str(cwd), where


@needs_posix
@pytest.mark.parametrize("name", ["sh", "pwsh"])
def test_a_folder_named_like_a_glob_reached_through_a_symlink_finds_its_project(name: str, tmp_path: Path) -> None:
    """pyt.ps1 handed the logical folder to /bin/sh's `cd -P` unquoted, and PowerShell 7 globs
    such an argument off Windows: from g[0-9]/pkg, a link into this project, it walked up from
    g1/pkg, a link into another one, and ran that project's runner."""
    project = _copy_project(tmp_path / "code" / "game")
    other = _copy_project(tmp_path / "code" / "other")
    (other / "src" / "pkg").mkdir()
    (tmp_path / "g1").symlink_to(other / "src", target_is_directory=True)  # what g[0-9] matches as a glob
    run, cwd = _through_a_link(tmp_path, name, project, _clean_env(**_nothing_installed(tmp_path)), "g[0-9]")
    where = f"stdout={run.out!r} stderr={run.err!r}"
    assert run.rc == 5 and run.probe and run.probe["argv"] == ["x", "a b"], where
    assert Path(str(run.probe["root"])).resolve() == project.resolve(), where
    assert run.probe["caller_cwd"] == str(cwd), where


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to make files another user owns")
@pytest.mark.parametrize("name", ["sh", "bash", "pwsh"])
def test_the_walk_up_from_the_physical_folder_keeps_the_ownership_rule(name: str, tmp_path: Path) -> None:
    """A symlink into a folder of another user's project (anyone may create one pointing there)
    leads the physical walk-up to that project: its runner is refused as on the logical walk."""
    import pwd

    nobody = pwd.getpwnam("nobody")
    project = _copy_project(tmp_path / "theirs")
    (project / "src" / "pkg").mkdir()
    (project / ".pytemplate" / "pyt.py").write_text("print('PWNED')\n", encoding="utf-8")
    for path in (project, project / ".pytemplate", project / ".pytemplate" / "pyt.py"):
        os.chown(path, nobody.pw_uid, nobody.pw_gid)
    run, _ = _through_a_link(tmp_path, name, project, _clean_env(**_nothing_installed(tmp_path)))
    assert run.rc == 2 and "is not yours" in run.err and "PWNED" not in run.out + run.err, run.out + run.err


def _copy_project(dest: Path) -> Path:
    """The launcher and the runner (enough for __probe) in `dest`."""
    (dest / ".pytemplate").mkdir(parents=True)
    shutil.copyfile(LAUNCHER, dest / "pyt")
    (dest / "pyt").chmod(0o755)
    shutil.copyfile(ROOT / ".pytemplate" / "pyt.py", dest / ".pytemplate" / "pyt.py")
    shutil.copytree(ROOT / ".pytemplate" / "runner", dest / ".pytemplate" / "runner", ignore=shutil.ignore_patterns("__pycache__"))
    (dest / "src").mkdir()
    return dest


@needs_posix
@pytest.mark.parametrize("name", ["sh", "dash", "bash", "zsh", "ksh", "mksh", "busybox"])
def test_posix_backslash_in_the_project_path(tmp_path: Path, name: str) -> None:
    """A backslash is a legal POSIX file-name character: only Windows shells convert it to /."""
    argv = _shell_argv(name)
    project = _copy_project(tmp_path / "a\\b" / "p")
    for launcher, cwd in (("./pyt", project), ("../pyt", project / "src"), (str(project / "pyt"), tmp_path)):
        run = Run([*argv, launcher, "__probe", "4", "0", *ARGS], cwd, _clean_env())
        where = f"{launcher} from {cwd}: stdout={run.out!r} stderr={run.err!r}"
        assert run.rc == 4 and run.probe, where
        assert run.probe["argv"] == ARGS, where
        assert run.probe["root"] == str(project.resolve()), where
        assert run.probe["caller_cwd_raw"] == str(cwd), where
        assert run.probe["caller_cwd"] == str(cwd), where


# --- outside any project: the installed template (pyt install) ------------------------------------------


@pytest.fixture(scope="module")
def uv_dirs() -> dict[str, str]:
    """uv's cache and Python folders as this environment resolves them. A test that moves HOME
    or XDG_DATA_HOME (where the launchers look for the installed template) passes them on: uv
    would otherwise start from empty ones and download CPython."""
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv not found")
    found: dict[str, str] = {}
    for key, args in (("UV_CACHE_DIR", ("cache", "dir")), ("UV_PYTHON_INSTALL_DIR", ("python", "dir"))):
        r = subprocess.run([uv, *args], env=_clean_env(), capture_output=True, text=True, timeout=60, check=False)
        lines = r.stdout.strip().splitlines()
        if r.returncode != 0 or not lines:
            pytest.skip(f"uv {' '.join(args)} failed: {r.stderr.strip()}")
        found[key] = lines[-1]
    return found


def _installed(data: Path) -> Path:
    """The launcher and the runner where the launchers look for the installed template of the
    user data folder `data` (cmd_install.snapshot_dir)."""
    return _copy_project(data / "pytemplate" / "template")


def _outside(tmp: Path) -> tuple[Path, Path]:
    """A copy of the launcher in a bin folder (where pyt install puts it), and a folder outside
    any project to run it from."""
    (tmp / "bin").mkdir()
    shutil.copyfile(LAUNCHER, tmp / "bin" / "pyt")
    (tmp / "away").mkdir()
    return tmp / "bin" / "pyt", tmp / "away"


def _check_installed(run: Run, root: Path, cwd: Path, args: list[str] = ARGS) -> None:
    """The installed template's runner ran, in its global mode, in the caller's folder."""
    where = f"stdout={run.out!r} stderr={run.err!r}"
    assert run.probe and run.rc == 5, where
    assert run.probe["argv"] == args, where
    assert run.probe["root"] == str(root), where
    assert run.probe["global"] == "1", where
    assert run.probe["caller_cwd_raw"] == str(cwd) and run.probe["caller_cwd"] == str(cwd), where


@needs_posix
@pytest.mark.parametrize("name", POSIX_SHELLS)
def test_outside_a_project_the_launcher_runs_the_installed_template(name: str, tmp_path: Path, uv_dirs: dict[str, str]) -> None:
    """pyt install copies the template into the user data folder and this launcher into uv's
    tool bin folder: run where no project is, it runs that copy's runner, in its global mode
    (PYTEMPLATE_GLOBAL=1: help, new, doctor, install, uninstall), with argv untouched."""
    installed = _installed(tmp_path / "data")
    launcher, away = _outside(tmp_path)
    env = _clean_env(XDG_DATA_HOME=str(tmp_path / "data"), **uv_dirs)
    _check_installed(Run([*_shell_argv(name), launcher, "__probe", "5", "0", *ARGS], away, env), installed, away)


@needs_posix
@pytest.mark.parametrize("xdg", ["unset", "relative"])
def test_the_installed_template_is_found_through_home(xdg: str, tmp_path: Path, uv_dirs: dict[str, str]) -> None:
    """Without an absolute XDG_DATA_HOME (the XDG spec ignores a relative one) the installed
    template is ~/.local/share/pytemplate/template. A relative XDG_DATA_HOME would name a folder
    below the caller's: a decoy there must never run."""
    home = tmp_path / "home"
    installed = _installed(home / ".local" / "share")
    launcher, away = _outside(tmp_path)
    _installed(away / "data")  # the decoy
    env = _clean_env(HOME=str(home), **uv_dirs)
    env.pop("XDG_DATA_HOME", None)
    if xdg == "relative":
        env["XDG_DATA_HOME"] = "data"
    _check_installed(Run(["/bin/sh", launcher, "__probe", "5", "0", "x"], away, env), installed, away, ["x"])


@needs_posix
def test_a_relative_home_names_no_installed_template(tmp_path: Path) -> None:
    """A relative HOME would name a folder below the caller's (anybody's, in /tmp): like the
    runner's cmd_install.data_home, the launcher then looks nowhere."""
    launcher, away = _outside(tmp_path)
    _installed(away / "home" / ".local" / "share")  # the decoy
    env = _clean_env(HOME="home")
    env.pop("XDG_DATA_HOME", None)
    run = Run(["/bin/sh", launcher, "__probe", "5", "0", "x"], away, env)
    assert run.rc == 2 and NO_ROOT in run.err and INSTALL_HINT in run.err and not run.probe, run.out + run.err


@needs_posix
def test_a_projects_runner_never_runs_in_global_mode(tmp_path: Path) -> None:
    """A PYTEMPLATE_GLOBAL of the caller (a stale export) never reaches a project's runner."""
    run = Run(["/bin/sh", "pyt", "__probe", "0", "0", "x"], ROOT, {**_clean_env(), "PYTEMPLATE_GLOBAL": "1"})
    run.check(0, ROOT, args=["x"])
    assert run.probe["global"] == "", run.probe


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to make files another user owns")
def test_the_installed_template_of_another_user_is_never_run(tmp_path: Path) -> None:
    """The installed template lives in a folder the environment names (XDG_DATA_HOME, HOME):
    the launcher runs it only when it is the user's own, like a folder found by walking up."""
    import pwd

    nobody = pwd.getpwnam("nobody")
    data = tmp_path / "data"
    entry = data / "pytemplate" / "template" / ".pytemplate" / "pyt.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("print('PWNED')\n", encoding="utf-8")
    for path in (data, data / "pytemplate", entry.parent.parent, entry.parent, entry):
        os.chown(path, nobody.pw_uid, nobody.pw_gid)
    launcher, away = _outside(tmp_path)
    for shell in ("sh", "dash", "bash"):
        if not shutil.which(shell):
            continue
        r = subprocess.run([shell, str(launcher), "help"], cwd=away, capture_output=True, text=True, timeout=60, check=False,
                           env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "XDG_DATA_HOME": str(data)})  # fmt: skip
        assert r.returncode == 2 and "is not yours" in r.stderr and "PWNED" not in r.stdout + r.stderr, (shell, r.stdout, r.stderr)


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to make files another user owns")
@pytest.mark.parametrize("launcher", ["pyt", "pyt.ps1"])
def test_a_link_to_your_own_pyt_py_in_another_users_folder_is_never_run(launcher: str, tmp_path: Path) -> None:
    """The ownership rule read only .pytemplate/pyt.py, and a hard link keeps the owner of the
    file it links: on macOS (no hard-link protection) any user may link a pyt.py of yours (the
    installed template's, at a known path) into a .pytemplate of theirs next to a runner/ of
    theirs, which pyt.py then imports, as you, wherever you type pyt below it (/tmp,
    /Users/Shared). The folder must be yours too. Here root links (Linux protects hard links)."""
    import pwd

    nobody = pwd.getpwnam("nobody")
    mine = tmp_path / "mine" / ".pytemplate"
    mine.mkdir(parents=True)
    shutil.copyfile(ROOT / ".pytemplate" / "pyt.py", mine / "pyt.py")
    theirs = tmp_path / "shared" / ".pytemplate"
    (theirs / "runner").mkdir(parents=True)
    os.link(mine / "pyt.py", theirs / "pyt.py")  # owned by the caller, like the file it links
    (theirs / "runner" / "__init__.py").write_text("print('PWNED')\n", encoding="utf-8")
    (theirs / "runner" / "cli.py").write_text("def main(argv, entry=False):\n    print('PWNED', argv)\n    return 0\n", encoding="utf-8")
    for path in (theirs.parent, theirs, theirs / "runner", theirs / "runner" / "__init__.py", theirs / "runner" / "cli.py"):
        os.chown(path, nobody.pw_uid, nobody.pw_gid)
    work = theirs.parent / "work"
    work.mkdir()
    (tmp_path / "bin").mkdir()
    shutil.copyfile(ROOT / launcher, tmp_path / "bin" / launcher)
    if launcher == "pyt":
        argv: list[str | Path] = ["/bin/sh", tmp_path / "bin" / "pyt", "help"]
    else:
        argv = [_pwsh(), "-NoProfile", "-NonInteractive", "-File", tmp_path / "bin" / "pyt.ps1", "help"]
    run = Run(argv, work, _clean_env(**_nothing_installed(tmp_path)))
    assert run.rc == 2 and "is not yours" in run.err and "PWNED" not in run.out + run.err, run.out + run.err


# Folder names pyt.ps1 once handed to a native program as another folder, with the siblings it
# named instead: PowerShell 7 globs a native argument that is not a quoted literal (Linux/macOS),
# and its legacy passing drops the double quotes of one. `p ` sorts before every other sibling a
# glob matches, so it is the one `p*` and `p?` name first.
MISREAD = {"p[0-9]": ["p1"], "p*": ["p ", "p1"], "p?": ["p ", "p1"], 'q"r': ["qr"]}
MISREAD_CASES = [  # (launcher, $PSNativeCommandArgumentPassing, folder name, place)
    *[(launcher, "", name, "project") for launcher in ("pyt", "pyt.ps1") for name in MISREAD],
    *[("pyt.ps1", "Legacy", name, "project") for name in ("p[0-9]", 'q"r')],
    *[(launcher, "", name, "installed") for launcher in ("pyt", "pyt.ps1") for name in ("p[0-9]", "p*")],
]


def _not_yours(entry: Path) -> None:
    """Make the runner `entry` (a .pytemplate/pyt.py that exists) another user's, as `test -O`
    reads it: its file and folders given to nobody as root; else replaced by a link to
    /dev/null, which root owns (test -O follows the link)."""
    if os.geteuid() == 0:
        import pwd

        nobody = pwd.getpwnam("nobody")
        for path in (entry.parent.parent, entry.parent, entry):
            os.chown(path, nobody.pw_uid, nobody.pw_gid)
    else:
        entry.unlink()
        entry.symlink_to(os.devnull)


def _run_copy(launcher: Path, args: list[str], cwd: Path, env: dict[str, str], mode: str = "") -> Run:
    """Run the copy `launcher` from `cwd`: pyt with /bin/sh, pyt.ps1 with pwsh (with that
    $PSNativeCommandArgumentPassing when `mode` names one)."""
    if launcher.suffix != ".ps1":
        return Run(["/bin/sh", launcher, *args], cwd, env)
    words = " ".join("'" + a.replace("'", "''") + "'" for a in [str(launcher), *args])
    code = (f"$PSNativeCommandArgumentPassing = '{mode}'\n" if mode else "") + f"& {words}\nexit $LASTEXITCODE"
    return Run([_pwsh(), "-NoProfile", "-NonInteractive", "-EncodedCommand", _ps_encoded(code)], cwd, env)


def _misread_setup(tmp: Path, whose: str, name: str, place: str) -> tuple[Path, Path]:
    """The runner the launcher finds (a project `<tmp>/t/<name>` it walks up to, or the installed
    template of the data folder `<tmp>/t/<name>`) and the siblings MISREAD names, each with a
    .pytemplate/pyt.py: `whose` the runner is (another user's runner prints PWNED); the siblings
    are the other owner's. Returns that folder and the folder to run the launcher from."""
    base = tmp / "t"
    folder = base / name
    if place == "project":
        root, siblings = folder, [base / s for s in MISREAD[name]]
    else:
        root, siblings = folder / "pytemplate" / "template", [base / s / "pytemplate" / "template" for s in MISREAD[name]]
    if whose == "yours":
        _copy_project(root)
    else:
        (root / ".pytemplate").mkdir(parents=True)
        (root / ".pytemplate" / "pyt.py").write_text("print('PWNED')\n", encoding="utf-8")
        _not_yours(root / ".pytemplate" / "pyt.py")
    for sibling in siblings:
        (sibling / ".pytemplate").mkdir(parents=True)
        (sibling / ".pytemplate" / "pyt.py").write_text("print('PWNED')\n" if whose == "yours" else "raise SystemExit(0)\n", encoding="utf-8")
        if whose == "yours":
            _not_yours(sibling / ".pytemplate" / "pyt.py")
    cwd = tmp / "away"
    if place == "project":
        cwd = root / "src"
    cwd.mkdir(parents=True, exist_ok=True)
    return root, cwd


@needs_posix
@pytest.mark.parametrize("whose", ["theirs", "yours"])
@pytest.mark.parametrize(("launcher", "mode", "name", "place"), MISREAD_CASES)
def test_the_ownership_rule_reads_the_folder_it_runs(
    launcher: str, mode: str, name: str, place: str, whose: str, tmp_path: Path, uv_dirs: dict[str, str]
) -> None:
    """pyt.ps1 handed the runner it found to /bin/sh's ownership check unquoted, and PowerShell 7
    globs such an argument off Windows: in a folder named p[0-9] the owner of p1 next to it
    decided. Another user's runner there ran, as the user, when p1 was the user's; the user's own
    project (or installed template) was refused when p1 was another user's. Legacy passing
    dropped the quotes of q"r: the check read qr, and uv then ran qr's runner. Both launchers
    check, and run, the folder they found (as root: nobody's files; else links to a file of root's)."""
    if not (os.geteuid() == 0 or os.stat(os.devnull).st_uid != os.geteuid()):
        pytest.skip("no file of another user to link to")
    root, cwd = _misread_setup(tmp_path, whose, name, place)
    (tmp_path / "bin").mkdir()
    shutil.copyfile(ROOT / launcher, tmp_path / "bin" / launcher)
    data = str(tmp_path / "t" / name) if place == "installed" else str(tmp_path / "no-data")
    env = _clean_env(XDG_DATA_HOME=data, **uv_dirs)
    run = _run_copy(tmp_path / "bin" / launcher, ["__probe", "5", "0", "x"], cwd, env, mode)
    where = f"stdout={run.out!r} stderr={run.err!r}"
    assert "PWNED" not in run.out + run.err, where
    if whose == "theirs":
        assert run.rc == 2 and "is not yours" in run.err and str(root / ".pytemplate" / "pyt.py") in run.err, where
    else:
        assert run.rc == 5 and run.probe and run.probe["argv"] == ["x"], where
        assert run.probe["root"] == str(root) and run.probe["global"] == ("1" if place == "installed" else ""), where


def _old_project(dest: Path) -> Path:
    """A project made before the launchers were renamed: its runner is .pytemplate/deploy.py."""
    project = _copy_project(dest)
    (project / ".pytemplate" / "pyt.py").rename(project / ".pytemplate" / "deploy.py")
    (project / "pyt").unlink()
    return project


@needs_posix
@pytest.mark.parametrize("name", POSIX_SHELLS)
def test_a_project_made_before_the_rename_runs_its_deploy_py(name: str, tmp_path: Path) -> None:
    """The installed pyt, run inside a project made before the launchers were renamed, runs that
    project's .pytemplate/deploy.py (a project's runner: never the global mode)."""
    old = _old_project(tmp_path / "old")
    launcher, _ = _outside(tmp_path)
    run = Run([*_shell_argv(name), launcher, "__probe", "5", "0", *ARGS], old / "src", _clean_env(**_nothing_installed(tmp_path)))
    where = f"stdout={run.out!r} stderr={run.err!r}"
    assert run.rc == 5 and run.probe, where
    assert run.probe["argv"] == ARGS and run.probe["root"] == str(old) and run.probe["global"] == "", where


@needs_posix
def test_pyt_py_comes_before_deploy_py(tmp_path: Path) -> None:
    """A launcher next to .pytemplate/deploy.py (an old project given the new launcher) runs it;
    a folder that holds both runs pyt.py."""
    old = _old_project(tmp_path / "old")
    shutil.copyfile(LAUNCHER, old / "pyt")
    both = _copy_project(tmp_path / "both")
    (both / ".pytemplate" / "deploy.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
    (tmp_path / "away").mkdir()
    env = _clean_env(**_nothing_installed(tmp_path))
    for project in (old, both):
        run = Run(["/bin/sh", project / "pyt", "__probe", "5", "0", "x"], tmp_path / "away", env)
        where = f"{project.name}: stdout={run.out!r} stderr={run.err!r}"
        assert run.rc == 5 and run.probe and run.probe["root"] == str(project), where


# --- niubash hygiene on every exit path (simulated: niubash itself is Windows only) --------------------

FUNCTIONS = re.findall(r"(?m)^(_pt_\w+)\(\) \{", LAUNCHER.read_text(encoding="ascii"))


@needs_posix
@pytest.mark.parametrize("case", ["ok", "no-project", "no-uv", "installed"])
@pytest.mark.parametrize("name", ["bash", "dash", "busybox", "ksh", "mksh", "yash"])
def test_in_process_run_leaves_no_name_behind(name: str, case: str, tmp_path: Path, request: pytest.FixtureRequest) -> None:
    """niubash runs ./pyt inside the calling shell: no _pt_ name may survive any exit (errors
    too), the exports are dropped and the caller's UV_PYTHON is kept. Simulated by sourcing the
    launcher with an EXIT trap that reports what is left when its `exit` ends the shell (not in
    zsh: after the launcher's `emulate sh` an exit from a sourced file skips the trap).
    `installed`: no project, so the installed template runs (PYTEMPLATE_GLOBAL=1 dropped too)."""
    argv = _shell_argv(name)
    assert len(FUNCTIONS) >= 10, FUNCTIONS
    launcher, cwd, code = LAUNCHER, ROOT, 5
    keep = {"UV_PYTHON": str(tmp_path / "no" / "python"), **_python_traps(tmp_path)}  # the session's own
    env = _clean_env(__RUBASH_SHELL_NAME="1", **keep)
    if case == "ok":
        env["PYTEMPLATE_GLOBAL"] = "1"  # a stale export: never the global mode for a project's runner
    elif case == "no-project":
        launcher, cwd, code = tmp_path / "pyt", tmp_path, 2
        shutil.copyfile(LAUNCHER, launcher)
        env.update(_nothing_installed(tmp_path))
    elif case == "no-uv":
        if _system_uv():
            pytest.skip("uv is installed in a system folder the launcher always searches")
        code = 127
        env = _no_uv_env(tmp_path, __RUBASH_SHELL_NAME="1", **keep)
    elif case == "installed":
        installed = _installed(tmp_path / "data")
        launcher, cwd = _outside(tmp_path)
        env.update(XDG_DATA_HOME=str(tmp_path / "data"), **request.getfixturevalue("uv_dirs"))
    report = (
        "printf 'LEFT:'; set | grep '^_pt_' | tr '\\n' ' '; printf '\\n'; "
        f"for f in {' '.join(FUNCTIONS)}; do if command -v \"$f\" >/dev/null 2>&1; then printf 'FUNC:%s\\n' \"$f\"; fi; done; "
        "printf 'VARS:%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' \"${PYTEMPLATE_LAUNCHER-unset}\" \"${PYTEMPLATE_CALLER_CWD-unset}\" "
        "\"${PYTEMPLATE_GLOBAL-unset}\" \"${UV_PYTHON-unset}\" \"${PYTHONHOME-unset}\" \"${PYTHONPATH-unset}\" \"${UV_WORKING_DIR-unset}\" "
        "\"${UV_MANAGED_PYTHON-unset}\" \"${UV_NO_MANAGED_PYTHON-unset}\""
    )
    ptcmd = f"trap {q(report)} EXIT; set -- __probe {code} 0 x; . {q(str(launcher))}"
    run = Run([*argv, "-c", 'eval "$PTCMD"'], cwd, {**env, "PTCMD": ptcmd})
    lines = run.out.splitlines()
    where = f"stdout={run.out!r} stderr={run.err!r}"
    assert run.rc == code, where
    assert "LEFT:" in lines and not [ln for ln in lines if ln.startswith("FUNC:")], where
    assert "VARS:unset|unset|unset|" + "|".join(keep.values()) in lines, where
    if case in ("ok", "installed"):
        assert run.probe and run.probe["launcher"] == "sh:niubash" and run.probe["argv"] == ["x"], where
        assert run.probe["cwd"] == str(cwd), where  # not UV_WORKING_DIR
        assert run.probe["global"] == ("1" if case == "installed" else ""), where
    if case == "installed":
        assert run.probe["root"] == str(installed), where


@needs_posix
@pytest.mark.parametrize("name", ["bash", "zsh"])
def test_an_in_process_run_runs_the_project_it_is_typed_in(name: str, tmp_path: Path, uv_dirs: dict[str, str]) -> None:
    """niubash runs ./pyt inside the calling shell, where $0 is the caller's. The installed pyt
    (its own folder holds no project) then took the folder of that $0 for its own: a helper
    script of project A that runs `cd ../B/src && pyt ...` ran A's runner, whose commands then
    changed the wrong project. The shell names the file it runs ($BASH_SOURCE, zsh's %x): only
    that name counts, and the walk-up finds B. Simulated by sourcing the launcher from the script."""
    argv = _shell_argv(name)
    a, b = _copy_project(tmp_path / "A"), _copy_project(tmp_path / "B")
    launcher, away = _outside(tmp_path)
    script = a / "release.sh"
    script.write_text(f"cd {q(str(b / 'src'))} || exit 9\nset -- __probe 5 0 x\n. {q(str(launcher))}\n", encoding="utf-8", newline="\n")
    run = Run([*argv, script], away, _clean_env(__RUBASH_SHELL_NAME="1", **_nothing_installed(tmp_path), **uv_dirs))
    where = f"stdout={run.out!r} stderr={run.err!r}"
    assert run.rc == 5 and run.probe and run.probe["argv"] == ["x"], where
    assert run.probe["root"] == str(b) and run.probe["caller_cwd"] == str(b / "src"), where


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to make files another user owns")
@pytest.mark.parametrize("name", ["bash", "dash", "busybox", "ksh", "mksh", "yash"])
def test_an_in_process_run_never_takes_the_callers_folder_for_its_own(name: str, tmp_path: Path) -> None:
    """Under `niu -c "pyt help"` $0 is `niu`: the installed pyt took the current folder for its
    own and ran the .pytemplate/pyt.py there without the ownership rule of the walk-up (at C:\\,
    one any user may create). A name that is no file of that folder names no folder of the
    launcher: the walk-up decides, and refuses another user's runner."""
    import pwd

    argv = _shell_argv(name)
    nobody = pwd.getpwnam("nobody")
    shared = tmp_path / "shared"
    entry = shared / ".pytemplate" / "pyt.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("print('PWNED')\n", encoding="utf-8")
    for path in (shared, entry.parent, entry):
        os.chown(path, nobody.pw_uid, nobody.pw_gid)
    launcher, _ = _outside(tmp_path)
    env = _clean_env(__RUBASH_SHELL_NAME="1", PTCMD=f"set -- help; . {q(str(launcher))}", **_nothing_installed(tmp_path))
    run = Run([*argv, "-c", 'eval "$PTCMD"', "niu"], shared, env)  # $0 = niu, as under niu -c
    assert run.rc == 2 and "is not yours" in run.err and "PWNED" not in run.out + run.err, run.out + run.err


@needs_posix
def test_the_no_uv_case_runs_in_a_folder_that_is_not_ascii(tmp_path: Path) -> None:
    """`./pyt selftest` must pass in a project folder that is not ASCII (`My Game e-acute`):
    the no-uv cases build their environment from scratch, and without the caller's locale yash
    could not read the launcher's path in PTCMD, ran nothing and exited 0."""
    import locale

    if locale.nl_langinfo(locale.CODESET).upper().replace("-", "") != "UTF8":
        pytest.skip("this test process does not run in a UTF-8 locale")
    argv = _shell_argv("yash")
    if _system_uv():
        pytest.skip("uv is installed in a system folder the launcher always searches")
    project = tmp_path / "My Game \u00e9" / "p"
    (project / ".pytemplate").mkdir(parents=True)
    (project / ".pytemplate" / "pyt.py").write_text("", encoding="utf-8")
    shutil.copyfile(LAUNCHER, project / "pyt")
    ptcmd = f"set -- __probe 127 0 x; . {q(str(project / 'pyt'))}"
    run = Run([*argv, "-c", 'eval "$PTCMD"'], project, {**_no_uv_env(tmp_path, __RUBASH_SHELL_NAME="1"), "PTCMD": ptcmd})
    assert run.rc == 127 and "uv not found" in run.err, f"stdout={run.out!r} stderr={run.err!r}"


# --- UV_PYTHON never picks the runner's Python ---------------------------------------------------------


@needs_posix
def test_launcher_clears_the_callers_uv_python(tmp_path: Path) -> None:
    """The runner runs on the project's Python: a caller's UV_PYTHON (here a missing one, which uv
    would refuse) must not choose it."""
    env = _clean_env(UV_PYTHON=str(tmp_path / "no" / "python3.10"))
    Run(["/bin/sh", "pyt", "__probe", "0", "0", "x"], ROOT, env).check(0, ROOT, args=["x"])


def _python_traps(tmp: Path) -> dict[str, str]:
    """What a caller may export that breaks the Python uv starts the runner on (a PYTHONHOME
    without a stdlib, a PYTHONPATH module shadowing one the runner imports), moves the folder
    uv starts it in (UV_WORKING_DIR), or stops uv itself: UV_MANAGED_PYTHON and
    UV_NO_MANAGED_PYTHON next to the launchers' --python-preference ("cannot be used with")."""
    shadow = tmp / "shadow"
    shadow.mkdir(exist_ok=True)
    (shadow / "tomllib.py").write_text('raise SystemExit("shadowed tomllib")\n', encoding="utf-8")
    (tmp / "elsewhere").mkdir(exist_ok=True)
    return {
        "PYTHONHOME": str(tmp / "no-home"), "PYTHONPATH": str(shadow), "UV_WORKING_DIR": str(tmp / "elsewhere"),
        "UV_MANAGED_PYTHON": "1", "UV_NO_MANAGED_PYTHON": "1",
    }  # fmt: skip


@needs_posix
@pytest.mark.parametrize("launcher", ["pyt", "pyt.ps1"])
def test_launcher_ignores_the_callers_python_home_path_and_uv_working_dir(launcher: str, tmp_path: Path) -> None:
    """README: the runner ignores PYTHONHOME, PYTHONPATH and UV_WORKING_DIR. The launchers remove
    them for uv (like UV_PYTHON): every command used to die with `Failed to import encodings`,
    and UV_WORKING_DIR moved the runner (and the caller's folder with it) elsewhere."""
    env = _clean_env(**_python_traps(tmp_path))
    cwd = ROOT / ".pytemplate"
    if launcher == "pyt":
        run = Run(["/bin/sh", LAUNCHER, "__probe", "0", "0", "x"], cwd, env)
    else:
        run = Run([_pwsh(), "-NoProfile", "-NonInteractive", "-File", ROOT / "pyt.ps1", "__probe", "0", "0", "x"], cwd, env)
    run.check(0, cwd, args=["x"])
    assert run.probe["cwd"] == str(cwd), run.out + run.err


@needs_posix
def test_user_uv_python_older_than_3_11() -> None:
    old = next((p for p in (shutil.which(f"python3.{m}") for m in (10, 9, 8)) if p), None)
    if old is None:
        pytest.skip("no Python older than 3.11 here")
    run = Run(["/bin/sh", "pyt", "help"], ROOT, _clean_env(UV_PYTHON=old))
    assert run.rc == 0 and "tomllib" not in run.err, run.out + run.err
    # `uv run` by hand keeps UV_PYTHON: the entry point stops with one clear line, no traceback.
    uv = shutil.which("uv", path=_clean_env().get("PATH"))
    if uv is None:
        pytest.skip("uv not on PATH")
    run = Run([uv, "run", "--quiet", "--script", ROOT / ".pytemplate" / "pyt.py", "help"], ROOT, _clean_env(UV_PYTHON=old))
    assert run.rc == 3, run.out + run.err
    assert "3.11" in run.err and "UV_PYTHON" in run.err
    assert "Traceback" not in run.err and "internal runner error" not in run.err


def test_entry_refuses_python_older_than_3_11() -> None:
    entry = ROOT / ".pytemplate" / "pyt.py"
    code = (
        "import runpy, sys; sys.version_info = (3, 10, 0, 'final', 0); sys.argv = ['pyt.py', 'help']; "
        f"runpy.run_path({str(entry)!r}, run_name='__main__')"
    )
    r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60, env=_clean_env(), check=False)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "3.11" in r.stderr and "UV_PYTHON" in r.stderr and r.stderr.startswith("error: ")
    assert "Traceback" not in r.stderr and "internal runner error" not in r.stderr and not r.stdout


def test_entry_checks_the_version_before_importing_the_runner() -> None:
    text = (ROOT / ".pytemplate" / "pyt.py").read_text(encoding="utf-8")
    assert text.index("sys.version_info < (3, 11)") < text.index("from runner.cli import main")


@needs_posix
@pytest.mark.parametrize("launcher", ["pyt", "pyt.ps1"])
def test_runner_runs_on_python_cpython_whatever_the_caller_pins(launcher: str, tmp_path: Path) -> None:
    """uv reads the .python-version next to .pytemplate/pyt.py (python.cpython): neither a
    .python-version in the caller's folder nor the caller's UV_PYTHON picks the runner's Python
    (CLAUDE.md 5.2). The runner therefore runs on whatever python.cpython says (3.11+)."""
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"3\.\d+", pinned), pinned
    other = "3.11" if pinned != "3.11" else "3.12"
    (tmp_path / ".python-version").write_text(other + "\n", encoding="utf-8")
    env = _clean_env(UV_PYTHON=other)
    if launcher == "pyt":
        run = Run(["/bin/sh", LAUNCHER, "__probe", "0", "0", "x"], tmp_path, env)
    else:
        run = Run([_pwsh(), "-NoProfile", "-NonInteractive", "-File", ROOT / "pyt.ps1", "__probe", "0", "0", "x"], tmp_path, env)
    assert run.rc == 0 and run.probe, run.out + run.err
    assert run.probe["argv"] == ["x"] and Path(str(run.probe["root"])) == ROOT
    assert str(run.probe["python"]).startswith(pinned + "."), (run.probe["python"], pinned)


# --- the Python the runner starts on (project.launcher_python) --------------------------------------


def _request_of(argv: list[str | Path], project: Path, tmp: Path) -> tuple[str, str]:
    """The --python= request and the --python-preference a launcher gives uv (a fake uv that
    prints its arguments)."""
    fake = tmp / "fake-uv"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do printf \'ARG:%s\\n\' "$a"; done\n', encoding="ascii")
    fake.chmod(0o755)
    run = Run([*argv, "help"], project, _clean_env(UV=str(fake)))
    args = [ln[4:] for ln in run.out.splitlines() if ln.startswith("ARG:")]
    assert run.rc == 0 and args[:2] == ["run", "--quiet"] and args[2].startswith("--python="), run.out + run.err
    assert args[3] == "--python-preference" and args[5] == "--script", args
    return args[2].removeprefix("--python="), args[4]


@needs_posix
@pytest.mark.parametrize("launcher", ["pyt", "pyt.ps1"])
def test_the_runner_starts_on_python_cpython_once_the_project_has_an_environment(launcher: str, tmp_path: Path) -> None:
    """The launchers make the --python= request and --python-preference of
    project.launcher_python: while the project has an environment, none (an empty value), so uv
    follows .python-version (python.cpython) as it always did, and only-managed; else ">=3.11",
    any CPython 3.11 or newer, a system one too (managed), where the runner moves the commands
    that need python.cpython onto it. The environment counts when its interpreter is there:
    .venv-wsl too (WSL on a Windows checkout), never a dangling link (its Python is gone)."""
    sys.path.insert(0, str(ROOT / ".pytemplate"))
    from runner import project as runner_project

    proj = _copy_project(tmp_path / "p")
    shutil.copyfile(ROOT / "pyt.ps1", proj / "pyt.ps1")
    argv: list[str | Path] = ["/bin/sh", proj / "pyt"] if launcher == "pyt" else [_pwsh(), "-NoProfile", "-NonInteractive", "-File", proj / "pyt.ps1"]
    venv, wsl = proj / ".venv" / "bin" / "python", proj / ".venv-wsl" / "bin" / "python"

    def expect(request: str, preference: str) -> None:
        assert _request_of(argv, proj, tmp_path) == (request, preference) == runner_project.launcher_python(proj)

    expect(">=3.11", "managed")
    venv.parent.mkdir(parents=True)
    venv.symlink_to(tmp_path / "gone" / "python")  # dangling
    expect(">=3.11", "managed")
    venv.unlink()
    venv.symlink_to(sys.executable)
    expect("", "only-managed")
    venv.unlink()
    wsl.parent.mkdir(parents=True)
    wsl.symlink_to(sys.executable)
    expect("", "only-managed")


# --- the Windows-only helpers are plain sh: run them in every POSIX shell ---------------------------

HELPERS = ("_pt_slashes", "_pt_backslashes", "_pt_drive", "_pt_winpath", "_pt_try_uv", "_pt_try_dir", "_pt_expand", "_pt_uv_in_list", "_pt_uv_from_registry")


def _launcher_functions(*names: str) -> str:
    text = LAUNCHER.read_text(encoding="ascii")
    out = []
    for name in names:
        m = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", text)
        assert m, f"{name}() not found in pyt"
        out.append(m.group(0))
    return "".join(out)


def _run_helpers(name: str, calls: str, env: dict[str, str]) -> list[str]:
    """Run the launcher's helpers as on Windows (_pt_win=1) in shell `name`: code through the
    environment, never argv (section 4.7)."""
    prelude = ("emulate sh\n" if name == "zsh" else "") + "_pt_win=1\n_pt_exe=uv\n"
    run = Run([*_shell_argv(name), "-c", 'eval "$PT_CODE"'], ROOT, {**env, "PT_CODE": prelude + _launcher_functions(*HELPERS) + calls})
    assert run.rc == 0, run.out + run.err
    return run.out.splitlines()


WINPATH_CASES = [
    ("/c/Users/x", "C:\\Users\\x"),
    ("/C/x", "C:\\x"),
    ("/c", "C:\\"),
    ("/cygdrive/d/a b/c", "D:\\a b\\c"),
    ("//server/share/x", "\\\\server\\share\\x"),
    ("C:/x/y", "C:\\x\\y"),
    ("C:\\x\\y", "C:\\x\\y"),
]
EXPAND_CASES = [
    ("%PT_SYSROOT%\\x", "OK:C:\\Windows\\x"),
    ("%pt_sysroot%\\x", "OK:C:\\Windows\\x"),  # MSYS2/Cygwin upper-case SYSTEMROOT: retried upper-case
    ("%PT_NOPE%\\x", "FAIL"),
    ("100%", "OK:100%"),
    ("%1%", "FAIL"),
    ("plain", "OK:plain"),
]


@needs_posix
@pytest.mark.parametrize("name", ["sh", "dash", "bash", "busybox", "ksh", "mksh", "yash", "zsh"])
def test_windows_helpers_in_posix_shells(name: str) -> None:
    calls = "".join(f"_pt_winpath {q(raw)}\nprintf 'W:%s\\n' \"$_pt_r\"\n" for raw, _ in WINPATH_CASES)
    calls += "".join(f"_pt_drive {q(d)}\nprintf 'D:%s\\n' \"$_pt_r\"\n" for d in ("c", "z", "C", "cd"))
    calls += "".join(f"if _pt_expand {q(raw)}; then printf 'E:OK:%s\\n' \"$_pt_r\"; else printf 'E:FAIL\\n'; fi\n" for raw, _ in EXPAND_CASES)
    out = _run_helpers(name, calls, _clean_env(PT_SYSROOT="C:\\Windows"))
    assert [ln[2:] for ln in out if ln.startswith("W:")] == [want for _, want in WINPATH_CASES]
    assert [ln[2:] for ln in out if ln.startswith("D:")] == ["C", "Z", "C", "cd"]
    assert [ln[2:] for ln in out if ln.startswith("E:")] == [want for _, want in EXPAND_CASES]


@needs_posix
@pytest.mark.parametrize("name", ["sh", "dash", "bash", "busybox", "ksh", "mksh", "yash", "zsh"])
def test_winpath_inside_the_msys_root(name: str, tmp_path: Path) -> None:
    """A path inside the MSYS2/Cygwin root (/home/me/p) needs that root's cygpath. A non-login MSYS2
    shell has no /usr/bin on PATH (or WinuxCmd's cygpath, which answers \\home\\me\\p): then the
    POSIX path is kept, which MSYS converts for uv.exe itself, never turned into \\home\\me\\p
    (a folder on the current drive: every command failed)."""
    if Path("/usr/bin/cygpath").exists():
        pytest.skip("a real /usr/bin/cygpath here")
    fake = tmp_path / "bin"
    fake.mkdir()
    env = _clean_env(PATH=f"{fake}:/usr/bin:/bin")
    calls = "_pt_winpath /home/me/p\nprintf 'W:%s\\n' \"$_pt_r\"\n_pt_winpath '/tmp/a b'\nprintf 'W:%s\\n' \"$_pt_r\"\n"
    assert _run_helpers(name, calls, env) == ["W:/home/me/p", "W:/tmp/a b"]  # no cygpath at all
    cygpath = fake / "cygpath"
    cygpath.write_text('#!/bin/sh\n[ "$1 $2" = "-m --" ] && printf \'%s\\n\' "C:/msys64$3"\n', encoding="ascii", newline="\n")
    cygpath.chmod(0o755)
    assert _run_helpers(name, calls, env) == ["W:C:\\msys64\\home\\me\\p", "W:C:\\msys64\\tmp\\a b"]
    cygpath.write_text("#!/bin/sh\nprintf '%s\\n' '\\home\\me\\p'\n", encoding="ascii", newline="\n")  # WinuxCmd's
    assert _run_helpers(name, calls, env) == ["W:/home/me/p", "W:/tmp/a b"]


def test_winpath_asks_the_roots_own_cygpath_first() -> None:
    """The MSYS/Cygwin root's /usr/bin/cygpath comes before the first cygpath on PATH (WinuxCmd's
    copy from niubash or xonsh-shell-kit, which knows no mounts)."""
    body = _launcher_functions("_pt_winpath")
    assert 0 <= body.index("/usr/bin/cygpath -m") < body.index("command -v cygpath"), body


@needs_posix
@pytest.mark.parametrize("name", ["sh", "dash", "bash", "busybox", "ksh", "mksh", "yash", "zsh"])
def test_registry_path_quoted_entries(name: str, tmp_path: Path) -> None:
    """Quoted PATH entries ("C:\\Program Files\\x"), as some installers write them, are found."""
    uvdir = tmp_path / "q dir"
    uvdir.mkdir()
    (uvdir / "uv").write_text("#!/bin/sh\n", encoding="ascii")
    (uvdir / "uv").chmod(0o755)
    fake = tmp_path / "fake"
    fake.mkdir()
    reg = fake / "reg.exe"  # CRLF output with the 4-space layout of reg.exe
    reg.write_text(
        "#!/bin/sh\ncase $2 in\n"
        "    HKCU*) printf '\\r\\nHKEY_CURRENT_USER\\\\Environment\\r\\n    Path    %s    %s\\r\\n\\r\\n' \"$PT_TYPE\" \"$PT_VALUE\" ;;\n"
        "    *) printf '\\r\\nHKEY_LOCAL_MACHINE\\\\x\\r\\n    Path    REG_EXPAND_SZ    %%SystemRoot%%\\\\nope\\r\\n\\r\\n' ;;\n"
        "esac\n",
        encoding="ascii", newline="\n",
    )  # fmt: skip
    reg.chmod(0o755)
    # The helpers probe absolute Windows folders only (X:\..., \\server\...): here the folder is
    # written as a share, //tmp/..., which the Linux and macOS kernels read as /tmp/...
    share = "/" + str(uvdir)
    lists = [f"/nope;{share};/x", f'/nope;"{share}";/x', f'"{share}"', '"%PT_Q%"', "/nope;/x"]
    calls = "".join(f"_pt_uv=\nif _pt_uv_in_list {q(v)}; then printf 'L:%s\\n' \"$_pt_uv\"; else printf 'L:none\\n'; fi\n" for v in lists)
    calls += "_pt_uv=\nif _pt_uv_from_registry; then printf 'R:%s\\n' \"$_pt_uv\"; else printf 'R:none\\n'; fi\n"
    env = _clean_env(PATH=f"{fake}:/usr/bin:/bin", PT_Q=share, PT_TYPE="REG_EXPAND_SZ", PT_VALUE='%PT_NOPE%\\x;"%PT_Q%";/nope')
    found = "/" + str(uvdir / "uv")
    assert _run_helpers(name, calls, env) == [f"L:{found}"] * 4 + ["L:none", f"R:{found}"]
    env.update(PT_TYPE="REG_SZ", PT_VALUE=f'"{share}"')
    assert _run_helpers(name, "_pt_uv=\n_pt_uv_from_registry || :\nprintf 'R:%s\\n' \"$_pt_uv\"\n", env) == [f"R:{found}"]
    env.update(PT_VALUE="/nope")
    assert _run_helpers(name, "_pt_uv=\n_pt_uv_from_registry || :\nprintf 'R:%s\\n' \"$_pt_uv\"\n", env) == ["R:"]


@needs_posix
@pytest.mark.parametrize("name", ["sh", "dash", "bash", "busybox", "ksh", "mksh", "yash", "zsh"])
def test_registry_path_skips_relative_entries(name: str, tmp_path: Path) -> None:
    """A relative entry of the PATH stored in the registry names a folder below the current
    one, which may be anybody's: a uv there ran. So does a root-relative one (\\bin, a folder of
    the drive root, which any user may create; here the path of the folder with one slash or
    backslash first). Only absolute folders are probed, X:\\... or a share (here //tmp/..., which
    the kernel reads as /tmp/...), as pyt.cmd and pyt.ps1 probe them."""
    rel = tmp_path / "rel"
    rel.mkdir()
    (rel / "uv").write_text("#!/bin/sh\n", encoding="ascii")
    (rel / "uv").chmod(0o755)
    lists = ["rel", "./rel", "%PT_REL%", str(rel), str(rel).replace("/", "\\"), f"rel;/{rel}"]
    calls = f"cd {q(str(tmp_path))} || exit 9\n"
    calls += "".join(f"_pt_uv=\nif _pt_uv_in_list {q(v)}; then printf 'L:%s\\n' \"$_pt_uv\"; else printf 'L:none\\n'; fi\n" for v in lists)
    assert _run_helpers(name, calls, _clean_env(PT_REL="rel")) == ["L:none"] * 5 + [f"L:/{rel / 'uv'}"]


# --- the uv search order (CLAUDE.md 4.1), for ./pyt and pyt.ps1 -------------------------------

SEARCH_ORDER = ("envuv", "path", "uvi", "uvi_bin", "xdgbin", "xdgdata_bin", "home_local", "cargo", "home_cargo", "nix")


def _fake_uvs(tmp: Path) -> tuple[dict[str, Path], dict[str, str]]:
    """A fake uv (prints FAKE:<place>) in every folder the launchers search, and the environment."""
    home = tmp / "home"
    places = {
        "envuv": tmp / "envuv" / "uv",
        "path": tmp / "path" / "uv",
        "uvi": tmp / "uvi" / "uv",
        "uvi_bin": tmp / "uvi" / "bin" / "uv",
        "xdgbin": tmp / "xdgbin" / "uv",
        "xdgdata_bin": tmp / "share" / "bin" / "uv",
        "home_local": home / ".local" / "bin" / "uv",
        "cargo": tmp / "cargo" / "bin" / "uv",
        "home_cargo": home / ".cargo" / "bin" / "uv",
        "nix": home / ".nix-profile" / "bin" / "uv",
    }
    for name, path in places.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\necho FAKE:{name}\n", encoding="ascii", newline="\n")
        path.chmod(0o755)
    (tmp / "share" / "data").mkdir()
    env = {
        "PATH": f"{tmp / 'path'}:/usr/bin:/bin", "HOME": str(home), "UV": str(places["envuv"]), "CI": "1",
        "UV_INSTALL_DIR": str(tmp / "uvi"), "XDG_BIN_HOME": str(tmp / "xdgbin"),
        "XDG_DATA_HOME": str(tmp / "share" / "data"), "CARGO_HOME": str(tmp / "cargo"),
    }  # fmt: skip
    return places, env


def _pwsh() -> str:
    exe = shutil.which("pwsh")
    if not exe:
        pytest.skip("pwsh not installed")
    return exe


def _ps_encoded(script: str) -> str:
    import base64

    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


@needs_posix
@pytest.mark.parametrize("launcher", ["pyt", "pyt.ps1"])
def test_uv_search_order(launcher: str, tmp_path: Path) -> None:
    """$UV (a file) -> PATH -> UV_INSTALL_DIR[/bin] -> XDG_BIN_HOME -> XDG_DATA_HOME/../bin ->
    ~/.local/bin -> CARGO_HOME/bin -> ~/.cargo/bin -> (system folders) -> ~/.nix-profile/bin:
    remove the winner, the next one must win; none left: exit 127."""
    if any(Path(d, "uv").exists() for d in ("/usr/bin", "/bin")):
        pytest.skip("uv is in /usr/bin or /bin: PATH cannot hide it")
    system = _system_uv()
    order = [p for p in SEARCH_ORDER if not (system and p == "nix")]  # nix comes after the system folders
    places, env = _fake_uvs(tmp_path)
    want = ["FAKE:path", "FAKE:path", *[f"FAKE:{p}" for p in order]]
    bad_uv = [str(tmp_path / "envuv"), str(tmp_path / "missing" / "uv")]  # a folder, a missing file: skipped
    if launcher == "pyt":
        seen = [Run(["/bin/sh", LAUNCHER, "help"], ROOT, {**env, "UV": bad}).out.strip() for bad in bad_uv]
        for place in order:
            seen.append(Run(["/bin/sh", LAUNCHER, "help"], ROOT, env).out.strip())
            places[place].unlink()
        last = Run(["/bin/sh", LAUNCHER, "help"], ROOT, env)
        rc, err = last.rc, last.err
    else:
        ps1 = str(ROOT / "pyt.ps1").replace("'", "''")
        lines = [f"$env:UV = '{bad}'; & '{ps1}' help" for bad in bad_uv] + [f"$env:UV = '{places['envuv']}'"]
        for place in order:
            lines += [f"& '{ps1}' help", f"Remove-Item -LiteralPath '{places[place]}'"]
        lines += [f"& '{ps1}' help 2>$null", "'RC=' + $LASTEXITCODE", "exit 0"]
        r = subprocess.run(
            [_pwsh(), "-NoProfile", "-NonInteractive", "-EncodedCommand", _ps_encoded("\n".join(lines))],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False,
        )  # fmt: skip
        seen = [ln for ln in r.stdout.splitlines() if ln.startswith("FAKE:")]
        rc = 127 if "RC=127" in r.stdout else -1
        err = r.stdout + r.stderr
    assert seen == want
    if not system:
        assert rc == 127, err


@needs_posix
def test_sh_skips_a_uv_without_exec_bit(tmp_path: Path) -> None:
    places, env = _fake_uvs(tmp_path)
    for place in ("envuv", "path", "uvi"):
        places[place].chmod(0o644)  # a broken download: the next folder wins, like pyt.ps1
    assert Run(["/bin/sh", LAUNCHER, "help"], ROOT, env).out.strip() == "FAKE:uvi_bin"


# --- the interactive install prompt (a pseudo-terminal) --------------------------------------------


def _pty_run(argv: list[str | Path], env: dict[str, str], answer: bytes | None, timeout: float = 120) -> tuple[int, str]:
    """Run argv on a pseudo-terminal (stdin, stdout and stderr; it is also the controlling
    terminal) and type `answer` once the [y/N] prompt shows."""
    import fcntl
    import pty
    import select
    import termios

    def controlling_tty() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    master, slave = pty.openpty()
    try:
        p = subprocess.Popen([str(a) for a in argv], cwd=ROOT, env=env, stdin=slave, stdout=slave, stderr=slave, preexec_fn=controlling_tty)
    finally:
        os.close(slave)
    out = b""
    answered = answer is None
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() > deadline:
                p.kill()
                pytest.fail(f"timed out: {out!r}")
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                if p.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO: the terminal has no writer left
                break
            if not chunk:
                break
            out += chunk
            if b"\x1b[6n" in chunk:  # a cursor position query (.NET's console): answer it
                os.write(master, b"\x1b[1;1R")
            if not answered and b"[y/N]" in out:
                os.write(master, answer or b"")
                answered = True
    finally:
        os.close(master)
    return p.wait(timeout=30), out.decode("utf-8", "replace")


INSTALLER = """mkdir -p "$HOME/.local/bin"
printf '%s\\n' '#!/bin/sh' 'echo "FAKEUV $*"' > "$HOME/.local/bin/uv"
chmod +x "$HOME/.local/bin/uv"
echo "installer ran"
"""


@needs_posix
@pytest.mark.parametrize("answer", ["y", "n", "CI"])
@pytest.mark.parametrize("launcher", ["pyt", "pyt.ps1"])
def test_install_prompt_through_a_terminal(launcher: str, answer: str, tmp_path: Path) -> None:
    """No uv, stdin and stderr on a terminal: [y/N]. y runs the official installer (a fake curl
    here) and then the uv it installed; n prints the hints (exit 127); CI=1 never asks."""
    if _system_uv():
        pytest.skip("uv is installed in a system folder the launcher always searches")
    bindir, home = tmp_path / "bin", tmp_path / "home"
    bindir.mkdir()
    home.mkdir()
    (tmp_path / "install.sh").write_text(INSTALLER, encoding="ascii", newline="\n")
    (bindir / "curl").write_text(f"#!/bin/sh\ncat {q(str(tmp_path / 'install.sh'))}\n", encoding="ascii", newline="\n")
    (bindir / "curl").chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "TERM": "dumb"}
    if answer == "CI":
        env["CI"] = "1"
    argv: list[str | Path] = ["/bin/sh", LAUNCHER, "help"] if launcher == "pyt" else [_pwsh(), "-NoProfile", "-File", ROOT / "pyt.ps1", "help"]
    rc, out = _pty_run(argv, env, None if answer == "CI" else answer.encode() + b"\r")
    if answer == "y":
        assert rc == 0 and "installer ran" in out and re.search(r"FAKEUV run --quiet --python=\S* --python-preference (only-)?managed --script ", out), out
        assert out.rstrip().endswith("help"), out
    else:
        assert rc == 127 and "installer ran" not in out and "curl -LsSf" in out and "brew install uv" in out, out
        assert ("[y/N]" in out) == (answer == "n"), out


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to make files another user owns")
def test_a_launcher_outside_a_project_never_runs_another_users_one(tmp_path: Path) -> None:
    """A copy of the launcher outside any project walks up from $PWD: it ran the
    .pytemplate/pyt.py another user had planted in /tmp, as this user. Refused now, with
    how to run it on purpose; nothing of it runs (uv is never even looked for)."""
    import pwd

    nobody = pwd.getpwnam("nobody")
    shared = tmp_path / "shared"
    (shared / ".pytemplate").mkdir(parents=True)
    (shared / ".pytemplate" / "pyt.py").write_text("print('PWNED')\n", encoding="utf-8")
    for path in (shared, shared / ".pytemplate", shared / ".pytemplate" / "pyt.py"):
        os.chown(path, nobody.pw_uid, nobody.pw_gid)
    shared.chmod(0o777)
    (shared / "victim").mkdir()
    copy = tmp_path / "bin" / "pyt"
    copy.parent.mkdir()
    shutil.copy(LAUNCHER, copy)
    for shell in ("sh", "dash", "bash"):
        if not shutil.which(shell):
            continue
        r = subprocess.run([shell, str(copy), "help"], cwd=shared / "victim", capture_output=True, text=True, timeout=60, check=False,
                           env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})  # fmt: skip
        assert r.returncode == 2 and "is not yours" in r.stderr and "PWNED" not in r.stdout + r.stderr, (shell, r.stdout, r.stderr)


@needs_posix
@pytest.mark.parametrize("name", POSIX_SHELLS)
def test_the_walk_up_never_takes_a_drive_root_on_windows(name: str) -> None:
    """On Windows, whose owners the sh launcher does not read, any user may create folders
    at C:\\: a C:\\.pytemplate\\pyt.py is never the project a walk-up finds. Run as on
    Windows (_pt_win=1) in every POSIX shell, like the other Windows helpers (a bare `bash` on
    Windows is the WSL stub)."""
    drives = ("C:/", "C:", "/c", "/cygdrive/d", "/", "C:/Users/x", "/c/Users/x")
    calls = "".join(f'if _pt_foreign "{d}"; then echo "{d} refused"; else echo "{d} taken"; fi\n' for d in drives)
    prelude = ("emulate sh\n" if name == "zsh" else "") + "_pt_win=1\n"
    run = Run([*_shell_argv(name), "-c", 'eval "$PT_CODE"'], ROOT, _clean_env(PT_CODE=prelude + _launcher_functions("_pt_foreign") + calls))
    assert run.rc == 0, run.out + run.err
    assert run.out.splitlines() == ["C:/ refused", "C: refused", "/c refused", "/cygdrive/d refused", "/ refused", "C:/Users/x taken", "/c/Users/x taken"]
