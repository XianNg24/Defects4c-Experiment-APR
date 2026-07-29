# agentic_apr — an evidence-driven repair agent for Defects4C

An agentic automated-program-repair (APR) system for the **Defects4C** C/C++
benchmark. Given a buggy function, the agent reproduces the failure, **triages
what actually went wrong**, applies whichever diagnostic tools fit (a sanitizer
rebuild for a crash, a test diff for an assertion, …), asks an LLM for a fix,
verifies it against the real test suite, and — if it fails — runs a **Critic**
that turns the failure into structured feedback for the next round. Every run
writes fully inspectable artifacts and a self-contained HTML dashboard.

It is **label-free**: nothing keys off the dataset's bug/vulnerability tag. Tool
use is decided from the *observed* failure, so an ordinary bug that segfaults and
a memory-safety CVE flow through the same pipeline.

```
 observe ─▶ triage ─▶ seed tools (+ LLM may request more) ─▶ generate(k) ─▶ verify
 (repro log)  (classify)   (sanitizer / test-diff / …)          │             │
                                                      ┌── all-k-fail ◀─────────┘
                                                      ▼
                                                 Critic ──▶ next round (bounded)
```

## Why this is more than a wrapper

Two correctness issues that quietly inflate most naive setups are handled here:

- **Pass/fail oracle.** A real fix requires `return_code == 0` **and**
  `fix_status == "success"`. The harness test script exits `0` even when a *test*
  fails (only *build* failures set `rc=1`), and `fix_status` can be stale on a
  build failure — so neither field alone is trustworthy. Using `return_code`
  alone (as the reference `http_tutorial.py` does) reports false positives.
- **Patch extraction.** The task is single-line *infill*, and the model answers
  "the buggy line was `X` … the correct line is `Y`" — buggy first, fix last.
  `build_patch` extracts the *first* fenced block, so the naive path inserts the
  buggy line. The agent passes the **last** fenced block (the fix).

The dashboard shows the true verdict next to the `return_code`-only one, and each
bug's model attempt next to the **ground-truth reference fix** (`git` diff of the
fixing commit, computed post-hoc — never shown to the agent).

## Repository layout

```
agentic_apr/
├── run.py              CLI entrypoint
├── config.py           env-overridable endpoints, model, budgets, toggles
├── harness_client.py   HTTP client for the Defects4C service (new_main.py)
├── llm.py              OpenAI-compatible model client (vLLM/OpenAI)
├── agent_state.py      per-bug trajectory (attempts, verdicts, diagnosis)
├── graph.py            LangGraph orchestrator (observe→triage→tools→generate→verify→critic)
├── triage.py           classify an observed failure (crash/assertion/compile/…)
├── tools.py            diagnostic tool registry (sanitizer_rebuild, test_diff, …)
├── asan_parse.py       parse ASan/UBSan reports → structured dict
├── critic.py           Phase 3 Critic: structured failure feedback (+ JSONL cache)
├── ground_truth.py     reference fix via git (dashboard-only; never fed to the agent)
├── artifacts.py        per-bug artifact writers
├── viz.py              self-contained HTML timeline + aggregate dashboard
├── scripts/
│   ├── serve_vllm.sh          launch the model endpoint
│   └── make_fixed_tokenizer.py rebuild the corrected tokenizer dir
├── harness/new_main.patch     required Defects4C harness fixes (see Setup)
└── docs/DESIGN.md             the phased design spec
```

## Prerequisites

- **Docker** (for the Defects4C harness container).
- A CUDA **GPU** for the model. The reference config fits a ~7B model on a 24 GB
  card (NVIDIA L4). Only the driver is required — **no CUDA toolkit** (the serve
  script forces native kernels so nothing needs `nvcc`).
- Host RAM: 16 GB works but is tight — **add swap** (e.g. a 32 GB swapfile). vLLM
  stages the fp16 weights in host RAM during load; sanitizer builds are memory-heavy.
- Python 3.10+ on the host for the agent; `uv` (or `pip`) for the venv.

## Setup

### 1. Defects4C harness

Clone and build the benchmark (see its README for full detail):

```bash
git clone https://github.com/defects4c/defects4c.git
cd defects4c
bash step1_build_docker.sh          # build the image
# run the container, mapping host 11111 -> container 80, with the data volumes:
docker run -d --name my_defects4c -p 11111:80 \
  -v "$PWD/out_tmp_dirs:/out" -v "$PWD/patche_dirs:/patches" \
  -v "$PWD/defectsc_tpl:/src" -v "$PWD/LLM_Defects4C:/src2" \
  base/defect4c:latest
docker exec my_defects4c bash -lc 'cd /src && bash bulk_git_clone_v2.sh'  # fetch project sources
```

**Apply the required harness fixes** (`harness/new_main.patch`) — three additive
changes to `new_main.py`: (a) `asyncio.to_thread` → `run_in_executor` (the
container is Python 3.8, which lacks `to_thread`); (b) an opt-in `sanitize=` build
path for `/reproduce`; (c) a redis-backed `/reproduce` handle so `/status` works
across gunicorn workers.

```bash
cd defects4c && git apply /path/to/agentic_apr/harness/new_main.patch
# reload the web service inside the container:
docker exec my_defects4c bash -lc 'kill -HUP $(pgrep -f gunicorn | sort -n | head -1)'
curl -s http://127.0.0.1:11111/health        # -> {"status":"ok"}
```

> If `/reproduce` or `/fix` polling ever 404s, redis in the container was likely
> OOM-killed; restart it: `docker exec my_defects4c redis-server --daemonize yes
> --maxmemory 512mb --maxmemory-policy allkeys-lru`.

### 2. Model endpoint (vLLM)

Serve an OpenAI-compatible endpoint. The tokenizer fix is **required** for
deepseek-coder under transformers 5.x (otherwise whitespace leaks as `Ġ`/`Ċ` and
corrupts every patch):

```bash
uv venv .venv-vllm && source .venv-vllm/bin/activate
uv pip install vllm
python scripts/make_fixed_tokenizer.py        # writes ./deepseek_tokenizer_fixed
bash scripts/serve_vllm.sh                     # serves on :8888
```

Any OpenAI-compatible endpoint works — just set `OPENAI_BASE_URL` / `OPENAI_MODEL`.

### 3. The agent

```bash
cd agentic_apr
uv venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt
python harness_client.py --smoke   # read-only: lists bugs, prints one prompt
python llm.py --smoke              # checks the model endpoint
```

## Usage

```bash
# one bug
python run.py --bug-id CESNET___libyang@7c7783df... --k 1

# a subset: k candidates, one repair round, Critic on
python run.py --project CESNET___libyang --limit 6 --k 3 --repair-rounds 1

# ablations
python run.py --limit 20 --no-critic             # raw log feedback instead of the Critic
python run.py --limit 20 --no-diagnose           # skip the triage/tools step
python run.py --limit 20 --no-sanitizer-rebuild  # keep diagnosis, skip the slow ASan build

# build the dashboard for a run (writes index.html + <bug>/timeline.html)
python viz.py runs/run_YYYYMMDD-HHMMSS
```

Key flags: `--k` (candidates/round), `--repair-rounds` (Critic loop budget),
`--model`, `--limit`, `--project`.

## Configuration (all env-overridable — see `config.py`)

| Env var | Default | Meaning |
|---|---|---|
| `DEFECTS4C_BASE_URL` | `http://127.0.0.1:11111` | harness service |
| `OPENAI_BASE_URL` | `http://127.0.0.1:8888/v1/` | model endpoint |
| `OPENAI_MODEL` | `deepseek-ai/deepseek-coder-6.7b-instruct` | model id |
| `APR_K` | `1` | candidates per round |
| `APR_REPAIR_ROUNDS` | `0` | Critic → re-generate cycles |
| `APR_DIAGNOSE` | `1` | run observe→triage→tools |
| `APR_SANITIZER_REBUILD` | `1` | allow the (slow) sanitizer rebuild tool |
| `APR_CRITIC` | `1` | structured Critic feedback on all-k-fail |
| `APR_EXCLUDE_SUBSTR` | `llvm___llvm` | skip CPU-heavy defects |

## Artifacts

Each run writes `runs/<ts>/`:

```
run_meta.json            params
results.jsonl            one row per bug
<bug_id>/
  defect.json            raw /get_defect
  diagnosis.json         triage evidence + tools used
  round<r>_cand<c>_{prompt.txt,response.txt,.diff}
  verdict_round<r>_cand<c>.json
  trace.json             full trajectory (source of truth for the dashboard)
```

`viz.py` renders these into a self-contained, light/dark HTML dashboard: KPI
tiles (true pass@k, false positives), a per-project breakdown, and a per-bug
timeline with the diagnosis, each round's prompt/diff/verdict, the reference fix,
and any Critic notes.

## Status

- **Phase 1** — minimal LangGraph repair loop (k candidates, repair rounds) ✅
- **Phase 2** — evidence-driven diagnosis: triage + tool registry (ASan/UBSan
  reproduce, sanitizer parser), hybrid deterministic-seed + LLM tool requests ✅
- **Phase 3** — Critic (structured feedback on all-k-fail, disk-cached) ✅
- **Phase 5** — visualization (per-bug timeline + aggregate dashboard) ✅
- **Phase 4** — cross-bug memory `(project, error_class) → lessons` — planned

See `docs/DESIGN.md` for the full phased plan.
