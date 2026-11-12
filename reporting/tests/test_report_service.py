
from datetime import date, datetime, timezone
from decimal import Decimal
from reporting.detailed_report import build_detailed_report

from reporting.models import ReportingPeriod
from reporting.mock_repository import MockReportingRepository
from reporting.report_service import load_branch_report_data


def create_test_repository():
    repo = MockReportingRepository()

    repo.sales_data = [
        {
            "branch_id": 1,
            "transaction_id": 100,
            "product_id": 10,
            "date": date(2026, 1, 10),
            "quantity": 5,
            "unit_price": 100,
            "total_amount": 450,
            "discount_amount": 50,
        }
    ]

    repo.returns_data = [
        {
            "branch_id": 1,
            "return_id": 1,
            "transaction_id": 100,
            "product_id": 10,
            "quantity": 1,
            "refund_amount": 30,
            "returned_at": datetime(
                2026, 1, 15, tzinfo=timezone.utc
            ),
        }
    ]

    repo.products_data = [
        {
            "branch_id": 1,
            "product_id": 10,
            "product_name": "Product A",
            "cost_price": 20,
        }
    ]

    return repo


def create_test_period():
    return ReportingPeriod(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )


def test_load_branch_report_data():
    repo = create_test_repository()
    period = create_test_period()

    report = load_branch_report_data(repo, 1, period)

    # Verify source data
    assert report.branch_id == 1
    assert len(report.sales_lines) == 1
    assert len(report.returns) == 1
    assert len(report.products) == 1
    assert report.products[0]["product_id"] == 10

    # Verify KPI calculations
    assert report.kpis is not None
    assert report.kpis.gross_sales == 500
    assert report.kpis.discounts == 50
    assert report.kpis.net_revenue == 420
    assert report.kpis.units == 5
    assert report.kpis.transactions == 1
    assert report.kpis.cogs == 100
    assert report.kpis.gross_profit == 320
    assert report.kpis.average_transaction_value == 420

    # Verify data completeness
    assert report.data_status.is_complete is True
    assert report.data_status.missing_sources == ()


def test_report_detects_missing_returns_source():
    repo = create_test_repository()
    period = create_test_period()

    # Simulate unavailable returns source
    repo.source_availability["returns"] = False

    report = load_branch_report_data(repo, 1, period)

    assert report.data_status.is_complete is False
    assert "returns" in report.data_status.missing_sources

    # Other KPIs should still be calculated
    assert report.kpis is not None
    assert report.kpis.gross_sales == 500


def test_report_unavailable_when_sales_source_is_missing():
    repo = create_test_repository()
    period = create_test_period()

    repo.source_availability["sales"] = False

    report = load_branch_report_data(repo, 1, period)

    assert report.data_status.status == "unavailable"
    assert report.data_status.is_complete is False
    assert "sales" in report.data_status.missing_sources

    assert report.kpis.gross_sales is None
    assert report.kpis.net_revenue is None
    assert report.kpis.units is None
    assert report.kpis.transactions is None


def test_partial_report_when_returns_source_is_missing():
    repo = create_test_repository()
    period = create_test_period()

    repo.source_availability["returns"] = False

    report = load_branch_report_data(repo, 1, period)

    assert report.data_status.status == "partial"
    assert report.data_status.is_complete is False

    # Sales-based KPIs remain available
    assert report.kpis.gross_sales == 500
    assert report.kpis.units == 5
    assert report.kpis.transactions == 1

    # KPIs requiring returns are unavailable
    assert report.kpis.net_revenue is None
    assert report.kpis.gross_profit is None
    assert report.kpis.average_transaction_value is None


def test_partial_report_when_products_source_is_missing():
    repo = create_test_repository()
    period = create_test_period()

    repo.source_availability["products"] = False

    report = load_branch_report_data(repo, 1, period)

    assert report.data_status.status == "partial"
    assert report.data_status.is_complete is False
    assert "products" in report.data_status.missing_sources

    # Sales and revenue KPIs remain available
    assert report.kpis.gross_sales == 500
    assert report.kpis.net_revenue == 420

    # Product-cost KPIs are unavailable
    assert report.kpis.cogs is None
    assert report.kpis.gross_profit is None


def test_build_detailed_report():
    repo = create_test_repository()
    period = create_test_period()

    branch_report = load_branch_report_data(
        repo,
        1,
        period,
    )

    detailed_report = build_detailed_report(branch_report)

    # Verify branch information
    assert detailed_report["branch_id"] == 1

    # Verify report status
    assert detailed_report["report_status"]["status"] == "complete"

    # Verify sales summary
    summary = detailed_report["sales_summary"]

    assert summary["gross_sales"] == "500.00"
    assert summary["discounts"] == "50.00"
    assert summary["net_revenue"] == "420.00"
    assert summary["units_sold"] == 5
    assert summary["transactions"] == 1

    # Verify profitability
    profitability = detailed_report["profitability"]

    assert profitability["cogs"] == "100.00"
    assert profitability["gross_profit"] == "320.00"

    # Verify source data counts
    assert detailed_report["source_data"]["sales_lines_count"] == 1
    assert detailed_report["source_data"]["returns_count"] == 1
    assert detailed_report["source_data"]["products_count"] == 1



from decimal import Decimal


def test_pilot_comparison_against_manual_figures():
    """
    Compare system-generated KPIs against manually calculated
    figures using the pilot test dataset.
    """

    repo = create_test_repository()
    period = create_test_period()

    report = load_branch_report_data(repo, 1, period)

    assert report.kpis is not None

    # Manually calculated expected figures
    manual_figures = {
        "gross_sales": Decimal("500"),
        "discounts": Decimal("50"),
        "net_revenue": Decimal("420"),
        "units": 5,
        "transactions": 1,
        "cogs": Decimal("100"),
        "gross_profit": Decimal("320"),
        "average_transaction_value": Decimal("420"),
    }

    # Actual figures generated by the reporting system
    agent_figures = {
        "gross_sales": report.kpis.gross_sales,
        "discounts": report.kpis.discounts,
        "net_revenue": report.kpis.net_revenue,
        "units": report.kpis.units,
        "transactions": report.kpis.transactions,
        "cogs": report.kpis.cogs,
        "gross_profit": report.kpis.gross_profit,
        "average_transaction_value": (
            report.kpis.average_transaction_value
        ),
    }

    assert agent_figures == manual_figures
