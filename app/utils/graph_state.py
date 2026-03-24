"""
graph_state.py — Shared pipeline state for CodeLineage agents

Single source of truth for every key passed between LangGraph nodes:
    github_tool (indexer) → impact_analyzer → context_builder → critic

LangGraph reads this TypedDict to validate state at every edge.
total=False means every key is optional — nodes only write what they produce.
"""

from typing import TypedDict, Dict, List, Any


class GraphState(TypedDict, total=False):

    # ── PR metadata ─────────────────────────────────────────────────────────────
    # Built in main.py from the GitHub webhook payload.
    # Every node can read this for repo/PR context.
    pr_context: Dict[str, Any]
    # {
    #   repo_name:   "alice/my-repo",
    #   pr_number:   42,
    #   pr_title:    "...",
    #   head_sha:    "abc123",
    #   author:      "alice",
    #   base_branch: "main",
    #   head_branch: "feature/...",
    # }

    # ── Knowledge graph ──────────────────────────────────────────────────────────
    # Built by github_tool.fetch_pr_and_build_graph().
    # Keyed by file path → parsed AST data from ast_tool.
    graph: Dict[str, Any]

    # ── PR diff ──────────────────────────────────────────────────────────────────
    # Raw diff list from GitHub API — one entry per changed .py file.
    pr_diff: List[Dict[str, Any]]
    # [{ file_path, status, additions, deletions, patch }, ...]

    # ── Changed files ────────────────────────────────────────────────────────────
    # Just the file paths from pr_diff — used by impact_analyzer.
    changed_files: List[str]

    # ── Impact analysis outputs ──────────────────────────────────────────────────
    # Written by impact_analyzer, read by context_builder and critic.
    changed_functions: List[str]    # functions defined in changed files
    impacted_functions: List[str]   # functions that CALL INTO changed functions
    impacted_files: List[str]       # files containing impacted functions

    # ── LLM context ──────────────────────────────────────────────────────────────
    # Written by context_builder, read by critic as its main prompt payload.
    llm_context: str

    # ── Critic retry tracking ────────────────────────────────────────────────────
    # Managed by the conditional edge router in pipeline.py.
    retry_count: int    # increments on each critic retry — hard cap at 2

    # ── Final output ─────────────────────────────────────────────────────────────
    # Written by critic, posted to GitHub by main.py.
    review: str         # formatted PR review markdown string