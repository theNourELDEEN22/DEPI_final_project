"""D7 + D8 - Sales Team decision flow and change requests (Epic D: Offers).

D7: Approve, Reject or Request changes on an offer that is PENDING_APPROVAL.
    Every decision records who, when, why and which exact version was reviewed.
D8: A change request creates a NEW version (version + 1, back to draft) with a
    new forecast. It must go through the check (D6 builds the card, which re-runs
    validation and guardrails) and a new approval. The old approval hash never
    matches the new version.

The approval hash binds an approval to (offer_id, version, content hash). It is
what D9 and D10 verify before an offer may go live.

Storage goes through a small interface so M1's approval service (A12) and
audit log (A11) can replace the in-memory store without touching this logic.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Protocol

from offers.review import ReviewCard
from offers.schema import Offer, OfferStatus


class Decision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class DecisionError(ValueError):
    """The decision or change request is not allowed."""


@dataclass(frozen=True)
class DecisionRecord:
    offer_id: str
    version: int
    content_hash: str          # hash of the exact version that was reviewed
    decision: Decision
    actor: str
    decided_at: datetime
    reason: Optional[str]
    approval_hash: Optional[str] = None  # set for approvals only


class DecisionStore(Protocol):
    def save(self, record: DecisionRecord) -> None: ...
    def records_for(self, offer_id: str) -> list[DecisionRecord]: ...


@dataclass
class InMemoryDecisionStore:
    """Append-only stand-in for the approval service (A12) and audit log (A11)."""

    _records: list[DecisionRecord] = field(default_factory=list)

    def save(self, record: DecisionRecord) -> None:
        self._records.append(record)

    def records_for(self, offer_id: str) -> list[DecisionRecord]:
        return [r for r in self._records if r.offer_id == offer_id]

    def all(self) -> list[DecisionRecord]:
        return list(self._records)


def approval_hash_for(offer: Offer) -> str:
    """Binds an approval to this offer id, this version and this exact content."""
    raw = f"{offer.offer_id}|{offer.version}|{offer.content_hash()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def decide(
    offer: Offer,
    card: ReviewCard,
    decision: Decision | str,
    actor: str,
    reason: str | None,
    store: DecisionStore,
    now: datetime | None = None,
) -> tuple[Offer, DecisionRecord]:
    """Record a Sales Team decision. Returns the updated offer and the stored record."""
    now = now or datetime.now(timezone.utc)

    try:
        decision = Decision(decision)
    except ValueError as exc:
        raise DecisionError(f"unknown decision: {decision!r}") from exc

    actor = (actor or "").strip()
    reason = (reason or "").strip() or None
    if not actor:
        raise DecisionError("actor is required")
    if decision != Decision.APPROVE and reason is None:
        raise DecisionError(f"a reason is required to {decision.value.replace('_', ' ')}")

    if offer.status != OfferStatus.PENDING_APPROVAL:
        raise DecisionError(f"only a pending offer can be decided, offer is {offer.status.value}")

    # The decision must be about exactly what the reviewer saw.
    if (
        card.offer_id != offer.offer_id
        or card.version != offer.version
        or card.content_hash != offer.content_hash()
    ):
        raise DecisionError("the review card does not match this offer version")

    if any(r.version == offer.version for r in store.records_for(offer.offer_id)):
        raise DecisionError(f"version {offer.version} already has a decision")

    update: dict = {}
    approval_hash = None
    if decision == Decision.APPROVE:
        approval_hash = approval_hash_for(offer)
        update = {"status": OfferStatus.APPROVED, "approval_hash": approval_hash}
    elif decision == Decision.REJECT:
        update = {"status": OfferStatus.REJECTED}
    else:
        update = {"status": OfferStatus.CHANGES_REQUESTED}

    record = DecisionRecord(
        offer_id=offer.offer_id,
        version=offer.version,
        content_hash=offer.content_hash(),
        decision=decision,
        actor=actor,
        decided_at=now,
        reason=reason,
        approval_hash=approval_hash,
    )
    store.save(record)
    return offer.model_copy(update=update), record


def is_approval_valid(offer: Offer, store: DecisionStore) -> bool:
    """True only if this exact version was approved and the stored hash still matches.
    D9 and D10 call this before activation."""
    if offer.status not in (OfferStatus.APPROVED, OfferStatus.ACTIVE):
        return False
    if not offer.approval_hash:
        return False
    expected = approval_hash_for(offer)
    if offer.approval_hash != expected:
        return False
    return any(
        r.decision == Decision.APPROVE
        and r.version == offer.version
        and r.approval_hash == expected
        for r in store.records_for(offer.offer_id)
    )


# Fields a change request may never touch.
_PROTECTED = {"offer_id", "version", "status", "approval_hash", "created_at"}


def revise_offer(
    offer: Offer,
    changes: dict,
    new_forecast_ref: str,
    store: DecisionStore,
) -> Offer:
    """D8: turn a change request into a new DRAFT version with a new forecast.

    The new version has no approval. It has to pass the check again (build the
    review card with D6) and be approved again (decide)."""
    if offer.status != OfferStatus.CHANGES_REQUESTED:
        raise DecisionError(f"only an offer with requested changes can be revised, offer is {offer.status.value}")
    if not any(
        r.version == offer.version and r.decision == Decision.REQUEST_CHANGES
        for r in store.records_for(offer.offer_id)
    ):
        raise DecisionError("no change request is recorded for this version")
    if not changes:
        raise DecisionError("a change request needs at least one change")
    forbidden = sorted(_PROTECTED & set(changes))
    if forbidden:
        raise DecisionError(f"these fields cannot be changed: {', '.join(forbidden)}")
    if "forecast_ref" in changes:
        raise DecisionError("pass the new forecast with new_forecast_ref, not in changes")
    if not new_forecast_ref or new_forecast_ref == offer.forecast_ref:
        raise DecisionError("a new forecast reference is required for the new version")

    try:
        return offer.new_version(**changes, forecast_ref=new_forecast_ref)
    except ValueError as exc:  # invalid values in the requested changes
        raise DecisionError(str(exc)) from exc