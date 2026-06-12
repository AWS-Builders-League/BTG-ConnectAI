"""Unit tests for the Strands_Agent Lambda (Task 13.2).

Example-based tests pinning down the concrete behaviour of the
Conversational_Agent (``src/lambdas/ai_agent/``):

* **Tool routing** (Requirement 10.3, 12.2) — each ``@tool`` wrapper invokes the
  correct Action Group Lambda (resolved from its environment variable) with the
  expected JSON payload and returns the parsed response. The authenticated
  client's ``phoneNumber`` is injected from the per-invocation context, never by
  the model, and the request ``correlationId`` flows through as the idempotent
  transfer execution name.
* **Transfer returns immediately** (Requirement 8.3, 12, 18.7) —
  ``initiate_transfer_breb`` fires the state machine via the initiator Lambda and
  returns *immediately* with the ``executionArn`` plus an OTP-by-SMS instruction;
  it performs exactly one Lambda invoke (it never waits/polls for the OTP).
* **Out-of-domain requests are declined** (Requirement 10.3, 12.2) — the closed
  domain is encoded in :data:`prompts.SYSTEM_PROMPT`, and the handler faithfully
  surfaces the model's decline text back to the Message_Processor.
* **Guardrails block** (Requirement 12.6) — :func:`agent.build_agent` applies the
  ``infra`` Bedrock Guardrail (``GuardrailId`` / ``GuardrailVersion`` from the
  cross-stack contract) to the Bedrock model when configured, and the handler
  surfaces a guardrail-blocked reply to the client.

Mocking approach
----------------
No real AWS or network calls are made.

* **tools** — the module-level ``boto3`` Lambda client (``tools._lambda_client``)
  is replaced with a :class:`_FakeLambdaClient` that records invocations and
  returns canned payloads (optionally simulating a ``FunctionError``). The
  ``phoneNumber`` / ``correlationId`` context is set with the real
  :func:`tools.invocation_context` so the production wiring is exercised.
* **agent** — :class:`strands.models.BedrockModel` and :class:`strands.Agent`
  are monkeypatched with light fakes that capture their constructor kwargs, so
  ``build_agent`` is verified without standing up a real Bedrock model.
* **handler** — ``agent.build_agent`` is monkeypatched to return a
  :class:`_FakeAgent`, so the handler's request/response shaping is tested
  without invoking Bedrock.

``AWS_DEFAULT_REGION`` (and dummy credentials) are set *before* importing the
modules under test because ``tools`` builds a module-level ``boto3`` Lambda
client at import time.
"""

from __future__ import annotations

import io
import json
import os

import pytest

# ``tools`` builds ``boto3.client("lambda")`` at import time — give boto3 a
# region (and dummy credentials) before importing it.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

from lambdas.ai_agent import agent as agent_mod  # noqa: E402
from lambdas.ai_agent import handler as handler_mod  # noqa: E402
from lambdas.ai_agent import tools as tools_mod  # noqa: E402
from lambdas.ai_agent.prompts import (  # noqa: E402
    REFERENTIAL_DISCLAIMER,
    SERVICES_MENU,
    SYSTEM_PROMPT,
    build_system_prompt,
)

pytestmark = pytest.mark.unit

_PHONE = "+573001234567"
_CORRELATION_ID = "corr-abc-123"

_BALANCE_FN = "balance-query-fn"
_TRANSFER_FN = "transfer-breb-initiator-fn"
_STATEMENT_FN = "statement-generator-fn"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeLambdaClient:
    """Records ``invoke`` calls and returns canned JSON payloads.

    Mirrors the slice of the boto3 Lambda client that :func:`tools._invoke_lambda`
    uses: ``invoke(FunctionName, InvocationType, Payload)`` returning a dict whose
    ``"Payload"`` is a readable byte stream and (optionally) a ``"FunctionError"``.
    """

    def __init__(self) -> None:
        self.invocations: list[dict] = []
        self._responses: dict[str, tuple[object, str | None]] = {}

    def set_response(
        self, function_name: str, payload: object, function_error: str | None = None
    ) -> None:
        self._responses[function_name] = (payload, function_error)

    def invoke(self, *, FunctionName: str, InvocationType: str, Payload: bytes) -> dict:
        self.invocations.append(
            {
                "FunctionName": FunctionName,
                "InvocationType": InvocationType,
                "Payload": json.loads(Payload.decode("utf-8")),
            }
        )
        payload, function_error = self._responses.get(FunctionName, ({}, None))
        response: dict = {"Payload": io.BytesIO(json.dumps(payload).encode("utf-8"))}
        if function_error:
            response["FunctionError"] = function_error
        return response

    def payload_for(self, function_name: str) -> dict:
        """Return the JSON payload of the (single) invoke for ``function_name``."""
        matches = [c for c in self.invocations if c["FunctionName"] == function_name]
        assert len(matches) == 1, f"expected exactly one invoke of {function_name}"
        return matches[0]["Payload"]


class _FakeAgent:
    """Callable stand-in for a Strands ``Agent``.

    ``str(result)`` (used by the handler to extract the assistant text) yields the
    configured ``reply``. An optional ``on_call`` hook lets a test simulate a tool
    side effect (e.g. recording a statement reference) during the run.
    """

    def __init__(self, reply: str, on_call=None) -> None:
        self.reply = reply
        self.on_call = on_call
        self.inputs: list[str] = []

    def __call__(self, input_text: str):
        self.inputs.append(input_text)
        if self.on_call is not None:
            self.on_call(input_text)
        return self.reply


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_lambda(monkeypatch):
    """Replace ``tools._lambda_client`` with a fake and set tool env vars."""
    monkeypatch.setenv(tools_mod.BALANCE_QUERY_FUNCTION_ENV, _BALANCE_FN)
    monkeypatch.setenv(tools_mod.TRANSFER_INITIATOR_FUNCTION_ENV, _TRANSFER_FN)
    monkeypatch.setenv(tools_mod.STATEMENT_GENERATOR_FUNCTION_ENV, _STATEMENT_FN)

    client = _FakeLambdaClient()
    monkeypatch.setattr(tools_mod, "_lambda_client", client)
    return client


# ---------------------------------------------------------------------------
# 1. Tool routing (Requirements 10.3, 12.2)
# ---------------------------------------------------------------------------


class TestToolRouting:
    def test_query_balance_routes_to_balance_query_lambda(self, fake_lambda) -> None:
        fake_lambda.set_response(
            _BALANCE_FN, {"success": True, "data": {"products": []}}
        )

        with tools_mod.invocation_context(_PHONE, _CORRELATION_ID):
            result = tools_mod.query_balance()

        # Routed to the balance-query Lambda with the injected phone number.
        payload = fake_lambda.payload_for(_BALANCE_FN)
        assert payload == {"phoneNumber": _PHONE, "productType": None}
        assert result == {"success": True, "data": {"products": []}}

    def test_query_balance_forwards_product_type_filter(self, fake_lambda) -> None:
        fake_lambda.set_response(_BALANCE_FN, {"success": True, "data": {}})

        with tools_mod.invocation_context(_PHONE, _CORRELATION_ID):
            tools_mod.query_balance(product_type="fondo_inversion")

        payload = fake_lambda.payload_for(_BALANCE_FN)
        assert payload["productType"] == "fondo_inversion"
        assert payload["phoneNumber"] == _PHONE

    def test_generate_statement_routes_and_records_reference(self, fake_lambda) -> None:
        fake_lambda.set_response(
            _STATEMENT_FN,
            {
                "success": True,
                "s3Bucket": "statement-bucket",
                "s3Key": "statements/abc.pdf",
                "fileName": "extracto_abc.pdf",
            },
        )

        with tools_mod.invocation_context(_PHONE, _CORRELATION_ID):
            result = tools_mod.generate_statement("2025-02-28", account_id="1001234568")
            reference = tools_mod.get_statement_reference()

        payload = fake_lambda.payload_for(_STATEMENT_FN)
        assert payload == {
            "phoneNumber": _PHONE,
            "cutoffDate": "2025-02-28",
            "accountId": "1001234568",
        }
        assert result["success"] is True
        # The S3 reference is recorded into the per-invocation sink so the handler
        # can attach the PDF without the model echoing the keys back.
        assert reference == {
            "s3Bucket": "statement-bucket",
            "s3Key": "statements/abc.pdf",
            "fileName": "extracto_abc.pdf",
        }

    def test_generate_statement_omits_account_id_when_absent(self, fake_lambda) -> None:
        fake_lambda.set_response(_STATEMENT_FN, {"success": False, "error": "x"})

        with tools_mod.invocation_context(_PHONE, _CORRELATION_ID):
            tools_mod.generate_statement("2025-02-28")

        payload = fake_lambda.payload_for(_STATEMENT_FN)
        assert "accountId" not in payload

    def test_function_error_returns_graceful_failure(self, fake_lambda) -> None:
        # A function-level error must not crash the turn — the tool returns a
        # uniform failure shape the model can apologize over.
        fake_lambda.set_response(_BALANCE_FN, {}, function_error="Unhandled")

        with tools_mod.invocation_context(_PHONE, _CORRELATION_ID):
            result = tools_mod.query_balance()

        assert result["success"] is False
        assert result["error"] == "tool_invocation_failed"
        assert "message" in result

    def test_phone_number_comes_from_context_not_model(self, fake_lambda) -> None:
        # When no context is set and no explicit phone is passed, the tool raises
        # (a wiring error) rather than silently querying an unknown client.
        fake_lambda.set_response(_BALANCE_FN, {"success": True})

        with pytest.raises(ValueError):
            tools_mod.query_balance()


# ---------------------------------------------------------------------------
# 2. Transfer returns immediately with executionArn (Requirements 8.3, 12, 18.7)
# ---------------------------------------------------------------------------


class TestTransferReturnsImmediately:
    def test_returns_execution_arn_immediately(self, fake_lambda) -> None:
        execution_arn = (
            "arn:aws:states:us-east-1:123456789012:execution:TransferBreb:corr-abc-123"
        )
        fake_lambda.set_response(
            _TRANSFER_FN, {"executionArn": execution_arn, "status": "STARTED"}
        )

        with tools_mod.invocation_context(_PHONE, _CORRELATION_ID):
            result = tools_mod.initiate_transfer_breb(
                source_account="1001234568",
                destination_account="2009876543",
                amount=1_000_000.0,
                concept="Pago arriendo",
            )

        # Returned immediately with the execution arn + STARTED status.
        assert result["executionArn"] == execution_arn
        assert result["status"] == "STARTED"
        # Exactly one Lambda invoke — the tool never waits/polls for the OTP.
        assert len(fake_lambda.invocations) == 1
        # An OTP-by-SMS instruction is surfaced to the model.
        assert "OTP" in result["message"] or "SMS" in result["message"]

    def test_payload_includes_correlation_id_for_idempotency(self, fake_lambda) -> None:
        fake_lambda.set_response(_TRANSFER_FN, {"executionArn": "arn", "status": "STARTED"})

        with tools_mod.invocation_context(_PHONE, _CORRELATION_ID):
            tools_mod.initiate_transfer_breb(
                source_account="1001234568",
                destination_account="2009876543",
                amount=500_000.0,
            )

        payload = fake_lambda.payload_for(_TRANSFER_FN)
        # correlationId is the idempotent state-machine execution name (Property 19).
        assert payload["correlationId"] == _CORRELATION_ID
        assert payload["phoneNumber"] == _PHONE
        assert payload["sourceAccount"] == "1001234568"
        assert payload["destinationAccount"] == "2009876543"
        assert payload["amount"] == 500_000.0

    def test_default_otp_message_set_when_initiator_omits_it(self, fake_lambda) -> None:
        # Initiator returns no message → the tool supplies the Spanish OTP-by-SMS
        # instruction so the model never waits for the OTP synchronously.
        fake_lambda.set_response(_TRANSFER_FN, {"executionArn": "arn", "status": "STARTED"})

        with tools_mod.invocation_context(_PHONE, _CORRELATION_ID):
            result = tools_mod.initiate_transfer_breb(
                source_account="1001234568",
                destination_account="2009876543",
                amount=10_000.0,
            )

        assert "código OTP por SMS" in result["message"]


# ---------------------------------------------------------------------------
# 3. Out-of-domain requests are declined (Requirements 10.3, 12.2)
# ---------------------------------------------------------------------------


class TestOutOfDomainDeclined:
    def test_system_prompt_encodes_closed_domain_decline(self) -> None:
        # The model is instructed to decline anything outside the banking domain
        # and to list the available services.
        prompt = SYSTEM_PROMPT.lower()
        assert "fuera del dominio" in prompt
        assert "declina" in prompt
        # Only the three banking services define the domain.
        assert "consulta de saldos" in prompt
        assert "transferencias bre-b" in prompt
        assert "extractos" in prompt

    def test_services_menu_lists_three_services(self) -> None:
        # The decline copy reuses the shared services menu (Req 4.1/10.3/12.2).
        assert "Transferencias BRE-B" in SERVICES_MENU
        assert "Consulta de saldos" in SERVICES_MENU
        assert "extractos" in SERVICES_MENU.lower()

    def test_handler_surfaces_decline_text(self, monkeypatch) -> None:
        # When the model declines an out-of-domain request, the handler returns
        # that text verbatim to the Message_Processor.
        decline_text = (
            "Lo siento, solo puedo ayudarte con servicios bancarios de BTG "
            "Pactual. " + SERVICES_MENU
        )
        fake_agent = _FakeAgent(reply=decline_text)
        monkeypatch.setattr(agent_mod, "build_agent", lambda session_id=None: fake_agent)

        event = {
            "sessionId": "session-xyz",
            "inputText": "¿Cuál es la capital de Francia?",
            "phoneNumber": _PHONE,
            "correlationId": _CORRELATION_ID,
        }
        result = handler_mod.handler(event, None)

        assert result == {"response": decline_text}
        # The user's question was passed through to the agent.
        assert fake_agent.inputs == ["¿Cuál es la capital de Francia?"]

    def test_disclaimer_constant_is_referential(self) -> None:
        assert "referencial" in REFERENTIAL_DISCLAIMER.lower()


# ---------------------------------------------------------------------------
# 3b. Current-date injection (so the model never misjudges past vs future)
# ---------------------------------------------------------------------------


class TestCurrentDateInjection:
    def test_build_system_prompt_injects_today_and_keeps_base(self) -> None:
        prompt = build_system_prompt("2026-06-12")

        # The supplied date is present as the authoritative reference...
        assert "2026-06-12" in prompt
        assert "fecha de hoy" in prompt.lower()
        # ...and the full base behaviour contract is preserved underneath.
        assert prompt.endswith(SYSTEM_PROMPT)

    def test_statement_rule_defers_future_check_to_tool(self) -> None:
        # The extract rule must tell the model to use today's date and let the
        # tool validate, instead of guessing the year from memory.
        prompt = build_system_prompt("2026-06-12").lower()
        assert "generate_statement" in prompt
        assert "fecha actual" in prompt

    def test_agent_today_is_iso_colombia(self) -> None:
        # The agent's notion of "today" is ISO YYYY-MM-DD in Colombia local time,
        # matching the statement-generator tool so they never disagree on "future".
        today = agent_mod._today_iso()
        assert len(today) == 10 and today[4] == "-" and today[7] == "-"


# ---------------------------------------------------------------------------
# 4. Guardrails block (Requirement 12.6)
# ---------------------------------------------------------------------------


class _CaptureBedrockModel:
    """Captures the kwargs ``build_agent`` passes to ``BedrockModel``."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        type(self).last_kwargs = kwargs


class _CaptureAgent:
    """Captures the kwargs ``build_agent`` passes to ``Agent``."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        type(self).last_kwargs = kwargs


class TestGuardrails:
    def test_build_agent_applies_guardrail_when_configured(self, monkeypatch) -> None:
        monkeypatch.setattr(agent_mod, "BedrockModel", _CaptureBedrockModel)
        monkeypatch.setattr(agent_mod, "Agent", _CaptureAgent)
        monkeypatch.setenv("GUARDRAIL_ID", "gr-12345")
        monkeypatch.setenv("GUARDRAIL_VERSION", "7")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

        agent_mod.build_agent(session_id="session-xyz", force_new=True)

        model_kwargs = _CaptureBedrockModel.last_kwargs
        # The infra Bedrock Guardrail is applied to both input and output.
        assert model_kwargs["guardrail_id"] == "gr-12345"
        assert model_kwargs["guardrail_version"] == "7"
        assert model_kwargs["model_id"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        # The agent wires the Spanish system prompt and the three banking tools.
        agent_kwargs = _CaptureAgent.last_kwargs
        system_prompt = agent_kwargs["system_prompt"]
        # The base behaviour contract is preserved...
        assert system_prompt.endswith(SYSTEM_PROMPT)
        # ...with an authoritative current-date header injected on top so the
        # model judges past/future dates correctly (today is supplied, not guessed).
        assert "FECHA de HOY".lower() in system_prompt.lower()
        today = agent_mod._today_iso()
        assert today in system_prompt
        assert agent_kwargs["tools"] == tools_mod.TOOLS

    def test_build_agent_omits_guardrail_when_unset(self, monkeypatch) -> None:
        _CaptureBedrockModel.last_kwargs = {}
        monkeypatch.setattr(agent_mod, "BedrockModel", _CaptureBedrockModel)
        monkeypatch.setattr(agent_mod, "Agent", _CaptureAgent)
        monkeypatch.delenv("GUARDRAIL_ID", raising=False)

        agent_mod.build_agent(session_id="session-xyz", force_new=True)

        model_kwargs = _CaptureBedrockModel.last_kwargs
        assert "guardrail_id" not in model_kwargs
        assert "guardrail_version" not in model_kwargs

    def test_handler_surfaces_guardrail_blocked_reply(self, monkeypatch) -> None:
        # When a guardrail intervenes, the model's blocked reply is surfaced to
        # the client through the handler unchanged.
        blocked_text = "Lo siento, no puedo ayudarte con esa solicitud."
        fake_agent = _FakeAgent(reply=blocked_text)
        monkeypatch.setattr(agent_mod, "build_agent", lambda session_id=None: fake_agent)

        event = {
            "sessionId": "session-xyz",
            "inputText": "contenido bloqueado",
            "phoneNumber": _PHONE,
            "correlationId": _CORRELATION_ID,
        }
        result = handler_mod.handler(event, None)

        assert result == {"response": blocked_text}


# ---------------------------------------------------------------------------
# 5. Handler response shaping (Requirements 10, 11, 12)
# ---------------------------------------------------------------------------


class TestHandlerResponseShaping:
    def test_plain_text_reply_shape(self, monkeypatch) -> None:
        fake_agent = _FakeAgent(reply="Tu saldo es $1.000.000,00")
        monkeypatch.setattr(agent_mod, "build_agent", lambda session_id=None: fake_agent)

        result = handler_mod.handler(
            {
                "sessionId": "s",
                "inputText": "Mi saldo",
                "phoneNumber": _PHONE,
                "correlationId": _CORRELATION_ID,
            },
            None,
        )

        assert result == {"response": "Tu saldo es $1.000.000,00"}

    def test_statement_reply_shape_includes_pdf_reference(
        self, monkeypatch, fake_lambda
    ) -> None:
        # Simulate the model calling generate_statement during the run; the
        # handler must lift the recorded S3 reference into the structured payload.
        fake_lambda.set_response(
            _STATEMENT_FN,
            {
                "success": True,
                "s3Bucket": "statement-bucket",
                "s3Key": "statements/feb.pdf",
                "fileName": "extracto_feb.pdf",
            },
        )

        def _emit_statement(_input_text: str) -> None:
            tools_mod.generate_statement("2025-02-28")

        fake_agent = _FakeAgent(reply="Aquí está tu extracto.", on_call=_emit_statement)
        monkeypatch.setattr(agent_mod, "build_agent", lambda session_id=None: fake_agent)

        result = handler_mod.handler(
            {
                "sessionId": "s",
                "inputText": "Quiero mi extracto a 2025-02-28",
                "phoneNumber": _PHONE,
                "correlationId": _CORRELATION_ID,
            },
            None,
        )

        assert result["response"]["text"] == "Aquí está tu extracto."
        assert result["response"]["statement"] == {
            "s3Bucket": "statement-bucket",
            "s3Key": "statements/feb.pdf",
            "fileName": "extracto_feb.pdf",
        }

    def test_handler_reraises_on_agent_failure(self, monkeypatch) -> None:
        # A failure during agent execution is re-raised so the Message_Processor
        # retries the message via SQS (Requirement 3.9).
        def _boom(session_id=None):
            class _Boom:
                def __call__(self, _text):
                    raise RuntimeError("bedrock unavailable")

            return _Boom()

        monkeypatch.setattr(agent_mod, "build_agent", _boom)

        with pytest.raises(RuntimeError, match="bedrock unavailable"):
            handler_mod.handler(
                {
                    "sessionId": "s",
                    "inputText": "Mi saldo",
                    "phoneNumber": _PHONE,
                    "correlationId": _CORRELATION_ID,
                },
                None,
            )
