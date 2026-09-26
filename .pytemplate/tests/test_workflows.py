"""The template repository's own workflows (.github/workflows/template-*.yml): what its CI
promises (every OS, the floors, projects made with ./deploy new, pinned Neovim) and what keeps
the scheduled ones running. Template repository only: `./deploy new` copies neither these
workflows nor the marker. Text checks (the runner is stdlib only, no YAML parser); actionlint,
when installed, validates the files themselves."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_dev, cmd_nvim, config, envs, nvimtest, upx  # noqa: E402
from runner.project import ROOT  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
IMAGE = WORKFLOWS / "template-ci-image"  # the Linux CI image (template-ci-image.yml)
pytestmark = pytest.mark.skipif(
    not (ROOT / ".pytemplate" / "template-repo").is_file(), reason="about the template repository's own workflows"
)
# the template workflows this file checks in depth (template-e2e.yml's depths and triggers:
# test_e2e_plan.py); every one of them skips projects made from the template
GATED = (
    "template-ci-image.yml",
    "template-e2e.yml",
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
    text = _text("template-selftest.yml")
    assert triggers(text) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    found = jobs(text)
    main = found["selftest"]
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in main
    assert 0 < main.index("./deploy render --check") < main.index("./deploy setup") < main.index("run: ./deploy selftest -rs")
    assert "./deploy.ps1 selftest -rs" in main and "MSYS2_ROOT" in main  # Windows: MSYS2 found by the launcher tests
    assert "rhysd/action-setup-vim@v1" in main and "actionlint" in main and "xonsh==" in main
    floor = found["python-floor"]
    assert "--python 3.11 --with \"$pytest\" python -m pytest" in floor and ".pytemplate/tests" in floor
    assert "uv run --quiet --python 3.11 --script .pytemplate/deploy.py help" in floor
    uv_floor = found["uv-floor"]
    assert "resolution-strategy: lowest" in uv_floor and "MIN_UV" in uv_floor and "./deploy selftest" in uv_floor
    assert envs.MIN_UV in (ROOT / "pyproject.toml").read_text(encoding="utf-8")  # what setup-uv resolves "lowest" from
    new = found["new-project"]
    assert "preset: [raylib, flet]" in new and "./deploy new" in new and "./deploy selftest" in new


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
    """The workflow greps pins out of the code and deselects one test by name: a rename must
    fail here, not turn a CI step into `--from ""` or a deselect that matches nothing."""
    from runner import cmd_dev

    text = _text("template-selftest.yml")
    tests, runner = ROOT / ".pytemplate" / "tests", ROOT / ".pytemplate" / "runner"
    assert "sed -n 's/^MIN_UV = \"\\([0-9.]*\\)\".*/\\1/p' .pytemplate/runner/envs.py" in text
    assert re.findall(r'(?m)^MIN_UV = "([0-9.]*)"', (runner / "envs.py").read_text(encoding="utf-8")) == [envs.MIN_UV]
    assert "sed -n 's/^BASEDPYRIGHT = \"\\(.*\\)\".*/\\1/p' .pytemplate/runner/cmd_dev.py" in text
    assert re.findall(r'(?m)^BASEDPYRIGHT = "(.*)"', (runner / "cmd_dev.py").read_text(encoding="utf-8")) == [cmd_dev.BASEDPYRIGHT]
    assert "grep -m1 -o 'taplo==[0-9.]*' .pytemplate/tests/test_render_core.py" in text
    assert re.search(r'"taplo==[0-9.]+"', (tests / "test_render_core.py").read_text(encoding="utf-8"))
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
    assert ("ubuntu-latest", "v0.12.5") in rows and ("windows-latest", "v0.12.5") in rows and ("ubuntu-latest", minimum) in rows
    assert "version: ${{ matrix.nvim }}" in pinned and "github.event_name != 'schedule'" in pinned
    assert "name: nvim-logs-${{ matrix.os }}-${{ matrix.nvim }}" in pinned  # one artifact per row
    assert "version: stable" in canary and "github.event_name == 'schedule'" in canary
    assert f"rm {nvimtest.LOCK.relative_to(ROOT).as_posix()}" in canary
    upload = canary[canary.index("actions/upload-artifact") - 200 :]
    assert "if: always()" in upload  # the new pins travel even when the canary is red


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
    (astral-sh/setup-uv since v8)."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        uses = re.findall(r"uses: ([^\s]+)", path.read_text(encoding="utf-8"))
        assert uses and all(re.fullmatch(r"[\w-]+/[\w-]+@v\d+(\.\d+\.\d+)?", u) for u in uses), (path.name, uses)
        assert all(re.fullmatch(r"astral-sh/setup-uv@v\d+\.\d+\.\d+", u) for u in uses if "setup-uv" in u), path.name


# --- the Linux CI image (template-ci-image.yml and its folder) ---------------------------------


def _pins(root: Path | None = None) -> ModuleType:
    """template-ci-image/pins.py as a module (no bytecode left in the folder); `root` points it at
    a copy of the project."""
    spec = importlib.util.spec_from_file_location("ci_image_pins", IMAGE / "pins.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    before, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
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
    assert code["BASEDPYRIGHT"] == cmd_dev.BASEDPYRIGHT and code["UPX"] == upx.VERSION
    values = pins.values()
    assert envs.uv_version(f"uv {values['UV']}") >= envs.uv_version(f"uv {envs.MIN_UV}")  # type: ignore[operator]
    assert re.fullmatch(r"ubuntu:\d+\.\d+@sha256:[0-9a-f]{64}", pins.BASE)
    assert re.fullmatch(r"\d{8}T\d{6}Z", values["APT_SNAPSHOT"])
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for k, v in values.items() if k.endswith("_SHA256"))
    assert re.fullmatch(r"xonsh==[\d.]+", values["XONSH"]) and values["CI_UID"].isdigit()


def test_ci_image_reads_only_its_pins() -> None:
    """Every $NAME the image's scripts read comes from pins.env (or is their own)."""
    keys = set(_pins().values())
    own = {"ca", "tmp", "work", "pytest", "preset", "here", "root", "python", "ref", "source", "ctx", "image", "base"}
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
    assert all(any(rel.startswith(f".pytemplate/presets/{p}/") for rel in pins.data_files()) for p in ("script", "raylib", "flet"))


def test_ci_image_tag_follows_its_inputs(tmp_path: Path) -> None:
    """A changed lock or image file gives another tag; a CRLF or BOM checkout of the same bytes
    (Windows, core.autocrlf) gives the same one."""
    pins = _pins()
    files = [f".github/workflows/template-ci-image/{n}" for n in pins.IMAGE_FILES] + pins.data_files()
    files += [".pytemplate/runner/cmd_nvim.py", ".pytemplate/deploy.py", ".pytemplate/tests/test_render_core.py"]
    files += [".pytemplate/runner/cmd_dev.py", ".pytemplate/runner/upx.py"]
    for rel in files:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    copy = _pins(tmp_path)
    ref = copy.ref("Owner/Repo")
    assert re.fullmatch(r"ghcr\.io/owner/repo-ci:[0-9a-f]{16}", ref) and ref == pins.ref("owner/repo")
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"\xef\xbb\xbf" + lock.read_bytes().replace(b"\n", b"\r\n"))
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
    assert set(found) == {"selftest", "python-floor", "new-project", "launchers", "nvim"}
    for name, body in found.items():
        assert "needs.image.outputs.usable == 'true'" in body and "packages: read" in body, name
        assert "image: ${{ needs.image.outputs.ref }}" in body and "password: ${{ github.token }}" in body, name
        assert f"options: --init --user {pins.CI_UID}" in body and "HOME: /home/runner" in body, name
        assert "shell: bash" in body and "setup-uv" not in body and "apt-get" not in body, name
        assert "${{ runner.temp }}" not in body, name  # the host's path inside a container job
    assert f"nvim: [{pins.NVIM}, {pins.from_code()['NVIM_MIN']}]" in found["nvim"]


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
    selftest = _text("template-selftest.yml")
    assert f"download/v{pins.ACTIONLINT}/actionlint_{pins.ACTIONLINT}_linux_amd64.tar.gz" in selftest
    assert pins.ACTIONLINT_SHA256 in selftest


def test_ci_image_scripts_pass_shellcheck() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    files = [str(IMAGE / n) for n in ("build", "system", "warm")]
    r = subprocess.run([shellcheck, "-s", "sh", *files], capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_ci_image_pins_pass_mypy_strict() -> None:
    """pins.py is product code (CLAUDE.md 13.4) that the selftest's mypy run does not cover."""
    if importlib.util.find_spec("mypy") is None:
        pytest.skip("mypy is not installed in this interpreter")
    argv = [sys.executable, "-m", "mypy", "--strict", "--no-incremental", "--python-version", "3.11", str(IMAGE / "pins.py")]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not os.environ.get("CI_IMAGE_INPUTS"), reason="runs inside the CI image only")
def test_ci_image_holds_this_checkout_and_its_tools() -> None:
    """Inside the image: it was built from this checkout's inputs, and every tool the suites look
    for is there (a missing one would only turn tests into skips)."""
    pins = _pins()
    assert Path(os.environ["CI_IMAGE_INPUTS"]).read_text(encoding="utf-8") == pins.canonical()
    tools = ("git", "uv", "nvim", "pwsh", "xonsh", "actionlint", "fish", "busybox", "shellcheck", "unzip", "objdump")
    tools += ("node", "cc", "dash", "zsh", "ksh", "mksh", "yash", "fdfind", "curl", "tar")
    assert [t for t in tools if shutil.which(t) is None] == []
    out = subprocess.run(["uv", "--version"], capture_output=True, text=True, check=True).stdout
    assert out.split()[1] == pins.UV
    for request in (pins.from_code()["CPYTHON"], "3.11"):
        subprocess.run(["uv", "python", "find", "--no-python-downloads", request], capture_output=True, check=True)
    assert (Path.home() / ".cache" / "pytemplate" / "tools" / f"upx-{pins.from_code()['UPX']}").is_dir()
