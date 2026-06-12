"""Unit tests for the Auth_Service Lambda (Task 6.3, Requirements 5.3 / 5.5 / 5.7).

These example-based tests pin down the concrete behaviour of the mock login
endpoint backing the Login_Page. The handler validates a signed callback token,
checks hardcoded demo credentials, verifies the user owns the link's phone, and
writes a 30-minute Auth_Session to DynamoDB.

What is exercised
-----------------
* **Valid auth creates a session with the correct TTL (Req 5.3)** — a valid
  token + correct credentials + matching phone returns ``200 {"status":
  "success"}`` and writes an Auth_Session keyed by the normalized phone with
  ``ttl ≈ now + AUTH_SESSION_TTL`` (1800s) and the right user attributes.
* **Invalid username (Req 5.5)** — a username not in ``TEST_USERS`` →
  ``401 invalid_credentials`` and *no* session written.
* **Wrong password (Req 5.5)** — a registered username with a bad password →
  ``401`` and no session.
* **Phone mismatch (Req 5.7)** — valid credentials but the link's phone differs
  from the user's ``phone_number`` → ``401 invalid_credentials`` and no session
  (the token is forged for the mismatched phone so the token check passes and
  the phone-vs-user check is what rejects).
* **Invalid/expired/forged token (Req 5.2 integrity)** — correct credentials and
  phone but a bad token → ``403 invalid_token`` and no session.
* **OPTIONS preflight** — returns ``200`` with CORS headers.
* **bad_request** — missing body or missing required fields → ``400``.

Mocking approach
----------------
The handler creates a module-level ``boto3.resource("dynamodb")`` at import
time. Under ``mock_aws`` we **rebind ``handler._dynamodb``** to a resource
created inside the mock, create the Auth_Session table (``pk`` String HASH),
and restore the original on teardown. ``AUTH_TABLE_NAME`` /
``CALLBACK_TOKEN_SECRET`` / region / dummy credentials are set in the
environment. Tokens are generated with the *same* HMAC-SHA256 scheme the
handler validates against, keyed with the test ``CALLBACK_TOKEN_SECRET``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import boto3
import pytest
from moto import mock_aws

from lambdas.auth_service import handler
from shared.constants import AUTH_SESSION_TTL

pytestmark = pytest.mark.unit

_REGION = "us-east-1"
_TABLE_NAME = "BTGConnectAI-sandbox-AuthSession"
_SECRET = "test_callback_token_secret_0123456789"  # noqa: S105 (test fixture)

# A known demo user (must match users.TEST_USERS).
_USERNAME = "carlos.rodriguez"
_PASSWORD = "BtgDemo2025!"  # noqa: S105 (test fixture)
_PHONE = "+573001112233"
_NAME = "Carlos Rodríguez"
_DOCUMENT_ID = "1010101010"
_EMAIL = "carlos.rodriguez@example.com"

# A different registered user's phone (for the mismatch test).
_OTHER_PHONE = "+573004445566"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_table(monkeypatch):
    """Provide a moto Auth_Session table wired into the handler module.

    Yields the boto3 ``Table`` resource so tests can read back written items.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AUTH_TABLE_NAME", _TABLE_NAME)
    monkeypatch.setenv("CALLBACK_TOKEN_SECRET", _SECRET)

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=_REGION)
        resource.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = resource.Table(_TABLE_NAME)

        original = handler._dynamodb
        handler._dynamodb = resource
        try:
            yield table
        finally:
            handler._dynamodb = original


_INBOUND_QUEUE_NAME = "BTGConnectAI-sandbox-inbound.fifo"


@pytest.fixture
def auth_table_with_queue(monkeypatch):
    """Like :func:`auth_table` but also wires a moto inbound FIFO queue.

    Sets ``INBOUND_QUEUE_URL`` and rebinds both ``handler._dynamodb`` and
    ``handler._sqs`` to the mocked resources, so the auto-resume path
    (:func:`handler._resume_pending_request`) is exercised end-to-end. Yields
    ``(table, sqs_client, queue_url)``.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AUTH_TABLE_NAME", _TABLE_NAME)
    monkeypatch.setenv("CALLBACK_TOKEN_SECRET", _SECRET)

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=_REGION)
        resource.create_table(
            TableName=_TABLE_NAME,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = resource.Table(_TABLE_NAME)

        sqs = boto3.client("sqs", region_name=_REGION)
        queue_url = sqs.create_queue(
            QueueName=_INBOUND_QUEUE_NAME,
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
        )["QueueUrl"]
        monkeypatch.setenv("INBOUND_QUEUE_URL", queue_url)

        orig_db, orig_sqs = handler._dynamodb, handler._sqs
        handler._dynamodb = resource
        handler._sqs = sqs
        try:
            yield table, sqs, queue_url
        finally:
            handler._dynamodb = orig_db
            handler._sqs = orig_sqs


def _put_pending(table, *, phone: str = _PHONE, text: str = "Necesito mi extracto",
                 ttl_offset: int = 600) -> None:
    """Write a pending-request marker (``pending#<phone>``) into the table."""
    table.put_item(
        Item={
            "pk": f"{handler.PENDING_REQUEST_PK_PREFIX}{phone}",
            "phoneNumber": phone,
            "pendingRequest": text,
            "createdAt": "2026-06-12T00:00:00+00:00",
            "ttl": int(time.time()) + ttl_offset,
        }
    )


class _LambdaContext:
    """Minimal Lambda context for Powertools' ``inject_lambda_context``."""

    function_name = "auth-service"
    memory_limit_in_mb = 128
    invoked_function_arn = (
        "arn:aws:lambda:us-east-1:123456789012:function:auth-service"
    )
    aws_request_id = "test-request-id"


_CONTEXT = _LambdaContext()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(phone: str, *, exp_offset: int = 600, secret: str = _SECRET) -> str:
    """Build a signed callback token for ``phone`` using the handler's scheme.

    The token is ``"<expEpoch>.<hmacSHA256>"`` where the signature is an
    HMAC-SHA256 over ``"<phone>:<expEpoch>"`` keyed with ``secret``. ``phone``
    must already be normalized (no ``whatsapp:`` prefix) to match the handler's
    re-derivation.
    """
    exp = int(time.time()) + exp_offset
    sig = hmac.new(
        secret.encode("utf-8"),
        f"{phone}:{exp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{exp}.{sig}"


def _make_event(
    body: dict | None,
    *,
    method: str = "POST",
    is_base64: bool = False,
) -> dict:
    """Build a Function URL (payload v2.0) event with the given JSON body."""
    raw = None
    if body is not None:
        raw = json.dumps(body)
        if is_base64:
            raw = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    return {
        "requestContext": {"http": {"method": method}},
        "headers": {"content-type": "application/json"},
        "body": raw,
        "isBase64Encoded": is_base64,
    }


def _valid_body(**overrides: str) -> dict:
    """Build a valid login request body for the known demo user."""
    body = {
        "username": _USERNAME,
        "password": _PASSWORD,
        "phone": _PHONE,
        "token": _make_token(_PHONE),
    }
    body.update(overrides)
    return body


def _call(body: dict | None, **event_kwargs) -> dict:
    """Invoke the handler with an event built from ``body``."""
    return handler.handler(_make_event(body, **event_kwargs), _CONTEXT)


def _body_json(result: dict) -> dict:
    """Decode a handler response body to a dict (empty string → {})."""
    raw = result.get("body") or "{}"
    return json.loads(raw)


# ---------------------------------------------------------------------------
# 1. Valid auth creates a session with the correct TTL (Req 5.3)
# ---------------------------------------------------------------------------


class TestValidAuthentication:
    def test_valid_credentials_create_session_returns_success(self, auth_table) -> None:
        before = int(time.time())

        result = _call(_valid_body())

        after = int(time.time())

        assert result["statusCode"] == 200
        assert _body_json(result) == {"status": "success"}

        item = auth_table.get_item(Key={"pk": _PHONE}).get("Item")
        assert item is not None
        assert item["pk"] == _PHONE
        assert item["username"] == _USERNAME
        assert item["name"] == _NAME
        assert item["documentId"] == _DOCUMENT_ID
        assert item["email"] == _EMAIL
        assert item["sessionId"]
        assert item["createdAt"]
        assert item["expiresAt"]

        # ttl ≈ now + AUTH_SESSION_TTL (1800s), allowing for clock drift across
        # the call window.
        ttl = int(item["ttl"])
        assert before + AUTH_SESSION_TTL <= ttl <= after + AUTH_SESSION_TTL + 1
        # Sanity: comfortably inside the documented tolerance band.
        assert 1700 <= ttl - before <= 1900

    def test_whatsapp_prefixed_phone_normalized_in_session_key(self, auth_table) -> None:
        # The login link may carry a ``whatsapp:`` prefix; the token is signed
        # over the normalized phone, and the session is keyed by it.
        body = _valid_body(phone=f"whatsapp:{_PHONE}", token=_make_token(_PHONE))

        result = _call(body)

        assert result["statusCode"] == 200
        assert auth_table.get_item(Key={"pk": _PHONE}).get("Item") is not None


# ---------------------------------------------------------------------------
# 2. Invalid username (Req 5.5)
# ---------------------------------------------------------------------------


class TestInvalidUsername:
    def test_unknown_username_returns_401_and_writes_nothing(self, auth_table) -> None:
        body = _valid_body(username="nonexistent.user")

        result = _call(body)

        assert result["statusCode"] == 401
        assert _body_json(result) == {"status": "error", "error": "invalid_credentials"}
        assert auth_table.get_item(Key={"pk": _PHONE}).get("Item") is None


# ---------------------------------------------------------------------------
# 3. Wrong password (Req 5.5)
# ---------------------------------------------------------------------------


class TestWrongPassword:
    def test_wrong_password_returns_401_and_writes_nothing(self, auth_table) -> None:
        body = _valid_body(password="WrongPassword!")

        result = _call(body)

        assert result["statusCode"] == 401
        assert _body_json(result) == {"status": "error", "error": "invalid_credentials"}
        assert auth_table.get_item(Key={"pk": _PHONE}).get("Item") is None


# ---------------------------------------------------------------------------
# 4. Phone mismatch (Req 5.7)
# ---------------------------------------------------------------------------


class TestPhoneMismatch:
    def test_phone_not_belonging_to_user_returns_401(self, auth_table) -> None:
        # Valid credentials for carlos, but the link is for a different
        # (registered) phone. The token is forged for that phone so the token
        # check passes — the phone-vs-user check is what rejects.
        body = _valid_body(phone=_OTHER_PHONE, token=_make_token(_OTHER_PHONE))

        result = _call(body)

        assert result["statusCode"] == 401
        assert _body_json(result) == {"status": "error", "error": "invalid_credentials"}
        # No session written under either phone.
        assert auth_table.get_item(Key={"pk": _OTHER_PHONE}).get("Item") is None
        assert auth_table.get_item(Key={"pk": _PHONE}).get("Item") is None


# ---------------------------------------------------------------------------
# 5. Invalid / expired / forged token (Req 5.2 integrity)
# ---------------------------------------------------------------------------


class TestInvalidToken:
    def test_malformed_token_returns_403(self, auth_table) -> None:
        body = _valid_body(token="not-a-valid-token")

        result = _call(body)

        assert result["statusCode"] == 403
        assert _body_json(result) == {"status": "error", "error": "invalid_token"}
        assert auth_table.get_item(Key={"pk": _PHONE}).get("Item") is None

    def test_expired_token_returns_403(self, auth_table) -> None:
        # exp in the past → rejected as expired.
        body = _valid_body(token=_make_token(_PHONE, exp_offset=-10))

        result = _call(body)

        assert result["statusCode"] == 403
        assert _body_json(result) == {"status": "error", "error": "invalid_token"}
        assert auth_table.get_item(Key={"pk": _PHONE}).get("Item") is None

    def test_forged_signature_returns_403(self, auth_table) -> None:
        # Token signed with the wrong secret → signature mismatch.
        body = _valid_body(token=_make_token(_PHONE, secret="wrong-secret"))

        result = _call(body)

        assert result["statusCode"] == 403
        assert _body_json(result) == {"status": "error", "error": "invalid_token"}
        assert auth_table.get_item(Key={"pk": _PHONE}).get("Item") is None


# ---------------------------------------------------------------------------
# 6. OPTIONS preflight
# ---------------------------------------------------------------------------


class TestCorsPreflight:
    def test_options_returns_200_with_cors_headers(self, auth_table) -> None:
        result = _call(None, method="OPTIONS")

        assert result["statusCode"] == 200
        headers = result["headers"]
        assert headers["Access-Control-Allow-Origin"] == "*"
        assert "POST" in headers["Access-Control-Allow-Methods"]
        assert "OPTIONS" in headers["Access-Control-Allow-Methods"]
        assert "Content-Type" in headers["Access-Control-Allow-Headers"]


# ---------------------------------------------------------------------------
# 7. bad_request (missing body / missing fields)
# ---------------------------------------------------------------------------


class TestBadRequest:
    def test_missing_body_returns_400(self, auth_table) -> None:
        result = _call(None)

        assert result["statusCode"] == 400
        assert _body_json(result) == {"status": "error", "error": "bad_request"}

    def test_missing_required_field_returns_400(self, auth_table) -> None:
        # Drop the token field.
        body = _valid_body()
        del body["token"]

        result = _call(body)

        assert result["statusCode"] == 400
        assert _body_json(result) == {"status": "error", "error": "bad_request"}
        assert auth_table.get_item(Key={"pk": _PHONE}).get("Item") is None

    def test_base64_encoded_valid_body_is_accepted(self, auth_table) -> None:
        # Function URLs may deliver the body base64-encoded.
        result = _call(_valid_body(), is_base64=True)

        assert result["statusCode"] == 200
        assert auth_table.get_item(Key={"pk": _PHONE}).get("Item") is not None


# ---------------------------------------------------------------------------
# 8. Auto-resume of the pending request after login (Req 5.1 / 5.4)
# ---------------------------------------------------------------------------


class TestAutoResumePendingRequest:
    def test_pending_request_is_enqueued_and_consumed(self, auth_table_with_queue) -> None:
        # A successful login with an outstanding pending request re-injects it as
        # a synthetic Twilio-shaped message and deletes the pending marker.
        table, sqs, queue_url = auth_table_with_queue
        _put_pending(table, text="Necesito mi extracto de mayo")

        result = _call(_valid_body())

        assert result["statusCode"] == 200
        # The Auth_Session was created and the pending marker consumed (deleted).
        assert table.get_item(Key={"pk": _PHONE}).get("Item") is not None
        pending_pk = f"{handler.PENDING_REQUEST_PK_PREFIX}{_PHONE}"
        assert table.get_item(Key={"pk": pending_pk}).get("Item") is None

        # A single synthetic message was enqueued with the webhook-compatible shape.
        messages = sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10
        ).get("Messages", [])
        assert len(messages) == 1
        body = json.loads(messages[0]["Body"])
        assert body["From"] == f"whatsapp:{_PHONE}"
        assert body["Body"] == "Necesito mi extracto de mayo"
        assert body["NumMedia"] == "0"
        assert body["correlationId"]

    def test_no_pending_request_enqueues_nothing(self, auth_table_with_queue) -> None:
        # Login succeeds and nothing is enqueued when there is no pending request.
        table, sqs, queue_url = auth_table_with_queue

        result = _call(_valid_body())

        assert result["statusCode"] == 200
        messages = sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10
        ).get("Messages", [])
        assert messages == []

    def test_expired_pending_is_discarded_not_replayed(self, auth_table_with_queue) -> None:
        # An expired pending marker is consumed but never replayed (stale intent).
        table, sqs, queue_url = auth_table_with_queue
        _put_pending(table, ttl_offset=-10)

        result = _call(_valid_body())

        assert result["statusCode"] == 200
        pending_pk = f"{handler.PENDING_REQUEST_PK_PREFIX}{_PHONE}"
        assert table.get_item(Key={"pk": pending_pk}).get("Item") is None
        messages = sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10
        ).get("Messages", [])
        assert messages == []

    def test_resume_failure_does_not_break_login(
        self, auth_table_with_queue, monkeypatch
    ) -> None:
        # The resume is best-effort: an SQS failure must not fail the login, which
        # has already written the session and must still return success.
        table, sqs, _queue_url = auth_table_with_queue
        _put_pending(table)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("sqs unavailable")

        monkeypatch.setattr(handler._sqs, "send_message", _boom)

        result = _call(_valid_body())

        assert result["statusCode"] == 200
        assert _body_json(result) == {"status": "success"}
        # The session is intact regardless of the resume failure.
        assert table.get_item(Key={"pk": _PHONE}).get("Item") is not None
