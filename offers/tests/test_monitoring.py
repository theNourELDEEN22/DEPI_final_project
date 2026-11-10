from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offers.monitoring import (
    CampaignStats,
    InMemoryStats,
    MonitorAction,
    MonitorConfig,
    PosStopOutcome,
    PosStopResponse,
    monitor_campaign,
    stop_key_for,
)
from offers.pos_activation import idempotency_key_for
from offers.schema import Offer, OfferStatus, OfferType
from offers.validation import RejectionLog

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
START = NOW
END = NOW + timedelta(days=7)
MID = NOW + timedelta(days=3)

STOPPED = PosStopResponse(PosStopOutcome.STOPPED)
STILL_ACTIVE = PosStopResponse(PosStopOutcome.STILL_ACTIVE)
UNCLEAR = PosStopResponse(PosStopOutcome.UNCLEAR, detail="timeout")


class FakePos:
    """Scripted mock POS. An item that is an Exception is raised instead of returned."""

    def __init__(self, deactivate=(), state=()):
        self._deactivate = list(deactivate)
        self._state = list(state)
        self.calls: list[tuple[str, str]] = []

    def _next(self, script):
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def deactivate(self, key):
        self.calls.append(("deactivate", key))
        return self._next(self._deactivate)

    def get_state(self, key):
        self.calls.append(("state", key))
        return self._next(self._state)


def active_offer(**over) -> Offer:
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
        status=OfferStatus.ACTIVE,
        approval_hash="a" * 64,
    )
    base.update(over)
    return Offer(**base)


def stats_for(offer, *, redemptions=40, returns=2, sold=55, stock=60, as_of=MID):
    return InMemoryStats(
        {offer.offer_id: CampaignStats(as_of=as_of, units_sold=sold, redemptions=redemptions, returns=returns, stock_on_hand=stock)}
    )


def run(offer, stats, pos, *, now=MID, log=None, config=MonitorConfig()):
    return monitor_campaign(
        offer, stats=stats, pos=pos, log=log if log is not None else RejectionLog(), config=config, now=now
    )


# --- keep running -----------------------------------------------------------


def test_campaign_below_cap_and_before_end_continues():
    offer = active_offer()
    pos = FakePos()
    result = run(offer, stats_for(offer, redemptions=40), pos)
    assert result.action == MonitorAction.CONTINUE
    assert result.offer.status == OfferStatus.ACTIVE
    assert pos.calls == []


def test_snapshot_carries_sales_redemptions_returns_and_stock():
    offer = active_offer()
    snap = run(offer, stats_for(offer, redemptions=40, returns=2, sold=55, stock=60), FakePos()).snapshot
    assert (snap.units_sold, snap.redemptions, snap.returns, snap.stock_on_hand) == (55, 40, 2, 60)
    assert snap.quantity_cap == 100
    assert snap.cap_used_percent == Decimal("40.00")
    assert snap.overshoot == 0
    assert snap.offer_id == offer.offer_id and snap.version == offer.version


# --- stop at the approved cap -------------------------------------------------


def test_campaign_stops_exactly_at_the_cap():
    offer = active_offer()
    pos = FakePos(deactivate=[STOPPED])
    result = run(offer, stats_for(offer, redemptions=100), pos)
    assert result.action == MonitorAction.STOPPED_AT_CAP
    assert result.offer.status == OfferStatus.STOPPED
    assert pos.calls == [("deactivate", stop_key_for(offer))]
    assert "cap" in result.reasons[0]


def test_one_below_the_cap_does_not_stop():
    offer = active_offer()
    pos = FakePos()
    assert run(offer, stats_for(offer, redemptions=99), pos).action == MonitorAction.CONTINUE
    assert pos.calls == []


def test_overshoot_is_recorded_when_the_pos_lagged():
    offer = active_offer()
    result = run(offer, stats_for(offer, redemptions=108), FakePos(deactivate=[STOPPED]))
    assert result.action == MonitorAction.STOPPED_AT_CAP
    assert result.snapshot.overshoot == 8
    assert result.snapshot.cap_used_percent == Decimal("108.00")


def test_returns_do_not_free_up_the_cap():
    offer = active_offer()
    result = run(offer, stats_for(offer, redemptions=100, returns=30), FakePos(deactivate=[STOPPED]))
    assert result.action == MonitorAction.STOPPED_AT_CAP


# --- stop at the end date -----------------------------------------------------


def test_campaign_ends_at_its_end_date():
    offer = active_offer()
    pos = FakePos(deactivate=[STOPPED])
    result = run(offer, stats_for(offer, redemptions=10, as_of=END), pos, now=END)
    assert result.action == MonitorAction.ENDED
    assert result.offer.status == OfferStatus.ENDED


def test_one_second_before_the_end_it_continues():
    offer = active_offer()
    pos = FakePos()
    now = END - timedelta(seconds=1)
    result = run(offer, stats_for(offer, as_of=now), pos, now=now)
    assert result.action == MonitorAction.CONTINUE
    assert pos.calls == []


def test_end_date_stops_the_campaign_even_without_figures():
    offer = active_offer()
    result = run(offer, InMemoryStats(), FakePos(deactivate=[STOPPED]), now=END + timedelta(hours=1))
    assert result.action == MonitorAction.ENDED
    assert result.snapshot is None


# --- missing or stale figures -----------------------------------------------------


def test_missing_figures_do_not_stop_the_campaign():
    offer = active_offer()
    pos = FakePos()
    result = run(offer, InMemoryStats(), pos)
    assert result.action == MonitorAction.DATA_UNAVAILABLE
    assert result.offer.status == OfferStatus.ACTIVE
    assert pos.calls == []


def test_stale_figures_are_not_trusted_even_if_they_show_the_cap_reached():
    offer = active_offer()
    old = MID - timedelta(minutes=45)
    pos = FakePos()
    result = run(offer, stats_for(offer, redemptions=100, as_of=old), pos)
    assert result.action == MonitorAction.DATA_UNAVAILABLE
    assert "stale" in result.reasons[0]
    assert pos.calls == []


def test_figures_exactly_at_the_age_limit_are_still_used():
    offer = active_offer()
    edge = MID - timedelta(minutes=30)
    result = run(offer, stats_for(offer, as_of=edge), FakePos())
    assert result.action == MonitorAction.CONTINUE


# --- stopping through an unclear POS ------------------------------------------------


def test_unclear_stop_checks_the_pos_state_first():
    offer = active_offer()
    pos = FakePos(deactivate=[UNCLEAR], state=[STOPPED])
    result = run(offer, stats_for(offer, redemptions=100), pos)
    assert [c[0] for c in pos.calls] == ["deactivate", "state"]
    assert result.action == MonitorAction.STOPPED_AT_CAP
    assert result.offer.status == OfferStatus.STOPPED


def test_timeout_on_stop_counts_as_unclear():
    offer = active_offer()
    pos = FakePos(deactivate=[TimeoutError("no answer")], state=[STOPPED])
    result = run(offer, stats_for(offer, redemptions=100), pos)
    assert [c[0] for c in pos.calls] == ["deactivate", "state"]
    assert result.action == MonitorAction.STOPPED_AT_CAP


def test_pos_still_running_gets_one_retry_with_the_same_key():
    offer = active_offer()
    pos = FakePos(deactivate=[UNCLEAR, STOPPED], state=[STILL_ACTIVE])
    result = run(offer, stats_for(offer, redemptions=100), pos)
    assert [c[0] for c in pos.calls] == ["deactivate", "state", "deactivate"]
    assert len({c[1] for c in pos.calls}) == 1
    assert result.action == MonitorAction.STOPPED_AT_CAP


def test_retry_that_does_not_stop_it_is_unconfirmed():
    offer = active_offer()
    pos = FakePos(deactivate=[UNCLEAR, UNCLEAR], state=[STILL_ACTIVE])
    result = run(offer, stats_for(offer, redemptions=100), pos)
    assert result.action == MonitorAction.STOP_UNCONFIRMED
    assert result.offer.status == OfferStatus.ACTIVE  # never claim stopped without proof
    assert [c[0] for c in pos.calls] == ["deactivate", "state", "deactivate"]  # no third try


def test_unclear_state_is_never_followed_by_a_blind_second_stop():
    offer = active_offer()
    pos = FakePos(deactivate=[UNCLEAR], state=[UNCLEAR])
    result = run(offer, stats_for(offer, redemptions=100), pos)
    assert result.action == MonitorAction.STOP_UNCONFIRMED
    assert result.offer.status == OfferStatus.ACTIVE
    assert [c[0] for c in pos.calls] == ["deactivate", "state"]


def test_state_check_that_raises_is_unconfirmed():
    offer = active_offer()
    pos = FakePos(deactivate=[UNCLEAR], state=[ConnectionError("down")])
    assert run(offer, stats_for(offer, redemptions=100), pos).action == MonitorAction.STOP_UNCONFIRMED


def test_unconfirmed_end_is_reported_too():
    offer = active_offer()
    pos = FakePos(deactivate=[UNCLEAR], state=[UNCLEAR])
    result = run(offer, InMemoryStats(), pos, now=END)
    assert result.action == MonitorAction.STOP_UNCONFIRMED
    assert result.offer.status == OfferStatus.ACTIVE


# --- keys, state and audit -----------------------------------------------------------


def test_stop_key_is_stable_and_different_from_the_activation_key():
    offer = active_offer()
    assert stop_key_for(offer) == stop_key_for(offer)
    assert stop_key_for(offer) != idempotency_key_for(offer)
    assert stop_key_for(offer).startswith(idempotency_key_for(offer))


@pytest.mark.parametrize(
    "status", [OfferStatus.APPROVED, OfferStatus.STOPPED, OfferStatus.ENDED, OfferStatus.DRAFT]
)
def test_only_active_offers_are_monitored(status):
    offer = active_offer(status=status)
    pos = FakePos()
    result = run(offer, stats_for(offer, redemptions=500), pos, now=END + timedelta(days=1))
    assert result.action == MonitorAction.NOT_MONITORED
    assert pos.calls == []


def test_a_stopped_campaign_is_not_stopped_twice():
    offer = active_offer()
    pos = FakePos(deactivate=[STOPPED])
    first = run(offer, stats_for(offer, redemptions=100), pos)
    second = run(first.offer, stats_for(offer, redemptions=100), pos)
    assert second.action == MonitorAction.NOT_MONITORED
    assert len(pos.calls) == 1


def test_every_run_is_logged():
    offer = active_offer()
    log = RejectionLog()
    run(offer, stats_for(offer, redemptions=40), FakePos(), log=log)
    run(offer, stats_for(offer, redemptions=100), FakePos(deactivate=[STOPPED]), log=log)
    entries = [e for e in log.entries if e["stage"] == "monitoring"]
    assert [e["action"] for e in entries] == ["continue", "stopped_at_cap"]
    assert entries[1]["offer_id"] == offer.offer_id
    assert entries[1]["reasons"]
