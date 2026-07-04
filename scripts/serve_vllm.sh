#!/usr/bin/env bash
# Serve deepseek-coder-6.7b under vLLM for the agent. Run from the repo root
# after `python scripts/make_fixed_tokenizer.py`.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
[ -d .venv ] && source .venv/bin/activate

MODEL="${APR_MODEL:-deepseek-ai/deepseek-coder-6.7b-instruct}"
# No CUDA toolkit (nvcc) on many GPU boxes, only the driver — force native kernels
# instead of flashinfer/triton JIT-compiled ones, which need nvcc.
export VLLM_USE_FLASHINFER_SAMPLER=0
export VLLM_ATTENTION_BACKEND=FLASH_ATTN

exec python -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" \
    --served-model-name "$MODEL" \
    --tokenizer "$HERE/deepseek_tokenizer_fixed" \
    --port "${APR_LLM_PORT:-8888}" \
    --max-model-len "${APR_MAX_MODEL_LEN:-15360}" \
    --gpu-memory-utilization "${APR_GPU_UTIL:-0.95}" \
    --enforce-eager --safetensors-load-strategy lazy
