"""Twilio outbound messaging module for the Message_Processor Lambda.

Centralizes every outbound interaction with the client over the Twilio
WhatsApp REST API so the rest of the Lambda never talks to Twilio directly:

* :func:`send_twilio_message` — deliver an agent/text response to the client via
  the Twilio REST API (``messages.create``), splitting it into ordered chunks
  when it exceeds the WhatsApp single-message limit
  (Requirements 3.7, 3.10).
* :func:`split_message` — pure helper that breaks a string into ``<= max_length``
  chunks. **Round-trip safe**: ``"".join(split_message(text)) == text`` for any
  input (validated by Property 1, Task 5.7).
* :func:`send_welcome_message` — send the Spanish welcome message listing the
  three available services after the client accepts the Terms & Conditions
  (Requirements 4.1, 4.2).
* :func:`send_twilio_document` — generate a short-lived (5 min) S3 presigned GET
  URL for a statement PDF and send it to the client as a WhatsApp document
  attachment via Twilio ``media_url`` (Requirement 9.4).

Environment / client construction
----------------------------------
Twilio credentials (``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN``) and the
sender address (``TWILIO_WHATSAPP_NUMBER``, with or without the ``whatsapp:``
prefix) are read lazily *inside* the functions so the module imports cleanly in
environments without the configuration (tests inject it before calling). The
boto3 S3 client and the Twilio REST client are constructed once and reused
across warm Lambda invocations.

This module is the messaging dependency that ``consent.py`` (and ``auth.py``)
import lazily; it intentionally exposes the exact names ``send_twilio_message``
and ``send_welcome_message``.
"""

from __future__ import annotations

import os
from typing import Any

import boto3

from shared.constants import MAX_TWILIO_MESSAGE_LENGTH
from shared.logger import get_logger
from shared.masking import mask_phone

logger = get_logger("message-processor")

# Validity (in seconds) of the S3 presigned URL handed to Twilio so it can
# download the statement PDF. Kept short (5 minutes) because Twilio fetches the
# media immediately on send (Requirement 9.4 / design §statement-generator).
PRESIGNED_URL_EXPIRY_SECONDS: int = 300

# Caption shown alongside the statement PDF document attachment.
STATEMENT_DOCUMENT_CAPTION: str = "📄 Aquí tienes tu extracto bancario."

# Welcome message presented after the client accepts the Terms & Conditions
# (Requirements 4.1, 4.2). Lists the three available services and tells the
# client they can request any of them in natural language (text or audio),
# without selecting from a menu.
WELCOME_MESSAGE: str = (
    "¡Bienvenido a BTG ConnectAI! 🎉\n\n"
    "Ya puedes usar nuestros servicios bancarios directamente por WhatsApp:\n\n"
    "• Transferencias BRE-B entre cuentas\n"
    "• Consulta de saldos (Fondos de Inversión y Cuenta Corriente)\n"
    "• Generación de extractos bancarios en PDF\n\n"
    "No necesitas seleccionar opciones de un menú: solo escríbeme o envíame una "
    "nota de voz contándome qué necesitas y yo me encargo. 😊"
)

# Module-level resources/clients reused across warm invocations.
_s3_client = boto3.client("s3")
_twilio_client: Any = None  # Lazily constructed (see _get_twilio_client).


def _get_twilio_client() -> Any:
    """Construct (once) and return the Twilio REST client.

    Built lazily on first send so the module imports cleanly without Twilio
    credentials configured. Credentials come from ``TWILIO_ACCOUNT_SID`` /
    ``TWILIO_AUTH_TOKEN``.

    Returns:
        A cached ``twilio.rest.Client`` instance.

    Raises:
        KeyError: If the Twilio credential environment variables are not set.
    """
    global _twilio_client
    if _twilio_client is None:
        from twilio.rest import Client

        _twilio_client = Client(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )
    return _twilio_client


def _whatsapp_address(phone_number: str) -> str:
    """Return a ``whatsapp:``-prefixed address for a bare or prefixed number.

    Accepts a value with or without the ``whatsapp:`` prefix and always returns
    it prefixed, matching the convention used in ``consent.py`` / ``auth.py``.
    """
    normalized = phone_number.strip()
    if normalized.startswith("whatsapp:"):
        return normalized
    return f"whatsapp:{normalized}"


def split_message(
    text: str, max_length: int = MAX_TWILIO_MESSAGE_LENGTH
) -> list[str]:
    """Split ``text`` into ordered chunks each at most ``max_length`` characters.

    Splitting strategy (Requirement 3.10):

    * If ``text`` already fits in a single message it is returned unchanged as a
      one-element list.
    * Otherwise the text is cut into consecutive slices. Within each slice we
      prefer to break on the **last newline**, then the **last space**, that
      falls inside the ``max_length`` window, so chunks end on natural word/line
      boundaries when possible. If no such boundary exists (e.g. a single very
      long token) we fall back to a hard cut at exactly ``max_length``.

    **Round-trip guarantee** (Property 1, Task 5.7): the chosen boundary is kept
    at the *end* of the current chunk (no characters — including the boundary
    whitespace — are ever dropped, trimmed or altered), therefore
    ``"".join(split_message(text, n)) == text`` holds for every input, and every
    returned chunk satisfies ``len(chunk) <= n``.

    Args:
        text: The text to split.
        max_length: Maximum length of each chunk. Defaults to
            :data:`shared.constants.MAX_TWILIO_MESSAGE_LENGTH` (1600).

    Returns:
        The list of chunks, in order. Concatenating them reproduces ``text``
        exactly.

    Raises:
        ValueError: If ``max_length`` is not a positive integer (a non-positive
            limit cannot satisfy the chunk-size invariant).
    """
    if max_length <= 0:
        raise ValueError("max_length must be a positive integer")

    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_length:
        # Prefer the last newline, then the last space, inside the window so the
        # chunk ends on a natural boundary. The boundary character stays at the
        # end of the current chunk (index + 1) so no characters are lost.
        newline_index = remaining.rfind("\n", 0, max_length)
        if newline_index != -1:
            split_index = newline_index + 1
        else:
            space_index = remaining.rfind(" ", 0, max_length)
            split_index = space_index + 1 if space_index != -1 else max_length

        chunks.append(remaining[:split_index])
        remaining = remaining[split_index:]

    if remaining:
        chunks.append(remaining)

    return chunks


def send_twilio_message(phone_number: str, text: str) -> None:
    """Send a text response to the client via the Twilio REST API (Req 3.7).

    The response is delivered as one or more outbound WhatsApp messages using
    ``messages.create`` (not as an HTTP webhook response). If ``text`` exceeds
    :data:`shared.constants.MAX_TWILIO_MESSAGE_LENGTH` it is split with
    :func:`split_message` and the chunks are sent **sequentially, in order**, so
    the client reads them in the right sequence (Requirement 3.10).

    Empty or whitespace-only text is skipped (nothing to deliver).

    Args:
        phone_number: The client's phone number (``whatsapp:`` prefix optional).
        text: The message body to deliver.
    """
    if not text or not text.strip():
        logger.info(
            "skipping empty twilio message", extra={"phone": mask_phone(phone_number)}
        )
        return

    client = _get_twilio_client()
    from_address = _whatsapp_address(os.environ["TWILIO_WHATSAPP_NUMBER"])
    to_address = _whatsapp_address(phone_number)

    chunks = split_message(text)
    for chunk in chunks:
        client.messages.create(from_=from_address, to=to_address, body=chunk)

    logger.info(
        "twilio message sent",
        extra={"phone": mask_phone(phone_number), "chunks": len(chunks)},
    )


def send_welcome_message(phone_number: str) -> None:
    """Send the welcome message listing the available services (Req 4.1 / 4.2).

    Delivered after the client accepts the Terms & Conditions for the first
    time. The message lists the three services (transferencias BRE-B, consulta
    de saldos de Fondos de Inversión y Cuenta Corriente, y generación de
    extractos) informatively, telling the client they can request any of them in
    natural language without selecting from a menu.

    Args:
        phone_number: The client's phone number (``whatsapp:`` prefix optional).
    """
    send_twilio_message(phone_number, WELCOME_MESSAGE)
    logger.info("welcome message sent", extra={"phone": mask_phone(phone_number)})


def send_twilio_document(
    phone_number: str,
    s3_bucket: str,
    s3_key: str,
    caption: str | None = None,
) -> None:
    """Send an S3-stored PDF to the client as a WhatsApp attachment (Req 9.4).

    Twilio needs a publicly fetchable URL to attach media, so a short-lived
    (:data:`PRESIGNED_URL_EXPIRY_SECONDS`, 5 minutes) S3 presigned ``get_object``
    URL is generated and passed to ``messages.create`` via ``media_url``. The
    statement PDF is therefore delivered directly to the client over WhatsApp
    (it is not emailed). The presigned URL expires shortly after Twilio fetches
    the media, limiting exposure.

    Args:
        phone_number: The client's phone number (``whatsapp:`` prefix optional).
        s3_bucket: The bucket holding the statement PDF.
        s3_key: The object key of the statement PDF.
        caption: Optional message body shown with the document. Defaults to
            :data:`STATEMENT_DOCUMENT_CAPTION`.
    """
    presigned_url = _s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": s3_bucket, "Key": s3_key},
        ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
    )

    client = _get_twilio_client()
    client.messages.create(
        from_=_whatsapp_address(os.environ["TWILIO_WHATSAPP_NUMBER"]),
        to=_whatsapp_address(phone_number),
        body=caption if caption is not None else STATEMENT_DOCUMENT_CAPTION,
        media_url=[presigned_url],
    )

    logger.info(
        "twilio document sent",
        extra={"phone": mask_phone(phone_number), "s3_key": s3_key},
    )


__all__ = [
    "PRESIGNED_URL_EXPIRY_SECONDS",
    "STATEMENT_DOCUMENT_CAPTION",
    "WELCOME_MESSAGE",
    "split_message",
    "send_twilio_message",
    "send_welcome_message",
    "send_twilio_document",
]
