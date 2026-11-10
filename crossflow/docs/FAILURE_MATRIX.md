# Failure-test matrix (E14)

Every failure case the project promises to handle, where it's handled, and the test that proves it. Run all of them with `python -m pytest`.

**Status:** ✅ test passes · ⏳ waiting on another epic · ⚠️ known gap, decision needed

## Cross-workflow (Epic E)

| # | Failure | Expected behaviour | Test | Status |
|---|---|---|---|---|
| F01 | Same H1 message delivered twice | one offer candidate; the replay is skipped | `crossflow/tests/test_failures.py::test_f01_*` | ✅ |
| F02 | POS refuses an activation | offer stays APPROVED; POS and records agree | `test_f02_*` | ✅ |
| F03 | Activation reply lost **and** status check fails | UNCONFIRMED; reconciliation flags it | `test_f03_*` | ✅ |
| F04 | Stop never confirmed by the POS | offer stays ACTIVE; no re-evaluation | `test_f04_*` | ✅ |
| F05 | Offer recorded stopped but still live on the POS | URGENT finding | `test_f05_*` | ✅ |
| F06 | Unknown offer live on the POS | URGENT finding | `test_f06_*` | ✅ |
| F07 | Campaign figures stale | DATA_UNAVAILABLE; no stop sent | `test_f07_*` | ✅ |
| F08 | Outbox message FAILED or stuck PENDING | listed for the operator (runbook R1) | `test_f08_*` | ✅ |
| F09 | Live POS terms differ from the approval | URGENT finding | `test_f09_*` | ✅ |
| F10 | Malformed H1 message | violations reported, no crash | `test_contracts.py::test_malformed_*` | ✅ |
| F11 | No stored forecast (too little clean history) | no offer candidate | `test_forecast.py::test_not_enough_history_*`, `test_handoffs.py::test_repeated_signals_*` | ✅ |
| F12 | Offer section totals differ from Offers records | report section BLOCKED with reasons | `test_reports.py::test_any_difference_blocks_*` | ✅ |
| F13 | Daily reports disagree with the monthly report | every disagreement listed | `test_reports.py::test_a_wrong_daily_*`, `test_a_different_snapshot_*` | ✅ |
| F14 | Same monitoring result delivered twice | one re-evaluation | `test_handoffs.py::test_replayed_monitoring_*` | ✅ |
| F15 | AI or system identity tries to change a demand level | refused | `test_demand.py::test_ai_and_system_*` | ✅ |
| F16 | Demand proposal goes stale before it's applied | refused | `test_demand.py::test_stale_*` | ✅ |
| F17 | Flaky POS during the full scenario | same outcome, still one human action | `test_scenario.py::test_a_flaky_pos_*` | ✅ |
| F18 | Near-expiry Low-demand stock | offer, not restock | `test_handoffs.py::test_restocking_fully_agrees_*` | ⚠️ D-06 |
| F19 | H1 near-expiry reorder signals without batches | refused by the contract | `test_contracts.py::test_restocking_signal_message_*` | ⚠️ G-1 |

## Inside the workflows (owned by M1–M4, listed so the matrix is complete)

| # | Failure | Test | Status |
|---|---|---|---|
| W01 | POS reply lost, garbage, or request lost on activation | `offers/tests/test_failure_cases.py`, `crossflow/tests/test_mock_pos.py` | ✅ |
| W02 | Prices or start date changed after approval | `offers/tests/test_failure_cases.py`, `test_recheck.py` | ✅ |
| W03 | Approval of an old version | `offers/tests/test_decisions.py`, `test_hardening.py` | ✅ |
| W04 | Missing KPI data, so no impact and no report | `offers/tests/test_failure_cases.py` | ✅ |
| W05 | AI agent failure or invalid output (Strategist) | `offer_strategist/tests/test_graph.py` | ✅ |
| W06 | Restocking: email failure, reviewer never answers (B15) | none: **Restocking has no tests yet** | ⏳ M2 |
| W07 | Duplicate scheduler run: no duplicate emails, reports or offers (A16) | on the unmerged `foundation/complete-foundation-A4-A17` branch | ⏳ M1 |
| W08 | Late data, KPI mismatch, AI failure in reports (C15) | Epic C not in the repo yet | ⏳ M3 |

## Pilot comparison

"Agent numbers match manual figures": `test_reports.py::test_reference_kpis_match_a_hand_calculation` checks every KPI against a hand calculation written in the test. When M3's KPI library lands, run the same data through it and compare with `crossflow.reports.compare_same_snapshot`.
