"""Tests for runner/hooks.py: the git pre-commit hook (script, install/uninstall/status, foreign
hooks, core.hooksPath, the git environment of hooks, the staged-file mapping and which checks
run on which files).

Every repository is a throwaway `git init` in tmp_path; tests that need git skip without it.
The real end-to-end run (uv, ruff, real commits from every shell) is manual: see the report
of the change that added this file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config, hooks, lintc, mypyc, proc, render  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.project import IS_WINDOWS, TEMPLATE  # noqa: E402
from runner.ui import DeployError  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
GUARD = TEMPLATE / "tests" / "test_no_spanish.py"


def make(data: dict[str, object] | None = None) -> Config:
    cfg: Config = config._build(Config, data or {}, "")
    return cfg


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"  # no core.autocrlf / core.hooksPath from the machine
    return env


def git(cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.autocrlf=false", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=check, env=env or git_env(),
    )


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The runner's own git calls: no machine config, no discovery above tmp_path."""
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))


def make_repo(tmp_path: Path, sub: str = "") -> tuple[Path, Path]:
    """Return (top, project): a git repository with the project in `sub`."""
    top = tmp_path / "repo"
    top.mkdir()
    git(top, "init", "-q")
    project = top / sub if sub else top
    project.mkdir(parents=True, exist_ok=True)
    return top, project


def find(project: Path, top: Path | None = None, environ: dict[str, str] | None = None) -> hooks.Repo:
    return hooks.find_repo(project, environ=environ or {}, cwd=top or project)


# --- the script ------------------------------------------------------------------------------------


def test_hook_script_is_ascii_lf_and_marked() -> None:
    text = hooks.hook_script("./deploy")
    assert text.isascii()
    assert "\r" not in text
    assert text.startswith("#!/bin/sh\n")
    assert hooks.MARKER in text
    assert "./deploy hooks uninstall" in text
    assert "git commit --no-verify" in text
    assert "_pt_launcher='./deploy'" in text
    assert text.rstrip().endswith('exec sh "$_pt_launcher" hooks run')
    assert hooks.LOCAL in text


def test_sh_literal() -> None:
    assert hooks.sh_literal("./a b/deploy") == "'./a b/deploy'"
    assert hooks.sh_literal("it's") == "'it'\\''s'"
    quoted = hooks.sh_literal("./caf\u00e9/deploy")
    assert quoted.isascii()
    assert quoted == "\"$(printf './caf\\303\\251/deploy')\""
    assert hooks.sh_literal("50%\\x\u00e9").isascii()


def posix_sh() -> str | None:
    if not IS_WINDOWS:
        return "/bin/sh" if Path("/bin/sh").is_file() else None
    from runner import shells

    for sh in shells.discover(distros=lambda wsl: []):
        if sh.name in ("git-sh", "msys2-dash", "git-dash"):
            return sh.argv[0]
    return None


def test_sh_literal_round_trip_in_sh() -> None:
    sh = posix_sh()
    if sh is None:
        pytest.skip("no POSIX sh")
    text = "./caf\u00e9 50%/it's \\n/deploy"
    r = subprocess.run([sh, "-c", f"printf '%s' {hooks.sh_literal(text)}"], capture_output=True, check=True)
    assert r.stdout.decode("utf-8") == text


@needs_git
def test_launcher_path_is_relative_to_the_top(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path, "apps/my app")
    repo = find(project, top)
    assert repo.prefix == "apps/my app"
    assert repo.launcher == "./apps/my app/deploy"
    assert "_pt_launcher='./apps/my app/deploy'" in hooks.hook_script(repo.launcher)
    assert hooks.run_line(repo) == "sh './apps/my app/deploy' hooks run || exit $?"
    assert find(top).launcher == "./deploy"
    assert hooks.run_line(find(top)) == "sh ./deploy hooks run || exit $?"


def test_not_a_git_work_tree(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        with pytest.raises(DeployError) as e:
            find(tmp_path)
        assert e.value.code == 3
        return
    with pytest.raises(DeployError, match="not inside a git work tree") as e:
        find(tmp_path)
    assert e.value.code == 2


# --- install / uninstall / status --------------------------------------------------------------------


@needs_git
def test_install_update_uninstall(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    repo = find(project)
    target = repo.default_dir / hooks.HOOK
    assert not repo.custom_hooks_path
    assert hooks.classify(target, repo) == "missing"
    assert "installed" in hooks.install(repo)
    data = target.read_bytes()
    assert data == hooks.hook_script("./deploy").encode("ascii")
    if not IS_WINDOWS:
        assert target.stat().st_mode & 0o777 == 0o755
    assert hooks.classify(target, repo) == "installed"
    assert "already installed" in hooks.install(repo)
    target.write_text(f"#!/bin/sh\n# {hooks.MARKER}\nexec sh ./old/deploy hooks run\n", encoding="utf-8")
    assert hooks.classify(target, repo) == "outdated"
    assert "updated" in hooks.install(repo)
    assert target.read_bytes() == data
    assert "removed" in hooks.uninstall(repo)
    assert not target.exists()
    assert hooks.uninstall(repo) == "no pytemplate pre-commit hook installed"


@needs_git
def test_install_creates_a_missing_hooks_dir(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    shutil.rmtree(top / ".git" / "hooks")
    repo = find(project)
    hooks.install(repo)
    assert (top / ".git" / "hooks" / hooks.HOOK).is_file()


@needs_git
def test_foreign_hook_is_preserved_and_chained(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    repo = find(project)
    target = repo.default_dir / hooks.HOOK
    local = repo.default_dir / hooks.LOCAL
    foreign = b"#!/bin/sh\necho mine\n"
    target.write_bytes(foreign)
    assert hooks.classify(target, repo) == "foreign"
    with pytest.raises(DeployError, match="--force"):
        hooks.install(repo)
    assert target.read_bytes() == foreign
    msg = hooks.install(repo, force=True)
    assert hooks.LOCAL in msg
    assert local.read_bytes() == foreign
    assert hooks.classify(target, repo) == "installed"
    passed, label, _ = hooks._status_line(make(), repo)
    assert passed is True and hooks.LOCAL in label
    assert "restored" in hooks.uninstall(repo)
    assert target.read_bytes() == foreign
    assert not local.exists()
    # both files there already: never overwrite either
    local.write_bytes(b"#!/bin/sh\necho older\n")
    with pytest.raises(DeployError, match="merge them by hand"):
        hooks.install(repo, force=True)
    assert target.read_bytes() == foreign


@needs_git
def test_a_hook_that_already_calls_hooks_run(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    repo = find(project)
    target = repo.default_dir / hooks.HOOK
    target.write_text("#!/bin/sh\nnpm test || exit 1\nsh ./deploy hooks run\n", encoding="utf-8")
    assert hooks.classify(target, repo) == "calls"
    assert "already runs" in hooks.install(repo)
    assert "left alone" in hooks.uninstall(repo)
    assert target.is_file()


@needs_git
def test_core_hooks_path_is_respected(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path, "proj")
    git(top, "config", "core.hooksPath", "hk")
    repo = find(project, top)
    assert repo.custom_hooks_path
    assert repo.hooks_dir == Path(os.path.normpath(top / "hk"))
    with pytest.raises(DeployError) as e:
        hooks.install(repo, force=True)
    assert "core.hooksPath = 'hk'" in str(e.value)
    assert "sh ./proj/deploy hooks run || exit $?" in str(e.value)
    assert not (repo.default_dir / hooks.HOOK).exists()
    assert not (top / "hk").exists()
    passed, label, hint = hooks._status_line(make(), repo)
    assert passed is None and "core.hooksPath" in label and "sh ./proj/deploy hooks run" in hint
    (top / "hk").mkdir()
    (top / "hk" / hooks.HOOK).write_text("#!/bin/sh\nsh ./proj/deploy hooks run || exit $?\n", encoding="utf-8")
    assert "already runs" in hooks.install(repo)
    assert hooks._status_line(make(), repo)[0] is True
    # core.hooksPath naming the default folder is not a custom one
    git(top, "config", "core.hooksPath", ".git/hooks")
    assert not find(project, top).custom_hooks_path


@needs_git
def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    top, project = make_repo(tmp_path)
    repo = find(project)
    target = repo.default_dir / hooks.HOOK
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert "would be installed" in hooks.install(repo)
    assert not target.exists()
    monkeypatch.setattr(proc, "DRY_RUN", False)
    hooks.install(repo)
    monkeypatch.setattr(proc, "DRY_RUN", True)
    assert "would remove" in hooks.uninstall(repo)
    assert target.is_file()


@needs_git
def test_ensure_installed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    top, project = make_repo(tmp_path)
    target = top / ".git" / "hooks" / hooks.HOOK
    hooks.ensure_installed(make({"hooks": {"pre_commit": False}}), project)
    assert not target.exists()
    hooks.ensure_installed(make(), project)
    assert hooks.MARKER in target.read_text(encoding="utf-8")
    assert "installed" in capsys.readouterr().err
    hooks.ensure_installed(make(), project)  # already there: silent
    assert capsys.readouterr().err == ""
    target.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    hooks.ensure_installed(make(), project)
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert "another tool's hook" in capsys.readouterr().err
    outside = tmp_path / "plain"
    outside.mkdir()
    hooks.ensure_installed(make(), outside)  # not a git work tree: nothing, no error
    assert capsys.readouterr().err == ""


@needs_git
def test_doctor_lines(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    lines: list[tuple[bool | None, str, str]] = []

    def check(passed: bool | None, label: str, hint: str = "") -> None:
        lines.append((passed, label, hint))

    hooks.doctor(make(), check, project)
    assert lines[-1][0] is None and "not installed" in lines[-1][1] and lines[-1][2].startswith("./deploy hooks install")
    hooks.install(find(project))
    hooks.doctor(make(), check, project)
    assert lines[-1][0] is True
    (top / ".git" / "hooks" / hooks.HOOK).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hooks.doctor(make(), check, project)
    assert lines[-1][0] is None and "another tool" in lines[-1][1]
    count = len(lines)
    outside = tmp_path / "plain"
    outside.mkdir()
    hooks.doctor(make(), check, outside)
    assert len(lines) == count


def test_cmd_hooks_rejects_bad_arguments() -> None:
    with pytest.raises(DeployError, match="unknown subcommand"):
        hooks.cmd_hooks(make(), ["instal"])
    with pytest.raises(DeployError, match="unrecognized arguments"):
        hooks.cmd_hooks(make(), ["install", "--forse"])
    with pytest.raises(DeployError, match="unrecognized arguments"):
        hooks.cmd_hooks(make(), ["run", "src"])


# --- the environment git gives hooks, and the staged files ------------------------------------------


def test_git_env_pins_relative_paths(tmp_path: Path) -> None:
    top = tmp_path / "top"
    env = hooks.git_env({"GIT_INDEX_FILE": ".git/index", "GIT_AUTHOR_NAME": "x"}, top)
    assert env == {"GIT_INDEX_FILE": os.path.normpath(top / ".git" / "index")}
    absolute = str(tmp_path / "wt" / "index")
    env = hooks.git_env({"GIT_DIR": ".git", "GIT_INDEX_FILE": absolute}, top)
    assert env["GIT_DIR"] == os.path.normpath(top / ".git")
    assert env["GIT_INDEX_FILE"] == absolute
    assert env["GIT_WORK_TREE"] == str(top)  # GIT_DIR alone means: the cwd is the top
    assert hooks.git_env({"GIT_DIR": ".git", "GIT_WORK_TREE": "w"}, top)["GIT_WORK_TREE"] == os.path.normpath(top / "w")
    assert hooks.git_env({}, top) == {}


def test_project_paths() -> None:
    names = ["a.py", "apps/p/src/x.py", "apps/p/deploy", "apps/pp/y.py", "apps/p", "", "Apps/P/z.py"]
    assert hooks.project_paths("", names, ignore_case=False) == ["a.py", "apps/p/src/x.py", "apps/p/deploy", "apps/pp/y.py", "apps/p", "Apps/P/z.py"]
    assert hooks.project_paths("apps/p", names, ignore_case=False) == ["src/x.py", "deploy"]
    assert hooks.project_paths("apps/p/", names, ignore_case=True) == ["src/x.py", "deploy", "z.py"]


def test_python_files() -> None:
    staged = ["src/a.py", "src/pkg/b.pyi", "tests/test_c.py", "tools/d.py", "e.py", "src/f.txt", ".pytemplate/runner/g.py"]
    assert hooks.python_files(staged, ["src", "tests"]) == ["src/a.py", "src/pkg/b.pyi", "tests/test_c.py"]
    assert hooks.python_files(staged, ["src"]) == ["src/a.py", "src/pkg/b.pyi"]


def test_batches() -> None:
    files = [f"src/{i:03}.py" for i in range(100)]
    batches = list(hooks._batches(files, limit=100))
    assert [f for b in batches for f in b] == files
    assert all(sum(len(f) + 3 for f in b) <= 100 for b in batches)


@needs_git
def test_staged_files_from_a_subfolder_project(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path, "apps/p")
    for rel in ("outside.txt", "apps/p/src/a.py", "apps/p/src/gone.py", "apps/p/old.txt"):
        (top / rel).parent.mkdir(parents=True, exist_ok=True)
        (top / rel).write_text("x = 1\n", encoding="utf-8")
    git(top, "add", "-A")
    git(top, "commit", "-q", "--no-verify", "-m", "one")
    (top / "apps/p/src/a.py").write_text("x = 2\n", encoding="utf-8")
    (top / "apps/p/new.py").write_text("y = 1\n", encoding="utf-8")
    (top / "outside.txt").write_text("changed\n", encoding="utf-8")
    git(top, "rm", "-q", "apps/p/src/gone.py")
    git(top, "mv", "apps/p/old.txt", "apps/p/renamed.txt")
    git(top, "add", "-A")
    repo = find(project, top)
    assert sorted(hooks.staged_files(repo)) == ["new.py", "renamed.txt", "src/a.py"]
    # as a hook sees it: a relative GIT_INDEX_FILE (relative to the top), GIT_DIR without GIT_WORK_TREE
    for environ in ({"GIT_INDEX_FILE": ".git/index"}, {"GIT_DIR": ".git", "GIT_INDEX_FILE": ".git/index"}):
        hooked = find(project, top, environ)
        assert sorted(hooks.staged_files(hooked)) == ["new.py", "renamed.txt", "src/a.py"]
        assert hooked.launcher == "./apps/p/deploy"


@needs_git
def test_staged_files_in_the_first_commit(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path)
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(top, "add", "a.py")
    assert hooks.staged_files(find(project)) == ["a.py"]


@needs_git
def test_unstaged_files(tmp_path: Path) -> None:
    top, project = make_repo(tmp_path, "p")
    for name in ("gen.json", "clean.json", "ignored.json"):
        (project / name).write_text("{}\n", encoding="utf-8")
    (top / ".gitignore").write_text("p/ignored.json\n", encoding="utf-8")
    git(top, "add", "p/gen.json", "p/clean.json", ".gitignore")
    git(top, "commit", "-q", "--no-verify", "-m", "one")
    (project / "gen.json").write_text('{"a": 1}\n', encoding="utf-8")
    (project / "new.json").write_text("{}\n", encoding="utf-8")
    repo = find(project, top)
    paths = ["gen.json", "clean.json", "ignored.json", "new.json", "missing.json"]
    assert hooks.unstaged_files(repo, paths) == ["gen.json", "new.json"]
    git(top, "add", "p/gen.json")
    assert hooks.unstaged_files(repo, paths) == ["new.json"]
    assert hooks.unstaged_files(repo, []) == []


# --- which checks run on which files ---------------------------------------------------------------


class Tools:
    """Records the tool calls of `hooks.checks` (ruff, uv lock --check) and fakes the renderer."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.ruff_calls: list[tuple[list[str], list[str]]] = []
        self.ruff_code = 0
        self.ruff_output = ""
        self.lock_code = 0
        self.generated: dict[str, str] = {"gen.json": "{}\n"}
        self.render_result: tuple[list[str], list[str]] = ([], [])
        self.pyproject_outdated = False
        self.compiled: list[Path] = []
        self.findings: list[str] = []
        self.linted: list[Path] = []
        cfg_file = tmp_path / "ruff-profile.toml"
        monkeypatch.setattr(hooks, "ruff", self._ruff)
        monkeypatch.setattr(hooks, "uv_lock_check", lambda cfg: (self.lock_code, "uv.lock needs to be updated"))
        monkeypatch.setattr(hooks, "_profile_file", lambda cfg, profile, kind: cfg_file)
        monkeypatch.setattr(render, "outputs", lambda cfg: dict(self.generated))
        monkeypatch.setattr(render, "apply", lambda cfg, check=False: self.render_result)
        monkeypatch.setattr(render, "pyproject_outdated", lambda cfg: self.pyproject_outdated)
        monkeypatch.setattr(mypyc, "compiled_sources", lambda cfg: list(self.compiled))
        monkeypatch.setattr(lintc, "lint", self._lint)
        monkeypatch.setattr(lintc, "describe", lambda files: ", ".join(p.stem for p in files))

    def _ruff(self, cfg: Config, args: Sequence[str | Path], files: Sequence[str]) -> tuple[int, str]:
        self.ruff_calls.append(([str(a) for a in args], list(files)))
        return self.ruff_code, self.ruff_output

    def _lint(self, cfg: Config, files: list[Path]) -> list[str]:
        self.linted += files
        return list(self.findings)


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Tools:
    return Tools(monkeypatch, tmp_path)


def staged_project(tmp_path: Path, files: dict[str, bytes], *, commit: Sequence[str] = ()) -> tuple[hooks.Repo, list[str]]:
    """A project in a subfolder with `files` written, `commit` committed first, the rest staged."""
    top, project = make_repo(tmp_path, "proj")
    for rel, data in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_bytes(data)
    if commit:
        git(project, "add", *commit)
        git(project, "commit", "-q", "--no-verify", "-m", "base")
    git(project, "add", "-A")
    repo = find(project, top)
    return repo, hooks.staged_files(repo)


def results(cfg: Config, repo: hooks.Repo, staged: list[str], *, template_repo: bool = False) -> dict[str, hooks.Result]:
    out: dict[str, hooks.Result] = {}
    for r in hooks.checks(cfg, repo, staged, code_dirs=["src", "tests"], template_repo=template_repo):
        out[r.label.split(":")[0].split(" (")[0]] = r
    return out


@needs_git
def test_checks_ruff_only_on_staged_python_files(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {
        "src/pkg/a.py": b"x = 1\n", "src/pkg/b.pyi": b"y: int\n", "tests/test_a.py": b"def test(): pass\n",
        "tools/other.py": b"z = 1\n", "README.md": b"# r\n", "gen.json": b"{}\n",
    })
    res = results(make(), repo, staged)
    assert [files for _, files in tools.ruff_calls] == [["src/pkg/a.py", "src/pkg/b.pyi", "tests/test_a.py"]] * 2
    check_args, format_args = (args for args, _ in tools.ruff_calls)
    assert check_args[:1] == ["check"] and "--force-exclude" in check_args and "--exit-zero" not in check_args
    assert format_args[:2] == ["format", "--check"]
    assert res["ruff check"].passed is True and "profile 'off'" in res["ruff check"].label
    assert res["ruff format"].passed is True
    assert res["generated files up to date"].passed is True
    assert res["generated files staged"].passed is True
    assert res["pyproject.toml and uv.lock up to date"].passed is True
    assert res["mypyc rules"].passed is None
    assert res["launchers"].passed is None
    assert "language guard" not in res


@needs_git
def test_checks_skip_ruff_without_python_files(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"README.md": b"# r\n"})
    res = results(make(), repo, staged)
    assert tools.ruff_calls == []
    assert res["ruff"].passed is None


@needs_git
def test_checks_report_ruff_failures_and_exit_zero(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"src/a.py": b"print(y)\n"})
    tools.ruff_code, tools.ruff_output = 1, "src/a.py:1:7: F821 Undefined name `y`\nFound 1 error."
    res = results(make(), repo, staged)
    assert res["ruff check"].passed is False and "F821" in res["ruff check"].output
    assert res["ruff format"].passed is False and "./deploy fmt" in res["ruff format"].hint
    tools.ruff_calls.clear()
    tools.ruff_code = 0
    res = results(make({"typing": {"relaxed": "warn"}}), repo, staged)
    assert "--exit-zero" in tools.ruff_calls[0][0]
    assert res["ruff check"].passed is True and "warnings only" in res["ruff check"].label


@needs_git
def test_checks_generated_files(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"gen.json": b"{}\n", "src/a.py": b"x = 1\n"}, commit=["gen.json"])
    (repo.project / "gen.json").write_bytes(b'{"new": 1}\n')  # regenerated after the staging
    staged = hooks.staged_files(repo)
    res = results(make(), repo, staged)
    assert res["generated files staged"].passed is False
    assert "git add gen.json" in res["generated files staged"].hint
    tools.render_result = (["gen.json"], [".mypy.ini"])
    res = results(make(), repo, staged)
    assert res["generated files up to date"].passed is False
    assert "./deploy render" in res["generated files up to date"].hint and ".mypy.ini" in res["generated files up to date"].hint


@needs_git
def test_checks_lock(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(
        tmp_path, {"pyproject.toml": b"[project]\n", "uv.lock": b"v1\n"}, commit=["pyproject.toml", "uv.lock"]
    )
    (repo.project / "pyproject.toml").write_bytes(b"[project]\nname = 'x'\n")
    git(repo.project, "add", "pyproject.toml")
    (repo.project / "uv.lock").write_bytes(b"v2\n")  # re-locked but not staged
    staged = hooks.staged_files(repo)
    assert staged == ["pyproject.toml"]
    res = results(make(), repo, staged)
    assert res["pyproject.toml and uv.lock"].passed is False
    assert "git add uv.lock" in res["pyproject.toml and uv.lock"].hint
    git(repo.project, "add", "uv.lock")
    tools.lock_code = 1
    tools.pyproject_outdated = True
    res = results(make(), repo, hooks.staged_files(repo))
    hint = res["pyproject.toml and uv.lock"].hint
    assert "does not match pytemplate.toml" in hint and "uv.lock needs to be updated" in hint and "git add" not in hint


def test_uv_error_message() -> None:
    out = (
        "Resolved 25 packages in 20ms\n"
        "error: The lockfile at `uv.lock` needs to be updated, but `--check` was\n"
        "       provided.\n\nhint: To update the lockfile, run `uv lock`.\n"
    )
    assert hooks._uv_error(out) == "error: The lockfile at `uv.lock` needs to be updated, but `--check` was provided."
    assert hooks._uv_error("one\nlast line\n\n") == "last line"
    assert hooks._uv_error("") == ""


@needs_git
def test_checks_mypyc_rules_only_on_staged_compiled_modules(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"src/pkg/core/a.py": b"x = 1\n", "src/pkg/core/b.py": b"y = 1\n"}, commit=["src/pkg/core/b.py"])
    tools.compiled = [repo.project / "src/pkg/core/a.py", repo.project / "src/pkg/core/b.py"]
    tools.findings = ["src/pkg/core/a.py:1: nested class 'X': mypyc does not support it"]
    res = results(make(), repo, staged)  # cpython active: profile 'off' -> warnings
    assert tools.linted == [repo.project / "src/pkg/core/a.py"]
    assert res["mypyc rules"].passed is True and res["mypyc rules"].warnings == tools.findings
    res = results(make({"backend": {"active": "mypyc"}}), repo, staged)
    assert res["mypyc rules"].passed is False and res["mypyc rules"].errors == tools.findings
    res = results(make({"backend": {"supported": ["cpython"]}}), repo, staged)
    assert res["mypyc rules"].passed is None


@needs_git
def test_checks_launchers(tmp_path: Path, tools: Tools) -> None:
    repo, staged = staged_project(tmp_path, {"deploy": b"#!/bin/sh\r\necho hi\r\n", "deploy.cmd": b"@echo off\r\n"})
    res = results(make(), repo, staged)
    launchers = next(r for k, r in res.items() if k.startswith("launchers"))
    assert launchers.passed is False
    assert "deploy: CRLF line endings" in launchers.label
    assert "git mode 100644" in launchers.label  # staged without the exec bit
    assert "git update-index --chmod=+x deploy" in launchers.hint
    (repo.project / "deploy").write_bytes(b"#!/bin/sh\necho hi\n")
    git(repo.project, "add", "--chmod=+x", "deploy")
    res = results(make(), repo, hooks.staged_files(repo))
    assert res["launchers"].passed is True


@needs_git
def test_checks_language_guard_in_the_template_repo(tmp_path: Path, tools: Tools) -> None:
    spanish = "# no se encuentra el archivo\n".encode()  # lang: allow
    repo, staged = staged_project(tmp_path, {"notes.md": spanish, "ok.md": b"# fine\n", "uv.lock": spanish, "logo.png": spanish})
    res = results(make(), repo, staged, template_repo=True)
    guard = res["language guard"]
    assert guard.passed is False
    assert guard.output.startswith("notes.md:1:")
    assert "uv.lock" not in guard.output and "logo.png" not in guard.output


def test_language_guard_loads_without_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    real_find_spec = importlib.util.find_spec
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "pytest" else real_find_spec(name, *a))
    guard = hooks.load_language_guard(GUARD)
    assert "pytest" not in sys.modules
    assert guard.offending_lines("no se encuentra el archivo")  # lang: allow
    assert not guard.offending_lines("plain English")


@needs_git
def test_run_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    repo, staged = staged_project(tmp_path, {"a.txt": b"a\n"})
    outcome: list[hooks.Result] = []

    def fake_checks(cfg: Config, repo: hooks.Repo, staged: Sequence[str], **_: object) -> Iterator[hooks.Result]:
        yield from outcome

    monkeypatch.setattr(hooks, "checks", fake_checks)
    for key in hooks.GIT_REPO_VARS:
        monkeypatch.setenv(key, "x")  # run() drops them for the tools; monkeypatch restores them
    outcome[:] = [hooks.Result(True, "one"), hooks.Result(None, "two")]
    assert hooks.run(make(), repo) == 0
    assert "all checks passed" in capsys.readouterr().err
    assert not any(key in os.environ for key in hooks.GIT_REPO_VARS)
    outcome[:] = [hooks.Result(False, "bad", "how to fix", output="tool says no")]
    assert hooks.run(make(), repo) == 1
    err = capsys.readouterr().err
    assert err.index("tool says no") < err.index("how to fix") < err.index("1 check failed")
    assert "git commit --no-verify" in err
    git(repo.project, "reset", "-q")
    assert hooks.run(make(), repo) == 0
    assert "nothing to check" in capsys.readouterr().err


# --- the real hook, run by git ---------------------------------------------------------------------

FAKE_LAUNCHER = """#!/bin/sh
if [ -f .topmark ]; then _w=top; else _w=elsewhere; fi
printf '%s\\n' "launcher $* from $_w" >> "$PT_HOOK_LOG"
exit "${PT_HOOK_EXIT:-0}"
"""
LOCAL_HOOK = """#!/bin/sh
printf '%s\\n' "local hook" >> "$PT_HOOK_LOG"
exit "${PT_LOCAL_EXIT:-0}"
"""


@needs_git
@pytest.mark.parametrize("sub", ["", "apps/my app", "caf\u00e9"])
def test_git_runs_the_hook(tmp_path: Path, sub: str) -> None:
    """git runs the installed hook from the top: it calls `sh <launcher> hooks run`, chains a
    kept foreign hook first, blocks the commit on failure, and --no-verify skips it."""
    top, project = make_repo(tmp_path, sub)
    (project / "deploy").write_bytes(FAKE_LAUNCHER.encode("ascii"))  # no exec bit needed: the hook uses sh
    (top / ".topmark").write_text("", encoding="utf-8")
    (top / ".git" / "info" / "exclude").write_text(".topmark\n", encoding="utf-8")
    log = tmp_path / "hook.log"
    env = dict(git_env(), PT_HOOK_LOG=log.as_posix())
    hooks_dir = top / ".git" / "hooks"
    (hooks_dir / hooks.HOOK).write_bytes(LOCAL_HOOK.encode("ascii"))
    if not IS_WINDOWS:
        (hooks_dir / hooks.HOOK).chmod(0o755)
    hooks.install(find(project, top), force=True)

    def commit(name: str, *flags: str, **extra: str) -> int:
        (project / name).write_text(name, encoding="utf-8")
        git(top, "add", "-A", env=env)
        return git(top, "commit", "-q", "-m", name, *flags, env={**env, **extra}, check=False).returncode

    assert commit("one.txt") == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["local hook", "launcher hooks run from top"]
    log.unlink()
    assert commit("two.txt", PT_HOOK_EXIT="1") != 0
    assert log.read_text(encoding="utf-8").splitlines() == ["local hook", "launcher hooks run from top"]
    log.unlink()
    assert commit("three.txt", PT_LOCAL_EXIT="1") != 0
    assert log.read_text(encoding="utf-8").splitlines() == ["local hook"]  # stops before the checks
    log.unlink()
    assert commit("four.txt", "--no-verify", PT_HOOK_EXIT="1") == 0
    assert not log.exists()
    (project / "deploy").unlink()  # a checkout without the launcher: the checks are skipped
    (hooks_dir / hooks.LOCAL).unlink()
    assert commit("five.txt") == 0
    assert git(top, "log", "--format=%s", env=env).stdout.split() == ["five.txt", "four.txt", "one.txt"]
