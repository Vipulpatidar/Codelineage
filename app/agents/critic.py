# import os
# import logging
# from google import genai

# from app.tools.github_tool import fetch_specific_files
# from .graph_context import collect_graph_context

# log = logging.getLogger("codelineage.critic")

# client = genai.Client(
#     api_key=os.getenv("GEMINI_API_KEY")
# )

# MODEL = "gemini-2.5-flash"
# MAX_AGENT_LOOPS = 3


# def build_prompt(context, state):

#     return f"""
# You are CodeLineage autonomous PR review agent.

# You analyze dependency-aware code changes.

# Changed Files:
# {state["changed_files"]}

# Impacted Files:
# {state.get("impacted")}

# Context:
# {context}

# If more files needed respond:

# REQUEST_FILES:
# file.py

# Else respond:

# FINAL_REVIEW:
# <review>
# """


# def extract_requested_files(text):

#     if "REQUEST_FILES:" not in text:
#         return []

#     part = text.split("REQUEST_FILES:")[1]

#     lines = part.strip().splitlines()

#     files = []
#     for l in lines:
#         l = l.strip()
#         if not l:
#             continue
#         if "FINAL" in l:
#             break
#         files.append(l)

#     return files


# def extract_final(text):

#     if "FINAL_REVIEW:" not in text:
#         return None

#     return text.split("FINAL_REVIEW:")[1].strip()


# async def run_critic(state: dict):

#     repo_context = ""

#     # ───────────────────────────────
#     # STEP 1: graph-aware fetch
#     # ───────────────────────────────
#     files = collect_graph_context(state)

#     log.info(f"Fetching graph-aware files: {files}")

#     fetched = fetch_specific_files(
#         state["pr_context"],
#         files
#     )

#     for f in fetched:
#         repo_context += f"\n\nFILE: {f['path']}\n"
#         repo_context += f["content"]

#     # ───────────────────────────────
#     # STEP 2: agent loop
#     # ───────────────────────────────
#     log.info("Graph-aware context built")
#     for i in range(MAX_AGENT_LOOPS):

#         log.info(f"Agent reasoning step {i+1}")

#         prompt = build_prompt(repo_context, state)

#         import asyncio
#         log.info("Calling Gemini...")
#         response = await asyncio.to_thread(
#             client.models.generate_content,
#             model=MODEL,
#             contents=prompt,
#         )

#         text = response.text

#         final = extract_final(text)
#         if final:
#             state["review"] = final
#             return state

#         requested = extract_requested_files(text)

#         if not requested:
#             state["review"] = text
#             return state

#         log.info(f"LLM requested additional files: {requested}")

#         new_files = fetch_specific_files(
#             state["pr_context"],
#             requested
#         )

#         for f in new_files:
#             repo_context += f"\n\nFILE: {f['path']}\n"
#             repo_context += f["content"]

#     state["review"] = text
#     return state
"""
critic.py — LangGraph Node 4

The reasoning core of CodeLineage.
Uses Gemini 2.5 Flash to analyze the full pipeline context and produce
a structured PR review. Implements a self-correction loop via LangGraph
conditional edges: if the review is flagged as low-confidence it routes
back here with a retry_count increment (max 2 retries, enforced in pipeline.py).

State reads:
    state["llm_context"]         — full context string from context_builder
    state["changed_functions"]   — list of changed function names
    state["impacted_files"]      — list of impacted file paths
    state["pr_context"]          — PR metadata (repo, number, author)
    state["retry_count"]         — how many times we've already retried

State writes:
    state["review"]              — formatted markdown PR review comment
    state["retry_count"]         — incremented if this is a retry pass
"""

import logging
import os

from google import genai
from langsmith import traceable

from app.utils.graph_state import GraphState

log = logging.getLogger("codelineage.critic")

_client = genai.Client(api_key="AIzaSyAFMIMk3LMIS_djL4b3gXQcoYmW8JUOGp4")
MODEL = "gemini-2.5-flash"


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def _build_review_prompt(state: GraphState) -> str:
    """
    Builds the main review prompt sent to Gemini.

    Includes:
        - full llm_context from context_builder (diff + signatures + callers + models + routes)
        - explicit instructions on what to look for
        - structured output format
    """
    llm_context = state.get("llm_context", "(no context built)")
    pr_context = state.get("pr_context", {})
    changed_functions = state.get("changed_functions", [])
    impacted_files = state.get("impacted_files", [])

    return f"""You are CodeLineage, an expert Python PR reviewer with deep knowledge of
dependency analysis, runtime breakage, and API contract violations.

You have been given:
1. The git diff showing exactly what changed in this PR
2. The full signatures of every changed function (args, return types, calls)
3. Every function in the codebase that calls those changed functions
4. DB models touched by the change
5. API routes in affected files

PR metadata:
  repo:    {pr_context.get("repo_name", "unknown")}
  PR #:    {pr_context.get("pr_number", "?")}
  author:  {pr_context.get("author", "unknown")}
  title:   {pr_context.get("pr_title", "unknown")}

Changed functions: {changed_functions}
Impacted files:    {impacted_files}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CODEBASE CONTEXT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{llm_context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Analyze the above and produce a thorough PR review.

Look specifically for:
1. Function signature changes that break callers
   (removed arg, changed arg type, changed return type)
2. Callers that pass arguments the changed function no longer accepts
3. DB model field changes that break ORM queries upstream
4. API route handler signature changes that break the route contract
5. Missing pre-conditions — argument that must be pre-loaded before calling
   this function but is no longer passed
6. N+1 query patterns introduced by the change
7. Security regressions — decorator removed (@login_required, @permission_classes)
8. Data shape mismatches between layers (model → service → route)
9. Side effects that callers unknowingly depended on

Format your review EXACTLY as follows (markdown):

## CodeLineage Review

### Critical Issues
<!-- For each critical finding: -->
**[file.py:LINE]** `function_name()` — clear one-line description of the break.
**Impact:** which callers are affected and how they will fail.
**Suggestion:** specific code fix.

### Warnings
<!-- Same format, for non-breaking but risky changes -->

### Info
<!-- Low-risk observations worth noting -->

### Summary
One paragraph summarising the overall risk of this PR.

If there are no issues in a section, write "None found." under that heading.
Be specific — always name the file, line number, function, and caller.
Do not invent issues that are not supported by the context above.
"""


def _build_retry_prompt(state: GraphState, previous_review: str) -> str:
    """
    Builds a self-correction prompt for retry passes.

    On a retry the critic is shown its previous review and asked to
    challenge each finding — are these real breaks or false positives?
    """
    llm_context = state.get("llm_context", "")
    retry_count = state.get("retry_count", 0)

    return f"""You are CodeLineage performing a self-correction pass (attempt {retry_count}).

You previously produced this review:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{previous_review}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For each Critical Issue and Warning in your review, challenge yourself:
- Is the caller ACTUALLY broken, or does it already handle the change?
- Is the argument truly missing, or is it passed under a different name?
- Is the return type change actually consumed by the caller?
- Could this be a false positive?

Remove any findings you cannot confirm from the context.
Demote "Critical" to "Warning" if the caller has error handling.

Here is the original codebase context for reference:
{llm_context}

Produce a corrected review in the same format as before.
Only include findings you are confident are real issues.
"""


# ─────────────────────────────────────────────────────────────────────────────
# MAIN NODE
# ─────────────────────────────────────────────────────────────────────────────

@traceable(name="critic")
async def run_critic(state: GraphState) -> GraphState:
    """
    LangGraph node — calls Gemini to produce the PR review.

    On first call (retry_count == 0): runs the main review prompt.
    On retry (retry_count > 0): runs the self-correction prompt against
    the previous review stored in state["review"].

    The decision to retry is made by the conditional edge in pipeline.py,
    not here. This node just produces the review and updates retry_count.
    """
    import asyncio

    retry_count = state.get("retry_count", 0)
    previous_review = state.get("review", "")

    if retry_count == 0 or not previous_review:
        # ── First pass: full review ──────────────────────────────────────────
        log.info("Critic — first pass, running full review prompt")
        prompt = _build_review_prompt(state)
    else:
        # ── Retry pass: self-correction ──────────────────────────────────────
        log.info(f"Critic — retry pass {retry_count}, running self-correction prompt")
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

    state["review"] = review_text
    state["retry_count"] = retry_count  # pipeline.py will increment on retry

    return state


# ─────────────────────────────────────────────────────────────────────────────
# CONFIDENCE SCORER — used by the conditional edge router in pipeline.py
# ─────────────────────────────────────────────────────────────────────────────

def score_review_confidence(state: GraphState) -> float:
    """
    Heuristic confidence score for the current review.

    Used by the conditional edge in pipeline.py to decide:
        score < 0.7  → route back to critic for self-correction
        score >= 0.7 → proceed to post_comment

    Scoring rules (each deducts from 1.0):
        - Review is empty or very short          → 0.0 (immediate retry)
        - Contains "I cannot" / "insufficient"   → -0.3 (LLM gave up)
        - No structured sections found           → -0.2 (bad format)
        - Critical issues with no line numbers   → -0.1 (vague findings)
        - Review looks well-formed               → 1.0 baseline

    This is intentionally simple — the real quality gate is the
    self-correction prompt which asks the LLM to challenge itself.
    """
    review = state.get("review", "")

    if not review or len(review) < 100:
        log.info("Confidence: 0.0 — review too short or empty")
        return 0.0

    score = 1.0

    low_confidence_phrases = [
        "i cannot", "i'm unable", "insufficient context",
        "not enough information", "cannot determine",
    ]
    review_lower = review.lower()
    for phrase in low_confidence_phrases:
        if phrase in review_lower:
            score -= 0.3
            log.info(f"Confidence: -0.3 for phrase '{phrase}'")
            break

    # check for expected section headers
    required_sections = [
        "## codelineage review",
        "### critical",
        "### warnings",
    ]
    for section in required_sections:
        if section not in review_lower:
            score -= 0.1
            log.info(f"Confidence: -0.1 for missing section '{section}'")

    # critical findings should reference file:line
    if "### critical" in review_lower:
        critical_block = review_lower.split("### critical")[1].split("###")[0]
        if "none found" not in critical_block:
            # if there are critical findings they should have line numbers
            if ".py:" not in critical_block:
                score -= 0.1
                log.info("Confidence: -0.1 — critical findings lack file:line refs")

    score = max(0.0, min(1.0, score))
    log.info(f"Review confidence score: {score:.2f}")
    return score