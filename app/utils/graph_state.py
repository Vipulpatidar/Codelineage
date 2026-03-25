"""
graph_state.py — Shared pipeline state for CodeLineage agents

Single source of truth for every key passed between LangGraph nodes:
    github_tool (indexer) → impact_analyzer → context_builder → critic

LangGraph reads this TypedDict to validate state at every edge.
total=False means every key is optional — nodes only write what they produce.

CHANGES v2:
    - added qualified_changed_functions  (Gap 1 fix: file::func keys, not bare names)
    - added execution_paths              (Gap 3 fix: traced call chains for critic)
    - added changed_lines                (Gap 1 fix: only flag actually-diffed lines)
"""

from typing import TypedDict, Dict, List, Any


class GraphState(TypedDict, total=False):

    # ── PR metadata ──────────────────────────────────────────────────────────
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

    # ── Knowledge graph ──────────────────────────────────────────────────────
    # Built by github_tool.fetch_pr_and_build_graph().
    # Keyed by file path → parsed AST data from ast_tool.
    graph: Dict[str, Any]

    # ── PR diff ──────────────────────────────────────────────────────────────
    # Raw diff list from GitHub API — one entry per changed .py file.
    pr_diff: List[Dict[str, Any]]
    # [{ file_path, status, additions, deletions, patch }, ...]

    # ── Changed files ─────────────────────────────────────────────────────────
    # Just the file paths from pr_diff — used by impact_analyzer.
    changed_files: List[str]

    # ── Changed line ranges per file ──────────────────────────────────────────
    # GAP 1 FIX: parsed from the git patch hunk headers so impact_analyzer
    # can determine which specific functions were touched by the diff,
    # rather than flagging every function in a changed file.
    #
    # Format: { "services/ingest.py": [34, 35, 36, 38], ... }
    changed_lines: Dict[str, List[int]]

    # ── Impact analysis outputs ───────────────────────────────────────────────
    # Written by impact_analyzer, read by context_builder and critic.

    # GAP 1 FIX: bare names (kept for backward compat with context_builder sections)
    changed_functions: List[str]

    # GAP 2 FIX: qualified keys prevent name-collision false positives.
    # Format: ["services/ingest.py::process_document", "models/faiss.py::add_vectors"]
    # Used internally by impact_analyzer for caller matching.
    qualified_changed_functions: List[str]

    impacted_functions: List[str]   # functions that CALL INTO changed functions
    impacted_files: List[str]       # files containing impacted functions

    # ── Execution paths ───────────────────────────────────────────────────────
    # GAP 3 FIX: traced call chains from changed functions down to their
    # deepest callers. Each path is an ordered list of "file::func" strings.
    # context_builder renders these as readable chains for the critic.
    #
    # Example:
    #   [
    #     ["api/upload.py::upload_endpoint",
    #      "services/ingest.py::process_document",
    #      "models/faiss.py::add_vectors"],          ← changed function at the end
    #
    #     ["services/ingest.py::process_document",
    #      "models/faiss.py::_migrate_to_id_map"],   ← another chain
    #   ]
    execution_paths: List[List[str]]

    # ── LLM context ───────────────────────────────────────────────────────────
    # Written by context_builder, read by critic as its main prompt payload.
    llm_context: str

    # ── Critic retry tracking ─────────────────────────────────────────────────
    # Managed by the conditional edge router in pipeline.py.
    retry_count: int    # increments on each critic retry — hard cap at 2

    # ── Final output ──────────────────────────────────────────────────────────
    # Written by critic, posted to GitHub by main.py.
    review: str         # formatted PR review markdown string