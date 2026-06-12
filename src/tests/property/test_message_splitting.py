"""Property-based tests for Twilio message splitting.

Covers:
    * Property 1 — Message Splitting Round-Trip  (Validates: Requirements 3.10)

`split_message` breaks an arbitrary string into ordered chunks that each fit
within Twilio's WhatsApp single-message limit (1600 chars). The two universal
guarantees exercised here, per the design document and the function's
docstring, are:

    1. Round-trip: ``"".join(split_message(text, n)) == text`` for any input
       (no characters are dropped, trimmed, reordered or altered).
    2. Chunk size: every returned chunk satisfies ``len(chunk) <= n``.

Note: importing ``messaging`` constructs a module-level boto3 S3 client. We set
a default region before importing so the import succeeds in environments
without AWS configuration; ``split_message`` itself is pure (no AWS/Twilio).
"""

from __future__ import annotations

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shared.constants import MAX_TWILIO_MESSAGE_LENGTH
from lambdas.message_processor.messaging import split_message

# ---------------------------------------------------------------------------
# Property 1: Message Splitting Round-Trip
# Validates: Requirements 3.10
# ---------------------------------------------------------------------------

# Arbitrary text including newlines, spaces, unicode and the empty string.
# st.text() already produces all of these; we draw a broad alphabet so the
# newline/space boundary-preference branches and the unicode path are exercised.
_arbitrary_text = st.text(
    alphabet=st.characters(min_codepoint=0, max_codepoint=0x10FFFF),
    min_size=0,
    max_size=5000,
)

# Bias generation toward many newlines/spaces so the boundary-break branches
# (rfind "\n" / rfind " ") are hit frequently, not just the hard-cut fallback.
_whitespace_heavy_text = st.text(
    alphabet=st.sampled_from(["a", "b", " ", "\n", "ñ", "🎉"]),
    min_size=0,
    max_size=3000,
)

# Small max_length values stress the splitter far more than realistic ones,
# producing many chunks and many boundary decisions per input.
_max_lengths = st.integers(min_value=1, max_value=2000)


def _assert_split_invariants(text: str, max_length: int) -> None:
    """Assert the two Property 1 invariants for one (text, max_length) pair."""
    chunks = split_message(text, max_length)

    # (1) Round-trip: concatenation reproduces the original text exactly.
    assert "".join(chunks) == text

    # (2) Chunk size: every chunk fits within the limit.
    assert all(len(chunk) <= max_length for chunk in chunks)


@pytest.mark.property
@given(text=_arbitrary_text, max_length=_max_lengths)
def test_split_message_round_trip_arbitrary(text: str, max_length: int) -> None:
    """For any text and positive max_length, round-trip and size hold."""
    _assert_split_invariants(text, max_length)


@pytest.mark.property
@given(text=_whitespace_heavy_text, max_length=st.integers(min_value=1, max_value=200))
def test_split_message_round_trip_whitespace_heavy(text: str, max_length: int) -> None:
    """Whitespace-heavy text exercises the newline/space boundary branches."""
    _assert_split_invariants(text, max_length)


@pytest.mark.property
@given(text=_arbitrary_text)
def test_split_message_round_trip_default_limit(text: str) -> None:
    """The default Twilio limit (1600) keeps round-trip and chunks <= 1600."""
    chunks = split_message(text)
    assert "".join(chunks) == text
    assert all(len(chunk) <= MAX_TWILIO_MESSAGE_LENGTH for chunk in chunks)


@pytest.mark.property
@given(
    text=st.lists(
        st.sampled_from(["palabra ", "linea\n", "x", "ñ", "🎉"]),
        min_size=400,
        max_size=2000,
    ).map("".join)
)
def test_split_message_large_text_default_limit(text: str) -> None:
    """Large generated texts stay within the default 1600-char limit."""
    chunks = split_message(text)
    assert "".join(chunks) == text
    assert chunks  # non-empty text yields at least one chunk
    assert all(len(chunk) <= MAX_TWILIO_MESSAGE_LENGTH for chunk in chunks)


@pytest.mark.property
@given(text=st.text(min_size=1, max_size=2000), max_length=_max_lengths)
def test_split_message_non_empty_text_has_no_empty_chunks(
    text: str, max_length: int
) -> None:
    """Non-empty input produces at least one chunk and no empty chunks."""
    chunks = split_message(text, max_length)
    assert chunks
    assert all(len(chunk) > 0 for chunk in chunks)


@pytest.mark.property
@given(max_length=_max_lengths)
def test_split_message_empty_text_round_trips(max_length: int) -> None:
    """Empty text round-trips regardless of how the splitter represents it."""
    result = split_message("", max_length)
    assert "".join(result) == ""


def test_split_message_rejects_non_positive_max_length() -> None:
    """A non-positive max_length cannot satisfy the size invariant."""
    with pytest.raises(ValueError):
        split_message("hello", 0)
    with pytest.raises(ValueError):
        split_message("hello", -5)
