
from reporting.completeness import (
    check_data_completeness,
    wait_for_data_completeness,
)

from reporting.models import (
    ReportingPeriod,
    ReportDataStatus,
    BranchReportData,
    BranchKPIs,
    OfferReportSummary,
)

from reporting.validation import (
    validate_branch_id,
    validate_reporting_period,
    validate_sales_lines,
    validate_returns,
)

from reporting.repository import ReportingRepository

from reporting.kpis import (
    calculate_gross_sales,
    calculate_discounts,
    calculate_net_revenue,
    calculate_units_sold,
    calculate_transactions,
    calculate_cogs,
    calculate_gross_profit,
    calculate_atv,
)

from reporting.offers_adapter import build_offer_report_summaries

from offers.guardrails import PriceReader
from offers.monitoring import CampaignStats
from offers.schema import Offer


def load_branch_report_data(
    repository: ReportingRepository,
    branch_id: int,
    period: ReportingPeriod,
    data_wait_timeout_seconds: float = 0,
    retry_interval_seconds: float = 5,
) -> BranchReportData:
    """
    Load branch data, validate source records,
    calculate available KPIs, and build a branch report.

    Supports complete, partial, and unavailable reports.

    Waits for required data sources until the configured
    timeout is reached.
    """

    validate_branch_id(branch_id)
    validate_reporting_period(period)

    # Wait for required sources before loading report data.
    source_status = wait_for_data_completeness(
        availability_reader=repository.get_source_availability,
        timeout_seconds=data_wait_timeout_seconds,
        retry_interval_seconds=retry_interval_seconds,
    )

    # Read the latest availability after the waiting period.
    available_sources = repository.get_source_availability()

    sales_lines = []
    returns = []
    products = []

    if available_sources.get("sales", False):
        sales_lines = repository.fetch_sales_lines(
            branch_id=branch_id,
            period=period,
        )
        validate_sales_lines(sales_lines)

    if available_sources.get("returns", False):
        returns = repository.fetch_returns(
            branch_id=branch_id,
            period=period,
        )
        validate_returns(returns)

    if available_sources.get("products", False):
        product_ids = sorted({
            int(row["product_id"])
            for row in sales_lines + returns
        })

        products = repository.fetch_products(
            branch_id=branch_id,
            product_ids=product_ids,
        )

    cost_by_product = {
        int(product["product_id"]): product.get("cost_price")
        for product in products
    }

    sales_product_ids = {
        int(row["product_id"])
        for row in sales_lines
    }

    missing_cost_products = [
        product_id
        for product_id in sales_product_ids
        if (
            product_id not in cost_by_product
            or cost_by_product[product_id] is None
        )
    ]

    gross_sales = None
    discounts = None
    units = None
    transactions = None

    if available_sources.get("sales", False):
        discounts = calculate_discounts(sales_lines)
        units = calculate_units_sold(sales_lines)
        transactions = calculate_transactions(sales_lines)

        if available_sources.get("prices", False):
            gross_sales = calculate_gross_sales(sales_lines)

    net_revenue = None

    if (
        available_sources.get("sales", False)
        and available_sources.get("returns", False)
        and available_sources.get("prices", False)
    ):
        net_revenue = calculate_net_revenue(
            sales_lines,
            returns,
        )

    cogs = None

    if (
        available_sources.get("sales", False)
        and available_sources.get("products", False)
        and available_sources.get("cost", False)
        and not missing_cost_products
    ):
        cogs = calculate_cogs(
            sales_lines,
            products,
        )

    gross_profit = None

    if net_revenue is not None and cogs is not None:
        gross_profit = calculate_gross_profit(
            net_revenue,
            cogs,
        )

    atv = None

    if net_revenue is not None and transactions is not None:
        atv = calculate_atv(
            net_revenue,
            transactions,
        )

    kpis = BranchKPIs(
        branch_id=branch_id,
        gross_sales=gross_sales,
        discounts=discounts,
        net_revenue=net_revenue,
        units=units,
        transactions=transactions,
        cogs=cogs,
        gross_profit=gross_profit,
        average_transaction_value=atv,
    )

    missing_sources = list(source_status.missing_sources)

    # Include missing product costs when individual products
    # do not have a usable cost_price.
    if missing_cost_products and "product_costs" not in missing_sources:
        missing_sources.append("product_costs")

    # A source may have changed availability between the last
    # retry check and the latest availability read.
    for source in (
        "sales",
        "returns",
        "products",
        "prices",
        "cost",
        "inventory_movements",
    ):
        if (
            not available_sources.get(source, False)
            and source not in missing_sources
        ):
            missing_sources.append(source)

    if not available_sources.get("sales", False):
        report_status = "unavailable"
    elif missing_sources:
        report_status = "partial"
    else:
        report_status = "complete"

    data_status = ReportDataStatus(
        is_complete=(report_status == "complete"),
        status=report_status,
        missing_sources=tuple(missing_sources),
        cutoff_time=source_status.cutoff_time,
    )

    return BranchReportData(
        branch_id=branch_id,
        period=period,
        sales_lines=sales_lines,
        returns=returns,
        products=products,
        data_status=data_status,
        kpis=kpis,
    )


def attach_offer_data(
    report: BranchReportData,
    offer_records: list[tuple[Offer, CampaignStats]],
    prices: PriceReader,
) -> BranchReportData:
    """
    Attach Offers-generated totals to an existing branch report.

    Discount and redemption calculations are delegated to Offers.
    """

    for offer, _stats in offer_records:
        if offer.branch_id != str(report.branch_id):
            raise ValueError(
                f"Offer {offer.offer_id} belongs to branch "
                f"{offer.branch_id}, not branch {report.branch_id}"
            )

    offer_summaries: list[OfferReportSummary] = (
        build_offer_report_summaries(
            records=offer_records,
            prices=prices,
        )
    )

    return report.model_copy(
        update={"offers": offer_summaries}
    )
