
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest

from reporting.kpi_reader import KpiReaderAdapter
from reporting.mock_repository import MockReportingRepository


@pytest.fixture
def repository():
    repo = MockReportingRepository()

    repo.sales_data = [
        {
            "branch_id": 1,
            "product_id": 10,
            "date": date(2026, 1, 10),
            "time": time(10, 0),
            "quantity": 2,
            "unit_price": Decimal("100"),
            "total_amount": Decimal("180"),
            "discount_amount": Decimal("20"),
            "transaction_id": 100,
        },
        {
            "branch_id": 1,
            "product_id": 20,
            "date": date(2026, 1, 10),
            "time": time(10, 30),
            "quantity": 5,
            "unit_price": Decimal("100"),
            "total_amount": Decimal("400"),
            "discount_amount": Decimal("100"),
            "transaction_id": 101,
        },
        {
            "branch_id": 2,
            "product_id": 10,
            "date": date(2026, 1, 10),
            "time": time(10, 0),
            "quantity": 10,
            "unit_price": Decimal("100"),
            "total_amount": Decimal("1000"),
            "discount_amount": Decimal("0"),
            "transaction_id": 200,
        },
        {
            "branch_id": 1,
            "product_id": 10,
            "date": date(2026, 1, 10),
            "time": time(12, 0),
            "quantity": 100,
            "unit_price": Decimal("100"),
            "total_amount": Decimal("10000"),
            "discount_amount": Decimal("0"),
            "transaction_id": 102,
        },
    ]

    repo.returns_data = [
        {
            "branch_id": 1,
            "product_id": 10,
            "returned_at": datetime(
                2026, 1, 10, 11, 0, tzinfo=timezone.utc
            ),
            "refund_amount": Decimal("30"),
        },
        {
            "branch_id": 1,
            "product_id": 20,
            "returned_at": datetime(
                2026, 1, 10, 11, 0, tzinfo=timezone.utc
            ),
            "refund_amount": Decimal("100"),
        },
        {
            "branch_id": 1,
            "product_id": 10,
            "returned_at": datetime(
                2026, 1, 10, 12, 0, tzinfo=timezone.utc
            ),
            "refund_amount": Decimal("999"),
        },
    ]

    repo.products_data = [
        {
            "branch_id": 1,
            "product_id": 10,
            "cost_price": Decimal("20"),
        },
        {
            "branch_id": 1,
            "product_id": 20,
            "cost_price": Decimal("30"),
        },
        {
            "branch_id": 2,
            "product_id": 10,
            "cost_price": Decimal("15"),
        },
    ]

    return repo


@pytest.fixture
def reader(repository):
    return KpiReaderAdapter(repository)


@pytest.fixture
def period():
    start = datetime(
        2026, 1, 10, 9, 0, tzinfo=timezone.utc
    )
    end = datetime(
        2026, 1, 10, 12, 0, tzinfo=timezone.utc
    )
    return start, end


def test_units_filters_by_branch_product_and_period(reader, period):
    start, end = period

    result = reader.units(
        branch_id="1",
        product_ids=["10"],
        start=start,
        end=end,
    )

    assert result == 2


def test_units_does_not_require_returns_or_products(repository, period):
    repository.source_availability["returns"] = False
    repository.source_availability["products"] = False

    reader = KpiReaderAdapter(repository)
    start, end = period

    result = reader.units(
        branch_id="1",
        product_ids=["10"],
        start=start,
        end=end,
    )

    assert result == 2


def test_net_revenue_uses_sales_and_returns(reader, period):
    start, end = period

    result = reader.net_revenue(
        branch_id="1",
        product_ids=["10"],
        start=start,
        end=end,
    )

    assert result == Decimal("150.00")


def test_net_revenue_does_not_require_products(repository, period):
    repository.source_availability["products"] = False

    reader = KpiReaderAdapter(repository)
    start, end = period

    result = reader.net_revenue(
        branch_id="1",
        product_ids=["10"],
        start=start,
        end=end,
    )

    assert result == Decimal("150.00")


def test_gross_profit_uses_reporting_kpi_calculations(reader, period):
    start, end = period

    result = reader.gross_profit(
        branch_id="1",
        product_ids=["10"],
        start=start,
        end=end,
    )

    # Net revenue = 180 - 30 = 150
    # COGS = 2 * 20 = 40
    # Gross profit = 150 - 40 = 110
    assert result == Decimal("110.00")


def test_missing_sales_source_returns_none(repository, period):
    repository.source_availability["sales"] = False

    reader = KpiReaderAdapter(repository)
    start, end = period

    assert reader.units("1", ["10"], start, end) is None
    assert reader.net_revenue("1", ["10"], start, end) is None
    assert reader.gross_profit("1", ["10"], start, end) is None


def test_missing_returns_source_affects_revenue_and_profit(repository, period):
    repository.source_availability["returns"] = False

    reader = KpiReaderAdapter(repository)
    start, end = period

    assert reader.units("1", ["10"], start, end) == 2
    assert reader.net_revenue("1", ["10"], start, end) is None
    assert reader.gross_profit("1", ["10"], start, end) is None


def test_missing_products_source_affects_only_gross_profit(repository, period):
    repository.source_availability["products"] = False

    reader = KpiReaderAdapter(repository)
    start, end = period

    assert reader.units("1", ["10"], start, end) == 2
    assert reader.net_revenue("1", ["10"], start, end) == Decimal("150.00")
    assert reader.gross_profit("1", ["10"], start, end) is None


def test_missing_product_cost_returns_none(repository, period):
    repository.products_data[0]["cost_price"] = None

    reader = KpiReaderAdapter(repository)
    start, end = period

    result = reader.gross_profit(
        "1",
        ["10"],
        start,
        end,
    )

    assert result is None


def test_empty_product_ids_return_none(reader, period):
    start, end = period

    assert reader.units("1", [], start, end) is None
    assert reader.net_revenue("1", [], start, end) is None
    assert reader.gross_profit("1", [], start, end) is None


def test_invalid_period_raises_value_error(reader, period):
    start, end = period

    with pytest.raises(ValueError):
        reader.units(
            "1",
            ["10"],
            end,
            start,
        )


def test_non_numeric_ids_raise_value_error(reader, period):
    start, end = period

    with pytest.raises(ValueError):
        reader.units(
            "invalid",
            ["10"],
            start,
            end,
        )


def test_sales_at_period_end_are_excluded(reader, period):
    start, end = period

    result = reader.units(
        "1",
        ["10"],
        start,
        end,
    )

    # The sale at exactly 12:00 is outside [09:00, 12:00).
    assert result == 2


def test_returns_at_period_end_are_excluded(reader, period):
    start, end = period

    result = reader.net_revenue(
        "1",
        ["10"],
        start,
        end,
    )

    # The refund of 999 at exactly 12:00 is excluded.
    assert result == Decimal("150.00")
