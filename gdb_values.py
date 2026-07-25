"""Capture runtime variable values at the buggy line via gdb (feasibility prototype).

For a value-dependent defect (the masked line is a condition/comparison), this breaks the
failing test at the buggy source line inside the container, and prints the values of the
identifiers that line references plus the local frame. The intent is a *dynamic* evidence
block — the actual program state at the point of failure — to complement the static
diagnosis. Prototype scope: resolves the failing-test executable for cppcheck (own
runner) and GoogleTest (via ctest); other frameworks return an "unresolved" marker.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess

import config

CONTAINER = os.environ.get("APR_CONTAINER", "my_defects4c")
_IDENT = re.compile(r"[A-Za-z_]\w+")
_SKIP = {"if", "for", "while", "switch", "else", "return", "const", "struct", "int", "char",
         "void", "unsigned", "long", "short", "size_t", "sizeof", "true", "false", "NULL",
         "nullptr", "static", "do", "case", "break", "continue", "auto", "bool", "new",
         "delete", "std", "this", "enum", "union", "double", "float", "and", "or", "not"}


def _defect(bug_id: str) -> dict | None:
    d = glob.glob(f"runs/*/{bug_id}/defect.json")
    if not d:
        return None
    pd = json.load(open(d[0]))
    meta = pd["additional_info"]["metadata"]
    files = meta.get("files", {})
    src = (files.get("src") or [None])[0]
    lineno = files.get("src0_location", {}).get("line_number")
    txt = pd["prompt_data"]["prompt"][-1]["content"]
    import graph
    m = graph._BUGGY_HUNK.search(txt)
    phys = [l for l in m.group(1).splitlines()
            if l.strip() and not l.strip().startswith("//")] if m else []
    line = " ".join(phys)
    proj, sha = bug_id.split("@")
    return {"proj": proj, "sha": sha, "src": src, "lineno": lineno, "line": line,
            "first_line": phys[0].strip() if phys else ""}


def _real_lineno(src_container: str, first_line: str, meta_lineno: int) -> int:
    """Match the buggy line's text in the container source to find its ACTUAL line number
    (metadata line numbers drift; else-if/brace lines are poor breakpoint targets). Returns
    the matching line nearest the metadata line, or the metadata line if no clean match."""
    frag = first_line.strip().rstrip("{").strip()
    frag = frag[4:].strip() if frag.startswith("else ") else frag   # break on the condition
    if len(frag) < 6:
        return meta_lineno
    try:
        r = _in_container(["grep", "-nF", frag, src_container], 30)
    except Exception:  # noqa: BLE001
        return meta_lineno
    nums = [int(x.split(":", 1)[0]) for x in r.stdout.splitlines() if ":" in x
            and x.split(":", 1)[0].isdigit()]
    return min(nums, key=lambda n: abs(n - meta_lineno)) if nums else meta_lineno


def _in_container(argv: list, timeout: int = 200) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", CONTAINER] + argv,
                          capture_output=True, text=True, timeout=timeout)


def _failing_test_cmd(bug_id: str) -> tuple | None:
    """(exe, [args]) that reproduces the failure, resolved from the saved repro log."""
    logs = glob.glob(f"runs/*/{bug_id}/observed_repro.log")
    if not logs:
        return None
    log = open(max(logs, key=os.path.getmtime), errors="replace").read()
    proj, sha = bug_id.split("@")
    build = f"/out/{proj}/git_repo_dir_{sha}/build_{sha}"

    # (Fix 1) The ctest test(s) that failed — from the per-test result line (most reliable)
    # and the summary line, covering assertion, SEGFAULT and exception outcomes.
    failed = re.findall(r"Test\s+#\d+:\s*(\S+)\s*\.*\*\*\*(?:Failed|Exception|SEGFAULT)", log)
    failed += re.findall(r"^\s*\d+\s*-\s*(\S+)\s*\((?:Failed|SEGFAULT|Exception|Timeout)",
                         log, re.M)
    # the specific case, to narrow the run: gtest 'Suite.Test' or cmocka '[ FAILED ] name'
    gt = re.search(r"\[\s*FAILED\s*\]\s+([A-Za-z_]\w*)\.([A-Za-z_/]\w*)", log)
    cm = re.search(r"\[\s*FAILED\s*\]\s+(test_\w+)", log)

    # cppcheck: its runner takes the test class name directly.
    if "cppcheck" in proj:
        m = re.search(r"\((\w+)::\w+\):\s*Assertion failed", log)
        cls = (m.group(1) if m else None) or (failed[0] if failed else None)
        if cls and cls != "testrunner":
            return (f"{build}/bin/testrunner", [cls])
        if cls == "testrunner":                 # single catch-all ctest test
            return (f"{build}/bin/testrunner", [])

    if not failed:
        return None                             # (Fix 1) don't run an arbitrary wrong test

    # resolve the failing ctest test's real command (GoogleTest, cmocka, …).
    try:
        j = _in_container(["ctest", "--test-dir", build, "--show-only=json-v1"], 60)
        tests = json.loads(j.stdout).get("tests", [])
        entry = next((t for t in tests if t.get("name") in set(failed)), None)
        if entry and entry.get("command"):
            cmd = entry["command"]
            # skip non-test commands (lint/copyright wrappers) that ctest may list
            if any(s in " ".join(cmd) for s in ("python", "copyright", "lint", ".py")):
                return None
            exe, extra = cmd[0], cmd[1:]
            if gt and not any("gtest_filter" in a for a in extra):
                extra = [f"--gtest_filter={gt.group(1)}.{gt.group(2)}"]
            elif cm and not extra:              # cmocka: filter via env, applied in capture()
                return (exe, extra, {"CMOCKA_TEST_FILTER": cm.group(1)})
            return (exe, extra)
    except Exception:  # noqa: BLE001
        pass
    return None


_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gdb_values_cache")


def capture(bug_id: str, use_cache: bool = True) -> str:
    """Cached wrapper: gdb capture runs once per defect (identical across candidates/runs)."""
    cp = os.path.join(_CACHE_DIR, re.sub(r"[^\w.-]", "_", bug_id)[:90] + ".txt")
    if use_cache and os.path.exists(cp):
        return open(cp, errors="replace").read()
    try:
        out = _capture(bug_id)
    except Exception as e:  # noqa: BLE001 — never break a run over the gdb block
        out = f"(gdb capture error: {e})"
    if use_cache:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        try:
            open(cp, "w").write(out)
        except OSError:
            pass
    return out


def _capture(bug_id: str) -> str:
    info = _defect(bug_id)
    if not info or not info.get("src") or not info.get("lineno"):
        return "(no source location in metadata)"
    cmd = _failing_test_cmd(bug_id)
    if not cmd:
        return "(could not resolve failing-test executable)"
    exe, args = cmd[0], cmd[1]
    env = cmd[2] if len(cmd) > 2 else {}
    base = os.path.basename(info["src"])
    line = info["line"]
    # (Fix 2) use the buggy line's real location in the container source, not the metadata
    # line number (which drifts and often lands on a non-breakable brace/else-if line).
    src_container = f"/out/{info['proj']}/git_repo_dir_{info['sha']}/{info['src']}"
    real_line = _real_lineno(src_container, info["first_line"], info["lineno"])
    idents = [i for i in dict.fromkeys(_IDENT.findall(line)) if i not in _SKIP][:6]
    # member-access / call chains on the buggy line (e.g. tok->tokAt(6),
    # condTok->values().front().intvalue) — the actionable sub-expressions, evaluated with
    # gdb's expression evaluator (read-only accessors).
    exprs = re.findall(r"[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*|\s*\([^()]*\))+", line)
    exprs = [re.sub(r"\s+", "", e) for e in dict.fromkeys(exprs)][:6]

    gdb = ["gdb", "-batch",
           "-ex", "set pagination off", "-ex", "set width 0", "-ex", "set confirm off",
           # (Fix 2) break at the real line plus its immediate neighbours, so an
           # else-if/brace or off-by-one still lands on executable code near the defect.
           "-ex", f"break {base}:{real_line}",
           "-ex", f"break {base}:{real_line + 1}",
           "-ex", "run",
           "-ex", "bt 2"]
    for e in exprs + [i for i in idents if i not in exprs]:
        # label each value with its expression; `output` prints the value without a $N tag
        gdb += ["-ex", f'printf "\\nVAL {e} = "', "-ex", f"output {e}"]
    gdb += ["-ex", "quit", "--args", exe] + args
    envargs = [x for k, v in env.items() for x in ("-e", f"{k}={v}")]
    try:
        r = subprocess.run(["docker", "exec"] + envargs + [CONTAINER] + gdb,
                           capture_output=True, text=True, timeout=200)
    except subprocess.TimeoutExpired:
        return "(gdb timed out — buggy line likely not reached, or test hangs)"
    out = r.stdout
    hit = re.search(r"Breakpoint [12],", out) is not None
    placed = "Breakpoint 1 at" in out or "Breakpoint 2 at" in out
    vals = [ln[4:].rstrip() for ln in out.splitlines() if ln.startswith("VAL ")]
    frame = next((ln.rstrip() for ln in out.splitlines() if ln.strip().startswith("#0")), "")
    if not hit:
        if not placed:
            why = "breakpoint could not be placed (no code at that line)"
        elif "SIGSEGV" in out or "SIGABRT" in out:
            why = "the test crashed before reaching the line"
        else:
            why = "buggy line not reached by this test"
        return f"(gdb ran but did not stop at {base}:{real_line} — {why})"
    body = "\n".join(v for v in vals if "No symbol" not in v and v.split("=", 1)[-1].strip())
    return (f"Runtime state at the buggy line ({base}:{real_line}):\n"
            f"// line: {info['line']}\n"
            + (f"// frame: {frame.strip()[:120]}\n" if frame else "")
            + (body or "(no in-scope values captured)"))


if __name__ == "__main__":
    import sys
    print(capture(sys.argv[1]))
