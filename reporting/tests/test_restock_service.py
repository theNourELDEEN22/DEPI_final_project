
from datetime import date, datetime
from decimal import Decimal

from reporting.mock_repository import MockReportingRepository
from reporting.models import ReportingPeriod
from reporting.restock_service import build_restock_section


def create_repository():
    repository = MockReportingRepository()

    repository.inventory_movements = [
        {
            "branch_id": 1,
            "product_id": 101,
            "movement_type": "SALE",
            "quantity_change": -5,
            "occurred_at": datetime(2026, 1, 10),
        },
        {
            "branch_id": 1,
            "product_id": 101,
            "movement_type": "RESTOCK",
            "quantity_change": 20,
            "occurred_at": datetime(2026, 1, 12),
        },
        {
            "branch_id": 1,
            "product_id": 102,
            "movement_type": "EXPIRY_WRITE_OFF",
            "quantity_change": -3,
            "occurred_at": datetime(2026, 1, 15),
        },
    ]

    repository.product_batches = [
        {
            "batch_id": 1,
            "batch_number": "BATCH-001",
            "branch_id": 1,
            "product_id": 101,
            "stock_quantity": 10,
            "expiration_date": date(2026, 2, 5),
        },
        {
            "batch_id": 2,
            "batch_number": "BATCH-002",
            "branch_id": 1,
            "product_id": 102,
            "stock_quantity": 5,
            "expiration_date": date(2026, 5, 1),
        },
    ]

    repository.restock_orders = [
        {
            "restock_order_id": 1,
            "run_id": "RUN-001",
            "branch_id": 1,
            "product_id": 101,
            "supplier_id": 10,
            "requested_quantity": 50,
            "trigger_reason": "HIGH_DEMAND_LOW_STOCK",
            "status": "PENDING_APPROVAL",
        }
    ]

    return repository


def create_period():
    return ReportingPeriod(
        start=datetime(2026, 1, 1),
        end=datetime(2026, 2, 1),
    )


def test_stock_impact_comes_from_inventory_movements():
    repository = create_repository()

    # Deliberately unrelated stock_quantity values.
    repository.products_data = [
        {
            "branch_id": 1,
            "product_id": 101,
            "stock_quantity": 999,
        }
    ]

    result = build_restock_section(
        repository=repository,
        branch_id=1,
        period=create_period(),
        as_of=date(2026, 1, 10),
    )

    assert result["stock_impact"]["total_quantity_change"] == 12


def test_stock_impact_breakdown_by_movement_type():
    repository = create_repository()

    result = build_restock_section(
        repository=repository,
        branch_id=1,
        period=create_period(),
        as_of=date(2026, 1, 10),
    )

    breakdown = result["stock_impact"]["movement_breakdown"]

    assert breakdown["SALE"] == -5
    assert breakdown["RESTOCK"] == 20
    assert breakdown["EXPIRY_WRITE_OFF"] == -3
    assert result["stock_impact"]["movements_count"] == 3


def test_near_expiry_batches_are_identified():
    repository = create_repository()

    result = build_restock_section(
        repository=repository,
        branch_id=1,
        period=create_period(),
        as_of=date(2026, 1, 10),
        near_expiry_days=30,
    )

    assert len(result["near_expiry"]) == 1
    assert result["near_expiry"][0]["batch_id"] == 1
    assert result["near_expiry"][0]["days_until_expiry"] == 26


def test_restock_orders_are_returned():
    repository = create_repository()

    result = build_restock_section(
        repository=repository,
        branch_id=1,
        period=create_period(),
        as_of=date(2026, 1, 10),
    )

    assert len(result["restock_orders"]) == 1
    assert result["restock_orders"][0]["product_id"] == 101
    assert result["restock_orders"][0]["requested_quantity"] == 50
    assert result["restock_orders"][0]["status"] == "PENDING_APPROVAL"


def test_near_expiry_excludes_expired_batches():
    repository = create_repository()

    repository.product_batches.append({
        "batch_id": 3,
        "batch_number": "BATCH-003",
        "branch_id": 1,
        "product_id": 103,
        "stock_quantity": 8,
        "expiration_date": date(2026, 1, 9),
    })

    result = build_restock_section(
        repository=repository,
        branch_id=1,
        period=create_period(),
        as_of=date(2026, 1, 10),
    )

    batch_ids = [
        batch["batch_id"]
        for batch in result["near_expiry"]
    ]

    assert 3 not in batch_ids


def test_negative_near_expiry_days_raises_error():
    repository = create_repository()

    try:
        build_restock_section(
            repository=repository,
            branch_id=1,
            period=create_period(),
            as_of=date(2026, 1, 10),
            near_expiry_days=-1,
        )
    except ValueError as error:
        assert "near_expiry_days" in str(error)
    else:
        raise AssertionError("Expected ValueError")
