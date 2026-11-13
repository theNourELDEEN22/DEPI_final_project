
from datetime import datetime, date, timezone
from reporting.models import ReportingPeriod
from reporting.mock_repository import MockReportingRepository


def test_sales_are_filtered_by_branch_and_period():
    repo = MockReportingRepository()

    repo.sales_data = [
        {
            "branch_id": 1,
            "transaction_id": 100,
            "date": date(2026, 1, 10),
            "quantity": 5,
        },
        {
            "branch_id": 2,
            "transaction_id": 200,
            "date": date(2026, 1, 10),
            "quantity": 10,
        },
        {
            "branch_id": 1,
            "transaction_id": 300,
            "date": date(2026, 2, 10),
            "quantity": 3,
        },
    ]

    period = ReportingPeriod(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    result = repo.fetch_sales_lines(1, period)

    assert len(result) == 1
    assert result[0]["transaction_id"] == 100


def test_products_are_filtered_by_branch_and_product_ids():
    repo = MockReportingRepository()

    repo.products_data = [
        {"branch_id": 1, "product_id": 10},
        {"branch_id": 1, "product_id": 20},
        {"branch_id": 2, "product_id": 10},
    ]

    result = repo.fetch_products(1, [10])

    assert result == [
        {"branch_id": 1, "product_id": 10}
    ]


def test_returns_are_filtered_by_branch_and_period():
    repo = MockReportingRepository()

    repo.returns_data = [
        {
            "branch_id": 1,
            "return_id": 1,
            "returned_at": datetime(2026, 1, 15, tzinfo=timezone.utc),
        },
        {
            "branch_id": 2,
            "return_id": 2,
            "returned_at": datetime(2026, 1, 15, tzinfo=timezone.utc),
        },
    ]

    period = ReportingPeriod(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    result = repo.fetch_returns(1, period)

    assert len(result) == 1
    assert result[0]["return_id"] == 1

def test_returns_exclude_period_end():
    repo = MockReportingRepository()

    repo.returns_data = [
        {
            "branch_id": 1,
            "return_id": 10,
            "returned_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
        }
    ]

    period = ReportingPeriod(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    result = repo.fetch_returns(1, period)

    assert result == []
