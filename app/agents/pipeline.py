"""
pipeline.py — LangGraph StateGraph for CodeLineage

This is the orchestration layer. It wires the four agents into a
proper LangGraph graph with:
    - typed state (GraphState)
    - conditional edges for the critic retry loop
    - a compiled app that main.py calls with ainvoke()

Graph topology:

    [impact_analyzer]
           ↓
    [context_builder]
           ↓
       [critic]  ←──────────────┐
           ↓                    │
    route_after_critic()        │  retry_count < 2
           ↓                    │  AND confidence < 0.7
      ┌────┴────┐               │
      │ "retry" │ ──────────────┘
      │  "done" │ ──→ [post_comment_node] → END
      └─────────┘

Note: the indexer (github_tool.fetch_pr_and_build_graph) runs BEFORE
the graph because it's an async I/O operation that builds the initial
state. LangGraph nodes are synchronous Python functions or async coroutines —
the indexer fits better as a pre-graph setup step called in main.py.
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
        "done"   → proceed to post_comment

    Routing logic:
        1. If retry_count >= MAX_RETRIES → always proceed (done)
        2. If confidence < 0.7          → retry
        3. Otherwise                    → done
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

    LangGraph needs a distinct node name for the retry path so the
    conditional edge can route to it separately from the first critic call.
    Incrementing retry_count here (before the LLM call) means run_critic
    will see retry_count > 0 and use the self-correction prompt.
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

    Node names map to:
        "impact"        → analyze_impact()       — impact_analyzer.py
        "context"       → build_context()        — context_builder.py
        "critic"        → run_critic()           — critic.py
        "critic_retry"  → critic_retry_node()    — this file (wraps critic)

    Edges:
        impact  → context  (always)
        context → critic   (always)
        critic  → router   (conditional: "retry" or "done")
        retry   → critic_retry → router (conditional again)
        done    → END
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
    # route_after_critic() returns "retry" or "done"
    # "retry" → go to critic_retry node (which increments count + re-runs critic)
    # "done"  → end the graph (main.py posts the comment after ainvoke returns)
    workflow.add_conditional_edges(
        "critic",
        route_after_critic,
        {
            "retry": "critic_retry",
            "done":  END,
        },
    )

    # ── Conditional edge after retry ─────────────────────────────────────────
    # Same router — if still low confidence and retries left, retry again.
    # If max retries reached, done.
    workflow.add_conditional_edges(
        "critic_retry",
        route_after_critic,
        {
            "retry": "critic_retry",
            "done":  END,
        },
    )

    compiled = workflow.compile()

    log.info("LangGraph pipeline compiled successfully")
    log.info("  Nodes: impact → context → critic → (retry?) → END")

    return compiled


# ── Singleton compiled graph — built once, reused per request ────────────────
pipeline = build_pipeline()