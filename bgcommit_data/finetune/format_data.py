#!/usr/bin/env python3
"""Format bgcommit edit pairs into chat-style repair examples for QLoRA SFT.

Input : bgcommit_data/bgcommit_editpairs_sample.jsonl  (buggy/fixed hunks + metadata)
Output: finetune/data/{train,val}.jsonl  as {"messages":[system,user,assistant]}

The chat shape mirrors the Defects4C inference prompt (system persona + "here is buggy
C/C++, return the fix"), so training and inference speak the same format. Only the
assistant turn (the fix) is the learning target — the training script masks the prompt.
"""
import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "bgcommit_editpairs_sample.jsonl")
OUT = os.path.join(HERE, "data")

SYSTEM = "You are a C/CPP code program repair expert"
# light quality filter: drop non-fix noise so the model learns repair, not churn
_SKIP_MSG = re.compile(r"^\s*(merge|revert|bump|format|clang-format|whitespace|"
                       r"rename|typo|indent|style|update)\b", re.I)


def usable(r: dict) -> bool:
    b, f = r.get("buggy", ""), r.get("fixed", "")
    if not b.strip() or not f.strip() or b == f:
        return False
    msg = (r.get("commit_message") or "").strip()
    if not msg or _SKIP_MSG.match(msg):
        return False
    nb, nf = len(b.splitlines()), len(f.splitlines())
    if max(nb, nf) > 60 or max(len(b), len(f)) > 4000:   # keep hunks context-friendly
        return False
    return True


def to_chat(r: dict) -> dict:
    user = ("The following C/C++ code contains a bug. Return the corrected code, "
            "changing only what is necessary to fix it.\n"
            f"```cpp\n{r['buggy'].strip(chr(10))}\n```")
    assistant = f"```cpp\n{r['fixed'].strip(chr(10))}\n```"
    return {"messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ], "meta": {"repo": r.get("repo"), "sha": r.get("sha"), "file": r.get("file")}}


def main():
    rows = [json.loads(l) for l in open(SRC) if l.strip()]
    kept = [to_chat(r) for r in rows if usable(r)]
    random.seed(0)
    random.shuffle(kept)
    n_val = max(50, len(kept) // 20)              # ~5% val, at least 50
    val, train = kept[:n_val], kept[n_val:]
    os.makedirs(OUT, exist_ok=True)
    for name, part in (("train", train), ("val", val)):
        with open(os.path.join(OUT, f"{name}.jsonl"), "w") as f:
            for ex in part:
                f.write(json.dumps(ex) + "\n")
    print(f"input pairs:        {len(rows)}")
    print(f"kept after filter:  {len(kept)}  ({100*len(kept)/len(rows):.0f}%)")
    print(f"train / val:        {len(train)} / {len(val)}")
    print(f"wrote -> {OUT}/train.jsonl , {OUT}/val.jsonl")


if __name__ == "__main__":
    main()
