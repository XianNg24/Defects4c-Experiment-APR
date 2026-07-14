#!/usr/bin/env python3
"""Build a 3-model side-by-side comparison page for runs at identical settings.

    python scripts/make_comparison.py

Emits comparison.html: per-model results, what k=10 sampling bought over k=1, the
ensemble (union / core / uniquely-solved), a per-bug matrix, and the three models'
patches side by side against the reference fix.
"""
import html
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
os.chdir(APP)

import ground_truth  # noqa: E402
import viz  # noqa: E402

# All runs below share k=10, temp 0.8, rounds 0, 151 bugs — the only variable is the
# model (across a row) or the prompt (augmented vs baseline).
AUGMENTED = [
    ("deepseek",  "deepseek-coder-6.7b",   "runs/run_20260712-220953", "m1"),
    ("codellama", "CodeLlama-7b-Instruct", "runs/run_20260713-123547", "m2"),
    ("qwen",      "Qwen2.5-Coder-7B",      "runs/run_20260713-172754", "m3"),
]
# BASELINE: the dataset prompt verbatim — no diagnosis, no declarations, no guidance.
BASELINE = [
    ("deepseek",  "deepseek-coder-6.7b",   "runs/run_20260714-002016", "m1"),
    ("codellama", "CodeLlama-7b-Instruct", "runs/run_20260714-042240", "m2"),
    ("qwen",      "Qwen2.5-Coder-7B",      "runs/run_20260714-082713", "m3"),
]
# the earlier k=1 runs, for the "what sampling bought" delta (augmented page only)
K1 = {"deepseek": "runs/run_20260712-164344",
      "codellama": "runs/run_20260712-181712",
      "qwen": "runs/run_20260712-140151"}

MODE = "baseline" if "--baseline" in sys.argv else "augmented"
RUNS = BASELINE if MODE == "baseline" else AUGMENTED
OUTFILE = "comparison_baseline.html" if MODE == "baseline" else "comparison.html"

esc = html.escape

CSS = """
:root{
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --border:rgba(11,11,11,.10);
  --good:#0ca30c; --good-ink:#006300; --critical:#d03b3b; --warning:#fab219;
  --infra:#7d78b8;
  --add-bg:rgba(12,163,12,.12); --add-gutter:#0ca30c;
  --del-bg:rgba(208,59,59,.12); --del-gutter:#d03b3b;
  --chip:#f0efec;
  /* categorical model hues - deliberately off the green/red axis so the
     solved/failed semantics stay unambiguous next to them */
  --m1:#3a6ea5; --m2:#7d5ba6; --m3:#2f8f83;
  --m1-bg:rgba(58,110,165,.10); --m2-bg:rgba(125,91,166,.10); --m3-bg:rgba(47,143,131,.10);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --border:rgba(255,255,255,.10);
  --good-ink:#0ca30c; --chip:#26261f;
  --add-bg:rgba(12,163,12,.16); --del-bg:rgba(208,59,59,.16);
  --m1:#6fa3d8; --m2:#b18cd9; --m3:#57c2b3;
  --m1-bg:rgba(111,163,216,.14); --m2-bg:rgba(177,140,217,.14); --m3-bg:rgba(87,194,179,.14);
}}
:root[data-theme="dark"]{
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --border:rgba(255,255,255,.10);
  --good-ink:#0ca30c; --chip:#26261f;
  --add-bg:rgba(12,163,12,.16); --del-bg:rgba(208,59,59,.16);
  --m1:#6fa3d8; --m2:#b18cd9; --m3:#57c2b3;
  --m1-bg:rgba(111,163,216,.14); --m2-bg:rgba(177,140,217,.14); --m3-bg:rgba(87,194,179,.14);
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}
.mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace}
.wrap{max-width:1180px;margin:0 auto;padding:32px 20px 72px}
h1{font-size:23px;margin:0 0 6px;text-wrap:balance}
h2{font-size:14px;margin:36px 0 12px;color:var(--ink2);text-transform:uppercase;
  letter-spacing:.06em;font-weight:650}
.sub{color:var(--muted);font-size:13.5px;margin-bottom:18px;max-width:68ch}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:26px}
.chip{display:inline-block;font-size:12px;color:var(--ink2);background:var(--chip);
  border:1px solid var(--border);border-radius:6px;padding:2px 9px;
  font-variant-numeric:tabular-nums}
.note{font-size:13px;color:var(--ink2);max-width:70ch;margin:10px 0 0}

/* model tiles */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:15px 17px;border-top:3px solid var(--rule,var(--muted))}
.tile.m1{--rule:var(--m1)} .tile.m2{--rule:var(--m2)} .tile.m3{--rule:var(--m3)}
.tile.ens{--rule:var(--good);background:var(--add-bg)}
.tile .k{font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.tile .v{font-size:30px;font-weight:660;margin-top:5px;font-variant-numeric:tabular-nums}
.tile .d{font-size:12.5px;color:var(--ink2);margin-top:3px;font-variant-numeric:tabular-nums}

/* sampling delta */
.lift{display:grid;gap:8px;margin-top:4px}
.lift-row{display:grid;grid-template-columns:130px 1fr 120px;gap:12px;align-items:center}
.lift-row .nm{font-size:13px;font-weight:600}
.bars{display:flex;flex-direction:column;gap:3px}
.bar{height:11px;border-radius:3px;background:var(--chip);position:relative;overflow:hidden}
.bar span{display:block;height:100%;border-radius:3px}
.bar.k1 span{background:var(--muted);opacity:.55}
.lift-row.m1 .bar.k10 span{background:var(--m1)}
.lift-row.m2 .bar.k10 span{background:var(--m2)}
.lift-row.m3 .bar.k10 span{background:var(--m3)}
.lift .delta{font-size:13px;color:var(--good-ink);font-weight:650;text-align:right;
  font-variant-numeric:tabular-nums}
.legend{font-size:11.5px;color:var(--muted);margin-top:6px}

/* ensemble */
.ens-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stack{display:flex;height:26px;border-radius:6px;overflow:hidden;margin:10px 0 6px;gap:2px}
.stack i{display:block;font-style:normal}
.stack .core{background:var(--good)}
.stack .u1{background:var(--m1)} .stack .u2{background:var(--m2)} .stack .u3{background:var(--m3)}
.stack .pair{background:var(--muted);opacity:.45}
.keys{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--ink2)}
.keys b{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px}

/* matrix */
.scroll{overflow-x:auto;border:1px solid var(--border);border-radius:10px;
  background:var(--surface)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--grid)}
th{color:var(--muted);font-weight:650;font-size:11px;text-transform:uppercase;
  letter-spacing:.04em;position:sticky;top:0;background:var(--surface);z-index:1}
th.mh{text-align:center}
th.m1{color:var(--m1)} th.m2{color:var(--m2)} th.m3{color:var(--m3)}
td.cell{text-align:center;width:92px}
tr:last-child td{border-bottom:none}
.dot{display:inline-block;font-size:12px;font-weight:700;width:22px;height:22px;
  line-height:22px;border-radius:6px;border:1px solid var(--border)}
.dot.s{color:var(--good-ink);background:var(--add-bg)}
.dot.u{color:var(--muted);background:var(--chip)}
.dot.i{color:var(--infra);background:rgba(125,120,184,.14)}
.dot.e{color:var(--warning);background:rgba(250,178,25,.14)}
.bug{font-size:12.5px}
.bug .sha{color:var(--muted)}
tr.all-solved td{background:var(--add-bg)}
tr.none-solved td{background:transparent}

/* per-bug detail */
details.bugd{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  margin:10px 0;padding:0}
details.bugd>summary{cursor:pointer;padding:11px 14px;font-size:13px;
  display:flex;align-items:center;gap:10px;flex-wrap:wrap}
details.bugd>summary::-webkit-details-marker{display:none}
details.bugd>summary:focus-visible{outline:2px solid var(--m1);outline-offset:-2px}
.body{padding:0 14px 14px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.col{border:1px solid var(--border);border-radius:8px;overflow:hidden}
.col .h{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;padding:7px 10px;
  font-weight:650;border-bottom:1px solid var(--border)}
.col.m1 .h{color:var(--m1);background:var(--m1-bg)}
.col.m2 .h{color:var(--m2);background:var(--m2-bg)}
.col.m3 .h{color:var(--m3);background:var(--m3-bg)}
.col.ref .h{color:var(--good-ink);background:var(--add-bg)}
pre{margin:0;padding:10px;overflow-x:auto;font-size:12px;line-height:1.45;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  background:var(--plane);white-space:pre}
.diff .add{background:var(--add-bg);border-left:3px solid var(--add-gutter);display:block}
.diff .del{background:var(--del-bg);border-left:3px solid var(--del-gutter);display:block}
.empty{padding:10px;font-size:12px;color:var(--muted)}
.badge{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;font-weight:650;
  padding:1px 8px;border-radius:999px;border:1px solid var(--border)}
.badge.good{color:var(--good-ink);background:var(--add-bg)}
.badge.bad{color:var(--critical);background:var(--del-bg)}
.badge.infra{color:var(--infra);background:rgba(125,120,184,.14)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

SYM = {"s": "✔", "u": "·", "i": "⚙", "e": "!"}


def render_diff(diff):
    if not diff:
        return "<div class='empty'>no patch produced</div>"
    out = []
    for ln in diff.splitlines():
        if ln.startswith(("diff --git", "index ", "--- ", "+++ ", "@@")):
            continue
        c = "add" if ln.startswith("+") else "del" if ln.startswith("-") else ""
        out.append(f"<span class='{c}'>{esc(ln)}</span>" if c else esc(ln))
    body = "\n".join(out).strip("\n")
    return f"<pre class='diff'>{body}</pre>" if body else "<div class='empty'>no change</div>"


def best_attempt(trace):
    """The winning candidate if solved, else the first — one patch per model per bug."""
    atts = trace.get("attempts", [])
    for a in atts:
        if viz.true_pass(a.get("verdict", {})):
            return a
    return atts[0] if atts else None


def main():
    data, meta0 = {}, None
    for tag, _label, d, _c in RUNS:
        meta, rows, traces = viz.load_run(d)
        meta0 = meta0 or meta
        st = {r["bug_id"]: viz._status(r, traces) for r in rows}
        data[tag] = {"rows": rows, "traces": traces, "st": st,
                     "solved": {b for b, s in st.items() if s == "s"},
                     "infra": {b for b, s in st.items() if s == "i"}}

    tags = [t for t, _, _, _ in RUNS]
    all_bugs = sorted({b for t in tags for b in data[t]["st"]})
    n = len(all_bugs)
    infra = data[tags[0]]["infra"]
    buildable = n - len(infra)

    S = {t: data[t]["solved"] for t in tags}
    union = S[tags[0]] | S[tags[1]] | S[tags[2]]
    core = S[tags[0]] & S[tags[1]] & S[tags[2]]
    uniq = {t: S[t] - set().union(*[S[o] for o in tags if o != t]) for t in tags}
    best = max(len(S[t]) for t in tags)

    # k=1 baselines for the sampling delta
    k1n = {}
    for t, d in K1.items():
        try:
            m, rows, tr = viz.load_run(d)
            k1n[t] = sum(1 for r in rows if viz._status(r, tr) == "s")
        except Exception:  # noqa: BLE001
            k1n[t] = None

    if MODE == "baseline":
        title = "Defects4C · 3-model BASELINE comparison (k=10)"
        h1 = "Three 7B models on the raw Defects4C prompt"
        sub = ("Baseline ablation: each model gets the dataset's own prompt <b>verbatim</b> — "
               "no failure diagnosis, no symbol declarations, no repair guidance. Same "
               "sampling as the augmented runs (10 candidates at temperature 0.8), so the "
               "only variable is what we put in the prompt.")
    else:
        title = "Defects4C · 3-model comparison (k=10)"
        h1 = "Three 7B models on Defects4C, head to head"
        sub = ("Identical settings across all three runs — the only variable is the model. "
               "Sampling 10 candidates at temperature 0.8 and accepting any that passes the "
               "test oracle.")
    P = ["<style>", CSS, "</style>",
         f"<title>{esc(title)}</title>",
         "<div class='wrap'>",
         f"<h1>{esc(h1)}</h1>",
         f"<div class='sub'>{sub}</div>",
         "<div class='chips'>",
         ("<span class='chip'>prompt: dataset verbatim</span>" if MODE == "baseline"
          else "<span class='chip'>prompt: augmented</span>"),
         f"<span class='chip'>k = {meta0.get('k')}</span>",
         f"<span class='chip'>temperature {meta0.get('temperature')}</span>",
         f"<span class='chip'>repair rounds {meta0.get('repair_rounds')}</span>",
         f"<span class='chip'>{n} bugs</span>",
         f"<span class='chip'>{buildable} buildable</span>",
         f"<span class='chip'>{len(infra)} infra-blocked</span>",
         "<span class='chip'>no sanitizer rebuild</span>",
         "</div>"]

    # ── model tiles + ensemble ────────────────────────────────────────────────
    P.append("<h2>Result</h2><div class='tiles'>")
    for tag, label, _d, cls in RUNS:
        s = len(S[tag])
        P.append(
            f"<div class='tile {cls}'><div class='k'>{esc(label)}</div>"
            f"<div class='v'>{s}<span style='font-size:15px;color:var(--muted)'>/{buildable}</span></div>"
            f"<div class='d'>{100*s/buildable:.1f}% of buildable</div></div>")
    P.append(
        f"<div class='tile ens'><div class='k'>Ensemble · any model</div>"
        f"<div class='v'>{len(union)}<span style='font-size:15px;color:var(--muted)'>/{buildable}</span></div>"
        f"<div class='d'>{100*len(union)/buildable:.1f}% · +{len(union)-best} over best single</div></div>")
    P.append("</div>")

    # ── the delta section: sampling (augmented page) or augmentation (baseline page) ──
    if MODE == "baseline":
        # what our prompt augmentation is actually worth, at the same k and temperature
        aug = {}
        for tag, _l, d, _c in AUGMENTED:
            _m, rows_a, tr_a = viz.load_run(d)
            aug[tag] = sum(1 for r in rows_a if viz._status(r, tr_a) == "s")
        mx = max(max(len(S[t]) for t in tags), max(aug.values()))
        P.append("<h2>What our augmentation bought</h2>"
                 "<div class='sub' style='margin-bottom:12px'>Same model, same k, same "
                 "temperature — the only difference is whether the prompt carried our failure "
                 "diagnosis, symbol declarations and repair guidance. The effect is <b>not "
                 "uniform</b>: it is decisive for Qwen and a wash for the other two.</div>"
                 "<div class='lift'>")
        for tag, label, _d, cls in RUNS:
            b, a = len(S[tag]), aug[tag]          # b=baseline, a=augmented
            d = a - b
            sign = "+" if d > 0 else ""
            colour = ("var(--good-ink)" if d > 0 else
                      "var(--critical)" if d < 0 else "var(--muted)")
            P.append(
                f"<div class='lift-row {cls}'><div class='nm'>{esc(label)}</div>"
                f"<div class='bars'>"
                f"<div class='bar k1'><span style='width:{100*b/mx:.1f}%'></span></div>"
                f"<div class='bar k10'><span style='width:{100*a/mx:.1f}%'></span></div>"
                f"</div>"
                f"<div class='delta' style='color:{colour}'>{b} → {a} &nbsp;{sign}{d}</div></div>")
        P.append("</div><div class='legend'>upper bar: baseline (raw dataset prompt) "
                 "&nbsp;·&nbsp; lower bar: augmented (diagnosis + declarations + guidance)"
                 "</div>")
        P.append("<p class='note'>At k=1 the augmentation was worth +3 on deepseek. At k=10 "
                 "that edge disappears — sampling ten diverse candidates appears to recover "
                 "what the diagnosis was pointing at, for the models strong enough to use it. "
                 "Qwen is the exception: without the guidance it loses <b>12</b> bugs, so the "
                 "augmentation is doing real work there rather than being uniformly "
                 "redundant.</p>")
    elif all(k1n.get(t) for t in tags):
        mx = max(max(len(S[t]) for t in tags), max(k1n[t] for t in tags))
        P.append("<h2>What sampling bought</h2>"
                 "<div class='sub' style='margin-bottom:12px'>The same models, same prompts — "
                 "only k and temperature changed. At k=1 the dataset's own temperature (0.01) "
                 "is near-greedy, so extra candidates would have been near-duplicates.</div>"
                 "<div class='lift'>")
        for tag, label, _d, cls in RUNS:
            a, b = k1n[tag], len(S[tag])
            P.append(
                f"<div class='lift-row {cls}'><div class='nm'>{esc(label)}</div>"
                f"<div class='bars'>"
                f"<div class='bar k1'><span style='width:{100*a/mx:.1f}%'></span></div>"
                f"<div class='bar k10'><span style='width:{100*b/mx:.1f}%'></span></div>"
                f"</div>"
                f"<div class='delta'>{a} → {b} &nbsp;+{100*(b-a)/a:.0f}%</div></div>")
        P.append("</div><div class='legend'>upper bar: k=1 @ temp 0.01 &nbsp;·&nbsp; "
                 "lower bar: k=10 @ temp 0.8</div>")

    # ── ensemble breakdown ────────────────────────────────────────────────────
    pair_only = len(union) - len(core) - sum(len(uniq[t]) for t in tags)
    P.append("<h2>Where the models disagree</h2>"
             "<div class='sub' style='margin-bottom:12px'>The union beats every individual "
             "model because the solve sets are only partly overlapping — each model repairs "
             "bugs the other two cannot.</div>")
    seg = []
    if core:
        seg.append(f"<i class='core' style='flex:{len(core)}'></i>")
    if pair_only > 0:
        seg.append(f"<i class='pair' style='flex:{pair_only}'></i>")
    for i, t in enumerate(tags, 1):
        if uniq[t]:
            seg.append(f"<i class='u{i}' style='flex:{len(uniq[t])}'></i>")
    P.append("<div class='stack'>" + "".join(seg) + "</div>")
    P.append("<div class='keys'>"
             f"<span><b style='background:var(--good)'></b>all three ({len(core)})</span>"
             f"<span><b style='background:var(--muted);opacity:.45'></b>two of three ({pair_only})</span>"
             + "".join(
                 f"<span><b style='background:var(--m{i})'></b>only {esc(lbl)} ({len(uniq[t])})</span>"
                 for i, (t, lbl, _d, _c) in enumerate(RUNS, 1))
             + "</div>")
    P.append(f"<p class='note'>Best single model solves <b>{best}</b>. Taking any model's "
             f"passing patch solves <b>{len(union)}</b> — <b>+{len(union)-best}</b> bugs, a "
             f"{100*(len(union)-best)/best:.0f}% relative gain, at no change to the models "
             f"themselves.</p>")

    # ── per-bug matrix ────────────────────────────────────────────────────────
    P.append("<h2>Per-bug matrix</h2>"
             "<div class='sub' style='margin-bottom:12px'>Every bug, every model. "
             "✔ solved · · unsolved · ⚙ infra-blocked (baseline won't build).</div>"
             "<div class='scroll'><table><thead><tr><th>bug</th>"
             + "".join(f"<th class='mh {c}'>{esc(l)}</th>" for _t, l, _d, c in RUNS)
             + "<th class='mh'>solved by</th></tr></thead><tbody>")

    def rank(b):
        cnt = sum(1 for t in tags if data[t]["st"].get(b) == "s")
        return (-cnt, b)

    for b in sorted(all_bugs, key=rank):
        sts = [data[t]["st"].get(b, "e") for t in tags]
        cnt = sum(1 for s in sts if s == "s")
        cls = "all-solved" if cnt == 3 else ""
        proj, sha = (b.split("@") + [""])[:2]
        P.append(f"<tr class='{cls}'><td class='bug mono'>{esc(proj)}"
                 f"<span class='sha'>@{esc(sha[:8])}</span></td>")
        for s in sts:
            P.append(f"<td class='cell'><span class='dot {s}'>{SYM.get(s,'?')}</span></td>")
        P.append(f"<td class='cell mono' style='color:var(--muted)'>{cnt if cnt else '—'}</td></tr>")
    P.append("</tbody></table></div>")

    # ── three-way patch comparison ────────────────────────────────────────────
    P.append("<h2>Patches, side by side</h2>"
             "<div class='sub' style='margin-bottom:12px'>Each model's patch for the bug "
             "(the passing candidate if it solved it, otherwise its first attempt), against "
             "the human reference fix. Solved-first.</div>")
    for b in sorted(all_bugs, key=rank):
        sts = [data[t]["st"].get(b, "e") for t in tags]
        cnt = sum(1 for s in sts if s == "s")
        proj, sha = (b.split("@") + [""])[:2]
        pills = "".join(
            f"<span class='badge {'good' if s=='s' else 'infra' if s=='i' else 'bad'}'>"
            f"{esc(l)}</span>"
            for (_t, l, _d, _c), s in zip(RUNS, sts))
        P.append(
            f"<details class='bugd'><summary><span class='mono'>{esc(proj)}"
            f"<span class='sha' style='color:var(--muted)'>@{esc(sha[:10])}</span></span>"
            f"{pills}<span style='margin-left:auto;color:var(--muted);font-size:12px'>"
            f"{cnt}/3 solved</span></summary><div class='body'><div class='cols'>")
        for (t, label, _d, cls), s in zip(RUNS, sts):
            tr = data[t]["traces"].get(b)
            a = best_attempt(tr) if tr else None
            head = f"{label} — {'solved' if s=='s' else 'infra-blocked' if s=='i' else 'failed'}"
            P.append(f"<div class='col {cls}'><div class='h'>{esc(head)}</div>"
                     + (render_diff(a.get("patch_diff")) if a
                        else "<div class='empty'>no attempt (infra-blocked)</div>")
                     + "</div>")
        ref = None
        try:
            ref = ground_truth.reference_fix(b)
        except Exception:  # noqa: BLE001
            pass
        P.append("<div class='col ref'><div class='h'>reference fix (human)</div>"
                 + (render_diff(ref["diff"]) if ref
                    else "<div class='empty'>no reference diff available</div>")
                 + "</div>")
        P.append("</div></div></details>")

    P.append("</div>")
    out = os.path.join(APP, OUTFILE)
    with open(out, "w", encoding="utf-8") as f:
        f.write("".join(P))
    kb = os.path.getsize(out) // 1024
    print(f"bugs={n} buildable={buildable} infra={len(infra)}")
    for t, l, _d, _c in RUNS:
        print(f"  {l:24} solved {len(S[t]):3}  ({100*len(S[t])/buildable:.1f}%)")
    print(f"  union {len(union)}  core {len(core)}  best-single {best}  gain +{len(union)-best}")
    print(f"wrote {out} ({kb} KB)")


if __name__ == "__main__":
    main()
