"""Demo: plan offers for one Maadi branch night.

    python -m offer_strategist.demo            # offline, scripted agents (no key needed)
    python -m offer_strategist.demo --gemini   # real Gemini agents (needs GEMINI_API_KEY)

The scripted run makes deliberate mistakes on the first round, so you can see
the critic (the real guardrails) refuse them and the designer fix them.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from offers.guardrails import InMemoryActiveOffers, InMemoryPrices, InMemoryProductRules, PriceInfo
from offers.intake import InMemoryOfferRepository
from offers.review import Evidence, Forecast, InMemoryEvidence, InMemoryForecasts
from offers.schema import OfferType
from offers.validation import InMemoryCatalog, RejectionLog
from offer_strategist.facts import StrategistRequest
from offer_strategist.fakes import ScriptedLLM, make_proposal
from offer_strategist.graph import StrategistDeps, build_planner_graph, plan_offers

BRANCH = "1"  # Maadi
#            id      name             price   cost  stock  expiring  expires in (days)
CATALOG = [("2001", "Milk 1L",        "32",  "22",  140,   60,       6),
           ("2002", "Yogurt cup",     "12",  "7",   300,   0,        None),
           ("2003", "Cheddar 250g",   "85",  "60",  45,    30,       12),
           ("2004", "Croissant",      "15",  "6",   80,    80,       1)]


def build_world(llm, now: datetime) -> tuple[StrategistDeps, list[StrategistRequest]]:
    prices, expiry, evidence, forecasts, requests = {}, {}, {}, {}, []
    for pid, name, price, cost, stock, expiring, days in CATALOG:
        prices[(BRANCH, pid)] = PriceInfo(price=Decimal(price), cost=Decimal(cost))
        if days is not None:
            expiry[(BRANCH, pid)] = now + timedelta(days=days)
        evidence[f"sig-{pid}"] = Evidence(f"sig-{pid}", "stock_signal", {"on_hand": stock, "expiring_units": expiring})
        forecasts[f"fc-{pid}"] = Forecast(f"fc-{pid}", Decimal(stock) * Decimal("0.4"), now)
        if expiring:
            requests.append(
                StrategistRequest(
                    branch_id=BRANCH, product_id=pid, product_name=name, category="Dairy",
                    demand_level="medium", stock_on_hand=stock, expiring_units=expiring,
                    evidence_ref=f"sig-{pid}", forecast_ref=f"fc-{pid}",
                    bundle_partner_ids=("2002",) if pid == "2001" else (),
                )
            )
    deps = StrategistDeps(
        llm=llm,
        catalog=InMemoryCatalog({BRANCH: {row[0] for row in CATALOG}}),
        prices=InMemoryPrices(prices),
        rules=InMemoryProductRules(expiry=expiry),
        active=InMemoryActiveOffers(),
        evidence_reader=InMemoryEvidence(evidence),
        forecast_reader=InMemoryForecasts(forecasts),
        repo=InMemoryOfferRepository(),
        log=RejectionLog(),
        clock=lambda: now,
    )
    return deps, requests


def scripted_llm() -> ScriptedLLM:
    return ScriptedLLM(
        {
            # too deep a discount first (margin), then a sensible one
            "2001": [make_proposal(discount=40, quantity_cap=60, duration_days=5),
                     make_proposal(OfferType.BUNDLE, partner="2002", bundle_price=38,
                                   quantity_cap=60, duration_days=5)],
            # buy2get1 is not allowed at this cost, then a small discount
            "2003": [make_proposal(OfferType.BUY2GET1, quantity_cap=30, duration_days=10),
                     make_proposal(discount=12, quantity_cap=30, duration_days=10)],
        }
    )


def print_result(result: dict) -> None:
    print(f"\n=== product {result['product_id']}: {result['outcome'].upper()} after {result['rounds']} round(s)")
    for attempt in result["attempts"]:
        proposal = attempt["proposal"] or {}
        terms = {k: v for k, v in proposal.items() if v is not None and k != "rationale"}
        verdict = "PASSED" if attempt["passed"] else "REFUSED"
        print(f"  round {attempt['round']}: {verdict} {terms}")
        for reason in attempt["reasons"]:
            print(f"      - {reason}")
    if result["outcome"] != "submitted":
        for reason in result["reasons"]:
            print(f"  reason: {reason}")
        return
    card = result["review_card"]
    print(f"  -> Sales review card, offer {card['offer_id'][:8]} v{card['version']} hash {card['content_hash'][:12]}")
    for line in card["lines"]:
        print(f"     {line['label']}: list {line['list_price']} -> offer {line['offer_price']}, "
              f"discount {line['discount_percent']}%, margin {line['margin_percent']}%")
    notes = result["ai_notes"]
    print(f"  [{notes['label']}] {notes['designer_rationale']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gemini", action="store_true", help="use real Gemini agents (needs GEMINI_API_KEY)")
    args = parser.parse_args()

    if args.gemini:
        from offer_strategist.llm import GeminiStrategistLLM

        llm = GeminiStrategistLLM()
        print(f"Using Gemini model {llm.model}")
    else:
        llm = scripted_llm()
        print("Using scripted agents (offline). Run with --gemini for real ones.")

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    deps, requests = build_world(llm, now)
    results = plan_offers(build_planner_graph(deps), requests)
    for result in results:
        print_result(result)
    submitted = sum(r["outcome"] == "submitted" for r in results)
    print(f"\n{submitted} of {len(results)} offer(s) wait for a Sales decision (D7). Nothing was activated.")


if __name__ == "__main__":
    main()
