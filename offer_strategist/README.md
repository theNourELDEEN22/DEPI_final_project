# AI Offer Strategist

A LangGraph multi-agent workflow that plans offers for near-expiry stock. Two Gemini agents propose an offer. The existing Offers guardrails decide whether it is allowed. A person approves it.

It closes a gap between two epics: Restocking (B) marks overstocked near-expiry products with "send it for offer review instead", but until now nothing picked them up. The Offers pipeline (D) could check and approve offers, but someone had to write each candidate by hand.

## Flow

```
gather_facts ── cannot be promoted ─────────────────────────► END  (skipped, no model call)
     │
  analyst    Gemini agent: urgency, best mechanic, target units (rule-based fallback)
     │
  designer   Gemini agent: concrete terms  ◄──── guardrail reasons ────┐
     │                                                                 │
  critic     D3 validation + D4/D5 guardrails (existing code) ── refused, rounds left
     │ passed                                  │ refused, no rounds left (default 3)
  submit     D2 intake + D6 Sales review card  escalate → a person looks at it
     │
    END      offer is PENDING_APPROVAL; the Sales Team decides with D7 `decide()`
```

The `build_planner_graph` planner fans out one strategist per product with LangGraph `Send` and runs them in parallel.

## Safety rules

- **The models only propose.** Every gate is the existing deterministic code. Nothing here approves or activates an offer.
- **Figures come from stored data.** Every number the agents see comes from the catalog, prices, product rules, forecasts and stock signals, and is computed with `Decimal`. The review card contains no AI text. The analyst brief and the designer rationale are returned separately and labeled `AI-GENERATED`.
- **The model can't change who or what an offer is for.** The proposal schema has no branch, product, evidence or forecast fields; those always come from the request. A bundle partner the model invents is refused.
- **Exact limits before the first call.** The designer receives `max_discount_percent_allowed`, which already includes the margin and price-floor rules: a proposal at the ceiling passes and one cent more fails. It also receives the longest duration that ends before expiry, the quantity cap limit and the valid bundle price range.
- **Fail closed, cheaply.** A product with no price, forecast or evidence, a restriction, an existing approved or active offer, an existing candidate for the same signal, or too little shelf life is skipped before any model call.
- **Model failures never crash a run.** A failed or unparsable designer reply costs one round. A failed analyst call falls back to a labeled rule-based brief.
- **Idempotent.** The same stock signal is never turned into two candidates. With a checkpointer and a `thread_id`, a finished run is returned instead of being planned again.

## Use

```powershell
python -m pip install -r offer_strategist\requirements.txt
$env:GEMINI_API_KEY = "..."            # never commit it; GOOGLE_API_KEY also works
$env:GEMINI_MODEL = "gemini-2.5-flash" # optional, this is the default
python -m offer_strategist.demo --gemini   # or without --gemini: offline scripted agents
```

```python
from offer_strategist import (
    GeminiStrategistLLM, RestockEvidence, StrategistDeps,
    build_planner_graph, plan_offers, requests_from_restock_run,
)

# `restock_state` is the final state of a Restocking run (restocking/workflow.py)
requests, skipped = requests_from_restock_run(restock_state, forecast_ref_for=forecast_lookup)
deps = StrategistDeps(
    llm=GeminiStrategistLLM(),
    catalog=catalog, prices=prices, rules=product_rules, active=active_offers,
    evidence_reader=RestockEvidence(restock_state), forecast_reader=forecasts,
    repo=offer_repository,
)
results = plan_offers(build_planner_graph(deps, checkpointer), requests,
                      thread_id=f"strategist:{branch_id}:{business_date}")
```

Each result has `outcome` (`submitted` / `skipped` / `escalated`), `rounds`, `reasons`, every `attempts` entry with its guardrail reasons, the pending `offer`, its `review_card`, and the labeled `ai_notes`.

## Tests

```powershell
python -m pytest offer_strategist
```

The tests use `ScriptedLLM` (in `fakes.py`) and a fake Gemini client, so they need no API key and no network.

## Still open (same placeholders as the other modules)

- `StrategistConfig` values (3 rounds, 12 h lead time, start within 72 h) wait for the Week 1 decisions meeting (E1).
- The in-memory stores are replaced by M1's database adapters, the same way as in the rest of `offers/`.
