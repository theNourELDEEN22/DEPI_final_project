
from datetime import datetime, timezone
from typing import Any

from reporting.models import BranchReportData
from reporting.validation import validate_report
from reporting.repository import ReportingRepository


def prepare_report_run(
    repository: ReportingRepository,
    report: BranchReportData,
) -> dict[str, Any]:
    """Validate and save a report run."""

    validation_result = validate_report(report)
    errors = validation_result["errors"]

    # Errors caused only by unavailable sources are data incompleteness,
    # not KPI validation failures.
    data_availability_errors = (
        "Required source is unavailable:",
        "source data is unavailable.",
    )

    has_data_validation_failure = any(
        not any(
            marker in error
            for marker in data_availability_errors
        )
        for error in errors
    )

    if has_data_validation_failure:
        status = "validation_failed"
    elif report.data_status.status != "complete":
        status = "data_incomplete"
    else:
        status = "ready_to_send"

    report_run = {
        "branch_id": report.branch_id,
        "period": {
            "start": report.period.start.isoformat(),
            "end": report.period.end.isoformat(),
        },
        "status": status,
        "is_valid": validation_result["is_valid"],
        "validation_errors": errors,
        "data_status": report.data_status.model_dump(mode="json"),
        "kpis": (
            report.kpis.model_dump(mode="json")
            if report.kpis is not None
            else None
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return repository.save_report_run(report_run)


def can_send_report(report_run: dict[str, Any]) -> bool:
    """Check whether a report is eligible for sending."""

    return (
        report_run.get("status") == "ready_to_send"
        and report_run.get("is_valid") is True
        and report_run.get("data_status", {}).get("status") == "complete"
        and not report_run.get("validation_errors")
    )


def ensure_report_can_be_sent(
    report_run: dict[str, Any],
) -> None:
    """Block sending when the report is not eligible."""

    if not can_send_report(report_run):
        raise ValueError(
            "Report cannot be sent. "
            f"Current status: {report_run.get('status', 'unknown')}"
        )
