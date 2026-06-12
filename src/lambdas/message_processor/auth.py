"""Authentication & session module for the Message_Processor Lambda.

Implements the *Auth-Before-Action* principle (design §Principios 10) and the
session-management requirements:

* **Req 5.1** — a banking action without an active Auth_Session must trigger a
  login prompt *before* the request is processed (:func:`send_login_link`,
  :func:`store_pending_request`).
* **Req 5.6 / 6.2** — an active Auth_Session (TTL not expired) lets the client
  operate without re-authenticating (:func:`get_auth_session` + a ``False`` from
  :func:`is_expired`).
* **Req 5.8** — an expired Auth_Session forces re-authentication (a ``True``
  from :func:`is_expired`).
* **Req 6.1** — the Auth_Session lives in DynamoDB with a 30-minute TTL
  (:data:`shared.constants.AUTH_SESSION_TTL`), keyed by phone number. This module
  only *reads* the session; the Auth_Service Lambda writes it on login.
* **Req 11.1 / Property 3** — the conversational session id is derived
  deterministically from the phone number (:func:`derive_session_id`).

Cross-stack / environment contract
----------------------------------
The Auth_Session table is owned by the ``infra`` repo and its name is injected
via the ``AUTH_TABLE_NAME`` environment variable (resolved from the cross-stack
contract ``BTGConnectAI-sandbox-AuthTableName`` / SSM
``/btgconnectai/sandbox/ddb/auth-name``). The variable is read lazily inside the
accessor so the module can be imported without the environment being configured
(tests inject it before calling).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import boto3

from shared.constants import AUTH_SESSION_TTL
from shared.logger import get_logger
from shared.masking import mask_phone

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from shared.types import AuthSession

logger = get_logger("message-processor")

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Fixed UUID namespace for deterministic conversational session-id derivation.
# Generated once and frozen: changing it would invalidate every existing
# Bedrock Agent session mapping, so it must stay constant (Req 11.1 /
# Property 3: Session ID Determinism).
SESSION_ID_NAMESPACE: uuid.UUID = uuid.UUID("6f3c2b1a-8d4e-5f6a-9b0c-1d2e3f4a5b6c")

# Pending requests are stored in the same Auth_Session table under a namespaced
# partition key so they never collide with a real session item (whose pk is the
# bare phone number). This keeps the original request recoverable after the
# client completes the login flow (Req 5.1 / 4).
PENDING_REQUEST_PK_PREFIX: str = "pending#"

# The login link (and therefore the pending request) is valid for 10 minutes —
# matching the copy of the WhatsApp message sent in :func:`send_login_link`.
LOGIN_LINK_VALIDITY_SECONDS: int = 600

# Module-level boto3 resource so the connection is reused across warm
# invocations of the Lambda.
_dynamodb = boto3.resource("dynamodb")

# Lazily-constructed Twilio REST client (built on first send so the module can
# be imported in environments without Twilio credentials).
_twilio_client: Any = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_phone(phone_number: str) -> str:
    """Normalize a phone number to a bare E.164 string.

    Strips the optional ``whatsapp:`` channel prefix and surrounding whitespace
    so the same client always maps to the same partition key and session id,
    regardless of whether the caller passed ``"whatsapp:+57300..."`` or the bare
    ``"+57300..."``.

    Args:
        phone_number: A raw phone identifier, optionally ``whatsapp:``-prefixed.

    Returns:
        The trimmed E.164 phone number without the channel prefix.
    """
    return phone_number.strip().removeprefix("whatsapp:").strip()


def _get_auth_table():
    """Return the Auth_Session DynamoDB table resource.

    The table name is read lazily from ``AUTH_TABLE_NAME`` so importing this
    module never requires the environment to be configured.

    Returns:
        The boto3 DynamoDB ``Table`` for the Auth_Session table.

    Raises:
        KeyError: If ``AUTH_TABLE_NAME`` is not set.
    """
    return _dynamodb.Table(os.environ["AUTH_TABLE_NAME"])


def _now() -> datetime:
    """Return the current UTC time (isolated for testability)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Session-id derivation (Req 11.1 / Property 3)
# ---------------------------------------------------------------------------


def derive_session_id(phone_number: str) -> str:
    """Derive the conversational session id from a phone number.

    Deterministic and injective: the same phone number always yields the same
    session id (so subsequent WhatsApp messages map to the same Bedrock Agent
    session and keep conversational memory), and two different phone numbers
    yield different session ids. Implemented with a UUID v5 over a fixed
    namespace, which is a stable SHA-1-based hash of the normalized phone.

    Args:
        phone_number: The client's phone number (``whatsapp:`` prefix optional).

    Returns:
        The deterministic session id as a string UUID.
    """
    normalized = _normalize_phone(phone_number)
    return str(uuid.uuid5(SESSION_ID_NAMESPACE, normalized))


# ---------------------------------------------------------------------------
# Auth_Session access (Req 6.1 / 6.2)
# ---------------------------------------------------------------------------


def get_auth_session(phone_number: str) -> AuthSession | None:
    """Fetch the Auth_Session for a phone number from DynamoDB.

    Looks the session up by partition key (the normalized E.164 phone number).
    Because DynamoDB TTL deletion is *eventual* (an expired item can linger for
    up to 48h), callers must still gate on :func:`is_expired` before trusting an
    item returned here (Req 5.8 / 6.2).

    Args:
        phone_number: The client's phone number (``whatsapp:`` prefix optional).

    Returns:
        The Auth_Session item, or ``None`` if no session exists for the number.

    Raises:
        botocore.exceptions.ClientError: If the DynamoDB request fails. The
            caller is responsible for surfacing a temporary-unavailability
            message to the client (Req 6.5).
    """
    normalized = _normalize_phone(phone_number)
    table = _get_auth_table()

    response = table.get_item(Key={"pk": normalized})
    item = response.get("Item")
    if not item:
        logger.info("no auth session found", extra={"phone": mask_phone(normalized)})
        return None

    return item  # type: ignore[return-value]


def is_expired(auth_session: AuthSession | None) -> bool:
    """Return whether an Auth_Session is expired or otherwise unusable.

    The session is considered expired (``True``) when:

    * the session is ``None`` or empty, or
    * it carries neither a ``ttl`` (unix epoch) nor an ``expiresAt`` (ISO 8601)
      field to check against, or
    * its expiry instant is at or before the current time.

    Conversely an active session (TTL strictly in the future) returns ``False``,
    allowing banking actions without re-authentication (Req 5.6 / 6.2).

    Args:
        auth_session: The Auth_Session item, or ``None``.

    Returns:
        ``True`` if the session is missing or expired, ``False`` if still active.
    """
    if not auth_session:
        return True

    now_epoch = _now().timestamp()

    ttl = auth_session.get("ttl")
    if ttl is not None:
        try:
            return float(ttl) <= now_epoch
        except (TypeError, ValueError):
            logger.warning("auth session has non-numeric ttl, treating as expired")
            return True

    # Fall back to the ISO 8601 ``expiresAt`` field when ``ttl`` is absent.
    expires_at = auth_session.get("expiresAt")
    if expires_at:
        try:
            expires_dt = datetime.fromisoformat(expires_at)
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            return expires_dt.timestamp() <= now_epoch
        except ValueError:
            logger.warning("auth session has invalid expiresAt, treating as expired")
            return True

    # No expiry information at all — fail closed.
    logger.warning("auth session missing ttl/expiresAt, treating as expired")
    return True


# ---------------------------------------------------------------------------
# Pending request persistence (Req 5.1 / 5.4)
# ---------------------------------------------------------------------------


def store_pending_request(phone_number: str, input_text: str) -> None:
    """Persist the client's original request so it can be resumed after login.

    When a banking action arrives without an active session we send a login
    link; the client then authenticates on the Login_Page and returns to
    WhatsApp. Storing the original request lets the flow resume the action the
    client actually asked for instead of forcing them to retype it.

    Storage choice: the request is written to the **Auth_Session table** under a
    namespaced partition key (``pending#<phone>``) so it never collides with the
    real session item (whose pk is the bare phone number) that the Auth_Service
    writes on successful login. The marker carries the same 30-minute-style TTL
    semantics as the login link (10 minutes) so abandoned logins self-clean via
    DynamoDB TTL.

    Args:
        phone_number: The client's phone number (``whatsapp:`` prefix optional).
        input_text: The original request text to replay after authentication.
    """
    normalized = _normalize_phone(phone_number)
    table = _get_auth_table()

    now = _now()
    ttl = int(now.timestamp()) + LOGIN_LINK_VALIDITY_SECONDS

    table.put_item(
        Item={
            "pk": f"{PENDING_REQUEST_PK_PREFIX}{normalized}",
            "phoneNumber": normalized,
            "pendingRequest": input_text,
            "createdAt": now.isoformat(),
            "ttl": ttl,
        }
    )
    logger.info("stored pending request", extra={"phone": mask_phone(normalized)})


# ---------------------------------------------------------------------------
# Login link (Req 5.1)
# ---------------------------------------------------------------------------


def generate_callback_token(phone_number: str) -> str:
    """Generate a verifiable, time-bound callback token for the login link.

    The token is stateless and self-describing: ``"<expEpoch>.<signature>"``
    where ``signature`` is an HMAC-SHA256 over ``"<phone>:<expEpoch>"`` keyed
    with a shared secret. The Auth_Service can re-derive and compare the
    signature (in constant time) and check the embedded expiry without any extra
    storage, so the login link cannot be forged or replayed after it expires.

    The signing key is read from the ``CALLBACK_TOKEN_SECRET`` environment
    variable (sourced from Secrets Manager in production). If it is unset a
    process-local random key is generated so the module still works in local
    runs — note that tokens are then only valid within a single process.

    Args:
        phone_number: The client's phone number (``whatsapp:`` prefix optional).

    Returns:
        The signed callback token string.
    """
    normalized = _normalize_phone(phone_number)
    exp_epoch = int(_now().timestamp()) + LOGIN_LINK_VALIDITY_SECONDS

    secret = os.environ.get("CALLBACK_TOKEN_SECRET")
    if not secret:
        logger.warning(
            "CALLBACK_TOKEN_SECRET not set, using a process-local key "
            "(tokens valid only within this process)"
        )
        secret = uuid.uuid4().hex

    signature = hmac.new(
        secret.encode("utf-8"),
        f"{normalized}:{exp_epoch}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return f"{exp_epoch}.{signature}"


def _get_twilio_client() -> Any:
    """Return a cached Twilio REST client built from environment credentials.

    The client is constructed lazily on first use from ``TWILIO_ACCOUNT_SID`` and
    ``TWILIO_AUTH_TOKEN``. In production these are loaded from Secrets Manager
    and injected into the environment; this accessor does not itself resolve the
    secret.

    Returns:
        A ``twilio.rest.Client`` instance.

    Raises:
        KeyError: If the Twilio credential environment variables are not set.
    """
    global _twilio_client
    if _twilio_client is None:
        from twilio.rest import Client

        _twilio_client = Client(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )
    return _twilio_client


def _whatsapp_address(phone_number: str) -> str:
    """Return a ``whatsapp:``-prefixed address for a bare or prefixed number."""
    normalized = phone_number.strip()
    if normalized.startswith("whatsapp:"):
        return normalized
    return f"whatsapp:{normalized}"


def send_login_link(phone_number: str) -> None:
    """Send the WhatsApp login link prompting the client to authenticate.

    Builds the Login_Page URL from the ``LOGIN_PAGE_URL`` environment variable,
    appending the (url-encoded) phone number and a signed callback token, then
    sends it to the client over the Twilio REST API. This is the action that
    satisfies Req 5.1: a banking action without an active session triggers a
    login prompt before processing.

    The Twilio sender address comes from ``TWILIO_WHATSAPP_NUMBER`` (the
    ``whatsapp:`` prefix is added when absent). This sends a plain-text message
    with the link directly via the Twilio SDK; it can later be routed through
    the shared messaging module (task 5.6) without changing this contract.

    Args:
        phone_number: The client's phone number (``whatsapp:`` prefix optional).
    """
    normalized = _normalize_phone(phone_number)
    callback_token = generate_callback_token(normalized)
    login_page_url = os.environ["LOGIN_PAGE_URL"]
    login_url = f"{login_page_url}?phone={quote(normalized)}&token={callback_token}"

    client = _get_twilio_client()
    client.messages.create(
        from_=_whatsapp_address(os.environ["TWILIO_WHATSAPP_NUMBER"]),
        to=_whatsapp_address(normalized),
        body=(
            "🔐 Para ejecutar operaciones bancarias necesitas autenticarte.\n\n"
            f"Inicia sesión aquí: {login_url}\n\n"
            "El enlace es válido por 10 minutos."
        ),
    )
    logger.info("login link sent", extra={"phone": mask_phone(normalized)})


__all__ = [
    "AUTH_SESSION_TTL",
    "SESSION_ID_NAMESPACE",
    "PENDING_REQUEST_PK_PREFIX",
    "LOGIN_LINK_VALIDITY_SECONDS",
    "derive_session_id",
    "get_auth_session",
    "is_expired",
    "store_pending_request",
    "generate_callback_token",
    "send_login_link",
]
