"""OpenAI-compatible LLM client for the agent.

Decision §4.2: open models (DeepSeek-Coder, CodeLlama, Qwen2.5-Coder) are
served behind one OpenAI-compatible endpoint (vLLM), so the same client code
works for every model and for hosted APIs — set OPENAI_BASE_URL / OPENAI_MODEL.

k>1 requests k sampled candidates in a single call (n=k). k=1 uses the
defect's recommended temperature (often ~0, i.e. near-greedy).

Usage:
    OPENAI_BASE_URL=... OPENAI_MODEL=... python llm.py --smoke
"""
from __future__ import annotations

from typing import Any, Optional

import config


def _client():
    # Imported lazily so harness_client --smoke has no hard openai dependency.
    from openai import OpenAI
    return OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL)


def served_model() -> str:
    """The model id the endpoint is actually serving, so the agent auto-syncs to
    whatever vLLM was launched with. Falls back to config.OPENAI_MODEL."""
    try:
        return _client().models.list().data[0].id
    except Exception:  # noqa: BLE001 — endpoint down / unexpected shape
        return config.OPENAI_MODEL


def generate(messages: list[dict], *, k: int = 1,
             temperature: float = 0.7, seed: int = config.SEED,
             model: str = config.OPENAI_MODEL,
             max_tokens: int = config.LLM_MAX_TOKENS) -> dict[str, Any]:
    """Return {candidates: [text, ...], model, usage} for a chat request.

    k candidates come back via `n=k`. Every candidate's raw text is returned;
    patch extraction happens downstream (build_patch handles code blocks/diffs).
    """
    resp = _client().chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        n=k,
        seed=seed,
    )
    candidates = [c.message.content or "" for c in resp.choices]
    usage = getattr(resp, "usage", None)
    return {
        "candidates": candidates,
        "model": model,
        "temperature": temperature,
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
        } if usage else {},
    }


# ── smoke test ────────────────────────────────────────────────────────────────
def _smoke() -> int:
    print(f"[smoke] base_url = {config.OPENAI_BASE_URL}")
    print(f"[smoke] model    = {config.OPENAI_MODEL}")
    try:
        out = generate(
            [{"role": "user", "content": "Reply with exactly the word: pong"}],
            k=1, temperature=0.0, max_tokens=16,
        )
    except Exception as e:  # noqa: BLE001 — smoke test surfaces any failure plainly
        print(f"[smoke] SKIP/FAIL: could not reach model endpoint: {e}")
        print("[smoke] set OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY to a live "
              "OpenAI-compatible endpoint (e.g. a vLLM server) and re-run.")
        return 2  # distinct from harness failure; endpoint may just not be up yet
    text = (out["candidates"][0] or "").strip()
    print(f"[smoke] response: {text!r}")
    print(f"[smoke] usage: {out['usage']}")
    print("[smoke] PASS: model endpoint reachable" if text else
          "[smoke] WARN: empty completion")
    return 0 if text else 1


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:
        sys.exit(_smoke())
    print(__doc__)
