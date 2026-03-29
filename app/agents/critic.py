"""
critic.py — LangGraph Node 4

The reasoning core of CodeLineage.
Uses Gemini 2.5 Flash to analyze the full pipeline context and produce
a structured PR review. Implements a self-correction loop via LangGraph
conditional edges.

CHANGES v3 (gap fixes from honest assessment):
    GAP A — Return value contract changes (new item 9b in review prompt)
        The most-missed class of production bug: a function changes what it
        *guarantees* about its output (uniqueness, sort order, non-None) and
        downstream consumers silently depend on that guarantee.  Added explicit
        checklist item covering enumerate/index consumers and data mutation paths.

    GAP B — Retry checklist items G and H
        Added two new edge-case questions to _build_edge_case_checklist():
          G) RETURN VALUE CONSUMERS — does any consumer index/enumerate the
             return value and use position to identify records?
          H) DATA MUTATION PATHS — does the return value feed a DELETE/UPDATE?

    GAP C — Confidence scorer: enumerate/consumer heuristic
        score_review_confidence() now penalises reviews that ignore
        enumerate/indexing patterns when the context contains them.

    GAP D — Hardcoded API key removed
        The fallback value in genai.Client() was a real-looking key in source.
        Now fails loudly if GEMINI_API_KEY env var is absent.

    GAP 6 — prompt now explicitly asks for execution path analysis
        Added item 10 to the check list: "trace the exact execution path
        to the crash point and name the file + line number."
        Added a dedicated EXECUTION PATH ANALYSIS section in the output format.

    GAP 7 — assumption / constant validation step
        Added item 11: "validate numeric constants and magic values."

    GAP 8 — retry prompt injects NEW information
        The self-correction pass now receives the execution_paths and a
        structured edge-case checklist that was NOT in the first pass.

State reads:
    state["llm_context"]         — full context string from context_builder
    state["changed_functions"]   — list of changed function names
    state["impacted_files"]      — list of impacted file paths
    state["execution_paths"]     — traced call chains
    state["pr_context"]          — PR metadata (repo, number, author)
    state["retry_count"]         — how many times we've already retried

State writes:
    state["review"]              — formatted markdown PR review comment
    state["retry_count"]         — unchanged here; pipeline.py increments on retry
"""

import logging
import os

from google import genai
from langsmith import traceable

from app.utils.graph_state import GraphState

log = logging.getLogger("codelineage.critic")

# GAP D FIX: remove hardcoded fallback key — fail loudly if env var is absent.
_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY","")
if not _GEMINI_API_KEY:
    raise ValueError(
        "GEMINI_API_KEY environment variable is not set. "
        "Set it before starting the server."
    )

_client = genai.Client(api_key=_GEMINI_API_KEY)
MODEL   = "gemini-2.5-flash"


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_review_prompt(state: GraphState) -> str:
    """
    Builds the main review prompt sent to Gemini.

    CHANGES v3:
        - Added item 9b: return value contract changes (the most-missed bug class).
        - Added item 10: execution path tracing to exact crash point.
        - Added item 11: constant / magic value validation.
        - Added EXECUTION PATH ANALYSIS section to output format.
        - Added ASSUMPTION CHECKS section to output format.
    """
    llm_context       = state.get("llm_context", "(no context built)")
    pr_context        = state.get("pr_context", {})
    changed_functions = state.get("changed_functions", [])
    impacted_files    = state.get("impacted_files", [])
    execution_paths   = state.get("execution_paths", [])

    path_summary = _render_path_summary(execution_paths)

    return f"""You are CodeLineage, an expert Python PR reviewer with deep knowledge of
dependency analysis, runtime breakage, and API contract violations.

You have been given:
1. The git diff showing exactly what changed in this PR
2. The full signatures AND source code of every changed function
3. Every function in the codebase that calls those changed functions (with source)
4. DB models touched by the change
5. API routes in affected files
6. Traced execution paths from entry points down to changed functions

PR metadata:
  repo:    {pr_context.get("repo_name", "unknown")}
  PR #:    {pr_context.get("pr_number", "?")}
  author:  {pr_context.get("author", "unknown")}
  title:   {pr_context.get("pr_title", "unknown")}

Changed functions: {changed_functions}
Impacted files:    {impacted_files}

Execution path summary:
{path_summary}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CODEBASE CONTEXT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{llm_context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Analyze the above and produce a thorough PR review.

Look specifically for:
1.  Function signature changes that break callers
    (removed arg, changed arg type, changed return type)
2.  Callers that pass arguments the changed function no longer accepts
3.  DB model field changes that break ORM queries upstream
4.  API route handler signature changes that break the route contract
5.  Missing pre-conditions — argument that must be pre-loaded before calling
    this function but is no longer passed
6.  N+1 query patterns introduced by the change
7.  Security regressions — decorator removed (@login_required, @permission_classes)
8.  Data shape mismatches between layers (model → service → route)
9.  Side effects that callers unknowingly depended on

9b. RETURN VALUE CONTRACT CHANGES (this is the most-missed class of production bug):
    If a changed function's return value changes its *shape* or *guarantees*
    (e.g. was always unique, now may contain duplicates; was always sorted,
    now unordered; was always non-None, now may return None; was a dict,
    now a list):
    - Find EVERY consumer of this return value in the codebase.
    - Ask: does the consumer ASSUME the old guarantee?
    - Follow the data through ALL layers: what gets BUILT from this return value?
      Think: indices, maps, citation numbers, UI labels, source references,
      delete targets, foreign keys — anything constructed from the returned items.
    - CRITICAL: if any downstream code uses enumerate(), zip(), or positional
      indexing ([i], [i+1]) on the returned data to build an identifier map
      (e.g. source_map = {{str(i+1): chunk for i, chunk in enumerate(results)}}),
      then duplicate items in the return value will cause SILENT DATA CORRUPTION:
      two different identifiers will point to the same underlying record, and
      any operation that uses those identifiers to target a specific record
      (view, edit, delete) will silently target the WRONG record.
    - This class of bug raises NO exception and produces NO visible error —
      it only surfaces as wrong data or wrong deletes in production.
    - Give a clear REJECT verdict if this pattern is present.

10. EXECUTION PATH ANALYSIS (most important):
    For each execution path listed above, trace the chain step by step.
    Identify the SPECIFIC LINE NUMBER where a runtime error will occur.
    Do not just say "this will fail" — say:
      "services/ingest.py:38 calls add_vectors() which now expects 768-dim
       input, but the FAISS index loaded at models/faiss.py:55 is 384-dim,
       so models/faiss.py:103 will raise a dimension mismatch at runtime."
    Every critical finding MUST name: file, line number, error type.

11. ASSUMPTION / CONSTANT VALIDATION:
    Check every numeric constant, magic value, or config change in the diff.
    Ask: is this a valid value for its purpose?
    Examples:
      - Embedding dimensions must be powers of 2 (128, 256, 384, 512, 768, 1024).
        Any other value (e.g. 786) is almost certainly a typo.
      - Port numbers must be in 1–65535.
      - Batch sizes should be powers of 2 for GPU efficiency.
      - Timeout values should be positive integers.
    Flag any constant that does not meet these validity constraints.

Format your review EXACTLY as follows (markdown):

## CodeLineage Review

### Execution Path Analysis
<!-- For each traced execution path, show the crash point: -->
**Path: [entry_file] → ... → [changed_file]**
Crash point: `[file.py:LINE]` — [explain exactly what fails and why].
Triggered by: [which call in which caller passes the breaking value].

If no paths were traced, write "No execution paths traced."

### Critical Issues
<!-- For each critical finding: -->
**[file.py:LINE]** `function_name()` — clear one-line description of the break.
**Impact:** which callers are affected and how they will fail.
**Suggestion:** specific code fix.

### Assumption Violations
<!-- Constants or magic values that are likely wrong: -->
**[file.py:LINE]** `CONSTANT_NAME = value` — why this value is invalid.
**Expected:** what the valid range or set of values is.
**Suggestion:** correct value or validation to add.

### Warnings
<!-- Same format, for non-breaking but risky changes -->

### Info
<!-- Low-risk observations worth noting -->

### Summary
One paragraph summarising the overall risk of this PR.
Include a clear verdict on the final line:
  **Verdict: APPROVE** / **Verdict: APPROVE WITH CONDITIONS** / **Verdict: REJECT**
State the primary reason for a REJECT verdict in one sentence.

Rules:
- Be specific — always name the file, line number, function, and caller.
- Do not invent issues that are not supported by the context above.
- If a section has no findings, write "None found." under that heading.
- Prefer specific crash explanations over vague warnings.
- Return value contract violations that enable silent data corruption MUST be
  listed as Critical Issues, not Warnings, and MUST trigger a REJECT verdict.
"""


def _build_retry_prompt(state: GraphState, previous_review: str) -> str:
    """
    Builds a self-correction prompt for retry passes.

    GAP 8 FIX: the retry pass now injects NEW information that was NOT
    in the first pass — an explicit edge-case checklist and a structured
    question about each execution path.
    """
    llm_context     = state.get("llm_context", "")
    retry_count     = state.get("retry_count", 0)
    execution_paths = state.get("execution_paths", [])

    edge_case_checklist = _build_edge_case_checklist(state)
    path_questions      = _build_path_questions(execution_paths)

    return f"""You are CodeLineage performing a self-correction pass (attempt {retry_count}).

You previously produced this review:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{previous_review}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SELF-CORRECTION STEP 1 — Challenge your existing findings:
For each Critical Issue and Warning in your review:
- Is the caller ACTUALLY broken, or does it already handle the change?
- Is the argument truly missing, or is it passed under a different name?
- Is the return type change actually consumed by the caller?
- Could this be a false positive?
Remove findings you cannot confirm. Demote "Critical" to "Warning" if
the caller has error handling around the call site.

SELF-CORRECTION STEP 2 — NEW edge-case checklist (not in your first pass):
Answer each question explicitly before producing your corrected review.

{edge_case_checklist}

SELF-CORRECTION STEP 3 — Execution path deep dive (not in your first pass):
For each execution path below, answer: where exactly does it crash?
Name file + line number. If you said it crashes somewhere in your first
review, verify that line number is correct against the source shown.

{path_questions}

SELF-CORRECTION STEP 4 — Constant validation:
List every numeric constant or magic value changed in this PR.
For each one: is it a valid value for its purpose?
(Embedding dims = powers of 2, ports = 1-65535, etc.)

Here is the original codebase context for reference:
{llm_context}

Now produce a corrected and more precise review in the same format as before.
Only include findings you are confident are real issues.
Every critical finding must name file, line number, and the exact error type.
"""


def _render_path_summary(execution_paths: list) -> str:
    """Renders execution paths as a compact bulleted list for the prompt header."""
    if not execution_paths:
        return "  (no execution paths traced)"

    lines = []
    for idx, path in enumerate(execution_paths[:10], 1):
        hops = " → ".join(
            (p.split("::", 1)[-1] if "::" in p else p)
            for p in path
        )
        lines.append(f"  Path {idx}: {hops}")
    if len(execution_paths) > 10:
        lines.append(f"  ... and {len(execution_paths) - 10} more paths (in context below)")
    return "\n".join(lines)


def _build_edge_case_checklist(state: GraphState) -> str:
    """
    GAP 8 FIX: Builds NEW edge-case questions not present in the first pass.
    GAP B FIX: Added items G (return value consumers) and H (data mutation paths).
    """
    checklist = """Edge case questions to answer explicitly:

A) COLD START: If there is NO existing state/index/cache on a fresh deployment,
   does the changed code still work correctly? Or does it assume pre-existing data?

B) MIGRATION: If old data exists (from before this PR), does the new code handle it?
   Look for: loading old serialised files, reading old DB rows, parsing old formats.

C) PARTIAL DEPLOYMENT: If only some instances are updated (rolling deploy),
   could old and new code conflict? E.g. one instance writes new format,
   another reads it expecting old format.

D) CONCURRENT ACCESS: Does any changed function access shared state
   (in-memory cache, singleton, global variable)?  Could two concurrent
   requests produce a race condition after this change?

E) ERROR PROPAGATION: If the changed function raises an exception, does the
   caller catch it?  Does the caller's caller?  Where does it surface?

F) CONSTANT VALIDITY: List every numeric constant or config value changed.
   Is each one a valid value for its type?

G) RETURN VALUE CONSUMERS: For each changed function, find everywhere its
   return value is consumed downstream. Ask:
   - Is the return value passed to enumerate(), zip(), or indexed with [i]?
   - Is a dict or map built from it using positional keys (e.g. str(i+1))?
   - Is any identifier derived from position in the list used to target a
     specific record for viewing, editing, or deletion?
   If yes: changing the return value's cardinality (e.g. removing deduplication,
   allowing duplicates) causes SILENT DATA CORRUPTION — two identifiers will
   refer to the same record. This will NOT raise an exception. It will only
   appear as wrong data or wrong deletes in production.
   This is a CRITICAL issue and warrants a REJECT verdict.

H) DATA MUTATION PATHS: Does the changed function's return value (directly or
   after processing) feed a DELETE, UPDATE, or INSERT operation anywhere in
   the call graph? Trace the full path:
   changed_function() → consumer() → ... → db.delete() / remove_vectors() / etc.
   If yes: verify that the data shape at the mutation point is still correct.
   A duplicate or misidentified record at a delete call corrupts or destroys data."""

    return checklist


def _build_path_questions(execution_paths: list) -> str:
    """Builds per-path crash-point questions for the retry prompt."""
    if not execution_paths:
        return "  (no paths to analyse)"

    lines = []
    for idx, path in enumerate(execution_paths[:5], 1):
        hops = " → ".join(
            (p.split("::", 1)[-1] if "::" in p else p)
            for p in path
        )
        lines.append(f"Path {idx}: {hops}")
        lines.append(f"  Q: Which exact line in which file in this chain raises the error?")
        lines.append(f"  Q: What is the error type (TypeError, ValueError, RuntimeError, etc.)?")
        lines.append(f"  Q: Does any function in this chain catch or swallow that error?")
        lines.append(f"  Q: Does any function in this chain build a map/index from the return")
        lines.append(f"     value using enumerate() or positional keys?  If so, name the file")
        lines.append(f"     and line, and explain what breaks if the list contains duplicates.")
        lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN NODE
# ─────────────────────────────────────────────────────────────────────────────

@traceable(name="critic")
async def run_critic(state: GraphState) -> GraphState:
    """
    LangGraph node — calls Gemini to produce the PR review.

    On first call (retry_count == 0): runs the main review prompt.
    On retry (retry_count > 0): runs the self-correction prompt which
    injects NEW edge-case information not present in the first pass.
    """
    import asyncio

    retry_count     = state.get("retry_count", 0)
    previous_review = state.get("review", "")

    if retry_count == 0 or not previous_review:
        log.info("Critic — first pass, running full review prompt")
        prompt = _build_review_prompt(state)
    else:
        log.info(f"Critic — retry pass {retry_count}, running enriched self-correction prompt")
        prompt = _build_retry_prompt(state, previous_review)

    log.info(f"Calling Gemini {MODEL} (prompt length: {len(prompt)} chars)")

    try:
        response = await asyncio.to_thread(
            _client.models.generate_content,
            model=MODEL,
            contents=prompt,
        )
        review_text = response.text

    except Exception as e:
        log.error(f"Gemini call failed: {e}")
        review_text = (
            "## CodeLineage Review\n\n"
            f"⚠️ Review generation failed: {e}\n\n"
            "Please review this PR manually."
        )

    log.info(f"Critic produced review ({len(review_text)} chars)")

    state["review"]      = review_text
    state["retry_count"] = retry_count  # pipeline.py increments on retry

    return state


# ─────────────────────────────────────────────────────────────────────────────
# CONFIDENCE SCORER
# ─────────────────────────────────────────────────────────────────────────────

def score_review_confidence(state: GraphState) -> float:
    """
    Heuristic confidence score for the current review.

    Used by the conditional edge in pipeline.py to decide:
        score < 0.7  → route back to critic for self-correction
        score >= 0.7 → proceed to post_comment

    CHANGES v3:
        GAP C FIX: penalises reviews that ignore enumerate/indexing patterns
        when the LLM context contains them — the most common signal that a
        return-value contract violation is present and was missed.

        GAP 10 FIX: also checks for execution path analysis section presence.
    """
    review          = state.get("review", "")
    execution_paths = state.get("execution_paths", [])
    llm_context     = state.get("llm_context", "")

    if not review or len(review) < 100:
        log.info("Confidence: 0.0 — review too short or empty")
        return 0.0

    score = 1.0
    review_lower = review.lower()

    # Check for surrender phrases
    low_confidence_phrases = [
        "i cannot", "i'm unable", "insufficient context",
        "not enough information", "cannot determine",
    ]
    for phrase in low_confidence_phrases:
        if phrase in review_lower:
            score -= 0.3
            log.info(f"Confidence: -0.3 for phrase '{phrase}'")
            break

    # Check for expected section headers
    required_sections = [
        "## codelineage review",
        "### critical",
        "### warnings",
    ]
    for section in required_sections:
        if section not in review_lower:
            score -= 0.1
            log.info(f"Confidence: -0.1 for missing section '{section}'")

    # Critical findings should reference file:line
    if "### critical" in review_lower:
        critical_block = review_lower.split("### critical")[1].split("###")[0]
        if "none found" not in critical_block:
            if ".py:" not in critical_block:
                score -= 0.1
                log.info("Confidence: -0.1 — critical findings lack file:line refs")

    # Review should include a verdict
    if "verdict:" not in review_lower:
        score -= 0.1
        log.info("Confidence: -0.1 — review has no verdict line")

    # GAP 10 FIX: if we have execution paths, the review should have path analysis
    if execution_paths:
        has_path_analysis = (
            "execution path" in review_lower
            or "path analysis" in review_lower
            or "crash point" in review_lower
        )
        if not has_path_analysis:
            score -= 0.2
            log.info(
                "Confidence: -0.2 — execution paths were provided but "
                "review has no path analysis section"
            )

    # GAP 7 FIX: if the review mentions no assumption checks and the diff
    # had numeric constants, penalise slightly
    if "### assumption" not in review_lower:
        score -= 0.05
        log.info("Confidence: -0.05 — no assumption violations section")

    # GAP C FIX: if the context contains enumerate/indexing patterns (a strong
    # signal that return-value consumer corruption may be present), the review
    # should address them.  Silence on this pattern is the most common way
    # data-integrity bugs slip through automated review.
    context_lower = llm_context.lower()
    has_indexing_pattern = (
        "enumerate(" in context_lower
        or "source_map" in context_lower
        or "str(i +" in context_lower
        or "str(i+" in context_lower
        or "[i + 1]" in context_lower
        or "[i+1]" in context_lower
    )
    if has_indexing_pattern:
        review_addresses_indexing = (
            "enumerate" in review_lower
            or "consumer" in review_lower
            or "source_map" in review_lower
            or "positional" in review_lower
            or "duplicate" in review_lower
        )
        if not review_addresses_indexing:
            score -= 0.15
            log.info(
                "Confidence: -0.15 — context has enumerate/indexing patterns "
                "but review does not address return-value consumer risk"
            )

    score = max(0.0, min(1.0, score))
    log.info(f"Review confidence score: {score:.2f}")
    return score
