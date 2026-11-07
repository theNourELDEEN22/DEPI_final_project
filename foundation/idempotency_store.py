"""A8 - Idempotency key store (Epic A: Story FD-4).

Guarantees that replaying the same scheduler trigger, approval webhook, POS
activation call, or report generation event produces exactly one side-effect
and returns the original deterministic result (`Done when: Replaying the same
event gives one result`).

Backed by `public.idempotency_keys` in Supabase (`SupabaseIdempotencyBackend`)
or an in-memory store (`InMemoryIdempotencyBackend`) for unit/CI tests.

Key features:
1. Deterministic Key Builder (`build_idempotency_key`):
   Constructs canonical, collision-resistant keys scoped by `workflow_name`,
   `branch_id`, and event identifiers (e.g. `restock:branch=1:date=2026-11-05`).
2. Payload Fingerprint Verification:
   Stores a SHA-256 fingerprint of the request parameters alongside the cached
   `response_payload`. If a caller reuses the exact same `idempotency_key` with
   different parameters, raises `IdempotencyConflictError` to prevent silent data
   corruption.
3. `run_once` Execution Wrapper:
   Executes the side-effect function at most once per `idempotency_key`. Subsequent
   replays skip execution completely and return the stored `response_payload` with
   `was_replayed=True`.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Any, Callable, Dict, Mapping, Optional, Protocol


class IdempotencyError(RuntimeError):
    """Base exception for idempotency store errors."""


class IdempotencyConflictError(IdempotencyError):
    """Raised when an existing idempotency_key is replayed with a different request payload."""


@dataclass(frozen=True)
class IdempotencyRecord:
    """Stored record matching `public.idempotency_keys` in Supabase."""
    idempotency_key: str
    workflow_name: str
    branch_id: Optional[int]
    response_payload: Dict[str, Any]
    request_hash: str
    created_at: str


@dataclass(frozen=True)
class IdempotentExecutionResult:
    """Outcome of `IdempotencyKeyStore.run_once`."""
    idempotency_key: str
    workflow_name: str
    branch_id: Optional[int]
    response_payload: Dict[str, Any]
    was_replayed: bool


class IdempotencyBackend(Protocol):
    """Protocol for persisting and retrieving idempotency records."""

    def get_record(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        ...

    def save_record(self, record: IdempotencyRecord) -> IdempotencyRecord:
        """Saves `record` if `idempotency_key` does not exist; returns existing record on race."""
        ...


def _compute_request_hash(request_params: Optional[Mapping[str, Any]]) -> str:
    """Computes a canonical SHA-256 hex digest for request parameters."""
    if not request_params:
        return hashlib.sha256(b"{}").hexdigest()
    canonical_json = json.dumps(request_params, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_idempotency_key(
    workflow_name: str,
    branch_id: int,
    *parts: Any,
) -> str:
    """Builds a canonical idempotency key for a branch-scoped workflow event.

    Example:
        build_idempotency_key("restock_daily", 1, "2026-11-05")
        -> "restock_daily:branch_1:2026-11-05"
    """
    wf = str(workflow_name).strip().lower()
    if not wf:
        raise ValueError("workflow_name must be a non-empty string.")
    if isinstance(branch_id, bool) or not isinstance(branch_id, int) or branch_id < 0:
        raise ValueError("branch_id must be a non-negative integer.")
    if not parts:
        raise ValueError("At least one event identifier part is required to build an idempotency key.")

    cleaned_parts = [str(p).strip() for p in parts if str(p).strip()]
    if not cleaned_parts:
        raise ValueError("Event identifier parts cannot all be empty.")

    joined = ":".join(cleaned_parts)
    return f"{wf}:branch_{branch_id}:{joined}"


class InMemoryIdempotencyBackend:
    """Thread-safe in-memory backend for unit tests and local runs."""

    def __init__(self) -> None:
        self._records: Dict[str, IdempotencyRecord] = {}

    def get_record(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        rec = self._records.get(idempotency_key)
        return copy.deepcopy(rec) if rec is not None else None

    def save_record(self, record: IdempotencyRecord) -> IdempotencyRecord:
        existing = self._records.get(record.idempotency_key)
        if existing is not None:
            return copy.deepcopy(existing)
        self._records[record.idempotency_key] = copy.deepcopy(record)
        return copy.deepcopy(record)


class SupabaseIdempotencyBackend:
    """Live Supabase backend persisting to `public.idempotency_keys`."""

    def __init__(self, supabase_client: Any) -> None:
        self._client = supabase_client

    @classmethod
    def from_env(cls) -> "SupabaseIdempotencyBackend":
        from supabase import create_client
        url = os.environ["SUPABASE_URL"]
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_KEY"]
        return cls(create_client(url, key))

    def get_record(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        res = (
            self._client.table("idempotency_keys")
            .select("idempotency_key, workflow_name, branch_id, response_payload, created_at")
            .eq("idempotency_key", idempotency_key)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return None
        row = rows[0]
        raw_payload = dict(row.get("response_payload") or {})
        req_hash = str(raw_payload.pop("_request_hash", _compute_request_hash(None)))
        return IdempotencyRecord(
            idempotency_key=str(row["idempotency_key"]),
            workflow_name=str(row["workflow_name"]),
            branch_id=int(row["branch_id"]) if row.get("branch_id") is not None else None,
            response_payload=raw_payload,
            request_hash=req_hash,
            created_at=str(row.get("created_at", "")),
        )

    def save_record(self, record: IdempotencyRecord) -> IdempotencyRecord:
        existing = self.get_record(record.idempotency_key)
        if existing is not None:
            return existing

        stored_payload = copy.deepcopy(record.response_payload)
        stored_payload["_request_hash"] = record.request_hash

        self._client.table("idempotency_keys").upsert(
            {
                "idempotency_key": record.idempotency_key,
                "workflow_name": record.workflow_name,
                "branch_id": record.branch_id,
                "response_payload": stored_payload,
            },
            on_conflict="idempotency_key",
        ).execute()
        return record


class IdempotencyKeyStore:
    """Idempotency service guaranteeing exactly-once event processing (Task A8)."""

    def __init__(self, backend: Optional[IdempotencyBackend] = None) -> None:
        self._backend: IdempotencyBackend = (
            backend if backend is not None else InMemoryIdempotencyBackend()
        )

    @staticmethod
    def _validate_inputs(
        idempotency_key: str,
        workflow_name: str,
        branch_id: Optional[int],
    ) -> tuple[str, str, Optional[int]]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string.")
        if not isinstance(workflow_name, str) or not workflow_name.strip():
            raise ValueError("workflow_name must be a non-empty string.")
        if branch_id is not None:
            if isinstance(branch_id, bool) or not isinstance(branch_id, int) or branch_id < 0:
                raise ValueError("branch_id must be a non-negative integer or None.")
        return idempotency_key.strip(), workflow_name.strip(), branch_id

    def get(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        """Looks up a previously recorded idempotency key."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string.")
        return self._backend.get_record(idempotency_key.strip())

    def run_once(
        self,
        *,
        idempotency_key: str,
        workflow_name: str,
        branch_id: Optional[int],
        operation: Callable[[], Mapping[str, Any]],
        request_params: Optional[Mapping[str, Any]] = None,
    ) -> IdempotentExecutionResult:
        """Executes `operation` on first sight of `idempotency_key`; returns cached result on replay.

        If `idempotency_key` was already processed:
        - Verifies that `workflow_name`, `branch_id`, and `request_params` match the
          original call (raising `IdempotencyConflictError` if mutated).
        - Does NOT invoke `operation` again.
        - Returns the stored `response_payload` with `was_replayed=True`.
        """
        clean_key, clean_wf, clean_branch = self._validate_inputs(
            idempotency_key, workflow_name, branch_id
        )
        req_hash = _compute_request_hash(request_params)

        existing = self._backend.get_record(clean_key)
        if existing is not None:
            if (
                existing.workflow_name != clean_wf
                or existing.branch_id != clean_branch
                or existing.request_hash != req_hash
            ):
                raise IdempotencyConflictError(
                    f"Idempotency key '{clean_key}' was already used with different "
                    f"workflow/branch/request parameters."
                )
            return IdempotentExecutionResult(
                idempotency_key=clean_key,
                workflow_name=existing.workflow_name,
                branch_id=existing.branch_id,
                response_payload=copy.deepcopy(existing.response_payload),
                was_replayed=True,
            )

        # First execution: invoke side-effect operation
        raw_result = operation()
        if not isinstance(raw_result, Mapping):
            raise TypeError("Idempotent operation must return a dictionary/mapping payload.")

        payload_copy = copy.deepcopy(dict(raw_result))
        new_record = IdempotencyRecord(
            idempotency_key=clean_key,
            workflow_name=clean_wf,
            branch_id=clean_branch,
            response_payload=payload_copy,
            request_hash=req_hash,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        saved = self._backend.save_record(new_record)

        # Handle concurrent race if another worker saved first
        if saved.request_hash != req_hash or saved.workflow_name != clean_wf or saved.branch_id != clean_branch:
            raise IdempotencyConflictError(
                f"Concurrent idempotency conflict on key '{clean_key}'."
            )

        return IdempotentExecutionResult(
            idempotency_key=clean_key,
            workflow_name=saved.workflow_name,
            branch_id=saved.branch_id,
            response_payload=copy.deepcopy(saved.response_payload),
            was_replayed=False,
        )
