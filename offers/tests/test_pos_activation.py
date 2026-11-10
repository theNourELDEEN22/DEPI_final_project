import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offers.decisions import Decision, InMemoryDecisionStore, decide, is_approval_valid
from offers.guardrails import (
    GuardrailConfig,
    InMemoryActiveOffers,
    InMemoryPrices,
    InMemoryProductRules,
    PriceInfo,
)
from offers.pos_activation import (
    ActivationOutcome,
    PosOutcome,
    PosResponse,
    activate_on_pos,
    idempotency_key_for,
    pos_payload,
)
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
ACTIVATION_TIME = NOW + timedelta(hours=20)

CATALOG = InMemoryCatalog({"b1": {"p1", "p2"}})
PRICES = InMemoryPrices({("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("60"))})
EVIDENCE = InMemoryEvidence({"sig-1": Evidence("sig-1", "stock_signal", {"stock_on_hand": 120})})
FORECASTS = InMemoryForecasts({"fc-1": Forecast("fc-1", Decimal("80"), NOW)})

ACTIVE = PosResponse(PosOutcome.ACTIVE, pos_ref="pos-77")
REJECTED = PosResponse(PosOutcome.REJECTED, detail="product not on the POS price list")
UNCLEAR = PosResponse(PosOutcome.UNCLEAR, detail="timeout")
NOT_FOUND = PosResponse(PosOutcome.NOT_FOUND)


class FakePos:
    """Scripted mock POS. An item that is an Exception is raised instead of returned."""

    def __init__(self, activate=(), status=()):
        self._activate = list(activate)
        self._status = list(status)
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[dict] = []

    def _next(self, script):
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def activate(self, idempotency_key, payload):
        self.calls.append(("activate", idempotency_key))
        self.payloads.append(payload)
        return self._next(self._activate)

    def get_status(self, idempotency_key):
        self.calls.append(("status", idempotency_key))
        return self._next(self._status)


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


def approved_offer():
    card, pending = build_review_card(
        make_offer(),
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
    store = InMemoryDecisionStore()
    approved, _ = decide(pending, card, Decision.APPROVE, "sara", None, store, NOW)
    return approved, store


def run(offer, store, pos, *, log=None, rules=None):
    return activate_on_pos(
        offer,
        pos=pos,
        store=store,
        catalog=CATALOG,
        prices=PRICES,
        rules=rules or InMemoryProductRules(),
        active=InMemoryActiveOffers(),
        validation_config=ValidationConfig(),
        guardrail_config=GuardrailConfig(),
        log=log if log is not None else RejectionLog(),
        now=ACTIVATION_TIME,
    )


# --- happy path and blocking ------------------------------------------------


def test_confirmed_activation_makes_the_offer_active():
    offer, store = approved_offer()
    pos = FakePos(activate=[ACTIVE])
    result = run(offer, store, pos)
    assert result.outcome == ActivationOutcome.ACTIVATED
    assert result.offer.status == OfferStatus.ACTIVE
    assert result.pos_ref == "pos-77"
    assert [c[0] for c in pos.calls] == ["activate"]  # no status call needed
    assert is_approval_valid(result.offer, store)


def test_failed_recheck_blocks_and_the_pos_is_never_called():
    offer, store = approved_offer()
    pos = FakePos(activate=[ACTIVE])
    result = run(offer, store, pos, rules=InMemoryProductRules(restricted={("b1", "p1")}))
    assert result.outcome == ActivationOutcome.BLOCKED
    assert result.offer.status == OfferStatus.APPROVED
    assert any("restricted" in r for r in result.reasons)
    assert pos.calls == []


def test_an_unapproved_offer_is_never_sent():
    offer, store = approved_offer()
    draft = offer.model_copy(update={"status": OfferStatus.DRAFT})
    pos = FakePos(activate=[ACTIVE])
    assert run(draft, store, pos).outcome == ActivationOutcome.BLOCKED
    assert pos.calls == []


def test_an_already_active_offer_is_not_sent_again():
    offer, store = approved_offer()
    live = offer.model_copy(update={"status": OfferStatus.ACTIVE})
    pos = FakePos()
    result = run(live, store, pos)
    assert result.outcome == ActivationOutcome.ACTIVATED
    assert pos.calls == []


# --- idempotency key and payload ---------------------------------------------


def test_idempotency_key_is_stable_and_version_specific():
    offer, store = approved_offer()
    assert idempotency_key_for(offer) == idempotency_key_for(offer)
    assert offer.offer_id in idempotency_key_for(offer)
    v2 = offer.new_version(discount_percent=Decimal("15"))
    v2 = v2.model_copy(update={"approval_hash": "another-approval-hash-value"})
    assert idempotency_key_for(v2) != idempotency_key_for(offer)


def test_no_key_without_an_approval_hash():
    with pytest.raises(ValueError):
        idempotency_key_for(make_offer())


def test_the_pos_receives_the_key_and_the_approved_terms():
    offer, store = approved_offer()
    pos = FakePos(activate=[ACTIVE])
    run(offer, store, pos)
    key = pos.calls[0][1]
    assert key == idempotency_key_for(offer)
    payload = pos.payloads[0]
    assert payload["offer_id"] == offer.offer_id
    assert payload["version"] == 1
    assert payload["approval_hash"] == offer.approval_hash
    assert payload["quantity_cap"] == 100
    assert payload["terms"] == {"discount_percent": "20"}
    json.dumps(payload)  # JSON-safe


def test_payload_dates_are_iso_strings():
    offer, _ = approved_offer()
    payload = pos_payload(offer)
    assert payload["start_date"] == START.isoformat()
    assert payload["end_date"] == END.isoformat()


# --- POS says no ------------------------------------------------------------


def test_pos_rejection_keeps_the_offer_approved_and_records_why():
    offer, store = approved_offer()
    pos = FakePos(activate=[REJECTED])
    result = run(offer, store, pos)
    assert result.outcome == ActivationOutcome.REJECTED_BY_POS
    assert result.offer.status == OfferStatus.APPROVED
    assert "price list" in result.reasons[0]
    assert [c[0] for c in pos.calls] == ["activate"]  # a refusal is not retried


# --- unclear responses: status check FIRST -----------------------------------


def test_unclear_reply_triggers_a_status_check_before_anything_else():
    offer, store = approved_offer()
    pos = FakePos(activate=[UNCLEAR], status=[ACTIVE])
    result = run(offer, store, pos)
    assert [c[0] for c in pos.calls] == ["activate", "status"]
    assert result.outcome == ActivationOutcome.ACTIVATED
    assert result.offer.status == OfferStatus.ACTIVE


def test_timeout_is_treated_as_unclear_not_as_failure_or_success():
    offer, store = approved_offer()
    pos = FakePos(activate=[TimeoutError("no answer")], status=[ACTIVE])
    result = run(offer, store, pos)
    assert [c[0] for c in pos.calls] == ["activate", "status"]
    assert result.outcome == ActivationOutcome.ACTIVATED


def test_unreadable_reply_is_treated_as_unclear():
    offer, store = approved_offer()
    pos = FakePos(activate=[None], status=[ACTIVE])
    result = run(offer, store, pos)
    assert [c[0] for c in pos.calls] == ["activate", "status"]
    assert result.outcome == ActivationOutcome.ACTIVATED


def test_unclear_then_pos_never_saw_it_retries_once_with_the_same_key():
    offer, store = approved_offer()
    pos = FakePos(activate=[UNCLEAR, ACTIVE], status=[NOT_FOUND])
    result = run(offer, store, pos)
    assert [c[0] for c in pos.calls] == ["activate", "status", "activate"]
    assert len({c[1] for c in pos.calls}) == 1  # same idempotency key every time
    assert result.outcome == ActivationOutcome.ACTIVATED


def test_retry_that_is_still_unclear_ends_unconfirmed():
    offer, store = approved_offer()
    pos = FakePos(activate=[UNCLEAR, UNCLEAR], status=[NOT_FOUND])
    result = run(offer, store, pos)
    assert result.outcome == ActivationOutcome.UNCONFIRMED
    assert result.offer.status == OfferStatus.APPROVED
    assert [c[0] for c in pos.calls] == ["activate", "status", "activate"]  # no third send


def test_retry_that_the_pos_rejects_ends_rejected():
    offer, store = approved_offer()
    pos = FakePos(activate=[UNCLEAR, REJECTED], status=[NOT_FOUND])
    assert run(offer, store, pos).outcome == ActivationOutcome.REJECTED_BY_POS


def test_unclear_status_is_never_followed_by_a_blind_resend():
    offer, store = approved_offer()
    pos = FakePos(activate=[UNCLEAR], status=[UNCLEAR])
    result = run(offer, store, pos)
    assert result.outcome == ActivationOutcome.UNCONFIRMED
    assert result.offer.status == OfferStatus.APPROVED
    assert [c[0] for c in pos.calls] == ["activate", "status"]


def test_status_check_that_raises_ends_unconfirmed():
    offer, store = approved_offer()
    pos = FakePos(activate=[UNCLEAR], status=[ConnectionError("down")])
    result = run(offer, store, pos)
    assert result.outcome == ActivationOutcome.UNCONFIRMED
    assert [c[0] for c in pos.calls] == ["activate", "status"]


def test_status_says_rejected():
    offer, store = approved_offer()
    pos = FakePos(activate=[UNCLEAR], status=[REJECTED])
    assert run(offer, store, pos).outcome == ActivationOutcome.REJECTED_BY_POS


# --- audit -------------------------------------------------------------------


def test_every_activation_result_is_logged_with_its_key():
    offer, store = approved_offer()
    log = RejectionLog()
    result = run(offer, store, FakePos(activate=[ACTIVE]), log=log)
    entries = [e for e in log.entries if e["stage"] == "activation"]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "activated"
    assert entries[0]["idempotency_key"] == result.idempotency_key


def test_unconfirmed_result_is_logged_for_manual_follow_up():
    offer, store = approved_offer()
    log = RejectionLog()
    run(offer, store, FakePos(activate=[UNCLEAR], status=[UNCLEAR]), log=log)
    entry = [e for e in log.entries if e["stage"] == "activation"][0]
    assert entry["outcome"] == "unconfirmed"
    assert entry["reasons"]
