
from datetime import datetime, timezone
from decimal import Decimal

from offers.guardrails import InMemoryPrices, PriceInfo
from offers.impact import offer_totals
from offers.monitoring import CampaignStats
from offers.schema import Offer, OfferStatus, OfferType

from reporting.offers_adapter import build_offer_report_summary


def test_reporting_offer_summary_matches_offers_totals():
    offer = Offer(
        offer_id="offer-001",
        version=1,
        branch_id="1",
        type=OfferType.DISCOUNT,
        status=OfferStatus.ACTIVE,
        product_ids=["product-001"],
        start_date=datetime(2026, 11, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 11, 30, tzinfo=timezone.utc),
        quantity_cap=100,
        discount_percent=Decimal("20"),
    )

    stats = CampaignStats(
        as_of=datetime(2026, 11, 11, tzinfo=timezone.utc),
        units_sold=50,
        redemptions=10,
        returns=2,
        stock_on_hand=40,
    )

    prices = InMemoryPrices(
        data={
            ("1", "product-001"): PriceInfo(
                price=Decimal("100"),
                cost=Decimal("50"),
            )
        }
    )

    # Original Offers calculation
    expected = offer_totals(offer, stats, prices)

    # Reporting adapter
    actual = build_offer_report_summary(
        offer=offer,
        stats=stats,
        prices=prices,
    )

    assert actual.offer_id == expected["offer_id"]
    assert actual.version == expected["version"]
    assert actual.redemptions == expected["redemptions"]
    assert actual.returns == expected["returns"]
    assert actual.discount_amount == Decimal(expected["discount_amount"])


def test_offer_discount_amount_uses_offers_calculation():
    offer = Offer(
        offer_id="offer-002",
        version=1,
        branch_id="1",
        type=OfferType.DISCOUNT,
        status=OfferStatus.ACTIVE,
        product_ids=["product-001"],
        start_date=datetime(2026, 11, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 11, 30, tzinfo=timezone.utc),
        quantity_cap=100,
        discount_percent=Decimal("20"),
    )

    stats = CampaignStats(
        as_of=datetime(2026, 11, 11, tzinfo=timezone.utc),
        units_sold=50,
        redemptions=10,
        returns=2,
        stock_on_hand=40,
    )

    prices = InMemoryPrices(
        data={
            ("1", "product-001"): PriceInfo(
                price=Decimal("100"),
                cost=Decimal("50"),
            )
        }
    )

    summary = build_offer_report_summary(offer, stats, prices)

    # 20% of 100 = 20 discount per redeemed unit.
    # 10 redemptions = 200 total discount.
    assert summary.discount_amount == Decimal("200.00")


from reporting.models import (
    BranchReportData,
    ReportingPeriod,
    ReportDataStatus,
)
from reporting.report_service import attach_offer_data
from reporting.detailed_report import build_detailed_report


def test_offer_totals_match_final_detailed_report():
    offer = Offer(
        offer_id="offer-003",
        version=1,
        branch_id="1",
        type=OfferType.DISCOUNT,
        status=OfferStatus.ACTIVE,
        product_ids=["product-001"],
        start_date=datetime(2026, 11, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 11, 30, tzinfo=timezone.utc),
        quantity_cap=100,
        discount_percent=Decimal("20"),
    )

    stats = CampaignStats(
        as_of=datetime(2026, 11, 11, tzinfo=timezone.utc),
        units_sold=50,
        redemptions=10,
        returns=2,
        stock_on_hand=40,
    )

    prices = InMemoryPrices(
        data={
            ("1", "product-001"): PriceInfo(
                price=Decimal("100"),
                cost=Decimal("50"),
            )
        }
    )

    # Calculate expected values using Offers directly.
    expected = offer_totals(offer, stats, prices)

    # Create the original branch report.
    report = BranchReportData(
        branch_id=1,
        period=ReportingPeriod(
            start=datetime(2026, 11, 1, tzinfo=timezone.utc),
            end=datetime(2026, 12, 1, tzinfo=timezone.utc),
        ),
        data_status=ReportDataStatus(
            is_complete=True,
            status="complete",
        ),
    )

    # Attach Offers data through the reporting adapter.
    report_with_offers = attach_offer_data(
        report=report,
        offer_records=[(offer, stats)],
        prices=prices,
    )

    # Build the final detailed report.
    detailed_report = build_detailed_report(report_with_offers)

    offer_row = detailed_report["offers_summary"]["offers"][0]
    totals = detailed_report["offers_summary"]["totals"]

    # Verify individual offer values.
    assert offer_row["offer_id"] == expected["offer_id"]
    assert offer_row["version"] == expected["version"]
    assert offer_row["redemptions"] == expected["redemptions"]
    assert offer_row["returns"] == expected["returns"]
    assert offer_row["discount_amount"] == expected["discount_amount"]

    # Verify final report totals.
    assert totals["offers_count"] == 1
    assert totals["redemptions"] == expected["redemptions"]
    assert totals["returns"] == expected["returns"]
    assert totals["discount_amount"] == expected["discount_amount"]
