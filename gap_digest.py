"""Deterministic gap-filler for the symbol digest.

When the buggy code references a *project* symbol whose definition the clang/regex
digest dropped — cross-header macros, a missing compile_commands.json, or an
unresolved system include (e.g. tcpdump's netdissect.h behind `#include <pcap.h>`) —
grep the repo for its definition and append it. Sizing over the benchmark: 85/131
defects reference a genuine project-specific macro that is in-repo but absent from
the digest they were shown (ND_TCHECK/EXTRACT_16BITS, libyang LYS_*/CHECK_*, php
ZVAL_*). This closes that gap without libclang succeeding.
"""
import os, re, json, subprocess
import clang_digest as _cd  # reuse _IDENT and _repo_and_src

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gap_digest_cache")
_INC = ["--include=*.c", "--include=*.h", "--include=*.cc", "--include=*.cpp",
        "--include=*.hpp", "--include=*.hh", "--include=*.cxx", "--include=*.hxx"]
_DEF = re.compile(r"#\s*define\s+([A-Za-z_]\w+)")
# standard fixed-width/size types some repos re-#define for portability — the model
# already knows these, so never inject them.
_STD_TYPE = re.compile(r"^(u_?)?(u?int(_least|_fast)?(8|16|32|64|max|ptr)|size|ssize|"
                       r"ptrdiff|intptr|uintptr|wchar|char16|char32)_t$")


def _run(args, timeout):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001 — grep missing / timeout / huge repo
        return ""


def _sys_macros():
    """Macro names defined under /usr/include — the model already knows these, so they
    are never worth injecting. Computed once, cached to disk."""
    p = os.path.join(_CACHE, "_sys_macros.json")
    if os.path.exists(p):
        try:
            return set(json.load(open(p)))
        except Exception:  # noqa: BLE001
            pass
    out = _run(["grep", "-rhoE", r"#[[:space:]]*define[[:space:]]+[A-Za-z_][A-Za-z0-9_]*", "/usr/include"], 240)
    names = {m.group(1) for line in out.splitlines() if (m := _DEF.search(line))}
    os.makedirs(_CACHE, exist_ok=True)
    try:
        json.dump(sorted(names), open(p, "w"))
    except Exception:  # noqa: BLE001
        pass
    return names


def _index(repo):
    """name -> 'file:lineno' for macros and struct/union/enum tags in the repo. Cached."""
    key = re.sub(r"[^\w.-]", "_", repo.strip("/"))[-100:] + ".json"
    p = os.path.join(_CACHE, key)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:  # noqa: BLE001
            pass
    idx = {}
    out = _run(["grep", "-rnoE", r"#[[:space:]]*define[[:space:]]+[A-Za-z_][A-Za-z0-9_]*", repo, *_INC], 180)
    for line in out.splitlines():
        m = re.match(r"(.*?):(\d+):.*?#\s*define\s+([A-Za-z_]\w+)", line)
        if m and m.group(3) not in idx:
            idx[m.group(3)] = f"{m.group(1)}:{m.group(2)}"
    out = _run(["grep", "-rnoE", r"(struct|union|enum)[[:space:]]+[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\{", repo, *_INC], 120)
    for line in out.splitlines():
        m = re.match(r"(.*?):(\d+):.*?(?:struct|union|enum)\s+([A-Za-z_]\w+)", line)
        if m and m.group(3) not in idx:
            idx[m.group(3)] = f"{m.group(1)}:{m.group(2)}"
    os.makedirs(_CACHE, exist_ok=True)
    try:
        json.dump(idx, open(p, "w"))
    except Exception:  # noqa: BLE001
        pass
    return idx


def _read_def(loc, max_lines=8):
    """Read the definition at file:lineno, following macro `\\` line-continuations."""
    try:
        path, ln = loc.rsplit(":", 1)
        lines = open(path, errors="replace").read().splitlines()
    except Exception:  # noqa: BLE001
        return ""
    i = int(ln) - 1
    if not (0 <= i < len(lines)):
        return ""
    out = [lines[i].strip()]
    while lines[i].rstrip().endswith("\\") and len(out) < max_lines and i + 1 < len(lines):
        i += 1
        out.append(lines[i].strip())
    return "\n".join(out)


def augment(bug_id, buggy_text, existing, *, max_syms=12, max_chars=1600):
    """Append definitions for project symbols referenced in `buggy_text` that are not
    already in `existing`. Returns `existing` unchanged if nothing is missing."""
    if not buggy_text:
        return existing
    try:
        repo, _ = _cd._repo_and_src(bug_id)
    except Exception:  # noqa: BLE001
        return existing
    if not repo or not os.path.isdir(repo):
        return existing
    refs = set(_cd._IDENT.findall(buggy_text))
    present = set(_cd._IDENT.findall(existing or ""))
    idx = _index(repo)
    sysm = _sys_macros()
    # distinctive = has an underscore or an uppercase letter — drops common-word noise
    # macros (#define data ..., #define flags 0) while keeping ND_TCHECK / zval_ptr_dtor.
    cand = [n for n in refs if n in idx and n not in present and len(n) > 3
            and n not in sysm and not _STD_TYPE.match(n)
            and ("_" in n or any(c.isupper() for c in n))]
    if not cand:
        return existing
    items = [(n, _read_def(idx[n])) for n in sorted(cand)[:60]]
    items = [(n, d) for n, d in items if d]

    def _func_like(n, d):  # #define NAME(...) — behavioral, rank above bare constants
        return bool(re.match(r"#\s*define\s+" + re.escape(n) + r"\(", d.lstrip()))
    items.sort(key=lambda nd: (not _func_like(*nd), nd[0]))
    # cap per name-family so an enum flood (DH6OPT_*) can't crowd out structural macros
    defs, fam = [], {}
    for n, d in items:
        pref = n.split("_", 1)[0] if "_" in n else n[:5]
        if fam.get(pref, 0) >= 3:
            continue
        fam[pref] = fam.get(pref, 0) + 1
        defs.append(d)
        if len(defs) >= max_syms:
            break
    if not defs:
        return existing
    block = "\n".join(dict.fromkeys(defs))[:max_chars]
    header = ("// additional project symbols referenced above (definitions found in the "
              "repo; use them, do not redefine):\n")
    if existing:
        return existing + "\n\n" + header + block
    return ("Declarations from the repository available to your fix "
            "(do not redefine these; use them):\n" + header + block)
