# """
# context_builder.py — Builds LLM context from impacted code

# This agent converts impact analysis into a prompt-ready context
# containing changed functions, impacted callers, and code snippets.
# """

# import logging
# from app.utils.graph_state import GraphState

# log = logging.getLogger("codelineage.context")


# def build_context(state: GraphState) -> GraphState:
#     """
#     Build LLM context from impacted files.

#     Reads:
#         state["graph"]
#         state["changed_functions"]
#         state["impacted_files"]

#     Writes:
#         state["llm_context"]
#     """

#     graph = state.get("graph", {})
#     changed_functions = state.get("changed_functions", [])
#     impacted_files = state.get("impacted_files", [])

#     context_parts = []

#     # ── Changed functions ─────────────────────────────
#     context_parts.append("CHANGED FUNCTIONS:")
#     for fn in changed_functions:
#         context_parts.append(f" - {fn}")

#     context_parts.append("\nIMPACTED FILES:")

#     # ── impacted code snippets ───────────────────────
#     for file_path in impacted_files:
#         file_data = graph.get(file_path, {})

#         context_parts.append(f"\nFILE: {file_path}")

#         functions = file_data.get("functions", {})

#         for func_name, func_info in functions.items():
#             context_parts.append(f"\nFunction: {func_name}")

#             # optional: calls
#             calls = func_info.get("calls", [])
#             if calls:
#                 context_parts.append(f"Calls: {calls}")

#     llm_context = "\n".join(context_parts)

#     state["llm_context"] = llm_context

#     log.info(
#         f"Context built — {len(impacted_files)} files "
#         f"({len(llm_context)} chars)"
#     )

#     return state
"""
context_builder.py — LangGraph Node 3

Builds the full LLM prompt context from the knowledge graph.
Transforms raw graph data into a structured text blob the critic can reason over.

State reads:
    state["graph"]               — full knowledge graph
    state["pr_diff"]             — raw git diffs with patches
    state["changed_files"]       — paths of changed files
    state["changed_functions"]   — function names that changed
    state["impacted_files"]      — files with callers of changed functions
    state["impacted_functions"]  — function names of those callers

State writes:
    state["llm_context"]         — complete context string for critic
"""

import logging

from langsmith import traceable

from app.utils.graph_state import GraphState

log = logging.getLogger("codelineage.context")


@traceable(name="context_builder")
def build_context(state: GraphState) -> GraphState:
    """
    LangGraph node — assembles the critic's prompt context.

    Sections in the output string:
        1. CHANGED FILES     — git patch for each changed file
        2. CHANGED FUNCTIONS — signature + calls + return type
        3. IMPACTED FILES    — functions in those files + their call sites
        4. DB MODELS         — any models touched by changed files
        5. API ROUTES        — routes whose handlers are in impacted files
    """
    graph = state.get("graph", {})
    pr_diff = state.get("pr_diff", [])
    changed_files = state.get("changed_files", [])
    changed_functions = state.get("changed_functions", [])
    impacted_files = state.get("impacted_files", [])

    parts: list[str] = []

    # ── Section 1: Git diff patches ─────────────────────────────────────────
    parts.append("=" * 60)
    parts.append("GIT DIFF — WHAT CHANGED IN THIS PR")
    parts.append("=" * 60)

    for diff_entry in pr_diff:
        file_path = diff_entry.get("file_path", "")
        status = diff_entry.get("status", "modified")
        patch = diff_entry.get("patch", "")
        parts.append(f"\n[{status.upper()}] {file_path}")
        if patch:
            parts.append(patch)
        else:
            parts.append("(no patch available — file may be binary or newly added)")

    # ── Section 2: Changed function signatures ───────────────────────────────
    parts.append("\n" + "=" * 60)
    parts.append("CHANGED FUNCTIONS — FULL SIGNATURES")
    parts.append("=" * 60)

    for file_path in changed_files:
        file_data = graph.get(file_path, {})

        # top-level functions
        for func_name, func_info in file_data.get("functions", {}).items():
            parts.append(_format_function(func_name, func_info, file_path))

        # class methods
        for class_name, class_data in file_data.get("classes", {}).items():
            for method_name, method_info in class_data.get("methods", {}).items():
                full_name = f"{class_name}.{method_name}"
                parts.append(_format_function(full_name, method_info, file_path))

    if not changed_files:
        parts.append("(no Python functions found in changed files)")

    # ── Section 3: Impacted callers ──────────────────────────────────────────
    parts.append("\n" + "=" * 60)
    parts.append("IMPACTED FILES — FUNCTIONS THAT CALL CHANGED CODE")
    parts.append("=" * 60)
    parts.append(
        "These functions call one or more changed functions.\n"
        "If the changed function's signature or behaviour changed,\n"
        "these callers may break at runtime.\n"
    )

    for file_path in impacted_files:
        file_data = graph.get(file_path, {})
        parts.append(f"\nFILE: {file_path}")

        for func_name, func_info in file_data.get("functions", {}).items():
            calls = func_info.get("calls", [])
            # only show this function if it actually calls something changed
            calls_changed = [c for c in calls if c in changed_functions]
            if calls_changed:
                parts.append(_format_function(func_name, func_info, file_path))
                parts.append(f"  ↳ calls changed: {calls_changed}")

        for class_name, class_data in file_data.get("classes", {}).items():
            for method_name, method_info in class_data.get("methods", {}).items():
                calls = method_info.get("calls", [])
                calls_changed = [c for c in calls if c in changed_functions]
                if calls_changed:
                    full_name = f"{class_name}.{method_name}"
                    parts.append(_format_function(full_name, method_info, file_path))
                    parts.append(f"  ↳ calls changed: {calls_changed}")

    if not impacted_files:
        parts.append("(no callers found outside changed files)")

    # ── Section 4: DB models touched ────────────────────────────────────────
    db_models_found = {}
    for file_path in changed_files:
        file_data = graph.get(file_path, {})
        for model_name, model_info in file_data.get("db_models", {}).items():
            db_models_found[model_name] = (file_path, model_info)

    if db_models_found:
        parts.append("\n" + "=" * 60)
        parts.append("DB MODELS — CHANGED OR TOUCHED BY CHANGED CODE")
        parts.append("=" * 60)
        for model_name, (file_path, model_info) in db_models_found.items():
            fields = model_info.get("fields", [])
            parts.append(f"\nModel: {model_name}  ({file_path}:{model_info.get('line_number','')})")
            parts.append(f"  Fields: {fields}")

    # ── Section 5: API routes in impacted files ──────────────────────────────
    routes_found = {}
    all_relevant_files = list(set(changed_files + impacted_files))
    for file_path in all_relevant_files:
        file_data = graph.get(file_path, {})
        for route_key, route_info in file_data.get("api_routes", {}).items():
            routes_found[route_key] = (file_path, route_info)

    if routes_found:
        parts.append("\n" + "=" * 60)
        parts.append("API ROUTES — IN CHANGED OR IMPACTED FILES")
        parts.append("=" * 60)
        parts.append(
            "If the handler function's signature changed, the route contract may break.\n"
        )
        for route_key, (file_path, route_info) in routes_found.items():
            handler = route_info.get("handler", "unknown")
            line = route_info.get("line_number", "?")
            parts.append(f"  {route_key}  →  {handler}()  [{file_path}:{line}]")

    llm_context = "\n".join(parts)

    state["llm_context"] = llm_context

    log.info(
        f"Context built — "
        f"{len(changed_files)} changed files, "
        f"{len(impacted_files)} impacted files, "
        f"{len(llm_context)} chars total"
    )

    return state


# ── Helpers ──────────────────────────────────────────────────────────────────

def _format_function(name: str, info: dict, file_path: str) -> str:
    """
    Formats a single function's info into a readable block.

    Output example:
        Function: get_user  [services/user.py:12]
          args:       user_id, db
          returns:    User
          calls:      db.query, serialize_user
          called_by:  delete_user, UserService.update_user
          decorators: login_required
    """
    args = ", ".join(info.get("args", []))
    returns = info.get("returns", "unknown")
    calls = info.get("calls", [])
    called_by = info.get("called_by", [])
    decorators = info.get("decorators", [])
    line = info.get("line_number", "?")

    lines = [
        f"\nFunction: {name}  [{file_path}:{line}]",
        f"  args:       {args or '(none)'}",
        f"  returns:    {returns}",
        f"  calls:      {', '.join(calls) or '(none)'}",
        f"  called_by:  {', '.join(called_by) or '(none)'}",
    ]
    if decorators:
        lines.append(f"  decorators: {', '.join(decorators)}")

    return "\n".join(lines)