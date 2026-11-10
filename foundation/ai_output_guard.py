"""A5 - Block AI output from setting recipients or branch (Epic A: Story FD-1).

Prevents prompt-injection or LLM hallucinations from routing emails/reports to
unauthorized addresses or reading/writing across supermarket branches.

Core guarantees:
1. Any recipient field (`recipient`, `to`, `email`, `cc`, `bcc`, etc.) supplied
   by AI output is stripped and ignored. The recipient is resolved strictly from
   `ConfigService.get_recipients(trusted_branch_id)` (or a DB-verified supplier
   record for approved purchase orders).
2. Any branch field (`branch_id`, `branch`, `branch_name`, `target_branch`)
   supplied by AI output is stripped and ignored. The `branch_id` is locked to
   the trusted runtime execution context.
3. Ignored AI fields are captured in `ignored_ai_fields` so the Audit Log (A11)
   can record blocked override attempts.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional

from foundation.config_service import ConfigService


FORBIDDEN_RECIPIENT_KEYS: FrozenSet[str] = frozenset(
    {
        "recipient",
        "recipients",
        "to",
        "to_email",
        "email",
        "cc",
        "bcc",
        "approver",
        "approver_email",
        "restock_approver",
        "report_recipient",
        "escalation_operator",
        "supplier_email",
    }
)

FORBIDDEN_BRANCH_KEYS: FrozenSet[str] = frozenset(
    {
        "branch_id",
        "branch",
        "branch_name",
        "target_branch",
        "target_branch_id",
        "source_branch_id",
    }
)

ALL_BLOCKED_AI_KEYS: FrozenSet[str] = FORBIDDEN_RECIPIENT_KEYS | FORBIDDEN_BRANCH_KEYS

VALID_RECIPIENT_ROLES: FrozenSet[str] = frozenset(
    {
        "restock_approver",
        "report_recipient",
        "escalation_operator",
        "supplier",
    }
)


@dataclass(frozen=True)
class SanitizedAIOutput:
    """Result of sanitizing an LLM tool call or structured output dictionary."""
    payload: Dict[str, Any]
    enforced_branch_id: int
    enforced_recipient: str
    ignored_ai_fields: Dict[str, Any] = field(default_factory=dict)

    @property
    def had_blocked_fields(self) -> bool:
        """True if the AI output attempted to set any recipient or branch field."""
        return bool(self.ignored_ai_fields)


def _strip_blocked_keys_recursive(
    data: Any,
    ignored_out: Dict[str, Any],
    prefix: str = "",
) -> Any:
    """Recursively strips forbidden recipient and branch keys from nested dicts/lists."""
    if isinstance(data, Mapping):
        cleaned: Dict[str, Any] = {}
        for raw_key, value in data.items():
            key_str = str(raw_key)
            normalized_key = key_str.strip().lower()
            path_key = f"{prefix}.{key_str}" if prefix else key_str

            if normalized_key in ALL_BLOCKED_AI_KEYS:
                ignored_out[path_key] = copy.deepcopy(value)
                continue

            cleaned[key_str] = _strip_blocked_keys_recursive(value, ignored_out, prefix=path_key)
        return cleaned

    if isinstance(data, list):
        return [
            _strip_blocked_keys_recursive(item, ignored_out, prefix=f"{prefix}[{idx}]")
            for idx, item in enumerate(data)
        ]

    return copy.deepcopy(data)


class AIOutputGuard:
    """Guardrail that blocks AI outputs from setting email recipients or branch IDs (Task A5)."""

    def __init__(self, config_service: Optional[ConfigService] = None) -> None:
        self._config = config_service if config_service is not None else ConfigService()

    def resolve_trusted_recipient(
        self,
        *,
        trusted_branch_id: int,
        recipient_role: str = "restock_approver",
        verified_supplier_email: Optional[str] = None,
    ) -> str:
        """Resolves the authoritative email recipient from ConfigService or DB supplier row."""
        if trusted_branch_id <= 0:
            raise ValueError("trusted_branch_id must be a positive integer.")

        role = recipient_role.strip().lower()
        if role not in VALID_RECIPIENT_ROLES:
            raise ValueError(
                f"Invalid recipient_role '{recipient_role}'. Must be one of {sorted(VALID_RECIPIENT_ROLES)}."
            )

        if role == "supplier":
            if not verified_supplier_email or "@" not in verified_supplier_email:
                raise ValueError(
                    "recipient_role='supplier' requires a non-empty verified_supplier_email from public.suppliers."
                )
            return verified_supplier_email.strip()

        branch_recipients = self._config.get_recipients(trusted_branch_id)
        if role == "restock_approver":
            return branch_recipients.restock_approver
        if role == "report_recipient":
            return branch_recipients.report_recipient
        return branch_recipients.escalation_operator

    def sanitize_ai_output(
        self,
        ai_output: Mapping[str, Any],
        *,
        trusted_branch_id: int,
        recipient_role: str = "restock_approver",
        verified_supplier_email: Optional[str] = None,
    ) -> SanitizedAIOutput:
        """Strips any AI-supplied recipient or branch fields and enforces trusted values.

        Args:
            ai_output: Raw dictionary or structured tool call produced by the LLM.
            trusted_branch_id: Authoritative branch ID from the runtime context.
            recipient_role: Which configured recipient role to assign
                ('restock_approver', 'report_recipient', 'escalation_operator', or 'supplier').
            verified_supplier_email: DB-verified supplier email when role='supplier'.

        Returns:
            SanitizedAIOutput with cleaned `payload`, `enforced_branch_id`,
            `enforced_recipient`, and `ignored_ai_fields`.
        """
        if not isinstance(ai_output, Mapping):
            raise TypeError("ai_output must be a mapping/dictionary.")

        enforced_recipient = self.resolve_trusted_recipient(
            trusted_branch_id=trusted_branch_id,
            recipient_role=recipient_role,
            verified_supplier_email=verified_supplier_email,
        )

        ignored: Dict[str, Any] = {}
        cleaned_payload: Dict[str, Any] = _strip_blocked_keys_recursive(ai_output, ignored)

        # Lock authoritative branch_id and recipient from trusted context & config
        cleaned_payload["branch_id"] = int(trusted_branch_id)
        cleaned_payload["recipient"] = enforced_recipient

        return SanitizedAIOutput(
            payload=cleaned_payload,
            enforced_branch_id=int(trusted_branch_id),
            enforced_recipient=enforced_recipient,
            ignored_ai_fields=ignored,
        )

    def sanitize_langgraph_state(
        self,
        state_update: Mapping[str, Any],
        *,
        trusted_branch_id: int,
        recipient_role: str = "restock_approver",
        verified_supplier_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Sanitizes a LangGraph node state update dict before merging into graph state."""
        result = self.sanitize_ai_output(
            state_update,
            trusted_branch_id=trusted_branch_id,
            recipient_role=recipient_role,
            verified_supplier_email=verified_supplier_email,
        )
        safe_state = dict(result.payload)
        safe_state["_ignored_ai_fields"] = result.ignored_ai_fields
        return safe_state
