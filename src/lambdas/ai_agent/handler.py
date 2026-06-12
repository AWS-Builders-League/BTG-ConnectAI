"""Strands_Agent Lambda entry point (Conversational_Agent).

Invoked **synchronously** (``RequestResponse``) by the Message_Processor with the
event::

    {
        "sessionId": "<deterministic conversational session id>",
        "inputText": "<typed / transcribed text or button payload>",
        "phoneNumber": "+57300XXXXXXX",
        "correlationId": "<request correlation id>"
    }

It runs the Strands Agent (Claude 3.5 Haiku on Bedrock, with the ``infra``
Guardrail and the three banking tools) over ``inputText`` and returns the
``{"response": ...}`` payload the Message_Processor expects (Requirements 10, 11,
12).

Response payload shape
----------------------
The Message_Processor's :func:`invoke_strands_agent` reads ``result["response"]``,
and :func:`extract_statement_info` / :func:`remove_statement_metadata` accept that
value as **either**:

* a plain ``str`` (text-only reply), or
* an object ``{"text": <agent_text>, "statement": {"s3Bucket", "s3Key",
  "fileName"}}`` when a statement PDF was generated during the turn.

Accordingly this handler returns::

    {"response": <agent_text>}                                  # no statement
    {"response": {"text": <agent_text>, "statement": {...}}}     # statement PDF

The statement reference is **not** parsed out of the model's prose. Instead, the
``generate_statement`` tool records the ``{s3Bucket, s3Key, fileName}`` into a
per-invocation sink (see :mod:`.tools`); after the agent run we read it with
:func:`tools.get_statement_reference` and lift it into the structured payload.

Extracting the agent text
--------------------------
Strands' ``Agent.__call__`` returns a result object. We obtain its text via
``str(result)`` — the documented, version-robust way to get the final assistant
message as a string (the result object's ``__str__`` yields the textual reply).
This avoids assuming a specific attribute name (e.g. ``result.message``) that may
differ across SDK versions.

Error handling
--------------
On any failure during agent execution we log the exception and **re-raise**. The
Message_Processor invokes this Lambda with ``RequestResponse`` and treats a
function error as a retryable failure (it raises, and SQS retries the whole
message — Requirement 3.9). Raising here is therefore consistent with the
Message_Processor design and preferable to returning a degraded "success"
payload that would be delivered to the client as a normal reply.

Lazy ``strands`` import
-----------------------
The agent factory (and therefore the ``strands`` SDK) is imported **inside**
:func:`handler`, not at module top level, so this module imports without the SDK
present and the package module graph stays importable for tooling/tests.
"""

from __future__ import annotations

from typing import Any

from shared.logger import get_logger

from . import tools

logger = get_logger("ai-agent")


def _extract_text(result: Any) -> str:
    """Return the agent's final assistant text from a Strands result object.

    Uses ``str(result)`` as the version-robust way to obtain the textual reply
    (the result object's ``__str__`` yields the final message). Falls back to an
    empty string if the conversion yields nothing usable.

    Args:
        result: The object returned by ``Agent.__call__``.

    Returns:
        The agent's textual response (possibly empty).
    """
    if result is None:
        return ""
    text = str(result).strip()
    return text


def handler(event: dict, context: Any) -> dict:
    """Lambda entry point for the Strands_Agent.

    Args:
        event: ``{"sessionId", "inputText", "phoneNumber", "correlationId"}``.
        context: The Lambda context object (unused).

    Returns:
        ``{"response": <str>}`` for a text-only reply, or
        ``{"response": {"text": <str>, "statement": {...}}}`` when a statement
        PDF was generated during the turn.

    Raises:
        Exception: Any failure during agent execution is logged and re-raised so
            the Message_Processor treats it as a retryable batch failure
            (Requirement 3.9).
    """
    session_id = event.get("sessionId")
    input_text = event.get("inputText", "")
    phone_number = event.get("phoneNumber", "")
    correlation_id = event.get("correlationId")

    # Bind the correlation id so every downstream log line is traceable
    # (Requirement 13.1 / 13.2).
    if correlation_id:
        logger.append_keys(correlation_id=correlation_id)

    logger.info(
        "ai-agent invocation",
        extra={"session_id": session_id, "input_len": len(input_text or "")},
    )

    # Lazily import the agent factory so this module (and the package graph) does
    # not require the strands SDK at import time.
    from .agent import build_agent

    try:
        agent = build_agent(session_id=session_id)

        # Bind phoneNumber / correlationId for the tools (the model never sees
        # them) for the duration of the agent run.
        with tools.invocation_context(phone_number, correlation_id):
            result = agent(input_text)
            statement_ref = tools.get_statement_reference()

        agent_text = _extract_text(result)
    except Exception:
        logger.exception("ai-agent execution failed")
        # Re-raise so the Message_Processor retries via SQS (Requirement 3.9).
        raise

    # Shape the response payload the Message_Processor expects.
    if statement_ref:
        logger.info("ai-agent produced a statement reference")
        return {
            "response": {
                "text": agent_text,
                "statement": {
                    "s3Bucket": statement_ref.get("s3Bucket"),
                    "s3Key": statement_ref.get("s3Key"),
                    "fileName": statement_ref.get("fileName", ""),
                },
            }
        }

    return {"response": agent_text}


__all__ = ["handler"]
