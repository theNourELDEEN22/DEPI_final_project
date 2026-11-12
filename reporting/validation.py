
from decimal import Decimal

from reporting.models import ReportingPeriod, BranchReportData

from reporting.kpis import (
    calculate_gross_sales,
    calculate_discounts,
    calculate_net_revenue,
    calculate_units_sold,
    calculate_transactions,
    calculate_cogs,
    calculate_gross_profit,
    calculate_atv,
)


def validate_branch_id(branch_id: int) -> None:
    if not isinstance(branch_id, int) or isinstance(branch_id, bool):
        raise ValueError("branch_id must be an integer")

    if branch_id <= 0:
        raise ValueError("branch_id must be positive")


def validate_reporting_period(period: ReportingPeriod) -> None:
    if period.end <= period.start:
        raise ValueError("Invalid reporting period")


def validate_sales_lines(sales_lines: list[dict]) -> None:
    for index, row in enumerate(sales_lines):
        if row["quantity"] < 0:
            raise ValueError(f"Negative quantity in sales line {index}")

        if row["unit_price"] < 0:
            raise ValueError(f"Negative unit price in sales line {index}")

        if row["total_amount"] < 0:
            raise ValueError(f"Negative total amount in sales line {index}")

        if row.get("discount_amount", 0) < 0:
            raise ValueError(f"Negative discount in sales line {index}")


def validate_returns(returns: list[dict]) -> None:
    for index, row in enumerate(returns):
        quantity = Decimal(str(row["quantity"]))
        refund_amount = Decimal(str(row["refund_amount"]))

        if quantity <= 0:
            raise ValueError(f"Invalid return quantity in row {index}")

        if refund_amount < 0:
            raise ValueError(f"Negative refund amount in row {index}")


def validate_report(report: BranchReportData) -> dict:
    """
    Reconcile available report KPIs against source records.

    Data completeness and KPI correctness are separate concerns.

    Missing sources do not automatically mean KPI validation failed.
    Only KPIs that can be calculated from available sources are checked.
    """

    errors = []

    if report.kpis is None:
        return {
            "is_valid": False,
            "errors": ["Report KPIs are unavailable."],
        }

    kpis = report.kpis
    missing_sources = set(report.data_status.missing_sources)

    sales_available = "sales" not in missing_sources
    returns_available = "returns" not in missing_sources
    products_available = "products" not in missing_sources
    prices_available = "prices" not in missing_sources
    cost_available = (
        "cost" not in missing_sources
        and "product_costs" not in missing_sources
    )

    def compare(name, recorded, calculated):
        if calculated is None:
            errors.append(
                f"{name}: source data is unavailable."
            )
        elif recorded is None:
            errors.append(
                f"{name}: KPI value is missing."
            )
        elif recorded != calculated:
            errors.append(
                f"{name} mismatch: expected {recorded}, got {calculated}."
            )

    expected_net_revenue = None
    expected_transactions = None
    expected_cogs = None

    # Sales-based KPIs
    if sales_available:
        expected_discounts = calculate_discounts(report.sales_lines)
        expected_units = calculate_units_sold(report.sales_lines)
        expected_transactions = calculate_transactions(report.sales_lines)

        compare("discounts", kpis.discounts, expected_discounts)
        compare("units", kpis.units, expected_units)
        compare("transactions", kpis.transactions, expected_transactions)

        # Gross sales requires both sales and price data.
        if prices_available:
            expected_gross_sales = calculate_gross_sales(
                report.sales_lines
            )
            compare(
                "gross_sales",
                kpis.gross_sales,
                expected_gross_sales,
            )

    # Net revenue requires sales, returns, and prices.
    if sales_available and returns_available and prices_available:
        expected_net_revenue = calculate_net_revenue(
            report.sales_lines,
            report.returns,
        )

        compare(
            "net_revenue",
            kpis.net_revenue,
            expected_net_revenue,
        )

    # COGS requires sales, products, and cost data.
    if sales_available and products_available and cost_available:
        expected_cogs = calculate_cogs(
            report.sales_lines,
            report.products,
        )

        compare(
            "cogs",
            kpis.cogs,
            expected_cogs,
        )

    # Gross profit requires valid net revenue and COGS.
    if expected_net_revenue is not None and expected_cogs is not None:
        expected_gross_profit = calculate_gross_profit(
            expected_net_revenue,
            expected_cogs,
        )

        compare(
            "gross_profit",
            kpis.gross_profit,
            expected_gross_profit,
        )

    # ATV requires valid net revenue and transaction count.
    if (
        expected_net_revenue is not None
        and expected_transactions is not None
    ):
        expected_atv = calculate_atv(
            expected_net_revenue,
            expected_transactions,
        )

        compare(
            "average_transaction_value",
            kpis.average_transaction_value,
            expected_atv,
        )

    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
    }
