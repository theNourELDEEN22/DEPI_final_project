"""Operator tools behind the runbooks (E15).

reconcile_pos   compare what the POS has live with what Offers recorded. Used after
                an UNCONFIRMED activation or a STOP_UNCONFIRMED (runbook R3).
stuck_outbox    outbox messages that need an operator (runbook R1).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from offers.pos_activation import idempotency_key_for
from offers.schema import Offer, OfferStatus


def reconcile_pos(live_on_pos: dict[str, dict[str, Any]], offers: Iterable[Offer]) -> list[dict[str, str]]:
    """Every difference between the POS and Offers, with what to do about it.

    `live_on_pos` maps activation key -> payload (MockPOS.live_offers(), or the real
    POS's list of live promotions mapped the same way)."""
    findings: list[dict[str, str]] = []
    known_keys: set[str] = set()
    for offer in offers:
        if not offer.approval_hash:
            continue
        key = idempotency_key_for(offer)
        known_keys.add(key)
        live = key in live_on_pos
        if offer.status == OfferStatus.ACTIVE and not live:
            findings.append({"offer_id": offer.offer_id, "key": key, "problem": "recorded ACTIVE but not live on the POS",
                             "action": "check the POS; re-run activation with the SAME key, or mark the offer stopped"})
        elif offer.status == OfferStatus.APPROVED and live:
            findings.append({"offer_id": offer.offer_id, "key": key, "problem": "live on the POS but recorded APPROVED (unconfirmed activation)",
                             "action": "confirm on the POS, then record the offer ACTIVE"})
        elif offer.status in (OfferStatus.STOPPED, OfferStatus.ENDED) and live:
            findings.append({"offer_id": offer.offer_id, "key": key, "problem": f"recorded {offer.status.value.upper()} but still live on the POS",
                             "action": "URGENT: stop it on the POS now; it is selling outside its approval"})
        elif offer.status == OfferStatus.ACTIVE and live and live_on_pos[key].get("approval_hash") != offer.approval_hash:
            findings.append({"offer_id": offer.offer_id, "key": key, "problem": "live terms do not match the approved version",
                             "action": "URGENT: stop it on the POS and escalate to the Sales Team"})
    for key in sorted(set(live_on_pos) - known_keys):
        findings.append({"offer_id": str(live_on_pos[key].get("offer_id")), "key": key,
                         "problem": "live on the POS but unknown to Offers",
                         "action": "URGENT: stop it on the POS; no approval exists for it"})
    return findings


def stuck_outbox(messages: Iterable[dict[str, Any]], now: datetime, max_pending: timedelta = timedelta(minutes=30)) -> list[dict[str, Any]]:
    """FAILED messages, and PENDING ones older than max_pending (outbox_messages rows)."""
    stuck = []
    for message in messages:
        created = message["created_at"]
        if message["status"] == "FAILED" or (message["status"] == "PENDING" and now - created > max_pending):
            stuck.append(message)
    return stuck
