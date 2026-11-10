"""E12 - XW-6: demand levels change only through reviewed data."""
from datetime import datetime, timedelta, timezone

import pytest

from crossflow.contracts import DemandLevel
from crossflow.datagen import generate_pilot_branch
from crossflow.demand import DemandReviewQueue, ReviewError, propose_demand_levels

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def pilot():
    return generate_pilot_branch(7)


def proposals_for(pilot, source="rules"):
    return propose_demand_levels(
        pilot.sales, pilot.products, branch_id=1, period_start=pilot.as_of - timedelta(days=28),
        period_end=pilot.as_of, now=NOW, source=source,
    )


def underrated(pilot, source="rules"):
    pid = pilot.cases["underrated_seller"]
    return next(p for p in proposals_for(pilot, source) if p.product_id == pid)


def test_sell_through_proposes_a_new_level_for_the_underrated_product(pilot):
    proposal = underrated(pilot)
    assert (proposal.current_level, proposal.proposed_level) == (DemandLevel.MEDIUM, DemandLevel.HIGH)
    assert proposal.avg_daily_units >= 8 and proposal.days == 28


def test_nothing_changes_without_a_review(pilot):
    queue = DemandReviewQueue()
    queue.submit(underrated(pilot))
    products, refusals = queue.apply_approved(pilot.products, NOW)
    assert "not reviewed" in refusals[0]
    assert {p["product_id"]: p["demand_level"] for p in products} == {p["product_id"]: p["demand_level"] for p in pilot.products}


@pytest.mark.parametrize("reviewer", ["ai", "AI-agent", "agent:offers", "system", "crossflow", ""])
def test_ai_and_system_identities_cannot_review(pilot, reviewer):
    queue = DemandReviewQueue()
    proposal = underrated(pilot)
    queue.submit(proposal)
    with pytest.raises(ReviewError):
        queue.review(proposal.proposal_id, reviewer=reviewer, decision="approve", reason=None, now=NOW)


@pytest.mark.parametrize("reviewer", ["aisha.manager", "agentina", "manager.maadi"])
def test_people_whose_names_start_like_a_bot_are_still_people(reviewer):
    from crossflow.demand import default_is_human
    assert default_is_human(reviewer)


def test_human_approval_applies_the_change_with_an_audit_trail(pilot):
    queue = DemandReviewQueue()
    proposal = underrated(pilot)
    queue.submit(proposal)
    queue.review(proposal.proposal_id, reviewer="manager.maadi", decision="approve", reason="matches the shelf", now=NOW)
    products, refusals = queue.apply_approved(pilot.products, NOW)
    assert refusals == []
    changed = next(p for p in products if p["product_id"] == proposal.product_id)
    assert changed["demand_level"] == "High"
    assert pilot.product(proposal.product_id)["demand_level"] == "Medium"  # input rows untouched
    applied = [a for a in queue.audit if a["action"] == "demand_level_applied"]
    assert applied == [{"action": "demand_level_applied", "proposal_id": proposal.proposal_id, "actor": "manager.maadi",
                        "product_id": proposal.product_id, "before": "Medium", "after": "High", "at": NOW}]
    # applying again changes nothing more
    again, _ = queue.apply_approved(products, NOW)
    assert len([a for a in queue.audit if a["action"] == "demand_level_applied"]) == 1


def test_ai_sourced_proposals_still_need_a_human(pilot):
    queue = DemandReviewQueue()
    proposal = underrated(pilot, source="ai")
    queue.submit(proposal)
    assert queue.apply_approved(pilot.products, NOW)[1]  # refused until reviewed
    queue.review(proposal.proposal_id, reviewer="manager.maadi", decision="approve", reason=None, now=NOW)
    products, refusals = queue.apply_approved(pilot.products, NOW)
    assert refusals == []


def test_rejection_needs_a_reason_and_changes_nothing(pilot):
    queue = DemandReviewQueue()
    proposal = underrated(pilot)
    queue.submit(proposal)
    with pytest.raises(ReviewError):
        queue.review(proposal.proposal_id, reviewer="manager.maadi", decision="reject", reason=" ", now=NOW)
    queue.review(proposal.proposal_id, reviewer="manager.maadi", decision="reject", reason="Ramadan spike", now=NOW)
    products, _ = queue.apply_approved(pilot.products, NOW)
    assert next(p for p in products if p["product_id"] == proposal.product_id)["demand_level"] == "Medium"


def test_stale_proposal_is_refused(pilot):
    queue = DemandReviewQueue()
    proposal = underrated(pilot)
    queue.submit(proposal)
    queue.review(proposal.proposal_id, reviewer="manager.maadi", decision="approve", reason=None, now=NOW)
    moved = [dict(p, demand_level="Low") if p["product_id"] == proposal.product_id else p for p in pilot.products]
    _, refusals = queue.apply_approved(moved, NOW)
    assert "stale" in refusals[0]


def test_replayed_proposal_and_double_review_are_refused(pilot):
    queue = DemandReviewQueue()
    proposal = underrated(pilot)
    assert queue.submit(proposal) is True
    assert queue.submit(proposal) is False
    queue.review(proposal.proposal_id, reviewer="manager.maadi", decision="approve", reason=None, now=NOW)
    with pytest.raises(ReviewError):
        queue.review(proposal.proposal_id, reviewer="other.manager", decision="approve", reason=None, now=NOW)


def test_promo_sales_do_not_count_as_normal_demand(pilot):
    pid = pilot.cases["low_demand_low_stock"]
    day = pilot.as_of - timedelta(days=1)
    promo = [{"branch_id": 1, "product_id": pid, "date": day, "quantity": 500, "offer_id": "o-1"}]
    proposals = propose_demand_levels(pilot.sales + promo, pilot.products, branch_id=1,
                                      period_start=pilot.as_of - timedelta(days=28), period_end=pilot.as_of, now=NOW)
    assert pid not in {p.product_id for p in proposals}


def test_period_too_short():
    with pytest.raises(ValueError):
        propose_demand_levels([], [], branch_id=1, period_start=NOW.date(), period_end=NOW.date() + timedelta(days=3), now=NOW)
