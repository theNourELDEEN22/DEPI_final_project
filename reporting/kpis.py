
from decimal import Decimal
from typing import Any


def calculate_gross_sales(
    sales_lines: list[dict[str, Any]],
) -> Decimal:
    """Calculate sales before discounts."""
    return sum(
        (
            Decimal(str(row["total_amount"]))
            + Decimal(str(row.get("discount_amount", 0)))
            for row in sales_lines
        ),
        Decimal("0.00"),
    )


def calculate_net_revenue(
    sales_lines: list[dict[str, Any]],
    returns: list[dict[str, Any]],
) -> Decimal:
    """Calculate revenue after discounts and refunds."""
    sales_total = sum(
        (Decimal(str(row["total_amount"])) for row in sales_lines),
        Decimal("0.00"),
    )

    refunds_total = sum(
        (Decimal(str(row["refund_amount"])) for row in returns),
        Decimal("0.00"),
    )

    return sales_total - refunds_total


def calculate_units_sold(
    sales_lines: list[dict[str, Any]],
) -> int:
    """Calculate total units sold."""
    return sum(row["quantity"] for row in sales_lines)


def calculate_transactions(
    sales_lines: list[dict[str, Any]],
) -> int:
    """Count unique sales receipts."""
    return len({row["transaction_id"] for row in sales_lines})


def calculate_cogs(
    sales_lines: list[dict[str, Any]],
    products: list[dict[str, Any]],
) -> Decimal:
    """Calculate cost of goods sold using current product cost."""
    cost_by_product = {
        row["product_id"]: Decimal(str(row["cost_price"]))
        for row in products
    }

    return sum(
        (
            Decimal(str(line["quantity"]))
            * cost_by_product[line["product_id"]]
            for line in sales_lines
        ),
        Decimal("0.00"),
    )


def calculate_gross_profit(
    net_revenue: Decimal,
    cogs: Decimal,
) -> Decimal:
    """Calculate gross profit."""
    return net_revenue - cogs


def calculate_atv(
    net_revenue: Decimal,
    transactions: int,
) -> Decimal | None:
    """Calculate average transaction value."""
    if transactions == 0:
        return None

    return net_revenue / Decimal(transactions)

def calculate_discounts(
    sales_lines: list[dict[str, Any]]
) -> Decimal:
    return sum(
        (
            Decimal(str(row.get("discount_amount", 0)))
            for row in sales_lines
        ),
        Decimal("0.00"),
    )
