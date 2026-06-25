"""Mock_Core synthetic banking data for the transfer-breb flow.

This is the same canonical Mock_Core dataset used by the ``balance-query``
Action Group Lambda (the three demo clients — Carlos Rodríguez, María López and
Juan García — with identical phone numbers, account numbers and balances). In
production both Lambdas import this from the shared Lambda Layer; in the MVP the
data is inlined so the flow runs without a real core.

IMPORTANT — MVP semantics: the dataset is a module-level constant, so any
balance mutation performed by ``execute_transfer`` only lives for the duration
of a single Lambda invocation (each cold/warm start re-reads the same constant).
This is acceptable for the MVP because the receipt is what matters; in
production these reads/writes become calls to the real core via PrivateLink.

Helpers exposed:

* :func:`get_client_by_phone` — resolve a client by their E.164 phone number.
* :func:`find_account_by_number` — resolve a product/account by its account
  number, optionally scoped to a single client (used for source accounts) or
  searched across every client (used for destination accounts).
* :func:`is_valid_destination` — whether an account number is a known, valid
  BRE-B destination (any account present in Mock_Core).
* :data:`VALID_DESTINATION_ACCOUNTS` — the set of all known account numbers.
"""

from __future__ import annotations

from shared.types import MockClient, MockProduct

# ---------------------------------------------------------------------------
# Canonical Mock_Core dataset (must stay in sync with balance-query).
# ---------------------------------------------------------------------------

MOCK_CLIENTS: list[MockClient] = [
    {
        "phone_number": "+573001234567",
        "name": "Carlos Rodríguez",
        "email": "carlos.rodriguez@example.com",
        "document_id": "1234567890",
        "products": [
            {
                "account_id": "ACC-001",
                "account_number": "2001234567",
                "product_type": "fondo_inversion",
                "product_name": "Fondo BTG Pactual Liquidez",
                "currency": "COP",
                "available_balance": 12_500_000.00,
                "total_balance": 12_500_000.00,
                "cutoff_date": "2024-12-15",
            },
            {
                "account_id": "ACC-002",
                "account_number": "1001234568",
                "product_type": "cuenta_corriente",
                "product_name": "Cuenta Corriente BTG",
                "currency": "COP",
                "available_balance": 3_750_000.50,
                "total_balance": 4_200_000.50,
                "cutoff_date": "2024-12-15",
            },
        ],
        "transactions": [
            {"transaction_id": "TRX-001", "account_id": "ACC-002", "date": "2024-12-14T10:30:00Z", "description": "Nómina Empresa XYZ", "amount": 5_000_000, "currency": "COP", "type": "credit"},
            {"transaction_id": "TRX-002", "account_id": "ACC-002", "date": "2024-12-13T15:45:00Z", "description": "Pago servicios públicos", "amount": -350_000, "currency": "COP", "type": "debit"},
            {"transaction_id": "TRX-003", "account_id": "ACC-002", "date": "2024-12-12T09:00:00Z", "description": "Transferencia a Fondo", "amount": -2_000_000, "currency": "COP", "type": "debit"},
            {"transaction_id": "TRX-004", "account_id": "ACC-001", "date": "2024-12-12T09:01:00Z", "description": "Aporte desde Cuenta Corriente", "amount": 2_000_000, "currency": "COP", "type": "credit"},
            {"transaction_id": "TRX-005", "account_id": "ACC-002", "date": "2024-12-10T14:20:00Z", "description": "Compra Rappi", "amount": -85_000, "currency": "COP", "type": "debit"},
        ],
    },
    {
        "phone_number": "+573009876543",
        "name": "María López",
        "email": "maria.lopez@example.com",
        "document_id": "0987654321",
        "products": [
            {
                "account_id": "ACC-003",
                "account_number": "2009876543",
                "product_type": "fondo_inversion",
                "product_name": "Fondo BTG Pactual Renta Fija",
                "currency": "COP",
                "available_balance": 25_000_000.00,
                "total_balance": 25_000_000.00,
                "cutoff_date": "2024-12-15",
            },
            {
                "account_id": "ACC-004",
                "account_number": "1009876544",
                "product_type": "cuenta_corriente",
                "product_name": "Cuenta Corriente BTG",
                "currency": "COP",
                "available_balance": 8_750_000.50,
                "total_balance": 8_750_000.50,
                "cutoff_date": "2024-12-15",
            },
        ],
        "transactions": [
            {"transaction_id": "TRX-006", "account_id": "ACC-004", "date": "2024-12-14T08:00:00Z", "description": "Transferencia recibida", "amount": 3_000_000, "currency": "COP", "type": "credit"},
            {"transaction_id": "TRX-007", "account_id": "ACC-004", "date": "2024-12-11T16:30:00Z", "description": "Pago tarjeta de crédito", "amount": -1_500_000, "currency": "COP", "type": "debit"},
        ],
    },
    {
        "phone_number": "+573005551234",
        "name": "Juan García",
        "email": "juan.garcia@example.com",
        "document_id": "1122334455",
        "products": [
            {
                "account_id": "ACC-005",
                "account_number": "1005551234",
                "product_type": "cuenta_corriente",
                "product_name": "Cuenta Corriente BTG",
                "currency": "COP",
                "available_balance": 1_200_000.00,
                "total_balance": 1_200_000.00,
                "cutoff_date": "2024-12-15",
            },
        ],
        "transactions": [
            {"transaction_id": "TRX-008", "account_id": "ACC-005", "date": "2024-12-13T11:00:00Z", "description": "Depósito efectivo", "amount": 500_000, "currency": "COP", "type": "credit"},
        ],
    },
    {
        "phone_number": "+573193928783",
        "name": "Juan Salgado",
        "email": "juan.salgado@example.com",
        "document_id": "4040404040",
        "products": [
            {
                "account_id": "ACC-006",
                "account_number": "1003928783",
                "product_type": "cuenta_corriente",
                "product_name": "Cuenta Corriente BTG",
                "currency": "COP",
                "available_balance": 7_500_000.00,
                "total_balance": 7_500_000.00,
                "cutoff_date": "2024-12-15",
            },
            {
                "account_id": "ACC-007",
                "account_number": "2003928783",
                "product_type": "fondo_inversion",
                "product_name": "Fondo BTG Pactual Liquidez",
                "currency": "COP",
                "available_balance": 18_000_000.00,
                "total_balance": 18_000_000.00,
                "cutoff_date": "2024-12-15",
            },
        ],
        "transactions": [
            {"transaction_id": "TRX-009", "account_id": "ACC-006", "date": "2024-12-14T09:00:00Z", "description": "Nómina", "amount": 6_000_000, "currency": "COP", "type": "credit"},
        ],
    },
    {
        "phone_number": "+573118918239",
        "name": "Ricardo Bachiller",
        "email": "ricardo.bachiller@example.com",
        "document_id": "1010101010",
        "products": [
            {
                "account_id": "ACC-008",
                "account_number": "1008918239",
                "product_type": "cuenta_corriente",
                "product_name": "Cuenta Corriente BTG",
                "currency": "COP",
                "available_balance": 4_800_000.00,
                "total_balance": 4_800_000.00,
                "cutoff_date": "2024-12-15",
            },
            {
                "account_id": "ACC-009",
                "account_number": "2008918239",
                "product_type": "fondo_inversion",
                "product_name": "Fondo BTG Pactual Acciones",
                "currency": "COP",
                "available_balance": 22_000_000.00,
                "total_balance": 22_000_000.00,
                "cutoff_date": "2024-12-15",
            },
        ],
        "transactions": [
            {"transaction_id": "TRX-010", "account_id": "ACC-008", "date": "2024-12-10T08:30:00Z", "description": "Nómina", "amount": 5_500_000, "currency": "COP", "type": "credit"},
            {"transaction_id": "TRX-011", "account_id": "ACC-008", "date": "2024-12-13T14:00:00Z", "description": "Pago arriendo", "amount": -1_800_000, "currency": "COP", "type": "debit"},
        ],
    },
    {
        "phone_number": "+573197968449",
        "name": "Jersons",
        "email": "jersons@example.com",
        "document_id": "2020202020",
        "products": [
            {
                "account_id": "ACC-010",
                "account_number": "1007968449",
                "product_type": "cuenta_corriente",
                "product_name": "Cuenta Corriente BTG",
                "currency": "COP",
                "available_balance": 10_200_000.00,
                "total_balance": 10_200_000.00,
                "cutoff_date": "2024-12-15",
            },
            {
                "account_id": "ACC-011",
                "account_number": "2007968449",
                "product_type": "fondo_inversion",
                "product_name": "Fondo BTG Pactual Liquidez",
                "currency": "COP",
                "available_balance": 35_000_000.00,
                "total_balance": 35_000_000.00,
                "cutoff_date": "2024-12-15",
            },
        ],
        "transactions": [
            {"transaction_id": "TRX-012", "account_id": "ACC-010", "date": "2024-12-05T09:00:00Z", "description": "Transferencia recibida", "amount": 8_000_000, "currency": "COP", "type": "credit"},
            {"transaction_id": "TRX-013", "account_id": "ACC-010", "date": "2024-12-12T16:45:00Z", "description": "Pago universidad", "amount": -2_300_000, "currency": "COP", "type": "debit"},
        ],
    },
    {
        "phone_number": "+573002148017",
        "name": "Sebas Coneo",
        "email": "sebas.coneo@example.com",
        "document_id": "4040404040",
        "products": [
            {
                "account_id": "ACC-012",
                "account_number": "1002148017",
                "product_type": "cuenta_corriente",
                "product_name": "Cuenta Corriente BTG",
                "currency": "COP",
                "available_balance": 6_350_000.00,
                "total_balance": 6_350_000.00,
                "cutoff_date": "2024-12-15",
            },
            {
                "account_id": "ACC-013",
                "account_number": "2002148017",
                "product_type": "fondo_inversion",
                "product_name": "Fondo BTG Pactual Renta Fija",
                "currency": "COP",
                "available_balance": 15_500_000.00,
                "total_balance": 15_500_000.00,
                "cutoff_date": "2024-12-15",
            },
        ],
        "transactions": [
            {"transaction_id": "TRX-014", "account_id": "ACC-012", "date": "2024-12-08T09:30:00Z", "description": "Nómina", "amount": 5_000_000, "currency": "COP", "type": "credit"},
            {"transaction_id": "TRX-015", "account_id": "ACC-012", "date": "2024-12-14T20:00:00Z", "description": "Compra tecnología", "amount": -2_500_000, "currency": "COP", "type": "debit"},
        ],
    },
]


# Known valid BRE-B destination accounts: every account number present in
# Mock_Core. A transfer whose destination is not in this set is rejected with
# ``InvalidDestinationError`` (Requirement 8.10).
VALID_DESTINATION_ACCOUNTS: frozenset[str] = frozenset(
    product["account_number"]
    for client in MOCK_CLIENTS
    for product in client["products"]
)


def get_client_by_phone(phone_number: str) -> MockClient | None:
    """Return the Mock_Core client for ``phone_number`` (E.164), or ``None``.

    Args:
        phone_number: The client's phone number in E.164 format
            (e.g. ``"+573001234567"``).

    Returns:
        The matching :class:`~shared.types.MockClient`, or ``None`` when no
        client has that phone number.
    """
    for client in MOCK_CLIENTS:
        if client["phone_number"] == phone_number:
            return client
    return None


def find_account_by_number(
    phone_number: str | None,
    account_number: str,
) -> MockProduct | None:
    """Resolve a product/account by its account number.

    Args:
        phone_number: When provided, the search is scoped to that client's
            products only — used to confirm a *source* account both exists and
            belongs to the requesting client. When ``None``, every client's
            products are searched — used to confirm a *destination* account
            exists anywhere in Mock_Core.
        account_number: The account number to look up.

    Returns:
        The matching :class:`~shared.types.MockProduct`, or ``None`` if not
        found (or the scoped client does not exist).
    """
    if phone_number is not None:
        client = get_client_by_phone(phone_number)
        clients = [client] if client else []
    else:
        clients = MOCK_CLIENTS

    for client in clients:
        for product in client["products"]:
            if product["account_number"] == account_number:
                return product
    return None


def is_valid_destination(account_number: str) -> bool:
    """Return whether ``account_number`` is a known, valid BRE-B destination."""
    return account_number in VALID_DESTINATION_ACCOUNTS


__all__ = [
    "MOCK_CLIENTS",
    "VALID_DESTINATION_ACCOUNTS",
    "get_client_by_phone",
    "find_account_by_number",
    "is_valid_destination",
]
