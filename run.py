"""CLI entrypoint for the agentic APR loop.

Examples:
    # one specific bug
    python run.py --bug-id CESNET___libyang@140ede9c... --k 1

    # first 5 non-llvm bugs, k=5 with 1 repair round
    python run.py --limit 5 --k 5 --repair-rounds 1

    # all bugs of one project
    python run.py --project the-tcpdump-group___tcpdump --k 3
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from rich.console import Console
from rich.table import Table

import config
from artifacts import RunArtifacts
from graph import RepairRunner
from harness_client import HarnessClient

console = Console()


def select_bugs(client: HarnessClient, args) -> list[str]:
    if args.bug_id:
        return [args.bug_id]
    bugs = client.list_bugs()
    if args.project:
        bugs = [b for b in bugs if b.split("@")[0] == args.project]
    if args.limit:
        bugs = bugs[: args.limit]
    return bugs


def main() -> int:
    p = argparse.ArgumentParser(description="Agentic APR for Defects4C")
    p.add_argument("--bug-id", help="single bug (project@sha)")
    p.add_argument("--project", help="restrict to one project")
    p.add_argument("--limit", type=int, help="max number of bugs")
    p.add_argument("--k", type=int, default=config.K_CANDIDATES)
    p.add_argument("--repair-rounds", type=int, default=config.REPAIR_ROUNDS)
    p.add_argument("--model", default=config.OPENAI_MODEL)
    p.add_argument("--seed", type=int, default=config.SEED)
    p.add_argument("--patch-method", default="direct",
                   help="build_patch extraction: direct|diff|inline|auto")
    p.add_argument("--no-diagnose", action="store_true",
                   help="skip the observe→triage→tools diagnosis step (Phase-1 behavior)")
    p.add_argument("--no-sanitizer-rebuild", action="store_true",
                   help="keep diagnosis but never run the expensive sanitizer rebuild "
                        "(cheap log-only tools only)")
    p.add_argument("--max-tool-requests", type=int, default=config.MAX_TOOL_REQUESTS,
                   help="how many extra tools the LLM may request (hybrid loop)")
    p.add_argument("--no-critic", action="store_true",
                   help="Phase 3: use raw log-tail feedback instead of the Critic on all-k-fail")
    args = p.parse_args()
    diagnose = not args.no_diagnose
    use_critic = not args.no_critic
    if args.no_sanitizer_rebuild:
        config.ENABLE_SANITIZER_REBUILD = False

    client = HarnessClient()
    if not client.health():
        console.print("[red]Harness /health not ok — is the container running?[/red]")
        return 1

    bugs = select_bugs(client, args)
    if not bugs:
        console.print("[red]No bugs selected.[/red]")
        return 1

    run_id = dt.datetime.now().strftime("run_%Y%m%d-%H%M%S")
    meta = {
        "run_id": run_id, "model": args.model, "k": args.k,
        "repair_rounds": args.repair_rounds, "seed": args.seed,
        "patch_method": args.patch_method, "diagnose": diagnose,
        "use_critic": use_critic,
        "sanitizer_rebuild": config.ENABLE_SANITIZER_REBUILD, "n_bugs": len(bugs),
        "base_url": config.DEFECTS4C_BASE_URL, "llm_endpoint": config.OPENAI_BASE_URL,
    }
    run_art = RunArtifacts(config.RUNS_DIR, run_id, meta)
    console.print(f"[bold]Run {run_id}[/bold]  model={args.model}  k={args.k}  "
                  f"repair_rounds={args.repair_rounds}  bugs={len(bugs)}")
    console.print(f"Artifacts: {run_art.dir}\n")

    runner = RepairRunner(client, k=args.k, repair_rounds=args.repair_rounds,
                          model=args.model, seed=args.seed,
                          patch_method=args.patch_method, diagnose=diagnose,
                          max_tool_requests=args.max_tool_requests, use_critic=use_critic)

    n_solved = 0
    for i, bug_id in enumerate(bugs, 1):
        console.print(f"[cyan][{i}/{len(bugs)}][/cyan] {bug_id} ...", end=" ")
        try:
            defect = client.get_defect(bug_id)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]get_defect failed: {e}[/red]")
            run_art.append_result({"bug_id": bug_id, "error": str(e), "solved": False})
            continue

        art = run_art.bug(bug_id)
        art.write_defect(defect)
        try:
            state = runner.run(defect, art)
        except Exception as e:  # noqa: BLE001 — one bug failing must not sink the run
            console.print(f"[red]error: {e}[/red]")
            run_art.append_result({"bug_id": bug_id, "error": str(e), "solved": False})
            continue

        art.write_trace(state)
        solved = state.solved
        n_solved += int(solved)
        rounds_used = max((a.round_idx for a in state.attempts), default=0) + 1
        console.print("[green]SOLVED[/green]" if solved else "[yellow]unsolved[/yellow]",
                      f"({len(state.attempts)} attempts, {rounds_used} rounds)")
        run_art.append_result({
            "bug_id": bug_id, "project": state.project, "solved": solved,
            "n_attempts": len(state.attempts), "rounds_used": rounds_used,
            "winner": state.winner,
        })

    # ── summary ──
    tbl = Table(title=f"{run_id} — pass@{args.k}")
    tbl.add_column("metric"); tbl.add_column("value", justify="right")
    tbl.add_row("bugs", str(len(bugs)))
    tbl.add_row(f"solved (pass@{args.k})", f"{n_solved}/{len(bugs)}")
    tbl.add_row("solve rate", f"{100*n_solved/len(bugs):.1f}%")
    console.print()
    console.print(tbl)
    console.print(f"\nResults: {run_art.results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
