"""Consent flow module for the Message_Processor Lambda (Requirement 1).

Implements the regulatory Terms & Conditions gate that runs before any banking
service. The flow is *consent-first*: no message is processed until the client
has an ``accepted`` record in the Consent_Store DynamoDB table (created by the
``infra`` repo, name resolved from the cross-stack contract via the
``CONSENT_TABLE_NAME`` environment variable).

Behaviour (mapped to the acceptance criteria):

* :func:`get_consent` — reads the Consent_Store record for a phone number
  (Requirements 1.4, 1.5).
* :func:`store_consent` — persists the accept/reject decision with a timestamp
  and the accepted T&C version (Requirements 1.2, 1.5).
* :func:`handle_consent_flow` — routes the inbound message:
    * ``ButtonPayload == "accept_tc"`` → store ``accepted`` + send welcome
      (Requirement 1.2).
    * ``ButtonPayload == "reject_tc"`` → store ``rejected`` + reply that
      acceptance is mandatory (Requirement 1.3).
    * otherwise (first contact, no consent) → send the interactive T&C message
      (Requirement 1.1).
* :func:`send_terms_and_conditions_message` — sends a Twilio Content Template
  with accept/reject quick-reply buttons (Requirement 1.1).

Cross-module assumption: the Twilio messaging module (``messaging``, Task 5.6)
provides ``send_twilio_message`` and ``send_welcome_message``. To keep this
module importable on its own — and to avoid a hard dependency on a module being
built in parallel — those helpers are imported lazily *inside*
:func:`handle_consent_flow`. The interactive T&C send is self-contained here so
the core consent gate works regardless of the messaging module's state.
"""

from __future__ import annotations

import os
import unicodedata
from datetime import datetime, timezone
from typing import Literal

import boto3

from shared.constants import TC_VERSION
from shared.logger import get_logger
from shared.masking import mask_phone
from shared.types import ConsentRecord, TwilioWebhookPayload

logger = get_logger("message-processor")

ConsentStatus = Literal["accepted", "rejected"]

# Reply sent when a client rejects the Terms & Conditions (Requirement 1.3).
# Kept local so the consent gate does not depend on the messaging module's
# error-message catalogue.
CONSENT_REQUIRED_MESSAGE: str = (
    "Para utilizar los servicios de BTG ConnectAI es obligatorio aceptar los "
    "Términos y Condiciones. No podremos procesar tus solicitudes hasta que los "
    "aceptes. Cuando quieras, escríbenos de nuevo para revisarlos y aceptarlos."
)

# Plain-text Terms & Conditions message used only when no Content Template SID is
# configured. Asks the client to reply ACEPTO / RECHAZO.
TERMS_AND_CONDITIONS_TEXT: str = (
    "👋 ¡Bienvenido a BTG ConnectAI!\n\n"
    f"Antes de continuar debes aceptar nuestros Términos y Condiciones "
    f"(versión {TC_VERSION}). Tratamos tus datos conforme a la ley y solo para "
    "atender tus solicitudes bancarias.\n\n"
    "Responde *ACEPTO* para continuar o *RECHAZO* si no estás de acuerdo."
)


def _normalize_text(text: str) -> str:
    """Lower-case, strip and remove accents from a free-text reply.

    Used to match consent keywords robustly (``"Sí"`` -> ``"si"``,
    ``"ACEPTO"`` -> ``"acepto"``).
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return no_accents.strip().lower()


def interpret_consent_reply(
    button_payload: str | None, body: str | None
) -> Literal["accept", "reject", "unknown"]:
    """Map a quick-reply button **or** free text to a consent decision.

    Quick-reply buttons take precedence (the primary UX); if no recognized button
    is present the message body is matched (accent/case-insensitively) against the
    accept/reject keyword sets. Returns ``"unknown"`` when neither matches.
    """
    if button_payload == ACCEPT_BUTTON_PAYLOAD:
        return "accept"
    if button_payload == REJECT_BUTTON_PAYLOAD:
        return "reject"

    normalized = _normalize_text(body or "")
    if normalized in _ACCEPT_KEYWORDS:
        return "accept"
    if normalized in _REJECT_KEYWORDS:
        return "reject"
    return "unknown"

# Quick-reply button payloads Twilio sends back in ``ButtonPayload``.
ACCEPT_BUTTON_PAYLOAD: str = "accept_tc"
REJECT_BUTTON_PAYLOAD: str = "reject_tc"

# Text keywords accepted as consent decisions when the client replies with plain
# text instead of (or in addition to) a quick-reply button. Compared accent- and
# case-insensitively. Buttons remain the primary UX; this is a safety net.
_ACCEPT_KEYWORDS: frozenset[str] = frozenset(
    {"acepto", "aceptar", "acepto los terminos", "si", "acepto tc", "1"}
)
_REJECT_KEYWORDS: frozenset[str] = frozenset(
    {"rechazo", "rechazar", "no", "no acepto", "2"}
)

# Optional Twilio Content Template SID for the interactive (button) T&C message.
# When present the quick-reply buttons are sent; when absent we fall back to a
# plain-text message so the consent gate never hard-fails.
_TC_TEMPLATE_SID_ENV: str = "TWILIO_TC_TEMPLATE_SID"

# Module-level resources/clients are reused across warm invocations.
_dynamodb = boto3.resource("dynamodb")
_twilio_client = None  # Lazily constructed (see _get_twilio_client).


def _get_consent_table():
    """Return the Consent_Store DynamoDB table handle.

    The table name is read lazily from ``CONSENT_TABLE_NAME`` so the module can
    be imported without the environment configured — tests set the variable
    before invoking the functions.

    Raises:
        KeyError: If ``CONSENT_TABLE_NAME`` is not configured.
    """
    return _dynamodb.Table(os.environ["CONSENT_TABLE_NAME"])


def _normalize_phone(phone_number: str) -> str:
    """Strip the ``whatsapp:`` channel prefix so the key is a bare E.164 number.

    The Message_Processor already passes a bare number, but normalizing here
    keeps the partition key stable regardless of the caller.
    """
    return phone_number.replace("whatsapp:", "").strip()


def get_consent(phone_number: str) -> ConsentRecord | None:
    """Fetch the Consent_Store record for ``phone_number`` (Requirement 1.4).

    Args:
        phone_number: The client's phone number (E.164, with or without the
            ``whatsapp:`` prefix).

    Returns:
        The :class:`ConsentRecord` if one exists, otherwise ``None`` (first
        contact — the T&C flow must run).
    """
    pk = _normalize_phone(phone_number)
    response = _get_consent_table().get_item(Key={"pk": pk})
    item = response.get("Item")
    if item is None:
        logger.info("no consent record found", extra={"phone": mask_phone(pk)})
        return None
    return item  # type: ignore[return-value]


def has_accepted_consent(consent: ConsentRecord | None) -> bool:
    """Return ``True`` only when ``consent`` exists and is ``accepted``.

    Helper used by the gate (and Property 5: existing accepted consent skips the
    T&C flow, Requirement 1.4).
    """
    return bool(consent) and consent.get("status") == "accepted"  # type: ignore[union-attr]


def store_consent(phone_number: str, status: ConsentStatus) -> ConsentRecord:
    """Persist a consent decision in the Consent_Store (Requirements 1.2, 1.5).

    Writes the partition key (phone number), the decision ``status``, the
    decision timestamp and the accepted Terms & Conditions version. Consent
    never expires, so no TTL is set.

    Args:
        phone_number: The client's phone number (E.164).
        status: ``"accepted"`` or ``"rejected"``.

    Returns:
        The :class:`ConsentRecord` that was written.
    """
    pk = _normalize_phone(phone_number)
    now = datetime.now(timezone.utc).isoformat()

    record: ConsentRecord = {
        "pk": pk,
        "status": status,
        "acceptedAt": now,  # Timestamp of the decision (Requirement 1.5)
        "tcVersion": TC_VERSION,
        "updatedAt": now,
    }

    _get_consent_table().put_item(Item=dict(record))
    logger.info(
        "consent stored",
        extra={"phone": mask_phone(pk), "status": status, "tc_version": TC_VERSION},
    )
    return record


def handle_consent_flow(
    payload: TwilioWebhookPayload,
    consent: ConsentRecord | None,
    phone_number: str,
) -> None:
    """Drive the consent gate for a message from a client without valid consent.

    Routing (Requirements 1.1, 1.2, 1.3):
        * ``accept_tc`` button → store ``accepted`` and send the welcome message.
        * ``reject_tc`` button → store ``rejected`` and reply that acceptance is
          mandatory.
        * anything else (first contact) → send the interactive T&C message.

    The messaging helpers are imported lazily so this module stays importable
    even while the messaging module (Task 5.6) is being built in parallel.

    Args:
        payload: The parsed inbound Twilio webhook payload.
        consent: The current Consent_Store record (``None`` or ``rejected``).
        phone_number: The client's phone number (E.164).
    """
    button = payload.get("ButtonPayload")
    body = payload.get("Body")
    decision = interpret_consent_reply(button, body)

    if decision == "accept":
        store_consent(phone_number, "accepted")
        from .messaging import send_welcome_message  # lazy: avoids hard coupling

        send_welcome_message(phone_number)
        logger.info("consent accepted", extra={"phone": mask_phone(phone_number)})
        return

    if decision == "reject":
        store_consent(phone_number, "rejected")
        from .messaging import send_twilio_message  # lazy: avoids hard coupling

        send_twilio_message(phone_number, CONSENT_REQUIRED_MESSAGE)
        logger.info("consent rejected", extra={"phone": mask_phone(phone_number)})
        return

    # First message with no (accepted) consent — present the T&C.
    send_terms_and_conditions_message(phone_number)


def _get_twilio_client():
    """Construct (once) and return the Twilio REST client.

    Built lazily so the module imports cleanly without Twilio credentials in the
    environment. Credentials are read from ``TWILIO_ACCOUNT_SID`` /
    ``TWILIO_AUTH_TOKEN``.
    """
    global _twilio_client
    if _twilio_client is None:
        from twilio.rest import Client

        _twilio_client = Client(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )
    return _twilio_client


def _whatsapp_from() -> str:
    """Return the ``whatsapp:`` sender address from ``TWILIO_WHATSAPP_NUMBER``.

    Accepts the env value with or without the ``whatsapp:`` prefix and always
    returns it prefixed, as Twilio requires.
    """
    number = os.environ["TWILIO_WHATSAPP_NUMBER"]
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


def send_terms_and_conditions_message(phone_number: str) -> None:
    """Send the T&C message (Req 1.1).

    If ``TWILIO_TC_TEMPLATE_SID`` is configured, send the interactive Content
    Template (accept/reject quick-reply buttons). Otherwise fall back to a
    plain-text message asking the client to reply *ACEPTO* / *RECHAZO* — this
    keeps the consent gate working in the Twilio sandbox without a template.

    Args:
        phone_number: The client's phone number (E.164).
    """
    pk = _normalize_phone(phone_number)
    client = _get_twilio_client()
    template_sid = os.environ.get(_TC_TEMPLATE_SID_ENV)

    if template_sid:
        client.messages.create(
            from_=_whatsapp_from(),
            to=f"whatsapp:{pk}",
            content_sid=template_sid,
        )
    else:
        client.messages.create(
            from_=_whatsapp_from(),
            to=f"whatsapp:{pk}",
            body=TERMS_AND_CONDITIONS_TEXT,
        )
    logger.info("terms and conditions message sent", extra={"phone": mask_phone(pk)})


__all__ = [
    "ACCEPT_BUTTON_PAYLOAD",
    "REJECT_BUTTON_PAYLOAD",
    "CONSENT_REQUIRED_MESSAGE",
    "TERMS_AND_CONDITIONS_TEXT",
    "interpret_consent_reply",
    "get_consent",
    "has_accepted_consent",
    "store_consent",
    "handle_consent_flow",
    "send_terms_and_conditions_message",
]
