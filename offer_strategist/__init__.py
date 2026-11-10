"""AI Offer Strategist: a LangGraph multi-agent workflow that plans offers for
near-expiry stock. LLM agents propose; the existing Offers guardrails decide;
a person approves. See offer_strategist/README.md."""
from offer_strategist.facts import FactsUnavailable, StrategistConfig, StrategistRequest, gather_facts
from offer_strategist.graph import (
    StrategistDeps,
    build_planner_graph,
    build_strategist_graph,
    plan_offer,
    plan_offers,
)
from offer_strategist.llm import (
    GeminiStrategistLLM,
    OfferProposal,
    StrategistLLM,
    StrategistLLMError,
    StrategyBrief,
)
from offer_strategist.restock_bridge import RestockEvidence, requests_from_restock_run

__all__ = [
    "FactsUnavailable",
    "GeminiStrategistLLM",
    "OfferProposal",
    "RestockEvidence",
    "StrategistConfig",
    "StrategistDeps",
    "StrategistLLM",
    "StrategistLLMError",
    "StrategistRequest",
    "StrategyBrief",
    "build_planner_graph",
    "build_strategist_graph",
    "gather_facts",
    "plan_offer",
    "plan_offers",
    "requests_from_restock_run",
]
