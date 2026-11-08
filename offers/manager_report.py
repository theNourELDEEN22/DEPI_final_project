"""D13 - Manager financial report via the outbox; waste reduction labeled estimate
(Epic D: Offers).

Builds the finance manager's report for a FINISHED campaign (STOPPED or ENDED)
from the impact figures (D12), and puts it in the outbox. Nothing is sent from
here: a dispatcher sends outbox messages (the same pattern as A10 / B10), and the
dedup key makes sure one finished campaign produces one message, even if this
runs twice.

Rule from the task table: estimates are labeled.
- MEASURED figures come straight from the impact report (KPI functions).
- The waste reduction is an ESTIMATE. It carries the label "ESTIMATE", the method
  and the assumptions, in the data and in the text. It is left out (and says so)
  when the stock at the start is not known; it is never guessed.

Waste-reduction estimate method:
  expected leftover = max(0, stock at start - baseline units)
  units saved       = min(units cleared by the offer, expected leftover)
  value             = units saved x average unit cost
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional, Protocol

from offers.impact import ImpactReport, average_discount_and_cost
from offers.guardrails import PriceReader
from offers.review import _jsonable
from offers.schema import Offer

TWO_PLACES = Decimal("0.01")
ESTIMATE_LABEL = "ESTIMATE"


class ReportNotAllowed(ValueError):
    """Only a finished campaign (final impact) gets a manager report."""


@dataclass(frozen=True)
class OutboxMessage:
    dedup_key: str
    recipient_role: str
    subject: str
    body: str
    payload: dict[str, Any]
    created_at: datetime


class Outbox(Protocol):
    def enqueue(self, message: OutboxMessage) -> bool:
        """True if queued, False if a message with this dedup key already exists."""
        ...


@dataclass
class InMemoryOutbox:
    """Stand-in for the shared outbox (A10)."""

    messages: list[OutboxMessage] = field(default_factory=list)

    def enqueue(self, message: OutboxMessage) -> bool:
        if any(m.dedup_key == message.dedup_key for m in self.messages):
            return False
        self.messages.append(message)
        return True


def _waste_estimate(
    impact: ImpactReport, offer: Offer, prices: PriceReader, stock_at_start: Optional[int]
) -> Optional[dict[str, Any]]:
    if stock_at_start is None:
        return None
    _discount, unit_cost = average_discount_and_cost(offer, prices)
    expected_leftover = max(0, stock_at_start - impact.baseline.units)
    units_saved = min(impact.units_cleared, expected_leftover)
    return {
        "label": ESTIMATE_LABEL,
        "is_estimate": True,
        "units_saved": units_saved,
        "value": (unit_cost * units_saved).quantize(TWO_PLACES),
        "method": "units saved = min(units cleared, max(0, stock at start - baseline units)); value = units saved x average unit cost",
        "assumptions": [
            "baseline units show what would have sold without the offer",
            "stock left after the baseline would have been written off",
            f"stock at start was {stock_at_start} units",
        ],
    }


def build_manager_report(
    offer: Offer,
    impact: ImpactReport,
    *,
    prices: PriceReader,
    stock_at_start: Optional[int],
) -> dict[str, Any]:
    if not impact.is_final:
        raise ReportNotAllowed("the manager report is only built for a finished campaign")
    if impact.offer_id != offer.offer_id or impact.version != offer.version:
        raise ReportNotAllowed("the impact report is for a different offer version")

    return _jsonable(
        {
            "title": "Offer financial report",
            "offer_id": offer.offer_id,
            "version": offer.version,
            "branch_id": offer.branch_id,
            "period": {"start": impact.window_start, "end": impact.window_end},
            "measured": {
                "net_revenue": impact.actual.net_revenue,
                "gross_profit": impact.actual.gross_profit,
                "units": impact.actual.units,
                "baseline_net_revenue": impact.baseline.net_revenue,
                "baseline_gross_profit": impact.baseline.gross_profit,
                "baseline_units": impact.baseline.units,
                "incremental_net_revenue": impact.incremental.net_revenue,
                "incremental_gross_profit": impact.incremental.gross_profit,
                "incremental_units": impact.incremental.units,
                "discount_cost": impact.discount_cost,
                "roi": impact.roi,
                "redemptions": impact.redemptions,
                "returns": impact.returns,
                "stock_cleared_units": impact.units_cleared,
                "stock_on_hand_at_end": impact.stock_on_hand,
            },
            "estimates": {"waste_reduction": _waste_estimate(impact, offer, prices, stock_at_start)},
        }
    )


def render_report_text(report: dict[str, Any]) -> str:
    m = report["measured"]
    lines = [
        f"{report['title']} - offer {report['offer_id']} v{report['version']} (branch {report['branch_id']})",
        f"Period: {report['period']['start']} -> {report['period']['end']}",
        "",
        "Measured figures:",
        f"  Net revenue: {m['net_revenue']} (baseline {m['baseline_net_revenue']}, incremental {m['incremental_net_revenue']})",
        f"  Gross profit: {m['gross_profit']} (baseline {m['baseline_gross_profit']}, incremental {m['incremental_gross_profit']})",
        f"  Units: {m['units']} (baseline {m['baseline_units']}, incremental {m['incremental_units']})",
        f"  Discount cost: {m['discount_cost']}",
        f"  ROI: {m['roi'] if m['roi'] is not None else 'not available (no discount cost)'}",
        f"  Redemptions: {m['redemptions']}, returns: {m['returns']}, stock cleared: {m['stock_cleared_units']} units",
        f"  Stock on hand at the end: {m['stock_on_hand_at_end']} units",
        "",
    ]
    waste = report["estimates"]["waste_reduction"]
    if waste is None:
        lines.append("ESTIMATE: waste reduction not available (stock at start unknown).")
    else:
        lines.append(
            f"ESTIMATE: waste reduction {waste['value']} ({waste['units_saved']} units saved). "
            f"Method: {waste['method']}."
        )
        lines += [f"  Assumption: {a}" for a in waste["assumptions"]]
    return "\n".join(lines)


def queue_manager_report(
    report: dict[str, Any],
    outbox: Outbox,
    now: datetime | None = None,
) -> bool:
    """Put the report in the outbox. False means this campaign's report was already queued."""
    now = now or datetime.now(timezone.utc)
    return outbox.enqueue(
        OutboxMessage(
            dedup_key=f"manager_report:{report['offer_id']}:v{report['version']}",
            recipient_role="finance_manager",
            subject=f"Offer financial report: {report['offer_id']} v{report['version']}",
            body=render_report_text(report),
            payload=report,
            created_at=now,
        )
    )
