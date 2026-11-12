"""§7A.2 (D23): when the deal-page AI summary is (re)written."""

from __future__ import annotations

import pytest

from kx.agents.memory import deal_summary_due


@pytest.mark.parametrize(
    ("turns", "handoff", "due"),
    [
        (1, False, False),  # the buyer's first message alone: nothing to summarise yet
        (2, False, True),  # after the first exchange
        (4, False, False),
        (6, False, True),  # then every third exchange
        (8, False, False),
        (12, False, True),
        (9, True, True),  # always at hand-over, when the salesperson opens the deal
        (1, True, False),
    ],
)
def test_summary_schedule(turns: int, handoff: bool, due: bool) -> None:
    assert deal_summary_due(turns, handoff=handoff) is due
