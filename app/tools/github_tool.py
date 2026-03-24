# """
# github_tool.py — GitHub API client for CodeLineage

# Responsibility:
#     Fetches PR diff and repo files from GitHub API
#     Decides whether to build graph from scratch or update incrementally
#     Calls ast_tool.py to parse files
#     Calls graph.py to store and retrieve graphs

# Flow:
#     fetch_pr_and_build_graph(pr_context)
#         ↓
#     fetch PR diff        → which files changed?
#         ↓
#     check graph cache    → graph exists?
#         ↓ NO                        ↓ YES
#     fetch all .py files       fetch only changed files
#     build full graph          + their dependents
#         ↓                         ↓
#     parse with ast_tool       parse with ast_tool
#         ↓                         ↓
#     set_graph()               update_files()
#         ↓
#     return graph + changed_files + pr_diff

# Called by:
#     main.py → handle_pull_request()
# """

# import base64
# import logging
# import os

# from github import Github, GithubException
# from dotenv import load_dotenv

# from app.tools.ast_tool import build_knowledge_graph, parse_file
# from app.utils.graph import (
#     graph_exists,
#     get_graph,
#     set_graph,
#     update_files,
#     get_dependents,
# )

# load_dotenv()

# log = logging.getLogger("codelineage.github")


# # ══════════════════════════════════════════════════════════════════════════════
# # SECTION 1 — GITHUB CLIENT
# # ══════════════════════════════════════════════════════════════════════════════

# def _get_github_client() -> Github:
#     """
#     Creates and returns an authenticated GitHub client.

#     Uses GITHUB_TOKEN from .env.
#     PyGithub handles rate limiting and retries automatically.
#     """
#     token = os.getenv("GITHUB_TOKEN")

#     if not token:
#         raise ValueError(
#             "GITHUB_TOKEN not set in .env — "
#             "cannot make GitHub API calls"
#         )

#     return Github(token)


# # ══════════════════════════════════════════════════════════════════════════════
# # SECTION 2 — FETCH PR DIFF
# # ══════════════════════════════════════════════════════════════════════════════

# def fetch_pr_diff(pr_context: dict) -> list[dict]:
#     """
#     Fetches the list of files changed in a PR.

#     GitHub API: GET /repos/{owner}/{repo}/pulls/{pr_number}/files

#     Args:
#         pr_context: the dict built in handle_pull_request()
#             {
#                 repo_name:  "alice/my-repo",
#                 pr_number:  42,
#                 head_sha:   "abc123",
#                 ...
#             }

#     Returns:
#         list of changed files:
#         [
#             {
#                 file_path: "services/user.py",
#                 status:    "modified",      # added / modified / removed
#                 additions: 10,
#                 deletions: 3,
#                 patch:     "@@ -1,5 +1,8 @@..."  # raw git diff
#             },
#             ...
#         ]

#     We only return .py files — we ignore everything else
#     (templates, migrations, static files, config files etc.)
#     """
#     gh      = _get_github_client()
#     repo    = gh.get_repo(pr_context["repo_name"])
#     pr      = repo.get_pull(pr_context["pr_number"])

#     changed_files = []

#     for f in pr.get_files():

#         # only care about Python files
#         if not f.filename.endswith(".py"):
#             continue

#         # skip migrations — they're auto-generated and noisy
#         if "migrations" in f.filename:
#             continue

#         changed_files.append({
#             "file_path": f.filename,
#             "status":    f.status,        # added / modified / removed
#             "additions": f.additions,
#             "deletions": f.deletions,
#             "patch":     f.patch or "",   # raw git diff, None if binary
#         })

#     log.info(
#         f"PR #{pr_context['pr_number']} diff — "
#         f"{len(changed_files)} .py file(s) changed"
#     )

#     for f in changed_files:
#         log.info(
#             f"  {f['status']:8} {f['file_path']} "
#             f"(+{f['additions']} -{f['deletions']})"
#         )

#     return changed_files


# # ══════════════════════════════════════════════════════════════════════════════
# # SECTION 3 — FETCH REPO FILES
# # ══════════════════════════════════════════════════════════════════════════════

# def fetch_all_py_files(pr_context: dict) -> list[dict]:
#     """
#     Fetches the content of every .py file in the repo.

#     Two GitHub API calls:
#         1. GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1
#            → gets the full file tree (just paths, no content)

#         2. GET /repos/{owner}/{repo}/contents/{path}?ref={sha}
#            → gets content of each .py file one by one

#     Args:
#         pr_context: dict with repo_name and head_sha

#     Returns:
#         list of files:
#         [
#             {
#                 "path":    "services/user.py",
#                 "content": "def get_user(...):\n    ..."
#             },
#             ...
#         ]

#     Skips:
#         - non .py files
#         - migration files
#         - __pycache__ files
#         - test files (we can add these back in v2)
#     """
#     gh      = _get_github_client()
#     repo    = gh.get_repo(pr_context["repo_name"])
#     sha     = pr_context["head_sha"]

#     # Step 1 — get full file tree (paths only, very fast)
#     log.info(f"Fetching file tree for {pr_context['repo_name']}:{sha[:8]}")
#     tree = repo.get_git_tree(sha, recursive=True)

#     # filter to only .py files we care about
#     py_paths = [
#         item.path
#         for item in tree.tree
#         if (
#             item.path.endswith(".py")
#             and "migrations"    not in item.path
#             and "__pycache__"   not in item.path
#             and item.type == "blob"   # blob = file, tree = directory
#         )
#     ]

#     log.info(f"Found {len(py_paths)} .py files to fetch")

#     # Step 2 — fetch content of each file
#     files = []

#     for path in py_paths:
#         content = _fetch_file_content(repo, path, sha)

#         if content is None:
#             continue

#         files.append({
#             "path":    path,
#             "content": content,
#         })

#     log.info(f"Fetched {len(files)} files successfully")
#     return files


# def fetch_specific_files(pr_context: dict, file_paths: list[str]) -> list[dict]:
#     """
#     Fetches content of specific files only.

#     Used during incremental indexing — when graph already exists
#     we only need to re-fetch changed files + their dependents,
#     not the entire repo.

#     Args:
#         pr_context: dict with repo_name and head_sha
#         file_paths: list of specific paths to fetch
#                     e.g. ["services/user.py", "routes/user_routes.py"]

#     Returns:
#         same format as fetch_all_py_files()
#     """
#     gh   = _get_github_client()
#     repo = gh.get_repo(pr_context["repo_name"])
#     sha  = pr_context["head_sha"]

#     files = []

#     for path in file_paths:
#         content = _fetch_file_content(repo, path, sha)

#         if content is None:
#             continue

#         files.append({
#             "path":    path,
#             "content": content,
#         })

#     log.info(f"Fetched {len(files)} specific files")
#     return files


# def _fetch_file_content(repo, path: str, sha: str) -> str | None:
#     """
#     Fetches and decodes the content of a single file.

#     GitHub returns file content as base64 encoded string.
#     We decode it to plain text here.

#     Returns None if file cannot be fetched (deleted, too large, etc.)
#     """
#     try:
#         file_obj = repo.get_contents(path, ref=sha)

#         # GitHub returns content as base64
#         # decode to plain text string
#         content = base64.b64decode(file_obj.content).decode("utf-8")
#         return content

#     except GithubException as e:
#         log.warning(f"  [SKIP] Could not fetch {path}: {e.status} {e.data}")
#         return None

#     except Exception as e:
#         log.warning(f"  [SKIP] Error fetching {path}: {e}")
#         return None


# # ══════════════════════════════════════════════════════════════════════════════
# # SECTION 4 — MAIN ENTRY POINT
# # ══════════════════════════════════════════════════════════════════════════════

# async def fetch_pr_and_build_graph(pr_context: dict) -> dict:
#     """
#     Main entry point — called by handle_pull_request() in main.py

#     Orchestrates the full flow:
#         1. fetch PR diff
#         2. check graph cache
#         3a. if no graph → fetch all files → build full graph → store
#         3b. if graph exists → fetch changed files + dependents → update graph
#         4. return everything LangGraph pipeline needs

#     Args:
#         pr_context: {
#             repo_name:  "alice/my-repo",
#             pr_number:  42,
#             head_sha:   "abc123sha",
#             ...
#         }

#     Returns:
#         {
#             graph:           complete knowledge graph,
#             pr_diff:         list of changed files with patches,
#             changed_files:   list of changed .py file paths only,
#         }
#     """
#     repo_name  = pr_context["repo_name"]
#     commit_sha = pr_context["head_sha"]

#     log.info(f"Starting graph build for {repo_name}:{commit_sha[:8]}")

#     # ── Step 1: fetch PR diff ─────────────────────────────────────────────────
#     # always fetch the diff regardless of cache state
#     # we need to know what changed in this specific PR
#     pr_diff = fetch_pr_diff(pr_context)

#     # extract just the file paths that changed
#     changed_file_paths = [f["file_path"] for f in pr_diff]

#     # ── Step 2: check cache ───────────────────────────────────────────────────
#     if not graph_exists(repo_name, commit_sha):

#         # ── Flow A: no graph yet — build from scratch ─────────────────────────
#         log.info("No cached graph — fetching entire repo")

#         # fetch every .py file in the repo
#         all_files = fetch_all_py_files(pr_context)

#         # parse all files and build complete knowledge graph
#         log.info(f"Building knowledge graph from {len(all_files)} files")
#         graph = build_knowledge_graph(all_files)

#         # store in cache
#         set_graph(repo_name, commit_sha, graph)

#     else:

#         # ── Flow B: graph exists — incremental update ─────────────────────────
#         log.info("Cached graph found — running incremental update")

#         graph = get_graph(repo_name, commit_sha)

#         # find all dependents of changed files
#         # e.g. services/user.py changed → routes/user_routes.py depends on it
#         # so we need to re-parse routes/user_routes.py too
#         files_to_reparse = set(changed_file_paths)

#         for file_path in changed_file_paths:
#             dependents = get_dependents(repo_name, commit_sha, file_path)

#             if dependents:
#                 log.info(
#                     f"  {file_path} has {len(dependents)} dependent(s): "
#                     f"{dependents}"
#                 )
#                 files_to_reparse.update(dependents)

#         log.info(
#             f"Re-parsing {len(files_to_reparse)} file(s) "
#             f"({len(changed_file_paths)} changed + "
#             f"{len(files_to_reparse) - len(changed_file_paths)} dependents)"
#         )

#         # fetch only those specific files
#         updated_raw = fetch_specific_files(pr_context, list(files_to_reparse))

#         # parse each one individually
#         updated_parsed = {}
#         for f in updated_raw:
#             parsed = parse_file(f["content"], f["path"])
#             updated_parsed[f["path"]] = parsed

#         # update only those nodes in the graph
#         graph = update_files(repo_name, commit_sha, updated_parsed)

#     # ── Step 3: return everything LangGraph needs ─────────────────────────────
#     log.info(
#         f"Graph ready — {len(graph)} files indexed, "
#         f"{len(changed_file_paths)} file(s) changed in this PR"
#     )

#     return {
#         "graph":         graph,
#         "pr_diff":       pr_diff,
#         "changed_files": changed_file_paths,
#     }
    
# async def post_pr_comment(pr_context: dict, comment: str):
#     """
#     Post review comment back to GitHub PR
#     """

#     import httpx

#     token = os.getenv("GITHUB_TOKEN")

#     repo = pr_context["repo_name"]
#     pr_number = pr_context["pr_number"]

#     url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"

#     headers = {
#         "Authorization": f"Bearer {token}",
#         "Accept": "application/vnd.github+json",
#     }

#     payload = {
#         "body": comment
#     }

#     # ✅ log comment
#     log.info(
#         f"Posting PR comment to {repo}#{pr_number}\n"
#         f"{'-'*60}\n"
#         f"{comment}\n"
#         f"{'-'*60}"
#     )

#     async with httpx.AsyncClient() as client:
#         resp = await client.post(url, headers=headers, json=payload)

#     if resp.status_code >= 300:
#         log.error(
#             f"Failed posting PR comment: {resp.status_code} {resp.text}"
#         )
#         raise RuntimeError(
#             f"Failed to post PR comment: {resp.status_code} {resp.text}"
#         )

#     # ✅ success log
#     log.info(
#         f"Successfully posted PR comment to {repo}#{pr_number}"
#     )
#     """
#     Post review comment back to GitHub PR
#     """

#     import httpx

#     token = os.getenv("GITHUB_TOKEN")

#     repo = pr_context["repo_name"]
#     pr_number = pr_context["pr_number"]

#     url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"

#     headers = {
#         "Authorization": f"Bearer {token}",
#         "Accept": "application/vnd.github+json",
#     }

#     payload = {
#         "body": comment
#     }

#     async with httpx.AsyncClient() as client:
#         resp = await client.post(url, headers=headers, json=payload)

#     if resp.status_code >= 300:
#         raise RuntimeError(
#             f"Failed to post PR comment: {resp.status_code} {resp.text}"
#         )    
"""
github_tool.py — GitHub API client for CodeLineage

Responsibility:
    Fetches PR diff and repo files from GitHub API.
    Decides whether to build the knowledge graph from scratch
    or update it incrementally (only changed files + their dependents).
    Calls ast_tool.py to parse files into graph nodes.
    Calls graph.py to store and retrieve cached graphs.
    Posts the final review comment back to the PR.

Flow:
    fetch_pr_and_build_graph(pr_context)
        ↓
    fetch PR diff          → which .py files changed?
        ↓
    check graph cache      → graph exists for this repo+sha?
        ↓ NO                            ↓ YES
    fetch ALL .py files       fetch changed files + their dependents only
    build full graph          update only those nodes in the graph
        ↓                         ↓
    set_graph()               update_files()
        ↓
    return { graph, pr_diff, changed_files }

Called by:
    main.py → handle_pull_request()  (before the LangGraph pipeline)
"""

import base64
import logging
import os

import httpx
from github import Github, GithubException
from dotenv import load_dotenv

from app.tools.ast_tool import build_knowledge_graph, parse_file
from app.utils.graph import (
    graph_exists,
    get_graph,
    set_graph,
    update_files,
    get_dependents,
)

load_dotenv()

log = logging.getLogger("codelineage.github")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — GITHUB CLIENT
# ══════════════════════════════════════════════════════════════════════════════

def _get_github_client() -> Github:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN not set in .env")
    return Github(token)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — FETCH PR DIFF
# ══════════════════════════════════════════════════════════════════════════════

def fetch_pr_diff(pr_context: dict) -> list[dict]:
    """
    Fetches the list of .py files changed in a PR.

    Returns:
        [
            {
                file_path: "services/user.py",
                status:    "modified",   # added / modified / removed
                additions: 10,
                deletions: 3,
                patch:     "@@ -1,5 +1,8 @@..."
            },
            ...
        ]
    Skips non-.py files and migration files.
    """
    gh   = _get_github_client()
    repo = gh.get_repo(pr_context["repo_name"])
    pr   = repo.get_pull(pr_context["pr_number"])

    changed_files = []

    for f in pr.get_files():
        if not f.filename.endswith(".py"):
            continue
        if "migrations" in f.filename:
            continue

        changed_files.append({
            "file_path": f.filename,
            "status":    f.status,
            "additions": f.additions,
            "deletions": f.deletions,
            "patch":     f.patch or "",
        })

    log.info(
        f"PR #{pr_context['pr_number']} diff — "
        f"{len(changed_files)} .py file(s) changed"
    )
    for f in changed_files:
        log.info(
            f"  {f['status']:8} {f['file_path']} "
            f"(+{f['additions']} -{f['deletions']})"
        )

    return changed_files


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — FETCH REPO FILES
# ══════════════════════════════════════════════════════════════════════════════

def fetch_all_py_files(pr_context: dict) -> list[dict]:
    """
    Fetches content of every .py file in the repo at head_sha.
    Used for first-time full graph build.

    Returns:
        [{ "path": "services/user.py", "content": "..." }, ...]
    """
    gh   = _get_github_client()
    repo = gh.get_repo(pr_context["repo_name"])
    sha  = pr_context["head_sha"]

    log.info(f"Fetching file tree for {pr_context['repo_name']}:{sha[:8]}")
    tree = repo.get_git_tree(sha, recursive=True)

    py_paths = [
        item.path
        for item in tree.tree
        if (
            item.path.endswith(".py")
            and "migrations"  not in item.path
            and "__pycache__" not in item.path
            and item.type == "blob"
        )
    ]

    log.info(f"Found {len(py_paths)} .py files to fetch")

    files = []
    for path in py_paths:
        content = _fetch_file_content(repo, path, sha)
        if content is None:
            continue
        files.append({"path": path, "content": content})

    log.info(f"Fetched {len(files)} files successfully")
    return files


def fetch_specific_files(pr_context: dict, file_paths: list[str]) -> list[dict]:
    """
    Fetches content of specific files only.
    Used during incremental indexing — only changed files + their dependents.

    Returns same format as fetch_all_py_files().
    """
    gh   = _get_github_client()
    repo = gh.get_repo(pr_context["repo_name"])
    sha  = pr_context["head_sha"]

    files = []
    for path in file_paths:
        content = _fetch_file_content(repo, path, sha)
        if content is None:
            continue
        files.append({"path": path, "content": content})

    log.info(f"Fetched {len(files)} specific files")
    return files


def _fetch_file_content(repo, path: str, sha: str) -> str | None:
    """Fetches and base64-decodes one file. Returns None on any error."""
    try:
        file_obj = repo.get_contents(path, ref=sha)
        return base64.b64decode(file_obj.content).decode("utf-8")
    except GithubException as e:
        log.warning(f"  [SKIP] Could not fetch {path}: {e.status} {e.data}")
        return None
    except Exception as e:
        log.warning(f"  [SKIP] Error fetching {path}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — MAIN ENTRY POINT (pre-graph indexer step)
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_pr_and_build_graph(pr_context: dict) -> dict:
    """
    Main entry point — called by main.py before the LangGraph pipeline.

    Orchestrates:
        1. Fetch PR diff
        2. Check graph cache
        3a. No graph  → fetch all files → full build → cache
        3b. Has graph → fetch changed + dependents only → incremental update
        4. Return { graph, pr_diff, changed_files }

    Args:
        pr_context: { repo_name, pr_number, head_sha, ... }

    Returns:
        {
            graph:         complete knowledge graph (all .py files),
            pr_diff:       list of changed files with raw patches,
            changed_files: list of changed .py file paths,
        }
    """
    repo_name  = pr_context["repo_name"]
    commit_sha = pr_context["head_sha"]

    log.info(f"Starting graph build for {repo_name}:{commit_sha[:8]}")

    # ── Step 1: Fetch PR diff ─────────────────────────────────────────────────
    pr_diff = fetch_pr_diff(pr_context)
    changed_file_paths = [f["file_path"] for f in pr_diff]

    # ── Step 2: Check cache ───────────────────────────────────────────────────
    if not graph_exists(repo_name, commit_sha):

        # ── Flow A: full build ────────────────────────────────────────────────
        log.info("No cached graph — fetching entire repo for full build")
        all_files = fetch_all_py_files(pr_context)

        log.info(f"Building knowledge graph from {len(all_files)} files")
        graph = build_knowledge_graph(all_files)
        set_graph(repo_name, commit_sha, graph)

    else:

        # ── Flow B: incremental update ────────────────────────────────────────
        log.info("Cached graph found — running incremental update")
        graph = get_graph(repo_name, commit_sha)

        # find dependents of changed files (files that import them)
        files_to_reparse = set(changed_file_paths)
        for file_path in changed_file_paths:
            dependents = get_dependents(repo_name, commit_sha, file_path)
            if dependents:
                log.info(f"  {file_path} has dependents: {dependents}")
                files_to_reparse.update(dependents)

        log.info(
            f"Re-parsing {len(files_to_reparse)} file(s) "
            f"({len(changed_file_paths)} changed + "
            f"{len(files_to_reparse) - len(changed_file_paths)} dependents)"
        )

        updated_raw = fetch_specific_files(pr_context, list(files_to_reparse))

        updated_parsed = {}
        for f in updated_raw:
            updated_parsed[f["path"]] = parse_file(f["content"], f["path"])

        graph = update_files(repo_name, commit_sha, updated_parsed)

    log.info(
        f"Graph ready — {len(graph)} files indexed, "
        f"{len(changed_file_paths)} file(s) changed in this PR"
    )

    return {
        "graph":         graph,
        "pr_diff":       pr_diff,
        "changed_files": changed_file_paths,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — POST PR COMMENT
# ══════════════════════════════════════════════════════════════════════════════

async def post_pr_comment(pr_context: dict, comment: str) -> None:
    """
    Posts the CodeLineage review as a comment on the GitHub PR.

    Uses GitHub REST API directly via httpx (avoids PyGithub's sync client
    for the async FastAPI context).

    Args:
        pr_context: { repo_name, pr_number, ... }
        comment:    formatted markdown string from critic.py
    """
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN not set — cannot post PR comment")

    repo      = pr_context["repo_name"]
    pr_number = pr_context["pr_number"]
    url       = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    log.info(
        f"Posting review comment to {repo}#{pr_number} "
        f"({len(comment)} chars)"
    )

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json={"body": comment})

    if resp.status_code >= 300:
        log.error(f"Failed posting comment: {resp.status_code} {resp.text}")
        raise RuntimeError(
            f"GitHub API error {resp.status_code}: {resp.text}"
        )

    log.info(f"Review comment posted successfully to {repo}#{pr_number} ✓")