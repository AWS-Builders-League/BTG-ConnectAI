"""Strands Agent tools — thin wrappers over the Action Group Lambdas.

The Strands Agent exposes three tools (design §8 *Definición de Tools*). Each one
invokes the corresponding Action Group Lambda via ``boto3 lambda.invoke`` and
returns the parsed JSON payload so the model can render a natural reply:

* :func:`query_balance` → ``balance-query`` (Requirement 7).
* :func:`initiate_transfer_breb` → ``transfer-breb-initiator`` which fires the
  ``TransferBrebStateMachine`` and returns **immediately** with an
  ``executionArn``; the OTP is sent by SMS and handled asynchronously — the tool
  never waits for it (Requirements 8.3, 12, 18.7).
* :func:`generate_statement` → ``statement-generator`` (Requirement 9).

Passing ``phoneNumber`` / ``correlationId`` to the tools
--------------------------------------------------------
Strands tools are module-level functions whose *signature + docstring* become the
schema the model sees, so the model can only fill parameters it is told about.
The client's ``phoneNumber`` and the request ``correlationId`` are **not** known
to the model — they come from the invocation, not the conversation. We therefore
carry them in :mod:`contextvars` set per invocation by the handler via
:func:`invocation_context`, and the tools read them with :func:`current_phone_number`
/ :func:`current_correlation_id`. ``phone_number`` is kept as an *optional*
parameter on the balance/statement tools (default ``None``) so the functions stay
directly unit-testable, but in production the model omits it and the contextvar
supplies the authenticated number — the model can never spoof another client's
phone. ``correlationId`` is never exposed to the model at all.

Surfacing a generated statement to the handler
-----------------------------------------------
When :func:`generate_statement` succeeds it records the ``{s3Bucket, s3Key,
fileName}`` reference into a per-invocation sink (:func:`current_statement_ref` /
set internally). After the agent run the handler reads it with
:func:`get_statement_reference` and lifts it into the structured
``{"response": {"text": ..., "statement": {...}}}`` payload the Message_Processor
expects, instead of relying on the model to echo the S3 keys back verbatim.

``strands`` optional at import time
-----------------------------------
The ``@tool`` decorator comes from the Strands SDK, which may be absent in some
test environments. We import it defensively: if ``strands`` is unavailable the
decorator degrades to a no-op passthrough, so this module (and the Lambda-invoking
logic it contains) imports and runs without the SDK. A ``@tool``-decorated
function remains directly callable and returns the underlying ``dict`` either way.

Environment variables (resolved from the cross-stack contract):
* ``BALANCE_QUERY_FUNCTION_NAME`` — name/ARN of the ``balance-query`` Lambda.
* ``TRANSFER_INITIATOR_FUNCTION_NAME`` — name/ARN of ``transfer-breb-initiator``.
* ``STATEMENT_GENERATOR_FUNCTION_NAME`` — name/ARN of ``statement-generator``.
All are read lazily, so importing this module never requires the environment to
be configured.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
from typing import Any, Iterator

import boto3

from shared.logger import get_logger
from shared.masking import mask_account, mask_phone

logger = get_logger("ai-agent")

# --- Optional strands import -------------------------------------------------
# Keep the Lambda-invoking logic importable/testable even without the SDK.
try:  # pragma: no cover - exercised indirectly depending on environment
    from strands import tool
except ImportError:  # pragma: no cover - fallback when strands is absent

    def tool(func: Any = None, **_kwargs: Any) -> Any:
        """No-op fallback for :func:`strands.tool` when the SDK is unavailable.

        Supports both bare (``@tool``) and parameterized (``@tool(...)``) usage
        and returns the wrapped function unchanged so it stays directly callable.
        """
        if func is None:
            return lambda f: f
        return func


# --- Environment variable names ----------------------------------------------
BALANCE_QUERY_FUNCTION_ENV: str = "BALANCE_QUERY_FUNCTION_NAME"
TRANSFER_INITIATOR_FUNCTION_ENV: str = "TRANSFER_INITIATOR_FUNCTION_NAME"
STATEMENT_GENERATOR_FUNCTION_ENV: str = "STATEMENT_GENERATOR_FUNCTION_NAME"

# Module-level Lambda client reused across warm invocations.
_lambda_client = boto3.client("lambda")

# --- Per-invocation context --------------------------------------------------
# Defaults are ``None``; the handler sets concrete values for the duration of one
# Lambda invocation. ContextVars are isolated per execution context, so warm
# container reuse never leaks one client's phone number into another's request.
_phone_number_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ai_agent_phone_number", default=None
)
_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ai_agent_correlation_id", default=None
)
# Sink for the statement reference produced during the invocation (if any). A
# fresh list is installed per invocation by :func:`invocation_context`.
_statement_ref_var: contextvars.ContextVar[list[dict[str, str]]] = (
    contextvars.ContextVar("ai_agent_statement_ref", default=[])
)


def current_phone_number() -> str | None:
    """Return the authenticated phone number for the current invocation."""
    return _phone_number_var.get()


def current_correlation_id() -> str | None:
    """Return the request correlation id for the current invocation."""
    return _correlation_id_var.get()


@contextlib.contextmanager
def invocation_context(
    phone_number: str, correlation_id: str | None = None
) -> Iterator[None]:
    """Bind the per-invocation ``phoneNumber`` / ``correlationId`` for the tools.

    Installs a fresh statement-reference sink and sets the phone/correlation
    context vars for the duration of the ``with`` block, restoring the previous
    values on exit (so warm-container reuse stays isolated).

    Args:
        phone_number: The authenticated client's phone number (E.164).
        correlation_id: The request correlation id propagated from the
            Message_Processor (used as the idempotent transfer execution name).
    """
    phone_token = _phone_number_var.set(phone_number)
    corr_token = _correlation_id_var.set(correlation_id)
    sink_token = _statement_ref_var.set([])
    try:
        yield
    finally:
        _phone_number_var.reset(phone_token)
        _correlation_id_var.reset(corr_token)
        _statement_ref_var.reset(sink_token)


def get_statement_reference() -> dict[str, str] | None:
    """Return the statement reference recorded during the invocation, if any.

    When :func:`generate_statement` produced a PDF, returns the most recent
    ``{"s3Bucket", "s3Key", "fileName"}`` reference; otherwise ``None``.
    """
    refs = _statement_ref_var.get()
    return refs[-1] if refs else None


def _record_statement_reference(reference: dict[str, str]) -> None:
    """Append a statement S3 reference to the current invocation sink."""
    # ``ContextVar`` holds the list by reference, so mutating in place is enough
    # and avoids needing a token here.
    _statement_ref_var.get().append(reference)


def _resolve_phone_number(provided: str | None) -> str:
    """Return the phone number to use: explicit arg, else the context var.

    Raises:
        ValueError: If neither an explicit ``phone_number`` nor a context value
            is available (a programming/wiring error, not a model concern).
    """
    phone_number = provided or current_phone_number()
    if not phone_number:
        raise ValueError(
            "phone_number is not available: neither passed explicitly nor set "
            "in the invocation context"
        )
    return phone_number


def _invoke_lambda(function_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Synchronously invoke an Action Group Lambda and parse its JSON payload.

    Args:
        function_name: The target Lambda name/ARN.
        payload: The JSON-serializable request payload.

    Returns:
        The parsed response ``dict``. On a function-level error or a non-dict
        payload, returns a uniform ``{"success": False, "error": ...}`` shape so
        the model can apologize gracefully rather than crashing the turn.
    """
    response = _lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    raw = response["Payload"].read()

    if response.get("FunctionError"):
        logger.error(
            "action group lambda returned a function error",
            extra={
                "function": function_name,
                "functionError": response["FunctionError"],
            },
        )
        return {
            "success": False,
            "error": "tool_invocation_failed",
            "message": (
                "No fue posible completar la operación en este momento. "
                "Inténtalo nuevamente en unos minutos."
            ),
        }

    result = json.loads(raw)
    if not isinstance(result, dict):
        return {"success": True, "data": result}
    return result


@tool
def query_balance(product_type: str | None = None, phone_number: str | None = None) -> dict:
    """Consulta los saldos del cliente en BTG Pactual.

    No proporciones phone_number: el sistema inyecta automáticamente el número
    del cliente autenticado.

    Args:
        product_type: Opcional. "fondo_inversion" o "cuenta_corriente". Si se
            omite, retorna todos los productos del cliente.
        phone_number: Uso interno del sistema. No lo completes.

    Returns:
        dict con la lista de productos y sus saldos (available_balance,
        total_balance, cutoff_date) o un error si el cliente no existe.
    """
    resolved_phone = _resolve_phone_number(phone_number)
    function_name = os.environ[BALANCE_QUERY_FUNCTION_ENV]

    logger.info(
        "tool query_balance",
        extra={"phone": mask_phone(resolved_phone), "product_type": product_type},
    )

    return _invoke_lambda(
        function_name,
        {"phoneNumber": resolved_phone, "productType": product_type},
    )


@tool
def initiate_transfer_breb(
    source_account: str,
    destination_account: str,
    amount: float,
    concept: str = "",
    phone_number: str | None = None,
) -> dict:
    """Inicia una transferencia BRE-B entre cuentas.

    Dispara el TransferBrebStateMachine, que enviará un código OTP por SMS al
    cliente para autorizar la transferencia. NO espera el OTP: retorna de
    inmediato. Úsala SOLO después de que el cliente confirmó explícitamente la
    operación (cuenta origen, cuenta destino, monto y concepto).

    No proporciones phone_number: el sistema inyecta automáticamente el número
    del cliente autenticado.

    Args:
        source_account: Cuenta origen del cliente.
        destination_account: Cuenta destino de la transferencia.
        amount: Monto a transferir en COP.
        concept: Concepto o descripción de la transferencia (opcional).
        phone_number: Uso interno del sistema. No lo completes.

    Returns:
        dict con {executionArn, status, message} indicando que se envió el OTP
        por SMS. El cliente debe responder ese código para autorizar.
    """
    resolved_phone = _resolve_phone_number(phone_number)
    correlation_id = current_correlation_id()
    function_name = os.environ[TRANSFER_INITIATOR_FUNCTION_ENV]

    logger.info(
        "tool initiate_transfer_breb",
        extra={
            "phone": mask_phone(resolved_phone),
            "source": mask_account(source_account),
            "destination": mask_account(destination_account),
            "amount": amount,
        },
    )

    payload: dict[str, Any] = {
        "phoneNumber": resolved_phone,
        "sourceAccount": source_account,
        "destinationAccount": destination_account,
        "amount": amount,
        "concept": concept,
    }
    # The initiator uses correlationId as the idempotent execution name
    # (Property 19); only include it when present.
    if correlation_id:
        payload["correlationId"] = correlation_id

    result = _invoke_lambda(function_name, payload)

    # Surface a clear, Spanish, OTP-by-SMS instruction to the model regardless of
    # the exact shape the initiator returns (Requirement 8.3, 12). The agent must
    # NOT wait for the OTP synchronously.
    result.setdefault(
        "message",
        (
            "Te envié un código OTP por SMS. Por favor respóndelo aquí para "
            "autorizar la transferencia."
        ),
    )
    return result


@tool
def generate_statement(
    cutoff_date: str,
    account_id: str | None = None,
    phone_number: str | None = None,
) -> dict:
    """Genera un extracto bancario en PDF hasta una fecha de corte.

    No proporciones phone_number: el sistema inyecta automáticamente el número
    del cliente autenticado.

    Args:
        cutoff_date: Fecha de corte (ISO 8601, AAAA-MM-DD). DEBE ser una fecha
            pasada; una fecha futura se rechaza.
        account_id: Opcional. ID de la cuenta. Si se omite, se usa la cuenta
            corriente del cliente.
        phone_number: Uso interno del sistema. No lo completes.

    Returns:
        dict con {success, s3Bucket, s3Key, fileName, message} o un error si la
        fecha es futura o el cliente/cuenta no existe. El sistema entrega el PDF
        al cliente por WhatsApp automáticamente.
    """
    resolved_phone = _resolve_phone_number(phone_number)
    function_name = os.environ[STATEMENT_GENERATOR_FUNCTION_ENV]

    logger.info(
        "tool generate_statement",
        extra={
            "phone": mask_phone(resolved_phone),
            "account_id": account_id,
            "cutoff_date": cutoff_date,
        },
    )

    payload: dict[str, Any] = {
        "phoneNumber": resolved_phone,
        "cutoffDate": cutoff_date,
    }
    if account_id:
        payload["accountId"] = account_id

    result = _invoke_lambda(function_name, payload)

    # If a PDF was generated, record its S3 reference so the handler can attach
    # it to the structured response (the model need not echo the keys back).
    if result.get("success") and result.get("s3Bucket") and result.get("s3Key"):
        _record_statement_reference(
            {
                "s3Bucket": result["s3Bucket"],
                "s3Key": result["s3Key"],
                "fileName": result.get("fileName", ""),
            }
        )

    return result


# The objects the Agent registers as tools (design §8). Listing them explicitly
# keeps the agent wiring decoupled from this module's internals.
TOOLS = [query_balance, initiate_transfer_breb, generate_statement]


__all__ = [
    "query_balance",
    "initiate_transfer_breb",
    "generate_statement",
    "TOOLS",
    "invocation_context",
    "current_phone_number",
    "current_correlation_id",
    "get_statement_reference",
    "BALANCE_QUERY_FUNCTION_ENV",
    "TRANSFER_INITIATOR_FUNCTION_ENV",
    "STATEMENT_GENERATOR_FUNCTION_ENV",
]
