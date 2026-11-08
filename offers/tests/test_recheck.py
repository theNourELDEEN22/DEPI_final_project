from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offers.decisions import Decision, InMemoryDecisionStore, decide
from offers.guardrails import (
    GuardrailConfig,
    InMemoryActiveOffers,
    InMemoryPrices,
    InMemoryProductRules,
    PriceInfo,
)
from offers.recheck import final_recheck
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
ACTIVATION_TIME = NOW + timedelta(hours=20)  # still before the offer starts

CATALOG = InMemoryCatalog({"b1": {"p1", "p2"}})
PRICES = InMemoryPrices(
    {
        ("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("60")),
        ("b1", "p2"): PriceInfo(price=Decimal("100"), cost=Decimal("10")),
    }
)
EVIDENCE = InMemoryEvidence({"sig-1": Evidence("sig-1", "stock_signal", {"stock_on_hand": 120})})
FORECASTS = InMemoryForecasts({"fc-1": Forecast("fc-1", Decimal("80"), NOW)})


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


def recheck(offer, store, *, catalog=CATALOG, prices=PRICES, rules=None, active=None, log=None, now=ACTIVATION_TIME):
    return final_recheck(
        offer,
        store=store,
        catalog=catalog,
        prices=prices,
        rules=rules or InMemoryProductRules(),
        active=active or InMemoryActiveOffers(),
        validation_config=ValidationConfig(),
        guardrail_config=GuardrailConfig(),
        log=log if log is not None else RejectionLog(),
        now=now,
    )


def test_clean_approved_offer_passes():
    offer, store = approved_offer()
    result = recheck(offer, store)
    assert result.ok and result.reasons == ()


@pytest.mark.parametrize(
    "status", [OfferStatus.DRAFT, OfferStatus.PENDING_APPROVAL, OfferStatus.REJECTED, OfferStatus.CHANGES_REQUESTED]
)
def test_offer_that_is_not_approved_is_blocked(status):
    offer, store = approved_offer()
    result = recheck(offer.model_copy(update={"status": status}), store)
    assert not result.ok
    assert any("approved offer" in r for r in result.reasons)


def test_missing_approval_is_blocked():
    offer, _ = approved_offer()
    result = recheck(offer, InMemoryDecisionStore())  # no approval on record
    assert not result.ok
    assert any("approval" in r for r in result.reasons)


def test_forged_approval_hash_is_blocked():
    offer, store = approved_offer()
    forged = offer.model_copy(update={"approval_hash": "forged"})
    assert not recheck(forged, store).ok


def test_content_changed_after_approval_is_blocked():
    offer, store = approved_offer()
    tampered = offer.model_copy(update={"discount_percent": Decimal("15")})
    result = recheck(tampered, store)
    assert not result.ok
    assert any("approval" in r for r in result.reasons)


def test_price_change_since_approval_is_blocked():
    offer, store = approved_offer()
    costlier = InMemoryPrices({("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("78"))})
    result = recheck(offer, store, prices=costlier)  # margin falls to 2.5%
    assert not result.ok
    assert any("margin" in r for r in result.reasons)


def test_product_restricted_since_approval_is_blocked():
    offer, store = approved_offer()
    rules = InMemoryProductRules(restricted={("b1", "p1")})
    result = recheck(offer, store, rules=rules)
    assert not result.ok
    assert any("restricted" in r for r in result.reasons)


def test_expiry_moved_earlier_since_approval_is_blocked():
    offer, store = approved_offer()
    rules = InMemoryProductRules(expiry={("b1", "p1"): END - timedelta(days=3)})
    result = recheck(offer, store, rules=rules)
    assert not result.ok
    assert any("expiry" in r for r in result.reasons)


def test_new_conflicting_offer_since_approval_is_blocked():
    offer, store = approved_offer()
    other = make_offer(status=OfferStatus.ACTIVE)  # different offer_id, same product and dates
    result = recheck(offer, store, active=InMemoryActiveOffers([other]))
    assert not result.ok
    assert any("conflicts" in r for r in result.reasons)


def test_offer_does_not_conflict_with_itself():
    offer, store = approved_offer()
    assert recheck(offer, store, active=InMemoryActiveOffers([offer])).ok


def test_stale_offer_whose_start_has_passed_is_blocked():
    offer, store = approved_offer()
    result = recheck(offer, store, now=START + timedelta(days=1))
    assert not result.ok
    assert any("past" in r for r in result.reasons)


def test_branch_removed_since_approval_is_blocked():
    offer, store = approved_offer()
    result = recheck(offer, store, catalog=InMemoryCatalog({}))
    assert not result.ok
    assert any("unknown branch" in r for r in result.reasons)


def test_all_failures_are_collected_together():
    offer, store = approved_offer()
    rules = InMemoryProductRules(restricted={("b1", "p1")})
    costlier = InMemoryPrices({("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("78"))})
    result = recheck(offer, store, rules=rules, prices=costlier, now=START + timedelta(days=1))
    assert len(result.reasons) >= 3


def test_failure_is_logged_and_success_is_not():
    offer, store = approved_offer()
    log = RejectionLog()
    assert recheck(offer, store, log=log).ok
    assert log.entries == []

    recheck(offer, store, rules=InMemoryProductRules(restricted={("b1", "p1")}), log=log)
    assert len(log.entries) == 1
    entry = log.entries[0]
    assert entry["stage"] == "pre_activation"
    assert entry["offer_id"] == offer.offer_id and entry["version"] == offer.version
    assert entry["reasons"]
