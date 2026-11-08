import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from offers.guardrails import InMemoryPrices, PriceInfo
from offers.impact import (
    ImpactUnavailable,
    Metrics,
    calculate_impact,
    offer_totals,
)
from offers.monitoring import CampaignStats
from offers.schema import BundleItem, Offer, OfferStatus, OfferType

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
START = NOW
END = NOW + timedelta(days=7)
MID = NOW + timedelta(days=3)

PRICES = InMemoryPrices(
    {
        ("b1", "p1"): PriceInfo(price=Decimal("100"), cost=Decimal("60")),
        ("b1", "p2"): PriceInfo(price=Decimal("100"), cost=Decimal("10")),
    }
)


class SpyKpi:
    """Stands for M3's KPI functions. Records every call so tests can see what was asked."""

    def __init__(self, units=150, revenue=Decimal("12000"), profit=Decimal("3000")):
        self.values = {"units": units, "net_revenue": revenue, "gross_profit": profit}
        self.calls: list[tuple] = []

    def _answer(self, name, branch_id, product_ids, start, end):
        self.calls.append((name, branch_id, tuple(product_ids), start, end))
        return self.values[name]

    def units(self, *args):
        return self._answer("units", *args)

    def net_revenue(self, *args):
        return self._answer("net_revenue", *args)

    def gross_profit(self, *args):
        return self._answer("gross_profit", *args)


class FakeBaseline:
    def __init__(self, metrics=Metrics(100, Decimal("10000"), Decimal("2800"))):
        self.metrics = metrics
        self.calls: list[tuple] = []

    def get_baseline(self, offer_id, version, branch_id, product_ids, start, end):
        self.calls.append((offer_id, version, branch_id, tuple(product_ids), start, end))
        return self.metrics


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


def stats(redemptions=40, returns=2, sold=150, stock=60):
    return CampaignStats(as_of=END, units_sold=sold, redemptions=redemptions, returns=returns, stock_on_hand=stock)


def impact(offer, *, kpi=None, baseline=None, st=None, prices=PRICES, window_end=None, now=MID):
    return calculate_impact(
        offer,
        stats=st or stats(),
        kpi=kpi or SpyKpi(),
        baseline=baseline or FakeBaseline(),
        prices=prices,
        window_end=window_end,
        now=now,
    )


def test_final_impact_numbers():
    r = impact(make_offer())
    assert r.is_final
    assert r.actual == Metrics(150, Decimal("12000.00"), Decimal("3000.00"))
    assert r.baseline == Metrics(100, Decimal("10000.00"), Decimal("2800.00"))
    assert r.incremental == Metrics(50, Decimal("2000.00"), Decimal("200.00"))
    assert r.discount_cost == Decimal("800.00")  # 40 redemptions x 20 discount
    assert r.roi == Decimal("0.25")  # 200 / 800
    assert r.redemptions == 40 and r.returns == 2
    assert r.units_cleared == 38
    assert r.stock_on_hand == 60


def test_the_kpi_functions_are_called_for_the_branch_products_and_window():
    kpi = SpyKpi()
    baseline = FakeBaseline()
    impact(make_offer(), kpi=kpi, baseline=baseline)
    assert sorted(c[0] for c in kpi.calls) == ["gross_profit", "net_revenue", "units"]
    for _name, branch, products, start, end in kpi.calls:
        assert (branch, products, start, end) == ("b1", ("p1",), START, END)
    assert baseline.calls[0][2:] == ("b1", ("p1",), START, END)


def test_figures_come_from_the_kpi_functions_not_from_local_maths():
    r1 = impact(make_offer(), kpi=SpyKpi(revenue=Decimal("12000")))
    r2 = impact(make_offer(), kpi=SpyKpi(revenue=Decimal("15000")))
    assert r2.actual.net_revenue == Decimal("15000.00")
    assert r2.incremental.net_revenue - r1.incremental.net_revenue == Decimal("3000.00")


def test_negative_impact_is_reported_as_it_is():
    r = impact(make_offer(), kpi=SpyKpi(units=90, revenue=Decimal("9000"), profit=Decimal("2000")))
    assert r.incremental == Metrics(-10, Decimal("-1000.00"), Decimal("-800.00"))
    assert r.roi == Decimal("-1.00")


def test_no_redemptions_means_no_roi():
    r = impact(make_offer(), st=stats(redemptions=0, returns=0))
    assert r.discount_cost == Decimal("0.00")
    assert r.roi is None
    assert r.units_cleared == 0


# --- window and status ---------------------------------------------------------


def test_active_offer_gives_an_interim_report_up_to_now():
    kpi = SpyKpi()
    r = impact(make_offer(status=OfferStatus.ACTIVE), kpi=kpi, now=MID)
    assert not r.is_final
    assert r.window_end == MID
    assert kpi.calls[0][4] == MID


def test_active_offer_past_its_end_date_is_capped_at_the_end_date():
    r = impact(make_offer(status=OfferStatus.ACTIVE), now=END + timedelta(days=2))
    assert r.window_end == END


def test_ended_offer_uses_its_end_date():
    assert impact(make_offer(status=OfferStatus.ENDED)).window_end == END


def test_stopped_offer_needs_the_stop_time():
    offer = make_offer(status=OfferStatus.STOPPED)
    with pytest.raises(ImpactUnavailable):
        impact(offer)
    kpi = SpyKpi()
    r = impact(offer, kpi=kpi, window_end=MID)
    assert r.is_final and r.window_end == MID
    assert kpi.calls[0][3:] == (START, MID)


@pytest.mark.parametrize("status", [OfferStatus.DRAFT, OfferStatus.PENDING_APPROVAL, OfferStatus.APPROVED, OfferStatus.REJECTED])
def test_an_offer_that_never_ran_has_no_impact(status):
    with pytest.raises(ImpactUnavailable):
        impact(make_offer(status=status))


def test_empty_window_is_refused():
    with pytest.raises(ImpactUnavailable):
        impact(make_offer(status=OfferStatus.ACTIVE), now=START)


# --- missing data is refused, never guessed ---------------------------------------


@pytest.mark.parametrize("missing", ["units", "net_revenue", "gross_profit"])
def test_missing_kpi_data_is_refused(missing):
    kpi = SpyKpi()
    kpi.values[missing] = None
    with pytest.raises(ImpactUnavailable):
        impact(make_offer(), kpi=kpi)


def test_missing_baseline_is_refused():
    with pytest.raises(ImpactUnavailable):
        impact(make_offer(), baseline=FakeBaseline(metrics=None))


def test_missing_prices_are_refused():
    with pytest.raises(ImpactUnavailable):
        impact(make_offer(), prices=InMemoryPrices())


# --- bundle and report feed ----------------------------------------------------------


def test_bundle_discount_cost_uses_the_bundle_price():
    bundle = make_offer(
        type=OfferType.BUNDLE,
        product_ids=["p1", "p2"],
        discount_percent=None,
        bundle_items=[BundleItem(product_id="p1", quantity=1), BundleItem(product_id="p2", quantity=1)],
        bundle_price=Decimal("150"),
    )
    kpi = SpyKpi()
    r = impact(bundle, kpi=kpi, st=stats(redemptions=10, returns=0))
    assert r.discount_cost == Decimal("500.00")  # (200 - 150) x 10 bundles
    assert kpi.calls[0][2] == ("p1", "p2")


def test_offer_totals_feed_the_daily_report():
    totals = offer_totals(make_offer(), stats(redemptions=40, returns=2), PRICES)
    offer = make_offer()
    assert totals["redemptions"] == 40 and totals["returns"] == 2
    assert totals["discount_amount"] == "800.00"
    assert set(totals) == {"offer_id", "version", "redemptions", "returns", "discount_amount"}
    assert totals["version"] == offer.version


def test_report_is_json_serializable():
    d = impact(make_offer()).to_dict()
    json.dumps(d)
    assert d["roi"] == "0.25"
    assert d["incremental"]["net_revenue"] == "2000.00"
