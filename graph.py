"""LangGraph orchestrator for one bug's repair.

    gather_context → generate(k) → verify(k)
                         ▲             │
                         └──── repair ─┘   (bounded by repair_rounds)

- generate: k candidates in one LLM call (n=k) at the initial round; k=1 per
  repair round (as in PIE).
- verify: build_patch → fix → poll status for each candidate; STOP at the first
  pass (gives pass@k while avoiding needless heavy /fix runs).
- repair: on all-fail, append the failing test-log tail as feedback and re-generate.

Phase 1 uses raw log-tail feedback; Phase 3 replaces it with the Critic.
"""
from __future__ import annotations

import re
from typing import Optional, TypedDict

import requests
from langgraph.graph import StateGraph, END

import config
import llm
import triage
import tools
import critic
import asan_parse
from agent_state import AgentState
from artifacts import BugArtifacts
from harness_client import HarnessClient, HarnessError


class GState(TypedDict):
    round_idx: int
    messages: list
    feedback: Optional[str]
    solved: bool


def _extract_diff(patch_data: dict) -> Optional[str]:
    """Prefer the unified-diff field build_patch returns for display."""
    return patch_data.get("patch_content") or patch_data.get("content")


def _as_text(v) -> str:
    """Coerce a harness field to text. The harness json.loads-es any redis field
    beginning with '{'/'[', so log strings that are valid JSON come back as
    dict/list — round-trip those back to their JSON text; keep real strings as-is."""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v)
    return str(v)


_CODE_BLOCK = re.compile(r"```(?:[a-zA-Z0-9+]*)\s*\n?(.*?)```", re.S)


def extract_fix_code(response: str) -> str:
    """Return the code to insert at the infill location.

    The model's convention is to restate the *buggy* line first, then give the
    *corrected* line in a later fenced block ("The buggy line was: … / The
    correct line should be: …"). build_patch extracts the FIRST fenced block, so
    passing the raw response inserts the buggy line. We therefore hand it just
    the LAST fenced block (the fix), re-wrapped so build_patch still finds one.
    """
    blocks = _CODE_BLOCK.findall(response or "")
    if not blocks:
        return response
    return "```cpp\n" + blocks[-1].strip("\n") + "\n```"


def _inject_diagnosis(messages: list, block: str) -> list:
    """Fold the diagnosis bundle into the last user message."""
    if not block:
        return messages
    out = [dict(m) for m in messages]
    for m in reversed(out):
        if m["role"] == "user":
            m["content"] = m["content"].rstrip() + "\n\n" + block + \
                "\nUse this diagnosis to locate and fix the defect."
            return out
    out.append({"role": "user", "content": block})
    return out


_TOOL_REQUEST = re.compile(r"REQUEST_TOOL:\s*([a-z_]+)", re.I)


def _feedback_block(verdict: dict) -> str:
    """Turn a failing verdict into a compact repair-prompt feedback section."""
    tail = (verdict.get("log_tail") or "").strip()
    # Keep the last ~40 lines — enough to show the failing test / compile error.
    lines = tail.splitlines()
    if len(lines) > 40:
        tail = "\n".join(lines[-40:])
    reason = "the patch did not compile or the test suite still failed"
    return (
        "Your previous fix was applied but " + reason + ".\n"
        "Test / build output (tail):\n"
        "```\n" + tail + "\n```\n"
        "Produce a corrected full function that makes the tests pass. "
        "Return only the fixed code in a single ```cpp ... ``` block."
    )


class RepairRunner:
    """Owns the harness client + LLM params and builds the per-bug graph."""

    def __init__(self, client: HarnessClient, *,
                 k: int = config.K_CANDIDATES,
                 repair_rounds: int = config.REPAIR_ROUNDS,
                 model: str = config.OPENAI_MODEL,
                 seed: int = config.SEED,
                 patch_method: str = "direct",
                 diagnose: bool = config.ENABLE_DIAGNOSIS,
                 max_tool_requests: int = config.MAX_TOOL_REQUESTS,
                 use_critic: bool = config.USE_CRITIC):
        self.client = client
        self.k = k
        self.repair_rounds = repair_rounds
        self.model = model
        self.seed = seed
        self.patch_method = patch_method
        self.diagnose = diagnose               # observe→triage→tools before generating
        self.max_tool_requests = max_tool_requests
        self.use_critic = use_critic           # Phase 3: structured feedback on all-k-fail
        self.graph = self._build_graph()

    # ── graph wiring ──────────────────────────────────────────────────────────
    def _build_graph(self):
        # generate always feeds verify with no branch between them, so they are a
        # single node — LangGraph only propagates channels declared in GState,
        # which would drop a candidate list handed off between two nodes.
        g = StateGraph(GState)
        g.add_node("attempt", self._attempt)
        g.set_entry_point("attempt")
        g.add_conditional_edges("attempt", self._route, {"repair": "attempt", "done": END})
        return g.compile()

    def _route(self, state: GState) -> str:
        if state["solved"]:
            return "done"
        if state["round_idx"] > self.repair_rounds:
            return "done"
        return "repair"

    # ── node: generate k candidates, verify until first pass ───────────────────
    def _attempt(self, state: GState) -> GState:
        # Repair rounds append feedback and sample a single candidate.
        messages = list(state["messages"])
        if state["feedback"]:
            messages = messages + [{"role": "user", "content": state["feedback"]}]
        k = self.k if state["round_idx"] == 0 else 1

        out = llm.generate(messages, k=k, temperature=self._state_temp,
                           seed=self.seed, model=self.model)
        candidates = out["candidates"]
        gen_messages = messages
        round_idx = state["round_idx"]

        last_fail_verdict = None
        last_fail_code = ""
        last_fail_diff = ""
        for cand_idx, response in enumerate(candidates):
            patch_path, patch_diff, verdict = self._verify_one(response)
            self._art.write_candidate(round_idx, cand_idx,
                                      prompt_messages=gen_messages,
                                      response=response,
                                      patch_diff=patch_diff, verdict=verdict)
            self._state.add_attempt(round_idx=round_idx, cand_idx=cand_idx,
                                    prompt_messages=gen_messages, llm_response=response,
                                    patch_path=patch_path, patch_diff=patch_diff,
                                    verdict=verdict)
            if verdict.get("passed"):
                state["solved"] = True
                state["feedback"] = None
                state["round_idx"] = round_idx + 1
                return state
            last_fail_verdict = verdict
            last_fail_code = extract_fix_code(response)
            last_fail_diff = patch_diff or ""

        # all candidates failed this round → build feedback for the next round
        state["solved"] = False
        if last_fail_verdict is None:
            state["feedback"] = None
        elif self.use_critic:
            state["feedback"] = self._run_critic(last_fail_code, last_fail_verdict,
                                                 last_fail_diff)
        else:
            state["feedback"] = _feedback_block(last_fail_verdict)
        state["round_idx"] = round_idx + 1
        return state

    def _run_critic(self, failed_code: str, verdict: dict, failed_diff: str = "") -> str:
        """Phase 3: structured critique → feedback; also recorded on the round's
        last attempt for the trace/dashboard."""
        diagnosis = self._state.diagnosis or {}
        ev = dict(diagnosis.get("evidence") or {})
        ev["blocks"] = diagnosis.get("blocks") or []
        crit = critic.critique(self._bug_id, self._buggy_context, failed_code, verdict,
                               ev, patch_diff=failed_diff, model=self.model, seed=self.seed)
        if self._state.attempts:
            self._state.attempts[-1].critic_note = (
                f"{crit.failure_class}: {crit.root_cause}\n"
                f"suggested replacement:\n{crit.replacement_block}")
        return crit.as_feedback()

    # ── one candidate: build_patch → fix → status ─────────────────────────────
    def _verify_one(self, response: str):
        code = extract_fix_code(response)     # last fenced block = the corrected line
        # No usable code (model rambled / empty block) — build_patch would 400.
        if not code.strip() or code.strip().strip("`").strip().rstrip("cpp").strip() == "":
            return None, None, {"passed": False, "build_ok": False,
                                "error": "no code extracted from model response",
                                "return_code": None}
        try:
            patch = self.client.build_patch(self._bug_id, code, method=self.patch_method)
        except (HarnessError, requests.RequestException) as e:
            return None, None, {"passed": False, "build_ok": False,
                                "error": f"build_patch: {e}", "return_code": None}
        patch_path = patch.get("fix_p")
        patch_diff = _extract_diff(patch)
        try:
            handle = self.client.fix(self._bug_id, patch_path)
            final = self.client.wait_for_fix(handle)
        except (HarnessError, requests.RequestException) as e:
            return patch_path, patch_diff, {"passed": False, "build_ok": True,
                                            "error": f"fix: {e}", "return_code": None}
        rc = final.get("return_code")
        fix_status = (final.get("fix_status") or "").strip().lower()
        # A real fix must BOTH compile and pass the tests. return_code alone is not
        # enough: the harness test script ends on a `cat` (exit 0), so a *test*
        # failure never reaches the exit code — only a *build* failure gives rc=1.
        # And fix_status can be stale ("success" left over) when the build failed
        # and the test never ran. So require rc==0 AND fix_status=="success".
        passed = (rc == 0) and fix_status.startswith("success")
        # fix_log/fix_msg are meant to be strings, but the harness deserializes any
        # redis field that starts with '{' or '[' via json.loads — so a build log
        # that is valid JSON (e.g. clang JSON diagnostics) comes back as a dict/list.
        # Coerce to text before we concatenate or .strip() it downstream.
        fix_log = _as_text(final.get("fix_log", ""))
        fix_msg = _as_text(final.get("fix_msg", ""))
        # Sanitizer report of the FAILED PATCH itself: where /fix builds with a
        # sanitizer (the vuln set does so natively), a patch that still triggers
        # the fault leaves its trace in fix_log/fix_msg — "why *your* change still
        # crashes", distinct from the original bug's trace. None when clean/absent.
        patch_sanitizer = None if passed else asan_parse.parse_log(fix_log + "\n" + fix_msg)
        return patch_path, patch_diff, {
            "passed": passed,
            "return_code": rc,
            "fix_status": final.get("fix_status"),
            "status": final.get("status"),
            "log_tail": fix_log,
            "patch_sanitizer": patch_sanitizer,
            "error": _as_text(final.get("error", "")),
            "timed_out": final.get("_timed_out", False),
        }

    # ── Phase 2: observe → triage → tools (evidence-driven, label-free) ────────
    def _observe(self, bug_id: str) -> str:
        """Return the reproduction log for the buggy revision, reproducing first
        if warmup hasn't already left one on disk."""
        logs = self.client.read_test_logs(bug_id)
        if logs.strip():
            return logs
        try:
            handle = self.client.reproduce(bug_id, force_cleanup=False)
            self.client.wait_for_reproduce(handle)
        except HarnessError:
            return ""
        return self.client.read_test_logs(bug_id)

    def _diagnose(self, bug_id: str, messages: list, art: BugArtifacts) -> tuple[list, dict]:
        """observe→triage→seed tools, then let the LLM request more (hybrid).
        Returns (messages_with_diagnosis, diagnosis_record)."""
        log = self._observe(bug_id)
        evidence = triage.triage(log)
        ctx = tools.ToolContext(client=self.client, bug_id=bug_id,
                                log_text=log, evidence=evidence)
        results = tools.seed(ctx)                     # deterministic seed
        results += self._llm_request_tools(ctx, messages, results)  # hybrid extra tools

        blocks = [r.prompt_block for r in results if r.prompt_block]
        record = {
            "evidence": {k: v for k, v in evidence.items() if k != "log_excerpt"},
            "tools_used": [r.tool for r in results],
            "blocks": blocks,
        }
        art.write_diagnosis(record, raw_log=log)
        if blocks:
            header = f"Diagnosis of the observed failure ({evidence['summary']}):"
            messages = _inject_diagnosis(messages, header + "\n\n" + "\n\n".join(blocks))
        return messages, record

    def _llm_request_tools(self, ctx, base_messages, seeded) -> list:
        """Let the model ask for additional diagnostic tools, bounded by budget.
        Only offers tools not already run and applicable/available."""
        extra = []
        if self.max_tool_requests <= 0:
            return extra
        run_names = {r.tool for r in seeded}
        unknown = ctx.evidence.get("failure_class") == "unknown"
        for _ in range(self.max_tool_requests):
            # The deterministic seed already ran every tool applicable to a
            # *classified* failure, so LLM escalation is only meaningful when the
            # failure is unknown (offer everything) or an applicable tool remains.
            offer = [t for t in tools.REGISTRY
                     if t.name not in run_names and (unknown or t.applicable(ctx.evidence))]
            if not offer:
                break
            menu = "\n".join(f"- {t.name}: {t.description}" for t in offer)
            probe = base_messages + [{
                "role": "user",
                "content": ("Before writing the fix, you may gather more evidence. "
                            "Available tools:\n" + menu +
                            "\n\nReply with exactly `REQUEST_TOOL: <name>` to run one, "
                            "or `READY` to proceed with what you have."),
            }]
            try:
                resp = llm.generate(probe, k=1, temperature=0.0,
                                    seed=self.seed, model=self.model)["candidates"][0]
            except llm.LLMUnavailable:
                raise                   # endpoint is dead — abort the run
            except Exception:  # noqa: BLE001
                break
            m = _TOOL_REQUEST.search(resp or "")
            if not m:
                break
            tool = tools.get(m.group(1).lower())
            if not tool or tool.name in run_names:
                break
            res = tool.run(ctx)
            run_names.add(tool.name)
            if res.prompt_block or res.data:
                extra.append(res)
        return extra

    # ── entrypoint ────────────────────────────────────────────────────────────
    def run(self, defect: dict, art: BugArtifacts) -> AgentState:
        bug_id = defect["bug_id"]
        project = bug_id.split("@")[0]
        pd = defect["prompt_data"]
        temperature = pd.get("temperature", 0.7) or 0.7

        state = AgentState(bug_id=bug_id, project=project, mode="static",
                           temperature=temperature)
        # bind per-bug context for the node methods
        self._bug_id = bug_id
        self._state = state
        self._art = art
        self._state_temp = temperature
        # the original buggy function + infill location (last user turn), for the Critic
        self._buggy_context = next((m["content"] for m in reversed(pd["prompt"])
                                    if m["role"] == "user"), "")

        messages = list(pd["prompt"])
        # Phase 2 gather_context: observe the real failure, triage it, and apply
        # whichever diagnostic tools fit — no dataset label, no preset ASAN mode.
        if self.diagnose:
            messages, record = self._diagnose(bug_id, messages, art)
            state.diagnosis = record
            state.mode = record["evidence"].get("failure_class", "static")
            # Tier 3: if the buggy baseline itself doesn't build, the bug is
            # untestable — an environment/toolchain problem, not a model failure.
            # Skip repair and mark it so it's excluded from pass@k.
            if state.mode == "compile_error":
                state.infra_blocked = True
                return state

        init: GState = {
            "round_idx": 0,
            "messages": messages,
            "feedback": None,
            "solved": False,
        }
        # recursion budget: initial + repair rounds, ×2 nodes, + slack
        self.graph.invoke(init, {"recursion_limit": 4 * (self.repair_rounds + 2)})
        return state
