"""E6-E9 - handoffs between Restocking and Offers, using Restocking's real evaluation."""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from offers.intake import InMemoryOfferRepository
from offers.monitoring import MonitorAction, MonitorResult
from offers.guardrails import InMemoryActiveOffers
from offers.schema import OfferStatus
from offer_strategist.graph import StrategistDeps, build_strategist_graph, plan_offer
from restocking.workflow import _evaluate_inventory
from crossflow.contracts import parse_restock_signal_message
from crossflow.datagen import generate_pilot_branch
from crossflow.forecast import DemandForecaster, ForecastStore
from crossflow.handoffs import (
    ReevaluationQueue,
    Route,
    SignalEvidence,
    clearance_notices,
    cleared_product_ids,
    offer_requests_from_signals,
    reevaluate,
    reevaluation_for,
    restock_signal_message,
    restocking_routes,
    route_product,
)
from crossflow.scenario import RuleBasedStrategistLLM, _consume_stock
from crossflow.tests.support import make_offer

CAIRO = ZoneInfo("Africa/Cairo")


@pytest.fixture(scope="module")
def pilot():
    return generate_pilot_branch(7)


@pytest.fixture(scope="module")
def inventory(pilot):
    return {row["product_id"]: row for row in pilot.restock_inventory()}


EXPECTED = {
    "near_expiry_overstock": Route.OFFER,
    "near_expiry_low_stock": Route.RESTOCK,
    "high_demand_low_stock": Route.RESTOCK,
    "medium_demand_low_stock": Route.RESTOCK,
    "low_demand_low_stock": Route.NONE,
    "low_demand_near_expiry": Route.OFFER,  # proposed rule D-06
    "underrated_seller": Route.NONE,
}


# --- E7 -------------------------------------------------------------------------------------------


def test_rule_routes_every_named_case(pilot, inventory):
    for case, expected in EXPECTED.items():
        decision = route_product(inventory[pilot.cases[case]], pilot.settings, pilot.as_of)
        assert decision.route == expected, (case, decision.reason)


def test_near_expiry_and_overstocked_gives_an_offer_not_a_restock(pilot, inventory):
    """E7 done-when: the rule is covered by a test, against Restocking's own code."""
    pid = pilot.cases["near_expiry_overstock"]
    routes = restocking_routes(list(inventory.values()), pilot.settings, pilot.as_of)
    assert routes[pid] == Route.OFFER
    assert route_product(inventory[pid], pilot.settings, pilot.as_of).route == Route.OFFER


def test_restocking_agrees_with_the_rule_except_the_open_low_demand_decision(pilot, inventory):
    routes = restocking_routes(list(inventory.values()), pilot.settings, pilot.as_of)
    disagreements = {
        pid for pid, row in inventory.items()
        if routes[pid] != route_product(row, pilot.settings, pilot.as_of).route
    }
    assert disagreements == {pilot.cases["low_demand_near_expiry"]}


@pytest.mark.xfail(strict=True, reason="D-06 open: Restocking reorders near-expiry Low-demand stock (NEAR_EXPIRY)")
def test_restocking_fully_agrees_with_the_rule(pilot, inventory):
    routes = restocking_routes(list(inventory.values()), pilot.settings, pilot.as_of)
    assert all(routes[pid] == route_product(row, pilot.settings, pilot.as_of).route for pid, row in inventory.items())


# --- E6 --------------------------------------------------------------------------------------------


def nightly_signals(pilot, inventory):
    evaluation = _evaluate_inventory({"as_of": pilot.as_of.isoformat(), "products": list(inventory.values()),
                                      "settings": pilot.settings, "active_offers": []})
    run = {"run_id": f"restock:1:{pilot.as_of}", "branch_id": 1, **evaluation}
    signals, _ = parse_restock_signal_message(restock_signal_message(run), datetime.now(timezone.utc))
    return signals


def test_stock_signal_becomes_an_offer_candidate_that_links_to_it(pilot, inventory):
    signals = nightly_signals(pilot, inventory)
    now = datetime.combine(pilot.as_of, time(0, 30), CAIRO)
    forecasts = ForecastStore(DemandForecaster(pilot.sales), lambda: now)
    requests, _ = offer_requests_from_signals(
        signals, inventory=inventory, settings=pilot.settings, as_of=pilot.as_of,
        forecast_ref_for=lambda b, p: forecasts.create_horizon(b, p, now).ref,
    )
    pid = pilot.cases["near_expiry_overstock"]
    signal = next(s for s in signals if s.product_id == pid)
    request = next(r for r in requests if r.product_id == str(pid))
    assert request.evidence_ref == signal.evidence_ref

    deps = StrategistDeps(
        llm=RuleBasedStrategistLLM(), catalog=pilot.catalog(), prices=pilot.prices(), rules=pilot.product_rules(),
        active=InMemoryActiveOffers(), evidence_reader=SignalEvidence(signals), forecast_reader=forecasts,
        repo=InMemoryOfferRepository(), clock=lambda: now,
    )
    result = plan_offer(build_strategist_graph(deps), request)
    assert result["outcome"] == "submitted", result["reasons"]
    assert result["offer"]["evidence_ref"] == signal.evidence_ref            # the candidate links to H1
    assert result["review_card"]["evidence"]["ref"] == signal.evidence_ref   # and the card shows it
    assert result["review_card"]["evidence"]["facts"]["signal_key"] == signal.idempotency_key


def test_repeated_signals_give_one_request_and_missing_forecast_is_skipped(pilot, inventory):
    signals = nightly_signals(pilot, inventory)
    requests, skipped = offer_requests_from_signals(
        signals + signals, inventory=inventory, settings=pilot.settings, as_of=pilot.as_of,
        forecast_ref_for=lambda b, p: None,
    )
    assert requests == []
    assert [s["reason"] for s in skipped] == ["no stored forecast"]


def test_low_stock_near_expiry_signal_is_not_turned_into_an_offer(pilot, inventory):
    from crossflow.contracts import StockSignal
    pid = pilot.cases["near_expiry_low_stock"]
    signal = StockSignal(idempotency_key="k", branch_id=1, occurred_at=datetime.now(timezone.utc), run_id="r",
                         product_id=pid, signal_type="NEAR_EXPIRY", evidence_ref="r:x",
                         expiring_batches=[{"expires_on": pilot.as_of + timedelta(days=3), "days_left": 3, "quantity": 5}])
    requests, skipped = offer_requests_from_signals(
        [signal], inventory=inventory, settings=pilot.settings, as_of=pilot.as_of, forecast_ref_for=lambda b, p: "fc")
    assert requests == [] and "routes to restock" in skipped[0]["reason"]


# --- E8 ---------------------------------------------------------------------------------------------


def test_restock_draft_skips_products_being_cleared_and_says_why(pilot, inventory):
    pid = pilot.cases["high_demand_low_stock"]
    now = datetime.combine(pilot.as_of, time(0, 30), CAIRO)
    offer = make_offer(branch_id="1", product_ids=[str(pid)], start_date=now - timedelta(days=1),
                       end_date=now + timedelta(days=2)).model_copy(update={"status": OfferStatus.ACTIVE})
    notices = clearance_notices([offer], now)
    cleared = cleared_product_ids(notices, 1, pilot.as_of)
    assert cleared == [pid]

    evaluation = _evaluate_inventory({"as_of": pilot.as_of.isoformat(), "products": list(inventory.values()),
                                      "settings": pilot.settings, "active_offers": cleared})
    assert pid not in {c["product_id"] for c in evaluation["candidates"]}
    skipped = next(s for s in evaluation["skipped_products"] if s["product_id"] == pid)
    assert "active offer" in skipped["reason"]


def test_only_approved_or_live_offers_that_have_not_ended_clear_products():
    now = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
    window = dict(start_date=now - timedelta(days=1), end_date=now + timedelta(days=1))
    offers = [
        make_offer(product_ids=["1"], **window).model_copy(update={"status": OfferStatus.APPROVED}),
        make_offer(product_ids=["2"], **window).model_copy(update={"status": OfferStatus.PENDING_APPROVAL}),
        make_offer(product_ids=["3"], start_date=now - timedelta(days=3), end_date=now - timedelta(hours=1))
        .model_copy(update={"status": OfferStatus.ACTIVE}),
    ]
    notices = clearance_notices(offers, now)
    assert [n.product_ids for n in notices] == [(1,)]
    assert notices[0].status == "approved"


def test_clearance_needs_database_product_ids():
    now = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
    offer = make_offer(product_ids=["p-abc"], start_date=now, end_date=now + timedelta(days=1)) \
        .model_copy(update={"status": OfferStatus.ACTIVE})
    with pytest.raises(ValueError, match="database ids"):
        clearance_notices([offer], now)


# --- E9 ---------------------------------------------------------------------------------------------


def finished(pid, action):
    offer = make_offer(product_ids=[str(pid)]).model_copy(update={"status": OfferStatus.STOPPED})
    return MonitorResult(action, offer)


@pytest.mark.parametrize("action, triggers", [
    (MonitorAction.STOPPED_AT_CAP, True),
    (MonitorAction.ENDED, True),
    (MonitorAction.CONTINUE, False),
    (MonitorAction.STOP_UNCONFIRMED, False),  # not proven stopped: no re-evaluation yet
    (MonitorAction.DATA_UNAVAILABLE, False),
])
def test_only_a_confirmed_end_or_sell_out_triggers_re_evaluation(action, triggers):
    assert (reevaluation_for(finished(5, action)) is not None) == triggers


def test_re_evaluation_runs_for_the_affected_products_only(pilot, inventory):
    pid = pilot.cases["near_expiry_overstock"]
    request = reevaluation_for(finished(pid, MonitorAction.STOPPED_AT_CAP))
    sold_out = _consume_stock(inventory[pid], inventory[pid]["on_hand"] - 10)  # 10 fresh units left
    others = [row for p, row in inventory.items() if p != pid]
    result = reevaluate(request, [sold_out, *others], pilot.settings, pilot.as_of)
    assert result["evaluated_product_ids"] == [pid]
    assert [(c["product_id"], c["trigger_reason"]) for c in result["candidates"]] == [(pid, "HIGH_DEMAND_LOW_STOCK")]


def test_replayed_monitoring_result_queues_one_re_evaluation():
    queue = ReevaluationQueue()
    result = finished(5, MonitorAction.ENDED)
    assert queue.submit(reevaluation_for(result)) is True
    assert queue.submit(reevaluation_for(result)) is False  # the same result delivered again
    assert queue.submit(None) is False
    assert len(queue.requests) == 1
