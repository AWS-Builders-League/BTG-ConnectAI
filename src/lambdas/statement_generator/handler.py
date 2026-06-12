"""statement-generator Action Group Lambda — main handler.

Implements Requirement 9 (Generación de Extractos). The Strands_Agent
``generate_statement`` tool invokes this Lambda via ``boto3 lambda.invoke`` with
a simple JSON payload (design §5 statement-generator)::

    {"phoneNumber": "+573001234567",
     "accountId": "CC-001",          # optional
     "cutoffDate": "2025-01-15"}

``accountId`` is optional; when omitted the client's *Cuenta Corriente* (or the
first product) is used. For interoperability the handler also accepts the
snake_case keys ``phone_number`` / ``account_id`` / ``cutoff_date``.

Flow (Requirement 9.2, 9.3, 9.5, 9.6):
    1. Validate ``cutoffDate``. If it is a **future** date → return a
       ``future_date`` error asking the client for a past date (Req 9.2).
    2. Resolve the client by phone; unknown client → ``client_not_found`` error.
    3. Select the account (``accountId`` or default *Cuenta Corriente*).
    4. Filter the client's transactions for that account up to the cut-off
       (``date <= cutoff``). An empty result still produces a PDF (Req 9.6).
    5. Render the PDF (Req 9.5) and ``put_object`` it into the Statement_Bucket
       (``STATEMENT_BUCKET`` env, created by ``infra``, reached via the S3
       Gateway Endpoint). The closing balance is the product's
       ``available_balance`` — a documented MVP simplification, since Mock_Core
       does not carry per-date balance history.
    6. Return the S3 reference so the Message_Processor can download the PDF and
       deliver it over WhatsApp (Req 9.4). This Lambda does **not** publish to
       any SQS notification queue — the statement is delivered only via WhatsApp.

Response contract
-----------------
Success::

    {"success": True,
     "s3Bucket": "...", "s3Key": "statements/.../<uuid>.pdf",
     "fileName": "extracto_<account>_<cutoff>.pdf",
     "message": "..."}

Future date (Req 9.2)::

    {"success": False, "error": "future_date",
     "message": "La fecha de corte debe ser una fecha pasada..."}

This Lambda runs inside the VPC in production (banking domain) and writes to S3
through the Gateway Endpoint; that is a deployment concern (Task 15) and needs
no code change here.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime
from typing import Any

import boto3

from shared.formatting import format_cop
from shared.logger import get_logger
from shared.masking import mask_account
from shared.types import MockClient, MockProduct, MockTransaction

from .mock_data import get_client_by_phone
from .pdf_generator import generate_statement_pdf

logger = get_logger("statement-generator")

# Module-level S3 client (reused across warm invocations). Reaches the
# Statement_Bucket through the S3 Gateway Endpoint when running in-VPC.
_s3_client = boto3.client("s3")

_PDF_CONTENT_TYPE = "application/pdf"


def _today() -> date:
    """Return today's date (UTC). Isolated so tests can reason about it."""
    return datetime.utcnow().date()


def _parse_cutoff_date(raw: str | None) -> date | None:
    """Parse a ``YYYY-MM-DD`` cut-off date, or ``None`` if missing/invalid."""
    if not raw:
        return None
    try:
        # Accept full ISO datetimes too, keeping only the date component.
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _select_account(
    client: MockClient, account_id: str | None
) -> MockProduct | None:
    """Pick the requested product, else the Cuenta Corriente, else the first.

    Returns ``None`` only when the client has no products at all, or when an
    explicit ``account_id`` was given that does not match any product.
    """
    products = client.get("products", [])
    if not products:
        return None

    if account_id:
        wanted = str(account_id).strip()
        for product in products:
            if product["account_id"] == wanted:
                return product
        return None

    # Default: prefer the Cuenta Corriente; fall back to the first product.
    for product in products:
        if product["product_type"] == "cuenta_corriente":
            return product
    return products[0]


def _filter_transactions(
    client: MockClient, account_id: str, cutoff: date
) -> list[MockTransaction]:
    """Return the account's movements dated on or before ``cutoff``."""
    selected: list[MockTransaction] = []
    for tx in client.get("transactions", []):
        if tx.get("account_id") != account_id:
            continue
        tx_date = _parse_cutoff_date(tx.get("date"))
        if tx_date is not None and tx_date <= cutoff:
            selected.append(tx)
    # Chronological order for the statement table.
    selected.sort(key=lambda t: str(t.get("date", "")))
    return selected


def generate_statement(
    phone_number: str,
    cutoff_date: str | None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Generate a PDF statement, store it in S3 and return the reference.

    See the module docstring for the full flow. Returns a JSON-serializable
    ``ActionGroupResponse`` dict.
    """
    cutoff = _parse_cutoff_date(cutoff_date)
    if cutoff is None:
        logger.info("statement: missing or invalid cutoff date")
        return {
            "success": False,
            "error": "invalid_date",
            "message": (
                "No se reconoció la fecha de corte. Indica una fecha válida "
                "en formato AAAA-MM-DD."
            ),
        }

    # Requirement 9.2: a future cut-off date is rejected.
    if cutoff > _today():
        logger.info("statement: future cutoff rejected")
        return {
            "success": False,
            "error": "future_date",
            "message": (
                "La fecha de corte debe ser una fecha pasada. Por favor "
                "indícame una nueva fecha de corte."
            ),
        }

    client = get_client_by_phone(phone_number)
    if client is None:
        logger.info("statement: client not found")
        return {
            "success": False,
            "error": "client_not_found",
            "message": (
                "No se encontró información de productos para el cliente."
            ),
        }

    account = _select_account(client, account_id)
    if account is None:
        logger.info(
            "statement: account not found",
            extra={"requested_account_id": account_id},
        )
        return {
            "success": False,
            "error": "account_not_found",
            "message": (
                "No se encontró el producto solicitado para generar el "
                "extracto."
            ),
        }

    cutoff_iso = cutoff.isoformat()
    transactions = _filter_transactions(client, account["account_id"], cutoff)

    logger.info(
        "statement: rendering",
        extra={
            "client_document_masked": mask_account(client["document_id"]),
            "account_masked": mask_account(account["account_number"]),
            "cutoff_date": cutoff_iso,
            "movement_count": len(transactions),
        },
    )

    pdf_bytes = generate_statement_pdf(
        client=client,
        account=account,
        transactions=transactions,
        cutoff_date=cutoff_iso,
    )

    bucket = os.environ.get("STATEMENT_BUCKET")
    if not bucket:
        logger.error("statement: STATEMENT_BUCKET env var not configured")
        return {
            "success": False,
            "error": "configuration_error",
            "message": (
                "No fue posible generar el extracto en este momento. "
                "Inténtalo más tarde."
            ),
        }

    # Partition keys by client document for tidy, low-cardinality prefixes; the
    # UUID guarantees uniqueness so concurrent requests never collide.
    document_id = client.get("document_id", "unknown")
    s3_key = f"statements/{document_id}/{uuid.uuid4()}.pdf"
    file_name = f"extracto_{account['account_id']}_{cutoff_iso}.pdf"

    _s3_client.put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=pdf_bytes,
        ContentType=_PDF_CONTENT_TYPE,
    )

    logger.info(
        "statement: stored",
        extra={"s3_key": s3_key, "size_bytes": len(pdf_bytes)},
    )

    closing_balance = account.get(
        "available_balance", account.get("total_balance", 0)
    )
    if transactions:
        message = (
            f"Extracto generado con {len(transactions)} movimiento(s). "
            f"Saldo final: {format_cop(closing_balance)}."
        )
    else:
        # Requirement 9.6: empty statement.
        message = (
            "Extracto generado. No se encontraron movimientos para el "
            f"período. Saldo final: {format_cop(closing_balance)}."
        )

    return {
        "success": True,
        "s3Bucket": bucket,
        "s3Key": s3_key,
        "fileName": file_name,
        "message": message,
    }


def _extract_inputs(
    event: dict[str, Any],
) -> tuple[str, str | None, str | None]:
    """Pull ``phoneNumber`` / ``cutoffDate`` / ``accountId`` from the event.

    Accepts both the camelCase keys used by the Strands tool (design contract)
    and snake_case variants for robustness.
    """
    phone_number = event.get("phoneNumber") or event.get("phone_number") or ""
    cutoff_date = event.get("cutoffDate")
    if cutoff_date is None:
        cutoff_date = event.get("cutoff_date")
    account_id = event.get("accountId")
    if account_id is None:
        account_id = event.get("account_id")
    return phone_number, cutoff_date, account_id


def handler(event: dict[str, Any], context: object = None) -> dict[str, Any]:
    """Lambda entry point. Routes the invoke payload to :func:`generate_statement`.

    Args:
        event: The JSON payload from the Strands_Agent ``generate_statement``
            tool.
        context: Lambda context (unused).

    Returns:
        The ``ActionGroupResponse`` dict produced by :func:`generate_statement`.
    """
    event = event or {}
    phone_number, cutoff_date, account_id = _extract_inputs(event)

    if not phone_number:
        logger.warning("statement: missing phoneNumber")
        return {
            "success": False,
            "error": "missing_phone_number",
            "message": "No se recibió el número de teléfono del cliente.",
        }

    return generate_statement(phone_number, cutoff_date, account_id)


__all__ = ["handler", "generate_statement"]
