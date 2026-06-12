"""Example-based unit tests for shared masking utilities (Requirement 14.4).

These complement the hypothesis property tests (Task 1.3) by pinning down the
concrete edge-case semantics: empty/None inputs, strings shorter than the
visible suffix, the exact-length boundary, and the specialised helpers
(``mask_phone`` / ``mask_account`` / ``mask_document``).
"""

from __future__ import annotations

import pytest

from shared.masking import (
    MASK_CHAR,
    VISIBLE_SUFFIX_LENGTH,
    mask_account,
    mask_document,
    mask_phone,
    mask_sensitive,
)

pytestmark = pytest.mark.unit


class TestMaskSensitiveEdgeCases:
    """Edge cases for the core ``mask_sensitive`` helper."""

    def test_none_returns_empty_string(self) -> None:
        assert mask_sensitive(None) == ""

    def test_empty_string_returns_empty_string(self) -> None:
        assert mask_sensitive("") == ""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1", "*"),
            ("12", "**"),
            ("123", "***"),
        ],
    )
    def test_strings_shorter_than_visible_are_fully_masked(
        self, value: str, expected: str
    ) -> None:
        # Keeping the whole short string would expose all of it, so every
        # character is masked while length is preserved.
        assert mask_sensitive(value) == expected

    def test_exact_length_string_is_unchanged(self) -> None:
        # length == visible: there are no leading characters to mask.
        assert mask_sensitive("1234") == "1234"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("12345", "*2345"),
            ("1234567", "***4567"),
            ("573001234567", "********4567"),
        ],
    )
    def test_longer_strings_expose_only_last_four(
        self, value: str, expected: str
    ) -> None:
        assert mask_sensitive(value) == expected

    def test_length_is_preserved_for_long_inputs(self) -> None:
        value = "9876543210"
        masked = mask_sensitive(value)
        assert len(masked) == len(value)
        assert masked.endswith("3210")
        assert masked[:-VISIBLE_SUFFIX_LENGTH] == MASK_CHAR * (len(value) - VISIBLE_SUFFIX_LENGTH)

    def test_non_string_value_is_coerced(self) -> None:
        # Integers (or other objects) are coerced to str before masking.
        assert mask_sensitive(573001234567) == "********4567"

    def test_custom_visible_count(self) -> None:
        assert mask_sensitive("12345678", visible=2) == "******78"

    def test_custom_mask_char(self) -> None:
        assert mask_sensitive("12345678", mask_char="#") == "####5678"


class TestMaskPhone:
    def test_masks_all_but_last_four(self) -> None:
        assert mask_phone("+573001234567") == "*********4567"

    def test_empty_and_none(self) -> None:
        assert mask_phone("") == ""
        assert mask_phone(None) == ""

    def test_short_value_fully_masked(self) -> None:
        assert mask_phone("12") == "**"


class TestMaskAccount:
    def test_masks_all_but_last_four(self) -> None:
        assert mask_account("1234567890") == "******7890"

    def test_exact_length_unchanged(self) -> None:
        assert mask_account("7890") == "7890"

    def test_none(self) -> None:
        assert mask_account(None) == ""


class TestMaskDocument:
    def test_masks_all_but_last_four(self) -> None:
        assert mask_document("CC1098765432") == "********5432"

    def test_short_value_fully_masked(self) -> None:
        assert mask_document("99") == "**"

    def test_empty(self) -> None:
        assert mask_document("") == ""
