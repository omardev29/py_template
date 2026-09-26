"""Regression tests for the runner bug fixes (portable, pyz, tasks, presets, mypyc, config)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipapp
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_dev, cmd_env, config, envs, mypyc, presets, tasks  # noqa: E402
from runner.config import Config  # noqa: E402
from runner.methods import flet, nuitka, portable, pyz, wheel  # noqa: E402
from runner.ui import DeployError  # noqa: E402

IS_WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="needs cmd.exe")


def make(data: dict[str, Any]) -> Config:
    cfg: Config = config._build(Config, data, "")
    config.validate(cfg)
    return cfg


def _posix_sh() -> str | None:
    """Return a real POSIX sh (never the System32 WSL stub), or None."""
    candidates = [shutil.which("sh")]
    if IS_WINDOWS:
        candidates += [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "sh.exe"),
            os.path.expanduser(r"~\scoop\apps\git\current\bin\sh.exe"),
        ]
    for c in candidates:
        if c and os.path.isfile(c) and "system32" not in c.lower() and "windowsapps" not in c.lower():
            return c
    return None


def _run_cmd(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["cmd.exe", "/d", "/c", str(path)], capture_output=True, text=True, timeout=120, check=False)


# --- 1. the portable smoke test sees lib/ like boot.py --------------------------------------------


def test_uv_run_pins_the_project_outside_the_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The `flet build` stage has its own pyproject.toml: without --project, uv took it for
    # the project and `uv run --locked` failed with "Unable to find lockfile at uv.lock".
    from runner import proc
    from runner.project import ROOT

    seen: list[list[str]] = []

    def fake_run(argv: list[Any], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(proc, "run", fake_run)
    env = envs.cpython_env(make({}))
    envs.uv_run(env, ["flet", "--version"], cwd=tmp_path)
    envs.uv_run(env, ["ruff", "--version"])
    envs.uv_run(env, ["ruff", "--version"], cwd=ROOT)
    elsewhere, default, root = seen
    assert elsewhere[1:5] == ["run", "--locked", "--project", str(ROOT)]
    assert "--project" not in default and "--project" not in root


def test_portable_smoke_code_puts_lib_on_sys_path(tmp_path: Path) -> None:
    (tmp_path / "app" / "pkg").mkdir(parents=True)
    (tmp_path / "lib").mkdir()
    (tmp_path / "app" / "pkg" / "__init__.py").write_text("")
    # A compiled module that imports a third-party package at import time (raylib's core/render.py),
    # and a package that prints on import (raylib prints a banner)
    (tmp_path / "app" / "pkg" / "core.py").write_text("import thirdparty\n")
    (tmp_path / "lib" / "thirdparty.py").write_text("print('BANNER')\n")
    code = portable.smoke_code(["pkg.core"])
    r = subprocess.run([sys.executable, "-s", "-c", code], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    # It imports fine; it is reported only because this test module is a .py, not a .pyd
    assert r.stdout.splitlines() == ["BANNER", portable.SMOKE_MARK + "pkg.core"]


# --- 12. the bundled dependencies win over the ones installed in the running Python -------------

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
MAIN = "import thirdparty, sys\nprint('from=' + thirdparty.WHERE, *sys.argv[1:])\n"


def _run_with_shadow(target: Path, shadow: Path, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run `target` like `python target arg` with `shadow` already on sys.path (a system site-packages)."""
    code = f"import runpy,sys;sys.path.append({str(shadow)!r});sys.argv=[{str(target)!r},'arg'];runpy.run_path({str(target)!r},run_name='__main__')"
    r = subprocess.run([sys.executable, "-s", "-c", code], cwd=cwd, capture_output=True, text=True, env=env, timeout=120, check=False)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _shadow(tmp_path: Path) -> Path:
    shadow = tmp_path / "site-packages"
    shadow.mkdir()
    (shadow / "thirdparty.py").write_text("WHERE = 'system'\n")
    return shadow


def test_portable_boot_puts_lib_before_site_packages(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "app").mkdir(parents=True)
    (out / "lib").mkdir()
    shutil.copy2(TEMPLATES / "portable" / "boot.py", out / "boot.py")
    (out / "app" / "main.py").write_text(MAIN)
    (out / "lib" / "thirdparty.py").write_text("WHERE = 'lib'\n")
    assert _run_with_shadow(out / "boot.py", _shadow(tmp_path), tmp_path) == "from=lib arg"


def test_pyz_main_puts_lib_before_site_packages(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "common" / "app").mkdir(parents=True)
    (root / "common" / "lib").mkdir()
    shutil.copy2(TEMPLATES / "pyz" / "__main__.py", root / "__main__.py")
    (root / "common" / "app" / "main.py").write_text(MAIN)
    (root / "common" / "lib" / "thirdparty.py").write_text("WHERE = 'lib'\n")
    info = {"name": "t", "build_id": "b1", "min_python": [3, 11], "targets": [], "pure": True, "backend": "cpython"}
    (root / "_pyz.json").write_text(json.dumps(info))
    zipapp.create_archive(root, tmp_path / "t.pyz")
    cache = tmp_path / "cache"  # never the user's real cache
    env = {**os.environ, "LOCALAPPDATA": str(cache), "XDG_CACHE_HOME": str(cache), "HOME": str(cache)}
    assert _run_with_shadow(tmp_path / "t.pyz", _shadow(tmp_path), tmp_path, env) == "from=lib arg"
    assert any(cache.rglob(".complete"))


# --- 2. tasks resolve {python} lazily -------------------------------------------------------------


def test_tasks_do_not_resolve_the_python_unless_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make(
        {
            "tasks": {
                "plain": {"cmd": ["tool", "{root}", "{backend}"], "uv": False, "env": {"OUT": "{build}"}},
                "needs": {"cmd": ["{python}", "-V"], "uv": False},
                "typo": {"cmd": ["{nope}"], "uv": False},
            },
        }
    )

    def no_python(_cfg: Config, _backend: str) -> envs.PyEnv:
        raise DeployError("no python", 3)

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(envs, "runtime_env", no_python)
    monkeypatch.setattr(tasks.proc, "run", fake_run)
    assert tasks.run_task(cfg, "plain", ["x"], lambda _argv: 0) == 0
    assert calls[-1][0] == "tool" and calls[-1][2:] == ["cpython", "x"]
    with pytest.raises(DeployError, match="no python"):
        tasks.run_task(cfg, "needs", [], lambda _argv: 0)
    with pytest.raises(DeployError, match="unknown placeholder 'nope'"):
        tasks.run_task(cfg, "typo", [], lambda _argv: 0)


def test_task_python_placeholder_is_the_runtime_env() -> None:
    cfg = make({})
    values = tasks.Placeholders(cfg, "cpython")
    assert "{python}".format_map(values) == str(envs.cpython_env(cfg).python)


# --- 3. the pyz .cmd wrapper ----------------------------------------------------------------------


def test_pyz_wrapper_is_ascii_crlf_without_blocks() -> None:
    text = pyz._wrapper_cmd(make({}), "cpython", "myapp.pyz")
    assert text.isascii()
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    assert not any(line.rstrip().endswith("(") for line in text.splitlines())  # no ( ) blocks


@windows_only
def test_pyz_wrapper_runs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "__main__.py").write_text("import sys\nprint('ran', *sys.argv[1:])\n")
    zipapp.create_archive(root, tmp_path / "app.pyz")
    cmd = tmp_path / "app.cmd"
    cmd.write_text(pyz._wrapper_cmd(make({}), "cpython", "app.pyz"), encoding="ascii", newline="")
    r = _run_cmd(cmd)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "ran"


# --- 4. binary files in presets -------------------------------------------------------------------


def test_preset_skeleton_copies_binary_files_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = tmp_path / "p" / "files"
    (files / "src" / "__pkg__").mkdir(parents=True)
    binary = b"\x89PNG\r\n\x1a\n\x00{{name}}\xff\r\n"
    (files / "src" / "__pkg__" / "blob").write_bytes(binary)  # no suffix, not text
    (files / "src" / "__pkg__" / "logo.png").write_bytes(b"{{name}}\r\n")  # not a text suffix
    (files / "src" / "__pkg__" / "latin1.txt").write_bytes(b"caf\xe9 {{name}}\r\n")  # not UTF-8
    (files / "LICENSE").write_bytes(b"Copyright {{name}}\r\n")  # no suffix, text
    (files / "src" / "__pkg__" / "mod.py").write_bytes(b"# {{pkg}}\r\n")
    monkeypatch.setattr(presets, "PRESETS", tmp_path)
    out = presets.skeleton("p", "My-App")
    assert out["src/my_app/blob"] == binary
    assert out["src/my_app/logo.png"] == b"{{name}}\r\n"
    assert out["src/my_app/latin1.txt"] == b"caf\xe9 {{name}}\r\n"
    assert out["LICENSE"] == b"Copyright My-App\n"
    assert out["src/my_app/mod.py"] == b"# my_app\n"

    # pristine(): text files may be checked out with CRLF, binary files are compared as-is
    root = tmp_path / "proj"
    for rel, data in out.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.replace(b"\n", b"\r\n") if rel.endswith(".py") else data)
    monkeypatch.setattr(presets, "ROOT", root)
    cfg: Config = config._build(Config, {"app": {"name": "My-App", "preset": "p"}}, "")
    assert presets.pristine(cfg)
    (root / "src" / "my_app" / "blob").write_bytes(binary.replace(b"\r\n", b"\n"))
    assert not presets.pristine(cfg)


# --- 5. compile.annotate --------------------------------------------------------------------------


def test_compile_annotate_writes_the_report_on_every_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    (src / "myapp" / "core").mkdir(parents=True)
    (src / "myapp" / "__init__.py").write_text("")
    (src / "myapp" / "core" / "__init__.py").write_text("")
    (src / "myapp" / "core" / "m.py").write_text("X = 1\n")
    build = tmp_path / ".build"
    html = build / "reports" / "mypyc-annotate.html"
    monkeypatch.setattr(mypyc, "SRC", src)
    monkeypatch.setattr(config, "SRC", src)
    monkeypatch.setattr(mypyc, "BUILD", build)
    monkeypatch.setattr(mypyc, "ANNOTATE_HTML", html)
    specs: list[dict[str, Any]] = []

    def fake_run(argv: list[Any], **_kw: Any) -> subprocess.CompletedProcess[str]:
        spec = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        specs.append(spec)
        for f in spec["files"]:  # what setuptools would build
            (Path(spec["stage"]) / f).with_suffix(".cp314-win_amd64.pyd").write_bytes(b"")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mypyc.proc, "run", fake_run)
    monkeypatch.setattr(mypyc.proc, "find_uv", lambda: "uv")
    for profile in ("dev", "release"):
        mypyc.build(make({"compile": {"annotate": True}}), profile)
        assert specs[-1]["annotate"] == str(html)
        assert html.parent.is_dir()
    mypyc.build(make({}), "dev")
    assert specs[-1]["annotate"] == ""
    other = tmp_path / "other.html"  # ./deploy report passes its own path: it wins
    mypyc.build(make({"compile": {"annotate": True}}), "dev", annotate=other, compile_c=False)
    assert specs[-1]["annotate"] == str(other)


# --- 6. sync_tree -----------------------------------------------------------------------------------


def test_sync_tree_detects_same_size_edits_within_a_second(tmp_path: Path) -> None:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    f = src / "a.py"
    f.write_bytes(b"aaaa")
    base = 1_700_000_000 * 10**9
    os.utime(f, ns=(base, base + 100_000))  # multiples of 100 ns: NTFS resolution
    assert mypyc.sync_tree(src, dst) == 1
    f.write_bytes(b"bbbb")
    os.utime(f, ns=(base, base + 500_000))  # same size, same second
    assert mypyc.sync_tree(src, dst) == 1
    assert (dst / "a.py").read_bytes() == b"bbbb"
    assert (dst / "a.py").stat().st_mtime_ns == f.stat().st_mtime_ns
    assert mypyc.sync_tree(src, dst) == 0

    # Extensions: never copied from src, never deleted from dst
    (dst / "a.cp314-win_amd64.pyd").write_bytes(b"x")
    (src / "b.so").write_bytes(b"y")
    mypyc.sync_tree(src, dst)
    assert (dst / "a.cp314-win_amd64.pyd").exists() and not (dst / "b.so").exists()
    f.unlink()
    assert mypyc.sync_tree(src, dst) == 1
    assert not (dst / "a.py").exists() and (dst / "a.cp314-win_amd64.pyd").exists()


# --- 7. portable launchers ------------------------------------------------------------------------

TRICKY = {"PCT": "50% %PATH%", "AMP": "a&b|c<d>e^f(x86)", "SQ": "it's"}
POSIX_TRICKY = {**TRICKY, "DQ": 'say "hi" $HOME `id` \\ end'}
BOOT = (
    "import json, os, sys\n"
    "keys = sys.argv[1].split('+')\n"
    "print(json.dumps({'env': {k: os.environ.get(k) for k in keys}}))\n"
)


def _portable_folder(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    (out / "boot.py").write_text(BOOT)
    return out


def test_portable_env_values_are_quoted() -> None:
    cfg = make({"deploy": {"portable": {"env": POSIX_TRICKY}}})
    sh_lines = portable._env_lines(cfg, windows=False)
    assert "export SQ='it'\"'\"'s'" in sh_lines
    cmd_cfg = make({"deploy": {"portable": {"env": TRICKY}}})
    assert 'set "PCT=50%% %%PATH%%"' in portable._env_lines(cmd_cfg, windows=True)
    with pytest.raises(DeployError, match="deploy.portable.env.DQ"):
        portable._env_lines(cfg, windows=True)  # a double quote cannot be written to a .cmd


def test_portable_env_names_are_validated() -> None:
    with pytest.raises(DeployError, match="invalid environment variable name"):
        make({"deploy": {"portable": {"env": {"A B": "1"}}}})
    with pytest.raises(DeployError, match="must be of type string"):
        make({"deploy": {"portable": {"env": {"N": 1}}}})
    with pytest.raises(DeployError, match="must be of type string"):
        make({"tasks": {"t": {"cmd": ["x"], "env": {"N": 1}}}})


def test_system_launchers_probe_the_minimum_version(tmp_path: Path) -> None:
    cfg = make({"deploy": {"portable": {"runtime": "system"}}})
    cmd = portable.cmd_launcher(cfg, "cpython", tmp_path, None)
    assert "where " not in cmd
    assert 'py -3.14 -c "import sys; sys.exit(sys.version_info[:2] < (3, 14))" >nul 2>nul && goto run0' in cmd
    assert cmd.isascii() and "\n" not in cmd.replace("\r\n", "")
    sh = portable.sh_launcher(cfg, "pypy", tmp_path, None)
    assert "for py in pypy3 pypy; do" in sh
    assert 'HERE=$(cd "$(dirname "${BASH_SOURCE:-$0}")" && pwd)' in sh  # niubash keeps the caller's $0


@windows_only
def test_portable_system_cmd_launcher_runs(tmp_path: Path) -> None:
    # runtime = "system": the same env lines as a bundled launcher, plus the version probe.
    # (A bundled runtime needs a full interpreter copy: the real portable build covers it.)
    out = _portable_folder(tmp_path)
    cfg = make({"deploy": {"portable": {"runtime": "system", "env": TRICKY}}})
    path = out / "app.cmd"
    path.write_text(portable.cmd_launcher(cfg, "cpython", out, None), encoding="ascii", newline="")
    r = subprocess.run(
        ["cmd.exe", "/d", "/c", str(path), "+".join(TRICKY)], capture_output=True, text=True, timeout=120, check=False
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {"env": TRICKY}


@windows_only
def test_portable_system_cmd_launcher_fails_cleanly(tmp_path: Path) -> None:
    out = _portable_folder(tmp_path)
    cfg = make({"python": {"cpython": "3.99"}, "deploy": {"portable": {"runtime": "system"}}})
    path = out / "app.cmd"
    path.write_text(portable.cmd_launcher(cfg, "cpython", out, None), encoding="ascii", newline="")
    r = _run_cmd(path)
    assert r.returncode == 9009
    assert "needs Python 3.99 or newer" in r.stderr


@pytest.mark.skipif(_posix_sh() is None, reason="no POSIX sh")
@pytest.mark.parametrize("cpython", ["3.11", "3.99"])
def test_portable_system_sh_launcher_runs(tmp_path: Path, cpython: str) -> None:
    out = _portable_folder(tmp_path)
    cfg = make({"python": {"cpython": cpython}, "deploy": {"portable": {"runtime": "system", "env": POSIX_TRICKY}}})
    path = out / "app.sh"
    path.write_text(portable.sh_launcher(cfg, "cpython", out, None), encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    sh = _posix_sh()
    assert sh is not None
    r = subprocess.run([sh, path.as_posix(), "+".join(POSIX_TRICKY)], capture_output=True, text=True, env=env, timeout=120, check=False)
    if cpython == "3.99":
        assert r.returncode == 127
        assert "needs Python 3.99 or newer" in r.stderr
    else:
        assert r.returncode == 0, r.stdout + r.stderr
        assert json.loads(r.stdout.strip().splitlines()[-1]) == {"env": POSIX_TRICKY}


# --- 8. commands reject unknown arguments ---------------------------------------------------------


def test_commands_reject_unknown_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make({})

    def must_not_run(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("the command ran")

    # Nothing may be touched if the check is broken
    monkeypatch.setattr(cmd_env, "BUILD", tmp_path / "b")
    monkeypatch.setattr(cmd_env, "DIST", tmp_path / "d")
    monkeypatch.setattr(cmd_env, "ROOT", tmp_path)
    monkeypatch.setattr(cmd_env.ui, "step", must_not_run)
    monkeypatch.setattr(cmd_env, "_envs_for", must_not_run)
    monkeypatch.setattr(cmd_dev.envs, "uv_run", must_not_run)
    cases = [
        (cmd_dev.cmd_lint, ["--fix", "--unsafe"], "--unsafe"),
        (cmd_dev.cmd_fmt, ["src"], "src"),
        (cmd_env.cmd_clean, ["--env"], "--env"),
        (cmd_env.cmd_setup, ["cpython"], "cpython"),
        (cmd_env.cmd_doctor, ["-v"], "-v"),
        (cmd_env.cmd_sync, ["pypy", "extra"], "extra"),
    ]
    for func, args, bad in cases:
        with pytest.raises(DeployError, match=f"unrecognized arguments: {re.escape(bad)}") as e:
            func(cfg, args)
        assert e.value.code == 2


def test_lint_and_fmt_pass_their_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_uv_run(_env: Any, argv: list[Any], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cmd_dev.envs, "uv_run", fake_uv_run)
    cfg = make({})
    assert cmd_dev.cmd_lint(cfg, ["--fix"]) == 0
    assert calls[-1][:3] == ["ruff", "check", "--fix"]
    assert cmd_dev.cmd_fmt(cfg, []) == 0
    assert calls[-1][:3] == ["ruff", "format", "src"]


# --- 9. app.preset is validated -------------------------------------------------------------------


def test_app_preset_must_exist() -> None:
    assert make({"app": {"preset": "raylib"}}).app.preset == "raylib"
    with pytest.raises(DeployError, match="is not a preset of this template"):
        make({"app": {"preset": "nope"}})
    with pytest.raises(DeployError, match="is not a preset of this template"):
        make({"app": {"preset": "../presets/script"}})


# --- 10/11. dead code, pinned tools ---------------------------------------------------------------


def test_tools_are_pinned() -> None:
    assert re.fullmatch(r"basedpyright==\d+\.\d+\.\d+", cmd_dev.BASEDPYRIGHT)
    assert re.fullmatch(r"nuitka==\d+\.\d+\.\d+", nuitka.NUITKA)


def test_wheel_setup_py_follows_compile_options() -> None:
    cfg = make({"compile": {"separate": True, "multi_file": True, "strict_dunder_typing": True}})
    text = wheel.setup_py(cfg)
    compile(text, "setup.py", "exec")
    assert "separate=True" in text and "multi_file=True" in text and "strict_dunder_typing=True" in text
    assert "group_name=None" in text
    assert "group_name='myapp'" in wheel.setup_py(make({}))


def test_flet_build_pyproject_takes_only_tool_flet() -> None:
    text = (
        '[project]\nname = "app"\nversion = "0.1.0"\n\n[tool.uv]\nx = 1\n\n'
        "# >>> pytemplate-preset\n[tool.flet]\norg = \"com.example\"\n\n"
        '[tool.flet.app]\npath = "src"\nmodule = "main"\n\n[tool.other]\nleak = true\n# <<< pytemplate-preset\n'
    )
    data = tomllib.loads(text)
    out = tomllib.loads(flet.build_pyproject(make({}), data, ["rich==15.0.0", "cffi==2.0; implementation_name == 'cpython'"]))
    assert out["project"]["dependencies"] == ["rich==15.0.0", "cffi==2.0; implementation_name == 'cpython'"]
    assert out["project"]["requires-python"] == ">=3.14"
    assert out["tool"] == {"flet": data["tool"]["flet"]}  # no [tool.other], no [tool.uv]
    # [tool.flet.app] alone (no bare [tool.flet] header) used to be replaced by the default
    only_app = tomllib.loads('[project]\nname = "a"\nversion = "1"\n\n[tool.flet.app]\npath = "app"\n')
    assert tomllib.loads(flet.build_pyproject(make({}), only_app, []))["tool"] == {"flet": {"app": {"path": "app"}}}
    no_flet = tomllib.loads('[project]\nname = "a"\nversion = "1"\n')
    assert tomllib.loads(flet.build_pyproject(make({}), no_flet, []))["tool"] == {"flet": {"app": {"path": "src"}}}
