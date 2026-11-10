"""A6 - Authentication, authorization and branch-isolation middleware (Epic A: Story FD-2).

Enforces three security layers across all Retail AI Agent workflows:
1. Authentication: Verifies actor identity (`actor_id`, `role`, `allowed_branches`,
   and active token/session status) before any workflow node or human review action executes.
2. Authorization (RBAC): Enforces role-based permissions (e.g., only authorized
   `restock_approver` or `branch_manager` actors can approve a restock order in B9/A14;
   only `sales_team` or `branch_manager` can approve an offer in D7).
3. Branch Isolation: Guarantees that a workflow run scoped to Branch A can NEVER
   read or write data belonging to Branch B (`Done when: Branch A run cannot read or write branch B`).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence


class SecurityError(PermissionError):
    """Base class for foundation security exceptions."""


class AuthenticationError(SecurityError):
    """Raised when an actor is unauthenticated, unknown, or has an invalid token."""


class AuthorizationError(SecurityError):
    """Raised when an authenticated actor lacks permission for an action."""


class BranchIsolationError(SecurityError):
    """Raised when a Branch A run attempts to read or write Branch B data."""


# ---------------------------------------------------------------------------
# Roles & Permissions
# ---------------------------------------------------------------------------
ROLE_PERMISSIONS: Dict[str, FrozenSet[str]] = {
    "agent_runtime": frozenset(
        {
            "inventory:read",
            "restock:draft",
            "report:read",
            "report:write",
            "offer:draft",
        }
    ),
    "restock_approver": frozenset(
        {
            "inventory:read",
            "restock:read",
            "restock:approve",
            "restock:reject",
        }
    ),
    "sales_team": frozenset(
        {
            "offer:read",
            "offer:approve",
            "offer:reject",
            "offer:request_changes",
        }
    ),
    "branch_manager": frozenset(
        {
            "inventory:read",
            "restock:read",
            "restock:approve",
            "restock:reject",
            "report:read",
            "offer:read",
            "offer:approve",
            "offer:reject",
        }
    ),
    "operator_admin": frozenset(
        {
            "inventory:read",
            "restock:read",
            "restock:approve",
            "restock:reject",
            "restock:escalate",
            "report:read",
            "report:write",
            "offer:read",
            "offer:approve",
            "offer:reject",
            "config:write",
        }
    ),
    "viewer": frozenset(
        {
            "inventory:read",
            "report:read",
            "offer:read",
        }
    ),
}


@dataclass(frozen=True)
class ActorPrincipal:
    """Authenticated user or system service identity."""
    actor_id: str
    role: str
    allowed_branches: FrozenSet[int]
    email: Optional[str] = None
    is_authenticated: bool = True
    auth_token: Optional[str] = None

    def can_access_branch(self, branch_id: int) -> bool:
        return int(branch_id) in self.allowed_branches


@dataclass(frozen=True)
class BranchExecutionContext:
    """Immutable execution context binding a workflow run to a single branch."""
    branch_id: int
    run_id: str
    principal: ActorPrincipal

    def __post_init__(self) -> None:
        if self.branch_id <= 0:
            raise ValueError("branch_id must be a positive integer.")
        if not self.run_id or not str(self.run_id).strip():
            raise ValueError("run_id must be a non-empty string.")


# Default registry of pilot branch actors matching `001_shared_foundation_schema.sql`
DEFAULT_ACTORS: Dict[str, ActorPrincipal] = {
    "system.scheduler.branch1": ActorPrincipal(
        actor_id="system.scheduler.branch1",
        role="agent_runtime",
        allowed_branches=frozenset({1}),
        auth_token="tok-branch-1",
    ),
    "system.scheduler.branch2": ActorPrincipal(
        actor_id="system.scheduler.branch2",
        role="agent_runtime",
        allowed_branches=frozenset({2}),
        auth_token="tok-branch-2",
    ),
    "system.scheduler.branch3": ActorPrincipal(
        actor_id="system.scheduler.branch3",
        role="agent_runtime",
        allowed_branches=frozenset({3}),
        auth_token="tok-branch-3",
    ),
    "stock.maadi@supermarket.eg": ActorPrincipal(
        actor_id="stock.maadi@supermarket.eg",
        role="restock_approver",
        allowed_branches=frozenset({1}),
        email="stock.maadi@supermarket.eg",
        auth_token="tok-stock-maadi",
    ),
    "stock.nasrcity@supermarket.eg": ActorPrincipal(
        actor_id="stock.nasrcity@supermarket.eg",
        role="restock_approver",
        allowed_branches=frozenset({2}),
        email="stock.nasrcity@supermarket.eg",
        auth_token="tok-stock-nasr",
    ),
    "stock.october@supermarket.eg": ActorPrincipal(
        actor_id="stock.october@supermarket.eg",
        role="restock_approver",
        allowed_branches=frozenset({3}),
        email="stock.october@supermarket.eg",
        auth_token="tok-stock-oct",
    ),
    "manager.maadi@supermarket.eg": ActorPrincipal(
        actor_id="manager.maadi@supermarket.eg",
        role="branch_manager",
        allowed_branches=frozenset({1}),
        email="manager.maadi@supermarket.eg",
        auth_token="tok-mgr-maadi",
    ),
    "sales.maadi@supermarket.eg": ActorPrincipal(
        actor_id="sales.maadi@supermarket.eg",
        role="sales_team",
        allowed_branches=frozenset({1}),
        email="sales.maadi@supermarket.eg",
        auth_token="tok-sales-maadi",
    ),
}


class AuthAndBranchIsolationMiddleware:
    """Middleware enforcing authentication, RBAC authorization, and strict branch isolation (Task A6)."""

    def __init__(
        self,
        actors: Optional[Mapping[str, ActorPrincipal]] = None,
        role_permissions: Optional[Mapping[str, FrozenSet[str]]] = None,
    ) -> None:
        self._actors: Dict[str, ActorPrincipal] = (
            dict(actors) if actors is not None else copy.deepcopy(DEFAULT_ACTORS)
        )
        self._role_permissions: Dict[str, FrozenSet[str]] = (
            dict(role_permissions) if role_permissions is not None else copy.deepcopy(ROLE_PERMISSIONS)
        )

    def register_actor(self, principal: ActorPrincipal) -> None:
        """Registers or updates an actor principal in the middleware directory."""
        self._actors[principal.actor_id] = principal

    # -------------------------------------------------------------------------
    # 1. Authentication
    # -------------------------------------------------------------------------
    def authenticate(
        self,
        actor_id: str,
        *,
        auth_token: Optional[str] = None,
        require_token: bool = False,
    ) -> ActorPrincipal:
        """Authenticates an actor by ID and optional token."""
        if not actor_id or not str(actor_id).strip():
            raise AuthenticationError("Actor ID is required for authentication.")

        principal = self._actors.get(str(actor_id).strip())
        if principal is None or not principal.is_authenticated:
            raise AuthenticationError(f"Unauthenticated or unknown actor: '{actor_id}'.")

        if require_token or auth_token is not None:
            if not auth_token or auth_token != principal.auth_token:
                raise AuthenticationError(f"Invalid authentication token for actor '{actor_id}'.")

        return principal

    # -------------------------------------------------------------------------
    # 2. Authorization & Context Creation
    # -------------------------------------------------------------------------
    def authorize(
        self,
        principal: ActorPrincipal,
        permission: str,
        *,
        target_branch_id: int,
    ) -> None:
        """Verifies that the authenticated principal has `permission` on `target_branch_id`."""
        if not principal or not principal.is_authenticated:
            raise AuthenticationError("Principal is not authenticated.")

        role_key = principal.role.strip().lower()
        allowed_perms = self._role_permissions.get(role_key, frozenset())
        if permission not in allowed_perms:
            raise AuthorizationError(
                f"Actor '{principal.actor_id}' with role '{principal.role}' lacks permission '{permission}'."
            )

        if not principal.can_access_branch(target_branch_id):
            raise BranchIsolationError(
                f"Branch isolation violation: Actor '{principal.actor_id}' (branches={sorted(principal.allowed_branches)}) "
                f"cannot access branch_id={target_branch_id}."
            )

    def create_branch_context(
        self,
        *,
        actor_id: str,
        branch_id: int,
        run_id: str,
        required_permission: Optional[str] = None,
        auth_token: Optional[str] = None,
    ) -> BranchExecutionContext:
        """Authenticates actor, verifies branch access, and creates a locked BranchExecutionContext."""
        principal = self.authenticate(actor_id, auth_token=auth_token)
        if not principal.can_access_branch(branch_id):
            raise BranchIsolationError(
                f"Actor '{principal.actor_id}' is not authorized to start a run for branch_id={branch_id}."
            )
        if required_permission:
            self.authorize(principal, required_permission, target_branch_id=branch_id)

        return BranchExecutionContext(
            branch_id=int(branch_id),
            run_id=str(run_id),
            principal=principal,
        )

    # -------------------------------------------------------------------------
    # 3. Branch-Isolation Read & Write Guards (Task A6 Core Requirement)
    # -------------------------------------------------------------------------
    def guard_read_query(
        self,
        context: BranchExecutionContext,
        *,
        requested_branch_id: Optional[int] = None,
        filters: Optional[Mapping[str, Any]] = None,
        permission: str = "inventory:read",
    ) -> Dict[str, Any]:
        """Ensures a read operation is strictly scoped to `context.branch_id`.

        Raises `BranchIsolationError` if the caller or query filter attempts to read
        another branch. Returns a safe filter dictionary with `branch_id` locked.
        """
        self.authorize(context.principal, permission, target_branch_id=context.branch_id)

        if requested_branch_id is not None and int(requested_branch_id) != context.branch_id:
            raise BranchIsolationError(
                f"Branch {context.branch_id} run cannot read branch {requested_branch_id}."
            )

        safe_filters: Dict[str, Any] = dict(filters) if filters else {}
        if "branch_id" in safe_filters and int(safe_filters["branch_id"]) != context.branch_id:
            raise BranchIsolationError(
                f"Branch {context.branch_id} run cannot read branch {safe_filters['branch_id']} via filter."
            )

        safe_filters["branch_id"] = context.branch_id
        return safe_filters

    def guard_read_rows(
        self,
        context: BranchExecutionContext,
        rows: Sequence[Mapping[str, Any]],
        *,
        permission: str = "inventory:read",
    ) -> List[Dict[str, Any]]:
        """Verifies that every returned database row belongs to `context.branch_id`."""
        self.authorize(context.principal, permission, target_branch_id=context.branch_id)

        verified: List[Dict[str, Any]] = []
        for idx, row in enumerate(rows):
            row_branch = row.get("branch_id")
            if row_branch is None or int(row_branch) != context.branch_id:
                raise BranchIsolationError(
                    f"Cross-branch read leak blocked at row {idx}: Branch {context.branch_id} run "
                    f"received row belonging to branch_id={row_branch}."
                )
            verified.append(copy.deepcopy(dict(row)))
        return verified

    def guard_write_payload(
        self,
        context: BranchExecutionContext,
        payload: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        permission: str = "restock:draft",
    ) -> Dict[str, Any] | List[Dict[str, Any]]:
        """Ensures a write operation (insert/update) only writes to `context.branch_id`.

        Raises `BranchIsolationError` if any record specifies a `branch_id` different
        from `context.branch_id`.
        """
        self.authorize(context.principal, permission, target_branch_id=context.branch_id)

        def _validate_single(record: Mapping[str, Any]) -> Dict[str, Any]:
            if not isinstance(record, Mapping):
                raise TypeError("Write record must be a mapping/dictionary.")
            if "branch_id" in record and record["branch_id"] is not None:
                target_branch = int(record["branch_id"])
                if target_branch != context.branch_id:
                    raise BranchIsolationError(
                        f"Branch {context.branch_id} run cannot write branch {target_branch}."
                    )
            clean_rec = copy.deepcopy(dict(record))
            clean_rec["branch_id"] = context.branch_id
            return clean_rec

        if isinstance(payload, Mapping):
            return _validate_single(payload)

        return [_validate_single(item) for item in payload]


class BranchScopedRepository:
    """In-memory or adapter-backed table repository enforcing strict Branch Isolation."""

    def __init__(
        self,
        middleware: AuthAndBranchIsolationMiddleware,
        initial_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    ) -> None:
        self._middleware = middleware
        self._rows: List[Dict[str, Any]] = [copy.deepcopy(dict(r)) for r in (initial_rows or [])]

    def read_all(
        self,
        context: BranchExecutionContext,
        *,
        requested_branch_id: Optional[int] = None,
        permission: str = "inventory:read",
    ) -> List[Dict[str, Any]]:
        """Reads rows for `context.branch_id`; blocks any cross-branch read attempt."""
        safe_filters = self._middleware.guard_read_query(
            context,
            requested_branch_id=requested_branch_id,
            permission=permission,
        )
        target_branch = safe_filters["branch_id"]
        matching = [r for r in self._rows if int(r.get("branch_id", -1)) == target_branch]
        return self._middleware.guard_read_rows(context, matching, permission=permission)

    def write_row(
        self,
        context: BranchExecutionContext,
        row: Mapping[str, Any],
        *,
        permission: str = "restock:draft",
    ) -> Dict[str, Any]:
        """Inserts a row for `context.branch_id`; blocks any cross-branch write attempt."""
        safe_row = self._middleware.guard_write_payload(context, row, permission=permission)
        assert isinstance(safe_row, dict)
        self._rows.append(safe_row)
        return copy.deepcopy(safe_row)

    def update_rows(
        self,
        context: BranchExecutionContext,
        *,
        target_branch_id: int,
        match_fn: Callable[[Mapping[str, Any]], bool],
        updates: Mapping[str, Any],
        permission: str = "restock:draft",
    ) -> int:
        """Updates matching rows in `context.branch_id`; blocks cross-branch updates."""
        if int(target_branch_id) != context.branch_id:
            raise BranchIsolationError(
                f"Branch {context.branch_id} run cannot update rows in branch {target_branch_id}."
            )
        safe_updates = self._middleware.guard_write_payload(context, updates, permission=permission)
        assert isinstance(safe_updates, dict)

        updated_count = 0
        for row in self._rows:
            if int(row.get("branch_id", -1)) == context.branch_id and match_fn(row):
                row.update(safe_updates)
                updated_count += 1
        return updated_count
