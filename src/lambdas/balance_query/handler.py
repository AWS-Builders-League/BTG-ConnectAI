"""balance-query Action Group Lambda — main handler.

Implements Requirement 7 (Consulta de Saldos). The Strands_Agent ``query_balance``
tool invokes this Lambda via ``boto3 lambda.invoke`` with a simple JSON payload
(design §5 balance-query, *not* the Bedrock Agents managed format)::

    {"phoneNumber": "+573001234567", "productType": "fondo_inversion"}

``productType`` is optional; when omitted (or null) the handler returns *all*
products for the client (Requirement 7.2). For interoperability the handler also
accepts the snake_case keys ``phone_number`` / ``product_type``.

Response contract (design §ActionGroupResponse)
-----------------------------------------------
Success::

    {"success": True,
     "data": {"phoneNumber": ..., "clientName": ..., "products": [ ... ]},
     "message": "..."}

Each product carries the Requirement 7.3 fields — ``product_type`` (display
label), ``product_name``, ``currency``, ``available_balance``, ``total_balance``,
``cutoff_date`` — plus pre-formatted COP strings (``available_balance_formatted``
/ ``total_balance_formatted``) for the agent to render, while keeping the raw
numeric values.

Client-not-found (Requirement 7.4)::

    {"success": False,
     "error": "client_not_found",
     "message": "No se encontró información de productos para el cliente."}

The response is plain JSON-serializable ``dict`` data. This Lambda runs inside
the VPC in production (banking domain); that is a deployment concern (Task 15)
and needs no code change here.
"""

from __future__ import annotations

from typing import Any

from shared.formatting import format_cop
from shared.logger import get_logger
from shared.masking import mask_account
from shared.types import MockClient, MockProduct

from .mock_data import get_client_by_phone

logger = get_logger("balance-query")

# Canonical product types as stored in Mock_Core, mapped to the Spanish display
# labels required by Requirement 7.3.
_PRODUCT_TYPE_LABELS: dict[str, str] = {
    "fondo_inversion": "Fondo de Inversión",
    "cuenta_corriente": "Cuenta Corriente",
}

# Synonyms / natural variants the agent might pass, normalized to the canonical
# Mock_Core product_type values. Kept intentionally small and explicit.
_PRODUCT_TYPE_SYNONYMS: dict[str, str] = {
    "fondo_inversion": "fondo_inversion",
    "fondo de inversion": "fondo_inversion",
    "fondo de inversión": "fondo_inversion",
    "fondo": "fondo_inversion",
    "fondos": "fondo_inversion",
    "fondos de inversion": "fondo_inversion",
    "fondos de inversión": "fondo_inversion",
    "inversion": "fondo_inversion",
    "inversión": "fondo_inversion",
    "cuenta_corriente": "cuenta_corriente",
    "cuenta corriente": "cuenta_corriente",
    "cuenta": "cuenta_corriente",
    "corriente": "cuenta_corriente",
}

# Disclaimer surfaced with referential financial data (Requirement 7.5 is the
# agent's responsibility, but we echo it so downstream rendering is consistent).
_DISCLAIMER = (
    "Información referencial. Los registros oficiales están en los portales "
    "del banco."
)


def _normalize_product_type(product_type: str | None) -> str | None:
    """Map a requested product type to its canonical Mock_Core value.

    Returns ``None`` when no filter was requested (so all products are returned).
    Unknown values are returned normalized (lowercased/trimmed) so they simply
    match nothing and yield an empty product list rather than raising.
    """
    if product_type is None:
        return None

    key = str(product_type).strip().lower()
    if not key:
        return None

    return _PRODUCT_TYPE_SYNONYMS.get(key, key)


def _serialize_product(product: MockProduct) -> dict[str, Any]:
    """Build the Requirement 7.3 view of a product (raw values + COP formatting)."""
    return {
        "account_id": product["account_id"],
        "account_number_masked": mask_account(product["account_number"]),
        "product_type": product["product_type"],
        "product_type_label": _PRODUCT_TYPE_LABELS.get(
            product["product_type"], product["product_type"]
        ),
        "product_name": product["product_name"],
        "currency": product["currency"],
        "available_balance": product["available_balance"],
        "total_balance": product["total_balance"],
        "available_balance_formatted": format_cop(product["available_balance"]),
        "total_balance_formatted": format_cop(product["total_balance"]),
        "cutoff_date": product["cutoff_date"],
    }


def get_balance(
    phone_number: str, product_type: str | None = None
) -> dict[str, Any]:
    """Return the balances of a client's products from Mock_Core.

    Logic (Requirement 7.1–7.4):
        1. Find the client by ``phone_number``. If none → client-not-found error
           (Req 7.4).
        2. If ``product_type`` is given, filter the client's products to that
           type (Req 7.1). Otherwise return *all* products (Req 7.2).
        3. Each returned product includes the Req 7.3 fields plus formatted COP.

    Args:
        phone_number: Client phone number in E.164 format.
        product_type: Optional product filter — ``"fondo_inversion"`` or
            ``"cuenta_corriente"`` (common Spanish variants are accepted). When
            ``None``/empty, all products are returned.

    Returns:
        A JSON-serializable ``ActionGroupResponse`` dict.
    """
    client: MockClient | None = get_client_by_phone(phone_number)

    if client is None:
        logger.info(
            "balance query: client not found",
            extra={"product_type": product_type},
        )
        return {
            "success": False,
            "error": "client_not_found",
            "message": (
                "No se encontró información de productos para el cliente."
            ),
        }

    canonical_type = _normalize_product_type(product_type)

    products = client["products"]
    if canonical_type is not None:
        products = [p for p in products if p["product_type"] == canonical_type]

    serialized = [_serialize_product(p) for p in products]

    logger.info(
        "balance query resolved",
        extra={
            "client_document_masked": mask_account(client["document_id"]),
            "requested_product_type": product_type,
            "canonical_product_type": canonical_type,
            "product_count": len(serialized),
        },
    )

    message = (
        "No se encontraron productos para el tipo solicitado."
        if not serialized
        else f"Se encontraron {len(serialized)} producto(s)."
    )

    return {
        "success": True,
        "data": {
            "phoneNumber": client["phone_number"],
            "clientName": client["name"],
            "products": serialized,
            "disclaimer": _DISCLAIMER,
        },
        "message": message,
    }


def _extract_inputs(event: dict[str, Any]) -> tuple[str, str | None]:
    """Pull ``phoneNumber`` / ``productType`` from the invoke event.

    Accepts both the camelCase keys used by the Strands tool (design contract)
    and snake_case variants for robustness.
    """
    phone_number = event.get("phoneNumber") or event.get("phone_number") or ""
    product_type = event.get("productType")
    if product_type is None:
        product_type = event.get("product_type")
    return phone_number, product_type


def handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    """Lambda entry point. Routes the invoke payload to :func:`get_balance`.

    Args:
        event: The JSON payload from the Strands_Agent ``query_balance`` tool.
        context: Lambda context (unused).

    Returns:
        The ``ActionGroupResponse`` dict produced by :func:`get_balance`.
    """
    event = event or {}
    phone_number, product_type = _extract_inputs(event)

    if not phone_number:
        logger.warning("balance query missing phoneNumber")
        return {
            "success": False,
            "error": "missing_phone_number",
            "message": "No se recibió el número de teléfono del cliente.",
        }

    return get_balance(phone_number, product_type)


__all__ = ["handler", "get_balance"]
