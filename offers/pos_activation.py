"""D10 - POS activation with idempotency key and response check (Epic D: Offers).

Flow for an APPROVED offer:
  1. Final recheck (D9). Any failure blocks activation and the POS is never called.
  2. Build a deterministic idempotency key for (offer, version, approval).
  3. Send the activation to the POS.
       - confirmed  -> offer becomes ACTIVE
       - rejected   -> offer stays APPROVED, reason recorded
       - UNCLEAR (timeout, error, garbage response) -> do NOT guess and do NOT blindly
         send again: ask the POS for the status of that key FIRST.
            * POS says active      -> ACTIVE
            * POS never saw it     -> send once more with the SAME key
            * still unclear        -> UNCONFIRMED, a person has to check
The idempotency key lets the POS ignore a repeated request, so a retry can never
create the offer twice. Only the POS can say an offer is live; we never assume it.

The real POS (the mock POS from M5, task E3) plugs in through the PosClient
interface; only a small adapter is needed to map its replies to PosResponse.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol

from offers.decisions import DecisionStore
from offers.guardrails import (
    ActiveOffers,
    GuardrailConfig,
    PriceReader,
    ProductRules,
)
from offers.recheck import final_recheck
from offers.review import _jsonable, _terms
from offers.schema import Offer, OfferStatus
from offers.validation import CatalogReader, ValidationConfig


class PosOutcome(str, Enum):
    ACTIVE = "active"        # the POS confirms the offer is live
    REJECTED = "rejected"    # the POS refused it
    NOT_FOUND = "not_found"  # status check: the POS never received this key
    UNCLEAR = "unclear"      # timeout, error, or a reply we cannot read


@dataclass(frozen=True)
class PosResponse:
    outcome: PosOutcome
    pos_ref: Optional[str] = None   # the POS's own id for the live offer
    detail: Optional[str] = None


class PosClient(Protocol):
    def activate(self, idempotency_key: str, payload: dict) -> PosResponse: ...
    def get_status(self, idempotency_key: str) -> PosResponse: ...


class ActivationOutcome(str, Enum):
    ACTIVATED = "activated"
    BLOCKED = "blocked"                  # final recheck failed, POS never called
    REJECTED_BY_POS = "rejected_by_pos"
    UNCONFIRMED = "unconfirmed"          # unclear even after the status check


@dataclass(frozen=True)
class ActivationResult:
    outcome: ActivationOutcome
    offer: Offer
    reasons: tuple[str, ...] = ()
    idempotency_key: Optional[str] = None
    pos_ref: Optional[str] = None


def idempotency_key_for(offer: Offer) -> str:
    """Same offer + same version + same approval -> same key, on every retry."""
    if not offer.approval_hash:
        raise ValueError("an offer needs an approval hash before it can be activated")
    return f"offer:{offer.offer_id}:v{offer.version}:{offer.approval_hash[:16]}"


def pos_payload(offer: Offer) -> dict[str, Any]:
    """What the POS receives: the approved commercial terms, nothing else."""
    return _jsonable(
        {
            "offer_id": offer.offer_id,
            "version": offer.version,
            "approval_hash": offer.approval_hash,
            "branch_id": offer.branch_id,
            "type": offer.type.value,
            "product_ids": list(offer.product_ids),
            "start_date": offer.start_date,
            "end_date": offer.end_date,
            "quantity_cap": offer.quantity_cap,
            "terms": _terms(offer),
        }
    )


def _safe(call, *args) -> PosResponse:
    """Any failure or unreadable reply counts as UNCLEAR, never as success."""
    try:
        response = call(*args)
    except Exception as exc:  # timeouts, connection errors, adapter errors
        return PosResponse(PosOutcome.UNCLEAR, detail=f"{type(exc).__name__}: {exc}")
    if not isinstance(response, PosResponse):
        return PosResponse(PosOutcome.UNCLEAR, detail="unreadable POS response")
    return response


def activate_on_pos(
    offer: Offer,
    *,
    pos: PosClient,
    store: DecisionStore,
    catalog: CatalogReader,
    prices: PriceReader,
    rules: ProductRules,
    active: ActiveOffers,
    validation_config: ValidationConfig,
    guardrail_config: GuardrailConfig,
    log,
    now: datetime | None = None,
) -> ActivationResult:
    now = now or datetime.now(timezone.utc)

    if offer.status == OfferStatus.ACTIVE:  # already live: nothing to send
        return ActivationResult(ActivationOutcome.ACTIVATED, offer)

    check = final_recheck(
        offer,
        store=store,
        catalog=catalog,
        prices=prices,
        rules=rules,
        active=active,
        validation_config=validation_config,
        guardrail_config=guardrail_config,
        log=log,
        now=now,
    )
    if not check.ok:
        return ActivationResult(ActivationOutcome.BLOCKED, offer, reasons=check.reasons)

    key = idempotency_key_for(offer)
    payload = pos_payload(offer)

    def finish(outcome: ActivationOutcome, reasons=(), pos_ref=None) -> ActivationResult:
        new_offer = (
            offer.model_copy(update={"status": OfferStatus.ACTIVE})
            if outcome == ActivationOutcome.ACTIVATED
            else offer
        )
        log.entries.append(
            {
                "offer_id": offer.offer_id,
                "version": offer.version,
                "stage": "activation",
                "outcome": outcome.value,
                "idempotency_key": key,
                "reasons": list(reasons),
                "at": now,
            }
        )
        return ActivationResult(outcome, new_offer, tuple(reasons), key, pos_ref)

    def from_response(resp: PosResponse) -> Optional[ActivationResult]:
        if resp.outcome == PosOutcome.ACTIVE:
            return finish(ActivationOutcome.ACTIVATED, pos_ref=resp.pos_ref)
        if resp.outcome == PosOutcome.REJECTED:
            return finish(
                ActivationOutcome.REJECTED_BY_POS,
                reasons=[resp.detail or "rejected by the POS"],
            )
        return None  # NOT_FOUND / UNCLEAR: caller decides

    first = from_response(_safe(pos.activate, key, payload))
    if first is not None:
        return first

    # Unclear reply: ask the POS what it has for this key BEFORE doing anything else.
    status = _safe(pos.get_status, key)
    decided = from_response(status)
    if decided is not None:
        return decided

    if status.outcome == PosOutcome.NOT_FOUND:
        # The POS never received it. Safe to send again with the same key, once.
        second = from_response(_safe(pos.activate, key, payload))
        if second is not None:
            return second
        return finish(
            ActivationOutcome.UNCONFIRMED,
            reasons=["POS reply unclear after retry, check the POS manually"],
        )

    return finish(
        ActivationOutcome.UNCONFIRMED,
        reasons=["POS reply unclear and status check unclear, check the POS manually"],
    )
