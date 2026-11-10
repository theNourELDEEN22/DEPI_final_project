"""AI Offer Strategist - the LangGraph multi-agent workflow.

One product (build_strategist_graph):

    gather_facts ── cannot be promoted ─────────────────────────► END  (skipped)
         │
      analyst    LLM agent, rule-based fallback if it fails
         │
      designer   LLM agent  ◄──────── guardrail reasons ───────┐
         │                                                     │
      critic     D3 validation + D4/D5 guardrails (code) ── fail, rounds left
         │ pass                                  │ fail, no rounds left
      submit     D2 intake + D6 review card      escalate
         │                                       │
        END (submitted: waits for Sales, D7)    END (escalated to a person)

Many products (build_planner_graph): a LangGraph `Send` fan-out runs one
strategist per product in parallel and collects the results.

The models only PROPOSE. Every gate is the existing deterministic code, and the
approval stays with a person (D7). Nothing here activates an offer.
State holds only JSON values, so any LangGraph checkpointer can store it.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Callable, Optional, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, ValidationError

from offers.guardrails import (
    ActiveOffers,
    GuardrailConfig,
    PriceReader,
    ProductRules,
    check_all_guardrails,
)
from offers.intake import InMemoryOfferRepository, IntakeError, candidate_key, intake_candidate
from offers.review import EvidenceReader, ForecastReader, ReviewNotAllowed, build_review_card
from offers.schema import Offer, OfferType
from offers.validation import CatalogReader, RejectionLog, ValidationConfig, validate_offer
from offer_strategist.facts import (
    TWO_PLACES,
    FactsUnavailable,
    StrategistConfig,
    StrategistRequest,
    gather_facts,
)
from offer_strategist.llm import OfferProposal, StrategistLLM, StrategyBrief, fallback_brief

AI_LABEL = "AI-GENERATED: advisory text, not a stored figure"

# Fields the intake ignores anyway; dropped so the stored draft gets fresh ones.
_SERVER_FIELDS = {"offer_id", "version", "status", "approval_hash", "created_at"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class StrategistDeps:
    """Everything the workflow reads or writes. M1's real stores replace the in-memory ones."""

    llm: StrategistLLM
    catalog: CatalogReader
    prices: PriceReader
    rules: ProductRules
    active: ActiveOffers
    evidence_reader: EvidenceReader
    forecast_reader: ForecastReader
    repo: InMemoryOfferRepository
    log: Any = field(default_factory=RejectionLog)  # anything with an .entries list
    validation_config: ValidationConfig = field(default_factory=ValidationConfig)
    guardrail_config: GuardrailConfig = field(default_factory=GuardrailConfig)
    config: StrategistConfig = field(default_factory=StrategistConfig)
    clock: Callable[[], datetime] = _utc_now


class StrategistState(TypedDict, total=False):
    request: dict[str, Any]
    as_of: str
    facts: dict[str, Any]
    brief: dict[str, Any]
    round: int
    proposal: Optional[dict[str, Any]]
    design_error: Optional[str]
    attempts: Annotated[list[dict[str, Any]], operator.add]
    candidate: Optional[dict[str, Any]]
    outcome: str  # planning | skipped | submitted | escalated
    reasons: list[str]
    offer: dict[str, Any]
    review_card: dict[str, Any]


# --- turning a proposal into an offer ----------------------------------------------------


def build_candidate(
    proposal: OfferProposal,
    request: StrategistRequest,
    facts: dict[str, Any],
    *,
    now: datetime,
    config: StrategistConfig,
) -> tuple[Optional[Offer], list[str]]:
    """Proposal + locked fields -> Offer. Returns (offer or None, reasons).

    Branch, products, evidence and forecast always come from the request, never
    from the model. All reasons are collected so the designer can fix them in one go."""
    reasons: list[str] = []

    if not config.min_lead_hours <= proposal.start_in_hours <= config.max_start_hours:
        reasons.append(
            f"the offer must start between {config.min_lead_hours} and {config.max_start_hours} "
            f"hours from now, not {proposal.start_in_hours}"
        )
    if proposal.quantity_cap > request.stock_on_hand:
        reasons.append(
            f"quantity cap {proposal.quantity_cap} is above the {request.stock_on_hand} units in stock"
        )

    start = now + timedelta(hours=proposal.start_in_hours)
    fields: dict[str, Any] = {
        "branch_id": request.branch_id,
        "type": proposal.type.value,
        "product_ids": [request.product_id],
        "start_date": start,
        "end_date": start + timedelta(days=proposal.duration_days),
        "quantity_cap": proposal.quantity_cap,
        "evidence_ref": request.evidence_ref,
        "forecast_ref": request.forecast_ref,
    }

    buildable = True
    if proposal.type == OfferType.DISCOUNT:
        if proposal.discount_percent is None:
            reasons.append("a discount offer needs discount_percent")
            buildable = False
        else:
            fields["discount_percent"] = Decimal(str(proposal.discount_percent)).quantize(TWO_PLACES)
    elif proposal.type == OfferType.BUY2GET1:
        fields.update(buy_qty=2, get_qty=1)
    else:
        allowed = [p["product_id"] for p in facts.get("bundle_partners", [])]
        if proposal.bundle_partner_id not in allowed:
            reasons.append(
                f"bundle partner {proposal.bundle_partner_id!r} is not one of the offered partners "
                f"({', '.join(allowed) or 'none'})"
            )
            buildable = False
        if proposal.bundle_price is None:
            reasons.append("a bundle offer needs bundle_price")
            buildable = False
        if buildable:
            fields["bundle_items"] = [
                {"product_id": request.product_id, "quantity": 1},
                {"product_id": proposal.bundle_partner_id, "quantity": 1},
            ]
            fields["bundle_price"] = Decimal(str(proposal.bundle_price)).quantize(TWO_PLACES)

    if not buildable:
        return None, reasons
    try:
        return Offer(**fields), reasons
    except ValidationError as exc:
        reasons.extend(f"offer schema: {err['msg']}" for err in exc.errors())
        return None, reasons


# --- the per-product graph --------------------------------------------------------------


def build_strategist_graph(
    deps: StrategistDeps, checkpointer: BaseCheckpointSaver | None = None
):
    """Compile the multi-agent workflow for one product."""

    def record(state: StrategistState, action: str, reasons, **extra) -> None:
        request = state.get("request") or {}
        deps.log.entries.append(
            {
                "offer_id": extra.pop("offer_id", None),
                "version": extra.pop("version", None),
                "branch_id": request.get("branch_id"),
                "product_id": request.get("product_id"),
                "stage": "strategist",
                "action": action,
                "reasons": list(reasons),
                "at": deps.clock(),
                **extra,
            }
        )

    def gather(state: StrategistState) -> dict[str, Any]:
        now = deps.clock()
        try:
            request = StrategistRequest.model_validate(state.get("request") or {})
        except ValidationError as exc:
            reasons = [f"invalid request: {'.'.join(map(str, e['loc']))} {e['msg']}" for e in exc.errors()]
            record(state, "skipped", reasons)
            return {"outcome": "skipped", "reasons": reasons, "round": 0}
        try:
            facts = gather_facts(
                request,
                catalog=deps.catalog,
                prices=deps.prices,
                rules=deps.rules,
                active=deps.active,
                evidence_reader=deps.evidence_reader,
                forecast_reader=deps.forecast_reader,
                repo=deps.repo,
                validation_config=deps.validation_config,
                guardrail_config=deps.guardrail_config,
                config=deps.config,
                now=now,
            )
        except FactsUnavailable as exc:
            record(state, "skipped", exc.reasons)
            return {"outcome": "skipped", "reasons": list(exc.reasons), "round": 0}
        return {
            "request": request.model_dump(mode="json"),
            "facts": facts,
            "as_of": now.isoformat(),
            "round": 0,
            "outcome": "planning",
            "reasons": [],
        }

    def analyst(state: StrategistState) -> dict[str, Any]:
        facts = state["facts"]
        try:
            brief = deps.llm.analyze(facts)
            if not isinstance(brief, StrategyBrief):
                brief = StrategyBrief.model_validate(brief)
            payload = {**brief.model_dump(mode="json"), "source": "analyst_agent"}
            payload["target_units"] = min(payload["target_units"], int(facts["stock_on_hand"]))
        except Exception as exc:  # any model failure: carry on with a labeled rule-based brief
            payload = fallback_brief(facts, f"{type(exc).__name__}: {exc}"[:300])
        return {"brief": payload}

    def designer(state: StrategistState) -> dict[str, Any]:
        round_no = state.get("round", 0) + 1
        try:
            proposal = deps.llm.design(state["facts"], state["brief"], list(state.get("attempts") or []))
            if not isinstance(proposal, OfferProposal):
                proposal = OfferProposal.model_validate(proposal)
        except Exception as exc:  # a failed call costs a round; it never crashes the run
            error = f"the designer agent gave no usable proposal ({type(exc).__name__}: {str(exc)[:200]})"
            return {"round": round_no, "proposal": None, "design_error": error}
        return {"round": round_no, "proposal": proposal.model_dump(mode="json"), "design_error": None}

    def critic(state: StrategistState) -> dict[str, Any]:
        request = StrategistRequest.model_validate(state["request"])
        as_of = datetime.fromisoformat(state["as_of"])
        proposal = state.get("proposal")
        offer: Optional[Offer] = None
        if proposal is None:
            reasons = [state.get("design_error") or "no proposal"]
        else:
            offer, reasons = build_candidate(
                OfferProposal.model_validate(proposal), request, state["facts"], now=as_of, config=deps.config
            )
            if offer is not None:
                reasons += validate_offer(offer, deps.catalog, deps.validation_config, as_of).reasons
                reasons += check_all_guardrails(
                    offer, deps.prices, deps.rules, deps.active, deps.guardrail_config
                ).reasons
        passed = not reasons
        record(state, "proposal_passed" if passed else "proposal_refused", reasons, round=state["round"])
        return {
            "attempts": [{"round": state["round"], "proposal": proposal, "reasons": list(reasons), "passed": passed}],
            "candidate": offer.model_dump(mode="json") if (offer is not None and passed) else None,
        }

    def submit(state: StrategistState) -> dict[str, Any]:
        raw = {k: v for k, v in state["candidate"].items() if k not in _SERVER_FIELDS}
        existing = deps.repo.find_by_candidate_key(candidate_key(raw))
        if existing is not None:  # another run took this signal in the meantime
            reasons = [f"this stock signal already has an offer candidate: {existing.offer_id}"]
            record(state, "skipped", reasons)
            return {"outcome": "skipped", "reasons": reasons}
        try:
            offer = intake_candidate(raw, deps.repo)
        except IntakeError as exc:
            reasons = [f"intake refused the offer: {exc}"]
            record(state, "escalated", reasons)
            return {"outcome": "escalated", "reasons": reasons}
        try:
            card, pending = build_review_card(
                offer,
                catalog=deps.catalog,
                prices=deps.prices,
                rules=deps.rules,
                active=deps.active,
                evidence_reader=deps.evidence_reader,
                forecast_reader=deps.forecast_reader,
                validation_config=deps.validation_config,
                guardrail_config=deps.guardrail_config,
                now=deps.clock(),
            )
        except ReviewNotAllowed as exc:
            reasons = ["the Sales review check refused the offer", *exc.reasons]
            record(state, "escalated", reasons, offer_id=offer.offer_id, version=offer.version)
            return {"outcome": "escalated", "reasons": reasons, "offer": offer.model_dump(mode="json")}
        record(state, "submitted", [], offer_id=pending.offer_id, version=pending.version)
        return {
            "outcome": "submitted",
            "reasons": [],
            "offer": pending.model_dump(mode="json"),
            "review_card": card.to_dict(),
        }

    def escalate(state: StrategistState) -> dict[str, Any]:
        attempts = state.get("attempts") or []
        last = attempts[-1]["reasons"] if attempts else []
        reasons = [f"no proposal passed the guardrails after {state.get('round', 0)} round(s)", *last]
        record(state, "escalated", reasons)
        return {"outcome": "escalated", "reasons": reasons}

    def after_gather(state: StrategistState) -> str:
        return "analyst" if state.get("outcome") == "planning" else "end"

    def after_critic(state: StrategistState) -> str:
        if state.get("candidate"):
            return "submit"
        if state.get("round", 0) >= deps.config.max_rounds:
            return "escalate"
        return "designer"

    graph = StateGraph(StrategistState)
    graph.add_node("gather_facts", gather)
    graph.add_node("analyst", analyst)
    graph.add_node("designer", designer)
    graph.add_node("critic", critic)
    graph.add_node("submit", submit)
    graph.add_node("escalate", escalate)

    graph.add_edge(START, "gather_facts")
    graph.add_conditional_edges("gather_facts", after_gather, {"analyst": "analyst", "end": END})
    graph.add_edge("analyst", "designer")
    graph.add_edge("designer", "critic")
    graph.add_conditional_edges(
        "critic", after_critic, {"submit": "submit", "designer": "designer", "escalate": "escalate"}
    )
    graph.add_edge("submit", END)
    graph.add_edge("escalate", END)
    return graph.compile(checkpointer=checkpointer)


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    """The result a UI or the planner shows. AI text is kept apart and labeled."""
    request = state.get("request") or {}
    attempts = list(state.get("attempts") or [])
    last = attempts[-1].get("proposal") if attempts else None
    return {
        "branch_id": str(request.get("branch_id")),
        "product_id": str(request.get("product_id")),
        "outcome": state.get("outcome", "unknown"),
        "rounds": state.get("round", 0),
        "reasons": list(state.get("reasons") or []),
        "offer_id": (state.get("offer") or {}).get("offer_id"),
        "offer": state.get("offer"),
        "review_card": state.get("review_card"),
        "attempts": attempts,
        "ai_notes": {
            "label": AI_LABEL,
            "brief": state.get("brief"),
            "designer_rationale": (last or {}).get("rationale"),
        },
    }


def _payload(request) -> dict[str, Any]:
    if isinstance(request, BaseModel):
        return request.model_dump(mode="json")
    return dict(request)


def plan_offer(graph, request, *, thread_id: str | None = None) -> dict[str, Any]:
    """Run the strategist for one product. With a thread_id (needs a checkpointer),
    a finished run is returned as-is instead of being planned twice."""
    config: dict[str, Any] = {}
    if thread_id:
        config = {"configurable": {"thread_id": thread_id}}
        existing = graph.get_state(config)
        if existing and existing.values and not existing.next:
            return summarize(existing.values)
    return summarize(graph.invoke({"request": _payload(request)}, config))


# --- many products in parallel ---------------------------------------------------------------


class PlannerState(TypedDict, total=False):
    requests: list[dict[str, Any]]
    results: Annotated[list[dict[str, Any]], operator.add]


class ProductTask(TypedDict):
    request: dict[str, Any]


def build_planner_graph(deps: StrategistDeps, checkpointer: BaseCheckpointSaver | None = None):
    """Fan out one strategist per (branch, product) with LangGraph Send, in parallel."""
    strategist = build_strategist_graph(deps)  # no own checkpointer: uses the planner's

    def fan_out(state: PlannerState) -> list[Send]:
        seen: set[tuple[str, str]] = set()
        sends: list[Send] = []
        for request in state.get("requests") or []:
            key = (str(request.get("branch_id")), str(request.get("product_id")))
            if key in seen:
                continue  # one plan per product per run
            seen.add(key)
            sends.append(Send("plan_product", {"request": request}))
        return sends

    def plan_product(task: ProductTask) -> dict[str, Any]:
        final = strategist.invoke({"request": task["request"]})
        return {"results": [summarize(final)]}

    graph = StateGraph(PlannerState)
    graph.add_node("plan_product", plan_product)
    graph.add_conditional_edges(START, fan_out, ["plan_product"])
    graph.add_edge("plan_product", END)
    return graph.compile(checkpointer=checkpointer)


def plan_offers(
    planner,
    requests,
    *,
    thread_id: str | None = None,
    max_concurrency: int = 4,
) -> list[dict[str, Any]]:
    """Plan many products. Results are sorted by (branch, product).
    With a thread_id (needs a checkpointer), a finished run is not planned again."""
    config: dict[str, Any] = {"max_concurrency": max_concurrency}
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}
        existing = planner.get_state(config)
        if existing and existing.values and not existing.next:
            return _sorted(existing.values.get("results") or [])
    final = planner.invoke({"requests": [_payload(r) for r in requests]}, config)
    return _sorted(final.get("results") or [])


def _sorted(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(results, key=lambda r: (r["branch_id"], r["product_id"]))
