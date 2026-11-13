# Operator runbooks (E15)

**Status:** draft, to be reviewed by the Operator.

**For:** the on-call operator of the pilot branch. Every step names the exact table, status or tool. When a step says **escalate**, call the owner named in the runbook. Don't fix data by hand without a second person.

---

## R1 · `delivery_failed`: an email or event did not go out

**You see:** `outbox_messages.status = 'FAILED'`, a PENDING message older than 30 min, or `report_runs.delivery_status = 'DELIVERY_FAILED'`.
**Find them:** `crossflow.ops.stuck_outbox(rows, now)` lists them.

1. Read the message's `channel`, `recipient` and `retry_count`.
2. **Wrong recipient?** Stop. Recipients must come from `config_settings.authorized_recipients`, never from AI output (A5). Escalate to M1.
3. **Recipient correct, transient error** (mail server down): set the message back to `PENDING`. The dispatcher retries it with the **same `idempotency_key`**, so the recipient can't get it twice.
4. **Still failing after 3 retries:** contact the recipient directly by phone or email, then mark the message with a note in `audit_log`.
5. Never create a new message for the same thing; that breaks idempotency. Reuse the existing row.

**Restock email specifics:** one email per supplier per approved draft version. The key is `restock:{run_id}:v{version}:supplier:{id}`.

## R2 · Blocked report

**You see:** `report_runs.delivery_status = 'BLOCKED_VALIDATION'`, or an offer or restock section with status `BLOCKED`.

1. Open the stored reasons (C9 saves them; E10 returns `mismatches`).
2. **"redemptions/receipts/discounts differ from Offers"**: compare the sales lines with the Offers record for that `offer_id`.
   - Lines missing (late POS sync): wait for the data and re-run the report for the **same period**. It reuses the same `idempotency_key`, so it can't send twice.
   - Offers record wrong: escalate to M4.
3. **"daily and monthly disagree"**: check that both used the same `snapshot_ref` and `query_version`. A rerun on a newer snapshot is the usual cause. Rebuild both from one snapshot.
4. **Data not available by the deadline**: the report can go out as **PARTIAL** with its label and cutoff time (C4). Only the branch manager decides that.
5. A blocked report is **never** sent anyway. That's the point of the block.

## R3 · POS reconciliation

**When:** activation result `UNCONFIRMED`, monitoring result `STOP_UNCONFIRMED`, or a daily check.
**Run:** `crossflow.ops.reconcile_pos(pos_live_offers, offers)`. Each finding comes with its action:

| Finding | Do this |
|---|---|
| live on the POS but recorded APPROVED (unconfirmed activation) | confirm on the POS screen, then record the offer ACTIVE |
| recorded ACTIVE but not live on the POS | check the POS; re-run activation (it uses the **same key**, so it's safe) or record it stopped |
| **URGENT** recorded STOPPED/ENDED but still live | stop it on the POS **now**: it's selling outside its approval. Then tell the Sales Team |
| **URGENT** live on the POS but unknown to Offers | stop it now; there's no approval for it |
| **URGENT** live terms don't match the approved version | stop it now, escalate to the Sales Team |

**`STOP_UNCONFIRMED` is urgent while the campaign keeps selling past its cap.** The snapshot's `overshoot` shows by how much.

## R4 · Escalated runs

| You see | Meaning | Do this |
|---|---|---|
| `restock_orders.status = 'ESCALATED'` | max rejections reached, or no supplier for a category | read `user_feedback`; fix the supplier list or talk to the reviewer; start a fresh run the next night |
| Strategist result `escalated` | no offer proposal passed the guardrails in 3 rounds | read `attempts[*].reasons`. The product may need a manual offer or a write-off decision |
| Strategist result `skipped` | the product can't be promoted (no price, forecast or evidence; restricted; already on offer; too close to expiry) | usually nothing. "Too close to expiry" means a write-off decision for the manager |

## R5 · Demand-level proposals waiting (H6)

Proposals wait in the review queue until a **branch manager** approves or rejects them. An unreviewed proposal changes nothing. A stale proposal (the level changed in the meantime) is refused when it's applied; create a new one for the next period.

---

## Demo (for the pilot review)

```powershell
python -m crossflow.demo            # offline, about 1 second
python -m crossflow.demo --gemini   # with the real AI agents (needs GEMINI_API_KEY)
```

The demo walks one product from the nightly stock check to the manager report, with a single human approval.

**Reviewed by the Operator:** ☐  Date: ______
