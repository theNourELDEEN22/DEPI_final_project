from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from supabase import Client

from .models import RestockingState


BUSINESS_TZ = ZoneInfo("Africa/Cairo")


class SupabaseRestockRepository:
    """Reads the shared Foundation schema and writes only Restocking records."""

    def __init__(self, client: Client) -> None:
        self.client = client

    @staticmethod
    def _rows(query: Any) -> list[dict[str, Any]]:
        return query.execute().data or []

    def _all(self, table: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        page_size = 500
        while True:
            query = self.client.table(table).select("*")
            for column, value in filters.items():
                query = query.eq(column, value)
            page = self._rows(query.range(offset, offset + page_size - 1))
            rows.extend(page)
            if len(page) < page_size:
                return rows
            offset += page_size

    def load_inventory(self, branch_id: int, as_of: str) -> list[dict[str, Any]]:
        products = self._all("products", {"branch_id": branch_id})
        batches = self._all("product_batches", {"branch_id": branch_id})
        batches_by_product: dict[int, list[dict[str, Any]]] = {}
        for batch in batches:
            batches_by_product.setdefault(int(batch["product_id"]), []).append(batch)

        business_date = date.fromisoformat(as_of)
        result = []
        for product in products:
            product_batches = batches_by_product.get(int(product["product_id"]), [])
            sellable_qty = sum(
                int(batch["stock_quantity"])
                for batch in product_batches
                if date.fromisoformat(batch["expiration_date"]) >= business_date
            )
            if not product_batches:
                sellable_qty = int(product.get("stock_quantity", 0))
                if sellable_qty and product.get("expiration_date"):
                    product_batches = [{
                        "batch_id": None,
                        "expiration_date": product["expiration_date"],
                        "stock_quantity": sellable_qty,
                    }]
            result.append({**product, "on_hand": sellable_qty, "batches": product_batches})
        return result

    def load_active_offers(self, branch_id: int, as_of: str) -> set[int]:
        business_date = date.fromisoformat(as_of)
        start = datetime.combine(business_date, time.min, BUSINESS_TZ).isoformat()
        end = datetime.combine(business_date, time.max, BUSINESS_TZ).isoformat()
        rows = self._rows(
            self.client.table("offers")
            .select("product_id")
            .eq("branch_id", branch_id)
            .eq("status", "ACTIVE")
            .lte("start_at", end)
            .gte("end_at", start)
        )
        return {int(row["product_id"]) for row in rows}

    def load_settings(self, branch_id: int) -> dict[str, Any]:
        branch_rows = self._rows(
            self.client.table("config_settings")
            .select("config_value")
            .eq("config_key", "restock_thresholds")
            .eq("branch_id", branch_id)
            .limit(1)
        )
        rows = branch_rows or self._rows(
            self.client.table("config_settings")
            .select("config_value")
            .eq("config_key", "restock_thresholds")
            .eq("branch_id", 0)
            .limit(1)
        )
        if not rows:
            raise ValueError("Missing restock_thresholds in config_settings.")
        settings = rows[0]["config_value"]
        if not isinstance(settings, dict):
            raise ValueError("restock_thresholds must be a JSON object.")
        return settings

    def load_suppliers(self, categories: set[str]) -> dict[str, list[dict[str, Any]]]:
        if not categories:
            return {}
        rows = self._rows(
            self.client.table("suppliers")
            .select("supplier_id,supplier_name,product_category,shipping_duration_days,minimum_order_quantity,email")
            .in_("product_category", sorted(categories))
        )
        result: dict[str, list[dict[str, Any]]] = {}
        for supplier in rows:
            if not supplier.get("email"):
                continue
            result.setdefault(supplier["product_category"], []).append(supplier)
        for options in result.values():
            options.sort(key=lambda item: (item["shipping_duration_days"], item["supplier_id"]))
        return result

    def publish_stock_signals(self, state: RestockingState) -> None:
        signals = [
            {
                "product_id": item["product_id"],
                "sku": item["sku"],
                "branch_id": state["branch_id"],
                "signal_type": item["trigger_reason"],
                "on_hand": item["on_hand"],
                "evidence_ref": f"{state['run_id']}:{item['product_id']}",
            }
            for item in state["candidates"]
        ]
        signaled_products = {item["product_id"] for item in signals}
        signals.extend(
            {
                "product_id": item["product_id"],
                "branch_id": state["branch_id"],
                "signal_type": "NEAR_EXPIRY",
                "evidence_ref": f"{state['run_id']}:{item['product_id']}:expiry",
                "batches": item["batches"],
            }
            for item in state["expiry_watch"]
            if item["product_id"] not in signaled_products
        )
        if not signals:
            return
        key = f"restocking:{state['run_id']}:H1"
        self.client.table("outbox_messages").upsert(
            {
                "idempotency_key": key,
                "branch_id": state["branch_id"],
                "channel": "EVENT_BUS",
                "recipient": "offers-and-reporting",
                "subject": "restocking.stock_signal",
                "payload": {"run_id": state["run_id"], "signals": signals},
            },
            on_conflict="idempotency_key",
            ignore_duplicates=True,
        ).execute()

    def save_draft(self, state: RestockingState) -> tuple[int, str, list[dict[str, Any]]]:
        draft = state["draft"]
        version_hash = draft["version_hash"]
        existing = self._rows(
            self.client.table("restock_orders")
            .select("*")
            .eq("run_id", state["run_id"])
            .eq("version_hash", version_hash)
        )
        if existing:
            return int(existing[0]["version"]), version_hash, existing
        prior = self._rows(
            self.client.table("restock_orders")
            .select("version")
            .eq("run_id", state["run_id"])
            .order("version", desc=True)
            .limit(1)
        )
        version = int(prior[0]["version"]) + 1 if prior else 1
        orders = [
            {
                "run_id": state["run_id"],
                "branch_id": state["branch_id"],
                "product_id": line["product_id"],
                "supplier_id": line["supplier_id"],
                "requested_quantity": line["requested_quantity"],
                "trigger_reason": line["trigger_reason"],
                "status": "PENDING_APPROVAL",
                "retry_count": state["retry_count"],
                "user_feedback": state.get("feedback"),
                "version": version,
                "version_hash": version_hash,
            }
            for line in draft["lines"]
        ]
        saved = self._rows(
            self.client.table("restock_orders").insert(orders).select("*")
        ) if orders else []
        if not saved:
            raise RuntimeError("The draft had no order lines to save.")
        self.record_audit(
            state, "restocking-agent", "draft_created",
            {"version": version, "version_hash": version_hash, "order_ids": [r["restock_order_id"] for r in saved]},
        )
        return version, version_hash, saved

    def record_decision(self, state: RestockingState, decision: str, reason: str = "") -> None:
        if decision not in {"APPROVE", "REJECT", "REQUEST_CHANGES"}:
            raise ValueError(f"Unsupported approval decision: {decision}")
        order_ids = [int(row["restock_order_id"]) for row in state["order_rows"]]
        if not order_ids:
            raise ValueError("Cannot record a decision without saved restock orders.")
        approval_rows = [
            {
                "entity_type": "RESTOCK_ORDER",
                "entity_id": order_id,
                "branch_id": state["branch_id"],
                "actor_id": state.get("reviewer_id", "restocking-agent"),
                "actor_role": state.get("reviewer_role", "system"),
                "decision": decision,
                "reason": reason or None,
                "reviewed_version": state["draft_version"],
                "reviewed_hash": state["version_hash"],
            }
            for order_id in order_ids
        ]
        self.client.table("approvals").insert(approval_rows).execute()

        if decision == "APPROVE":
            status = "APPROVED"
        elif decision == "REJECT":
            status = "REJECTED"
        else:
            status = "DRAFT"
        self.client.table("restock_orders").update(
            {"status": status, "retry_count": state["retry_count"], "user_feedback": reason or None}
        ).eq("run_id", state["run_id"]).eq("version", state["draft_version"]).execute()
        self.record_audit(
            state, state.get("reviewer_id", "restocking-agent"), decision.lower(),
            {"version": state["draft_version"], "version_hash": state["version_hash"], "reason": reason},
        )

    def revalidate_draft(self, state: RestockingState) -> bool:
        current = self.load_inventory(state["branch_id"], datetime.now(BUSINESS_TZ).date().isoformat())
        current_by_id = {int(product["product_id"]): product for product in current}
        current_offers = self.load_active_offers(
            state["branch_id"], datetime.now(BUSINESS_TZ).date().isoformat()
        )
        for line in state["draft"]["lines"]:
            product_id = int(line["product_id"])
            product = current_by_id.get(product_id)
            if not product or int(product["on_hand"]) != int(line["on_hand"]):
                return False
            if product_id in current_offers:
                return False
        return True

    def queue_supplier_emails(self, state: RestockingState) -> None:
        suppliers = state["supplier_options"]
        grouped: dict[int, list[dict[str, Any]]] = {}
        supplier_by_id: dict[int, dict[str, Any]] = {}
        for line in state["draft"]["lines"]:
            supplier_id = int(line["supplier_id"])
            grouped.setdefault(supplier_id, []).append(line)
            for options in suppliers.values():
                for option in options:
                    if int(option["supplier_id"]) == supplier_id:
                        supplier_by_id[supplier_id] = option
        for supplier_id, lines in grouped.items():
            supplier = supplier_by_id[supplier_id]
            key = f"restock:{state['run_id']}:v{state['draft_version']}:supplier:{supplier_id}"
            payload = {
                "run_id": state["run_id"],
                "version": state["draft_version"],
                "version_hash": state["version_hash"],
                "branch_id": state["branch_id"],
                "supplier_id": supplier_id,
                "lines": lines,
            }
            self.client.table("outbox_messages").upsert(
                {
                    "idempotency_key": key,
                    "branch_id": state["branch_id"],
                    "channel": "EMAIL",
                    "recipient": supplier["email"],
                    "subject": f"Restock request {state['run_id']} (v{state['draft_version']})",
                    "payload": payload,
                },
                on_conflict="idempotency_key",
                ignore_duplicates=True,
            ).execute()

    def record_audit(self, state: RestockingState, actor: str, action: str,
                     details: dict[str, Any] | None = None) -> None:
        self.client.table("audit_log").insert(
            {
                "run_id": state["run_id"],
                "branch_id": state["branch_id"],
                "actor": actor,
                "action": action,
                "entity_type": "RESTOCK_ORDER",
                "entity_id": state.get("run_id"),
                "version": state.get("draft_version"),
                "details": details or {},
            }
        ).execute()

    def escalate(self, state: RestockingState, reason: str) -> None:
        self.client.table("restock_orders").update(
            {"status": "ESCALATED", "retry_count": state["retry_count"],
             "user_feedback": reason}
        ).eq("run_id", state["run_id"]).eq("version", state.get("draft_version", 1)).execute()
        self.record_audit(state, "restocking-agent", "escalated", {"reason": reason})
        self.client.table("outbox_messages").upsert(
            {
                "idempotency_key": f"restock:{state['run_id']}:escalation:v{state.get('draft_version', 1)}",
                "branch_id": state["branch_id"],
                "channel": "EVENT_BUS",
                "recipient": "restocking-operator",
                "subject": "restocking.escalation",
                "payload": {"run_id": state["run_id"], "reason": reason,
                            "retry_count": state["retry_count"]},
            },
            on_conflict="idempotency_key",
            ignore_duplicates=True,
        ).execute()
