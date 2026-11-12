
from datetime import datetime
from decimal import Decimal
from typing import Optional

from reporting.models import ReportingPeriod
from reporting.repository import ReportingRepository

from reporting.kpis import (
    calculate_units_sold,
    calculate_net_revenue,
    calculate_gross_profit,
    calculate_cogs,
)


class KpiReaderAdapter:
    """
    Exposes Reporting KPI calculations through Offers KpiReader interface.

    All KPI formulas are reused from reporting.kpis.

    Required sources:
    - units: sales
    - net_revenue: sales + returns
    - gross_profit: sales + returns + products

    Missing required source data or product costs return None.
    """

    def __init__(self, repository: ReportingRepository):
        self.repository = repository

    def _load_data(
        self,
        branch_id: str,
        product_ids: list[str],
        start: datetime,
        end: datetime,
        required_sources: tuple[str, ...],
    ):
        if not product_ids:
            return None

        if end <= start:
            raise ValueError("End datetime must be after start datetime")

        try:
            branch_number = int(branch_id)
            product_numbers = [int(pid) for pid in product_ids]
        except (TypeError, ValueError):
            raise ValueError(
                "Branch ID and product IDs must be numeric"
            ) from None

        period = ReportingPeriod(
            start=start,
            end=end,
        )

        available = self.repository.get_source_availability()

        if not all(
            available.get(source, False)
            for source in required_sources
        ):
            return None

        sales_lines = self.repository.fetch_sales_lines(
            branch_id=branch_number,
            period=period,
        )

        selected_products = set(product_numbers)

        sales_lines = [
            row
            for row in sales_lines
            if int(row["product_id"]) in selected_products
        ]

        returns = []

        if "returns" in required_sources:
            returns = self.repository.fetch_returns(
                branch_id=branch_number,
                period=period,
            )

            returns = [
                row
                for row in returns
                if int(row["product_id"]) in selected_products
            ]

        products = []

        if "products" in required_sources:
            products = self.repository.fetch_products(
                branch_id=branch_number,
                product_ids=product_numbers,
            )

        return sales_lines, returns, products

    def units(
        self,
        branch_id: str,
        product_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> Optional[int]:

        data = self._load_data(
            branch_id,
            product_ids,
            start,
            end,
            required_sources=("sales",),
        )

        if data is None:
            return None

        sales_lines, _, _ = data

        return calculate_units_sold(sales_lines)

    def net_revenue(
        self,
        branch_id: str,
        product_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> Optional[Decimal]:

        data = self._load_data(
            branch_id,
            product_ids,
            start,
            end,
            required_sources=("sales", "returns"),
        )

        if data is None:
            return None

        sales_lines, returns, _ = data

        return calculate_net_revenue(
            sales_lines,
            returns,
        )

    def gross_profit(
        self,
        branch_id: str,
        product_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> Optional[Decimal]:

        data = self._load_data(
            branch_id,
            product_ids,
            start,
            end,
            required_sources=("sales", "returns", "products"),
        )

        if data is None:
            return None

        sales_lines, returns, products = data

        cost_by_product = {
            int(row["product_id"]): row.get("cost_price")
            for row in products
        }

        sales_product_ids = {
            int(row["product_id"])
            for row in sales_lines
        }

        missing_costs = [
            product_id
            for product_id in sales_product_ids
            if (
                product_id not in cost_by_product
                or cost_by_product[product_id] is None
            )
        ]

        if missing_costs:
            return None

        cogs = calculate_cogs(
            sales_lines,
            products,
        )

        net_revenue = calculate_net_revenue(
            sales_lines,
            returns,
        )

        return calculate_gross_profit(
            net_revenue,
            cogs,
        )
