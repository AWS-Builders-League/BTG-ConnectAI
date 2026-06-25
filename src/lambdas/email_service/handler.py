"""Email_Service Lambda — main handler (async, SQS triggered).

This Lambda is the consumer side of the email notification pattern. It is wired
to the ``email-notification-queue`` (owned by ``infra``) through an SQS Event
Source Mapping configured with ``batchSize=10``,
``maximumBatchingWindowInSeconds=5`` and ``reportBatchItemFailures=true`` so a
single bad message is retried on its own without re-driving the whole batch.

Per-message pipeline (:func:`record_handler`)
---------------------------------------------
1. Parse the SQS body into an EmailNotificationEvent and bind the event's
   ``correlationId`` to the logger.
2. Route on ``type``:
   * ``transfer_confirmation`` → :func:`send_transfer_confirmation`, which builds
     the BTG Pactual HTML/text email from the transfer receipt and sends it via
     Resend.
   * any other (unknown) ``type`` → logged as a warning and **skipped**.

Failure semantics
-----------------
:func:`record_handler` does **not** swallow send/transport errors: an exception
propagates to Powertools' :func:`process_partial_response`, which reports that
record as a ``batchItemFailure``. SQS then redelivers *only* that message; after
``maxReceiveCount`` (5) attempts it is moved to ``email-dlq`` automatically.

Data masking
------------
Account numbers in the receipt arrive already masked from
``transfer-breb-execute``. As defence in depth, every account field is passed
through :func:`~shared.masking.mask_account` again before rendering.

Environment / cross-stack contract
-----------------------------------
* ``RESEND_SECRET_ARN`` — ARN of the Secrets Manager secret containing the
  Resend API key and from_email.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import resend
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

# Powertools batch processor for SQS.
processor = BatchProcessor(event_type=EventType.SQS)

# Module-level Secrets Manager client.
_secrets_client = boto3.client("secretsmanager")

# Cached Resend config.
_resend_configured: bool = False


def _configure_resend() -> str:
    """Load Resend API key + from_email from Secrets Manager (cached)."""
    global _resend_configured
    if _resend_configured:
        return os.environ.get("_RESEND_FROM_EMAIL", "")

    secret_arn = os.environ["RESEND_SECRET_ARN"]
    response = _secrets_client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])

    resend.api_key = secret["api_key"]
    from_email = secret.get("from_email", "BTG Pactual <noreply@leadelivery.online>")
    os.environ["_RESEND_FROM_EMAIL"] = from_email
    _resend_configured = True
    return from_email


# Referential-information disclaimer shown on every notification.
DISCLAIMER: str = (
    "Esta información es referencial. Para registros oficiales, consulta los "
    "portales del banco."
)


def _format_amount(receipt: dict[str, Any]) -> str:
    """Render the receipt amount as a COP string, tolerating bad/missing values."""
    amount = receipt.get("amount")
    try:
        return format_cop(amount)
    except (ValueError, TypeError, ArithmeticError):
        return str(amount)


def _build_text_body(receipt: dict[str, Any], client_name: str) -> str:
    """Build the plain-text alternative of the confirmation email."""
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
    """Build the HTML body of the confirmation email (BTG Pactual identity)."""
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
    """Build and send the transfer-confirmation email via Resend.

    Args:
        to: The recipient email address (the client's email).
        payload: The event payload — either the receipt itself or a dict wrapping
            it under ``receipt`` plus an optional ``clientName``.

    Raises:
        Exception: If the Resend send call fails. The exception propagates so
            SQS retries the message.
    """
    from_email = _configure_resend()

    # Tolerate both shapes: payload IS the receipt, or payload = {"receipt": {...}, "clientName": ...}
    receipt = payload.get("receipt") if isinstance(payload.get("receipt"), dict) else payload
    client_name = str(payload.get("clientName", "") or "")

    logger.info(
        "sending transfer confirmation email",
        extra={
            "transactionId": receipt.get("transactionId"),
            "destination": mask_account(receipt.get("destinationAccount", "")),
        },
    )

    params: resend.Emails.SendParams = {
        "from": from_email,
        "to": [to],
        "subject": "BTG Pactual — Confirmación de transferencia",
        "html": _build_html_body(receipt, client_name),
        "text": _build_text_body(receipt, client_name),
    }

    resend.Emails.send(params)


def record_handler(record: Any) -> None:
    """Process a single SQS record (one notification event).

    Parses the body, binds the correlationId, and routes on ``type``. An unknown
    type is logged and skipped; a ``transfer_confirmation`` is sent via Resend.

    Args:
        record: The Powertools ``SQSRecord``.
    """
    event: EmailNotificationEvent = json.loads(record.body)

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

    Args:
        event: The SQS event from the email-notification-queue.
        context: The Lambda context.

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
    "DISCLAIMER",
    "send_transfer_confirmation",
    "record_handler",
    "handler",
]
