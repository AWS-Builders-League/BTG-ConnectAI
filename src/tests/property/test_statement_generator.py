"""Property-based tests for the statement-generator Action Group Lambda.

Covers the statement-generator properties from the design document and Task 7.6:

    * Property 13 — Future Date Rejection            (Validates: Requirements 9.2)
    * Property 14 — Valid Statement Returns S3 Reference
                                                     (Validates: Requirements 9.3, 14.7)

Testing approach
----------------
The handler renders a real PDF and ``put_object``s it into the Statement_Bucket
(``STATEMENT_BUCKET`` env). We exercise the *real* S3 path using moto's
``mock_aws`` rather than mocking ``put_object`` so the "object actually exists in
S3" guarantee of Property 14 is verified end-to-end.

S3 client rebinding (critical):
``statement_generator.handler`` builds a module-level boto3 S3 client at import
time (``handler._s3_client``). That client is created *outside* any moto context
and would talk to real AWS. Inside the ``_mocked_s3`` fixture we create a fresh
S3 client within the ``mock_aws`` context, create the bucket, point the
``STATEMENT_BUCKET`` env var at it, and **rebind ``handler._s3_client``** to the
mocked client for the duration of the test (restoring the originals afterwards).

Hypothesis + function-scoped fixture:
The moto bucket/client are set up *once per test function* via a fixture, and
Hypothesis iterates many examples inside that single setup. This is intentional
(moto setup per example would be slow), so we suppress the
``function_scoped_fixture`` health check. To keep examples independent under the
shared, persistent bucket:
  * Property 13 (nothing must be written) snapshots the object count before each
    example and asserts it is unchanged afterwards — robust against any state
    that previous examples may have left behind.
  * Property 14 writes one object per example under a unique uuid key (the
    handler embeds a ``uuid4`` in the key), so examples never collide; we verify
    the *returned* key exists and its body is a real PDF.

"today" is the handler's ``_today()`` (Colombia/America-Bogota date). We compute the future/past
date boundaries from it once at import so the generated dates are always
strictly future (today + 1 .. today + 3650) or strictly past
(2024-01-01 .. today - 1), avoiding midnight-boundary flakiness.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, timedelta

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3  # noqa: E402
import pytest  # noqa: E402
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from moto import mock_aws  # noqa: E402

from lambdas.statement_generator import handler as sg  # noqa: E402
from lambdas.statement_generator.mock_data import MOCK_CLIENTS  # noqa: E402

_MAX_EXAMPLES = 100
_REGION = "us-east-1"
_BUCKET = "btg-test-statement-bucket"

# Boundary computed from the handler's own notion of "today" (UTC) so the test
# stays consistent with the rejection logic (``cutoff > _today()``).
_TODAY = sg._today()
_TOMORROW = _TODAY + timedelta(days=1)
_PAST_FLOOR = date(2024, 1, 1)

# Registered client phone numbers (both properties sample from these).
_registered_phone = st.sampled_from(
    sorted(c["phone_number"] for c in MOCK_CLIENTS)
)

# Strictly-future cut-off dates (today + 1 .. today + ~10 years), ISO-formatted.
_future_date = st.dates(
    min_value=_TOMORROW, max_value=_TODAY + timedelta(days=3650)
).map(lambda d: d.isoformat())

# Strictly-past cut-off dates (2024-01-01 .. yesterday), ISO-formatted.
_past_date = st.dates(
    min_value=_PAST_FLOOR, max_value=_TODAY - timedelta(days=1)
).map(lambda d: d.isoformat())


@pytest.fixture()
def mocked_s3() -> Iterator[object]:
    """Provide a moto-backed S3, the bucket, and a rebound handler client.

    Sets ``STATEMENT_BUCKET`` to the created bucket, rebinds
    ``handler._s3_client`` to a client created inside the ``mock_aws`` context,
    and restores both on teardown.
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_BUCKET)

        original_client = sg._s3_client
        original_bucket = os.environ.get("STATEMENT_BUCKET")
        sg._s3_client = s3
        os.environ["STATEMENT_BUCKET"] = _BUCKET
        try:
            yield s3
        finally:
            sg._s3_client = original_client
            if original_bucket is None:
                os.environ.pop("STATEMENT_BUCKET", None)
            else:
                os.environ["STATEMENT_BUCKET"] = original_bucket


def _object_count(s3: object) -> int:
    """Return the number of objects currently in the test bucket."""
    response = s3.list_objects_v2(Bucket=_BUCKET)
    return response.get("KeyCount", 0)


# ---------------------------------------------------------------------------
# Property 13: Future Date Rejection
# Validates: Requirements 9.2
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(
    max_examples=_MAX_EXAMPLES,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(phone=_registered_phone, cutoff=_future_date)
def test_future_date_is_rejected_and_nothing_written(
    mocked_s3: object, phone: str, cutoff: str
) -> None:
    """For any strictly-future cut-off date and a registered client, the handler
    rejects with ``future_date`` and writes NO object to S3 (Req 9.2)."""
    before = _object_count(mocked_s3)

    result = sg.handler({"phoneNumber": phone, "cutoffDate": cutoff})

    assert result["success"] is False
    assert result["error"] == "future_date"
    assert "s3Key" not in result
    # The rejection happens before any PDF render / put_object, so the bucket
    # must be exactly as it was before this example ran.
    assert _object_count(mocked_s3) == before


# ---------------------------------------------------------------------------
# Property 14: Valid Statement Returns S3 Reference
# Validates: Requirements 9.3, 14.7
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(
    max_examples=_MAX_EXAMPLES,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(phone=_registered_phone, cutoff=_past_date)
def test_valid_past_date_returns_s3_reference_and_stores_pdf(
    mocked_s3: object, phone: str, cutoff: str
) -> None:
    """For any valid past cut-off date and a registered client, the handler
    returns ``success`` with a non-empty S3 reference, the object actually
    exists at that key, and its body is a real PDF (Req 9.3 / 14.7)."""
    result = sg.handler({"phoneNumber": phone, "cutoffDate": cutoff})

    assert result["success"] is True

    bucket = result["s3Bucket"]
    s3_key = result["s3Key"]
    file_name = result["fileName"]

    # Non-empty S3 reference fields.
    assert isinstance(bucket, str) and bucket == _BUCKET
    assert isinstance(s3_key, str) and s3_key
    assert s3_key.endswith(".pdf")
    assert isinstance(file_name, str) and file_name.endswith(".pdf")

    # The object must actually exist in S3 at the returned key.
    head = mocked_s3.head_object(Bucket=bucket, Key=s3_key)
    assert head["ContentType"] == "application/pdf"

    # The stored content is a real PDF (magic bytes).
    body = mocked_s3.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
    assert body.startswith(b"%PDF")
