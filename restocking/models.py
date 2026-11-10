from __future__ import annotations

from typing import Any, Literal, Protocol, TypedDict


class RestockingState(TypedDict, total=False):
    run_id: str
    branch_id: int
    as_of: str
    settings: dict[str, Any]
    products: list[dict[str, Any]]
    active_offers: list[int]
    expiry_watch: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    skipped_products: list[dict[str, Any]]
    supplier_options: dict[str, list[dict[str, Any]]]
    draft: dict[str, Any]
    draft_version: int
    version_hash: str
    order_rows: list[dict[str, Any]]
    retry_count: int
    max_retries: int
    reviewer_id: str
    reviewer_role: str
    decision: Literal["APPROVE", "REJECT"]
    feedback: str
    approval_valid: bool
    status: str
    errors: list[str]


class ReviewerAuthorizer(Protocol):
    """Adapter backed by the shared authentication and authorization service."""

    def authorize_branch_reviewer(self, actor_id: str, branch_id: int) -> str:
        """Return the trusted role or raise PermissionError."""


class RestockRepository(Protocol):
    def load_inventory(self, branch_id: int, as_of: str) -> list[dict[str, Any]]: ...
    def load_active_offers(self, branch_id: int, as_of: str) -> set[int]: ...
    def load_settings(self, branch_id: int) -> dict[str, Any]: ...
    def load_suppliers(self, categories: set[str]) -> dict[str, list[dict[str, Any]]]: ...
    def publish_stock_signals(self, state: RestockingState) -> None: ...
    def save_draft(self, state: RestockingState) -> tuple[int, str, list[dict[str, Any]]]: ...
    def record_decision(self, state: RestockingState, decision: str, reason: str = "") -> None: ...
    def revalidate_draft(self, state: RestockingState) -> bool: ...
    def queue_supplier_emails(self, state: RestockingState) -> None: ...
    def escalate(self, state: RestockingState, reason: str) -> None: ...
    def record_audit(self, state: RestockingState, actor: str, action: str,
                     details: dict[str, Any] | None = None) -> None: ...
