"""E10 / E11 - Cross-checks between the reports and the workflows (Epic E).

E10 / XW-4  The daily report shows offer discounts, redemptions and receipts, and
            restock orders. Their totals must match what Offers (H5) and
            Restocking (H3) recorded. On any mismatch the section is BLOCKED with
            the reasons (the same "block on failure" rule as C9).
E11 / XW-5  Reports built from the same snapshot never disagree: every daily and
            monthly payload must share snapshot_ref / query_version, additive KPIs
            of the month equal the sum of its days, and ATV is consistent.

The checks work on the H3/H4/H5 contracts, so they apply to M3's real report
code (Epic C) as soon as it produces those payloads; until then the reference
KPIs and the reference sections below produce them.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

from crossflow.contracts import KpiPayload, OfferTotals, RestockOrderRecord
from crossflow.reference_kpis import line_moment

TWO_PLACES = Decimal("0.01")
ADDITIVE_KPIS = ("gross_sales", "discounts", "refunds", "net_revenue", "units", "transactions")


# --- E10: offers section ---------------------------------------------------------------------------


def offer_section(sales_lines: Iterable[dict[str, Any]], branch_id: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Reference offer section of a report, built from the sales lines only."""
    rows: dict[str, dict[str, Any]] = {}
    for line in sales_lines:
        if int(line["branch_id"]) != branch_id or not line.get("offer_id") or not start <= line_moment(line) < end:
            continue
        row = rows.setdefault(str(line["offer_id"]), {
            "offer_id": str(line["offer_id"]), "redemptions": 0, "discount_amount": Decimal(0),
            "receipts": set(), "lines": 0,
        })
        row["redemptions"] += int(line["quantity"])
        row["discount_amount"] += Decimal(str(line["discount_amount"]))
        row["receipts"].add(line["transaction_id"])
        row["lines"] += 1
    return [
        {**row, "receipts": len(row["receipts"]), "discount_amount": row["discount_amount"].quantize(TWO_PLACES)}
        for row in sorted(rows.values(), key=lambda r: r["offer_id"])
    ]


def check_offer_section(section: list[dict[str, Any]], records: Iterable[OfferTotals],
                        tolerance_per_line: Decimal = TWO_PLACES) -> dict[str, Any]:
    """Compare a report's offer rows with the Offers records (H5).

    Redemptions and receipts must match exactly. The discount may differ by at most
    one cent per sales line: the POS rounds each line, Offers multiplies once."""
    by_offer = {r["offer_id"]: r for r in section}
    mismatches: list[str] = []
    seen = set()
    for record in records:
        seen.add(record.offer_id)
        row = by_offer.get(record.offer_id)
        if row is None:
            if record.redemptions or record.discount_amount:
                mismatches.append(f"offer {record.offer_id}: in Offers records but missing from the report")
            continue
        if row["redemptions"] != record.redemptions:
            mismatches.append(f"offer {record.offer_id}: redemptions {row['redemptions']} in the report, {record.redemptions} in Offers")
        if row["receipts"] != record.receipts:
            mismatches.append(f"offer {record.offer_id}: receipts {row['receipts']} in the report, {record.receipts} in Offers")
        allowed = tolerance_per_line * max(1, int(row.get("lines", 1)))
        if abs(Decimal(str(row["discount_amount"])) - record.discount_amount) > allowed:
            mismatches.append(
                f"offer {record.offer_id}: discounts {row['discount_amount']} in the report, "
                f"{record.discount_amount} in Offers (allowed difference {allowed})"
            )
    for offer_id in sorted(set(by_offer) - seen):
        mismatches.append(f"offer {offer_id}: in the report but Offers has no record of it")
    return {"status": "BLOCKED" if mismatches else "OK", "offers": section, "mismatches": mismatches}


# --- E10: restock section -------------------------------------------------------------------------


def restock_section(orders: Iterable[RestockOrderRecord]) -> dict[str, dict[str, int]]:
    """Reference restock section: order lines and units per status."""
    totals: dict[str, dict[str, int]] = defaultdict(lambda: {"orders": 0, "units": 0})
    for order in orders:
        totals[order.status]["orders"] += 1
        totals[order.status]["units"] += order.requested_quantity
    return dict(totals)


def check_restock_section(section: dict[str, dict[str, int]], records: Iterable[RestockOrderRecord]) -> dict[str, Any]:
    expected = restock_section(records)
    mismatches = []
    for status in sorted(set(expected) | set(section)):
        want = expected.get(status, {"orders": 0, "units": 0})
        got = section.get(status, {"orders": 0, "units": 0})
        if want != got:
            mismatches.append(f"restock {status}: report shows {got}, Restocking recorded {want}")
    return {"status": "BLOCKED" if mismatches else "OK", "restock": section, "mismatches": mismatches}


# --- E11: reports never disagree ------------------------------------------------------------------


def compare_same_snapshot(a: KpiPayload, b: KpiPayload) -> list[str]:
    """Two reports of the same branch, period, snapshot and query version must be identical."""
    if (a.branch_id, a.period_start, a.period_end, a.snapshot_ref, a.query_version) != (
        b.branch_id, b.period_start, b.period_end, b.snapshot_ref, b.query_version
    ):
        return ["the two payloads are not for the same branch, period, snapshot and query version"]
    left, right = a.model_dump(), b.model_dump()
    return [f"{k}: {left[k]} vs {right[k]}" for k in left if left[k] != right[k]]


def compare_daily_with_monthly(dailies: list[KpiPayload], monthly: KpiPayload) -> list[str]:
    """The month must be exactly the sum of its days, from the same snapshot."""
    problems: list[str] = []
    for d in dailies:
        if (d.snapshot_ref, d.query_version) != (monthly.snapshot_ref, monthly.query_version):
            problems.append(f"daily {d.period_start:%Y-%m-%d}: snapshot {d.snapshot_ref}/{d.query_version} "
                            f"differs from the monthly {monthly.snapshot_ref}/{monthly.query_version}")
        if d.branch_id != monthly.branch_id:
            problems.append(f"daily {d.period_start:%Y-%m-%d}: branch {d.branch_id}, monthly is {monthly.branch_id}")

    ordered = sorted(dailies, key=lambda p: p.period_start)
    if not ordered or ordered[0].period_start != monthly.period_start or ordered[-1].period_end != monthly.period_end:
        problems.append("the daily reports do not cover exactly the monthly period")
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev.period_end != nxt.period_start:
            problems.append(f"gap or overlap between {prev.period_end.isoformat()} and {nxt.period_start.isoformat()}")

    # A receipt belongs to one local day, so transactions add up too.
    for kpi in ADDITIVE_KPIS:
        total = sum(getattr(d, kpi) for d in ordered)
        if total != getattr(monthly, kpi):
            problems.append(f"{kpi}: days add up to {total}, the month says {getattr(monthly, kpi)}")

    if monthly.transactions:
        atv = (monthly.net_revenue / monthly.transactions).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if monthly.average_transaction_value != atv:
            problems.append(f"average_transaction_value: {monthly.average_transaction_value}, expected {atv}")
    elif monthly.average_transaction_value is not None:
        problems.append("average_transaction_value must be N/A when there are no transactions")

    if all(d.gross_profit is not None for d in ordered) and monthly.gross_profit is not None:
        total_profit = sum((d.gross_profit for d in ordered), Decimal(0))
        if total_profit != monthly.gross_profit:
            problems.append(f"gross_profit: days add up to {total_profit}, the month says {monthly.gross_profit}")
    return problems
