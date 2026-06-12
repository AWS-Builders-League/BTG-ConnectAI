"""Email_Service Lambda — main handler (async, SQS triggered).

This Lambda is the consumer side of the email notification pattern (design §7,
Requirements 17.3–17.7). It is wired to the ``email-notification-queue`` (owned
by ``infra``) through an SQS Event Source Mapping configured with ``batchSize=10``,
``maximumBatchingWindowInSeconds=5`` and ``reportBatchItemFailures=true`` so a
single bad message is retried on its own without re-driving the whole batch
(Requirement 17.3).

Per-message pipeline (:func:`record_handler`)
---------------------------------------------
1. Parse the SQS body into an :class:`~shared.types.EmailNotificationEvent` and
   bind the event's ``correlationId`` to the logger — the id is propagated from
   the state machine, never regenerated, so a transfer stays traceable from
   WhatsApp through to the confirmation email (Requirements 13.1, 13.2).
2. Route on ``type``:
   * ``transfer_confirmation`` → :func:`send_transfer_confirmation`, which builds
     the BTG Pactual HTML/text email from the transfer receipt and sends it via
     Amazon SES (Requirements 17.2, 17.4).
   * any other (unknown) ``type`` → logged as a warning and **skipped**. See
     "Unknown event types" below.

Failure semantics (Requirement 17.3 / 17.5)
--------------------------------------------
:func:`record_handler` does **not** swallow send/transport errors: an exception
(e.g. an SES ``ClientError``) propagates to Powertools'
:func:`process_partial_response`, which reports that record as a
``batchItemFailure``. SQS then redelivers *only* that message; after
``maxReceiveCount`` (3) attempts it is moved to ``email-dlq`` automatically.
Because the producer is fire-and-forget, none of this ever reaches the WhatsApp
flow (Requirement 17.7).

Unknown event types
-------------------
An event whose ``type`` is not handled is a *malformed/poison* message, not a
transient transport failure. Raising on it would make SQS redeliver it three
times and then DLQ it, burning retries on something that can never succeed. We
therefore log a warning and acknowledge (skip) the record so the queue is not
stuck looping a message no retry will fix. (Transport failures of *known* types
still raise and are retried/DLQ'd as above.)

Data masking (Requirement 17.6)
-------------------------------
Account numbers in the receipt arrive already masked from
``transfer-breb-execute`` (it stores ``mask_account(...)`` values). As defence in
depth — and to satisfy Requirement 17.6 regardless of the producer — every
account field is passed through :func:`~shared.masking.mask_account` again before
it is rendered into the email body or logged (the masking is idempotent on an
already-masked value).

Environment / cross-stack contract
-----------------------------------
* ``SES_SENDER_EMAIL`` — the verified SES sender/identity used as the email
  ``Source`` (e.g. ``noreply@btgpactual.com.co``). Read lazily inside the send so
  the module imports cleanly without the environment configured.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)

from shared.formatting import format_cop
from shared.logger import get_logger
from shared.masking import mask_account
from shared.types import EmailNotificationEvent

logger = get_logger("email-service")

# Powertools batch processor for SQS. With ``reportBatchItemFailures=true`` on
# the event source mapping, records whose handler raises are reported as
# ``batchItemFailure`` and retried individually (Requirement 17.3).
processor = BatchProcessor(event_type=EventType.SQS)

# Module-level SES client reused across warm invocations.
_ses_client = boto3.client("ses")

# Environment variable holding the verified SES sender identity (Requirement 17.4).
SES_SENDER_EMAIL_ENV: str = "SES_SENDER_EMAIL"

# Referential-information disclaimer shown on every notification: this email is a
# convenience copy, not the bank's official record (consistent with the agent's
# financial-data disclaimer, design §Conversational_Agent prompt rule 4).
DISCLAIMER: str = (
    "Esta información es referencial. Para registros oficiales, consulta los "
    "portales del banco."
)


def _format_amount(receipt: dict[str, Any]) -> str:
    """Render the receipt amount as a COP string, tolerating bad/missing values.

    Uses :func:`~shared.formatting.format_cop` (Colombian convention
    ``$X.XXX.XXX,YY``). If ``amount`` is absent or not numeric the raw value is
    returned as a string so a formatting hiccup never aborts an otherwise valid
    confirmation email.

    Args:
        receipt: The transfer receipt dict.

    Returns:
        The formatted amount, e.g. ``"$1.234.567,89"``.
    """
    amount = receipt.get("amount")
    try:
        return format_cop(amount)
    except (ValueError, TypeError, ArithmeticError):
        return str(amount)


def _build_text_body(receipt: dict[str, Any], client_name: str) -> str:
    """Build the plain-text alternative of the confirmation email.

    Account numbers are re-masked defensively (Requirement 17.6). Provided as the
    ``Text`` part so clients that do not render HTML still get a readable receipt.

    Args:
        receipt: The transfer receipt (Requirement 8.12 shape).
        client_name: The client's display name (may be empty).

    Returns:
        The plain-text email body.
    """
    greeting = f"Hola {client_name}," if client_name else "Hola,"
    return "\n".join(
        [
            greeting,
            "",
            "Tu transferencia BRE-B fue procesada exitosamente.",
            "",
            f"Comprobante: {receipt.get('transactionId', '')}",
            f"Cuenta origen: {mask_account(receipt.get('sourceAccount', ''))}",
            f"Cuenta destino: {mask_account(receipt.get('destinationAccount', ''))}",
            f"Monto: {_format_amount(receipt)} {receipt.get('currency', 'COP')}",
            f"Concepto: {receipt.get('concept', '') or 'N/A'}",
            f"Fecha: {receipt.get('executedAt', '')}",
            f"Estado: {receipt.get('status', '')}",
            "",
            DISCLAIMER,
            "",
            "BTG Pactual",
        ]
    )


def _build_html_body(receipt: dict[str, Any], client_name: str) -> str:
    """Build the HTML body of the confirmation email (BTG Pactual identity).

    Account numbers are re-masked defensively (Requirement 17.6). The template is
    a self-contained, inline-styled HTML fragment (no external assets) so it
    renders consistently across email clients.

    Args:
        receipt: The transfer receipt (Requirement 8.12 shape).
        client_name: The client's display name (may be empty).

    Returns:
        The HTML email body.
    """
    greeting = f"Hola {client_name}," if client_name else "Hola,"
    source = mask_account(receipt.get("sourceAccount", ""))
    destination = mask_account(receipt.get("destinationAccount", ""))
    amount = _format_amount(receipt)
    currency = receipt.get("currency", "COP")
    transaction_id = receipt.get("transactionId", "")
    concept = receipt.get("concept", "") or "N/A"
    executed_at = receipt.get("executedAt", "")
    status = receipt.get("status", "")

    rows = [
        ("Comprobante", transaction_id),
        ("Cuenta origen", source),
        ("Cuenta destino", destination),
        ("Monto", f"{amount} {currency}"),
        ("Concepto", concept),
        ("Fecha", executed_at),
        ("Estado", status),
    ]
    table_rows = "".join(
        f'<tr>'
        f'<td style="padding:8px 12px;color:#666;font-size:14px;'
        f'border-bottom:1px solid #eee;">{label}</td>'
        f'<td style="padding:8px 12px;color:#111;font-size:14px;font-weight:600;'
        f'border-bottom:1px solid #eee;text-align:right;">{value}</td>'
        f'</tr>'
        for label, value in rows
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;max-width:600px;">
        <tr><td style="background:#002b5c;padding:24px;color:#ffffff;font-size:20px;font-weight:700;">BTG Pactual</td></tr>
        <tr><td style="padding:24px;">
          <p style="margin:0 0 12px;color:#111;font-size:16px;">{greeting}</p>
          <p style="margin:0 0 20px;color:#333;font-size:15px;">Tu transferencia BRE-B fue procesada exitosamente.</p>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:6px;">
            {table_rows}
          </table>
          <p style="margin:20px 0 0;color:#888;font-size:12px;line-height:1.5;">{DISCLAIMER}</p>
        </td></tr>
        <tr><td style="background:#fafafa;padding:16px 24px;color:#aaa;font-size:11px;">© BTG Pactual</td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_transfer_confirmation(to: str, payload: dict[str, Any]) -> None:
    """Build and send the transfer-confirmation email via Amazon SES.

    The ``payload`` carries the transfer receipt produced by
    ``transfer-breb-execute``. The state machine publishes the receipt directly
    as ``payload`` (``payload.$: $.receipt``); for robustness this also accepts a
    nested ``{"receipt": {...}, "clientName": "..."}`` shape (the documented
    ``receipt + clientName`` contract). Account numbers are masked before
    inclusion (Requirement 17.6).

    Args:
        to: The recipient email address (the client's email).
        payload: The event payload — either the receipt itself or a dict wrapping
            it under ``receipt`` plus an optional ``clientName``.

    Raises:
        KeyError: If ``SES_SENDER_EMAIL`` is not configured.
        botocore.exceptions.ClientError: If the SES ``send_email`` call fails. The
            exception propagates so SQS retries the message (Requirement 17.3/17.5).
    """
    sender = os.environ[SES_SENDER_EMAIL_ENV]

    # Tolerate both the on-wire shape (payload IS the receipt) and the documented
    # wrapped shape (payload = {"receipt": {...}, "clientName": ...}).
    receipt = payload.get("receipt") if isinstance(payload.get("receipt"), dict) else payload
    client_name = str(payload.get("clientName", "") or "")

    logger.info(
        "sending transfer confirmation email",
        extra={
            "transactionId": receipt.get("transactionId"),
            "destination": mask_account(receipt.get("destinationAccount", "")),
        },
    )

    _ses_client.send_email(
        Source=sender,
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {
                "Data": "BTG Pactual — Confirmación de transferencia",
                "Charset": "UTF-8",
            },
            "Body": {
                "Html": {"Data": _build_html_body(receipt, client_name), "Charset": "UTF-8"},
                "Text": {"Data": _build_text_body(receipt, client_name), "Charset": "UTF-8"},
            },
        },
    )


def record_handler(record: Any) -> None:
    """Process a single SQS record (one notification event).

    Parses the body into an :class:`~shared.types.EmailNotificationEvent`, binds
    the propagated ``correlationId`` to the logger, and routes on ``type``. An
    unknown type is logged and skipped (see module docstring "Unknown event
    types"); a ``transfer_confirmation`` is sent via SES. Transport errors are
    *not* caught: they propagate to :func:`process_partial_response` so the record
    is retried individually as a ``batchItemFailure`` (Requirement 17.3 / 17.5).

    Args:
        record: The Powertools ``SQSRecord``. Its ``body`` is the JSON the state
            machine enqueued.
    """
    event: EmailNotificationEvent = json.loads(record.body)

    # Propagate the correlation id minted upstream — never regenerate it (Req 13.2).
    correlation_id = event.get("correlationId")
    if correlation_id:
        logger.append_keys(correlation_id=correlation_id)

    event_type = event.get("type")
    if event_type == "transfer_confirmation":
        send_transfer_confirmation(event["to"], event.get("payload", {}))
        return

    # Unknown type: poison message — log and skip rather than loop retries/DLQ.
    logger.warning(
        "skipping email notification with unknown type",
        extra={"eventType": event_type},
    )


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point — process the SQS batch with partial-failure reporting.

    Delegates to Powertools' :func:`process_partial_response`, which runs
    :func:`record_handler` per record and returns ``{"batchItemFailures": [...]}``
    for any records that raised, so SQS retries only those messages
    (Requirement 17.3).

    Args:
        event: The SQS event delivered by the ``email-notification-queue`` event
            source mapping.
        context: The Lambda context (used by Powertools for partial responses).

    Returns:
        The partial batch response mapping consumed by the SQS integration.
    """
    return process_partial_response(
        event=event,
        record_handler=record_handler,
        processor=processor,
        context=context,
    )


__all__ = [
    "SES_SENDER_EMAIL_ENV",
    "DISCLAIMER",
    "send_transfer_confirmation",
    "record_handler",
    "handler",
]
