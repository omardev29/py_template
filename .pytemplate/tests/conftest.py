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

from collections.abc import Iterator
from pathlib import Path

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
