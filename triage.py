"""Triage an *observed* failure into a class + structured evidence.

Deliberately label-free: we never look at the dataset's bug/vulnerability tag or
its taxonomy. We read the reproduction log and classify what actually happened,
which is what decides (in tools.py) which diagnostic tools are worth running.
The same code therefore handles an ordinary bug that segfaults and a
memory-safety CVE identically — both are `crash`.

Classes (most to least specific):
    sanitizer_report  a sanitizer already printed a report (ASan/UBSan)
    crash             process died on a signal (SIGSEGV/SIGABRT/…) w/o a report
    compile_error     the build failed before tests ran
    assertion_mismatch a test ran and failed on an expected/actual check
    timeout           the run was killed for exceeding its time budget
    passed            no failure detected in the log
    unknown           a failure we could not classify
"""
from __future__ import annotations

import os
import re
from typing import Optional

import asan_parse

_CRASH = re.compile(
    r"Segmentation fault|SIGSEGV|SIGABRT|\bAborted\b|core dumped|"
    r"\*\*\*Exception|\(SEGFAULT\)|"   # ctest crash markers (not stray 'segfault' in a test name)
    r"Test interrupted by SIG|stack smashing|double free|munmap_chunk", re.I)
_COMPILE = re.compile(
    r"\berror:|ninja: build stopped|Command \d+ failed|"
    r"undefined reference|No such file or directory", re.I)
# Real timeout evidence only. NOT the bare word "timeout": ctest prints
# "Test timeout computed to be: N" for every test, so matching "timeout" mislabels
# ordinary test failures. A genuine timeout shows ctest's "***Timeout" or a kill.
_TIMEOUT = re.compile(
    r"\*\*\*Timeout|\btimed out\b|\bKilled\b|\bTerminated\b|exceeded.{0,20}time|signal 9",
    re.I)
_ASSERT_FAIL = re.compile(
    r"\bFAILED\b|\d+% tests? (?:failed|passed)|\bFailure\b|Assertion|EXPECT_|ASSERT_", re.I)

# gtest expected/actual block
# gtest: 'file:line: Failure'  |  cmocka: 'file:line: error: Failure!'
_ASSERT_LOC = re.compile(
    r"(?P<file>[^\s:]+\.(?:cc|cpp|c|h|hpp)):(?P<line>\d+):\s*(?:error:\s*)?Failure")
# cmocka prints the real detail (asserted expression, or 'a != b') on its own
# '[ ERROR ] --- ...' line, *before* the generic 'error: Failure!' trailer.
_CMOCKA_ERR = re.compile(r"\[\s*ERROR\s*\]\s*---\s*(.+)")
# gtest prints the useful part as labeled lines ('Value of:', 'Actual:', 'Expected:');
# the ctest 'N: ' line prefix defeats a plain multi-line capture, so grab the labels.
_GTEST_EXPECT = re.compile(
    r"\b(Value of|Actual|Expected|Which is|To be equal to):\s*([^\n]+)")


def _tail(text: str, n: int = 40) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def _extract_assertion(text: str) -> Optional[dict]:
    loc = _ASSERT_LOC.search(text)
    out: dict = {}
    if loc:
        out["file"] = os.path.basename(loc.group("file"))
        out["line"] = int(loc.group("line"))
    cmocka = [c.strip() for c in _CMOCKA_ERR.findall(text)]
    if cmocka:
        # the asserted expression / operand mismatch — the actionable part
        uniq = list(dict.fromkeys(cmocka))
        out["detail"] = "; ".join(uniq[-3:])[:400]
    else:
        gt = _GTEST_EXPECT.findall(text)
        if gt:
            out["detail"] = " | ".join(f"{k}: {v.strip()}"
                                       for k, v in dict.fromkeys(gt))[:400]
        else:
            m = re.search(r"Failure\b.*?(?=\n\S|\Z)", text, re.S)
            if m:
                out["detail"] = m.group(0).strip()[:400]
    return out or None


def _extract_compile_error(text: str) -> Optional[dict]:
    m = re.search(r"(?P<file>[^\s:]+):(?P<line>\d+):(?:\d+:)?\s*(?:fatal )?error:\s*(?P<msg>.+)", text)
    if m:
        return {"file": m.group("file"), "line": int(m.group("line")),
                "message": m.group("msg").strip()[:300]}
    m2 = re.search(r"(?:fatal )?error:\s*(?P<msg>.+)", text)
    return {"file": None, "line": None, "message": m2.group("msg").strip()[:300]} if m2 else None


def triage(log_text: str) -> dict:
    """Classify a reproduction log into {failure_class, ...evidence}."""
    if not log_text or not log_text.strip():
        return {"failure_class": "unknown", "summary": "no reproduction log available",
                "log_excerpt": ""}

    diag = asan_parse.parse_log(log_text)
    if diag:
        return {"failure_class": "sanitizer_report", "sanitizer": diag,
                "summary": f"{diag['sanitizer']}: {diag['error_type']}"
                           + (f" at {diag['fault_file']}:{diag['fault_line']}"
                              if diag.get("fault_file") else ""),
                "log_excerpt": _tail(log_text)}

    passed = re.search(r"100% tests passed|\bPASSED\b|All tests passed", log_text)
    crash = _CRASH.search(log_text)
    if crash:
        return {"failure_class": "crash", "signal": crash.group(0),
                "summary": f"process crashed ({crash.group(0)}) with no sanitizer report",
                "log_excerpt": _tail(log_text)}

    # compile errors only count if there's no successful test section afterwards
    if _COMPILE.search(log_text) and not passed:
        ce = _extract_compile_error(log_text)
        return {"failure_class": "compile_error", "compile_error": ce,
                "summary": "build failed: " + (ce["message"] if ce else "compile error"),
                "log_excerpt": _tail(log_text)}

    # Assertion failures are checked before timeout: a run that produced a real
    # test failure is an assertion_mismatch even if a "***Timeout" appears for some
    # *other* test — the failing test is the actionable signal.
    if _ASSERT_FAIL.search(log_text) and not re.search(r"0 tests? failed|100% tests passed", log_text):
        a = _extract_assertion(log_text)
        loc = f" at {a['file']}:{a['line']}" if a and a.get("file") else ""
        return {"failure_class": "assertion_mismatch", "assertion": a,
                "summary": f"a test failed on an assertion{loc}",
                "log_excerpt": _tail(log_text)}

    if _TIMEOUT.search(log_text) and not passed:
        return {"failure_class": "timeout", "summary": "run exceeded its time budget",
                "log_excerpt": _tail(log_text)}

    if passed:
        return {"failure_class": "passed", "summary": "no failure observed",
                "log_excerpt": _tail(log_text, 10)}

    return {"failure_class": "unknown", "summary": "failure could not be classified",
            "log_excerpt": _tail(log_text)}


# The harness flattens build logs to a single line (no newlines), so we can't anchor
# on line starts. Match 'file:line[:col]: error: msg', bounding the message at the
# next diagnostic location, a caret marker, or end of string.
_CC_DIAG = re.compile(
    r"([\w./+\-]+):(\d+):(?:(\d+):)?\s*(?:fatal error|error):\s*(.{1,160}?)"
    r"(?=\s+[\w./+\-]+:\d+:\d+:|\s+\^|$)")


def compile_errors(log_text: str, *, limit: int = 8) -> list[str]:
    """Extract clang/gcc diagnostics ('file:line:col: error: msg') from a build log,
    so repair feedback can headline exactly what to fix instead of a raw ninja dump.
    Deduped, order-preserving, capped."""
    out, seen = [], set()
    for m in _CC_DIAG.finditer(log_text or ""):
        f, line, col, msg = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        if not msg or "errors generated" in msg or f in ("ld", "clang", "gcc", "cc1", "cc1plus"):
            continue                      # linker/driver summary noise, not a fix site
        loc = f"{os.path.basename(f)}:{line}" + (f":{col}" if col else "")
        line_s = f"{loc}: {msg}"
        if line_s not in seen:
            seen.add(line_s)
            out.append(line_s)
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) > 1:
        print(json.dumps(triage(open(sys.argv[1], errors="replace").read()), indent=2))
    else:
        print(__doc__)
