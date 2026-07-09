"""Phase 3 — Critic agent.

On all-k-fail, a separate LLM call diagnoses *why* the attempt failed and proposes
a concrete corrected line, so the next round gets a structured
`{failure_class, root_cause, replacement_block}` instead of a raw log tail (PIE's
core finding: structured feedback cuts repeated mistakes).

The `replacement_block` is folded into the re-generate prompt. Critiques are
disk-cached (JSONL) keyed by (bug_id, failed code, log tail) so re-runs are cheap
and deterministic.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Optional

import asan_parse
import config
import llm

CACHE_PATH = os.path.join(config.BASE_DIR, ".critic_cache.jsonl")

_SYS = ("You are a C/C++ program-repair critic. A previous attempt to fill in a "
        "single removed line failed its test. Diagnose the real root cause and "
        "propose the corrected code for the infill location.")

_JSON = re.compile(r"\{.*\}", re.S)
_CODE = re.compile(r"```(?:[a-zA-Z0-9+]*)\s*\n?(.*?)```", re.S)


@dataclass
class Critique:
    failure_class: str
    root_cause: str
    replacement_block: str
    raw: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def as_feedback(self) -> str:
        """Render for the re-generate prompt."""
        block = self.replacement_block.strip()
        return (
            "A critic analyzed your previous failed attempt.\n"
            f"- failure: {self.failure_class}\n"
            f"- root cause: {self.root_cause}\n"
            "- suggested corrected code for the infill location:\n"
            f"```cpp\n{block}\n```\n"
            "Using this, output the corrected code for the infill location as a "
            "single ```cpp code block and nothing else.")


# Bump when the critic prompt changes so stale critiques aren't reused.
_PROMPT_VERSION = "v2-diff+patchsan"


def _key(bug_id: str, code: str, verdict: dict) -> str:
    tail = (verdict.get("log_tail") or "")[-500:]
    san = "1" if verdict.get("patch_sanitizer") else "0"
    return hashlib.md5(
        f"{_PROMPT_VERSION}|{bug_id}|{code}|{tail}|{san}".encode()).hexdigest()


def _load_cache() -> dict:
    cache = {}
    if os.path.exists(CACHE_PATH):
        for line in open(CACHE_PATH):
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                    cache[r["key"]] = r["critique"]
                except (json.JSONDecodeError, KeyError):
                    pass
    return cache


def _append_cache(key: str, critique: Critique) -> None:
    with open(CACHE_PATH, "a") as f:
        f.write(json.dumps({"key": key, "critique": critique.to_dict()}) + "\n")


def _parse(resp: str, fallback_class: str) -> Critique:
    m = _JSON.search(resp or "")
    if m:
        try:
            d = json.loads(m.group(0))
            return Critique(
                failure_class=str(d.get("failure_class", fallback_class)),
                root_cause=str(d.get("root_cause", "")).strip(),
                replacement_block=str(d.get("replacement_code", d.get("replacement_block", ""))).strip(),
                raw=resp)
        except json.JSONDecodeError:
            pass
    # fallback: last code block is the suggested replacement
    blocks = _CODE.findall(resp or "")
    return Critique(failure_class=fallback_class,
                    root_cause=(resp or "").strip()[:300],
                    replacement_block=(blocks[-1].strip() if blocks else ""),
                    raw=resp)


def _prompt(buggy_context: str, failed_code: str, verdict: dict, evidence: dict,
            patch_diff: str = "") -> list:
    ev = evidence or {}
    diag = ""
    if ev.get("summary"):
        diag = f"\nObserved failure ({ev.get('summary')}) — this is the ORIGINAL bug:"
        for b in (ev.get("blocks") or []):
            diag += "\n" + b
    # Full applied diff, not just the inserted line, so the critic sees context it
    # displaced (bad edit location, broken surrounding statement, etc.).
    diff_block = ""
    if (patch_diff or "").strip():
        d = patch_diff.strip()
        diff_block = f"\n\nThe full diff that was applied and failed:\n```diff\n{d[:1500]}\n```"
    # Sanitizer report of the FAILED PATCH — "why *your* change still crashes",
    # only present when /fix builds with a sanitizer and the patch still triggers it.
    patch_san = ""
    if verdict.get("patch_sanitizer"):
        patch_san = ("\n\nYour patched code STILL triggers the sanitizer (this is the "
                     "patch's own failure, not the original bug's):\n"
                     + asan_parse.to_prompt_block(verdict["patch_sanitizer"]))
    tail = (verdict.get("log_tail") or "").strip()
    lines = tail.splitlines()
    if len(lines) > 30:
        tail = "\n".join(lines[-30:])
    user = (
        f"Buggy function and infill location:\n```cpp\n{buggy_context.strip()}\n```\n\n"
        f"The line the previous attempt inserted (which failed):\n```cpp\n{failed_code.strip()}\n```"
        f"{diff_block}\n"
        f"{diag}{patch_san}\n\n"
        f"Test/build output (tail):\n```\n{tail}\n```\n\n"
        "Respond with a JSON object only, of the form:\n"
        '{"failure_class": "...", "root_cause": "one sentence", '
        '"replacement_code": "the corrected code for the infill location"}')
    return [{"role": "system", "content": _SYS}, {"role": "user", "content": user}]


def critique(bug_id: str, buggy_context: str, failed_code: str, verdict: dict,
             evidence: Optional[dict] = None, *, patch_diff: str = "",
             model: str = config.OPENAI_MODEL,
             seed: int = config.SEED, use_cache: bool = True) -> Critique:
    key = _key(bug_id, failed_code, verdict)
    if use_cache:
        cached = _load_cache().get(key)
        if cached:
            return Critique(**cached)
    fallback_class = (evidence or {}).get("failure_class", "unknown")
    try:
        resp = llm.generate(_prompt(buggy_context, failed_code, verdict, evidence, patch_diff),
                            k=1, temperature=0.0, seed=seed, model=model)["candidates"][0]
    except Exception as e:  # noqa: BLE001 — critic failure must not sink the run
        return Critique(fallback_class, f"critic call failed: {e}", "", "")
    crit = _parse(resp, fallback_class)
    if use_cache:
        _append_cache(key, crit)
    return crit
