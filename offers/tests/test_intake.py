from datetime import datetime, timedelta, timezone

import pytest

from offers.intake import InMemoryOfferRepository, IntakeError, intake_candidate
from offers.schema import OfferStatus

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def raw(**over):
    base = dict(
        branch_id="b1",
        type="discount",
        product_ids=["p1"],
        start_date=NOW,
        end_date=NOW + timedelta(days=7),
        discount_percent="20",
        evidence_ref="sig-1",
        forecast_ref="fc-1",
    )
    base.update(over)
    return base


def test_candidate_stored_as_draft_in_standard_schema():
    repo = InMemoryOfferRepository()
    offer = intake_candidate(raw(), repo)
    assert offer.status == OfferStatus.DRAFT
    assert offer.evidence_ref == "sig-1" and offer.forecast_ref == "fc-1"
    assert len(repo.all()) == 1


@pytest.mark.parametrize("missing", ["evidence_ref", "forecast_ref", "branch_id"])
def test_missing_required_field_rejected(missing):
    repo = InMemoryOfferRepository()
    bad = raw()
    bad.pop(missing)
    with pytest.raises(IntakeError):
        intake_candidate(bad, repo)
    assert repo.all() == []


def test_replay_gives_one_offer():
    repo = InMemoryOfferRepository()
    a = intake_candidate(raw(), repo)
    b = intake_candidate(raw(), repo)
    assert a.offer_id == b.offer_id
    assert len(repo.all()) == 1


def test_sender_cannot_set_status_or_approval():
    repo = InMemoryOfferRepository()
    offer = intake_candidate(raw(status="approved", approval_hash="fake"), repo)
    assert offer.status == OfferStatus.DRAFT
    assert offer.approval_hash is None


def test_bad_shape_rejected_with_intake_error():
    repo = InMemoryOfferRepository()
    with pytest.raises(IntakeError):
        intake_candidate(raw(type="buy2get1"), repo)  # buy/get qty missing