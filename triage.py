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
# A genuine build/link failure — real even when ctest 'ran' the test (the binary
# failed to compile or load), so it overrides the tests-ran guard below.
_LINK_ERR = re.compile(
    r"undefined reference|undefined symbol|symbol lookup error|"
    r"ninja: (?:build stopped|fatal|error)|cannot find -l", re.I)
# Real timeout evidence only. NOT the bare word "timeout": ctest prints
# "Test timeout computed to be: N" for every test, so matching "timeout" mislabels
# ordinary test failures. A genuine timeout shows ctest's "***Timeout" or a kill.
_TIMEOUT = re.compile(
    r"\*\*\*Timeout|\btimed out\b|\bKilled\b|\bTerminated\b|exceeded.{0,20}time|signal 9",
    re.I)
# Real test-failure markers only. NOT the bare word "Failed"/"Failure": cmake
# configure prints "Performing Test X - Failed" for absent features, and build logs
# carry it too, which fabricated assertions out of pure build output.
_ASSERT_FAIL = re.compile(
    r"\[\s*FAILED\s*\]|"                        # cmocka/gtest per-test marker
    r"\d+% tests? (?:failed|passed)|"           # ctest summary line
    r"\btests? failed\b|"                       # "N tests failed"
    r"\*\*\*Failed|"                            # ctest per-test result
    r"\bTESTFAIL\b|\b(?:stdout|stderr|exit|memory|protocol) FAILED|"  # curl runtests.pl
    r"#\s*FAIL:\s*[1-9]|"                       # automake summary '# FAIL: N' (N>0)
    r":\d+:\s*(?:error:\s*)?Failure\b|"         # gtest/cmocka  file:line: Failure
    r"\bAssertion\b.*\bfail|"                   # assert() / assertion failed
    r"\bEXPECT_\w+|\bASSERT_\w+", re.I)         # gtest macros surfaced in output
# Evidence that tests actually ran (so a build 'error:' can't be a compile failure).
_TESTS_RAN = re.compile(
    r"#\s*(?:TOTAL|PASS|FAIL):|\d+% tests|\btests? (?:passed|failed)|"
    r"\[\s*(?:OK|FAILED|RUN)\s*\]|Testing Complete|TESTDONE|Test #\d+", re.I)
# automake per-test failures ('FAIL: <name>') — the actionable list.
_AUTOMAKE_FAIL = re.compile(r"^\s*FAIL:\s+(\S+)", re.M)

# gtest expected/actual block
# Failing-test location across frameworks, keeping the FULL path so the tool can
# read the test source there: gtest 'file:line: Failure', cmocka 'file:line: error:
# Failure', Catch2 'file:line: FAILED', cppcheck 'file:line(Class::test): Assertion'.
_ASSERT_LOC = re.compile(
    r"(?P<file>[\w./+-]+\.(?:cc|cpp|c|h|hpp)):(?P<line>\d+)"
    r"(?::\s*(?:error:\s*)?(?:Failure|FAILED)|\([\w:]+\):\s*Assertion failed)")
# gtest failure whose detail isn't a 'Value of/Expected/Actual' label (e.g. ASSERT_OK,
# custom matchers): the failed expression + message follow 'file:line: Failure'.
_GTEST_FAIL_BLOCK = re.compile(
    r":\d+:\s*Failure\s*\n(.*?)(?=\n[ \t]*\n|\n\[\s*(?:FAILED|OK|RUN)|\n={5,}|\Z)", re.S)
# cmocka prints the real detail (asserted expression, or 'a != b') on its own
# '[ ERROR ] --- ...' line, *before* the generic 'error: Failure!' trailer.
_CMOCKA_ERR = re.compile(r"\[\s*ERROR\s*\]\s*---\s*(.+)")
# Labeled expected/actual lines. gtest puts the value on the SAME line
# ('Value of: X'); cppcheck's test framework puts it on the FOLLOWING line(s), which
# may be empty ("expect no warnings"). Capture the value up to the next label / blank
# line / '____' separator, so it works for both and an empty value stays empty.
_EA_LABEL = r"Value of|Expected|Actual|Which is|To be equal to"
# Stop the value at: the next label, a blank line, a '____' separator, a gtest status
# marker ('[  FAILED  ]', '[ RUN ]', '[----------]' — but NOT cppcheck's '[file:line]'
# value), or end of text.
_GTEST_EXPECT = re.compile(
    rf"\b({_EA_LABEL}):[ \t]*(.*?)"
    rf"(?=\n[ \t]*(?:{_EA_LABEL}):|\n[ \t]*\n|\n[ \t]*_+"
    rf"|\n[ \t]*\[\s*(?:FAILED|OK|RUN|PASSED|-+|=+)|\Z)", re.S)
# ctest -VV prefixes every line with 'N: ' (the test number); strip it before parsing.
_CTEST_PREFIX = re.compile(r"(?m)^[ \t]*\d+:[ \t]?")
# gtest colorizes markers ('\x1b[0;31m[ FAILED ]'); strip so detail is clean and the
# '[ FAILED ]' block boundary is matchable.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# cppcheck's plain ASSERT(...) prints only 'file:line(Class::test): Assertion failed'
# (no expected/actual) — capture the location + test so we can show the condition.
_CPPCHECK_ASSERT = re.compile(
    r"([\w./+-]+\.(?:cpp|cc|c)):(\d+)\(([\w:]+)\):\s*Assertion failed", re.I)
# curl's runtests.pl: '<N>: stdout FAILED: <reason>' + the test's description line
# 'test <N>...[<description>]'.
_CURL_FAIL = re.compile(
    r"^\s*(\d+):\s*((?:stdout|stderr|exit code|protocol|memory) FAILED[^\n]*)"
    r"(?:\n\s*([^\n]+))?", re.M)
_CURL_TEST = re.compile(r"test\s+(\d+)\.\.\.\[(.+?)\]")
# Catch2 (CLI11, many C++ libs): 'file:line: FAILED:' then the CHECK/REQUIRE
# expression and its 'with expansion:' / exception message, up to a blank line.
_CATCH2 = re.compile(
    r"([\w./+-]+):(\d+):\s*FAILED:\s*(.*?)(?=\n[ \t]*\n|\n={5,}|\Z)", re.S)


def _tail(text: str, n: int = 40) -> str:
    # Strip ANSI colors + the ctest 'N: ' line prefix so the excerpt (the fallback
    # shown when no structured detail is extracted) is clean for every framework.
    lines = [l for l in _ANSI.sub("", _CTEST_PREFIX.sub("", text)).splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def _extract_assertion(text: str) -> Optional[dict]:
    clean = _ANSI.sub("", _CTEST_PREFIX.sub("", text))   # drop colors + ctest 'N: '
    out: dict = {}
    # Location + FULL path (any framework), so the tool can read the test source there.
    loc = _ASSERT_LOC.search(clean)
    if loc:
        out["file"] = os.path.basename(loc.group("file"))
        out["line"] = int(loc.group("line"))
        out["path"] = loc.group("file")
    cmocka = [c.strip() for c in _CMOCKA_ERR.findall(clean)]
    if cmocka:
        # the asserted expression / operand mismatch — the actionable part
        uniq = list(dict.fromkeys(cmocka))
        out["detail"] = "; ".join(uniq[-3:])[:400]
    else:
        gt = _GTEST_EXPECT.findall(clean)
        cc = _CPPCHECK_ASSERT.search(clean)
        catch2 = _CATCH2.findall(clean)
        gfb = _GTEST_FAIL_BLOCK.search(clean)
        if gt:
            parts = []
            for k, v in gt:
                v = " ".join(v.split())          # collapse newlines/whitespace
                parts.append(f"{k}: {v if v else '(no output)'}")
            out["detail"] = " | ".join(dict.fromkeys(parts))[:400]
        elif cc:
            out["detail"] = f"ASSERT failed in {cc.group(3)}"   # location set above
        elif catch2:
            out["detail"] = " | ".join(
                f"{os.path.basename(f_)}:{ln}: {' '.join(body.split())}"
                for f_, ln, body in catch2[:2])[:400]
        elif _CURL_FAIL.search(text):
            # curl (use raw text: its '1441:' test number looks like a ctest prefix
            # and would be stripped from `clean`). Show the failed test + reason.
            desc = dict(_CURL_TEST.findall(text))
            parts = []
            for num, kind, reason in _CURL_FAIL.findall(text)[:3]:
                d = desc.get(num, "")
                head = f"test {num}" + (f" ({d})" if d else "")
                parts.append(f"{head}: {kind.strip()} {reason.strip()}".strip())
            out["detail"] = " | ".join(parts)[:400]
        elif gfb and gfb.group(1).strip():
            # gtest with a non-labeled failure (ASSERT_OK, custom matcher): the failed
            # expression + status message that follow 'file:line: Failure'.
            out["detail"] = " ".join(gfb.group(1).split())[:400]
        elif _AUTOMAKE_FAIL.findall(clean):
            # automake TAP: the names of the failing tests.
            names = list(dict.fromkeys(_AUTOMAKE_FAIL.findall(clean)))
            out["detail"] = "failed tests: " + ", ".join(names[:6])
        else:
            m = re.search(r"Failure\b.*?(?=\n\S|\Z)", clean, re.S)
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

    # Compile failure: fire on a genuine build/link error (real even if ctest ran the
    # test — the binary didn't build/load), OR on a generic '_COMPILE' hit only when no
    # tests ran. If a test summary is present, a stray 'error:' (e.g. automake's
    # '# ERROR: 0') is not a compile failure.
    if not passed and (_LINK_ERR.search(log_text) or compile_errors(log_text) or
                       (_COMPILE.search(log_text) and not _TESTS_RAN.search(log_text))):
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
