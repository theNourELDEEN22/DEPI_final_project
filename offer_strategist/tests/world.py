"""Shared test data: branch "1" with three products.

101  price 100, cost 60, expires in 10 days  -> discount ceiling 33.33%
102  price  40, cost 10, no expiry           -> discount ceiling 50% (policy max)
103  price  50, cost 35, expires in 1 day    -> too close to expiry for any offer
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from offers.guardrails import InMemoryActiveOffers, InMemoryPrices, InMemoryProductRules, PriceInfo
from offers.intake import InMemoryOfferRepository
from offers.review import Evidence, Forecast, InMemoryEvidence, InMemoryForecasts
from offers.validation import InMemoryCatalog, RejectionLog
from offer_strategist.facts import StrategistConfig, StrategistRequest
from offer_strategist.graph import StrategistDeps

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
PRODUCTS = ("101", "102", "103")


def make_request(pid: str = "101", **over) -> StrategistRequest:
    base = dict(
        branch_id="1",
        product_id=pid,
        product_name=f"Product {pid}",
        category="Dairy",
        demand_level="medium",
        stock_on_hand=120,
        expiring_units=40,
        evidence_ref=f"ev-{pid}",
        forecast_ref=f"fc-{pid}",
    )
    base.update(over)
    return StrategistRequest(**base)


def make_deps(llm, *, active=None, rules=None, config=None, repo=None) -> StrategistDeps:
    return StrategistDeps(
        llm=llm,
        catalog=InMemoryCatalog({"1": set(PRODUCTS)}),
        prices=InMemoryPrices(
            {
                ("1", "101"): PriceInfo(price=Decimal("100"), cost=Decimal("60")),
                ("1", "102"): PriceInfo(price=Decimal("40"), cost=Decimal("10")),
                ("1", "103"): PriceInfo(price=Decimal("50"), cost=Decimal("35")),
            }
        ),
        rules=rules
        or InMemoryProductRules(
            expiry={("1", "101"): NOW + timedelta(days=10), ("1", "103"): NOW + timedelta(days=1)}
        ),
        active=active or InMemoryActiveOffers(),
        evidence_reader=InMemoryEvidence(
            {f"ev-{p}": Evidence(f"ev-{p}", "stock_signal", {"on_hand": 120}) for p in PRODUCTS}
        ),
        forecast_reader=InMemoryForecasts(
            {f"fc-{p}": Forecast(f"fc-{p}", Decimal("60"), NOW) for p in PRODUCTS}
        ),
        repo=repo or InMemoryOfferRepository(),
        log=RejectionLog(),
        config=config or StrategistConfig(),
        clock=lambda: NOW,
    )
