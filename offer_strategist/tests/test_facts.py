from datetime import timedelta
from decimal import Decimal

import pytest

from offers.guardrails import (
    GuardrailConfig,
    InMemoryActiveOffers,
    InMemoryProductRules,
    PriceInfo,
    check_guardrails,
)
from offers.schema import Offer, OfferStatus, OfferType
from offer_strategist.facts import (
    FactsUnavailable,
    StrategistConfig,
    buy2get1_allowed,
    bundle_price_range,
    gather_facts,
    max_discount_percent,
)
from offer_strategist.tests.world import NOW, make_deps, make_request


def facts_for(request, deps):
    return gather_facts(
        request,
        catalog=deps.catalog,
        prices=deps.prices,
        rules=deps.rules,
        active=deps.active,
        evidence_reader=deps.evidence_reader,
        forecast_reader=deps.forecast_reader,
        repo=deps.repo,
        validation_config=deps.validation_config,
        guardrail_config=deps.guardrail_config,
        config=deps.config,
        now=NOW,
    )


# --- the limits handed to the designer are exact --------------------------------------


@pytest.mark.parametrize(
    "info, expected",
    [
        (PriceInfo(price=Decimal("100"), cost=Decimal("60")), Decimal("33.33")),  # margin binds
        (PriceInfo(price=Decimal("40"), cost=Decimal("10")), Decimal("50.00")),   # policy max binds
        (PriceInfo(price=Decimal("100"), cost=Decimal("50"), floor_price=Decimal("80")), Decimal("20.00")),  # floor binds
        (PriceInfo(price=Decimal("100"), cost=Decimal("95")), Decimal("0.00")),  # no room at all
    ],
)
def test_max_discount_ceiling(info, expected):
    assert max_discount_percent(info, GuardrailConfig()) == expected


@pytest.mark.parametrize(
    "info",
    [
        PriceInfo(price=Decimal("100"), cost=Decimal("60")),
        PriceInfo(price=Decimal("40"), cost=Decimal("10")),
        PriceInfo(price=Decimal("100"), cost=Decimal("50"), floor_price=Decimal("80")),
        PriceInfo(price=Decimal("37.50"), cost=Decimal("21.10")),
    ],
)
def test_ceiling_passes_the_real_guardrails_and_one_cent_more_fails(info):
    from offers.guardrails import InMemoryPrices

    config = GuardrailConfig()
    prices = InMemoryPrices({("1", "p"): info})
    ceiling = max_discount_percent(info, config)

    def offer(pct):
        return Offer(
            branch_id="1", type=OfferType.DISCOUNT, product_ids=["p"], discount_percent=pct,
            start_date=NOW + timedelta(days=1), end_date=NOW + timedelta(days=5), quantity_cap=10,
        )

    assert check_guardrails(offer(ceiling), prices, config).ok
    assert not check_guardrails(offer(ceiling + Decimal("0.01")), prices, config).ok


def test_buy2get1_allowed_only_when_two_thirds_price_keeps_the_margin():
    assert buy2get1_allowed(PriceInfo(price=Decimal("40"), cost=Decimal("10")), GuardrailConfig())
    assert not buy2get1_allowed(PriceInfo(price=Decimal("50"), cost=Decimal("35")), GuardrailConfig())
    # the policy max discount is below 33.33%
    assert not buy2get1_allowed(
        PriceInfo(price=Decimal("40"), cost=Decimal("10")), GuardrailConfig(max_discount_percent=Decimal("30"))
    )


def test_bundle_price_range():
    low, high = bundle_price_range(
        PriceInfo(price=Decimal("100"), cost=Decimal("60")),
        PriceInfo(price=Decimal("40"), cost=Decimal("10")),
        GuardrailConfig(),
    )
    assert (low, high) == (Decimal("77.78"), Decimal("140"))  # 70 / 0.9 rounded up


# --- gathering ------------------------------------------------------------------------------


def test_facts_carry_stored_numbers_and_limits():
    facts = facts_for(make_request(bundle_partner_ids=("102", "103", "999")), make_deps(llm=None))
    assert facts["list_price"] == "100" and facts["unit_cost"] == "60"
    assert facts["forecast_units"] == "60"
    assert facts["days_to_expiry"] == 10
    limits = facts["limits"]
    assert limits["max_discount_percent_allowed"] == "33.33"
    assert limits["max_quantity_cap"] == 120
    assert limits["max_duration_days"] == 9  # 10 days to expiry minus the 12 h lead time
    assert [p["product_id"] for p in facts["bundle_partners"]] == ["102"]
    dropped = " ".join(facts["dropped_bundle_partners"])
    assert "103" in dropped and "999" in dropped


def reasons_for(request, deps):
    with pytest.raises(FactsUnavailable) as err:
        facts_for(request, deps)
    return " | ".join(err.value.reasons)


def test_missing_forecast_and_evidence_fail_closed():
    text = reasons_for(make_request(forecast_ref="fc-x", evidence_ref="ev-x"), make_deps(llm=None))
    assert "forecast not found" in text and "evidence not found" in text


def test_too_close_to_expiry_is_skipped():
    assert "shelf life" in reasons_for(make_request("103"), make_deps(llm=None))


def test_product_with_an_approved_offer_is_skipped():
    other = Offer(
        branch_id="1", type=OfferType.DISCOUNT, product_ids=["101"], discount_percent=Decimal("10"),
        start_date=NOW - timedelta(days=1), end_date=NOW + timedelta(days=3), quantity_cap=10,
        status=OfferStatus.APPROVED,
    )
    deps = make_deps(llm=None, active=InMemoryActiveOffers([other]))
    assert "already has offer" in reasons_for(make_request(), deps)


def test_restricted_product_and_empty_stock_are_skipped():
    rules = InMemoryProductRules(restricted={("1", "101")})
    text = reasons_for(make_request(stock_on_hand=0), make_deps(llm=None, rules=rules))
    assert "restricted" in text and "no stock" in text


def test_unknown_product_is_skipped():
    assert "does not belong" in reasons_for(make_request("555"), make_deps(llm=None))


def test_config_rejects_bad_round_limits():
    with pytest.raises(ValueError):
        StrategistConfig(max_rounds=0)
    with pytest.raises(ValueError):
        StrategistConfig(min_lead_hours=48, max_start_hours=24)
