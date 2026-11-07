from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from offers.schema import BundleItem, Offer, OfferType

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
COMMON = dict(
    branch_id="b1",
    product_ids=["p1"],
    start_date=NOW,
    end_date=NOW + timedelta(days=7),
    evidence_ref="sig-1",
    forecast_ref="fc-1",
)


def test_discount_offer_unused_fields_are_null():
    o = Offer(type=OfferType.DISCOUNT, discount_percent=Decimal("20"), **COMMON)
    assert o.buy_qty is None and o.get_qty is None
    assert o.bundle_items is None and o.bundle_price is None


def test_buy2get1_offer_unused_fields_are_null():
    o = Offer(type=OfferType.BUY2GET1, buy_qty=2, get_qty=1, **COMMON)
    assert o.discount_percent is None
    assert o.bundle_items is None and o.bundle_price is None


def test_bundle_offer_unused_fields_are_null():
    o = Offer(
        type=OfferType.BUNDLE,
        bundle_items=[BundleItem(product_id="p1", quantity=1),
                      BundleItem(product_id="p2", quantity=1)],
        bundle_price=Decimal("99.50"),
        **COMMON,
    )
    assert o.discount_percent is None and o.buy_qty is None and o.get_qty is None


def test_unused_field_rejected():
    with pytest.raises(ValidationError):
        Offer(type=OfferType.DISCOUNT, discount_percent=Decimal("10"), buy_qty=2, **COMMON)


def test_missing_required_field_rejected():
    with pytest.raises(ValidationError):
        Offer(type=OfferType.DISCOUNT, **COMMON)


def test_bad_dates_rejected():
    bad = {**COMMON, "end_date": NOW - timedelta(days=1)}
    with pytest.raises(ValidationError):
        Offer(type=OfferType.DISCOUNT, discount_percent=Decimal("10"), **bad)


def test_new_version_keeps_id_and_invalidates_old_hash():
    v1 = Offer(type=OfferType.DISCOUNT, discount_percent=Decimal("20"), **COMMON)
    v2 = v1.new_version(discount_percent=Decimal("15"))
    assert v2.offer_id == v1.offer_id
    assert v2.version == 2
    assert v2.approval_hash is None
    assert v2.content_hash() != v1.content_hash()