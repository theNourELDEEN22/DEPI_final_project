
from typing import Any

from offers.impact import offer_totals
from offers.guardrails import PriceReader
from offers.monitoring import CampaignStats
from offers.schema import Offer

from reporting.models import OfferReportSummary


def build_offer_report_summary(
    offer: Offer,
    stats: CampaignStats,
    prices: PriceReader,
) -> OfferReportSummary:
    """
    Build reporting offer data using the original Offers calculation.

    Reporting does not recalculate redemptions or discounts.
    """

    totals: dict[str, Any] = offer_totals(
        offer=offer,
        stats=stats,
        prices=prices,
    )

    return OfferReportSummary(
        offer_id=totals["offer_id"],
        version=totals["version"],
        redemptions=totals["redemptions"],
        returns=totals["returns"],
        discount_amount=totals["discount_amount"],
    )


def build_offer_report_summaries(
    records: list[tuple[Offer, CampaignStats]],
    prices: PriceReader,
) -> list[OfferReportSummary]:
    """
    Build summaries for multiple offers using Offers as the source of truth.
    """

    return [
        build_offer_report_summary(
            offer=offer,
            stats=stats,
            prices=prices,
        )
        for offer, stats in records
    ]
