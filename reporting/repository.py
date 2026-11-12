
from datetime import datetime
from typing import Any, Protocol

from reporting.models import ReportingPeriod


class ReportingRepository(Protocol):
    """Contract for reading branch reporting data and storing report runs."""

    def fetch_sales_lines(
        self,
        branch_id: int,
        period: ReportingPeriod,
    ) -> list[dict[str, Any]]:
        ...

    def fetch_returns(
        self,
        branch_id: int,
        period: ReportingPeriod,
    ) -> list[dict[str, Any]]:
        ...

    def fetch_products(
        self,
        branch_id: int,
        product_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        ...

    def get_source_availability(self) -> dict[str, bool]:
        ...

    def save_report_run(
        self,
        report_run: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def fetch_report_run(
        self,
        report_run_id: str,
    ) -> dict[str, Any] | None:
        ...

    def fetch_report_runs(
        self,
        branch_id: int | None = None,
    ) -> list[dict[str, Any]]:
        ...
