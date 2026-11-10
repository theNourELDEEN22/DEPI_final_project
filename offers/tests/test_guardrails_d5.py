from datetime import datetime, timedelta, timezone
from decimal import Decimal

from offers.guardrails import (
    GuardrailConfig,
    InMemoryActiveOffers,
    InMemoryPrices,
    InMemoryProductRules,
    PriceInfo,
    check_all_guardrails,
    check_extended_guardrails,
    run_all_guardrails,
)
from offers.schema import Offer, OfferStatus, OfferType
from offers.validation import RejectionLog

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
START = NOW + timedelta(days=1)
END = NOW + timedelta(days=8)
CONFIG = GuardrailConfig(max_quantity_cap=500)

PRICES = InMemoryPrices(
    {
        ("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("60")),
        ("b1", "p2"): PriceInfo(price=Decimal("100"), cost=Decimal("10")),
    }
)
NO_RULES = InMemoryProductRules()
NO_ACTIVE = InMemoryActiveOffers()


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


def check(offer, rules=NO_RULES, active=NO_ACTIVE):
    return check_extended_guardrails(offer, rules, active, CONFIG)


# --- quantity cap ---------------------------------------------------------


def test_valid_offer_passes_d5():
    assert check(make_offer()).ok


def test_missing_quantity_cap_rejected():
    result = check(make_offer(quantity_cap=None))
    assert not result.ok
    assert len(result.reasons) == 1 and "quantity cap is required" in result.reasons[0]


def test_quantity_cap_above_max_rejected():
    result = check(make_offer(quantity_cap=501))
    assert not result.ok
    assert len(result.reasons) == 1 and "above the maximum" in result.reasons[0]


def test_quantity_cap_exactly_at_max_passes():
    assert check(make_offer(quantity_cap=500)).ok


# --- expiry dates ---------------------------------------------------------


def test_offer_ending_after_expiry_rejected():
    rules = InMemoryProductRules(expiry={("b1", "p1"): END - timedelta(days=2)})
    result = check(make_offer(), rules)
    assert not result.ok
    assert len(result.reasons) == 1 and "expiry" in result.reasons[0]


def test_offer_ending_exactly_on_expiry_passes():
    rules = InMemoryProductRules(expiry={("b1", "p1"): END})
    assert check(make_offer(), rules).ok


def test_product_without_expiry_is_fine():
    assert check(make_offer(), InMemoryProductRules()).ok


# --- restrictions ---------------------------------------------------------


def test_restricted_product_rejected():
    rules = InMemoryProductRules(restricted={("b1", "p1")})
    result = check(make_offer(), rules)
    assert not result.ok
    assert len(result.reasons) == 1 and "restricted" in result.reasons[0]


# --- active-offer conflicts ----------------------------------------------


def other_offer(**over) -> Offer:
    return make_offer(status=OfferStatus.ACTIVE, **over)


def test_overlapping_active_offer_on_same_product_conflicts():
    active = InMemoryActiveOffers([other_offer(start_date=START + timedelta(days=2), end_date=END + timedelta(days=5))])
    result = check(make_offer(), active=active)
    assert not result.ok
    assert len(result.reasons) == 1 and "conflicts" in result.reasons[0]


def test_approved_offer_also_conflicts():
    other = make_offer(status=OfferStatus.APPROVED)
    result = check(make_offer(), active=InMemoryActiveOffers([other]))
    assert not result.ok


def test_different_product_does_not_conflict():
    active = InMemoryActiveOffers([other_offer(product_ids=["p2"])])
    assert check(make_offer(), active=active).ok


def test_non_overlapping_dates_do_not_conflict():
    active = InMemoryActiveOffers(
        [other_offer(start_date=END, end_date=END + timedelta(days=7))]
    )
    assert check(make_offer(), active=active).ok


def test_draft_or_rejected_offers_do_not_conflict():
    drafts = InMemoryActiveOffers(
        [make_offer(status=OfferStatus.DRAFT), make_offer(status=OfferStatus.REJECTED)]
    )
    assert check(make_offer(), active=drafts).ok


def test_new_version_does_not_conflict_with_itself():
    v1 = make_offer(status=OfferStatus.ACTIVE)
    v2 = v1.new_version(discount_percent=Decimal("15"))
    assert check(v2, active=InMemoryActiveOffers([v1])).ok


# --- combined D4 + D5 -------------------------------------------------------


def test_all_guardrails_pass_for_a_clean_offer():
    assert check_all_guardrails(make_offer(), PRICES, NO_RULES, NO_ACTIVE, CONFIG).ok


def test_d4_and_d5_reasons_collected_together():
    rules = InMemoryProductRules(restricted={("b1", "p1")})
    bad = make_offer(discount_percent=Decimal("40"), quantity_cap=None)  # margin + cap + restricted
    result = check_all_guardrails(bad, PRICES, rules, NO_ACTIVE, CONFIG)
    assert not result.ok
    assert len(result.reasons) == 3


def test_run_all_guardrails_rejects_and_logs():
    log = RejectionLog()
    rules = InMemoryProductRules(restricted={("b1", "p1")})
    offer, result = run_all_guardrails(make_offer(), PRICES, rules, NO_ACTIVE, CONFIG, log, NOW)
    assert not result.ok
    assert offer.status == OfferStatus.REJECTED
    assert log.entries[0]["stage"] == "guardrails"
