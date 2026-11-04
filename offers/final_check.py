"""D9 - Final guardrail recheck before activation (Epic D: Offers).

Rule from the task table: any failure blocks activation.

Time passes between approval and activation: prices and costs move, other offers
get approved, products become restricted, expiry dates change, the start date can
slip into the past. So nothing is trusted from the approval moment. The offer is
checked again against CURRENT data:

  1. it is approved, and the approval is still valid for this exact version (D7/D8)
  2. validation (D3): branch, products, dates
  3. it has not already ended
  4. all guardrails (D4/D5): discount, margin, floor, cap, expiry, restrictions, conflicts

All reasons are collected so one look shows everything that is wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from offers.decisions import DecisionStore, is_approval_valid
from offers.guardrails import (
    ActiveOffers,
    GuardrailConfig,
    PriceReader,
    ProductRules,
    check_all_guardrails,
)
from offers.schema import Offer, OfferStatus
from offers.validation import CatalogReader, ValidationConfig, validate_offer


@dataclass(frozen=True)
class RecheckResult:
    ok: bool
    reasons: tuple[str, ...] = ()


def final_recheck(
    offer: Offer,
    *,
    decision_store: DecisionStore,
    catalog: CatalogReader,
    prices: PriceReader,
    rules: ProductRules,
    active: ActiveOffers,
    validation_config: ValidationConfig,
    guardrail_config: GuardrailConfig,
    now: datetime | None = None,
) -> RecheckResult:
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []

    if offer.status != OfferStatus.APPROVED:
        reasons.append(f"offer must be approved to activate, it is {offer.status.value}")
    elif not is_approval_valid(offer, decision_store):
        reasons.append("approval is missing or no longer matches this version")

    reasons += validate_offer(offer, catalog, validation_config, now).reasons

    if offer.end_date <= now:
        reasons.append("offer has already ended")

    reasons += check_all_guardrails(offer, prices, rules, active, guardrail_config).reasons

    return RecheckResult(ok=not reasons, reasons=tuple(reasons))