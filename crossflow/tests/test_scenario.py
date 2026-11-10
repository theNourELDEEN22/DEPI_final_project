"""E13 - full scenario: near expiry to Manager report, with only human approvals."""
import pytest

from offers.monitoring import MonitorAction
from offers.pos_activation import ActivationOutcome
from crossflow.mock_pos import MockPOS
from crossflow.scenario import run_full_scenario


@pytest.fixture(scope="module")
def result():
    return run_full_scenario(seed=7)


def test_runs_with_only_human_approvals(result):
    assert [(a["actor"], a["action"]) for a in result.human_actions] == [("sales.maadi", "approve offer")]


def test_every_stage_completed(result):
    assert result.plan["outcome"] == "submitted"
    assert result.activation.outcome == ActivationOutcome.ACTIVATED
    assert result.monitor.action in (MonitorAction.STOPPED_AT_CAP, MonitorAction.ENDED)
    assert result.reevaluation["evaluated_product_ids"] == [result.pilot.cases["near_expiry_overstock"]]
    assert result.impact.is_final
    assert len(result.outbox.messages) == 1
    assert result.offer_check["status"] == "OK", result.offer_check["mismatches"]


def test_the_offer_traces_back_to_the_stock_signal(result):
    pid = result.pilot.cases["near_expiry_overstock"]
    signal = next(s for s in result.signals if s.product_id == pid)
    assert result.plan["offer"]["evidence_ref"] == signal.evidence_ref
    assert result.manager_report["offer_id"] == result.plan["offer_id"]


def test_the_campaign_respected_its_approval(result):
    snap = result.monitor.snapshot
    approved_cap = result.plan["offer"]["quantity_cap"]
    assert snap.quantity_cap == approved_cap
    if result.monitor.action == MonitorAction.STOPPED_AT_CAP:
        assert snap.redemptions >= approved_cap
    assert result.pos.live_offers() == {}  # nothing left running on the POS


def test_manager_report_labels_its_estimate(result):
    message = result.outbox.messages[0]
    assert message.recipient_role == "finance_manager"
    assert "ESTIMATE" in message.body
    assert result.manager_report["estimates"]["waste_reduction"]["label"] == "ESTIMATE"


def test_only_the_known_h1_gap_is_refused_by_the_contract(result):
    assert result.contract_violations  # gap G-1, see docs/EVENT_CONTRACTS.md
    assert all("must list its expiring batches" in v for v in result.contract_violations)


def test_same_seed_gives_the_same_numbers(result):
    again = run_full_scenario(seed=7)
    assert again.impact.actual == result.impact.actual
    assert again.impact.baseline == result.impact.baseline
    assert again.monitor.snapshot.redemptions == result.monitor.snapshot.redemptions


@pytest.mark.parametrize("pos", ["unclear", "lost_request", "garbage"])
def test_a_flaky_pos_changes_nothing_for_the_people(pos):
    flaky = run_full_scenario(seed=7, pos=MockPOS.scenario(pos))
    assert len(flaky.human_actions) == 1
    assert flaky.activation.outcome == ActivationOutcome.ACTIVATED
    assert flaky.monitor.action in (MonitorAction.STOPPED_AT_CAP, MonitorAction.ENDED)
    assert flaky.pos.live_offers() == {}
    assert flaky.offer_check["status"] == "OK"
