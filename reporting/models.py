
from datetime import datetime
from decimal import Decimal
from typing import Optional, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


class ReportingPeriod(BaseModel):
    """Defines the requested reporting time window."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_period(self):
        if self.end <= self.start:
            raise ValueError(
                "Reporting period end must be after start"
            )
        return self


class BranchKPIs(BaseModel):
    """Standardized KPI values for a branch."""

    model_config = ConfigDict(frozen=True)

    branch_id: int
    gross_sales: Optional[Decimal] = None
    discounts: Optional[Decimal] = None
    net_revenue: Optional[Decimal] = None
    units: Optional[int] = Field(default=None, ge=0)
    transactions: Optional[int] = Field(default=None, ge=0)
    cogs: Optional[Decimal] = None
    gross_profit: Optional[Decimal] = None
    average_transaction_value: Optional[Decimal] = None


class OfferReportSummary(BaseModel):
    """
    Stores offer totals provided by the Offers module.

    Values are consumed from offer_totals().
    Reporting must not recalculate discounts or redemptions.
    """

    model_config = ConfigDict(frozen=True)

    offer_id: str
    version: int = Field(ge=1)
    redemptions: int = Field(ge=0)
    returns: int = Field(ge=0)
    discount_amount: Decimal = Field(ge=0)


class ReportDataStatus(BaseModel):
    """Describes the availability of reporting data."""

    model_config = ConfigDict(frozen=True)

    is_complete: bool

    status: Optional[
        Literal["complete", "partial", "unavailable"]
    ] = None

    missing_sources: tuple[str, ...] = ()

    cutoff_time: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def set_default_status(cls, values):
        if isinstance(values, dict) and values.get("status") is None:
            values = values.copy()
            values["status"] = (
                "complete"
                if values.get("is_complete", False)
                else "partial"
            )

        return values

    @model_validator(mode="after")
    def validate_status(self):
        if self.status == "complete" and not self.is_complete:
            raise ValueError(
                "A complete report must have is_complete=True"
            )

        if self.status in ("partial", "unavailable") and self.is_complete:
            raise ValueError(
                "A partial or unavailable report cannot be complete"
            )

        return self


class BranchReportData(BaseModel):
    """Contains all source data required to generate a branch report."""

    model_config = ConfigDict(frozen=True)

    branch_id: int
    period: ReportingPeriod

    sales_lines: list[dict[str, Any]] = Field(
        default_factory=list
    )

    returns: list[dict[str, Any]] = Field(
        default_factory=list
    )

    products: list[dict[str, Any]] = Field(
        default_factory=list
    )

    offers: list[OfferReportSummary] = Field(
        default_factory=list
    )

    data_status: ReportDataStatus

    kpis: Optional[BranchKPIs] = None
