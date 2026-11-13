
from reporting.mock_repository import MockReportingRepository


def test_save_report_run_generates_id_and_created_at():
    repository = MockReportingRepository()

    report = {
        "branch_id": 1,
        "status": "complete",
        "net_revenue": "1500.00",
    }

    saved_report = repository.save_report_run(report)

    assert saved_report["report_run_id"]
    assert saved_report["created_at"]
    assert saved_report["branch_id"] == 1
    assert saved_report["status"] == "complete"


def test_fetch_report_run_returns_saved_report():
    repository = MockReportingRepository()

    saved_report = repository.save_report_run({
        "branch_id": 1,
        "status": "complete",
    })

    report = repository.fetch_report_run(
        saved_report["report_run_id"]
    )

    assert report is not None
    assert report["branch_id"] == 1
    assert report["status"] == "complete"


def test_fetch_report_run_returns_none_for_missing_id():
    repository = MockReportingRepository()

    report = repository.fetch_report_run("non-existing-id")

    assert report is None


def test_fetch_report_runs_filters_by_branch():
    repository = MockReportingRepository()

    repository.save_report_run({
        "branch_id": 1,
        "status": "complete",
    })

    repository.save_report_run({
        "branch_id": 2,
        "status": "partial",
    })

    reports = repository.fetch_report_runs(branch_id=1)

    assert len(reports) == 1
    assert reports[0]["branch_id"] == 1


def test_fetch_report_runs_returns_all_reports():
    repository = MockReportingRepository()

    repository.save_report_run({"branch_id": 1})
    repository.save_report_run({"branch_id": 2})

    reports = repository.fetch_report_runs()

    assert len(reports) == 2
