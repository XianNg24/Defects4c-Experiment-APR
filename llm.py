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


class LLMUnavailable(RuntimeError):
    """The endpoint is dead — not this bug being hard. Raised after repeated
    transport failures so a wedged server aborts the run instead of silently marking
    every remaining bug 'error: Request timed out.'"""


_consecutive_failures = 0


def _client():
    # Imported lazily so harness_client --smoke has no hard openai dependency.
    from openai import OpenAI
    return OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL,
                  timeout=config.LLM_TIMEOUT, max_retries=config.LLM_MAX_RETRIES)


def served_model() -> str:
    """The model id the endpoint is actually serving, so the agent auto-syncs to
    whatever vLLM was launched with. Falls back to config.OPENAI_MODEL."""
    try:
        return _client().models.list().data[0].id
    except Exception:  # noqa: BLE001 — endpoint down / unexpected shape
        return config.OPENAI_MODEL


def served_precision(model: str = None) -> str:
    """The serving float precision, for reproducibility. If APR_DTYPE forces one,
    that; otherwise the served model's native config torch_dtype (what vLLM 'auto'
    loads). So two runs with the same value are numerically comparable."""
    if config.LLM_DTYPE and config.LLM_DTYPE != "auto":
        return config.LLM_DTYPE
    import glob
    import json
    import os
    model = model or served_model()
    pat = os.path.expanduser(
        f"~/.cache/huggingface/hub/models--{model.replace('/', '--')}/snapshots/*/config.json")
    for cfg in glob.glob(pat):
        try:
            return json.load(open(cfg)).get("torch_dtype", "unknown")
        except Exception:  # noqa: BLE001
            pass
    return "unknown"


def _merge_consecutive(messages: list[dict]) -> list[dict]:
    """Merge adjacent same-role messages into one. Some chat templates (CodeLlama,
    Llama-2) require strict user/assistant alternation and 400 on two consecutive
    user turns — which our diagnosis/tool-request/repair-feedback paths can produce.
    Merging is a no-op for lenient templates (deepseek)."""
    out: list[dict] = []
    for m in messages:
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] = out[-1]["content"].rstrip() + "\n\n" + m["content"]
        else:
            out.append(dict(m))
    return out


# vLLM reports context overflow in two formats; we parse the window size and,
# when available, the message-token count:
#   "maximum context length is 15360 tokens ... (11304 in the messages, 4096 ...)"
#   "maximum context length is 15360 tokens ... prompt contains at least 11265 input"
import re as _re
_MIN_COMPLETION = 256          # never shrink the answer budget below this


def _parse_ctx_error(text: str):
    """(ctx, msg_tokens|None, exact) from a vLLM context-length 400, or None if not
    one. The "N in the messages" form is exact; "at least N input tokens" is a lower
    bound (it rises as we shrink the completion), so completion-shrink can't use it."""
    mc = _re.search(r"maximum context length is (\d+)", text)
    if not mc:
        return None
    exact = _re.search(r"\((\d+) in the messages", text)
    if exact:
        return int(mc.group(1)), int(exact.group(1)), True
    lb = _re.search(r"prompt contains at least (\d+) input tokens", text)
    return int(mc.group(1)), (int(lb.group(1)) if lb else None), False


def _truncate_to_chars(messages: list[dict], target_total_chars: int) -> list[dict]:
    """Shrink the largest message so the messages total ≤ target_total_chars, keeping
    its head (function signature) and tail (the infill point + instructions). Returns
    the input unchanged if it's already small enough or can't be reduced."""
    out = [dict(m) for m in messages]
    total = sum(len(m["content"]) for m in out)
    if total <= target_total_chars:
        return out
    big = max(out, key=lambda m: len(m["content"]))
    c = big["content"]
    keep = len(c) - (total - target_total_chars)
    if keep < len(c) and keep > 0:
        big["content"] = (c[: keep * 6 // 10]
                          + "\n\n... [context truncated to fit the model window] ...\n\n"
                          + c[-keep * 4 // 10:])
    return out


def generate(messages: list[dict], *, k: int = 1,
             temperature: float = 0.7, seed: int = config.SEED,
             model: str = config.OPENAI_MODEL,
             max_tokens: int = config.LLM_MAX_TOKENS) -> dict[str, Any]:
    """Return {candidates: [text, ...], model, usage} for a chat request.

    k candidates come back via `n=k`. Every candidate's raw text is returned;
    patch extraction happens downstream (build_patch handles code blocks/diffs).

    On a context-length 400 the request is re-fit to the model window (shrink the
    completion budget, then truncate the prompt if still too long) and retried,
    rather than failing the bug.
    """
    # A wedged/dead server shows up as a read timeout OR a connection error (refused,
    # reset, accept-backlog full). Both mean "endpoint down". APIStatusError (400/404)
    # does not — that's a real problem with this one request, so it stays per-bug.
    from openai import APIConnectionError, APITimeoutError, BadRequestError
    global _consecutive_failures
    msgs = _merge_consecutive(messages)
    cur_max = max_tokens
    for _ in range(8):
        try:
            resp = _client().chat.completions.create(
                model=model,
                messages=msgs,
                temperature=temperature,
                max_tokens=cur_max,
                n=k,
                seed=seed,
            )
        except (APITimeoutError, APIConnectionError) as e:
            _consecutive_failures += 1
            if _consecutive_failures >= config.LLM_MAX_CONSECUTIVE_TIMEOUTS:
                raise LLMUnavailable(
                    f"{_consecutive_failures} consecutive transport failures at "
                    f"{config.OPENAI_BASE_URL} (timeout={config.LLM_TIMEOUT:.0f}s): "
                    f"{type(e).__name__} — the endpoint looks wedged or down. "
                    "Check the vLLM log; restart it before re-running.") from None
            raise
        except BadRequestError as e:
            parsed = _parse_ctx_error(str(e))
            if not parsed:
                raise                       # a different 400 — real per-request problem
            _consecutive_failures = 0       # server answered, so it's alive
            ctx, msg_tokens, exact = parsed
            # Escalate cheapest-first: (1) with an exact message-token count, shrink the
            # completion budget to the exact room the prompt leaves — the prompt is
            # essential, the answer is a few lines; (2) else drop the completion to its
            # floor; (3) if the prompt itself still won't fit, truncate it and retry.
            if exact and ctx - msg_tokens - 32 >= _MIN_COMPLETION \
                    and cur_max > ctx - msg_tokens - 32:
                cur_max = ctx - msg_tokens - 32
            elif cur_max > _MIN_COMPLETION:
                cur_max = _MIN_COMPLETION
            else:
                # Prompt itself won't fit even with a floor completion. vLLM only
                # reports a capped lower bound here, so we can't compute an exact
                # char target — cut the largest message by a fixed fraction each pass
                # until it fits; raise if it can't shrink further.
                cur_total = sum(len(m["content"]) for m in msgs)
                new = _truncate_to_chars(msgs, int(cur_total * 0.55))
                if sum(len(m["content"]) for m in new) >= cur_total:
                    raise
                msgs = new
            continue
        break
    else:
        raise RuntimeError("context-fit retries exhausted")
    _consecutive_failures = 0
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
