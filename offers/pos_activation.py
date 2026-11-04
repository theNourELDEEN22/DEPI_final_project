"""D10 - POS activation with idempotency key and response check (Epic D: Offers).

Flow: final recheck (D9) -> send to the POS with an idempotency key -> check the answer.

Rule from the task table: an unclear response triggers a status check FIRST.
Never guess and never blindly send again, because the POS may have activated the
offer even though the answer was lost.

  confirmed  : 2xx and a body that says this offer is active  -> ACTIVE
  rejected   : 4xx with an explicit rejection in the body      -> stop, no retry
  unclear    : anything else (timeout, 5xx, empty or odd body, wrong offer id)
               -> ask the POS for the status of this activation
                    active      -> ACTIVE (confirmed by the status check)
                    not active  -> safe to retry with the SAME idempotency key
                    unknown     -> stop: UNCONFIRMED, a person must check

The idempotency key is built from offer id, version and approval hash, so a repeat
of the same activation can never create a second one, and a new version always
gets a new key.

M5 builds the mock POS (E3). It only has to match the small PosClient interface here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol

from offers.decisions import DecisionStore
from offers.final_check import final_recheck
from offers.guardrails import ActiveOffers, GuardrailConfig, PriceReader, ProductRules
from offers.review import _jsonable, _terms
from offers.schema import Offer, OfferStatus
from offers.validation import CatalogReader, ValidationConfig


@dataclass(frozen=True)
class PosResponse:
    http_status: Optional[int]
    body: Optional[dict[str, Any]] = None


class PosClient(Protocol):
    def activate_offer(self, payload: dict[str, Any], idempotency_key: str) -> PosResponse: ...
    def get_activation_status(self, idempotency_key: str) -> PosResponse: ...


@dataclass(frozen=True)
class ActivationConfig:
    max_retries: int = 1  # extra attempts after the first, only after a status check


class Outcome(str, Enum):
    ACTIVATED = "activated"
    BLOCKED = "blocked"                # the final recheck failed, the POS was never called
    REJECTED_BY_POS = "rejected_by_pos"
    FAILED = "failed"                  # status check says not active after all attempts
    UNCONFIRMED = "unconfirmed"        # status unknown: a person must check the POS


@dataclass(frozen=True)
class ActivationResult:
    outcome: Outcome
    offer: Offer
    reasons: tuple[str, ...]
    idempotency_key: Optional[str]
    activate_calls: int
    status_checked: bool
    confirmed_via: Optional[str] = None  # "response" or "status_check"


def idempotency_key(offer: Offer) -> str:
    return f"offer-{offer.offer_id}-v{offer.version}-{(offer.approval_hash or '')[:16]}"


def build_pos_payload(offer: Offer) -> dict[str, Any]:
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


def _classify_activation(resp: PosResponse, offer: Offer) -> tuple[str, str]:
    """Return ("confirmed" | "rejected" | "unclear", detail)."""
    body = resp.body if isinstance(resp.body, dict) else None
    if resp.http_status in (200, 201) and body is not None:
        if body.get("status") == "active" and body.get("offer_id") == offer.offer_id:
            return "confirmed", "POS confirmed activation"
        return "unclear", "POS answered 2xx but did not confirm this offer"
    if (
        resp.http_status is not None
        and 400 <= resp.http_status < 500
        and resp.http_status not in (408, 429)
        and body is not None
        and body.get("status") in ("rejected", "error")
    ):
        return "rejected", f"POS rejected the offer: {body.get('reason', 'no reason given')}"
    return "unclear", f"unclear POS response (http {resp.http_status})"


def _classify_status(resp: PosResponse, offer: Offer) -> str:
    """Return "active" | "not_active" | "unknown"."""
    body = resp.body if isinstance(resp.body, dict) else None
    if resp.http_status == 404:
        return "not_active"
    if resp.http_status == 200 and body is not None:
        state = body.get("state")
        if state == "active" and body.get("offer_id") == offer.offer_id:
            return "active"
        if state in ("not_found", "inactive"):
            return "not_active"
    return "unknown"


def activate_offer(
    offer: Offer,
    *,
    pos: PosClient,
    decision_store: DecisionStore,
    catalog: CatalogReader,
    prices: PriceReader,
    rules: ProductRules,
    active: ActiveOffers,
    validation_config: ValidationConfig,
    guardrail_config: GuardrailConfig,
    activation_config: ActivationConfig = ActivationConfig(),
    now: datetime | None = None,
) -> ActivationResult:
    now = now or datetime.now(timezone.utc)

    recheck = final_recheck(
        offer,
        decision_store=decision_store,
        catalog=catalog,
        prices=prices,
        rules=rules,
        active=active,
        validation_config=validation_config,
        guardrail_config=guardrail_config,
        now=now,
    )
    if not recheck.ok:
        return ActivationResult(Outcome.BLOCKED, offer, recheck.reasons, None, 0, False)

    key = idempotency_key(offer)
    payload = build_pos_payload(offer)
    calls = 0
    status_checked = False
    last_detail = ""

    def done(outcome: Outcome, reasons: tuple[str, ...], via: str | None = None) -> ActivationResult:
        new_offer = offer.model_copy(update={"status": OfferStatus.ACTIVE}) if outcome == Outcome.ACTIVATED else offer
        return ActivationResult(outcome, new_offer, reasons, key, calls, status_checked, via)

    for _attempt in range(1 + activation_config.max_retries):
        calls += 1
        try:
            verdict, last_detail = _classify_activation(pos.activate_offer(payload, key), offer)
        except Exception as exc:  # timeouts, connection errors: the answer is lost
            verdict, last_detail = "unclear", f"no usable answer from the POS ({type(exc).__name__})"

        if verdict == "confirmed":
            return done(Outcome.ACTIVATED, (), "response")
        if verdict == "rejected":
            return done(Outcome.REJECTED_BY_POS, (last_detail,))

        # Unclear answer: status check FIRST, before anything else.
        status_checked = True
        try:
            state = _classify_status(pos.get_activation_status(key), offer)
        except Exception:
            state = "unknown"

        if state == "active":
            return done(Outcome.ACTIVATED, (), "status_check")
        if state == "unknown":
            return done(
                Outcome.UNCONFIRMED,
                (last_detail, "status check was also unclear: check the POS manually"),
            )
        # not_active: the POS did not activate it, so a retry with the same key is safe

    return done(
        Outcome.FAILED,
        (last_detail, f"status check says not active after {calls} attempt(s)"),
    )