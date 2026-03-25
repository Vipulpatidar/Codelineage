"""
impact_analyzer.py — LangGraph Node 2

Reads the knowledge graph + changed files from state.

CHANGES v2 (gap fixes):
    GAP 1 — diff-aware changed function detection
        Previously every function in a changed file was flagged as "changed"
        even if the diff didn't touch it.  Now we parse changed_lines from
        the PR diff hunk headers and only flag functions whose line range
        overlaps the actual diff.

    GAP 2 — file-qualified function keys
        Previously caller matching used bare function names, so two files
        both defining add_vectors() would both be flagged.
        Now every function is keyed as "file_path::func_name".
        The bare names list (changed_functions) is still written for
        backward-compat with context_builder display sections.

    GAP 3 — execution path tracing
        After finding impacted callers, we trace every path from an
        entry-point caller all the way down to the changed function,
        producing execution_paths: List[List[str]].
        context_builder renders these chains so the critic can name the
        exact crash point (e.g. "api/upload.py → ingest.py:38 → faiss.py:103").

State reads:
    state["graph"]           — full knowledge graph from github_tool
    state["changed_files"]   — list of .py paths changed in this PR
    state["changed_lines"]   — { file_path: [line_numbers] } from diff parser
    state["pr_diff"]         — raw diff list (used to build changed_lines if absent)

State writes:
    state["changed_functions"]            — bare names (display use)
    state["qualified_changed_functions"]  — "file::func" keys (matching use)
    state["impacted_functions"]           — callers of changed functions
    state["impacted_files"]               — files containing those callers
    state["execution_paths"]              — traced call chains (Gap 3)
"""

import logging
import re
from typing import Dict, List, Set, Tuple

from langsmith import traceable

from app.utils.graph_state import GraphState

log = logging.getLogger("codelineage.impact")

# Maximum depth for execution path tracing — prevents infinite loops
# in recursive codebases and keeps context size sane.
_MAX_PATH_DEPTH = 8


# ─────────────────────────────────────────────────────────────────────────────
# DIFF LINE PARSER
# ─────────────────────────────────────────────────────────────────────────────

def _parse_changed_lines(pr_diff: List[dict]) -> Dict[str, List[int]]:
    """
    GAP 1 FIX — Parse git patch hunk headers to find which line numbers
    actually changed in each file.

    Git unified diff hunk header format:
        @@ -old_start,old_count +new_start,new_count @@

    We care about the NEW file's line numbers (+new_start) because the
    knowledge graph is built from the new (post-PR) version of the file.

    Returns:
        { "services/ingest.py": [34, 35, 36, 38, 39], ... }
    """
    result: Dict[str, List[int]] = {}

    for diff_entry in pr_diff:
        file_path = diff_entry.get("file_path", "")
        patch = diff_entry.get("patch", "")

        if not patch:
            continue

        changed: List[int] = []
        current_line = 0

        for raw_line in patch.splitlines():
            # hunk header: @@ -a,b +c,d @@
            hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
            if hunk_match:
                current_line = int(hunk_match.group(1))
                continue

            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                # added or modified line in the new file
                changed.append(current_line)
                current_line += 1
            elif raw_line.startswith("-") and not raw_line.startswith("---"):
                # deleted line — does not advance new-file line counter
                pass
            else:
                # context line
                current_line += 1

        if changed:
            result[file_path] = changed

    return result


# ─────────────────────────────────────────────────────────────────────────────
# FUNCTION RANGE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _function_line_range(func_info: dict) -> Tuple[int, int]:
    """
    Returns (start_line, end_line) for a function from the graph.

    ast_tool stores line_number (start) and end_line_number.
    If end_line_number is absent we estimate start + body_line_count
    or fall back to start + 50 as a conservative guess.
    """
    start = func_info.get("line_number", 0)
    end   = func_info.get("end_line_number", 0)
    if not end:
        end = start + func_info.get("body_lines", 50)
    return start, end


def _func_touches_diff(func_info: dict, diff_lines: List[int]) -> bool:
    """
    Returns True if any changed line falls within the function's line range.
    """
    if not diff_lines:
        return False
    start, end = _function_line_range(func_info)
    diff_set = set(diff_lines)
    return any(start <= ln <= end for ln in diff_set)


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTION PATH TRACER
# ─────────────────────────────────────────────────────────────────────────────

def _trace_execution_paths(
    graph: Dict,
    qualified_changed: Set[str],
    changed_files: List[str],
) -> List[List[str]]:
    """
    GAP 3 FIX — Traces every call path that leads into a changed function.

    Strategy (reverse BFS from changed functions upward through callers):
        1. For each changed function F (as "file::func"), find all callers C.
        2. For each caller C, find all callers of C.
        3. Continue until we reach a function with no callers (entry point)
           or hit _MAX_PATH_DEPTH.
        4. Record each complete path as [entry_point, ..., changed_function].

    Paths are stored in reverse order (entry → changed) so the critic
    can read them as natural execution flow.

    Returns:
        List of paths, each path being ["file::func", "file::func", ...]
        ordered from caller → callee → changed function.
    """
    # Build reverse lookup: "file::func" → list of "file::func" that call it
    # (i.e. who calls this function)
    callers_of: Dict[str, List[str]] = {}

    for file_path, file_data in graph.items():
        # top-level functions
        for func_name, func_info in file_data.get("functions", {}).items():
            key = f"{file_path}::{func_name}"
            calls = func_info.get("calls", [])
            for callee_bare in calls:
                # resolve callee bare name to qualified key
                for qkey in qualified_changed:
                    qfile, qfunc = qkey.split("::", 1)
                    if callee_bare == qfunc or callee_bare == qkey:
                        callers_of.setdefault(qkey, []).append(key)

        # class methods
        for class_name, class_data in file_data.get("classes", {}).items():
            for method_name, method_info in class_data.get("methods", {}).items():
                key = f"{file_path}::{class_name}.{method_name}"
                calls = method_info.get("calls", [])
                for callee_bare in calls:
                    for qkey in qualified_changed:
                        qfile, qfunc = qkey.split("::", 1)
                        if callee_bare == qfunc or callee_bare == qkey:
                            callers_of.setdefault(qkey, []).append(key)

    # Also scan every function in the graph for callers of impacted functions
    # (not just changed functions) — we want the full chain up to entry points.
    # Build a general "who calls what" map for path extension.
    all_callers_of: Dict[str, List[str]] = {}
    for file_path, file_data in graph.items():
        for func_name, func_info in file_data.get("functions", {}).items():
            key = f"{file_path}::{func_name}"
            for callee_bare in func_info.get("calls", []):
                # find the qualified callee in the graph
                for fp2, fd2 in graph.items():
                    if callee_bare in fd2.get("functions", {}):
                        callee_key = f"{fp2}::{callee_bare}"
                        all_callers_of.setdefault(callee_key, []).append(key)
                    for cn2, cd2 in fd2.get("classes", {}).items():
                        if callee_bare in cd2.get("methods", {}):
                            callee_key = f"{fp2}::{cn2}.{callee_bare}"
                            all_callers_of.setdefault(callee_key, []).append(key)

    # BFS: start from each changed function and walk upward through callers
    paths: List[List[str]] = []
    visited_paths: Set[Tuple[str, ...]] = set()

    for changed_key in qualified_changed:
        # DFS upward from this changed function
        stack: List[List[str]] = [[changed_key]]

        while stack:
            current_path = stack.pop()

            if len(current_path) > _MAX_PATH_DEPTH:
                # emit what we have — too deep to keep going
                path_tuple = tuple(reversed(current_path))
                if path_tuple not in visited_paths:
                    visited_paths.add(path_tuple)
                    paths.append(list(path_tuple))
                continue

            tip = current_path[0]  # we prepend callers, so tip is current top
            upstream = all_callers_of.get(tip, [])

            if not upstream:
                # tip has no callers — it's an entry point, emit the path
                reversed_path = list(reversed(current_path))
                path_tuple = tuple(reversed_path)
                if path_tuple not in visited_paths:
                    visited_paths.add(path_tuple)
                    paths.append(reversed_path)
            else:
                for caller_key in upstream:
                    # avoid cycles
                    if caller_key not in current_path:
                        stack.append([caller_key] + current_path)

    # Sort by path length descending so longest (most specific) paths appear first
    paths.sort(key=len, reverse=True)

    # Deduplicate sub-paths: if path A is a suffix of path B, drop A
    unique_paths: List[List[str]] = []
    for path in paths:
        path_str = "::".join(path)
        is_subpath = any(
            "::".join(other).endswith(path_str)
            for other in unique_paths
            if other != path
        )
        if not is_subpath:
            unique_paths.append(path)

    return unique_paths[:20]  # cap at 20 paths to avoid bloating context


# ─────────────────────────────────────────────────────────────────────────────
# MAIN NODE
# ─────────────────────────────────────────────────────────────────────────────

@traceable(name="impact_analyzer")
def analyze_impact(state: GraphState) -> GraphState:
    """
    LangGraph node — determines blast radius of changed code.

    Three-pass approach:
        Pass 1: parse changed_lines from pr_diff (or use pre-parsed value).
        Pass 2: collect ONLY the functions whose line range overlaps the diff
                (not every function in every changed file).
        Pass 3: find callers using file-qualified keys to avoid name collisions.
        Pass 4: trace execution paths from entry points down to changed functions.
    """
    graph        = state.get("graph", {})
    changed_files = state.get("changed_files", [])
    pr_diff      = state.get("pr_diff", [])

    # ── Pass 1: build changed_lines if not already in state ──────────────────
    changed_lines: Dict[str, List[int]] = state.get("changed_lines", {})
    if not changed_lines and pr_diff:
        changed_lines = _parse_changed_lines(pr_diff)
        state["changed_lines"] = changed_lines
        log.info(f"Parsed changed_lines for {len(changed_lines)} files from pr_diff")

    # ── Pass 2: collect ACTUALLY changed functions ────────────────────────────
    # GAP 1 FIX: only flag functions whose body overlaps the diff lines.
    # GAP 2 FIX: use "file::func" qualified keys to prevent name collisions.
    qualified_changed: Set[str] = set()
    bare_changed: Set[str] = set()

    for file_path in changed_files:
        file_data   = graph.get(file_path, {})
        diff_lines  = changed_lines.get(file_path, [])

        # If we have no diff line info for this file (e.g. newly added file),
        # fall back to flagging all functions in it — conservative but safe.
        no_diff_info = not diff_lines

        # top-level functions
        for func_name, func_info in file_data.get("functions", {}).items():
            if no_diff_info or _func_touches_diff(func_info, diff_lines):
                qkey = f"{file_path}::{func_name}"
                qualified_changed.add(qkey)
                bare_changed.add(func_name)
                log.debug(f"  Changed function: {qkey}")

        # class methods
        for class_name, class_data in file_data.get("classes", {}).items():
            for method_name, method_info in class_data.get("methods", {}).items():
                if no_diff_info or _func_touches_diff(method_info, diff_lines):
                    qkey      = f"{file_path}::{class_name}.{method_name}"
                    qkey_bare = f"{file_path}::{method_name}"
                    qualified_changed.add(qkey)
                    qualified_changed.add(qkey_bare)
                    bare_changed.add(method_name)
                    bare_changed.add(f"{class_name}.{method_name}")
                    log.debug(f"  Changed method: {qkey}")

    log.info(
        f"Diff-aware changed functions: {len(bare_changed)} bare names, "
        f"{len(qualified_changed)} qualified keys"
    )

    # ── Pass 3: find callers using qualified keys ─────────────────────────────
    # GAP 2 FIX: match callee names against BOTH the bare name AND the
    # "file::func" qualified key so aliased imports are caught too.
    impacted_functions: Set[str] = set()
    impacted_files: Set[str]     = set()

    for file_path, file_data in graph.items():
        if file_path in changed_files:
            continue

        # top-level functions
        for func_name, func_info in file_data.get("functions", {}).items():
            calls = set(func_info.get("calls", []))

            # check against bare names (most common case)
            if calls & bare_changed:
                impacted_functions.add(func_name)
                impacted_files.add(file_path)
                log.debug(f"  Impacted: {func_name} in {file_path}")
                continue

            # check qualified calls (catches "module.function" style calls)
            for call in calls:
                for qkey in qualified_changed:
                    if qkey.endswith(f"::{call}") or call == qkey:
                        impacted_functions.add(func_name)
                        impacted_files.add(file_path)
                        break

        # class methods
        for class_name, class_data in file_data.get("classes", {}).items():
            for method_name, method_info in class_data.get("methods", {}).items():
                calls     = set(method_info.get("calls", []))
                full_name = f"{class_name}.{method_name}"

                if calls & bare_changed:
                    impacted_functions.add(full_name)
                    impacted_files.add(file_path)
                    log.debug(f"  Impacted method: {full_name} in {file_path}")
                    continue

                for call in calls:
                    for qkey in qualified_changed:
                        if qkey.endswith(f"::{call}") or call == qkey:
                            impacted_functions.add(full_name)
                            impacted_files.add(file_path)
                            break

    # Also harvest pre-computed called_by links from changed functions
    for file_path in changed_files:
        file_data = graph.get(file_path, {})

        for func_name, func_info in file_data.get("functions", {}).items():
            qkey = f"{file_path}::{func_name}"
            if qkey not in qualified_changed:
                continue
            for caller_name in func_info.get("called_by", []):
                for fp, fd in graph.items():
                    if fp in changed_files:
                        continue
                    if caller_name in fd.get("functions", {}):
                        impacted_functions.add(caller_name)
                        impacted_files.add(fp)
                    for cn, cd in fd.get("classes", {}).items():
                        if caller_name in cd.get("methods", {}):
                            impacted_functions.add(f"{cn}.{caller_name}")
                            impacted_files.add(fp)

    # ── Pass 4: trace execution paths ────────────────────────────────────────
    # GAP 3 FIX: build full call chains from entry points down to changed funcs
    execution_paths = _trace_execution_paths(graph, qualified_changed, changed_files)
    log.info(f"Traced {len(execution_paths)} execution paths")

    # ── Write results ─────────────────────────────────────────────────────────
    state["changed_functions"]           = sorted(bare_changed)
    state["qualified_changed_functions"] = sorted(qualified_changed)
    state["impacted_functions"]          = sorted(impacted_functions)
    state["impacted_files"]              = sorted(impacted_files)
    state["execution_paths"]             = execution_paths

    log.info(
        "Impact analysis complete — "
        f"{len(bare_changed)} changed funcs (diff-aware), "
        f"{len(impacted_functions)} impacted funcs, "
        f"{len(impacted_files)} impacted files, "
        f"{len(execution_paths)} execution paths"
    )

    return state