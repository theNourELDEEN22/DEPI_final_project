import json

from langgraph.checkpoint.memory import InMemorySaver

from offers.decisions import Decision, InMemoryDecisionStore, decide
from offers.review import ReviewCard
from offers.schema import Offer, OfferStatus, OfferType
from offer_strategist.facts import StrategistConfig
from offer_strategist.fakes import ScriptedLLM, make_proposal
from offer_strategist.graph import build_strategist_graph, plan_offer
from offer_strategist.tests.world import make_deps, make_request


def run(llm, request=None, **deps_over):
    deps = make_deps(llm, **deps_over)
    result = plan_offer(build_strategist_graph(deps), request or make_request())
    return result, deps


# --- happy path ------------------------------------------------------------------------------


def test_valid_first_proposal_goes_to_sales_review():
    llm = ScriptedLLM({"101": [make_proposal(discount=20, rationale="NOTE-FROM-THE-MODEL")]})
    result, deps = run(llm)

    assert result["outcome"] == "submitted"
    assert result["rounds"] == 1
    assert result["offer"]["status"] == OfferStatus.PENDING_APPROVAL.value
    assert result["offer"]["discount_percent"] == "20.00"
    assert result["review_card"]["content_hash"]
    # the stored draft is in the repository, exactly one
    assert len(deps.repo.all()) == 1
    # the AI text never reaches the review card (D6: stored figures only)
    assert "NOTE-FROM-THE-MODEL" not in json.dumps(result["review_card"])
    assert result["ai_notes"]["designer_rationale"] == "NOTE-FROM-THE-MODEL"
    assert result["ai_notes"]["label"].startswith("AI-GENERATED")


def test_identity_fields_come_from_the_request_not_the_model():
    llm = ScriptedLLM({"101": [make_proposal(discount=20)]})
    result, _ = run(llm)
    offer = result["offer"]
    assert offer["branch_id"] == "1"
    assert offer["product_ids"] == ["101"]
    assert offer["evidence_ref"] == "ev-101" and offer["forecast_ref"] == "fc-101"


def test_submitted_offer_can_be_approved_with_the_existing_decision_flow():
    llm = ScriptedLLM({"101": [make_proposal(discount=20)]})
    result, _ = run(llm)
    offer = Offer(**result["offer"])
    card_data = result["review_card"]
    card = ReviewCard(
        offer_id=card_data["offer_id"], version=card_data["version"], content_hash=card_data["content_hash"],
        status=card_data["status"], generated_at=None, details={}, evidence={}, forecast={}, lines=[], flags={},
    )
    approved, record = decide(offer, card, Decision.APPROVE, "sales.maadi", None, InMemoryDecisionStore())
    assert approved.status == OfferStatus.APPROVED and record.approval_hash


def test_bundle_with_an_offered_partner():
    llm = ScriptedLLM(
        {"101": [make_proposal(OfferType.BUNDLE, partner="102", bundle_price=110, duration_days=7)]}
    )
    result, _ = run(llm, make_request(bundle_partner_ids=("102",)))
    assert result["outcome"] == "submitted", result["reasons"]
    assert result["offer"]["bundle_price"] == "110.00"


# --- the critic loop ----------------------------------------------------------------------------


def test_refused_proposal_is_sent_back_with_reasons_and_fixed():
    llm = ScriptedLLM({"101": [make_proposal(discount=45), make_proposal(discount=20)]})
    result, deps = run(llm)

    assert result["outcome"] == "submitted"
    assert result["rounds"] == 2
    first, second = result["attempts"]
    assert not first["passed"] and any("margin" in r for r in first["reasons"])
    assert second["passed"]
    # the designer saw the first attempt and its reasons on round 2
    _pid, seen = llm.design_calls[1]
    assert len(seen) == 1 and any("margin" in r for r in seen[0]["reasons"])
    actions = [e["action"] for e in deps.log.entries]
    assert actions == ["proposal_refused", "proposal_passed", "submitted"]


def test_no_passing_proposal_escalates_and_stores_nothing():
    llm = ScriptedLLM({"101": [make_proposal(discount=45)] * 3})
    result, deps = run(llm)

    assert result["outcome"] == "escalated"
    assert result["rounds"] == 3
    assert "3 round(s)" in result["reasons"][0]
    assert deps.repo.all() == []
    assert result["offer_id"] is None


def test_round_limit_is_configurable():
    llm = ScriptedLLM({"101": [make_proposal(discount=45), make_proposal(discount=20)]})
    result, _ = run(llm, config=StrategistConfig(max_rounds=1))
    assert result["outcome"] == "escalated" and result["rounds"] == 1


def test_designer_failure_costs_a_round_but_never_crashes():
    llm = ScriptedLLM({"101": [TimeoutError("model timed out"), make_proposal(discount=20)]})
    result, _ = run(llm)
    assert result["outcome"] == "submitted" and result["rounds"] == 2
    assert "designer agent" in result["attempts"][0]["reasons"][0]


def test_analyst_failure_falls_back_to_a_labeled_rule_based_brief():
    llm = ScriptedLLM({"101": [make_proposal(discount=20)]}, briefs={"101": RuntimeError("quota")})
    result, _ = run(llm)
    assert result["outcome"] == "submitted"
    assert result["ai_notes"]["brief"]["source"] == "fallback"


def test_all_reasons_are_collected_in_one_round():
    llm = ScriptedLLM(
        {"101": [make_proposal(discount=45, start_in_hours=2, duration_days=12, quantity_cap=500)]},
    )
    result, _ = run(llm, config=StrategistConfig(max_rounds=1))
    reasons = " | ".join(result["attempts"][0]["reasons"])
    for expected in ("must start", "above the 120 units", "margin", "expiry"):
        assert expected in reasons


def test_bundle_partner_the_model_made_up_is_refused():
    llm = ScriptedLLM({"101": [make_proposal(OfferType.BUNDLE, partner="102", bundle_price=110)]})
    result, _ = run(llm, config=StrategistConfig(max_rounds=1))  # no partners offered
    assert result["outcome"] == "escalated"
    assert any("not one of the offered partners" in r for r in result["attempts"][0]["reasons"])


# --- no model calls when the product cannot be promoted ----------------------------------------


def test_skipped_products_cost_no_model_calls():
    llm = ScriptedLLM({})
    result, _ = run(llm, make_request("103"))  # expires tomorrow
    assert result["outcome"] == "skipped"
    assert llm.analyze_calls == [] and llm.design_calls == []


def test_invalid_request_is_skipped_not_crashed():
    deps = make_deps(ScriptedLLM({}))
    result = plan_offer(build_strategist_graph(deps), {"branch_id": "1", "product_id": "101"})
    assert result["outcome"] == "skipped"
    assert any("invalid request" in r for r in result["reasons"])


def test_same_stock_signal_is_not_planned_twice():
    llm = ScriptedLLM({"101": [make_proposal(discount=20), make_proposal(discount=15)]})
    deps = make_deps(llm)
    graph = build_strategist_graph(deps)
    assert plan_offer(graph, make_request())["outcome"] == "submitted"
    again = plan_offer(graph, make_request())
    assert again["outcome"] == "skipped"
    assert any("already has a discount offer candidate" in r for r in again["reasons"])
    assert len(llm.design_calls) == 1 and len(deps.repo.all()) == 1


def test_checkpointed_run_is_returned_not_replanned():
    llm = ScriptedLLM({"101": [make_proposal(discount=20)]})
    graph = build_strategist_graph(make_deps(llm), checkpointer=InMemorySaver())
    first = plan_offer(graph, make_request(), thread_id="strategist:1:101:2026-11-10")
    second = plan_offer(graph, make_request(), thread_id="strategist:1:101:2026-11-10")
    assert first["outcome"] == second["outcome"] == "submitted"
    assert first["offer_id"] == second["offer_id"]
    assert len(llm.design_calls) == 1
