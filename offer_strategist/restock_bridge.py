"""AI Offer Strategist - input from the Restocking graph (Epic B).

Restocking flags near-expiry stock in `expiry_watch`. Products it is NOT
reordering (their stock is still high, so it says "send it for offer review
instead") are exactly the ones an offer should clear. This module turns a
finished Restocking run into strategist requests, and serves the matching
stock-signal evidence so the Sales review card (D6) can show it.

The evidence refs use the same format Restocking publishes in its outbox signal:
"{run_id}:{product_id}:expiry".
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from offers.review import Evidence
from offer_strategist.facts import StrategistRequest


def _evidence_ref(run_id: str, product_id: Any) -> str:
    return f"{run_id}:{product_id}:expiry"


def _offer_targets(state: Mapping[str, Any]):
    """(product row, expiry item) for near-expiry products that are not being reordered."""
    products = {str(p["product_id"]): p for p in state.get("products") or []}
    reordered = {str(c["product_id"]) for c in state.get("candidates") or []}
    for item in state.get("expiry_watch") or []:
        pid = str(item["product_id"])
        yield pid, products.get(pid), item, pid in reordered


def requests_from_restock_run(
    state: Mapping[str, Any],
    forecast_ref_for: Callable[[str, str], Optional[str]],
    bundle_partners_for: Callable[[str, str], tuple[str, ...]] | None = None,
) -> tuple[list[StrategistRequest], list[dict[str, Any]]]:
    """Return (requests, skipped). `forecast_ref_for(branch_id, product_id)` looks up a
    stored forecast (E5); a product without one is skipped, never planned blind."""
    run_id = str(state["run_id"])
    branch = str(state["branch_id"])
    requests: list[StrategistRequest] = []
    skipped: list[dict[str, Any]] = []

    for pid, product, item, reordered in _offer_targets(state):
        name = item.get("product_name") or (product or {}).get("product_name", "")
        if reordered:
            skipped.append({"product_id": pid, "product_name": name, "reason": "restocking is reordering it"})
            continue
        if product is None:
            skipped.append({"product_id": pid, "product_name": name, "reason": "product row missing from the run"})
            continue
        forecast_ref = forecast_ref_for(branch, pid)
        if not forecast_ref:
            skipped.append({"product_id": pid, "product_name": name, "reason": "no stored forecast"})
            continue
        requests.append(
            StrategistRequest(
                branch_id=branch,
                product_id=pid,
                product_name=name,
                category=str(product.get("category") or ""),
                demand_level=str(product.get("demand_level") or "medium").lower(),
                stock_on_hand=int(product.get("on_hand", product.get("stock_quantity", 0))),
                expiring_units=sum(int(b.get("quantity", 0)) for b in item.get("batches") or []),
                evidence_ref=_evidence_ref(run_id, pid),
                forecast_ref=forecast_ref,
                bundle_partner_ids=tuple(bundle_partners_for(branch, pid)) if bundle_partners_for else (),
            )
        )
    return requests, skipped


class RestockEvidence:
    """EvidenceReader over a finished Restocking run (stand-in until H1 is stored)."""

    def __init__(self, state: Mapping[str, Any]) -> None:
        run_id = str(state["run_id"])
        self._data: dict[str, Evidence] = {}
        for pid, product, item, reordered in _offer_targets(state):
            if reordered or product is None:
                continue
            ref = _evidence_ref(run_id, pid)
            batches = [dict(b) for b in item.get("batches") or []]
            self._data[ref] = Evidence(
                ref=ref,
                kind="stock_signal",
                facts={
                    "signal_type": "NEAR_EXPIRY",
                    "run_id": run_id,
                    "on_hand": int(product.get("on_hand", product.get("stock_quantity", 0))),
                    "demand_level": str(product.get("demand_level") or ""),
                    "expiring_units": sum(int(b.get("quantity", 0)) for b in batches),
                    "expiring_batches": batches,
                },
            )

    def get_evidence(self, ref: str) -> Optional[Evidence]:
        return self._data.get(ref)
