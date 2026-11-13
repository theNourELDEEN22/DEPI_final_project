"""E13 - Full scenario: near expiry to Manager report, with only human approvals (Epic E).

One pilot branch, one night, one campaign, end to end through the REAL code of
every workflow in the repo:

  1  Restocking evaluates the branch (B4-B6, restocking/workflow.py)
  2  it publishes H1; the near-expiry overstocked product routes to an offer (E6, E7)
  3  a forecast is stored for it (E5)
  4  the AI Offer Strategist plans the offer: validation, guardrails, review card (D2-D6)
  5  HUMAN: the Sales Team approves the exact version on the card (D7)  <- the only human input
  6  activation on the mock POS with an idempotency key (D9, D10, E3)
  7  the campaign sells through the POS; monitoring stops it at the cap or end (D11)
  8  the stop triggers a stock re-evaluation of that product (E9)
  9  impact vs the forecast baseline, with the KPI functions (D12, E5, H4)
 10  the Manager financial report goes to the outbox, estimate labeled (D13)
 11  the report's offer section is checked against the Offers totals (E10, H5)

`human_actions` records every manual input; the test asserts it is exactly one
approval. By default the agents are RuleBasedStrategistLLM (no API key, fully
deterministic); pass GeminiStrategistLLM() to use the real ones.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo

from offers.decisions import Decision, InMemoryDecisionStore, decide
from offers.guardrails import InMemoryActiveOffers
from offers.impact import ImpactReport, calculate_impact, offer_totals
from offers.intake import InMemoryOfferRepository
from offers.manager_report import InMemoryOutbox, build_manager_report, queue_manager_report
from offers.monitoring import MonitorAction, MonitorResult, monitor_campaign
from offers.pos_activation import ActivationOutcome, ActivationResult, activate_on_pos
from offers.review import ReviewCard
from offers.schema import Offer, OfferType
from offers.validation import RejectionLog
from offer_strategist.graph import StrategistDeps, build_planner_graph, plan_offers
from offer_strategist.llm import OfferProposal, StrategistLLM, StrategyBrief, Urgency
from restocking.workflow import _evaluate_inventory
from crossflow.contracts import OfferTotals, StockSignal, parse_restock_signal_message
from crossflow.datagen import DAILY_RATE, PilotData, _poisson, generate_pilot_branch
from crossflow.forecast import BaselineService, DemandForecaster, ForecastStore
from crossflow.handoffs import (
    ReevaluationQueue,
    SignalEvidence,
    offer_requests_from_signals,
    reevaluate,
    reevaluation_for,
    restock_signal_message,
)
from crossflow.mock_pos import MockPOS
from crossflow.reference_kpis import ReferenceKpis, SalesCampaignStats
from crossflow.reports import check_offer_section, offer_section

CAIRO = ZoneInfo("Africa/Cairo")
OPEN_HOURS = range(8, 23)  # the branch sells 08:00-22:59
PROMO_LIFT = 4             # demand multiplier while the offer is live (simulation only)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RuleBasedStrategistLLM:
    """Deterministic stand-in for the two Gemini agents. It reads the same facts and
    limits and proposes the smallest sensible discount inside them."""

    def analyze(self, facts: dict[str, Any]) -> StrategyBrief:
        days = facts.get("days_to_expiry")
        urgency = Urgency.HIGH if days is not None and days <= 3 else Urgency.MEDIUM
        stock = int(facts["stock_on_hand"])
        return StrategyBrief(
            urgency=urgency, recommended_mechanic=OfferType.DISCOUNT,
            target_units=min(stock, int(facts.get("expiring_units") or stock)),
            rationale="Rule-based: clear the expiring units before they expire.",
        )

    def design(self, facts: dict[str, Any], brief: dict[str, Any], attempts: list[dict[str, Any]]) -> OfferProposal:
        limits = facts["limits"]
        pct = min(Decimal(limits["max_discount_percent_allowed"]), Decimal("20"))
        return OfferProposal(
            type=OfferType.DISCOUNT,
            discount_percent=float(pct) if pct > 0 else 0.01,
            bundle_partner_id=None,
            bundle_price=None,
            start_in_hours=int(limits["min_start_in_hours"]),
            duration_days=min(int(limits["max_duration_days"]), 3),
            quantity_cap=max(1, min(int(limits["max_quantity_cap"]), int(brief["target_units"]) or 1)),
            rationale=f"Rule-based: {pct}% off, capped at the {brief['target_units']} target units.",
        )


@dataclass
class ScenarioResult:
    pilot: PilotData
    timeline: list[str] = field(default_factory=list)
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    contract_violations: list[str] = field(default_factory=list)
    signals: list[StockSignal] = field(default_factory=list)
    plan: Optional[dict[str, Any]] = None
    activation: Optional[ActivationResult] = None
    monitor: Optional[MonitorResult] = None
    reevaluation: Optional[dict[str, Any]] = None
    impact: Optional[ImpactReport] = None
    manager_report: Optional[dict[str, Any]] = None
    outbox: InMemoryOutbox = field(default_factory=InMemoryOutbox)
    offer_check: Optional[dict[str, Any]] = None
    pos: Optional[MockPOS] = None
    log: RejectionLog = field(default_factory=RejectionLog)

    def say(self, at: datetime, text: str) -> None:
        self.timeline.append(f"{at.astimezone(CAIRO):%a %d %b %H:%M}  {text}")


def _card_from_dict(card: dict[str, Any]) -> ReviewCard:
    return ReviewCard(
        offer_id=card["offer_id"], version=card["version"], content_hash=card["content_hash"],
        status=card["status"], generated_at=card["generated_at"], details=card["details"],
        evidence=card["evidence"], forecast=card["forecast"], lines=card["lines"], flags=card["flags"],
    )


def _consume_stock(row: dict[str, Any], units: int) -> dict[str, Any]:
    """Inventory row after selling `units`, oldest expiry first."""
    batches = sorted((dict(b) for b in row["batches"]), key=lambda b: b["expiration_date"])
    left = units
    for batch in batches:
        take = min(left, batch["stock_quantity"])
        batch["stock_quantity"] -= take
        left -= take
    return {**row, "on_hand": max(0, row["on_hand"] - units), "batches": batches}


def run_full_scenario(
    *,
    seed: int = 7,
    llm: Optional[StrategistLLM] = None,
    pos: Optional[MockPOS] = None,
    approver: str = "sales.maadi",
) -> ScenarioResult:
    pilot = generate_pilot_branch(seed)
    result = ScenarioResult(pilot=pilot, pos=pos or MockPOS())
    pos = result.pos
    settings, branch, as_of = pilot.settings, pilot.branch_id, pilot.as_of
    clock = Clock(datetime.combine(as_of, time(0, 30), CAIRO))
    inventory = {row["product_id"]: row for row in pilot.restock_inventory()}
    case = pilot.cases["near_expiry_overstock"]
    case_row = inventory[case]

    # 1. Restocking's nightly evaluation, unchanged code.
    run_id = f"restock:{branch}:{as_of.isoformat()}"
    evaluation = _evaluate_inventory({"as_of": as_of.isoformat(), "products": list(inventory.values()),
                                      "settings": settings, "active_offers": []})
    run_state = {"run_id": run_id, "branch_id": branch, **evaluation}
    result.say(clock(), f"Restocking run {run_id}: {len(evaluation['candidates'])} reorder candidates, "
                        f"{len(evaluation['expiry_watch'])} near-expiry products")

    # 2. H1, as Restocking publishes it, through the contract.
    signals, violations = parse_restock_signal_message(restock_signal_message(run_state), clock())
    result.signals, result.contract_violations = signals, violations
    result.say(clock(), f"H1: {len(signals)} stock signals accepted, {len(violations)} refused by the contract")

    # 3 + 4. Forecasts (E5), routing (E6/E7), and the AI Offer Strategist (D2-D6).
    history = list(pilot.sales)
    forecaster = DemandForecaster(history)
    forecasts = ForecastStore(forecaster, clock)

    def forecast_ref_for(branch_id: int, product_id: int) -> Optional[str]:
        forecast = forecasts.create_horizon(branch_id, product_id, clock())
        return forecast.ref if forecast else None

    requests, skipped = offer_requests_from_signals(
        signals, inventory=inventory, settings=settings, as_of=as_of, forecast_ref_for=forecast_ref_for)
    result.say(clock(), f"E6/E7: {len(requests)} offer candidate(s), {len(skipped)} signal(s) not for offers")

    catalog, prices, rules = pilot.catalog(), pilot.prices(), pilot.product_rules()
    active = InMemoryActiveOffers()
    deps = StrategistDeps(
        llm=llm or RuleBasedStrategistLLM(), catalog=catalog, prices=prices, rules=rules, active=active,
        evidence_reader=SignalEvidence(signals), forecast_reader=forecasts,
        repo=InMemoryOfferRepository(), log=result.log, clock=clock,
    )
    plans = plan_offers(build_planner_graph(deps), requests)
    plan = next((p for p in plans if p["product_id"] == str(case)), None)
    result.plan = plan
    if plan is None or plan["outcome"] != "submitted":
        result.say(clock(), f"Strategist did not submit the case product: {plan and plan['reasons']}")
        return result
    result.say(clock(), f"Strategist: offer {plan['offer_id'][:8]} submitted after {plan['rounds']} round(s), "
                        f"evidence {plan['offer']['evidence_ref']}")

    # 5. The only human step: the Sales Team approves the version on the card.
    clock.now = datetime.combine(as_of, time(9, 0), CAIRO)
    store = InMemoryDecisionStore()
    approved, record = decide(Offer(**plan["offer"]), _card_from_dict(plan["review_card"]),
                              Decision.APPROVE, approver, None, store, clock())
    result.human_actions.append({"actor": approver, "action": "approve offer", "offer_id": approved.offer_id,
                                 "version": approved.version, "at": clock()})
    result.say(clock(), f"HUMAN {approver} approved v{approved.version} (hash {record.approval_hash[:12]})")

    # 6. Activation just before the start (final recheck + POS idempotency key).
    clock.now = approved.start_date - timedelta(minutes=30)
    activation = activate_on_pos(
        approved, pos=pos, store=store, catalog=catalog, prices=prices, rules=rules, active=active,
        validation_config=deps.validation_config, guardrail_config=deps.guardrail_config,
        log=result.log, now=clock(),
    )
    result.activation = activation
    result.say(clock(), f"POS activation: {activation.outcome.value}")
    if activation.outcome != ActivationOutcome.ACTIVATED:
        return result
    live = activation.offer
    active.offers.append(live)

    # 7. The campaign sells through the POS; monitoring runs every hour.
    stock_at_start = case_row["on_hand"]
    sold = {"units": 0}
    campaign_lines: list[dict[str, Any]] = []
    stats = SalesCampaignStats(campaign_lines, [], lambda b, ps: stock_at_start - sold["units"], clock)
    stats.track(live)
    rng = random.Random(seed + 1)
    next_line = max(l["sale_line_id"] for l in history) + 1
    next_txn = max(l["transaction_id"] for l in history) + 1
    product = pilot.product(case)
    hourly_rate = DAILY_RATE["High"] / len(OPEN_HOURS)
    t = live.start_date
    monitor = None
    while True:
        local_hour = t.astimezone(CAIRO).hour
        if local_hour in OPEN_HOURS and stock_at_start - sold["units"] > 0:
            promo_live = any(p["offer_id"] == live.offer_id for p in pos.live_offers().values())
            for _ in range(_poisson(rng, hourly_rate * (PROMO_LIFT if promo_live else 1))):
                qty = min(rng.choice((1, 1, 2)), stock_at_start - sold["units"])
                if qty <= 0:
                    break
                campaign_lines.append(pos.ring_up(
                    sale_line_id=next_line, transaction_id=next_txn, branch_id=branch, product_id=case,
                    sku=product["sku"], product_name=product["product_name"], quantity=qty,
                    unit_price=product["unit_price"], at=t + timedelta(minutes=rng.randint(0, 59)),
                ))
                next_line, next_txn = next_line + 1, next_txn + 1
                sold["units"] += qty
        t += timedelta(hours=1)
        clock.now = t
        monitor = monitor_campaign(live, stats=stats, pos=pos, log=result.log, now=clock())
        if monitor.action not in (MonitorAction.CONTINUE, MonitorAction.DATA_UNAVAILABLE):
            break
    result.monitor = monitor
    snap = monitor.snapshot
    result.say(clock(), f"Monitoring: {monitor.action.value}"
                        + (f", {snap.redemptions}/{snap.quantity_cap} redeemed (overshoot {snap.overshoot})" if snap else ""))
    if monitor.action not in (MonitorAction.STOPPED_AT_CAP, MonitorAction.ENDED):
        return result

    # 8. XW-3: the end / sell-out triggers a re-evaluation of the product.
    queue = ReevaluationQueue()
    request = reevaluation_for(monitor)
    queue.submit(request)
    now_row = _consume_stock(case_row, sold["units"])
    result.reevaluation = reevaluate(request, [now_row], settings, clock().astimezone(CAIRO).date())
    result.say(clock(), f"E9: re-evaluated products {result.reevaluation['evaluated_product_ids']} "
                        f"({len(result.reevaluation['candidates'])} reorder candidate(s))")

    # 9. Impact vs baseline with the KPI functions (H4) and the forecast baseline (E5).
    window_end = clock() if monitor.offer.status.value == "stopped" else None
    final_stats = stats.get_stats(live.offer_id, live.version, live.branch_id)
    all_lines = history + campaign_lines
    kpis = ReferenceKpis(all_lines, pilot.returns, prices)
    impact = calculate_impact(monitor.offer, stats=final_stats, kpi=kpis,
                              baseline=BaselineService(forecaster, prices), prices=prices,
                              window_end=window_end, now=clock())
    result.impact = impact
    result.say(clock(), f"Impact: {impact.actual.units} units vs baseline {impact.baseline.units}, "
                        f"incremental profit {impact.incremental.gross_profit}, ROI {impact.roi}")

    # 10. Manager report through the outbox.
    report = build_manager_report(monitor.offer, impact, prices=prices, stock_at_start=stock_at_start)
    queue_manager_report(report, result.outbox, now=clock())
    result.manager_report = report
    result.say(clock(), f"Manager report queued ({len(result.outbox.messages)} message in the outbox)")

    # 11. E10: the report's offer section must match the Offers totals (H5).
    totals = offer_totals(monitor.offer, final_stats, prices)
    pos_log = pos.redemption_log(live.offer_id)
    h5 = OfferTotals(
        idempotency_key=f"offer-totals:{live.offer_id}:v{live.version}",
        branch_id=branch, occurred_at=clock(), offer_id=live.offer_id, version=live.version,
        period_start=live.start_date, period_end=impact.window_end,
        redemptions=final_stats.redemptions, returns=final_stats.returns,
        discount_amount=Decimal(totals["discount_amount"]), receipts=pos_log["receipts"],
    )
    section = offer_section(campaign_lines, branch, live.start_date, impact.window_end)
    result.offer_check = check_offer_section(section, [h5])
    result.say(clock(), f"E10: daily report offer section {result.offer_check['status']}")
    return result
