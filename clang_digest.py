"""libclang-backed symbol digest — the semantic counterpart to source_context.py.

Instead of regex-matching one .c file, this parses the buggy file into a real Clang AST
(resolving through the project's headers) and, for the symbols the buggy function
references, emits the compiler's own declarations:
  - callee prototypes (from wherever they are declared, incl. headers)
  - referenced struct/union/enum/typedef definitions
  - referenced macro definitions

Same output contract as source_context.symbol_digest, so it is a drop-in alternative.
Results are cached on disk (keyed by bug id + source mtime) so libclang runs once per bug.

Falls back to "" on any failure (missing lib, parse error, source not on disk) — it must
never break a run. Cannot run on bgcommit (no repo/build), only on the executable set.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import config
from test_source import _meta

_VERSION = 3          # bump to invalidate all caches when the algorithm changes
_IDENT = re.compile(r"[A-Za-z_]\w+")
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".clang_digest_cache")

_cindex = None        # lazily imported so a missing binding degrades to ""


def _clang():
    global _cindex
    if _cindex is None:
        import clang.cindex as cindex          # raises if libclang absent
        _cindex = cindex
    return _cindex


def _resource_flags() -> list[str]:
    """Clang's builtin headers (stddef.h/stdarg.h/…). libclang doesn't add them itself,
    so without this every parse fails on 'stddef.h file not found'. Prefer the
    version-matched clang resource dir; fall back to GCC's freestanding headers."""
    import glob
    for d in sorted(glob.glob("/usr/lib/llvm-*/lib/clang/*/include"), reverse=True):
        if os.path.exists(os.path.join(d, "stddef.h")):
            return ["-isystem", d]
    for d in sorted(glob.glob("/usr/lib/gcc/*/*/include"), reverse=True):
        if os.path.exists(os.path.join(d, "stddef.h")):
            return ["-isystem", d]
    return []


# ── locating the source + its compile flags ───────────────────────────────────
def _repo_and_src(bug_id: str):
    try:
        project, sha = bug_id.split("@", 1)
    except ValueError:
        return None, None
    b = _meta(project, sha)
    srcs = ((b or {}).get("files") or {}).get("src") or []
    if not srcs:
        return None, None
    repo = os.path.join(config.OUT_DIR, project, f"git_repo_dir_{sha}")
    return repo, os.path.join(repo, srcs[0])


def _flags_from_ccjson(repo: str, src: str) -> list[str] | None:
    """The real -I/-D/-std flags for `src` from a cmake compile_commands.json, if any."""
    import glob
    for cc in glob.glob(os.path.join(repo, "build*", "compile_commands.json")):
        try:
            entries = json.load(open(cc))
        except (OSError, json.JSONDecodeError):
            continue
        base = os.path.basename(src)
        e = next((x for x in entries if x.get("file", "").endswith(base)), None)
        if not e:
            continue
        import shlex
        cmd = e.get("command") or " ".join(e.get("arguments", []))
        cmd = cmd.replace("/out/", config.OUT_DIR.rstrip("/") + "/")
        keep, toks, skip = [], shlex.split(cmd), False
        for t in toks[1:]:                       # drop the compiler (toks[0])
            if skip:
                skip = False
                continue
            if t in ("-c", "-o"):
                skip = t == "-o"
                continue
            if t.endswith((".c", ".cc", ".cpp", ".cxx")):
                continue
            if t.startswith(("-I", "-D", "-std", "-isystem", "-include")) or t == "-nostdinc":
                keep.append(t)
        return keep + ["-ferror-limit=0"]
    return None


def _fallback_flags(repo: str, src: str) -> list[str]:
    """No compile_commands.json: point -I at the project's own include dirs so its headers
    resolve. Adds both header dirs AND their parents (so a prefixed include like
    "arrow/table_builder.h" resolves via the parent of the dir that holds the header).
    libclang is error-tolerant — unresolved external/system headers still yield a usable
    partial AST, and the project symbols the digest wants live in these dirs."""
    inc = {os.path.dirname(src), repo}
    for sub in ("include", "src", "lib", "source", "headers", "cpp", os.path.join("cpp", "src")):
        d = os.path.join(repo, sub)
        if os.path.isdir(d):
            inc.add(d)
    for root, dirs, _ in os.walk(repo):
        if root[len(repo):].count(os.sep) > 3:
            dirs[:] = []
            continue
        if os.path.basename(root) in ("include", "src"):
            inc.add(root)
            inc.add(os.path.dirname(root))          # parent, for prefixed includes
    std = "-std=c++17" if src.endswith((".cc", ".cpp", ".cxx", ".hpp", ".hh")) else "-std=gnu11"
    return [std] + [f"-I{d}" for d in sorted(inc)] + ["-ferror-limit=0"]


# ── declaration text extraction ───────────────────────────────────────────────
def _read_extent(cur) -> str:
    ext = cur.extent
    f = ext.start.file
    if not f:
        return ""
    try:
        raw = open(f.name, "rb").read()[ext.start.offset:ext.end.offset]
        return re.sub(r"[ \t]+", " ", raw.decode("utf-8", "replace")).strip()
    except OSError:
        return ""


def _prototype(fn) -> str:
    try:
        args = [a.spelling for a in fn.type.argument_types()]
    except Exception:  # noqa: BLE001
        args = [a.type.spelling for a in fn.get_arguments()]
    if fn.type.kind and fn.type.is_function_variadic():
        args = args + ["..."]
    argstr = ", ".join(args) if args else "void"
    return f"{fn.result_type.spelling} {fn.spelling}({argstr})"


def _macro_text(defn) -> str:
    t = _read_extent(defn)
    return t if t.startswith("#") else f"#define {t}"


# ── the digest ────────────────────────────────────────────────────────────────
def _compute(bug_id: str, buggy_text: str, max_chars: int) -> str:
    cindex = _clang()
    repo, src = _repo_and_src(bug_id)
    if not src or not os.path.exists(src):
        return ""
    res = _resource_flags()
    opts = (cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
            | cindex.TranslationUnit.PARSE_INCOMPLETE)

    def _parse(flags):
        try:
            return cindex.Index.create().parse(src, args=flags + res + ["-w"], options=opts)
        except Exception:  # noqa: BLE001 — driver rejected the flags
            return None

    # Prefer the build's real flags; they occasionally hard-fail the driver, so fall
    # back to project-include flags (which parse to a usable partial AST).
    tu = _parse(_flags_from_ccjson(repo, src) or []) if _flags_from_ccjson(repo, src) else None
    tu = tu or _parse(_fallback_flags(repo, src))
    if tu is None:
        return ""

    refs = set(_IDENT.findall(buggy_text))
    if not refs:
        return ""
    CK = cindex.CursorKind
    repo_abs = os.path.abspath(repo)
    callees, macros, types = {}, {}, {}

    def from_repo(cur) -> bool:
        """Only PROJECT declarations — the model already knows the standard library and
        system headers, and a std::list definition would swamp the digest."""
        try:
            f = cur.location.file or cur.extent.start.file
            return bool(f) and os.path.abspath(f.name).startswith(repo_abs)
        except Exception:  # noqa: BLE001
            return False

    def add_type(decl):
        d = decl.get_definition() or decl
        name = d.spelling or decl.spelling
        if name and name in refs and name not in types and from_repo(d):
            txt = _read_extent(d)
            if txt:
                types[name] = txt[:1200]

    for n in tu.cursor.walk_preorder():
        k, ref = n.kind, n.referenced
        if k == CK.CALL_EXPR and ref and ref.spelling in refs and from_repo(ref):
            callees.setdefault(ref.spelling, _prototype(ref))
        elif k == CK.MACRO_INSTANTIATION and n.spelling in refs:
            if ref and ref.kind == CK.MACRO_DEFINITION and from_repo(ref):
                macros.setdefault(n.spelling, _macro_text(ref))
        elif k == CK.DECL_REF_EXPR and ref and ref.spelling in refs and from_repo(ref):
            if ref.kind == CK.FUNCTION_DECL:
                callees.setdefault(ref.spelling, _prototype(ref))
        elif k == CK.TYPE_REF and ref and ref.spelling in refs:
            add_type(ref)
        # a variable/field of a struct type the buggy code accesses (catches the
        # 'no member named X' class even when the type name isn't spelled out)
        elif k in (CK.VAR_DECL, CK.FIELD_DECL, CK.PARM_DECL):
            rec = n.type.get_canonical().get_declaration()
            if rec and rec.kind in (CK.STRUCT_DECL, CK.UNION_DECL) and rec.spelling in refs:
                add_type(rec)

    sections = []
    if macros:
        sections.append("// macros\n" + "\n".join(dict.fromkeys(macros.values())))
    if types:
        sections.append("// types\n" + "\n\n".join(dict.fromkeys(types.values())))
    if callees:
        sections.append("// function signatures (callees)\n"
                        + "\n".join(f"{s};" for s in dict.fromkeys(callees.values())))
    if not sections:
        return ""
    body = "\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars].rsplit("\n", 1)[0] + "\n// ... (truncated)"
    return (f"Declarations from {os.path.basename(src)} available to your fix "
            "(do not redefine these; use them):\n" + body)


def _cache_key(bug_id: str, buggy_text: str) -> str:
    _, src = _repo_and_src(bug_id)
    mtime = int(os.path.getmtime(src)) if src and os.path.exists(src) else 0
    h = hashlib.md5((bug_id + str(mtime) + str(len(buggy_text))).encode()).hexdigest()[:16]
    return re.sub(r"[^\w.-]", "_", bug_id)[:80] + "_" + h + ".json"


def digest(bug_id: str, buggy_text: str = "", *, max_chars: int = 2500,
           use_cache: bool = True) -> str:
    """libclang symbol digest for `bug_id`. Cached on disk; "" on any failure."""
    cpath = os.path.join(_CACHE_DIR, _cache_key(bug_id, buggy_text))
    if use_cache and os.path.exists(cpath):
        try:
            c = json.load(open(cpath))
            if c.get("v") == _VERSION:
                return c["digest"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    try:
        out = _compute(bug_id, buggy_text, max_chars)
    except Exception:  # noqa: BLE001 — never break a run over the digest
        out = ""
    if use_cache:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        try:
            json.dump({"v": _VERSION, "digest": out}, open(cpath, "w"))
        except OSError:
            pass
    return out


if __name__ == "__main__":
    # side-by-side view: regex digest vs libclang digest for a bug
    import sys
    import json as _j
    import source_context
    from harness_client import HarnessClient
    bug = sys.argv[1]
    defect = HarnessClient().get_defect(bug)
    buggy = next((m["content"] for m in reversed(defect["prompt_data"]["prompt"])
                  if m["role"] == "user"), "")
    print("=" * 70, "\nREGEX digest (source_context.symbol_digest):\n", "=" * 70)
    print(source_context.symbol_digest(bug, buggy) or "(empty)")
    print("\n" + "=" * 70, "\nLIBCLANG digest (clang_digest.digest):\n", "=" * 70)
    print(digest(bug, buggy, use_cache=False) or "(empty)")
