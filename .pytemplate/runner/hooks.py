"""hooks [install [--force]|uninstall|run|status]: the git pre-commit hook of the project.

A native git hook (no pre-commit framework: only uv is needed). `install` writes a small sh
script, `pre-commit`, into the directory git runs hooks from (`git rev-parse --git-path
hooks`, which also handles linked worktrees). The script runs `sh <launcher> hooks run`, with
the POSIX launcher `deploy` given relative to the top of the work tree: git runs pre-commit
hooks from there, whatever client makes the commit (Git Bash, cmd, PowerShell, xonsh, VS Code,
lazygit...), and on Windows it runs them with Git's own sh.exe. `sh` is explicit so the exec
bit of `deploy` does not matter. `./deploy setup` installs the hook when `hooks.pre_commit` is
true in pytemplate.toml (the default).

- A pre-commit hook that is not pytemplate's (no MARKER) is never overwritten: `install`
  fails, `install --force` keeps it as `pre-commit.local` and the new hook runs it first;
  `uninstall` removes only pytemplate's hook and puts the old one back.
- With `core.hooksPath` set (husky, a shared hooks folder...) git ignores `.git/hooks`: the
  hook is not installed there; `install` and `status` print the line to add to that setup.
- Linked worktrees share one hooks directory: the hook calls the launcher at the same
  relative path in every worktree, and a checkout without it skips the checks. A checkout
  whose runner predates `hooks run` (an old branch) fails the hook: commit there with
  `git commit --no-verify`.

`hooks run` checks the STAGED files (`git diff --cached`), fast, so no mypy (that stays in
`./deploy check`, the editors and CI):
  1. ruff check (the active backend's typing profile, like `check`) and ruff format --check
     on the staged .py/.pyi files under src/ and tests/;
  2. the generated files are up to date (`render --check`) and none has unstaged changes;
  3. pyproject.toml matches pytemplate.toml and uv.lock is up to date (`uv lock --check`);
     a staged pyproject.toml needs uv.lock staged too;
  4. the mypyc rules (lintc) on staged compiled modules: errors only under the mypyc profile;
  5. the static launcher checks of `doctor` on staged launchers;
  6. [template repo] the language guard (test_no_spanish.offending_lines) on staged files.

Limits: the checks read the WORKING-TREE version of the staged files, not the staged
content, so a partially staged file is checked with its unstaged changes too (commit it
whole, or `git stash push --keep-index` first). `git commit --no-verify` skips the hook.

Git runs hooks from the top of the work tree and may export repository variables relative to
it (GIT_INDEX_FILE=.git/index) or GIT_DIR without GIT_WORK_TREE (linked worktrees: "the cwd is
the top"). The project may live in a subfolder, so `git_env` pins them as absolute paths for
the git calls made here, and `run` removes them from the environment of every other tool
(uv may run git for git dependencies: it must not see the hook's repository).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
import types
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import envs, lintc, mypyc, proc, render, ui
from .cmd_dev import _profile_file, only_flags
from .config import Config
from .project import IS_WINDOWS, ROOT, STATE_FILE, TEMPLATE, native_path
from .ui import DeployError

Check = Callable[[bool | None, str, str], None]

HOOK = "pre-commit"
LOCAL = "pre-commit.local"  # a foreign hook moved aside by `install --force`; ours runs it first
MARKER = "pytemplate pre-commit hook"
LAUNCHERS = ("deploy", "deploy.cmd", "deploy.ps1")
PY_SUFFIXES = (".py", ".pyi")
USAGE = "install [--force] | uninstall | run | status"
# Repository variables git exports to hooks, possibly relative to the top of the work tree
GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")
# ...and the ones removed from the environment of the tools `run` starts
GIT_REPO_VARS = (*GIT_LOCATION_VARS, "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_NAMESPACE")
ARG_LIMIT = 8000  # characters of file arguments per tool call (Windows command lines: 32767)


# --- the repository ------------------------------------------------------------------------------


def git_env(environ: Mapping[str, str], cwd: Path) -> dict[str, str]:
    """Return git's repository variables made absolute (relative ones are relative to `cwd`).

    GIT_DIR without GIT_WORK_TREE means "cwd is the top of the work tree", so GIT_WORK_TREE is
    pinned to `cwd` then: git calls from another folder (the project root) stay correct.
    """
    out: dict[str, str] = {}
    for key in GIT_LOCATION_VARS:
        value = native_path(environ.get(key, ""))
        if value:
            out[key] = value if os.path.isabs(value) else os.path.normpath(os.path.join(cwd, value))
    if "GIT_DIR" in out and "GIT_WORK_TREE" not in out:
        out["GIT_WORK_TREE"] = str(cwd)
    return out


def _git(args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """Run git quietly; `env` (git_env) is the only source of the repository variables."""
    base = {k: v for k, v in proc.base_env().items() if k not in GIT_REPO_VARS}
    return proc.run(["git", "--literal-pathspecs", *args], cwd=cwd, env={**base, **env}, capture=True, check=False, echo=False)


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _within(path: Path, parent: Path) -> str | None:
    """Return `path` relative to `parent` as a POSIX string ("" for the same folder), or None."""
    rel = os.path.relpath(os.path.normpath(path), os.path.normpath(parent)) if _drive(path) == _drive(parent) else ".."
    if rel == os.curdir:
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


def _drive(path: Path) -> str:
    return os.path.splitdrive(os.path.normcase(str(path)))[0]


@dataclass(frozen=True)
class Repo:
    project: Path  # the project root (where ./deploy is)
    top: Path  # top of the git work tree
    hooks_dir: Path  # where git runs hooks from (core.hooksPath, or <common dir>/hooks)
    default_dir: Path  # <common dir>/hooks: where `install` writes
    prefix: str  # the project relative to `top`, POSIX ("" = the top itself)
    env: dict[str, str] = field(default_factory=dict)  # git_env() for every git call

    @property
    def custom_hooks_path(self) -> bool:
        """Whether core.hooksPath sends git elsewhere (git then ignores the default dir)."""
        return not _same(self.hooks_dir, self.default_dir)

    @property
    def launcher(self) -> str:
        """The POSIX launcher relative to the top of the work tree, as the hook calls it."""
        return f"./{self.prefix}/deploy" if self.prefix else "./deploy"

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _git(args, self.project, self.env)

    def hooks_path_value(self) -> str:
        return self.git("config", "--get", "core.hooksPath").stdout.strip()


def find_repo(project: Path = ROOT, environ: Mapping[str, str] | None = None, cwd: Path | None = None) -> Repo:
    """Return the git repository of `project`; DeployError when git is missing (3) or it is
    not inside a git work tree (2)."""
    if shutil.which("git") is None:
        raise DeployError("git not found in PATH", 3)
    env = git_env(os.environ if environ is None else environ, cwd or Path(os.getcwd()))
    r = _git(["rev-parse", "--show-toplevel", "--git-common-dir", "--git-path", "hooks"], project, env)
    lines = [native_path(ln) for ln in r.stdout.splitlines()]  # MSYS2's own git prints /c/...
    if r.returncode != 0 or len(lines) != 3:
        raise DeployError(f"{project} is not inside a git work tree (git init first)", 2)
    top = Path(lines[0])
    prefix = _within(project, top)
    if prefix is None:
        raise DeployError(f"the project {project} is not inside its git work tree {top}", 2)
    return Repo(
        project=project,
        top=top,
        hooks_dir=Path(os.path.normpath(project / lines[2])),  # relative ones are relative to the cwd
        default_dir=Path(os.path.normpath(project / lines[1] / "hooks")),
        prefix=prefix,
        env=env,
    )


# --- the hook script -----------------------------------------------------------------------------


def sh_literal(text: str) -> str:
    """Quote `text` for sh with ASCII only: '...' or, for non-ASCII, "$(printf '\\ooo...')"."""
    if text.isascii():
        return "'" + text.replace("'", "'\\''") + "'"
    fmt = "".join(chr(b) if 32 <= b < 127 and chr(b) not in "%\\'" else f"\\{b:03o}" for b in text.encode("utf-8"))
    return f"\"$(printf '{fmt}')\""


def hook_script(launcher: str) -> str:
    """Return the pre-commit hook: pure ASCII, LF, runs `sh <launcher> hooks run` from the top."""
    lines = [
        "#!/bin/sh",
        f"# {MARKER}: written by ./deploy hooks install (it rewrites this file: do not edit)",
        "# Runs `./deploy hooks run`: fast checks of the staged files (ruff, ruff format,",
        "# generated files, uv.lock, mypyc rules, launchers). mypy runs in ./deploy check.",
        "#   remove it:    ./deploy hooks uninstall",
        "#   skip it once: git commit --no-verify",
        f"# A hook that was here before is kept as {LOCAL} and runs first.",
        "case $0 in */*) _pt_dir=${0%/*} ;; *) _pt_dir=. ;; esac",
        f'if [ -x "$_pt_dir/{LOCAL}" ]; then',
        f'    "$_pt_dir/{LOCAL}" "$@" || exit $?',
        "fi",
        f"_pt_launcher={sh_literal(launcher)}",
        'if [ ! -f "$_pt_launcher" ]; then',
        "    printf '%s\\n' \"pytemplate pre-commit: $_pt_launcher not found in this checkout: checks skipped\" >&2",
        "    exit 0",
        "fi",
        'exec sh "$_pt_launcher" hooks run',
    ]
    return "\n".join(lines) + "\n"


def run_line(repo: Repo) -> str:
    """The line to add to a hook that pytemplate does not manage (core.hooksPath)."""
    word = repo.launcher if re.fullmatch(r"[A-Za-z0-9_./-]+", repo.launcher) else sh_literal(repo.launcher)
    return f"sh {word} hooks run || exit $?"


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return ""


def classify(path: Path, repo: Repo) -> str:
    """missing | installed | outdated (ours, different content) | calls (another hook that
    runs `hooks run`) | foreign."""
    if not path.is_file():
        return "missing"
    text = _read(path)
    if MARKER in text:
        return "installed" if text == hook_script(repo.launcher) else "outdated"
    if "deploy" in text and "hooks run" in text:
        return "calls"
    return "foreign"


def _show(path: Path, repo: Repo) -> str:
    rel = _within(path, repo.project)
    return rel if rel else path.as_posix()


def _hooks_path_hint(repo: Repo) -> str:
    return (
        f"Add this line to {_show(repo.hooks_dir / HOOK, repo)} (a sh script; git runs it from the top of the work tree),\n"
        f"or run it from the tool that manages that folder:\n    {run_line(repo)}"
    )


def _write_hook(path: Path, text: str) -> None:
    path.write_bytes(text.encode("ascii"))  # bytes: LF on every OS
    if not IS_WINDOWS:
        path.chmod(0o755)


def install(repo: Repo, *, force: bool = False) -> str:
    """Install or update the hook; return the message to print (DeployError if it cannot)."""
    if repo.custom_hooks_path:
        state = classify(repo.hooks_dir / HOOK, repo)
        if state in ("calls", "installed"):
            return f"{_show(repo.hooks_dir / HOOK, repo)} already runs ./deploy hooks run (core.hooksPath)"
        raise DeployError(
            f"core.hooksPath = {repo.hooks_path_value()!r}: git runs the hooks in {_show(repo.hooks_dir, repo)}, "
            "not in the default folder, so pytemplate does not install its hook there.\n"
            + "\n".join(f"  {line}" for line in _hooks_path_hint(repo).splitlines())
        )
    target = repo.default_dir / HOOK
    local = repo.default_dir / LOCAL
    script = hook_script(repo.launcher)
    state = classify(target, repo)
    moved = False
    if state in ("foreign", "calls"):
        if state == "calls" and not force:
            return f"{_show(target, repo)} already runs ./deploy hooks run (not pytemplate's file: left alone)"
        if not force:
            raise DeployError(
                f"{_show(target, repo)} already exists and is not pytemplate's hook: left alone.\n"
                f"  ./deploy hooks install --force keeps it as {LOCAL} and runs it before the checks"
            )
        if local.exists():
            raise DeployError(f"both {_show(target, repo)} and {_show(local, repo)} exist: merge them by hand, then ./deploy hooks install")
        moved = True
    elif state == "installed":
        return f"pre-commit hook already installed: {_show(target, repo)}"
    dry = proc.DRY_RUN
    if not dry:
        repo.default_dir.mkdir(parents=True, exist_ok=True)
        if moved:
            os.replace(target, local)
        _write_hook(target, script)
    verb = {"missing": "installed", "outdated": "updated"}.get(state, "installed")
    msg = f"pre-commit hook {'would be ' + verb if dry else verb}: {_show(target, repo)} -> sh {repo.launcher} hooks run"
    if moved:
        msg += f"\n  the previous hook {'would be' if dry else 'was'} kept as {_show(local, repo)} and runs first"
    elif local.is_file():
        msg += f"\n  it runs {_show(local, repo)} first"
    return msg


def uninstall(repo: Repo) -> str:
    """Remove pytemplate's hook (never another one) and restore the hook it had moved aside."""
    target = repo.default_dir / HOOK
    local = repo.default_dir / LOCAL
    state = classify(target, repo)
    dry = proc.DRY_RUN
    if state in ("foreign", "calls"):
        return f"{_show(target, repo)} is not pytemplate's hook: left alone"
    parts: list[str] = []
    if state in ("installed", "outdated"):
        if not dry:
            target.unlink()
        parts.append(f"{'would remove' if dry else 'removed'} {_show(target, repo)}")
    if local.is_file():
        if not dry:
            os.replace(local, target)
        parts.append(f"{'would restore' if dry else 'restored'} the previous hook ({_show(local, repo)} -> {HOOK})")
    return "; ".join(parts) if parts else "no pytemplate pre-commit hook installed"


# --- status, setup, doctor -------------------------------------------------------------------------


def _status_line(cfg: Config, repo: Repo) -> tuple[bool | None, str, str]:
    """Return (passed, label, hint) describing the hook, for `status` and `doctor`."""
    if repo.custom_hooks_path:
        state = classify(repo.hooks_dir / HOOK, repo)
        where = _show(repo.hooks_dir / HOOK, repo)
        if state in ("installed", "calls"):
            return True, f"git pre-commit hook: {where} runs ./deploy hooks run (core.hooksPath)", ""
        return None, f"git pre-commit hook: core.hooksPath = {repo.hooks_path_value()!r}, pytemplate's checks are not in it", _hooks_path_hint(repo)
    target = repo.default_dir / HOOK
    state = classify(target, repo)
    chained = (repo.default_dir / LOCAL).is_file()
    if state == "installed":
        extra = f" (runs {LOCAL} first)" if chained else ""
        return True, f"git pre-commit hook installed: {_show(target, repo)} -> sh {repo.launcher} hooks run{extra}", ""
    if state == "outdated":
        return None, "git pre-commit hook outdated (another launcher path or template version)", "./deploy hooks install"
    if state == "calls":
        return True, f"git pre-commit hook: {_show(target, repo)} runs ./deploy hooks run", ""
    if state == "foreign":
        return None, f"git pre-commit hook: {_show(target, repo)} is another tool's hook", (
            f"./deploy hooks install --force keeps it as {LOCAL}, runs it first, then pytemplate's checks"
        )
    off = "" if cfg.hooks.pre_commit else "   (hooks.pre_commit = false: ./deploy setup does not install it)"
    return None, "git pre-commit hook not installed", "./deploy hooks install" + off


def show_status(cfg: Config, project: Path = ROOT) -> int:
    try:
        repo = find_repo(project)
    except DeployError as e:
        ui.info(f"hooks: {e} (no git hook)")
        return 0
    ui.step(f"git hooks: {_show(repo.hooks_dir, repo)}")
    passed, label, hint = _status_line(cfg, repo)
    ui.check_line(passed, label, hint)
    if repo.custom_hooks_path and classify(repo.default_dir / HOOK, repo) in ("installed", "outdated"):
        ui.check_line(None, f"{_show(repo.default_dir / HOOK, repo)} is pytemplate's but inactive (core.hooksPath)", "./deploy hooks uninstall removes it")
    return 0


def ensure_installed(cfg: Config, project: Path = ROOT) -> None:
    """Called by ./deploy setup: install the hook if hooks.pre_commit and it is missing (or
    update pytemplate's own). Prints at most one line and never fails setup."""
    if not cfg.hooks.pre_commit:
        return
    try:
        repo = find_repo(project)
    except DeployError:
        return  # no git, or not a git work tree: nothing to do
    try:
        if repo.custom_hooks_path:
            if classify(repo.hooks_dir / HOOK, repo) not in ("installed", "calls"):
                ui.info("git pre-commit hook: core.hooksPath is set, not installed (./deploy hooks status says what to add)")
            return
        state = classify(repo.default_dir / HOOK, repo)
        if state in ("missing", "outdated"):
            ui.ok(install(repo).splitlines()[0])
        elif state == "foreign":
            ui.info("git pre-commit hook: another tool's hook is installed, left alone (./deploy hooks install --force chains both)")
    except (DeployError, OSError) as e:
        ui.warn(f"git pre-commit hook not installed: {e}")


def doctor(cfg: Config, check: Check, project: Path = ROOT) -> None:
    """One line for ./deploy doctor: whether the hook is installed (nothing outside git)."""
    try:
        repo = find_repo(project)
    except DeployError:
        return
    ui.step("git hook")
    check(*_status_line(cfg, repo))


# --- run: the checks -------------------------------------------------------------------------------


@dataclass
class Result:
    passed: bool | None  # None: not applicable
    label: str
    hint: str = ""
    output: str = ""  # tool output printed under the line
    warnings: list[str] = field(default_factory=list)  # printed with ui.warn
    errors: list[str] = field(default_factory=list)  # printed with ui.error


def project_paths(prefix: str, names: Iterable[str], *, ignore_case: bool = IS_WINDOWS) -> list[str]:
    """Map paths relative to the top of the work tree to paths relative to the project
    (`prefix` = the project relative to the top); paths outside the project are dropped."""
    head = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def fold(s: str) -> str:
        return s.casefold() if ignore_case else s

    out: list[str] = []
    for name in names:
        if not name:
            continue
        if not head:
            out.append(name)
        elif fold(name[: len(head)]) == fold(head) and len(name) > len(head):
            out.append(name[len(head) :])
    return out


def staged_files(repo: Repo) -> list[str]:
    """Return the staged (added, copied, modified, renamed) files, relative to the project."""
    r = repo.git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    if r.returncode != 0:
        raise DeployError(f"git diff --cached failed: {r.stderr.strip()}")
    return project_paths(repo.prefix, r.stdout.split("\0"))


def unstaged_files(repo: Repo, paths: Sequence[str]) -> list[str]:
    """Return which of `paths` (relative to the project) have unstaged changes or are untracked
    (and not ignored): a commit made now would miss them."""
    if not paths:
        return []
    diff = repo.git("diff", "--name-only", "-z", "--", *paths)
    others = repo.git("ls-files", "--others", "--exclude-standard", "--full-name", "-z", "--", *paths)
    names = diff.stdout.split("\0") + others.stdout.split("\0")
    return sorted(set(project_paths(repo.prefix, names)))


def python_files(staged: Sequence[str], dirs: Sequence[str]) -> list[str]:
    """Return the staged .py/.pyi files under the code dirs (src/, tests/)."""
    return [p for p in staged if p.endswith(PY_SUFFIXES) and "/" in p and p.split("/", 1)[0] in dirs]


def _batches(files: Sequence[str], limit: int = ARG_LIMIT) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for f in files:
        if batch and size + len(f) + 3 > limit:
            yield batch
            batch, size = [], 0
        batch.append(f)
        size += len(f) + 3
    if batch:
        yield batch


def ruff(cfg: Config, args: Sequence[str | Path], files: Sequence[str]) -> tuple[int, str]:
    """Run `ruff ARGS FILES` in the tools environment (in batches); return (exit code, output)."""
    code = 0
    out: list[str] = []
    for batch in _batches(files):
        # --quiet: uv's own notes (creating .venv, installing) are not ruff's findings
        r = envs.uv(envs.tool_env(cfg), ["run", "--quiet", "--locked", "ruff", *args, *batch], capture=True, check=False, echo=False)
        code = max(code, r.returncode)
        out += [t.strip() for t in (r.stdout, r.stderr) if t.strip()]
    return code, "\n".join(out)


def uv_lock_check(cfg: Config) -> tuple[int, str]:
    r = envs.uv(envs.tool_env(cfg), ["lock", "--check"], capture=True, check=False, echo=False)
    return r.returncode, (r.stderr.strip() or r.stdout.strip())


def check_ruff(cfg: Config, files: Sequence[str]) -> Iterator[Result]:
    if not files:
        yield Result(None, "ruff: no staged Python file")
        return
    profile = cfg.profile_for()
    data = render.load_profile(profile)
    exit_zero = bool(data.get("ruff", {}).get("exit_zero"))
    config_file = _profile_file(cfg, profile, "ruff")
    n = f"{len(files)} file{'s' if len(files) != 1 else ''}"
    args: list[str | Path] = ["check", "--config", config_file, "--force-exclude", "--output-format", "concise"]
    code, out = ruff(cfg, [*args, "--exit-zero"] if exit_zero else args, files)
    if code == 0 and exit_zero and "Found " in out:
        yield Result(True, f"ruff check: {n}, warnings only (profile '{profile}')", output=out)
    elif code == 0:
        yield Result(True, f"ruff check: {n} (profile '{profile}')")
    else:
        yield Result(False, f"ruff check: {n} (profile '{profile}')", "./deploy lint --fix fixes some; then git add", output=out)
    code, out = ruff(cfg, ["format", "--check", "--config", config_file, "--force-exclude", "--output-format", "concise"], files)
    if code == 0:
        yield Result(True, f"ruff format: {n} formatted")
    else:
        yield Result(False, "ruff format: files need formatting", "./deploy fmt, then git add", output=out)


def check_generated(cfg: Config, generated: Sequence[str], dirty: set[str]) -> Iterator[Result]:
    changed, edited = render.apply(cfg, check=True)
    if changed or edited:
        hints = []
        if changed:
            hints.append(f"outdated: {', '.join(changed)}\n./deploy render, then git add them")
        if edited:
            hints.append(f"hand-edited: {', '.join(edited)}\nchange pytemplate.toml or .pytemplate/templates (./deploy render --diff), or ./deploy render --force")
        yield Result(False, "generated files up to date", "\n".join(hints))
    else:
        yield Result(True, "generated files up to date")
    missed = [p for p in generated if p in dirty]
    if missed:
        yield Result(False, "generated files staged", f"unstaged: {', '.join(missed)}\ngit add {' '.join(missed)}")
    else:
        yield Result(True, "generated files staged")


def _uv_error(out: str) -> str:
    """Return uv's `error:` message (uv wraps it: indented continuation lines), else its last line."""
    lines = out.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.lower().startswith("error")), None)
    if start is None:
        return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    parts = [lines[start].strip()]
    for ln in lines[start + 1 :]:
        if not ln.strip() or not ln[0].isspace():
            break
        parts.append(ln.strip())
    return " ".join(parts)


def check_lock(cfg: Config, staged: set[str], dirty: set[str]) -> Result:
    hints: list[str] = []
    if render.pyproject_outdated(cfg):
        hints.append("pyproject.toml does not match pytemplate.toml: ./deploy lock")
    code, out = uv_lock_check(cfg)
    if code != 0:
        hints.append(f"uv lock --check: {_uv_error(out) or f'exit code {code}'}\n./deploy lock")
    if "pyproject.toml" in staged and "uv.lock" in dirty:
        hints.append("pyproject.toml is staged but uv.lock has unstaged changes: git add uv.lock")
    if hints:
        return Result(False, "pyproject.toml and uv.lock", "\n".join(hints))
    return Result(True, "pyproject.toml and uv.lock up to date")


def check_mypyc(cfg: Config, project: Path, staged: set[str]) -> Result:
    if not cfg.supports("mypyc"):
        return Result(None, "mypyc rules: mypyc is not in backend.supported")
    try:
        sources = mypyc.compiled_sources(cfg)
    except DeployError as e:
        return Result(False, "mypyc rules", str(e))
    files = [p for p in sources if _within(p, project) in staged]
    if not files:
        return Result(None, "mypyc rules: no staged compiled module")
    findings = [str(f) for f in lintc.lint(cfg, files)]
    strict = cfg.profile_for() == "mypyc"
    if not findings:
        return Result(True, f"mypyc rules: {lintc.describe(files)}")
    if strict:
        return Result(False, f"mypyc rules: {len(findings)} problem(s)", "fix them, or move the code to a boundary module", errors=findings)
    return Result(True, f"mypyc rules: {len(findings)} warning(s) (non-blocking with profile '{cfg.profile_for()}')", warnings=findings)


def _index_modes(repo: Repo, names: Sequence[str]) -> dict[str, str]:
    r = repo.git("ls-files", "-s", "--", *names)
    modes: dict[str, str] = {}
    for line in r.stdout.splitlines():
        meta, _, path = line.partition("\t")
        if meta and path:
            modes[path.rsplit("/", 1)[-1]] = meta.split()[0]
    return modes


def check_launchers(repo: Repo, staged: set[str]) -> Result:
    names = [n for n in LAUNCHERS if n in staged]
    if not names:
        return Result(None, "launchers: none staged")
    from .shells import launcher_problems  # the doctor's static checks (a big module: only here)

    modes = _index_modes(repo, names)
    problems: list[str] = []
    fixes: list[str] = []
    for name in names:
        path = repo.project / name
        if not path.is_file():
            continue
        for problem, fix in launcher_problems(name, path.read_bytes(), modes.get(name) if name == "deploy" else None):
            problems.append(f"{name}: {problem}")
            fixes.append(fix)
    if problems:
        return Result(False, "launchers: " + "; ".join(problems), "\n".join(dict.fromkeys(fixes)))
    return Result(True, f"launchers: {', '.join(names)}")


class _Anything:
    """Stands in for pytest while the language guard module is imported without it."""

    def __getattr__(self, name: str) -> _Anything:
        return self

    def __call__(self, *args: object, **kwargs: object) -> _Anything:
        return self


def load_language_guard(path: Path) -> types.ModuleType:
    """Import .pytemplate/tests/test_no_spanish.py (it imports pytest, which the runner lacks)."""
    spec = importlib.util.spec_from_file_location("_pytemplate_language_guard", path)
    if spec is None or spec.loader is None:
        raise DeployError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    shim = "pytest" not in sys.modules and importlib.util.find_spec("pytest") is None
    if shim:
        fake = types.ModuleType("pytest")
        setattr(fake, "mark", _Anything())  # pytest.mark.skipif(...) at module level
        sys.modules["pytest"] = fake
    try:
        spec.loader.exec_module(module)
    finally:
        if shim:
            sys.modules.pop("pytest", None)
    return module


def check_language(project: Path, staged: Sequence[str], guard_file: Path) -> Result:
    guard = load_language_guard(guard_file)
    offending: Callable[[str], list[tuple[int, str, str]]] = guard.offending_lines
    allowed: frozenset[str] = guard.ALLOWED_PATHS
    binary: frozenset[str] = guard.BINARY_SUFFIXES
    found: list[str] = []
    checked = 0
    for rel_path in staged:
        path = project / rel_path
        if rel_path in allowed or path.suffix.lower() in binary or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        checked += 1
        found += [f"{rel_path}:{n}: {word!r}: {line[:100]}" for n, word, line in offending(text)]
    if found:
        return Result(False, f"language guard: {len(found)} line(s) not in English", "translate them, or add `lang: allow` to the line", output="\n".join(found[:50]))
    return Result(True, f"language guard: {checked} file(s) in English")


def checks(cfg: Config, repo: Repo, staged: Sequence[str], *, code_dirs: Sequence[str], template_repo: bool) -> Iterator[Result]:
    """Yield the result of every check, in order (the caller prints them as they come)."""
    staged_set = set(staged)
    yield from check_ruff(cfg, python_files(staged, code_dirs))
    generated = sorted({*render.outputs(cfg), STATE_FILE.relative_to(ROOT).as_posix()})
    dirty = set(unstaged_files(repo, [*generated, "uv.lock"]))
    yield from check_generated(cfg, generated, dirty)
    yield check_lock(cfg, staged_set, dirty)
    yield check_mypyc(cfg, repo.project, staged_set)
    yield check_launchers(repo, staged_set)
    if template_repo:
        yield check_language(repo.project, staged, TEMPLATE / "tests" / "test_no_spanish.py")


def _print(result: Result) -> None:
    """The check line, then the tool output, then how to fix it."""
    ui.check_line(result.passed, result.label)
    for line in result.output.splitlines():
        ui.info(f"         {line}".rstrip())
    for w in result.warnings:
        ui.warn(w)
    for e in result.errors:
        ui.error(e)
    if result.passed is not True:
        for line in result.hint.splitlines():
            ui.info(f"         {line}")


def run(cfg: Config, repo: Repo) -> int:
    """Check the staged files; return 1 if any check failed."""
    start = time.perf_counter()
    for key in GIT_REPO_VARS:  # uv and ruff must not see the hook's repository (repo.env keeps them for git)
        os.environ.pop(key, None)
    staged = staged_files(repo)
    if not staged:
        ui.info("pre-commit: no staged file in this project: nothing to check")
        return 0
    ui.step(f"pre-commit: checking {len(staged)} staged file{'s' if len(staged) != 1 else ''}")
    dirs = [d for d in ("src", "tests") if (repo.project / d).is_dir()]
    failed = 0
    for result in checks(cfg, repo, staged, code_dirs=dirs, template_repo=(TEMPLATE / "template-repo").is_file()):
        _print(result)
        failed += result.passed is False
    seconds = time.perf_counter() - start
    if failed:
        ui.error(
            f"pre-commit: {failed} check{'s' if failed != 1 else ''} failed ({seconds:.1f} s). Fix, `git add` and commit again\n"
            "  (skip the hook once: git commit --no-verify)"
        )
        return 1
    ui.ok(f"pre-commit: all checks passed ({seconds:.1f} s)")
    return 0


# --- the command ---------------------------------------------------------------------------------


def cmd_hooks(cfg: Config, args: list[str]) -> int:
    """hooks [install [--force]|uninstall|run|status]"""
    sub, rest = (args[0], args[1:]) if args else ("status", [])
    allowed: dict[str, tuple[str, ...]] = {"install": ("--force",), "uninstall": (), "run": (), "status": ()}
    if sub not in allowed:
        raise DeployError(f"hooks: unknown subcommand {sub!r}  ({USAGE})")
    flags = only_flags(f"hooks {sub}", rest, allowed[sub])
    if sub == "status":
        return show_status(cfg)
    repo = find_repo(ROOT)
    if sub == "run":
        return run(cfg, repo)
    if sub == "install":
        ui.ok(install(repo, force="--force" in flags))
    else:
        ui.info(uninstall(repo))
    return 0
