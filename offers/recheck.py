"""D9 - Final guardrail recheck before activation (Epic D: Offers).

Approval can be days old. Before an approved offer goes live, everything is
checked again against CURRENT data: approval still valid for this exact
version, validation (branch, products, dates), and every guardrail (discount,
margin, floor, quantity cap, expiry, restrictions, active-offer conflicts).

Rule from the task table: any failure blocks activation.
A blocked offer is not changed; the reasons are returned and logged so a
person can see why (a new version has to be reviewed and approved again).
"""
from __future__ import annotations

from datetime import datetime, timezone

from offers.decisions import DecisionStore, is_approval_valid
from offers.guardrails import (
    ActiveOffers,
    GuardrailConfig,
    GuardrailResult,
    PriceReader,
    ProductRules,
    check_all_guardrails,
)
from offers.schema import Offer, OfferStatus
from offers.validation import CatalogReader, ValidationConfig, validate_offer


def final_recheck(
    offer: Offer,
    *,
    store: DecisionStore,
    catalog: CatalogReader,
    prices: PriceReader,
    rules: ProductRules,
    active: ActiveOffers,
    validation_config: ValidationConfig,
    guardrail_config: GuardrailConfig,
    log,  # anything with an .entries list (RejectionLog now, audit_log A11 later)
    now: datetime | None = None,
) -> GuardrailResult:
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []

    if offer.status != OfferStatus.APPROVED:
        reasons.append(f"only an approved offer can be activated, offer is {offer.status.value}")

    if not is_approval_valid(offer, store):
        reasons.append("approval is missing or does not match this exact version")

    reasons += validate_offer(offer, catalog, validation_config, now).reasons
    reasons += check_all_guardrails(offer, prices, rules, active, guardrail_config).reasons

    result = GuardrailResult(ok=not reasons, reasons=tuple(reasons))
    if not result.ok:
        log.entries.append(
            {
                "offer_id": offer.offer_id,
                "version": offer.version,
                "stage": "pre_activation",
                "reasons": list(result.reasons),
                "at": now,
            }
        )
    return result
