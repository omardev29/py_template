"""Hypothesis settings of the runner's own tests (`./pyt selftest`).

The property-based tests (`@given`) keep no example database: what a run tries depends on the
code, the profile and the seed only (a database replays the failures of earlier runs first, those
of other code too). A failure prints the smallest input it found and a blob that replays it
(`@reproduce_failure`). No deadline: on a loaded machine (the Windows runners) one slow example
failed a test that was right. The parent is the profile Hypothesis chose itself: its "ci" one when
it sees a CI, which tries the same inputs on every run.
`./pyt selftest --hypothesis-profile=pytemplate-deep` tries 20 times as many inputs. Hypothesis
still keeps caches of its own in .hypothesis/ (gitignored; the runner skips it like the other tool
caches).
"""

from __future__ import annotations

from hypothesis import settings

settings.register_profile("pytemplate", settings.default, database=None, deadline=None, print_blob=True)
settings.register_profile("pytemplate-deep", settings.get_profile("pytemplate"), max_examples=2000)
settings.load_profile("pytemplate")
