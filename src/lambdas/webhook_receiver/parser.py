"""Form-urlencoded parsing for the Twilio WhatsApp webhook.

The Twilio_Webhook_API (API Gateway HTTP API) delivers the webhook body as an
``application/x-www-form-urlencoded`` string. When the payload is flagged as
``isBase64Encoded`` by API Gateway, the body arrives base64-encoded and must be
decoded before parsing.

This module extracts the fields the Webhook_Receiver cares about into a plain
``dict`` shaped like :class:`shared.types.TwilioWebhookPayload` (minus the
``correlationId``, which the handler injects later — see task 4.4).
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs

from shared.types import TwilioWebhookPayload

# Fields always present in the returned payload (defaulted when absent).
_REQUIRED_FIELDS = ("MessageSid", "From", "To", "Body", "NumMedia")

# Fields only included in the result when Twilio actually sent them.
_OPTIONAL_FIELDS = (
    "MediaUrl0",
    "MediaContentType0",
    "ButtonPayload",
    "ProfileName",
)


def parse_form_urlencoded(body: str, is_base64: bool) -> TwilioWebhookPayload:
    """Parse a Twilio form-urlencoded webhook body into a payload dict.

    Args:
        body: The raw request body delivered by API Gateway. Either a plain
            form-urlencoded string or, when ``is_base64`` is ``True``, a
            base64-encoded form-urlencoded string.
        is_base64: ``True`` when API Gateway set ``isBase64Encoded`` on the
            event, meaning ``body`` must be base64-decoded before parsing.

    Returns:
        A ``dict`` compatible with the :class:`~shared.types.TwilioWebhookPayload`
        shape. Always contains ``MessageSid``, ``From``, ``To``, ``Body`` and
        ``NumMedia`` (``Body`` defaults to ``""``, ``NumMedia`` to ``"0"`` and
        the remaining required fields to ``""``). Optional media/button/profile
        fields are included only when present in the body. ``correlationId`` is
        NOT injected here; the handler adds it before enqueueing.
    """
    decoded_body = _decode_body(body, is_base64)

    # parse_qs returns ``{field: [value, ...]}``; Twilio sends single values, so
    # we take the first element of each list. ``keep_blank_values`` preserves an
    # empty ``Body`` (common for media-only messages).
    parsed = parse_qs(decoded_body, keep_blank_values=True)

    def first(field: str) -> str | None:
        values = parsed.get(field)
        return values[0] if values else None

    payload: TwilioWebhookPayload = {
        "MessageSid": first("MessageSid") or "",
        "From": first("From") or "",
        "To": first("To") or "",
        "Body": first("Body") or "",
        "NumMedia": first("NumMedia") or "0",
    }

    for field in _OPTIONAL_FIELDS:
        value = first(field)
        if value is not None:
            payload[field] = value  # type: ignore[literal-required]

    return payload


def _decode_body(body: str, is_base64: bool) -> str:
    """Return the form-urlencoded body, base64-decoding it when needed."""
    if not body:
        return ""
    if is_base64:
        return base64.b64decode(body).decode("utf-8")
    return body
