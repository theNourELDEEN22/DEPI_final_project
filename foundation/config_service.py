"""A4 - Config Service: schedules, thresholds, recipients, timezone, guardrail limits (Epic A).

Provides dynamic configuration lookup backed by Supabase (`public.config_settings`)
or an in-memory store for deterministic unit testing and CI runs.

Key capabilities:
- Resolves branch-specific overrides (`branch_id = N`) with automatic fallback to
  global defaults (`branch_id = 0`).
- Resolves category and subcategory restock thresholds (e.g., Dairy, Bakery,
  Frozen Food / Frozen Chicken) and enforces that Low demand never triggers on
  quantity alone.
- Exposes typed dataclasses for Schedules, Restock Thresholds, Authorized Recipients,
  and Offer Guardrail Limits (using `Decimal` for financial accuracy).
- Guarantees that updating a threshold immediately changes the very next run.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional, Protocol


class ConfigBackend(Protocol):
    """Protocol for reading and writing rows in `public.config_settings`."""

    def get_row(self, config_key: str, branch_id: int) -> Optional[Dict[str, Any]]:
        ...

    def upsert_row(
        self,
        config_key: str,
        branch_id: int,
        config_value: Dict[str, Any],
        updated_by: str = "system",
    ) -> None:
        ...


@dataclass(frozen=True)
class ScheduleConfig:
    """Cron schedules and canonical timezone (FD-1)."""
    restock_cron: str = "0 0 * * *"
    daily_report_cron: str = "0 1 * * *"
    timezone: str = "Africa/Cairo"


@dataclass(frozen=True)
class CategoryThreshold:
    """Restock thresholds for a specific product category or subcategory."""
    expiry_days: int
    high_demand_qty: int
    medium_demand_qty: int
    shelf_life_days: Optional[int] = None


@dataclass(frozen=True)
class RestockEvaluation:
    """Result of evaluating a product against the active config thresholds."""
    should_restock: bool
    trigger_reason: Optional[str]
    applied_threshold: CategoryThreshold


@dataclass(frozen=True)
class RecipientConfig:
    """Authorized email recipients for a specific branch (FD-1, A5)."""
    branch_id: int
    restock_approver: str
    report_recipient: str
    escalation_operator: str = "ops.lead@supermarket.eg"


@dataclass(frozen=True)
class OfferGuardrailConfig:
    """Guardrail limits for Epic D (Offers), compatible with M4's GuardrailConfig."""
    max_discount_percent: Decimal = Decimal("40.0")
    min_margin_percent: Decimal = Decimal("5.0")

    @property
    def max_discount_pct(self) -> Decimal:
        return self.max_discount_percent

    @property
    def min_margin_pct(self) -> Decimal:
        return self.min_margin_percent


# Default seed matching `001_shared_foundation_schema.sql`
DEFAULT_CONFIG_SEED: Dict[tuple[str, int], Dict[str, Any]] = {
    ("restock_thresholds", 0): {
        "max_retries": 3,
        "default": {
            "expiry_days": 3,
            "high_demand_qty": 15,
            "medium_demand_qty": 8,
        },
        "categories": {
            "Bakery":          {"expiry_days": 2,  "high_demand_qty": 60, "medium_demand_qty": 35, "shelf_life_days": 5},
            "Dairy":           {"expiry_days": 3,  "high_demand_qty": 50, "medium_demand_qty": 30, "shelf_life_days": 14},
            "Frozen Food":     {"expiry_days": 14, "high_demand_qty": 50, "medium_demand_qty": 30, "shelf_life_days": 270},
            "Beverages":       {"expiry_days": 14, "high_demand_qty": 80, "medium_demand_qty": 45, "shelf_life_days": 300},
            "Snacks":          {"expiry_days": 10, "high_demand_qty": 40, "medium_demand_qty": 25, "shelf_life_days": 180},
            "Staples":         {"expiry_days": 15, "high_demand_qty": 60, "medium_demand_qty": 35, "shelf_life_days": 540},
            "Cleaning":        {"expiry_days": 14, "high_demand_qty": 25, "medium_demand_qty": 15, "shelf_life_days": 720},
            "Home":            {"expiry_days": 14, "high_demand_qty": 25, "medium_demand_qty": 15, "shelf_life_days": 720},
            "Personal Care":   {"expiry_days": 15, "high_demand_qty": 20, "medium_demand_qty": 12, "shelf_life_days": 720},
            "School Supplies": {"expiry_days": 21, "high_demand_qty": 15, "medium_demand_qty": 8,  "shelf_life_days": 1095},
        },
        "subcategories": {
            "Frozen Chicken":  {"expiry_days": 10, "high_demand_qty": 50, "medium_demand_qty": 30, "unit": "kg_or_pack"},
            "Milk":            {"expiry_days": 3,  "high_demand_qty": 60, "medium_demand_qty": 35, "unit": "pack"},
            "Bread":           {"expiry_days": 2,  "high_demand_qty": 70, "medium_demand_qty": 40, "unit": "pack"},
        },
    },
    ("schedules", 0): {
        "restock_cron": "0 0 * * *",
        "daily_report_cron": "0 1 * * *",
        "timezone": "Africa/Cairo",
    },
    ("offer_guardrails", 0): {
        "max_discount_pct": 40.0,
        "min_margin_pct": 5.0,
    },
    ("authorized_recipients", 1): {
        "restock_approver": "stock.maadi@supermarket.eg",
        "report_recipient": "manager.maadi@supermarket.eg",
        "escalation_operator": "ops.maadi@supermarket.eg",
    },
    ("authorized_recipients", 2): {
        "restock_approver": "stock.nasrcity@supermarket.eg",
        "report_recipient": "manager.nasrcity@supermarket.eg",
        "escalation_operator": "ops.nasrcity@supermarket.eg",
    },
    ("authorized_recipients", 3): {
        "restock_approver": "stock.october@supermarket.eg",
        "report_recipient": "manager.october@supermarket.eg",
        "escalation_operator": "ops.october@supermarket.eg",
    },
}


class InMemoryConfigBackend:
    """In-memory backend pre-loaded with the Supabase default seed for fast tests."""

    def __init__(self, initial_data: Optional[Dict[tuple[str, int], Dict[str, Any]]] = None) -> None:
        source = initial_data if initial_data is not None else DEFAULT_CONFIG_SEED
        self._store: Dict[tuple[str, int], Dict[str, Any]] = copy.deepcopy(source)

    def get_row(self, config_key: str, branch_id: int) -> Optional[Dict[str, Any]]:
        val = self._store.get((config_key, branch_id))
        return copy.deepcopy(val) if val is not None else None

    def upsert_row(
        self,
        config_key: str,
        branch_id: int,
        config_value: Dict[str, Any],
        updated_by: str = "system",
    ) -> None:
        self._store[(config_key, branch_id)] = copy.deepcopy(config_value)


class SupabaseConfigBackend:
    """Live Supabase backend querying `public.config_settings`."""

    def __init__(self, supabase_client: Any) -> None:
        self._client = supabase_client

    @classmethod
    def from_env(cls) -> "SupabaseConfigBackend":
        from supabase import create_client
        url = os.environ["SUPABASE_URL"]
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_KEY"]
        return cls(create_client(url, key))

    def get_row(self, config_key: str, branch_id: int) -> Optional[Dict[str, Any]]:
        response = (
            self._client.table("config_settings")
            .select("config_value")
            .eq("config_key", config_key)
            .eq("branch_id", branch_id)
            .limit(1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        if not rows:
            return None
        return rows[0].get("config_value")

    def upsert_row(
        self,
        config_key: str,
        branch_id: int,
        config_value: Dict[str, Any],
        updated_by: str = "system",
    ) -> None:
        self._client.table("config_settings").upsert(
            {
                "config_key": config_key,
                "branch_id": branch_id,
                "config_value": config_value,
                "updated_by": updated_by,
            },
            on_conflict="config_key,branch_id",
        ).execute()


class ConfigService:
    """Centralized configuration service for Epic A (Task A4).

    Reads directly from the configured backend on every call so that changing a
    threshold or guardrail limit immediately affects the next workflow run.
    """

    def __init__(self, backend: Optional[ConfigBackend] = None) -> None:
        self._backend: ConfigBackend = backend if backend is not None else InMemoryConfigBackend()

    def _resolve_config(self, config_key: str, branch_id: int = 0) -> Dict[str, Any]:
        """Resolves branch-specific config (`branch_id > 0`), falling back to global (`0`)."""
        if branch_id != 0:
            branch_val = self._backend.get_row(config_key, branch_id)
            if branch_val is not None:
                return branch_val
        global_val = self._backend.get_row(config_key, 0)
        if global_val is not None:
            return global_val
        raise KeyError(f"Configuration '{config_key}' not found for branch_id={branch_id} or global(0).")

    # -------------------------------------------------------------------------
    # 1. Schedules & Timezone
    # -------------------------------------------------------------------------
    def get_schedules(self, branch_id: int = 0) -> ScheduleConfig:
        raw = self._resolve_config("schedules", branch_id)
        return ScheduleConfig(
            restock_cron=str(raw.get("restock_cron", "0 0 * * *")),
            daily_report_cron=str(raw.get("daily_report_cron", "0 1 * * *")),
            timezone=str(raw.get("timezone", "Africa/Cairo")),
        )

    def get_timezone(self, branch_id: int = 0) -> str:
        return self.get_schedules(branch_id).timezone

    # -------------------------------------------------------------------------
    # 2. Restock Thresholds & Retry Limits (Category & Subcategory Aware)
    # -------------------------------------------------------------------------
    def get_max_retries(self, branch_id: int = 0) -> int:
        raw = self._resolve_config("restock_thresholds", branch_id)
        return int(raw.get("max_retries", 3))

    def get_restock_threshold(
        self,
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        branch_id: int = 0,
    ) -> CategoryThreshold:
        """Returns the threshold for a subcategory -> category -> global default."""
        raw = self._resolve_config("restock_thresholds", branch_id)
        default_block = raw.get("default", {"expiry_days": 3, "high_demand_qty": 15, "medium_demand_qty": 8})
        categories_block = raw.get("categories", {})
        subcategories_block = raw.get("subcategories", {})

        chosen = dict(default_block)
        if category and category in categories_block:
            chosen.update(categories_block[category])
        if subcategory and subcategory in subcategories_block:
            chosen.update(subcategories_block[subcategory])

        return CategoryThreshold(
            expiry_days=int(chosen["expiry_days"]),
            high_demand_qty=int(chosen["high_demand_qty"]),
            medium_demand_qty=int(chosen["medium_demand_qty"]),
            shelf_life_days=int(chosen["shelf_life_days"]) if "shelf_life_days" in chosen and chosen["shelf_life_days"] is not None else None,
        )

    def evaluate_restock_need(
        self,
        *,
        stock_quantity: int,
        days_until_expiry: int,
        demand_level: str,
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        branch_id: int = 0,
    ) -> RestockEvaluation:
        """Evaluates whether a product batch requires restocking on the current run.

        Rules:
        1. If `days_until_expiry <= threshold.expiry_days` -> trigger `NEAR_EXPIRY`.
        2. If `demand_level == 'High'` and `stock_quantity <= threshold.high_demand_qty`
           -> trigger `HIGH_DEMAND_LOW_STOCK`.
        3. If `demand_level == 'Medium'` and `stock_quantity <= threshold.medium_demand_qty`
           -> trigger `MEDIUM_DEMAND_LOW_STOCK`.
        4. `Low` demand NEVER triggers on quantity alone (RS-3 / B6).
        """
        threshold = self.get_restock_threshold(category=category, subcategory=subcategory, branch_id=branch_id)
        normalized_demand = demand_level.strip().capitalize()

        if days_until_expiry <= threshold.expiry_days:
            return RestockEvaluation(True, "NEAR_EXPIRY", threshold)

        if normalized_demand == "High" and stock_quantity <= threshold.high_demand_qty:
            return RestockEvaluation(True, "HIGH_DEMAND_LOW_STOCK", threshold)

        if normalized_demand == "Medium" and stock_quantity <= threshold.medium_demand_qty:
            return RestockEvaluation(True, "MEDIUM_DEMAND_LOW_STOCK", threshold)

        return RestockEvaluation(False, None, threshold)

    def update_restock_threshold(
        self,
        *,
        expiry_days: Optional[int] = None,
        high_demand_qty: Optional[int] = None,
        medium_demand_qty: Optional[int] = None,
        max_retries: Optional[int] = None,
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        branch_id: int = 0,
        updated_by: str = "admin",
    ) -> CategoryThreshold:
        """Updates a threshold in the backend so the very next run uses the new values."""
        current = copy.deepcopy(self._resolve_config("restock_thresholds", branch_id))
        if max_retries is not None:
            if max_retries < 0:
                raise ValueError("max_retries must be non-negative.")
            current["max_retries"] = int(max_retries)

        if subcategory:
            target = current.setdefault("subcategories", {}).setdefault(
                subcategory, copy.deepcopy(current.get("default", {"expiry_days": 3, "high_demand_qty": 15, "medium_demand_qty": 8}))
            )
        elif category:
            target = current.setdefault("categories", {}).setdefault(
                category, copy.deepcopy(current.get("default", {"expiry_days": 3, "high_demand_qty": 15, "medium_demand_qty": 8}))
            )
        else:
            target = current.setdefault(
                "default", {"expiry_days": 3, "high_demand_qty": 15, "medium_demand_qty": 8}
            )

        if expiry_days is not None:
            if expiry_days < 0:
                raise ValueError("expiry_days must be non-negative.")
            target["expiry_days"] = int(expiry_days)
        if high_demand_qty is not None:
            if high_demand_qty <= 0:
                raise ValueError("high_demand_qty must be a positive integer.")
            target["high_demand_qty"] = int(high_demand_qty)
        if medium_demand_qty is not None:
            if medium_demand_qty <= 0:
                raise ValueError("medium_demand_qty must be a positive integer.")
            target["medium_demand_qty"] = int(medium_demand_qty)

        self._backend.upsert_row("restock_thresholds", branch_id, current, updated_by=updated_by)
        return self.get_restock_threshold(category=category, subcategory=subcategory, branch_id=branch_id)

    # -------------------------------------------------------------------------
    # 3. Authorized Recipients (Branch-Scoped)
    # -------------------------------------------------------------------------
    def get_recipients(self, branch_id: int) -> RecipientConfig:
        if branch_id <= 0:
            raise ValueError("branch_id must be a positive branch identifier (e.g., 1, 2, or 3).")
        raw = self._resolve_config("authorized_recipients", branch_id)
        return RecipientConfig(
            branch_id=branch_id,
            restock_approver=str(raw["restock_approver"]),
            report_recipient=str(raw["report_recipient"]),
            escalation_operator=str(raw.get("escalation_operator", "ops.lead@supermarket.eg")),
        )

    # -------------------------------------------------------------------------
    # 4. Offer Guardrail Limits (Epic D)
    # -------------------------------------------------------------------------
    def get_offer_guardrails(self, branch_id: int = 0) -> OfferGuardrailConfig:
        raw = self._resolve_config("offer_guardrails", branch_id)
        max_disc = raw.get("max_discount_pct", raw.get("max_discount_percent", 40.0))
        min_marg = raw.get("min_margin_pct", raw.get("min_margin_percent", 5.0))
        return OfferGuardrailConfig(
            max_discount_percent=Decimal(str(max_disc)),
            min_margin_percent=Decimal(str(min_marg)),
        )

    def update_offer_guardrails(
        self,
        *,
        max_discount_pct: Optional[Decimal | float | str] = None,
        min_margin_pct: Optional[Decimal | float | str] = None,
        branch_id: int = 0,
        updated_by: str = "admin",
    ) -> OfferGuardrailConfig:
        current = copy.deepcopy(self._resolve_config("offer_guardrails", branch_id))
        if max_discount_pct is not None:
            val = Decimal(str(max_discount_pct))
            if val <= Decimal("0") or val >= Decimal("100"):
                raise ValueError("max_discount_pct must be between 0 and 100.")
            current["max_discount_pct"] = float(val)
        if min_margin_pct is not None:
            val = Decimal(str(min_margin_pct))
            if val < Decimal("0") or val >= Decimal("100"):
                raise ValueError("min_margin_pct must be between 0 and 100.")
            current["min_margin_pct"] = float(val)

        self._backend.upsert_row("offer_guardrails", branch_id, current, updated_by=updated_by)
        return self.get_offer_guardrails(branch_id=branch_id)
