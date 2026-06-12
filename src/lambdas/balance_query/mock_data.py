"""Mock_Core banking data for the balance-query Lambda.

Synthetic, hardcoded banking data that stands in for the real core banking
system during the hackathon MVP (Requirement 7, design §Mock_Core). Each client
owns exactly two products — a **Fondo de Inversión** and a **Cuenta Corriente** —
plus a few movements used by the statement-generator.

Canonical client / phone / account contract
--------------------------------------------
This Mock_Core is conceptually shared with the ``transfer-breb`` and
``statement-generator`` Lambdas, which are built in parallel. Because each Lambda
is packaged separately, we keep a self-contained copy here, but the **client,
phone and account data below is canonical** — sibling Lambdas MUST mirror it so
the demo stays coherent. Phone numbers and document IDs match the Auth_Service
test users (design §Usuarios de Prueba Hardcodeados):

| Cliente          | phone_number   | document_id | Cuenta Corriente (account_id / number) | Fondo de Inversión (account_id / number) |
|------------------|----------------|-------------|----------------------------------------|------------------------------------------|
| Carlos Rodríguez | +573001234567  | 1234567890  | CC-001 / 4001234567                    | FI-001 / 9001234567                      |
| María López      | +573009876543  | 0987654321  | CC-002 / 4009876543                    | FI-002 / 9009876543                      |
| Juan García      | +573005551234  | 1122334455  | CC-003 / 4005551234                    | FI-003 / 9005551234                      |

All amounts are in COP. ``cutoff_date`` is the balance cut-off date (ISO 8601).
"""

from __future__ import annotations

from shared.types import MockClient

# Canonical balance cut-off date for the MVP demo dataset.
CUTOFF_DATE: str = "2025-01-31"

# Mock_Core: the canonical synthetic clients. Keep this list in sync with the
# transfer-breb and statement-generator Lambdas (see module docstring).
MOCK_CLIENTS: list[MockClient] = [
    {
        "phone_number": "+573001234567",
        "name": "Carlos Rodríguez",
        "email": "carlos.rodriguez@example.com",
        "document_id": "1234567890",
        "products": [
            {
                "account_id": "CC-001",
                "account_number": "4001234567",
                "product_type": "cuenta_corriente",
                "product_name": "Cuenta Corriente BTG",
                "currency": "COP",
                "available_balance": 5_250_000.00,
                "total_balance": 5_250_000.00,
                "cutoff_date": CUTOFF_DATE,
            },
            {
                "account_id": "FI-001",
                "account_number": "9001234567",
                "product_type": "fondo_inversion",
                "product_name": "Fondo Renta Fija BTG",
                "currency": "COP",
                "available_balance": 12_800_000.50,
                "total_balance": 13_000_000.00,
                "cutoff_date": CUTOFF_DATE,
            },
        ],
        "transactions": [
            {
                "transaction_id": "TX-CC-001-01",
                "account_id": "CC-001",
                "date": "2025-01-05T09:15:00-05:00",
                "description": "Pago nómina enero",
                "amount": 4_500_000.00,
                "currency": "COP",
                "type": "credit",
            },
            {
                "transaction_id": "TX-CC-001-02",
                "account_id": "CC-001",
                "date": "2025-01-12T18:40:00-05:00",
                "description": "Compra supermercado",
                "amount": 320_000.00,
                "currency": "COP",
                "type": "debit",
            },
        ],
    },
    {
        "phone_number": "+573009876543",
        "name": "María López",
        "email": "maria.lopez@example.com",
        "document_id": "0987654321",
        "products": [
            {
                "account_id": "CC-002",
                "account_number": "4009876543",
                "product_type": "cuenta_corriente",
                "product_name": "Cuenta Corriente BTG",
                "currency": "COP",
                "available_balance": 3_120_000.00,
                "total_balance": 3_120_000.00,
                "cutoff_date": CUTOFF_DATE,
            },
            {
                "account_id": "FI-002",
                "account_number": "9009876543",
                "product_type": "fondo_inversion",
                "product_name": "Fondo Acciones BTG",
                "currency": "COP",
                "available_balance": 8_450_000.75,
                "total_balance": 8_450_000.75,
                "cutoff_date": CUTOFF_DATE,
            },
        ],
        "transactions": [
            {
                "transaction_id": "TX-CC-002-01",
                "account_id": "CC-002",
                "date": "2025-01-08T11:00:00-05:00",
                "description": "Transferencia recibida",
                "amount": 1_200_000.00,
                "currency": "COP",
                "type": "credit",
            },
            {
                "transaction_id": "TX-FI-002-01",
                "account_id": "FI-002",
                "date": "2025-01-20T10:30:00-05:00",
                "description": "Aporte a fondo",
                "amount": 500_000.00,
                "currency": "COP",
                "type": "credit",
            },
        ],
    },
    {
        "phone_number": "+573005551234",
        "name": "Juan García",
        "email": "juan.garcia@example.com",
        "document_id": "1122334455",
        "products": [
            {
                "account_id": "CC-003",
                "account_number": "4005551234",
                "product_type": "cuenta_corriente",
                "product_name": "Cuenta Corriente BTG",
                "currency": "COP",
                "available_balance": 980_500.00,
                "total_balance": 980_500.00,
                "cutoff_date": CUTOFF_DATE,
            },
            {
                "account_id": "FI-003",
                "account_number": "9005551234",
                "product_type": "fondo_inversion",
                "product_name": "Fondo Liquidez BTG",
                "currency": "COP",
                "available_balance": 25_000_000.00,
                "total_balance": 25_000_000.00,
                "cutoff_date": CUTOFF_DATE,
            },
        ],
        "transactions": [
            {
                "transaction_id": "TX-CC-003-01",
                "account_id": "CC-003",
                "date": "2025-01-03T08:05:00-05:00",
                "description": "Pago servicios públicos",
                "amount": 210_000.00,
                "currency": "COP",
                "type": "debit",
            },
            {
                "transaction_id": "TX-FI-003-01",
                "account_id": "FI-003",
                "date": "2025-01-15T14:20:00-05:00",
                "description": "Rendimientos fondo",
                "amount": 180_000.00,
                "currency": "COP",
                "type": "credit",
            },
        ],
    },
]


def get_client_by_phone(phone_number: str) -> MockClient | None:
    """Return the Mock_Core client matching ``phone_number`` (E.164), or ``None``.

    The lookup is exact on the stored E.164 number; surrounding whitespace on the
    input is tolerated. Returns ``None`` when no client owns that number, which
    the handler maps to the Requirement 7.4 "no product info found" error.

    Args:
        phone_number: The client's phone number in E.164 format (e.g.
            ``"+573001234567"``).

    Returns:
        The matching :class:`~shared.types.MockClient`, or ``None`` if not found.
    """
    if not phone_number:
        return None

    normalized = phone_number.strip()
    for client in MOCK_CLIENTS:
        if client["phone_number"] == normalized:
            return client
    return None


__all__ = ["CUTOFF_DATE", "MOCK_CLIENTS", "get_client_by_phone"]
