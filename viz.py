"""Phase 5 visualization: turn a run's artifacts into self-contained HTML.

Generates, from `runs/<ts>/`:
  - `index.html`            aggregate dashboard (KPI tiles, per-project breakdown,
                            bug table linking into each timeline)
  - `<bug_id>/timeline.html` per-bug timeline: diagnosis → rounds → (prompt,
                            diff, verdict) — reads only that bug's trace.json

No external assets (inline CSS/JS), light+dark aware. Designed to enrich as later
phases add fields: a Critic `replacement_block` or Memory "pitfalls" in the trace
render as extra sections automatically.

    python viz.py runs/run_YYYYMMDD-HHMMSS      # build dashboard + all timelines
"""
from __future__ import annotations

import html
import json
import os
import sys
from typing import Optional

# ── palette (validated dataviz reference instance) ────────────────────────────
CSS = """
:root{
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --border:rgba(11,11,11,.10);
  --good:#0ca30c; --good-ink:#006300; --critical:#d03b3b; --warning:#fab219;
  --add-bg:rgba(12,163,12,.12); --add-gutter:#0ca30c;
  --del-bg:rgba(208,59,59,.12); --del-gutter:#d03b3b;
  --chip:#f0efec;
}
:root[data-theme="dark"], html.dark{ }
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --border:rgba(255,255,255,.10);
  --good:#0ca30c; --good-ink:#0ca30c; --critical:#d03b3b; --warning:#fab219;
  --add-bg:rgba(12,163,12,.16); --del-bg:rgba(208,59,59,.16); --chip:#26261f;
}}
:root[data-theme="dark"]{
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --border:rgba(255,255,255,.10);
  --good-ink:#0ca30c; --add-bg:rgba(12,163,12,.16); --del-bg:rgba(208,59,59,.16);
  --chip:#26261f;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}
a{color:inherit}
.wrap{max-width:1100px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;margin:28px 0 12px;color:var(--ink2)}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px}
.tile .k{font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.tile .v{font-size:30px;font-weight:650;margin-top:6px;font-variant-numeric:tabular-nums}
.tile .v.good{color:var(--good-ink)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
td.num{font-variant-numeric:tabular-nums;text-align:right}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;
  padding:2px 9px;border-radius:999px;border:1px solid var(--border)}
.badge.good{color:var(--good-ink);background:var(--add-bg)}
.badge.bad{color:var(--critical);background:var(--del-bg)}
.badge.err{color:var(--warning);background:rgba(250,178,25,.14)}
.chip{display:inline-block;font-size:12px;color:var(--ink2);background:var(--chip);
  border:1px solid var(--border);border-radius:6px;padding:1px 8px;font-variant-numeric:tabular-nums}
.pbar{display:flex;height:12px;border-radius:6px;overflow:hidden;gap:2px;background:transparent}
.pbar span{display:block}
.seg-good{background:var(--good)} .seg-bad{background:var(--muted)} .seg-err{background:var(--critical)}
.proj{display:grid;grid-template-columns:180px 1fr 90px;gap:12px;align-items:center;margin:8px 0;font-size:13px}
.proj .lbl{color:var(--ink2)} .proj .cnt{color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin:14px 0}
.round-h{font-size:14px;font-weight:650;margin:0 0 2px}
details{margin:8px 0}
summary{cursor:pointer;color:var(--ink2);font-size:13px;user-select:none}
pre{margin:8px 0 0;padding:12px;background:var(--plane);border:1px solid var(--grid);
  border-radius:8px;overflow-x:auto;font-size:12.5px;line-height:1.45;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre}
.diff{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
  border:1px solid var(--grid);border-radius:8px;overflow-x:auto;background:var(--surface)}
.diff .ln{display:block;padding:0 10px;white-space:pre}
.diff .add{background:var(--add-bg);border-left:3px solid var(--add-gutter)}
.diff .del{background:var(--del-bg);border-left:3px solid var(--del-gutter)}
.diff .hunk{color:var(--muted)} .diff .meta{color:var(--muted)}
.diag{border-left:3px solid var(--warning);padding:2px 0 2px 14px;margin:6px 0}
.k{color:var(--muted)} .mono{font-family:ui-monospace,Menlo,monospace}
.toggle{position:fixed;top:14px;right:16px;font-size:12px;color:var(--ink2);
  background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:5px 10px;cursor:pointer}
.back{font-size:13px;color:var(--ink2);text-decoration:none}
"""

TOGGLE_JS = """
<script>
(function(){var r=document.documentElement,b=document.querySelector('.toggle');
if(!b)return;b.onclick=function(){var d=r.getAttribute('data-theme')==='dark'?'light':'dark';
r.setAttribute('data-theme',d);b.textContent=d==='dark'?'☀ light':'☾ dark';};})();
</script>"""


def esc(s) -> str:
    return html.escape("" if s is None else str(s))


# ── the pass/fail oracle (recomputed from stored fields, not the stale flag) ───
def _fix_status(v: dict) -> str:
    return (v.get("fix_status") or "").strip().replace("\\n", "").lower()


def true_pass(v: dict) -> bool:
    """A real fix compiles AND passes tests: rc==0 AND fix_status==success."""
    return v.get("return_code") == 0 and _fix_status(v).startswith("success")


def naive_pass(v: dict) -> bool:
    """The old, wrong oracle: rc==0 alone (accepts test failures)."""
    return v.get("return_code") == 0


def bug_solved(trace: dict) -> bool:
    return any(true_pass(a.get("verdict", {})) for a in trace.get("attempts", []))


def bug_false_positive(trace: dict) -> bool:
    """Any attempt the old oracle would have called a pass but that truly failed."""
    return any(naive_pass(a.get("verdict", {})) and not true_pass(a.get("verdict", {}))
               for a in trace.get("attempts", []))


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
            f"<button class='toggle'>☾ dark</button><div class='wrap'>{body}</div>"
            f"{TOGGLE_JS}</body></html>")


# ── per-bug timeline ──────────────────────────────────────────────────────────
def _render_diff(diff: Optional[str]) -> str:
    if not diff:
        return "<div class='k'>(no diff)</div>"
    out = []
    for ln in diff.splitlines():
        cls = ""
        if ln.startswith(("+++", "---", "diff ", "index ")):
            cls = "meta"
        elif ln.startswith("@@"):
            cls = "hunk"
        elif ln.startswith("+"):
            cls = "add"
        elif ln.startswith("-"):
            cls = "del"
        out.append(f"<span class='ln {cls}'>{esc(ln) or '&nbsp;'}</span>")
    return "<div class='diff'>" + "".join(out) + "</div>"


def _render_prompt(messages: list) -> str:
    parts = []
    for m in messages or []:
        parts.append(f"### {m.get('role','?').upper()}\n{m.get('content','')}")
    return "<pre>" + esc("\n\n".join(parts)) + "</pre>"


def _verdict_badge(v: dict) -> str:
    if true_pass(v):
        return "<span class='badge good'>✔ passed</span>"
    if v.get("error"):
        return f"<span class='badge err'>⚠ {esc(v.get('error'))[:60]}</span>"
    rc = v.get("return_code")
    # distinguish a build failure from a test failure, and flag the old false positive
    if rc == 0 and _fix_status(v).startswith("fail"):
        return ("<span class='badge bad'>✘ test failed</span> "
                "<span class='badge err' title='return_code==0 but the test did not pass'>"
                "false-positive under rc-only</span>")
    return f"<span class='badge bad'>✘ build failed (rc={esc(rc)})</span>"


def _render_reference_fix(bug_id: Optional[str]) -> str:
    """Ground-truth fix (git commit_before→after), for human comparison only —
    never seen by the agent."""
    try:
        import ground_truth
        ref = ground_truth.reference_fix(bug_id) if bug_id else None
    except Exception:  # noqa: BLE001
        ref = None
    if not ref:
        return ""
    return (f"<h2>Reference fix <span class='k' style='font-weight:400'>"
            f"(ground truth · {esc(ref['before'])}→{esc(ref['after'])} · not shown to the agent)"
            f"</span></h2><div style='border-left:3px solid var(--good);padding-left:2px'>"
            f"{_render_diff(ref['diff'])}</div>")


def _render_diagnosis(diag: Optional[dict]) -> str:
    if not diag:
        return ""
    ev = diag.get("evidence", {})
    tools = ", ".join(diag.get("tools_used", [])) or "none"
    blocks = "\n\n".join(diag.get("blocks", []))
    body = (f"<div class='diag'><div><span class='k'>failure class</span> "
            f"<span class='chip'>{esc(ev.get('failure_class','?'))}</span> "
            f"<span class='k'>tools</span> <span class='chip'>{esc(tools)}</span></div>"
            f"<div style='margin-top:4px'>{esc(ev.get('summary',''))}</div>")
    if blocks:
        body += f"<pre>{esc(blocks)}</pre>"
    return f"<h2>Diagnosis</h2>{body}</div>"


def _timeline_body(trace: dict, back: str = "") -> str:
    solved = bug_solved(trace)
    head = (f"{back}"
            f"<h1 class='mono'>{esc(trace.get('bug_id'))}</h1>"
            f"<div class='sub'>{'<span class=\"badge good\">✔ solved</span>' if solved else '<span class=\"badge bad\">✘ unsolved</span>'}"
            f" &nbsp; project <span class='chip'>{esc(trace.get('project'))}</span>"
            f" &nbsp; failure <span class='chip'>{esc(trace.get('mode'))}</span>"
            f" &nbsp; temp <span class='chip'>{esc(trace.get('temperature'))}</span></div>")
    diag = _render_diagnosis(trace.get("diagnosis"))
    ref = _render_reference_fix(trace.get("bug_id"))

    # group attempts by round
    rounds: dict[int, list] = {}
    for a in trace.get("attempts", []):
        rounds.setdefault(a.get("round_idx", 0), []).append(a)
    body = [head, diag, ref, "<h2>Repair rounds</h2>"]
    for ridx in sorted(rounds):
        for a in rounds[ridx]:
            v = a.get("verdict", {})
            label = "initial generation" if ridx == 0 else f"repair round {ridx}"
            body.append(
                f"<div class='card'><div class='round-h'>Round {ridx} · candidate "
                f"{a.get('cand_idx')} &nbsp; {_verdict_badge(v)}</div>"
                f"<div class='k'>{label}</div>"
                f"<details><summary>prompt</summary>{_render_prompt(a.get('prompt_messages'))}</details>"
                f"<div style='margin-top:8px'>{_render_diff(a.get('patch_diff'))}</div>"
                + (f"<details><summary>test / build log tail</summary><pre>{esc((v.get('log_tail') or '')[-2000:])}</pre></details>"
                   if v.get("log_tail") else "")
                + (f"<h2 style='margin-top:10px'>Critic</h2><pre>{esc(a.get('critic_note'))}</pre>"
                   if a.get("critic_note") else "")
                + "</div>")
    return "".join(body)


def render_timeline(trace: dict) -> str:
    back = "<a class='back' href='../index.html'>← dashboard</a>"
    return _page(trace.get("bug_id", "bug"), _timeline_body(trace, back))


# ── aggregate dashboard ───────────────────────────────────────────────────────
def _tile(k: str, v: str, good=False) -> str:
    return f"<div class='tile'><div class='k'>{esc(k)}</div><div class='v{' good' if good else ''}'>{esc(v)}</div></div>"


def _safe_bug_dir(bug_id: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._@-]", "_", bug_id)


def _status(r: dict, traces: dict) -> str:
    """True per-bug status recomputed from the trace's verdicts, not stale flags."""
    if r.get("error"):
        return "e"
    tr = traces.get(r.get("bug_id"))
    if tr and bug_solved(tr):
        return "s"
    return "u"


def render_dashboard(meta: dict, rows: list, traces: dict, single: bool = False) -> str:
    n = len(rows)
    st = {r.get("bug_id"): _status(r, traces) for r in rows}
    solved = [b for b, s in st.items() if s == "s"]
    errored = [b for b, s in st.items() if s == "e"]
    fps = sum(1 for tr in traces.values() if bug_false_positive(tr) and not bug_solved(tr))
    rate = (100 * len(solved) / n) if n else 0
    k = meta.get("k", 1)

    tiles = ("<div class='tiles'>"
             + _tile("bugs", str(n))
             + _tile(f"solved (pass@{k}, true)", f"{len(solved)}/{n}", good=True)
             + _tile("solve rate", f"{rate:.0f}%", good=True)
             + _tile("false positives (rc-only)", str(fps))
             + _tile("errored", str(len(errored)))
             + _tile("model", str(meta.get("model", "?")).split("/")[-1])
             + "</div>"
             + "<div class='sub' style='margin-top:8px'>Pass = compiles <b>and</b> tests "
               "pass (return_code==0 AND fix_status==success). “False positives” "
               "counts bugs the old return_code-only oracle would have marked solved but "
               "whose tests actually failed.</div>")

    # per-project stacked breakdown (recomputed status)
    projs: dict[str, dict] = {}
    for r in rows:
        p = r.get("project") or r.get("bug_id", "?").split("@")[0]
        d = projs.setdefault(p, {"s": 0, "u": 0, "e": 0, "n": 0})
        d["n"] += 1
        d[_status(r, traces)] += 1
    proj_html = ["<h2>Per-project breakdown</h2>"]
    for p, d in sorted(projs.items(), key=lambda kv: -kv[1]["n"]):
        segs = ""
        for cls, key in (("seg-good", "s"), ("seg-bad", "u"), ("seg-err", "e")):
            if d[key]:
                segs += f"<span class='{cls}' style='flex:{d[key]}'></span>"
        proj_html.append(
            f"<div class='proj'><div class='lbl mono'>{esc(p)}</div>"
            f"<div class='pbar'>{segs}</div>"
            f"<div class='cnt'>{d['s']}/{d['n']}</div></div>")

    # bug table
    trows = []
    for r in sorted(rows, key=lambda r: (_status(r, traces) != "s", r.get("bug_id", ""))):
        bid = r.get("bug_id", "?")
        tr = traces.get(bid, {})
        status = _status(r, traces)
        if status == "e":
            badge = "<span class='badge err'>⚠ error</span>"
        elif status == "s":
            badge = "<span class='badge good'>✔ solved</span>"
        elif tr and bug_false_positive(tr):
            badge = ("<span class='badge bad'>✘ unsolved</span> "
                     "<span class='badge err'>was false-pos</span>")
        else:
            badge = "<span class='badge bad'>✘ unsolved</span>"
        fclass = tr.get("mode", "")
        short = bid.split("@")[0]
        sha = "@" + bid.split("@")[1][:10] if "@" in bid else ""
        # only link bugs that actually produced a trace (errored bugs have none)
        if bid in traces:
            href = f"#bug-{esc(_safe_bug_dir(bid))}" if single else f"{esc(_safe_bug_dir(bid))}/timeline.html"
            name = (f"<a class='mono' href='{href}'>"
                    f"{esc(short)}<span class='k'>{esc(sha)}</span></a>")
        else:
            name = f"<span class='mono'>{esc(short)}<span class='k'>{esc(sha)}</span></span>"
        trows.append(
            f"<tr><td>{name}</td><td>{badge}</td>"
            f"<td>{'<span class=\"chip\">'+esc(fclass)+'</span>' if fclass else ''}</td>"
            f"<td class='num'>{esc(r.get('n_attempts',''))}</td>"
            f"<td class='num'>{esc(r.get('rounds_used',''))}</td></tr>")
    table = ("<h2>Bugs</h2><table><thead><tr><th>bug</th><th>result</th>"
             "<th>failure class</th><th class='num'>attempts</th>"
             "<th class='num'>rounds</th></tr></thead><tbody>"
             + "".join(trows) + "</tbody></table>")

    head = (f"<h1>Repair run · {esc(meta.get('run_id',''))}</h1>"
            f"<div class='sub'>model <span class='chip'>{esc(meta.get('model'))}</span> "
            f"k={esc(meta.get('k'))} · repair_rounds={esc(meta.get('repair_rounds'))} · "
            f"diagnose={esc(meta.get('diagnose'))}</div>")
    body = head + tiles + "".join(proj_html) + table
    if single:
        return body
    return _page(f"run {meta.get('run_id','')}", body)


def build_single_page(meta: dict, rows: list, traces: dict) -> str:
    """One self-contained page: dashboard + every timeline inlined via #anchors.
    Suitable for publishing as a shareable Artifact (no sub-pages)."""
    parts = [render_dashboard(meta, rows, traces, single=True)]
    for bid, tr in traces.items():
        anchor = _safe_bug_dir(bid)
        top = "<a class='back' href='#'>↑ dashboard</a>"
        parts.append(f"<section id='bug-{esc(anchor)}' style='margin-top:40px;"
                     f"border-top:1px solid var(--grid);padding-top:8px'>"
                     f"{_timeline_body(tr, top)}</section>")
    return _page(f"run {meta.get('run_id','')}", "".join(parts))


# ── driver ────────────────────────────────────────────────────────────────────
def build_artifact_body(meta: dict, rows: list, traces: dict) -> str:
    """Content for a claude.ai Artifact: <style> + body inner only (no doctype/
    head/body wrapper, no theme toggle — the Artifact host supplies those)."""
    parts = [f"<style>{CSS}</style><div class='wrap'>",
             render_dashboard(meta, rows, traces, single=True)]
    for bid, tr in traces.items():
        anchor = _safe_bug_dir(bid)
        top = "<a class='back' href='#'>↑ dashboard</a>"
        parts.append(f"<section id='bug-{esc(anchor)}' style='margin-top:40px;"
                     f"border-top:1px solid var(--grid);padding-top:8px'>"
                     f"{_timeline_body(tr, top)}</section>")
    parts.append("</div>")
    return "".join(parts)


def load_run(run_dir: str):
    with open(os.path.join(run_dir, "run_meta.json")) as f:
        meta = json.load(f)
    rp = os.path.join(run_dir, "results.jsonl")
    rows = [json.loads(l) for l in open(rp) if l.strip()] if os.path.exists(rp) else []
    traces = {}
    for entry in os.listdir(run_dir):
        tpath = os.path.join(run_dir, entry, "trace.json")
        if os.path.exists(tpath):
            tr = json.load(open(tpath))
            traces[tr["bug_id"]] = tr
    return meta, rows, traces


def build(run_dir: str) -> str:
    with open(os.path.join(run_dir, "run_meta.json")) as f:
        meta = json.load(f)
    rows = []
    rp = os.path.join(run_dir, "results.jsonl")
    if os.path.exists(rp):
        rows = [json.loads(l) for l in open(rp) if l.strip()]

    traces: dict[str, dict] = {}
    for entry in os.listdir(run_dir):
        tpath = os.path.join(run_dir, entry, "trace.json")
        if os.path.exists(tpath):
            tr = json.load(open(tpath))
            traces[tr["bug_id"]] = tr
            with open(os.path.join(run_dir, entry, "timeline.html"), "w") as f:
                f.write(render_timeline(tr))

    index = os.path.join(run_dir, "index.html")
    with open(index, "w") as f:
        f.write(render_dashboard(meta, rows, traces))
    return index


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    out = build(sys.argv[1])
    print(f"dashboard: {out}")
    print(f"timelines: {os.path.dirname(out)}/<bug_id>/timeline.html")
