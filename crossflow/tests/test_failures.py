"""E14 - cross-workflow failure cases (the rows of docs/FAILURE_MATRIX.md marked "crossflow")."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from offers.guardrails import InMemoryActiveOffers
from offers.intake import InMemoryOfferRepository
from offers.monitoring import CampaignStats, InMemoryStats, MonitorAction, monitor_campaign
from offers.pos_activation import ActivationOutcome
from offers.schema import OfferStatus
from offers.validation import RejectionLog
from offer_strategist.graph import StrategistDeps, build_strategist_graph, plan_offer
from restocking.workflow import _evaluate_inventory
from crossflow.contracts import parse_restock_signal_message
from crossflow.datagen import generate_pilot_branch
from crossflow.forecast import DemandForecaster, ForecastStore
from crossflow.handoffs import SignalEvidence, offer_requests_from_signals, reevaluation_for, restock_signal_message
from crossflow.mock_pos import Fault, MockPOS
from crossflow.ops import reconcile_pos, stuck_outbox
from crossflow.scenario import RuleBasedStrategistLLM
from crossflow.tests.support import START, activate, approved

CAIRO = ZoneInfo("Africa/Cairo")


def test_f01_duplicate_h1_delivery_plans_one_offer():
    """The same night's H1 message delivered twice: one candidate, the replay is skipped."""
    pilot = generate_pilot_branch(7)
    inventory = {row["product_id"]: row for row in pilot.restock_inventory()}
    now = datetime.combine(pilot.as_of, time(0, 30), CAIRO)
    evaluation = _evaluate_inventory({"as_of": pilot.as_of.isoformat(), "products": list(inventory.values()),
                                      "settings": pilot.settings, "active_offers": []})
    message = restock_signal_message({"run_id": f"restock:1:{pilot.as_of}", "branch_id": 1, **evaluation})
    forecasts = ForecastStore(DemandForecaster(pilot.sales), lambda: now)
    repo = InMemoryOfferRepository()
    first_signals, _ = parse_restock_signal_message(message, now)
    deps = StrategistDeps(llm=RuleBasedStrategistLLM(), catalog=pilot.catalog(), prices=pilot.prices(),
                          rules=pilot.product_rules(), active=InMemoryActiveOffers(),
                          evidence_reader=SignalEvidence(first_signals), forecast_reader=forecasts, repo=repo,
                          clock=lambda: now)
    graph = build_strategist_graph(deps)

    outcomes = []
    for _delivery in range(2):
        signals, _ = parse_restock_signal_message(message, now)
        requests, _ = offer_requests_from_signals(
            signals, inventory=inventory, settings=pilot.settings, as_of=pilot.as_of,
            forecast_ref_for=lambda b, p: forecasts.create_horizon(b, p, now).ref)
        outcomes += [plan_offer(graph, r)["outcome"] for r in requests]
    assert outcomes == ["submitted", "skipped"]
    assert len(repo.all()) == 1


def test_f02_activation_refused_by_the_pos_leaves_the_offer_approved_and_consistent():
    offer, store = approved()
    pos = MockPOS.scenario("failure")
    result = activate(offer, store, pos)
    assert result.outcome == ActivationOutcome.REJECTED_BY_POS
    assert result.offer.status == OfferStatus.APPROVED
    assert reconcile_pos(pos.live_offers(), [result.offer]) == []


def test_f03_unconfirmed_activation_is_found_by_pos_reconciliation():
    offer, store = approved()
    pos = MockPOS(activate_faults=[Fault.LOST_REPLY], status_faults=[Fault.SERVER_ERROR])
    result = activate(offer, store, pos)
    assert result.outcome == ActivationOutcome.UNCONFIRMED
    findings = reconcile_pos(pos.live_offers(), [result.offer])
    assert [f["problem"] for f in findings] == ["live on the POS but recorded APPROVED (unconfirmed activation)"]


def test_f04_unconfirmed_stop_keeps_the_offer_active_and_triggers_no_re_evaluation():
    offer, store = approved()
    pos = MockPOS(stop_faults=[Fault.REJECT, Fault.REJECT])
    live = activate(offer, store, pos).offer
    now = START + timedelta(days=2)
    stats = InMemoryStats({live.offer_id: CampaignStats(now, 120, 100, 0, 10)})
    result = monitor_campaign(live, stats=stats, pos=pos, log=RejectionLog(), now=now)
    assert result.action == MonitorAction.STOP_UNCONFIRMED
    assert result.offer.status == OfferStatus.ACTIVE
    assert reevaluation_for(result) is None


def test_f05_offer_recorded_stopped_but_still_live_is_urgent():
    offer, store = approved()
    pos = MockPOS()
    live = activate(offer, store, pos).offer
    findings = reconcile_pos(pos.live_offers(), [live.model_copy(update={"status": OfferStatus.STOPPED})])
    assert findings[0]["action"].startswith("URGENT")


def test_f06_unknown_offer_live_on_the_pos_is_urgent():
    offer, store = approved()
    pos = MockPOS()
    activate(offer, store, pos)
    findings = reconcile_pos(pos.live_offers(), [])
    assert findings[0]["problem"] == "live on the POS but unknown to Offers" and findings[0]["action"].startswith("URGENT")


def test_f07_stale_campaign_figures_never_stop_and_never_call_the_pos():
    offer, store = approved()
    pos = MockPOS()
    live = activate(offer, store, pos).offer
    now = START + timedelta(days=2)
    old = InMemoryStats({live.offer_id: CampaignStats(now - timedelta(hours=2), 120, 100, 0, 10)})
    result = monitor_campaign(live, stats=old, pos=pos, log=RejectionLog(), now=now)
    assert result.action == MonitorAction.DATA_UNAVAILABLE
    assert not [c for c in pos.calls if c[0] == "stop"]


def test_f08_stuck_outbox_messages_are_listed_for_the_operator():
    now = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    rows = [
        {"message_id": 1, "status": "FAILED", "created_at": now - timedelta(minutes=2)},
        {"message_id": 2, "status": "PENDING", "created_at": now - timedelta(hours=2)},
        {"message_id": 3, "status": "PENDING", "created_at": now - timedelta(minutes=5)},
        {"message_id": 4, "status": "DISPATCHED", "created_at": now - timedelta(days=1)},
    ]
    assert [m["message_id"] for m in stuck_outbox(rows, now)] == [1, 2]


def test_f09_live_terms_that_differ_from_the_approval_are_urgent():
    offer, store = approved()
    pos = MockPOS()
    live = activate(offer, store, pos).offer
    tampered = {k: dict(v, approval_hash="0" * 64) for k, v in pos.live_offers().items()}
    findings = reconcile_pos(tampered, [live])
    assert findings[0]["problem"] == "live terms do not match the approved version"
