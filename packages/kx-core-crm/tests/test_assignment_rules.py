"""WP5 unit tests: assignment rule order and away/backup (the SRS §9, §24, D5). Pure, no DB."""

from __future__ import annotations

import uuid

from hypothesis import given
from hypothesis import strategies as st

from kx.crm.services.assignment import BuyerFacts, Candidate, available, choose, next_in_turn, rule_pool

SARA = Candidate(
    uuid.UUID(int=1), frozenset({"en", "ar"}), frozenset({"AE", "SA"}), frozenset({"ALU"}), seq=5
)
RAVI = Candidate(
    uuid.UUID(int=2), frozenset({"en", "hi", "ta"}), frozenset({"IN", "LK"}), frozenset({"PLASTIC"}), seq=3
)
OMAR = Candidate(uuid.UUID(int=3), frozenset({"ar"}), frozenset({"AE"}), frozenset({"PLASTIC"}), seq=None)
TEAM = (SARA, RAVI, OMAR)
ARAB_SA_ALU = BuyerFacts(language="ar", country="SA", category="ALU")


def test_default_is_round_robin_whoever_waited_longest() -> None:
    assert choose(TEAM, ARAB_SA_ALU, ())[0] == OMAR  # never assigned yet
    assert next_in_turn((SARA, RAVI)) == RAVI  # older turn first
    assert choose(TEAM, ARAB_SA_ALU, ())[1] is False  # no rule decided


def test_rules_are_checked_in_order_language_then_country_then_category() -> None:
    # language: Sara + Omar speak Arabic; country: only Sara covers SA -> Sara (not Omar, who is next in turn)
    picked, by_rule = choose(TEAM, ARAB_SA_ALU, ("language", "country", "category"))
    assert (picked, by_rule) == (SARA, True)
    assert rule_pool(TEAM, ARAB_SA_ALU, ("language",)) == [SARA, OMAR]


def test_several_matches_round_robin_among_them() -> None:
    buyer = BuyerFacts(language="ar", country="AE")
    assert choose(TEAM, buyer, ("language", "country"))[0] == OMAR  # Sara and Omar match; Omar's turn


def test_a_rule_nobody_matches_is_skipped() -> None:
    buyer = BuyerFacts(language="zh", country="SA")
    assert rule_pool(TEAM, buyer, ("language", "country")) == [SARA]


def test_scenario_7_arabic_buyer_goes_to_an_arabic_speaking_salesperson() -> None:
    picked, _ = choose((SARA, RAVI), BuyerFacts(language="ar-SA", country="SA"), ("language",))
    assert picked == SARA


def test_away_is_skipped_and_the_backup_covers() -> None:
    away = Candidate(
        SARA.user_id, SARA.languages, SARA.countries, is_away=True, backup_id=RAVI.user_id, seq=1
    )
    assert choose((away, RAVI, OMAR), ARAB_SA_ALU, ("language", "country"))[0] == RAVI  # Sara's backup
    everyone = {c.user_id: c for c in (away, RAVI)}
    assert available([away], everyone) == [RAVI]


def test_everyone_away_without_backup_goes_to_the_manager() -> None:
    a = Candidate(uuid.UUID(int=7), is_away=True)
    b = Candidate(uuid.UUID(int=8), is_away=True, backup_id=uuid.UUID(int=7))  # backup is away too
    assert choose((a, b), BuyerFacts(), ())[0] is None


ids = st.integers(min_value=1, max_value=6).map(lambda i: uuid.UUID(int=i))


@given(
    st.lists(
        st.builds(
            Candidate,
            user_id=ids,
            languages=st.frozensets(st.sampled_from(["en", "ar", "hi"])),
            is_away=st.booleans(),
            backup_id=st.none() | ids,
            seq=st.none() | st.integers(min_value=0, max_value=50),
        ),
        max_size=6,
        unique_by=lambda c: c.user_id,
    ),
    st.sampled_from(["en", "ar", "hi", None]),
)
def test_never_picks_someone_who_is_away(team: list[Candidate], language: str | None) -> None:
    picked, _ = choose(team, BuyerFacts(language=language), ("language",))
    if picked is not None:
        assert not picked.is_away and picked.user_id in {c.user_id for c in team}
