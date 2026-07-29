# QLoRA repair fine-tune on bgcommit edit pairs

Fine-tune a 7B code model on real C/C++ bug-fix edit pairs mined from Defects4C's
`bgcommit` corpus, then evaluate on the held-out repair benchmark. Fits the L4
(23 GB) via 4-bit QLoRA. See the [root README](../../README.md) for the
end-to-end flow; this file covers the fine-tuning specifics.

## Data

`../bgcommit_editpairs_sample.jsonl` — hunk-level buggy→fixed pairs joined from
the Step-2 commit tar against the Step-3 single-function index. `format_infill.py`
turns these into the **infill format** used at evaluation time: the changed span
is masked in the fixed code and the model learns to emit the replacement.

```bash
python format_infill.py          # -> data/train.jsonl (~4,980), data/val.jsonl (~262)
```

The infill format is essential. A whole-hunk format (predict the entire fixed
hunk) teaches the model to **echo** the input — a large fraction of the target is
copyable from the prompt — and collapses solve rate. Training and eval use the
same masked-hunk scaffolding so the model never learns to repeat the masked line.

Decontamination is by exact commit SHA against the benchmark's test set (the
benchmark defects are a subset of `bgcommit`); a semantic dedup (UniXcoder cosine)
would be stronger.

## Train

Training needs the whole GPU — **stop vLLM first** (it holds ~21 GB).

```bash
# validate the pipeline without the GPU (data tokenizes, masking + configs build)
python train_qlora.py --model deepseek-ai/deepseek-coder-6.7b-instruct --dry-run

# train (~1–2 hr on the L4)
python train_qlora.py \
    --model deepseek-ai/deepseek-coder-6.7b-instruct \
    --output-dir output/<model>-bgcommit-infill-lora-ep2 \
    --epochs 2 --lr 5e-5 --lora-r 16 --lora-alpha 32 \
    --batch 4 --grad-accum 4 --max-len 1024
```

The recipe is identical across models (lr 5e-5, LoRA r=16 / α=32 / dropout 0.05
on the seven attn+MLP projections, max-len 1024). Output lands in `--output-dir`:
`adapter_model.safetensors` + `adapter_config.json` (the LoRA weights, ~160 MB),
the tokenizer, `training_metadata.json`, and resumable `checkpoint-*/`.

**How many epochs?** From the sweep in [report2.md](../../report2.md): CodeLlama
and Qwen reach the ~49/134 ceiling in **1 epoch**; deepseek needs **2** (1 epoch
is undertrained, 3 epochs mildly overfits the narrow corpus). Start at 2 and
sweep if a base looks flat.

## Merge & serve

```bash
python merge_and_export.py --adapter output/<model>-bgcommit-infill-lora-ep2
# -> output/<model>-bgcommit-infill-lora-ep2-merged/   (fp16, vLLM-ready)

bash ../../scripts/serve_vllm.sh \
    "$(pwd)/output/<model>-bgcommit-infill-lora-ep2-merged"
```

Alternatively load the adapter at inference without merging:
`PeftModel.from_pretrained(base_model, "output/<model>-...-lora-ep2")`.

## Evaluate

Serve the merged model, then run the benchmark exactly as any A/B — `run.py`
auto-syncs to the served model:

```bash
python ../../run.py --k 10 --temperature 0.8 --no-sanitizer-rebuild
```

Compare solved against the stock-model baseline (stock k=10: deepseek 38,
CodeLlama 36, Qwen 28). k=10 sampling variance is ~±3, so use ≥2 seeds
(`--seed`) when a delta is small.
