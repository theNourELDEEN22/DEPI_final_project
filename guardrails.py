"""D4 - Guardrails: max discount, margin and price floor (Epic D: Offers).

Runs after validation (D3). D5 adds more rules to this same file
(quantity cap, expiry dates, restrictions, active-offer conflicts),
and D9 re-runs everything right before activation.

Every rule gets the same numbers for all three offer types by reducing them to
"price units": (label, list_price, offer_price, cost, floor).
  - discount : one unit per product, offer_price = price * (1 - d/100)
  - buy2get1 : one unit per product, offer_price = price * 2/3 (average unit price)
  - bundle   : one unit for the whole bundle, offer_price = bundle_price
Missing price data fails closed: no data means the offer is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Protocol

from offers.schema import Offer, OfferStatus, OfferType


@dataclass(frozen=True)
class GuardrailConfig:
    # Placeholders until the Week 1 decisions meeting (E1); later from M1's config service (A4).
    max_discount_percent: Decimal = Decimal("50")
    min_margin_percent: Decimal = Decimal("10")


@dataclass(frozen=True)
class PriceInfo:
    price: Decimal
    cost: Decimal
    floor_price: Optional[Decimal] = None  # if not set, the floor is the cost

    @property
    def effective_floor(self) -> Decimal:
        return self.floor_price if self.floor_price is not None else self.cost


class PriceReader(Protocol):
    """Read-only view of price and cost. M1's products table (A1) will implement this."""

    def get_price_info(self, branch_id: str, product_id: str) -> Optional[PriceInfo]: ...


@dataclass
class InMemoryPrices:
    data: dict[tuple[str, str], PriceInfo] = field(default_factory=dict)

    def get_price_info(self, branch_id: str, product_id: str) -> Optional[PriceInfo]:
        return self.data.get((branch_id, product_id))


@dataclass(frozen=True)
class GuardrailResult:
    ok: bool
    reasons: tuple[str, ...] = ()


def _price_units(offer: Offer, prices: PriceReader):
    """Return (units, missing_data_reasons)."""
    if offer.type == OfferType.BUNDLE:
        wanted = [(i.product_id, i.quantity) for i in (offer.bundle_items or [])]
    else:
        wanted = [(pid, 1) for pid in offer.product_ids]

    infos: dict[str, PriceInfo] = {}
    missing: list[str] = []
    for pid, _ in wanted:
        info = prices.get_price_info(offer.branch_id, pid)
        if info is None:
            missing.append(f"no price/cost data for product {pid}")
        else:
            infos[pid] = info
    if missing:
        return [], missing

    if offer.type == OfferType.BUNDLE:
        list_price = sum(infos[pid].price * q for pid, q in wanted)
        cost = sum(infos[pid].cost * q for pid, q in wanted)
        floor = sum(infos[pid].effective_floor * q for pid, q in wanted)
        return [("bundle", list_price, offer.bundle_price, cost, floor)], []

    units = []
    for pid, _ in wanted:
        info = infos[pid]
        if offer.type == OfferType.DISCOUNT:
            offer_price = info.price * (Decimal(1) - offer.discount_percent / Decimal(100))
        else:  # buy2get1: pay for 2, get 3 -> average unit price is 2/3
            offer_price = info.price * Decimal(2) / Decimal(3)
        units.append((f"product {pid}", info.price, offer_price, info.cost, info.effective_floor))
    return units, []


def check_guardrails(
    offer: Offer, prices: PriceReader, config: GuardrailConfig
) -> GuardrailResult:
    units, missing = _price_units(offer, prices)
    if missing:
        return GuardrailResult(ok=False, reasons=tuple(missing))

    reasons: list[str] = []
    for label, list_price, offer_price, cost, floor in units:
        if list_price <= 0:
            reasons.append(f"{label}: invalid list price")
            continue

        # 1) max discount
        discount_pct = (list_price - offer_price) / list_price * Decimal(100)
        if discount_pct > config.max_discount_percent:
            reasons.append(
                f"{label}: discount {discount_pct:.2f}% is above the max {config.max_discount_percent}%"
            )

        # 2) margin
        if offer_price <= 0:
            reasons.append(f"{label}: margin cannot be computed (offer price is zero)")
        else:
            margin_pct = (offer_price - cost) / offer_price * Decimal(100)
            if margin_pct < config.min_margin_percent:
                reasons.append(
                    f"{label}: margin {margin_pct:.2f}% is below the minimum {config.min_margin_percent}%"
                )

        # 3) price floor
        if offer_price < floor:
            reasons.append(
                f"{label}: price {offer_price:.2f} is below the floor {floor:.2f}"
            )

    return GuardrailResult(ok=not reasons, reasons=tuple(reasons))


def run_guardrails(
    offer: Offer,
    prices: PriceReader,
    config: GuardrailConfig,
    log,  # offers.validation.RejectionLog (or anything with an .entries list)
    now: datetime | None = None,
) -> tuple[Offer, GuardrailResult]:
    """Check; on violation return the offer as REJECTED and log the reasons.
    On success the offer is returned unchanged."""
    now = now or datetime.now(timezone.utc)
    result = check_guardrails(offer, prices, config)
    if result.ok:
        return offer, result
    log.entries.append(
        {
            "offer_id": offer.offer_id,
            "version": offer.version,
            "stage": "guardrails",
            "reasons": list(result.reasons),
            "at": now,
        }
    )
    return offer.model_copy(update={"status": OfferStatus.REJECTED}), result