"""Property-based tests for the Message_Processor consent gate.

Covers:
    * Property 5 — Existing Consent Skips T&C   (Validates: Requirement 1.4)

Requirement 1.4: a Bank_Client that already has an ``accepted`` consent record
in the Consent_Store, when sending a message, has the Terms & Conditions flow
skipped and the message processed directly. The gate decision is made by
``has_accepted_consent(get_consent(phone))`` — Property 5 asserts that for ANY
phone number with a stored ``accepted`` record this predicate is ``True`` (so
the T&C flow is skipped), and, complementarily, that a ``rejected`` or absent
record does NOT satisfy the gate (so the T&C flow still runs).

DynamoDB mocking strategy
-------------------------
``consent.py`` creates its boto3 resource at import time
(``_dynamodb = boto3.resource("dynamodb")``). To guarantee correctness
regardless of import ordering, each Hypothesis example runs inside its own
``mock_aws()`` context where we:

1. rebind ``consent._dynamodb`` to a fresh resource created *inside* the mock, and
2. (re)create the Consent_Store table.

This makes every example fully isolated (clean table) and immune to the
module-level resource having been built before the mock was active. AWS
credentials/region are forced to dummy test values before the module import so
no real AWS calls can ever occur.
"""

from __future__ import annotations

import os

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

# --- Force a fake AWS environment BEFORE importing the module under test. -----
# boto3.resource("dynamodb") (run at consent.py import time) needs a region; the
# dummy credentials ensure moto never falls through to real AWS.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

CONSENT_TABLE_NAME = "Consent_Store-test"
os.environ["CONSENT_TABLE_NAME"] = CONSENT_TABLE_NAME

from lambdas.message_processor import consent  # noqa: E402  (after env setup)

# ---------------------------------------------------------------------------
# Phone-number strategy: E.164-ish Colombian numbers, with and without the
# ``whatsapp:`` channel prefix that consent.py normalizes away.
# ---------------------------------------------------------------------------
_phone_strategy = st.builds(
    lambda prefix, digits: f"{prefix}+57{digits}",
    st.sampled_from(["", "whatsapp:"]),
    st.text(alphabet="0123456789", min_size=8, max_size=12),
)


def _create_consent_table(dynamodb) -> None:
    """Create the Consent_Store table (``pk`` String partition key)."""
    dynamodb.create_table(
        TableName=CONSENT_TABLE_NAME,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


# ---------------------------------------------------------------------------
# Property 5: Existing Consent Skips T&C
# Validates: Requirement 1.4
# ---------------------------------------------------------------------------
@pytest.mark.property
@settings(max_examples=50, deadline=None)
@given(phone=_phone_strategy)
def test_existing_accepted_consent_skips_tc(phone: str) -> None:
    """Any phone with a stored ``accepted`` record satisfies the gate.

    ``has_accepted_consent(get_consent(phone))`` must be ``True`` so the
    Message_Processor skips the Terms & Conditions flow (Requirement 1.4).
    """
    with mock_aws():
        consent._dynamodb = boto3.resource("dynamodb")
        _create_consent_table(consent._dynamodb)

        consent.store_consent(phone, "accepted")

        record = consent.get_consent(phone)
        assert record is not None
        assert consent.has_accepted_consent(record) is True


@pytest.mark.property
@settings(max_examples=50, deadline=None)
@given(phone=_phone_strategy)
def test_non_accepted_consent_does_not_skip_tc(phone: str) -> None:
    """A ``rejected`` or absent record does NOT satisfy the gate.

    Complement of Property 5: the gate only opens for ``accepted`` consent, so
    a rejected record and a first-contact (no record) both keep the T&C flow
    active (Requirement 1.4).
    """
    with mock_aws():
        consent._dynamodb = boto3.resource("dynamodb")
        _create_consent_table(consent._dynamodb)

        # First contact — no record stored yet.
        assert consent.get_consent(phone) is None
        assert consent.has_accepted_consent(consent.get_consent(phone)) is False

        # Rejected consent must not open the gate either.
        consent.store_consent(phone, "rejected")
        record = consent.get_consent(phone)
        assert record is not None
        assert consent.has_accepted_consent(record) is False
