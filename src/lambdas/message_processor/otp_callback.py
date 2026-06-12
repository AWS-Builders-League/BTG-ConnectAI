"""OTP callback / priority-routing module for the Message_Processor Lambda.

Implements the WhatsApp side of the BRE-B transfer OTP flow (Requirement 16,
criteria 16.4–16.7). The transfer itself is orchestrated by the
``TransferBrebStateMachine`` Step Functions workflow, which pauses at the
``GenerateOTP`` state using the ``waitForTaskToken`` integration pattern and
persists an :class:`~shared.types.OTPRecord` in the ``OTP_Store`` DynamoDB table
(owned by the ``infra`` repo, name resolved from the cross-stack contract via
the ``OTP_TABLE_NAME`` environment variable).

Priority routing (Requirement 16.4)
------------------------------------
When the Message_Processor consumes an inbound WhatsApp message it first calls
:func:`get_pending_otp`. If an *active* (non-expired) OTP record exists for the
phone number, this flow takes **priority over the Strands Agent** — the message
is treated as an OTP code attempt and the agent is not invoked.

Callback behaviour (Requirements 16.5, 16.6, 16.7)
--------------------------------------------------
:func:`validate_and_callback` resolves the inbound code against the pending
record:

* **No active / expired record** → ignore. The workflow is not resumed here;
  Step Functions handles the timeout itself via ``HeartbeatSeconds=300`` and the
  ``States.Timeout`` catch (Requirement 16, criterion 8 / Property 17).
* **Correct code** → ``send_task_success(taskToken, {"valid": true})`` to resume
  the workflow at ``ValidateOTP`` **and** ``delete_item`` the OTP record
  (Requirement 16.5).
* **Incorrect code, still below the block threshold** → ``update_item`` to
  increment ``attempts`` and send a "Código incorrecto" retry message via
  Twilio, without resuming the workflow (Requirement 16.6).
* **Incorrect code on the final (3rd) attempt** →
  ``send_task_failure(taskToken, error="OTPBlockedError")`` and ``delete_item``
  the record, so the workflow transitions to ``NotifyOTPBlocked``
  (Requirement 16.7 / Property 18).

Attempts / threshold semantics (Property 18 — Brute Force Block)
----------------------------------------------------------------
The OTP record starts at ``attempts == 0`` (written by the OTP_Service). The
block fires on the **3rd failed attempt** — i.e. after 3 wrong codes in total —
which is :data:`MAX_OTP_ATTEMPTS`. Because ``attempts`` counts *previously
recorded* failures, the decision on an incoming wrong code is:

* ``attempts >= MAX_OTP_ATTEMPTS - 1`` (i.e. ``attempts >= 2``, meaning 2 prior
  failures are already recorded and this is the 3rd) → **block**
  (``send_task_failure`` + delete).
* otherwise (``attempts < 2``, i.e. this is the 1st or 2nd wrong code) →
  increment ``attempts`` and send the retry message.

So the wrong-code sequence is: 1st → attempts 0→1 (retry), 2nd → attempts 1→2
(retry), 3rd → block. Exactly 3 failed attempts trigger the block.

Cross-module dependency
-----------------------
The retry message is sent through the shared messaging module
(``messaging.send_twilio_message``, Task 5.6). It is imported lazily inside
:func:`validate_and_callback` so this module imports cleanly even while the
messaging module is built in parallel and so Twilio credentials are not required
to import it.
"""

from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timezone

import boto3

from shared.logger import get_logger
from shared.masking import mask_phone
from shared.types import OTPRecord

logger = get_logger("message-processor")

# Total number of failed attempts that triggers the brute-force block
# (Requirement 16.7 / Property 18). The OTP record starts at attempts == 0, so
# the 3rd wrong code blocks the workflow.
MAX_OTP_ATTEMPTS: int = 3

# Retry message sent to the client after an incorrect (but not yet blocking) OTP
# code (Requirement 16.6).
INCORRECT_CODE_MESSAGE: str = (
    "Código incorrecto. Por favor verifica e ingresa nuevamente el código que te "
    "enviamos por SMS."
)

# Output handed back to the state machine when the code is valid (Requirement
# 16.5). Resumes the workflow at the ``ValidateOTP`` Choice state.
TASK_SUCCESS_OUTPUT: dict = {"valid": True}

# Error name caught by the state machine to transition to ``NotifyOTPBlocked``
# (Requirement 16.7).
OTP_BLOCKED_ERROR: str = "OTPBlockedError"

# Module-level resources/clients are reused across warm Lambda invocations.
_dynamodb = boto3.resource("dynamodb")
_stepfunctions = boto3.client("stepfunctions")


def _get_otp_table():
    """Return the OTP_Store DynamoDB table handle.

    The table name is read lazily from ``OTP_TABLE_NAME`` so the module can be
    imported without the environment configured — tests set the variable before
    invoking the functions.

    Raises:
        KeyError: If ``OTP_TABLE_NAME`` is not configured.
    """
    return _dynamodb.Table(os.environ["OTP_TABLE_NAME"])


def _normalize_phone(phone_number: str) -> str:
    """Strip the ``whatsapp:`` channel prefix so the key is a bare E.164 number.

    Kept consistent with ``consent.py`` / ``auth.py`` / ``messaging.py`` so the
    same client always maps to the same OTP_Store partition key regardless of
    whether the caller passed a ``whatsapp:``-prefixed value.
    """
    return phone_number.replace("whatsapp:", "").strip()


def _now_epoch() -> float:
    """Return the current UTC time as a unix epoch (isolated for testability)."""
    return datetime.now(timezone.utc).timestamp()


def _is_expired(record: OTPRecord) -> bool:
    """Return whether an OTP record is past its TTL.

    DynamoDB TTL deletion is *eventual* (an expired item can linger for up to
    48h after its ``ttl``), so the record must be treated as expired here as
    soon as ``ttl <= now`` even if DynamoDB has not yet removed it
    (Requirement 16, criterion 4 / Property 17: expired OTP is ignored).

    A record without a usable numeric ``ttl`` is treated as expired (fail
    closed) — it cannot be trusted to gate a financial operation.
    """
    ttl = record.get("ttl")
    if ttl is None:
        logger.warning("otp record missing ttl, treating as expired")
        return True
    try:
        return float(ttl) <= _now_epoch()
    except (TypeError, ValueError):
        logger.warning("otp record has non-numeric ttl, treating as expired")
        return True


def get_pending_otp(phone_number: str) -> OTPRecord | None:
    """Fetch the active OTP record for ``phone_number`` (Requirement 16.4).

    Reads the ``OTP_Store`` item by partition key and returns it only when it
    exists **and** has not expired. An expired record is reported as ``None`` so
    the caller never resumes a timed-out workflow from the WhatsApp side (Step
    Functions owns the timeout via ``HeartbeatSeconds``); see Property 17.

    Used by the Message_Processor for priority routing: a non-``None`` result
    means the inbound message must be handled as an OTP code attempt and the
    Strands Agent must not be invoked.

    Args:
        phone_number: The client's phone number (E.164, ``whatsapp:`` prefix
            optional).

    Returns:
        The active :class:`~shared.types.OTPRecord`, or ``None`` when no record
        exists or the record has expired.
    """
    pk = _normalize_phone(phone_number)
    response = _get_otp_table().get_item(Key={"pk": pk})
    item = response.get("Item")

    if item is None:
        logger.info("no pending otp", extra={"phone": mask_phone(pk)})
        return None

    record: OTPRecord = item  # type: ignore[assignment]

    if _is_expired(record):
        logger.info("pending otp expired, ignoring", extra={"phone": mask_phone(pk)})
        return None

    return record


def validate_and_callback(
    phone_number: str,
    code: str,
    pending_otp: OTPRecord | None = None,
) -> None:
    """Validate an inbound OTP code and drive the Step Functions callback.

    This is the OTP side of the priority-routing flow. Behaviour
    (Requirements 16.5, 16.6, 16.7):

    * **No active record / expired** → ignore (return without action). The
      workflow timeout is handled by Step Functions ``HeartbeatSeconds``.
    * **Correct code** → ``send_task_success(taskToken, {"valid": true})`` and
      delete the OTP record (Requirement 16.5).
    * **Incorrect code, below the block threshold** (this is the 1st or 2nd wrong
      code) → increment ``attempts`` and send a retry message via Twilio without
      resuming the workflow (Requirement 16.6).
    * **Incorrect code on the 3rd (final) attempt** →
      ``send_task_failure(taskToken, "OTPBlockedError")`` and delete the record
      (Requirement 16.7 / Property 18).

    See the module docstring for the precise attempts/threshold semantics.

    Args:
        phone_number: The client's phone number (E.164, ``whatsapp:`` prefix
            optional).
        code: The raw message body the client sent (treated as the OTP code;
            surrounding whitespace is ignored).
        pending_otp: The already-fetched active OTP record, if the caller
            resolved it (e.g. the main handler's priority-routing check). When
            ``None`` it is looked up via :func:`get_pending_otp`.
    """
    pk = _normalize_phone(phone_number)

    record = pending_otp if pending_otp is not None else get_pending_otp(pk)
    if record is None:
        # No active OTP challenge for this number — nothing to do. Step Functions
        # handles any timeout itself (Requirement 16, criterion 8 / Property 17).
        logger.info("no active otp to validate, ignoring", extra={"phone": mask_phone(pk)})
        return

    task_token = record["taskToken"]
    submitted = (code or "").strip()
    expected = str(record.get("code", ""))

    # Constant-time compare to avoid leaking timing information about the code.
    if hmac.compare_digest(submitted, expected):
        _stepfunctions.send_task_success(
            taskToken=task_token,
            output=json.dumps(TASK_SUCCESS_OUTPUT),
        )
        _get_otp_table().delete_item(Key={"pk": pk})
        logger.info("otp valid, workflow resumed", extra={"phone": mask_phone(pk)})
        return

    # Incorrect code. ``attempts`` is the count of *previously recorded*
    # failures. When 2 are already recorded this incoming wrong code is the 3rd
    # and final attempt → block (Requirement 16.7 / Property 18).
    attempts = int(record.get("attempts", 0) or 0)

    if attempts >= MAX_OTP_ATTEMPTS - 1:
        _stepfunctions.send_task_failure(
            taskToken=task_token,
            error=OTP_BLOCKED_ERROR,
        )
        _get_otp_table().delete_item(Key={"pk": pk})
        logger.warning(
            "otp blocked after max failed attempts",
            extra={"phone": mask_phone(pk), "attempts": attempts + 1},
        )
        return

    # 1st or 2nd wrong code — record the failure and ask the client to retry.
    # The workflow keeps waiting on the task token (no callback is sent).
    _get_otp_table().update_item(
        Key={"pk": pk},
        UpdateExpression="SET attempts = if_not_exists(attempts, :zero) + :inc",
        ExpressionAttributeValues={":inc": 1, ":zero": 0},
    )

    from .messaging import send_twilio_message  # lazy: avoids hard coupling

    send_twilio_message(pk, INCORRECT_CODE_MESSAGE)
    logger.info(
        "otp incorrect, retry requested",
        extra={"phone": mask_phone(pk), "attempts": attempts + 1},
    )


# Alias matching the name used by the Message_Processor main handler wiring
# (design §record_handler: ``handle_otp_callback(phone, body, pending_otp)``).
handle_otp_callback = validate_and_callback


__all__ = [
    "MAX_OTP_ATTEMPTS",
    "INCORRECT_CODE_MESSAGE",
    "OTP_BLOCKED_ERROR",
    "TASK_SUCCESS_OUTPUT",
    "get_pending_otp",
    "validate_and_callback",
    "handle_otp_callback",
]
