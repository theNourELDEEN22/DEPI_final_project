"""E5 - demand forecast and baseline."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from offers.guardrails import InMemoryPrices, PriceInfo
from crossflow.forecast import BaselineService, DemandForecaster, ForecastConfig, ForecastStore, _day_shares

CAIRO = ZoneInfo("Africa/Cairo")
DAY = date(2026, 11, 10)  # a Tuesday
START = datetime.combine(DAY, time.min, CAIRO)


def lines(per_day, days=28, product=7, promo_days=(), price=Decimal("10")):
    out, n = [], 0
    for back in range(1, days + 1):
        day = DAY - timedelta(days=back)
        qty = per_day(day) if callable(per_day) else per_day
        if qty:
            n += 1
            out.append({"branch_id": 1, "product_id": product, "date": day, "quantity": qty,
                        "total_amount": price * qty, "offer_id": "promo" if day in promo_days else None})
    return out


def test_flat_demand_forecast_scales_with_the_window():
    f = DemandForecaster(lines(5))
    assert f.forecast_units(1, 7, START, START + timedelta(days=2)) == Decimal("10.00")
    assert f.forecast_units(1, 7, START, START + timedelta(hours=12)) == Decimal("2.50")


def test_promo_days_are_left_out_of_normal_demand():
    promo = {DAY - timedelta(days=d) for d in (1, 2, 3)}
    spiky = lines(lambda d: 40 if d in promo else 5, promo_days=promo)
    assert DemandForecaster(spiky).forecast_units(1, 7, START, START + timedelta(days=1)) == Decimal("5.00")


def test_not_enough_history_gives_no_forecast():
    f = DemandForecaster(lines(5, days=28, promo_days={DAY - timedelta(days=d) for d in range(1, 23)}))
    assert f.forecast_units(1, 7, START, START + timedelta(days=1)) is None  # only 6 clean days
    store = ForecastStore(f, lambda: START)
    assert store.create(1, 7, START, START + timedelta(days=1)) is None


def test_days_without_sales_count_as_zero():
    every_other = lines(lambda d: 10 if d.toordinal() % 2 else 0)
    assert DemandForecaster(every_other).forecast_units(1, 7, START, START + timedelta(days=28)) == Decimal("140.00")


def test_weekday_pattern_is_kept_but_shrunk_toward_one():
    fridays = lines(lambda d: 10 if d.weekday() == 4 else 5, days=56)
    shrunk = DemandForecaster(fridays, ForecastConfig(lookback_days=56)).profile(1, 7, DAY)
    raw = DemandForecaster(fridays, ForecastConfig(lookback_days=56, weekday_shrinkage=0)).profile(1, 7, DAY)
    tiny = Decimal("1e-20")
    assert abs(raw.weekday_index[4] - Decimal("1.75")) < tiny    # 10 / (320 / 56)
    assert Decimal(1) < shrunk.weekday_index[4] < raw.weekday_index[4]
    assert abs(shrunk.weekday_index[4] - Decimal("1.5")) < tiny  # (8 x 1.75 + 4) / 12


def test_shrinkage_keeps_the_weekly_total():
    fridays = lines(lambda d: 10 if d.weekday() == 4 else 5, days=56)
    f = DemandForecaster(fridays, ForecastConfig(lookback_days=56))
    assert f.forecast_units(1, 7, START, START + timedelta(days=7)) == Decimal("40.00")  # 7 x 320/56


def test_day_shares_cover_whole_days_across_october_including_dst():
    start = datetime(2026, 10, 1, tzinfo=CAIRO)
    end = datetime(2026, 11, 1, tzinfo=CAIRO)
    shares = _day_shares(start, end)
    assert len(shares) == 31 and all(share == 1 for _day, share in shares)


def test_forecast_store_is_readable_by_reference_and_deterministic():
    store = ForecastStore(DemandForecaster(lines(5)), lambda: START)
    forecast = store.create_horizon(1, 7, START)
    assert forecast.expected_units == Decimal("35.00")  # 7-day horizon
    assert store.get_forecast(forecast.ref) is forecast
    assert store.create_horizon(1, 7, START).ref == forecast.ref
    assert store.get_forecast("fc:unknown") is None


def test_baseline_prices_forecast_units_at_normal_net_price_and_current_cost():
    prices = InMemoryPrices({("1", "7"): PriceInfo(price=Decimal("12"), cost=Decimal("6"))})
    baseline = BaselineService(DemandForecaster(lines(5, price=Decimal("10"))), prices)
    metrics = baseline.get_baseline("o", 1, "1", ["7"], START, START + timedelta(days=2))
    assert (metrics.units, metrics.net_revenue, metrics.gross_profit) == (10, Decimal("100.00"), Decimal("40.00"))


def test_baseline_is_refused_when_anything_is_missing():
    baseline = BaselineService(DemandForecaster(lines(5)), InMemoryPrices())
    assert baseline.get_baseline("o", 1, "1", ["7"], START, START + timedelta(days=1)) is None


def test_bad_configuration_and_empty_window():
    with pytest.raises(ValueError):
        ForecastConfig(lookback_days=5, min_history_days=7)
    with pytest.raises(ValueError):
        DemandForecaster([]).forecast_units(1, 7, START, START)
