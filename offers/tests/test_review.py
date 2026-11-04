import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

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
    ReviewNotAllowed,
    build_review_card,
)
from offers.schema import BundleItem, Offer, OfferStatus, OfferType
from offers.validation import InMemoryCatalog, ValidationConfig

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
START = NOW + timedelta(days=1)
END = NOW + timedelta(days=8)

CATALOG = InMemoryCatalog({"b1": {"p1", "p2"}})
PRICES = InMemoryPrices(
    {
        ("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("60")),
        ("b1", "p2"): PriceInfo(price=Decimal("100"), cost=Decimal("10")),
    }
)
EVIDENCE = InMemoryEvidence(
    {"sig-1": Evidence(ref="sig-1", kind="stock_signal", facts={"stock_on_hand": 120, "days_to_expiry": 9})}
)
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


def build(offer, *, prices=PRICES, rules=None, active=None, evidence=EVIDENCE, forecasts=FORECASTS):
    return build_review_card(
        offer,
        catalog=CATALOG,
        prices=prices,
        rules=rules or InMemoryProductRules(),
        active=active or InMemoryActiveOffers(),
        evidence_reader=evidence,
        forecast_reader=forecasts,
        validation_config=ValidationConfig(),
        guardrail_config=GuardrailConfig(),
        now=NOW,
    )


def test_card_has_every_section():
    card, _ = build(make_offer())
    d = card.to_dict()
    for key in ("offer_id", "version", "content_hash", "details", "evidence", "forecast", "lines", "flags"):
        assert key in d
    assert d["version"] == 1


def test_figures_come_from_stored_data():
    card, _ = build(make_offer())
    line = card.lines[0]
    assert line["list_price"] == Decimal("100")
    assert line["offer_price"] == Decimal("80.00")
    assert line["cost"] == Decimal("60")
    assert line["discount_percent"] == Decimal("20.00")
    assert line["margin_percent"] == Decimal("25.00")  # (80-60)/80
    assert card.forecast["expected_units"] == Decimal("80")
    assert card.evidence["facts"] == {"stock_on_hand": 120, "days_to_expiry": 9}


def test_changing_stored_prices_changes_the_card():
    other = InMemoryPrices({("b1", "p1"): PriceInfo(price=Decimal("200"), cost=Decimal("60"))})
    card, _ = build(make_offer(), prices=other)
    line = card.lines[0]
    assert line["list_price"] == Decimal("200")
    assert line["offer_price"] == Decimal("160.00")
    assert line["margin_percent"] == Decimal("62.50")  # (160-60)/160


def test_hash_matches_offer_and_status_moves_to_pending():
    offer = make_offer()
    card, pending = build(offer)
    assert card.content_hash == offer.content_hash() == pending.content_hash()
    assert pending.status == OfferStatus.PENDING_APPROVAL
    assert offer.status == OfferStatus.DRAFT  # original untouched
    assert card.status == "pending_approval"


def test_new_version_gets_a_different_hash_on_its_card():
    v1 = make_offer()
    card1, _ = build(v1)
    v2 = v1.new_version(discount_percent=Decimal("15"))
    card2, _ = build(v2)
    assert card2.version == 2
    assert card2.content_hash != card1.content_hash


def test_cap_above_forecast_is_flagged():
    card, _ = build(make_offer(quantity_cap=100))  # forecast is 80
    assert card.flags["quantity_cap_above_forecast"] is True
    assert card.flags["cap_to_forecast_ratio"] == Decimal("1.25")


def test_cap_below_forecast_is_not_flagged():
    card, _ = build(make_offer(quantity_cap=40))
    assert card.flags["quantity_cap_above_forecast"] is False
    assert card.flags["cap_to_forecast_ratio"] == Decimal("0.50")


def test_days_before_expiry_comes_from_stored_expiry():
    rules = InMemoryProductRules(expiry={("b1", "p1"): END + timedelta(days=3)})
    card, _ = build(make_offer(), rules=rules)
    assert card.flags["days_between_offer_end_and_expiry"] == 3


def test_bundle_card_has_one_line_for_the_whole_bundle():
    bundle = make_offer(
        type=OfferType.BUNDLE,
        product_ids=["p1", "p2"],
        discount_percent=None,
        bundle_items=[BundleItem(product_id="p1", quantity=1), BundleItem(product_id="p2", quantity=1)],
        bundle_price=Decimal("150"),
    )
    card, _ = build(bundle)
    assert len(card.lines) == 1
    assert card.lines[0]["list_price"] == Decimal("200")
    assert card.lines[0]["margin_percent"] == Decimal("53.33")  # (150-70)/150
    assert card.details["terms"]["bundle_price"] == Decimal("150")


def test_guardrail_violation_blocks_the_card():
    with pytest.raises(ReviewNotAllowed) as exc:
        build(make_offer(discount_percent=Decimal("40")))  # margin 0%
    assert any("margin" in r for r in exc.value.reasons)


def test_validation_failure_blocks_the_card():
    with pytest.raises(ReviewNotAllowed) as exc:
        build(make_offer(branch_id="nope"))
    assert any("unknown branch" in r for r in exc.value.reasons)


def test_missing_stored_evidence_blocks_the_card():
    with pytest.raises(ReviewNotAllowed) as exc:
        build(make_offer(), evidence=InMemoryEvidence())
    assert any("evidence" in r for r in exc.value.reasons)


def test_missing_stored_forecast_blocks_the_card():
    with pytest.raises(ReviewNotAllowed) as exc:
        build(make_offer(), forecasts=InMemoryForecasts())
    assert any("forecast" in r for r in exc.value.reasons)


def test_missing_forecast_ref_blocks_the_card():
    with pytest.raises(ReviewNotAllowed):
        build(make_offer(forecast_ref=None))


@pytest.mark.parametrize("status", [OfferStatus.APPROVED, OfferStatus.REJECTED, OfferStatus.ACTIVE])
def test_only_drafts_can_go_to_review(status):
    with pytest.raises(ReviewNotAllowed):
        build(make_offer(status=status))


def test_card_is_json_serializable_and_deterministic():
    offer = make_offer()
    first, _ = build(offer)
    second, _ = build(offer)
    assert first.to_dict() == second.to_dict()
    json.dumps(first.to_dict())  # must not raise


def test_text_view_uses_only_stored_values():
    card, _ = build(make_offer())
    text = card.render_text()
    assert card.offer_id in text
    assert "margin 25.00%" in text
    assert "Forecast fc-1: 80 units" in text