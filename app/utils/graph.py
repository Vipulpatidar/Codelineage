"""
graph.py — In-memory knowledge graph cache for CodeLineage

Responsibility:
    Stores and retrieves knowledge graphs built by ast_tool.py
    Manages incremental updates when only some files change
    Tracks which repos have been indexed and when

Structure:
    cache = {
        "alice/my-repo:abc123sha": {
            "repo_name":  "alice/my-repo",
            "commit_sha": "abc123sha",
            "created_at": "2024-01-01 10:00:00",
            "graph": {
                "services/user.py":      { ... },
                "models/user.py":        { ... },
                "routes/user_routes.py": { ... },
            }
        }
    }

Called by:
    github_tool.py — to store and retrieve graphs
"""

import logging
from datetime import datetime

log = logging.getLogger("codelineage.graph")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — THE CACHE
# ══════════════════════════════════════════════════════════════════════════════

# This is the single in-memory store for all knowledge graphs.
# Lives for the lifetime of the server process.
# Key format: "repo_name:commit_sha"
# e.g. "alice/my-repo:abc123sha"

_cache: dict = {}


def _make_key(repo_name: str, commit_sha: str) -> str:
    """
    Builds the cache key from repo name and commit sha.

    We include commit_sha so different commits of the same repo
    are stored separately. This prevents stale graph data from
    one commit being used for a different commit's PR review.

    "alice/my-repo" + "abc123" → "alice/my-repo:abc123"
    """
    return f"{repo_name}:{commit_sha}"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — READ / WRITE
# ══════════════════════════════════════════════════════════════════════════════

def graph_exists(repo_name: str, commit_sha: str) -> bool:
    """
    Checks if a graph exists for this repo + commit.

    Called by github_tool.py FIRST before deciding what to do:
        YES → use cached graph, only re-parse changed files
        NO  → fetch entire repo, build graph from scratch

    Args:
        repo_name:  "alice/my-repo"
        commit_sha: "abc123sha"

    Returns:
        True if graph exists, False if not
    """
    key    = _make_key(repo_name, commit_sha)
    exists = key in _cache

    if exists:
        log.info(f"Cache HIT  — {repo_name}:{commit_sha[:8]}")
    else:
        log.info(f"Cache MISS — {repo_name}:{commit_sha[:8]}")

    return exists


def get_graph(repo_name: str, commit_sha: str) -> dict | None:
    """
    Retrieves the full knowledge graph for a repo + commit.

    Returns the graph dict if found, None if not found.

    The graph structure is:
        {
            "services/user.py":      { file data from ast_tool },
            "models/user.py":        { file data from ast_tool },
            "routes/user_routes.py": { file data from ast_tool },
            ...one key per .py file in the repo...
        }
    """
    key   = _make_key(repo_name, commit_sha)
    entry = _cache.get(key)

    if entry is None:
        return None

    return entry["graph"]


def set_graph(repo_name: str, commit_sha: str, graph: dict) -> None:
    """
    Stores a complete knowledge graph for a repo + commit.

    Called by github_tool.py after building the graph from scratch
    (first PR on a repo, or first time we see this commit sha).

    Args:
        repo_name:  "alice/my-repo"
        commit_sha: "abc123sha"
        graph:      complete knowledge graph from ast_tool.build_knowledge_graph()
    """
    key = _make_key(repo_name, commit_sha)

    _cache[key] = {
        "repo_name":  repo_name,
        "commit_sha": commit_sha,
        "created_at": datetime.utcnow().isoformat(),
        "graph":      graph,
    }

    file_count = len(graph)
    log.info(f"Graph stored — {repo_name}:{commit_sha[:8]} ({file_count} files)")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — INCREMENTAL UPDATE
# ══════════════════════════════════════════════════════════════════════════════

def update_files(
    repo_name:    str,
    commit_sha:   str,
    updated_files: dict,
) -> dict | None:
    """
    Updates only specific files in an existing graph.

    This is the incremental indexing optimization.
    Instead of re-parsing the entire repo on every PR,
    we only update the files that actually changed.

    Called by github_tool.py when:
        - graph already exists for this repo
        - PR only changed a few files
        - we re-parsed those files + their dependents

    Args:
        repo_name:     "alice/my-repo"
        commit_sha:    "abc123sha"
        updated_files: dict of { file_path → new parsed data }
                       e.g. { "services/user.py": { ... } }

    Returns:
        updated graph, or None if graph not found in cache

    Example:
        PR changes services/user.py
            ↓
        github_tool re-parses:
            services/user.py          (changed file)
            routes/user_routes.py     (dependent of services/user.py)
            ↓
        update_files() called with those 2 files
            ↓
        only those 2 nodes updated in the graph
        rest of the 998 files untouched
    """
    key   = _make_key(repo_name, commit_sha)
    entry = _cache.get(key)

    if entry is None:
        log.warning(
            f"update_files called but no graph found "
            f"for {repo_name}:{commit_sha[:8]} — cannot update"
        )
        return None

    # update only the changed file nodes
    for file_path, file_data in updated_files.items():
        entry["graph"][file_path] = file_data
        log.info(f"  Updated node: {file_path}")

    log.info(
        f"Graph updated — {repo_name}:{commit_sha[:8]} "
        f"({len(updated_files)} file(s) refreshed)"
    )

    return entry["graph"]


def get_dependents(repo_name: str, commit_sha: str, file_path: str) -> list[str]:
    """
    Returns the list of files that depend on a given file.

    Used by github_tool.py during incremental indexing to know
    which additional files to re-parse when a file changes.

    Example:
        services/user.py changes
            ↓
        get_dependents("alice/my-repo", "abc123", "services/user.py")
            ↓
        returns ["routes/user_routes.py", "middleware/auth.py"]
            ↓
        github_tool re-parses those files too
        even though they weren't in the PR diff

    Args:
        repo_name:  "alice/my-repo"
        commit_sha: "abc123sha"
        file_path:  "services/user.py"

    Returns:
        list of file paths that import this file
        empty list if file not found or has no dependents
    """
    graph = get_graph(repo_name, commit_sha)

    if graph is None:
        return []

    file_data = graph.get(file_path, {})
    return file_data.get("dependents", [])


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — CACHE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def delete_graph(repo_name: str, commit_sha: str) -> None:
    """
    Removes a specific graph from cache.

    Called when:
        - a repo is deleted
        - cache is getting too large
        - we want to force a full re-index
    """
    key = _make_key(repo_name, commit_sha)

    if key in _cache:
        del _cache[key]
        log.info(f"Graph deleted — {repo_name}:{commit_sha[:8]}")
    else:
        log.warning(f"delete_graph — key not found: {key}")


def delete_all_graphs_for_repo(repo_name: str) -> None:
    """
    Removes ALL cached graphs for a repo across all commits.

    Called when:
        - repo is deleted from GitHub
        - we want to completely reset a repo's graph
    """
    keys_to_delete = [
        key for key in _cache
        if key.startswith(f"{repo_name}:")
    ]

    for key in keys_to_delete:
        del _cache[key]

    log.info(
        f"All graphs deleted for {repo_name} "
        f"({len(keys_to_delete)} entries removed)"
    )


def get_cache_stats() -> dict:
    """
    Returns stats about the current cache state.

    Useful for debugging and monitoring — how many repos
    are cached, how many files total, memory estimate.

    Returns:
        {
            total_graphs:  3,
            repos: [
                {
                    repo_name:   "alice/my-repo",
                    commit_sha:  "abc123",
                    file_count:  47,
                    created_at:  "2024-01-01 10:00:00",
                    size_kb:     188
                },
                ...
            ],
            estimated_total_size_kb: 564
        }
    """
    stats = {
        "total_graphs": len(_cache),
        "repos":        [],
        "estimated_total_size_kb": 0,
    }

    for key, entry in _cache.items():
        file_count = len(entry["graph"])

        # rough size estimate — 4KB per file on average
        size_kb = file_count * 4

        stats["repos"].append({
            "repo_name":  entry["repo_name"],
            "commit_sha": entry["commit_sha"][:8],
            "file_count": file_count,
            "created_at": entry["created_at"],
            "size_kb":    size_kb,
        })

        stats["estimated_total_size_kb"] += size_kb

    return stats