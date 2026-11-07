"""A14 - Add idempotency, audit and approver authorization to Restocking (Epic A: Stories FD-3, FD-4).

Wires the Shared Foundation services and middleware (A4-A12) directly into the
Restocking workflow (`EvaluateInventoryNode`, `RetrieveSuppliersNode`,
`DraftReportNode`, `HumanReviewNode`, and `SendEmailNode`):

1. Branch Isolation & RBAC Approver Authorization (A6):
   - `query_inventory_db` is strictly scoped to `context.branch_id`.
   - `HumanReviewNode` authenticates the reviewer and verifies `restock:approve`
     or `restock:reject` permission on `branch_id` before accepting any decision.
2. Input Validation, AI Output Guard & Bounded Retries (A5, A7):
   - `DraftReportNode` validates positive-integer `requested_quantity`, strips any
     AI-supplied recipient or branch overrides via `AIOutputGuard`, and computes a
     cryptographic SHA-256 `version_hash` for the draft.
   - Rejections increment `retry_count` via `BoundedRetryPolicy`; exceeding
     `max_retries` halts the loop, marks the order `ESCALATED`, and queues an
     operator alert in the outbox (RS-8 / B12).
3. Cryptographic Approval & Audit Trail (A11, A12):
   - Every run (`RUN_STARTED`, `RUN_INTERRUPTED`, `RUN_COMPLETED`, `RUN_ESCALATED`),
     human decision (`DECISION_APPROVE`, `DECISION_REJECT`), and email dispatch
     (`DELIVERY_SENT`) is logged in `AuditLogWriter`.
   - `ApprovalService` records the exact `reviewed_version` and `reviewed_hash`
     and verifies them right before `SendEmailNode` dispatches.
4. Idempotency & Transactional Outbox (A8, A10):
   - `SendEmailNode` wraps order finalization and outbox email staging inside
     `IdempotencyKeyStore.run_once(...)` so replaying an approval or restarting
     the server never sends duplicate supplier emails.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from foundation.ai_output_guard import AIOutputGuard
from foundation.approval_service import ApprovalRecord, ApprovalService, compute_version_hash
from foundation.audit_log_writer import AuditLogWriter
from foundation.auth_branch_middleware import (
    AuthAndBranchIsolationMiddleware,
    BranchExecutionContext,
    BranchScopedRepository,
)
from foundation.config_service import ConfigService
from foundation.idempotency_store import IdempotencyKeyStore, build_idempotency_key
from foundation.langgraph_runtime import (
    CheckpointedGraphRuntime,
    CheckpointBackend,
    END_NODE,
    GraphCheckpoint,
    InMemoryCheckpointBackend,
    InMemoryOutboxBackend,
    OutboxMessage,
    TransactionalOutbox,
)
from foundation.validation_retry_middleware import (
    BoundedRetryPolicy,
    InputValidator,
    RetryLimitExceededError,
)


@dataclass
class RestockingDependencies:
    """Bundle of Shared Foundation services injected into the Restocking workflow."""
    config_service: ConfigService = field(default_factory=ConfigService)
    auth_middleware: AuthAndBranchIsolationMiddleware = field(
        default_factory=AuthAndBranchIsolationMiddleware
    )
    ai_guard: Optional[AIOutputGuard] = None
    retry_policy: Optional[BoundedRetryPolicy] = None
    idempotency_store: IdempotencyKeyStore = field(default_factory=IdempotencyKeyStore)
    audit_writer: AuditLogWriter = field(default_factory=AuditLogWriter)
    approval_service: Optional[ApprovalService] = None
    checkpointer: CheckpointBackend = field(default_factory=InMemoryCheckpointBackend)
    outbox: TransactionalOutbox = field(
        default_factory=lambda: TransactionalOutbox(backend=InMemoryOutboxBackend())
    )

    def __post_init__(self) -> None:
        if self.ai_guard is None:
            self.ai_guard = AIOutputGuard(config_service=self.config_service)
        if self.retry_policy is None:
            self.retry_policy = BoundedRetryPolicy(config_service=self.config_service)
        if self.approval_service is None:
            self.approval_service = ApprovalService(audit_writer=self.audit_writer)


class GuardedRestockingWorkflow:
    """Restocking LangGraph workflow wired with Epic A Shared Foundation middleware (Task A14)."""

    def __init__(
        self,
        *,
        dependencies: Optional[RestockingDependencies] = None,
        inventory_rows: Optional[Iterable[Mapping[str, Any]]] = None,
        supplier_rows: Optional[Iterable[Mapping[str, Any]]] = None,
        active_offer_product_ids: Optional[Mapping[int, Iterable[int]]] = None,
    ) -> None:
        self.deps = dependencies if dependencies is not None else RestockingDependencies()
        assert self.deps.ai_guard is not None
        assert self.deps.retry_policy is not None
        assert self.deps.approval_service is not None

        self.inventory_repo = BranchScopedRepository(
            self.deps.auth_middleware,
            initial_rows=inventory_rows or [],
        )
        self._suppliers: List[Dict[str, Any]] = [
            copy.deepcopy(dict(s)) for s in (supplier_rows or [])
        ]
        # Maps branch_id -> set of product_ids currently cleared by active offers (H2 / B14)
        self._active_offers: Dict[int, set[int]] = {
            int(b): {int(pid) for pid in pids}
            for b, pids in (active_offer_product_ids or {}).items()
        }
        self._saved_orders: Dict[int, Dict[str, Any]] = {}
        self._published_signals: List[Dict[str, Any]] = []

    @property
    def saved_orders(self) -> Dict[int, Dict[str, Any]]:
        return copy.deepcopy(self._saved_orders)

    @property
    def published_signals(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._published_signals)

    def _build_runtime(self, context: BranchExecutionContext) -> CheckpointedGraphRuntime:
        """Constructs the checkpointed Restocking graph bound to `context`."""
        graph = CheckpointedGraphRuntime(
            "restock_workflow",
            checkpointer=self.deps.checkpointer,
            outbox=self.deps.outbox,
            interrupt_before=["HumanReviewNode"],
        )

        # ---------------------------------------------------------------------
        # Node 1: EvaluateInventoryNode (Daily 12 AM Africa/Cairo evaluation)
        # ---------------------------------------------------------------------
        def evaluate_inventory_node(
            state: Dict[str, Any],
            outbox: TransactionalOutbox,
        ) -> Dict[str, Any]:
            branch_id = context.branch_id
            rows = self.inventory_repo.read_all(context, permission="inventory:read")
            cleared_pids = self._active_offers.get(branch_id, set())

            flagged: List[Dict[str, Any]] = []
            skipped: List[Dict[str, Any]] = []

            for row in rows:
                pid = int(row["product_id"])
                if pid in cleared_pids:
                    skipped.append(
                        {
                            "product_id": pid,
                            "sku": row.get("sku"),
                            "reason": "Skipped: product is being cleared by an active offer (H2)",
                        }
                    )
                    continue

                eval_res = self.deps.config_service.evaluate_restock_need(
                    stock_quantity=int(row["stock_quantity"]),
                    days_until_expiry=int(row["days_until_expiry"]),
                    demand_level=str(row.get("demand_level", "Medium")),
                    category=row.get("category"),
                    subcategory=row.get("subcategory"),
                    branch_id=branch_id,
                )
                if eval_res.should_restock:
                    flagged_item = {
                        "product_id": pid,
                        "sku": row.get("sku", f"SKU-{pid:04d}"),
                        "product_name": row.get("product_name", f"Product {pid}"),
                        "category": row["category"],
                        "subcategory": row.get("subcategory"),
                        "stock_quantity": int(row["stock_quantity"]),
                        "days_until_expiry": int(row["days_until_expiry"]),
                        "demand_level": str(row.get("demand_level", "Medium")),
                        "trigger_reason": eval_res.trigger_reason,
                        "requested_quantity": int(
                            row.get("recommended_restock_qty")
                            or max(eval_res.applied_threshold.high_demand_qty, 24)
                        ),
                    }
                    flagged.append(flagged_item)
                    self._published_signals.append(
                        {
                            "event_type": "STOCK_SIGNAL_H1",
                            "branch_id": branch_id,
                            "run_id": context.run_id,
                            "product_id": pid,
                            "trigger_reason": eval_res.trigger_reason,
                        }
                    )

            return {
                "product_details": flagged,
                "skipped_products": skipped,
            }

        # ---------------------------------------------------------------------
        # Node 2: RetrieveSuppliersNode (Category supplier & shipping duration)
        # ---------------------------------------------------------------------
        def retrieve_suppliers_node(
            state: Dict[str, Any],
            outbox: TransactionalOutbox,
        ) -> Dict[str, Any]:
            needed_categories = {item["category"] for item in state.get("product_details", [])}
            matched = [
                copy.deepcopy(sup)
                for sup in self._suppliers
                if sup.get("product_category") in needed_categories
            ]
            return {"supplier_options": matched}

        # ---------------------------------------------------------------------
        # Node 3: DraftReportNode (Positive-int validation + AI guard + Hash)
        # ---------------------------------------------------------------------
        def draft_report_node(
            state: Dict[str, Any],
            outbox: TransactionalOutbox,
        ) -> Dict[str, Any]:
            assert self.deps.ai_guard is not None
            branch_id = context.branch_id
            version = int(state.get("version", 1))
            retry_count = int(state.get("retry_count", 0))
            user_feedback = state.get("user_feedback")
            override_qty = state.get("override_requested_quantity")

            products = copy.deepcopy(state.get("product_details", []))
            suppliers_by_cat = {
                s["product_category"]: s for s in state.get("supplier_options", [])
            }

            if not products:
                raise ValueError("Cannot draft restock report: no flagged products found.")

            primary = products[0]
            if override_qty is not None:
                primary["requested_quantity"] = override_qty

            # Task A7 & B8: Strict positive-integer quantity validation before presenting to user
            validated_qty = InputValidator.validate_positive_int(
                primary.get("requested_quantity"),
                "requested_quantity",
            )
            primary["requested_quantity"] = validated_qty

            supplier = suppliers_by_cat.get(primary["category"])
            if supplier is None:
                raise ValueError(f"No supplier found for category '{primary['category']}'.")

            order_id = int(state.get("restock_order_id", 1001))

            # Construct raw AI-style draft output (including any simulated AI fields in state)
            raw_ai_draft: Dict[str, Any] = {
                "restock_order_id": order_id,
                "product_id": int(primary["product_id"]),
                "sku": primary["sku"],
                "product_name": primary["product_name"],
                "category": primary["category"],
                "supplier_id": int(supplier["supplier_id"]),
                "supplier_name": supplier["supplier_name"],
                "shipping_duration_days": int(supplier["shipping_duration_days"]),
                "requested_quantity": validated_qty,
                "trigger_reason": primary["trigger_reason"],
                "retry_count": retry_count,
                "user_feedback": user_feedback,
                "skipped_products": state.get("skipped_products", []),
                "summary_text": (
                    f"Restock {validated_qty} units of {primary['product_name']} "
                    f"from {supplier['supplier_name']} (lead time: {supplier['shipping_duration_days']}d). "
                    f"Reviewer notes applied: {user_feedback or 'None'}."
                ),
            }
            if "simulated_ai_injected_fields" in state:
                raw_ai_draft.update(state["simulated_ai_injected_fields"])

            # Task A5: Sanitize AI output so AI cannot override recipient or branch_id
            sanitized = self.deps.ai_guard.sanitize_ai_output(
                raw_ai_draft,
                trusted_branch_id=branch_id,
                recipient_role="restock_approver",
            )

            if sanitized.had_blocked_fields:
                self.deps.audit_writer.write(
                    run_id=context.run_id,
                    branch_id=branch_id,
                    actor=context.principal.actor_id,
                    action="SECURITY_AI_OVERRIDE_BLOCKED",
                    version=version,
                    entity_type="RESTOCK_ORDER",
                    entity_id=order_id,
                    details={"ignored_ai_fields": sanitized.ignored_ai_fields},
                )

            draft_payload = sanitized.payload
            version_hash = compute_version_hash(
                entity_type="RESTOCK_ORDER",
                entity_id=order_id,
                branch_id=branch_id,
                version=version,
                payload=draft_payload,
            )

            self._saved_orders[order_id] = {
                **draft_payload,
                "version": version,
                "version_hash": version_hash,
                "status": "PENDING_APPROVAL",
            }

            return {
                "restock_order_id": order_id,
                "version": version,
                "version_hash": version_hash,
                "drafted_report": draft_payload,
                "approver_recipient": sanitized.enforced_recipient,
                "ignored_ai_fields": sanitized.ignored_ai_fields,
            }

        # ---------------------------------------------------------------------
        # Node 4: HumanReviewNode (RBAC Auth + Approval Service + Rejection Loop)
        # ---------------------------------------------------------------------
        def human_review_node(
            state: Dict[str, Any],
            outbox: TransactionalOutbox,
        ) -> Dict[str, Any]:
            assert self.deps.approval_service is not None
            assert self.deps.retry_policy is not None

            branch_id = context.branch_id
            order_id = int(state["restock_order_id"])
            current_version = int(state["version"])
            draft_payload = state["drafted_report"]

            reviewer_id = str(state.get("reviewer_actor_id", ""))
            reviewer_token = state.get("reviewer_auth_token")
            decision = str(state.get("reviewer_decision", "")).strip().upper()
            reason = state.get("reviewer_reason")
            submitted_version = int(state.get("reviewed_version", current_version))
            submitted_hash = state.get("reviewed_hash", state["version_hash"])

            # Task A6 & B9: Authenticate human reviewer and verify branch-scoped RBAC permission
            principal = self.deps.auth_middleware.authenticate(
                reviewer_id,
                auth_token=reviewer_token,
            )
            required_perm = "restock:approve" if decision == "APPROVE" else "restock:reject"
            self.deps.auth_middleware.authorize(
                principal,
                required_perm,
                target_branch_id=branch_id,
            )

            # Task A12 & A11: Record versioned cryptographic approval/rejection and log to audit_log
            approval_rec: ApprovalRecord = self.deps.approval_service.record_decision(
                entity_type="RESTOCK_ORDER",
                entity_id=order_id,
                branch_id=branch_id,
                actor_id=principal.actor_id,
                actor_role=principal.role,
                decision=decision,
                reviewed_version=submitted_version,
                current_version=current_version,
                payload_snapshot=draft_payload,
                reviewed_hash=submitted_hash,
                reason=reason,
                run_id=context.run_id,
            )

            if decision == "APPROVE":
                self._saved_orders[order_id]["status"] = "APPROVED"
                return {
                    "review_outcome": "APPROVED",
                    "latest_approval_id": approval_rec.approval_id,
                }

            # Rejection path (RS-7 / RS-8 / B11 / B12): check bounded retry limit
            current_retries = int(state.get("retry_count", 0))
            try:
                next_retries = self.deps.retry_policy.check_hitl_rejection_retry(
                    current_retries,
                    branch_id=branch_id,
                )
            except RetryLimitExceededError:
                # Max retries reached -> Escalate to named operator via outbox
                operator_email = self.deps.config_service.get_recipients(
                    branch_id
                ).escalation_operator
                esc_key = build_idempotency_key(
                    "restock_escalation",
                    branch_id,
                    context.run_id,
                    order_id,
                )
                esc_msg = outbox.enqueue(
                    idempotency_key=esc_key,
                    branch_id=branch_id,
                    channel="EMAIL",
                    recipient=operator_email,
                    subject=f"ESCALATION: Restock Order #{order_id} exceeded max retries",
                    payload={
                        "restock_order_id": order_id,
                        "branch_id": branch_id,
                        "retry_count": current_retries + 1,
                        "last_rejection_reason": reason,
                    },
                )
                self._saved_orders[order_id]["status"] = "ESCALATED"
                self.deps.audit_writer.log_run(
                    run_id=context.run_id,
                    branch_id=branch_id,
                    actor=principal.actor_id,
                    workflow_name="restock",
                    status="ESCALATED",
                    version=current_version,
                    details={
                        "restock_order_id": order_id,
                        "escalation_recipient": operator_email,
                        "outbox_message_id": esc_msg.message_id,
                    },
                )
                return {
                    "review_outcome": "ESCALATED",
                    "retry_count": current_retries + 1,
                    "user_feedback": reason,
                }

            # Within retry limit -> increment retry_count, bump version, loop back to DraftReportNode
            next_version = current_version + 1
            self._saved_orders[order_id]["status"] = "REJECTED"
            return {
                "review_outcome": "REJECTED",
                "retry_count": next_retries,
                "version": next_version,
                "user_feedback": reason,
            }

        # ---------------------------------------------------------------------
        # Node 5: SendEmailNode (Approval verification + Idempotency + Outbox + Audit)
        # ---------------------------------------------------------------------
        def send_email_node(
            state: Dict[str, Any],
            outbox: TransactionalOutbox,
        ) -> Dict[str, Any]:
            assert self.deps.approval_service is not None
            assert self.deps.ai_guard is not None

            branch_id = context.branch_id
            order_id = int(state["restock_order_id"])
            current_version = int(state["version"])
            draft_payload = state["drafted_report"]

            # 1. Task A12 Gate: Verify exact version and hash were approved before sending
            self.deps.approval_service.verify_approved_for_execution(
                entity_type="RESTOCK_ORDER",
                entity_id=order_id,
                branch_id=branch_id,
                current_version=current_version,
                current_payload=draft_payload,
            )

            # 2. Task A5 Gate: Resolve supplier email strictly from verified DB supplier record
            supplier_id = int(draft_payload["supplier_id"])
            supplier_row = next(
                (s for s in self._suppliers if int(s["supplier_id"]) == supplier_id),
                None,
            )
            verified_supplier_email = (
                str(supplier_row.get("email", "orders@supplier-demo.eg"))
                if supplier_row
                else "orders@supplier-demo.eg"
            )
            trusted_supplier_recipient = self.deps.ai_guard.resolve_trusted_recipient(
                trusted_branch_id=branch_id,
                recipient_role="supplier",
                verified_supplier_email=verified_supplier_email,
            )

            # 3. Task A8 & A10 Gate: Idempotently queue email in TransactionalOutbox & log delivery
            idem_key = build_idempotency_key(
                "restock_supplier_email",
                branch_id,
                order_id,
                f"v{current_version}",
            )

            def _stage_and_log_dispatch() -> Dict[str, Any]:
                msg = outbox.enqueue(
                    idempotency_key=idem_key,
                    branch_id=branch_id,
                    channel="EMAIL",
                    recipient=trusted_supplier_recipient,
                    subject=(
                        f"Approved Purchase Order #{order_id} (v{current_version}) - "
                        f"Branch {branch_id}"
                    ),
                    payload={
                        "restock_order_id": order_id,
                        "branch_id": branch_id,
                        "version": current_version,
                        "version_hash": state["version_hash"],
                        "product_id": draft_payload["product_id"],
                        "requested_quantity": draft_payload["requested_quantity"],
                        "supplier_id": supplier_id,
                    },
                )
                self._saved_orders[order_id]["status"] = "DISPATCHED"
                self.deps.audit_writer.log_delivery(
                    run_id=context.run_id,
                    branch_id=branch_id,
                    actor=context.principal.actor_id,
                    channel="EMAIL",
                    recipient=trusted_supplier_recipient,
                    delivery_status="SENT",
                    entity_type="RESTOCK_ORDER",
                    entity_id=order_id,
                    version=current_version,
                    outbox_message_id=msg.message_id,
                )
                self.deps.audit_writer.log_run(
                    run_id=context.run_id,
                    branch_id=branch_id,
                    actor=context.principal.actor_id,
                    workflow_name="restock",
                    status="COMPLETED",
                    version=current_version,
                    details={"restock_order_id": order_id, "outbox_message_id": msg.message_id},
                )
                return {
                    "outbox_message_id": msg.message_id,
                    "supplier_recipient": trusted_supplier_recipient,
                    "idempotency_key": idem_key,
                }

            idem_res = self.deps.idempotency_store.run_once(
                idempotency_key=idem_key,
                workflow_name="restock",
                branch_id=branch_id,
                operation=_stage_and_log_dispatch,
                request_params={
                    "order_id": order_id,
                    "version": current_version,
                    "version_hash": state["version_hash"],
                },
            )
            return dict(idem_res.response_payload)

        graph.add_node("EvaluateInventoryNode", evaluate_inventory_node)
        graph.add_node("RetrieveSuppliersNode", retrieve_suppliers_node)
        graph.add_node("DraftReportNode", draft_report_node)
        graph.add_node("HumanReviewNode", human_review_node)
        graph.add_node("SendEmailNode", send_email_node)

        graph.add_edge("EvaluateInventoryNode", "RetrieveSuppliersNode")
        graph.add_edge("RetrieveSuppliersNode", "DraftReportNode")
        graph.add_edge("DraftReportNode", "HumanReviewNode")

        def _route_after_review(state: Dict[str, Any]) -> str:
            outcome = state.get("review_outcome")
            if outcome == "APPROVED":
                return "SendEmailNode"
            if outcome == "REJECTED":
                return "DraftReportNode"
            return END_NODE

        graph.add_conditional_edges("HumanReviewNode", _route_after_review)
        graph.add_edge("SendEmailNode", END_NODE)
        return graph

    # -------------------------------------------------------------------------
    # Public Execution Entrypoints
    # -------------------------------------------------------------------------
    def start_daily_run(
        self,
        *,
        actor_id: str,
        branch_id: int,
        run_id: str,
        initial_state: Optional[Mapping[str, Any]] = None,
    ) -> GraphCheckpoint:
        """Starts the daily branch restocking run and pauses at `HumanReviewNode`."""
        context = self.deps.auth_middleware.create_branch_context(
            actor_id=actor_id,
            branch_id=branch_id,
            run_id=run_id,
            required_permission="restock:draft",
        )
        self.deps.audit_writer.log_run(
            run_id=run_id,
            branch_id=branch_id,
            actor=actor_id,
            workflow_name="restock",
            status="STARTED",
            version=1,
        )
        graph = self._build_runtime(context)
        base_state: Dict[str, Any] = {"version": 1, "retry_count": 0}
        if initial_state:
            base_state.update(dict(initial_state))
        return graph.start(run_id=run_id, branch_id=branch_id, initial_state=base_state)

    def submit_human_review(
        self,
        *,
        run_id: str,
        branch_id: int,
        reviewer_actor_id: str,
        decision: str,
        reason: Optional[str] = None,
        reviewed_version: Optional[int] = None,
        reviewed_hash: Optional[str] = None,
        override_requested_quantity: Optional[int] = None,
        reviewer_auth_token: Optional[str] = None,
    ) -> GraphCheckpoint:
        """Submits a human reviewer's APPROVE or REJECT decision to resume the paused graph."""
        scheduler_actor = f"system.scheduler.branch{branch_id}"
        context = self.deps.auth_middleware.create_branch_context(
            actor_id=scheduler_actor,
            branch_id=branch_id,
            run_id=run_id,
        )
        graph = self._build_runtime(context)
        current_ckpt = graph.get_checkpoint(run_id, branch_id)
        if current_ckpt is None:
            raise ValueError(f"No active restocking run found for run_id='{run_id}', branch_id={branch_id}.")

        updates: Dict[str, Any] = {
            "reviewer_actor_id": reviewer_actor_id,
            "reviewer_auth_token": reviewer_auth_token,
            "reviewer_decision": decision,
            "reviewer_reason": reason,
            "reviewed_version": (
                reviewed_version
                if reviewed_version is not None
                else current_ckpt.state.get("version", 1)
            ),
            "reviewed_hash": (
                reviewed_hash
                if reviewed_hash is not None
                else current_ckpt.state.get("version_hash")
            ),
        }
        if override_requested_quantity is not None:
            updates["override_requested_quantity"] = override_requested_quantity

        try:
            return graph.resume(run_id=run_id, branch_id=branch_id, state_updates=updates)
        except Exception:
            # Preserve the clean INTERRUPTED checkpoint if review validation/auth fails
            self.deps.checkpointer.save_checkpoint(current_ckpt)
            raise
