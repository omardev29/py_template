"""Shells: the hidden `__probe` command, doctor checks, shell-setup snippets and `selftest --shells`.

The launchers (deploy, deploy.cmd, deploy.ps1) only find the project root and uv, export
PYTEMPLATE_CALLER_CWD and PYTEMPLATE_LAUNCHER, and hand every argument to
`uv run --quiet --script .pytemplate/deploy.py`. `__probe` is the target the launcher tests
call through each shell to check that argv, the exit code, the cwd and stdin arrive intact.

`selftest --shells` runs these checks through every shell installed on this machine:

    T1 argv   ./deploy __probe 0 0 ARGS from the root: ARGS arrive unchanged
    T2 exit   ./deploy __probe 37: the shell exits with 37
    T3 cwd    ../deploy from src/, and the absolute launcher from a folder outside the
              project: the runner sees the project root and the caller's folder
    T4 shx    xonsh-shell-kit's `!` route (a temporary script run by niubash or by MSYS2's
              non-login bash, from src/): the launcher must not depend on $0 nor cd
    T5 path   minimal PATH (MSYS2 login shells always have one): uv is still found
    T6 stdin  stdin reaches the runner
    T7 hints  uv hidden: exit 127 with install hints (Windows: winget, never curl)
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import proc, ui
from .config import BACKENDS, Config
from .project import IS_WINDOWS, IS_WSL, ROOT, caller_cwd, native_path, user_path
from .ui import DeployError

Check = Callable[[bool | None, str, str], None]


# --- __probe (launcher self-test target, not listed in help) -----------------------------------


def probe(argv: list[str]) -> int:
    """__probe EXIT STDIN(0|1) ARGS...: print one PTPROBE{json} line and exit with EXIT.

    The stdin line is read as bytes and decoded as UTF-8 with surrogateescape, whatever the
    locale: a byte that is not UTF-8 shows up as \\udcXX (raw bytes arrived), while a wrapper
    that re-encoded the text (PowerShell) leaves U+FFFD.
    """
    code = int(argv[0]) if argv and argv[0].lstrip("-").isdigit() else 0
    read_stdin = len(argv) > 1 and argv[1] == "1"
    line: str | None = None
    if read_stdin and sys.stdin is not None:
        raw = sys.stdin.buffer.readline() if hasattr(sys.stdin, "buffer") else sys.stdin.readline().encode("utf-8", "surrogateescape")
        line = raw.decode("utf-8", "surrogateescape").rstrip("\r\n")
    data = {
        "argv": argv[2:],
        "cwd": os.getcwd(),
        "caller_cwd_raw": os.environ.get("PYTEMPLATE_CALLER_CWD"),
        "caller_cwd": str(caller_cwd()),
        "launcher": os.environ.get("PYTEMPLATE_LAUNCHER"),
        "stdin_tty": sys.stdin.isatty() if sys.stdin else None,
        "stdin": line,
        "root": str(ROOT),
    }
    print("PTPROBE" + json.dumps(data, ensure_ascii=True), flush=True)
    return code


def parse_probe(stdout: str) -> dict[str, object] | None:
    """Return the JSON of the first PTPROBE line in a probe's stdout (None if there is none)."""
    for line in stdout.splitlines():
        start = line.find("PTPROBE{")
        if start < 0:
            continue
        try:
            data = json.loads(line[start + len("PTPROBE") :].strip())
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


# --- doctor --------------------------------------------------------------------------------------

_FIX_LF = "Convert it to LF (dos2unix {name}) and keep `{attr}` in .gitattributes, then: git add --renormalize ."
_FIX_CRLF = "Convert it to CRLF (unix2dos {name}) and keep `{attr}` in .gitattributes, then: git add --renormalize ."
_FIX_ASCII = "Keep the launchers ASCII-only: cmd, sh and Windows PowerShell 5.1 read them in legacy code pages"


def _git_modes(names: Sequence[str]) -> dict[str, str]:
    """Return the git index mode of each launcher (missing: untracked, or not a git checkout)."""
    if not shutil.which("git"):
        return {}
    r = proc.run(["git", "ls-files", "-s", "--", *names], capture=True, check=False, echo=False)
    modes: dict[str, str] = {}
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            meta, _, path = line.partition("\t")
            if meta and path:
                modes[path] = meta.split()[0]
    return modes


def launcher_problems(name: str, data: bytes, mode: str | None) -> list[tuple[str, str]]:
    """Return (problem, how to fix it) for one launcher's bytes and git index mode."""
    out: list[tuple[str, str]] = []
    if not data.isascii():
        out.append(("non-ASCII bytes" + (" (a UTF-8 BOM)" if data.startswith(b"\xef\xbb\xbf") else ""), _FIX_ASCII))
    if name == "deploy.cmd":
        bare_lf = data.count(b"\n") - data.count(b"\r\n")
        if bare_lf or b"\r" in data.replace(b"\r\n", b""):
            out.append(("not CRLF (labels and goto break with LF)", _FIX_CRLF.format(name=name, attr="*.cmd text eol=crlf")))
        return out
    if b"\r" in data:
        out.append(("CRLF line endings", _FIX_LF.format(name=name, attr=f"{name} text eol=lf")))
    if name == "deploy" and data.split(b"\n", 1)[0].rstrip(b"\r") != b"#!/bin/sh":
        out.append(("the first line is not #!/bin/sh", "Restore the template's first line: #!/bin/sh"))
    if mode is not None and mode != "100755":
        out.append((f"git mode {mode}", f"git update-index --chmod=+x {name}"))
    return out


def _check_launchers(check: Check) -> None:
    launcher = os.environ.get("PYTEMPLATE_LAUNCHER")
    if launcher:
        check(None, f"this run was started by: {launcher}", "")
    else:
        check(
            None,
            "this run was started by: unknown (PYTEMPLATE_LAUNCHER is not set)",
            "An older launcher, a shell-setup function that runs uv directly (xonsh, nu), or uv run by hand",
        )
    labels = {"deploy": "#!/bin/sh, LF, ASCII", "deploy.cmd": "CRLF, ASCII", "deploy.ps1": "LF, ASCII, no BOM"}
    modes = _git_modes(list(labels))
    for name, label in labels.items():
        path = ROOT / name
        if not path.is_file():
            check(False, f"{name} is missing", f"Restore it from the template: git checkout -- {name}")
            continue
        mode = modes.get(name) if name == "deploy" else None
        problems = launcher_problems(name, path.read_bytes(), mode)
        if name == "deploy" and not IS_WINDOWS and not os.access(path, os.X_OK):
            problems.append(("not executable", "chmod +x deploy"))
        if problems:
            check(False, f"{name}: " + "; ".join(p for p, _ in problems), "\n".join(dict.fromkeys(f for _, f in problems)))
        else:
            check(True, f"{name}: {label}" + (f", git mode {mode}" if mode else ""), "")
    # PowerShell runs a .ps1 without the x bit: only `./deploy.ps1` typed in bash or zsh needs it.
    ps1_mode = modes.get("deploy.ps1")
    if ps1_mode and ps1_mode != "100755":
        check(None, f"deploy.ps1: git mode {ps1_mode} (./deploy.ps1 from bash/zsh on Linux and macOS needs 100755)", "git update-index --chmod=+x deploy.ps1")


def _ps_policies() -> list[tuple[str, str]]:
    """Return (PowerShell edition, ExecutionPolicy) for Windows PowerShell 5.1 and PowerShell 7."""
    found = [(label, exe) for label, exe in (("Windows PowerShell 5.1", shutil.which("powershell")), ("PowerShell 7", shutil.which("pwsh"))) if exe]

    def policy(exe: str) -> str:
        try:
            r = subprocess.run(
                [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
                capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout.strip()

    with ThreadPoolExecutor(max_workers=2) as pool:
        policies = list(pool.map(policy, [exe for _, exe in found]))
    return [(label, p) for (label, _), p in zip(found, policies, strict=True) if p]


def doctor(check: Check) -> None:
    """Launcher and shell checks for ./deploy doctor (it prints its own step headers)."""
    ui.step("launchers")
    _check_launchers(check)

    ui.step("shell")
    if IS_WINDOWS:
        bash = shutil.which("bash") or ""
        if "system32" in bash.lower():
            check(
                None,
                f"`bash` points to WSL ({bash})",
                "On Windows use ./deploy from xonsh, pwsh, cmd, Git Bash or MSYS2; in WSL the runner uses separate -wsl environments",
            )
        for label, policy in _ps_policies():
            check(
                policy not in ("Restricted", "AllSigned"),
                f"{label}: ExecutionPolicy = {policy}",
                "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   (or use .\\deploy.cmd)",
            )
    if IS_WSL:
        check(None, "WSL on /mnt: .venv*-wsl environments and .build/wsl kept separate from Windows", "")


# --- shell-setup ---------------------------------------------------------------------------------

SETUP_SHELLS = ("xonsh", "pwsh", "powershell", "bash", "zsh", "niubash", "msys2", "fish", "nu")

POSIX_FUNCTION = r"""
# Walks up from $PWD to the folder that holds .pytemplate/deploy.py and runs its ./deploy
# (POSIX sh: C:/x, /c/x and / roots alike; it stops at the top instead of looping).
unalias deploy 2>/dev/null || true
deploy() {
    _pt_d=${PWD:-$(pwd)}
    while [ ! -f "${_pt_d%/}/.pytemplate/deploy.py" ]; do
        _pt_n=${_pt_d%/*}
        case $_pt_n in
            '') _pt_n=/ ;;
            [A-Za-z]:) _pt_n=$_pt_n/ ;;
        esac
        if [ "$_pt_n" = "$_pt_d" ]; then
            printf '%s\n' "deploy: no .pytemplate/deploy.py in $PWD or any parent folder" >&2
            unset _pt_d _pt_n
            return 2
        fi
        _pt_d=$_pt_n
    done
    set -- "${_pt_d%/}/deploy" "$@"
    unset _pt_d _pt_n
    "$@"
}
"""

PWSH_SNIPPET = r"""
# `deploy` in PowerShell from any folder of a pytemplate project (7.x and Windows 5.1).
# Paste into your profile, the file named by $PROFILE (5.1 and 7 each have their own), then
# open a new shell. Keep it ASCII: Windows PowerShell 5.1 reads BOM-less files as ANSI.
# PowerShell drops a bare -- before any function or script sees it: quote it ('--').
function deploy {
    $dir = (Get-Location).ProviderPath
    while ($dir -and -not [IO.File]::Exists([IO.Path]::Combine($dir, '.pytemplate', 'deploy.py'))) {
        $parent = [IO.Path]::GetDirectoryName($dir)
        if ($parent -eq $dir) { $parent = $null }
        $dir = $parent
    }
    if (-not $dir) {
        [Console]::Error.WriteLine('deploy: no .pytemplate/deploy.py in this folder or any parent folder')
        $global:LASTEXITCODE = 2
        return
    }
    & ([IO.Path]::Combine($dir, 'deploy.ps1')) @args
}
"""

FISH_SNIPPET = r"""
# `deploy` in fish from any folder of a pytemplate project (fish 3.5 or later).
# Save as ~/.config/fish/functions/deploy.fish, then open a new shell.
function deploy --description 'Run ./deploy of the enclosing pytemplate project'
    set -l dir $PWD
    while not test -f "$dir/.pytemplate/deploy.py"
        set -l parent (path dirname -- $dir)
        if test "$parent" = "$dir"
            echo "deploy: no .pytemplate/deploy.py in $PWD or any parent folder" >&2
            return 2
        end
        set dir $parent
    end
    "$dir/deploy" $argv
end
"""

NU_SNIPPET = r"""
# `deploy` in nushell from any folder of a pytemplate project.
# Paste into your config.nu (`$nu.config-path` prints where it is), then open a new shell.
# It runs .pytemplate/deploy.py with uv directly, so uv must be on PATH.
def --wrapped deploy [...rest] {
    mut dir = $env.PWD
    while not ($dir | path join '.pytemplate' 'deploy.py' | path exists) {
        let parent = ($dir | path dirname)
        if $parent == $dir {
            error make {msg: $"deploy: no .pytemplate/deploy.py in ($env.PWD) or any parent folder"}
        }
        $dir = $parent
    }
    let script = ($dir | path join '.pytemplate' 'deploy.py')
    with-env {PYTEMPLATE_CALLER_CWD: $env.PWD, PYTEMPLATE_LAUNCHER: 'nu'} { ^uv run --quiet --script $script ...$rest }
}
"""

XONSH_TEMPLATE = r'''
# `deploy` in xonsh from any folder of a pytemplate project (xonsh 0.14 or later).
# Paste into ~/.xonshrc, then open a new shell. It runs .pytemplate/deploy.py with uv
# directly (falling back to the launcher when uv is not on $PATH): on Windows ./deploy goes
# through deploy.cmd, and cmd.exe cannot pass & | < > ^ % inside arguments.
# The completion words were taken from this project by `./deploy shell-setup xonsh`.
import os as _pt_os
import shutil as _pt_shutil
import subprocess as _pt_subprocess
import sys as _pt_sys
from pathlib import Path as _PtPath

_PT_WORDS = @WORDS@
_PT_CHOICES = @CHOICES@
_PT_FLAGS = @FLAGS@
_PT_MISSING = "deploy: no .pytemplate/deploy.py in this folder or any parent folder"


def _pt_deploy_argv(args):
    here = _PtPath.cwd()
    for d in (here, *here.parents):
        script = d / ".pytemplate" / "deploy.py"
        if script.is_file():
            path = _pt_os.pathsep.join(str(p) for p in ${...}.get("PATH", []))
            uv = _pt_shutil.which("uv", path=path)
            if uv:
                return [uv, "run", "--quiet", "--script", str(script), *args]
            return [str(d / ("deploy.cmd" if _pt_os.name == "nt" else "deploy")), *args]
    return None


if hasattr(aliases, "return_command"):
    # Newer xonsh: the alias becomes a real command (pipes, redirects and $(...) work).
    @aliases.return_command
    def _pt_deploy(args, **_):
        argv = _pt_deploy_argv(args)
        if argv is None:
            return [_pt_sys.executable, "-c", "import sys; print(%r, file=sys.stderr); sys.exit(2)" % _PT_MISSING]
        return argv

else:
    from xonsh.tools import unthreadable as _pt_unthreadable

    @_pt_unthreadable
    def _pt_deploy(args, stdin=None):
        argv = _pt_deploy_argv(args)
        if argv is None:
            print(_PT_MISSING, file=_pt_sys.stderr)
            return 2
        return _pt_subprocess.call(argv)


aliases["deploy"] = _pt_deploy

try:
    from xonsh.completers.completer import add_one_completer as _pt_add_completer
    from xonsh.completers.tools import contextual_command_completer as _pt_command_completer
except ImportError:  # xonsh without contextual completers: no completion
    pass
else:

    @_pt_command_completer
    def _pt_deploy_complete(command):
        """./deploy commands, tasks, backends and options"""
        if not command.args or _PtPath(command.args[0].value).name.lower() not in ("deploy", "deploy.cmd", "deploy.ps1"):
            return None
        if command.arg_index == 1:
            words = _PT_WORDS
        elif command.arg_index >= 2 and len(command.args) > 1:
            name = command.args[1].value
            if command.prefix.startswith("-"):
                words = _PT_FLAGS.get(name, [])
            else:
                words = _PT_CHOICES.get(name, []) if command.arg_index == 2 else []
        else:
            return None
        return {w for w in words if w.startswith(command.prefix)} or None

    _pt_add_completer("deploy", _pt_deploy_complete, "start")
'''


def completion_words(cfg: Config | None) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    """Return (first words, choices of the 2nd word, --options) per command, from cli.COMMANDS."""
    from .cli import COMMANDS

    tasks = sorted(cfg.tasks) if cfg is not None else []
    first = sorted(COMMANDS) + [t for t in tasks if t not in COMMANDS] + ["-v", "-q", "--dry-run", "--no-render"]
    choices: dict[str, list[str]] = {}
    flags: dict[str, list[str]] = {}
    for name, command in COMMANDS.items():
        group = re.match(r"\[([^\]]+)\]", command.usage)
        if group and " " not in group[1]:
            words: list[str] = []
            for word in group[1].split("|"):
                words += list(BACKENDS) if word == "BACKEND" else [word]
            choices[name] = list(dict.fromkeys(w for w in words if re.fullmatch(r"-{0,2}[a-z][a-z0-9-]*", w)))
        options = list(dict.fromkeys(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", command.usage)))
        if options:
            flags[name] = options
    return first, choices, flags


def _py_words(words: Sequence[str], indent: str = "    ", width: int = 88) -> str:
    """Return a Python expression for a word list: '"a b c".split()', wrapped under `width`."""
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(indent) + len(current) + len(word) + 4 > width:
            lines.append(current)
            current = ""
        current = f"{current} {word}" if current else word
    lines.append(current)
    if len(lines) == 1:
        return f'"{lines[0]}".split()'
    body = "\n".join(f'{indent}"{line} "' for line in lines[:-1]) + f'\n{indent}"{lines[-1]}"'
    return f"(\n{body}\n).split()"


def _py_word_map(data: Mapping[str, Sequence[str]]) -> str:
    rows = [f'    "{k}": "{" ".join(v)}".split(),' for k, v in data.items() if v]
    return "{\n" + "\n".join(rows) + "\n}" if rows else "{}"


def xonsh_snippet(cfg: Config | None) -> str:
    first, choices, flags = completion_words(cfg)
    return (
        XONSH_TEMPLATE.replace("@WORDS@", _py_words(first))
        .replace("@CHOICES@", _py_word_map(choices))
        .replace("@FLAGS@", _py_word_map(flags))
    )


def _msys2_bashrc() -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "<you>"
    for root in _msys2_roots(dict(os.environ), standard=True):
        return str(root / "home" / user / ".bashrc")
    return "<MSYS2 root>\\home\\<you>\\.bashrc"


def _posix_header(shell: str) -> str:
    if shell == "bash":
        return "# `deploy` in bash from any folder of a pytemplate project.\n# Paste into ~/.bashrc (Git Bash on Windows: %USERPROFILE%\\.bashrc), then open a new shell."
    if shell == "zsh":
        return "# `deploy` in zsh from any folder of a pytemplate project.\n# Paste into ~/.zshrc, then open a new shell."
    if shell == "niubash":
        niu_env = os.environ.get("NIU_ENV") or "(unset: set NIU_ENV to a file first)"
        return (
            "# `deploy` in niubash from any folder of a pytemplate project.\n"
            "# Paste it into BOTH of these files, then open a new shell:\n"
            "#   ~/.niubashrc  (interactive niubash)\n"
            f"#   {niu_env}  (the file named by $NIU_ENV: `niu -c`, niu scripts and xonsh `!`\n"
            "#   lines read only that one; the xonsh-shell-kit mirrors aliases, not functions)"
        )
    return (
        "# `deploy` in MSYS2 (any MSYSTEM) from any folder of a pytemplate project.\n"
        f"# Paste into ~/.bashrc inside MSYS2 ({_msys2_bashrc()}) ABOVE the line\n"
        '#   [[ "$-" != *i* ]] && return\n'
        "# so non-interactive shells that source it get it too. xonsh `!m` lines run a non-login,\n"
        "# non-interactive bash: add BASH_ENV=<that .bashrc> to the msys2 env of the xonsh-shell-kit."
    )


def snippet(shell: str, cfg: Config | None = None) -> str:
    """Return the shell-setup snippet for `shell` (ASCII, with where to paste it on top)."""
    if shell in ("bash", "zsh", "niubash", "msys2"):
        return _posix_header(shell) + "\n" + POSIX_FUNCTION.strip("\n") + "\n"
    if shell in ("pwsh", "powershell"):
        return PWSH_SNIPPET.strip("\n") + "\n"
    if shell == "fish":
        return FISH_SNIPPET.strip("\n") + "\n"
    if shell == "nu":
        return NU_SNIPPET.strip("\n") + "\n"
    if shell == "xonsh":
        return xonsh_snippet(cfg).strip("\n") + "\n"
    raise DeployError(f"shell-setup: unknown shell '{shell}' ({' | '.join(SETUP_SHELLS)})")


def guess_shell(env: Mapping[str, str]) -> str | None:
    """Guess the calling shell from PYTEMPLATE_LAUNCHER, XONSH_VERSION and SHELL."""
    launcher = env.get("PYTEMPLATE_LAUNCHER", "")
    if launcher.startswith("ps1:"):
        return "pwsh"
    for prefix, shell in (("sh:niubash", "niubash"), ("sh:zsh", "zsh"), ("sh:bash", "bash")):
        if launcher.startswith(prefix):
            return shell
    if env.get("XONSH_VERSION"):
        return "xonsh"
    name = Path(env.get("SHELL", "")).name.lower()
    name = name[:-4] if name.endswith(".exe") else name
    return name if name in ("bash", "zsh", "fish", "nu") else None


def cmd_shell_setup(cfg: Config, args: list[str]) -> int:
    """shell-setup [xonsh|pwsh|powershell|bash|zsh|niubash|msys2|fish|nu]: print a `deploy` alias/function."""
    if len(args) > 1 or (args and args[0].startswith("-")):
        raise DeployError(f"usage: ./deploy shell-setup [{'|'.join(SETUP_SHELLS)}]")
    shell = args[0] if args else guess_shell(os.environ)
    if shell is None:
        raise DeployError(f"shell-setup: which shell? ./deploy shell-setup {'|'.join(SETUP_SHELLS)}")
    # LF even on Windows: the snippet is often appended to an rc file (bash rejects CRLF).
    sys.stdout.flush()
    sys.stdout.buffer.write(snippet(shell, cfg).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


# --- selftest --shells: discovery ------------------------------------------------------------------

TESTS: dict[str, str] = {"T1": "argv", "T2": "exit", "T3": "cwd", "T4": "shx", "T5": "path", "T6": "stdin", "T7": "hints"}
BASE_ARGS = ("plain", "with space", "", "back\\slash", "tail\\", "\u00fcn\u00ef", "--flag=x", "-v")
EXTRA_ARGS = ('q"uote', "*", "$HOME", "a'b", "--")
PS_ARGS = ("~", "~/x", "~\\x")  # PowerShell 7 expands these in unquoted native arguments
MSYSTEMS = ("MSYS", "UCRT64", "MINGW64", "CLANG64", "CLANGARM64")
POSIX_INTERPRETERS: dict[str, tuple[str, ...]] = {
    "bash": ("--norc", "--noprofile"),
    "dash": (),
    "zsh": ("-f",),
    "ksh": (),
    "mksh": (),
    "yash": (),
}


@dataclass(frozen=True)
class Shell:
    """One way of typing ./deploy: a shell executable plus how it receives the command."""

    name: str
    family: str  # posix | cmd | powershell | xonsh | fish | nu | wsl
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    mode: str = "c"  # c: command string | script: a script file (xonsh-shell-kit's `!` route)
    interp: tuple[str, ...] = ()  # posix: `<interp> ./deploy` (the launcher parsed by this shell)
    mixed: bool = False  # a POSIX shell on Windows: absolute paths are spelled C:/x
    bindirs: tuple[str, ...] = ()  # what the shell needs on a minimal PATH
    note: str = ""

    def describe(self) -> str:
        parts = [self.family if self.mode == "c" else f"{self.family}, script mode"]
        if self.interp:
            parts.append("launcher run as `" + Path(self.interp[0]).name + " ./deploy`")
        parts += [f"{k}={v}" for k, v in self.env]
        if self.note:
            parts.append(self.note)
        return "; ".join(parts)


def _is_file(path: Path | str) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return False


def _dedupe(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        try:
            key = os.path.normcase(os.path.realpath(p))
        except OSError:
            key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _mixed(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def _scoop_target(exe: Path) -> Path:
    """A scoop shim (shims/git.exe) names its real program in shims/git.shim."""
    shim = exe.with_suffix(".shim")
    if _is_file(shim):
        for line in shim.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r'\s*path\s*=\s*"?([^"]+?)"?\s*$', line)
            if m:
                return Path(m[1])
    return exe


def _git_roots(env: Mapping[str, str], which: Callable[[str], str | None], standard: bool) -> list[Path]:
    env = {k.upper(): v for k, v in env.items()}
    candidates: list[Path] = []
    for base in (env.get("PROGRAMFILES"), env.get("PROGRAMW6432")):
        if base and standard:
            candidates.append(Path(base) / "Git")
    for scoop in (env.get("SCOOP"), str(Path(env["USERPROFILE"]) / "scoop") if env.get("USERPROFILE") else None):
        if scoop:
            candidates.append(Path(scoop) / "apps" / "git" / "current")
    git = which("git")
    if git:
        # cmd/git.exe, bin/git.exe or mingw64/bin/git.exe (behind a scoop shim, maybe)
        candidates += list(_scoop_target(Path(git)).parents)[:3]
    # cmd/git.exe tells Git for Windows apart from an MSYS2 root with its own git package.
    return _dedupe(c for c in candidates if _is_file(c / "usr" / "bin" / "sh.exe") and _is_file(c / "cmd" / "git.exe"))


def _msys2_roots(env: Mapping[str, str], standard: bool) -> list[Path]:
    env = {k.upper(): v for k, v in env.items()}
    candidates = [Path(env["MSYS2_ROOT"])] if env.get("MSYS2_ROOT") else []
    if standard:
        candidates.append(Path("C:/msys64"))
    for scoop in (env.get("SCOOP"), str(Path(env["USERPROFILE"]) / "scoop") if env.get("USERPROFILE") else None):
        if scoop:
            candidates.append(Path(scoop) / "apps" / "msys2" / "current")
    return _dedupe(c for c in candidates if _is_file(c / "usr" / "bin" / "bash.exe") and _is_file(c / "usr" / "bin" / "msys-2.0.dll"))


def _cygwin_roots(env: Mapping[str, str], standard: bool) -> list[Path]:
    candidates = [Path(env["CYGWIN_ROOT"])] if env.get("CYGWIN_ROOT") else []
    if standard:
        candidates += [Path("C:/cygwin64"), Path("C:/cygwin")]
    return _dedupe(c for c in candidates if _is_file(c / "bin" / "bash.exe"))


def wsl_distros(wsl: str) -> list[str]:
    """Installed WSL distributions (`wsl -l -q` prints UTF-16); none when WSL is not set up."""
    try:
        r = subprocess.run([wsl, "-l", "-q"], capture_output=True, timeout=30, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    raw = r.stdout
    text = raw.decode("utf-16-le", errors="replace") if b"\x00" in raw else raw.decode("utf-8", errors="replace")
    names = [line.strip().strip("\x00").strip() for line in text.splitlines()]
    return [n for n in names if n and not n.lower().startswith("docker-desktop")]


def _suffix(index: int) -> str:
    return "" if index == 0 else str(index + 1)


def _discover_windows(env: Mapping[str, str], which: Callable[[str], str | None], standard: bool, distros: Callable[[str], list[str]]) -> list[Shell]:
    env = {k.upper(): v for k, v in env.items()}
    sysroot = env.get("SYSTEMROOT") or "C:\\Windows"
    system32 = str(Path(sysroot) / "System32")
    out: list[Shell] = []

    comspec = env.get("COMSPEC") or str(Path(system32) / "cmd.exe")
    if _is_file(comspec):
        out.append(Shell("cmd", "cmd", (comspec, "/d", "/s", "/c")))
    ps_flags = ("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass")
    powershell = which("powershell") or str(Path(system32) / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    if _is_file(powershell):
        out.append(Shell("powershell", "powershell", (powershell, *ps_flags), note="Windows PowerShell 5.1"))
    pwsh = which("pwsh") or str(Path(env.get("PROGRAMFILES") or "C:\\Program Files") / "PowerShell" / "7" / "pwsh.exe")
    if which("pwsh") or (standard and _is_file(pwsh)):
        out.append(Shell("pwsh", "powershell", (pwsh, *ps_flags)))
    xonsh = which("xonsh")
    if xonsh:
        out.append(Shell("xonsh", "xonsh", (xonsh, "--no-rc"), note="./deploy resolves to deploy.cmd"))

    niu = which("niu") or (str(Path(env["LOCALAPPDATA"]) / "Programs" / "Niubash" / "niu.exe") if env.get("LOCALAPPDATA") else "")
    if niu and _is_file(niu):
        bindirs = (str(Path(niu).parent),)
        out.append(Shell("niubash", "posix", (niu,), mixed=True, bindirs=bindirs, note="runs ./deploy in-process"))
        out.append(Shell("niubash-shx", "posix", (niu,), mode="script", mixed=True, bindirs=bindirs, note="niu <script>, like xonsh `!` lines"))

    for i, root in enumerate(_git_roots(env, which, standard)):
        usr = root / "usr" / "bin"
        git_bash = str(root / "bin" / "bash.exe") if _is_file(root / "bin" / "bash.exe") else str(usr / "bash.exe")
        tag = _suffix(i)
        bindirs = (str(usr),)
        genv = (("CHERE_INVOKING", "1"), ("MSYSTEM", "MINGW64"))
        out.append(Shell(f"git{tag}-bash", "posix", (git_bash, "--login"), genv, mixed=True, bindirs=bindirs, note="login, like the Git Bash terminal"))
        out.append(Shell(f"git{tag}-sh", "posix", (str(usr / "sh.exe"),), mixed=True, bindirs=bindirs, note="non-login, like CI `shell: bash`"))
        if _is_file(usr / "dash.exe"):
            dash = str(usr / "dash.exe")
            out.append(Shell(f"git{tag}-dash", "posix", (dash,), interp=(_mixed(dash),), mixed=True, bindirs=bindirs))

    arm = "arm" in (env.get("PROCESSOR_ARCHITECTURE", "") + env.get("PROCESSOR_ARCHITEW6432", "")).lower()
    for i, root in enumerate(_msys2_roots(env, standard)):
        usr = root / "usr" / "bin"
        msys_bash = str(usr / "bash.exe")
        tag = _suffix(i)
        bindirs = (str(usr),)
        for msystem in MSYSTEMS:
            if msystem == "CLANGARM64" and not arm:
                continue
            if msystem != "MSYS" and not (root / msystem.lower()).is_dir():
                continue
            menv = (("MSYSTEM", msystem), ("CHERE_INVOKING", "1"), ("MSYS2_PATH_TYPE", "minimal"))
            out.append(Shell(f"msys2{tag}-{msystem.lower()}", "posix", (msys_bash, "--login"), menv, mixed=True, bindirs=bindirs, note="login shell, minimal PATH"))
        shx_env = (("MSYSTEM", "MINGW64"), ("CHERE_INVOKING", "1"))
        out.append(Shell(f"msys2{tag}-shx", "posix", (msys_bash,), shx_env, mode="script", mixed=True, bindirs=bindirs, note="bash <script>, like xonsh `!m` lines"))
        if _is_file(usr / "dash.exe"):
            dash = str(usr / "dash.exe")
            out.append(Shell(f"msys2{tag}-dash", "posix", (dash,), interp=(_mixed(dash),), mixed=True, bindirs=bindirs))

    for i, root in enumerate(_cygwin_roots(env, standard)):
        tag = _suffix(i)
        cenv = (("CHERE_INVOKING", "1"), ("CYGWIN_NOWINPATH", "1"))
        bindirs = (str(root / "bin"),)
        out.append(Shell(f"cygwin{tag}", "posix", (str(root / "bin" / "bash.exe"), "--login"), cenv, mixed=True, bindirs=bindirs, note="login, no Windows PATH"))
        if _is_file(root / "bin" / "dash.exe"):
            dash = str(root / "bin" / "dash.exe")
            out.append(Shell(f"cygwin{tag}-dash", "posix", (dash,), interp=(_mixed(dash),), mixed=True, bindirs=bindirs))

    busybox = which("busybox") or which("busybox64u") or which("busybox64")
    if busybox:
        out.append(Shell("busybox", "posix", (busybox, "sh"), mixed=True, bindirs=(str(Path(busybox).parent),), note="busybox-w32"))
    nu = which("nu")
    if nu:
        out.append(Shell("nu", "nu", (nu, "--no-config-file"), note="./deploy.cmd"))
    fish = which("fish")
    if fish:
        out.append(Shell("fish", "fish", (fish, "--no-config"), mixed=True, bindirs=(str(Path(fish).parent),)))
    wsl = which("wsl")
    if wsl:
        for distro in distros(wsl):
            out.append(Shell(f"wsl-{re.sub(r'[^A-Za-z0-9.-]+', '-', distro).lower()}", "wsl", (wsl, "-d", distro), note=f"WSL {distro}"))
    return out


def _discover_posix(which: Callable[[str], str | None]) -> list[Shell]:
    out: list[Shell] = []
    if _is_file("/bin/sh"):
        out.append(Shell("sh", "posix", ("/bin/sh",), note="./deploy through its #!/bin/sh"))
    for name, flags in POSIX_INTERPRETERS.items():
        exe = which(name)
        if exe:
            out.append(Shell(name, "posix", (exe, *flags), interp=(exe,)))
    busybox = which("busybox")
    if busybox:
        out.append(Shell("busybox", "posix", (busybox, "sh"), interp=(busybox, "sh")))
    fish = which("fish")
    if fish:
        out.append(Shell("fish", "fish", (fish, "--no-config")))
    nu = which("nu")
    if nu:
        out.append(Shell("nu", "nu", (nu, "--no-config-file")))
    pwsh = which("pwsh")
    if pwsh:
        out.append(Shell("pwsh", "powershell", (pwsh, "-NoLogo", "-NoProfile", "-NonInteractive")))
    xonsh = which("xonsh")
    if xonsh:
        out.append(Shell("xonsh", "xonsh", (xonsh, "--no-rc")))
    return out


def discover(
    env: Mapping[str, str] | None = None,
    *,
    windows: bool = IS_WINDOWS,
    which: Callable[[str], str | None] | None = None,
    standard: bool = True,
    distros: Callable[[str], list[str]] = wsl_distros,
) -> list[Shell]:
    """Return the shells installed here (`standard`: also look in fixed folders like C:\\msys64)."""
    env = dict(os.environ if env is None else env)
    path = env.get("PATH") or env.get("Path")

    def default_which(name: str) -> str | None:
        return shutil.which(name, path=path)

    finder = which or default_which
    return _discover_windows(env, finder, standard, distros) if windows else _discover_posix(finder)


def select(shells: Sequence[Shell], names: Sequence[str]) -> list[Shell]:
    """Keep the shells named in `names` (exact, or a family prefix: msys2 -> msys2-*)."""
    if not names:
        return list(shells)
    picked = [s for s in shells if any(s.name == n or s.name.startswith(n + "-") for n in names)]
    unknown = [n for n in names if not any(s.name == n or s.name.startswith(n + "-") for s in shells)]
    if unknown:
        found = ", ".join(s.name for s in shells) or "none"
        raise DeployError(f"selftest --shells: not found here: {', '.join(unknown)}  (found: {found})")
    return picked


# --- selftest --shells: building the command for each shell ------------------------------------------


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


_CMD_UNSAFE = frozenset('%!"^\r\n')


def cmd_quote(s: str) -> str:
    """Quote one argument for a cmd.exe line that reaches a program through %* (no % ! " ^)."""
    if any(c in _CMD_UNSAFE for c in s):
        raise ValueError(f"cmd.exe cannot pass {s!r} through a batch file safely")
    if s and not any(c in s for c in " \t&|<>()"):
        return s
    return '"' + re.sub(r"(\\+)$", r"\1\1", s) + '"'


def ps_quote(s: str) -> str:
    for q in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        s = s.replace(q, q + q)
    return "'" + s + "'"


def fish_quote(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def nu_quote(s: str) -> str:
    hashes = "#"
    while "'" + hashes in s:
        hashes += "#"
    return f"r{hashes}'{s}'{hashes}"


def ps_encoded(code: str) -> str:
    """PowerShell -EncodedCommand payload: base64 of UTF-16LE."""
    return base64.b64encode(code.encode("utf-16-le")).decode("ascii")


def argset(sh: Shell) -> tuple[str, ...]:
    """T1 arguments this shell must pass through unchanged (cmd lines cannot carry `%!"^`)."""
    if sh.family == "cmd" or (sh.family == "nu" and IS_WINDOWS):
        return BASE_ARGS
    if sh.family == "powershell":
        return BASE_ARGS + EXTRA_ARGS + PS_ARGS
    return BASE_ARGS + EXTRA_ARGS


SHX_TRAILER = '__shx_rc=$?\nif [ -n "${SHX_CWD_FILE:-}" ]; then pwd > "$SHX_CWD_FILE" 2>/dev/null; fi\nexit $__shx_rc\n'


@dataclass(frozen=True)
class Invocation:
    argv: list[str] | str  # a str only for cmd.exe, which does not parse its line with the MSVC rules
    env: dict[str, str]
    script: Path | None = None


def command_text(sh: Shell, project: Path, where: str, args: Sequence[str], minimal_path: str = "") -> str:
    """The command line typed into the shell: ./deploy (root), ../deploy (sub) or the absolute launcher."""
    launcher = project / "deploy"
    if sh.family in ("posix", "wsl"):
        if where == "abs":
            word = f'"$(wslpath -u {sh_quote(str(launcher))})"' if sh.family == "wsl" else sh_quote(_mixed(launcher) if sh.mixed else str(launcher))
        else:
            word = "./deploy" if where == "root" else "../deploy"
        prefix = f"PATH={minimal_path}; export PATH; " if minimal_path else ""
        return prefix + " ".join([*map(sh_quote, sh.interp), word, *map(sh_quote, args)])
    if sh.family == "cmd":
        word = {"root": ".\\deploy", "sub": "..\\deploy", "abs": f'"{launcher}"'}[where]
        return " ".join([word, *map(cmd_quote, args)])
    if sh.family == "powershell":
        # On Windows ./deploy and ../deploy resolve to deploy.ps1, but an absolute path without
        # the extension resolves to the sh launcher and opens it through its file association.
        # Elsewhere the extensionless file is the sh launcher itself.
        ext = "" if IS_WINDOWS else ".ps1"
        word = {"root": f"./deploy{ext}", "sub": f"../deploy{ext}", "abs": str(project / "deploy.ps1")}[where]
        call = " ".join(["&", ps_quote(word), *map(ps_quote, args)])
        return f"$ProgressPreference = 'SilentlyContinue'\n{call}\nexit $LASTEXITCODE\n"
    if sh.family == "xonsh":
        word = {"root": "./deploy", "sub": "../deploy", "abs": f"@({ascii(str(launcher))})"}[where]
        return (
            "$XONSH_SUBPROC_CMD_RAISE_ERROR = False\n$XONSH_SUBPROC_RAISE_ERROR = False\n"
            f"_pt_r = ![{word} @({ascii(list(args))})]\nimport sys\nsys.exit(_pt_r.returncode)\n"
        )
    if sh.family == "fish":
        word = {"root": "./deploy", "sub": "../deploy", "abs": fish_quote(_mixed(launcher) if sh.mixed else str(launcher))}[where]
        return " ".join([word, *map(fish_quote, args)])
    if sh.family == "nu":
        name = "deploy.cmd" if IS_WINDOWS else "deploy"
        word = {"root": f"./{name}", "sub": f"../{name}", "abs": str(project / name)}[where]
        return " ".join([f"^'{word}'", *map(nu_quote, args)])  # '...': no escapes in nu
    raise ValueError(f"unknown shell family {sh.family}")


def invocation(sh: Shell, project: Path, where: str, args: Sequence[str], *, cwd: Path, scripts: Path, tag: str, minimal_path: str = "") -> Invocation:
    """Build the process for one probe (writes the script file in script mode)."""
    wsl_path = minimal_path if sh.family == "wsl" else ""
    text = command_text(sh, project, where, args, wsl_path)
    env: dict[str, str] = {}
    if sh.family == "cmd":
        return Invocation(subprocess.list2cmdline(list(sh.argv)) + ' "' + text + '"', env)
    if sh.family == "powershell":
        return Invocation([*sh.argv, "-EncodedCommand", ps_encoded(text)], env)
    if sh.family == "xonsh":
        return Invocation([*sh.argv, "-c", text], env)
    if sh.family == "nu":
        return Invocation([*sh.argv, "-c", text], env)
    if sh.mode == "script":
        script = scripts / f"{tag}.sh"
        script.write_text(text + "\n", encoding="utf-8", newline="\n")
        return Invocation([*sh.argv, str(script)], env, script)
    env["PTCMD"] = text
    if sh.family == "fish":
        return Invocation([*sh.argv, "-c", "eval $PTCMD"], env)
    if sh.family == "wsl":
        env["WSLENV"] = ":".join(x for x in (os.environ.get("WSLENV", ""), "PTCMD/u") if x)
        return Invocation([*sh.argv, "--cd", str(cwd), "-e", "sh", "-c", 'eval "$PTCMD"'], env)
    # POSIX shells never get the command in argv: Cygwin mangles backslashes there.
    return Invocation([*sh.argv, "-c", 'eval "$PTCMD"'], env)


# --- selftest --shells: running ----------------------------------------------------------------------


@dataclass
class ProbeRun:
    rc: int | None
    stdout: str
    stderr: str
    ms: int
    data: dict[str, object] | None
    error: str = ""


@dataclass
class Result:
    shell: str
    test: str
    status: str  # pass | fail | skip | n/a
    ms: int = 0
    detail: str = ""
    launcher: str = ""  # PYTEMPLATE_LAUNCHER as the runner saw it


_DROP_ENV = frozenset(
    {"UV", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "UV_RUN_RECURSION_DEPTH",
     "PYTHONHOME", "PYTHONPATH", "PWD", "OLDPWD", "SHX_CWD_FILE", "PTCMD"}
)


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The user's environment without what `uv run --script` added for this runner.

    UV would hand the launchers uv's path and hide their own discovery; a stale PWD would
    make MSYS2/niubash start in the wrong folder.
    """
    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items() if k.upper() not in _DROP_ENV and not k.upper().startswith("PYTEMPLATE_")}
    if sys.prefix != sys.base_prefix:
        own = os.path.normcase(str(Path(sys.prefix) / ("Scripts" if IS_WINDOWS else "bin")))
        for key in [k for k in env if k.upper() == "PATH"]:
            env[key] = os.pathsep.join(p for p in env[key].split(os.pathsep) if p and os.path.normcase(p) != own)
    return env


def _merge_env(base: Mapping[str, str], *layers: Mapping[str, str]) -> dict[str, str]:
    """base updated with each layer (keys compared without case on Windows)."""
    env = dict(base)
    for layer in layers:
        for key, value in layer.items():
            if IS_WINDOWS:
                for old in [k for k in env if k.upper() == key.upper()]:
                    del env[old]
            env[key] = value
    return env


def _expand_percent(value: str, env: Mapping[str, str]) -> str:
    upper = {k.upper(): v for k, v in env.items()}
    return re.sub(r"%([^%]+)%", lambda m: upper.get(m[1].upper(), m[0]), value)


def registry_path_dirs(env: Mapping[str, str]) -> list[str]:
    """The user and machine PATH from the Windows registry, %VARS% expanded with `env`."""
    dirs: list[str] = []
    # `== "win32"` around the code, not an early return: mypy (warn_unreachable) checks this
    # module for every OS, and the winreg code would be unreachable elsewhere.
    if sys.platform == "win32":
        import winreg

        keys = (
            (winreg.HKEY_CURRENT_USER, "Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        )
        for hive, key in keys:
            try:
                with winreg.OpenKey(hive, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, "Path")
            except OSError:
                continue
            dirs += [_expand_percent(p.strip().strip('"'), env) for p in str(value).split(";") if p.strip()]
    return dirs


def uv_standard_dirs(env: Mapping[str, str], windows: bool = IS_WINDOWS) -> list[Path]:
    """The folders the launchers search for uv besides PATH and the registry (installer order first)."""
    e = {k.upper(): v for k, v in env.items()} if windows else dict(env)
    home = (e.get("USERPROFILE") if windows else None) or e.get("HOME", "")
    pairs: list[tuple[str | None, str]] = [
        (e.get("UV_INSTALL_DIR"), ""), (e.get("UV_INSTALL_DIR"), "bin"), (e.get("XDG_BIN_HOME"), ""),
        (e.get("XDG_DATA_HOME"), "../bin"), (home, ".local/bin"), (e.get("CARGO_HOME"), "bin"), (home, ".cargo/bin"),
    ]
    globs: list[Path] = []
    if windows:
        local = e.get("LOCALAPPDATA") or (str(Path(home) / "AppData" / "Local") if home else "")
        for winget in ([Path(local) / "Microsoft" / "WinGet"] if local else []) + ([Path(e["PROGRAMFILES"]) / "WinGet"] if e.get("PROGRAMFILES") else []):
            pairs.append((str(winget), "Links"))
            globs.append(winget / "Packages")
        pairs += [
            (e.get("SCOOP"), "shims"), (home, "scoop/shims"), (e.get("SCOOP_GLOBAL"), "shims"),
            (e.get("PROGRAMDATA"), "scoop/shims"), (e.get("CHOCOLATEYINSTALL"), "bin"), (e.get("PROGRAMDATA"), "chocolatey/bin"),
        ]
    else:
        pairs += [("/opt/homebrew", "bin"), ("/usr/local", "bin"), ("/home/linuxbrew/.linuxbrew", "bin"), (home, ".nix-profile/bin"), ("/usr", "bin"), ("/", "bin")]
    dirs = [Path(base) / sub if sub else Path(base) for base, sub in pairs if base]
    for packages in globs:
        if packages.is_dir():
            dirs += sorted(packages.glob("astral-sh.uv_*"))
    return dirs


def find_uv_in(dirs: Iterable[Path | str], windows: bool = IS_WINDOWS) -> str:
    exe = "uv.exe" if windows else "uv"
    for d in dirs:
        if d and _is_file(Path(d) / exe):
            return str(d)
    return ""


@dataclass
class Context:
    project: Path
    sub: Path  # a folder inside the project (src/)
    tmp: Path
    away: Path  # a folder outside any project
    timeout: float
    env: dict[str, str]
    uv_standard: str = ""  # a standard folder that holds uv ("" = none: T5 is skipped)
    unhidden_uv: str = ""  # a folder that still holds uv with the T7 environment (registry PATH, /usr/bin...)
    sysroot: str = "C:\\Windows"
    running: set[subprocess.Popen[bytes]] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def minimal_path(self, sh: Shell) -> str:
        if sh.family == "wsl" or not IS_WINDOWS:
            return "/usr/bin:/bin"
        system32 = str(Path(self.sysroot) / "System32")
        return os.pathsep.join([*sh.bindirs, system32, self.sysroot, str(Path(system32) / "WindowsPowerShell" / "v1.0")])

    def hidden_env(self) -> dict[str, str]:
        """T7: every place the launchers look for uv points to an empty folder."""
        nouv = str(self.tmp / "nouv")
        keys = ["HOME", "UV_INSTALL_DIR", "XDG_BIN_HOME", "XDG_DATA_HOME", "CARGO_HOME"]
        if IS_WINDOWS:
            keys += ["USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMDATA", "SCOOP", "SCOOP_GLOBAL", "CHOCOLATEYINSTALL"]
        env = dict.fromkeys(keys, nouv)
        # No System32 either: that hides reg.exe, the launchers' registry fallback.
        env["PATH"] = nouv if IS_WINDOWS else "/usr/bin:/bin"
        return env


def _kill(p: subprocess.Popen[bytes]) -> None:
    """Kill a probe and everything it started (uv, python)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def spawn(ctx: Context, argv: list[str] | str, *, cwd: Path, env: Mapping[str, str], tag: str, stdin: bytes | None = None) -> ProbeRun:
    """Run one probe process with a timeout (output to files: a pipe held open by a leftover
    grandchild would block forever) and parse its PTPROBE line."""
    out_file = ctx.tmp / "out" / f"{tag}.out"
    err_file = ctx.tmp / "out" / f"{tag}.err"
    in_file = ctx.tmp / "out" / f"{tag}.in"
    in_file.write_bytes(stdin or b"")
    start = time.perf_counter()
    rc: int | None = None
    error = ""
    with out_file.open("wb") as fo, err_file.open("wb") as fe, in_file.open("rb") as fi:
        try:
            p = subprocess.Popen(
                argv, cwd=cwd, env=dict(env), stdin=fi if stdin is not None else subprocess.DEVNULL,
                stdout=fo, stderr=fe, start_new_session=not IS_WINDOWS,
            )
        except OSError as e:
            error = f"cannot start the shell: {e}"
        else:
            with ctx.lock:
                ctx.running.add(p)
            try:
                rc = p.wait(timeout=ctx.timeout)
            except subprocess.TimeoutExpired:
                _kill(p)
                error = f"timed out after {ctx.timeout:g} s"
            finally:
                with ctx.lock:
                    ctx.running.discard(p)
    ms = int((time.perf_counter() - start) * 1000)
    stdout = out_file.read_text(encoding="utf-8", errors="replace")
    stderr = err_file.read_text(encoding="utf-8", errors="replace")
    return ProbeRun(rc, stdout, stderr, ms, parse_probe(stdout), error)


def run_probe(
    ctx: Context, sh: Shell, where: str, probe_args: Sequence[str], *, cwd: Path, tag: str,
    env: Mapping[str, str] | None = None, stdin: bytes | None = None, minimal_path: bool = False,
) -> ProbeRun:
    """Run `<launcher> __probe ARGS...` through `sh` from `cwd`."""
    path = ctx.minimal_path(sh) if minimal_path else ""
    inv = invocation(sh, ctx.project, where, ["__probe", *probe_args], cwd=cwd, scripts=ctx.tmp / "scripts", tag=tag, minimal_path=path)
    layers: list[Mapping[str, str]] = [dict(sh.env)]
    if path and sh.family != "wsl":
        layers.append({"PATH": path})
    layers += [env or {}, inv.env]
    return spawn(ctx, inv.argv, cwd=cwd, env=_merge_env(ctx.env, *layers), tag=tag, stdin=stdin)


def _tail(run: ProbeRun) -> str:
    lines = [ln.strip() for ln in (run.stderr or run.stdout).splitlines() if ln.strip() and not ln.startswith("PTPROBE")]
    text = " | ".join(lines[-3:])
    return f": {text[:300]}" if text else ""


def host_path(raw: str) -> str:
    """A path the runner printed (maybe from WSL: /mnt/c/x) as a path of this host."""
    m = re.fullmatch(r"/mnt/([A-Za-z])(/.*)?", raw)
    if IS_WINDOWS and m:
        return m[1].upper() + ":" + (m[2] or "/").replace("/", "\\")
    return native_path(raw)


def same_path(raw: object, expected: Path) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        return os.path.samefile(host_path(raw), expected)
    except OSError:
        return False


def check_run(run: ProbeRun, code: int, project: Path) -> str:
    """'' when the probe printed its line, saw the project root and exited with `code`; else why not."""
    if run.error:
        return run.error
    if run.data is None:
        return f"no PTPROBE line (exit {run.rc})" + _tail(run)
    if run.rc != code:
        return f"exit {run.rc} (expected {code})" + _tail(run)
    if not same_path(run.data.get("root"), project):
        return f"the runner's root is {run.data.get('root')!r}, not the project"
    return ""


def check_cwd(run: ProbeRun, expected: Path) -> str:
    """'' when the launcher exported the caller's folder and the runner resolved it."""
    data = run.data or {}
    raw = data.get("caller_cwd_raw")
    if not same_path(raw, expected):
        return f"PYTEMPLATE_CALLER_CWD is {raw!r}, not {expected}"
    if not same_path(data.get("caller_cwd"), expected):
        return f"caller_cwd() is {data.get('caller_cwd')!r}, not {expected}"
    return ""


def shx_cwd(text: str) -> str:
    """xonsh-shell-kit's to_windows_path: C:/x, /c/x and /cygdrive/c/x -> C:\\x."""
    t = text.strip().strip('"')
    m = re.fullmatch(r"/cygdrive/([A-Za-z])(/.*)?", t) or re.fullmatch(r"/([A-Za-z])(/.*)?", t)
    if m:
        t = m[1].upper() + ":" + (m[2] or "/")
    return os.path.normpath(t.replace("/", "\\")) if IS_WINDOWS else t


def _shx_route(ctx: Context, sh: Shell, tag: str) -> tuple[ProbeRun, str]:
    """T4: xonsh-shell-kit writes the `!` line plus a trailer to %TEMP%/xonsh-shell-kit/bang-*.sh
    and runs `niu <script>` (or MSYS2's bash) from xonsh's folder; the trailer reports the
    shell's final folder, which xonsh then cd's to."""
    kit = ctx.tmp / "xonsh-shell-kit"
    script = kit / f"bang-{os.getpid()}-{sh.name}.sh"
    cwd_file = kit / f"bang-{os.getpid()}-{sh.name}.cwd"
    payload = command_text(sh, ctx.project, "sub", ["__probe", "0", "0", "shx"])
    script.write_text(payload + "\n\n" + SHX_TRAILER, encoding="utf-8", newline="\n")
    env = _merge_env(ctx.env, dict(sh.env), {"SHX_CWD_FILE": str(cwd_file)})
    run = spawn(ctx, [*sh.argv, str(script)], cwd=ctx.sub, env=env, tag=tag)
    why = check_run(run, 0, ctx.project) or check_cwd(run, ctx.sub)
    if not why:
        left = cwd_file.read_text(encoding="utf-8", errors="replace").strip() if cwd_file.is_file() else ""
        try:
            same = bool(left) and os.path.samefile(shx_cwd(left), ctx.sub)
        except OSError:
            same = False
        if not same:
            why = f"the shell ended in {left!r}, not {ctx.sub} (the launcher must not cd)"
    return run, why


def run_test(ctx: Context, sh: Shell, test: str) -> Result:
    """Run one T1..T7 test of one shell."""
    tag = f"{sh.name}-{test}"
    root = ctx.project
    run: ProbeRun | None = None
    why = ""
    ms = 0
    if test == "T1":
        args = argset(sh)
        run = run_probe(ctx, sh, "root", ["0", "0", *args], cwd=root, tag=tag)
        why = check_run(run, 0, root)
        if not why and run.data is not None and run.data.get("argv") != list(args):
            why = f"argv {run.data.get('argv')!r} != {list(args)!r}"
    elif test == "T2":
        run = run_probe(ctx, sh, "root", ["37", "0", "exit"], cwd=root, tag=tag)
        why = check_run(run, 37, root)
    elif test == "T3":
        for where, cwd, label in (("sub", ctx.sub, f"from {ctx.sub.name}/ with ../deploy"), ("abs", ctx.away, "from outside with the absolute launcher")):
            run = run_probe(ctx, sh, where, ["0", "0", "cwd"], cwd=cwd, tag=f"{tag}-{where}")
            ms += run.ms
            why = check_run(run, 0, root) or check_cwd(run, cwd)
            if why:
                why = f"{label}: {why}"
                break
    elif test == "T4":
        if sh.mode != "script":
            return Result(sh.name, test, "n/a")
        run, why = _shx_route(ctx, sh, tag)
    elif test == "T5":
        if not ctx.uv_standard:
            return Result(sh.name, test, "skip", 0, "uv is in no standard folder (only on PATH): nothing to find with a minimal PATH")
        run = run_probe(ctx, sh, "root", ["0", "0", "path"], cwd=root, tag=tag, minimal_path=True)
        why = check_run(run, 0, root)
    elif test == "T6":
        run = run_probe(ctx, sh, "root", ["0", "1", "stdin"], cwd=root, tag=tag, stdin=b"ping\n")
        why = check_run(run, 0, root)
        if not why and run.data is not None and run.data.get("stdin") != "ping":
            why = f"stdin {run.data.get('stdin')!r} != 'ping'"
    elif test == "T7":
        if sh.family == "wsl":
            return Result(sh.name, test, "skip", 0, "uv cannot be hidden inside WSL from here")
        run = run_probe(ctx, sh, "root", ["0", "0", "hints"], cwd=root, tag=tag, env=ctx.hidden_env())
        text = (run.stdout + "\n" + run.stderr).lower()
        if run.rc == 0 and ctx.unhidden_uv and not run.error:
            return Result(sh.name, test, "skip", run.ms, f"uv cannot be hidden: {ctx.unhidden_uv} (registry PATH or a system folder)")
        if run.error:
            why = run.error
        elif run.rc != 127:
            why = f"exit {run.rc} (expected 127 with uv hidden)" + _tail(run)
        elif IS_WINDOWS and ("winget" not in text or "curl" in text):
            why = "the hints must mention winget and never curl on Windows" + _tail(run)
        elif "uv" not in text:
            why = "no install hint" + _tail(run)
    else:
        raise ValueError(f"unknown test {test}")
    assert run is not None
    launcher = run.data.get("launcher") if run.data else None
    return Result(sh.name, test, "fail" if why else "pass", ms or run.ms, why, launcher if isinstance(launcher, str) else "")


def run_shell(ctx: Context, sh: Shell, tests: Sequence[str]) -> list[Result]:
    out: list[Result] = []
    for test in tests:
        try:
            out.append(run_test(ctx, sh, test))
        except (OSError, ValueError) as e:
            out.append(Result(sh.name, test, "fail", 0, f"{type(e).__name__}: {e}"))
    return out


# --- selftest --shells: report and command line ------------------------------------------------------


def _cell(r: Result | None) -> str:
    if r is None or r.status == "n/a":
        return "-"
    if r.status == "skip":
        return "skip"
    return f"{'ok' if r.status == 'pass' else 'FAIL'} {r.ms}"


def table(shells: Sequence[Shell], results: Sequence[Result], tests: Sequence[str]) -> list[str]:
    """The PASS/FAIL/SKIP grid: one row per shell, one column per test (ms), plus the launcher seen."""
    by_key = {(r.shell, r.test): r for r in results}
    width = max([len(s.name) for s in shells] + [5]) + 2
    lines = ["shell".ljust(width) + "".join(f"{t} {TESTS[t]}".ljust(10) for t in tests) + "launcher"]
    for sh in shells:
        launcher = next((r.launcher for r in results if r.shell == sh.name and r.launcher), "?")
        lines.append(sh.name.ljust(width) + "".join(_cell(by_key.get((sh.name, t))).ljust(10) for t in tests) + launcher)
    return lines


def report_json(project: Path, shells: Sequence[Shell], results: Sequence[Result], seconds: float) -> dict[str, object]:
    return {
        "version": 1,
        "project": str(project),
        "host": sys.platform,
        "seconds": round(seconds, 1),
        "tests": TESTS,
        "shells": [
            {
                "name": s.name, "family": s.family, "mode": s.mode, "exe": s.argv[0], "about": s.describe(),
                "launcher": next((r.launcher for r in results if r.shell == s.name and r.launcher), None),
            }
            for s in shells
        ],
        "results": [
            {"shell": r.shell, "test": r.test, "name": TESTS[r.test], "status": r.status, "ms": r.ms, "detail": r.detail}
            for r in results
        ],
        "summary": {k: sum(1 for r in results if r.status == k) for k in ("pass", "fail", "skip")},
    }


SELFTEST_USAGE = "./deploy selftest --shells [NAME,...] [--list] [--json] [--keep] [--project DIR] [--tests T1,...] [--jobs N] [--timeout S]"


@dataclass
class Options:
    names: list[str] = field(default_factory=list)
    list_only: bool = False
    as_json: bool = False
    keep: bool = False
    project: str = ""
    tests: list[str] = field(default_factory=lambda: list(TESTS))
    jobs: int = 8
    timeout: float = 60.0


def parse_options(args: Sequence[str]) -> Options:
    opts = Options(jobs=min(8, os.cpu_count() or 2))
    it = iter(args)
    for arg in it:
        key, eq, inline = arg.partition("=") if arg.startswith("--") else (arg, "", "")

        def value() -> str:
            v = inline if eq else next(it, "")
            if not v:
                raise DeployError(f"{key} needs a value  (usage: {SELFTEST_USAGE})")
            return v

        if key == "--list":
            opts.list_only = True
        elif key == "--json":
            opts.as_json = True
        elif key == "--keep":
            opts.keep = True
        elif key == "--project":
            opts.project = value()
        elif key == "--tests":
            tests = [t.strip().upper() for t in value().split(",") if t.strip()]
            if bad := [t for t in tests if t not in TESTS]:
                raise DeployError(f"unknown test(s): {', '.join(bad)}  (T1..T7)")
            opts.tests = [t for t in TESTS if t in tests]
        elif key in ("--jobs", "-j", "--timeout"):
            raw = value()
            try:
                number = float(raw)
            except ValueError:
                raise DeployError(f"{key}: not a number: {raw}") from None
            if number <= 0:
                raise DeployError(f"{key} must be greater than 0")
            if key == "--timeout":
                opts.timeout = number
            else:
                opts.jobs = int(number)
        elif arg.startswith("-"):
            raise DeployError(f"unknown option {arg}  (usage: {SELFTEST_USAGE})")
        else:
            opts.names += [n.strip() for n in arg.split(",") if n.strip()]
    return opts


def _list_shells(shells: Sequence[Shell], as_json: bool) -> None:
    if as_json:
        data = [{"name": s.name, "family": s.family, "mode": s.mode, "argv": list(s.argv), "about": s.describe()} for s in shells]
        print(json.dumps(data, indent=2))
        return
    ui.step(f"{len(shells)} shells found")
    width = max([len(s.name) for s in shells] + [5]) + 2
    for s in shells:
        ui.info(f"  {s.name.ljust(width)}{s.argv[0]}")
        ui.info(f"  {''.ljust(width)}{s.describe()}")


def _run_all(ctx: Context, shells: Sequence[Shell], tests: Sequence[str], jobs: int) -> list[Result]:
    results: list[Result] = []
    pool = ThreadPoolExecutor(max_workers=max(1, jobs))
    try:
        futures = {pool.submit(run_shell, ctx, sh, tests): sh for sh in shells}
        for future in as_completed(futures):
            got = future.result()
            results += got
            failed = [r.test for r in got if r.status == "fail"]
            ui.info(f"  {futures[future].name}: " + (f"FAIL {' '.join(failed)}" if failed else "ok"))
    except KeyboardInterrupt:
        with ctx.lock:
            for p in list(ctx.running):
                _kill(p)
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    order = {s.name: i for i, s in enumerate(shells)}
    return sorted(results, key=lambda r: (order[r.shell], r.test))


def selftest(cfg: Config, args: list[str]) -> int:
    """selftest --shells [NAME,...] [--list] [--json] [--keep] [--project DIR]: ./deploy __probe through every shell."""
    opts = parse_options(args)
    shells = select(discover(), opts.names)
    if opts.list_only:
        _list_shells(shells, opts.as_json)
        return 0
    if not shells:
        raise DeployError("selftest --shells: no shell found")
    project = user_path(opts.project).resolve() if opts.project else ROOT
    if not (project / ".pytemplate" / "deploy.py").is_file():
        raise DeployError(f"--project {project}: no .pytemplate/deploy.py there")

    tmp = Path(tempfile.mkdtemp(prefix="pts-"))
    for d in ("out", "scripts", "xonsh-shell-kit", "nouv", "away"):
        (tmp / d).mkdir()
    env = child_env()
    ctx = Context(
        project=project,
        sub=project / "src" if (project / "src").is_dir() else project / ".pytemplate",
        tmp=tmp,
        away=tmp / "away",
        timeout=opts.timeout,
        env=env,
        sysroot=os.environ.get("SYSTEMROOT", "C:\\Windows"),
    )
    ctx.uv_standard = find_uv_in([*uv_standard_dirs(env), *registry_path_dirs(env)])
    hidden = _merge_env(env, ctx.hidden_env())
    ctx.unhidden_uv = find_uv_in([*uv_standard_dirs(hidden), *registry_path_dirs(hidden)])

    ui.step(f"selftest --shells: {len(shells)} shells x {len(opts.tests)} tests, project {project}")
    started = time.perf_counter()
    try:
        results = _run_all(ctx, shells, opts.tests, opts.jobs)
    finally:
        if opts.keep:
            ui.info(f"scratch files kept in {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    seconds = time.perf_counter() - started

    ui.info("")
    for line in table(shells, results, opts.tests):
        ui.info(line)
    fails = [r for r in results if r.status == "fail"]
    skips = [r for r in results if r.status == "skip"]
    for title, group in (("failures", fails), ("skipped", skips)):
        if group:
            ui.info(f"\n{title}:")
            for r in group:
                ui.info(f"  {r.shell} {r.test} {TESTS[r.test]}: {r.detail}")
    if opts.as_json:
        print(json.dumps(report_json(project, shells, results, seconds), indent=2))
    passed = sum(1 for r in results if r.status == "pass")
    summary = f"{passed} passed, {len(fails)} failed, {len(skips)} skipped in {seconds:.1f} s"
    ui.info("")
    if fails:
        ui.error(f"selftest --shells: {summary}")
        return 1
    ui.ok(f"selftest --shells: {summary}")
    return 0
