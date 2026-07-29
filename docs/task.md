# Task: Agentic Automated Program Repair (APR) for Defects4C

Sanitizer-guided, LLM-driven bug repair on the Defects4C C/C++ benchmark.
Built **incrementally**: a minimal working agent first, then ASAN traces, a
critic, memory, and richer visualization added one phase at a time. Each phase
ships something runnable and measurable before the next begins.

---

## 1. Objective

Given a buggy C/C++ function from Defects4C, drive an LLM agent to produce a
patch that makes the project's test suite pass. Use **AddressSanitizer traces**
as high-signal diagnostic feedback for memory-safety bugs (a large fraction of
Defects4C are CVEs: tcpdump, libgd, jasper, mbedtls, …). The whole trajectory —
prompts, candidate diffs, ASAN traces, verdicts — must be **easy to inspect and
visualize**.

Success at the top level: measurable **pass@k** across a fixed bug subset, with
per-bug artifacts that let a human see exactly what the agent did.

---

## 2. What already exists (build on, do not rebuild)

**Defects4C harness** (`defects4c/`):
- HTTP service `new_main.py` (served on `http://127.0.0.1:11111`) with the full
  repair workflow already implemented:
  `GET /list_defects_bugid` → `GET /get_defect/{bug_id}` → `POST /build_patch`
  → `POST /fix` → `GET /status/{handle}` (poll). See `assets/http_api_usage.md`.
- `http_tutorial.py` — a **non-agentic reference client** for exactly this loop.
  Our Phase 1 agent is essentially this, restructured as a LangGraph state machine
  with k-sampling and repair rounds.
- Patch validation, Redis result caching (24h TTL), and test execution are all
  server-side. `return_code == 0` ⇒ all tests pass.
- Datasets: `defectsc_tpl/data/single_function_allinone.saved.jsonl` (364 bugs,
  ready-made prompt messages), plus `buggy_errmsg/` variants (single line / hunk /
  function; 156 / 107 / 101 bugs).
- Build flags flow into builds via Jinja `{{build_flags}}`
  (`common_build_tpl.jinja`, per-project `build_tpl.jinja`). **This is the ASAN
  injection point** (Phase 2).
- Test output is captured with `ctest -VV 2>&1 >> $test_log`
  (`common_test_tpl.jinja`), so an ASAN report on a crashing test **already lands
  in the test log** — we just need to build with ASAN and parse it.

**PIE reference agent** (`../PIE-Speed-Optimization/`) — patterns to reuse:
- `agent_state.py` — `AgentState` / `Attempt` trajectory dataclasses, serialized
  to per-sample JSON. Adopt directly.
- `local_llm.py` — HF transformers loader with fp16/4-bit auto-quant, k-sampling
  via `num_return_sequences`, chat-template prompt assembly, code extraction.
- `agent_critic.py` — structured Critic (failure_class + replacement_block),
  disk cache, delta-debug input shrinker. Blueprint for our Phase 3.
- `architecture.md` — the k-candidates + `repair_budget` + `mutate_budget`
  state-machine design and mode-aware evidence isolation.
- Per-sample artifact writers (`_write_prompt`, `_write_diff`, `_write_mode_candidates`)
  — the visualization substrate.

---

## 3. Architecture (target)

```
                 ┌─────────────────────────────────────────┐
                 │  Orchestrator (LangGraph state machine)  │
                 └─────────────────────────────────────────┘
   select_bug → gather_context → [ASAN reproduce] → generate(k) → verify(k)
                                        │                              │
                                        │              any pass? ──── yes ──▶ DONE
                                        │                              │
                                        │                             no
                                        │                              ▼
                                        └──────── critic ◀── repair_budget>0? ── yes
                                                    │                              │
                                                    └──── re-generate(k=1) ────────┘
                                                              (bounded loop)
```

**Agents / nodes** (each a pure function over `AgentState`):

| Node | LLM? | Input | Output |
|---|---|---|---|
| SelectBug | no | bug subset | next `bug_id` |
| GatherContext | no | `/get_defect` + (Phase 2) ASAN trace | prompt signals |
| Generate (Coder) | yes | buggy code, feedback, critic note | k candidate patches |
| Verify | no | candidate patch | `/build_patch` + `/fix` + `/status` verdict |
| Critic (Phase 3) | yes | failed candidate, ASAN trace, test log | `{failure_class, evidence, replacement_block}` |
| Memory (Phase 4) | no | `(project, error_class)` | distilled lessons |

**Two independent budgets** (mirrors PIE): `--k` initial candidates per round,
`--repair-rounds` critic→re-generate cycles after all k fail.

---

## 4. Design decisions to confirm before Phase 1

These change what we build — flagging rather than silently choosing. Defaults in
**bold** are my recommendation.

1. **Agent ↔ harness interface**: **HTTP API** (`new_main.py`, matches
   `http_tutorial.py`, server does patch-apply + validation + caching) vs. driving
   `bug_helper_v1_out2.py` directly. → Recommend HTTP API for Phases 1–3; only drop
   to direct invocation if ASAN needs build control the API can't expose.

2. **How to serve open models** (CodeLlama / DeepSeek-Coder): **vLLM behind an
   OpenAI-compatible endpoint** (the tutorial client already speaks this via
   `OPENAI_BASE_URL`; `step1_build_docker.sh` step5 already shows a
   `127.0.0.1:8888/v1` Qwen endpoint) vs. in-process HF transformers
   (`local_llm.py`). → Recommend vLLM: same client code for all models, no GPU
   memory contention with the agent process, trivial model swap.

3. **ASAN enablement** (Phase 2): inject `-fsanitize=address -fno-omit-frame-pointer
   -g` into `build_flags` via a **new opt-in "asan reproduce" path** that leaves the
   default build untouched (additive, matches repo philosophy) vs. modifying build
   templates globally. → Recommend the opt-in path. Open sub-question: does the
   HTTP API expose per-request build flags, or do we add a small `/reproduce_asan`
   route / a direct `bug_helper` call for the ASAN build?

4. **Bug subset for the first runs**: **the single-line repair set** (156 bugs,
   smallest edits, highest signal for a first baseline) vs. full-function set. →
   Recommend single-line first, expand once the loop is validated.

5. **Framework depth**: use **LangGraph from Phase 1 but keep the graph tiny**
   (3 nodes) vs. hand-roll first and migrate later (what PIE did). → Recommend
   LangGraph now since it's an explicit requirement, but resist over-modeling.

---

## 5. Phased plan

### Phase 0 — Scaffolding & smoke test — ✅ DONE
Goal: talk to the running service and one model end-to-end, no agent logic yet.

1. ✅ Created `agentic_apr/` with `config.py`, `requirements.txt`
   (`langgraph`, `langchain-core`, `openai`, `requests`, `rich`).
   → verified: `uv pip install -r requirements.txt` succeeds in `.venv`
   (host had no pip/venv; installed `uv` standalone — no apt, avoids dpkg-lock
   contention with the running warmup).
2. ✅ `harness_client.py` — wraps all 5 endpoints; `--smoke` exercises only the
   read-only ones (safe during warmup).
   → verified: `python harness_client.py --smoke` passes against the live
   container (159 non-llvm bugs, prints prompt for the first defect).
3. ◑ Model serving: `llm.py` OpenAI-compatible client written; `--smoke`
   degrades gracefully when no endpoint is up. GPU confirmed present
   (**NVIDIA L4, 23 GB** — fits all target models). Standing up the actual vLLM
   server is deferred (heavy: ~13 GB download + pins GPU; better triggered
   explicitly, and warmup is still running). See `agentic_apr/README.md` for the
   one-liner to launch it.
   → verify (when launched): `python llm.py --smoke` prints PASS.

### Phase 1 — Minimal agentic repair loop (the "simple system")
Goal: reproduce `http_tutorial.py` as a LangGraph agent with **k** and
**repair-rounds**, writing inspectable artifacts. No ASAN yet.

4. `agent_state.py` — port PIE's `AgentState`/`Attempt`; fields for `bug_id`,
   `project`, attempts (plan/patch_diff/verdict/critic_note).
   → verify: unit test constructs a state, adds attempts, serializes to JSON.
5. `graph.py` — LangGraph with nodes `gather_context → generate(k) → verify`.
   `generate` samples k candidates (temperature from `/get_defect`); `verify`
   calls `build_patch`+`fix`+`status` per candidate; first `return_code==0` wins.
   → verify: run on 1 known bug, agent reports pass/fail and writes a trace.
6. Repair loop: if all k fail and `repair_rounds>0`, append the raw test-log tail
   as feedback and re-generate (k=1), bounded by budget.
   → verify: on a bug that fails round 1, a second round runs and is logged.
7. Artifact writer `artifacts.py` (reuse PIE's `_write_prompt`/`_write_diff`):
   per-bug dir `runs/<ts>/<bug_id>/` with `prompt_round<i>.txt`,
   `candidate_<k>.patch`, `candidate_<k>.diff`, `verdict_<k>.json`, `trace.json`,
   `summary.json`.
   → verify: after a run, every prompt / patch / diff / verdict is on disk.
8. CLI `run.py --bugs <subset> --k N --repair-rounds R --model M --limit L`.
   → verify: `--limit 5` runs 5 bugs and emits an aggregate `pass@k` number.

**Phase 1 exit criteria:** end-to-end pass@1 and pass@k numbers on ≥20 bugs,
with full per-bug artifacts. This is the baseline everything else is measured against.

### Phase 2 — AddressSanitizer trace feedback
Goal: give the agent the crash trace, not just a pass/fail bit.

9. ASAN reproduce path (per decision #3): build the buggy revision with
   `-fsanitize=address -fno-omit-frame-pointer -g` and run its tests so the ASAN
   report is captured in the test log.
   → verify: a known memory-safety bug (e.g. a tcpdump/libgd CVE) emits a
   `==ERROR: AddressSanitizer:` block in the captured log.
10. `asan_parse.py` — extract structured fields from the report: error type
    (heap-buffer-overflow / use-after-free / …), faulting `file:line`, the top
    N stack frames, and the read/write + size.
    → verify: parser turns a real report into a compact JSON dict; unit-tested on
    2–3 saved reports.
11. Wire the parsed trace into `gather_context` as a "Sanitizer diagnosis:" block
    in the repair prompt (mode-isolated so we can A/B ASAN-on vs ASAN-off).
    → verify: prompt for an ASAN bug contains the faulting line + error class.
12. A/B: run the same subset with `--asan` on vs off.
    → verify: report pass@k delta attributable to the ASAN signal.

### Phase 3 — Critic agent (structured diagnosis)
Goal: replace raw-log feedback with a diagnosis, cutting repeated mistakes
(PIE's core finding).

13. Port `agent_critic.py`: on all-k-fail, Critic consumes (failed patch, ASAN
    trace, test-log diff) → `{failure_class, evidence, replacement_block}`; feed
    `replacement_block` into the re-generate prompt. Disk-cache critic outputs.
    → verify: on a bug that loops, round-2 prompt contains a concrete patch hunk,
    and critic outputs are cached to CSV/JSONL.

### Phase 4 — Memory (cross-bug lessons)
Goal: reuse distilled lessons keyed by `(project, error_class)`.

14. Append-only `agent_memory.jsonl` written from Critic outputs; RAG the top
    lessons for a `(project, error_class)` into the **initial** Generate prompt.
    → verify: a bug whose class was seen before gets a "Common pitfalls:" block;
    measure pass@k with/without memory.

### Phase 5 — Visualization dashboard (strong requirement — start early, finish here)
Goal: see, per bug and in aggregate, **what the agent did, which prompts, which
lines changed, and the result.**

15. Per-bug **timeline viewer**: a self-contained HTML page rendering the trace as
    rounds → (prompt shown/collapsible) → (unified diff with +/- highlighting) →
    (ASAN trace) → (verdict badge). Generated from `trace.json`; no external assets.
    → verify: opening one bug's page shows every round with syntax-highlighted diffs.
16. Aggregate **run dashboard**: table of bugs × (pass@1, pass@k, #rounds, error
    class, model), with pass-rate summary tiles and per-project breakdown; links
    into each bug's timeline page.
    → verify: dashboard for a ≥20-bug run renders and links resolve.
17. (Optional) publish the dashboard as a shareable Artifact for reviewers.

---

## 6. Repository layout (proposed)

```
DEFECTS4C-CODE-REPAIR/
├── task.md                     (THIS FILE)
├── defects4c/                  (existing harness — unchanged in Phases 1,3,4,5)
└── agentic_apr/                (NEW)
    ├── config.py               endpoints, model, k, repair-rounds, ASAN flag
    ├── requirements.txt        langgraph, langchain-core, openai, requests, rich
    ├── harness_client.py       HTTP wrapper over new_main.py
    ├── agent_state.py          AgentState + Attempt (ported from PIE)
    ├── llm.py                  OpenAI-compatible client (open models via vLLM)
    ├── graph.py                LangGraph orchestrator (nodes + budgets)
    ├── asan_parse.py           (Phase 2) sanitizer report → structured dict
    ├── critic.py               (Phase 3) structured diagnosis + cache
    ├── memory.py               (Phase 4) (project, error_class) → lessons
    ├── artifacts.py            per-bug artifact writers (ported from PIE)
    ├── viz/                    (Phase 5) trace + dashboard HTML generators
    ├── run.py                  CLI entrypoint
    └── runs/<ts>/<bug_id>/     per-bug artifacts + dashboards
```

---

## 7. Metrics

- **pass@1** and **pass@k** on a fixed subset (primary).
- **Rounds-to-fix** distribution (how much the repair loop helps).
- **ASAN ablation**: pass@k with vs without the sanitizer trace (Phase 2).
- **Critic ablation**: pass@k with vs without structured diagnosis (Phase 3).
- **Token cost** per fixed bug (prompt + completion).
- Break down all of the above **per project** and **per error class**.

---

## 8. Models (open-source, per requirement)

Served behind one OpenAI-compatible endpoint so client code is model-agnostic:
- **DeepSeek-Coder-6.7B-Instruct** — strong bug-repair, fill-in-middle training.
- **CodeLlama-13B-Instruct** (Q4) — Meta code model, good reasoning.
- **Qwen2.5-Coder-7B-Instruct** — strongest 7B code model (PIE's default), as a
  ceiling reference.
Start with one (DeepSeek-Coder-6.7B) to establish the loop, then compare.

---

## 9. Risks / open issues

- **ASAN false negatives**: some Defects4C bugs are logic bugs with no memory
  error — ASAN adds nothing there. Keep the non-ASAN path as fallback; route by
  whether a report was produced.
- **ASAN build cost / flakiness**: sanitizer builds are slower and some projects
  may not link cleanly with ASAN. Budget for a per-project allowlist.
- **Server-side concurrency**: `/fix` runs real test suites (heavy). Rate-limit
  agent verification calls; rely on Redis caching for repeats.
- **Patch extraction mismatches**: `build_patch` can fail with
  `err_context_mismatch_byte_range` if the LLM reformats surrounding code. May
  need to constrain the model to edit only the target hunk.
- **Determinism**: fix seed + temperature; record them in every artifact.

---

## 10. Immediate next step

Confirm the 5 decisions in §4, then implement **Phase 0** (scaffolding + smoke
test against the running service and one open model). Do not start Phase 2 (ASAN)
until Phase 1's pass@k baseline is on disk.
