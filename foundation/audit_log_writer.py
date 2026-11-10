"""A11 - Audit log writer (actor, timestamp, version, run_id) (Epic A: Story FD-3).

Provides an append-only audit trail backed by `public.audit_log` in Supabase
(`SupabaseAuditBackend`) or an in-memory store (`InMemoryAuditBackend`) for
deterministic unit and CI testing.

Satisfies Task A11 Done Condition:
- 'Every run, decision and delivery is logged' with `actor`, `timestamp`
  (`occurred_at`), `version`, `run_id`, `branch_id`, `action`, `entity_type`,
  `entity_id`, and structured JSON `details`.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Protocol


VALID_AUDIT_CATEGORIES: FrozenSet[str] = frozenset({"RUN", "DECISION", "DELIVERY", "SECURITY"})


class AuditLogError(ValueError):
    """Raised when an audit log entry is missing required fields or has invalid types."""


@dataclass(frozen=True)
class AuditLogEntry:
    """Immutable audit log record matching `public.audit_log` in Supabase."""
    audit_id: int
    run_id: str
    branch_id: Optional[int]
    actor: str
    action: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    version: Optional[int] = None
    details: Dict[str, Any] = field(default_factory=dict)
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def timestamp(self) -> str:
        """Alias for `occurred_at` satisfying the (actor, timestamp, version, run_id) contract."""
        return self.occurred_at


class AuditLogBackend(Protocol):
    """Protocol for append-only persistence of `AuditLogEntry` records."""

    def append(
        self,
        *,
        run_id: str,
        branch_id: Optional[int],
        actor: str,
        action: str,
        entity_type: Optional[str],
        entity_id: Optional[str],
        version: Optional[int],
        details: Dict[str, Any],
        occurred_at: str,
    ) -> AuditLogEntry:
        ...

    def list_entries(
        self,
        *,
        run_id: Optional[str] = None,
        branch_id: Optional[int] = None,
        action: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> List[AuditLogEntry]:
        ...


class InMemoryAuditBackend:
    """Append-only in-memory backend for unit tests and CI runs."""

    def __init__(self) -> None:
        self._entries: List[AuditLogEntry] = []
        self._next_id: int = 1

    def append(
        self,
        *,
        run_id: str,
        branch_id: Optional[int],
        actor: str,
        action: str,
        entity_type: Optional[str],
        entity_id: Optional[str],
        version: Optional[int],
        details: Dict[str, Any],
        occurred_at: str,
    ) -> AuditLogEntry:
        entry = AuditLogEntry(
            audit_id=self._next_id,
            run_id=run_id,
            branch_id=branch_id,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            version=version,
            details=copy.deepcopy(details),
            occurred_at=occurred_at,
        )
        self._next_id += 1
        self._entries.append(entry)
        return copy.deepcopy(entry)

    def list_entries(
        self,
        *,
        run_id: Optional[str] = None,
        branch_id: Optional[int] = None,
        action: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> List[AuditLogEntry]:
        results: List[AuditLogEntry] = []
        for item in self._entries:
            if run_id is not None and item.run_id != run_id:
                continue
            if branch_id is not None and item.branch_id != int(branch_id):
                continue
            if action is not None and item.action != action:
                continue
            if entity_type is not None and item.entity_type != entity_type:
                continue
            if entity_id is not None and item.entity_id != str(entity_id):
                continue
            results.append(copy.deepcopy(item))
        return results


class SupabaseAuditBackend:
    """Live Supabase backend writing to `public.audit_log`."""

    def __init__(self, supabase_client: Any) -> None:
        self._client = supabase_client

    @classmethod
    def from_env(cls) -> "SupabaseAuditBackend":
        from supabase import create_client
        url = os.environ["SUPABASE_URL"]
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_KEY"]
        return cls(create_client(url, key))

    @staticmethod
    def _row_to_entry(row: Mapping[str, Any]) -> AuditLogEntry:
        return AuditLogEntry(
            audit_id=int(row["audit_id"]),
            run_id=str(row["run_id"]),
            branch_id=int(row["branch_id"]) if row.get("branch_id") is not None else None,
            actor=str(row["actor"]),
            action=str(row["action"]),
            entity_type=str(row["entity_type"]) if row.get("entity_type") is not None else None,
            entity_id=str(row["entity_id"]) if row.get("entity_id") is not None else None,
            version=int(row["version"]) if row.get("version") is not None else None,
            details=dict(row.get("details") or {}),
            occurred_at=str(row.get("occurred_at", "")),
        )

    def append(
        self,
        *,
        run_id: str,
        branch_id: Optional[int],
        actor: str,
        action: str,
        entity_type: Optional[str],
        entity_id: Optional[str],
        version: Optional[int],
        details: Dict[str, Any],
        occurred_at: str,
    ) -> AuditLogEntry:
        res = (
            self._client.table("audit_log")
            .insert(
                {
                    "run_id": run_id,
                    "branch_id": branch_id,
                    "actor": actor,
                    "action": action,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "version": version,
                    "details": details,
                    "occurred_at": occurred_at,
                }
            )
            .execute()
        )
        return self._row_to_entry(res.data[0])

    def list_entries(
        self,
        *,
        run_id: Optional[str] = None,
        branch_id: Optional[int] = None,
        action: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> List[AuditLogEntry]:
        query = self._client.table("audit_log").select("*")
        if run_id is not None:
            query = query.eq("run_id", run_id)
        if branch_id is not None:
            query = query.eq("branch_id", int(branch_id))
        if action is not None:
            query = query.eq("action", action)
        if entity_type is not None:
            query = query.eq("entity_type", entity_type)
        if entity_id is not None:
            query = query.eq("entity_id", str(entity_id))
        res = query.order("audit_id").execute()
        return [self._row_to_entry(r) for r in (getattr(res, "data", None) or [])]


class AuditLogWriter:
    """Centralized audit log writer for runs, human/guardrail decisions, and deliveries (Task A11)."""

    def __init__(self, backend: Optional[AuditLogBackend] = None) -> None:
        self._backend: AuditLogBackend = (
            backend if backend is not None else InMemoryAuditBackend()
        )

    @staticmethod
    def _validate_core_fields(
        *,
        run_id: str,
        actor: str,
        action: str,
        version: int,
        branch_id: Optional[int],
    ) -> tuple[str, str, str, int, Optional[int]]:
        if not isinstance(run_id, str) or not run_id.strip():
            raise AuditLogError("run_id must be a non-empty string.")
        if not isinstance(actor, str) or not actor.strip():
            raise AuditLogError("actor must be a non-empty string.")
        if not isinstance(action, str) or not action.strip():
            raise AuditLogError("action must be a non-empty string.")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise AuditLogError(f"version must be a positive integer (>= 1), got {version!r}.")
        if branch_id is not None:
            if isinstance(branch_id, bool) or not isinstance(branch_id, int) or branch_id <= 0:
                raise AuditLogError(f"branch_id must be a positive integer or None, got {branch_id!r}.")
        return run_id.strip(), actor.strip(), action.strip().upper(), int(version), branch_id

    def write(
        self,
        *,
        run_id: str,
        actor: str,
        action: str,
        version: int = 1,
        branch_id: Optional[int] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str | int] = None,
        details: Optional[Mapping[str, Any]] = None,
        timestamp: Optional[str | datetime] = None,
    ) -> AuditLogEntry:
        """Writes a validated, immutable audit log entry."""
        clean_run, clean_actor, clean_action, clean_ver, clean_branch = self._validate_core_fields(
            run_id=run_id,
            actor=actor,
            action=action,
            version=version,
            branch_id=branch_id,
        )

        if timestamp is None:
            ts_str = datetime.now(timezone.utc).isoformat()
        elif isinstance(timestamp, datetime):
            if timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None:
                raise AuditLogError("timestamp datetime must be timezone-aware.")
            ts_str = timestamp.astimezone(timezone.utc).isoformat()
        elif isinstance(timestamp, str) and timestamp.strip():
            ts_str = timestamp.strip()
        else:
            raise AuditLogError("Invalid timestamp provided to AuditLogWriter.")

        clean_entity_type = str(entity_type).strip().upper() if entity_type else None
        clean_entity_id = str(entity_id).strip() if entity_id is not None else None
        clean_details = copy.deepcopy(dict(details)) if details else {}

        return self._backend.append(
            run_id=clean_run,
            branch_id=clean_branch,
            actor=clean_actor,
            action=clean_action,
            entity_type=clean_entity_type,
            entity_id=clean_entity_id,
            version=clean_ver,
            details=clean_details,
            occurred_at=ts_str,
        )

    # -------------------------------------------------------------------------
    # High-level helpers for the 3 mandatory categories: Run, Decision, Delivery
    # -------------------------------------------------------------------------
    def log_run(
        self,
        *,
        run_id: str,
        branch_id: int,
        actor: str,
        workflow_name: str,
        status: str,
        version: int = 1,
        details: Optional[Mapping[str, Any]] = None,
        timestamp: Optional[str | datetime] = None,
    ) -> AuditLogEntry:
        """Logs a workflow run event (e.g. RUN_STARTED, RUN_INTERRUPTED, RUN_COMPLETED)."""
        status_norm = str(status).strip().upper()
        action = status_norm if status_norm.startswith("RUN_") else f"RUN_{status_norm}"
        payload = {"category": "RUN", "workflow_name": str(workflow_name).strip()}
        if details:
            payload.update(dict(details))

        return self.write(
            run_id=run_id,
            branch_id=branch_id,
            actor=actor,
            action=action,
            version=version,
            entity_type="WORKFLOW_RUN",
            entity_id=run_id,
            details=payload,
            timestamp=timestamp,
        )

    def log_decision(
        self,
        *,
        run_id: str,
        branch_id: int,
        actor: str,
        decision: str,
        entity_type: str,
        entity_id: str | int,
        version: int,
        reason: Optional[str] = None,
        reviewed_hash: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        timestamp: Optional[str | datetime] = None,
    ) -> AuditLogEntry:
        """Logs a human review or guardrail decision (e.g. DECISION_APPROVE, DECISION_REJECT)."""
        dec_norm = str(decision).strip().upper()
        action = dec_norm if dec_norm.startswith("DECISION_") else f"DECISION_{dec_norm}"
        payload: Dict[str, Any] = {"category": "DECISION", "decision": dec_norm}
        if reason is not None:
            payload["reason"] = reason
        if reviewed_hash is not None:
            payload["reviewed_hash"] = reviewed_hash
        if details:
            payload.update(dict(details))

        return self.write(
            run_id=run_id,
            branch_id=branch_id,
            actor=actor,
            action=action,
            version=version,
            entity_type=entity_type,
            entity_id=str(entity_id),
            details=payload,
            timestamp=timestamp,
        )

    def log_delivery(
        self,
        *,
        run_id: str,
        branch_id: int,
        actor: str,
        channel: str,
        recipient: str,
        delivery_status: str,
        entity_type: str,
        entity_id: str | int,
        version: int = 1,
        outbox_message_id: Optional[int] = None,
        details: Optional[Mapping[str, Any]] = None,
        timestamp: Optional[str | datetime] = None,
    ) -> AuditLogEntry:
        """Logs an outbound delivery event (e.g. DELIVERY_SENT, DELIVERY_FAILED)."""
        status_norm = str(delivery_status).strip().upper()
        action = status_norm if status_norm.startswith("DELIVERY_") else f"DELIVERY_{status_norm}"
        payload: Dict[str, Any] = {
            "category": "DELIVERY",
            "channel": str(channel).strip().upper(),
            "recipient": str(recipient).strip(),
            "delivery_status": status_norm,
        }
        if outbox_message_id is not None:
            payload["outbox_message_id"] = int(outbox_message_id)
        if details:
            payload.update(dict(details))

        return self.write(
            run_id=run_id,
            branch_id=branch_id,
            actor=actor,
            action=action,
            version=version,
            entity_type=entity_type,
            entity_id=str(entity_id),
            details=payload,
            timestamp=timestamp,
        )

    def list_entries(
        self,
        *,
        run_id: Optional[str] = None,
        branch_id: Optional[int] = None,
        action: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> List[AuditLogEntry]:
        """Queries audit log entries matching the given filters."""
        return self._backend.list_entries(
            run_id=run_id,
            branch_id=branch_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
        )

    def has_logged_run_decision_and_delivery(self, run_id: str) -> bool:
        """Returns True if `run_id` has at least one RUN, one DECISION, and one DELIVERY entry."""
        entries = self.list_entries(run_id=run_id)
        categories = {e.details.get("category") for e in entries}
        return {"RUN", "DECISION", "DELIVERY"}.issubset(categories)
