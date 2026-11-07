# Epic A: Security Review Checklist & Stuck-Approvals Runbook (Task A17)

**Status:** `SIGNED_OFF`  
**Signed Off By:** `Ahmed-Abdelazeem-M1`  
**Timestamp (UTC):** `2026-11-07T14:19:05.409492+00:00`  
**Cryptographic Sign-Off Hash:** `ad853266ed29a0a8395372a716c6f587598a8cf86416a1dd3b9d50898402095d`

---

## 1. Security Review Checklist (Stories FD-1, FD-2, FD-3, FD-4)

| Check ID | Story | Control Description | Status | Verification Evidence |
| :--- | :--- | :--- | :--- | :--- |
| `SEC-01` | `FD-1` | Block AI output from setting recipients or branch | **PASS** | AI recipient/branch stripped; locked to branch_id=1 & stock.maadi@supermarket.eg |
| `SEC-02` | `FD-2` | Branch A run cannot read or write Branch B | **PASS** | BranchIsolationError raised on cross-branch read and write attempts |
| `SEC-03` | `FD-2` | Authentication and role-based approver authorization | **PASS** | AuthorizationError blocked sales_team role from approving restock orders |
| `SEC-04` | `FD-3` | Audit log records every run, decision and delivery | **PASS** | Verified RUN, DECISION, and DELIVERY entries in AuditLogWriter |
| `SEC-05` | `FD-3` | Approval service binds exact version and SHA-256 hash | **PASS** | VersionMismatchError blocked post-approval quantity tampering |
| `SEC-06` | `FD-4` | Input validation, idempotency store, and outbox deduplication | **PASS** | Zero quantity rejected; 3x replay executed once; outbox deduplicated |

---

## 2. Stuck-Approvals Operational Runbook (Story FD-3)

### 2.1 Symptom & Detection
A Restocking (`Epic B`) or Promotional Offer (`Epic D`) workflow run remains in `status = 'INTERRUPTED'` at `next_node = 'HumanReviewNode'` (or `restock_orders.status = 'PENDING_APPROVAL'`) longer than the branch SLA window:
* **Warning SLA (`>= 4 hours`):** Approver has not responded to the initial review card.
* **Critical Escalation SLA (`>= 12 hours`):** Approver is unavailable; order risks stockout or near-expiry waste.

### 2.2 Step-by-Step Remediation Procedure
1. **Inspect Stuck Checkpoints:**
   Run the automated detector to list all paused runs exceeding the SLA threshold:
   ```bash
   python -m foundation.security_pilot_runbook --check-stuck-approvals
   ```
2. **Tier 1 Remediation (`4h <= age < 12h` -> `REMIND_APPROVER`):**
   * The runbook service enqueues an idempotent reminder email (`restock_stuck_reminder:branch_<id>:<run_id>`) to the branch's configured `restock_approver` (`ConfigService.get_recipients(branch_id)`).
   * Logs `RUN_STUCK_APPROVAL_REMINDER` in `public.audit_log`.
3. **Tier 2 Remediation (`age >= 12h` -> `ESCALATE_TO_OPERATOR`):**
   * The runbook service enqueues an urgent escalation alert (`restock_stuck_escalation:branch_<id>:<run_id>`) to the branch's `escalation_operator` (`ops.<branch>@supermarket.eg`).
   * An `operator_admin` or `branch_manager` principal can then call `GuardedRestockingWorkflow.submit_human_review(...)` using their authorized role to `APPROVE` or `REJECT` the stuck draft.
   * Logs `RUN_STUCK_APPROVAL_ESCALATED` in `public.audit_log`.
4. **Post-Remediation Verification:**
   * Confirm `outbox_messages.status = 'DISPATCHED'` and verify that `approvals` records the exact `reviewed_version` and `reviewed_hash`.
