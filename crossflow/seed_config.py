"""Read config values straight from the Foundation migration seed.

Cross-workflow tests must use the SAME thresholds as the database, not a copy
that drifts. This reads the JSON seeded into config_settings by
foundation/db/001_shared_foundation_schema.sql. If the migration changes shape,
the tests that use this fail loudly instead of silently testing stale numbers.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

MIGRATION = Path(__file__).resolve().parent.parent / "foundation" / "db" / "001_shared_foundation_schema.sql"


@lru_cache(maxsize=None)
def load_seed_config(key: str, branch_id: int = 0) -> dict[str, Any]:
    text = MIGRATION.read_text(encoding="utf-8")
    pattern = rf"\(\s*'{re.escape(key)}'\s*,\s*{int(branch_id)}\s*,\s*'(.*?)'::jsonb\s*\)"
    match = re.search(pattern, text, flags=re.DOTALL)
    if match is None:
        raise KeyError(f"config_settings seed has no ({key!r}, {branch_id}) row in {MIGRATION.name}")
    return json.loads(match.group(1))


def restock_settings() -> dict[str, Any]:
    """The global restock_thresholds row, in the shape restocking.config expects."""
    return load_seed_config("restock_thresholds", 0)
