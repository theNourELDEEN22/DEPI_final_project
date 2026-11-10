"""Offline stand-in for the model, for tests and for the demo without an API key."""
from __future__ import annotations

import threading
from typing import Any, Optional

from offers.schema import OfferType
from offer_strategist.llm import OfferProposal, StrategyBrief, Urgency


def make_proposal(
    type: OfferType | str = OfferType.DISCOUNT,
    *,
    discount: Optional[float] = None,
    partner: Optional[str] = None,
    bundle_price: Optional[float] = None,
    start_in_hours: int = 12,
    duration_days: int = 7,
    quantity_cap: int = 50,
    rationale: str = "scripted proposal",
) -> OfferProposal:
    return OfferProposal(
        type=OfferType(type),
        discount_percent=discount,
        bundle_partner_id=partner,
        bundle_price=bundle_price,
        start_in_hours=start_in_hours,
        duration_days=duration_days,
        quantity_cap=quantity_cap,
        rationale=rationale,
    )


class ScriptedLLM:
    """Returns scripted answers per product id, in order. An Exception in the
    script is raised instead of returned. Safe to use from parallel branches."""

    def __init__(
        self,
        designs: dict[str, list[Any]],
        briefs: dict[str, Any] | None = None,
    ) -> None:
        self._designs = {pid: list(answers) for pid, answers in designs.items()}
        self._briefs = dict(briefs or {})
        self._lock = threading.Lock()
        self.analyze_calls: list[str] = []
        self.design_calls: list[tuple[str, list[dict[str, Any]]]] = []

    def analyze(self, facts: dict[str, Any]) -> StrategyBrief:
        pid = facts["product_id"]
        with self._lock:
            self.analyze_calls.append(pid)
            brief = self._briefs.get(pid)
        if isinstance(brief, Exception):
            raise brief
        return brief or StrategyBrief(
            urgency=Urgency.MEDIUM,
            recommended_mechanic=OfferType.DISCOUNT,
            target_units=int(facts.get("expiring_units") or facts["stock_on_hand"]),
            rationale="scripted brief",
        )

    def design(
        self, facts: dict[str, Any], brief: dict[str, Any], attempts: list[dict[str, Any]]
    ) -> OfferProposal:
        pid = facts["product_id"]
        with self._lock:
            self.design_calls.append((pid, [dict(a) for a in attempts]))
            queue = self._designs.get(pid) or []
            if not queue:
                raise RuntimeError(f"no scripted design left for product {pid}")
            answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
