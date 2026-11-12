
# Epic C - KPI & Report Data Contract

## 1. Purpose
Define standardized KPI calculations shared by Branch Reporting (Epic C) and Offers Impact Analysis (Epic D).

## 2. Scope
- Daily Branch Reports
- Monthly Branch Reports
- KPI calculations
- Shared KPI interface for Epic D

## 3. KPI Definitions

| KPI | Definition | Formula |
|---|---|---|
| Gross Sales | Sales value before discounts | Sum of quantity * unit_price |
| Discounts | Total applied discounts | Sum of discount_amount |
| Returns | Total refunded amount | Sum of refund_amount |
| Net Revenue | Revenue after discounts and returns | Gross Sales - Discounts - Returns |
| Units Sold | Total sold quantity | Sum of quantity |
| Transactions | Unique sales receipts | Count distinct transaction_id |
| ATV | Average transaction value | Net Revenue / Transactions |
| Gross Profit | Revenue after COGS | Net Revenue - COGS |

## 4. Source Tables

- sales_transactions
- sales_lines
- returns
- products
- branches

## 5. Data Rules

- All calculations must be filtered by branch_id.
- All calculations must respect the requested reporting period.
- Monetary values use two decimal places.
- Division by zero must return zero or a defined safe result.
- Transactions must be counted using distinct transaction_id.
- Returns must be handled separately from sales.

## 6. Pending Business Decisions

The following points require confirmation before final KPI implementation:

1. Is total_amount before or after discount?
2. Are product prices tax-inclusive?
3. Is historical cost available at sale time?
4. How should partial returns be allocated to reporting periods?

## 7. Integration with Epic D


## 7. Integration with Epic D

Epic D must reuse the same KPI calculations implemented by Epic C.

### Required Interface

The shared KPI service must expose:

- units(branch_id, product_ids, start, end) -> Optional[int]
- net_revenue(branch_id, product_ids, start, end) -> Optional[Decimal]
- gross_profit(branch_id, product_ids, start, end) -> Optional[Decimal]

### Parameters

- branch_id: Target branch identifier.
- product_ids: List of product identifiers.
- start: Reporting period start (inclusive).
- end: Reporting period end (exclusive).

### Return Rules

- Return None when required KPI data is unavailable.
- Monetary values must use Decimal.
- All calculations must be branch-scoped.
- All calculations must respect the requested product IDs and period.
- Epic D must not independently recalculate these KPIs.

### Baseline Integration

Baseline calculations are provided separately by Epic E through BaselineReader.

Epic C is responsible for actual KPI calculations only.

### Compatibility

The implementation must remain compatible with the KpiReader Protocol in offers/impact.py.


## 8. Version
Contract Version: 0.1.0
Status: Draft - Pending Team Review
