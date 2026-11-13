
import pytest
from datetime import datetime, timezone
from decimal import Decimal

from reporting.models import (
    ReportingPeriod,
    BranchKPIs,
    ReportDataStatus,
)


def test_valid_reporting_period():
    period = ReportingPeriod(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    assert period.start < period.end


def test_invalid_reporting_period():
    with pytest.raises(ValueError):
        ReportingPeriod(
            start=datetime(2026, 2, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


def test_branch_kpis():
    kpis = BranchKPIs(
        branch_id=1,
        units=100,
        net_revenue=Decimal("15000.00"),
        gross_profit=Decimal("3000.00"),
        transactions=50,
        average_transaction_value=Decimal("300.00"),
    )

    assert kpis.units == 100
    assert kpis.net_revenue == Decimal("15000.00")


def test_report_data_status():
    status = ReportDataStatus(
        is_complete=False,
        missing_sources=("returns",),
    )

    assert not status.is_complete
    assert "returns" in status.missing_sources
