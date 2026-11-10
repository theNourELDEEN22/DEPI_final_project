"""E2 - Event contracts for the handoffs H1-H6 (Epic E: Cross-Workflow).

    H1  StockSignal            Restocking -> Offers, Reporting
    H2  ClearanceNotice        Offers     -> Restocking    products an offer is clearing
    H3  RestockOrderRecord     Restocking -> Reporting
    H4  KpiPayload             Reporting  -> Offers, QA     KPI results + the snapshot they came from
    H5  OfferTotals            Offers     -> Reporting
    H6  DemandLevelProposal    sell-through -> config, ONLY after a human review

Every event carries schema_version, event_type, idempotency_key, branch_id
(the database BIGINT), occurred_at (timezone-aware) and producer. Unknown fields
are refused (extra="forbid") so a typo is a failure, not silently dropped data.

Adapters for formats that already exist in the repo return (events, violations)
instead of raising, so a gap between the contract and a producer is reported,
not hidden. See crossflow/docs/EVENT_CONTRACTS.md for the review copy.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SCHEMA_VERSION = 1


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetimes must include a timezone")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Event(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    idempotency_key: str = Field(min_length=1)
    branch_id: int = Field(gt=0)
    occurred_at: datetime
    producer: str = Field(min_length=1)

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


# --- H1 ------------------------------------------------------------------------------------------


class SignalType(str, Enum):
    NEAR_EXPIRY = "NEAR_EXPIRY"
    HIGH_DEMAND_LOW_STOCK = "HIGH_DEMAND_LOW_STOCK"
    MEDIUM_DEMAND_LOW_STOCK = "MEDIUM_DEMAND_LOW_STOCK"


class ExpiringBatch(_Strict):
    batch_id: Optional[int] = None
    expires_on: date
    days_left: int = Field(ge=0)
    quantity: int = Field(gt=0)


class StockSignal(Event):
    event_type: Literal["restocking.stock_signal"] = "restocking.stock_signal"
    producer: str = "restocking"
    run_id: str = Field(min_length=1)
    product_id: int = Field(gt=0)
    sku: Optional[str] = None
    signal_type: SignalType
    on_hand: Optional[int] = Field(default=None, ge=0)
    demand_level: Optional[str] = None
    expiring_batches: tuple[ExpiringBatch, ...] = ()
    evidence_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def _near_expiry_needs_batches(self) -> "StockSignal":
        if self.signal_type == SignalType.NEAR_EXPIRY and not self.expiring_batches:
            raise ValueError("a NEAR_EXPIRY signal must list its expiring batches")
        return self


def parse_restock_signal_message(
    message: dict[str, Any], occurred_at: datetime
) -> tuple[list[StockSignal], list[str]]:
    """Adapter for the outbox row Restocking writes today (restocking/repository.py,
    publish_stock_signals): one message per run, payload {"run_id", "signals": [...]}."""
    payload = message.get("payload") or {}
    base_key = str(message.get("idempotency_key") or "")
    signals: list[StockSignal] = []
    violations: list[str] = []
    for raw in payload.get("signals") or []:
        product = raw.get("product_id")
        try:
            signals.append(
                StockSignal(
                    idempotency_key=f"{base_key}:{product}:{raw.get('signal_type')}",
                    branch_id=int(raw.get("branch_id", message.get("branch_id"))),
                    occurred_at=occurred_at,
                    run_id=str(payload.get("run_id") or ""),
                    product_id=int(product),
                    sku=raw.get("sku"),
                    signal_type=raw.get("signal_type"),
                    on_hand=raw.get("on_hand"),
                    demand_level=raw.get("demand_level"),
                    expiring_batches=tuple(raw.get("batches") or ()),
                    evidence_ref=str(raw.get("evidence_ref") or ""),
                )
            )
        except (ValidationError, TypeError, ValueError) as exc:
            detail = "; ".join(e["msg"] for e in exc.errors()) if isinstance(exc, ValidationError) else str(exc)
            violations.append(f"product {product} ({raw.get('signal_type')}): {detail}")
    return signals, violations


# --- H2 ------------------------------------------------------------------------------------------


class ClearanceNotice(Event):
    """An approved or live offer is clearing these products: Restocking must not reorder them."""

    event_type: Literal["offers.clearance"] = "offers.clearance"
    producer: str = "offers"
    offer_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    status: Literal["approved", "active"]
    product_ids: tuple[int, ...] = Field(min_length=1)
    start_at: datetime
    end_at: datetime
    reason: str = Field(min_length=1)

    @field_validator("start_at", "end_at")
    @classmethod
    def _window_aware(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def _window_order(self) -> "ClearanceNotice":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


# --- H3 ------------------------------------------------------------------------------------------


class RestockOrderRecord(Event):
    """One restock_orders row (shared schema) as Reporting reads it."""

    event_type: Literal["restocking.order"] = "restocking.order"
    producer: str = "restocking"
    run_id: str = Field(min_length=1)
    restock_order_id: Optional[int] = None
    product_id: int = Field(gt=0)
    supplier_id: int = Field(gt=0)
    requested_quantity: int = Field(gt=0)
    trigger_reason: SignalType
    status: Literal["DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED", "DISPATCHED", "ESCALATED"]
    version: int = Field(ge=1)
    version_hash: Optional[str] = None


# --- H4 ------------------------------------------------------------------------------------------


class KpiPayload(_Strict):
    """What a report stores in report_runs.kpi_payload. Every report built from the
    same snapshot_ref and query_version must give the same numbers (XW-5)."""

    snapshot_ref: str = Field(min_length=1)
    query_version: str = Field(min_length=1)
    branch_id: int = Field(gt=0)
    period_start: datetime
    period_end: datetime
    gross_sales: Decimal
    discounts: Decimal
    refunds: Decimal
    net_revenue: Decimal
    units: int
    transactions: int = Field(ge=0)
    average_transaction_value: Optional[Decimal] = None  # N/A when there are no transactions
    gross_profit: Optional[Decimal] = None               # N/A when any cost is missing

    @field_validator("period_start", "period_end")
    @classmethod
    def _period_aware(cls, value: datetime) -> datetime:
        return _aware(value)


# --- H5 ------------------------------------------------------------------------------------------


class OfferTotals(Event):
    """Offers-side totals for one offer version over a period (the daily report compares them)."""

    event_type: Literal["offers.totals"] = "offers.totals"
    producer: str = "offers"
    offer_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    period_start: datetime
    period_end: datetime
    redemptions: int = Field(ge=0)
    returns: int = Field(ge=0)
    discount_amount: Decimal = Field(ge=0)
    receipts: int = Field(ge=0)

    @field_validator("period_start", "period_end")
    @classmethod
    def _period_aware(cls, value: datetime) -> datetime:
        return _aware(value)


# --- H6 ------------------------------------------------------------------------------------------


class DemandLevel(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class DemandLevelProposal(Event):
    """A proposed change to products.demand_level. Never applied without a human review."""

    event_type: Literal["crossflow.demand_level_proposal"] = "crossflow.demand_level_proposal"
    producer: str = "crossflow"
    proposal_id: str = Field(min_length=1)
    product_id: int = Field(gt=0)
    current_level: DemandLevel
    proposed_level: DemandLevel
    period_start: date
    period_end: date
    units_sold: int = Field(ge=0)
    days: int = Field(gt=0)
    avg_daily_units: Decimal
    source: Literal["rules", "ai"]


CONTRACTS: dict[str, type[BaseModel]] = {
    "H1": StockSignal,
    "H2": ClearanceNotice,
    "H3": RestockOrderRecord,
    "H4": KpiPayload,
    "H5": OfferTotals,
    "H6": DemandLevelProposal,
}


def json_schemas() -> dict[str, dict[str, Any]]:
    """JSON Schema of every contract, for review and for non-Python consumers."""
    return {name: model.model_json_schema() for name, model in CONTRACTS.items()}
