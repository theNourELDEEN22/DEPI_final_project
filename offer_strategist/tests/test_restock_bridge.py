from offer_strategist.restock_bridge import RestockEvidence, requests_from_restock_run

RUN = "restock:1:2026-11-10"

# The shape of a finished Restocking graph state (restocking/workflow.py).
STATE = {
    "run_id": RUN,
    "branch_id": 1,
    "products": [
        {"product_id": 101, "product_name": "Milk 1L", "category": "Dairy", "demand_level": "High", "on_hand": 120},
        {"product_id": 102, "product_name": "Bread", "category": "Bakery", "demand_level": "High", "on_hand": 5},
        {"product_id": 104, "product_name": "Yogurt", "category": "Dairy", "demand_level": "Medium", "on_hand": 30},
    ],
    "candidates": [{"product_id": 102}],  # restocking reorders bread
    "expiry_watch": [
        {"product_id": 101, "product_name": "Milk 1L", "batches": [
            {"batch_id": 1, "expires_on": "2026-11-13", "days_left": 3, "quantity": 25},
            {"batch_id": 2, "expires_on": "2026-11-14", "days_left": 4, "quantity": 15},
        ]},
        {"product_id": 102, "product_name": "Bread", "batches": [{"batch_id": 3, "quantity": 5}]},
        {"product_id": 104, "product_name": "Yogurt", "batches": [{"batch_id": 4, "quantity": 10}]},
    ],
}


def test_overstocked_near_expiry_products_become_requests():
    requests, skipped = requests_from_restock_run(
        STATE, forecast_ref_for=lambda b, p: {"101": "fc-101"}.get(p),
        bundle_partners_for=lambda b, p: ("104",),
    )
    assert len(requests) == 1
    milk = requests[0]
    assert (milk.branch_id, milk.product_id) == ("1", "101")
    assert milk.evidence_ref == f"{RUN}:101:expiry"  # same ref restocking publishes
    assert milk.expiring_units == 40 and milk.stock_on_hand == 120
    assert milk.demand_level == "high"
    assert milk.bundle_partner_ids == ("104",)

    reasons = {s["product_id"]: s["reason"] for s in skipped}
    assert reasons == {"102": "restocking is reordering it", "104": "no stored forecast"}


def test_evidence_reader_serves_the_stock_signal():
    evidence = RestockEvidence(STATE)
    milk = evidence.get_evidence(f"{RUN}:101:expiry")
    assert milk.kind == "stock_signal"
    assert milk.facts["expiring_units"] == 40 and milk.facts["on_hand"] == 120
    assert evidence.get_evidence(f"{RUN}:102:expiry") is None  # being reordered, no offer
