"""AI Offer Strategist - the facts the agents work from.

Every number the agents see is read from stored data (catalog, prices, product
rules, forecasts, stock signals) or computed from it here with Decimal. The
agents only READ facts; they never write them. This keeps the project rule from
D6: figures come from stored data, not AI text.

The pre-checks here run BEFORE any model call, so a product that cannot get an
offer anyway (no price, no forecast, already promoted, too close to expiry)
costs no tokens.

The limits handed to the designer agent are exact: `max_discount_percent_allowed`
already includes the margin and price-floor guardrails (D4), so a proposal at
that ceiling passes and one cent above it fails.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from offers.guardrails import (
    ActiveOffers,
    GuardrailConfig,
    PriceInfo,
    PriceReader,
    ProductRules,
    _all_product_ids,
)
from offers.intake import candidate_key
from offers.review import EvidenceReader, ForecastReader, _jsonable
from offers.schema import OfferStatus, OfferType
from offers.validation import CatalogReader, ValidationConfig

TWO_PLACES = Decimal("0.01")
ONE_DAY = timedelta(days=1)
INFINITY = Decimal("Infinity")


class StrategistRequest(BaseModel):
    """One product to plan an offer for, from a stock signal (H1) or a person."""

    model_config = ConfigDict(frozen=True)

    branch_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    product_name: str = ""
    category: str = ""
    demand_level: str = "medium"
    stock_on_hand: int = Field(ge=0)
    expiring_units: int = Field(default=0, ge=0)
    evidence_ref: str = Field(min_length=1)
    forecast_ref: str = Field(min_length=1)
    bundle_partner_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategistConfig:
    # Placeholders until the Week 1 decisions meeting (E1), like the other configs.
    max_rounds: int = 3        # designer <-> critic rounds before escalating to a person
    min_lead_hours: int = 12   # Sales needs time to review before the offer starts
    max_start_hours: int = 72  # an offer planned today starts within 3 days

    def __post_init__(self) -> None:
        if not 1 <= self.max_rounds <= 5:
            raise ValueError("max_rounds must be between 1 and 5")
        if self.min_lead_hours < 0 or self.max_start_hours < self.min_lead_hours:
            raise ValueError("need 0 <= min_lead_hours <= max_start_hours")


class FactsUnavailable(ValueError):
    """The product cannot be planned. `reasons` says why."""

    def __init__(self, reasons) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


# --- exact limits derived from the D4 guardrails ------------------------------------


def _min_unit_price(info: PriceInfo, config: GuardrailConfig) -> Decimal:
    """Lowest unit price that still passes the margin and price-floor guardrails."""
    margin = config.min_margin_percent / Decimal(100)
    by_margin = info.cost / (Decimal(1) - margin) if margin < 1 else INFINITY
    return max(by_margin, info.effective_floor)


def max_discount_percent(info: PriceInfo, config: GuardrailConfig) -> Decimal:
    """Deepest discount (rounded DOWN to 0.01) that passes max discount, margin and floor."""
    if info.price <= 0:
        return Decimal(0)
    ceiling = (Decimal(1) - _min_unit_price(info, config) / info.price) * Decimal(100)
    ceiling = min(ceiling, config.max_discount_percent)
    return max(Decimal(0), ceiling).quantize(TWO_PLACES, rounding=ROUND_DOWN)


def buy2get1_allowed(info: PriceInfo, config: GuardrailConfig) -> bool:
    """Buy 2 get 1 is a 33.33% effective discount (average unit price = 2/3)."""
    if info.price <= 0:
        return False
    return (
        Decimal(100) / Decimal(3) <= config.max_discount_percent
        and info.price * Decimal(2) / Decimal(3) >= _min_unit_price(info, config)
    )


def bundle_price_range(
    main: PriceInfo, partner: PriceInfo, config: GuardrailConfig
) -> tuple[Decimal, Decimal]:
    """(lowest bundle price that passes D4, the two list prices added up)."""
    list_price = main.price + partner.price
    cost = main.cost + partner.cost
    floor = main.effective_floor + partner.effective_floor
    margin = config.min_margin_percent / Decimal(100)
    by_margin = cost / (Decimal(1) - margin) if margin < 1 else INFINITY
    by_discount = list_price * (Decimal(1) - config.max_discount_percent / Decimal(100))
    low = max(by_margin, floor, by_discount)
    if low.is_finite():
        low = low.quantize(TWO_PLACES, rounding=ROUND_UP)
    return low, list_price


def max_duration_days(
    expiry: Optional[datetime],
    now: datetime,
    config: StrategistConfig,
    validation: ValidationConfig,
) -> int:
    """Longest offer (whole days) that starts after the lead time and ends by the expiry."""
    if expiry is None:
        return validation.max_duration_days
    earliest_start = now + timedelta(hours=config.min_lead_hours)
    return max(0, min(validation.max_duration_days, (expiry - earliest_start) // ONE_DAY))


# --- gathering ---------------------------------------------------------------------------


def gather_facts(
    request: StrategistRequest,
    *,
    catalog: CatalogReader,
    prices: PriceReader,
    rules: ProductRules,
    active: ActiveOffers,
    evidence_reader: EvidenceReader,
    forecast_reader: ForecastReader,
    repo,  # offers.intake.InMemoryOfferRepository (anything with find_by_candidate_key)
    validation_config: ValidationConfig,
    guardrail_config: GuardrailConfig,
    config: StrategistConfig,
    now: datetime,
) -> dict[str, Any]:
    """Return JSON-safe facts for the agents, or raise FactsUnavailable (fail closed)."""
    branch, product = request.branch_id, request.product_id

    if not catalog.branch_exists(branch):
        raise FactsUnavailable([f"unknown branch: {branch}"])
    if not catalog.product_in_branch(branch, product):
        raise FactsUnavailable([f"product {product} does not belong to branch {branch}"])

    reasons: list[str] = []
    if request.stock_on_hand <= 0:
        reasons.append("no stock on hand, nothing to clear")

    info = prices.get_price_info(branch, product)
    if info is None:
        reasons.append(f"no price/cost data for product {product}")
    elif info.price <= 0:
        reasons.append(f"product {product} has an invalid list price")

    if rules.is_restricted(branch, product):
        reasons.append(f"product {product} is restricted from promotions")

    evidence = evidence_reader.get_evidence(request.evidence_ref)
    if evidence is None:
        reasons.append(f"stored evidence not found: {request.evidence_ref}")
    forecast = forecast_reader.get_forecast(request.forecast_ref)
    if forecast is None:
        reasons.append(f"stored forecast not found: {request.forecast_ref}")

    for other in active.offers_in_branch(branch):
        if (
            other.status in (OfferStatus.APPROVED, OfferStatus.ACTIVE)
            and other.end_date > now
            and product in _all_product_ids(other)
        ):
            reasons.append(f"product {product} already has offer {other.offer_id} ({other.status.value})")

    for offer_type in OfferType:
        key = candidate_key(
            {"branch_id": branch, "evidence_ref": request.evidence_ref, "type": offer_type.value}
        )
        existing = repo.find_by_candidate_key(key)
        if existing is not None:
            reasons.append(
                f"this stock signal already has a {offer_type.value} offer candidate: {existing.offer_id}"
            )

    expiry = rules.earliest_expiry(branch, product)
    max_days = max_duration_days(expiry, now, config, validation_config)
    if max_days < validation_config.min_duration_days:
        reasons.append("not enough shelf life left for the shortest allowed offer")

    if reasons:
        raise FactsUnavailable(reasons)

    partners, dropped = _bundle_partners(
        request, info, catalog, prices, rules, guardrail_config, config, validation_config, now, max_days
    )

    return _jsonable(
        {
            "as_of": now,
            "branch_id": branch,
            "product_id": product,
            "product_name": request.product_name,
            "category": request.category,
            "demand_level": request.demand_level.lower(),
            "stock_on_hand": request.stock_on_hand,
            "expiring_units": request.expiring_units,
            "list_price": info.price,
            "unit_cost": info.cost,
            "price_floor": info.effective_floor,
            "earliest_expiry": expiry,
            "days_to_expiry": (expiry - now).days if expiry is not None else None,
            "forecast_ref": forecast.ref,
            "forecast_units": forecast.expected_units,
            "evidence_ref": evidence.ref,
            "evidence_kind": evidence.kind,
            "evidence_facts": dict(evidence.facts),
            "limits": {
                "max_discount_percent_allowed": max_discount_percent(info, guardrail_config),
                "policy_max_discount_percent": guardrail_config.max_discount_percent,
                "min_margin_percent": guardrail_config.min_margin_percent,
                "buy2get1_allowed": buy2get1_allowed(info, guardrail_config),
                "max_quantity_cap": min(request.stock_on_hand, guardrail_config.max_quantity_cap),
                "min_duration_days": validation_config.min_duration_days,
                "max_duration_days": max_days,
                "min_start_in_hours": config.min_lead_hours,
                "max_start_in_hours": config.max_start_hours,
            },
            "bundle_partners": partners,
            "dropped_bundle_partners": dropped,
        }
    )


def _bundle_partners(
    request, info, catalog, prices, rules, guardrail_config, config, validation_config, now, max_days
) -> tuple[list[dict], list[str]]:
    partners: list[dict] = []
    dropped: list[str] = []
    branch = request.branch_id
    for pid in dict.fromkeys(request.bundle_partner_ids):  # keep order, drop repeats
        if pid == request.product_id:
            continue
        if not catalog.product_in_branch(branch, pid):
            dropped.append(f"{pid}: not in branch {branch}")
            continue
        partner = prices.get_price_info(branch, pid)
        if partner is None or partner.price <= 0:
            dropped.append(f"{pid}: no usable price/cost data")
            continue
        if rules.is_restricted(branch, pid):
            dropped.append(f"{pid}: restricted from promotions")
            continue
        low, high = bundle_price_range(info, partner, guardrail_config)
        days = min(
            max_days,
            max_duration_days(rules.earliest_expiry(branch, pid), now, config, validation_config),
        )
        if not low.is_finite() or low > high:
            dropped.append(f"{pid}: no bundle price passes the guardrails")
            continue
        if days < validation_config.min_duration_days:
            dropped.append(f"{pid}: not enough shelf life left")
            continue
        partners.append(
            {
                "product_id": pid,
                "list_price": partner.price,
                "unit_cost": partner.cost,
                "min_bundle_price": low,
                "max_bundle_price": high,
                "max_duration_days": days,
            }
        )
    return partners, dropped
