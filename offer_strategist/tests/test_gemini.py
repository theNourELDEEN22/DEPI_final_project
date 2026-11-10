"""The Gemini adapter, against a fake client (no network, no API key)."""
import json
from types import SimpleNamespace

import pytest

from offer_strategist.llm import (
    DEFAULT_GEMINI_MODEL,
    GeminiStrategistLLM,
    OfferProposal,
    StrategistLLMError,
    StrategyBrief,
)

PROPOSAL = {
    "type": "discount",
    "discount_percent": 20,
    "bundle_partner_id": None,
    "bundle_price": None,
    "start_in_hours": 12,
    "duration_days": 7,
    "quantity_cap": 50,
    "rationale": "Clear 40 expiring units with a modest discount.",
}


class FakeModels:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.reply)


def llm_with(reply=None, error=None, model="gemini-test"):
    models = FakeModels(reply, error)
    return GeminiStrategistLLM(client=SimpleNamespace(models=models), model=model), models


def test_design_sends_structured_request_and_parses_the_reply():
    llm, models = llm_with(json.dumps(PROPOSAL))
    proposal = llm.design({"product_id": "101"}, {"urgency": "high"}, [{"round": 1, "reasons": ["margin"]}])

    assert isinstance(proposal, OfferProposal) and proposal.discount_percent == 20
    call = models.calls[0]
    assert call["model"] == "gemini-test"
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].response_schema is OfferProposal
    assert "designer agent" in call["config"].system_instruction
    sent = json.loads(call["contents"])
    assert sent["previous_attempts"][0]["reasons"] == ["margin"]


def test_analyze_uses_the_brief_schema():
    brief = {"urgency": "high", "recommended_mechanic": "buy2get1", "target_units": 40, "rationale": "x"}
    llm, models = llm_with(json.dumps(brief))
    assert isinstance(llm.analyze({"product_id": "101"}), StrategyBrief)
    assert models.calls[0]["config"].response_schema is StrategyBrief


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        "",
        json.dumps({**PROPOSAL, "discount_percent": 150}),   # out of range
        json.dumps({k: v for k, v in PROPOSAL.items() if k != "quantity_cap"}),  # missing field
        json.dumps({**PROPOSAL, "type": "free_money"}),      # unknown mechanic
    ],
)
def test_unusable_replies_raise_a_clear_error(reply):
    llm, _ = llm_with(reply)
    with pytest.raises(StrategistLLMError):
        llm.design({}, {}, [])


def test_api_failures_raise_a_clear_error():
    llm, _ = llm_with(error=ConnectionError("network down"))
    with pytest.raises(StrategistLLMError, match="network down"):
        llm.analyze({})


def test_model_comes_from_the_environment_or_the_default(monkeypatch):
    client = SimpleNamespace(models=FakeModels())
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert GeminiStrategistLLM(client=client).model == DEFAULT_GEMINI_MODEL
    monkeypatch.setenv("GEMINI_MODEL", "gemini-from-env")
    assert GeminiStrategistLLM(client=client).model == "gemini-from-env"


@pytest.mark.parametrize("schema", [OfferProposal, StrategyBrief])
def test_schemas_convert_to_the_gemini_schema_format(schema):
    # The SDK's own converter, offline. It refuses what the API refuses
    # (exclusive bounds, extra keywords), which a fake client would not notice.
    from google import genai
    from google.genai import _transformers

    converted = _transformers.t_schema(genai.Client(api_key="offline-test"), schema)
    assert set(converted.properties) == set(schema.model_fields)


def test_schemas_have_no_identity_fields():
    # The model can never choose the branch, the products, the evidence or the forecast.
    forbidden = {"branch_id", "product_ids", "evidence_ref", "forecast_ref", "status", "approval_hash"}
    assert not forbidden & set(OfferProposal.model_fields)
    assert not forbidden & set(StrategyBrief.model_fields)
