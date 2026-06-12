"""Runtime hydration of Twilio credentials and the callback HMAC secret.

The compute Lambdas (Message_Processor, message-handler-notify, Auth_Service)
read Twilio credentials and the login-callback signing key as **individual**
environment variables:

* ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN`` / ``TWILIO_WHATSAPP_NUMBER``
* ``CALLBACK_TOKEN_SECRET`` (HMAC key shared by Message_Processor — which signs
  the login-link token — and Auth_Service — which verifies it).

The CloudFormation templates, however, only inject the **secret ARN**
(``TWILIO_SECRET_ARN``) to avoid placing credentials in IaC. This module bridges
that gap: on first call it fetches the Twilio secret from Secrets Manager and
populates the individual environment variables the existing modules expect, so
their lazy ``os.environ[...]`` reads work unchanged.

The Twilio secret is a JSON object with the keys produced by the ``infra``
template (``secrets.yaml``)::

    {"account_sid": "...", "auth_token": "...", "whatsapp_number": "..."}

The callback HMAC key is **derived from the Twilio auth token** so that both the
signer (Message_Processor) and the verifier (Auth_Service) share the same stable,
high-entropy secret without provisioning a separate resource. This is acceptable
for the sandbox MVP; for production a dedicated, rotated secret is preferable.

The fetch is cached at module scope so warm invocations skip the Secrets Manager
call. Importing this module has no side effects (the secret is loaded only when
:func:`hydrate_twilio_env` is called).
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

from shared.logger import get_logger

logger = get_logger("twilio-env")

_TWILIO_SECRET_ARN_ENV = "TWILIO_SECRET_ARN"

# Mapping of target environment variable -> secret JSON key.
_ENV_FROM_SECRET = {
    "TWILIO_ACCOUNT_SID": "account_sid",
    "TWILIO_AUTH_TOKEN": "auth_token",
    "TWILIO_WHATSAPP_NUMBER": "whatsapp_number",
    # Optional Twilio Content Template SID for the interactive (button) T&C
    # message. Add a "tc_template_sid" key to the Twilio secret to enable buttons.
    "TWILIO_TC_TEMPLATE_SID": "tc_template_sid",
    # Shared HMAC key for the login-link callback token: reuse the Twilio auth
    # token so signer (Message_Processor) and verifier (Auth_Service) match.
    "CALLBACK_TOKEN_SECRET": "auth_token",
}

_hydrated = False
_secrets_client: Any = None


def hydrate_twilio_env(force: bool = False) -> None:
    """Populate Twilio/callback env vars from the Twilio secret (idempotent).

    Reads the secret whose ARN is in ``TWILIO_SECRET_ARN`` and sets
    ``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN``, ``TWILIO_WHATSAPP_NUMBER`` and
    ``CALLBACK_TOKEN_SECRET`` if they are not already present in the environment.
    Cached after the first successful run so warm invocations are free.

    No-op (with a warning) when ``TWILIO_SECRET_ARN`` is not configured, so it is
    safe to call unconditionally at the top of a handler.

    Args:
        force: When ``True``, re-fetch the secret even if already hydrated.
    """
    global _hydrated, _secrets_client

    if _hydrated and not force:
        return

    secret_arn = os.environ.get(_TWILIO_SECRET_ARN_ENV)
    if not secret_arn:
        logger.warning(
            "TWILIO_SECRET_ARN not set; skipping Twilio env hydration "
            "(expected only outside the deployed environment)"
        )
        return

    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")

    raw = _secrets_client.get_secret_value(SecretId=secret_arn)["SecretString"]
    secret = json.loads(raw)

    for env_name, secret_key in _ENV_FROM_SECRET.items():
        value = secret.get(secret_key)
        # Only set when we have a value and the env var isn't already provided
        # (so an explicit template/test override always wins).
        if value and not os.environ.get(env_name):
            os.environ[env_name] = value

    _hydrated = True
    logger.info("twilio env hydrated from secret")


__all__ = ["hydrate_twilio_env"]
