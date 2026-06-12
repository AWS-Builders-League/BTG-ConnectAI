"""Property-based tests for the balance-query Action Group Lambda.

Covers the balance-query correctness properties from the design document:

    * Property 9  — Balance Query Correctness   (Validates: Requirements 7.1, 7.2, 7.3)
    * Property 10 — Unknown Client Error         (Validates: Requirement 7.4)

Testing approach
----------------
``get_balance`` and ``get_client_by_phone`` are pure functions over the inline
Mock_Core data — no AWS calls — so these property tests need no mocking. We set
``AWS_DEFAULT_REGION`` before importing the handler purely as a defensive measure
(some shared modules may construct boto3 clients eagerly in other environments).

Property 9 samples a registered client phone from ``MOCK_CLIENTS`` and checks the
returned ``data.products`` against the canonical Mock_Core values: same product
count, matching balances, ``currency == "COP"``, and the Requirement 7.3 fields
present. It also exercises the product-type filter.

Property 10 generates arbitrary strings (including E.164-like numbers) and
``assume``s they are not any registered client's phone number, then asserts the
client-not-found error contract.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from lambdas.balance_query.handler import get_balance  # noqa: E402
from lambdas.balance_query.mock_data import MOCK_CLIENTS  # noqa: E402

# ---------------------------------------------------------------------------
# Strategies and helpers
# ---------------------------------------------------------------------------

_MAX_EXAMPLES = 100

# The set of registered phone numbers — used both to sample valid clients
# (Property 9) and to exclude them from the "unknown" space (Property 10).
_REGISTERED_PHONES: frozenset[str] = frozenset(c["phone_number"] for c in MOCK_CLIENTS)

# Required raw fields per product (Requirement 7.3) plus the formatted/derived
# fields the handler attaches.
_REQUIRED_PRODUCT_FIELDS = (
    "product_type",
    "product_type_label",
    "product_name",
    "currency",
    "available_balance",
    "total_balance",
    "available_balance_formatted",
    "total_balance_formatted",
    "cutoff_date",
    "account_number_masked",
)

# Registered client phone numbers (Property 9 samples from these).
_registered_phone = st.sampled_from(sorted(_REGISTERED_PHONES))

# Distinct canonical product types present in Mock_Core.
_product_types = st.sampled_from(["cuenta_corriente", "fondo_inversion"])

# Arbitrary phone-like / free-text strings for Property 10.
_e164_like = st.builds(
    lambda digits: "+" + digits,
    st.text(alphabet="0123456789", min_size=8, max_size=15),
)
_unknown_phone = st.one_of(_e164_like, st.text(max_size=20))


def _client_by_phone(phone: str) -> dict:
    """Return the canonical Mock_Core client dict for ``phone``."""
    return next(c for c in MOCK_CLIENTS if c["phone_number"] == phone)


# ---------------------------------------------------------------------------
# Property 9: Balance Query Correctness
# Validates: Requirements 7.1, 7.2, 7.3
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(phone=_registered_phone)
def test_balance_query_returns_all_products_matching_mock_core(phone: str) -> None:
    """For any registered client, an unfiltered query returns success with all
    products matching the Mock_Core values (Req 7.2, 7.3)."""
    client = _client_by_phone(phone)
    result = get_balance(phone)

    assert result["success"] is True
    data = result["data"]
    assert data["phoneNumber"] == client["phone_number"]
    assert data["clientName"] == client["name"]

    products = data["products"]
    # Same number of products as Mock_Core (Req 7.2).
    assert len(products) == len(client["products"])

    # Each returned product matches the corresponding Mock_Core product's
    # balances, currency, and carries the Req 7.3 fields.
    expected_by_account = {p["account_id"]: p for p in client["products"]}
    for product in products:
        for field in _REQUIRED_PRODUCT_FIELDS:
            assert field in product, f"missing field {field!r}"

        expected = expected_by_account[product["account_id"]]
        assert product["available_balance"] == expected["available_balance"]
        assert product["total_balance"] == expected["total_balance"]
        assert product["currency"] == "COP"
        assert product["product_type"] == expected["product_type"]
        assert product["product_name"] == expected["product_name"]
        assert product["cutoff_date"] == expected["cutoff_date"]


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(phone=_registered_phone, product_type=_product_types)
def test_balance_query_filter_by_product_type(phone: str, product_type: str) -> None:
    """Filtering by a product type returns only products of that type, and the
    count matches the client's products of that type (Req 7.1)."""
    client = _client_by_phone(phone)
    expected = [p for p in client["products"] if p["product_type"] == product_type]

    result = get_balance(phone, product_type)

    assert result["success"] is True
    products = result["data"]["products"]
    assert len(products) == len(expected)
    for product in products:
        assert product["product_type"] == product_type
        assert product["currency"] == "COP"


# ---------------------------------------------------------------------------
# Property 10: Unknown Client Error
# Validates: Requirement 7.4
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=_MAX_EXAMPLES)
@given(phone=_unknown_phone)
def test_unknown_client_returns_not_found_error(phone: str) -> None:
    """For any phone number not registered in Mock_Core, the query fails with a
    ``client_not_found`` error (Req 7.4)."""
    # Exclude any registered client (exact match after the handler's strip()).
    assume(phone.strip() not in _REGISTERED_PHONES)

    result = get_balance(phone)

    assert result["success"] is False
    assert result["error"] == "client_not_found"
    assert "data" not in result
