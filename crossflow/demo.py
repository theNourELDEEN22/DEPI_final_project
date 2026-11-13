"""Demo (E15): one product from the nightly stock check to the Manager report.

    python -m crossflow.demo            # offline, rule-based agents
    python -m crossflow.demo --gemini   # real Gemini agents (needs GEMINI_API_KEY)
    python -m crossflow.demo --pos unclear   # with a flaky POS: success|unclear|lost_request|garbage
"""
from __future__ import annotations

import argparse

from crossflow.mock_pos import MockPOS
from crossflow.scenario import run_full_scenario


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gemini", action="store_true", help="use the real Gemini agents")
    parser.add_argument("--pos", default="success", help="mock POS behaviour")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    llm = None
    if args.gemini:
        from offer_strategist.llm import GeminiStrategistLLM
        llm = GeminiStrategistLLM()

    result = run_full_scenario(seed=args.seed, llm=llm, pos=MockPOS.scenario(args.pos))
    pid = result.pilot.cases["near_expiry_overstock"]
    product = result.pilot.product(pid)
    print(f"Pilot branch: {result.pilot.branch['branch_name']}, {len(result.pilot.products)} products, "
          f"{len(result.pilot.sales)} sales lines over 8 weeks")
    print(f"Case product: {product['product_name']}, {product['stock_quantity']} in stock, "
          f"price {product['unit_price']}, cost {product['cost_price']}\n")
    for line in result.timeline:
        print("  " + line)
    if result.contract_violations:
        print(f"\nH1 contract gaps reported to Restocking ({len(result.contract_violations)}):")
        for violation in result.contract_violations:
            print(f"  - {violation}")
    print(f"\nHuman actions: {len(result.human_actions)} -> "
          + "; ".join(f"{a['actor']}: {a['action']}" for a in result.human_actions))
    if result.outbox.messages:
        print("\n--- Manager report (from the outbox) ---")
        print(result.outbox.messages[0].body)


if __name__ == "__main__":
    main()
