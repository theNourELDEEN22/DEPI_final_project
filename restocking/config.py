from __future__ import annotations

from typing import Any

from .models import RestockRepository


DEFAULT_THRESHOLDS = {
    "expiry_days": 7,
    "high_demand_qty": 25,
    "medium_demand_qty": 12,
}


def category_thresholds(settings: dict[str, Any], category: str,
                        subcategory: str = "") -> dict[str, Any]:
    defaults = settings.get("default", DEFAULT_THRESHOLDS)
    result = {**DEFAULT_THRESHOLDS, **defaults}
    result.update(settings.get("categories", {}).get(category, {}))
    result.update(settings.get("subcategories", {}).get(subcategory, {}))
    for key in ("expiry_days", "high_demand_qty", "medium_demand_qty"):
        value = result.get(key)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"Invalid restock threshold {key!r} for {category}.")
    return result


def load_branch_settings(repository: RestockRepository, branch_id: int) -> dict[str, Any]:
    settings = repository.load_settings(branch_id)
    if not isinstance(settings.get("max_retries", 3), int) or settings.get("max_retries", 3) < 0:
        raise ValueError("restock_thresholds.max_retries must be a non-negative integer")
    return settings
