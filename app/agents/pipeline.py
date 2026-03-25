"""
pipeline.py — LangGraph StateGraph for CodeLineage

Orchestration layer. Wires the four agents into a LangGraph graph with:
    - typed state (GraphState)
    - conditional edges for the critic retry loop
    - a compiled app that main.py calls with ainvoke()

CHANGES v2:
    No structural changes to the graph topology.
    All gap fixes live inside the individual nodes.
    The pipeline wires them up identically — the improvements are
    transparent to the orchestration layer.

Graph topology:

    [impact_analyzer]       ← now diff-aware + traces execution paths
           ↓
    [context_builder]       ← now renders execution paths + code snippets
           ↓
       [critic]  ←──────────────┐
           ↓                    │
    route_after_critic()        │  retry_count < 2
           ↓                    │  AND confidence < 0.7
      ┌────┴────┐               │
      │ "retry" │ ──────────────┘
      │  "done" │ ──→ END  (main.py posts comment after ainvoke returns)
      └─────────┘

Note: the indexer (github_tool.fetch_pr_and_build_graph) runs BEFORE
the graph as an async I/O setup step in main.py.
"""

import logging

from langgraph.graph import StateGraph, END

from app.utils.graph_state import GraphState
from app.agents.impact_analyzer import analyze_impact
from app.agents.context_builder import build_context
from app.agents.critic import run_critic, score_review_confidence

log = logging.getLogger("codelineage.pipeline")

MAX_RETRIES = 2


# ─────────────────────────────────────────────────────────────────────────────
# CONDITIONAL EDGE ROUTER
# ─────────────────────────────────────────────────────────────────────────────

def route_after_critic(state: GraphState) -> str:
    """
    Conditional edge function — decides what comes after the critic node.

    Returns:
        "retry"  → route back to critic (self-correction pass)
        "done"   → proceed to END (main.py posts the comment)

    Routing logic:
        1. If retry_count >= MAX_RETRIES → always proceed (done)
        2. If confidence < 0.7          → retry
        3. Otherwise                    → done

    v2 note: score_review_confidence now also penalises reviews that had
    execution paths available but didn't include path analysis — so the
    retry will naturally be triggered when the first pass misses Gap 6.
    """
    retry_count = state.get("retry_count", 0)

    if retry_count >= MAX_RETRIES:
        log.info(f"Router: max retries reached ({retry_count}) → done")
        return "done"

    confidence = score_review_confidence(state)

    if confidence < 0.7:
        log.info(
            f"Router: confidence {confidence:.2f} < 0.7, "
            f"retry_count={retry_count} → retry"
        )
        return "retry"

    log.info(f"Router: confidence {confidence:.2f} >= 0.7 → done")
    return "done"


# ─────────────────────────────────────────────────────────────────────────────
# RETRY WRAPPER
# ─────────────────────────────────────────────────────────────────────────────

async def critic_retry_node(state: GraphState) -> GraphState:
    """
    Thin wrapper that increments retry_count then calls run_critic.

    Incrementing here (before the LLM call) means run_critic sees
    retry_count > 0 and uses the enriched self-correction prompt
    (which injects edge-case checklist + path questions — Gap 8 fix).
    """
    state["retry_count"] = state.get("retry_count", 0) + 1
    log.info(f"Critic retry node — attempt {state['retry_count']}")
    return await run_critic(state)


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH ASSEMBLY
# ─────────────────────────────────────────────────────────────────────────────

def build_pipeline() -> StateGraph:
    """
    Assembles and compiles the LangGraph StateGraph.

    Called once at startup in main.py.
    Returns a compiled graph ready for ainvoke().
    """
    workflow = StateGraph(GraphState)

    # ── Register nodes ────────────────────────────────────────────────────────
    workflow.add_node("impact",       analyze_impact)
    workflow.add_node("context",      build_context)
    workflow.add_node("critic",       run_critic)
    workflow.add_node("critic_retry", critic_retry_node)

    # ── Entry point ───────────────────────────────────────────────────────────
    workflow.set_entry_point("impact")

    # ── Fixed edges ───────────────────────────────────────────────────────────
    workflow.add_edge("impact",  "context")
    workflow.add_edge("context", "critic")

    # ── Conditional edge after first critic call ──────────────────────────────
    workflow.add_conditional_edges(
        "critic",
        route_after_critic,
        {
            "retry": "critic_retry",
            "done":  END,
        },
    )

    # ── Conditional edge after retry ─────────────────────────────────────────
    workflow.add_conditional_edges(
        "critic_retry",
        route_after_critic,
        {
            "retry": "critic_retry",
            "done":  END,
        },
    )

    compiled = workflow.compile()

    log.info("LangGraph pipeline compiled (v2 — gap fixes active)")
    log.info("  impact: diff-aware, execution path tracing")
    log.info("  context: path rendering, code snippets")
    log.info("  critic: path analysis prompt, constant validation, enriched retry")

    return compiled


# ── Singleton compiled graph — built once, reused per request ────────────────
pipeline = build_pipeline()