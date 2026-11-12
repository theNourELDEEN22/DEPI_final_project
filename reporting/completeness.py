
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Callable

from reporting.models import ReportDataStatus


REQUIRED_SOURCES = (
    "sales",
    "returns",
    "products",
    "prices",
    "cost",
    "inventory_movements",
)


def check_data_completeness(
    available_sources: dict[str, bool],
    cutoff_time=None,
) -> ReportDataStatus:
    """Check whether all required reporting sources are available."""

    missing_sources = tuple(
        source
        for source in REQUIRED_SOURCES
        if not available_sources.get(source, False)
    )

    return ReportDataStatus(
        is_complete=len(missing_sources) == 0,
        missing_sources=missing_sources,
        cutoff_time=cutoff_time,
    )


def wait_for_data_completeness(
    availability_reader: Callable[[], dict[str, bool]],
    timeout_seconds: float = 60,
    retry_interval_seconds: float = 5,
) -> ReportDataStatus:
    """Retry availability checks until complete or timeout."""

    if timeout_seconds < 0:
        raise ValueError("timeout_seconds cannot be negative")

    if retry_interval_seconds <= 0:
        raise ValueError("retry_interval_seconds must be positive")

    deadline = monotonic() + timeout_seconds

    while True:
        available_sources = availability_reader()
        status = check_data_completeness(available_sources)

        if status.is_complete:
            return status

        remaining_time = deadline - monotonic()

        if remaining_time <= 0:
            return ReportDataStatus(
                is_complete=False,
                status="partial",
                missing_sources=status.missing_sources,
                cutoff_time=datetime.now(timezone.utc),
            )

        sleep(min(retry_interval_seconds, remaining_time))
