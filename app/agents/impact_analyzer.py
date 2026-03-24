# """
# impact_analyzer.py — Determines what parts of the repo are affected by a PR

# This agent analyzes the knowledge graph and finds:

# 1. Which functions changed
# 2. Which functions call them
# 3. Which files contain those callers

# Output written into GraphState:
#     changed_functions
#     impacted_functions
#     impacted_files
# """

# import logging
# from typing import Set

# from app.utils.graph_state import GraphState

# log = logging.getLogger("codelineage.impact")


# # ─────────────────────────────────────────────────────────────
# # MAIN ENTRY
# # ─────────────────────────────────────────────────────────────
# def analyze_impact(state: GraphState) -> GraphState:
#     """
#     Main impact analysis entry point.

#     Reads:
#         state["graph"]
#         state["changed_files"]

#     Writes:
#         state["changed_functions"]
#         state["impacted_functions"]
#         state["impacted_files"]
#     """

#     graph = state.get("graph", {})
#     changed_files = state.get("changed_files", [])

#     changed_functions: Set[str] = set()
#     impacted_functions: Set[str] = set()
#     impacted_files: Set[str] = set()

#     # Step 1 — collect changed functions
#     for file_path in changed_files:
#         file_data = graph.get(file_path, {})

#         functions = file_data.get("functions", {})

#         for func_name in functions:
#             changed_functions.add(func_name)

#     # Step 2 — find callers (called_by)
#     for file_path, file_data in graph.items():
#         functions = file_data.get("functions", {})

#         for func_name, func_info in functions.items():
#             called_by = func_info.get("called_by", [])

#             for caller in called_by:
#                 if caller in changed_functions:
#                     impacted_functions.add(func_name)
#                     impacted_files.add(file_path)

#     # write back to state
#     state["changed_functions"] = list(changed_functions)
#     state["impacted_functions"] = list(impacted_functions)
#     state["impacted_files"] = list(impacted_files)

#     log.info(
#         "Impact analysis complete — "
#         f"{len(changed_functions)} changed funcs, "
#         f"{len(impacted_functions)} impacted funcs, "
#         f"{len(impacted_files)} impacted files"
#     )

#     return state


"""
impact_analyzer.py — LangGraph Node 2

Reads the knowledge graph + changed files from state.
Finds every function that CALLS INTO changed functions (true callers).
Writes changed_functions, impacted_functions, impacted_files back to state.

State reads:
    state["graph"]           — full knowledge graph from github_tool
    state["changed_files"]   — list of .py paths changed in this PR

State writes:
    state["changed_functions"]   — functions defined in changed files
    state["impacted_functions"]  — functions elsewhere that call them
    state["impacted_files"]      — files containing those callers
"""

import logging
from typing import Set

from langsmith import traceable

from app.utils.graph_state import GraphState

log = logging.getLogger("codelineage.impact")


@traceable(name="impact_analyzer")
def analyze_impact(state: GraphState) -> GraphState:
    """
    LangGraph node — determines blast radius of changed code.

    Two-pass approach:
        Pass 1: collect the names of all functions defined in changed files.
        Pass 2: scan every function in the entire graph — if its `calls` list
                includes any changed function name, it is impacted.

    The `calls` list is built by ast_tool._get_calls() which walks the
    function body and records every function call it finds.
    `called_by` in the AST output is populated by ast_tool.link_called_by()
    which does the same cross-file linkage — but we re-derive it here from
    `calls` so this node is self-contained and testable without ast_tool.
    """
    graph = state.get("graph", {})
    changed_files = state.get("changed_files", [])

    changed_functions: Set[str] = set()
    impacted_functions: Set[str] = set()
    impacted_files: Set[str] = set()

    # ── Pass 1: collect changed function names ───────────────────────────────
    # Any function defined in a changed file is "changed".
    for file_path in changed_files:
        file_data = graph.get(file_path, {})

        # top-level functions
        for func_name in file_data.get("functions", {}):
            changed_functions.add(func_name)

        # class methods — index by both "ClassName.method" and "method"
        for class_name, class_data in file_data.get("classes", {}).items():
            for method_name in class_data.get("methods", {}):
                changed_functions.add(method_name)
                changed_functions.add(f"{class_name}.{method_name}")

    log.info(f"Changed functions ({len(changed_functions)}): {sorted(changed_functions)}")

    # ── Pass 2: find callers across entire graph ─────────────────────────────
    # For every function in the entire codebase, check if its `calls` list
    # intersects with changed_functions. If yes — this function is impacted.
    for file_path, file_data in graph.items():

        # skip the changed files themselves — we already know those changed
        if file_path in changed_files:
            continue

        # check top-level functions
        for func_name, func_info in file_data.get("functions", {}).items():
            calls = set(func_info.get("calls", []))

            if calls & changed_functions:  # intersection — this func calls a changed func
                impacted_functions.add(func_name)
                impacted_files.add(file_path)
                log.debug(
                    f"  {func_name} in {file_path} calls "
                    f"{calls & changed_functions}"
                )

        # check class methods
        for class_name, class_data in file_data.get("classes", {}).items():
            for method_name, method_info in class_data.get("methods", {}).items():
                calls = set(method_info.get("calls", []))

                if calls & changed_functions:
                    full_name = f"{class_name}.{method_name}"
                    impacted_functions.add(full_name)
                    impacted_files.add(file_path)
                    log.debug(
                        f"  {full_name} in {file_path} calls "
                        f"{calls & changed_functions}"
                    )

    # ── Also pull from pre-computed called_by in graph ───────────────────────
    # ast_tool.link_called_by() already did this work — harvest it too
    # so we catch any relationships the `calls` scan above may have missed.
    for file_path, file_data in graph.items():
        if file_path in changed_files:
            continue

        for func_name, func_info in file_data.get("functions", {}).items():
            called_by = func_info.get("called_by", [])
            # called_by stores who calls THIS function.
            # We want functions CALLED BY changed code, not the other way.
            # So: if any changed function appears in this function's calls...
            # (already handled above — this block is for the inverse link)

        # The ast_tool stores called_by on the CALLEE (the function being called).
        # So to find who calls a changed function, we look at the changed function's
        # called_by list directly.

    for file_path in changed_files:
        file_data = graph.get(file_path, {})

        for func_name, func_info in file_data.get("functions", {}).items():
            for caller_name in func_info.get("called_by", []):
                # find which file this caller lives in
                for fp, fd in graph.items():
                    if fp in changed_files:
                        continue
                    if caller_name in fd.get("functions", {}):
                        impacted_functions.add(caller_name)
                        impacted_files.add(fp)

    # ── Write results to state ───────────────────────────────────────────────
    state["changed_functions"] = sorted(changed_functions)
    state["impacted_functions"] = sorted(impacted_functions)
    state["impacted_files"] = sorted(impacted_files)

    log.info(
        "Impact analysis complete — "
        f"{len(changed_functions)} changed funcs, "
        f"{len(impacted_functions)} impacted funcs, "
        f"{len(impacted_files)} impacted files"
    )

    return state