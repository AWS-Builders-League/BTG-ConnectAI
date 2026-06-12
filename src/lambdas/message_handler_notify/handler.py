"""message-handler-notify Lambda handler (Requirements 8.4, 8.7-8.12).

This Lambda is invoked by the **terminal states** of the
``TransferBrebStateMachine``. Each terminal state passes a ``messageType`` (plus
optional ``receipt``/``error``) so a single function can deliver the right
Spanish WhatsApp message to the client and end the workflow:

================== ======================= ===============================
State machine state ``messageType``         Meaning
================== ======================= ===============================
``NotifyUserSuccess``       ``transfer_success``   Transfer completed — send the
                                                   comprobante (Req 8.4 / 8.12).
``NotifyValidationFailed``  ``validation_failed``  Insufficient funds or invalid
                                                   destination (Req 8.10).
``NotifyOTPExpired``        ``otp_expired``        OTP expired without callback
                                                   (Req 8.9).
``NotifyOTPBlocked``        ``otp_blocked``        Too many failed OTP attempts
                                                   (Req 8.8).
``NotifyTransferFailed``    ``transfer_failed``    Unexpected execution error
                                                   (Req 8.11).
================== ======================= ===============================

Receipt fields (``transfer_success``)
--------------------------------------
The ``receipt`` payload is produced by ``transfer_breb/execute.py`` and already
satisfies Requirement 8.12: ``transactionId``, ``sourceAccount`` (**already
masked**), ``destinationAccount`` (**already masked**), ``amount``, ``currency``
(``"COP"``), ``concept``, ``executedAt`` (ISO 8601) and ``status``
(``"COMPLETED"``). Because the accounts arrive masked we do **not** mask them
again here. The ``amount`` is rendered with :func:`shared.formatting.format_cop`
and the message ends with the standard "información referencial" disclaimer.

Environment / Twilio client
---------------------------
For consistency with ``message_processor/messaging.py`` the Twilio REST client
is constructed **lazily** from environment variables so the module imports
cleanly (and is easy to test by monkeypatching :func:`_get_twilio_client`):

* ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN`` — Twilio credentials. The
  CloudFormation template (Task 15) wires the ``TWILIO_SECRET_ARN`` secret into
  these env vars, mirroring the existing messaging convention.
* ``TWILIO_WHATSAPP_NUMBER`` — sender address, accepted with or without the
  ``whatsapp:`` prefix.

This Lambda runs OUTSIDE the VPC (it needs internet egress to reach Twilio).
"""

from __future__ import annotations

import os
from typing import Any

from shared.formatting import format_cop
from shared.logger import get_logger
from shared.masking import mask_account, mask_phone
from shared.twilio_env import hydrate_twilio_env

logger = get_logger("message-handler-notify")

# ---------------------------------------------------------------------------
# Disclaimer (Requirement 8.4 / design system-prompt rule 4). Reused verbatim so
# every financial/receipt message carries the same "información referencial"
# wording the Conversational_Agent uses.
# ---------------------------------------------------------------------------
REFERENTIAL_DISCLAIMER: str = (
    "📋 Esta información es referencial. Para registros oficiales, consulta los "
    "portales del banco."
)

# ---------------------------------------------------------------------------
# Static Spanish message bodies for the non-receipt terminal states.
# ---------------------------------------------------------------------------
VALIDATION_FAILED_MESSAGE: str = (
    "❌ No pudimos validar tu transferencia.\n\n"
    "Es posible que no tengas fondos suficientes o que la cuenta destino no sea "
    "válida. Revisa los datos e inténtalo de nuevo.\n\n"
    "No se realizó ningún movimiento en tu cuenta."
)

OTP_EXPIRED_MESSAGE: str = (
    "⏱️ Tu código de verificación expiró.\n\n"
    "Por seguridad, la transferencia fue cancelada y no se realizó ningún "
    "movimiento. Si deseas continuar, vuelve a solicitar la transferencia."
)

OTP_BLOCKED_MESSAGE: str = (
    "🔒 Bloqueamos la transferencia por seguridad.\n\n"
    "Ingresaste el código de verificación incorrecto demasiadas veces. No se "
    "realizó ningún movimiento en tu cuenta. Si deseas continuar, vuelve a "
    "solicitar la transferencia más tarde."
)

TRANSFER_FAILED_MESSAGE: str = (
    "⚠️ No pudimos completar tu transferencia.\n\n"
    "Ocurrió un error inesperado al procesar la operación. No se realizó ningún "
    "movimiento en tu cuenta. Por favor, inténtalo de nuevo más tarde."
)

# Safe generic fallback used when an unknown ``messageType`` is received so the
# client always gets *some* closing message instead of silence.
GENERIC_FALLBACK_MESSAGE: str = (
    "ℹ️ Tu solicitud de transferencia finalizó.\n\n"
    "Si tienes dudas sobre el estado de la operación, por favor inténtalo de "
    "nuevo o contacta al banco."
)


def build_success_message(receipt: dict[str, Any]) -> str:
    """Build the Spanish comprobante message for ``transfer_success``.

    The ``sourceAccount`` and ``destinationAccount`` in ``receipt`` are already
    masked by ``execute.py`` — they are used as-is (no double masking). The
    ``amount`` is formatted as COP via :func:`format_cop`.

    Args:
        receipt: The receipt payload (Requirement 8.12 fields).

    Returns:
        The fully formatted, multi-line Spanish receipt message including the
        "información referencial" disclaimer.
    """
    transaction_id = receipt.get("transactionId", "")
    source_account = receipt.get("sourceAccount", "")
    destination_account = receipt.get("destinationAccount", "")
    amount = receipt.get("amount", 0)
    concept = receipt.get("concept", "")
    executed_at = receipt.get("executedAt", "")
    status = receipt.get("status", "COMPLETED")

    lines = [
        "✅ ¡Transferencia exitosa!",
        "",
        "Comprobante de tu transferencia BRE-B:",
        f"• Número de transacción: {transaction_id}",
        f"• Cuenta origen: {source_account}",
        f"• Cuenta destino: {destination_account}",
        f"• Monto: {format_cop(amount)}",
    ]
    if concept:
        lines.append(f"• Concepto: {concept}")
    lines.extend(
        [
            f"• Fecha: {executed_at}",
            f"• Estado: {status}",
            "",
            REFERENTIAL_DISCLAIMER,
        ]
    )
    return "\n".join(lines)


def _build_message(message_type: str, event: dict[str, Any]) -> str:
    """Route ``message_type`` to its Spanish message body.

    Args:
        message_type: One of the supported terminal ``messageType`` values.
        event: The full state input (used to read ``receipt`` for the success
            case).

    Returns:
        The Spanish message body to deliver. Unknown types yield
        :data:`GENERIC_FALLBACK_MESSAGE` (a warning is logged by the caller).
    """
    if message_type == "transfer_success":
        return build_success_message(event.get("receipt") or {})
    if message_type == "validation_failed":
        return VALIDATION_FAILED_MESSAGE
    if message_type == "otp_expired":
        return OTP_EXPIRED_MESSAGE
    if message_type == "otp_blocked":
        return OTP_BLOCKED_MESSAGE
    if message_type == "transfer_failed":
        return TRANSFER_FAILED_MESSAGE
    return GENERIC_FALLBACK_MESSAGE


# Module-level Twilio client, constructed lazily and reused across warm
# invocations (see :func:`_get_twilio_client`).
_twilio_client: Any = None


def _get_twilio_client() -> Any:
    """Construct (once) and return the Twilio REST client.

    Built lazily on first send so the module imports cleanly without Twilio
    credentials configured (tests monkeypatch this function). Credentials come
    from ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN``.

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
    """Return a ``whatsapp:``-prefixed address for a bare or prefixed number."""
    normalized = phone_number.strip()
    if normalized.startswith("whatsapp:"):
        return normalized
    return f"whatsapp:{normalized}"


def _send_whatsapp_message(phone_number: str, body: str) -> None:
    """Deliver ``body`` to ``phone_number`` via the Twilio REST API."""
    client = _get_twilio_client()
    from_address = _whatsapp_address(os.environ["TWILIO_WHATSAPP_NUMBER"])
    to_address = _whatsapp_address(phone_number)
    client.messages.create(from_=from_address, to=to_address, body=body)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Send the terminal-state Spanish notification to the client.

    Args:
        event: The state input. Expected keys: ``phoneNumber`` (required),
            ``messageType`` (required), ``receipt`` (optional, for
            ``transfer_success``), ``error`` (optional, for failure states),
            ``correlationId`` (optional, for log correlation).
        context: Lambda context object (unused beyond logging).

    Returns:
        ``{"messageType": <type>, "delivered": True}`` on success.
    """
    phone_number = event["phoneNumber"]
    message_type = event["messageType"]
    correlation_id = event.get("correlationId")

    # Hydrate Twilio credentials from the Twilio secret (TWILIO_SECRET_ARN).
    hydrate_twilio_env()

    if correlation_id:
        logger.append_keys(correlation_id=correlation_id)

    known_types = {
        "transfer_success",
        "validation_failed",
        "otp_expired",
        "otp_blocked",
        "transfer_failed",
    }
    if message_type not in known_types:
        # Never log full account numbers / raw error payloads; mask the phone
        # and surface only a masked account from the error when present.
        logger.warning(
            "unknown messageType; sending generic fallback",
            extra={
                "phone": mask_phone(phone_number),
                "messageType": message_type,
            },
        )

    body = _build_message(message_type, event)
    _send_whatsapp_message(phone_number, body)

    logger.info(
        "notification sent",
        extra={
            "phone": mask_phone(phone_number),
            "messageType": message_type,
        },
    )

    return {"messageType": message_type, "delivered": True}


__all__ = [
    "REFERENTIAL_DISCLAIMER",
    "VALIDATION_FAILED_MESSAGE",
    "OTP_EXPIRED_MESSAGE",
    "OTP_BLOCKED_MESSAGE",
    "TRANSFER_FAILED_MESSAGE",
    "GENERIC_FALLBACK_MESSAGE",
    "build_success_message",
    "handler",
]
