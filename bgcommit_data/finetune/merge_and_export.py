#!/usr/bin/env python3
"""Merge a trained LoRA adapter into its base model -> a standalone HF model directory
that vLLM (or the agent) can serve directly, no PEFT at inference.

    python merge_and_export.py --adapter output/deepseek-coder-6.7b-instruct-bgcommit-lora

Reads base_model from the adapter's training_metadata.json (or --base). Writes a full
fp16 model to <adapter>-merged/, then you can:  bash scripts/serve_vllm.sh <that dir>
"""
import argparse
import json
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True, help="dir with adapter_model.safetensors")
    ap.add_argument("--base", default=None, help="override base model id")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    meta_p = os.path.join(args.adapter, "training_metadata.json")
    base = args.base or (json.load(open(meta_p))["base_model"] if os.path.exists(meta_p) else None)
    if not base:
        raise SystemExit("no base model — pass --base or ensure training_metadata.json exists")
    out = args.out or args.adapter.rstrip("/") + "-merged"

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel

    print(f"base:    {base}\nadapter: {args.adapter}\nout:     {out}")
    model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.float16, device_map="cpu")
    model = PeftModel.from_pretrained(model, args.adapter)
    model = model.merge_and_unload()                     # bake LoRA into the base weights
    model.save_pretrained(out, safe_serialization=True)
    AutoTokenizer.from_pretrained(args.adapter).save_pretrained(out)
    print(f"\n✓ standalone model -> {out}")
    print(f"  serve: bash scripts/serve_vllm.sh {os.path.abspath(out)}")


if __name__ == "__main__":
    main()
