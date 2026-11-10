import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offers.guardrails import InMemoryPrices, PriceInfo
from offers.impact import Metrics, calculate_impact
from offers.manager_report import (
    ESTIMATE_LABEL,
    InMemoryOutbox,
    ReportNotAllowed,
    build_manager_report,
    queue_manager_report,
    render_report_text,
)
from offers.monitoring import CampaignStats
from offers.schema import Offer, OfferStatus, OfferType

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
START = NOW
END = NOW + timedelta(days=7)

PRICES = InMemoryPrices({("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("60"))})


class Kpi:
    def units(self, *a):
        return 150

    def net_revenue(self, *a):
        return Decimal("12000")

    def gross_profit(self, *a):
        return Decimal("3000")


class Baseline:
    def get_baseline(self, *a):
        return Metrics(100, Decimal("10000"), Decimal("2800"))


def make_offer(**over) -> Offer:
    base = dict(
        branch_id="b1",
        type=OfferType.DISCOUNT,
        product_ids=["p1"],
        start_date=START,
        end_date=END,
        discount_percent=Decimal("20"),
        quantity_cap=100,
        evidence_ref="sig-1",
        forecast_ref="fc-1",
        status=OfferStatus.ENDED,
    )
    base.update(over)
    return Offer(**base)


def impact_for(offer):
    stats = CampaignStats(as_of=END, units_sold=150, redemptions=40, returns=2, stock_on_hand=60)
    return calculate_impact(offer, stats=stats, kpi=Kpi(), baseline=Baseline(), prices=PRICES, now=END)


def report(offer=None, stock_at_start=200):
    offer = offer or make_offer()
    return build_manager_report(offer, impact_for(offer), prices=PRICES, stock_at_start=stock_at_start)


# --- measured figures --------------------------------------------------------------


def test_measured_figures_are_the_impact_figures():
    m = report()["measured"]
    assert m["net_revenue"] == "12000.00"
    assert m["gross_profit"] == "3000.00"
    assert m["units"] == 150
    assert m["incremental_net_revenue"] == "2000.00"
    assert m["incremental_gross_profit"] == "200.00"
    assert m["discount_cost"] == "800.00"
    assert m["roi"] == "0.25"
    assert m["stock_cleared_units"] == 38
    assert m["redemptions"] == 40 and m["returns"] == 2


def test_measured_figures_are_not_labeled_as_estimates():
    text = render_report_text(report())
    measured_part = text.split("ESTIMATE")[0]
    assert "ESTIMATE" not in measured_part
    assert "is_estimate" not in json.dumps(report()["measured"])


# --- the waste reduction is a labeled estimate ----------------------------------------


def test_waste_reduction_is_labeled_as_an_estimate_with_method_and_assumptions():
    waste = report(stock_at_start=200)["estimates"]["waste_reduction"]
    assert waste["label"] == ESTIMATE_LABEL
    assert waste["is_estimate"] is True
    assert waste["units_saved"] == 38          # min(38 cleared, 100 expected leftover)
    assert waste["value"] == "2280.00"         # 38 x cost 60
    assert waste["method"]
    assert len(waste["assumptions"]) >= 2


def test_units_saved_cannot_exceed_the_expected_leftover():
    waste = report(stock_at_start=120)["estimates"]["waste_reduction"]
    assert waste["units_saved"] == 20          # leftover = 120 - 100 baseline
    assert waste["value"] == "1200.00"


def test_no_expected_leftover_means_zero_saved_but_still_labeled():
    waste = report(stock_at_start=80)["estimates"]["waste_reduction"]
    assert waste["units_saved"] == 0
    assert waste["value"] == "0.00"
    assert waste["label"] == ESTIMATE_LABEL


def test_unknown_stock_at_start_leaves_the_estimate_out_instead_of_guessing():
    r = report(stock_at_start=None)
    assert r["estimates"]["waste_reduction"] is None
    assert "ESTIMATE: waste reduction not available" in render_report_text(r)


def test_every_waste_line_in_the_text_says_estimate():
    for stock in (200, None):
        text = render_report_text(report(stock_at_start=stock))
        waste_lines = [ln for ln in text.splitlines() if "waste" in ln.lower()]
        assert waste_lines
        assert all("ESTIMATE" in ln for ln in waste_lines)


def test_report_is_json_serializable():
    json.dumps(report())


# --- who gets a report ----------------------------------------------------------------


def test_interim_report_is_not_built():
    offer = make_offer(status=OfferStatus.ACTIVE)
    stats = CampaignStats(as_of=NOW, units_sold=10, redemptions=5, returns=0, stock_on_hand=50)
    interim = calculate_impact(offer, stats=stats, kpi=Kpi(), baseline=Baseline(), prices=PRICES, now=NOW + timedelta(days=2))
    with pytest.raises(ReportNotAllowed):
        build_manager_report(offer, interim, prices=PRICES, stock_at_start=200)


def test_impact_of_another_version_is_refused():
    offer = make_offer()
    other = offer.model_copy(update={"version": 2})
    with pytest.raises(ReportNotAllowed):
        build_manager_report(other, impact_for(offer), prices=PRICES, stock_at_start=200)


# --- outbox ---------------------------------------------------------------------------


def test_report_goes_to_the_outbox_for_the_finance_manager():
    outbox = InMemoryOutbox()
    r = report()
    assert queue_manager_report(r, outbox, now=END) is True
    msg = outbox.messages[0]
    assert msg.recipient_role == "finance_manager"
    assert r["offer_id"] in msg.subject
    assert msg.payload == r
    assert "ESTIMATE" in msg.body
    assert msg.created_at == END


def test_the_same_campaign_is_queued_only_once():
    outbox = InMemoryOutbox()
    r = report()
    assert queue_manager_report(r, outbox) is True
    assert queue_manager_report(r, outbox) is False
    assert len(outbox.messages) == 1


def test_a_new_version_gets_its_own_message():
    outbox = InMemoryOutbox()
    v1 = make_offer()
    v2 = v1.model_copy(update={"version": 2})
    assert queue_manager_report(report(v1), outbox) is True
    assert queue_manager_report(report(v2), outbox) is True
    assert len({m.dedup_key for m in outbox.messages}) == 2
