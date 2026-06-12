"""Message_Processor Lambda — main handler (async, SQS FIFO triggered).

This is the orchestrator for an inbound WhatsApp message. The Webhook_Receiver
already answered Twilio with ``200 OK`` and enqueued the message on the inbound
SQS FIFO queue, so this Lambda runs *without* Twilio-side time pressure and does
all the heavy lifting (design §2 Message_Processor, Requirements 3.5, 3.6).

Per-message pipeline (:func:`record_handler`)
---------------------------------------------
1. Parse the SQS body into a :class:`~shared.types.TwilioWebhookPayload` and bind
   the message's own ``correlationId`` to the logger. The id is **never
   regenerated** here — it flows from the Webhook_Receiver so a single request is
   traceable end-to-end (Requirements 13.1, 13.2).
2. **OTP callback (priority)** — if an active OTP challenge exists for the phone
   number, the message is treated as an OTP code attempt and the Strands Agent is
   *not* invoked (design §record_handler step 1; Req 16.4 wiring).
3. **Consent gate** — no service runs without accepted Terms & Conditions; an
   un-consented message is routed to the consent flow and processing stops
   (Requirement 1.x, consent-first principle).
4. **Message-type routing** — quick-reply button payload, audio note
   (transcribed via Amazon Transcribe), plain text, or unsupported media. Audio
   that fails to transcribe and unsupported formats get a Spanish error reply and
   stop (Requirements 2.1, 2.4, 2.5).
5. **Auth gate** — a banking request without an active Auth_Session stores the
   original request and sends a login link, then stops (Requirement 5.1).
6. **Strands Agent invocation** — the derived (deterministic) session id and the
   input text are sent to the Strands_Agent Lambda; the response is delivered
   back to the client over Twilio, including a statement PDF attachment when the
   agent returned one (Requirements 3.7, 9.4).

Failure semantics (Requirement 3.9)
------------------------------------
:func:`record_handler` does **not** swallow unexpected errors. Any exception
propagates to Powertools' :func:`process_partial_response`, which reports the
record as a ``batchItemFailure`` so SQS retries *only* that message
(``reportBatchItemFailures=true``); after ``maxReceiveCount`` it lands in the
DLQ. The reply-and-return branches (consent, transcription failure, unsupported
format, auth required, OTP callback) are normal terminal outcomes, not failures.

Strands_Agent response contract
-------------------------------
The Strands_Agent Lambda (Task 13.x) does not exist yet; :func:`invoke_strands_agent`
defines the contract this handler expects. The agent Lambda returns a JSON
payload ``{"response": <value>}`` where ``<value>`` is **either**:

* a plain ``str`` — the text to send to the client (no attachment), **or**
* an object ``{"text": <str>, "statement": {"s3Bucket", "s3Key", "fileName"}}`` —
  a text body plus an optional statement PDF reference produced by the
  ``statement-generator`` Action Group (design line 93: ``{s3Bucket, s3Key,
  fileName}``).

:func:`invoke_strands_agent` returns that ``<value>`` (``str`` or ``dict``);
:func:`extract_statement_info` and :func:`remove_statement_metadata` normalize it
into an optional attachment and the text body. Both ``camelCase`` (the on-wire
convention) and ``snake_case`` keys are tolerated.

Environment / cross-stack contract
-----------------------------------
* ``AI_AGENT_FUNCTION_NAME`` — name/ARN of the Strands_Agent Lambda to invoke
  (resolved from the cross-stack contract / SSM). Read lazily so the module
  imports cleanly without the environment configured.
* DynamoDB table names, the Audio_Temp bucket and Twilio credentials are read by
  the delegate modules (``consent``, ``auth``, ``transcription``, ``messaging``,
  ``otp_callback``), each lazily.
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

from shared.logger import get_logger
from shared.types import TwilioWebhookPayload

from . import auth, consent, messaging, otp_callback, transcription

logger = get_logger("message-processor")

# Powertools batch processor for SQS. With ``reportBatchItemFailures=true`` on
# the event source mapping, records whose handler raises are reported as
# ``batchItemFailure`` and retried individually (Requirement 3.9).
processor = BatchProcessor(event_type=EventType.SQS)

# Module-level Lambda client reused across warm invocations (used to invoke the
# Strands_Agent Lambda).
_lambda_client = boto3.client("lambda")

# Environment variable holding the Strands_Agent Lambda name/ARN.
AGENT_FUNCTION_NAME_ENV: str = "AI_AGENT_FUNCTION_NAME"

# Spanish error replies (design §Error Response Templates). Kept local because no
# shared error catalogue module exists; ``messaging`` only exposes the welcome
# copy and ``consent`` owns the consent-required copy.
ERROR_MESSAGES: dict[str, str] = {
    "unsupported_format": (
        "👋 Solo acepto mensajes de texto y notas de voz. Escríbeme o envíame un "
        "audio con tu consulta."
    ),
    "transcription_failed": (
        "🎙️ No pude procesar tu nota de voz. Por favor intenta enviarla de nuevo "
        "o escríbeme tu consulta como texto."
    ),
}


def _is_audio_message(payload: TwilioWebhookPayload) -> bool:
    """Return whether ``payload`` is a WhatsApp audio note (voice message).

    An audio note has at least one media part (``NumMedia != "0"``) whose first
    content type is an ``audio/*`` MIME type (WhatsApp ships OGG/Opus). Any other
    media kind (image, video, sticker, document, location) is *not* audio and is
    handled as an unsupported format (Requirement 2.5).

    Args:
        payload: The parsed inbound Twilio webhook payload.

    Returns:
        ``True`` if the message carries an audio note, ``False`` otherwise.
    """
    return (
        payload.get("NumMedia", "0") != "0"
        and payload.get("MediaContentType0", "").startswith("audio/")
    )


def invoke_strands_agent(
    session_id: str, input_text: str, phone_number: str
) -> str | dict[str, Any]:
    """Invoke the Strands_Agent Lambda and return its response value.

    Performs a synchronous ``RequestResponse`` invocation of the agent Lambda
    (name/ARN from :data:`AGENT_FUNCTION_NAME_ENV`), passing the deterministic
    conversational ``sessionId`` (so Bedrock Agent session memory is preserved
    across messages), the ``inputText`` and the ``phoneNumber``.

    The agent Lambda is expected to return a JSON payload ``{"response": <value>}``
    where ``<value>`` is a plain ``str`` (text only) or an object with ``text``
    plus an optional ``statement`` reference (see the module docstring). This
    function returns that ``<value>`` unchanged. If the payload is not wrapped in
    a ``response`` key, the parsed payload itself is returned (tolerant parsing).

    Args:
        session_id: Deterministic conversational session id for the client.
        input_text: The text to send to the agent (typed, transcribed, or a
            button payload).
        phone_number: The client's phone number (E.164).

    Returns:
        The agent's response value: a ``str`` (text only) or a ``dict`` (text +
        optional statement reference).

    Raises:
        KeyError: If ``AI_AGENT_FUNCTION_NAME`` is not configured.
        RuntimeError: If the invoked agent Lambda reported a function error. The
            exception propagates so SQS retries the whole message (Req 3.9).
    """
    function_name = os.environ[AGENT_FUNCTION_NAME_ENV]

    invoke_response = _lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(
            {
                "sessionId": session_id,
                "inputText": input_text,
                "phoneNumber": phone_number,
            }
        ).encode("utf-8"),
    )

    raw_payload = invoke_response["Payload"].read()

    # A function-level error in the agent must not be silently treated as a valid
    # response — propagate so the message is retried via SQS (Requirement 3.9).
    if invoke_response.get("FunctionError"):
        logger.error(
            "strands agent returned a function error",
            extra={"functionError": invoke_response["FunctionError"]},
        )
        raise RuntimeError(
            f"Strands_Agent invocation failed: {raw_payload.decode('utf-8', 'replace')}"
        )

    result = json.loads(raw_payload)
    if isinstance(result, dict) and "response" in result:
        return result["response"]
    return result


def extract_statement_info(response: str | dict[str, Any] | None) -> dict[str, str] | None:
    """Extract an optional statement-PDF reference from the agent response.

    Looks for a ``statement`` object carrying both an S3 bucket and key. Tolerates
    the on-wire ``camelCase`` keys (``s3Bucket``/``s3Key``/``fileName``) and their
    ``snake_case`` equivalents, and normalizes them to the snake_case shape the
    :func:`messaging.send_twilio_document` caller uses.

    A plain-string response (text only) or any response without a complete
    statement reference yields ``None`` (no attachment to send).

    Args:
        response: The agent response value (``str``, ``dict`` or ``None``).

    Returns:
        ``{"s3_bucket", "s3_key", "file_name"}`` when a complete statement
        reference is present, otherwise ``None``.
    """
    if not isinstance(response, dict):
        return None

    statement = response.get("statement")
    if not isinstance(statement, dict):
        return None

    bucket = statement.get("s3Bucket") or statement.get("s3_bucket")
    key = statement.get("s3Key") or statement.get("s3_key")
    if not bucket or not key:
        return None

    return {
        "s3_bucket": bucket,
        "s3_key": key,
        "file_name": statement.get("fileName") or statement.get("file_name") or "",
    }


def remove_statement_metadata(response: str | dict[str, Any] | None) -> str:
    """Return the plain text body of the agent response (without attachment data).

    For a string response the string is returned verbatim. For an object response
    the text body is taken from ``text`` (preferred), then ``response``/``message``
    as fallbacks. Anything else yields an empty string so the caller skips sending
    an empty Twilio message.

    Args:
        response: The agent response value (``str``, ``dict`` or ``None``).

    Returns:
        The text to deliver to the client (possibly empty).
    """
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        text = response.get("text") or response.get("response") or response.get("message")
        return text if isinstance(text, str) else ""
    return ""


def record_handler(record: Any) -> None:
    """Process a single inbound SQS record (one WhatsApp message).

    Implements the per-message pipeline documented at module level. Each terminal
    branch (OTP callback, consent, transcription failure, unsupported format, auth
    required) replies to the client as appropriate and returns. Unexpected errors
    are *not* caught here: they propagate to :func:`process_partial_response` so
    the record is retried individually as a ``batchItemFailure`` (Requirement 3.9).

    Args:
        record: The Powertools ``SQSRecord`` for the message. Its ``body`` is the
            JSON the Webhook_Receiver enqueued.
    """
    payload: TwilioWebhookPayload = json.loads(record.body)

    # Use the correlation id minted by the Webhook_Receiver — never regenerate it
    # (Requirement 13.2): one request stays traceable across every Lambda.
    logger.append_keys(correlation_id=payload["correlationId"])

    phone_number = payload["From"].replace("whatsapp:", "")  # bare E.164

    # 1. OTP callback has priority: an active challenge means this message is an
    #    OTP code attempt — do not invoke the agent.
    pending_otp = otp_callback.get_pending_otp(phone_number)
    if pending_otp:
        otp_callback.handle_otp_callback(phone_number, payload.get("Body", ""), pending_otp)
        return

    # 2. Consent gate (consent-first): nothing runs without accepted T&C.
    consent_record = consent.get_consent(phone_number)
    if not consent.has_accepted_consent(consent_record):
        consent.handle_consent_flow(payload, consent_record, phone_number)
        return

    # 3. Determine the input text from the message type.
    if payload.get("ButtonPayload"):
        input_text = payload["ButtonPayload"]
    elif _is_audio_message(payload):
        transcribed = transcription.transcribe_audio(payload["MediaUrl0"], phone_number)
        if not transcribed:
            # Transcription failed or produced empty text (Requirement 2.4).
            messaging.send_twilio_message(phone_number, ERROR_MESSAGES["transcription_failed"])
            return
        input_text = transcribed
    elif payload.get("Body", "").strip():
        input_text = payload["Body"].strip()
    else:
        # Image, video, sticker, document, location, or an empty body
        # (Requirement 2.5).
        messaging.send_twilio_message(phone_number, ERROR_MESSAGES["unsupported_format"])
        return

    # 4. Auth gate: a banking request without an active session triggers a login
    #    prompt and stores the original request to resume after login (Req 5.1).
    auth_session = auth.get_auth_session(phone_number)
    if not auth_session or auth.is_expired(auth_session):
        auth.store_pending_request(phone_number, input_text)
        auth.send_login_link(phone_number)
        return

    # 5. Invoke the Strands_Agent with the deterministic conversational session.
    session_id = auth.derive_session_id(phone_number)
    response = invoke_strands_agent(session_id, input_text, phone_number)

    # 6. Statement PDF (if the agent produced one) is sent as a document
    #    attachment via Twilio (Requirement 9.4).
    statement_info = extract_statement_info(response)
    if statement_info:
        messaging.send_twilio_document(
            phone_number,
            statement_info["s3_bucket"],
            statement_info["s3_key"],
        )

    # 7. Send the text response (split into <=1600-char chunks by messaging).
    text_response = remove_statement_metadata(response)
    if text_response.strip():
        messaging.send_twilio_message(phone_number, text_response)


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point — process the SQS batch with partial-failure reporting.

    Delegates to Powertools' :func:`process_partial_response`, which runs
    :func:`record_handler` per record and returns the
    ``{"batchItemFailures": [...]}`` structure for any records that raised
    (Requirement 3.9). The event source mapping uses ``batchSize=1``, but partial
    reporting works for any batch size.

    Args:
        event: The SQS event delivered by the inbound FIFO event source mapping.
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
    "AGENT_FUNCTION_NAME_ENV",
    "ERROR_MESSAGES",
    "invoke_strands_agent",
    "extract_statement_info",
    "remove_statement_metadata",
    "record_handler",
    "handler",
]
