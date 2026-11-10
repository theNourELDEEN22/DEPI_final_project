from langgraph.checkpoint.memory import InMemorySaver

from offer_strategist.fakes import ScriptedLLM, make_proposal
from offer_strategist.graph import build_planner_graph, plan_offers
from offer_strategist.tests.world import make_deps, make_request


def scripted():
    return ScriptedLLM(
        {
            "101": [make_proposal(discount=45), make_proposal(discount=20)],  # fixed on round 2
            "102": [make_proposal(discount=30)],
        }
    )


def requests():
    # 101 twice: the planner plans each product once per run
    return [make_request("102"), make_request("101"), make_request("103"), make_request("101")]


def test_fan_out_plans_every_product_once_and_in_parallel():
    llm = scripted()
    results = plan_offers(build_planner_graph(make_deps(llm)), requests())

    assert [(r["product_id"], r["outcome"]) for r in results] == [
        ("101", "submitted"),
        ("102", "submitted"),
        ("103", "skipped"),
    ]
    assert [r["rounds"] for r in results] == [2, 1, 0]
    assert sorted(llm.analyze_calls) == ["101", "102"]  # 103 never reached a model


def test_empty_batch_returns_no_results():
    assert plan_offers(build_planner_graph(make_deps(ScriptedLLM({}))), []) == []


def test_checkpointed_batch_is_not_planned_twice():
    llm = scripted()
    planner = build_planner_graph(make_deps(llm), checkpointer=InMemorySaver())
    first = plan_offers(planner, requests(), thread_id="strategist:1:2026-11-10")
    second = plan_offers(planner, requests(), thread_id="strategist:1:2026-11-10")
    assert [r["offer_id"] for r in first] == [r["offer_id"] for r in second]
    assert len(llm.design_calls) == 3  # 2 rounds for 101 + 1 for 102, nothing on the re-run
