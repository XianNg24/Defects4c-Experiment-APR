# Why the models' patches fail to compile — a root-cause analysis

An analysis of **4,837 compile-failing candidate patches** collected across nine k=10
runs (deepseek / CodeLlama / Qwen × baseline / augmented / oracle) on the Defects4C
benchmark. It answers a question the aggregate solve-rate hides: when a 7B model's patch
doesn't build — which is **~40% of single-shot responses and ~51% of candidates at
temperature 0.8** — *why*?

## TL;DR

**Compile failures are not, mostly, the model inventing wrong APIs. They are the model
breaking the code it was told to leave alone.** Nearly two-thirds of compile failures are
the model corrupting the surrounding function's *structure* — mismatched braces, fused
tokens, re-emitted signatures, mis-scoped declarations — while trying to fill a
single masked line. The task is infill; the model keeps rewriting the frame.

| Root cause | Share | One-line description |
|---|---|---|
| **A. Syntax / broken structure** | **62.9%** | broke braces/scope; emitted malformed or fused code |
| **B. Symbol not in scope** | **26.2%** | referenced a variable/function/type not visible at the infill point |
| C. Wrong type / signature | 3.6% | wrong field, wrong arg count, incompatible types |
| (uncategorized / no diagnostic) | 7.3% | mostly GCC-dialect syntax errors of type A |

---

## Method

- **Corpus:** every candidate across the nine k=10 runs whose verdict log shows a genuine
  build failure — `ninja: build stopped`, `N errors generated`, `make: *** … Error`,
  `collect2: error`, or a linker failure. Test-framework output (cmocka's `error:
  Failure!`, ctest's `[ FAILED ]`) is explicitly excluded, so this measures *build*
  failure, not *test* failure.
- **Cause:** the **first** compiler `error:` diagnostic on each failing candidate,
  bucketed by pattern. Both clang (`use of undeclared identifier`) and GCC
  (`expected ';' before 'int'`) dialects are handled.
- **Scale:** 4,837 compile-failing candidates.

A note on the split with *test* failures: of all failing candidates, roughly half fail to
compile (this report) and half compile but fail the test (wrong logic). This report is
only about the compile half.

---

## A. Syntax / broken structure — 62.9% (the dominant cause)

The model does not respect the infill boundary. Asked to replace one masked line inside a
function, it re-emits surrounding code and, in doing so, unbalances braces, fuses tokens,
or drops anchors the rest of the function depends on. The compiler then reports the
damage from wherever the structure first became inconsistent — often far from the intended
edit.

Representative diagnostics, each a distinct structural failure mode:

| Diagnostic | What the model actually did |
|---|---|
| `namespaces can only be defined in global or namespace scope { … }` | closed too few braces, so following top-level code landed *inside* a function |
| `function definition is not allowed here` / `invalid storage class for function 'print_asc'` | closed too *many* braces, so a later function nested inside this one |
| `expected ';' before 'int'  →  _gdNGetstatic int` | the emitted line ran straight into the next line — two tokens fused |
| `qualified-id in declaration before '(' token  →  Optimizer& Optimizer::…` | re-emitted a *method signature* in the middle of a function body |
| `member initializer '…' does not name a non-static data member` | dropped a constructor-initializer fragment where it doesn't belong |
| `'else' without a previous 'if'` | moved or deleted the matching `if` |
| `label 'trunc' used but not defined` | deleted tcpdump's `trunc:` goto target while rewriting around it |
| `while loop outside of a function` | a brace mismatch ejected a loop out of its function |
| `'case' statement not in switch` / `duplicate case value` | corrupted a `switch` while editing one arm |

**Why it happens.** Three compounding reasons:

1. **The infill task fights the model's instinct to complete.** A code LLM is trained to
   *continue* code. Given a function with a hole, it tends to regenerate a plausible span
   around the hole rather than emit exactly the missing line. Every extra line it writes
   is another chance to misplace a brace.
2. **Brace/scope bookkeeping is fragile at 7B.** Keeping a running brace-depth count over
   a long C++ function is exactly the kind of precise, long-range state-tracking small
   models are weakest at. One dropped `}` invalidates everything after it.
3. **Temperature amplifies it.** Structural corruption rises with sampling temperature
   (the compile-fail rate goes 39% → 51% from temp 0.01 → 0.8). Diversity that helps
   *coverage* also produces more malformed frames.

**The over-generation signature is unambiguous.** For structural failures the patch adds
a **median of 8 lines** where the correct fix is a single masked line — **100% of them
emit more than one line.** The failure is quite literally that the model wrote too much.

---

## B. Symbol not in scope — 26.2%

The second-largest cause is the one usually assumed to be first: the model writes
*plausible* code that references a name not visible at the edit site.

| Diagnostic | Cause |
|---|---|
| `use of undeclared identifier 'local_moveto_mod'` | invented / assumed a local that was never declared |
| `use of undeclared identifier 'ctx'` | used `ctx` where it isn't a parameter of *this* function |
| `call to undeclared function 'lyd_error_format'` | called a helper not declared in this translation unit |
| `no member named 'first' in 'struct ly_set'` | assumed a struct field that doesn't exist |
| `must use 'struct' tag to refer to type 'ly_err_item'` | C-vs-C++ tag rule — wrote `ly_err_item` where C needs `struct ly_err_item` |

**Why it happens.** The base prompt shows only the buggy *function*. The model cannot see
the surrounding declarations — sibling helpers, struct layouts, which variables are in
scope — so it reasons about the fix from an incomplete picture and guesses names. This is
precisely the gap the `symbol_digest` augmentation targets, and it is genuinely a
*knowledge/visibility* failure rather than a synthesis one. (These patches also
over-generate — median 5 added lines — so cause A and B are correlated: the more the
model rewrites, the more out-of-scope names it reaches for.)

---

## C. Wrong type / signature — 3.6% (small)

Genuine type errors are rare: wrong argument count to a macro
(`too few arguments provided to function-like macro invocation LOGERR`), incompatible
assignments, no matching overload. When the model gets the structure and the names right,
it usually gets the types right too. Type confusion is **not** a meaningful driver of
compile failure here.

---

## Per-model pattern

The cause mix is remarkably consistent across the three model families — this is a
7B-class phenomenon, not a quirk of one model:

| cause | deepseek | CodeLlama | Qwen |
|---|---|---|---|
| structural / broke scope | 29% | 28% | **38%** |
| symbol not in scope | 28% | 27% | 25% |
| syntax (clang-dialect) | 9% | 10% | 6% |
| other (GCC-dialect syntax) | 28% | 30% | 27% |

The one difference worth noting: **Qwen breaks structure more often** (38% vs ~29%). This
lines up with Qwen being the weakest baseline model and the one our repair guidance helped
most (baseline 21 → augmented 33) — a large part of what the guidance ("edit only the
infill location, keep it minimal, single line") buys is fewer structural blow-ups.

---

## What this means

**1. "Hallucination" is the wrong mental model.** Only ~26% of compile failures are the
model reaching for symbols that don't exist. The dominant failure (63%) is *mechanical*:
the model damages the surrounding code while over-writing. It is less "the model doesn't
know C++" and more "the model won't stay inside the lines."

**2. This is the most *fixable* failure class we have.** Unlike the synthesis ceiling
(bugs unsolved even when handed the fix), structural corruption is a discipline problem
with concrete levers:
- **Constrain the edit surface.** The `_SINGLE_LINE_GUIDANCE` / `_CONDITION_GUIDANCE`
  prompt work already pushes single-line, in-place edits; the per-model data (Qwen)
  suggests it measurably reduces structural failures. Strengthening it — or enforcing a
  true single-line replacement mechanically rather than by instruction — would directly
  reclaim a large share of the 63%.
- **Emit a diff/line, not a function.** Prompt variants that ask for the whole fixed
  function invite over-generation; asking for only the replacement line removes most of
  the brace-bookkeeping surface.
- **Cheap post-generation repair.** A brace-balance / bracket-match check on the candidate
  before spending a build would catch a meaningful fraction pre-compile.

**3. It reframes the fine-tuning case.** A fine-tune on real single-function fix commits
(Defects4C's `bgcommit` corpus) would most plausibly help *here* — teaching the model to
emit minimal, in-place, well-formed edits. That targets the 63% structural bucket, which
is a format/discipline problem fine-tuning is well-suited to, rather than the synthesis
ceiling, which it is not.

**4. Sampling works partly by dodging this.** Because ~half of all candidates die at
compile time — mostly from structural noise — pass@k's real job is to keep drawing until a
structurally-intact candidate appears. Cutting the structural failure rate would raise the
yield of every sample and make each additional k worth more.

---

## Bottom line

When a 7B model fails to repair a Defects4C bug, about half the time it never even builds —
and when it doesn't build, **the model has almost always broken the function's structure
rather than misunderstood its logic.** The single most valuable reliability improvement
available is not better diagnosis or a bigger model; it is making the model **edit in
place and stop generating** — mechanically if instruction alone won't hold it.
