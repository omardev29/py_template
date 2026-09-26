"""The template repository's own workflows (.github/workflows/template-*.yml): what its CI
promises (every OS, the floors, projects made with ./deploy new, pinned Neovim) and what keeps
the scheduled ones running. Template repository only: `./deploy new` copies neither these
workflows nor the marker. Text checks (the runner is stdlib only, no YAML parser); actionlint,
when installed, validates the files themselves."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import cmd_nvim, envs, nvimtest  # noqa: E402
from runner.project import ROOT  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
pytestmark = pytest.mark.skipif(
    not (ROOT / ".pytemplate" / "template-repo").is_file(), reason="about the template repository's own workflows"
)
# the template workflows this file checks in depth (template-e2e.yml: test_e2e_plan.py's owner)
GATED = ("template-keepalive.yml", "template-launchers.yml", "template-nvim.yml", "template-selftest.yml")


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
    assert {"template-keepalive.yml", "template-selftest.yml", "template-nvim.yml", "template-launchers.yml"} <= scheduled
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
        assert re.search(r"(?m)^    needs: gate$", body), f"{name}: job {job} does not wait for the gate"
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
