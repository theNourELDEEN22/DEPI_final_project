from datetime import datetime, timedelta, timezone
from decimal import Decimal

from offers.schema import BundleItem, Offer, OfferStatus, OfferType
from offers.validation import (
    InMemoryCatalog,
    RejectionLog,
    ValidationConfig,
    run_validation,
    validate_offer,
)

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
CONFIG = ValidationConfig(min_duration_days=1, max_duration_days=30)
CATALOG = InMemoryCatalog({"b1": {"p1", "p2"}, "b2": {"p9"}})


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


def test_valid_offer_passes_and_is_not_logged():
    log = RejectionLog()
    offer, result = run_validation(make_offer(), CATALOG, CONFIG, log, NOW)
    assert result.ok
    assert offer.status == OfferStatus.DRAFT
    assert log.entries == []


def test_unknown_branch_rejected():
    result = validate_offer(make_offer(branch_id="nope"), CATALOG, CONFIG, NOW)
    assert not result.ok
    assert any("unknown branch" in r for r in result.reasons)


def test_product_from_another_branch_rejected():
    result = validate_offer(make_offer(product_ids=["p9"]), CATALOG, CONFIG, NOW)
    assert not result.ok
    assert any("p9" in r for r in result.reasons)


def test_bundle_item_from_another_branch_rejected():
    bundle = make_offer(
        type=OfferType.BUNDLE,
        discount_percent=None,
        bundle_items=[BundleItem(product_id="p1", quantity=1), BundleItem(product_id="p9", quantity=1)],
        bundle_price=Decimal("50"),
    )
    result = validate_offer(bundle, CATALOG, CONFIG, NOW)
    assert not result.ok
    assert any("bundle product p9" in r for r in result.reasons)


def test_start_in_the_past_rejected():
    result = validate_offer(
        make_offer(start_date=NOW - timedelta(days=2), end_date=NOW + timedelta(days=5)),
        CATALOG, CONFIG, NOW,
    )
    assert not result.ok
    assert any("past" in r for r in result.reasons)


def test_duration_too_short_rejected():
    result = validate_offer(
        make_offer(start_date=NOW + timedelta(days=1), end_date=NOW + timedelta(days=1, hours=3)),
        CATALOG, CONFIG, NOW,
    )
    assert not result.ok
    assert any("shorter" in r for r in result.reasons)


def test_duration_too_long_rejected():
    result = validate_offer(
        make_offer(start_date=NOW + timedelta(days=1), end_date=NOW + timedelta(days=60)),
        CATALOG, CONFIG, NOW,
    )
    assert not result.ok
    assert any("longer" in r for r in result.reasons)


def test_all_reasons_collected_in_one_pass():
    bad = make_offer(
        branch_id="nope",
        start_date=NOW - timedelta(days=2),
        end_date=NOW + timedelta(days=90),
    )
    result = validate_offer(bad, CATALOG, CONFIG, NOW)
    assert len(result.reasons) >= 3


def test_rejection_marks_status_and_logs_reasons():
    log = RejectionLog()
    offer, result = run_validation(make_offer(branch_id="nope"), CATALOG, CONFIG, log, NOW)
    assert not result.ok
    assert offer.status == OfferStatus.REJECTED
    assert len(log.entries) == 1
    entry = log.entries[0]
    assert entry["offer_id"] == offer.offer_id
    assert entry["stage"] == "validation"
    assert entry["reasons"]