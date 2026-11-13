# Event contracts H1–H6 (E2): review copy

**Status:** for review by M2, M3 and M4 (E2 done-when). The code is the source of truth: `crossflow/contracts.py`. To print the JSON Schemas:

```powershell
python -c "import json; from crossflow.contracts import json_schemas; print(json.dumps(json_schemas(), indent=2, default=str))"
```

## Every event

| Field | Rule |
|---|---|
| `schema_version` | `1`; bump it on any breaking change |
| `event_type` | fixed per contract, e.g. `restocking.stock_signal` |
| `idempotency_key` | stable for the same fact; consumers ignore a key they have already processed |
| `branch_id` | database BIGINT, > 0 |
| `occurred_at` | must include a timezone |
| `producer` | who emitted it |

Unknown fields are **refused**, so a typo fails instead of being silently dropped.

## The handoffs

| | Contract | From → To | Key fields | Review by |
|---|---|---|---|---|
| H1 | `StockSignal` | Restocking → Offers, Reporting | run_id, product_id, signal_type, on_hand, expiring_batches, **evidence_ref** | M2 |
| H2 | `ClearanceNotice` | Offers → Restocking | offer_id, version, status (approved/active), product_ids, start/end | M2, M4 |
| H3 | `RestockOrderRecord` | Restocking → Reporting | one `restock_orders` row: quantity, trigger, status, version, hash | M2, M3 |
| H4 | `KpiPayload` | Reporting → Offers, QA | the KPIs of D-05 plus **snapshot_ref** and **query_version** | M3 |
| H5 | `OfferTotals` | Offers → Reporting | redemptions, returns, discount_amount, receipts, period | M3, M4 |
| H6 | `DemandLevelProposal` | sell-through → config, **after review** | current/proposed level, units, days, source (rules/ai) | M2, M5 |

Rules inside the contracts:
- A `NEAR_EXPIRY` signal must list its expiring batches.
- A clearance window must be positive.
- KPI values that can't be computed are `null` (N/A), never 0.

## Gaps found in the current code (please fix or decide)

| # | Gap | Where | Owner |
|---|---|---|---|
| G-1 | Restocking's `NEAR_EXPIRY` **reorder** signals carry no batches, so H1 refuses them. Expiry-only signals are fine. | `restocking/repository.py` `publish_stock_signals`: candidates are published without `batches` | M2 |
| G-2 | All of a run's signals go in **one** outbox message keyed `restocking:{run_id}:H1` with `ignore_duplicates`. When a run is redrafted after a rejection, the new signals are silently dropped. | same | M2 |
| G-3 | Offer ids are UUID strings in code but BIGINT in the `offers` and `sales_transactions.offer_id` columns. | D-10 | M1, M4 |
| G-4 | Branch and product ids are strings in `offers/`, BIGINT everywhere else. Adapters convert, and `clearance_notices` refuses non-numeric ids. | D-10 | M4 |
| G-5 | Offers' `CampaignStats` has no **receipts** count, which H5 needs. The scenario takes it from the POS redemption log. | `offers/monitoring.py` | M4 |
| G-6 | Restocking reads offer status `'ACTIVE'` (database spelling); Offers uses lowercase and doesn't persist yet. H2 also needs `approved` (D-08). | `restocking/repository.py` `load_active_offers` | M2, M4 |
| G-7 | `offers.impact.KpiReader` takes no `snapshot_ref`, so XW-5 ("same snapshot") can't be enforced for the impact numbers. Proposed: add `snapshot_ref` to its three functions. | `offers/impact.py` | M3, M4 |

`crossflow/handoffs.py::restock_signal_message` copies Restocking's H1 format so the contracts can be tested without Supabase. If you change `publish_stock_signals`, change it too; `test_contracts.py` will show the effect.

**Reviewed:** M2 ☐ M3 ☐ M4 ☐
