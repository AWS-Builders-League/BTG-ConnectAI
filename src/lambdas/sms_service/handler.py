"""SMS_Service Lambda — main handler (async, SQS triggered).

This Lambda is the consumer side of the SMS notification pattern (design §7,
Requirements 17.2, 17.8). It is wired to the ``sms-notification-queue`` (owned
by ``infra``) through an SQS Event Source Mapping configured with ``batchSize=10``,
``maximumBatchingWindowInSeconds=5`` and ``reportBatchItemFailures=true`` so a
single bad message is retried on its own without re-driving the whole batch
(Requirement 17.3, mirrored from Email_Service).

Relationship to OTP_Service
---------------------------
This service is **independent of OTP_Service**. OTP delivery is *synchronous*
inside the ``TransferBrebStateMachine`` workflow (it runs under the
``waitForTaskToken`` integration and the state machine stays suspended until the
client replies). This SMS_Service, by contrast, only delivers *asynchronous,
post-operation* confirmations: the ``PublishNotifications`` state fires an event
onto the queue and moves on. The two share the Pinpoint send shape but serve
different stages of the flow.

Per-message pipeline (:func:`record_handler`)
---------------------------------------------
1. Parse the SQS body into an :class:`~shared.types.SmsNotificationEvent` and
   bind the event's ``correlationId`` to the logger — the id is propagated from
   the state machine, never regenerated, so a transfer stays traceable from
   WhatsApp through to the confirmation SMS (Requirements 13.1, 13.2).
2. Route on ``type``:
   * ``transfer_confirmation`` → :func:`send_transfer_confirmation_sms`, which
     builds a concise Spanish confirmation (amount + masked destination) and
     sends it via AWS Pinpoint (Requirement 17.2).
   * any other (unknown) ``type`` → logged as a warning and **skipped**. See
     "Unknown event types" below.

Failure semantics (Requirement 17.5 / 17.7)
-------------------------------------------
:func:`record_handler` does **not** swallow send/transport errors: an exception
(e.g. a Pinpoint ``ClientError``) propagates to Powertools'
:func:`process_partial_response`, which reports that record as a
``batchItemFailure``. SQS then redelivers *only* that message; after
``maxReceiveCount`` (3) attempts it is moved to ``sms-dlq`` automatically.
Because the producer is fire-and-forget, none of this ever reaches the WhatsApp
flow (Requirement 17.7).

Unknown event types
-------------------
An event whose ``type`` is not handled is a *malformed/poison* message, not a
transient transport failure. Raising on it would make SQS redeliver it three
times and then DLQ it, burning retries on something that can never succeed. We
therefore log a warning and acknowledge (skip) the record so the queue is not
stuck looping a message no retry will fix. This is the same poison-message
decision taken by Email_Service. (Transport failures of *known* types still
raise and are retried/DLQ'd as above.)

Data masking (Requirement 17.6)
-------------------------------
The ``destinationAccount`` carried on the event is already masked by the
producer (``transfer-breb-execute``). As defence in depth it is passed through
:func:`~shared.masking.mask_account` again before it is rendered into the SMS
body or logged; the masking is idempotent on an already-masked value, so a
double application is a no-op.

Environment / cross-stack contract
-----------------------------------
* ``PINPOINT_APP_ID`` — the Pinpoint (project) application id used to send.
* ``PINPOINT_ORIGINATION_NUMBER`` — optional dedicated origination number.
* ``PINPOINT_SENDER_ID`` — optional alphanumeric sender id.

All are read lazily inside the send so the module imports cleanly without the
environment configured.
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
from shared.types import SmsNotificationEvent

logger = get_logger("sms-service")

# Powertools batch processor for SQS. With ``reportBatchItemFailures=true`` on
# the event source mapping, records whose handler raises are reported as
# ``batchItemFailure`` and retried individually (Requirement 17.5).
processor = BatchProcessor(event_type=EventType.SQS)

# Module-level Pinpoint client reused across warm invocations (boto3 clients are
# thread-safe and connection-pooled).
_pinpoint_client = boto3.client("pinpoint")

# Pinpoint message type. A transfer confirmation is transactional (not
# promotional): it is triggered by a client-initiated banking operation and must
# not be throttled or subject to opt-out rules the way promotional traffic is.
SMS_MESSAGE_TYPE: str = "TRANSACTIONAL"

# Environment variable names (cross-stack contract with ``infra``).
PINPOINT_APP_ID_ENV: str = "PINPOINT_APP_ID"
PINPOINT_ORIGINATION_NUMBER_ENV: str = "PINPOINT_ORIGINATION_NUMBER"
PINPOINT_SENDER_ID_ENV: str = "PINPOINT_SENDER_ID"

# Concise Spanish post-operation confirmation body (Requirement 17.2). Kept short
# to fit a single SMS segment: it identifies the bank, the transferred amount
# (COP-formatted) and the masked destination account.
SMS_BODY_TEMPLATE: str = (
    "BTG Pactual: tu transferencia de {amount} a la cuenta {destination} "
    "fue procesada exitosamente."
)


def send_transfer_confirmation_sms(
    phone_number: str,
    amount: Any,
    destination_account: str,
) -> None:
    """Build and send the transfer-confirmation SMS via AWS Pinpoint.

    Renders a concise Spanish confirmation with the COP-formatted amount and the
    masked destination account, then dispatches it through Pinpoint
    ``send_messages``. The destination account is re-masked defensively even
    though the producer already masks it (the masking is idempotent;
    Requirement 17.6).

    Args:
        phone_number: Destination phone number in E.164 format.
        amount: The transferred amount (formatted with :func:`format_cop`).
        destination_account: The destination account (already masked upstream;
            re-masked here as defence in depth).

    Raises:
        KeyError: If ``PINPOINT_APP_ID`` is not configured.
        botocore.exceptions.ClientError: If the Pinpoint ``send_messages`` call
            fails. The exception propagates so SQS retries the message
            (Requirement 17.5 / 17.7).
    """
    masked_destination = mask_account(destination_account)
    body = SMS_BODY_TEMPLATE.format(
        amount=format_cop(amount),
        destination=masked_destination,
    )

    sms_message: dict[str, Any] = {
        "Body": body,
        "MessageType": SMS_MESSAGE_TYPE,
    }

    origination_number = os.environ.get(PINPOINT_ORIGINATION_NUMBER_ENV)
    if origination_number:
        sms_message["OriginationNumber"] = origination_number

    sender_id = os.environ.get(PINPOINT_SENDER_ID_ENV)
    if sender_id:
        sms_message["SenderId"] = sender_id

    logger.info(
        "sending transfer confirmation sms",
        extra={"destination": masked_destination},
    )

    _pinpoint_client.send_messages(
        ApplicationId=os.environ[PINPOINT_APP_ID_ENV],
        MessageRequest={
            "Addresses": {phone_number: {"ChannelType": "SMS"}},
            "MessageConfiguration": {"SMSMessage": sms_message},
        },
    )


def record_handler(record: Any) -> None:
    """Process a single SQS record (one notification event).

    Parses the body into an :class:`~shared.types.SmsNotificationEvent`, binds
    the propagated ``correlationId`` to the logger, and routes on ``type``. An
    unknown type is logged and skipped (see module docstring "Unknown event
    types"); a ``transfer_confirmation`` is sent via Pinpoint. Transport errors
    are *not* caught: they propagate to :func:`process_partial_response` so the
    record is retried individually as a ``batchItemFailure``
    (Requirement 17.5 / 17.7).

    Args:
        record: The Powertools ``SQSRecord``. Its ``body`` is the JSON the state
            machine enqueued.
    """
    event: SmsNotificationEvent = json.loads(record.body)

    # Propagate the correlation id minted upstream — never regenerate it (Req 13.2).
    correlation_id = event.get("correlationId")
    if correlation_id:
        logger.append_keys(correlation_id=correlation_id)

    event_type = event.get("type")
    if event_type == "transfer_confirmation":
        send_transfer_confirmation_sms(
            event["phoneNumber"],
            event.get("amount"),
            event.get("destinationAccount", ""),
        )
        return

    # Unknown type: poison message — log and skip rather than loop retries/DLQ.
    logger.warning(
        "skipping sms notification with unknown type",
        extra={"eventType": event_type},
    )


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point — process the SQS batch with partial-failure reporting.

    Delegates to Powertools' :func:`process_partial_response`, which runs
    :func:`record_handler` per record and returns ``{"batchItemFailures": [...]}``
    for any records that raised, so SQS retries only those messages
    (Requirement 17.5).

    Args:
        event: The SQS event delivered by the ``sms-notification-queue`` event
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
    "SMS_MESSAGE_TYPE",
    "SMS_BODY_TEMPLATE",
    "PINPOINT_APP_ID_ENV",
    "PINPOINT_ORIGINATION_NUMBER_ENV",
    "PINPOINT_SENDER_ID_ENV",
    "send_transfer_confirmation_sms",
    "record_handler",
    "handler",
]
