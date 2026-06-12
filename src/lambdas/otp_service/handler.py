"""OTP_Service Lambda — ``generate-and-wait`` handler (Requirement 16.1–16.3).

The ``TransferBrebStateMachine`` Step Functions workflow reaches the
``GenerateOTP`` state and invokes this Lambda with the
``arn:aws:states:::lambda:invoke.waitForTaskToken`` integration pattern, passing
``$$.Task.Token`` in the payload (Requirement 16.1). The event shape is::

    {
        "operation": "generate-and-wait",
        "phoneNumber": "+573001234567",
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
3. Sends the code to the client by SMS through AWS Pinpoint
   (``pinpoint.send_messages``) with a message that identifies the operation:
   the transfer amount (COP-formatted) and the **masked** destination account.
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

import os
import secrets
from datetime import datetime, timezone
from typing import Any

import boto3

from shared.constants import OTP_TTL
from shared.formatting import format_cop
from shared.logger import get_logger
from shared.masking import mask_account, mask_phone
from shared.types import OTPRecord

logger = get_logger("otp-service")

# Number of digits in the generated OTP code (Requirement 16.2). The
# Message_Processor compares the client's reply against this code verbatim.
OTP_CODE_LENGTH: int = 6

# Pinpoint message type. OTP delivery is transactional (not promotional): it is
# triggered by a client-initiated banking operation and must not be throttled or
# subject to opt-out rules the way promotional traffic is.
SMS_MESSAGE_TYPE: str = "TRANSACTIONAL"

# Validity window communicated to the client, in minutes. Derived from OTP_TTL
# (seconds) so the SMS copy always matches the record's actual TTL.
_OTP_VALID_MINUTES: int = OTP_TTL // 60

# SMS body template (Requirement 16.3): identifies the operation with the
# transfer amount and the masked destination account, and carries the code. The
# amount is pre-formatted as COP and the account is masked before formatting.
SMS_BODY_TEMPLATE: str = (
    "BTG Pactual: tu codigo de autorizacion es {code}. "
    "Transferencia de {amount} a la cuenta {destination}. "
    "Valido {minutes} minutos. No lo compartas con nadie."
)

# Module-level resources/clients are created once and reused across warm Lambda
# invocations (boto3 clients are thread-safe and connection-pooled).
_dynamodb = boto3.resource("dynamodb")
_pinpoint = boto3.client("pinpoint")


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


def _build_sms_body(transfer_amount: Any, destination_account: str) -> str:
    """Render the SMS body (Requirement 16.3): amount + masked destination + code.

    Note the ``{code}`` placeholder is filled by the caller so the code is never
    held in an intermediate variable that could be logged by accident.
    """
    return SMS_BODY_TEMPLATE.format(
        code="{code}",
        amount=format_cop(transfer_amount),
        destination=mask_account(destination_account),
        minutes=_OTP_VALID_MINUTES,
    )


def _send_sms(phone_number: str, body: str) -> None:
    """Send ``body`` to ``phone_number`` over SMS via AWS Pinpoint.

    Builds the standard Pinpoint ``send_messages`` request: the destination is an
    address keyed by the E.164 number with ``ChannelType: "SMS"``, and the
    payload is an ``SMSMessage`` with the transactional message type. An optional
    origination number / sender id is attached when configured via
    ``PINPOINT_ORIGINATION_NUMBER`` / ``PINPOINT_SENDER_ID``.

    Args:
        phone_number: Destination phone number in E.164 format.
        body: The fully-rendered SMS text (already contains the OTP code).
    """
    sms_message: dict[str, Any] = {
        "Body": body,
        "MessageType": SMS_MESSAGE_TYPE,
    }

    origination_number = os.environ.get("PINPOINT_ORIGINATION_NUMBER")
    if origination_number:
        sms_message["OriginationNumber"] = origination_number

    sender_id = os.environ.get("PINPOINT_SENDER_ID")
    if sender_id:
        sms_message["SenderId"] = sender_id

    _pinpoint.send_messages(
        ApplicationId=os.environ["PINPOINT_APP_ID"],
        MessageRequest={
            "Addresses": {phone_number: {"ChannelType": "SMS"}},
            "MessageConfiguration": {"SMSMessage": sms_message},
        },
    )


def handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    """Generate an OTP, persist it with the task token, and send it by SMS.

    Implements the ``generate-and-wait`` operation (Requirement 16.1–16.3). The
    handler is invoked by Step Functions with the task token in the payload;
    after persisting the record and dispatching the SMS it returns immediately so
    the state machine stays suspended waiting for the callback.

    Args:
        event: The ``generate-and-wait`` payload (see the module docstring).
        context: Lambda context (unused; ``executionArn`` is taken from the
            event when present).

    Returns:
        ``{"ok": True}`` once the record is stored and the SMS is dispatched.

    Raises:
        ValueError: If ``phoneNumber`` or ``taskToken`` is missing — without
            either, the workflow cannot be resumed and the OTP is meaningless,
            so the task is failed loudly rather than silently dropped.
    """
    event = event or {}

    phone_number = _normalize_phone(event.get("phoneNumber", ""))
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

    body = _build_sms_body(transfer_amount, destination_account).format(code=code)
    _send_sms(phone_number, body)
    logger.info(
        "otp sms dispatched",
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
    "SMS_MESSAGE_TYPE",
    "SMS_BODY_TEMPLATE",
]
