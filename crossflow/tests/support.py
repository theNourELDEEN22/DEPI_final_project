"""Shared fixtures: a tiny branch "1" with two products and an approved offer."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from offers.decisions import Decision, InMemoryDecisionStore, decide
from offers.guardrails import (
    GuardrailConfig,
    InMemoryActiveOffers,
    InMemoryPrices,
    InMemoryProductRules,
    PriceInfo,
)
from offers.pos_activation import activate_on_pos
from offers.review import Evidence, Forecast, InMemoryEvidence, InMemoryForecasts, build_review_card
from offers.schema import Offer, OfferType
from offers.validation import InMemoryCatalog, RejectionLog, ValidationConfig

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
START = NOW + timedelta(days=1)
END = NOW + timedelta(days=8)

CATALOG = InMemoryCatalog({"1": {"101", "102"}})
PRICES = InMemoryPrices({
    ("1", "101"): PriceInfo(price=Decimal("100"), cost=Decimal("60")),
    ("1", "102"): PriceInfo(price=Decimal("40"), cost=Decimal("10")),
})
EVIDENCE = InMemoryEvidence({"sig-1": Evidence("sig-1", "stock_signal", {"on_hand": 200})})
FORECASTS = InMemoryForecasts({"fc-1": Forecast("fc-1", Decimal("80"), NOW)})


def make_offer(**over) -> Offer:
    base = dict(branch_id="1", type=OfferType.DISCOUNT, product_ids=["101"], start_date=START, end_date=END,
                discount_percent=Decimal("20"), quantity_cap=100, evidence_ref="sig-1", forecast_ref="fc-1")
    base.update(over)
    return Offer(**base)


def approved(**over):
    card, pending = build_review_card(
        make_offer(**over), catalog=CATALOG, prices=PRICES, rules=InMemoryProductRules(),
        active=InMemoryActiveOffers(), evidence_reader=EVIDENCE, forecast_reader=FORECASTS,
        validation_config=ValidationConfig(), guardrail_config=GuardrailConfig(), now=NOW,
    )
    store = InMemoryDecisionStore()
    offer, _ = decide(pending, card, Decision.APPROVE, "sales.maadi", None, store, NOW)
    return offer, store


def activate(offer, store, pos, now=None):
    return activate_on_pos(
        offer, pos=pos, store=store, catalog=CATALOG, prices=PRICES, rules=InMemoryProductRules(),
        active=InMemoryActiveOffers(), validation_config=ValidationConfig(), guardrail_config=GuardrailConfig(),
        log=RejectionLog(), now=now or START - timedelta(hours=4),
    )
