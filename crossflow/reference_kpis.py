"""Reference KPI functions: the H4 stand-in until M3's library is exposed (C14),
and QA's independent cross-check of C6/C7 for the pilot comparison (E14).

Definitions (proposed for the C1 contract, see docs/DECISIONS_WEEK1.md, D-05):

    gross_sales   = sum(quantity x unit_price) of sales lines in the period
    discounts     = sum(discount_amount)
    refunds       = sum(refund_amount) of returns in the period (by returned_at)
    net_revenue   = gross_sales - discounts - refunds
    units         = sum(quantity) - returned quantity
    transactions  = distinct transaction_id with at least one line in the period
    ATV           = net_revenue / transactions;   N/A (None) when there are none
    gross_profit  = net_revenue - net units x unit cost;  N/A when any cost is missing

Periods are half-open [start, end). A sales line is placed by its local Cairo
date + time. Money is Decimal and rounded half-up to 0.01 only at the end.

ReferenceKpis also implements offers.impact.KpiReader (units / net_revenue /
gross_profit for a product subset), and SalesCampaignStats implements
offers.monitoring.StatsReader from the same sales lines.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from offers.guardrails import PriceReader
from offers.monitoring import CampaignStats
from offers.schema import Offer
from crossflow.contracts import KpiPayload

CAIRO = ZoneInfo("Africa/Cairo")
TWO_PLACES = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def line_moment(line: dict[str, Any]) -> datetime:
    day = line["date"] if isinstance(line["date"], date) else date.fromisoformat(str(line["date"]))
    moment = line["time"] if isinstance(line["time"], time) else time.fromisoformat(str(line["time"]))
    return datetime.combine(day, moment, CAIRO)


def _returned_at(item: dict[str, Any]) -> datetime:
    value = item["returned_at"]
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


class ReferenceKpis:
    def __init__(
        self,
        sales_lines: Iterable[dict[str, Any]],
        returns: Iterable[dict[str, Any]],
        prices: PriceReader,
    ) -> None:
        self._sales = sales_lines   # kept by reference: a live list sees new sales
        self._returns = returns
        self._prices = prices

    def _select(self, branch_id, start, end, product_ids):
        wanted = None if product_ids is None else {str(p) for p in product_ids}
        lines = [
            l for l in self._sales
            if str(l["branch_id"]) == str(branch_id)
            and (wanted is None or str(l["product_id"]) in wanted)
            and start <= line_moment(l) < end
        ]
        returns = [
            r for r in self._returns
            if str(r["branch_id"]) == str(branch_id)
            and (wanted is None or str(r["product_id"]) in wanted)
            and start <= _returned_at(r) < end
        ]
        return lines, returns

    def compute(
        self, branch_id: Any, start: datetime, end: datetime, product_ids: Optional[Iterable[Any]] = None
    ) -> dict[str, Any]:
        if end <= start:
            raise ValueError("the period is empty")
        lines, returns = self._select(branch_id, start, end, product_ids)
        gross = sum((Decimal(str(l["unit_price"])) * int(l["quantity"]) for l in lines), Decimal(0))
        discounts = sum((Decimal(str(l["discount_amount"] or 0)) for l in lines), Decimal(0))
        refunds = sum((Decimal(str(r["refund_amount"])) for r in returns), Decimal(0))
        net = gross - discounts - refunds

        net_units_by_product: dict[str, int] = {}
        for l in lines:
            net_units_by_product[str(l["product_id"])] = net_units_by_product.get(str(l["product_id"]), 0) + int(l["quantity"])
        for r in returns:
            net_units_by_product[str(r["product_id"])] = net_units_by_product.get(str(r["product_id"]), 0) - int(r["quantity"])
        units = sum(net_units_by_product.values())

        cost, cost_known = Decimal(0), True
        for product_id, qty in net_units_by_product.items():
            info = self._prices.get_price_info(str(branch_id), product_id)
            if info is None:
                cost_known = False
                break
            cost += info.cost * qty

        transactions = len({l["transaction_id"] for l in lines})
        return {
            "gross_sales": _q(gross),
            "discounts": _q(discounts),
            "refunds": _q(refunds),
            "net_revenue": _q(net),
            "units": units,
            "transactions": transactions,
            "average_transaction_value": _q(net / transactions) if transactions else None,
            "gross_profit": _q(net - cost) if cost_known else None,
        }

    def payload(
        self, branch_id: int, start: datetime, end: datetime, *, snapshot_ref: str, query_version: str
    ) -> KpiPayload:
        return KpiPayload(
            snapshot_ref=snapshot_ref, query_version=query_version, branch_id=int(branch_id),
            period_start=start, period_end=end, **self.compute(branch_id, start, end),
        )

    # --- offers.impact.KpiReader ---------------------------------------------------------------

    def units(self, branch_id: str, product_ids: list[str], start: datetime, end: datetime) -> Optional[int]:
        return self.compute(branch_id, start, end, product_ids)["units"]

    def net_revenue(self, branch_id: str, product_ids: list[str], start: datetime, end: datetime) -> Optional[Decimal]:
        return self.compute(branch_id, start, end, product_ids)["net_revenue"]

    def gross_profit(self, branch_id: str, product_ids: list[str], start: datetime, end: datetime) -> Optional[Decimal]:
        return self.compute(branch_id, start, end, product_ids)["gross_profit"]


class SalesCampaignStats:
    """offers.monitoring.StatsReader computed from sales lines (what the POS rang up)."""

    def __init__(
        self,
        sales_lines: Iterable[dict[str, Any]],
        returns: Iterable[dict[str, Any]],
        stock_on_hand: Callable[[str, list[str]], int],
        clock: Callable[[], datetime],
    ) -> None:
        self._sales = sales_lines
        self._returns = returns
        self._stock = stock_on_hand
        self._clock = clock
        self._offers: dict[str, Offer] = {}

    def track(self, offer: Offer) -> None:
        self._offers[offer.offer_id] = offer

    def get_stats(self, offer_id: str, version: int, branch_id: str) -> Optional[CampaignStats]:
        offer = self._offers.get(offer_id)
        if offer is None or offer.version != version:
            return None
        now = self._clock()
        products = sorted(set(offer.product_ids) | {i.product_id for i in (offer.bundle_items or [])})
        in_window = [
            l for l in self._sales
            if str(l["branch_id"]) == str(branch_id)
            and str(l["product_id"]) in products
            and offer.start_date <= line_moment(l) < min(now, offer.end_date)
        ]
        redeemed_txns = {(l["transaction_id"], str(l["product_id"])) for l in in_window if l.get("offer_id") == offer_id}
        returns = sum(
            int(r["quantity"]) for r in self._returns
            if (r["transaction_id"], str(r["product_id"])) in redeemed_txns
        )
        return CampaignStats(
            as_of=now,
            units_sold=sum(int(l["quantity"]) for l in in_window),
            redemptions=sum(int(l["quantity"]) for l in in_window if l.get("offer_id") == offer_id),
            returns=returns,
            stock_on_hand=self._stock(str(branch_id), products),
        )
