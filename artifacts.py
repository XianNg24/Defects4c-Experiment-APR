"""Per-bug artifact writers.

Every run writes an inspectable directory tree so a human (and the Phase 5
dashboard) can see exactly what the agent did:

    runs/<ts>/
    ├── run_meta.json                 params for the whole run
    ├── results.jsonl                 one row per bug (bug_id, solved, rounds, ...)
    └── <safe_bug_id>/
        ├── trace.json                full AgentState (source of truth for viz)
        ├── defect.json               raw /get_defect response
        ├── round<r>_cand<c>_prompt.txt
        ├── round<r>_cand<c>_response.txt
        ├── round<r>_cand<c>.diff
        └── verdict_round<r>_cand<c>.json
"""
from __future__ import annotations

import json
import os
import re


def safe_bug_id(bug_id: str) -> str:
    """Filesystem-safe form of project@sha."""
    return re.sub(r"[^A-Za-z0-9._@-]", "_", bug_id)


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content if content is not None else "")


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)


def render_prompt(messages: list) -> str:
    """Human-readable flattening of a chat message list."""
    out = []
    for m in messages:
        out.append(f"### {m['role'].upper()}\n{m['content']}")
    return "\n\n".join(out)


class BugArtifacts:
    """Handles for one bug's artifact directory."""

    def __init__(self, run_dir: str, bug_id: str):
        self.dir = os.path.join(run_dir, safe_bug_id(bug_id))
        os.makedirs(self.dir, exist_ok=True)

    def write_defect(self, defect: dict) -> None:
        _write_json(os.path.join(self.dir, "defect.json"), defect)

    def write_candidate(self, round_idx: int, cand_idx: int, *,
                        prompt_messages: list, response: str,
                        patch_diff: str | None, verdict: dict) -> None:
        stem = f"round{round_idx}_cand{cand_idx}"
        _write_text(os.path.join(self.dir, f"{stem}_prompt.txt"),
                    render_prompt(prompt_messages))
        _write_text(os.path.join(self.dir, f"{stem}_response.txt"), response)
        _write_text(os.path.join(self.dir, f"{stem}.diff"),
                    patch_diff or "(no diff — patch extraction failed)\n")
        _write_json(os.path.join(self.dir, f"verdict_{stem}.json"), verdict)

    def write_diagnosis(self, record: dict, *, raw_log: str = "") -> None:
        _write_json(os.path.join(self.dir, "diagnosis.json"), record)
        if raw_log:
            _write_text(os.path.join(self.dir, "observed_repro.log"), raw_log)

    def write_trace(self, state) -> None:
        state.serialise(os.path.join(self.dir, "trace.json"))


class RunArtifacts:
    """Top-level run directory + aggregate results."""

    def __init__(self, runs_root: str, run_id: str, meta: dict):
        self.dir = os.path.join(runs_root, run_id)
        os.makedirs(self.dir, exist_ok=True)
        _write_json(os.path.join(self.dir, "run_meta.json"), meta)
        self.results_path = os.path.join(self.dir, "results.jsonl")

    def bug(self, bug_id: str) -> BugArtifacts:
        return BugArtifacts(self.dir, bug_id)

    def append_result(self, row: dict) -> None:
        with open(self.results_path, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
