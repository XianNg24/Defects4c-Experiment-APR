"""Reference (ground-truth) fix for a bug, for DASHBOARD comparison only.

The real fix is `git diff <commit_before> <commit_after>` restricted to the bug's
source file(s), read from the checked-out repo under OUT_DIR. This is computed
post-hoc for visualization — it is NEVER shown to the agent (that would leak the
answer). Kept separate from the repair pipeline for exactly that reason.
"""
from __future__ import annotations

import functools
import json
import os
import subprocess

import config

_TPL = os.path.join(os.path.dirname(config.OUT_DIR), "defectsc_tpl")


def _meta(project: str, sha: str) -> dict | None:
    for major in ("projects_v1", "projects"):
        f = os.path.join(_TPL, major, project, "bugs_list_new.json")
        if os.path.exists(f):
            d = json.load(open(f))
            bugs = d if isinstance(d, list) else list(d.values())
            b = next((x for x in bugs if str(x.get("commit_after", "")).startswith(sha[:12])), None)
            if b:
                return b
    return None


@functools.lru_cache(maxsize=512)
def reference_fix(bug_id: str) -> dict | None:
    """Return {src, before, after, diff} for the ground-truth fix, or None."""
    try:
        project, sha = bug_id.split("@", 1)
    except ValueError:
        return None
    b = _meta(project, sha)
    if not b:
        return None
    before, after = b.get("commit_before"), b.get("commit_after")
    srcs = (b.get("files") or {}).get("src") or []
    repo = os.path.join(config.OUT_DIR, project, f"git_repo_dir_{sha}")
    if not (before and after and srcs and os.path.isdir(repo)):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", repo, "-c", "safe.directory=*", "diff", before, after, "--", *srcs],
            capture_output=True, text=True, timeout=30)
        diff = out.stdout.strip()
    except Exception:  # noqa: BLE001
        return None
    if not diff:
        return None
    return {"src": srcs, "before": before[:12], "after": after[:12], "diff": diff}


if __name__ == "__main__":
    import sys
    print(json.dumps(reference_fix(sys.argv[1]), indent=2) if len(sys.argv) > 1 else __doc__)
