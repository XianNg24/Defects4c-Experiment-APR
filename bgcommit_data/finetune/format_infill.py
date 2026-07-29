#!/usr/bin/env python3
"""Format bgcommit edit pairs as INFILL examples that match the Defects4C eval task,
and DECONTAMINATE against the 151 test commits.

The first fine-tune failed (18 -> 1) because it trained on whole-hunk 'buggy -> fixed'
where the two overlap ~90%, so the model learned to ECHO its input. Here we instead:
  - diff each pair to the changed span,
  - mask that span in the FIXED code with '>>> [ INFILL ] <<<' (the eval prompt shape),
  - show the buggy span as a hint, and make the TARGET the fixed span.
Because target != hint by construction, the model must *transform*, not reproduce.

Also drops any pair whose commit sha is one of the 151 benchmark test commits (leakage).
"""
import difflib
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "bgcommit_editpairs_sample.jsonl")
OUT = os.path.join(HERE, "data")
SYSTEM = "You are a C/CPP code program repair expert"
_SKIP_MSG = re.compile(r"^\s*(merge|revert|bump|format|clang-format|whitespace|"
                       r"rename|typo|indent|style|update)\b", re.I)
INFILL = ">>> [ INFILL ] <<<"


def test_shas() -> set:
    """First-12 shas of the 151 benchmark bugs — to exclude from training. Read from any
    existing run's results.jsonl (the full 151 bug ids), no harness needed."""
    import glob
    runs = os.path.join(os.path.dirname(os.path.dirname(HERE)), "runs")
    for rj in sorted(glob.glob(os.path.join(runs, "*", "results.jsonl")),
                     key=os.path.getsize, reverse=True):
        ids = [json.loads(l).get("bug_id", "") for l in open(rj) if l.strip()]
        ids = [b for b in ids if "@" in b]
        if len(ids) >= 150:
            return {b.split("@")[1][:12] for b in ids}
    raise SystemExit("no run with >=150 bug ids found to derive the test set")


def make_infill(buggy: str, fixed: str):
    """(context_with_hole, buggy_span_hint, fixed_span_target) or None."""
    bl, fl = buggy.split("\n"), fixed.split("\n")
    ops = [op for op in difflib.SequenceMatcher(None, bl, fl, autojunk=False).get_opcodes()
           if op[0] != "equal"]
    if not ops:
        return None
    i1, i2 = ops[0][1], ops[-1][2]           # changed span in buggy
    j1, j2 = ops[0][3], ops[-1][4]           # changed span in fixed
    target = fl[j1:j2]
    hint = bl[i1:i2]
    if not target:                            # pure deletion — nothing to predict
        return None
    indent = re.match(r"\s*", target[0]).group(0)
    context = fl[:j1] + [indent + INFILL] + fl[j2:]
    return "\n".join(context), "\n".join(hint), "\n".join(target)


def to_chat(r, ctx, hint, target):
    # verbatim to the Defects4C eval prompt scaffolding (intro / function-with-infill /
    # "original buggy hunk which was removed…" / "// buggy hunk" block / closing line),
    # so training and inference are the same shape. hint is always present (replace-only).
    user = ("\nThe following code contains a buggy hunk that has been removed.\n"
            f"```cpp\n{ctx}\n```\n"
            "This was the original buggy hunk which was removed by the infill location\n"
            f"```cpp\n// buggy hunk\n{hint}\n```\n\n\n"
            "Please provide the correct line following commit message at the infill location.")
    return {"messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
        {"role": "assistant", "content": f"```cpp\n{target}\n```"},
    ], "meta": {"repo": r.get("repo"), "sha": r.get("sha"), "file": r.get("file")}}


def main():
    import random
    leak = test_shas()
    rows = [json.loads(l) for l in open(SRC) if l.strip()]
    kept, dropped_leak, dropped_echo, dropped_insert = [], 0, 0, 0
    for r in rows:
        if (r.get("sha") or "")[:12] in leak:
            dropped_leak += 1
            continue
        b, f = r.get("buggy", ""), r.get("fixed", "")
        msg = (r.get("commit_message") or "").strip()
        if not b.strip() or not f.strip() or b == f or not msg or _SKIP_MSG.match(msg):
            continue
        if max(len(b.splitlines()), len(f.splitlines())) > 60:
            continue
        mk = make_infill(b, f)
        if not mk:
            continue
        ctx, hint, target = mk
        if not hint.strip():
            dropped_insert += 1               # pure insertion: no buggy line to show as a
            continue                          # hint -> can't match eval's always-present hint
        if not target.strip() or re.sub(r"\s+", "", target) == re.sub(r"\s+", "", hint):
            dropped_echo += 1                 # target == hint -> would teach echo; drop
            continue
        if len(target) > 1200:
            continue
        kept.append(to_chat(r, ctx, hint, target))

    random.seed(0)
    random.shuffle(kept)
    n_val = max(50, len(kept) // 20)
    val, train = kept[:n_val], kept[n_val:]
    os.makedirs(OUT, exist_ok=True)
    for name, part in (("train", train), ("val", val)):
        with open(os.path.join(OUT, f"{name}.jsonl"), "w") as fp:
            for ex in part:
                fp.write(json.dumps(ex) + "\n")
    print(f"input pairs:          {len(rows)}")
    print(f"dropped (leakage):    {dropped_leak}   (insert-type, no hint): {dropped_insert}   "
          f"(target==hint echo): {dropped_echo}")
    print(f"kept replace-type infill examples (all with buggy hint): {len(kept)}")
    print(f"train / val:          {len(train)} / {len(val)}")
    print(f"wrote -> {OUT}/train.jsonl , {OUT}/val.jsonl")


if __name__ == "__main__":
    main()
