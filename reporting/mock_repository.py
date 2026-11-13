
from copy import deepcopy
from datetime import date, datetime, time
from typing import Any
from uuid import uuid4

from reporting.models import ReportingPeriod


class MockReportingRepository:
    """
    Temporary in-memory repository for testing
    Branch Reporting without Supabase.
    """

    def __init__(self):
        self.sales_data: list[dict[str, Any]] = []
        self.returns_data: list[dict[str, Any]] = []
        self.products_data: list[dict[str, Any]] = []

        # C12: Inventory and restocking data
        self.inventory_movements: list[dict[str, Any]] = []
        self.product_batches: list[dict[str, Any]] = []
        self.restock_orders: list[dict[str, Any]] = []

        # Reporting source availability
        self.source_availability = {
            "sales": True,
            "returns": True,
            "products": True,
            "prices": True,
            "cost": True,
            "inventory_movements": True,
            "product_batches": True,
            "restock_orders": True,
        }

        self.report_runs: dict[str, dict[str, Any]] = {}

    def fetch_sales_lines(
        self,
        branch_id: int,
        period: ReportingPeriod,
    ) -> list[dict[str, Any]]:
        """
        Fetch sales for a branch within [period.start, period.end).

        Supports:
        - Date + time: accurate datetime filtering.
        - Date only: backward-compatible daily filtering.
        """

        result = []

        for row in self.sales_data:
            if row["branch_id"] != branch_id:
                continue

            sale_date = row["date"]

            if isinstance(sale_date, str):
                sale_date = date.fromisoformat(sale_date)

            if isinstance(sale_date, datetime):
                sale_datetime = sale_date

                if (
                    sale_datetime.tzinfo is None
                    and period.start.tzinfo is not None
                ):
                    sale_datetime = sale_datetime.replace(
                        tzinfo=period.start.tzinfo
                    )

                if period.start <= sale_datetime < period.end:
                    result.append(row)

                continue

            sale_time = row.get("time")

            if sale_time is not None:
                if isinstance(sale_time, str):
                    sale_time = time.fromisoformat(sale_time)

                sale_datetime = datetime.combine(
                    sale_date,
                    sale_time,
                    tzinfo=period.start.tzinfo,
                )

                if period.start <= sale_datetime < period.end:
                    result.append(row)

            else:
                if period.start.date() <= sale_date < period.end.date():
                    result.append(row)

        return result

    def fetch_returns(
        self,
        branch_id: int,
        period: ReportingPeriod,
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self.returns_data
            if row["branch_id"] == branch_id
            and period.start <= row["returned_at"] < period.end
        ]

    def fetch_products(
        self,
        branch_id: int,
        product_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self.products_data
            if row["branch_id"] == branch_id
            and (
                product_ids is None
                or row["product_id"] in product_ids
            )
        ]

    def fetch_inventory_movements(
        self,
        branch_id: int,
        period: ReportingPeriod,
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self.inventory_movements
            if row["branch_id"] == branch_id
            and period.start <= row["occurred_at"] < period.end
        ]

    def fetch_product_batches(
        self,
        branch_id: int,
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self.product_batches
            if row["branch_id"] == branch_id
        ]

    def fetch_restock_orders(
        self,
        branch_id: int,
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self.restock_orders
            if row["branch_id"] == branch_id
        ]

    def get_source_availability(self) -> dict[str, bool]:
        return self.source_availability.copy()

    def save_report_run(
        self,
        report_run: dict[str, Any],
    ) -> dict[str, Any]:
        saved_report = deepcopy(report_run)

        report_run_id = (
            saved_report.get("report_run_id") or str(uuid4())
        )

        saved_report["report_run_id"] = report_run_id
        saved_report.setdefault(
            "created_at",
            datetime.now().isoformat(),
        )

        self.report_runs[report_run_id] = saved_report

        return deepcopy(saved_report)

    def fetch_report_run(
        self,
        report_run_id: str,
    ) -> dict[str, Any] | None:
        report = self.report_runs.get(report_run_id)

        return deepcopy(report) if report is not None else None

    def fetch_report_runs(
        self,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        reports = list(self.report_runs.values())

        if branch_id is not None:
            reports = [
                report
                for report in reports
                if report.get("branch_id") == branch_id
            ]

        return deepcopy(reports)
