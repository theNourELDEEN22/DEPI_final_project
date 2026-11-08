"""Hardening tests found by a verification pass:
- dates without a timezone
- the hash must not change when the same value is spelled differently
- exact boundaries of rules that had no boundary test
"""
import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from offers.decisions import Decision, DecisionError, InMemoryDecisionStore, approval_hash_for, decide
from offers.guardrails import (
    GuardrailConfig,
    InMemoryActiveOffers,
    InMemoryPrices,
    InMemoryProductRules,
    PriceInfo,
    check_guardrails,
)
from offers.intake import InMemoryOfferRepository, IntakeError, intake_candidate
from offers.review import (
    Evidence,
    Forecast,
    InMemoryEvidence,
    InMemoryForecasts,
    build_review_card,
)
from offers.schema import Offer, OfferType
from offers.validation import InMemoryCatalog, ValidationConfig, validate_offer

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
START = NOW + timedelta(days=1)
END = NOW + timedelta(days=8)
CATALOG = InMemoryCatalog({"b1": {"p1"}})


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


# --- dates must carry a timezone ------------------------------------------------------


@pytest.mark.parametrize("field", ["start_date", "end_date"])
def test_dates_without_a_timezone_are_refused(field):
    naive = datetime(2026, 11, 2, 9, 0) if field == "start_date" else datetime(2026, 11, 9, 9, 0)
    with pytest.raises(ValidationError):
        make_offer(**{field: naive})


def test_intake_reports_naive_dates_as_an_intake_error_not_a_crash():
    raw = dict(
        branch_id="b1", type="discount", product_ids=["p1"], discount_percent="20",
        start_date=datetime(2026, 11, 2, 9, 0), end_date=datetime(2026, 11, 9, 9, 0),
        evidence_ref="sig-1", forecast_ref="fc-1",
    )
    with pytest.raises(IntakeError):
        intake_candidate(raw, InMemoryOfferRepository())


def test_dates_in_another_timezone_are_accepted():
    cairo = timezone(timedelta(hours=2))
    offer = make_offer(start_date=datetime(2026, 11, 2, 11, 0, tzinfo=cairo), end_date=datetime(2026, 11, 9, 11, 0, tzinfo=cairo))
    assert validate_offer(offer, CATALOG, ValidationConfig(), NOW).ok


# --- the hash does not depend on how a value is written --------------------------------------


def same_offer(**over):
    """Two Offer objects that are the same offer (same id and version)."""
    a = make_offer(offer_id="fixed-id", **{k: v[0] for k, v in over.items()})
    b = make_offer(offer_id="fixed-id", **{k: v[1] for k, v in over.items()})
    return a, b


@pytest.mark.parametrize("spelling", ["20", "20.0", "20.00", "2E+1"])
def test_hash_ignores_how_a_decimal_is_written(spelling):
    a = make_offer(offer_id="x", discount_percent=Decimal("20"))
    b = make_offer(offer_id="x", discount_percent=Decimal(spelling))
    assert a.content_hash() == b.content_hash()
    assert approval_hash_for(a) == approval_hash_for(b)


def test_hash_ignores_the_timezone_used_to_write_the_same_instant():
    cairo = timezone(timedelta(hours=2))
    a = make_offer(offer_id="x")
    b = make_offer(offer_id="x", start_date=START.astimezone(cairo), end_date=END.astimezone(cairo))
    assert a.content_hash() == b.content_hash()


def test_hash_still_changes_when_the_value_really_changes():
    a = make_offer(offer_id="x", discount_percent=Decimal("20"))
    assert a.content_hash() != make_offer(offer_id="x", discount_percent=Decimal("15")).content_hash()
    assert a.content_hash() != make_offer(offer_id="x", quantity_cap=101).content_hash()
    assert a.content_hash() != make_offer(offer_id="x", end_date=END + timedelta(seconds=1)).content_hash()


# --- boundaries that had no test ---------------------------------------------------------------------


def prices(cost):
    return InMemoryPrices({("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal(cost))})


def test_margin_exactly_at_the_minimum_is_allowed():
    # 20% off -> price 80, cost 72 -> margin exactly 10%
    result = check_guardrails(make_offer(), prices("72"), GuardrailConfig(min_margin_percent=Decimal("10")))
    assert result.ok, result.reasons


def test_margin_just_below_the_minimum_is_refused():
    # cost 73 -> margin 8.75%
    result = check_guardrails(make_offer(), prices("73"), GuardrailConfig(min_margin_percent=Decimal("10")))
    assert not result.ok and any("margin" in r for r in result.reasons)


def test_start_date_exactly_at_the_grace_limit_is_allowed_and_one_second_later_is_not():
    config = ValidationConfig(start_grace_minutes=5)
    ok = make_offer(start_date=NOW - timedelta(minutes=5), end_date=NOW + timedelta(days=3))
    late = make_offer(start_date=NOW - timedelta(minutes=5, seconds=1), end_date=NOW + timedelta(days=3))
    assert validate_offer(ok, CATALOG, config, NOW).ok
    result = validate_offer(late, CATALOG, config, NOW)
    assert not result.ok and any("past" in r for r in result.reasons)


def test_a_card_with_the_right_version_but_a_different_hash_is_refused():
    price_table = prices("60")
    card, pending = build_review_card(
        make_offer(),
        catalog=CATALOG,
        prices=price_table,
        rules=InMemoryProductRules(),
        active=InMemoryActiveOffers(),
        evidence_reader=InMemoryEvidence({"sig-1": Evidence("sig-1", "stock_signal", {})}),
        forecast_reader=InMemoryForecasts({"fc-1": Forecast("fc-1", Decimal("80"), NOW)}),
        validation_config=ValidationConfig(),
        guardrail_config=GuardrailConfig(),
        now=NOW,
    )
    tampered = dataclasses.replace(card, content_hash="0" * 64)  # same id, same version
    with pytest.raises(DecisionError):
        decide(pending, tampered, Decision.APPROVE, "sara", None, InMemoryDecisionStore(), NOW)
