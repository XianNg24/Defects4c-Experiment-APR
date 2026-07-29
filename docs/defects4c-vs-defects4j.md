# Defects4C vs. Defects4J — a benchmark comparison

Written from hands-on experience building an agentic APR system on **Defects4C** (C/C++),
contrasted with the well-established **Defects4J** (Java) benchmark. The short version:
Defects4J was engineered to avoid the three problems that dominated our Defects4C work
(heterogeneous test frameworks, thin test-case information, baseline build/compile
failures) — but it trades them for a different set of harder problems (fault
localization, patch overfitting, data leakage).

---

## At a glance

| Dimension | Defects4C (C/C++) | Defects4J (Java) |
|---|---|---|
| Language | C / C++ | Java |
| Size | 248 bugs + 102 CVE vulns (2 tracks) | ~835 bugs, ~17 projects (single track) |
| Test frameworks | 8+ heterogeneous (gtest, Catch2, cmocka, cppcheck, curl `runtests.pl`, automake TAP, tcpdump `TESTLIST`, PHP `run-tests.php`/`.phpt`) | Essentially always **JUnit** |
| Build systems | cmake+ninja, autotools, automake, per-project recipes | Ant / Maven, hidden behind one CLI |
| Test-case metadata | Sparse — must be scraped from logs / repo | Curated & exported per bug |
| Baseline builds | Fragile — deps, era-specific libs, bulk commits | Guaranteed reproducible, pinned JDK |
| Fault localization | **Given** (exact buggy hunk masked) | **Not given** — tool must localize |
| Memory-safety oracle | Sanitizer / PoC track exists (partial) | None — memory-safe language |
| Maturity / exposure | Newer, less in training data | 10+ yrs public, heavy training-data leakage |

---

## The three problems we hit on Defects4C

### 1. Multiple test frameworks — **Defects4J largely avoids this**
Defects4C bugs span 8+ test runners, each with a distinct failure format, so a large part
of our `triage.py` / `test_source.py` is per-framework extractors:
- gtest `Value of:/Expected:/Actual:`, Catch2 `file:line: FAILED:`, cmocka `[ ERROR ]`,
  cppcheck `ASSERT_EQUALS` + plain `ASSERT`, curl `N: stdout FAILED`, automake TAP
  `# FAIL: N`, tcpdump data-driven `TESTLIST` + expected `.out`, PHP `run-tests.php`
  `FAIL … [x.phpt]` + the `.phpt` `--EXPECTF--` oracle.
- ctest `-VV` adds its own noise: `N:` line prefixes, ANSI colors, `Test timeout
  computed…`, `***Failed` — all of which produced false positives until guarded.

**Defects4J:** projects use different *build* tools (Ant/Maven) but they're normalized
behind one CLI (`defects4j compile` / `test` / `export`), and the *test* framework is
uniformly JUnit → one failure shape (assertion message + Java stack trace). No extractor
zoo.

### 2. Insufficient test-case information — **Defects4J solves it by construction**
On Defects4C the base prompt only names *which test binary* failed; we had to reconstruct
the actual specification from the repo:
- brace-match the failing test function out of source (gtest/cmocka),
- pull a plain `ASSERT`'s condition from the test's source window,
- for data-driven runners, dig the golden output out of the fixture (tcpdump
  `TESTLIST` → expected `.out`; PHP `.phpt` → `--FILE--` + `--EXPECTF--`).

**Defects4J** exports this per bug, no scraping:
- `tests.trigger` — the exact tests that fail on buggy / pass on fixed,
- the stack trace + assertion message for each triggering test,
- `tests.relevant`, `tests.all`, `classes.modified`, `classes.loaded`,
- the developer's bug-fixing diff.

### 3. Compile / infra errors — **almost absent for the Defects4J baseline**
On Defects4C the baseline itself often won't build: missing `-dev` packages, era-specific
library versions a single Docker image can't satisfy, autotools/JIT builds that take
minutes, and mislabeled entries whose "fix" commit is a bulk import (e.g. libgd
CVE-2016-9317, where the src diff is a blank line). In one run, **14 of 66 CVE bugs were
`compile_error` on the baseline**, before any model patch.

**Defects4J** guarantees both buggy and fixed versions **compile and run reproducibly**
with pinned JDKs (mostly 7/8) and Docker images. Being pure Java, there is no native
dependency hell — the JVM abstracts the platform. Compile errors only come from the
*candidate patch the model generates*, which is inherent to APR everywhere.

### Bonus: the sanitizer/memory-safety axis doesn't exist in Defects4J
Defects4C has a CVE/vulnerability track whose oracle is (sometimes) a PoC run under
AddressSanitizer/UBSan. But that trace only appears when the project's `build_tpl.jinja`
enables sanitizers — measured on one run, only **5 of 66 CVE bugs** produced a real
sanitizer trace; the rest surfaced as output diffs (`assertion_mismatch`) or
`compile_error`. Java is memory-safe: bugs are assertion failures or exceptions (NPE,
etc.) with clean stack traces — no ASan/UBSan/PoC dimension, no "test named `_asan` but
not instrumented", no segfaults without a trace.

---

## Where Defects4J is actually *harder*

The difficulty doesn't vanish; it moves.

- **No fault-localization hint.** Defects4C masks the exact buggy hunk (perfect
  localization — the model's job is synthesis). Defects4J gives you nothing; you must run
  spectrum-based localization (GZoltar) yourself. Localization is genuinely harder there.
- **Patch overfitting / weak test suites.** The classic Defects4J critique: a generated
  patch passes the triggering tests but is semantically wrong ("plausible but
  incorrect"). The suite under-specifies the fix. (Defects4C has a milder version via the
  `return_code==0 AND fix_status==success` oracle.)
- **Flaky / order-dependent tests** in a few projects muddy pass/fail.
- **Data leakage.** Defects4J has been public 10+ years and is heavily represented in LLM
  training data — a real confound when evaluating an LLM repair agent. Defects4C (newer,
  C/C++) is less exposed.
- **Old-JDK pinning** is its own environment friction, though far milder than the C/C++
  dependency tail.

---

## Net takeaway

Porting this agent to Defects4J would let us **delete** most of what was hard here — the
per-framework `triage.py` extractors, the `test_source.py` reconstruction, and nearly all
the Docker/dependency and sanitizer machinery — because the harness hands you a uniform
JUnit failure, the exact triggering tests with their assertions, and a guaranteed-building
baseline.

In exchange we'd have to **add** what Defects4C gives for free: fault localization, and
defenses against overfit/plausible-but-wrong patches.

The problems shift from **"can I even build it and read the failure?"** (the C/C++
reality) to **"where is the bug, and is my patch actually correct?"** (the Java reality).
