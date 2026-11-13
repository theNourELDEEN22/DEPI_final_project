
from datetime import date, datetime, timezone

from reporting.models import ReportingPeriod
from reporting.mock_repository import MockReportingRepository
from reporting.report_service import load_branch_report_data
from reporting.detailed_report import build_detailed_report


def create_repository():
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


def create_period():
    return ReportingPeriod(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )


def test_build_complete_detailed_report():
    repo = create_repository()
    period = create_period()

    branch_report = load_branch_report_data(repo, 1, period)
    detailed_report = build_detailed_report(branch_report)

    assert detailed_report["branch_id"] == 1
    assert detailed_report["report_status"]["status"] == "complete"

    summary = detailed_report["sales_summary"]

    assert summary["gross_sales"] == "500.00"
    assert summary["discounts"] == "50.00"
    assert summary["net_revenue"] == "420.00"
    assert summary["units_sold"] == 5
    assert summary["transactions"] == 1

    profitability = detailed_report["profitability"]

    assert profitability["cogs"] == "100.00"
    assert profitability["gross_profit"] == "320.00"

    assert detailed_report["source_data"]["sales_lines_count"] == 1
    assert detailed_report["source_data"]["returns_count"] == 1
    assert detailed_report["source_data"]["products_count"] == 1


def test_build_partial_detailed_report():
    repo = create_repository()
    period = create_period()

    # Simulate unavailable returns source
    repo.source_availability["returns"] = False

    branch_report = load_branch_report_data(repo, 1, period)
    detailed_report = build_detailed_report(branch_report)

    assert detailed_report["report_status"]["status"] == "partial"

    summary = detailed_report["sales_summary"]

    # Available sales KPIs
    assert summary["gross_sales"] == "500.00"
    assert summary["discounts"] == "50.00"
    assert summary["units_sold"] == 5
    assert summary["transactions"] == 1

    # KPIs requiring returns are unavailable
    assert summary["net_revenue"] is None
    assert summary["average_transaction_value"] is None

    # Gross profit requires net revenue
    assert detailed_report["profitability"]["gross_profit"] is None

def test_build_unavailable_detailed_report():
    repo = create_repository()
    period = create_period()

    repo.source_availability["sales"] = False

    branch_report = load_branch_report_data(repo, 1, period)
    detailed_report = build_detailed_report(branch_report)

    assert detailed_report["report_status"]["status"] == "unavailable"

    summary = detailed_report["sales_summary"]

    assert summary["gross_sales"] is None
    assert summary["discounts"] is None
    assert summary["net_revenue"] is None
    assert summary["units_sold"] is None
    assert summary["transactions"] is None

    assert detailed_report["profitability"]["cogs"] is None
    assert detailed_report["profitability"]["gross_profit"] is None