"""E2 - event contracts H1-H6."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from restocking.workflow import _evaluate_inventory
from crossflow.contracts import (
    CONTRACTS,
    ClearanceNotice,
    DemandLevelProposal,
    KpiPayload,
    OfferTotals,
    RestockOrderRecord,
    StockSignal,
    json_schemas,
    parse_restock_signal_message,
)
from crossflow.datagen import generate_pilot_branch
from crossflow.handoffs import restock_signal_message

AT = datetime(2026, 11, 10, 0, 30, tzinfo=timezone.utc)
COMMON = dict(idempotency_key="k-1", branch_id=1, occurred_at=AT)


def examples():
    return {
        "H1": StockSignal(**COMMON, run_id="restock:1:2026-11-10", product_id=7, signal_type="NEAR_EXPIRY",
                          on_hand=120, expiring_batches=[{"expires_on": date(2026, 11, 13), "days_left": 3, "quantity": 40}],
                          evidence_ref="restock:1:2026-11-10:7:expiry"),
        "H2": ClearanceNotice(**COMMON, offer_id="o-1", version=1, status="active", product_ids=(7,),
                              start_at=AT, end_at=AT + timedelta(days=2), reason="clearing"),
        "H3": RestockOrderRecord(**COMMON, run_id="r", product_id=7, supplier_id=101, requested_quantity=30,
                                 trigger_reason="HIGH_DEMAND_LOW_STOCK", status="APPROVED", version=1),
        "H4": KpiPayload(snapshot_ref="snap-1", query_version="q1", branch_id=1, period_start=AT,
                         period_end=AT + timedelta(days=1), gross_sales=Decimal("100"), discounts=Decimal("0"),
                         refunds=Decimal("0"), net_revenue=Decimal("100"), units=4, transactions=2,
                         average_transaction_value=Decimal("50"), gross_profit=Decimal("30")),
        "H5": OfferTotals(**COMMON, offer_id="o-1", version=1, period_start=AT, period_end=AT + timedelta(days=1),
                          redemptions=10, returns=0, discount_amount=Decimal("25.50"), receipts=8),
        "H6": DemandLevelProposal(**COMMON, proposal_id="p-1", product_id=7, current_level="Medium",
                                  proposed_level="High", period_start=date(2026, 10, 13), period_end=date(2026, 11, 10),
                                  units_sold=280, days=28, avg_daily_units=Decimal("10"), source="rules"),
    }


def test_every_handoff_has_a_contract_and_a_json_schema():
    schemas = json_schemas()
    assert set(schemas) == {"H1", "H2", "H3", "H4", "H5", "H6"} == set(CONTRACTS)
    for name, schema in schemas.items():
        assert schema["type"] == "object" and schema["properties"], name


def test_examples_validate_and_round_trip_through_json():
    for name, event in examples().items():
        model = CONTRACTS[name]
        assert model.model_validate_json(event.model_dump_json()) == event, name


def test_unknown_fields_are_refused():
    data = examples()["H1"].model_dump()
    data["recipient"] = "someone@example.com"
    with pytest.raises(ValidationError):
        StockSignal(**data)


def test_datetimes_without_timezone_are_refused():
    data = examples()["H5"].model_dump()
    data["occurred_at"] = datetime(2026, 11, 10, 9, 0)
    with pytest.raises(ValidationError):
        OfferTotals(**data)


def test_near_expiry_signal_must_list_its_batches():
    data = examples()["H1"].model_dump()
    data["expiring_batches"] = ()
    with pytest.raises(ValidationError, match="expiring batches"):
        StockSignal(**data)


def test_clearance_window_must_be_positive():
    data = examples()["H2"].model_dump()
    data["end_at"] = data["start_at"]
    with pytest.raises(ValidationError):
        ClearanceNotice(**data)


def test_restocking_signal_message_through_the_contract():
    """The format Restocking publishes today, checked against H1. Two facts for M2:
    expiry-only signals pass; NEAR_EXPIRY *reorder* signals carry no batches (gap G-1)."""
    pilot = generate_pilot_branch(7)
    evaluation = _evaluate_inventory({"as_of": pilot.as_of.isoformat(), "products": pilot.restock_inventory(),
                                      "settings": pilot.settings, "active_offers": []})
    run = {"run_id": "restock:1:2026-11-10", "branch_id": 1, **evaluation}
    signals, violations = parse_restock_signal_message(restock_signal_message(run), AT)

    overstock = pilot.cases["near_expiry_overstock"]
    assert any(s.product_id == overstock and s.signal_type.value == "NEAR_EXPIRY" for s in signals)
    reorder_near_expiry = {c["product_id"] for c in evaluation["candidates"] if c["trigger_reason"] == "NEAR_EXPIRY"}
    assert reorder_near_expiry and len(violations) == len(reorder_near_expiry)
    assert all("must list its expiring batches" in v for v in violations)
    # idempotency keys are unique and stable
    keys = [s.idempotency_key for s in signals]
    assert len(keys) == len(set(keys))
    again, _ = parse_restock_signal_message(restock_signal_message(run), AT)
    assert [s.idempotency_key for s in again] == keys


def test_malformed_message_reports_violations_instead_of_crashing():
    message = {"idempotency_key": "x", "branch_id": 1,
               "payload": {"run_id": "r", "signals": [{"product_id": "abc", "signal_type": "NOPE"}]}}
    signals, violations = parse_restock_signal_message(message, AT)
    assert signals == [] and len(violations) == 1
