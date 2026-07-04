"""AgentState — per-bug trajectory for the agentic repair loop.

Adapted from PIE's agent_state.py to the Defects4C patch/verify flow: each
Attempt is one candidate (LLM response → build_patch → fix → status verdict).
Serialised to the per-bug artifact dir so the whole trajectory is inspectable
and drives the Phase 5 visualization.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional
import json
import os


@dataclass
class Attempt:
    """One candidate patch and its verification verdict."""
    round_idx: int              # 0 = initial generation, 1+ = repair rounds
    cand_idx: int               # candidate index within the round (0..k-1)
    prompt_messages: list       # exact messages sent to the LLM
    llm_response: str           # raw model output
    patch_path: Optional[str]   # /patches/... path from build_patch (None if extraction failed)
    patch_diff: Optional[str]   # unified diff, for display
    verdict: dict               # {passed, return_code, fix_status, error, log_tail, build_ok}
    critic_note: Optional[str] = None   # Phase 3

    @property
    def passed(self) -> bool:
        return bool(self.verdict.get("passed"))


@dataclass
class AgentState:
    bug_id: str                 # project@sha
    project: str
    mode: str = "static"        # observed failure_class (crash|assertion_mismatch|…)
    temperature: float = 0.7
    attempts: list = field(default_factory=list)
    winner: Optional[dict] = None   # summary of the passing attempt, if any
    diagnosis: Optional[dict] = None   # Phase 2: {evidence, tools_used, blocks}
    infra_blocked: bool = False     # baseline doesn't build (env/toolchain) — not a model failure

    def add_attempt(self, **kw) -> Attempt:
        a = Attempt(**kw)
        self.attempts.append(a)
        if a.passed and self.winner is None:
            self.winner = {
                "round_idx": a.round_idx,
                "cand_idx": a.cand_idx,
                "patch_path": a.patch_path,
            }
        return a

    @property
    def solved(self) -> bool:
        return self.winner is not None

    def failed_attempts(self) -> list:
        return [a for a in self.attempts if not a.passed]

    def to_dict(self) -> dict:
        return {
            "bug_id": self.bug_id,
            "project": self.project,
            "mode": self.mode,
            "temperature": self.temperature,
            "solved": self.solved,
            "winner": self.winner,
            "infra_blocked": self.infra_blocked,
            "diagnosis": self.diagnosis,
            "n_attempts": len(self.attempts),
            "attempts": [asdict(a) for a in self.attempts],
        }

    def serialise(self, out_path: str) -> None:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
