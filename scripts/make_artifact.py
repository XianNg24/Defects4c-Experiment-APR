#!/usr/bin/env python3
"""Build a self-contained dashboard HTML for a run — ready to publish as an Artifact.

    python scripts/make_artifact.py [RUN_DIR] [--serve [PORT]]

RUN_DIR defaults to the most recent runs/run_*. The output is a single self-contained
HTML file (the same body I publish to claude.ai): the KPI tiles, per-project and
per-category breakdowns, and every bug's diagnosis / attempts / diff / reference fix.

Publishing the file:
  - ask Claude to publish it (Claude calls the Artifact tool → a claude.ai URL), or
  - view it locally with `--serve` (no claude.ai needed), or
  - open the file directly in a browser.
A plain script cannot create a claude.ai artifact URL — that needs the Artifact tool.
"""
import glob
import http.server
import os
import socketserver
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import viz  # noqa: E402


def latest_run() -> str:
    runs = sorted(glob.glob(os.path.join(APP, "runs", "run_*")), key=os.path.getmtime)
    if not runs:
        sys.exit("no runs found under runs/")
    return runs[-1]


def build(run_dir: str) -> str:
    meta, rows, traces = viz.load_run(run_dir)
    model = meta.get("model", "?").split("/")[-1]
    rid = meta.get("run_id", os.path.basename(run_dir))
    n = len(rows)
    solved = sum(1 for r in rows if r.get("solved"))
    infra = sum(1 for r in rows if r.get("infra_blocked"))
    title = f"Defects4C Repair Run · {rid} ({model})"
    body = f"<title>{title}</title>\n" + viz.build_artifact_body(meta, rows, traces)
    out = os.path.join(run_dir, "artifact.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"run:     {rid}  ({model})")
    print(f"bugs:    {n}   solved: {solved}   infra-blocked: {infra}")
    print(f"wrote:   {out}  ({len(body) // 1024} KB)")
    return out


def serve(path: str, port: int) -> None:
    directory = os.path.dirname(path)
    fname = os.path.basename(path)

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=directory, **k)

    with socketserver.TCPServer(("", port), H) as httpd:
        print(f"\nserving at http://localhost:{port}/{fname}  (Ctrl-C to stop)")
        print("in VS Code, forward the port to open it in your browser.")
        httpd.serve_forever()


def main() -> None:
    args = [a for a in sys.argv[1:]]
    run_dir = next((a for a in args if not a.startswith("--") and not a.isdigit()), None)
    run_dir = run_dir or latest_run()
    if not os.path.isdir(run_dir):
        sys.exit(f"not a run dir: {run_dir}")

    out = build(run_dir)

    if "--serve" in args:
        port = next((int(a) for a in args if a.isdigit()), 8899)
        serve(out, port)
    else:
        print("\nto publish: ask Claude to publish this file as an artifact,")
        print(f"            or re-run with --serve to view it locally:")
        print(f"            python scripts/make_artifact.py {os.path.basename(run_dir)} --serve")


if __name__ == "__main__":
    main()
