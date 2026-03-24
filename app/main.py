# import hashlib
# import hmac
# import json
# import logging
# import os

# from dotenv import load_dotenv
# from fastapi import FastAPI, Header, HTTPException, Request
# from app.agents.context_builder import build_context
# from app.tools.github_tool import fetch_pr_and_build_graph, post_pr_comment

# # NEW — pipeline imports
# from app.utils.graph_state import GraphState
# from app.agents.impact_analyzer import analyze_impact
# from app.agents.critic import run_critic

# load_dotenv()

# # ── Logging ────────────────────────────────────────────────────────────────────
# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s  %(levelname)-8s  %(message)s",
#     datefmt="%H:%M:%S",
# )
# log = logging.getLogger("codelineage")

# # ── App ────────────────────────────────────────────────────────────────────────
# app = FastAPI(title="CodeLineage", version="0.1.0")

# WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")


# # ── Signature verification ─────────────────────────────────────────────────────
# def verify_signature(payload: bytes, signature: str) -> bool:
#     if not WEBHOOK_SECRET:
#         log.warning("GITHUB_WEBHOOK_SECRET not set — skipping signature check (dev mode)")
#         return True

#     expected = "sha256=" + hmac.new(
#         WEBHOOK_SECRET.encode(), payload, hashlib.sha256
#     ).hexdigest()

#     return hmac.compare_digest(expected, signature)


# # ── Health check ───────────────────────────────────────────────────────────────
# @app.get("/")
# async def root():
#     return {"status": "CodeLineage is running"}


# # ── Webhook endpoint ───────────────────────────────────────────────────────────
# @app.post("/webhook")
# async def github_webhook(
#     request: Request,
#     x_github_event: str = Header(...),
#     x_hub_signature_256: str = Header(default=""),
# ):
#     payload_bytes = await request.body()

#     # ── Verify signature ───────────────────────────────────────────────────────
#     if not verify_signature(payload_bytes, x_hub_signature_256):
#         log.error("Webhook signature mismatch — request rejected")
#         raise HTTPException(status_code=401, detail="Invalid signature")

#     payload = json.loads(payload_bytes)

#     log.info(f"Received GitHub event: {x_github_event!r}")

#     if x_github_event == "ping":
#         log.info("Ping received — webhook connected successfully ✓")
#         return {"message": "pong"}

#     if x_github_event == "pull_request":
#         return await handle_pull_request(payload)

#     log.info(f"Ignoring event: {x_github_event!r}")
#     return {"message": f"Event '{x_github_event}' ignored"}


# # ── PR handler ─────────────────────────────────────────────────────────────────
# async def handle_pull_request(payload: dict) -> dict:
#     action = payload.get("action")

#     if action not in ("opened", "synchronize"):
#         log.info(f"PR action '{action}' — nothing to do")
#         return {"message": f"PR action '{action}' ignored"}

#     pr = payload["pull_request"]
#     repo = payload["repository"]

#     # ── Build pr_context ──────────────────────────────────────────────────────
#     pr_context = {
#         "repo_name": repo["full_name"],
#         "pr_number": pr["number"],
#         "pr_title": pr["title"],
#         "base_branch": pr["base"]["ref"],
#         "head_branch": pr["head"]["ref"],
#         "head_sha": pr["head"]["sha"],
#         "author": pr["user"]["login"],
#         "changed_files": pr.get("changed_files", 0),
#         "additions": pr.get("additions", 0),
#         "deletions": pr.get("deletions", 0),
#     }

#     log.info(
#         f"PR #{pr_context['pr_number']} — {pr_context['pr_title']!r}\n"
#         f"  repo:    {pr_context['repo_name']}\n"
#         f"  author:  {pr_context['author']}\n"
#         f"  action:  {action}\n"
#         f"  sha:     {pr_context['head_sha'][:8]}\n"
#         f"  changes: +{pr_context['additions']} -{pr_context['deletions']} "
#         f"across {pr_context['changed_files']} file(s)"
#     )

#     # ── Step 1: Fetch PR + build knowledge graph ──────────────────────────────
#     log.info("STEP 1 — fetching PR and building graph")

#     result = await fetch_pr_and_build_graph(pr_context)

#     log.info(
#         "STEP 1 — graph ready\n"
#         f"  files indexed: {len(result['graph'])}\n"
#         f"  changed files: {result['changed_files']}"
#     )

#     # ── Step 2: Build pipeline state ─────────────────────────────────────────
#     log.info("STEP 2 — building pipeline state")

#     state: GraphState = {
#         "pr_context": pr_context,
#         "graph": result["graph"],
#         "changed_files": result["changed_files"],
#     }

#     try:
#         # ── Step 3: Impact Analysis ──────────────────────────────────────────
#         log.info("STEP 3 — impact analysis start")

#         state = analyze_impact(state)

#         log.info(
#             "STEP 3 — impact analysis complete\n"
#             f"  changed funcs: {len(state.get('changed_functions', []))}\n"
#             f"  impacted funcs: {len(state.get('impacted_functions', []))}\n"
#             f"  impacted files: {len(state.get('impacted_files', []))}"
#         )

#         # ── Step 4: Context Builder ──────────────────────────────────────────
#         log.info("STEP 4 — context builder start")

#         state = build_context(state)

#         context = state.get("context", "")
#         log.info(f"STEP 4 — context built (chars: {len(context)})")

#         # ── Step 5: LLM Critic ───────────────────────────────────────────────
#         log.info("STEP 5 — critic start")

#         state = await run_critic(state)

#         log.info("STEP 5 — critic finished")

#         review = state.get("review", "")
#         log.info(f"STEP 5 — review generated (chars: {len(review)})")

#         # ── Step 6: Post GitHub Comment ──────────────────────────────────────
#         log.info("STEP 6 — posting PR comment")

#         await post_pr_comment(
#             pr_context,
#             review
#         )

#         log.info("STEP 6 — PR comment posted")

#         log.info(
#             "AI REVIEW\n"
#             f"{review}"
#         )

#     except Exception:
#         log.exception("PIPELINE FAILED")
#         raise

#     return {
#         "message": "PR processed",
#         "pr": pr_context,
#         "files_indexed": len(result["graph"]),
#         "changed_files": result["changed_files"],
#         "impacted_files": state.get("impacted_files", []),
#     }

"""
main.py — FastAPI webhook entry point for CodeLineage

Flow per PR event:
    1. Receive GitHub webhook (PR opened / synchronize)
    2. Verify HMAC signature
    3. Build pr_context dict from payload
    4. Call github_tool.fetch_pr_and_build_graph() — builds knowledge graph
       (runs BEFORE the LangGraph pipeline because it's heavy async I/O)
    5. Seed initial GraphState with graph + diff + changed_files
    6. Run LangGraph pipeline (impact → context → critic → [retry?])
    7. Post final review comment to GitHub PR
"""

import hashlib
import hmac
import json
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

from app.agents.pipeline import pipeline
from app.tools.github_tool import fetch_pr_and_build_graph, post_pr_comment
from app.utils.graph_state import GraphState

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("codelineage.main")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="CodeLineage", version="0.2.0")

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")


# ── Signature verification ────────────────────────────────────────────────────

def verify_signature(payload: bytes, signature: str) -> bool:
    """Validates the X-Hub-Signature-256 header from GitHub."""
    if not WEBHOOK_SECRET:
        log.warning("GITHUB_WEBHOOK_SECRET not set — skipping check (dev mode)")
        return True

    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "CodeLineage is running", "version": "0.2.0"}


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.post("/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str = Header(default=""),
):
    payload_bytes = await request.body()

    if not verify_signature(payload_bytes, x_hub_signature_256):
        log.error("Webhook signature mismatch — request rejected")
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(payload_bytes)
    log.info(f"GitHub event: {x_github_event!r}")

    if x_github_event == "ping":
        log.info("Ping — webhook connected ✓")
        return {"message": "pong"}

    if x_github_event == "pull_request":
        return await handle_pull_request(payload)

    log.info(f"Ignoring event: {x_github_event!r}")
    return {"message": f"Event '{x_github_event}' ignored"}


# ── PR handler ─────────────────────────────────────────────────────────────────

async def handle_pull_request(payload: dict) -> dict:
    """
    Orchestrates the full CodeLineage pipeline for a PR event.

    Steps:
        1. Extract PR metadata into pr_context
        2. Build knowledge graph (pre-graph async I/O step)
        3. Seed GraphState
        4. Run LangGraph pipeline via ainvoke()
        5. Post review comment to GitHub
    """
    action = payload.get("action")

    if action not in ("opened", "synchronize"):
        log.info(f"PR action '{action}' — nothing to do")
        return {"message": f"PR action '{action}' ignored"}

    pr   = payload["pull_request"]
    repo = payload["repository"]

    # ── Step 1: Build pr_context ─────────────────────────────────────────────
    pr_context = {
        "repo_name":    repo["full_name"],
        "pr_number":    pr["number"],
        "pr_title":     pr["title"],
        "base_branch":  pr["base"]["ref"],
        "head_branch":  pr["head"]["ref"],
        "head_sha":     pr["head"]["sha"],
        "author":       pr["user"]["login"],
        "changed_files": pr.get("changed_files", 0),
        "additions":    pr.get("additions", 0),
        "deletions":    pr.get("deletions", 0),
    }

    log.info(
        f"PR #{pr_context['pr_number']} — {pr_context['pr_title']!r}\n"
        f"  repo:    {pr_context['repo_name']}\n"
        f"  author:  {pr_context['author']}\n"
        f"  action:  {action}\n"
        f"  sha:     {pr_context['head_sha'][:8]}\n"
        f"  changes: +{pr_context['additions']} -{pr_context['deletions']} "
        f"across {pr_context['changed_files']} file(s)"
    )

    # ── Step 2: Build knowledge graph ─────────────────────────────────────────
    # Runs before LangGraph — fetches files from GitHub and runs AST parser.
    # Returns graph, pr_diff, changed_files.
    log.info("STEP 1 — fetching PR and building knowledge graph")

    indexer_result = await fetch_pr_and_build_graph(pr_context)

    log.info(
        f"STEP 1 done — "
        f"{len(indexer_result['graph'])} files indexed, "
        f"{len(indexer_result['changed_files'])} Python file(s) changed"
    )

    # ── Step 3: Seed initial GraphState ───────────────────────────────────────
    initial_state: GraphState = {
        "pr_context":    pr_context,
        "graph":         indexer_result["graph"],
        "pr_diff":       indexer_result["pr_diff"],
        "changed_files": indexer_result["changed_files"],
        "retry_count":   0,
    }

    # ── Step 4: Run LangGraph pipeline ────────────────────────────────────────
    # pipeline.ainvoke() runs:
    #   impact_analyzer → context_builder → critic → [self-correction?] → END
    log.info("STEP 2 — running LangGraph pipeline")
    log.info("  nodes: impact → context → critic → (conditional retry) → END")

    try:
        final_state: GraphState = await pipeline.ainvoke(initial_state)

    except Exception:
        log.exception("LangGraph pipeline failed")
        raise

    review = final_state.get("review", "")
    retry_count = final_state.get("retry_count", 0)

    log.info(
        f"STEP 2 done — "
        f"review generated ({len(review)} chars), "
        f"retries used: {retry_count}"
    )

    # ── Step 5: Post review comment to GitHub ─────────────────────────────────
    log.info("STEP 3 — posting PR review comment")

    await post_pr_comment(pr_context, review)

    log.info("STEP 3 done — PR comment posted ✓")

    # ── Response ──────────────────────────────────────────────────────────────
    return {
        "message":        "PR processed",
        "pr_number":      pr_context["pr_number"],
        "repo":           pr_context["repo_name"],
        "files_indexed":  len(indexer_result["graph"]),
        "changed_files":  indexer_result["changed_files"],
        "impacted_files": final_state.get("impacted_files", []),
        "retries_used":   retry_count,
        "review_length":  len(review),
    }