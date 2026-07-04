"""Diagnostic tool registry for the repair agent.

Each tool is a self-contained diagnostic that (a) declares when it is *applicable*
to an observed failure, (b) runs to produce structured evidence + a prompt block,
and (c) exposes an LLM-facing spec so the model can request it by name.

Selection is hybrid (per the design decision): `seed()` runs the tools whose
`applicable()` predicate matches the triaged evidence — deterministic, cheap
tools always, the one expensive tool (a full sanitizer rebuild) only when the
observed failure is a crash. The LLM may then request any additional tool by
name (handled in graph.py). Nothing here looks at the dataset's bug/vuln label.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import config
import asan_parse


@dataclass
class ToolContext:
    client: object            # HarnessClient
    bug_id: str
    log_text: str             # the observed reproduction log
    evidence: dict            # triage() output


@dataclass
class ToolResult:
    tool: str
    prompt_block: str         # what gets folded into the repair prompt
    data: dict                # structured, for artifacts/trace
    expensive: bool = False


class Tool:
    name: str = "tool"
    description: str = ""      # LLM-facing: what it gives you and when to ask
    expensive: bool = False

    def applicable(self, evidence: dict) -> bool:
        return False

    def run(self, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "expensive": self.expensive}


# ── sanitizer rebuild: the one expensive tool ─────────────────────────────────
class SanitizerRebuildTool(Tool):
    name = "sanitizer_rebuild"
    description = ("Rebuild the project with AddressSanitizer+UBSan and re-run the "
                  "failing test to turn a raw crash into a precise trace "
                  "(error class, faulting file:line, stack). Ask for this when the "
                  "program crashed/segfaulted and you need to know where and why.")
    expensive = True

    def applicable(self, evidence: dict) -> bool:
        if not config.ENABLE_SANITIZER_REBUILD:
            return False
        # A crash with no report is the canonical case; 'unknown' failures often
        # hide a memory error too. If a report already exists, no rebuild needed.
        return evidence.get("failure_class") in ("crash", "unknown")

    def run(self, ctx: ToolContext) -> ToolResult:
        try:
            h = ctx.client.reproduce(ctx.bug_id, sanitize=config.SANITIZE, force_cleanup=True)
            ctx.client.wait_for_reproduce(h)
            logs = ctx.client.read_test_logs(ctx.bug_id)
        except Exception as e:  # noqa: BLE001 — a tool failing must not sink the run
            return ToolResult(self.name, "", {"error": str(e)}, expensive=True)
        diag = asan_parse.parse_log(logs)
        if not diag:
            return ToolResult(self.name,
                              "", {"note": "sanitizer rebuild produced no report "
                                   "(the test framework likely caught the signal first)"},
                              expensive=True)
        return ToolResult(self.name, asan_parse.to_prompt_block(diag),
                          {"diagnosis": diag}, expensive=True)


# ── cheap, log-only tools ─────────────────────────────────────────────────────
class TestDiffTool(Tool):
    name = "test_diff"
    description = ("Extract the failing test's assertion — the expected vs actual "
                  "values and the test file:line. Ask for this on an ordinary "
                  "test failure to see exactly what the code should produce.")

    def applicable(self, evidence: dict) -> bool:
        return evidence.get("failure_class") == "assertion_mismatch"

    def run(self, ctx: ToolContext) -> ToolResult:
        a = ctx.evidence.get("assertion") or {}
        lines = ["Failing test assertion:"]
        if a.get("file"):
            lines.append(f"- at {a['file']}:{a.get('line')}")
        if a.get("detail"):
            lines.append("- " + a["detail"].replace("\n", "\n  "))
        if len(lines) == 1:
            lines.append("- " + (ctx.evidence.get("log_excerpt", "")[:400]))
        return ToolResult(self.name, "\n".join(lines), {"assertion": a})


class CompileDiagTool(Tool):
    name = "compile_diag"
    description = ("Extract the first compiler error and its location. Ask for this "
                  "when the patch or code fails to build.")

    def applicable(self, evidence: dict) -> bool:
        return evidence.get("failure_class") == "compile_error"

    def run(self, ctx: ToolContext) -> ToolResult:
        ce = ctx.evidence.get("compile_error") or {}
        if not ce.get("message"):     # nothing real to report — emit no block
            return ToolResult(self.name, "", {"compile_error": None})
        loc = f"{ce.get('file')}:{ce.get('line')}" if ce.get("file") else "(unknown location)"
        block = f"Compile error:\n- {loc}\n- {ce['message']}"
        return ToolResult(self.name, block, {"compile_error": ce})


class FailingTestTool(Tool):
    name = "failing_test"
    description = ("Show the source of the failing test — the assertions/conditions "
                  "your fix must satisfy. Ask for this to see exactly what behavior "
                  "the test expects.")

    def applicable(self, evidence: dict) -> bool:
        # any real failure where a test ran (not a pure compile error)
        return evidence.get("failure_class") in (
            "crash", "assertion_mismatch", "timeout", "unknown", "sanitizer_report")

    def run(self, ctx: ToolContext) -> ToolResult:
        import test_source
        block = test_source.failing_tests(ctx.bug_id, ctx.log_text)
        if not block:
            return ToolResult(self.name, "", {"note": "failing-test source not found"})
        return ToolResult(
            self.name,
            "Failing test (the conditions your fix must satisfy):\n```c\n" + block + "\n```",
            {"chars": len(block)})


REGISTRY: list[Tool] = [SanitizerRebuildTool(), FailingTestTool(),
                        TestDiffTool(), CompileDiagTool()]
_BY_NAME = {t.name: t for t in REGISTRY}


def format_sanitizer_report(evidence: dict) -> Optional[str]:
    """When triage already found a sanitizer report, format it directly (no rebuild)."""
    if evidence.get("failure_class") == "sanitizer_report" and evidence.get("sanitizer"):
        return asan_parse.to_prompt_block(evidence["sanitizer"])
    return None


def seed(ctx: ToolContext) -> list[ToolResult]:
    """Run every tool applicable to the observed evidence (deterministic seed)."""
    results = []
    pre = format_sanitizer_report(ctx.evidence)
    if pre:
        results.append(ToolResult("sanitizer_report", pre,
                                  {"diagnosis": ctx.evidence["sanitizer"]}))
    for tool in REGISTRY:
        if tool.applicable(ctx.evidence):
            results.append(tool.run(ctx))
    return [r for r in results if r.prompt_block or r.data]


def get(name: str) -> Optional[Tool]:
    return _BY_NAME.get(name)


def available_specs() -> list[dict]:
    return [t.spec() for t in REGISTRY]
