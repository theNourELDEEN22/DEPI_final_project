"""D14 - Failure cases: unclear POS responses and stale offer data (Epic D: Offers).

These are scenario tests across the whole chain (review, decision, recheck,
POS activation, monitoring, impact, manager report) against a POS that behaves
like a real one: it applies requests by idempotency key and can lose replies,
send garbage or never receive a request.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offers.decisions import Decision, DecisionError, InMemoryDecisionStore, decide
from offers.guardrails import (
    GuardrailConfig,
    InMemoryActiveOffers,
    InMemoryPrices,
    InMemoryProductRules,
    PriceInfo,
)
from offers.impact import ImpactUnavailable, Metrics, calculate_impact
from offers.manager_report import InMemoryOutbox, build_manager_report, queue_manager_report
from offers.monitoring import (
    CampaignStats,
    InMemoryStats,
    MonitorAction,
    PosStopOutcome,
    PosStopResponse,
    monitor_campaign,
)
from offers.pos_activation import ActivationOutcome, PosOutcome, PosResponse, activate_on_pos
from offers.review import (
    Evidence,
    Forecast,
    InMemoryEvidence,
    InMemoryForecasts,
    build_review_card,
)
from offers.schema import Offer, OfferStatus, OfferType
from offers.validation import InMemoryCatalog, RejectionLog, ValidationConfig

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
START = NOW + timedelta(days=1)
END = NOW + timedelta(days=8)
ACTIVATION_TIME = NOW + timedelta(hours=20)  # before the offer starts
MID = START + timedelta(days=3)

CATALOG = InMemoryCatalog({"b1": {"p1"}})
PRICES = InMemoryPrices({("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("60"))})
EVIDENCE = InMemoryEvidence({"sig-1": Evidence("sig-1", "stock_signal", {"stock_on_hand": 200})})
FORECASTS = InMemoryForecasts(
    {"fc-1": Forecast("fc-1", Decimal("80"), NOW), "fc-2": Forecast("fc-2", Decimal("90"), NOW)}
)


class StatefulPos:
    """A POS that applies requests by idempotency key. A fault hits the FIRST request only."""

    def __init__(self, activate_fault=None, stop_fault=None):
        self.live: dict[str, dict] = {}
        self.stopped: set[str] = set()
        self.activate_requests = 0
        self.stop_requests = 0
        self.activate_fault = activate_fault  # "lose_reply" | "garbage" | "drop_request"
        self.stop_fault = stop_fault          # "lose_reply" | "drop_request"

    def activate(self, key, payload):
        self.activate_requests += 1
        first = self.activate_requests == 1
        if first and self.activate_fault == "drop_request":
            raise TimeoutError("request never arrived")
        self.live.setdefault(key, payload)  # idempotent: the same key is applied once
        if first and self.activate_fault == "lose_reply":
            raise TimeoutError("reply lost")
        if first and self.activate_fault == "garbage":
            return "<html>502 Bad Gateway</html>"
        return PosResponse(PosOutcome.ACTIVE, pos_ref="pos-1")

    def get_status(self, key):
        return PosResponse(PosOutcome.ACTIVE if key in self.live else PosOutcome.NOT_FOUND)

    def deactivate(self, key):
        self.stop_requests += 1
        first = self.stop_requests == 1
        if first and self.stop_fault == "drop_request":
            raise TimeoutError("request never arrived")
        self.stopped.add(key)
        if first and self.stop_fault == "lose_reply":
            raise TimeoutError("reply lost")
        return PosStopResponse(PosStopOutcome.STOPPED)

    def get_state(self, key):
        return PosStopResponse(PosStopOutcome.STOPPED if key in self.stopped else PosStopOutcome.STILL_ACTIVE)


class Kpi:
    def __init__(self, available=True):
        self.available = available

    def units(self, *a):
        return 150 if self.available else None

    def net_revenue(self, *a):
        return Decimal("12000") if self.available else None

    def gross_profit(self, *a):
        return Decimal("3000") if self.available else None


class Baseline:
    def get_baseline(self, *a):
        return Metrics(100, Decimal("10000"), Decimal("2800"))


def make_offer(**over) -> Offer:
    base = dict(
        branch_id="b1",
        type=OfferType.DISCOUNT,
        product_ids=["p1"],
        start_date=START,
        end_date=END,
        discount_percent=Decimal("20"),
        quantity_cap=100,
        evidence_ref="sig-1",
        forecast_ref="fc-1",
    )
    base.update(over)
    return Offer(**base)


def review(offer):
    return build_review_card(
        offer,
        catalog=CATALOG,
        prices=PRICES,
        rules=InMemoryProductRules(),
        active=InMemoryActiveOffers(),
        evidence_reader=EVIDENCE,
        forecast_reader=FORECASTS,
        validation_config=ValidationConfig(),
        guardrail_config=GuardrailConfig(),
        now=NOW,
    )


def approved():
    card, pending = review(make_offer())
    store = InMemoryDecisionStore()
    offer, _ = decide(pending, card, Decision.APPROVE, "sara", None, store, NOW)
    return offer, store


def activate(offer, store, pos, *, prices=PRICES, now=ACTIVATION_TIME, log=None):
    return activate_on_pos(
        offer,
        pos=pos,
        store=store,
        catalog=CATALOG,
        prices=prices,
        rules=InMemoryProductRules(),
        active=InMemoryActiveOffers(),
        validation_config=ValidationConfig(),
        guardrail_config=GuardrailConfig(),
        log=log if log is not None else RejectionLog(),
        now=now,
    )


def campaign_stats(offer, redemptions=100, as_of=MID):
    return InMemoryStats(
        {offer.offer_id: CampaignStats(as_of=as_of, units_sold=150, redemptions=redemptions, returns=2, stock_on_hand=60)}
    )


# --- unclear POS responses on activation ------------------------------------------------


def test_lost_reply_does_not_create_a_second_offer():
    offer, store = approved()
    pos = StatefulPos(activate_fault="lose_reply")
    result = activate(offer, store, pos)
    assert result.outcome == ActivationOutcome.ACTIVATED
    assert len(pos.live) == 1
    assert pos.activate_requests == 1  # the status check found it live, nothing was resent


def test_garbage_reply_does_not_create_a_second_offer():
    offer, store = approved()
    pos = StatefulPos(activate_fault="garbage")
    result = activate(offer, store, pos)
    assert result.outcome == ActivationOutcome.ACTIVATED
    assert len(pos.live) == 1 and pos.activate_requests == 1


def test_request_that_never_arrived_is_sent_again_exactly_once():
    offer, store = approved()
    pos = StatefulPos(activate_fault="drop_request")
    result = activate(offer, store, pos)
    assert result.outcome == ActivationOutcome.ACTIVATED
    assert pos.activate_requests == 2
    assert len(pos.live) == 1


def test_duplicate_scheduler_run_still_gives_one_live_offer():
    offer, store = approved()
    pos = StatefulPos()
    activate(offer, store, pos)
    again = activate(offer, store, pos)  # a second run with the same approved offer
    assert again.outcome == ActivationOutcome.ACTIVATED
    assert len(pos.live) == 1


# --- stale offer data blocks activation ------------------------------------------------------


def test_start_date_passed_while_waiting_blocks_activation():
    offer, store = approved()
    pos = StatefulPos()
    result = activate(offer, store, pos, now=START + timedelta(days=1))
    assert result.outcome == ActivationOutcome.BLOCKED
    assert any("past" in r for r in result.reasons)
    assert pos.activate_requests == 0 and pos.live == {}


def test_prices_moved_since_approval_block_activation():
    offer, store = approved()
    pos = StatefulPos()
    costlier = InMemoryPrices({("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("78"))})
    result = activate(offer, store, pos, prices=costlier)
    assert result.outcome == ActivationOutcome.BLOCKED
    assert any("margin" in r for r in result.reasons)
    assert pos.activate_requests == 0


def test_decision_on_a_card_of_an_older_version_is_refused():
    card_v1, pending_v1 = review(make_offer())
    v2 = pending_v1.new_version(discount_percent=Decimal("15"), forecast_ref="fc-2")
    _card_v2, pending_v2 = review(v2)
    with pytest.raises(DecisionError):
        decide(pending_v2, card_v1, Decision.APPROVE, "sara", None, InMemoryDecisionStore(), NOW)


# --- stale campaign data and unclear stop replies ---------------------------------------------------


def monitor(offer, stats, pos, now=MID):
    return monitor_campaign(offer, stats=stats, pos=pos, log=RejectionLog(), now=now)


def live_offer():
    offer, store = approved()
    pos = StatefulPos()
    result = activate(offer, store, pos)
    return result.offer, pos


def test_stale_campaign_figures_never_trigger_a_stop():
    offer, pos = live_offer()
    old = campaign_stats(offer, redemptions=100, as_of=MID - timedelta(hours=3))
    result = monitor(offer, old, pos)
    assert result.action == MonitorAction.DATA_UNAVAILABLE
    assert pos.stop_requests == 0


def test_lost_reply_on_stop_is_confirmed_by_the_state_check():
    offer, pos = live_offer()
    pos.stop_fault = "lose_reply"
    result = monitor(offer, campaign_stats(offer), pos)
    assert result.action == MonitorAction.STOPPED_AT_CAP
    assert result.offer.status == OfferStatus.STOPPED
    assert pos.stop_requests == 1


def test_stop_request_that_never_arrived_is_sent_again_once():
    offer, pos = live_offer()
    pos.stop_fault = "drop_request"
    result = monitor(offer, campaign_stats(offer), pos)
    assert result.action == MonitorAction.STOPPED_AT_CAP
    assert pos.stop_requests == 2
    assert len(pos.stopped) == 1


# --- stale or missing KPI data stops the report --------------------------------------------------------


def test_missing_kpi_data_gives_no_impact_and_no_report():
    offer, pos = live_offer()
    stopped = monitor(offer, campaign_stats(offer), pos)
    outbox = InMemoryOutbox()
    stats = CampaignStats(as_of=MID, units_sold=150, redemptions=100, returns=2, stock_on_hand=60)
    with pytest.raises(ImpactUnavailable):
        calculate_impact(
            stopped.offer, stats=stats, kpi=Kpi(available=False), baseline=Baseline(),
            prices=PRICES, window_end=MID,
        )
    assert outbox.messages == []


# --- the whole lifecycle with a flaky POS ------------------------------------------------------------------


def test_full_lifecycle_with_lost_replies_ends_in_one_report():
    # draft -> review -> approve
    offer, store = approved()
    pos = StatefulPos(activate_fault="lose_reply", stop_fault="lose_reply")

    # activate (reply lost, status check confirms)
    activated = activate(offer, store, pos)
    assert activated.outcome == ActivationOutcome.ACTIVATED
    live = activated.offer

    # the campaign sells out its cap; the stop reply is lost too
    stopped = monitor(live, campaign_stats(live, redemptions=100), pos)
    assert stopped.action == MonitorAction.STOPPED_AT_CAP
    assert stopped.snapshot.redemptions == 100

    # impact and the manager report
    stats = CampaignStats(as_of=MID, units_sold=150, redemptions=100, returns=2, stock_on_hand=60)
    impact = calculate_impact(
        stopped.offer, stats=stats, kpi=Kpi(), baseline=Baseline(), prices=PRICES, window_end=MID
    )
    report = build_manager_report(stopped.offer, impact, prices=PRICES, stock_at_start=200)
    outbox = InMemoryOutbox()
    assert queue_manager_report(report, outbox) is True
    assert queue_manager_report(report, outbox) is False  # a re-run adds nothing
    assert len(outbox.messages) == 1

    # one live offer and one stop on the POS, the report labels its estimate
    assert len(pos.live) == 1 and len(pos.stopped) == 1
    assert "ESTIMATE" in outbox.messages[0].body
