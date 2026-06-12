"""Twilio webhook signature validation.

Twilio signs every webhook request with the ``X-Twilio-Signature`` header,
computed as an HMAC-SHA1 of the full request URL plus the sorted POST
parameters, keyed with the account's ``TWILIO_AUTH_TOKEN``. Validating this
signature is the system's defense against forged or unauthorized requests
hitting the public API Gateway endpoint.

If the signature does not match, the Webhook_Receiver handler responds
``403 Forbidden`` without enqueuing the message (Requirement 3.2). This module
exposes a single pure function, :func:`validate_twilio_signature`, that wraps
``twilio.request_validator.RequestValidator`` and degrades gracefully on
missing inputs.
"""

from __future__ import annotations

from twilio.request_validator import RequestValidator

from shared.logger import get_logger

logger = get_logger("webhook-receiver")


def validate_twilio_signature(
    auth_token: str,
    signature: str,
    url: str,
    params: dict[str, str],
) -> bool:
    """Validate the ``X-Twilio-Signature`` of an incoming webhook request.

    Uses ``twilio.request_validator.RequestValidator`` to recompute the
    expected signature from ``url`` and ``params`` and compare it (in constant
    time) against the signature Twilio sent.

    Args:
        auth_token: The Twilio account auth token (loaded from Secrets Manager).
        signature: The value of the ``X-Twilio-Signature`` request header.
        url: The full webhook URL exactly as Twilio targeted it
            (scheme + host + path), used as part of the signed payload.
        params: The POST form fields of the webhook request as a flat
            ``{name: value}`` mapping.

    Returns:
        ``True`` if the signature is valid; ``False`` otherwise (including when
        the auth token or signature is missing/empty, or when validation
        raises).
    """
    if not auth_token:
        logger.warning("Missing Twilio auth token, rejecting request")
        return False

    if not signature:
        logger.warning("Missing X-Twilio-Signature header, rejecting request")
        return False

    try:
        validator = RequestValidator(auth_token)
        is_valid = validator.validate(url, params or {}, signature)
    except Exception:
        # Defensive: never let a validation error surface as a 5xx. Treat any
        # unexpected failure as an invalid signature.
        logger.exception("Twilio signature validation raised, rejecting request")
        return False

    if not is_valid:
        logger.warning("Invalid Twilio signature, rejecting request")

    return is_valid


__all__ = ["validate_twilio_signature"]
