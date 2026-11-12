
from decimal import Decimal

from reporting.kpis import (
    calculate_gross_sales,
    calculate_net_revenue,
    calculate_units_sold,
    calculate_transactions,
    calculate_cogs,
    calculate_gross_profit,
    calculate_atv,
)


def test_gross_sales():
    sales = [
        {"quantity": 2, "unit_price": 100,
         "total_amount": 180, "discount_amount": 20}
    ]
    assert calculate_gross_sales(sales) == Decimal("200.00")


def test_net_revenue():
    sales = [{"total_amount": 180}]
    returns = [{"refund_amount": 30}]
    assert calculate_net_revenue(sales, returns) == Decimal("150.00")


def test_units_sold():
    sales = [{"quantity": 2}, {"quantity": 3}]
    assert calculate_units_sold(sales) == 5


def test_transactions():
    sales = [
        {"transaction_id": 100},
        {"transaction_id": 100},
        {"transaction_id": 101},
    ]
    assert calculate_transactions(sales) == 2


def test_cogs():
    sales = [{"product_id": 10, "quantity": 3}]
    products = [{"product_id": 10, "cost_price": 20}]
    assert calculate_cogs(sales, products) == Decimal("60.00")


def test_gross_profit():
    assert calculate_gross_profit(
        Decimal("150.00"), Decimal("60.00")
    ) == Decimal("90.00")


def test_atv():
    assert calculate_atv(Decimal("300.00"), 3) == Decimal("100.00")


def test_atv_zero_transactions():
    assert calculate_atv(Decimal("0.00"), 0) is None

def test_discounts():
    from reporting.kpis import calculate_discounts

    sales_lines = [
        {"discount_amount": 50},
        {"discount_amount": 20},
        {"discount_amount": 30},
    ]

    result = calculate_discounts(sales_lines)

    assert result == 100