#!/usr/bin/env python
"""Build a corrected tokenizer dir for deepseek-coder to serve under vLLM.

Why: transformers 5.x's AutoTokenizer resolves this model to a broken
LlamaTokenizer that applies Llama SentencePiece (▁) decode semantics to a
GPT-2 ByteLevel (Ġ/Ċ) tokenizer, so all whitespace is dropped / leaks as Ġ/Ċ in
model output (which corrupts every patch). The tokenizer.json itself is fine.
Fix: copy the tokenizer files and set `tokenizer_class` to PreTrainedTokenizerFast
so it uses the ByteLevel decoder defined in tokenizer.json.

    python scripts/make_fixed_tokenizer.py            # writes ./deepseek_tokenizer_fixed
Then serve vLLM with:  --tokenizer /abs/path/to/deepseek_tokenizer_fixed
"""
import json
import os
import shutil
import sys

MODEL = os.environ.get("APR_MODEL", "deepseek-ai/deepseek-coder-6.7b-instruct")
DST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "deepseek_tokenizer_fixed")


def main() -> int:
    from huggingface_hub import snapshot_download
    src = snapshot_download(MODEL, allow_patterns=["tokenizer*.json",
                                                   "special_tokens_map.json"])
    os.makedirs(DST, exist_ok=True)
    for f in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
        p = os.path.join(src, f)
        if os.path.exists(p):
            shutil.copy(p, DST)
    cfg_path = os.path.join(DST, "tokenizer_config.json")
    cfg = json.load(open(cfg_path))
    cfg["tokenizer_class"] = "PreTrainedTokenizerFast"   # use ByteLevel decode, not Llama
    json.dump(cfg, open(cfg_path, "w"), indent=2)

    # sanity check: round-trip must preserve whitespace
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(DST)
    s = "hello world\n  indented"
    ok = tok.decode(tok.encode(s)) == s
    print(f"wrote {DST}")
    print("whitespace round-trip:", "OK" if ok else "STILL BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
