import logging
from app.utils.graph import get_dependents

log = logging.getLogger("codelineage.graph_context")


def collect_graph_context(state: dict):
    """
    Collect related files using dependency graph
    """

    repo = state["pr_context"]["repo_name"]
    sha = state["pr_context"]["head_sha"]

    changed = state["changed_files"]

    files = set(changed)

    # get dependents
    for f in changed:
        deps = get_dependents(repo, sha, f)

        if deps:
            log.info(f"{f} dependents: {deps}")
            files.update(deps)

    # also include impacted files
    impacted = state.get("impacted", [])
    files.update(impacted)

    log.info(f"Graph-aware context files: {files}")

    return list(files)