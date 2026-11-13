
from typing import Any
from decimal import Decimal

from reporting.models import BranchReportData


def build_detailed_report(
    report: BranchReportData,
) -> dict[str, Any]:
    """
    Build a detailed, structured report for a branch.
    """

    offers_summary = {
        "offers": [
            {
                "offer_id": offer.offer_id,
                "version": offer.version,
                "redemptions": offer.redemptions,
                "returns": offer.returns,
                "discount_amount": str(offer.discount_amount),
            }
            for offer in report.offers
        ],
        "totals": {
            "offers_count": len(report.offers),
            "redemptions": sum(
                offer.redemptions for offer in report.offers
            ),
            "returns": sum(
                offer.returns for offer in report.offers
            ),
            "discount_amount": str(
                sum(
                    (
                        offer.discount_amount
                        for offer in report.offers
                    ),
                    Decimal("0.00"),
                )
            ),
        },
    }

    return {
        "branch_id": report.branch_id,

        "reporting_period": {
            "start": report.period.start.isoformat(),
            "end": report.period.end.isoformat(),
        },

        "report_status": {
            "status": report.data_status.status,
            "is_complete": report.data_status.is_complete,
            "missing_sources": list(
                report.data_status.missing_sources
            ),
            "cutoff_time": (
                report.data_status.cutoff_time.isoformat()
                if report.data_status.cutoff_time
                else None
            ),
        },

        "sales_summary": {
            "gross_sales": (
                str(report.kpis.gross_sales)
                if report.kpis and report.kpis.gross_sales is not None
                else None
            ),
            "discounts": (
                str(report.kpis.discounts)
                if report.kpis and report.kpis.discounts is not None
                else None
            ),
            "net_revenue": (
                str(report.kpis.net_revenue)
                if report.kpis and report.kpis.net_revenue is not None
                else None
            ),
            "units_sold": (
                report.kpis.units
                if report.kpis
                else None
            ),
            "transactions": (
                report.kpis.transactions
                if report.kpis
                else None
            ),
            "average_transaction_value": (
                str(report.kpis.average_transaction_value)
                if report.kpis and report.kpis.average_transaction_value is not None
                else None
            ),
        },

        "profitability": {
            "cogs": (
                str(report.kpis.cogs)
                if report.kpis and report.kpis.cogs is not None
                else None
            ),
            "gross_profit": (
                str(report.kpis.gross_profit)
                if report.kpis and report.kpis.gross_profit is not None
                else None
            ),
        },

        "offers_summary": offers_summary,

        "source_data": {
            "sales_lines_count": len(report.sales_lines),
            "returns_count": len(report.returns),
            "products_count": len(report.products),
            "offers_count": len(report.offers),
        },
    }
