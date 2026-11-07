"""D2 - Candidate intake with evidence_ref and forecast_ref (Epic D: Offers).

A candidate arrives (from a stock signal, H1, or a manual proposal), is converted
to the standard Offer schema as a DRAFT, and is stored.
Validation (D3) and guardrails (D4/D5) run AFTER intake, not here.

Storage goes through a small repository interface so M1's real DB can replace
the in-memory version without touching this logic.
"""
from __future__ import annotations

from typing import Protocol

from offers.schema import Offer, OfferStatus


class IntakeError(ValueError):
    """Candidate rejected at intake (missing evidence/forecast or bad shape)."""


class OfferRepository(Protocol):
    def save(self, offer: Offer) -> None: ...
    def find_by_candidate_key(self, key: str) -> Offer | None: ...


class InMemoryOfferRepository:
    """Stand-in until the real offers table (A3) exists."""

    def __init__(self) -> None:
        self._rows: list[Offer] = []
        self._by_key: dict[str, Offer] = {}

    def save(self, offer: Offer, candidate_key: str | None = None) -> None:
        self._rows.append(offer)
        if candidate_key:
            self._by_key[candidate_key] = offer

    def find_by_candidate_key(self, key: str) -> Offer | None:
        return self._by_key.get(key)

    def all(self) -> list[Offer]:
        return list(self._rows)


def candidate_key(raw: dict) -> str:
    """Same signal + same branch + same type = same candidate (idempotent intake)."""
    return f"{raw['branch_id']}|{raw['evidence_ref']}|{raw['type']}"


def intake_candidate(raw: dict, repo: InMemoryOfferRepository) -> Offer:
    """Convert a raw candidate dict into a stored DRAFT Offer.

    Rules:
    - evidence_ref and forecast_ref are mandatory (OF-1): no evidence, no candidate.
    - Replaying the same candidate returns the existing offer, never a duplicate.
    - Anything the sender says about status/approval is ignored: always starts as draft.
    """
    for field in ("branch_id", "type", "evidence_ref", "forecast_ref"):
        if not raw.get(field):
            raise IntakeError(f"candidate is missing '{field}'")

    key = candidate_key(raw)
    existing = repo.find_by_candidate_key(key)
    if existing is not None:
        return existing

    clean = {k: v for k, v in raw.items() if k not in {"status", "approval_hash", "version", "offer_id"}}
    try:
        offer = Offer(**clean, status=OfferStatus.DRAFT)
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        raise IntakeError(str(exc)) from exc

    repo.save(offer, candidate_key=key)
    return offer