"""A16 - Duplicate-scheduler and stale-data failure test harness (Epic A: Story FD-4).

Satisfies Task A16 Done Condition: 'No duplicate emails, reports or offers'.

Provides failure-injection runners and stale-data guards across all three
Retail AI Agent workflows (Restocking, Branch Reporting, Promotional Offers):

1. Duplicate-Scheduler Protection (`DuplicateSchedulerHarness`):
   - Wraps scheduled Restocking runs, Daily/Monthly Branch Report runs, and
     Promotional Offer generation + POS activation runs in `IdempotencyKeyStore`
     (A8) and `TransactionalOutbox` (A10).
   - Proves that firing the cron scheduler or webhook N times for the same
     branch and schedule window produces at most 1 supplier email, 1 branch
     report, and 1 promotional offer / POS activation.

2. Stale-Data Detection & Failure Recovery (`StaleDataGuard`):
   - Blocks Restocking runs when the inventory/batch sync timestamp is older
     than `max_staleness_seconds` or when a human approval references a stale
     draft version/hash (`VersionMismatchError`).
   - Blocks unverified Branch Reports when upstream POS transactions (`sales_lines`)
     have not synced past the half-open period end (`end_utc`), preventing
     corrupted or duplicate report deliveries during upstream lag retries.
   - Blocks Promotional Offer POS activations when product cost/expiry data goes
     stale between approval and activation (violating `min_margin_percent`,
     price floor, or expiry window).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from foundation.ai_output_guard import AIOutputGuard
from foundation.approval_service import (
    ApprovalService,
    InMemoryApprovalBackend,
    VersionMismatchError,
    compute_version_hash,
)
from foundation.audit_log_writer import AuditLogWriter, InMemoryAuditBackend
from foundation.auth_branch_middleware import AuthAndBranchIsolationMiddleware
from foundation.config_service import ConfigService, InMemoryConfigBackend
from foundation.idempotency_store import (
    IdempotencyKeyStore,
    InMemoryIdempotencyBackend,
    build_idempotency_key,
)
from foundation.langgraph_runtime import (
    InMemoryCheckpointBackend,
    InMemoryOutboxBackend,
    OutboxMessage,
    TransactionalOutbox,
)
from foundation.money_time_utils import calculate_margin_pct, quantize_money, resolve_daily_period, to_utc
from foundation.restocking_middleware import GuardedRestockingWorkflow, RestockingDependencies
from foundation.staging_deploy import PILOT_INVENTORY_SEED, PILOT_SUPPLIERS_SEED


class StaleDataError(RuntimeError):
    """Raised when upstream inventory, sales, or pricing data is stale at execution time."""


@dataclass(frozen=True)
class FailureSimulationSummary:
    """Counts proving zero duplicate side-effects under scheduler & stale-data faults."""
    scheduler_attempts: int
    emails_dispatched: int
    reports_generated: int
    offers_activated: int
    stale_runs_blocked: int


class StaleDataGuard:
    """Validates data freshness before Restocking, Reporting, or Offer activation (Task A16)."""

    @staticmethod
    def verify_inventory_freshness(
        *,
        snapshot_synced_at: datetime,
        evaluation_time: datetime,
        max_staleness_minutes: int = 30,
    ) -> None:
        """Raises `StaleDataError` if inventory snapshot age exceeds `max_staleness_minutes`."""
        sync_utc = to_utc(snapshot_synced_at)
        eval_utc = to_utc(evaluation_time)
        age = eval_utc - sync_utc
        if age > timedelta(minutes=max_staleness_minutes):
            raise StaleDataError(
                f"Stale inventory snapshot rejected: synced at {sync_utc.isoformat()} "
                f"({int(age.total_seconds() // 60)}m old > max {max_staleness_minutes}m)."
            )

    @staticmethod
    def verify_report_upstream_completeness(
        *,
        report_date: str,
        latest_pos_sync_utc: datetime,
        timezone_name: str = "Africa/Cairo",
    ) -> None:
        """Raises `StaleDataError` if POS sales_lines sync has not reached `period.end_utc`."""
        period = resolve_daily_period(report_date, tz_name=timezone_name)
        sync_utc = to_utc(latest_pos_sync_utc)
        if sync_utc < period.end_utc:
            raise StaleDataError(
                f"Stale POS data for daily report '{report_date}': latest_pos_sync_utc "
                f"({sync_utc.isoformat()}) is behind period end ({period.end_utc.isoformat()})."
            )

    @staticmethod
    def verify_offer_freshness_before_pos_activation(
        *,
        offer_price: Decimal | str,
        live_cost_price: Decimal | str,
        days_until_expiry: int,
        min_margin_pct: Decimal,
    ) -> None:
        """Re-validates live product cost and expiry right before POS activation (D9 / A16)."""
        if int(days_until_expiry) <= 0:
            raise StaleDataError(
                f"Stale offer blocked before POS activation: batch already expired "
                f"(days_until_expiry={days_until_expiry})."
            )
        price_dec = quantize_money(offer_price, field_name="offer_price")
        cost_dec = quantize_money(live_cost_price, field_name="live_cost_price")
        if price_dec < cost_dec:
            raise StaleDataError(
                f"Stale offer blocked: offer_price ({price_dec}) is below updated live_cost_price ({cost_dec})."
            )
        live_margin = calculate_margin_pct(price_dec, cost_dec)
        if live_margin < min_margin_pct:
            raise StaleDataError(
                f"Stale offer blocked: live margin ({live_margin}%) fell below min_margin_pct ({min_margin_pct}%)."
            )


class DuplicateSchedulerAndStaleDataHarness:
    """End-to-end harness exercising duplicate schedulers and stale data across Epics B, C, and D."""

    def __init__(self) -> None:
        self.config = ConfigService(backend=InMemoryConfigBackend())
        self.auth = AuthAndBranchIsolationMiddleware()
        self.ai_guard = AIOutputGuard(config_service=self.config)
        self.idem_store = IdempotencyKeyStore(backend=InMemoryIdempotencyBackend())
        self.audit = AuditLogWriter(backend=InMemoryAuditBackend())
        self.approvals = ApprovalService(
            backend=InMemoryApprovalBackend(),
            audit_writer=self.audit,
        )
        self.checkpointer = InMemoryCheckpointBackend()
        self.outbox_backend = InMemoryOutboxBackend()
        self.outbox = TransactionalOutbox(backend=self.outbox_backend)

        self.deps = RestockingDependencies(
            config_service=self.config,
            auth_middleware=self.auth,
            ai_guard=self.ai_guard,
            idempotency_store=self.idem_store,
            audit_writer=self.audit,
            approval_service=self.approvals,
            checkpointer=self.checkpointer,
            outbox=self.outbox,
        )
        self.restock_workflow = GuardedRestockingWorkflow(
            dependencies=self.deps,
            inventory_rows=copy.deepcopy(PILOT_INVENTORY_SEED),
            supplier_rows=copy.deepcopy(PILOT_SUPPLIERS_SEED),
        )

        # Trackers for verification
        self.dispatched_emails: List[OutboxMessage] = []
        self.dispatched_webhooks: List[OutboxMessage] = []
        self.report_runs: Dict[str, Dict[str, Any]] = {}
        self.active_offers: Dict[str, Dict[str, Any]] = {}
        self.stale_blocks_count: int = 0

    def flush_outbox(self, *, branch_id: Optional[int] = None) -> None:
        """Dispatches all PENDING outbox messages, separating EMAIL and POS_WEBHOOK deliveries."""
        def _sender(msg: OutboxMessage) -> None:
            if msg.channel == "EMAIL":
                self.dispatched_emails.append(msg)
            elif msg.channel == "POS_WEBHOOK":
                self.dispatched_webhooks.append(msg)

        self.outbox.dispatch_pending(_sender, branch_id=branch_id)

    # -------------------------------------------------------------------------
    # 1. Restocking Scheduler Trigger (with Freshness & Idempotency Guard)
    # -------------------------------------------------------------------------
    def trigger_scheduled_restock_run(
        self,
        *,
        branch_id: int,
        schedule_date: str,
        snapshot_synced_at: datetime,
        evaluation_time: datetime,
        auto_approve_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Simulates a cron scheduler firing the daily restock workflow for `(branch_id, schedule_date)`."""
        try:
            StaleDataGuard.verify_inventory_freshness(
                snapshot_synced_at=snapshot_synced_at,
                evaluation_time=evaluation_time,
            )
        except StaleDataError as exc:
            self.stale_blocks_count += 1
            self.audit.log_run(
                run_id=f"restock-{branch_id}-{schedule_date}",
                branch_id=branch_id,
                actor=f"system.scheduler.branch{branch_id}",
                workflow_name="restock",
                status="BLOCKED_STALE_DATA",
                details={"reason": str(exc)},
            )
            raise

        sched_key = build_idempotency_key("scheduler_restock", branch_id, schedule_date)
        run_id = f"restock-b{branch_id}-{schedule_date}"

        def _start_and_optionally_approve() -> Dict[str, Any]:
            ckpt = self.restock_workflow.start_daily_run(
                actor_id=f"system.scheduler.branch{branch_id}",
                branch_id=branch_id,
                run_id=run_id,
            )
            if auto_approve_by:
                ckpt = self.restock_workflow.submit_human_review(
                    run_id=run_id,
                    branch_id=branch_id,
                    reviewer_actor_id=auto_approve_by,
                    decision="APPROVE",
                    reason="Scheduled test approval",
                )
            return {
                "run_id": run_id,
                "branch_id": branch_id,
                "status": ckpt.status,
                "restock_order_id": ckpt.state.get("restock_order_id"),
            }

        res = self.idem_store.run_once(
            idempotency_key=sched_key,
            workflow_name="restock_scheduler",
            branch_id=branch_id,
            operation=_start_and_optionally_approve,
            request_params={"branch_id": branch_id, "schedule_date": schedule_date},
        )
        self.flush_outbox(branch_id=branch_id)
        return dict(res.response_payload)

    # -------------------------------------------------------------------------
    # 2. Branch Reporting Scheduler Trigger (with Completeness & Idempotency Guard)
    # -------------------------------------------------------------------------
    def trigger_scheduled_daily_report(
        self,
        *,
        branch_id: int,
        report_date: str,
        latest_pos_sync_utc: datetime,
        gross_sales: str = "15420.50",
        net_revenue: str = "15100.00",
    ) -> Dict[str, Any]:
        """Simulates a cron scheduler firing the 1:00 AM daily branch report workflow."""
        try:
            StaleDataGuard.verify_report_upstream_completeness(
                report_date=report_date,
                latest_pos_sync_utc=latest_pos_sync_utc,
                timezone_name=self.config.get_timezone(branch_id),
            )
        except StaleDataError as exc:
            self.stale_blocks_count += 1
            self.audit.log_run(
                run_id=f"report-b{branch_id}-{report_date}",
                branch_id=branch_id,
                actor=f"system.scheduler.branch{branch_id}",
                workflow_name="reporting",
                status="BLOCKED_STALE_DATA",
                details={"reason": str(exc)},
            )
            raise

        idem_key = build_idempotency_key("daily_branch_report", branch_id, report_date)
        run_id = f"report-b{branch_id}-{report_date}"

        def _generate_and_queue_report() -> Dict[str, Any]:
            recipient = self.ai_guard.resolve_trusted_recipient(
                trusted_branch_id=branch_id,
                recipient_role="report_recipient",
            )
            report_record = {
                "report_id": len(self.report_runs) + 1,
                "run_id": run_id,
                "branch_id": branch_id,
                "report_date": report_date,
                "gross_sales": str(quantize_money(gross_sales)),
                "net_revenue": str(quantize_money(net_revenue)),
                "recipient": recipient,
                "status": "SENT",
            }
            self.report_runs[idem_key] = report_record

            msg = self.outbox.enqueue(
                idempotency_key=idem_key,
                branch_id=branch_id,
                channel="EMAIL",
                recipient=recipient,
                subject=f"Daily Branch Report - Branch {branch_id} ({report_date})",
                payload=report_record,
            )
            self.audit.log_run(
                run_id=run_id,
                branch_id=branch_id,
                actor=f"system.scheduler.branch{branch_id}",
                workflow_name="reporting",
                status="COMPLETED",
            )
            self.audit.log_delivery(
                run_id=run_id,
                branch_id=branch_id,
                actor="system.outbox.dispatcher",
                channel="EMAIL",
                recipient=recipient,
                delivery_status="SENT",
                entity_type="REPORT_RUN",
                entity_id=report_record["report_id"],
                outbox_message_id=msg.message_id,
            )
            return report_record

        res = self.idem_store.run_once(
            idempotency_key=idem_key,
            workflow_name="reporting",
            branch_id=branch_id,
            operation=_generate_and_queue_report,
            request_params={"branch_id": branch_id, "report_date": report_date},
        )
        self.flush_outbox(branch_id=branch_id)
        return dict(res.response_payload)

    # -------------------------------------------------------------------------
    # 3. Promotional Offer Creation & POS Activation (with Pre-Activation Recheck)
    # -------------------------------------------------------------------------
    def trigger_scheduled_offer_activation(
        self,
        *,
        branch_id: int,
        offer_id: int,
        product_id: int,
        campaign_date: str,
        approved_version: int,
        current_version: int,
        approved_offer_price: str,
        approved_cost_price: str,
        live_cost_price: str,
        days_until_expiry: int,
        reviewer_actor_id: str = "sales.maadi@supermarket.eg",
    ) -> Dict[str, Any]:
        """Simulates offer approval + scheduled/webhook POS activation with stale-data recheck."""
        approved_payload = {
            "offer_id": offer_id,
            "product_id": product_id,
            "offer_price": str(quantize_money(approved_offer_price)),
            "cost_price": str(quantize_money(approved_cost_price)),
        }

        # Ensure initial approval is recorded once for `approved_version`
        existing_decision = self.approvals.get_latest_decision(
            entity_type="OFFER",
            entity_id=offer_id,
            branch_id=branch_id,
        )
        if existing_decision is None:
            self.approvals.record_decision(
                entity_type="OFFER",
                entity_id=offer_id,
                branch_id=branch_id,
                actor_id=reviewer_actor_id,
                actor_role="sales_team",
                decision="APPROVE",
                reason="Initial offer approval",
                reviewed_version=approved_version,
                current_version=approved_version,
                payload_snapshot=approved_payload,
                run_id=f"offer-b{branch_id}-{offer_id}",
            )

        # Step 1: Verify cryptographic approval against current version and live cost payload
        live_payload = {
            "offer_id": offer_id,
            "product_id": product_id,
            "offer_price": str(quantize_money(approved_offer_price)),
            "cost_price": str(quantize_money(live_cost_price)),
        }
        try:
            self.approvals.verify_approved_for_execution(
                entity_type="OFFER",
                entity_id=offer_id,
                branch_id=branch_id,
                current_version=current_version,
                current_payload=live_payload,
            )
            guardrails = self.config.get_offer_guardrails(branch_id=branch_id)
            StaleDataGuard.verify_offer_freshness_before_pos_activation(
                offer_price=approved_offer_price,
                live_cost_price=live_cost_price,
                days_until_expiry=days_until_expiry,
                min_margin_pct=guardrails.min_margin_percent,
            )
        except (VersionMismatchError, StaleDataError) as exc:
            self.stale_blocks_count += 1
            self.audit.log_run(
                run_id=f"offer-b{branch_id}-{offer_id}",
                branch_id=branch_id,
                actor=f"system.scheduler.branch{branch_id}",
                workflow_name="offers",
                status="BLOCKED_STALE_OR_VERSION_MISMATCH",
                version=current_version,
                details={"reason": str(exc)},
            )
            raise

        idem_key = build_idempotency_key(
            "offer_pos_activation",
            branch_id,
            offer_id,
            f"v{current_version}",
            campaign_date,
        )

        def _activate_on_pos() -> Dict[str, Any]:
            offer_record = {
                "offer_id": offer_id,
                "branch_id": branch_id,
                "product_id": product_id,
                "version": current_version,
                "offer_price": str(quantize_money(approved_offer_price)),
                "status": "ACTIVE",
            }
            self.active_offers[idem_key] = offer_record
            self.outbox.enqueue(
                idempotency_key=idem_key,
                branch_id=branch_id,
                channel="POS_WEBHOOK",
                recipient=f"https://pos.supermarket.eg/branches/{branch_id}/offers/activate",
                payload=offer_record,
            )
            return offer_record

        res = self.idem_store.run_once(
            idempotency_key=idem_key,
            workflow_name="offers",
            branch_id=branch_id,
            operation=_activate_on_pos,
            request_params={
                "offer_id": offer_id,
                "version": current_version,
                "campaign_date": campaign_date,
            },
        )
        self.flush_outbox(branch_id=branch_id)
        return dict(res.response_payload)
