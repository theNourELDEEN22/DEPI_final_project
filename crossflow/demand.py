"""E12 - XW-6: sell-through updates demand levels, only through reviewed data (Epic E).

1. propose_demand_levels() compares each product's sell-through over a period
   with fixed rules and proposes a new demand_level (H6) where it differs.
   Promo sales are left out: an offer's spike is not normal demand.
2. A person reviews each proposal (approve / reject, with identity and reason).
   AI or agent identities cannot review. AI-sourced proposals are allowed, but go
   through the same human review.
3. apply_approved() changes products.demand_level only for APPROVED proposals,
   and refuses a proposal whose product changed since it was made (stale), just
   like an offer approval is bound to the exact version reviewed.

There is no other way to change a demand level here: no direct AI changes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable, Literal, Optional

from crossflow.contracts import DemandLevel, DemandLevelProposal

TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class DemandRules:
    # Placeholders until the Week 1 decisions meeting (E1, decision D-07).
    high_min_daily_units: Decimal = Decimal("8")
    medium_min_daily_units: Decimal = Decimal("3")
    min_period_days: int = 14

    def classify(self, avg_daily: Decimal) -> DemandLevel:
        if avg_daily >= self.high_min_daily_units:
            return DemandLevel.HIGH
        if avg_daily >= self.medium_min_daily_units:
            return DemandLevel.MEDIUM
        return DemandLevel.LOW


def propose_demand_levels(
    sales_lines: Iterable[dict[str, Any]],
    products: Iterable[dict[str, Any]],
    *,
    branch_id: int,
    period_start: date,
    period_end: date,
    now: datetime,
    rules: DemandRules = DemandRules(),
    source: Literal["rules", "ai"] = "rules",
) -> list[DemandLevelProposal]:
    """Proposals for products whose sell-through says a different level. [start, end) by local date."""
    days = (period_end - period_start).days
    if days < rules.min_period_days:
        raise ValueError(f"a sell-through period needs at least {rules.min_period_days} days")
    units: dict[int, int] = {}
    for line in sales_lines:
        day = line["date"] if isinstance(line["date"], date) else date.fromisoformat(str(line["date"]))
        if int(line["branch_id"]) != branch_id or line.get("offer_id") or not period_start <= day < period_end:
            continue
        units[int(line["product_id"])] = units.get(int(line["product_id"]), 0) + int(line["quantity"])

    proposals = []
    for product in products:
        if int(product["branch_id"]) != branch_id:
            continue
        pid = int(product["product_id"])
        sold = units.get(pid, 0)
        avg = (Decimal(sold) / Decimal(days)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        current = DemandLevel(str(product["demand_level"]))
        proposed = rules.classify(avg)
        if proposed == current:
            continue
        proposal_id = f"demand:{branch_id}:{pid}:{period_start.isoformat()}:{period_end.isoformat()}"
        proposals.append(DemandLevelProposal(
            idempotency_key=proposal_id, branch_id=branch_id, occurred_at=now,
            proposal_id=proposal_id, product_id=pid, current_level=current, proposed_level=proposed,
            period_start=period_start, period_end=period_end, units_sold=sold, days=days,
            avg_daily_units=avg, source=source,
        ))
    return proposals


def proposal_hash(proposal: DemandLevelProposal) -> str:
    raw = json.dumps(proposal.model_dump(mode="json", exclude={"occurred_at"}), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class ReviewError(ValueError):
    """The review or the change is not allowed."""


@dataclass(frozen=True)
class DemandReview:
    proposal_id: str
    proposal_hash: str
    decision: Literal["approve", "reject"]
    reviewer: str
    reason: Optional[str]
    reviewed_at: datetime


NON_HUMAN_PREFIXES = ("ai", "agent", "bot", "system", "crossflow", "restocking", "offers")


def default_is_human(actor: str) -> bool:
    """Stand-in until the Foundation auth middleware (A6) supplies real identities.
    Refuses "ai", "agent:offers", "AI-agent"... but not a person called "aisha"."""
    name = actor.strip().lower()
    if not name:
        return False
    return not any(name == p or name.startswith(tuple(p + sep for sep in ":-._ ")) for p in NON_HUMAN_PREFIXES)


@dataclass
class DemandReviewQueue:
    is_human: Callable[[str], bool] = default_is_human
    proposals: dict[str, DemandLevelProposal] = field(default_factory=dict)
    reviews: dict[str, DemandReview] = field(default_factory=dict)
    applied: set[str] = field(default_factory=set)
    audit: list[dict[str, Any]] = field(default_factory=list)

    def submit(self, proposal: DemandLevelProposal) -> bool:
        if proposal.proposal_id in self.proposals:
            return False  # replayed proposal
        self.proposals[proposal.proposal_id] = proposal
        return True

    def review(self, proposal_id: str, *, reviewer: str, decision: Literal["approve", "reject"],
               reason: Optional[str], now: datetime) -> DemandReview:
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise ReviewError(f"unknown proposal {proposal_id}")
        if proposal_id in self.reviews:
            raise ReviewError(f"proposal {proposal_id} already has a review")
        if not self.is_human(reviewer):
            raise ReviewError(f"{reviewer!r} is not an authorized human reviewer")
        if decision not in ("approve", "reject"):
            raise ReviewError(f"unknown decision {decision!r}")
        if decision == "reject" and not (reason or "").strip():
            raise ReviewError("a reason is required to reject")
        review = DemandReview(proposal_id, proposal_hash(proposal), decision, reviewer.strip(),
                              (reason or "").strip() or None, now)
        self.reviews[proposal_id] = review
        self.audit.append({"action": f"demand_proposal_{decision}", "proposal_id": proposal_id,
                           "actor": review.reviewer, "reason": review.reason, "at": now})
        return review

    def apply_approved(self, products: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
        """Return (updated product rows, refusals). Input rows are not modified."""
        by_id = {int(p["product_id"]): dict(p) for p in products}
        refusals: list[str] = []
        for proposal_id, proposal in self.proposals.items():
            if proposal_id in self.applied:
                continue
            review = self.reviews.get(proposal_id)
            if review is None:
                refusals.append(f"{proposal_id}: not reviewed, nothing changed")
                continue
            if review.decision != "approve":
                continue
            if review.proposal_hash != proposal_hash(proposal):
                refusals.append(f"{proposal_id}: proposal changed after review")
                continue
            product = by_id.get(proposal.product_id)
            if product is None:
                refusals.append(f"{proposal_id}: product {proposal.product_id} not found")
                continue
            if str(product["demand_level"]) != proposal.current_level.value:
                refusals.append(
                    f"{proposal_id}: stale, demand level is {product['demand_level']} now, "
                    f"the proposal was made for {proposal.current_level.value}"
                )
                continue
            product["demand_level"] = proposal.proposed_level.value
            self.applied.add(proposal_id)
            self.audit.append({"action": "demand_level_applied", "proposal_id": proposal_id,
                               "actor": review.reviewer, "product_id": proposal.product_id,
                               "before": proposal.current_level.value, "after": proposal.proposed_level.value,
                               "at": now})
        return list(by_id.values()), refusals
