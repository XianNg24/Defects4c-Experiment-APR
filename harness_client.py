"""Thin client over the Defects4C HTTP service (new_main.py).

Wraps the five endpoints the repair workflow uses:
    list_defects_bugid → get_defect → build_patch → fix → status (poll)

The state machine in graph.py (Phase 1) calls these; nothing here is
agent-specific. `--smoke` exercises only the two READ-ONLY endpoints
(list + get_defect) so it is safe to run while the container is busy
(e.g. during run_warmup.sh) — it never triggers a build or test run.

Usage:
    python harness_client.py --smoke
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

import config


class HarnessError(RuntimeError):
    """Raised when the service returns an error status or an HTTP failure."""




class HarnessClient:
    def __init__(self, base_url: str = config.DEFECTS4C_BASE_URL,
                 timeout: int = config.HARNESS_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ── low-level ─────────────────────────────────────────────────────────────
    def _get(self, path: str) -> dict[str, Any]:
        r = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict) -> dict[str, Any]:
        r = requests.post(
            f"{self.base_url}{path}",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    # ── read-only ─────────────────────────────────────────────────────────────
    def health(self) -> bool:
        return self._get("/health").get("status") == "ok"

    def list_bugs(self, exclude_substr: Optional[str] = config.EXCLUDE_SUBSTR) -> list[str]:
        data = self._get("/list_defects_bugid")
        if data.get("status") != "success":
            raise HarnessError(f"list_defects_bugid failed: {data}")
        bugs = data.get("defects", [])
        if exclude_substr:
            subs = [s.strip() for s in exclude_substr.split(",") if s.strip()]
            bugs = [b for b in bugs if not any(s in b for s in subs)]
        return bugs

    def get_defect(self, bug_id: str) -> dict[str, Any]:
        """Full prompt + metadata for one defect.

        Returns the raw response; callers use `bug_id` (authoritative
        project@sha), `prompt_data.prompt` (message list), and
        `prompt_data.temperature`.
        """
        data = self._get(f"/get_defect/{bug_id}")
        if data.get("status") != "success":
            raise HarnessError(f"get_defect({bug_id}) failed: {data}")
        return data

    # ── write / compute (used from Phase 1 onward) ────────────────────────────
    def build_patch(self, bug_id: str, llm_response: str, method: str = "direct",
                    generate_diff: bool = True, persist_flag: bool = True) -> dict[str, Any]:
        """Turn an LLM response into a patch file on disk. Returns `fix_p` path."""
        data = self._post("/build_patch", {
            "bug_id": bug_id,
            "llm_response": llm_response,
            "method": method,
            "generate_diff": generate_diff,
            "persist_flag": persist_flag,
        })
        if not data.get("success"):
            raise HarnessError(f"build_patch failed: {data.get('error_code')}: {data.get('error')}")
        return data

    def reproduce(self, bug_id: str, sanitize: Optional[str] = None,
                  force_cleanup: bool = False) -> str:
        """Build+test the bug's revisions; with `sanitize` set, an ASAN/UBSan build
        so the crash report lands in the test log. Returns the poll handle."""
        payload = {"bug_id": bug_id, "is_force_cleanup": force_cleanup}
        if sanitize:
            payload["sanitize"] = sanitize
        data = self._post("/reproduce", payload)
        handle = data.get("handle")
        if not handle:
            raise HarnessError(f"reproduce() returned no handle: {data}")
        return handle

    def wait_for_reproduce(self, handle: str,
                           poll_interval: int = config.FIX_POLL_INTERVAL,
                           max_wait: int = config.REPRODUCE_MAX_WAIT) -> dict[str, Any]:
        return self.wait_for_fix(handle, poll_interval=poll_interval, max_wait=max_wait)

    def read_test_logs(self, bug_id: str) -> str:
        """Return the BUGGY revision's failure output from the host mount — the run
        we diagnose (sanitizer report / crash / assertion lives here).

        The `.log`/`.msg` the harness writes are already *test-only* ctest output
        (`ctest … >> test_log`), so they carry the ctest summary, the crash-vs-failure
        marker (`***Exception` vs `***Failed`) and the gtest assertion — everything we
        need, with no build noise. (The JUnit `.log.xml` is structured but can't tell a
        crash from an assertion for non-gtest frameworks, so it isn't preferred.)
          1. `.log` / `.msg` — the real test output (vuln set writes its sanitizer
             oracle to `.msg`);
          2. the reproduce/build log (`<sha>.log`) — ONLY when tests never ran (the
             build broke); prefixed so it is not mistaken for test output and triage
             does not mine failures out of build noise.
        Buggy revision before fix, first non-empty wins."""
        import os
        project, sha = bug_id.split("@", 1)
        logdir = os.path.join(config.OUT_DIR, project, "logs")
        for stem in (f"test_{sha}_buggy", f"test_{sha}_fix"):
            for ext in (".log", ".msg"):
                p = os.path.join(logdir, stem + ext)
                if os.path.exists(p):
                    txt = open(p, errors="replace").read()
                    if txt.strip():
                        return txt
        # No test output at all → tests never ran (build broke). Surface the build log
        # so the configure/compile error is still visible, but label it: it is NOT
        # test output, and triage must not mine test failures out of build noise.
        bl = os.path.join(logdir, f"{sha}.log")
        if os.path.exists(bl):
            txt = open(bl, errors="replace").read()
            if txt.strip():
                # A build log can be ~MB; the error (if any) is at the end — the head is
                # just successful steps. Keep the last 200 lines so we never return an
                # unbounded blob (triage tails further before anything hits the prompt).
                tail = "\n".join(txt.splitlines()[-200:])
                return ("[no test output captured — tests did not run; last 200 lines "
                        "of the build log below]\n" + tail)
        return ""

    def fix(self, bug_id: str, patch_path: str) -> str:
        """Submit a patch for test-suite verification; return the poll handle."""
        data = self._post("/fix", {"bug_id": bug_id, "patch_path": patch_path})
        handle = data.get("handle")
        if not handle:
            raise HarnessError(f"fix() returned no handle: {data}")
        return handle

    def status(self, handle: str) -> dict[str, Any]:
        return self._get(f"/status/{handle}")

    def wait_for_fix(self, handle: str,
                     poll_interval: int = config.FIX_POLL_INTERVAL,
                     max_wait: int = config.FIX_MAX_WAIT) -> dict[str, Any]:
        """Poll /status until completed/failed or timeout. Returns final status dict.

        A 404 is treated as "not visible yet" rather than fatal: a long task whose
        handle was stored while redis was briefly unavailable only reappears once
        its terminal state is written. We tolerate that for the whole budget.
        """
        deadline = time.time() + max_wait
        last: dict[str, Any] = {}
        while time.time() < deadline:
            try:
                last = self.status(handle)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    time.sleep(poll_interval)
                    continue
                raise
            if last.get("status") in ("completed", "failed"):
                return last
            time.sleep(poll_interval)
        last["_timed_out"] = True
        return last


# ── smoke test (read-only) ────────────────────────────────────────────────────
def _smoke() -> int:
    client = HarnessClient()
    print(f"[smoke] base_url = {client.base_url}")

    if not client.health():
        print("[smoke] FAIL: /health did not return ok")
        return 1
    print("[smoke] health: ok")

    bugs = client.list_bugs()
    print(f"[smoke] list_defects_bugid: {len(bugs)} bugs (excluding '{config.EXCLUDE_SUBSTR}')")
    if not bugs:
        print("[smoke] FAIL: no bugs returned")
        return 1
    print(f"[smoke] first bug: {bugs[0]}")

    defect = client.get_defect(bugs[0])
    msgs = defect["prompt_data"]["prompt"]
    temp = defect["prompt_data"].get("temperature")
    print(f"[smoke] get_defect: bug_id={defect['bug_id']}")
    print(f"[smoke]   {len(msgs)} prompt messages, temperature={temp}")
    for m in msgs:
        preview = m["content"].replace("\n", " ")[:80]
        print(f"[smoke]   - {m['role']}: {preview}...")

    print("[smoke] PASS: read-only endpoints healthy (no build/test triggered)")
    return 0


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:
        sys.exit(_smoke())
    print(__doc__)
