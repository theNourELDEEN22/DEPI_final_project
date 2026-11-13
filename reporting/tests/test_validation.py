
import pytest
from decimal import Decimal
from datetime import datetime, timezone, date

from reporting.validation import (
    validate_branch_id,
    validate_reporting_period,
    validate_sales_lines,
    validate_returns,
    validate_report,
)

from reporting.models import ReportingPeriod
from reporting.report_service import load_branch_report_data
from reporting.mock_repository import MockReportingRepository


def test_valid_branch_id():
    validate_branch_id(1)


def test_invalid_branch_id():
    with pytest.raises(ValueError):
        validate_branch_id(0)


def test_valid_reporting_period():
    period = ReportingPeriod(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    validate_reporting_period(period)


def test_valid_sales_lines():
    sales_lines = [
        {
            "quantity": 5,
            "unit_price": 100,
            "total_amount": 450,
            "discount_amount": 50,
        }
    ]

    validate_sales_lines(sales_lines)


def test_negative_quantity():
    sales_lines = [
        {
            "quantity": -5,
            "unit_price": 100,
            "total_amount": 450,
            "discount_amount": 50,
        }
    ]

    with pytest.raises(ValueError):
        validate_sales_lines(sales_lines)


def test_negative_unit_price():
    sales_lines = [
        {
            "quantity": 5,
            "unit_price": -100,
            "total_amount": 450,
            "discount_amount": 50,
        }
    ]

    with pytest.raises(ValueError):
        validate_sales_lines(sales_lines)


def test_negative_total_amount():
    sales_lines = [
        {
            "quantity": 5,
            "unit_price": 100,
            "total_amount": -450,
            "discount_amount": 50,
        }
    ]

    with pytest.raises(ValueError):
        validate_sales_lines(sales_lines)


def test_negative_discount():
    sales_lines = [
        {
            "quantity": 5,
            "unit_price": 100,
            "total_amount": 450,
            "discount_amount": -50,
        }
    ]

    with pytest.raises(ValueError):
        validate_sales_lines(sales_lines)


def test_valid_returns():
    returns = [
        {
            "quantity": 1,
            "refund_amount": 30,
        }
    ]

    validate_returns(returns)


def test_zero_return_quantity():
    returns = [
        {
            "quantity": 0,
            "refund_amount": 30,
        }
    ]

    with pytest.raises(ValueError):
        validate_returns(returns)


def test_negative_refund_amount():
    returns = [
        {
            "quantity": 1,
            "refund_amount": -30,
        }
    ]

    with pytest.raises(ValueError):
        validate_returns(returns)


def create_valid_report():
    repo = MockReportingRepository()

    repo.sales_data = [{
        "branch_id": 1,
        "transaction_id": 100,
        "product_id": 10,
        "date": date(2026, 1, 10),
        "quantity": 5,
        "unit_price": 100,
        "total_amount": 450,
        "discount_amount": 50,
    }]

    repo.returns_data = [{
        "branch_id": 1,
        "return_id": 1,
        "transaction_id": 100,
        "product_id": 10,
        "quantity": 1,
        "refund_amount": 30,
        "returned_at": datetime(
            2026, 1, 15, tzinfo=timezone.utc
        ),
    }]

    repo.products_data = [{
        "branch_id": 1,
        "product_id": 10,
        "cost_price": 20,
    }]

    period = ReportingPeriod(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    return load_branch_report_data(repo, 1, period)


def test_validate_report_success():
    report = create_valid_report()
    result = validate_report(report)

    assert result["is_valid"] is True
    assert result["errors"] == []


def test_validate_report_detects_kpi_mismatch():
    report = create_valid_report()

    corrupted_kpis = report.kpis.model_copy(
        update={"net_revenue": Decimal("999.00")}
    )

    corrupted_report = report.model_copy(
        update={"kpis": corrupted_kpis}
    )

    result = validate_report(corrupted_report)

    assert result["is_valid"] is False
    assert any(
        "net_revenue mismatch" in error
        for error in result["errors"]
    )


def test_validate_report_remains_valid_when_source_is_missing():
    report = create_valid_report()

    incomplete_status = report.data_status.model_copy(
        update={
            "is_complete": False,
            "status": "partial",
            "missing_sources": ("returns",),
        }
    )

    incomplete_report = report.model_copy(
        update={"data_status": incomplete_status}
    )

    result = validate_report(incomplete_report)

    # Missing returns should not invalidate available sales KPIs.
    assert result["is_valid"] is True
    assert result["errors"] == []

    # Data completeness is checked separately.
    assert incomplete_report.data_status.status == "partial"
    assert "returns" in incomplete_report.data_status.missing_sources


def test_validate_report_fails_when_kpi_is_missing():
    report = create_valid_report()

    incomplete_kpis = report.kpis.model_copy(
        update={"gross_sales": None}
    )

    incomplete_report = report.model_copy(
        update={"kpis": incomplete_kpis}
    )

    result = validate_report(incomplete_report)

    assert result["is_valid"] is False
    assert any(
        "gross_sales" in error
        for error in result["errors"]
    )
