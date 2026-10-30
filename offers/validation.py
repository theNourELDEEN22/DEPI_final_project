"""D3 - Schema and business-rule validation; log rejection reasons (Epic D: Offers).

Runs after intake (D2) and before guardrails (D4/D5).
A failed offer is marked REJECTED, its reasons are logged, and it never reaches approval.
All reasons are collected (not just the first) so the fix is done in one round.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from offers.schema import Offer, OfferStatus


@dataclass(frozen=True)
class ValidationConfig:
    # Placeholders until the Week 1 decisions meeting (E1); later they come from M1's config service (A4).
    min_duration_days: int = 1
    max_duration_days: int = 30
    start_grace_minutes: int = 5


class CatalogReader(Protocol):
    """Read-only view of branches/products. M1's DB (A1) will implement this."""

    def branch_exists(self, branch_id: str) -> bool: ...
    def product_in_branch(self, branch_id: str, product_id: str) -> bool: ...


@dataclass
class InMemoryCatalog:
    """Test stand-in: {branch_id: {product_ids}}."""

    data: dict[str, set[str]] = field(default_factory=dict)

    def branch_exists(self, branch_id: str) -> bool:
        return branch_id in self.data

    def product_in_branch(self, branch_id: str, product_id: str) -> bool:
        return product_id in self.data.get(branch_id, set())


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reasons: tuple[str, ...] = ()


@dataclass
class RejectionLog:
    """In-memory stand-in for the audit log (A11)."""

    entries: list[dict] = field(default_factory=list)

    def record(self, offer: Offer, reasons: tuple[str, ...], at: datetime) -> None:
        self.entries.append(
            {
                "offer_id": offer.offer_id,
                "version": offer.version,
                "stage": "validation",
                "reasons": list(reasons),
                "at": at,
            }
        )


def validate_offer(
    offer: Offer,
    catalog: CatalogReader,
    config: ValidationConfig,
    now: datetime,
) -> ValidationResult:
    reasons: list[str] = []

    # --- branch and products ---
    branch_ok = catalog.branch_exists(offer.branch_id)
    if not branch_ok:
        reasons.append(f"unknown branch: {offer.branch_id}")
    else:
        for pid in offer.product_ids:
            if not catalog.product_in_branch(offer.branch_id, pid):
                reasons.append(f"product {pid} does not belong to branch {offer.branch_id}")
        for item in offer.bundle_items or []:
            if not catalog.product_in_branch(offer.branch_id, item.product_id):
                reasons.append(
                    f"bundle product {item.product_id} does not belong to branch {offer.branch_id}"
                )

    # --- dates ---
    if offer.start_date < now - timedelta(minutes=config.start_grace_minutes):
        reasons.append("start_date is in the past")
    duration = offer.end_date - offer.start_date
    if duration < timedelta(days=config.min_duration_days):
        reasons.append(f"duration is shorter than {config.min_duration_days} day(s)")
    if duration > timedelta(days=config.max_duration_days):
        reasons.append(f"duration is longer than {config.max_duration_days} day(s)")

    return ValidationResult(ok=not reasons, reasons=tuple(reasons))


def run_validation(
    offer: Offer,
    catalog: CatalogReader,
    config: ValidationConfig,
    log: RejectionLog,
    now: datetime | None = None,
) -> tuple[Offer, ValidationResult]:
    """Validate; on failure return the offer as REJECTED and log the reasons.
    On success the offer is returned unchanged (still a draft, waiting for guardrails)."""
    now = now or datetime.now(timezone.utc)
    result = validate_offer(offer, catalog, config, now)
    if result.ok:
        return offer, result
    log.record(offer, result.reasons, now)
    return offer.model_copy(update={"status": OfferStatus.REJECTED}), result