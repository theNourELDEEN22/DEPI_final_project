"""E3 - Mock POS service: success, failure and unclear responses (Epic E).

Implements both POS interfaces Offers uses:
  offers.pos_activation.PosClient    activate / get_status
  offers.monitoring.PosStopClient    deactivate / get_state
and rings up sales with the live offer terms applied (ring_up), so a scenario
can produce real sales lines with offer_id and discount_amount.

It behaves like a real POS on the points that matter:
- requests are applied by idempotency key: replaying a key never creates a
  second offer; reusing a key with a DIFFERENT payload is refused;
- a stop key is the activation key + ":stop" (offers/monitoring.py);
- faults are scripted per call, in order, so every failure path is reproducible:

    Fault.REJECT        POS refuses, nothing applied
    Fault.LOST_REQUEST  timeout, the request never arrived (nothing applied)
    Fault.LOST_REPLY    applied, then the reply is lost (timeout)
    Fault.GARBAGE       applied, the reply is unreadable
    Fault.SERVER_ERROR  HTTP 5xx style error, nothing applied

MockPOS.scenario("success" | "failure" | "unclear" | "lost_request") gives the
standard setups for M4's tests (D10, D11, D14).
"""
from __future__ import annotations

import threading
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from offers.monitoring import PosStopOutcome, PosStopResponse
from offers.pos_activation import PosOutcome, PosResponse

TWO_PLACES = Decimal("0.01")
CAIRO = ZoneInfo("Africa/Cairo")
STOP_SUFFIX = ":stop"
REQUIRED_PAYLOAD_FIELDS = (
    "offer_id", "version", "approval_hash", "branch_id", "type",
    "product_ids", "start_date", "end_date", "quantity_cap", "terms",
)


class Fault(str, Enum):
    NONE = "none"
    REJECT = "reject"
    LOST_REQUEST = "lost_request"
    LOST_REPLY = "lost_reply"
    GARBAGE = "garbage"
    SERVER_ERROR = "server_error"


GARBAGE_REPLY = "<html><body>502 Bad Gateway</body></html>"


class MockPOS:
    def __init__(
        self,
        *,
        activate_faults: Iterable[Fault] = (),
        status_faults: Iterable[Fault] = (),
        stop_faults: Iterable[Fault] = (),
        state_faults: Iterable[Fault] = (),
    ) -> None:
        self._lock = threading.Lock()
        self._faults = {
            "activate": list(activate_faults),
            "status": list(status_faults),
            "stop": list(stop_faults),
            "state": list(state_faults),
        }
        self._payloads: dict[str, dict[str, Any]] = {}  # activation key -> payload
        self._refs: dict[str, str] = {}
        self._stopped: set[str] = set()                 # activation keys
        self._redemptions: dict[str, dict[str, Any]] = {}  # offer_id -> units, receipts, discount
        self.calls: list[tuple[str, str, str]] = []     # (operation, key, fault)

    @classmethod
    def scenario(cls, name: str) -> "MockPOS":
        presets = {
            "success": {},
            "failure": {"activate_faults": [Fault.REJECT]},
            "unclear": {"activate_faults": [Fault.LOST_REPLY], "stop_faults": [Fault.LOST_REPLY]},
            "lost_request": {"activate_faults": [Fault.LOST_REQUEST], "stop_faults": [Fault.LOST_REQUEST]},
            "garbage": {"activate_faults": [Fault.GARBAGE], "stop_faults": [Fault.GARBAGE]},
        }
        if name not in presets:
            raise ValueError(f"unknown scenario {name!r}; choose from {sorted(presets)}")
        return cls(**presets[name])

    # --- helpers --------------------------------------------------------------------------

    def _next(self, operation: str, key: str) -> Fault:
        queue = self._faults[operation]
        fault = queue.pop(0) if queue else Fault.NONE
        self.calls.append((operation, key, fault.value))
        return fault

    @staticmethod
    def _payload_problem(payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return "payload must be an object"
        missing = [f for f in REQUIRED_PAYLOAD_FIELDS if f not in payload]
        if missing:
            return f"payload is missing {', '.join(missing)}"
        return None

    # --- PosClient (D10) ---------------------------------------------------------------------

    def activate(self, idempotency_key: str, payload: dict) -> PosResponse:
        with self._lock:
            fault = self._next("activate", idempotency_key)
            if fault == Fault.LOST_REQUEST:
                raise TimeoutError("request timed out before reaching the POS")
            if fault == Fault.SERVER_ERROR:
                raise ConnectionError("POS answered HTTP 503")
            if fault == Fault.REJECT:
                return PosResponse(PosOutcome.REJECTED, detail="rejected by the mock POS")
            problem = self._payload_problem(payload)
            if problem:
                return PosResponse(PosOutcome.REJECTED, detail=problem)
            known = self._payloads.get(idempotency_key)
            if known is not None and known != payload:
                return PosResponse(PosOutcome.REJECTED, detail="idempotency key reused with a different payload")
            if idempotency_key in self._stopped:
                return PosResponse(PosOutcome.REJECTED, detail="this offer was stopped and cannot be re-activated")
            self._payloads[idempotency_key] = payload
            ref = self._refs.setdefault(idempotency_key, f"pos-{len(self._refs) + 1}")
            if fault == Fault.LOST_REPLY:
                raise TimeoutError("reply lost after the POS applied the request")
            if fault == Fault.GARBAGE:
                return GARBAGE_REPLY  # type: ignore[return-value]  # deliberately unreadable
            return PosResponse(PosOutcome.ACTIVE, pos_ref=ref)

    def get_status(self, idempotency_key: str) -> PosResponse:
        with self._lock:
            fault = self._next("status", idempotency_key)
            if fault in (Fault.LOST_REQUEST, Fault.LOST_REPLY):
                raise TimeoutError("status request timed out")
            if fault == Fault.SERVER_ERROR:
                raise ConnectionError("POS answered HTTP 503")
            if fault == Fault.GARBAGE:
                return GARBAGE_REPLY  # type: ignore[return-value]
            if idempotency_key not in self._payloads:
                return PosResponse(PosOutcome.NOT_FOUND)
            if idempotency_key in self._stopped:
                return PosResponse(PosOutcome.REJECTED, detail="stopped")
            return PosResponse(PosOutcome.ACTIVE, pos_ref=self._refs[idempotency_key])

    # --- PosStopClient (D11) ------------------------------------------------------------------

    @staticmethod
    def _activation_key(stop_key: str) -> str:
        return stop_key[: -len(STOP_SUFFIX)] if stop_key.endswith(STOP_SUFFIX) else stop_key

    def deactivate(self, idempotency_key: str) -> PosStopResponse:
        key = self._activation_key(idempotency_key)
        with self._lock:
            fault = self._next("stop", idempotency_key)
            if fault == Fault.LOST_REQUEST:
                raise TimeoutError("stop request timed out before reaching the POS")
            if fault == Fault.SERVER_ERROR:
                raise ConnectionError("POS answered HTTP 503")
            if fault == Fault.REJECT:
                return PosStopResponse(PosStopOutcome.STILL_ACTIVE, detail="stop refused by the mock POS")
            if key in self._payloads:
                self._stopped.add(key)
            if fault == Fault.LOST_REPLY:
                raise TimeoutError("reply lost after the POS applied the stop")
            if fault == Fault.GARBAGE:
                return GARBAGE_REPLY  # type: ignore[return-value]
            return PosStopResponse(PosStopOutcome.STOPPED)

    def get_state(self, idempotency_key: str) -> PosStopResponse:
        key = self._activation_key(idempotency_key)
        with self._lock:
            fault = self._next("state", idempotency_key)
            if fault in (Fault.LOST_REQUEST, Fault.LOST_REPLY):
                raise TimeoutError("state request timed out")
            if fault == Fault.SERVER_ERROR:
                raise ConnectionError("POS answered HTTP 503")
            if fault == Fault.GARBAGE:
                return GARBAGE_REPLY  # type: ignore[return-value]
            if key in self._payloads and key not in self._stopped:
                return PosStopResponse(PosStopOutcome.STILL_ACTIVE)
            return PosStopResponse(PosStopOutcome.STOPPED)

    # --- what is live, and selling -------------------------------------------------------------

    def live_offers(self) -> dict[str, dict[str, Any]]:
        """Activation key -> payload, for everything currently applied at the till."""
        with self._lock:
            return {k: dict(p) for k, p in self._payloads.items() if k not in self._stopped}

    def redemption_log(self, offer_id: str) -> dict[str, Any]:
        """The POS's own count for an offer: units redeemed, receipts, discount given."""
        with self._lock:
            log = self._redemptions.get(offer_id, {"units": 0, "receipts": set(), "discount": Decimal(0)})
            return {"units": log["units"], "receipts": len(log["receipts"]), "discount": log["discount"]}

    def ring_up(
        self,
        *,
        sale_line_id: int,
        transaction_id: int,
        branch_id: int,
        product_id: int,
        sku: str,
        product_name: str,
        quantity: int,
        unit_price: Decimal,
        at: datetime,
        payment_method: str = "cash",
    ) -> dict[str, Any]:
        """One sales line (sales_transactions shape) with any live offer applied.
        Supports discount and buy2get1; a bundle is never applied by this mock."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        offer_id, discount = None, Decimal("0")
        for payload in self.live_offers().values():
            if (
                str(payload["branch_id"]) == str(branch_id)
                and str(product_id) in {str(p) for p in payload["product_ids"]}
                and datetime.fromisoformat(payload["start_date"]) <= at < datetime.fromisoformat(payload["end_date"])
            ):
                terms = payload["terms"]
                if payload["type"] == "discount":
                    pct = Decimal(str(terms["discount_percent"]))
                    discount = unit_price * quantity * pct / Decimal(100)
                elif payload["type"] == "buy2get1":
                    discount = unit_price * (quantity // 3)
                else:
                    continue
                offer_id = payload["offer_id"]
                break
        discount = discount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if offer_id is not None:
            with self._lock:
                log = self._redemptions.setdefault(offer_id, {"units": 0, "receipts": set(), "discount": Decimal(0)})
                log["units"] += quantity
                log["receipts"].add(transaction_id)
                log["discount"] += discount
        local = at.astimezone(CAIRO)  # sales lines store the branch's local date and time
        return {
            "sale_line_id": sale_line_id,
            "transaction_id": transaction_id,
            "date": local.date(),
            "time": local.timetz().replace(tzinfo=None),
            "branch_id": branch_id,
            "product_id": product_id,
            "sku": sku,
            "product_name": product_name,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_amount": (unit_price * quantity - discount).quantize(TWO_PLACES),
            "payment_method": payment_method,
            "offer_id": offer_id,
            "discount_amount": discount,
        }
