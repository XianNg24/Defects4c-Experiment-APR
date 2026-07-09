#!/usr/bin/env bash
# Serve an instruct code model under vLLM for the agent. Pick the model as the
# first CLI arg (or via APR_MODEL; default deepseek-coder-6.7b). Examples:
#   bash scripts/serve_vllm.sh codellama/CodeLlama-7b-Instruct-hf
#   bash scripts/serve_vllm.sh Qwen/Qwen2.5-Coder-7B-Instruct
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
[ -d .venv ] && source .venv/bin/activate

MODEL="${1:-${APR_MODEL:-deepseek-ai/deepseek-coder-6.7b-instruct}}"
# No CUDA toolkit (nvcc) on many GPU boxes, only the driver — force native kernels
# instead of flashinfer/triton JIT-compiled ones, which need nvcc.
export VLLM_USE_FLASHINFER_SAMPLER=0
export VLLM_ATTENTION_BACKEND=FLASH_ATTN

# Only deepseek-coder needs the corrected tokenizer (its GPT-2 ByteLevel tokenizer
# decodes wrong under transformers 5.x). Other models use their own tokenizer.
TOKENIZER_ARG=()
if [[ "$MODEL" == *deepseek-coder* && -d "$HERE/deepseek_tokenizer_fixed" ]]; then
    TOKENIZER_ARG=(--tokenizer "$HERE/deepseek_tokenizer_fixed")
fi

exec python -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" \
    --served-model-name "$MODEL" \
    "${TOKENIZER_ARG[@]}" \
    --port "${APR_LLM_PORT:-8888}" \
    --dtype "${APR_DTYPE:-auto}" \
    --max-model-len "${APR_MAX_MODEL_LEN:-15360}" \
    --gpu-memory-utilization "${APR_GPU_UTIL:-0.95}" \
    --enforce-eager --safetensors-load-strategy lazy
