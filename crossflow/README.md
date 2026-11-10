# Epic E: Cross-Workflow and QA (M5)

This package connects Restocking (B), Reporting (C) and Offers (D) through the handoffs H1–H6. It also holds the shared QA pieces: decisions, event contracts, a mock POS, test data, the demand forecast, the scenario test and the runbooks.

It builds only on what is on `main`. It doesn't change any other member's code: where it found a disagreement, that is written down as a decision (`docs/DECISIONS_WEEK1.md`) or a contract gap (`docs/EVENT_CONTRACTS.md`), with a test that shows it.

```powershell
python -m pytest crossflow   # 112 tests + 1 expected failure (open decision D-06)
python -m crossflow.demo     # the full story in about 1 second
```

## Task status

| Task | What | Where | Status |
|---|---|---|---|
| E1 | Week 1 decisions: 13 decisions, each with the conflicts found in the repo and a recommendation | `docs/DECISIONS_WEEK1.md` | ready for the meeting |
| E2 | Event contracts H1–H6, JSON Schemas, adapter for Restocking's current H1 format, 7 gaps found | `contracts.py`, `docs/EVENT_CONTRACTS.md` | ready for review by M2, M3, M4 |
| E3 | Mock POS: success, failure, unclear, lost request, garbage. Idempotent by key; rings up sales with live offers | `mock_pos.py` | ✅ M4's D10/D11 code runs against it |
| E4 | Test data for one pilot branch: 8 weeks of sales, receipts, returns, batches, movements; 7 named cases; CSVs for the upload script | `datagen.py` | ✅ |
| E5 | Demand forecast and baseline (promo days excluded, weekday pattern, fails closed) | `forecast.py` | ✅ Offers reads a forecast number |
| E6 | H1 stock signal becomes an offer candidate linked to the signal | `handoffs.py` | ✅ |
| E7 | Rule: near expiry and overstocked gives an offer, not a restock | `handoffs.route_product` | ✅ covered by a test; one open case (D-06) |
| E8 | Restock draft skips products being cleared (H2) and shows why | `handoffs.clearance_notices` | ✅ through Restocking's own evaluation |
| E9 | Offer end or sell-out triggers a stock re-evaluation | `handoffs.reevaluate` | ✅ |
| E10 | Report offer and restock sections must match Offers and Restocking totals | `reports.py` | ✅ checks ready; ⏳ applied to M3's reports when Epic C lands |
| E11 | Reports never disagree (same snapshot; month equals the sum of its days) | `reports.py` | ✅ checks ready; ⏳ same |
| E12 | Sell-through updates demand levels, only after a human review | `demand.py` | ✅ |
| E13 | Full scenario: near expiry to Manager report, only human approvals | `scenario.py` | ✅ |
| E14 | Failure-test matrix (19 cross-workflow cases) and pilot comparison against a hand calculation | `docs/FAILURE_MATRIX.md`, `tests/test_failures.py` | ✅; 3 rows wait on M1–M3 |
| E15 | Runbooks: delivery_failed, blocked reports, POS reconciliation, escalations; demo | `docs/RUNBOOKS.md`, `ops.py`, `demo.py` | draft, for Operator review |

## What depends on other members

- **Epic C isn't in the repo.** E10/E11 run against `reference_kpis.py`, which implements the proposed C1 definitions. When M3's KPI library exists, feed its payloads to the same checks.
- **Offer data model vs database (D-10)** blocks any real database adapter for Offers. M1 and M4 need to decide it.
- **Restocking has no tests yet (B15).** The E7/E8/E9 tests do exercise its `_evaluate_inventory` decision code.

## Modules

| File | Purpose |
|---|---|
| `contracts.py` | H1–H6 pydantic contracts (frozen, unknown fields refused, timezone-aware) |
| `mock_pos.py` | `MockPOS`, implementing Offers' `PosClient` and `PosStopClient` with scripted faults |
| `datagen.py` | `generate_pilot_branch(seed)` → `PilotData`, plus views for Restocking and Offers |
| `forecast.py` | `DemandForecaster`, `ForecastStore` (ForecastReader), `BaselineService` (BaselineReader) |
| `reference_kpis.py` | reference KPIs (KpiReader) and `SalesCampaignStats` (StatsReader) |
| `handoffs.py` | E6–E9: routing rule, signal → candidate, clearance, re-evaluation |
| `demand.py` | E12: proposals, human review queue, apply with stale check |
| `reports.py` | E10/E11 cross-checks |
| `ops.py` | POS reconciliation and stuck-outbox listing for the runbooks |
| `scenario.py` | E13 end-to-end run; `RuleBasedStrategistLLM` for offline runs |
| `seed_config.py` | reads thresholds straight from the migration seed, so tests can't drift from the database |
