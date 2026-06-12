"""Webhook_Receiver Lambda — synchronous Twilio webhook entry point.

This Lambda sits behind the Twilio_Webhook_API (API Gateway HTTP API, payload
format v2.0) and has a single responsibility: respond ``200 OK`` to Twilio in
under one second (target <1s p99). It performs no business logic — it validates
the ``X-Twilio-Signature`` header, parses the form-urlencoded payload, and
enqueues the message to the inbound SQS FIFO queue. All heavy lifting is
delegated asynchronously to the Message_Processor (Requirements 3.1, 3.2, 3.3).

Pipeline
--------
1. Generate a ``correlation_id`` (UUID v4) before anything else and bind it to
   the logger so every log line of this invocation is traceable, and the id is
   propagated into the SQS payload for downstream Lambdas (Requirement 13.2).
2. Validate the ``X-Twilio-Signature`` against the Twilio auth token loaded from
   Secrets Manager. Invalid signature → ``403 Forbidden`` without enqueuing
   (Requirement 3.2).
3. Parse the body and enqueue to the inbound FIFO queue
   (``MessageGroupId = From``, ``MessageDeduplicationId = MessageSid``).
4. Return ``{"statusCode": 200, "body": ""}`` (Requirement 3.3).

Failure semantics
-----------------
If enqueuing fails the exception propagates and the Lambda returns a 5xx to
Twilio, which retries the webhook. SQS FIFO deduplication
(``MessageDeduplicationId = MessageSid``) discards the duplicate on retry within
the 5-minute window (Requirement 3.4), so retries are safe.

Twilio auth token / Secrets Manager
------------------------------------
The auth token is read from the secret whose ARN is provided via the
``TWILIO_SECRET_ARN`` environment variable (resolved from the cross-stack
contract published by ``infra``). The secret value is expected to be a JSON
object containing the auth token. Both of the following key spellings are
accepted (the loader tries them in order)::

    {"auth_token": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}
    {"TWILIO_AUTH_TOKEN": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}

The token is loaded lazily on first use and cached at module scope for the
lifetime of the execution environment (cold-start cache), so warm invocations
never call Secrets Manager.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Any
from urllib.parse import parse_qs

import boto3

from shared.logger import get_logger

from .enqueue import enqueue_message
from .parser import parse_form_urlencoded
from .twilio_signature import validate_twilio_signature

logger = get_logger("webhook-receiver")

# Module-level client reused across warm invocations.
_secrets_client = boto3.client("secretsmanager")

# Cold-start cache for the Twilio auth token. ``None`` means "not loaded yet".
_twilio_auth_token: str | None = None

# Secret JSON keys tried (in order) when extracting the auth token.
_AUTH_TOKEN_KEYS = ("auth_token", "TWILIO_AUTH_TOKEN")


def _load_twilio_auth_token() -> str:
    """Load and cache the Twilio auth token from Secrets Manager.

    The secret ARN is read lazily from the ``TWILIO_SECRET_ARN`` environment
    variable on first use. The secret payload is expected to be a JSON object
    containing the token under either ``auth_token`` or ``TWILIO_AUTH_TOKEN``.
    The resolved token is cached at module scope so subsequent (warm)
    invocations skip the Secrets Manager call entirely.

    Returns:
        The Twilio account auth token.

    Raises:
        KeyError: If ``TWILIO_SECRET_ARN`` is not configured, or the secret JSON
            does not contain a recognized auth-token key.
        json.JSONDecodeError: If the secret value is not valid JSON.
    """
    global _twilio_auth_token
    if _twilio_auth_token is not None:
        return _twilio_auth_token

    secret_arn = os.environ["TWILIO_SECRET_ARN"]
    response = _secrets_client.get_secret_value(SecretId=secret_arn)
    secret = json.loads(response["SecretString"])

    for key in _AUTH_TOKEN_KEYS:
        token = secret.get(key)
        if token:
            _twilio_auth_token = token
            return token

    raise KeyError(
        "Twilio auth token not found in secret; expected one of "
        f"{_AUTH_TOKEN_KEYS}"
    )


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Validate, parse and enqueue an inbound Twilio webhook.

    Args:
        event: API Gateway HTTP API (payload format v2.0) proxy event.
        context: Lambda context object (unused beyond Powertools injection).

    Returns:
        ``{"statusCode": 200, "body": ""}`` on success, or
        ``{"statusCode": 403, "body": ""}`` when the Twilio signature is
        invalid. Enqueue failures propagate so Twilio retries (SQS FIFO dedup
        discards the duplicate).
    """
    correlation_id = str(uuid.uuid4())
    logger.append_keys(correlation_id=correlation_id)

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    signature = headers.get("x-twilio-signature", "")

    request_context = event.get("requestContext") or {}
    domain_name = request_context.get("domainName", "")
    raw_path = event.get("rawPath", "")
    url = f"https://{domain_name}{raw_path}"

    raw_body = event.get("body", "") or ""
    is_base64 = event.get("isBase64Encoded", False)

    # Decode the body the same way for both signature validation and parsing so
    # the signed params exactly match what Twilio sent.
    decoded_body = _decode_body(raw_body, is_base64)
    params = {k: v[0] for k, v in parse_qs(decoded_body, keep_blank_values=True).items()}

    # 1. Validate the X-Twilio-Signature. Reject forged requests without
    #    enqueuing anything (Requirement 3.2).
    auth_token = _load_twilio_auth_token()
    if not validate_twilio_signature(auth_token, signature, url, params):
        return {"statusCode": 403, "body": ""}

    # 2. Parse the payload and enqueue to the inbound FIFO queue. A failure here
    #    propagates → 5xx → Twilio retries → SQS dedup discards duplicate.
    payload = parse_form_urlencoded(raw_body, is_base64)
    enqueue_message(payload, correlation_id)

    # 3. 200 OK — Twilio is happy and never waits on the real work.
    return {"statusCode": 200, "body": ""}


def _decode_body(body: str, is_base64: bool) -> str:
    """Return the request body, base64-decoding it when API Gateway flagged it."""
    if not body:
        return ""
    if is_base64:
        return base64.b64decode(body).decode("utf-8")
    return body


__all__ = ["handler"]
