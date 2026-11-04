from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offers.decisions import (
    Decision,
    DecisionError,
    InMemoryDecisionStore,
    approval_hash_for,
    decide,
    is_approval_valid,
    revise_offer,
)
from offers.guardrails import (
    GuardrailConfig,
    InMemoryActiveOffers,
    InMemoryPrices,
    InMemoryProductRules,
    PriceInfo,
)
from offers.review import (
    Evidence,
    Forecast,
    InMemoryEvidence,
    InMemoryForecasts,
    build_review_card,
)
from offers.schema import Offer, OfferStatus, OfferType
from offers.validation import InMemoryCatalog, ValidationConfig

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=2)

CATALOG = InMemoryCatalog({"b1": {"p1", "p2"}})
PRICES = InMemoryPrices(
    {
        ("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("60")),
        ("b1", "p2"): PriceInfo(price=Decimal("100"), cost=Decimal("10")),
    }
)
EVIDENCE = InMemoryEvidence({"sig-1": Evidence("sig-1", "stock_signal", {"stock_on_hand": 120})})
FORECASTS = InMemoryForecasts(
    {
        "fc-1": Forecast("fc-1", Decimal("80"), NOW),
        "fc-2": Forecast("fc-2", Decimal("90"), NOW),
    }
)


def make_offer(**over) -> Offer:
    base = dict(
        branch_id="b1",
        type=OfferType.DISCOUNT,
        product_ids=["p1"],
        start_date=NOW + timedelta(days=1),
        end_date=NOW + timedelta(days=8),
        discount_percent=Decimal("20"),
        quantity_cap=100,
        evidence_ref="sig-1",
        forecast_ref="fc-1",
    )
    base.update(over)
    return Offer(**base)


def review(offer):
    """Draft -> (card, pending offer), through the real D6 builder."""
    return build_review_card(
        offer,
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


def pending():
    card, offer = review(make_offer())
    return card, offer


# --- D7: Approve / Reject / Request changes -------------------------------


def test_approve_records_who_when_why_and_version():
    card, offer = pending()
    store = InMemoryDecisionStore()
    approved, rec = decide(offer, card, Decision.APPROVE, "sara", "good plan", store, LATER)
    assert approved.status == OfferStatus.APPROVED
    assert approved.approval_hash == approval_hash_for(offer)
    assert rec.actor == "sara"
    assert rec.decided_at == LATER
    assert rec.reason == "good plan"
    assert rec.version == offer.version
    assert rec.content_hash == offer.content_hash()
    assert rec.approval_hash == approved.approval_hash
    assert store.all() == [rec]


def test_approve_without_a_reason_is_allowed():
    card, offer = pending()
    approved, rec = decide(offer, card, "approve", "sara", None, InMemoryDecisionStore(), LATER)
    assert approved.status == OfferStatus.APPROVED
    assert rec.reason is None


def test_reject_needs_a_reason_and_records_it():
    card, offer = pending()
    store = InMemoryDecisionStore()
    with pytest.raises(DecisionError):
        decide(offer, card, Decision.REJECT, "sara", "  ", store, LATER)
    rejected, rec = decide(offer, card, Decision.REJECT, "sara", "wrong timing", store, LATER)
    assert rejected.status == OfferStatus.REJECTED
    assert rejected.approval_hash is None
    assert rec.reason == "wrong timing" and rec.decision == Decision.REJECT


def test_request_changes_needs_a_reason_and_records_it():
    card, offer = pending()
    store = InMemoryDecisionStore()
    with pytest.raises(DecisionError):
        decide(offer, card, Decision.REQUEST_CHANGES, "sara", None, store, LATER)
    changed, rec = decide(offer, card, Decision.REQUEST_CHANGES, "sara", "lower the discount", store, LATER)
    assert changed.status == OfferStatus.CHANGES_REQUESTED
    assert changed.approval_hash is None
    assert rec.decision == Decision.REQUEST_CHANGES


@pytest.mark.parametrize("actor", [None, "", "   "])
@pytest.mark.parametrize("decision", list(Decision))
def test_actor_is_required(actor, decision):
    card, offer = pending()
    store = InMemoryDecisionStore()
    with pytest.raises(DecisionError):
        decide(offer, card, decision, actor, "reason", store, LATER)
    assert store.all() == []


@pytest.mark.parametrize("status", [OfferStatus.APPROVED, OfferStatus.REJECTED, OfferStatus.ACTIVE])
def test_only_pending_offers_can_be_decided(status):
    card, offer = pending()
    wrong = offer.model_copy(update={"status": status})
    with pytest.raises(DecisionError):
        decide(wrong, card, Decision.APPROVE, "sara", None, InMemoryDecisionStore(), LATER)


def test_a_draft_cannot_be_decided():
    card, offer = pending()
    draft = offer.model_copy(update={"status": OfferStatus.DRAFT})
    with pytest.raises(DecisionError):
        decide(draft, card, Decision.APPROVE, "sara", None, InMemoryDecisionStore(), LATER)


def test_a_version_cannot_be_decided_twice():
    card, offer = pending()
    store = InMemoryDecisionStore()
    decide(offer, card, Decision.APPROVE, "sara", None, store, LATER)
    with pytest.raises(DecisionError):  # same pending object used again
        decide(offer, card, Decision.REJECT, "omar", "changed my mind", store, LATER)
    assert len(store.all()) == 1


def test_unknown_decision_is_refused():
    card, offer = pending()
    with pytest.raises(DecisionError):
        decide(offer, card, "maybe", "sara", None, InMemoryDecisionStore(), LATER)


def test_decision_on_a_stale_card_is_refused():
    card_v1, offer_v1 = pending()
    offer_v2 = offer_v1.new_version(discount_percent=Decimal("15")).model_copy(
        update={"status": OfferStatus.PENDING_APPROVAL}
    )
    with pytest.raises(DecisionError):
        decide(offer_v2, card_v1, Decision.APPROVE, "sara", None, InMemoryDecisionStore(), LATER)


def test_approval_does_not_change_the_content_hash():
    card, offer = pending()
    approved, _ = decide(offer, card, Decision.APPROVE, "sara", None, InMemoryDecisionStore(), LATER)
    assert approved.content_hash() == offer.content_hash()


# --- approval validity (used by D9 / D10) ---------------------------------


def test_valid_approval_is_recognised():
    card, offer = pending()
    store = InMemoryDecisionStore()
    approved, _ = decide(offer, card, Decision.APPROVE, "sara", None, store, LATER)
    assert is_approval_valid(approved, store)


def test_pending_or_rejected_offers_have_no_valid_approval():
    card, offer = pending()
    store = InMemoryDecisionStore()
    assert not is_approval_valid(offer, store)
    rejected, _ = decide(offer, card, Decision.REJECT, "sara", "no", store, LATER)
    assert not is_approval_valid(rejected, store)


def test_made_up_approval_hash_is_not_valid():
    card, offer = pending()
    store = InMemoryDecisionStore()
    fake = offer.model_copy(update={"status": OfferStatus.APPROVED, "approval_hash": "forged"})
    assert not is_approval_valid(fake, store)


def test_approved_offer_with_tampered_content_is_not_valid():
    card, offer = pending()
    store = InMemoryDecisionStore()
    approved, _ = decide(offer, card, Decision.APPROVE, "sara", None, store, LATER)
    tampered = approved.model_copy(update={"discount_percent": Decimal("45")})
    assert not is_approval_valid(tampered, store)


# --- D8: change request -> new version, forecast, check, approval ---------


def request_changes():
    card, offer = pending()
    store = InMemoryDecisionStore()
    changed, _ = decide(offer, card, Decision.REQUEST_CHANGES, "sara", "lower the discount", store, LATER)
    return changed, store


def test_change_request_creates_a_new_draft_version_with_a_new_forecast():
    changed, store = request_changes()
    v2 = revise_offer(changed, {"discount_percent": Decimal("15")}, "fc-2", store)
    assert v2.offer_id == changed.offer_id
    assert v2.version == changed.version + 1
    assert v2.status == OfferStatus.DRAFT
    assert v2.approval_hash is None
    assert v2.forecast_ref == "fc-2"
    assert v2.discount_percent == Decimal("15")
    assert v2.content_hash() != changed.content_hash()


def test_old_approval_hash_no_longer_matches_the_new_version():
    card, offer = pending()
    store = InMemoryDecisionStore()
    v1, _ = decide(offer, card, Decision.APPROVE, "sara", None, store, LATER)
    old_hash = v1.approval_hash

    v2 = v1.new_version(discount_percent=Decimal("15"), forecast_ref="fc-2")
    assert approval_hash_for(v2) != old_hash

    # reuse the old hash on the new version
    forged = v2.model_copy(update={"status": OfferStatus.APPROVED, "approval_hash": old_hash})
    assert not is_approval_valid(forged, store)
    # v1 itself is still valid
    assert is_approval_valid(v1, store)


def test_same_content_new_version_still_gets_a_different_approval_hash():
    card, offer = pending()
    v2 = offer.new_version()
    assert approval_hash_for(v2) != approval_hash_for(offer)


def test_revise_needs_a_recorded_change_request():
    card, offer = pending()
    store = InMemoryDecisionStore()
    fake = offer.model_copy(update={"status": OfferStatus.CHANGES_REQUESTED})
    with pytest.raises(DecisionError):
        revise_offer(fake, {"discount_percent": Decimal("15")}, "fc-2", store)


@pytest.mark.parametrize("status", [OfferStatus.DRAFT, OfferStatus.PENDING_APPROVAL, OfferStatus.APPROVED])
def test_only_offers_with_requested_changes_can_be_revised(status):
    changed, store = request_changes()
    wrong = changed.model_copy(update={"status": status})
    with pytest.raises(DecisionError):
        revise_offer(wrong, {"discount_percent": Decimal("15")}, "fc-2", store)


def test_revise_needs_a_new_forecast_reference():
    changed, store = request_changes()
    for ref in (None, "", "fc-1"):  # fc-1 is the old one
        with pytest.raises(DecisionError):
            revise_offer(changed, {"discount_percent": Decimal("15")}, ref, store)


def test_revise_needs_at_least_one_change():
    changed, store = request_changes()
    with pytest.raises(DecisionError):
        revise_offer(changed, {}, "fc-2", store)


@pytest.mark.parametrize("field", ["offer_id", "version", "status", "approval_hash", "created_at"])
def test_protected_fields_cannot_be_changed(field):
    changed, store = request_changes()
    with pytest.raises(DecisionError):
        revise_offer(changed, {field: "x"}, "fc-2", store)


def test_forecast_cannot_be_smuggled_in_through_changes():
    changed, store = request_changes()
    with pytest.raises(DecisionError):
        revise_offer(changed, {"forecast_ref": "fc-2"}, "fc-2", store)


def test_invalid_requested_values_are_refused():
    changed, store = request_changes()
    with pytest.raises(DecisionError):
        revise_offer(changed, {"discount_percent": Decimal("150")}, "fc-2", store)


def test_full_cycle_review_change_check_and_new_approval():
    card1, offer1 = pending()
    store = InMemoryDecisionStore()
    changed, _ = decide(offer1, card1, Decision.REQUEST_CHANGES, "sara", "lower the discount", store, LATER)

    v2 = revise_offer(changed, {"discount_percent": Decimal("15")}, "fc-2", store)
    card2, pending2 = review(v2)  # the check runs again inside the card builder
    assert card2.version == 2 and card2.forecast["ref"] == "fc-2"

    approved2, rec2 = decide(pending2, card2, Decision.APPROVE, "omar", "ok now", store, LATER)
    assert is_approval_valid(approved2, store)
    assert rec2.version == 2
    assert [r.version for r in store.records_for(offer1.offer_id)] == [1, 2]


def test_a_new_version_that_breaks_a_guardrail_never_reaches_review():
    from offers.review import ReviewNotAllowed

    changed, store = request_changes()
    v2 = revise_offer(changed, {"discount_percent": Decimal("40")}, "fc-2", store)  # margin 0%
    with pytest.raises(ReviewNotAllowed):
        review(v2)