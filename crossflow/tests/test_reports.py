"""E10 / E11 report cross-checks, and the pilot comparison against hand calculations (E14)."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from offers.guardrails import InMemoryPrices, PriceInfo
from crossflow.contracts import OfferTotals, RestockOrderRecord
from crossflow.datagen import generate_pilot_branch
from crossflow.reference_kpis import ReferenceKpis
from crossflow.reports import (
    check_offer_section,
    check_restock_section,
    compare_daily_with_monthly,
    compare_same_snapshot,
    offer_section,
    restock_section,
)

CAIRO = ZoneInfo("Africa/Cairo")
AT = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)


def line(txn, product, qty, price, *, day=date(2026, 11, 5), at=time(10, 0), offer=None, discount="0"):
    price, discount = Decimal(price), Decimal(discount)
    return {"sale_line_id": txn * 10 + product, "transaction_id": txn, "date": day, "time": at, "branch_id": 1,
            "product_id": product, "quantity": qty, "unit_price": price,
            "total_amount": price * qty - discount, "offer_id": offer, "discount_amount": discount}


# --- the pilot comparison: agent numbers match manual figures ------------------------------


def test_reference_kpis_match_a_hand_calculation():
    sales = [
        line(1, 7, 3, "10"), line(1, 8, 1, "25"),                                  # receipt 1: 30 + 25
        line(2, 7, 2, "10", at=time(18, 30), offer="o-1", discount="4.00"),        # receipt 2: 20 - 4
        line(3, 7, 5, "10", day=date(2026, 11, 6), at=time(0, 10)),                # next day: outside
    ]
    returns = [{"branch_id": 1, "transaction_id": 1, "product_id": 8, "quantity": 1, "refund_amount": Decimal("25"),
                "returned_at": datetime(2026, 11, 5, 20, 0, tzinfo=CAIRO)}]
    prices = InMemoryPrices({("1", "7"): PriceInfo(Decimal("10"), Decimal("6")),
                             ("1", "8"): PriceInfo(Decimal("25"), Decimal("15"))})
    day = datetime(2026, 11, 5, tzinfo=CAIRO)
    kpis = ReferenceKpis(sales, returns, prices).compute(1, day, day + timedelta(days=1))
    # Hand calculation:
    #   gross = 30 + 25 + 20 = 75; discounts = 4; refunds = 25; net = 75 - 4 - 25 = 46
    #   units = 3 + 1 + 2 - 1 returned = 5; transactions = 2; ATV = 46 / 2 = 23
    #   cost = 5 units of product 7 x 6 = 30 (product 8 nets to 0); profit = 46 - 30 = 16
    assert kpis == {
        "gross_sales": Decimal("75.00"), "discounts": Decimal("4.00"), "refunds": Decimal("25.00"),
        "net_revenue": Decimal("46.00"), "units": 5, "transactions": 2,
        "average_transaction_value": Decimal("23.00"), "gross_profit": Decimal("16.00"),
    }


def test_kpis_are_na_when_there_is_nothing_or_cost_is_missing():
    day = datetime(2026, 11, 5, tzinfo=CAIRO)
    empty = ReferenceKpis([], [], InMemoryPrices()).compute(1, day, day + timedelta(days=1))
    assert empty["average_transaction_value"] is None and empty["transactions"] == 0
    no_cost = ReferenceKpis([line(1, 7, 1, "10")], [], InMemoryPrices()).compute(1, day, day + timedelta(days=1))
    assert no_cost["gross_profit"] is None and no_cost["net_revenue"] == Decimal("10.00")


# --- E10: offers section vs Offers records (H5) ----------------------------------------------


def h5(redemptions=2, discount="4.00", receipts=1, offer="o-1"):
    return OfferTotals(idempotency_key="k", branch_id=1, occurred_at=AT, offer_id=offer, version=1,
                       period_start=AT - timedelta(days=1), period_end=AT, redemptions=redemptions, returns=0,
                       discount_amount=Decimal(discount), receipts=receipts)


SALES = [line(1, 7, 3, "10"), line(2, 7, 2, "10", offer="o-1", discount="4.00")]
DAY = (datetime(2026, 11, 5, tzinfo=CAIRO), datetime(2026, 11, 6, tzinfo=CAIRO))


def test_offer_section_matches_offers_records():
    section = offer_section(SALES, 1, *DAY)
    assert section == [{"offer_id": "o-1", "redemptions": 2, "discount_amount": Decimal("4.00"), "receipts": 1, "lines": 1}]
    assert check_offer_section(section, [h5()]) == {"status": "OK", "offers": section, "mismatches": []}


@pytest.mark.parametrize("record, problem", [
    (h5(redemptions=3), "redemptions"),
    (h5(receipts=2), "receipts"),
    (h5(discount="4.02"), "discounts"),
])
def test_any_difference_blocks_the_section(record, problem):
    check = check_offer_section(offer_section(SALES, 1, *DAY), [record])
    assert check["status"] == "BLOCKED" and problem in check["mismatches"][0]


def test_one_cent_rounding_per_line_is_allowed():
    assert check_offer_section(offer_section(SALES, 1, *DAY), [h5(discount="4.01")])["status"] == "OK"


def test_offers_missing_on_either_side_block_the_section():
    section = offer_section(SALES, 1, *DAY)
    assert "no record" in check_offer_section(section, [])["mismatches"][0]
    assert "missing from the report" in check_offer_section([], [h5()])["mismatches"][0]


def order(status, qty, pid=7):
    return RestockOrderRecord(idempotency_key=f"o-{pid}-{status}", branch_id=1, occurred_at=AT, run_id="r",
                              product_id=pid, supplier_id=101, requested_quantity=qty,
                              trigger_reason="HIGH_DEMAND_LOW_STOCK", status=status, version=1)


def test_restock_section_matches_restock_orders():
    records = [order("APPROVED", 30), order("APPROVED", 10, pid=8), order("REJECTED", 5, pid=9)]
    section = restock_section(records)
    assert section == {"APPROVED": {"orders": 2, "units": 40}, "REJECTED": {"orders": 1, "units": 5}}
    assert check_restock_section(section, records)["status"] == "OK"
    wrong = {**section, "APPROVED": {"orders": 2, "units": 45}}
    assert check_restock_section(wrong, records)["status"] == "BLOCKED"


# --- E11: daily and monthly reports never disagree ----------------------------------------------


@pytest.fixture(scope="module")
def october():
    pilot = generate_pilot_branch(7)
    kpis = ReferenceKpis(pilot.sales, pilot.returns, pilot.prices())
    snap = dict(snapshot_ref="snap-2026-11-01T02:00", query_version="kpi-v1")
    days = [datetime(2026, 10, d, tzinfo=CAIRO) for d in range(1, 32)]
    dailies = [kpis.payload(1, d, d + timedelta(days=1), **snap) for d in days]
    monthly = kpis.payload(1, days[0], datetime(2026, 11, 1, tzinfo=CAIRO), **snap)
    return dailies, monthly


def test_month_is_exactly_the_sum_of_its_days(october):
    dailies, monthly = october
    assert monthly.transactions > 0
    assert compare_daily_with_monthly(dailies, monthly) == []


def test_a_wrong_daily_figure_is_caught(october):
    dailies, monthly = october
    broken = list(dailies)
    broken[9] = broken[9].model_copy(update={"net_revenue": broken[9].net_revenue + Decimal("1")})
    problems = compare_daily_with_monthly(broken, monthly)
    assert any(p.startswith("net_revenue") for p in problems)


def test_a_different_snapshot_or_a_missing_day_is_caught(october):
    dailies, monthly = october
    other = list(dailies)
    other[0] = other[0].model_copy(update={"snapshot_ref": "snap-later"})
    assert any("snapshot" in p for p in compare_daily_with_monthly(other, monthly))
    gap = dailies[:5] + dailies[6:]
    assert any("gap" in p for p in compare_daily_with_monthly(gap, monthly))


def test_two_reports_from_the_same_snapshot_are_identical(october):
    dailies, _ = october
    assert compare_same_snapshot(dailies[3], dailies[3].model_copy()) == []
    changed = dailies[3].model_copy(update={"units": dailies[3].units + 1})
    assert compare_same_snapshot(dailies[3], changed) == [f"units: {dailies[3].units} vs {dailies[3].units + 1}"]
