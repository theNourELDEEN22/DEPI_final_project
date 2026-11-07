from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .config import category_thresholds, load_branch_settings
from .models import RestockRepository, RestockingState, ReviewerAuthorizer


BUSINESS_TZ = ZoneInfo("Africa/Cairo")


def _stable_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_inventory(state: RestockingState, repository: RestockRepository) -> dict[str, Any]:
    as_of = state["as_of"]
    settings = load_branch_settings(repository, state["branch_id"])
    products = repository.load_inventory(state["branch_id"], as_of)
    if not products:
        raise ValueError(f"No products were found for branch {state['branch_id']}.")
    offers = repository.load_active_offers(state["branch_id"], as_of)
    return {"settings": settings, "products": products,
            "active_offers": sorted(offers),
            "max_retries": int(settings.get("max_retries", 3))}


def _evaluate_inventory(state: RestockingState) -> dict[str, Any]:
    as_of = date.fromisoformat(state["as_of"])
    candidates: list[dict[str, Any]] = []
    expiry_watch: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for product in state["products"]:
        product_id = int(product["product_id"])
        category = product["category"]
        rules = category_thresholds(
            state["settings"], category, product.get("subcategory") or ""
        )
        expiry_days = int(rules.get("expiry_days", 3))
        expiring = []
        for batch in product.get("batches", []):
            quantity = int(batch["stock_quantity"])
            expires_on = date.fromisoformat(batch["expiration_date"])
            days_left = (expires_on - as_of).days
            if quantity > 0 and 0 <= days_left <= expiry_days:
                expiring.append({
                    "batch_id": batch["batch_id"],
                    "expires_on": expires_on.isoformat(),
                    "days_left": days_left,
                    "quantity": quantity,
                })
        if expiring:
            expiry_watch.append({
                "product_id": product_id,
                "product_name": product["product_name"],
                "batches": expiring,
            })

        demand_level = str(product.get("demand_level", "Low")).strip().lower()
        stock = int(product["on_hand"])
        reason = None
        trigger_qty = None
        demand_threshold = None
        if demand_level == "high":
            demand_threshold = int(rules.get("high_demand_qty", 25))
        elif demand_level == "medium":
            demand_threshold = int(rules.get("medium_demand_qty", 12))

        if expiring and demand_threshold is not None and stock > demand_threshold:
            skipped.append({
                "product_id": product_id,
                "product_name": product["product_name"],
                "reason": "Near-expiry stock is above its restock threshold; send it for offer review instead.",
            })
            continue
        if expiring:
            reason = "NEAR_EXPIRY"
            trigger_qty = demand_threshold or DEFAULT_STOCK_TARGET
        elif demand_level == "high" and stock <= demand_threshold:
            reason = "HIGH_DEMAND_LOW_STOCK"
        elif demand_level == "medium" and stock <= demand_threshold:
            reason = "MEDIUM_DEMAND_LOW_STOCK"

        if reason is None:
            continue
        trigger_qty = trigger_qty or demand_threshold or DEFAULT_STOCK_TARGET
        if product_id in state["active_offers"]:
            skipped.append({
                "product_id": product_id,
                "product_name": product["product_name"],
                "reason": "An active offer is already clearing this product.",
            })
            continue

        candidates.append({
            "product_id": product_id,
            "sku": product["sku"],
            "product_name": product["product_name"],
            "category": category,
            "subcategory": product.get("subcategory") or "",
            "demand_level": demand_level.upper(),
            "on_hand": stock,
            "requested_quantity": max(1, trigger_qty - stock),
            "trigger_reason": reason,
            "unit": product.get("unit") or "piece",
            "expiring_batches": expiring,
            "preferred_supplier_id": int(product["supplier_id"]),
        })

    return {"candidates": candidates, "expiry_watch": expiry_watch,
            "skipped_products": skipped}


DEFAULT_STOCK_TARGET = 25


def _publish_signals(state: RestockingState, repository: RestockRepository) -> dict[str, Any]:
    repository.publish_stock_signals(state)
    return {}


def _load_suppliers(state: RestockingState, repository: RestockRepository) -> dict[str, Any]:
    categories = {line["category"] for line in state["candidates"]}
    options_by_category = repository.load_suppliers(categories)
    lines = []
    for original in state["candidates"]:
        options = options_by_category.get(original["category"], [])
        if not options:
            continue
        options = [dict(option) for option in options]
        chosen = next(
            (option for option in options
             if int(option["supplier_id"]) == original["preferred_supplier_id"]),
            options[0],
        )
        line = {
            **original,
            "supplier_id": int(chosen["supplier_id"]),
            "supplier_name": chosen["supplier_name"],
            "lead_time_days": int(chosen["shipping_duration_days"]),
            "requested_quantity": max(
                int(original["requested_quantity"]),
                int(chosen["minimum_order_quantity"]),
            ),
            "supplier_options": options,
        }
        lines.append(line)
    return {"candidates": lines, "supplier_options": options_by_category,
            "unsourced_products": [line for line in state["candidates"]
                                   if line["category"] not in options_by_category]}


def _draft_report(state: RestockingState, repository: RestockRepository) -> dict[str, Any]:
    if not state["candidates"]:
        return {"status": "no_action", "draft": {"lines": [],
            "expiry_watch": state["expiry_watch"],
            "skipped_products": state["skipped_products"]}}
    if state.get("unsourced_products"):
        missing = ", ".join(item["product_name"] for item in state["unsourced_products"])
        return {"status": "sourcing_exception",
                "feedback": f"No eligible category supplier for: {missing}."}
    for line in state["candidates"]:
        quantity = line["requested_quantity"]
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValueError(f"Invalid restock quantity for {line['product_name']}.")

    draft = {
        "run_id": state["run_id"],
        "branch_id": state["branch_id"],
        "as_of": state["as_of"],
        "lines": state["candidates"],
        "expiry_watch": state["expiry_watch"],
        "skipped_products": state["skipped_products"],
        "reviewer_feedback": state.get("feedback", ""),
    }
    version_hash = _stable_hash(draft)
    draft["version_hash"] = version_hash
    saved_state = {**state, "draft": draft, "version_hash": version_hash}
    version, version_hash, order_rows = repository.save_draft(saved_state)
    return {"draft": draft, "draft_version": version,
            "version_hash": version_hash, "order_rows": order_rows,
            "status": "waiting_approval"}


def _human_review(state: RestockingState, authorizer: ReviewerAuthorizer) -> dict[str, Any]:
    response = interrupt({
        "run_id": state["run_id"],
        "draft_version": state["draft_version"],
        "version_hash": state["version_hash"],
        "draft": state["draft"],
        "allowed_decisions": ["APPROVE", "REJECT"],
    })
    if not isinstance(response, dict):
        raise ValueError("Review response must be a structured object.")
    decision = str(response.get("decision", "")).upper()
    actor_id = str(response.get("actor_id", "")).strip()
    reason = str(response.get("reason", "")).strip()
    if decision not in {"APPROVE", "REJECT"}:
        raise ValueError("Decision must be APPROVE or REJECT.")
    if not actor_id:
        raise ValueError("Authenticated reviewer identity is required.")
    if decision == "REJECT" and not reason:
        raise ValueError("A reason is required when rejecting a draft.")
    role = authorizer.authorize_branch_reviewer(actor_id, state["branch_id"])
    return {"decision": decision, "reviewer_id": actor_id,
            "reviewer_role": role, "feedback": reason}


def _route_after_draft(state: RestockingState) -> str:
    if state["status"] == "no_action":
        return "end"
    if state["status"] == "sourcing_exception":
        return "escalate"
    return "review"


def _route_after_review(state: RestockingState) -> str:
    return "revalidate" if state["decision"] == "APPROVE" else "reject"


def _revalidate(state: RestockingState, repository: RestockRepository) -> dict[str, Any]:
    current_date = datetime.now(BUSINESS_TZ).date().isoformat()
    if current_date != state["as_of"] or not repository.revalidate_draft(state):
        reason = "Stock, active offers, or the business date changed; a fresh review is required."
        repository.record_decision(state, "REQUEST_CHANGES", reason)
        return {"approval_valid": False, "feedback": reason, "as_of": current_date}
    return {"approval_valid": True}


def _route_after_revalidation(state: RestockingState) -> str:
    return "commit" if state["approval_valid"] else "refresh"


def _commit_approval(state: RestockingState, repository: RestockRepository) -> dict[str, Any]:
    repository.record_decision(state, "APPROVE")
    repository.queue_supplier_emails(state)
    repository.record_audit(
        state, state["reviewer_id"], "supplier_emails_queued",
        {"version": state["draft_version"], "version_hash": state["version_hash"]},
    )
    return {"status": "email_queued"}


def _record_rejection(state: RestockingState, repository: RestockRepository) -> dict[str, Any]:
    repository.record_decision(state, "REJECT", state["feedback"])
    retry_count = state["retry_count"] + 1
    return {"retry_count": retry_count,
            "status": "rejected" if retry_count <= state["max_retries"] else "retry_limit"}


def _route_after_rejection(state: RestockingState) -> str:
    return "redraft" if state["status"] == "rejected" else "escalate"


def _escalate(state: RestockingState, repository: RestockRepository) -> dict[str, Any]:
    repository.escalate(state, state.get("feedback", "No eligible supplier or retry limit reached."))
    return {"status": "escalated"}


def build_restocking_graph(repository: RestockRepository,
                           authorizer: ReviewerAuthorizer,
                           checkpointer: BaseCheckpointSaver):
    """Build the durable graph. Auth and Postgres checkpointing are injected by M1/API."""
    if checkpointer is None:
        raise ValueError("A durable PostgreSQL LangGraph checkpointer is required.")

    graph = StateGraph(RestockingState)
    graph.add_node("load_inventory", lambda state: _load_inventory(state, repository))
    graph.add_node("evaluate_inventory", _evaluate_inventory)
    graph.add_node("publish_stock_signals", lambda state: _publish_signals(state, repository))
    graph.add_node("retrieve_suppliers", lambda state: _load_suppliers(state, repository))
    graph.add_node("draft_report", lambda state: _draft_report(state, repository))
    graph.add_node("human_review", lambda state: _human_review(state, authorizer))
    graph.add_node("revalidate", lambda state: _revalidate(state, repository))
    graph.add_node("commit_approval", lambda state: _commit_approval(state, repository))
    graph.add_node("record_rejection", lambda state: _record_rejection(state, repository))
    graph.add_node("escalate", lambda state: _escalate(state, repository))

    graph.add_edge(START, "load_inventory")
    graph.add_edge("load_inventory", "evaluate_inventory")
    graph.add_edge("evaluate_inventory", "publish_stock_signals")
    graph.add_conditional_edges(
        "publish_stock_signals",
        lambda state: "suppliers" if state["candidates"] else "draft",
        {"suppliers": "retrieve_suppliers", "draft": "draft_report"},
    )
    graph.add_edge("retrieve_suppliers", "draft_report")
    graph.add_conditional_edges(
        "draft_report", _route_after_draft,
        {"end": END, "escalate": "escalate", "review": "human_review"},
    )
    graph.add_conditional_edges(
        "human_review", _route_after_review,
        {"revalidate": "revalidate", "reject": "record_rejection"},
    )
    graph.add_conditional_edges(
        "revalidate", _route_after_revalidation,
        {"commit": "commit_approval", "refresh": "load_inventory"},
    )
    graph.add_edge("commit_approval", END)
    graph.add_conditional_edges(
        "record_rejection", _route_after_rejection,
        {"redraft": "load_inventory", "escalate": "escalate"},
    )
    graph.add_edge("escalate", END)
    return graph.compile(checkpointer=checkpointer)


def new_run(branch_id: int, as_of: date | None = None) -> RestockingState:
    business_date = as_of or datetime.now(BUSINESS_TZ).date()
    date_text = business_date.isoformat()
    return {
        "run_id": f"restock:{branch_id}:{date_text}",
        "branch_id": branch_id,
        "as_of": date_text,
        "retry_count": 0,
        "status": "running",
        "feedback": "",
    }


def start_run(app: Any, state: RestockingState) -> dict[str, Any]:
    """Start once per branch/business date; resume is handled by LangGraph's interrupt."""
    config = {"configurable": {"thread_id": state["run_id"]}}
    existing = app.get_state(config)
    if existing and existing.values:
        return existing.values
    return app.invoke(state, config=config)


def resume_review(app: Any, run_id: str, decision: str,
                  actor_id: str, reason: str = "") -> dict[str, Any]:
    decision = decision.upper()
    if decision not in {"APPROVE", "REJECT"}:
        raise ValueError("Decision must be APPROVE or REJECT.")
    config = {"configurable": {"thread_id": run_id}}
    return app.invoke(
        Command(resume={"decision": decision, "actor_id": actor_id, "reason": reason}),
        config=config,
    )
