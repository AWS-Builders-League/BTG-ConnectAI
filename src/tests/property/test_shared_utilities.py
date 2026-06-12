"""Property-based tests for shared utilities.

Covers:
    * Property 4  — Data Masking Correctness   (Validates: Requirements 14.4)
    * Property 15 — COP Currency Formatting     (Validates: Requirements 10.5)

These use Hypothesis to exercise the universal properties defined in the
design document across a wide range of generated inputs.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shared.formatting import format_cop
from shared.masking import (
    MASK_CHAR,
    VISIBLE_SUFFIX_LENGTH,
    mask_account,
    mask_document,
    mask_phone,
    mask_sensitive,
)

# ---------------------------------------------------------------------------
# Property 4: Data Masking Correctness
# Validates: Requirements 14.4
# ---------------------------------------------------------------------------

# Sensitive values are arbitrary printable strings of length >= 4. We exclude
# the mask character itself from the visible suffix space so the "only last 4
# visible" assertion is meaningful (the suffix should reflect original chars),
# while still allowing the full character set elsewhere.
_sensitive_strings = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=VISIBLE_SUFFIX_LENGTH,
    max_size=40,
)

# Digit-only strings model realistic phone/account/document identifiers.
_digit_strings = st.text(alphabet="0123456789", min_size=VISIBLE_SUFFIX_LENGTH, max_size=20)


def _assert_masking_correct(original: str, masked: str) -> None:
    """Assert the Property 4 invariants for a length >= 4 input."""
    # (1) Length is preserved.
    assert len(masked) == len(original)

    # (2) Only the last 4 characters of the original remain visible.
    assert masked[-VISIBLE_SUFFIX_LENGTH:] == original[-VISIBLE_SUFFIX_LENGTH:]

    # (3) Every preceding character is replaced with the mask character.
    prefix = masked[:-VISIBLE_SUFFIX_LENGTH]
    assert all(ch == MASK_CHAR for ch in prefix)


@pytest.mark.property
@given(value=_sensitive_strings)
def test_mask_sensitive_exposes_only_last_four(value: str) -> None:
    """For any string of length >= 4, only the last 4 chars stay visible."""
    _assert_masking_correct(value, mask_sensitive(value))


@pytest.mark.property
@given(value=_digit_strings)
def test_mask_phone_exposes_only_last_four(value: str) -> None:
    """mask_phone preserves the last 4 digits and masks the rest."""
    _assert_masking_correct(value, mask_phone(value))


@pytest.mark.property
@given(value=_digit_strings)
def test_mask_account_exposes_only_last_four(value: str) -> None:
    """mask_account preserves the last 4 digits and masks the rest."""
    _assert_masking_correct(value, mask_account(value))


@pytest.mark.property
@given(value=_digit_strings)
def test_mask_document_exposes_only_last_four(value: str) -> None:
    """mask_document preserves the last 4 digits and masks the rest."""
    _assert_masking_correct(value, mask_document(value))


# ---------------------------------------------------------------------------
# Property 15: COP Currency Formatting
# Validates: Requirements 10.5
# ---------------------------------------------------------------------------

# Pattern: starts with $, integer part grouped in 3s by dots, exactly 2 decimals
# after a comma. e.g. "$0,00", "$1.234,50", "$1.234.567,89".
_COP_PATTERN = re.compile(r"^\$\d{1,3}(\.\d{3})*,\d{2}$")

# Non-negative numbers across the supported input types.
_non_negative_numbers = st.one_of(
    st.integers(min_value=0, max_value=10**18),
    st.floats(min_value=0, max_value=1e15, allow_nan=False, allow_infinity=False),
    st.decimals(min_value=0, max_value=Decimal("1e15"), allow_nan=False, allow_infinity=False),
)


@pytest.mark.property
@given(amount=_non_negative_numbers)
def test_format_cop_matches_colombian_pattern(amount: int | float | Decimal) -> None:
    """For any non-negative number, format_cop yields $X.XXX.XXX,YY."""
    result = format_cop(amount)
    assert _COP_PATTERN.match(result), f"format_cop({amount!r}) -> {result!r}"
