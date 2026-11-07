# Restocking workflow

This package owns the Restocking graph only. It uses the shared Foundation schema in `foundation/db/001_shared_foundation_schema.sql` and does not create parallel inventory, approval, audit, or outbox tables.

## Flow

1. Load branch products, batches, configured thresholds, and active offers.
2. Flag near-expiry batches and low stock for High/Medium demand products. Low demand alone never triggers an order.
3. Skip products covered by an active offer and publish stock signals through the shared `outbox_messages` table.
4. Find suppliers for each category, prepare a positive-quantity draft, and save versioned rows in `restock_orders`.
5. Pause for an authorized reviewer. Rejection feedback creates a new draft; the configured retry limit escalates the run.
6. Recheck inventory and offers after approval. Record the exact version/hash in `approvals`, then enqueue one email per supplier in `outbox_messages`.

All database reads are scoped by `branch_id`. The graph requires an injected `ReviewerAuthorizer` backed by the shared identity middleware and a durable PostgreSQL LangGraph checkpointer.

## Dependencies

```powershell
python -m pip install -r restocking\requirements.txt
```

Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` for the repository adapter. Set `LANGGRAPH_POSTGRES_URL` for the PostgreSQL checkpointer. Keep credentials in environment variables or a local `.env` file that is not committed.

## Run from the application

The scheduler should start one graph run per branch at 00:00 `Africa/Cairo`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from langgraph.checkpoint.postgres import PostgresSaver
from supabase import create_client

from restocking.repository import SupabaseRestockRepository
from restocking.workflow import build_restocking_graph, new_run, start_run

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
repository = SupabaseRestockRepository(supabase)
reviewer_authorizer = shared_identity_service  # supplied by the application/Foundation middleware

with PostgresSaver.from_conn_string(LANGGRAPH_POSTGRES_URL) as checkpointer:
    checkpointer.setup()
    graph = build_restocking_graph(repository, reviewer_authorizer, checkpointer)
    run = new_run(branch_id=1, as_of=datetime.now(ZoneInfo("Africa/Cairo")).date())
    result = start_run(graph, run)
```

When an authenticated reviewer responds, call `resume_review(graph, run_id, "APPROVE", actor_id)` or `resume_review(graph, run_id, "REJECT", actor_id, reason)`. The `actor_id` must come from the authenticated application session, never from model-generated text or an untrusted request field.

## Shared contracts used

- `products`, `product_batches`, `suppliers`, `offers`, `config_settings`
- `restock_orders`, `approvals`, `audit_log`, `outbox_messages`
- `outbox_messages` event idempotency keys use the run ID and draft version. The Foundation outbox worker owns external email and event delivery.

Demand and expiry cutoffs are read from the shared `restock_thresholds` config. The current Foundation seed values are implementation defaults and still need the team's Week 1 policy decision.
