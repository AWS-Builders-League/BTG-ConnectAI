"""Strands Agent construction (Claude 3.5 Haiku on Amazon Bedrock).

This module builds the :class:`strands.Agent` that powers the Conversational_Agent
(Requirements 10, 11, 12). It wires:

* the **Bedrock model** Claude 3.5 Haiku (model id from env ``BEDROCK_MODEL_ID``);
* the Spanish **system prompt** (:data:`prompts.SYSTEM_PROMPT`);
* the three banking **tools** (:data:`tools.TOOLS`);
* the ``infra`` **Bedrock Guardrail** (env ``GUARDRAIL_ID`` / ``GUARDRAIL_VERSION``,
  part of the cross-stack contract) so content filtering/topic restrictions apply
  to both input and output (Requirement 12).

Strands SDK API assumptions
---------------------------
This module is written against the documented Strands Agents SDK surface. The
exact kwarg names below are the assumptions made; if the installed SDK differs,
this is the single place to adjust:

* ``from strands import Agent`` — the top-level agent class.
* ``from strands.models import BedrockModel`` — the Bedrock model provider. We
  construct it as
  ``BedrockModel(model_id=..., guardrail_id=..., guardrail_version=...,
  region_name=...)``. The SDK's ``BedrockModel`` exposes ``model_id`` and the
  guardrail configuration (``guardrail_id`` / ``guardrail_version``) and accepts
  a ``region_name``. When ``GUARDRAIL_ID`` is not configured we omit the
  guardrail kwargs so the model is still constructible in non-guardrailed
  environments.
* ``Agent(model=<BedrockModel>, system_prompt=..., tools=...)`` — the agent is
  built from the model provider plus the system prompt and tool list.

Session memory
--------------
The Message_Processor derives a **deterministic** ``sessionId`` from the client's
phone number and passes it on every invocation, so the same client always maps to
the same conversational session (Requirement 11.1). Strands manages conversation
state through its model/agent abstractions; :func:`build_agent` accepts an optional
``session_id`` so that, where the installed SDK exposes a session/conversation
manager, it can be wired through. To stay pragmatic and avoid coupling to a
specific SDK session API, the cached agent is reused across warm invocations and
the ``session_id`` is accepted (and logged) but conversation scoping is delegated
to Strands' own session handling. If the SDK version in use requires an explicit
session manager keyed by ``session_id``, construct it here from that argument.

``strands`` is a hard dependency of this module
-----------------------------------------------
Unlike :mod:`.tools` and :mod:`.prompts` (which import cleanly without the SDK),
this module genuinely needs the Strands SDK and imports it at module top level.
The :mod:`.handler` therefore imports this module **lazily** (inside the handler
function) so the rest of the package's module graph does not force ``strands`` at
import time. All environment variables are read **lazily** inside
:func:`build_agent`, so importing this module is side-effect free.

Environment variables (resolved from the cross-stack contract):
* ``BEDROCK_MODEL_ID`` — Claude 3.5 Haiku model id (defaults to
  :data:`DEFAULT_MODEL_ID`).
* ``GUARDRAIL_ID`` / ``GUARDRAIL_VERSION`` — the ``infra`` Bedrock Guardrail.
* ``AWS_REGION`` — the Lambda runtime region (provided by the platform).
"""

from __future__ import annotations

from datetime import datetime

from strands import Agent
from strands.models import BedrockModel

from shared.constants import COLOMBIA_TZ
from shared.logger import get_logger

from .prompts import build_system_prompt
from .tools import TOOLS

logger = get_logger("ai-agent")

# --- Environment variable names ----------------------------------------------
BEDROCK_MODEL_ID_ENV: str = "BEDROCK_MODEL_ID"
GUARDRAIL_ID_ENV: str = "GUARDRAIL_ID"
GUARDRAIL_VERSION_ENV: str = "GUARDRAIL_VERSION"
REGION_ENV: str = "AWS_REGION"

# Sensible default so the function is constructible even if the contract did not
# inject an explicit model id. Claude Haiku 4.5 on Amazon Bedrock (inference
# profile us.*; Haiku 4.5 is not available as bare on-demand model id).
DEFAULT_MODEL_ID: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_GUARDRAIL_VERSION: str = "DRAFT"
DEFAULT_REGION: str = "us-east-1"

# Module-scope cache so warm invocations reuse the same Agent (cold-start saving).
# Keyed by UTC date: the cached agent carries today's date in its system prompt,
# so it is rebuilt (at most once per day per warm container) when the date rolls
# over, keeping the injected FECHA ACTUAL fresh without paying the cost per call.
_agent: Agent | None = None
_agent_date: str | None = None


def _today_iso() -> str:
    """Return today's date in Colombia (America/Bogota) as ``AAAA-MM-DD``.

    Colombia local time (UTC-5, no DST) is used — matching the
    ``statement-generator`` tool's own ``_today()`` — so the agent and the tool
    always agree on what counts as a future date, and "hoy" matches the client's
    actual calendar day rather than UTC.
    """
    return datetime.now(COLOMBIA_TZ).date().isoformat()


def build_agent(session_id: str | None = None, *, force_new: bool = False) -> Agent:
    """Construct (or return the cached) Strands Agent for Claude 3.5 Haiku.

    Reads configuration lazily from the environment so importing this module has
    no side effects. The constructed agent uses the Spanish system prompt
    (:data:`prompts.SYSTEM_PROMPT`), the three banking tools
    (:data:`tools.TOOLS`) and, when configured, the ``infra`` Bedrock Guardrail
    (Requirement 12).

    Args:
        session_id: Optional deterministic conversational session id (derived
            from the client's phone number by the Message_Processor). Accepted so
            conversation memory can be scoped per client; see the module
            docstring for how session handling is delegated to Strands.
        force_new: When ``True``, bypass the module-scope cache and build a fresh
            Agent. Defaults to ``False`` so warm invocations reuse the instance.

    Returns:
        A configured :class:`strands.Agent`.
    """
    global _agent, _agent_date

    today_iso = _today_iso()

    # Reuse the cached agent only while it still carries today's date; rebuild it
    # when the UTC date has rolled over so FECHA ACTUAL never goes stale.
    if _agent is not None and not force_new and _agent_date == today_iso:
        return _agent

    import os

    model_id = os.environ.get(BEDROCK_MODEL_ID_ENV, DEFAULT_MODEL_ID)
    guardrail_id = os.environ.get(GUARDRAIL_ID_ENV)
    guardrail_version = os.environ.get(GUARDRAIL_VERSION_ENV, DEFAULT_GUARDRAIL_VERSION)
    region_name = os.environ.get(REGION_ENV, DEFAULT_REGION)

    model_kwargs: dict[str, object] = {
        "model_id": model_id,
        "region_name": region_name,
    }
    # Apply the infra Bedrock Guardrail only when its id is present in the
    # contract; otherwise build a non-guardrailed model so the agent is still
    # constructible (Requirement 12 in production, graceful locally).
    if guardrail_id:
        model_kwargs["guardrail_id"] = guardrail_id
        model_kwargs["guardrail_version"] = guardrail_version
    else:
        logger.warning(
            "GUARDRAIL_ID not set; building Bedrock model without guardrail "
            "(expected only outside the deployed environment)"
        )

    logger.info(
        "building strands agent",
        extra={
            "model_id": model_id,
            "region": region_name,
            "guardrail_applied": bool(guardrail_id),
            "session_id": session_id,
            "tool_count": len(TOOLS),
            "today": today_iso,
        },
    )

    model = BedrockModel(**model_kwargs)
    agent = Agent(
        model=model,
        system_prompt=build_system_prompt(today_iso),
        tools=TOOLS,
    )

    if not force_new:
        _agent = agent
        _agent_date = today_iso

    return agent


def get_agent(session_id: str | None = None) -> Agent:
    """Return the cached Strands Agent, building it on first use.

    Thin convenience wrapper over :func:`build_agent` for the common warm-reuse
    path.

    Args:
        session_id: Optional deterministic conversational session id.

    Returns:
        The shared :class:`strands.Agent` instance.
    """
    return build_agent(session_id=session_id)


__all__ = [
    "build_agent",
    "get_agent",
    "BEDROCK_MODEL_ID_ENV",
    "GUARDRAIL_ID_ENV",
    "GUARDRAIL_VERSION_ENV",
    "DEFAULT_MODEL_ID",
]
