"""Hypothesis settings of the runner's own tests (`./pyt selftest`), and the guard of the project's
own environments.

The property-based tests (`@given`) keep no example database: what a run tries depends on the
code, the profile and the seed only (a database replays the failures of earlier runs first, those
of other code too). A failure prints the smallest input it found and a blob that replays it
(`@reproduce_failure`). No deadline: on a loaded machine (the Windows runners) one slow example
failed a test that was right. The parent is the profile Hypothesis chose itself: its "ci" one when
it sees a CI, which tries the same inputs on every run.
`./pyt selftest --hypothesis-profile=pytemplate-deep` tries 20 times as many inputs. Hypothesis
still keeps caches of its own in .hypothesis/ (gitignored; the runner skips it like the other tool
caches).

No test may replace the project's .venv (the suite runs in it, and later tests start its tools): a
test that runs the real uv with a Config of another python.cpython than the project's (the
template's default, 3.14) made uv recreate it, empty, on that Python, and every later test that
needed it failed far from the cause. `_the_project_environment_stays` names the test that did it.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings

settings.register_profile("pytemplate", settings.default, database=None, deadline=None, print_blob=True)
settings.register_profile("pytemplate-deep", settings.get_profile("pytemplate"), max_examples=2000)
settings.load_profile("pytemplate")

ROOT = Path(__file__).resolve().parents[2]
PROJECT_ENVS = (ROOT / ".venv", ROOT / ".venv-wsl")  # the tools environment (-wsl: WSL on a Windows checkout)


def _interpreters() -> list[str | None]:
    """The interpreter of each environment of the project (pyvenv.cfg's lines that name it), or
    None where there is none."""
    found: list[str | None] = []
    for env in PROJECT_ENVS:
        try:
            text = (env / "pyvenv.cfg").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            found.append(None)
            continue
        found.append("; ".join(line.strip() for line in text.splitlines() if line.startswith(("home", "implementation", "version_info"))))
    return found


@pytest.fixture(autouse=True)
def _the_project_environment_stays() -> Iterator[None]:
    before = _interpreters()
    yield
    after = _interpreters()
    for env, old, new in zip(PROJECT_ENVS, before, after):
        if old is not None and new != old:
            pytest.fail(
                f"this test replaced the project's {env.name} ({old} -> {new}): a real uv call with "
                "another python.cpython than the project's; give its Config the project's (config.load)",
                pytrace=False,
            )


@pytest.fixture
def unprivileged_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """This Python, run as a user whom folder modes stop (POSIX): itself when the suite is not
    root, else a wrapper that drops the capabilities that let root pass them (setpriv; the test
    skips without it). A test of what a read-only or unreadable folder does needs it: root
    removes, reads and enters any folder, and such a test passed as root on a broken runner."""
    if sys.platform == "win32":
        pytest.skip("POSIX folder modes")
    if os.geteuid() != 0:
        return Path(sys.executable)
    setpriv = shutil.which("setpriv")
    if setpriv is None:
        pytest.skip("root, and no setpriv to drop the capabilities that let root pass folder modes")
    wrapper = tmp_path_factory.mktemp("unprivileged") / "python"
    argv = [setpriv, "--bounding-set=-dac_override,-dac_read_search,-fowner", "--", sys.executable]
    wrapper.write_text("#!/bin/sh\nexec " + shlex.join(argv) + ' "$@"\n', encoding="utf-8", newline="\n")
    wrapper.chmod(0o755)
    return wrapper


@pytest.fixture
def default_signals() -> Iterator[None]:
    """The signal handling of a process started in a terminal, for a test that sends itself a
    signal, reads what a child inherits or starts a child that does: SIGINT at Python's own
    handler, SIGTERM and SIGHUP (POSIX) at their default; the caller's settings come back after
    the test. ./pyt selftest started as a background job of a script (a POSIX shell starts it
    with SIGINT ignored) or under nohup (SIGHUP ignored) handed every test SIG_IGN, and those
    tests measured how the suite was started instead of the runner: 4 of them failed there, in
    every project, and so did the baselines of selftest --mutation run under nohup."""
    wanted: dict[signal.Signals, Any] = {signal.SIGINT: signal.default_int_handler}
    if sys.platform != "win32":
        wanted.update({signal.SIGTERM: signal.SIG_DFL, signal.SIGHUP: signal.SIG_DFL})
    saved = {signum: signal.signal(signum, handler) for signum, handler in wanted.items()}
    try:
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


# A CI template of the tests' own, with every placeholder render.ci_workflow fills, laid out as
# templates/ci.yml lays them out. The tests of what the runner writes into a CI template (the
# matrix rows, the build backend, the Linux libraries, the placeholders) run on it: a project
# edits its own .pytemplate/templates/ci.yml (README: "edit that file"), or deletes it, and its
# selftest must pass. The shipped template itself is tested in the template repository.
MINIMAL_CI_TEMPLATE = """\
# __HEADER__
name: ci
on:
  push:
    branches: [main, master]
jobs:
  test:
    strategy:
      matrix:
        include:
__MATRIX__
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v7
__LINUX_DEPS__
      - run: ./pyt sync ${{ matrix.backends }}
      - run: ./pyt build __BUILD_BACKEND__ --method pyz
      - run: ls dist/__NAME__-__BUILD_BACKEND__-pyz
"""


@pytest.fixture
def minimal_ci_template(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """render.TEMPLATES with MINIMAL_CI_TEMPLATE as its ci.yml (the other templates, the typing
    profiles among them, as the project has them); the path of that ci.yml."""
    from runner import render  # the test modules put .pytemplate on sys.path

    folder = tmp_path_factory.mktemp("templates")
    shutil.copytree(render.TEMPLATES, folder, dirs_exist_ok=True)
    (folder / "ci.yml").write_text(MINIMAL_CI_TEMPLATE, encoding="utf-8", newline="\n")
    monkeypatch.setattr(render, "TEMPLATES", folder)
    return folder / "ci.yml"
