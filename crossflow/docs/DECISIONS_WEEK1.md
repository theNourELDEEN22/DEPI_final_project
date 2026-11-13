# Week 1 decisions (E1): proposal for sign-off

**Status:** PROPOSED. Nothing here is final until the team agrees in the decisions meeting.

**How to use this:** each decision lists what the repo does **today**, where the values conflict, and a recommendation. Tick one option per decision. The owner then updates the config seed or the code, and the tests listed under **Covered by** keep the decision enforced.

The conflicts below were found by reading `main` on 2026-11-10. Several parts already disagree with each other, so these need settling before the pilot.

---

## D-01 Expiry rule: when is stock "near expiry"?

| Source | Value |
|---|---|
| Task B5 | "expiring within **3 days**" |
| Config seed `restock_thresholds` | per category: Bakery 2, Dairy 3, Snacks 10, Frozen/Beverages/Cleaning/Home 14, Staples/Personal Care 15, School Supplies 21; default **7** |
| `restocking/workflow.py` | falls back to **3** when a category has no value |

**Recommendation:** keep the **per-category values from the seed**, since 3 days makes no sense for a 2-year shelf life, and change the default to 3 so it matches B5. Update the wording of B5.
**Owner:** M2. **Covered by:** `crossflow/tests/test_handoffs.py` (it reads the seed directly, so a change is picked up).

## D-02 Low-stock thresholds

| Source | High | Medium |
|---|---|---|
| Task B6 | **15** | **8** |
| Seed default | **25** | **12** |
| Seed per category | 15 to 80 | 8 to 45 |

**Recommendation:** per-category seed values, as for D-01. B6's 15/8 equals the seed's Personal Care / School Supplies row, so B6 was likely written from one category.
**Owner:** M2.

## D-03 Retry limits

- Restocking rejections: seed `max_retries = 3`, then escalate.
- AI Offer Strategist designer rounds: `StrategistConfig.max_rounds = 3`, then escalate.

**Recommendation:** keep both at 3. Escalations go to the named operator (B12).

## D-04 Offer guardrails: the two sources disagree

| | max discount | min margin | max quantity cap |
|---|---|---|---|
| Config seed `offer_guardrails` | **40%** | **5%** | not set |
| `offers/guardrails.py` `GuardrailConfig` defaults | **50%** | **10%** | 500 |

The code doesn't read the seed yet, so today the effective limits are 50% / 10%.
**Recommendation:** decide the numbers once, put all three in the seed, and have Offers read them through the config service (A4). Suggested: **40% / 10% / 500**.
**Owner:** M4 with M1.

## D-05 Financial definitions (shared with C1)

The definitions below are proposed and implemented in `crossflow/reference_kpis.py`. QA uses that file to cross-check M3's KPI library (C6/C7).

| KPI | Definition |
|---|---|
| Gross sales | sum of quantity × unit_price |
| Discounts | sum of discount_amount on the lines |
| Refunds | sum of refund_amount of returns **dated in the period** (by `returned_at`) |
| Net revenue | gross − discounts − refunds |
| Units | units sold − units returned |
| Transactions | distinct `transaction_id` with a line in the period |
| Average transaction value | net revenue ÷ transactions; **N/A** when there are none |
| Gross profit | net revenue − net units × unit cost; **N/A** when any cost is missing |

Still open:
- **Tax.** Are shelf prices VAT-inclusive (Egypt 14%)? The data has no tax column. Recommendation: treat prices as tax-inclusive and report net revenue including VAT, labeled as such.
- **Cost.** Use the current `products.cost_price` or the batch `cost_price`? Recommendation: current cost for the pilot, batch cost later.
- **Rounding.** Decimal everywhere, half-up to 0.01, rounded **once at the end** and never per line, except where the POS rounds each line (see D-12).
- **Period.** Half-open `[start, end)` in Africa/Cairo time, converted to UTC for storage (A9).

**Owner:** M3 with M5. **Covered by:** `test_reports.py::test_reference_kpis_match_a_hand_calculation`.

## D-06 Near-expiry stock with **Low** demand

Restocking today reorders it: a `NEAR_EXPIRY` order of up to 25 units of a slow product that is about to expire. That conflicts with the spirit of B6 ("Low demand never triggers").
**Recommendation:** route it to an **offer**, not a restock order. It is "overstocked" by definition: Low-demand products have no restock threshold.
**Owner:** M2. **Covered by:** `test_handoffs.py::test_restocking_fully_agrees_with_the_rule`. That test is marked *expected to fail* until Restocking follows this rule; it then passes and the marker must be removed (`strict=True` forces that).

## D-07 Demand-level rules for the H6 feedback loop (E12)

A product is High when it averages at least **8 units/day**, Medium at **3–8**, and Low below **3**. Averages are measured over at least **14 days**, without promo sales.
**Recommendation:** accept these for the pilot and re-check them after 4 weeks of real data. Changes always go through a manager's review; see D-13.

## D-08 Which offers stop Restocking from reordering (H2)?

Today Restocking only skips products with an offer whose database status is `'ACTIVE'`.
**Recommendation:** also skip **APPROVED** offers that haven't ended. Otherwise the night before an approved offer starts, Restocking reorders the very stock the offer is meant to clear. `crossflow.handoffs.clearance_notices` already does this.
**Owner:** M2 with M4.

## D-09 Forecast and baseline method (E5)

- Lookback **28 days**; days with promo sales are left out; at least **7 clean days**, otherwise there is no forecast and no offer.
- Weekday pattern shrunk toward 1 (4 pseudo-days).
- An offer candidate is reviewed against a **7-day** forecast.
- The baseline is the forecast units at the normal net price and the current cost.

**Recommendation:** accept for the pilot.

## D-10 Offer data model: code vs database (blocking for M1/M4)

| | `offers/` code | `offers` table |
|---|---|---|
| ids | `offer_id` UUID string, `branch_id`/`product_id` strings | BIGINT |
| products | `product_ids` list + `bundle_items` | single `product_id` + `bundle_product_id` |
| types | `discount`, `buy2get1`, `bundle` | `DISCOUNT`, `BUY_2_GET_1`, `BUNDLE` |
| statuses | `draft`, `pending_approval`, `changes_requested`, `ended`… | `CANDIDATE`, `PENDING_APPROVAL`, `EXPIRED`… |
| versions | same `offer_id`, version + 1 | `offer_id` is the **primary key**, so a second version row is impossible |

`sales_transactions.offer_id` is BIGINT too, so a POS line can't carry today's UUID offer ids.
**Recommendation:** add a versions table (or make the key `(offer_id, version)`), and settle on one set of names and id types. **Owner:** M1 + M4, before any database adapter is written.

## D-11 Offer timing (AI Offer Strategist)

An offer starts at least **12 h** after it's planned, so Sales has time to review, and at most **72 h** after.
**Recommendation:** accept.

## D-12 POS discount rounding

The POS rounds each line's discount, while Offers computes the total once. The two may differ by up to **0.01 per line**.
**Recommendation:** accept that tolerance in the E10 check (already implemented).

## D-13 Who may approve what

| Approval | Who |
|---|---|
| Restock orders | branch stock approver (seed `authorized_recipients.restock_approver`) |
| Offers | Sales Team |
| Demand-level changes (H6) | branch manager |

No AI or system identity may approve anything (enforced in `crossflow/demand.py` until A6 provides real identities).

---

**Sign-off:** M1 ☐ M2 ☐ M3 ☐ M4 ☐ M5 ☐  Date: ______
