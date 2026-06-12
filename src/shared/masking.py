"""Masking helpers for sensitive data (Requirement 14.4).

Sensitive values — phone numbers, account numbers and document IDs — must never
appear in full in logs or user-facing messages. The bank policy is to retain
only the last 4 characters; everything before them is replaced with a mask
character while the overall length is preserved (logical structure intact).

Property 4 (Data Masking Correctness) governs these helpers: for any input of
length >= 4 the output exposes only the last 4 characters and replaces every
preceding character with the mask character.
"""

from __future__ import annotations

# Character used to obscure masked positions.
MASK_CHAR: str = "*"

# Number of trailing characters left visible on a masked value.
VISIBLE_SUFFIX_LENGTH: int = 4


def mask_sensitive(
    value: object,
    visible: int = VISIBLE_SUFFIX_LENGTH,
    mask_char: str = MASK_CHAR,
) -> str:
    """Mask a sensitive value, leaving only the last ``visible`` chars exposed.

    Behaviour:
        * ``len >  visible`` → the leading ``len - visible`` characters are
          replaced with ``mask_char`` and the final ``visible`` characters are
          kept (length preserved). e.g. ``"573001234567" -> "********4567"``.
        * ``len == visible`` → returned unchanged (there are no preceding
          characters to mask); this satisfies Property 4 for the boundary case.
        * ``len <  visible`` → every character is masked, because keeping the
          whole short string would expose all of it. e.g. ``"12" -> "**"``.
        * ``None``/empty → empty string.

    Args:
        value: The value to mask. Coerced to ``str`` before processing.
        visible: How many trailing characters to keep visible. Defaults to 4.
        mask_char: The single character used for masked positions.

    Returns:
        The masked string with its original length preserved (for non-empty,
        non-``None`` inputs).
    """
    if value is None:
        return ""

    text = str(value)
    length = len(text)

    if length == 0:
        return ""

    if length < visible:
        return mask_char * length

    if length == visible:
        return text

    return mask_char * (length - visible) + text[-visible:]


def mask_phone(phone_number: object) -> str:
    """Mask a phone number, retaining only the last 4 digits (Req 14.4)."""
    return mask_sensitive(phone_number)


def mask_account(account_number: object) -> str:
    """Mask an account number, retaining only the last 4 digits (Req 14.4)."""
    return mask_sensitive(account_number)


def mask_document(document_id: object) -> str:
    """Mask a document ID, retaining only the last 4 digits (Req 14.4)."""
    return mask_sensitive(document_id)


__all__ = [
    "MASK_CHAR",
    "VISIBLE_SUFFIX_LENGTH",
    "mask_sensitive",
    "mask_phone",
    "mask_account",
    "mask_document",
]
