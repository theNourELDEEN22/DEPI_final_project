"""A12 - Approval service: actor, time, reason, version, hash (Epic A: Story FD-3).

Records and verifies human review decisions (`APPROVE`, `REJECT`, `REQUEST_CHANGES`)
for both `RESTOCK_ORDER` (Epic B) and `OFFER` (Epic D) workflows.

Backed by `public.approvals` in Supabase (`SupabaseApprovalBackend`) or an
in-memory store (`InMemoryApprovalBackend`) for deterministic unit and CI testing.

Key guarantees:
1. Cryptographic Version Hashing (`compute_version_hash`):
   Computes a canonical SHA-256 hash over `(entity_type, entity_id, branch_id,
   version, snapshot_payload)` so any subsequent change to quantity, price,
   discount, or version immediately invalidates earlier hashes (satisfying D8:
   'Old approval hash no longer matches').
2. Exact Version Binding (`Done when: Approval stores the exact version reviewed`):
   Stores `actor_id`, `actor_role`, `decided_at` (`time`), `reason`,
   `reviewed_version` (`version`), and `reviewed_hash` (`hash`), and rejects any
   decision where the reviewer's supplied version or hash does not match the
   current entity snapshot.
3. Execution Gate (`verify_approved_for_execution`):
   Before `SendEmailNode` (B10) or `POS Activation` (D10) executes, verifies that
   the latest decision for `(entity_type, entity_id, branch_id)` is `APPROVE` and
   that `reviewed_version` and `reviewed_hash` match the exact payload about to be
   dispatched.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Protocol

from foundation.audit_log_writer import AuditLogWriter


VALID_ENTITY_TYPES: FrozenSet[str] = frozenset({"RESTOCK_ORDER", "OFFER"})
VALID_DECISIONS: FrozenSet[str] = frozenset({"APPROVE", "REJECT", "REQUEST_CHANGES"})


class ApprovalError(ValueError):
    """Base exception for approval service errors."""


class VersionMismatchError(ApprovalError):
    """Raised when the reviewed version or cryptographic hash does not match the current draft."""


class UnapprovedExecutionError(ApprovalError):
    """Raised when an order or offer attempts to execute without a valid matching approval."""


@dataclass(frozen=True)
class ApprovalRecord:
    """Immutable approval record matching `public.approvals` in Supabase."""
    approval_id: int
    entity_type: str
    entity_id: int
    branch_id: int
    actor_id: str
    actor_role: str
    decision: str
    reason: Optional[str]
    reviewed_version: int
    reviewed_hash: str
    decided_at: str

    @property
    def actor(self) -> str:
        return self.actor_id

    @property
    def time(self) -> str:
        return self.decided_at

    @property
    def version(self) -> int:
        return self.reviewed_version

    @property
    def hash(self) -> str:
        return self.reviewed_hash


class ApprovalBackend(Protocol):
    """Protocol for persisting and querying `public.approvals`."""

    def insert_approval(
        self,
        *,
        entity_type: str,
        entity_id: int,
        branch_id: int,
        actor_id: str,
        actor_role: str,
        decision: str,
        reason: Optional[str],
        reviewed_version: int,
        reviewed_hash: str,
        decided_at: str,
    ) -> ApprovalRecord:
        ...

    def list_approvals(
        self,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        reviewed_version: Optional[int] = None,
    ) -> List[ApprovalRecord]:
        ...


def compute_version_hash(
    *,
    entity_type: str,
    entity_id: int,
    branch_id: int,
    version: int,
    payload: Mapping[str, Any],
) -> str:
    """Computes a canonical SHA-256 hex digest binding entity identity, version, and payload."""
    norm_type = str(entity_type).strip().upper()
    if norm_type not in VALID_ENTITY_TYPES:
        raise ApprovalError(
            f"Invalid entity_type '{entity_type}'. Must be one of {sorted(VALID_ENTITY_TYPES)}."
        )
    if isinstance(entity_id, bool) or not isinstance(entity_id, int) or entity_id <= 0:
        raise ApprovalError(f"entity_id must be a positive integer, got {entity_id!r}.")
    if isinstance(branch_id, bool) or not isinstance(branch_id, int) or branch_id <= 0:
        raise ApprovalError(f"branch_id must be a positive integer, got {branch_id!r}.")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ApprovalError(f"version must be a positive integer (>= 1), got {version!r}.")
    if not isinstance(payload, Mapping):
        raise ApprovalError("payload must be a mapping/dictionary.")

    envelope = {
        "entity_type": norm_type,
        "entity_id": int(entity_id),
        "branch_id": int(branch_id),
        "version": int(version),
        "payload": dict(payload),
    }
    canonical = json.dumps(envelope, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class InMemoryApprovalBackend:
    """Append-only in-memory backend for unit and CI tests."""

    def __init__(self) -> None:
        self._records: List[ApprovalRecord] = []
        self._next_id: int = 1

    def insert_approval(
        self,
        *,
        entity_type: str,
        entity_id: int,
        branch_id: int,
        actor_id: str,
        actor_role: str,
        decision: str,
        reason: Optional[str],
        reviewed_version: int,
        reviewed_hash: str,
        decided_at: str,
    ) -> ApprovalRecord:
        rec = ApprovalRecord(
            approval_id=self._next_id,
            entity_type=entity_type,
            entity_id=int(entity_id),
            branch_id=int(branch_id),
            actor_id=actor_id,
            actor_role=actor_role,
            decision=decision,
            reason=reason,
            reviewed_version=int(reviewed_version),
            reviewed_hash=reviewed_hash,
            decided_at=decided_at,
        )
        self._next_id += 1
        self._records.append(rec)
        return copy.deepcopy(rec)

    def list_approvals(
        self,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        reviewed_version: Optional[int] = None,
    ) -> List[ApprovalRecord]:
        results: List[ApprovalRecord] = []
        norm_type = entity_type.strip().upper() if entity_type else None
        for rec in self._records:
            if norm_type is not None and rec.entity_type != norm_type:
                continue
            if entity_id is not None and rec.entity_id != int(entity_id):
                continue
            if branch_id is not None and rec.branch_id != int(branch_id):
                continue
            if reviewed_version is not None and rec.reviewed_version != int(reviewed_version):
                continue
            results.append(copy.deepcopy(rec))
        return results


class SupabaseApprovalBackend:
    """Live Supabase backend persisting to `public.approvals`."""

    def __init__(self, supabase_client: Any) -> None:
        self._client = supabase_client

    @classmethod
    def from_env(cls) -> "SupabaseApprovalBackend":
        from supabase import create_client
        url = os.environ["SUPABASE_URL"]
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_KEY"]
        return cls(create_client(url, key))

    @staticmethod
    def _row_to_record(row: Mapping[str, Any]) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=int(row["approval_id"]),
            entity_type=str(row["entity_type"]),
            entity_id=int(row["entity_id"]),
            branch_id=int(row["branch_id"]),
            actor_id=str(row["actor_id"]),
            actor_role=str(row["actor_role"]),
            decision=str(row["decision"]),
            reason=row.get("reason"),
            reviewed_version=int(row["reviewed_version"]),
            reviewed_hash=str(row["reviewed_hash"]),
            decided_at=str(row.get("decided_at", "")),
        )

    def insert_approval(
        self,
        *,
        entity_type: str,
        entity_id: int,
        branch_id: int,
        actor_id: str,
        actor_role: str,
        decision: str,
        reason: Optional[str],
        reviewed_version: int,
        reviewed_hash: str,
        decided_at: str,
    ) -> ApprovalRecord:
        res = (
            self._client.table("approvals")
            .insert(
                {
                    "entity_type": entity_type,
                    "entity_id": int(entity_id),
                    "branch_id": int(branch_id),
                    "actor_id": actor_id,
                    "actor_role": actor_role,
                    "decision": decision,
                    "reason": reason,
                    "reviewed_version": int(reviewed_version),
                    "reviewed_hash": reviewed_hash,
                    "decided_at": decided_at,
                }
            )
            .execute()
        )
        return self._row_to_record(res.data[0])

    def list_approvals(
        self,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        reviewed_version: Optional[int] = None,
    ) -> List[ApprovalRecord]:
        query = self._client.table("approvals").select("*")
        if entity_type is not None:
            query = query.eq("entity_type", entity_type.strip().upper())
        if entity_id is not None:
            query = query.eq("entity_id", int(entity_id))
        if branch_id is not None:
            query = query.eq("branch_id", int(branch_id))
        if reviewed_version is not None:
            query = query.eq("reviewed_version", int(reviewed_version))
        res = query.order("approval_id").execute()
        return [self._row_to_record(r) for r in (getattr(res, "data", None) or [])]


class ApprovalService:
    """Service managing versioned human approvals and cryptographic hash verification (Task A12)."""

    def __init__(
        self,
        backend: Optional[ApprovalBackend] = None,
        *,
        audit_writer: Optional[AuditLogWriter] = None,
    ) -> None:
        self._backend: ApprovalBackend = (
            backend if backend is not None else InMemoryApprovalBackend()
        )
        self._audit = audit_writer

    def record_decision(
        self,
        *,
        entity_type: str,
        entity_id: int,
        branch_id: int,
        actor_id: str,
        actor_role: str,
        decision: str,
        reviewed_version: int,
        current_version: int,
        payload_snapshot: Mapping[str, Any],
        reviewed_hash: Optional[str] = None,
        reason: Optional[str] = None,
        run_id: Optional[str] = None,
        decided_at: Optional[str | datetime] = None,
    ) -> ApprovalRecord:
        """Validates and stores a reviewer's decision against the exact version & hash reviewed.

        - Verifies `reviewed_version == current_version` (preventing approval of stale drafts).
        - Computes the authoritative SHA-256 hash of `payload_snapshot` at `reviewed_version`;
          if the caller supplied `reviewed_hash` from a review card (D6/D7), verifies that it
          matches the computed hash (`VersionMismatchError` on mismatch).
        - Requires a non-empty `reason` whenever `decision` is `REJECT` or `REQUEST_CHANGES`.
        """
        norm_type = str(entity_type).strip().upper()
        if norm_type not in VALID_ENTITY_TYPES:
            raise ApprovalError(
                f"Invalid entity_type '{entity_type}'. Must be one of {sorted(VALID_ENTITY_TYPES)}."
            )

        norm_decision = str(decision).strip().upper()
        if norm_decision not in VALID_DECISIONS:
            raise ApprovalError(
                f"Invalid decision '{decision}'. Must be one of {sorted(VALID_DECISIONS)}."
            )

        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ApprovalError("actor_id must be a non-empty string.")
        if not isinstance(actor_role, str) or not actor_role.strip():
            raise ApprovalError("actor_role must be a non-empty string.")

        if isinstance(reviewed_version, bool) or not isinstance(reviewed_version, int) or reviewed_version <= 0:
            raise ApprovalError(f"reviewed_version must be a positive integer, got {reviewed_version!r}.")
        if isinstance(current_version, bool) or not isinstance(current_version, int) or current_version <= 0:
            raise ApprovalError(f"current_version must be a positive integer, got {current_version!r}.")

        if reviewed_version != current_version:
            raise VersionMismatchError(
                f"Stale approval rejected: reviewer submitted version {reviewed_version}, "
                f"but current entity version is {current_version}."
            )

        clean_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
        if norm_decision in ("REJECT", "REQUEST_CHANGES") and not clean_reason:
            raise ApprovalError(
                f"Decision '{norm_decision}' requires a non-empty reason/feedback note."
            )

        computed_hash = compute_version_hash(
            entity_type=norm_type,
            entity_id=entity_id,
            branch_id=branch_id,
            version=reviewed_version,
            payload=payload_snapshot,
        )

        if reviewed_hash is not None and str(reviewed_hash).strip() != computed_hash:
            raise VersionMismatchError(
                f"Approval hash mismatch for {norm_type} #{entity_id} (v{reviewed_version}): "
                f"expected '{computed_hash}', got '{reviewed_hash}'."
            )

        if decided_at is None:
            ts_str = datetime.now(timezone.utc).isoformat()
        elif isinstance(decided_at, datetime):
            if decided_at.tzinfo is None or decided_at.tzinfo.utcoffset(decided_at) is None:
                raise ApprovalError("decided_at datetime must be timezone-aware.")
            ts_str = decided_at.astimezone(timezone.utc).isoformat()
        elif isinstance(decided_at, str) and decided_at.strip():
            ts_str = decided_at.strip()
        else:
            raise ApprovalError("Invalid decided_at timestamp.")

        record = self._backend.insert_approval(
            entity_type=norm_type,
            entity_id=int(entity_id),
            branch_id=int(branch_id),
            actor_id=actor_id.strip(),
            actor_role=actor_role.strip(),
            decision=norm_decision,
            reason=clean_reason,
            reviewed_version=int(reviewed_version),
            reviewed_hash=computed_hash,
            decided_at=ts_str,
        )

        if self._audit is not None and run_id:
            self._audit.log_decision(
                run_id=run_id,
                branch_id=int(branch_id),
                actor=record.actor_id,
                decision=record.decision,
                entity_type=record.entity_type,
                entity_id=record.entity_id,
                version=record.reviewed_version,
                reason=record.reason,
                reviewed_hash=record.reviewed_hash,
                details={"actor_role": record.actor_role, "approval_id": record.approval_id},
                timestamp=record.decided_at,
            )

        return record

    def get_latest_decision(
        self,
        *,
        entity_type: str,
        entity_id: int,
        branch_id: int,
    ) -> Optional[ApprovalRecord]:
        """Returns the most recent `ApprovalRecord` for `(entity_type, entity_id, branch_id)`."""
        records = self._backend.list_approvals(
            entity_type=entity_type,
            entity_id=entity_id,
            branch_id=branch_id,
        )
        return records[-1] if records else None

    def verify_approved_for_execution(
        self,
        *,
        entity_type: str,
        entity_id: int,
        branch_id: int,
        current_version: int,
        current_payload: Mapping[str, Any],
    ) -> ApprovalRecord:
        """Verifies that the exact current version and payload have an active `APPROVE` record.

        Raises:
        - `UnapprovedExecutionError` if no approval exists or the latest decision is not `APPROVE`.
        - `VersionMismatchError` if the entity version or payload changed after the approval was recorded.
        """
        latest = self.get_latest_decision(
            entity_type=entity_type,
            entity_id=entity_id,
            branch_id=branch_id,
        )
        if latest is None:
            raise UnapprovedExecutionError(
                f"Cannot execute {entity_type} #{entity_id} (branch {branch_id}): no approval record found."
            )
        if latest.decision != "APPROVE":
            raise UnapprovedExecutionError(
                f"Cannot execute {entity_type} #{entity_id} (branch {branch_id}): "
                f"latest decision is '{latest.decision}', not 'APPROVE'."
            )
        if latest.reviewed_version != int(current_version):
            raise VersionMismatchError(
                f"Approval version mismatch for {entity_type} #{entity_id}: "
                f"approved version={latest.reviewed_version}, current version={current_version}."
            )

        current_hash = compute_version_hash(
            entity_type=entity_type,
            entity_id=entity_id,
            branch_id=branch_id,
            version=current_version,
            payload=current_payload,
        )
        if latest.reviewed_hash != current_hash:
            raise VersionMismatchError(
                f"Payload modified after approval for {entity_type} #{entity_id} (v{current_version}): "
                f"approved hash '{latest.reviewed_hash}' != current hash '{current_hash}'."
            )

        return latest

    def list_history(
        self,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        branch_id: Optional[int] = None,
        reviewed_version: Optional[int] = None,
    ) -> List[ApprovalRecord]:
        return self._backend.list_approvals(
            entity_type=entity_type,
            entity_id=entity_id,
            branch_id=branch_id,
            reviewed_version=reviewed_version,
        )
