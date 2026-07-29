# Reflections: Difficulties in Agentic C/C++ Program Repair (DEFECTS4C)

Notes on the practical difficulties encountered while building an agentic
automated-program-repair (APR) system for the DEFECTS4C C/C++ benchmark
(248 bugs across ~30 projects + 102 CVE vulnerabilities). These are the things
that consumed the most effort and that generalize beyond this project — most of
them are *not* about the LLM at all, but about the surrounding realities of
building, running, and understanding real C/C++ test suites.

---

## 1. Test-framework fragmentation (the single biggest source of friction)

There is no common failure format in C/C++. Every project reports test failures
differently, and a diagnosis pipeline has to understand each one to extract "what
failed and why." We ended up writing **eight** distinct extractors, and each was
discovered the hard way — by seeing the model receive garbage instead of the
failure.

| Framework | Projects | Failure shape | Gotcha |
|---|---|---|---|
| **GoogleTest** | fmt, entt, rocksdb, arrow, SPIRV | `file:line: Failure` + `Value of/Expected/Actual` | Non-`EXPECT_EQ` macros (`ASSERT_OK`, custom matchers) print only an expression + status, no labels. Enums print as `Which is: 4-byte object <..>`. Output is **ANSI-colored** (`\x1b[0;31m[ FAILED ]`). |
| **Catch2** | CLI11 | `file:line: FAILED:` + `CHECK/REQUIRE(...)` + `with expansion:` | The failure often sits *below* an "informational" banner test, so a naive tail/head grab shows the banner. |
| **cmocka** | libyang | `[ ERROR ] --- <expr / a!=b>` then a generic `file:line: error: Failure!` | The useful line is *before* the "Failure!" trailer, not after. cmocka also **catches SIGSEGV with its own handler**, hiding the ASan report. |
| **cppcheck (own)** | cppcheck | `ASSERT_EQUALS` → `Expected:`/`Actual:` values on the *next* line (may be empty = "expect no warning"); plain `ASSERT(cond)` → only `file:line(Class::test): Assertion failed`, **no values at all** | The plain-ASSERT case has zero detail — you must read the test source at that line to recover the condition. |
| **curl runtests.pl** | curl | `<N>: stdout FAILED:` + `TESTFAIL: These test cases failed: <N>`, test names on `test <N>...[desc]` | The failure detail is a *diff of expected vs generated stdout*; the log also opens with a huge "System characteristics" banner that a fallback grabs. |
| **automake TAP** | libgd | `FAIL: <name>` per test + `# TOTAL / # PASS / # FAIL: N` summary | The summary line **`# ERROR: 0`** (zero errors!) matched a greedy `error:` regex and got mislabeled a *compile error*. |
| **tcpdump runner** | tcpdump | `<name> : passed` per test, `<name> : TEST FAILED`, `Failed test: <name>`, then a `<`/`>` diff | The passing tests are listed *first*, so a fallback shows "esp1: passed, esp2: passed…" instead of the one failure and its decode diff. |
| **ctest -VV wrapper** | (wraps all of the above) | `Test #N: <exe> ***Failed` / `***Exception: SegFault`, prefixes **every line with `N: `**, prints `Test timeout computed to be: N` for *every* test | The `N:` prefix and `timeout computed` line poisoned nearly every regex; `***Failed` vs `***Exception` is the only crash-vs-assertion signal. |

**Lessons.**
- Text log parsing of test output is a long tail of special cases; each framework
  needs its own rule, and ctest's `-VV` layer adds a second layer of noise on top.
- Greedy regexes are the enemy. `\berror:` matched `# ERROR: 0`; `timeout` matched
  "Test timeout computed to be"; `- Failed` matched cmake's `Performing Test X - Failed`.
  Every classification signal must be anchored to a *real* marker, not a bare word.
- The JUnit XML that ctest can emit (`--output-junit`) is structured, but it
  **cannot distinguish a crash from an assertion** for non-gtest frameworks (both
  are `status="fail"` with an empty `<failure>`), so the flat `.log`/`.msg` (with
  `***Exception` vs `***Failed`) is actually the *better* source.

---

## 2. External-library installation is machine-specific and silent

A large fraction of "unsolved" bugs were never actually attempted — the project's
**baseline didn't build** because a dev dependency was missing on the machine. This
is entirely environmental and varies by container/host.

Concretely, of 23 baseline-build failures, ~15 were missing packages:

| Project | Missing | Symptom |
|---|---|---|
| php-src (×8) | `libxml2-dev` | `xml2-config not found` |
| libgd (×5) | `libfontconfig-dev` | `fontconfig requested but not found` |
| sniproxy | `gettext` (m4 macros) | `possibly undefined macro: AC_LIB_PREPARE_PREFIX` |
| cjose | `autoconf-archive` | `possibly undefined macro: ...` |

**Lessons.**
- Installing the `-dev` package is necessary but sometimes **not sufficient**:
  autotools projects ship a pre-generated `configure` that must be re-generated
  (`autoreconf -fi`) to pick up newly installed `.m4` macros. cjose/sniproxy stayed
  broken even after the macro packages were installed for exactly this reason.
- **ABI/version mismatches** are worse than missing libs: `arrow` links against a
  system gtest but references `testing::internal::g_linked_ptr_mutex`, a symbol
  removed in newer gtest — a runtime `symbol lookup error`, not a compile error, so
  it needs its *own* detection (and should be excluded, not "repaired").
- These failures are **invisible to the model** (it just sees "unsolved") unless the
  harness surfaces the configure/compile error — and they unfairly depress the
  reported solve rate because a bug you can't build looks identical to one you
  couldn't fix. They must be classified as *infra-blocked* and excluded from pass@k.
- The right home for these fixes is the build image (Dockerfile), so the environment
  is reproducible — but no image ever covers every project's transitive deps.

---

## 3. Build-system fragility and destructive state

The build/test harness is stateful and easy to corrupt, and a "wrong verdict on a
correct fix" is far more damaging than an honest failure.

- **A diagnosis step destroyed the build the fix needs.** A crash triggers a
  sanitizer rebuild; that rebuild ran `git clean -dfx`, which wiped the plain
  `build_<sha>` directory. The patch build (`ninja -C build_<sha>`, no cmake) then
  died with `ninja: fatal: chdir … No such file or directory`. The result: a
  *correct* fix (`tokAt(6)` → `tokAt(5)`) was marked FAILED. The same fix had
  "passed" in earlier runs only because the bug was *mis*-classified (so the
  destructive rebuild never ran). Correctness fixes can expose latent bugs.
- **`inplace_rebuild` assumes the build dir exists** (skips cmake for speed). Any
  process that removes it silently breaks all subsequent patch tests for that bug.
- **Logs are flattened** — the harness stripped newlines from build/test logs, so
  every `^…$` line-anchored regex silently failed until made newline-agnostic.
- **Logs can be huge** (up to ~1.1 MB build logs). Returning them unbounded risks
  exceeding the model context; only the *tail* of a build log (where the error is)
  is useful.
- **Sanitizer/scoped builds time out.** SPIRV-Tools has 600+ compile targets; an
  ASan build of the whole project doesn't finish, so those bugs never produce test
  output at all — no amount of parsing recovers what was never captured.
- **Concurrency is unsafe.** Two processes patching the same `git_repo_dir`
  corrupt each other (a probe run collided with a live run and inherited a mangled
  `assstatic` token from the other's patch). Ad-hoc probing must wait for the run.

---

## 4. Sanitizers help *identify*, not *fix* — and only sometimes even identify

AddressSanitizer/UBSan are powerful but narrower than intuition suggests.

- **Compile-time instrumentation is required.** The bug set is built *without*
  `-fsanitize`, so its segfaults produce only a bare "Segmentation fault" — no
  trace. Only the CVE/vuln set builds with ASan by design.
- **Segfaults are the case ASan is weakest on.** A heap overflow trips a redzone and
  gives a rich report; a **null-pointer deref or wild pointer** just faults in
  hardware — ASan's signal handler can print "SEGV on unknown address" (a stack
  trace, no allocation context), and if it isn't linked in, nothing.
- **The fault location is often not actionable.** Across 10 sanitizer bugs, the
  faulting frame landed in the ASan runtime (`sanitizer_common_interceptors.inc`) or
  the gtest framework header (`gtest-param-util.h`) as often as in real source.
- **Test frameworks swallow the report.** cmocka installs a SIGSEGV handler that
  fires before ASan, so a crash surfaces as a plain test failure with no trace.
- **Even with a perfect trace, a 7B model didn't fix it** (0/10). Localization was
  never the bottleneck — the benchmark already masks the exact buggy line — synthesis
  is. Valgrind (no rebuild, survives crashes, `N bytes after a M-byte block`) is a
  better fit for the crash cases than ASan, but it's 10–50× slower.

---

## 5. Diagnosis quality dominates, and it's fragile

The recurring failure mode was the model receiving *noise* — build output, a test
roster, a startup banner, or a fabricated "assertion" — instead of the real failure.
This came from a chain of small pitfalls:

- Greedy classifiers (see §1) mislabeling build noise as timeout / compile / assertion.
- A tool's *fallback* (log excerpt) firing on a build-only log and inventing a
  "Failing test assertion" out of `[170/694] Building CXX object …`.
- Extracting a location but not the *code* at it — the biggest quality lever was
  reading the **test source at the failing line** (`EXPECT_EQ(...)`,
  `ASSERT(function->hasBody())`, the `strcasecmp(name, current_element->string)` at a
  UB fault) so the model can link the failure to the fix.
- After correcting the extractors, useful-diagnosis coverage roughly doubled — a
  reminder that *the data was usually already on disk*; it just needed correct parsing.

---

## 6. Model behavior specific to C/C++ infill repair

- **Buggy-line echoing.** Because the prompt displays the removed buggy line as a
  hint, a weak model frequently copies it back verbatim (~15–24% for CodeLlama,
  ~7% for DeepSeek) — reproducing the bug and guaranteeing failure. Requires an
  explicit "your fix must differ from the buggy line" instruction and a pre-build
  echo guard.
- **Over-editing.** Asked for one line, the model sometimes rewrites the whole
  function and breaks it (a mangled `assstatic const char*` token, `case`/`break`
  outside a switch). Needs an explicit "change only the infill location, minimally"
  constraint.
- **Missing symbol context.** The prompt shows only the buggy function, so the model
  references undeclared identifiers/members (the single largest compile-error class).
  It helps to inject the file's relevant declarations (callee signatures, structs,
  macros) — but for multi-file projects the needed symbols live in headers we don't
  show, so it only partly helps.
- **Degenerate outputs.** At temperature, models emit empty/prose responses that the
  patch endpoint rejects (HTTP 400) — must be handled as an ordinary failed attempt,
  not a run-crashing error.
- **Context overflow.** A few functions (pcre2) exceed the model window; the request
  must be re-fit (shrink completion budget, then truncate) rather than error out.

---

## 7. Oracle and verification subtleties

- **`return_code` alone is a false-positive oracle.** The test script ends on a
  `cat` (exit 0), so a *test* failure never reaches the exit code — only a *build*
  failure gives rc=1. A real pass requires `return_code == 0 AND fix_status == success`.
- **Perfect fault localization is a double-edged sword.** The benchmark masks the
  exact line, so "where" is free — which means the entire difficulty is "what," and
  it also makes the buggy-line-echo trap easy to fall into.
- **Build-infra failures must not count against the model.** A patch that can't be
  built (missing dir, missing dep, ABI mismatch) is an environment failure, not a
  wrong fix; conflating them makes pass@k meaningless.

---

## 8. Serving and infrastructure

- **vLLM can wedge without crashing** — process alive, GPU at 0%, front-end
  unresponsive, connection backlog growing. Always serve with output to a log file;
  a foreground launch loses the only traceback.
- **One model per server.** Switching models (deepseek ↔ codellama ↔ qwen) requires
  a full server restart; `--model` only selects which id the client *requests*.
- **RAM, not GPU, was the first wall.** A 7B model *loads* through host RAM; on a
  15.7 GB box the load (not the download) OOM-panicked the machine until swap was
  added.
- **Fail-fast matters.** A dead endpoint returned per-request timeouts for 30 min
  each and silently marked ~14 bugs (and would have marked ~90 more) as errors,
  invalidating a run. The agent must detect a wedged endpoint and abort with results
  so far, rather than treat it as a per-bug failure.

---

## Cross-cutting takeaways

1. **Most of the difficulty is not the LLM.** Building the code, capturing the right
   failure signal, and not corrupting the environment dwarfed prompt/model work.
2. **Heterogeneity is the tax.** ~30 projects means ~8 test frameworks, ~as many
   build systems, and a long tail of per-project dependencies and quirks. There is
   no single code path that "just works."
3. **Honest measurement is hard and easy to get wrong.** False-positive oracles,
   misclassified infra failures, destructive diagnosis steps, and buggy-line echoes
   all quietly distort the solve rate in *both* directions.
4. **The useful signal is usually already there** — in the JUnit XML, the test
   source, the `.msg`, the allocation context — and the work is extracting it
   faithfully for a machine reader, per framework, without letting build noise leak in.
