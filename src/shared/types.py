"""Shared ``TypedDict`` contracts for BTG ConnectAI.

These structural types are the cross-Lambda data contracts: the Twilio webhook
payload re-packaged onto SQS, the DynamoDB records (Consent_Store, Auth_Session,
OTP_Store), the inline Mock_Core banking data, and the async notification events
published to the email/sms SQS queues. They document field names and types so
producers and consumers stay in sync; at runtime they are plain ``dict``s.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

# ---------------------------------------------------------------------------
# Inbound channel
# ---------------------------------------------------------------------------


class TwilioWebhookPayload(TypedDict):
    """Twilio WhatsApp webhook (form-urlencoded) re-packaged as JSON on SQS.

    The Webhook_Receiver parses the form body, extracts these fields, injects a
    ``correlationId`` (UUID v4) and enqueues the result on the inbound FIFO
    queue for the Message_Processor.
    """

    MessageSid: str  # Unique message ID (used for SQS FIFO dedup)
    From: str  # "whatsapp:+57300XXXXXXX"
    To: str  # "whatsapp:+14155XXXXXXX" (Twilio number)
    Body: str  # Message text (empty when media)
    NumMedia: str  # "0" | "1" | ...
    MediaUrl0: NotRequired[str]  # Media URL when NumMedia > 0
    MediaContentType0: NotRequired[str]  # "audio/ogg" | "image/jpeg" | ...
    ButtonPayload: NotRequired[str]  # Quick-reply button payload
    ProfileName: NotRequired[str]  # WhatsApp profile name of the client
    correlationId: str  # Injected by the Webhook_Receiver


# ---------------------------------------------------------------------------
# DynamoDB records
# ---------------------------------------------------------------------------


class ConsentRecord(TypedDict):
    """Item stored in the Consent_Store table (consent never expires)."""

    pk: str  # phoneNumber (E.164) — partition key
    status: Literal["accepted", "rejected"]
    acceptedAt: str  # ISO 8601 timestamp of acceptance
    tcVersion: str  # Accepted Terms & Conditions version (e.g. "1.0")
    updatedAt: str  # ISO 8601 timestamp of last update


class AuthSession(TypedDict):
    """Item stored in the Auth_Session table (TTL = 30 min)."""

    pk: str  # phoneNumber (E.164) — partition key
    sessionId: str  # UUID v4 of the session
    username: str  # Authenticated username
    name: str  # Full name of the user
    documentId: str  # Identity document (links to Mock_Core)
    createdAt: str  # ISO 8601 creation timestamp
    expiresAt: str  # ISO 8601 expiry timestamp
    ttl: int  # Unix epoch expiry (createdAt + 1800s)


class OTPRecord(TypedDict):
    """Item stored in the OTP_Store table (TTL = 5 min)."""

    pk: str  # phoneNumber (E.164) — partition key
    code: str  # 6-digit OTP code
    taskToken: str  # Step Functions task token to resume the workflow
    executionArn: str  # Step Functions execution ARN (audit)
    attempts: int  # Failed attempts (max 3)
    transferContext: dict  # Transfer data (amount, destination) for display
    createdAt: str  # ISO 8601 timestamp
    ttl: int  # Unix epoch expiry (now + 300s)


# ---------------------------------------------------------------------------
# Mock_Core banking data (inline in Action Group Lambdas / shared layer)
# ---------------------------------------------------------------------------


class MockProduct(TypedDict):
    """A banking product owned by a Mock_Core client."""

    account_id: str
    account_number: str
    product_type: Literal["fondo_inversion", "cuenta_corriente"]
    product_name: str
    currency: Literal["COP"]
    available_balance: float
    total_balance: float
    cutoff_date: str  # ISO 8601 date


class MockTransaction(TypedDict):
    """A movement on a Mock_Core account."""

    transaction_id: str
    account_id: str
    date: str  # ISO 8601 datetime
    description: str  # Max 100 chars
    amount: float
    currency: Literal["COP"]
    type: Literal["credit", "debit"]


class MockClient(TypedDict):
    """A synthetic banking client used across the Action Group Lambdas."""

    phone_number: str  # E.164
    name: str
    email: str
    document_id: str
    products: list[MockProduct]
    transactions: list[MockTransaction]


# ---------------------------------------------------------------------------
# Async notification events (SQS contracts)
# ---------------------------------------------------------------------------


class EmailNotificationEvent(TypedDict):
    """Event published to the email-notification-queue."""

    type: Literal["transfer_confirmation"]
    correlationId: str  # For tracing
    to: str  # Client email
    payload: dict  # receipt + clientName


class SmsNotificationEvent(TypedDict):
    """Event published to the sms-notification-queue."""

    type: Literal["transfer_confirmation"]
    correlationId: str
    phoneNumber: str  # E.164
    amount: float
    destinationAccount: str  # Already masked


__all__ = [
    "TwilioWebhookPayload",
    "ConsentRecord",
    "AuthSession",
    "OTPRecord",
    "MockProduct",
    "MockTransaction",
    "MockClient",
    "EmailNotificationEvent",
    "SmsNotificationEvent",
]
