"""D11 - Campaign monitoring: sales, redemptions, returns, stock; stop at cap or end
(Epic D: Offers).

For an ACTIVE offer, each monitoring run:
  1. reads the stored campaign figures (sales, redemptions, returns, stock),
  2. stops the campaign when its end date is reached, or when redemptions reach
     the approved quantity cap,
  3. returns a snapshot that D12 (impact) and D13 (manager report) can reuse.

Rules:
- The approved limit is exact: redemptions >= quantity_cap stops the campaign.
  Returns do NOT free up the cap (the exposure was already taken).
- The end date stops a campaign even when no figures are available.
- Missing or stale figures never stop a campaign by themselves; the run returns
  DATA_UNAVAILABLE so a person can look. (Policy to confirm with the team.)
- Stopping goes through the POS with a stable key. If the POS reply is unclear
  we ask the POS for the state first and never claim "stopped" without proof.
  Unconfirmed stops are returned and logged for urgent manual follow-up.

The real stats come from M3's reporting data (H4/H5) and the real POS from M5's
mock (E3); both plug in through the small interfaces below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Protocol

from offers.pos_activation import idempotency_key_for
from offers.schema import Offer, OfferStatus

TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class MonitorConfig:
    # Placeholder until the Week 1 decisions meeting (E1).
    max_data_age_minutes: int = 30


@dataclass(frozen=True)
class CampaignStats:
    as_of: datetime
    units_sold: int        # all units sold of the offer's products during the campaign
    redemptions: int       # units sold under the offer
    returns: int
    stock_on_hand: int


class StatsReader(Protocol):
    def get_stats(self, offer_id: str, version: int, branch_id: str) -> Optional[CampaignStats]: ...


@dataclass
class InMemoryStats:
    data: dict[str, CampaignStats] = field(default_factory=dict)

    def get_stats(self, offer_id: str, version: int, branch_id: str) -> Optional[CampaignStats]:
        return self.data.get(offer_id)


class PosStopOutcome(str, Enum):
    STOPPED = "stopped"            # the POS confirms the offer is no longer applied
    STILL_ACTIVE = "still_active"  # the POS says the offer is still running
    UNCLEAR = "unclear"            # timeout, error, unreadable reply


@dataclass(frozen=True)
class PosStopResponse:
    outcome: PosStopOutcome
    detail: Optional[str] = None


class PosStopClient(Protocol):
    def deactivate(self, idempotency_key: str) -> PosStopResponse: ...
    def get_state(self, idempotency_key: str) -> PosStopResponse: ...


class MonitorAction(str, Enum):
    CONTINUE = "continue"
    STOPPED_AT_CAP = "stopped_at_cap"
    ENDED = "ended"
    DATA_UNAVAILABLE = "data_unavailable"
    STOP_UNCONFIRMED = "stop_unconfirmed"
    NOT_MONITORED = "not_monitored"


@dataclass(frozen=True)
class CampaignSnapshot:
    offer_id: str
    version: int
    as_of: datetime
    units_sold: int
    redemptions: int
    returns: int
    stock_on_hand: int
    quantity_cap: int
    cap_used_percent: Decimal
    overshoot: int  # redemptions above the cap (the POS can lag before the stop lands)


@dataclass(frozen=True)
class MonitorResult:
    action: MonitorAction
    offer: Offer
    snapshot: Optional[CampaignSnapshot] = None
    reasons: tuple[str, ...] = ()


def stop_key_for(offer: Offer) -> str:
    return idempotency_key_for(offer) + ":stop"


def _safe(call, *args) -> PosStopResponse:
    try:
        response = call(*args)
    except Exception as exc:  # timeouts, connection errors, adapter errors
        return PosStopResponse(PosStopOutcome.UNCLEAR, detail=f"{type(exc).__name__}: {exc}")
    if not isinstance(response, PosStopResponse):
        return PosStopResponse(PosStopOutcome.UNCLEAR, detail="unreadable POS response")
    return response


def _stop_on_pos(pos: PosStopClient, key: str) -> bool:
    """True only when the POS confirms the offer is stopped."""
    first = _safe(pos.deactivate, key)
    if first.outcome == PosStopOutcome.STOPPED:
        return True
    # Anything else: ask the POS what it has BEFORE doing anything else.
    state = _safe(pos.get_state, key)
    if state.outcome == PosStopOutcome.STOPPED:
        return True
    if state.outcome == PosStopOutcome.STILL_ACTIVE:
        return _safe(pos.deactivate, key).outcome == PosStopOutcome.STOPPED  # one retry, same key
    return False


def _snapshot(offer: Offer, stats: CampaignStats) -> CampaignSnapshot:
    cap = offer.quantity_cap
    return CampaignSnapshot(
        offer_id=offer.offer_id,
        version=offer.version,
        as_of=stats.as_of,
        units_sold=stats.units_sold,
        redemptions=stats.redemptions,
        returns=stats.returns,
        stock_on_hand=stats.stock_on_hand,
        quantity_cap=cap,
        cap_used_percent=(Decimal(stats.redemptions) / Decimal(cap) * Decimal(100)).quantize(TWO_PLACES),
        overshoot=max(0, stats.redemptions - cap),
    )


def monitor_campaign(
    offer: Offer,
    *,
    stats: StatsReader,
    pos: PosStopClient,
    log,  # anything with an .entries list (RejectionLog now, audit_log A11 later)
    config: MonitorConfig = MonitorConfig(),
    now: datetime | None = None,
) -> MonitorResult:
    now = now or datetime.now(timezone.utc)

    def record(action: MonitorAction, reasons=()) -> None:
        log.entries.append(
            {
                "offer_id": offer.offer_id,
                "version": offer.version,
                "stage": "monitoring",
                "action": action.value,
                "reasons": list(reasons),
                "at": now,
            }
        )

    if offer.status != OfferStatus.ACTIVE:
        return MonitorResult(MonitorAction.NOT_MONITORED, offer)

    current = stats.get_stats(offer.offer_id, offer.version, offer.branch_id)
    stale = current is not None and now - current.as_of > timedelta(minutes=config.max_data_age_minutes)
    usable = current if (current is not None and not stale) else None
    snapshot = _snapshot(offer, usable) if usable else None

    # Why to stop: the end date needs no figures, the cap needs fresh ones.
    stop_status, stop_action, stop_reason = None, None, None
    if now >= offer.end_date:
        stop_status, stop_action, stop_reason = OfferStatus.ENDED, MonitorAction.ENDED, "end date reached"
    elif usable is not None and usable.redemptions >= offer.quantity_cap:
        stop_status = OfferStatus.STOPPED
        stop_action = MonitorAction.STOPPED_AT_CAP
        stop_reason = f"redemptions {usable.redemptions} reached the approved cap {offer.quantity_cap}"

    if stop_action is None:
        if usable is None:
            reason = "no campaign figures found" if current is None else "campaign figures are stale"
            record(MonitorAction.DATA_UNAVAILABLE, [reason])
            return MonitorResult(MonitorAction.DATA_UNAVAILABLE, offer, None, (reason,))
        record(MonitorAction.CONTINUE)
        return MonitorResult(MonitorAction.CONTINUE, offer, snapshot)

    if _stop_on_pos(pos, stop_key_for(offer)):
        record(stop_action, [stop_reason])
        return MonitorResult(
            stop_action,
            offer.model_copy(update={"status": stop_status}),
            snapshot,
            (stop_reason,),
        )

    reasons = (stop_reason, "POS did not confirm the stop, check the POS manually now")
    record(MonitorAction.STOP_UNCONFIRMED, reasons)
    return MonitorResult(MonitorAction.STOP_UNCONFIRMED, offer, snapshot, reasons)
