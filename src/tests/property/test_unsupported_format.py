"""Property-based tests for the Message_Processor unsupported-format rejection.

Covers:
    * Property 16 — Unsupported Message Format Rejection (Validates: Requirement 2.5)

Requirement 2.5: when a client sends an unsupported format (image, video,
sticker, document, location, ...) the Message_Processor replies that only text
and voice notes are accepted — and, critically, does **not** invoke the Strands
Agent.

What is exercised
-----------------
The type-routing branch of :func:`record_handler`. A message is *unsupported*
when it carries media whose ``MediaContentType0`` is **not** an ``audio/*`` MIME
type (and has no quick-reply ``ButtonPayload`` and an empty/whitespace ``Body``),
or when it carries no recognizable content at all (``NumMedia == "0"`` with an
empty body). For any such message the handler must send
``ERROR_MESSAGES["unsupported_format"]`` exactly once and never call the agent.

Isolation strategy
------------------
``record_handler`` is preceded by the OTP-callback and consent gates, which read
DynamoDB. Those gates are *not* under test here, so each example patches the
handler's collaborator modules to pass the gates deterministically:

* ``otp_callback.get_pending_otp`` → ``None`` (no pending OTP challenge),
* ``consent.get_consent`` → an ``accepted`` record and
  ``consent.has_accepted_consent`` → ``True`` (consent gate open).

``messaging.send_twilio_message`` is captured to assert the reply, and
``invoke_strands_agent`` is patched purely as a spy to prove it is never called.
Patches are applied with ``mock.patch.object`` *inside* each example so no
function-scoped fixture state leaks across Hypothesis iterations.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# --- Force a fake AWS region BEFORE importing the module under test. ----------
# handler.py builds boto3 clients (and its sibling modules build boto3 resources)
# at import time, which require a region. Dummy credentials guarantee no real AWS
# call can ever occur even though every AWS-touching collaborator is patched.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

from lambdas.message_processor import handler  # noqa: E402  (after env setup)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A MIME content type that is NOT an audio note. Mixes the concrete unsupported
# kinds named in Requirement 2.5 with freely generated strings constrained to
# never start with the ``audio/`` prefix (the only kind the handler accepts).
_non_audio_content_type = st.one_of(
    st.sampled_from(
        [
            "image/jpeg",
            "image/png",
            "image/webp",  # WhatsApp stickers
            "video/mp4",
            "video/3gpp",
            "application/pdf",  # document
            "application/vnd.ms-excel",
            "text/vcard",  # contact card
            "text/x-vlocation",  # location
        ]
    ),
    st.text(min_size=1, max_size=40).filter(lambda s: not s.startswith("audio/")),
)

# A body that is empty after stripping, so media routing is not pre-empted by the
# plain-text branch (``Body.strip()`` truthy → text).
_blank_body = st.sampled_from(["", " ", "   ", "\t", "\n", " \t\n "])

# E.164-ish Colombian numbers, with/without the ``whatsapp:`` channel prefix.
_phone_strategy = st.builds(
    lambda prefix, digits: f"{prefix}+57{digits}",
    st.sampled_from(["", "whatsapp:"]),
    st.text(alphabet="0123456789", min_size=8, max_size=12),
)

# Any non-zero media count (string, as Twilio sends it).
_num_media = st.sampled_from(["1", "2", "3", "10"])


def _make_record(payload: dict) -> SimpleNamespace:
    """Wrap a Twilio payload dict as a Powertools-``SQSRecord``-like object.

    ``record_handler`` only reads ``record.body`` (the enqueued JSON), so a simple
    namespace with a ``body`` attribute is a faithful stand-in.
    """
    return SimpleNamespace(body=json.dumps(payload))


def _run_handler_capturing(payload: dict) -> tuple[list[tuple[str, str]], mock.Mock]:
    """Run ``record_handler`` with all gate collaborators patched.

    Returns the list of ``(phone, text)`` arguments passed to
    ``send_twilio_message`` and the spy standing in for ``invoke_strands_agent``.
    """
    sent: list[tuple[str, str]] = []

    with (
        mock.patch.object(handler.otp_callback, "get_pending_otp", return_value=None),
        mock.patch.object(
            handler.consent, "get_consent", return_value={"status": "accepted"}
        ),
        mock.patch.object(handler.consent, "has_accepted_consent", return_value=True),
        mock.patch.object(
            handler.messaging,
            "send_twilio_message",
            side_effect=lambda phone, text: sent.append((phone, text)),
        ),
        mock.patch.object(handler, "invoke_strands_agent") as agent_spy,
    ):
        handler.record_handler(_make_record(payload))

    return sent, agent_spy


# ---------------------------------------------------------------------------
# Property 16: Unsupported Message Format Rejection
# Validates: Requirements 2.5
# ---------------------------------------------------------------------------
@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(
    phone=_phone_strategy,
    content_type=_non_audio_content_type,
    num_media=_num_media,
    body=_blank_body,
    correlation_id=st.uuids().map(str),
)
def test_non_audio_media_is_rejected_without_invoking_agent(
    phone: str,
    content_type: str,
    num_media: str,
    body: str,
    correlation_id: str,
) -> None:
    """Any non-audio media message routes to the unsupported-format reply.

    For a consented, OTP-free message that carries media with a non-``audio/*``
    content type (and no button, blank body), the handler must send exactly the
    ``unsupported_format`` copy and must NOT invoke the Strands Agent
    (Requirement 2.5).
    """
    payload = {
        "MessageSid": "SM" + "0" * 30,
        "From": phone,
        "To": "whatsapp:+14155550000",
        "Body": body,
        "NumMedia": num_media,
        "MediaUrl0": "https://api.twilio.com/media/abc",
        "MediaContentType0": content_type,
        "correlationId": correlation_id,
    }

    sent, agent_spy = _run_handler_capturing(payload)

    # The agent is never reached for an unsupported format.
    assert agent_spy.call_count == 0
    # Exactly one reply, and it is the unsupported-format copy.
    assert len(sent) == 1
    assert sent[0][1] == handler.ERROR_MESSAGES["unsupported_format"]


@pytest.mark.property
@settings(max_examples=50, deadline=None)
@given(
    phone=_phone_strategy,
    body=_blank_body,
    correlation_id=st.uuids().map(str),
)
def test_empty_message_is_rejected_without_invoking_agent(
    phone: str,
    body: str,
    correlation_id: str,
) -> None:
    """A message with no media and a blank body is also unsupported.

    Complement of the media case: ``NumMedia == "0"`` with an empty/whitespace
    body and no button payload carries nothing the agent can act on, so the
    handler returns the same ``unsupported_format`` reply and never invokes the
    agent (Requirement 2.5).
    """
    payload = {
        "MessageSid": "SM" + "1" * 30,
        "From": phone,
        "To": "whatsapp:+14155550000",
        "Body": body,
        "NumMedia": "0",
        "correlationId": correlation_id,
    }

    sent, agent_spy = _run_handler_capturing(payload)

    assert agent_spy.call_count == 0
    assert len(sent) == 1
    assert sent[0][1] == handler.ERROR_MESSAGES["unsupported_format"]
