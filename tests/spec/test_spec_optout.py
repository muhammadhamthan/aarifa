"""WP14 spec: the SRS §8.1 STOP rules, one case per example sentence in the SRS (D18)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kx.core.optout import (
    CONFIRMATION,
    FORBIDDEN_IN_CONFIRMATION,
    confirmation_text,
    match,
    match_voice,
)


@pytest.mark.parametrize(
    "text", ["STOP", "Stop.", "stop", "unsubscribe", "CANCEL", "End", "QUIT", "إيقاف", "रोको"]
)
def test_a_stop_word_that_is_the_whole_message_is_always_an_opt_out(text: str) -> None:
    """Step 1: "When the word is the whole message ... it is always an opt-out"."""
    m = match(text)
    assert (m.kind, m.scope) == ("opt_out", "all") and m.rule.startswith("word:")


def test_the_stop_word_sets_the_confirmation_language() -> None:
    assert (
        match("إيقاف").language == "ar" and match("रोको").language == "hi" and match("STOP").language == "en"
    )


@pytest.mark.parametrize(
    "text", ["don't message me", "Please remove me", "stop sending me messages", "Remove my number"]
)
def test_clear_phrases_are_an_opt_out(text: str) -> None:
    """Step 1: clear phrases ("don't message me", "remove me"); step 6: "stop sending me messages"."""
    assert match(text).kind == "opt_out"


@pytest.mark.parametrize(
    "text", ["stop, let me check", "stop, I meant 5,000 not 50,000", "stop, that's too expensive"]
)
def test_a_stop_word_inside_a_sentence_goes_to_the_ai(text: str) -> None:
    """Step 1: "When it sits inside a sentence, the meaning of the sentence decides (step 6)"."""
    assert match(text).kind == "context"


def test_channel_stops_stop_one_channel_only() -> None:
    """Step 6: "use email instead" stops WhatsApp only; "don't call me" stops calls only."""
    assert (match("Please use email instead").kind, match("Please use email instead").scope) == (
        "channel_stop",
        "whatsapp",
    )
    assert (
        match("don't call me").scope == "phone" and match("Don’t call me, WhatsApp is fine").scope == "phone"
    )


def test_ordinary_messages_are_not_stops() -> None:
    """Step 6: "too expensive" is negotiation (§12), not a stop."""
    for text in ("too expensive", "0.25 is high. Can you do 0.21?", "", "   ", "Can you deliver in 5 days?"):
        assert match(text).kind == "none"


def test_a_volunteered_reason_is_kept() -> None:
    """Step 4: free text the buyer writes on their own is saved as the reason."""
    assert match("remove me, found a cheaper supplier").reason == "found a cheaper supplier"
    assert match("STOP").reason is None


def test_voice_stop_means_wait_and_the_call_ends_only_on_a_clear_request() -> None:
    """Step 10: on voice "stop" usually means "wait"; "stop calling me" stops calls; unclear -> ask."""
    assert match_voice("stop") == "wait" and match_voice("stop, let me talk") == "wait"
    assert match_voice("stop calling me") == "phone_stop" and match_voice("don't call me") == "phone_stop"
    assert match_voice("goodbye") == "end_call" and match_voice("I don't want this call") == "end_call"
    assert match_voice("hmm stop it there please") == "unclear"
    assert match_voice("what sizes do you have?") == "none"


@pytest.mark.parametrize("language", sorted(CONFIRMATION))
def test_the_confirmation_says_done_what_stops_and_how_to_restart_and_nothing_else(language: str) -> None:
    """Step 3: no apology, button, question, pause offer, discount or promotion; the company is named."""
    text = confirmation_text(language, "Arifa Pack")
    assert "Arifa Pack" in text and "{company}" not in text
    assert not any(word in text.casefold() for word in FORBIDDEN_IN_CONFIRMATION)


def test_unknown_language_falls_back_to_english() -> None:
    assert confirmation_text("fr", "Arifa Pack") == confirmation_text("en", "Arifa Pack")


@given(st.text(max_size=80))
def test_the_matcher_never_crashes_and_is_deterministic(text: str) -> None:
    assert match(text) == match(text)
