"""Unit tests for Task A17: Security review, pilot deploy, stuck-approvals runbook (FD-2, FD-3).

Verifies the Done Condition: 'Review checklist signed off',
covering:
1. Automated Security Review Checklist (`SecurityReviewAuditor.run_security_review`)
   verifies all 6 Epic A security controls (`SEC-01` through `SEC-06`) and signs
   off (`is_signed_off=True`).
2. Gated Pilot Deployment (`PilotDeploymentCoordinator.execute_pilot_deploy`)
   blocks deployment if the security checklist is not signed off, and deploys
   all 3 pilot branches once signed off.
3. Stuck-Approvals Runbook (`StuckApprovalsRunbook.scan_and_remediate`) detects
   `INTERRUPTED` workflows waiting at `HumanReviewNode`, sends a reminder at
   `>= 4 hours`, escalates to the branch operator at `>= 12 hours`, deduplicates
   repeat scans via outbox idempotency, and exports `SECURITY_REVIEW_AND_RUNBOOK.md`.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foundation.langgraph_runtime import GraphCheckpoint
from foundation.security_pilot_runbook import (
    PilotDeploymentCoordinator,
    SecurityChecklistItem,
    SecurityReviewAuditor,
    SecuritySignOffError,
    SecuritySignOffReport,
    StuckApprovalsRunbook,
    main as a17_cli_main,
)


class TestSecurityPilotRunbookA17(unittest.TestCase):
    def test_security_review_checklist_signed_off_and_pilot_deploys(self) -> None:
        """Task A17 Done Condition: Review checklist signed off and pilot branches deployed."""
        auditor = SecurityReviewAuditor()
        report = auditor.run_security_review(reviewer="Ahmed-Abdelazeem-M1")

        self.assertTrue(report.is_signed_off)
        self.assertEqual(report.reviewer, "Ahmed-Abdelazeem-M1")
        self.assertEqual(len(report.items), 6)
        for item in report.items:
            self.assertTrue(item.passed, f"Security check {item.check_id} failed!")

        with tempfile.TemporaryDirectory() as tmpdir:
            runbook_path = Path(tmpdir) / "SECURITY_REVIEW_AND_RUNBOOK.md"
            coordinator = PilotDeploymentCoordinator(auditor=auditor)
            pilot_res = coordinator.execute_pilot_deploy(
                reviewer="Ahmed-Abdelazeem-M1",
                security_report=report,
                write_runbook_path=runbook_path,
            )
            self.assertEqual(pilot_res.pilot_status, "PILOT_LIVE_AND_SIGNED_OFF")
            self.assertEqual(pilot_res.staging_result.verified_branches, (1, 2, 3))
            self.assertTrue(runbook_path.exists())
            content = runbook_path.read_text(encoding="utf-8")
            self.assertIn("SIGNED_OFF", content)
            self.assertIn("SEC-01", content)
            self.assertIn("SEC-06", content)
            self.assertIn("Stuck-Approvals Operational Runbook", content)

        # CLI verification
        self.assertEqual(a17_cli_main(["--reviewer", "Ahmed-Abdelazeem-M1"]), 0)

    def test_pilot_deploy_blocked_if_security_checklist_not_signed_off(self) -> None:
        """Pilot deployment must fail closed if any security checklist item fails."""
        failed_report = SecuritySignOffReport(
            reviewer="Ahmed-Abdelazeem-M1",
            reviewed_at=datetime.now(timezone.utc).isoformat(),
            items=(
                SecurityChecklistItem(
                    check_id="SEC-02",
                    story_id="FD-2",
                    title="Branch isolation",
                    passed=False,
                    evidence="Simulated failure",
                ),
            ),
            is_signed_off=False,
            signature_hash="invalid",
        )
        coordinator = PilotDeploymentCoordinator()
        with self.assertRaises(SecuritySignOffError):
            coordinator.execute_pilot_deploy(security_report=failed_report)

    def test_stuck_approvals_runbook_detects_reminds_and_escalates_idempotently(self) -> None:
        """Stuck-approvals runbook reminds at >=4h, escalates at >=12h, and ignores fresh runs."""
        now_utc = datetime(2026, 11, 7, 18, 0, 0, tzinfo=timezone.utc)

        # 1. Fresh paused checkpoint (1 hour old -> below 4h SLA, should be ignored)
        fresh_ckpt = GraphCheckpoint(
            run_id="run-fresh-b1",
            branch_id=1,
            workflow_name="restock",
            current_node="DraftReportNode",
            next_node="HumanReviewNode",
            status="INTERRUPTED",
            step=3,
            state={"version": 1},
            updated_at=(now_utc - timedelta(hours=1)).isoformat(),
        )

        # 2. Warning-level stuck checkpoint in Branch 2 (6 hours old -> REMIND_APPROVER)
        warning_ckpt = GraphCheckpoint(
            run_id="run-warning-b2",
            branch_id=2,
            workflow_name="restock",
            current_node="DraftReportNode",
            next_node="HumanReviewNode",
            status="INTERRUPTED",
            step=3,
            state={"version": 1},
            updated_at=(now_utc - timedelta(hours=6)).isoformat(),
        )

        # 3. Critical stuck checkpoint in Branch 3 (15 hours old -> ESCALATE_TO_OPERATOR)
        critical_ckpt = GraphCheckpoint(
            run_id="run-critical-b3",
            branch_id=3,
            workflow_name="restock",
            current_node="DraftReportNode",
            next_node="HumanReviewNode",
            status="INTERRUPTED",
            step=3,
            state={"version": 2},
            updated_at=(now_utc - timedelta(hours=15)).isoformat(),
        )

        runbook = StuckApprovalsRunbook(warning_sla_hours=4.0, escalation_sla_hours=12.0)
        incidents = runbook.scan_and_remediate(
            [fresh_ckpt, warning_ckpt, critical_ckpt],
            now_utc=now_utc,
        )

        self.assertEqual(len(incidents), 2)

        # Incident 1: Branch 2 reminder to stock.nasrcity@supermarket.eg
        inc_b2 = incidents[0]
        self.assertEqual(inc_b2.run_id, "run-warning-b2")
        self.assertEqual(inc_b2.remediation_action, "REMIND_APPROVER")
        self.assertEqual(inc_b2.approver_recipient, "stock.nasrcity@supermarket.eg")

        # Incident 2: Branch 3 escalation to ops.october@supermarket.eg
        inc_b3 = incidents[1]
        self.assertEqual(inc_b3.run_id, "run-critical-b3")
        self.assertEqual(inc_b3.remediation_action, "ESCALATE_TO_OPERATOR")
        self.assertEqual(inc_b3.escalation_operator, "ops.october@supermarket.eg")

        # Re-scanning the exact same stuck checkpoints does NOT duplicate outbox messages
        incidents_replay = runbook.scan_and_remediate(
            [fresh_ckpt, warning_ckpt, critical_ckpt],
            now_utc=now_utc,
        )
        self.assertEqual(incidents_replay[0].outbox_message_id, inc_b2.outbox_message_id)
        self.assertEqual(incidents_replay[1].outbox_message_id, inc_b3.outbox_message_id)
        self.assertEqual(len(runbook.outbox.list_messages()), 2)


if __name__ == "__main__":
    unittest.main()
