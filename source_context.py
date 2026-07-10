"""Symbol digest of the buggy file — the declarations the base prompt omits.

The base prompt shows only the buggy function, so the model references identifiers
it can't see: undeclared-function / undefined-identifier / no-member compile errors
are the single biggest failure mode (see failure-taxonomy). This pulls, from the
buggy file, the declarations the fix is *likely* to need — filtered to the symbols
the buggy function already references — so the digest stays small and on-point:
callee signatures, referenced macros, and referenced struct/typedef layouts.
"""
from __future__ import annotations

import os
import re

import config
from test_source import _meta   # reuse the bugs_list_new.json lookup

_IDENT = re.compile(r"[A-Za-z_]\w+")
_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)([^\n]*)", re.M)
_CALLED = re.compile(r"\b([A-Za-z_]\w+)[ \t]*\(")     # names invoked in the buggy code
_TYPEDEF_LINE = re.compile(r"^[ \t]*typedef[ \t]+[^\n;{]{0,120};", re.M)


def _callee_sigs(src: str, called: set[str], limit: int = 12) -> list[str]:
    """Signature of each `called` name whose definition/prototype lives in this file.
    Name-driven and anchored at column 0: real definitions/prototypes start there,
    while call sites and control statements are indented — so this excludes the
    `if (foo(...))` / `rc = bar(...)` noise and survives both C style (`{` on its own
    line) and C++ methods (`Class::name(...)`)."""
    out = []
    for name in sorted(called):
        # col-0 start, optional return type/qualifiers, then 'name(args)' then '{' or ';'
        m = re.search(
            rf"^([A-Za-z_][\w:<>\*&,\t ]*?\b{re.escape(name)}[ \t]*\([^;{{]{{0,260}}\))[ \t]*\n?[ \t]*[{{;]",
            src, re.M)
        if m:
            out.append(re.sub(r"\s+", " ", m.group(1).strip()))
        if len(out) >= limit:
            break
    return out


def _src_path(bug_id: str) -> str | None:
    try:
        project, sha = bug_id.split("@", 1)
    except ValueError:
        return None
    b = _meta(project, sha)
    srcs = ((b or {}).get("files") or {}).get("src") or []
    if not srcs:
        return None
    return os.path.join(config.OUT_DIR, project, f"git_repo_dir_{sha}", srcs[0])


def _struct_blocks(src: str) -> list[tuple[str, str]]:
    """(name, text) for each `struct/union NAME {...}` or `typedef struct {...} NAME;`
    brace-matched out of the source. Name is whichever side carries it."""
    out = []
    for m in re.finditer(r"\b(?:typedef[ \t]+)?(?:struct|union|enum)\b[ \t]*(\w+)?[ \t]*\{", src):
        lead_name = m.group(1)
        i = src.find("{", m.start())
        depth = 0
        for j in range(i, min(len(src), i + 4000)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    tail = src[j + 1: j + 60]
                    trail_name = re.match(r"[ \t]*(\w+)", tail)
                    name = lead_name or (trail_name.group(1) if trail_name else None)
                    if name:
                        out.append((name, re.sub(r"[ \t]+", " ", src[m.start(): j + 1]).strip()))
                    break
    return out


def symbol_digest(bug_id: str, buggy_text: str = "", *, max_chars: int = 2500) -> str:
    """Declarations from the buggy file that the fix likely needs, filtered to the
    symbols the buggy function references. "" if the source isn't on disk."""
    p = _src_path(bug_id)
    if not p or not os.path.exists(p):
        return ""
    try:
        src = open(p, errors="replace").read()
    except OSError:
        return ""
    refs = set(_IDENT.findall(buggy_text))
    if not refs:
        return ""

    # callee signatures: functions the buggy code actually invokes
    called = set(_CALLED.findall(buggy_text)) & refs
    sigs = _callee_sigs(src, called)
    macros = [f"#define {name}{rest.rstrip()}"
              for name, rest in _DEFINE.findall(src) if name in refs]
    types = [t.strip() for t in _TYPEDEF_LINE.findall(src)
             if any(w in refs for w in _IDENT.findall(t))]
    types += [txt for name, txt in _struct_blocks(src) if name in refs]

    sections = []
    if macros:
        sections.append("// macros\n" + "\n".join(dict.fromkeys(macros)))
    if types:
        sections.append("// types\n" + "\n\n".join(dict.fromkeys(types)))
    if sigs:
        sections.append("// function signatures (callees)\n"
                        + "\n".join(dict.fromkeys(s + ";" for s in sigs)))
    if not sections:
        return ""
    body = "\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars].rsplit("\n", 1)[0] + "\n// ... (truncated)"
    return (f"Declarations from {os.path.basename(p)} available to your fix "
            "(do not redefine these; use them):\n" + body)
