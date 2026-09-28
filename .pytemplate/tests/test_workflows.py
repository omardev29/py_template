"""The template repository's own workflows (.github/workflows/template-*.yml): what its CI
promises (every OS, the floors, projects made with ./pyt new, pinned Neovim) and what keeps
the scheduled ones running. Template repository only: `./pyt new` copies neither these
workflows nor the marker. Text checks (the runner is stdlib only, no YAML parser); actionlint,
when installed, validates the files themselves."""

from __future__ import annotations

import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_dev, cmd_mode, cmd_nvim, config, envs, mutation, nvimtest, presets, proc, upx  # noqa: E402
from runner.cmd_build import BuildRequest  # noqa: E402
from runner.project import DIST, ROOT  # noqa: E402
from runner.ui import PytError  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
IMAGE = WORKFLOWS / "template-ci-image"  # the Linux CI image (template-ci-image.yml)
FLET = WORKFLOWS / "template-flet"  # the steps of template-flet.yml (check.py) and its browser pins
pytestmark = pytest.mark.skipif(
    not (ROOT / ".pytemplate" / "template-repo").is_file(), reason="about the template repository's own workflows"
)
# the template workflows this file checks in depth (template-e2e.yml's depths and triggers:
# test_e2e_plan.py); every one of them skips projects made from the template
GATED = (
    "template-ci-image.yml",
    "template-e2e.yml",
    "template-flet.yml",
    "template-keepalive.yml",
    "template-launchers.yml",
    "template-nvim.yml",
    "template-selftest.yml",
)


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _top_block(text: str, key: str) -> list[str]:
    """The lines of a top-level block (`on:`, `jobs:`) up to the next top-level key."""
    lines = text.split("\n")
    start = lines.index(f"{key}:")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith((" ", "#")):
            break
        block.append(line)
    return block


def triggers(text: str) -> set[str]:
    return {m.group(1) for line in _top_block(text, "on") if (m := re.fullmatch(r"  ([a-z_]+):.*", line))}


def jobs(text: str) -> dict[str, str]:
    """{job id: its text} of the `jobs:` block."""
    out: dict[str, list[str]] = {}
    current = ""
    for line in _top_block(text, "jobs"):
        m = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
        if m:
            current = m.group(1)
            out[current] = []
        elif current:
            out[current].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def test_the_helpers_read_a_workflow() -> None:
    text = "name: x\n\non:\n  push:\n    paths: [a]\n  schedule:\n    - cron: '1 2 * * 3'\n\njobs:\n  a:\n    runs-on: x\n  b:\n    needs: a\n"
    assert triggers(text) == {"push", "schedule"}
    assert jobs(text) == {"a": "    runs-on: x", "b": "    needs: a\n"}


def test_template_workflows_pass_actionlint() -> None:
    actionlint = shutil.which("actionlint")
    if actionlint is None:
        pytest.skip("actionlint is not installed")
    files = sorted(str(p) for p in WORKFLOWS.glob("*.yml"))
    r = subprocess.run([actionlint, "-no-color", *files], cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_scheduled_workflow_is_kept_alive() -> None:
    """GitHub disables a scheduled workflow (all its triggers) after 60 days without activity in a
    public repository: template-keepalive.yml re-enables every template-*.yml its grep finds."""
    names = sorted(p.name for p in WORKFLOWS.glob("template-*.yml"))
    scheduled = {n for n in names if "schedule" in triggers(_text(n))}
    assert {n for n in names if re.search(r"(?m)^  schedule:", _text(n))} == scheduled  # what the keepalive's grep sees
    assert set(GATED) <= scheduled
    keepalive = _text("template-keepalive.yml")
    assert triggers(keepalive) == {"schedule", "workflow_dispatch"}
    job = jobs(keepalive)["keepalive"]
    assert re.search(r"(?m)^      actions: write$", job) and "GH_TOKEN: ${{ github.token }}" in job
    assert "for file in .github/workflows/template-*.yml" in job and "grep -q '^  schedule:' \"$file\"" in job
    assert 'gh api --method PUT "repos/$GITHUB_REPOSITORY/actions/workflows/$name/enable"' in job
    assert "disabled_manually" in job  # a workflow disabled by hand stays disabled


@pytest.mark.parametrize("name", GATED)
def test_template_workflows_skip_projects_made_from_the_template(name: str) -> None:
    found = jobs(_text(name))
    assert ".pytemplate/template-repo" in found.pop("gate")
    for job, body in found.items():
        assert re.search(r"(?m)^    needs: (gate|\[gate(, [\w-]+)*\])$", body), f"{name}: job {job} does not wait for the gate"
        assert re.search(r"(?m)^    if: needs\.gate\.outputs\.template == 'true'", body), f"{name}: job {job} is not gated"


def test_selftest_workflow_covers_every_os_and_both_floors() -> None:
    """macOS and Windows here, the oldest uv on a bare runner; Linux (the suite, the runner's
    tests on its floor Python and the suite in new projects) in the CI image (stage 2)."""
    text = _text("template-selftest.yml")
    assert triggers(text) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    found = jobs(text)
    main = found["selftest"]
    assert "os: [macos-latest, windows-latest]" in main
    assert 0 < main.index("./pyt render --check") < main.index("./pyt setup") < main.index("run: ./pyt selftest -rs")
    assert "./pyt.ps1 selftest -rs" in main and "MSYS2_ROOT" in main  # Windows: MSYS2 found by the launcher tests
    assert "rhysd/action-setup-vim@v1" in main and "xonsh==" in main
    uv_floor = found["uv-floor"]
    assert "resolution-strategy: lowest" in uv_floor and "MIN_UV" in uv_floor and "./pyt selftest" in uv_floor
    assert envs.MIN_UV in (ROOT / "pyproject.toml").read_text(encoding="utf-8")  # what setup-uv resolves "lowest" from
    image = jobs(_text("template-ci-image.yml"))
    linux = image["selftest"]
    assert 0 < linux.index("./pyt render --check") < linux.index("./pyt setup") < linux.index("run: ./pyt selftest -rs")
    floor = image["python-floor"]
    assert "--python 3.11 --with \"$pytest\" --with \"$hypothesis\" python -m pytest" in floor and ".pytemplate/tests" in floor
    # the suite's own pytest settings, as ./pyt selftest passes them (cli.cmd_selftest): the
    # project's pyproject.toml gives its app's tests another pythonpath and addopts
    assert "-c .pytemplate/tests/pytest.ini --rootdir=." in floor
    assert "grep '^hypothesis==' " in floor  # the locked Hypothesis: the property tests need it
    assert "uv run --quiet --python 3.11 --script .pytemplate/pyt.py help" in floor
    new = image["new-project"]
    assert "preset: [raylib, flet]" in new and "./pyt new" in new and "./pyt selftest" in new


def test_linux_jobs_run_in_the_ci_image() -> None:
    """Stage 2 of the CI image (CLAUDE.md 13.2): outside template-ci-image.yml a Linux job is only
    a gate, uv-floor (the oldest uv), the nvim canary (the newest of everything), an e2e row or
    one of template-flet.yml's builds (the runner image's Android SDK and JDK, Flet's own
    Flutter); the other Linux jobs run in the image, which holds their tools at pinned versions."""
    allowed = {("template-selftest.yml", "uv-floor"), ("template-nvim.yml", "canary")}
    allowed |= {("template-flet.yml", "web"), ("template-flet.yml", "android")}
    linux = re.compile(r"(?m)^\s+(?:runs-on: ubuntu|os: \[[^\]]*ubuntu|- \{os: ubuntu)")
    for name in ("template-selftest.yml", "template-launchers.yml", "template-nvim.yml", "template-flet.yml"):
        for job, body in jobs(_text(name)).items():
            if job != "gate" and linux.search(body):
                assert (name, job) in allowed, f"{name}: the Linux job {job} belongs in the CI image"
    image = jobs(_text("template-ci-image.yml"))
    for job in ("selftest", "python-floor", "new-project", "launchers", "nvim"):
        assert "image: ${{ needs.image.outputs.ref }}" in image[job], job
    assert "shellcheck -s sh pyt" in image["launchers"]


def test_readme_tells_when_the_selftest_workflow_runs() -> None:
    """Pushes to main only (a branch pushed without a pull request runs nothing), pull
    requests, weekly and by hand: the README must not promise every push."""
    block = "\n".join(_top_block(_text("template-selftest.yml"), "on"))
    assert re.search(r"(?m)^  push:\n    branches: \[main\]$", block), block
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    entry = readme[readme.index("- `template-selftest.yml` runs") :]
    entry = entry[: entry.index("- `template-launchers.yml`")]
    assert "on pushes to `main`, pull requests, weekly and by hand" in entry, entry
    assert "every push" not in entry, entry


def test_what_the_selftest_workflow_reads_by_text_exists() -> None:
    """The workflow greps envs.MIN_UV out of the code and deselects one test by name: a rename
    must fail here, not turn a CI step into an empty check or a deselect that matches nothing."""
    text = _text("template-selftest.yml")
    tests, runner = ROOT / ".pytemplate" / "tests", ROOT / ".pytemplate" / "runner"
    assert "sed -n 's/^MIN_UV = \"\\([0-9.]*\\)\".*/\\1/p' .pytemplate/runner/envs.py" in text
    assert re.findall(r'(?m)^MIN_UV = "([0-9.]*)"', (runner / "envs.py").read_text(encoding="utf-8")) == [envs.MIN_UV]
    deselected = re.findall(r"--deselect \.pytemplate/tests/(test_\w+\.py)::(\w+)", text)
    assert deselected, "the uv-floor job deselects its one test by node id"
    for file, name in deselected:
        assert f"\ndef {name}(" in (tests / file).read_text(encoding="utf-8"), (file, name)


def test_nvim_workflow_pins_neovim_and_runs_a_canary() -> None:
    text = _text("template-nvim.yml")
    found = jobs(text)
    pinned, canary = found["nvim"], found["canary"]
    minimum = "v" + cmd_nvim.version_str(cmd_nvim.MIN_LAZYVIM)
    rows = re.findall(r"- \{os: ([\w-]+), nvim: (v[\d.]+)\}", pinned)
    assert rows == [("windows-latest", "v0.12.5")]  # Linux: both versions, in the CI image
    assert f"nvim: [v0.12.5, {minimum}]" in jobs(_text("template-ci-image.yml"))["nvim"]
    assert "version: ${{ matrix.nvim }}" in pinned and "github.event_name != 'schedule'" in pinned
    assert "name: nvim-logs-${{ matrix.os }}-${{ matrix.nvim }}" in pinned  # one artifact per row
    assert "version: stable" in canary and "github.event_name == 'schedule'" in canary
    assert f"rm {nvimtest.LOCK.relative_to(ROOT).as_posix()}" in canary
    upload = canary[canary.index("actions/upload-artifact") - 200 :]
    assert "if: always()" in upload  # the new pins travel even when the canary is red


def test_logs_of_a_job_that_timed_out_are_uploaded() -> None:
    """A job over its timeout-minutes is concluded "cancelled", not "failed": a step that uploads
    logs on `failure()` alone skipped exactly the logs of the runs that hung. template-e2e.yml
    had it fixed; the nvim jobs of template-nvim.yml and template-ci-image.yml had not."""
    found = []
    for path in sorted(WORKFLOWS.glob("template-*.yml")):
        for condition in re.findall(r"(?m)^\s+if: (.+)$", path.read_text(encoding="utf-8")):
            if "failure()" in condition:
                found.append(path.name)
                assert "cancelled()" in condition, (path.name, condition)
    assert {"template-nvim.yml", "template-ci-image.yml", "template-e2e.yml"} <= set(found)


def test_runner_labels_are_latest() -> None:
    """GitHub retires pinned image labels (a pinned one fails for certain within a few years);
    -latest only moves. Expressions (${{ matrix.os }}) are checked through the matrix values."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        labels = re.findall(r"runs-on: ([^\s$][^\s]*)", text)
        labels += [x.strip() for m in re.findall(r"\bos: \[([^\]]*)\]", text) for x in m.split(",")]
        labels += re.findall(r"\{os: ([\w-]+)", text)
        labels += re.findall(r"- os: ([\w-]+)", text)
        assert labels and all(label.endswith("-latest") for label in labels), (path.name, labels)


def test_actions_are_pinned() -> None:
    """A major tag, or an exact release where the action publishes no floating major tag
    (astral-sh/setup-uv since v8); an action of a subfolder (actions/cache/restore) likewise."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        uses = re.findall(r"uses: ([^\s]+)", path.read_text(encoding="utf-8"))
        assert uses and all(re.fullmatch(r"[\w-]+/[\w-]+(/[\w-]+)*@v\d+(\.\d+\.\d+)?", u) for u in uses), (path.name, uses)
        assert all(re.fullmatch(r"astral-sh/setup-uv@v\d+\.\d+\.\d+", u) for u in uses if "setup-uv" in u), path.name


def _step(text: str, name: str) -> str:
    """The text of the step whose name starts with `name`, up to the next step."""
    start = text.index(f"      - name: {name}")
    end = text.find("\n      - ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def test_every_downloaded_file_is_checked_against_a_pinned_sha256() -> None:
    """A tool a template workflow downloads is one release, checked against its SHA-256 before it
    runs (busybox-w32 was the rolling busybox64u.exe, unchecked: whatever frippery.org served that
    day ran on every push and pull request). Not a file: uv's installer in the optional WSL job,
    which installs the newest uv on purpose, as setup-uv does everywhere else."""
    checked = []
    for path in sorted(WORKFLOWS.glob("template-*.yml")):
        for job, body in jobs(path.read_text(encoding="utf-8")).items():
            for step in body.split("\n      - "):
                if re.search(r"-OutFile\b|curl [^\n|]*-o ", step):
                    # the pinned hash in the step, or in the job's env the step checks against
                    assert re.search(r"\b[0-9a-f]{64}\b", step) or re.search(r"(?m)^      [A-Z0-9_]*SHA256: [0-9a-f]{64}$", body), (path.name, job, step)
                    assert "sha256sum -c" in step or "Get-FileHash" in step, (path.name, job, step)
                    checked.append(path.name)
    assert "template-launchers.yml" in checked  # busybox-w32 (the CI image checks its own downloads)
    windows = jobs(_text("template-launchers.yml"))["windows"]
    assert re.search(r"(?m)^      BUSYBOX: busybox-w64-FRP-\d+-g[0-9a-f]+\.exe$", windows), windows
    step = _step(_text("template-launchers.yml"), "busybox-w32")
    assert '"https://frippery.org/files/busybox/$env:BUSYBOX"' in step  # never a rolling busybox64u.exe
    # frippery.org is often unreachable from the runners: the pinned file is cached, keyed by its
    # name and hash, saved right after it passed the check, and checked again when restored
    key = "key: busybox-w32-${{ env.BUSYBOX }}-${{ env.BUSYBOX_SHA256 }}"
    assert windows.index("uses: actions/cache/restore@v6") < windows.index(key) < windows.index("name: busybox-w32") < windows.index("uses: actions/cache/save@v6")
    assert "if: steps.busybox.outputs.cache-hit != 'true'" in windows


def test_the_busybox_step_refuses_a_file_that_is_not_the_pinned_one(tmp_path: Path) -> None:
    """The step's own PowerShell against a local server: the pinned hash passes and puts the
    folder on PATH, another file fails the step and is removed."""
    import hashlib
    import http.server
    import threading

    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    step = _step(_text("template-launchers.yml"), "busybox-w32")
    body = step.split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in body.splitlines())
    name = re.search(r"BUSYBOX: (\S+)", jobs(_text("template-launchers.yml"))["windows"])
    assert name and "https://frippery.org/files/busybox/$env:BUSYBOX" in script
    served = tmp_path / "served"
    served.mkdir()
    (served / name[1]).write_bytes(b"not really busybox\n")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: Quiet(*a, directory=str(served)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        ps1 = tmp_path / "step.ps1"
        ps1.write_text("$ErrorActionPreference = 'stop'\n" + script.replace("https://frippery.org/files/busybox/", url), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        good = hashlib.sha256(b"not really busybox\n").hexdigest()
        for case, digest, cached, ok in (
            ("downloaded", good, None, True),
            ("other file", "0" * 64, None, False),
            ("cached", good, b"not really busybox\n", True),  # the cache's copy: no download
            ("damaged cache", good, b"damaged\n", False),
        ):
            temp = tmp_path / case
            (temp / "busybox").mkdir(parents=True)
            if cached is not None:
                (temp / "busybox" / "busybox.exe").write_bytes(cached)
                (served / name[1]).unlink(missing_ok=True)  # the server has nothing: only the cache can pass
            gh_path = tmp_path / f"path-{case}"
            run_env = {**env, "BUSYBOX": name[1], "BUSYBOX_SHA256": digest, "RUNNER_TEMP": str(temp), "GITHUB_PATH": str(gh_path)}
            r = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(ps1)], env=run_env, capture_output=True, text=True, timeout=120, check=False)
            assert (r.returncode == 0) is ok, (case, r.stdout + r.stderr)
            assert (temp / "busybox" / "busybox.exe").is_file() is ok, case
            if ok:
                # the step's Windows path; pwsh on Linux and macOS reads its `\` as `/` too
                assert gh_path.read_text(encoding="utf-8").strip().replace("\\", "/") == str(temp / "busybox").replace("\\", "/"), case
            else:
                assert "not the pinned" in r.stdout + r.stderr and not gh_path.exists(), case
    finally:
        server.shutdown()
        server.server_close()


# --- the Linux CI image (template-ci-image.yml and its folder) ---------------------------------


def _load(name: str, path: Path) -> ModuleType:
    """A script of a workflow's folder as a fresh module (no bytecode left in the folder)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    before, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    return module


def _passes_mypy_strict(script: Path, tmp_path: Path) -> None:
    """A workflow's script is product code (CLAUDE.md 13.4) that the selftest's mypy run does not
    cover: the runner's own mypy config, never the project's .mypy.ini (the default typing profile
    sets ignore_errors there), and a scratch copy with a type error must fail."""
    if importlib.util.find_spec("mypy") is None:
        pytest.skip("mypy is not installed in this interpreter")
    config_file = ROOT / ".pytemplate" / "tests" / "mypy-runner.ini"
    broken = tmp_path / script.name
    broken.write_text(script.read_text(encoding="utf-8") + '\nBAD: int = "x"\n', encoding="utf-8")
    for path, code in ((script, 0), (broken, 1)):
        argv = [sys.executable, "-m", "mypy", "--config-file", str(config_file), "--no-incremental", "--python-version", "3.11", str(path)]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False, cwd=ROOT)
        assert r.returncode == code, (path, r.stdout + r.stderr)


def _pins(root: Path | None = None) -> ModuleType:
    """template-ci-image/pins.py as a module; `root` points it at a copy of the project."""
    module = _load("ci_image_pins", IMAGE / "pins.py")
    if root is not None:
        module.ROOT, module.HERE = root, root / IMAGE.relative_to(ROOT)
    return module


def test_ci_image_pins_come_from_the_code() -> None:
    """The pins the project owns are read from their files, never copied: each read value is the
    runner's own; the image's own pins have the shape the scripts rely on."""
    pins = _pins()
    code = pins.from_code()
    assert code["CPYTHON"] == config.load(set()).python.cpython
    assert code["PYTHON_FLOOR"] == "3.11"
    assert code["NVIM_MIN"] == "v" + cmd_nvim.version_str(cmd_nvim.MIN_LAZYVIM)
    assert f'"{code["TAPLO"]}"' in (ROOT / ".pytemplate" / "tests" / "test_render_core.py").read_text(encoding="utf-8")
    assert code["BASEDPYRIGHT"] == cmd_dev.BASEDPYRIGHT and code["BASEDPYRIGHT_NODE"] == cmd_dev.BASEDPYRIGHT_NODE
    assert code["UPX"] == upx.VERSION
    values = pins.values()
    assert envs.uv_version(f"uv {values['UV']}") >= envs.uv_version(f"uv {envs.MIN_UV}")  # type: ignore[operator]
    assert re.fullmatch(r"ubuntu:\d+\.\d+@sha256:[0-9a-f]{64}", pins.BASE)
    assert re.fullmatch(r"\d{8}T\d{6}Z", values["APT_SNAPSHOT"])
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for k, v in values.items() if k.endswith("_SHA256"))
    assert re.fullmatch(r"xonsh==[\d.]+", values["XONSH"]) and values["CI_UID"].isdigit()


def test_ci_image_reads_only_its_pins() -> None:
    """Every $NAME the image's scripts read comes from pins.env (or is their own)."""
    keys = set(_pins().values())
    own = {"ca", "tmp", "work", "pytest", "hypothesis", "preset", "serial", "here", "root", "python", "ref", "source", "ctx", "image", "base"}
    for name in ("system", "warm"):
        text = (IMAGE / name).read_text(encoding="utf-8")
        used = set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", text)) - {"HOME", "PATH", "PYTHON"}
        assert used <= keys | own, (name, used - keys - own)


def test_ci_image_folder_is_what_the_tag_hashes() -> None:
    pins = _pins()
    here = {p.name for p in IMAGE.iterdir()}
    assert here == {*pins.IMAGE_FILES, ".gitattributes"}, here  # pins.env and inputs.txt live only in the build context
    assert (IMAGE / ".gitattributes").read_text(encoding="utf-8") == "* text eol=lf\n"
    lines = pins.canonical().splitlines()
    for name in pins.IMAGE_FILES:
        assert any(line.endswith(f"  .github/workflows/template-ci-image/{name}") for line in lines), name
    for rel in pins.data_files():
        assert (ROOT / rel).is_file() and any(line.endswith(f"  {rel}") for line in lines), rel
    assert {"uv.lock", "pyproject.toml", ".python-version"} <= set(pins.data_files())
    assert {".pytemplate/tools/mutation_cr.py", ".pytemplate/tools/mutation_cr.py.lock"} <= set(pins.data_files())  # Cosmic Ray
    assert all(any(rel.startswith(f".pytemplate/presets/{p}/") for rel in pins.data_files()) for p in ("script", "raylib", "flet"))


def test_ci_image_tag_follows_its_inputs(tmp_path: Path) -> None:
    """A changed lock or image file gives another tag; a CRLF or BOM checkout of the same bytes
    (Windows, core.autocrlf) gives the same one."""
    pins = _pins()
    files = [f".github/workflows/template-ci-image/{n}" for n in pins.IMAGE_FILES] + pins.data_files()
    files += [".pytemplate/runner/cmd_nvim.py", ".pytemplate/pyt.py", ".pytemplate/tests/test_render_core.py"]
    files += [".pytemplate/runner/cmd_dev.py", ".pytemplate/runner/upx.py"]
    for rel in files:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    copy = _pins(tmp_path)
    ref = copy.ref("Owner/Repo")
    assert re.fullmatch(r"ghcr\.io/owner/repo-ci:[0-9a-f]{16}", ref) and ref == pins.ref("owner/repo")
    lock = tmp_path / "uv.lock"
    lf = (ROOT / "uv.lock").read_bytes().replace(b"\r\n", b"\n")  # this checkout may be CRLF already
    lock.write_bytes(b"\xef\xbb\xbf" + lf.replace(b"\n", b"\r\n"))
    assert b"\r\r\n" not in lock.read_bytes()
    assert copy.ref("owner/repo") == ref
    lock.write_bytes(lock.read_bytes() + b"# another lock\r\n")
    assert copy.ref("owner/repo") != ref
    lock.write_bytes((ROOT / "uv.lock").read_bytes())
    warm = tmp_path / ".github/workflows/template-ci-image/warm"
    warm.write_bytes(warm.read_bytes() + b"# edited\n")
    assert copy.ref("owner/repo") != ref
    with pytest.raises(SystemExit, match="not OWNER/REPO"):
        pins.ref("owner/repo; rm -rf /")


def test_ci_image_workflow_builds_publishes_and_runs_the_linux_jobs() -> None:
    text = _text("template-ci-image.yml")
    assert triggers(text) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    assert re.search(r"(?m)^permissions:\n  contents: read$", text)
    found = jobs(text)
    image = found.pop("image")
    found.pop("gate")
    assert "packages: write" in image and "persist-credentials: false" in image
    assert "pins.py ref \"$GITHUB_REPOSITORY\"" in image and "docker manifest inspect" in image
    # built when missing, and weekly from scratch; pushed only when missing, and only from here
    assert "if: steps.tag.outputs.published != 'true' || github.event_name == 'schedule'" in image
    assert "github.event_name == 'schedule' && '--no-cache --pull'" in image
    assert "steps.tag.outputs.published != 'true' && steps.tag.outputs.can_push == 'true'" in image
    assert 'if [ -z "$HEAD_REPO" ] || [ "$HEAD_REPO" = "$GITHUB_REPOSITORY" ]' in image
    assert "cat /opt/ci/inputs.txt | diff -" in image  # the image holds what its tag names
    pins = _pins()
    assert set(found) == {"selftest", "python-floor", "new-project", "launchers", "nvim", "mutation"}
    for name, body in found.items():
        assert "needs.image.outputs.usable == 'true'" in body and "packages: read" in body, name
        assert "image: ${{ needs.image.outputs.ref }}" in body and "password: ${{ github.token }}" in body, name
        assert f"options: --init --user {pins.CI_UID}" in body and "HOME: /home/runner" in body, name
        assert "shell: bash" in body and "setup-uv" not in body and "apt-get" not in body, name
        assert "${{ runner.temp }}" not in body, name  # the host's path inside a container job
    assert f"nvim: [{pins.NVIM}, {pins.from_code()['NVIM_MIN']}]" in found["nvim"]


def test_mutation_job_measures_the_lines_a_pull_request_changes() -> None:
    """template-ci-image.yml's mutation job: pull requests only, against the base their merge
    commit was made on (HEAD^1: the base branch may have moved since), within a budget that
    leaves the report of what ran, and red when the suite cannot judge, out of time too (the
    step's script runs in the next test); the JSON report is always uploaded."""
    body = jobs(_text("template-ci-image.yml"))["mutation"]
    assert "github.event_name == 'pull_request'" in body and not re.search(r"^\s+ref:", body, re.M)  # checkout: the merge commit
    assert "fetch-depth: 2" in body and "persist-credentials: false" in body
    assert "--mutation --diff HEAD^1 " in body and "--json > mutation.json" in body and "base_ref" not in body
    budget = re.search(r"timeout -k (\d+)m -s TERM (\d+)m \./pyt selftest --mutation", body)
    job = re.search(r"timeout-minutes: (\d+)", body)
    assert budget and job and int(budget[1]) + int(budget[2]) + 10 <= int(job[1])  # the report is written before the job's end
    assert '["failed"]' in body and "::warning::" in body and "::error::" in body  # out of time: the report decides
    report = body[body.index("name: Mutation report") :]
    assert "if: always()" in report and "path: mutation.json" in report


def test_the_mutation_step_follows_the_report_when_it_runs_out_of_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mutation job's own script, `timeout` faked, with the report the suite prints: a suite
    that ends by itself gives its exit code; one that runs out of time (timeout's 124) is a
    warning when its report judged what ran, and red when the report holds a failure (its
    `failed` key: a failed baseline, a mutant that could not be judged, an error) or cannot be
    read. The first run on GitHub ran out of time with three failed baselines, and the job was
    green with a warning."""
    bash = shutil.which("bash")
    uv = os.environ.get("UV") or shutil.which("uv")
    if sys.platform == "win32" or bash is None or not uv:
        pytest.skip("the step runs in the Linux image, with bash and uv")
    body = _step(_text("template-ci-image.yml"), "selftest --mutation of the runner lines").split("        run: |\n", 1)[1]
    script = tmp_path / "step.sh"
    script.write_bytes(("\n".join(line[10:] for line in body.splitlines()) + "\n").encode())
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "timeout").write_bytes(b'#!/bin/sh\ncat "$PT_REPORT"\nexit "$PT_CODE"\n')
    (fake / "timeout").chmod(0o755)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shutil.copy2(ROOT / ".python-version", checkout / ".python-version")  # the Python uv takes there
    # none of these is set in the job's container; offline: the Python uv takes is already there
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "UV_PYTHON_PREFERENCE")}
    env.update(PATH=os.pathsep.join([str(fake), str(Path(uv).parent), env.get("PATH", "")]), UV_PYTHON_DOWNLOADS="never", UV_OFFLINE="1")
    monkeypatch.setattr(proc, "find_uv", lambda: uv)

    def printed(status: str = mutation.KILLED, baseline: str = mutation.PASS, error: PytError | None = None, *, interrupted: bool) -> bytes:
        """What the suite prints on stdout (the step's mutation.json) for a report of one mutant."""
        mutant = mutation.Mutant(".pytemplate/runner/m.py", "core/AddNot", 0, 10, 4, 10, 9, "f", status, 1.0)
        report = mutation.Report([mutant], {"runner.m": mutation.Baseline("runner.m", ["t.py"], baseline, 2.0)}, tmp_path, interrupted=interrupted, error=error)
        monkeypatch.setattr(mutation, "run", lambda *a: report)
        try:
            mutation.selftest(SimpleNamespace(), ["--json"])  # type: ignore[arg-type]  # the faked run reads no configuration
        except PytError:
            pass  # an error that stopped the run comes after its report
        return capsys.readouterr().out.encode()

    stopped = PytError("selftest --mutation: Cosmic Ray's side stopped", 3)
    for case, stdout, code, expected, annotation in (
        ("ended by itself", printed(interrupted=False), 0, 0, ""),
        ("ended by itself, a failed baseline", printed(baseline=mutation.FAIL, interrupted=False), 1, 1, ""),
        ("out of time", printed(interrupted=True), 124, 0, "::warning::"),
        ("out of time, a failed baseline", printed(baseline=mutation.FAIL, interrupted=True), 124, 1, "::error::"),
        ("out of time, a mutant not judged", printed(mutation.ERROR, interrupted=True), 124, 1, "::error::"),
        ("out of time, an error that stopped the run", printed(error=stopped, interrupted=True), 124, 1, "::error::"),
        ("out of time, no report", b"", 124, 1, "::error::"),
        ("killed after its grace time", b"", 137, 137, ""),
    ):
        (tmp_path / "report").write_bytes(stdout)
        (checkout / "mutation.json").unlink(missing_ok=True)
        run_env = {**env, "PT_REPORT": str(tmp_path / "report"), "PT_CODE": str(code)}
        r = subprocess.run(  # as GitHub runs a `shell: bash` step
            [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", str(script)], cwd=checkout, env=run_env, capture_output=True, text=True, timeout=120, check=False
        )  # fmt: skip
        assert r.returncode == expected, (case, r.stdout + r.stderr)
        assert (annotation in r.stdout) if annotation else "::" not in r.stdout, (case, r.stdout)
        assert (checkout / "mutation.json").read_bytes() == stdout, case  # what the next step uploads


def test_workflow_literals_follow_the_ci_image_pins() -> None:
    """Where a workflow outside the image still writes a pin the image holds (the jobs on bare
    runners), it is the same version."""
    pins = _pins()
    minimum = pins.from_code()["NVIM_MIN"]
    for path in sorted(WORKFLOWS.glob("template-*.yml")):
        text = path.read_text(encoding="utf-8")
        for spec in re.findall(r"xonsh==[\d.]+", text):
            assert spec == pins.XONSH, (path.name, spec)
        for version in re.findall(r"(?:version|nvim): (v\d+\.\d+\.\d+)", text):
            assert version in {pins.NVIM, minimum}, (path.name, version)


def test_ci_image_scripts_pass_shellcheck() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    files = [str(IMAGE / n) for n in ("build", "system", "warm")]
    r = subprocess.run([shellcheck, "-s", "sh", *files], capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_ci_image_pins_pass_mypy_strict(tmp_path: Path) -> None:
    _passes_mypy_strict(IMAGE / "pins.py", tmp_path)


@pytest.mark.skipif(not os.environ.get("CI_IMAGE_INPUTS"), reason="runs inside the CI image only")
def test_ci_image_holds_this_checkout_and_its_tools() -> None:
    """Inside the image: it was built from this checkout's inputs, and every tool the suites look
    for is there (a missing one would only turn tests into skips). Its caches are in its user's
    home, the one passwd names, whatever HOME says: selftest --mutation's workers move HOME, and
    read from there this check failed the baselines of three modules in CI."""
    import pwd  # POSIX only: the image is Linux

    pins = _pins()
    assert Path(os.environ["CI_IMAGE_INPUTS"]).read_text(encoding="utf-8") == pins.canonical()
    tools = ("git", "uv", "nvim", "pwsh", "xonsh", "actionlint", "fish", "busybox", "shellcheck", "unzip", "objdump")
    tools += ("node", "cc", "dash", "zsh", "ksh", "mksh", "yash", "fdfind", "curl", "tar")
    assert [t for t in tools if shutil.which(t) is None] == []
    out = subprocess.run(["uv", "--version"], capture_output=True, text=True, check=True).stdout
    assert out.split()[1] == pins.UV
    for request in (pins.from_code()["CPYTHON"], "3.11"):
        subprocess.run(["uv", "python", "find", "--no-python-downloads", request], capture_output=True, check=True)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert (home / ".cache" / "pytemplate" / "tools" / f"upx-{pins.from_code()['UPX']}").is_dir()


# --- the flet builds (template-flet.yml and its folder) --------------------------------------

FLET_APP = ROOT / ".pytemplate" / "presets" / "flet" / "files" / "src" / "__pkg__" / "ui" / "app.py"
FLET_CONFIG = ROOT / ".pytemplate" / "presets" / "flet" / "files" / "pytemplate.toml"
FLET_PROJECT = "pt-flet"  # the folder the workflow's ./pyt new makes in $RUNNER_TEMP, and so the app's name


def _flet_check() -> ModuleType:
    return _load("template_flet_check", FLET / "check.py")


def _flet_project(folder: Path, name: str = FLET_PROJECT) -> str:
    """A project folder holding the flet preset's pytemplate.toml as ./pyt new writes it for
    `name`; returns its text."""
    text = FLET_CONFIG.read_text(encoding="utf-8").replace("{{name}}", name).replace("{{pkg}}", name.replace("-", "_").lower())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pytemplate.toml").write_text(text, encoding="utf-8")
    return text


def _cache_paths(body: str, action: str) -> list[str]:
    """The `path: |` lines of the step that uses `action`."""
    after = body[body.index(f"uses: {action}") :]
    lines = after[after.index("path: |\n") + len("path: |\n") :].splitlines()
    out = []
    for line in lines:
        if not line.startswith(" " * 12):
            break
        out.append(line.strip())
    return out


def test_flet_workflow_builds_for_the_web_and_android() -> None:
    """template-flet.yml: a project of the flet preset, made with ./pyt new and built with ./pyt as
    a user does, for the web (then run in a headless Chromium) and for Android (built only: an
    owner decision), on pushes to main and pull requests that touch what a flet build reads,
    weekly and by hand. The downloads are cached per Flet version and saved once the build
    passed; what the checks saw is uploaded whatever happened."""
    text = _text("template-flet.yml")
    assert triggers(text) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    on = "\n".join(_top_block(text, "on")) + "\n"
    assert re.search(r"(?m)^  push:\n    branches: \[main\]\n    paths:$", on), on
    lists = re.findall(r'(?m)^    paths:\n((?:      - ".+"\n)+)', on)
    assert len(lists) == 2 and lists[0] == lists[1], lists  # push and pull_request watch the same files
    paths = re.findall(r'"(.+)"', lists[0])
    for pattern in paths:  # a renamed file must fail here, not leave a trigger that never fires
        assert (ROOT / pattern[:-3]).is_dir() if pattern.endswith("/**") else (ROOT / pattern).is_file(), pattern
    # every module a flet build runs (envs, render, mypyc, config... not only methods/flet.py), the
    # launcher and the runner's Python the jobs start
    wanted = {".pytemplate/runner/**", ".pytemplate/pyt.py", "pyt", ".python-version", ".pytemplate/presets/flet/**", "uv.lock", ".github/workflows/template-flet.yml"}
    assert wanted | {".github/workflows/template-flet/**"} <= set(paths)
    assert re.search(r"(?m)^permissions:\n  contents: read$", text)

    found = jobs(text)
    assert set(found) == {"gate", "web", "android"}
    assert presets.name_from_folder(FLET_PROJECT) == FLET_PROJECT  # the app name the paths below use
    project = f'"$RUNNER_TEMP/{FLET_PROJECT}"'
    for job, target in (("web", "web"), ("android", "apk")):
        body = found[job]
        assert "runs-on: ubuntu-latest" in body and re.search(r"(?m)^    timeout-minutes: \d+$", body), job
        steps = [
            f"run: ./pyt new {project} --preset flet",
            "run: ./pyt setup",
            f"check.py target {project} {target}",
            f'check.py flet {project} >> "$GITHUB_OUTPUT"',
            "uses: actions/cache/restore@v6",
            "run: ./pyt build cpython --method flet",
            "uses: actions/cache/save@v6",
        ]
        where = [body.index(s) for s in steps]
        assert where == sorted(where), (job, where)
        assert body.count(f"working-directory: ${{{{ runner.temp }}}}/{FLET_PROJECT}") == 2, job  # setup and the build
        key = f"template-flet-{job}-${{{{ runner.os }}}}-${{{{ runner.arch }}}}-flet-${{{{ steps.flet.outputs.version }}}}"
        assert f"key: {key}" in body and "key: ${{ steps.cache.outputs.cache-primary-key }}" in body, job
        # saved on main only (a pull request restores main's): one copy per branch filled the 10 GB
        assert body.count("if: steps.cache.outputs.cache-hit != 'true' && github.ref == 'refs/heads/main'") == 1, job
        cached = ["~/flutter", "~/.pub-cache", "~/.flet/cache"] + (["~/.gradle/caches", "~/.gradle/wrapper"] if job == "android" else [])
        assert _cache_paths(body, "actions/cache/restore@v6") == _cache_paths(body, "actions/cache/save@v6") == cached, job
        upload = _step(body, "Upload")
        assert "if: always()" in upload and "uses: actions/upload-artifact@v7" in upload, job  # failed or cancelled too
        assert body.index("uses: actions/cache/save@v6") < body.index("uses: actions/upload-artifact@v7"), job

    web = found["web"]
    browser = f"--with-requirements {FLET.relative_to(ROOT).as_posix()}/browser.txt"
    install = f"{browser} python -m playwright install --with-deps --only-shell chromium"
    check = f'{browser} python .github/workflows/template-flet/check.py web {project} "$RUNNER_TEMP/browser"'
    assert web.index("uses: actions/cache/save@v6") < web.index(install) < web.index(check)
    upload = _step(web, "Upload")
    assert "name: flet-web-browser" in upload and "path: ${{ runner.temp }}/browser" in upload

    android = found["android"]
    assert re.search(r'uses: actions/setup-java@v6\n        with:\n          distribution: temurin\n          java-version: "17"\n', android)
    apk = f"check.py apk {project}"
    locked = re.search(r'(?m)^name = "packaging"\nversion = "([^"]+)"$', (ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert locked and f"--with packaging=={locked[1]} python .github/workflows/template-flet/{apk}" in android  # the markers of the build project
    signature = '"$tools/apksigner" verify --verbose "$apk"'
    folder = f"{FLET_PROJECT}/dist/{FLET_PROJECT}-cpython-flet-apk"
    assert android.index("uses: actions/setup-java@v6") < android.index("run: ./pyt build") < android.index(apk) < android.index(signature)
    assert f'find "$RUNNER_TEMP/{folder}" -name' in android and '"$tools/aapt2" dump badging "$apk"' in android
    upload = _step(android, "Upload")
    assert "name: flet-apk" in upload and f"path: ${{{{ runner.temp }}}}/{folder}" in upload
    assert re.search(r"(?m)^          retention-days: [1-7]$", upload), upload  # a build to look at, not a release
    names = re.findall(r"(?m)^          name: (\S+)$", text)
    assert sorted(names) == ["flet-apk", "flet-web-browser"]  # one artifact name per job


def test_flet_browser_pins_are_exact() -> None:
    """The web job's Playwright and its dependencies, one version each (`playwright install` takes
    the Chromium build of that release): an open range moved with every release."""
    lines = [line for line in (FLET / "browser.txt").read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
    names = []
    for line in lines:
        m = re.fullmatch(r"([a-z0-9-]+)==\d+(\.\d+)+", line)
        assert m, line
        names.append(m[1])
    assert "playwright" in names and len(set(names)) == len(names)


def test_flet_check_follows_the_skeleton() -> None:
    """check.py web looks for what the flet preset's app shows: a new wording there must fail here,
    not in the weekly run."""
    check = _flet_check()
    app = FLET_APP.read_text(encoding="utf-8")
    assert [fragment for fragment in check.SKELETON if fragment not in app] == []
    backend = "CPython"  # Pyodide's platform.python_implementation(); the web ships the core as .py
    assert check.READY == f"Core: {backend}. Press Draw."
    assert check.DONE.search(f"{300} iterations in {1.234:.2f} s (core: {backend})")
    assert not check.DONE.search(f"{300} iterations in {1.234:.2f} s (core: mypyc/{backend})")
    assert f'ft.Button("{check.BUTTON}"' in app
    assert '"emscripten"' in app  # NO_PROCESSES: in the browser the core runs in the event loop


def test_flet_check_names_the_runners_output_folders(tmp_path: Path) -> None:
    """check.py finds a build where ./pyt writes it (cmd_build.dist_path with flet's "-<target>")
    and reads app.name, the package and python.cpython as the runner does."""
    check = _flet_check()
    project = tmp_path / FLET_PROJECT
    cfg = cmd_mode._config_from_text(_flet_project(project), "pytemplate.toml")
    assert check.project_info(project) == (cfg.app.name, cfg.pkg, cfg.python.cpython)
    for target in ("web", "apk"):
        out = DIST.relative_to(ROOT) / (BuildRequest(cfg, "cpython", "flet", tmp_path).out_name + f"-{target}")
        assert check.output_dir(project, target) == project / out
    assert 'dist_path(req, f"-{target}")' in (ROOT / ".pytemplate" / "runner" / "methods" / "flet.py").read_text(encoding="utf-8")


def test_flet_target_step_edits_only_the_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """check.py target edits [deploy.flet] target with the project's own runner (its comment and
    every other line stay), refuses a folder without a runner, and never edits a file of another
    project whose runner was imported first (here the template's own, which pytest imported)."""
    check = _flet_check()
    monkeypatch.setattr(sys, "path", list(sys.path))  # set_target puts the project's .pytemplate first
    project = tmp_path / FLET_PROJECT
    text = _flet_project(project)
    with pytest.raises(check.Failed, match="no .pytemplate/runner"):
        check.set_target(project, "web")
    (project / ".pytemplate" / "runner").mkdir(parents=True)
    own = config.CONFIG_FILE.read_bytes()
    with pytest.raises(check.Failed, match="the runner imported edits"):
        check.set_target(project, "web")
    assert config.CONFIG_FILE.read_bytes() == own and (project / "pytemplate.toml").read_text(encoding="utf-8") == text

    monkeypatch.setattr(config, "CONFIG_FILE", project / "pytemplate.toml")  # as the project's own runner has it
    line = next(x for x in text.splitlines() if x.startswith('target = "host"   # '))  # [deploy.flet], with its comment
    for target in ("apk", "web", "host"):
        check.set_target(project, target)
        after = (project / "pytemplate.toml").read_text(encoding="utf-8").splitlines()
        assert len(after) == len(text.splitlines()), target
        changed = [(a, b) for a, b in zip(text.splitlines(), after, strict=True) if a != b]
        assert changed == ([] if target == "host" else [(line, line.replace('"host"', f'"{target}"'))]), changed
    assert (project / "pytemplate.toml").read_text(encoding="utf-8") == text
    (project / "pytemplate.toml").write_text(text.replace("[deploy.flet]", "[deploy.flet"), encoding="utf-8")
    with pytest.raises(check.Failed, match="pytemplate.toml"):  # the runner's own error, never a traceback
        check.set_target(project, "web")


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _apk_entries(pkg: str = "pt_flet", minor: str = "3.14") -> dict[str, dict[str, bytes] | bytes]:
    """What flet build apk 1.0.1 packs (serious_python_android 4.7.1): Flutter's and Python's
    runtimes per ABI, every native module split out to lib/<abi>/lib<dotted-name>.so with a
    .soref marker holding that name where the module was, and the app and the pure Python in
    stored asset zips (the inner dicts here)."""
    entries: dict[str, dict[str, bytes] | bytes] = {
        "AndroidManifest.xml": b"\x03\x00\x08\x00",
        "classes.dex": b"dex\n035\x00",
        "resources.arsc": b"\x02\x00\x0c\x00",
        "assets/flutter_assets/AssetManifest.bin": b"\x00",
    }
    for abi in ("arm64-v8a", "armeabi-v7a", "x86_64"):
        for lib in ("libflutter.so", "libapp.so", "libdart_bridge.so", f"libpython{minor}.so", "lib_ssl.so", "libmsgpack-_cmsgpack.so"):
            entries[f"lib/{abi}/{lib}"] = b"\x7fELF"
    entries["assets/app.zip"] = {n: b"" for n in ("main.py", f"{pkg}/__init__.py", f"{pkg}/ui/app.py", f"{pkg}/core/fractal.py", f"{pkg}/resources.py")}
    entries["assets/sitepackages.zip"] = {
        "flet/__init__.py": b"",
        "msgpack/_cmsgpack.soref": b"libmsgpack-_cmsgpack.so",
        **{f"{d}.dist-info/METADATA": b"" for d in ("flet-1.0.1", "msgpack-1.1.2", "httpx-0.28.1", "tomli_w-1.2.0", "Typing_Extensions-4.16.0")},
    }
    entries["assets/stdlib.zip"] = {"os.py": b"", "_ssl.soref": b"lib_ssl.so\n"}
    return entries


# The build project `./pyt build cpython --method flet` wrote for an apk: its pins, the relaxed
# binary one, markers as flet.target_markers writes them
APK_BUILD_PROJECT = """[project]
name = "pt-flet"
dependencies = [
    "flet==1.0.1 ; python_full_version < '3.15' and implementation_name == 'cpython'",
    "msgpack ; python_full_version < '3.15'",
    "httpx==0.28.1 ; python_full_version < '3.15' and platform_system != 'Emscripten'",
    "pyodide-http==0.2.2 ; platform_system == 'Emscripten'",
    "tomli-w==1.2.0 ; platform_system == 'Android'",
    "distro==1.9.0 ; platform_system == 'Linux'",
    "backports-tarfile==1.2.0 ; python_full_version < '3.12'",
    "typing-extensions==4.16.0",
]
"""


def _write_build_project(project: Path, text: str = APK_BUILD_PROJECT) -> None:
    path = project / ".build" / "flet-build" / "cpython" / "pyproject.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_apk(project: Path, entries: dict[str, dict[str, bytes] | bytes], name: str = "app-release.apk") -> Path:
    folder = project / "dist" / f"{FLET_PROJECT}-cpython-flet-apk"
    folder.mkdir(parents=True, exist_ok=True)
    apk = folder / name
    apk.write_bytes(_zip({n: _zip(v) if isinstance(v, dict) else v for n, v in entries.items()}))
    return apk


def test_flet_apk_check_reads_serious_pythons_layout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """check.py apk passes a complete build and names what an incomplete one lacks: a runtime of
    one ABI, a native module of one ABI, the app, the dependencies' native modules, a zip."""
    pytest.importorskip("packaging.requirements")
    check = _flet_check()
    project = tmp_path / FLET_PROJECT
    _flet_project(project)
    _write_build_project(project)
    with pytest.raises(check.Failed, match=r"0 \.apk files, not one"):
        check.check_apk(project)
    apk = _write_apk(project, _apk_entries())
    check.check_apk(project)
    out = capsys.readouterr().out
    assert "ok   app-release.apk" in out and "the dependencies' native modules: msgpack/_cmsgpack.soref" in out
    # the packages that hold on Android: not pyodide-http (the web), distro (Linux) or backports (< 3.12)
    assert check.android_requirements(project, "3.14") == ["flet", "msgpack", "httpx", "tomli-w", "typing-extensions"]
    assert "the 5 packages the build project requires on Android" in out

    def fails(entries: dict[str, dict[str, bytes] | bytes], reason: str) -> None:
        _write_apk(project, entries)
        with pytest.raises(check.Failed) as e:
            check.check_apk(project)
        assert reason in str(e.value), str(e.value)

    entries = _apk_entries()
    del entries["lib/x86_64/libpython3.14.so"]
    fails(entries, "lib/x86_64/ lacks libpython3.14.so")
    # a dependency of the app missing from the apk (import flet fails on the device)
    for gone in ("httpx-0.28.1", "tomli_w-1.2.0"):
        entries = _apk_entries()
        site = entries["assets/sitepackages.zip"]
        assert isinstance(site, dict)
        del site[f"{gone}.dist-info/METADATA"]
        fails(entries, f"assets/sitepackages.zip lacks {gone.rpartition('-')[0].replace('_', '-')} (no .dist-info)")
    entries = _apk_entries()
    del entries["lib/armeabi-v7a/libmsgpack-_cmsgpack.so"]
    fails(entries, "msgpack/_cmsgpack.soref names libmsgpack-_cmsgpack.so, which lib/armeabi-v7a/ lacks")
    entries = _apk_entries()
    app = entries["assets/app.zip"]
    assert isinstance(app, dict)
    del app["pt_flet/ui/app.py"]
    fails(entries, "assets/app.zip lacks pt_flet/ui/app.py[c]")
    entries = _apk_entries()
    entries["assets/sitepackages.zip"] = {"flet/__init__.py": b"", "msgpack/fallback.py": b""}
    fails(entries, "assets/sitepackages.zip: no native module (.soref) at all")
    entries = _apk_entries()
    del entries["assets/stdlib.zip"]
    fails(entries, "no assets/stdlib.zip")
    entries = _apk_entries()
    del entries["AndroidManifest.xml"]
    fails(entries, "no AndroidManifest.xml")
    # a compiled app (flet build --compile-app keeps only the .pyc) passes
    entries = _apk_entries()
    entries["assets/app.zip"] = {n.replace(".py", ".pyc"): b"" for n in _apk_entries()["assets/app.zip"]}
    _write_apk(project, entries)
    check.check_apk(project)
    # a member whose bytes do not match its CRC
    data = bytearray(apk.read_bytes())
    with zipfile.ZipFile(apk) as z:
        info = z.getinfo("lib/arm64-v8a/libapp.so")
    header = info.header_offset
    start = header + 30 + int.from_bytes(data[header + 26 : header + 28], "little") + int.from_bytes(data[header + 28 : header + 30], "little")
    data[start] ^= 0xFF
    apk.write_bytes(bytes(data))
    with pytest.raises(check.Failed, match="lib/arm64-v8a/libapp.so fails its CRC check"):
        check.check_apk(project)
    _write_apk(project, _apk_entries(), "app-debug.apk")
    with pytest.raises(check.Failed, match=r"2 \.apk files, not one"):
        check.check_apk(project)


class _FletPage:
    """Enough of Playwright's sync Page for check._drive, on a clock that moves only while the
    check waits: a Flet web app whose Python starts (python.js logs it), shows its controls in
    Flutter's semantics tree and draws a while after its button is clicked."""

    CENTRE = {"x": 640.0, "y": 450.0}

    def __init__(self, check: ModuleType, console: list[str], **app: Any) -> None:
        self.check, self.console = check, console
        self.app_title: str = app.get("title", FLET_PROJECT)
        self.python: str = app.get("python", "Python worker initialized: 1")
        self.drawn: str | None = app.get("drawn", "300 iterations in 1.23 s (core: CPython)")
        self.clicks: bool = app.get("clicks", True)
        self.loads: bool = app.get("loads", True)
        self.now = 0.0
        self.semantics = False
        self.pressed: float | None = None
        self.taps = 0
        self.mouse = SimpleNamespace(click=self._click)

    def goto(self, url: str, **_: Any) -> None:
        if not self.loads:
            raise TimeoutError("Timeout 120000ms exceeded.")  # what Playwright raises, by another class
        self.url = url

    def title(self) -> str:
        return self.app_title

    def wait_for_timeout(self, ms: float) -> None:
        self.now += ms / 1000
        if self.now >= 2 and self.python not in "\n".join(self.console):
            self.console.append(f"[console.log] {self.python}")

    def _shown(self) -> list[str]:
        if not self.semantics or self.now < 4:
            return []
        if self.pressed is None:
            status = "Core: CPython. Press Draw."
        elif self.now < self.pressed + 3 or self.drawn is None:
            status = "Computing..."
        else:
            status = self.drawn
        return ["Draw", status, f"Draw\n{status}"]

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if script == self.check.SEMANTICS_JS:
            was, self.semantics = self.semantics, True
            return "on" if was else "enabled"
        if script == self.check.TEXTS_JS:
            return self._shown()
        assert script == self.check.BUTTON_JS, script
        label, click = arg
        if label != "Draw" or "Draw" not in self._shown():
            return None
        if click:  # the semantics node's own tap
            self.taps += 1
            self._press()
            return {"x": 0, "y": 0}
        return dict(self.CENTRE)

    def _click(self, x: float, y: float) -> None:
        assert {"x": x, "y": y} == self.CENTRE
        if self.clicks:
            self._press()

    def _press(self) -> None:
        if self.pressed is None:
            self.pressed = self.now


def _drive(monkeypatch: pytest.MonkeyPatch, **app: Any) -> tuple[_FletPage, str]:
    """check._drive on a fake app: the page (and its clock) and why the check failed ("" passed)."""
    check = _flet_check()
    console: list[str] = []
    page = _FletPage(check, console, **app)
    monkeypatch.setattr(check, "time", SimpleNamespace(monotonic=lambda: page.now))
    try:
        check._drive(page, "http://127.0.0.1:1/", FLET_PROJECT, console)
    except check.Failed as e:
        return page, str(e)
    return page, ""


def test_flet_web_check_drives_the_app(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """check.py web passes only once the app's Python started, its controls and title showed and a
    click on Draw drew; a click the canvas lost is tapped again through the semantics node."""
    page, error = _drive(monkeypatch)
    out = capsys.readouterr().out
    assert error == "" and out.count("ok   ") == 3 and "'300 iterations in 1.23 s (core: CPython)'" in out, out
    assert page.taps == 0 and page.pressed is not None
    page, error = _drive(monkeypatch, clicks=False)  # the pointer event missed Flutter's hit test
    assert error == "" and page.taps == 1 and page.pressed is not None and page.pressed >= 20

    page, error = _drive(monkeypatch, loads=False)
    assert error == "http://127.0.0.1:1/ did not load: Timeout 120000ms exceeded."
    page, error = _drive(monkeypatch, python="Python worker init error: ModuleNotFoundError: No module named 'main'")
    assert "the app's Python did not start: [console.log] Python worker init error: ModuleNotFoundError" in error
    assert page.now < 10  # at once, not after the 300 s a first start may take
    page, error = _drive(monkeypatch, python="")
    assert "the app's Python starting (Pyodide): not within 300 s" in error
    page, error = _drive(monkeypatch, title="Flet")
    assert "not within 120 s; the page shows ['Draw\\nCore: CPython. Press Draw.'], title 'Flet'" in error, error
    page, error = _drive(monkeypatch, drawn="Draw failed (see the console)")
    assert "the Draw handler failed" in error
    page, error = _drive(monkeypatch, drawn=None)  # "Computing..." for ever
    assert "the drawing after a click on 'Draw': not within 300 s" in error


def test_flet_web_check_serves_the_build_as_a_static_host_does(tmp_path: Path) -> None:
    """The module worker of Flet's web build needs a JavaScript type and Pyodide application/wasm,
    whatever the machine's mime.types says; the server logs to the console.log list, not stderr."""
    import functools
    import http.server
    import threading
    import urllib.request

    check = _flet_check()
    names = {"index.html": "text/html", "python-worker.js": "text/javascript", "x.mjs": "text/javascript"}
    names |= {"pyodide.asm.wasm": "application/wasm", "app.json": "application/json"}
    for name in names:
        (tmp_path / name).write_text("x", encoding="utf-8")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(check._Site, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never a proxy for 127.0.0.1
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/"
        served = {name: opener.open(base + name, timeout=60).headers.get_content_type() for name in names}
    finally:
        server.shutdown()
        server.server_close()
    assert served == names
    assert any("GET /pyodide.asm.wasm" in line for line in check._Site.log), check._Site.log


def test_flet_check_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    check = _flet_check()
    assert check.main([]) == 2 and "Usage" in capsys.readouterr().err
    assert check.main(["web", str(tmp_path)]) == 2 and check.main(["target", str(tmp_path)]) == 2
    capsys.readouterr()
    project = tmp_path / FLET_PROJECT
    _flet_project(project)
    lock = '[[package]]\nname = "flet"\nversion = "1.0.1"\n\n[[package]]\nname = "flet-desktop"\nversion = "1.0.1"\n'
    (project / "uv.lock").write_text("version = 1\n\n" + lock, encoding="utf-8")
    assert check.main(["flet", str(project)]) == 0 and capsys.readouterr().out == "version=1.0.1\n"
    (project / "uv.lock").write_text("version = 1\n\n" + lock + lock.replace("1.0.1", "1.0.0"), encoding="utf-8")
    assert check.main(["flet", str(project)]) == 1 and "2 versions of flet, not one" in capsys.readouterr().err
    # a web check without a web build fails before it needs Playwright
    assert check.main(["web", str(project), str(tmp_path / "out")]) == 1
    assert "no index.html (did the web build run?)" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_flet_check_passes_mypy_strict(tmp_path: Path) -> None:
    _passes_mypy_strict(FLET / "check.py", tmp_path)
