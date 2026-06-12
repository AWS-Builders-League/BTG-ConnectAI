"""Shared constants for BTG ConnectAI.

Single source of truth for cross-Lambda magic numbers and versions. Importing
these by name (instead of hardcoding) keeps the WhatsApp limits, TTLs and the
Terms & Conditions version consistent across every component.
"""

from __future__ import annotations

# Maximum length (in characters) of a single Twilio/WhatsApp outbound message.
# Messages longer than this must be split into multiple chunks before sending.
MAX_TWILIO_MESSAGE_LENGTH: int = 1600

# Time-to-live (in seconds) of an authenticated banking session (30 minutes).
# Used to compute the DynamoDB `ttl` attribute on Auth_Session records.
AUTH_SESSION_TTL: int = 1800

# Time-to-live (in seconds) of an OTP challenge (5 minutes).
# Used to compute the DynamoDB `ttl` attribute on OTP_Store records.
OTP_TTL: int = 300

# Version of the Terms & Conditions currently presented to clients. Stored on
# the Consent_Store record so we can detect when a client accepted an older
# version and needs to re-consent.
TC_VERSION: str = "1.0"

__all__ = [
    "MAX_TWILIO_MESSAGE_LENGTH",
    "AUTH_SESSION_TTL",
    "OTP_TTL",
    "TC_VERSION",
]
