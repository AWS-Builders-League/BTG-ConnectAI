"""OTP_Service Lambda — ``generate-and-wait`` handler (Requirement 16.1–16.3).

The ``TransferBrebStateMachine`` Step Functions workflow reaches the
``GenerateOTP`` state and invokes this Lambda with the
``arn:aws:states:::lambda:invoke.waitForTaskToken`` integration pattern, passing
``$$.Task.Token`` in the payload (Requirement 16.1). The event shape is::

    {
        "operation": "generate-and-wait",
        "phoneNumber": "+573001234567",
        "clientEmail": "user@example.com",
        "transferAmount": 150000,
        "destinationAccount": "1021803076",
        "taskToken": "<long Step Functions task token>",
        "executionArn": "arn:aws:states:..."   # optional (audit)
    }

What the handler does (Requirement 16.2 / 16.3):

1. Generates a cryptographically-random 6-digit numeric code (``secrets``).
2. Persists an :class:`~shared.types.OTPRecord` in the ``OTP_Store`` DynamoDB
   table (owned by the ``infra`` repo, name resolved from the cross-stack
   contract via the ``OTP_TABLE_NAME`` environment variable):
   ``{pk, code, taskToken, executionArn, attempts: 0, transferContext,
   createdAt, ttl: now + 300}``. The ``pk`` is the bare E.164 phone number and
   ``attempts`` starts at ``0`` — this is exactly the shape the Message_Processor
   ``otp_callback`` module reads back to validate the code.
3. Sends the code to the client by **email** through Resend API with a message
   that identifies the operation: the transfer amount (COP-formatted) and the
   **masked** destination account.
4. Returns ``{"ok": True}`` immediately. The Lambda terminates while Step
   Functions stays suspended waiting for the callback (``SendTaskSuccess`` /
   ``SendTaskFailure``) driven by the Message_Processor — zero Lambda compute is
   billed while waiting (``HeartbeatSeconds=300``).

Security / privacy:

* The OTP code is **never** logged. The phone number and destination account are
  masked (:func:`shared.masking.mask_phone` / :func:`shared.masking.mask_account`)
  wherever they appear in logs.
* The code is generated with :mod:`secrets` (CSPRNG), not :mod:`random`.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any

import boto3
import resend

from shared.constants import OTP_TTL
from shared.formatting import format_cop
from shared.logger import get_logger
from shared.masking import mask_account, mask_phone
from shared.types import OTPRecord

logger = get_logger("otp-service")

# Number of digits in the generated OTP code (Requirement 16.2). The
# Message_Processor compares the client's reply against this code verbatim.
OTP_CODE_LENGTH: int = 6

# Validity window communicated to the client, in minutes. Derived from OTP_TTL
# (seconds) so the SMS copy always matches the record's actual TTL.
_OTP_VALID_MINUTES: int = OTP_TTL // 60

# Module-level resources/clients are created once and reused across warm Lambda
# invocations (boto3 clients are thread-safe and connection-pooled).
_dynamodb = boto3.resource("dynamodb")
_secrets_client = boto3.client("secretsmanager")

# Cached Resend config (loaded on first invocation from Secrets Manager).
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


def _get_otp_table():
    """Return the ``OTP_Store`` DynamoDB table handle.

    The table name is read lazily from ``OTP_TABLE_NAME`` so the module imports
    cleanly without the environment configured (tests set it before invoking).

    Raises:
        KeyError: If ``OTP_TABLE_NAME`` is not configured.
    """
    return _dynamodb.Table(os.environ["OTP_TABLE_NAME"])


def _normalize_phone(phone_number: str) -> str:
    """Strip the ``whatsapp:`` channel prefix so the key is a bare E.164 number.

    Kept consistent with the Message_Processor (``otp_callback._normalize_phone``)
    so the OTP_Store partition key written here matches the key read back when
    the client replies with the code.
    """
    return (phone_number or "").replace("whatsapp:", "").strip()


def _now() -> datetime:
    """Return the current UTC time (isolated for testability)."""
    return datetime.now(timezone.utc)


def _generate_code() -> str:
    """Generate a zero-padded 6-digit OTP using a CSPRNG (Requirement 16.2).

    Uses :func:`secrets.randbelow` so every value in ``000000``–``999999`` is
    equally likely and the code is not predictable from prior outputs.
    """
    upper_bound = 10**OTP_CODE_LENGTH
    return f"{secrets.randbelow(upper_bound):0{OTP_CODE_LENGTH}d}"


def _build_otp_email_html(
    code: str, transfer_amount: Any, destination_account: str
) -> str:
    """Build the HTML body for the OTP email."""
    masked_dest = mask_account(destination_account)
    amount_str = format_cop(transfer_amount)

    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f5;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;max-width:600px;">
        <tr><td style="background:#002b5c;padding:24px;color:#ffffff;font-size:20px;font-weight:700;">BTG Pactual</td></tr>
        <tr><td style="padding:32px 24px;">
          <p style="margin:0 0 16px;color:#111;font-size:16px;">Tu código de autorización es:</p>
          <p style="margin:0 0 24px;text-align:center;">
            <span style="display:inline-block;background:#f0f4ff;border:2px solid #002b5c;border-radius:8px;padding:16px 32px;font-size:32px;font-weight:700;letter-spacing:8px;color:#002b5c;">{code}</span>
          </p>
          <p style="margin:0 0 12px;color:#333;font-size:14px;">
            Transferencia de <strong>{amount_str}</strong> a la cuenta <strong>{masked_dest}</strong>.
          </p>
          <p style="margin:0 0 12px;color:#333;font-size:14px;">
            Válido por <strong>{_OTP_VALID_MINUTES} minutos</strong>. No lo compartas con nadie.
          </p>
          <p style="margin:24px 0 0;color:#888;font-size:12px;">Si no solicitaste esta transferencia, ignora este correo.</p>
        </td></tr>
        <tr><td style="background:#fafafa;padding:16px 24px;color:#aaa;font-size:11px;">© BTG Pactual</td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _build_otp_email_text(
    code: str, transfer_amount: Any, destination_account: str
) -> str:
    """Build the plain-text body for the OTP email."""
    masked_dest = mask_account(destination_account)
    amount_str = format_cop(transfer_amount)
    return (
        f"BTG Pactual — Código de autorización\n\n"
        f"Tu código es: {code}\n\n"
        f"Transferencia de {amount_str} a la cuenta {masked_dest}.\n"
        f"Válido por {_OTP_VALID_MINUTES} minutos. No lo compartas con nadie.\n\n"
        f"Si no solicitaste esta transferencia, ignora este correo.\n"
    )


def _send_otp_email(
    to_email: str, code: str, transfer_amount: Any, destination_account: str
) -> None:
    """Send the OTP code to the client by email via Resend.

    Args:
        to_email: The recipient email address.
        code: The 6-digit OTP code.
        transfer_amount: Amount being transferred.
        destination_account: Destination account (will be masked).
    """
    from_email = _configure_resend()

    params: resend.Emails.SendParams = {
        "from": from_email,
        "to": [to_email],
        "subject": f"BTG Pactual — Código de autorización: {code}",
        "html": _build_otp_email_html(code, transfer_amount, destination_account),
        "text": _build_otp_email_text(code, transfer_amount, destination_account),
    }

    resend.Emails.send(params)


def handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    """Generate an OTP, persist it with the task token, and send it by email.

    Implements the ``generate-and-wait`` operation (Requirement 16.1–16.3). The
    handler is invoked by Step Functions with the task token in the payload;
    after persisting the record and dispatching the email it returns immediately
    so the state machine stays suspended waiting for the callback.

    Args:
        event: The ``generate-and-wait`` payload (see the module docstring).
        context: Lambda context (unused; ``executionArn`` is taken from the
            event when present).

    Returns:
        ``{"ok": True}`` once the record is stored and the email is dispatched.

    Raises:
        ValueError: If ``phoneNumber`` or ``taskToken`` or ``clientEmail`` is
            missing — without these, the workflow cannot be resumed and the OTP
            is meaningless, so the task is failed loudly.
    """
    event = event or {}

    phone_number = _normalize_phone(event.get("phoneNumber", ""))
    client_email = (event.get("clientEmail") or "").strip()
    task_token = event.get("taskToken")
    transfer_amount = event.get("transferAmount")
    destination_account = event.get("destinationAccount", "")
    execution_arn = event.get("executionArn", "")

    if not phone_number:
        logger.error("otp generate: missing phoneNumber")
        raise ValueError("phoneNumber is required")
    if not task_token:
        logger.error("otp generate: missing taskToken", extra={"phone": mask_phone(phone_number)})
        raise ValueError("taskToken is required")
    if not client_email:
        logger.error("otp generate: missing clientEmail", extra={"phone": mask_phone(phone_number)})
        raise ValueError("clientEmail is required")

    code = _generate_code()
    now = _now()
    ttl = int(now.timestamp()) + OTP_TTL

    record: OTPRecord = {
        "pk": phone_number,
        "code": code,
        "taskToken": task_token,
        "executionArn": execution_arn,
        "attempts": 0,
        "transferContext": {
            "amount": transfer_amount,
            "destinationAccount": destination_account,
        },
        "createdAt": now.isoformat(),
        "ttl": ttl,
    }

    _get_otp_table().put_item(Item=record)
    logger.info(
        "otp persisted",
        extra={
            "phone": mask_phone(phone_number),
            "destination": mask_account(destination_account),
            "ttl": ttl,
            "hasExecutionArn": bool(execution_arn),
        },
    )

    _send_otp_email(client_email, code, transfer_amount, destination_account)
    logger.info(
        "otp email dispatched",
        extra={
            "phone": mask_phone(phone_number),
            "destination": mask_account(destination_account),
        },
    )

    # Return immediately. Step Functions stays suspended on the task token until
    # the Message_Processor resumes it via SendTaskSuccess / SendTaskFailure.
    return {"ok": True}


__all__ = [
    "handler",
    "OTP_CODE_LENGTH",
]
