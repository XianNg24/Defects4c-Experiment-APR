"""Central configuration for the agentic APR system.

Every value is overridable via environment variable so the same code runs
against a local Defects4C container, a remote service, and any
OpenAI-compatible model endpoint (vLLM, DeepSeek, OpenAI) without edits.
"""
import os

# ── Defects4C harness (new_main.py HTTP service) ──────────────────────────────
DEFECTS4C_BASE_URL = os.environ.get("DEFECTS4C_BASE_URL", "http://127.0.0.1:11111")
HARNESS_TIMEOUT = int(os.environ.get("DEFECTS4C_TIMEOUT", "30"))       # per HTTP call
FIX_POLL_INTERVAL = int(os.environ.get("DEFECTS4C_POLL_INTERVAL", "10"))
FIX_MAX_WAIT = int(os.environ.get("DEFECTS4C_FIX_MAX_WAIT", "600"))    # /fix can be slow

# ── LLM (OpenAI-compatible endpoint — open models served via vLLM) ────────────
# Decision §4.2: one OpenAI-compatible endpoint so client code is model-agnostic.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "EMPTY")             # vLLM ignores it
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8888/v1/")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "deepseek-ai/deepseek-coder-6.7b-instruct")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "4096"))
# Per-request timeout. openai's default is read=600s with 2 retries, so one wedged
# server costs ~30min per call and silently poisons a whole run.
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "180"))
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "1"))
# Consecutive timeouts that mean "the endpoint is dead", not "this bug is hard".
LLM_MAX_CONSECUTIVE_TIMEOUTS = int(os.environ.get("LLM_MAX_CONSECUTIVE_TIMEOUTS", "3"))
# Serving precision. "auto" = the model's native config torch_dtype (what vLLM
# loads by default). Set APR_DTYPE=bfloat16/float16 on BOTH serve + run to force one.
LLM_DTYPE = os.environ.get("APR_DTYPE", "auto")

# ── Agent loop budgets (see task.md §3 / §5) ──────────────────────────────────
K_CANDIDATES = int(os.environ.get("APR_K", "1"))          # candidates per round
REPAIR_ROUNDS = int(os.environ.get("APR_REPAIR_ROUNDS", "0"))
SEED = int(os.environ.get("APR_SEED", "0"))
# Sampling temperature. None = use the dataset's per-defect value, which is 0.01 for
# every entry (near-greedy). Raise it (0.6-0.8) whenever k > 1: at 0.01 the k candidates
# come back near-identical, so best-of-k adds cost without adding coverage.
_t = os.environ.get("APR_TEMPERATURE")
TEMPERATURE = float(_t) if _t else None
# Use the libclang semantic symbol digest (header-aware) instead of the regex one.
USE_CLANG_DIGEST = os.environ.get("APR_CLANG_DIGEST", "0") not in ("0", "false", "")
# Inject gdb-captured runtime values at the buggy line (value-dependent defects).
USE_GDB_VALUES = os.environ.get("APR_GDB_VALUES", "0") not in ("0", "false", "")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(BASE_DIR, "runs")

# Host path to the container's /out mount, so the agent can read test logs
# directly (sanitizer reports land in <OUT_DIR>/<project>/logs/test_<sha>_*.log).
OUT_DIR = os.environ.get(
    "DEFECTS4C_OUT_DIR",
    os.path.join(os.path.dirname(BASE_DIR), "defects4c", "out_tmp_dirs"))

# ── Diagnostics / tools (Phase 2, evidence-driven) ────────────────────────────
SANITIZE = os.environ.get("APR_SANITIZE", "address,undefined")
REPRODUCE_MAX_WAIT = int(os.environ.get("APR_REPRODUCE_MAX_WAIT", "1800"))  # ASAN full build
# Master switch for the evidence-driven diagnosis step (observe→triage→tools).
ENABLE_DIAGNOSIS = os.environ.get("APR_DIAGNOSE", "1") not in ("0", "false", "")
# The one expensive tool (full sanitizer rebuild); can be disabled for fast batches.
ENABLE_SANITIZER_REBUILD = os.environ.get("APR_SANITIZER_REBUILD", "1") not in ("0", "false", "")
# How many extra tools the LLM may request before committing a patch (hybrid loop).
MAX_TOOL_REQUESTS = int(os.environ.get("APR_MAX_TOOL_REQUESTS", "1"))

# ── Critic (Phase 3) ──────────────────────────────────────────────────────────
# On all-k-fail, a structured critique (failure_class, root_cause, replacement)
# replaces the raw log-tail feedback in the next repair round.
USE_CRITIC = os.environ.get("APR_CRITIC", "1") not in ("0", "false", "")

# Comma-separated substrings; a bug is skipped if it contains ANY of them.
# Excluded by default: llvm (CPU-heavy); njs (its ./configure/autotest hangs in a
# loop, spews GBs into autoconf.err, and orphans a runaway process that fills the
# disk — see host-resource-limits memory); pcre2 (autotools autogen.sh + serial
# configure feature-probes + a full --enable-jit libpcre2 compile make every build
# take minutes, unlike the cmake+ninja projects — only 1 bug, not worth the wall time).
EXCLUDE_SUBSTR = os.environ.get(
    "APR_EXCLUDE_SUBSTR", "llvm___llvm,nginx___njs,PCRE2Project___pcre2")
