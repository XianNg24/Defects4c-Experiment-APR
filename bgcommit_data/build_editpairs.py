#!/usr/bin/env python3
"""Build hunk-level buggy->fixed edit pairs from the bgcommit Step-2 tar, restricted to
the Step-3 76K single-function index. Streams the 9GB tar once (reads only matched
members — no mass extraction), parses each GitHub commit's source-file patch into a
(buggy hunk, fixed hunk) pair, and writes a jsonl training sample.
"""
import ast
import json
import os
import re
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
TAR = os.path.join(HERE, "full_9M_step2.tar")
INDEX = os.path.join(HERE, "full_76K_step3.1-3.2_filtering.txt")
OUT = os.path.join(HERE, "bgcommit_editpairs_sample.jsonl")

_SRC = (".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx", ".cu")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(.*)$")


def is_source(fn: str) -> bool:
    f = fn.lower()
    if any(t in f for t in ("test", "/tests/", "3rdparty", "third_party", "vendor")):
        return False
    return f.endswith(_SRC)


def first_hunk(patch: str):
    """(func_context, buggy, fixed) from the FIRST hunk of a GitHub patch, or None."""
    lines = patch.splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith("@@")]
    if not starts:
        return None
    i = starts[0]
    end = starts[1] if len(starts) > 1 else len(lines)
    func = _HUNK.match(lines[i])
    func = func.group(1).strip() if func else ""
    buggy, fixed = [], []
    for l in lines[i + 1:end]:
        if not l:
            buggy.append(""); fixed.append(""); continue
        tag, body = l[0], l[1:]
        if tag == " ":
            buggy.append(body); fixed.append(body)
        elif tag == "-":
            buggy.append(body)
        elif tag == "+":
            fixed.append(body)
    if buggy == fixed:                       # no actual change captured
        return None
    return func, "\n".join(buggy), "\n".join(fixed)


def main():
    keys = set()
    for line in open(INDEX, errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            path, _ = ast.literal_eval(line)
            keys.add(os.path.basename(path))
        except Exception:  # noqa: BLE001
            pass
    print(f"76K index keys: {len(keys)}")

    seen = matched = written = 0
    with tarfile.open(TAR, "r") as tar, open(OUT, "w") as out:
        for m in tar:
            seen += 1
            if seen % 200000 == 0:
                print(f"  scanned {seen:,} members, matched {matched}, written {written}")
            if not m.isfile() or os.path.basename(m.name) not in keys:
                continue
            matched += 1
            try:
                d = json.load(tar.extractfile(m))
            except Exception:  # noqa: BLE001
                continue
            files = [f for f in (d.get("files") or []) if is_source(f.get("filename", ""))
                     and f.get("patch")]
            if len(files) != 1:              # keep it a clean single-source-file edit
                continue
            fh = first_hunk(files[0]["patch"])
            if not fh:
                continue
            func, buggy, fixed = fh
            msg = ((d.get("commit") or {}).get("message") or "").strip()
            repo = "/".join(d.get("url", "").split("/repos/")[-1].split("/")[:2])
            out.write(json.dumps({
                "repo": repo, "sha": d.get("sha", "")[:12],
                "file": files[0]["filename"], "func_context": func,
                "commit_message": msg[:400],
                "buggy": buggy, "fixed": fixed,
            }) + "\n")
            written += 1
    print(f"\nscanned {seen:,} members | matched {matched} of the 76K | wrote {written} edit pairs")
    print(f"-> {OUT}  ({os.path.getsize(OUT)//1024} KB)")


if __name__ == "__main__":
    main()
