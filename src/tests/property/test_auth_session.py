"""Property-based tests for the Message_Processor auth & session module.

Covers the auth/session correctness properties from the design document:

    * Property 3 — Session ID Determinism            (Validates: Requirements 11.1)
    * Property 6 — Auth Gate: No Session → Login      (Validates: Requirements 5.1, 5.8)
    * Property 7 — Auth Gate: Active Session → Proceed (Validates: Requirements 6.1)

Testing approach
----------------
``derive_session_id`` (Property 3) is a pure function — it derives a UUID v5 over
a fixed namespace from the normalized phone number — so its property tests need
no AWS and are kept independent of the DynamoDB fixture.

Properties 6 and 7 read the Auth_Session table through ``get_auth_session`` /
``is_expired``, so they run against a moto-mocked DynamoDB. The ``auth`` module
builds its ``_dynamodb`` resource at import time, so the module-scoped
``auth_table`` fixture starts the ``mock_aws`` context, creates the table, and
rebinds ``auth._dynamodb`` to a resource created *inside* the mock (restoring it
on teardown). A module-scoped fixture is used deliberately to avoid Hypothesis's
function-scoped-fixture health check; each example isolates itself by keying on
its generated phone number and cleaning up the item it wrote.
"""

from __future__ import annotations

import os
from datetime import timedelta

import boto3
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from lambdas.message_processor import auth

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# E.164-like Colombian mobile numbers: "+57" followed by 10 digits. These
# normalize to themselves (no ``whatsapp:`` prefix), so the normalized form
# equals the generated value.
_phone_numbers = st.builds(
    lambda digits: "+57" + digits,
    st.text(alphabet="0123456789", min_size=10, max_size=10),
)

_MAX_EXAMPLES = 50


# ---------------------------------------------------------------------------
# Property 3: Session ID Determinism
# Validates: Requirements 11.1
# (Pure function — no AWS required.)
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(phone=_phone_numbers)
def test_session_id_is_deterministic(phone: str) -> None:
    """The same phone number always yields the same session id."""
    first = auth.derive_session_id(phone)
    second = auth.derive_session_id(phone)
    assert first == second


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(phone=_phone_numbers)
def test_session_id_prefix_invariant(phone: str) -> None:
    """A bare number and its ``whatsapp:``-prefixed form share a session id."""
    assert auth.derive_session_id(phone) == auth.derive_session_id(f"whatsapp:{phone}")


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(phone_a=_phone_numbers, phone_b=_phone_numbers)
def test_session_id_is_injective(phone_a: str, phone_b: str) -> None:
    """Two distinct (normalized) phone numbers yield distinct session ids."""
    assume(phone_a != phone_b)
    assert auth.derive_session_id(phone_a) != auth.derive_session_id(phone_b)


# ---------------------------------------------------------------------------
# DynamoDB-backed fixture for Properties 6 & 7
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def auth_table():
    """Provide a moto-mocked Auth_Session table wired into the ``auth`` module.

    The ``auth`` module constructs its boto3 DynamoDB resource at import time, so
    we start the mock, create the table, and rebind ``auth._dynamodb`` to a
    resource created within the mock. Module scope avoids Hypothesis's
    function-scoped-fixture health check.
    """
    table_name = "Auth_Session"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AUTH_TABLE_NAME"] = table_name

    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = dynamodb.Table(table_name)

        original = auth._dynamodb
        auth._dynamodb = dynamodb
        try:
            yield table
        finally:
            auth._dynamodb = original


def _make_session_item(phone: str, ttl_epoch: int) -> dict:
    """Build an Auth_Session item for ``phone`` expiring at ``ttl_epoch``."""
    now = auth._now()
    return {
        "pk": phone,
        "sessionId": auth.derive_session_id(phone),
        "username": "test.user",
        "name": "Test User",
        "documentId": "1234567890",
        "createdAt": now.isoformat(),
        "expiresAt": (now + timedelta(seconds=ttl_epoch - int(now.timestamp()))).isoformat(),
        "ttl": ttl_epoch,
    }


# ---------------------------------------------------------------------------
# Property 6: Auth Gate — No Session Triggers Login
# Validates: Requirements 5.1, 5.8
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(phone=_phone_numbers)
def test_no_record_needs_login(phone: str, auth_table) -> None:
    """With no stored session, the gate condition 'needs login' holds."""
    auth_table.delete_item(Key={"pk": phone})

    session = auth.get_auth_session(phone)
    needs_login = session is None or auth.is_expired(session)

    assert session is None
    assert needs_login


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(phone=_phone_numbers)
def test_expired_record_needs_login(phone: str, auth_table) -> None:
    """With an expired session (ttl in the past), 'needs login' holds."""
    past_ttl = int(auth._now().timestamp()) - 100
    auth_table.put_item(Item=_make_session_item(phone, past_ttl))
    try:
        session = auth.get_auth_session(phone)
        needs_login = session is None or auth.is_expired(session)

        assert session is not None
        assert auth.is_expired(session) is True
        assert needs_login
    finally:
        auth_table.delete_item(Key={"pk": phone})


# ---------------------------------------------------------------------------
# Property 7: Auth Gate — Active Session Allows Actions
# Validates: Requirements 6.1
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES, deadline=None)
@given(phone=_phone_numbers)
def test_active_session_proceeds(phone: str, auth_table) -> None:
    """With a session whose ttl is strictly in the future, it is not expired."""
    future_ttl = int(auth._now().timestamp()) + 3600
    auth_table.put_item(Item=_make_session_item(phone, future_ttl))
    try:
        session = auth.get_auth_session(phone)

        assert session is not None
        assert auth.is_expired(session) is False
    finally:
        auth_table.delete_item(Key={"pk": phone})
