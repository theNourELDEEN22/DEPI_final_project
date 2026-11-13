
from datetime import date, datetime, timezone

import pytest
from decimal import Decimal

from reporting.mock_repository import MockReportingRepository
from reporting.models import ReportingPeriod
from reporting.report_service import load_branch_report_data
from reporting.report_workflow import (
    prepare_report_run,
    can_send_report,
    ensure_report_can_be_sent,
)


def create_report(source_availability=None):
    repository = MockReportingRepository()

    repository.sales_data = [
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

    repository.returns_data = [
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

    repository.products_data = [
        {
            "branch_id": 1,
            "product_id": 10,
            "product_name": "Product A",
            "cost_price": 20,
        }
    ]

    if source_availability:
        repository.source_availability.update(source_availability)

    period = ReportingPeriod(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    report = load_branch_report_data(repository, 1, period)

    return repository, report


def test_complete_valid_report_is_ready_to_send():
    repository, report = create_report()

    saved = prepare_report_run(repository, report)

    assert saved["status"] == "ready_to_send"
    assert saved["is_valid"] is True
    assert can_send_report(saved) is True

    ensure_report_can_be_sent(saved)


def test_partial_report_is_saved_but_not_sendable():
    repository, report = create_report(
        {"returns": False}
    )

    saved = prepare_report_run(repository, report)

    assert saved["status"] == "data_incomplete"
    assert can_send_report(saved) is False

    with pytest.raises(ValueError):
        ensure_report_can_be_sent(saved)


def test_unavailable_report_is_not_sendable():
    repository, report = create_report(
        {"sales": False}
    )

    saved = prepare_report_run(repository, report)

    assert saved["status"] == "data_incomplete"
    assert can_send_report(saved) is False


def test_report_run_is_persisted_in_repository():
    repository, report = create_report()

    saved = prepare_report_run(repository, report)

    fetched = repository.fetch_report_run(
        saved["report_run_id"]
    )

    assert fetched is not None
    assert fetched["status"] == "ready_to_send"


def test_validation_failed_report_cannot_be_sent():
    repository, report = create_report()

    invalid_kpis = report.kpis.model_copy(

update={"gross_sales": Decimal("999")}    )
    invalid_report = report.model_copy(
        update={"kpis": invalid_kpis}
    )

    saved = prepare_report_run(repository, invalid_report)

    assert saved["status"] == "validation_failed"
    assert saved["validation_errors"]
    assert can_send_report(saved) is False

    with pytest.raises(ValueError):
        ensure_report_can_be_sent(saved)
