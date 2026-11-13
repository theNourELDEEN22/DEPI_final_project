"""E3 - the mock POS, driven through M4's real activation (D10) and monitoring (D11) code."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offers.monitoring import CampaignStats, InMemoryStats, MonitorAction, monitor_campaign
from offers.pos_activation import ActivationOutcome, PosOutcome, idempotency_key_for, pos_payload
from offers.schema import OfferStatus, OfferType
from offers.validation import RejectionLog
from crossflow.mock_pos import Fault, MockPOS
from crossflow.tests.support import END, START, activate, approved


@pytest.mark.parametrize(
    "scenario, outcome, activate_calls, live",
    [
        ("success", ActivationOutcome.ACTIVATED, 1, 1),
        ("failure", ActivationOutcome.REJECTED_BY_POS, 1, 0),
        ("unclear", ActivationOutcome.ACTIVATED, 1, 1),       # status check confirms, no resend
        ("lost_request", ActivationOutcome.ACTIVATED, 2, 1),  # never arrived: resent once, same key
        ("garbage", ActivationOutcome.ACTIVATED, 1, 1),
    ],
)
def test_activation_scenarios(scenario, outcome, activate_calls, live):
    offer, store = approved()
    pos = MockPOS.scenario(scenario)
    result = activate(offer, store, pos)
    assert result.outcome == outcome
    assert sum(1 for op, _k, _f in pos.calls if op == "activate") == activate_calls
    assert len(pos.live_offers()) == live


def test_unclear_reply_triggers_a_status_check_first():
    offer, store = approved()
    pos = MockPOS.scenario("unclear")
    activate(offer, store, pos)
    assert [op for op, _k, _f in pos.calls] == ["activate", "status"]


def test_status_check_also_unclear_gives_unconfirmed():
    offer, store = approved()
    pos = MockPOS(activate_faults=[Fault.LOST_REPLY], status_faults=[Fault.SERVER_ERROR])
    result = activate(offer, store, pos)
    assert result.outcome == ActivationOutcome.UNCONFIRMED
    assert result.offer.status == OfferStatus.APPROVED  # never claims live without proof
    assert len(pos.live_offers()) == 1                   # ...although the POS did apply it


def test_replayed_key_never_creates_a_second_offer_and_reused_key_with_new_terms_is_refused():
    offer, _ = approved()
    pos = MockPOS()
    key, payload = idempotency_key_for(offer), pos_payload(offer)
    assert pos.activate(key, payload).outcome == PosOutcome.ACTIVE
    assert pos.activate(key, payload).outcome == PosOutcome.ACTIVE
    assert len(pos.live_offers()) == 1
    changed = {**payload, "quantity_cap": 999}
    assert pos.activate(key, changed).outcome == PosOutcome.REJECTED


def test_incomplete_payload_is_refused():
    response = MockPOS().activate("k", {"offer_id": "x"})
    assert response.outcome == PosOutcome.REJECTED and "missing" in response.detail


def live_offer(pos=None):
    offer, store = approved()
    pos = pos or MockPOS()
    return activate(offer, store, pos).offer, pos


def at_cap(offer, now):
    return InMemoryStats({offer.offer_id: CampaignStats(now, units_sold=120, redemptions=100, returns=0, stock_on_hand=10)})


@pytest.mark.parametrize("faults, action, stop_calls", [
    ([], MonitorAction.STOPPED_AT_CAP, 1),
    ([Fault.LOST_REPLY], MonitorAction.STOPPED_AT_CAP, 1),          # state check confirms
    ([Fault.LOST_REQUEST], MonitorAction.STOPPED_AT_CAP, 2),        # still active -> one retry
    ([Fault.REJECT, Fault.REJECT], MonitorAction.STOP_UNCONFIRMED, 2),
])
def test_stop_scenarios(faults, action, stop_calls):
    offer, pos = live_offer(MockPOS(stop_faults=faults))
    now = START + timedelta(days=2)
    result = monitor_campaign(offer, stats=at_cap(offer, now), pos=pos, log=RejectionLog(), now=now)
    assert result.action == action
    assert sum(1 for op, _k, _f in pos.calls if op == "stop") == stop_calls
    assert (len(pos.live_offers()) == 0) == (action == MonitorAction.STOPPED_AT_CAP)


def ring(pos, at, qty=1, product=101):
    return pos.ring_up(sale_line_id=1, transaction_id=10, branch_id=1, product_id=product, sku="S",
                       product_name="P", quantity=qty, unit_price=Decimal("100"), at=at)


def test_ring_up_applies_the_live_discount_only_inside_its_window():
    offer, pos = live_offer()
    inside = ring(pos, START + timedelta(hours=1), qty=2)
    assert inside["offer_id"] == offer.offer_id
    assert inside["discount_amount"] == Decimal("40.00") and inside["total_amount"] == Decimal("160.00")
    before = ring(pos, START - timedelta(hours=1))
    other_product = ring(pos, START + timedelta(hours=1), product=102)
    assert before["offer_id"] is None and other_product["offer_id"] is None
    assert pos.redemption_log(offer.offer_id) == {"units": 2, "receipts": 1, "discount": Decimal("40.00")}


def test_ring_up_stops_applying_after_the_stop():
    offer, pos = live_offer()
    now = START + timedelta(days=2)
    monitor_campaign(offer, stats=at_cap(offer, now), pos=pos, log=RejectionLog(), now=now)
    assert ring(pos, now + timedelta(hours=1))["offer_id"] is None


def test_buy2get1_gives_every_third_unit_free():
    offer, store = approved(type=OfferType.BUY2GET1, discount_percent=None, buy_qty=2, get_qty=1,
                            product_ids=["102"])
    pos = MockPOS()
    activate(offer, store, pos)
    line = pos.ring_up(sale_line_id=1, transaction_id=1, branch_id=1, product_id=102, sku="S", product_name="P",
                       quantity=7, unit_price=Decimal("40"), at=START + timedelta(hours=2))
    assert line["discount_amount"] == Decimal("80.00")  # 2 free units out of 7


def test_sales_lines_use_the_branch_local_date_and_time():
    offer, pos = live_offer()
    line = ring(pos, datetime(2026, 11, 2, 22, 30, tzinfo=timezone.utc))  # 00:30 next day in Cairo
    assert str(line["date"]) == "2026-11-03" and str(line["time"]) == "00:30:00"


def test_unknown_scenario_name():
    with pytest.raises(ValueError):
        MockPOS.scenario("chaos")
