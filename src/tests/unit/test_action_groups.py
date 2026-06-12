"""Unit tests for the Action Group Lambdas (Task 7.7).

Example-based tests pinning down the concrete behaviour of the three
Action Group Lambdas the Strands_Agent invokes:

* **balance-query** (Requirements 7.1–7.4) — returns all products for a known
  client, filters by product type, and reports ``client_not_found`` for an
  unknown phone number.
* **transfer-breb** (Requirements 8.4, 8.7, 8.8) — the ``ValidateTransfer`` task
  accepts a valid transfer (source owned + funded, known destination), rejects
  insufficient funds with :class:`InsufficientFundsError`, and rejects an
  unknown destination with :class:`InvalidDestinationError`; the
  ``ExecuteTransfer`` task produces a COMPLETED receipt in COP with masked
  account numbers.
* **statement-generator** (Requirements 9.2, 9.3, 9.6) — generates a PDF for a
  valid past cut-off date (stored in S3), rejects a future cut-off date with
  ``future_date`` (writing nothing), and still produces an (empty) statement
  when there are no movements in the period.

Mocking approach
----------------
* **balance-query** is a pure function over an in-memory Mock_Core — no AWS, no
  mocking required.
* **transfer-breb** ``execute`` mutates the module-level Mock_Core balances. A
  fixture snapshots every product balance and restores it on teardown so the
  tests stay independent and order-insensitive.
* **statement-generator** writes the rendered PDF to S3. Under ``mock_aws`` we
  rebind the handler's module-level ``_s3_client`` to a client created inside
  the mock, create the Statement_Bucket and set ``STATEMENT_BUCKET``. The
  handler's ``_today`` is monkeypatched to a fixed date so "past" vs "future"
  cut-off dates are deterministic regardless of the wall clock.
"""

from __future__ import annotations

import copy
from datetime import date

import boto3
import pytest
from moto import mock_aws

from lambdas.balance_query import handler as balance_handler
from lambdas.statement_generator import handler as statement_handler
from lambdas.transfer_breb import execute as transfer_execute
from lambdas.transfer_breb import mock_data as transfer_mock_data
from lambdas.transfer_breb import validate as transfer_validate
from shared.errors import InsufficientFundsError, InvalidDestinationError

pytestmark = pytest.mark.unit

_REGION = "us-east-1"

# Canonical demo client used across the suite.
_CARLOS_PHONE = "+573001234567"
_UNKNOWN_PHONE = "+570000000000"

# Carlos' accounts in the transfer-breb Mock_Core.
_CARLOS_SOURCE_CC = "1001234568"        # cuenta_corriente, available 3_750_000.50
_CARLOS_SOURCE_AVAILABLE = 3_750_000.50
# A known valid destination (María's fondo) — present in VALID_DESTINATION_ACCOUNTS.
_VALID_DESTINATION = "2009876543"
_INVALID_DESTINATION = "0000000000"


# ---------------------------------------------------------------------------
# balance-query (Requirements 7.1–7.4)
# ---------------------------------------------------------------------------


class TestBalanceQuery:
    def test_all_products_for_known_client(self) -> None:
        # No product_type → all of the client's products (Req 7.2).
        result = balance_handler.get_balance(_CARLOS_PHONE)

        assert result["success"] is True
        data = result["data"]
        assert data["phoneNumber"] == _CARLOS_PHONE
        assert data["clientName"] == "Carlos Rodríguez"
        # Carlos owns exactly two products (cuenta_corriente + fondo_inversion).
        assert len(data["products"]) == 2
        product_types = {p["product_type"] for p in data["products"]}
        assert product_types == {"cuenta_corriente", "fondo_inversion"}

    def test_filtered_by_product_type_returns_only_that_type(self) -> None:
        # product_type filter narrows to the matching product(s) (Req 7.1).
        result = balance_handler.get_balance(_CARLOS_PHONE, product_type="fondo_inversion")

        assert result["success"] is True
        products = result["data"]["products"]
        assert len(products) == 1
        assert products[0]["product_type"] == "fondo_inversion"

    def test_handler_filters_via_camelcase_event(self) -> None:
        # The Strands tool sends camelCase keys through the Lambda entrypoint.
        result = balance_handler.handler(
            {"phoneNumber": _CARLOS_PHONE, "productType": "cuenta_corriente"}
        )

        assert result["success"] is True
        products = result["data"]["products"]
        assert len(products) == 1
        assert products[0]["product_type"] == "cuenta_corriente"

    def test_unknown_client_returns_client_not_found(self) -> None:
        # Unknown phone → success False with the Req 7.4 error code.
        result = balance_handler.get_balance(_UNKNOWN_PHONE)

        assert result["success"] is False
        assert result["error"] == "client_not_found"
        assert "data" not in result


# ---------------------------------------------------------------------------
# transfer-breb (Requirements 8.4, 8.7, 8.8)
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_transfer_balances():
    """Snapshot and restore Mock_Core balances mutated by ``execute``.

    ``execute.handler`` debits/credits the module-level Mock_Core in place. We
    deep-copy the balances before the test and write them back afterwards so the
    tests remain independent and order-insensitive.
    """
    snapshot = [
        copy.deepcopy(client["products"]) for client in transfer_mock_data.MOCK_CLIENTS
    ]
    try:
        yield
    finally:
        for client, saved_products in zip(transfer_mock_data.MOCK_CLIENTS, snapshot):
            client["products"] = saved_products


class TestTransferValidate:
    def test_valid_transfer_passes_validation(self) -> None:
        # Source owned + funded, destination known → valid True (Req 8.4/8.7/8.8).
        event = {
            "sourceAccount": _CARLOS_SOURCE_CC,
            "destinationAccount": _VALID_DESTINATION,
            "amount": 1_000_000.0,
            "concept": "Pago arriendo",
            "phoneNumber": _CARLOS_PHONE,
        }

        result = transfer_validate.handler(event, None)

        assert result["valid"] is True
        assert result["sourceAccount"] == _CARLOS_SOURCE_CC
        assert result["destinationAccount"] == _VALID_DESTINATION
        assert result["amount"] == 1_000_000.0

    def test_insufficient_funds_raises(self) -> None:
        # Amount above the source's available balance → InsufficientFundsError.
        event = {
            "sourceAccount": _CARLOS_SOURCE_CC,
            "destinationAccount": _VALID_DESTINATION,
            "amount": _CARLOS_SOURCE_AVAILABLE + 1_000_000.0,
            "phoneNumber": _CARLOS_PHONE,
        }

        with pytest.raises(InsufficientFundsError):
            transfer_validate.handler(event, None)

    def test_invalid_destination_raises(self) -> None:
        # Destination not in VALID_DESTINATION_ACCOUNTS → InvalidDestinationError.
        assert _INVALID_DESTINATION not in transfer_mock_data.VALID_DESTINATION_ACCOUNTS
        event = {
            "sourceAccount": _CARLOS_SOURCE_CC,
            "destinationAccount": _INVALID_DESTINATION,
            "amount": 1_000.0,
            "phoneNumber": _CARLOS_PHONE,
        }

        with pytest.raises(InvalidDestinationError):
            transfer_validate.handler(event, None)


class TestTransferExecute:
    def test_execute_produces_completed_receipt(self, restore_transfer_balances) -> None:
        event = {
            "sourceAccount": _CARLOS_SOURCE_CC,
            "destinationAccount": _VALID_DESTINATION,
            "amount": 1_000_000.0,
            "concept": "Pago arriendo",
            "phoneNumber": _CARLOS_PHONE,
        }

        result = transfer_execute.handler(event, None)

        receipt = result["receipt"]
        assert receipt["status"] == "COMPLETED"
        assert receipt["currency"] == "COP"
        assert receipt["amount"] == 1_000_000.0
        assert receipt["concept"] == "Pago arriendo"
        assert receipt["transactionId"]
        assert receipt["executedAt"]
        # Account numbers are masked (only the last 4 digits visible, Req 14.4).
        assert receipt["sourceAccount"] == "******4568"
        assert receipt["destinationAccount"] == "******6543"
        assert _CARLOS_SOURCE_CC not in receipt["sourceAccount"]

    def test_execute_debits_source_balance(self, restore_transfer_balances) -> None:
        amount = 500_000.0
        source = transfer_mock_data.find_account_by_number(_CARLOS_PHONE, _CARLOS_SOURCE_CC)
        before = source["available_balance"]

        transfer_execute.handler(
            {
                "sourceAccount": _CARLOS_SOURCE_CC,
                "destinationAccount": _VALID_DESTINATION,
                "amount": amount,
                "phoneNumber": _CARLOS_PHONE,
            },
            None,
        )

        after = transfer_mock_data.find_account_by_number(
            _CARLOS_PHONE, _CARLOS_SOURCE_CC
        )["available_balance"]
        assert after == before - amount


# ---------------------------------------------------------------------------
# statement-generator (Requirements 9.2, 9.3, 9.6)
# ---------------------------------------------------------------------------


_BUCKET = "btg-connectai-sandbox-statements"
# Fixed "today" so past/future cut-off dates are deterministic. The Mock_Core
# transactions are dated January 2025, so this date is after them.
_FIXED_TODAY = date(2025, 3, 1)


@pytest.fixture
def statement_bucket(monkeypatch):
    """Provide a moto S3 Statement_Bucket wired into the handler module.

    Yields the boto3 S3 client (bound inside the mock) so tests can read back
    stored objects. ``_today`` is pinned so cut-off date validation is stable.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("STATEMENT_BUCKET", _BUCKET)
    monkeypatch.setattr(statement_handler, "_today", lambda: _FIXED_TODAY)

    with mock_aws():
        client = boto3.client("s3", region_name=_REGION)
        client.create_bucket(Bucket=_BUCKET)

        original = statement_handler._s3_client
        statement_handler._s3_client = client
        try:
            yield client
        finally:
            statement_handler._s3_client = original


def _list_keys(client) -> list[str]:
    """Return the object keys currently in the Statement_Bucket."""
    response = client.list_objects_v2(Bucket=_BUCKET)
    return [obj["Key"] for obj in response.get("Contents", [])]


class TestStatementGenerator:
    def test_valid_past_date_generates_and_stores_pdf(self, statement_bucket) -> None:
        # Cut-off after the January movements but before _FIXED_TODAY (Req 9.3).
        result = statement_handler.generate_statement(_CARLOS_PHONE, "2025-02-28")

        assert result["success"] is True
        assert result["s3Bucket"] == _BUCKET
        assert result["s3Key"].endswith(".pdf")
        assert result["fileName"].startswith("extracto_")

        # The object actually landed in S3 and is a real PDF.
        keys = _list_keys(statement_bucket)
        assert result["s3Key"] in keys
        body = statement_bucket.get_object(Bucket=_BUCKET, Key=result["s3Key"])[
            "Body"
        ].read()
        assert body.startswith(b"%PDF")

    def test_future_date_rejected_and_writes_nothing(self, statement_bucket) -> None:
        # Cut-off after _FIXED_TODAY → future_date error, nothing written (Req 9.2).
        result = statement_handler.generate_statement(_CARLOS_PHONE, "2025-06-01")

        assert result["success"] is False
        assert result["error"] == "future_date"
        assert _list_keys(statement_bucket) == []

    def test_period_with_no_movements_still_generates_statement(
        self, statement_bucket
    ) -> None:
        # Cut-off before any of Carlos' transactions (earliest 2025-01-03) but
        # still a past date → empty statement is generated anyway (Req 9.6).
        result = statement_handler.generate_statement(_CARLOS_PHONE, "2025-01-01")

        assert result["success"] is True
        assert result["s3Key"] in _list_keys(statement_bucket)
        body = statement_bucket.get_object(Bucket=_BUCKET, Key=result["s3Key"])[
            "Body"
        ].read()
        assert body.startswith(b"%PDF")
