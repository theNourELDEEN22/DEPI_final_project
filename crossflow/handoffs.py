"""E6-E9 - The handoffs between Restocking and Offers (Epic E).

E6 / XW-1  stock signal (H1) becomes an offer candidate that links to the signal
E7         rule: near expiry AND overstocked gives an offer, not a restock order
E8 / XW-2  restock draft skips products being cleared by an offer (H2), with the reason
E9 / XW-3  an offer that ends or sells out triggers a stock re-evaluation of its products

Restocking's own decision code (restocking/workflow.py, _evaluate_inventory) is
used as-is: E8 and E9 feed it, and the E7 tests compare it with the rule here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from offers.monitoring import MonitorAction, MonitorResult
from offers.review import Evidence
from offers.schema import Offer, OfferStatus
from offer_strategist.facts import StrategistRequest
from restocking.config import category_thresholds
from restocking.workflow import _evaluate_inventory
from crossflow.contracts import ClearanceNotice, SignalType, StockSignal

CAIRO = ZoneInfo("Africa/Cairo")


# --- E7: the routing rule ---------------------------------------------------------------------


class Route(str, Enum):
    OFFER = "offer"
    RESTOCK = "restock"
    NONE = "none"


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    reason: str
    signal: Optional[SignalType] = None
    expiring_units: int = 0


def route_product(product: dict[str, Any], settings: dict[str, Any], as_of: date) -> RouteDecision:
    """The cross-workflow rule for one product (restocking inventory row shape).

    near expiry + stock above the demand threshold  -> OFFER   (clear it, don't buy more)
    near expiry + Low demand                        -> OFFER   (PROPOSED, decision D-06)
    near expiry + stock at/below the threshold      -> RESTOCK (NEAR_EXPIRY: replace it)
    High/Medium demand + stock at/below threshold   -> RESTOCK
    anything else, incl. Low demand with low stock  -> NONE    (B6)
    """
    rules = category_thresholds(settings, product["category"], product.get("subcategory") or "")
    expiry_days = int(rules["expiry_days"])
    expiring = 0
    for batch in product.get("batches") or []:
        quantity = int(batch["stock_quantity"])
        days_left = (date.fromisoformat(str(batch["expiration_date"])) - as_of).days
        if quantity > 0 and 0 <= days_left <= expiry_days:
            expiring += quantity

    level = str(product.get("demand_level") or "Low").strip().lower()
    threshold = {"high": rules["high_demand_qty"], "medium": rules["medium_demand_qty"]}.get(level)
    stock = int(product["on_hand"])

    if expiring:
        if threshold is None:
            return RouteDecision(Route.OFFER, "near expiry with Low demand: clear it with an offer (D-06)",
                                 SignalType.NEAR_EXPIRY, expiring)
        if stock > threshold:
            return RouteDecision(Route.OFFER, f"near expiry and overstocked ({stock} > {threshold})",
                                 SignalType.NEAR_EXPIRY, expiring)
        return RouteDecision(Route.RESTOCK, f"near expiry and low stock ({stock} <= {threshold})",
                             SignalType.NEAR_EXPIRY, expiring)
    if threshold is not None and stock <= threshold:
        signal = SignalType.HIGH_DEMAND_LOW_STOCK if level == "high" else SignalType.MEDIUM_DEMAND_LOW_STOCK
        return RouteDecision(Route.RESTOCK, f"{level} demand and low stock ({stock} <= {threshold})", signal)
    return RouteDecision(Route.NONE, "no action")


OFFER_REVIEW_MARKER = "offer review"  # restocking's skip reason for near-expiry overstock


def restocking_routes(products: list[dict[str, Any]], settings: dict[str, Any], as_of: date,
                      cleared: Iterable[int] = ()) -> dict[int, Route]:
    """What Restocking's own evaluation decides, mapped onto the same Route values."""
    result = _evaluate_inventory({
        "as_of": as_of.isoformat(), "products": products, "settings": settings,
        "active_offers": sorted(int(p) for p in cleared),
    })
    routes = {int(p["product_id"]): Route.NONE for p in products}
    for item in result["candidates"]:
        routes[int(item["product_id"])] = Route.RESTOCK
    for item in result["skipped_products"]:
        if OFFER_REVIEW_MARKER in item["reason"]:
            routes[int(item["product_id"])] = Route.OFFER
    return routes


# --- E6: H1 signal -> offer candidate ---------------------------------------------------------------


def restock_signal_message(run_state: dict[str, Any]) -> dict[str, Any]:
    """The outbox row Restocking writes for H1, built exactly like
    restocking/repository.py publish_stock_signals (which needs a live Supabase
    client, so it cannot run in tests). Keep the two in step."""
    run_id, branch_id = run_state["run_id"], run_state["branch_id"]
    signals = [
        {"product_id": item["product_id"], "sku": item["sku"], "branch_id": branch_id,
         "signal_type": item["trigger_reason"], "on_hand": item["on_hand"],
         "evidence_ref": f"{run_id}:{item['product_id']}"}
        for item in run_state["candidates"]
    ]
    signaled = {item["product_id"] for item in signals}
    signals.extend(
        {"product_id": item["product_id"], "branch_id": branch_id, "signal_type": "NEAR_EXPIRY",
         "evidence_ref": f"{run_id}:{item['product_id']}:expiry", "batches": item["batches"]}
        for item in run_state["expiry_watch"]
        if item["product_id"] not in signaled
    )
    return {
        "idempotency_key": f"restocking:{run_id}:H1",
        "branch_id": branch_id,
        "channel": "EVENT_BUS",
        "recipient": "offers-and-reporting",
        "subject": "restocking.stock_signal",
        "payload": {"run_id": run_id, "signals": signals},
    }


class SignalEvidence:
    """offers.review.EvidenceReader over H1 signals: the review card shows the signal itself."""

    def __init__(self, signals: Iterable[StockSignal]) -> None:
        self._by_ref = {s.evidence_ref: s for s in signals}

    def get_evidence(self, ref: str) -> Optional[Evidence]:
        signal = self._by_ref.get(ref)
        if signal is None:
            return None
        return Evidence(
            ref=ref,
            kind="stock_signal",
            facts={
                "signal_type": signal.signal_type.value,
                "run_id": signal.run_id,
                "on_hand": signal.on_hand,
                "demand_level": signal.demand_level,
                "expiring_units": sum(b.quantity for b in signal.expiring_batches),
                "expiring_batches": [b.model_dump(mode="json") for b in signal.expiring_batches],
                "signal_key": signal.idempotency_key,
            },
        )


def offer_requests_from_signals(
    signals: Iterable[StockSignal],
    *,
    inventory: dict[int, dict[str, Any]],
    settings: dict[str, Any],
    as_of: date,
    forecast_ref_for: Callable[[int, int], Optional[str]],
) -> tuple[list[StrategistRequest], list[dict[str, Any]]]:
    """NEAR_EXPIRY signals whose product routes to OFFER become strategist requests.
    `inventory` is product_id -> current restocking inventory row (the stock now)."""
    requests: list[StrategistRequest] = []
    skipped: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for signal in signals:
        key = (signal.branch_id, signal.product_id)
        if signal.signal_type != SignalType.NEAR_EXPIRY or key in seen:
            continue
        seen.add(key)
        product = inventory.get(signal.product_id)
        if product is None:
            skipped.append({"product_id": signal.product_id, "reason": "product not in the current inventory"})
            continue
        decision = route_product(product, settings, as_of)
        if decision.route != Route.OFFER:
            skipped.append({"product_id": signal.product_id, "reason": f"routes to {decision.route.value}: {decision.reason}"})
            continue
        forecast_ref = forecast_ref_for(signal.branch_id, signal.product_id)
        if not forecast_ref:
            skipped.append({"product_id": signal.product_id, "reason": "no stored forecast"})
            continue
        requests.append(StrategistRequest(
            branch_id=str(signal.branch_id),
            product_id=str(signal.product_id),
            product_name=str(product.get("product_name") or ""),
            category=str(product.get("category") or ""),
            demand_level=str(product.get("demand_level") or "medium").lower(),
            stock_on_hand=int(product["on_hand"]),
            expiring_units=decision.expiring_units,
            evidence_ref=signal.evidence_ref,  # the candidate links to the H1 signal
            forecast_ref=forecast_ref,
        ))
    return requests, skipped


# --- E8: H2 clearance --------------------------------------------------------------------------------


def _offer_products(offer: Offer) -> tuple[int, ...]:
    ids = list(dict.fromkeys([*offer.product_ids, *(i.product_id for i in (offer.bundle_items or []))]))
    try:
        return tuple(int(p) for p in ids)
    except ValueError as exc:
        raise ValueError(f"offer {offer.offer_id}: product ids must be database ids, got {ids}") from exc


def clearance_notices(offers: Iterable[Offer], now: datetime) -> list[ClearanceNotice]:
    """H2: every approved or live offer that has not ended yet."""
    notices = []
    for offer in offers:
        if offer.status not in (OfferStatus.APPROVED, OfferStatus.ACTIVE) or offer.end_date <= now:
            continue
        notices.append(ClearanceNotice(
            idempotency_key=f"clearance:{offer.offer_id}:v{offer.version}:{offer.status.value}",
            branch_id=int(offer.branch_id),
            occurred_at=now,
            offer_id=offer.offer_id,
            version=offer.version,
            status=offer.status.value,
            product_ids=_offer_products(offer),
            start_at=offer.start_date,
            end_at=offer.end_date,
            reason=f"being cleared by {offer.type.value} offer {offer.offer_id} (v{offer.version}, {offer.status.value})",
        ))
    return notices


def cleared_product_ids(notices: Iterable[ClearanceNotice], branch_id: int, as_of: date) -> list[int]:
    """Products Restocking must skip on business date `as_of` (its state['active_offers'])."""
    day_start = datetime.combine(as_of, time.min, CAIRO)
    return sorted({
        product
        for notice in notices
        if notice.branch_id == branch_id and notice.end_at > day_start
        for product in notice.product_ids
    })


# --- E9: offer end or sell-out -> re-evaluation ----------------------------------------------------------


@dataclass(frozen=True)
class ReevaluationRequest:
    idempotency_key: str
    branch_id: int
    product_ids: tuple[int, ...]
    offer_id: str
    version: int
    reason: str


def reevaluation_for(result: MonitorResult) -> Optional[ReevaluationRequest]:
    """XW-3: only a CONFIRMED end or sell-out triggers it (not an unconfirmed stop)."""
    if result.action not in (MonitorAction.ENDED, MonitorAction.STOPPED_AT_CAP):
        return None
    offer = result.offer
    return ReevaluationRequest(
        idempotency_key=f"reevaluate:{offer.offer_id}:v{offer.version}",
        branch_id=int(offer.branch_id),
        product_ids=_offer_products(offer),
        offer_id=offer.offer_id,
        version=offer.version,
        reason=f"offer {offer.offer_id} v{offer.version}: {result.action.value}",
    )


class ReevaluationQueue:
    """Idempotent: a replayed monitoring result never queues a second evaluation."""

    def __init__(self) -> None:
        self.requests: list[ReevaluationRequest] = []
        self._keys: set[str] = set()

    def submit(self, request: Optional[ReevaluationRequest]) -> bool:
        if request is None or request.idempotency_key in self._keys:
            return False
        self._keys.add(request.idempotency_key)
        self.requests.append(request)
        return True


def reevaluate(
    request: ReevaluationRequest,
    inventory: Iterable[dict[str, Any]],
    settings: dict[str, Any],
    as_of: date,
    still_cleared: Iterable[int] = (),
) -> dict[str, Any]:
    """Run Restocking's evaluation for the affected products only."""
    wanted = set(request.product_ids)
    products = [p for p in inventory if int(p["product_id"]) in wanted]
    result = _evaluate_inventory({
        "as_of": as_of.isoformat(), "products": products, "settings": settings,
        "active_offers": sorted(int(p) for p in still_cleared),
    })
    return {**result, "evaluated_product_ids": sorted(int(p["product_id"]) for p in products),
            "trigger": request.reason}
