"""The fix commit's message, from the dataset's saved GitHub API dump.

ORACLE / LEAKAGE — read before using. This text was written by the developer who
authored the fix, and it frequently states the patch outright ("Line-hook handling was
accessing debug info without checking whether it was present"). For a bug that has not
been fixed yet no such message can exist, so a run with --commit-message is an UPPER
BOUND, not a realistic APR result, and is not comparable to the paper's baselines.

Its use is to measure headroom: the diagnosis pipeline tries to *infer* what this
message simply *states*, so the gap between them bounds how much better diagnosis could
ever get. (Curiously, the dataset's own prompt ends with "provide the correct line
following commit message" — a slot it never fills.)
"""
from __future__ import annotations

import functools
import json
import os

import config

_FILE = os.path.join(os.path.dirname(config.OUT_DIR), "defectsc_tpl", "data",
                     "github_api_save.jsonl")


@functools.lru_cache(maxsize=1)
def _by_sha() -> dict:
    out: dict[str, str] = {}
    if not os.path.exists(_FILE):
        return out
    for line in open(_FILE, errors="replace"):
        try:
            content = json.loads(line).get("content")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(content, str):                 # some rows nest the JSON as a string
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                continue
        if not isinstance(content, dict):
            continue
        sha, commit = content.get("sha"), content.get("commit")
        if sha and isinstance(commit, dict):
            out[sha] = (commit.get("message") or "").strip()
    return out


def message(bug_id: str, max_chars: int = 700) -> str:
    """The fix commit's message for `bug_id`, or "" if absent."""
    return _by_sha().get(bug_id.split("@")[-1], "")[:max_chars]


if __name__ == "__main__":
    import sys
    from harness_client import HarnessClient
    if len(sys.argv) > 1:
        print(message(sys.argv[1]) or "(no commit message)")
    else:
        bugs = HarnessClient().list_bugs()
        have = sum(1 for b in bugs if message(b))
        print(f"commit message available for {have}/{len(bugs)} bugs")
