from datetime import datetime, timedelta, timezone
from decimal import Decimal

from offers.guardrails import (
    GuardrailConfig,
    InMemoryPrices,
    PriceInfo,
    check_guardrails,
    run_guardrails,
)
from offers.schema import BundleItem, Offer, OfferStatus, OfferType
from offers.validation import RejectionLog

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
CONFIG = GuardrailConfig(max_discount_percent=Decimal("50"), min_margin_percent=Decimal("10"))

PRICES = InMemoryPrices(
    {
        ("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("60")),
        ("b1", "p2"): PriceInfo(price=Decimal("100"), cost=Decimal("10")),
        ("b1", "p3"): PriceInfo(price=Decimal("100"), cost=Decimal("10"), floor_price=Decimal("70")),
        ("b1", "p4"): PriceInfo(price=Decimal("100"), cost=Decimal("65")),
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
        evidence_ref="sig-1",
        forecast_ref="fc-1",
    )
    base.update(over)
    return Offer(**base)


def make_bundle(price: str) -> Offer:
    return make_offer(
        type=OfferType.BUNDLE,
        product_ids=["p1", "p2"],
        discount_percent=None,
        bundle_items=[BundleItem(product_id="p1", quantity=1), BundleItem(product_id="p2", quantity=1)],
        bundle_price=Decimal(price),
    )


def test_valid_discount_passes():
    result = check_guardrails(make_offer(), PRICES, CONFIG)
    assert result.ok and result.reasons == ()


def test_discount_above_max_rejected():
    # p2: cost is low, so only the max-discount rule fires
    result = check_guardrails(
        make_offer(product_ids=["p2"], discount_percent=Decimal("60")), PRICES, CONFIG
    )
    assert not result.ok
    assert len(result.reasons) == 1 and "discount" in result.reasons[0]


def test_discount_exactly_at_max_passes():
    result = check_guardrails(
        make_offer(product_ids=["p2"], discount_percent=Decimal("50")), PRICES, CONFIG
    )
    assert result.ok


def test_margin_too_low_rejected():
    # p1: 40% off -> price 60 = cost, margin 0%; discount and floor are fine
    result = check_guardrails(make_offer(discount_percent=Decimal("40")), PRICES, CONFIG)
    assert not result.ok
    assert len(result.reasons) == 1 and "margin" in result.reasons[0]


def test_below_price_floor_rejected():
    # p3: 40% off -> price 60, floor is 70; discount and margin are fine
    result = check_guardrails(
        make_offer(product_ids=["p3"], discount_percent=Decimal("40")), PRICES, CONFIG
    )
    assert not result.ok
    assert len(result.reasons) == 1 and "floor" in result.reasons[0]


def test_floor_falls_back_to_cost_when_not_set():
    # p1 has no floor_price: 45% off -> price 55, below its cost 60
    result = check_guardrails(make_offer(discount_percent=Decimal("45")), PRICES, CONFIG)
    assert any("floor" in r for r in result.reasons)


def test_buy2get1_passes():
    offer = make_offer(
        type=OfferType.BUY2GET1, product_ids=["p2"], discount_percent=None, buy_qty=2, get_qty=1
    )
    assert check_guardrails(offer, PRICES, CONFIG).ok


def test_buy2get1_margin_too_low_rejected():
    # p4: average price 66.67 vs cost 65 -> margin about 2.5%
    offer = make_offer(
        type=OfferType.BUY2GET1, product_ids=["p4"], discount_percent=None, buy_qty=2, get_qty=1
    )
    result = check_guardrails(offer, PRICES, CONFIG)
    assert not result.ok
    assert len(result.reasons) == 1 and "margin" in result.reasons[0]


def test_bundle_passes():
    # list 200, cost 70, price 150 -> 25% off, healthy margin
    assert check_guardrails(make_bundle("150"), PRICES, CONFIG).ok


def test_bundle_discount_too_deep_rejected():
    # list 200, price 80 -> 60% off; margin and floor still fine
    result = check_guardrails(make_bundle("80"), PRICES, CONFIG)
    assert not result.ok
    assert len(result.reasons) == 1 and "discount" in result.reasons[0]


def test_missing_price_data_rejected():
    result = check_guardrails(make_offer(product_ids=["p404"]), PRICES, CONFIG)
    assert not result.ok
    assert "no price/cost data" in result.reasons[0]


def test_violation_marks_rejected_and_logs_reasons():
    log = RejectionLog()
    offer, result = run_guardrails(
        make_offer(discount_percent=Decimal("40")), PRICES, CONFIG, log, NOW
    )
    assert not result.ok
    assert offer.status == OfferStatus.REJECTED
    assert len(log.entries) == 1
    assert log.entries[0]["stage"] == "guardrails"
    assert log.entries[0]["reasons"]


def test_passing_offer_is_not_logged():
    log = RejectionLog()
    offer, result = run_guardrails(make_offer(), PRICES, CONFIG, log, NOW)
    assert result.ok
    assert offer.status == OfferStatus.DRAFT
    assert log.entries == []