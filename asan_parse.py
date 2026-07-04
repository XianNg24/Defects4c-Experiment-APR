"""Parse sanitizer reports out of a captured test log into a compact dict.

Handles the two sanitizer families that dominate the Defects4C taxonomy:
  - AddressSanitizer  → the 20 "Memory Error" bugs (overflow / null-deref / …)
  - UndefinedBehaviorSanitizer → the 62 "Sanitizer: Control Expression Error" bugs

Both land in the same test log (`common_test_tpl.jinja` redirects stderr), so a
single parser serves both. Output is intentionally small — it becomes a
"Sanitizer diagnosis:" block in the repair prompt (Phase 2 step 11), so we keep
the error class, the faulting `file:line`, the access, and the top app frames.

Usage:
    python asan_parse.py <logfile>        # pretty-print parsed dict
    python asan_parse.py --selftest       # run built-in tests
"""
from __future__ import annotations

import json
import re
from typing import Optional

# ── ASAN ──────────────────────────────────────────────────────────────────────
# ==1234==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x.. at pc ..
_ASAN_ERR = re.compile(
    r"==\d+==\s*ERROR:\s*AddressSanitizer:\s*(?P<kind>[a-zA-Z0-9_-]+)")
# READ of size 4 at 0x.. thread T0
_ASAN_ACCESS = re.compile(r"(?P<op>READ|WRITE)\s+of\s+size\s+(?P<size>\d+)")
# SUMMARY: AddressSanitizer: heap-buffer-overflow file.c:12:5 in func
_ASAN_SUMMARY = re.compile(
    r"SUMMARY:\s*AddressSanitizer:\s*(?P<kind>[a-zA-Z0-9_-]+)\s+(?P<loc>\S+)?")

# ── UBSan ─────────────────────────────────────────────────────────────────────
# file.c:12:5: runtime error: signed integer overflow: 2147483647 + 1 ...
_UBSAN = re.compile(
    r"(?P<file>[^\s:][^:]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s*runtime error:\s*(?P<msg>.+)")

# ── stack frames: "    #0 0x.. in func /path/file.c:12:5" ─────────────────────
_FRAME = re.compile(
    r"#(?P<idx>\d+)\s+0x[0-9a-fA-F]+\s+in\s+(?P<func>.+?)\s+"
    r"(?P<file>[^\s:]+):(?P<line>\d+)(?::(?P<col>\d+))?")

# frames in these paths are noise (the sanitizer runtime / libc), not the bug
_NOISE = ("/libsanitizer/", "/sanitizer_common/", "compiler-rt", "<null>",
          "libc_start", "/usr/lib/", "/usr/include/c++/")


def _first_app_frame(frames: list[dict]) -> Optional[dict]:
    for f in frames:
        if not any(n in (f.get("file") or "") for n in _NOISE):
            return f
    return frames[0] if frames else None


def parse_asan_block(text: str) -> Optional[dict]:
    m = _ASAN_ERR.search(text)
    if not m:
        return None
    kind = m.group("kind")
    frames = [
        {"idx": int(g["idx"]), "func": g["func"].strip(),
         "file": g["file"], "line": int(g["line"])}
        for g in (mm.groupdict() for mm in _FRAME.finditer(text))
    ]
    app = _first_app_frame(frames)
    out = {
        "sanitizer": "AddressSanitizer",
        "error_type": kind,
        "fault_file": app["file"] if app else None,
        "fault_line": app["line"] if app else None,
        "fault_func": app["func"] if app else None,
        "frames": frames[:6],
    }
    acc = _ASAN_ACCESS.search(text)
    if acc:
        out["access"] = {"op": acc.group("op"), "size": int(acc.group("size"))}
    return out


def parse_ubsan(text: str) -> Optional[dict]:
    m = _UBSAN.search(text)
    if not m:
        return None
    return {
        "sanitizer": "UndefinedBehaviorSanitizer",
        "error_type": m.group("msg").split(":")[0].strip()[:80],
        "fault_file": m.group("file"),
        "fault_line": int(m.group("line")),
        "fault_func": None,
        "message": m.group("msg").strip()[:300],
        "frames": [],
    }


def parse_log(text: str) -> Optional[dict]:
    """Return the first sanitizer report found, ASAN taking priority over UBSan."""
    return parse_asan_block(text) or parse_ubsan(text)


def to_prompt_block(diag: dict) -> str:
    """Render a parsed diagnosis as a compact 'Sanitizer diagnosis:' prompt section."""
    if not diag:
        return ""
    lines = [f"Sanitizer diagnosis ({diag['sanitizer']}):",
             f"- error: {diag['error_type']}"]
    if diag.get("fault_file"):
        loc = f"{diag['fault_file']}:{diag['fault_line']}"
        if diag.get("fault_func"):
            loc += f" (in {diag['fault_func']})"
        lines.append(f"- faulting location: {loc}")
    if diag.get("access"):
        lines.append(f"- invalid {diag['access']['op']} of size {diag['access']['size']}")
    if diag.get("message"):
        lines.append(f"- detail: {diag['message']}")
    if diag.get("frames"):
        lines.append("- top frames:")
        for f in diag["frames"][:4]:
            lines.append(f"    {f['func']} @ {f['file']}:{f['line']}")
    return "\n".join(lines)


# ── self-test on real-format samples ──────────────────────────────────────────
_SAMPLE_ASAN = """
=================================================================
==2841==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000d1c at pc 0x0001 bp 0x7ffc sp 0x7ffc
READ of size 4 at 0x602000000d1c thread T0
    #0 0x4f1a2b in parse_header /out/tcpdump/print-foo.c:214:9
    #1 0x4e0011 in pretty_print /out/tcpdump/print.c:88
    #2 0x7f00 in __libc_start_main /build/glibc/libc-start.c:308
SUMMARY: AddressSanitizer: heap-buffer-overflow /out/tcpdump/print-foo.c:214:9 in parse_header
"""

_SAMPLE_UBSAN = """
../src/parser.c:512:23: runtime error: signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'
SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior ../src/parser.c:512:23
"""


def _selftest() -> int:
    a = parse_log(_SAMPLE_ASAN)
    assert a and a["error_type"] == "heap-buffer-overflow", a
    assert a["fault_file"].endswith("print-foo.c") and a["fault_line"] == 214, a
    assert a["access"] == {"op": "READ", "size": 4}, a
    assert a["fault_func"] == "parse_header", a
    assert a["frames"][2]["func"] == "__libc_start_main", a  # noise still listed
    u = parse_log(_SAMPLE_UBSAN)
    assert u and u["sanitizer"] == "UndefinedBehaviorSanitizer", u
    assert u["error_type"].startswith("signed integer overflow"), u
    assert u["fault_line"] == 512, u
    assert parse_log("nothing here") is None
    print("[selftest] ASAN:\n" + to_prompt_block(a))
    print("\n[selftest] UBSan:\n" + to_prompt_block(u))
    print("\n[selftest] PASS")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if len(sys.argv) > 1:
        diag = parse_log(open(sys.argv[1]).read())
        print(json.dumps(diag, indent=2) if diag else "(no sanitizer report found)")
    else:
        print(__doc__)
