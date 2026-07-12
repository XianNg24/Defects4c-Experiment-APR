"""Extract the source of the *failing test(s)* — the conditions a fix must satisfy.

The failing test is the specification for the repair (standard in test-driven APR:
the test is the oracle, not the answer). The base prompt only names which test
binary failed; this pulls the actual test function's assertions from the repo:
`files.test` (bug metadata) gives the test file, the reproduction log names the
failing subtest (`[ FAILED ] <name>` for cmocka, `[ FAILED ] Suite.Test` for
gtest), and we brace-match that function out of the source.
"""
from __future__ import annotations

import json
import os
import re

import config

_TPL = os.path.join(os.path.dirname(config.OUT_DIR), "defectsc_tpl")
_FAILED = re.compile(r"\[\s*FAILED\s*\]\s+([A-Za-z_][\w.]*)")
# tcpdump's data-driven runner: 'Failed test: <name>' / '<name> : TEST FAILED'.
_TCPDUMP_FAIL = re.compile(r"Failed test:\s*(\S+)|^\s*(\S+)\s*:\s*TEST FAILED", re.M)


def _meta(project: str, sha: str) -> dict | None:
    for major in ("projects_v1", "projects"):
        f = os.path.join(_TPL, major, project, "bugs_list_new.json")
        if os.path.exists(f):
            d = json.load(open(f))
            bugs = d if isinstance(d, list) else list(d.values())
            b = next((x for x in bugs if str(x.get("commit_after", "")).startswith(sha[:12])), None)
            if b:
                return b
    return None


def _extract_func(src: str, name: str) -> str | None:
    """Brace-match the function/test named `name` out of C/C++ source."""
    for pat in (rf"\n[A-Za-z_][\w\s\*]*\b{re.escape(name)}\s*\(",       # cmocka: name(void**state)
                rf"\nTEST\w*\([^)]*,\s*{re.escape(name)}\s*\)"):        # gtest: TEST(Suite, name)
        m = re.search(pat, src)
        if not m:
            continue
        i = src.find("{", m.start())
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    return src[m.start():j + 1].strip()
    return None


def _testlist_source(repo: str, testfiles: list, log_text: str,
                     max_tests: int = 2, max_chars: int = 2000) -> str:
    """tcpdump-style data-driven tests: there is no test *function* — a TESTLIST row
    maps `<name> <input.pcap> <expected.out> <flags>`, and the expected `.out` file is
    the oracle. Show the failing row's command and its expected output so the model
    sees the full correct decode, not just the one diff line."""
    testlist = next((t for t in testfiles if os.path.basename(t) == "TESTLIST"), None)
    if not testlist:
        return ""
    tlp = os.path.join(repo, testlist)
    if not os.path.exists(tlp):
        return ""
    names = []
    for m in _TCPDUMP_FAIL.finditer(log_text or ""):
        n = (m.group(1) or m.group(2) or "").strip()
        if n and n not in names:
            names.append(n)
    if not names:
        return ""
    rows = open(tlp, errors="replace").read().splitlines()
    out = []
    for name in names[:max_tests]:
        entry = next((r for r in rows if r.split() and r.split()[0] == name), None)
        if not entry:
            continue
        cols = entry.split()
        block = [f"// TESTLIST row: {entry.strip()}"]
        if len(cols) >= 3:
            block.append(f"// runs: tcpdump {' '.join(cols[3:])} -r {cols[1]}"
                         f"  (stdout must equal {cols[2]})")
            outp = os.path.join(os.path.dirname(tlp), cols[2])
            if os.path.exists(outp):
                exp = open(outp, errors="replace").read().rstrip("\n")
                block.append(f"// expected output ({cols[2]}):\n{exp}")
        out.append("\n".join(block))
    return "\n\n".join(out)[:max_chars]


def source_window(container_path: str, line: int, ctx: int = 5,
                  max_chars: int = 700) -> str:
    """The source around `line` of `container_path` (a /out/... path from a log),
    with the target line marked — used to show a plain ASSERT's actual condition."""
    p = container_path
    if p.startswith("/out/"):
        p = os.path.join(config.OUT_DIR, p[len("/out/"):])
    if not os.path.exists(p):
        return ""
    try:
        lines = open(p, errors="replace").read().splitlines()
    except OSError:
        return ""
    lo, hi = max(0, line - 1 - ctx), min(len(lines), line + ctx)
    win = [f"{'>> ' if i == line - 1 else '   '}{i + 1}: {lines[i]}"
           for i in range(lo, hi)]
    return "\n".join(win)[:max_chars]


def failing_tests(bug_id: str, log_text: str, *, max_tests: int = 2,
                  max_chars: int = 2500) -> str:
    """Source of the failing test function(s) named in the log, or ""."""
    try:
        project, sha = bug_id.split("@", 1)
    except ValueError:
        return ""
    b = _meta(project, sha)
    if not b:
        return ""
    testfiles = (b.get("files") or {}).get("test") or []
    repo = os.path.join(config.OUT_DIR, project, f"git_repo_dir_{sha}")
    # data-driven tests (tcpdump TESTLIST + expected .out) have no test function.
    if any(os.path.basename(t) == "TESTLIST" for t in testfiles):
        return _testlist_source(repo, testfiles, log_text or "")
    names = []
    for m in _FAILED.finditer(log_text or ""):
        n = m.group(1).split(".")[-1]           # gtest Suite.Test -> Test
        if n and not n[0].isdigit() and n not in names:
            names.append(n)
    if not names or not testfiles:
        return ""
    out = []
    for tf in testfiles:
        p = os.path.join(repo, tf)
        if not os.path.exists(p):
            continue
        src = open(p, errors="replace").read()
        for name in names[:max_tests]:
            fn = _extract_func(src, name)
            if fn:
                out.append(f"// {tf} :: {name}\n{fn}")
    return "\n\n".join(out)[:max_chars]


if __name__ == "__main__":
    import sys
    from harness_client import HarnessClient
    bug = sys.argv[1]
    c = HarnessClient()
    log = c.read_test_logs(bug)
    print(failing_tests(bug, log) or "(no failing-test source found)")
