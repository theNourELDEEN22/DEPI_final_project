from typing import Any

from reporting.models import BranchReportData


def build_executive_summary(report: BranchReportData) -> dict[str, Any]:
    kpis = report.kpis
    status = report.data_status

    headline_metrics = {
        "gross_sales": (
            str(kpis.gross_sales)
            if kpis.gross_sales is not None
            else None
        ),
        "discounts": (
            str(kpis.discounts)
            if kpis.discounts is not None
            else None
        ),
        "net_revenue": (
            str(kpis.net_revenue)
            if kpis.net_revenue is not None
            else None
        ),
        "units_sold": kpis.units,
        "transactions": kpis.transactions,
        "gross_profit": (
            str(kpis.gross_profit)
            if kpis.gross_profit is not None
            else None
        ),
    }

    insights = []

    if status.status == "complete":
        insights.append("All required data sources are available.")
    elif status.status == "partial":
        insights.append("The report contains partial data.")

    if status.status == "unavailable":
        insights.append("Sales data is unavailable.")
    elif status.missing_sources:
        insights.append(
            "Missing sources: " + ", ".join(status.missing_sources)
        )

    if kpis.net_revenue is not None and kpis.gross_profit is not None:
        if kpis.gross_profit > 0:
            insights.append("The branch achieved a positive gross profit.")
        elif kpis.gross_profit < 0:
            insights.append("The branch recorded a gross loss.")
        else:
            insights.append("The branch broke even.")

    return {
        "branch_id": report.branch_id,
        "reporting_period": {
            "start": report.period.start.isoformat(),
            "end": report.period.end.isoformat(),
        },
        "status": status.status,
        "headline_metrics": headline_metrics,
        "insights": insights,
        "missing_sources": list(status.missing_sources),
    }