"""
context_builder.py — LangGraph Node 3

Builds the full LLM prompt context from the knowledge graph.
Transforms raw graph data into a structured text blob the critic can reason over.

CHANGES v2 (gap fixes):
    GAP 3 — Section 6: EXECUTION PATHS
        Renders the traced call chains from impact_analyzer into a readable
        format.  The critic now sees "api/upload.py → ingest.py:38 →
        faiss.py:103 [CHANGED]" instead of a flat list of impacted files.

    GAP 4 — code snippets in sections 2 and 3
        Functions now include their source_lines from the graph (if
        ast_tool stored them) so the critic can see the actual body,
        not just the signature.  This lets it reason about helper
        functions like _migrate_to_id_map() that were not in the diff.

    GAP 5 — alias-aware caller display
        context_builder still shows callers by name match but now also
        shows the qualified key so the critic can see which file each
        caller lives in unambiguously.

State reads:
    state["graph"]                        — full knowledge graph
    state["pr_diff"]                      — raw git diffs with patches
    state["changed_files"]                — paths of changed files
    state["changed_functions"]            — bare function names that changed
    state["qualified_changed_functions"]  — "file::func" keys
    state["impacted_files"]               — files with callers of changed functions
    state["impacted_functions"]           — function names of those callers
    state["execution_paths"]              — traced call chains (from impact_analyzer)

State writes:
    state["llm_context"]    — complete context string for critic
"""

import logging

from langsmith import traceable

from app.utils.graph_state import GraphState

log = logging.getLogger("codelineage.context")

# Max source lines to include per function body — keeps context size bounded
_MAX_SNIPPET_LINES = 40


@traceable(name="context_builder")
def build_context(state: GraphState) -> GraphState:
    """
    LangGraph node — assembles the critic's prompt context.

    Sections in the output string:
        1. CHANGED FILES          — git patch for each changed file
        2. CHANGED FUNCTIONS      — signature + source snippet
        3. IMPACTED CALLERS       — functions that call changed code + snippets
        4. DB MODELS              — models touched by changed files
        5. API ROUTES             — routes in changed/impacted files
        6. EXECUTION PATHS        — NEW: full call chains to crash points
    """
    graph                       = state.get("graph", {})
    pr_diff                     = state.get("pr_diff", [])
    changed_files               = state.get("changed_files", [])
    changed_functions           = state.get("changed_functions", [])
    qualified_changed_functions = state.get("qualified_changed_functions", [])
    impacted_files              = state.get("impacted_files", [])
    execution_paths             = state.get("execution_paths", [])

    parts: list[str] = []

    # ── Section 1: Git diff patches ──────────────────────────────────────────
    parts.append("=" * 60)
    parts.append("GIT DIFF — WHAT CHANGED IN THIS PR")
    parts.append("=" * 60)

    for diff_entry in pr_diff:
        file_path = diff_entry.get("file_path", "")
        status    = diff_entry.get("status", "modified")
        patch     = diff_entry.get("patch", "")
        parts.append(f"\n[{status.upper()}] {file_path}")
        if patch:
            parts.append(patch)
        else:
            parts.append("(no patch available — file may be binary or newly added)")

    # ── Section 2: Changed function signatures + snippets ───────────────────
    # GAP 4 FIX: include source_lines so critic sees actual body, not just sig.
    parts.append("\n" + "=" * 60)
    parts.append("CHANGED FUNCTIONS — SIGNATURES AND SOURCE")
    parts.append("=" * 60)
    parts.append(
        "These functions were directly modified by the PR diff.\n"
        "Source code is shown so the critic can reason about edge cases\n"
        "inside the body, not just the signature.\n"
    )

    for file_path in changed_files:
        file_data = graph.get(file_path, {})

        for func_name, func_info in file_data.get("functions", {}).items():
            parts.append(_format_function(func_name, func_info, file_path, show_snippet=True))

        for class_name, class_data in file_data.get("classes", {}).items():
            for method_name, method_info in class_data.get("methods", {}).items():
                full_name = f"{class_name}.{method_name}"
                parts.append(_format_function(full_name, method_info, file_path, show_snippet=True))

    if not changed_files:
        parts.append("(no Python functions found in changed files)")

    # ── Section 3: Impacted callers + snippets ───────────────────────────────
    # GAP 4 FIX: show caller source too — critic needs to see what arguments
    # the caller passes to detect signature-break issues.
    parts.append("\n" + "=" * 60)
    parts.append("IMPACTED CALLERS — FUNCTIONS THAT CALL CHANGED CODE")
    parts.append("=" * 60)
    parts.append(
        "These functions call one or more changed functions.\n"
        "If the changed function's signature or behaviour changed,\n"
        "these callers may break at runtime.\n"
        "Source code shown so the critic can see exactly what args are passed.\n"
    )

    for file_path in impacted_files:
        file_data = graph.get(file_path, {})
        parts.append(f"\nFILE: {file_path}")

        for func_name, func_info in file_data.get("functions", {}).items():
            calls         = func_info.get("calls", [])
            calls_changed = [c for c in calls if c in changed_functions]
            if calls_changed:
                parts.append(
                    _format_function(func_name, func_info, file_path, show_snippet=True)
                )
                parts.append(f"  ↳ calls changed: {calls_changed}")

        for class_name, class_data in file_data.get("classes", {}).items():
            for method_name, method_info in class_data.get("methods", {}).items():
                calls         = method_info.get("calls", [])
                calls_changed = [c for c in calls if c in changed_functions]
                if calls_changed:
                    full_name = f"{class_name}.{method_name}"
                    parts.append(
                        _format_function(full_name, method_info, file_path, show_snippet=True)
                    )
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
            parts.append(
                f"\nModel: {model_name}  "
                f"({file_path}:{model_info.get('line_number', '')})"
            )
            parts.append(f"  Fields: {fields}")

    # ── Section 5: API routes ────────────────────────────────────────────────
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
            line    = route_info.get("line_number", "?")
            parts.append(f"  {route_key}  →  {handler}()  [{file_path}:{line}]")

    # ── Section 6: Execution paths ───────────────────────────────────────────
    # GAP 3 FIX: the most important new section.
    # Renders traced call chains so the critic can name exact crash points.
    #
    # Example output:
    #
    #   Path 1 (3 hops):
    #     [1] api/upload.py::upload_endpoint
    #          ↓ calls process_document()
    #     [2] services/ingest.py::process_document
    #          ↓ calls add_vectors()
    #     [3] models/faiss.py::add_vectors   ◄ CHANGED
    #
    parts.append("\n" + "=" * 60)
    parts.append("EXECUTION PATHS — CALL CHAINS REACHING CHANGED CODE")
    parts.append("=" * 60)
    parts.append(
        "Each path shows how a top-level caller eventually reaches a changed function.\n"
        "The critic should identify the SPECIFIC LINE in each path where a runtime\n"
        "error will occur, not just the changed function itself.\n"
        "Functions marked ◄ CHANGED are the ones modified by this PR.\n"
    )

    if not execution_paths:
        parts.append("(no execution paths traced — check that the knowledge graph has call data)")
    else:
        # Build a set of just the function-name portion of qualified keys
        # for quick "is this changed?" lookup during path rendering.
        changed_bare = set(changed_functions)
        changed_qset = set(qualified_changed_functions)

        for idx, path in enumerate(execution_paths, 1):
            hop_count = len(path)
            parts.append(f"\nPath {idx} ({hop_count} hop{'s' if hop_count != 1 else ''}):")

            for step_idx, qualified_key in enumerate(path):
                # qualified_key format: "file/path.py::FuncName" or "file/path.py::Class.method"
                if "::" in qualified_key:
                    file_part, func_part = qualified_key.split("::", 1)
                else:
                    file_part = "?"
                    func_part = qualified_key

                is_changed = (
                    qualified_key in changed_qset
                    or func_part in changed_bare
                )
                changed_marker = "   ◄ CHANGED" if is_changed else ""

                # Get line number from graph for this function
                line_no = _get_function_line(graph, file_part, func_part)
                line_str = f":{line_no}" if line_no else ""

                parts.append(
                    f"  [{step_idx + 1}] {file_part}{line_str}  →  {func_part}(){changed_marker}"
                )

                # Show the call arrow between steps
                if step_idx < len(path) - 1:
                    next_key = path[step_idx + 1]
                    next_func = next_key.split("::", 1)[-1] if "::" in next_key else next_key
                    parts.append(f"       ↓ calls {next_func}()")

    llm_context = "\n".join(parts)
    state["llm_context"] = llm_context

    log.info(
        f"Context built — "
        f"{len(changed_files)} changed files, "
        f"{len(impacted_files)} impacted files, "
        f"{len(execution_paths)} execution paths, "
        f"{len(llm_context)} chars total"
    )

    return state


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_function_line(graph: dict, file_path: str, func_name: str) -> int:
    """Returns line_number for a function from the graph, or 0 if not found."""
    file_data = graph.get(file_path, {})

    # top-level function
    func_info = file_data.get("functions", {}).get(func_name)
    if func_info:
        return func_info.get("line_number", 0)

    # class method (func_name may be "ClassName.method")
    if "." in func_name:
        class_name, method_name = func_name.split(".", 1)
        class_data = file_data.get("classes", {}).get(class_name, {})
        method_info = class_data.get("methods", {}).get(method_name)
        if method_info:
            return method_info.get("line_number", 0)

    return 0


def _format_function(
    name: str,
    info: dict,
    file_path: str,
    show_snippet: bool = False,
) -> str:
    """
    Formats a single function's info into a readable block.

    GAP 4 FIX: if show_snippet=True and the graph has source_lines stored,
    append up to _MAX_SNIPPET_LINES lines of actual source code.

    Output example:
        Function: get_user  [services/user.py:12]
          args:       user_id, db
          returns:    User
          calls:      db.query, serialize_user
          called_by:  delete_user, UserService.update_user
          decorators: login_required

          Source:
            def get_user(user_id: int, db: Session) -> User:
                return db.query(User).filter(User.id == user_id).first()
    """
    args       = ", ".join(info.get("args", []))
    returns    = info.get("returns", "unknown")
    calls      = info.get("calls", [])
    called_by  = info.get("called_by", [])
    decorators = info.get("decorators", [])
    line       = info.get("line_number", "?")

    lines = [
        f"\nFunction: {name}  [{file_path}:{line}]",
        f"  args:       {args or '(none)'}",
        f"  returns:    {returns}",
        f"  calls:      {', '.join(calls) or '(none)'}",
        f"  called_by:  {', '.join(called_by) or '(none)'}",
    ]
    if decorators:
        lines.append(f"  decorators: {', '.join(decorators)}")

    # GAP 4 FIX: add source snippet when available
    if show_snippet:
        source_lines = info.get("source_lines", [])
        if source_lines:
            snippet = source_lines[:_MAX_SNIPPET_LINES]
            if len(source_lines) > _MAX_SNIPPET_LINES:
                snippet.append(f"  ... ({len(source_lines) - _MAX_SNIPPET_LINES} more lines)")
            lines.append("\n  Source:")
            for src_line in snippet:
                lines.append(f"    {src_line}")

    return "\n".join(lines)