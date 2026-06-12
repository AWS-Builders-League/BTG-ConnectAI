"""Currency formatting helpers for Colombian Pesos (COP).

Requirement 10.5 / Property 15: amounts are rendered as ``$X.XXX.XXX,YY`` where
a period (``.``) separates thousands and a comma (``,``) separates the two
decimal places — the Colombian convention (e.g. ``$1.234.567,89``).

The implementation is locale-independent (it does not rely on ``locale`` being
configured in the Lambda runtime) and uses :class:`decimal.Decimal` with
``ROUND_HALF_UP`` so monetary rounding is predictable.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# Quantum used to round amounts to exactly two decimal places.
_CENTS = Decimal("0.01")


def format_cop(amount: float | int | str | Decimal) -> str:
    """Format a numeric amount as a COP currency string.

    Produces ``$X.XXX.XXX,YY`` with a period as the thousands separator, a comma
    as the decimal separator, and exactly two decimal places. Negative amounts
    are prefixed with ``-`` (e.g. ``-$1.234,50``); Property 15 only constrains
    non-negative inputs.

    Args:
        amount: The value to format. Accepts ``int``, ``float``, ``str`` or
            ``Decimal``. Strings are parsed as decimals.

    Returns:
        The formatted currency string, e.g. ``format_cop(1234567.89)`` ->
        ``"$1.234.567,89"`` and ``format_cop(0)`` -> ``"$0,00"``.
    """
    value = Decimal(str(amount)).quantize(_CENTS, rounding=ROUND_HALF_UP)

    negative = value < 0
    value = abs(value)

    # Split into integer and (always 2-digit) decimal parts.
    integer_part, _, decimal_part = f"{value:.2f}".partition(".")

    # Insert thousands separators. ``{:,}`` yields commas; swap them for dots to
    # match the Colombian convention.
    grouped = f"{int(integer_part):,}".replace(",", ".")

    formatted = f"${grouped},{decimal_part}"
    return f"-{formatted}" if negative else formatted


__all__ = ["format_cop"]
