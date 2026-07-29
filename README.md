# Defects4C Code Repair — an agentic APR system with domain fine-tuning

An evidence-driven **automated program repair (APR)** agent for the
[Defects4C](https://github.com/defects4c/defects4c) C/C++ benchmark, plus a
**QLoRA fine-tuning** pipeline that adapts open 7B code models to the repair task
on mined bug-fix commits.

Given a buggy function, the agent reproduces the failure, triages what went
wrong, runs whichever diagnostics fit (a sanitizer rebuild for a crash, a test
diff for an assertion, gdb for a value-dependent condition), asks an LLM for a
fix, and verifies it against the real test suite — looping through a *Critic*
when candidates fail. It is **label-free**: nothing keys off the dataset's
bug/vulnerability tag; tool use is chosen from the observed failure. Internals
are documented in [docs/agent-internals.md](docs/agent-internals.md) and
[docs/DESIGN.md](docs/DESIGN.md).

---

## Repository layout

```
.
├── run.py                # CLI entrypoint — run the benchmark
├── graph.py              # observe → triage → tools → generate(k) → verify → critic
├── llm.py                # OpenAI-compatible client (vLLM endpoint)
├── harness_client.py     # HTTP client for the Defects4C service
├── scripts/              # serve_vllm.sh, dashboard builders
├── harness/              # patches applied to the Defects4C harness
├── bgcommit_data/        # fine-tuning data + pipeline (see finetune/README.md)
│   └── finetune/         #   train_qlora.py, format_infill.py, merge_and_export.py
├── docs/                 # design, agent internals, analysis fine-tuning, hallucination
└── runs/                 # experiment outputs (git-ignored)
```

The **Defects4C harness** (the benchmark defects, test oracles, and Docker build
recipes) is a separate upstream project used as an external dependency — see §1.

## Requirements

- Linux, one NVIDIA GPU (developed on an L4, 23 GB — enough for a 7B model under
  vLLM *or* 4-bit QLoRA training, one at a time)
- Docker (for the Defects4C harness container)
- Python 3.10+

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

The same virtualenv is used for the agent, serving, and fine-tuning.

## 1. Dataset & harness

The benchmark defects and their test oracles are served by the
[Defects4C](https://github.com/defects4c/defects4c) harness, which builds each
project inside Docker and exposes an HTTP API (defect listing, `get_defect`,
`fix`, `reproduce`). Clone it as a sibling directory next to this repo (the agent
looks for `../defects4c` by default; override with `DEFECTS4C_OUT_DIR` and
`DEFECTS4C_BASE_URL`):

```bash
cd ..
git clone https://github.com/defects4c/defects4c.git
cd defects4c
bash step1_build_docker.sh          # build the my_defects4c image (one-time, slow)
python http_tutorial.py             # start the HTTP service on :11111
```

The local harness fixes this project relies on (a reconfigure guard, dependency
and service patches) are kept as patches in [harness/](harness/). Verify the
service is up:

```bash
curl http://127.0.0.1:11111/health   # -> {"status":"ok"}
```

## 2. Serve a model

The agent talks to any OpenAI-compatible endpoint. Serve an open 7B code model
with vLLM (the helper handles the nvcc-less flags and the deepseek tokenizer fix):

```bash
bash scripts/serve_vllm.sh deepseek-ai/deepseek-coder-6.7b-instruct
# or codellama/CodeLlama-7b-Instruct-hf, Qwen/Qwen2.5-Coder-7B-Instruct, or a
# fine-tuned merged directory (see §4)
```

This listens on `:8888`. `run.py` auto-syncs to whatever model is being served.

## 3. Run the repair experiment

```bash
source .venv/bin/activate

# full benchmark, best-of-10 sampling at temperature 0.8
python run.py --k 10 --temperature 0.8 --no-sanitizer-rebuild

# a single defect
python run.py --bug-id CESNET___libyang@140ede9c... --k 1

# one project only
python run.py --project the-tcpdump-group___tcpdump --k 5
```

Useful flags: `--baseline` (send the dataset prompt verbatim, no diagnosis or
guidance), `--gdb-values` (inject runtime values for value-dependent defects),
`--seed N` (sampling seed), `--model <path-or-id>` (force a model id). Each run
writes fully-inspectable artifacts (prompts, candidate diffs, verdicts, an HTML
dashboard) under `runs/<run_id>/`.

**Pass oracle.** A real fix requires `return_code == 0` **and**
`fix_status == "success"` — the harness test script exits `0` even when a *test*
fails, so `return_code` alone is a false-positive oracle. `run.py` enforces both.

## 4. Fine-tuning

Adapt a base model to the repair task with 4-bit QLoRA on real C/C++ bug-fix
edit pairs mined from Defects4C's `bgcommit` corpus. Full details in
[bgcommit_data/finetune/README.md](bgcommit_data/finetune/README.md).

```bash
cd bgcommit_data/finetune
source ../../.venv/bin/activate

# 1. build the infill-format training data (masked-hunk → replacement)
python format_infill.py                       # -> data/train.jsonl, data/val.jsonl

# 2. sanity-check the whole pipeline without the GPU
python train_qlora.py --model deepseek-ai/deepseek-coder-6.7b-instruct --dry-run

# 3. train (needs the GPU free — stop vLLM first). ~1–2 hr on the L4.
python train_qlora.py \
    --model deepseek-ai/deepseek-coder-6.7b-instruct \
    --output-dir output/deepseek-coder-6.7b-instruct-bgcommit-infill-lora-ep2 \
    --epochs 2 --lr 5e-5 --lora-r 16 --lora-alpha 32 \
    --batch 4 --grad-accum 4 --max-len 1024

# 4. merge the adapter into a standalone model for vLLM
python merge_and_export.py --adapter output/deepseek-coder-6.7b-instruct-bgcommit-infill-lora-ep2
```

**Epoch note.** The infill format matters — a whole-hunk format teaches the model
to echo the input. On the sweep here, CodeLlama and Qwen converge to the ceiling
in **1 epoch**; deepseek needs **2** (1 epoch is undertrained, 3 mildly overfits).

Then serve the merged model and evaluate it exactly like any other:

```bash
bash scripts/serve_vllm.sh "$(pwd)/output/deepseek-...-infill-lora-ep2-merged"
python ../../run.py --k 10 --temperature 0.8 --no-sanitizer-rebuild
```