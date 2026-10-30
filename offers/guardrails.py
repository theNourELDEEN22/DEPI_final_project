"""D4 + D5 - Guardrails (Epic D: Offers).

D4: max discount, margin and price floor.
D5: quantity cap, expiry dates, restricted products, active-offer conflicts.
Runs after validation (D3). D9 re-runs check_all_guardrails right before activation.

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
    max_quantity_cap: int = 500  # D5: largest quantity cap an offer may carry


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


def _reject_and_log(
    offer: Offer, result: GuardrailResult, log, now: datetime
) -> tuple[Offer, GuardrailResult]:
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


def run_guardrails(
    offer: Offer,
    prices: PriceReader,
    config: GuardrailConfig,
    log,  # offers.validation.RejectionLog (or anything with an .entries list)
    now: datetime | None = None,
) -> tuple[Offer, GuardrailResult]:
    """D4 only. Check; on violation return the offer as REJECTED and log the reasons.
    On success the offer is returned unchanged."""
    now = now or datetime.now(timezone.utc)
    return _reject_and_log(offer, check_guardrails(offer, prices, config), log, now)


# ---------------------------------------------------------------------------
# D5 - quantity cap, expiry dates, restrictions, active-offer conflicts
# ---------------------------------------------------------------------------


class ProductRules(Protocol):
    """Product facts the D5 rules need. M1's product batches table (A1) will implement this."""

    def earliest_expiry(self, branch_id: str, product_id: str) -> Optional[datetime]: ...
    def is_restricted(self, branch_id: str, product_id: str) -> bool: ...


class ActiveOffers(Protocol):
    """Other offers already approved or active in a branch."""

    def offers_in_branch(self, branch_id: str) -> list[Offer]: ...


@dataclass
class InMemoryProductRules:
    expiry: dict[tuple[str, str], datetime] = field(default_factory=dict)
    restricted: set[tuple[str, str]] = field(default_factory=set)

    def earliest_expiry(self, branch_id: str, product_id: str) -> Optional[datetime]:
        return self.expiry.get((branch_id, product_id))

    def is_restricted(self, branch_id: str, product_id: str) -> bool:
        return (branch_id, product_id) in self.restricted


@dataclass
class InMemoryActiveOffers:
    offers: list[Offer] = field(default_factory=list)

    def offers_in_branch(self, branch_id: str) -> list[Offer]:
        return [o for o in self.offers if o.branch_id == branch_id]


def _all_product_ids(offer: Offer) -> set[str]:
    ids = set(offer.product_ids)
    ids.update(i.product_id for i in (offer.bundle_items or []))
    return ids


def check_extended_guardrails(
    offer: Offer,
    rules: ProductRules,
    active: ActiveOffers,
    config: GuardrailConfig,
) -> GuardrailResult:
    reasons: list[str] = []
    products = sorted(_all_product_ids(offer))

    # 1) quantity cap: required, positive, and not above the configured maximum
    if offer.quantity_cap is None:
        reasons.append("quantity cap is required")
    elif offer.quantity_cap > config.max_quantity_cap:
        reasons.append(
            f"quantity cap {offer.quantity_cap} is above the maximum {config.max_quantity_cap}"
        )

    for pid in products:
        # 2) expiry: the offer must end on or before the earliest batch expiry
        expiry = rules.earliest_expiry(offer.branch_id, pid)
        if expiry is not None and offer.end_date > expiry:
            reasons.append(
                f"product {pid}: offer ends {offer.end_date:%Y-%m-%d} after its expiry {expiry:%Y-%m-%d}"
            )
        # 3) restrictions: some products may not be promoted at all
        if rules.is_restricted(offer.branch_id, pid):
            reasons.append(f"product {pid} is restricted from promotions")

    # 4) conflicts: another approved/active offer on the same product with overlapping dates
    for other in active.offers_in_branch(offer.branch_id):
        if other.offer_id == offer.offer_id:
            continue  # a new version of the same offer never conflicts with itself
        if other.status not in (OfferStatus.APPROVED, OfferStatus.ACTIVE):
            continue
        shared = set(products) & _all_product_ids(other)
        overlaps = offer.start_date < other.end_date and other.start_date < offer.end_date
        if shared and overlaps:
            reasons.append(
                f"conflicts with offer {other.offer_id} on product(s) {', '.join(sorted(shared))}"
            )

    return GuardrailResult(ok=not reasons, reasons=tuple(reasons))


def check_all_guardrails(
    offer: Offer,
    prices: PriceReader,
    rules: ProductRules,
    active: ActiveOffers,
    config: GuardrailConfig,
) -> GuardrailResult:
    """D4 + D5 together. Reasons from both are collected in one pass."""
    d4 = check_guardrails(offer, prices, config)
    d5 = check_extended_guardrails(offer, rules, active, config)
    reasons = d4.reasons + d5.reasons
    return GuardrailResult(ok=not reasons, reasons=reasons)


def run_all_guardrails(
    offer: Offer,
    prices: PriceReader,
    rules: ProductRules,
    active: ActiveOffers,
    config: GuardrailConfig,
    log,
    now: datetime | None = None,
) -> tuple[Offer, GuardrailResult]:
    """The entry point after validation, and again for D9 right before activation."""
    now = now or datetime.now(timezone.utc)
    result = check_all_guardrails(offer, prices, rules, active, config)
    return _reject_and_log(offer, result, log, now)
