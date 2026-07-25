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
    line = (" ".join(l for l in m.group(1).splitlines()
                     if l.strip() and not l.strip().startswith("//"))) if m else ""
    proj, sha = bug_id.split("@")
    return {"proj": proj, "sha": sha, "src": src, "lineno": lineno, "line": line}


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

    # The ctest name(s) that failed (covers both assertion and SEGFAULT cases).
    failed = re.findall(r"^\s*\d+\s*-\s*(\S+)\s*\((?:Failed|SEGFAULT|Exception|Timeout)",
                        log, re.M)
    # a specific gtest case, if the log named one, to narrow the run
    g = re.search(r"\[\s*FAILED\s*\]\s+([A-Za-z_]\w*)\.([A-Za-z_/]\w*)", log)

    # cppcheck: its runner takes the test class name directly.
    if "cppcheck" in proj:
        cls = failed[0] if failed else None
        m = re.search(r"\((\w+)::\w+\):\s*Assertion failed", log)
        cls = cls or (m.group(1) if m else None)
        if cls:
            return (f"{build}/bin/testrunner", [cls])

    # resolve the ctest test's real command (works for GoogleTest and others).
    try:
        j = _in_container(["ctest", "--test-dir", build, "--show-only=json-v1"], 60)
        tests = json.loads(j.stdout).get("tests", [])
        want = set(failed)
        entry = next((t for t in tests if t.get("name") in want), tests[0] if tests else None)
        if entry and entry.get("command"):
            cmd = entry["command"]
            exe, extra = cmd[0], cmd[1:]
            if g and not any("gtest_filter" in a for a in extra):
                extra = [f"--gtest_filter={g.group(1)}.{g.group(2)}"]
            return (exe, extra)
    except Exception:  # noqa: BLE001
        pass
    return None


def capture(bug_id: str) -> str:
    info = _defect(bug_id)
    if not info or not info.get("src") or not info.get("lineno"):
        return "(no source location in metadata)"
    cmd = _failing_test_cmd(bug_id)
    if not cmd:
        return "(could not resolve failing-test executable)"
    exe, args = cmd
    base = os.path.basename(info["src"])
    line = info["line"]
    idents = [i for i in dict.fromkeys(_IDENT.findall(line)) if i not in _SKIP][:6]
    # member-access / call chains on the buggy line (e.g. tok->tokAt(6),
    # condTok->values().front().intvalue) — the actionable sub-expressions, evaluated with
    # gdb's expression evaluator (read-only accessors).
    exprs = re.findall(r"[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*|\s*\([^()]*\))+", line)
    exprs = [re.sub(r"\s+", "", e) for e in dict.fromkeys(exprs)][:6]

    gdb = ["gdb", "-batch",
           "-ex", "set pagination off", "-ex", "set width 0", "-ex", "set confirm off",
           "-ex", f"break {base}:{info['lineno']}",
           "-ex", "run",
           "-ex", "bt 2"]
    for e in exprs + [i for i in idents if i not in exprs]:
        # label each value with its expression; `output` prints the value without a $N tag
        gdb += ["-ex", f'printf "\\nVAL {e} = "', "-ex", f"output {e}"]
    gdb += ["-ex", "quit", "--args", exe] + args
    try:
        r = _in_container(gdb, 200)
    except subprocess.TimeoutExpired:
        return "(gdb timed out — buggy line likely not reached, or test hangs)"
    out = r.stdout
    hit = "Breakpoint 1," in out
    vals = [ln[4:].rstrip() for ln in out.splitlines() if ln.startswith("VAL ")]
    frame = next((ln.rstrip() for ln in out.splitlines() if ln.strip().startswith("#0")), "")
    if not hit:
        why = "SIGSEGV before the line" if "SIGSEGV" in out or "SIGABRT" in out \
              else "buggy line not reached by this test"
        return f"(gdb ran but did not stop at {base}:{info['lineno']} — {why})"
    body = "\n".join(v for v in vals if "No symbol" not in v and v.split("=", 1)[-1].strip())
    return (f"Runtime state at the buggy line ({base}:{info['lineno']}):\n"
            f"// line: {info['line']}\n"
            + (f"// frame: {frame.strip()[:120]}\n" if frame else "")
            + (body or "(no in-scope values captured)"))


if __name__ == "__main__":
    import sys
    print(capture(sys.argv[1]))
