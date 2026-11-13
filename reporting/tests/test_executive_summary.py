from datetime import datetime
from decimal import Decimal

from reporting.executive_summary import build_executive_summary
from reporting.models import (
    BranchKPIs,
    BranchReportData,
    ReportingPeriod,
    ReportDataStatus,
)


def create_report(status="complete"):
    period = ReportingPeriod(
        start=datetime(2026, 1, 1),
        end=datetime(2026, 2, 1),
    )

    data_status = ReportDataStatus(
        is_complete=(status == "complete"),
        status=status,
        missing_sources=(
            ("returns",) if status == "partial"
            else ("sales",) if status == "unavailable"
            else ()
        ),
    )

    kpis = BranchKPIs(
        branch_id=1,
        gross_sales=Decimal("1000"),
        discounts=Decimal("100"),
        net_revenue=Decimal("850"),
        units=20,
        transactions=10,
        cogs=Decimal("500"),
        gross_profit=Decimal("350"),
        average_transaction_value=Decimal("85"),
    )

    return BranchReportData(
        branch_id=1,
        period=period,
        sales_lines=[],
        returns=[],
        products=[],
        data_status=data_status,
        kpis=kpis,
    )


def test_executive_summary_complete_report():
    summary = build_executive_summary(create_report("complete"))

    assert summary["status"] == "complete"
    assert summary["headline_metrics"]["net_revenue"] == "850"
    assert "All required data sources are available." in summary["insights"]


def test_executive_summary_partial_report():
    summary = build_executive_summary(create_report("partial"))

    assert summary["status"] == "partial"
    assert summary["missing_sources"] == ["returns"]
    assert "The report contains partial data." in summary["insights"]


def test_executive_summary_unavailable_report():
    summary = build_executive_summary(create_report("unavailable"))

    assert summary["status"] == "unavailable"
    assert "Sales data is unavailable." in summary["insights"]