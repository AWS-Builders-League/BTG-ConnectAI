"""Example-based unit tests for COP currency formatting (Requirement 10.5).

These complement the hypothesis property tests (Task 1.3) by pinning down
concrete values: zero, plain integers, large numbers (millions/billions),
decimals, ROUND_HALF_UP rounding behaviour, and the accepted input types
(``int`` / ``float`` / ``str`` / ``Decimal``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared.formatting import format_cop

pytestmark = pytest.mark.unit


class TestFormatCopBasics:
    def test_zero(self) -> None:
        assert format_cop(0) == "$0,00"

    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            (1, "$1,00"),
            (12, "$12,00"),
            (123, "$123,00"),
            (1000, "$1.000,00"),
            (12345, "$12.345,00"),
        ],
    )
    def test_small_integers(self, amount: int, expected: str) -> None:
        assert format_cop(amount) == expected

    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            (1_000_000, "$1.000.000,00"),
            (1_234_567, "$1.234.567,00"),
            (1_000_000_000, "$1.000.000.000,00"),
            (1_234_567_890, "$1.234.567.890,00"),
        ],
    )
    def test_large_numbers(self, amount: int, expected: str) -> None:
        assert format_cop(amount) == expected


class TestFormatCopDecimals:
    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            (0.5, "$0,50"),
            (1234.5, "$1.234,50"),
            (1234567.89, "$1.234.567,89"),
            (1234567.8, "$1.234.567,80"),
        ],
    )
    def test_decimals(self, amount: float, expected: str) -> None:
        assert format_cop(amount) == expected

    def test_round_half_up_rounds_up(self) -> None:
        # .895 -> .90 under ROUND_HALF_UP (use Decimal to avoid float artefacts).
        assert format_cop(Decimal("1234567.895")) == "$1.234.567,90"

    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            (Decimal("0.005"), "$0,01"),
            (Decimal("0.004"), "$0,00"),
            (Decimal("2.675"), "$2,68"),
            (Decimal("10.125"), "$10,13"),
        ],
    )
    def test_rounding_boundaries(self, amount: Decimal, expected: str) -> None:
        assert format_cop(amount) == expected


class TestFormatCopInputTypes:
    def test_string_input(self) -> None:
        assert format_cop("1234567.89") == "$1.234.567,89"

    def test_decimal_input(self) -> None:
        assert format_cop(Decimal("1000000")) == "$1.000.000,00"

    def test_int_and_float_agree(self) -> None:
        assert format_cop(1000) == format_cop(1000.0) == "$1.000,00"


class TestFormatCopNegative:
    def test_negative_amount_is_prefixed(self) -> None:
        # Property 15 only constrains non-negative inputs; negatives are
        # rendered with a leading '-' which this example pins down.
        assert format_cop(-1234.5) == "-$1.234,50"
