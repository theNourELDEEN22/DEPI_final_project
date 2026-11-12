
from datetime import date, datetime, timedelta
from typing import Any

from reporting.models import ReportingPeriod


def build_restock_section(
    repository: Any,
    branch_id: int,
    period: ReportingPeriod,
    as_of: date | None = None,
    near_expiry_days: int = 30,
) -> dict[str, Any]:
    """
    Build the restock section for a branch report.

    Stock impact is calculated from inventory movements,
    not from the products.stock_quantity field.
    """

    if near_expiry_days < 0:
        raise ValueError("near_expiry_days cannot be negative")

    reference_date = as_of or date.today()
    expiry_limit = reference_date + timedelta(days=near_expiry_days)

    movements = repository.fetch_inventory_movements(
        branch_id,
        period,
    )

    batches = repository.fetch_product_batches(branch_id)

    orders = repository.fetch_restock_orders(branch_id)

    # Calculate stock impact exclusively from inventory movements.
    movement_breakdown: dict[str, int] = {}
    total_stock_impact = 0

    for movement in movements:
        movement_type = movement["movement_type"]
        quantity_change = int(movement["quantity_change"])

        movement_breakdown[movement_type] = (
            movement_breakdown.get(movement_type, 0)
            + quantity_change
        )

        total_stock_impact += quantity_change

    # Identify batches approaching expiration.
    near_expiry = []

    for batch in batches:
        expiration_date = batch["expiration_date"]

        if isinstance(expiration_date, datetime):
            expiration_date = expiration_date.date()

        if isinstance(expiration_date, str):
            expiration_date = date.fromisoformat(expiration_date)

        if reference_date <= expiration_date <= expiry_limit:
            near_expiry.append({
                "batch_id": batch.get("batch_id"),
                "batch_number": batch.get("batch_number"),
                "product_id": batch["product_id"],
                "stock_quantity": batch["stock_quantity"],
                "expiration_date": expiration_date.isoformat(),
                "days_until_expiry": (
                    expiration_date - reference_date
                ).days,
            })

    # Return existing restock orders for the branch.
    restock_orders = [
        {
            "restock_order_id": order.get("restock_order_id"),
            "run_id": order.get("run_id"),
            "product_id": order["product_id"],
            "supplier_id": order["supplier_id"],
            "requested_quantity": order["requested_quantity"],
            "trigger_reason": order["trigger_reason"],
            "status": order["status"],
        }
        for order in orders
    ]

    return {
        "branch_id": branch_id,
        "reporting_period": {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
        },
        "restock_orders": restock_orders,
        "near_expiry": near_expiry,
        "stock_impact": {
            "total_quantity_change": total_stock_impact,
            "movement_breakdown": movement_breakdown,
            "movements_count": len(movements),
        },
    }
