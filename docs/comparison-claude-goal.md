# GoaLoop vs. Claude Code `/goal` — competitive analysis

Both tools answer the same question: *how do you let an LLM agent keep
working toward a target across many turns/attempts until it's actually
done?* Claude Code's `/goal` command (shipped in v2.1.139, May 2026) is
an **in-process, continuation-based** loop gated by a fresh-model
**transcript-only** evaluator; GoaLoop is an **out-of-process,
fresh-attempt** loop gated by a fresh-process **executable** verification.

This document compares the two — what each is, how they're built, and
where they meaningfully differ — with verification as the centerpiece,
since that's where the designs diverge most.

> Reference points: Claude Code's feature is documented at
> [code.claude.com/docs/en/goal](https://code.claude.com/docs/en/goal);
> it is implemented as a session-scoped, prompt-based **Stop hook**.
> GoaLoop is the Python package + skills + `agents/goal-runner.md` in
> this repo; see [`design.md`](design.md). For the sibling comparison
> against OpenAI Codex's `goal`, see
> [`comparison-codex.md`](comparison-codex.md).

## TL;DR

| | **Claude Code `/goal`** | **GoaLoop** |
|---|---|---|
| Form factor | Built into Claude Code; a session-scoped wrapper over a prompt-based Stop hook | Out-of-process Python orchestrator + `claude -p` subprocesses |
| "Loop" mechanism | Same session auto-continues a new **turn** when the prior one finishes (shared context) | Each **attempt** is a brand-new `claude -p` process (no shared context) |
| Goal expression | One free-text `condition` (≤ 4000 chars), optional "stop after N turns" prose clause | Structured `goal.md`: objective + hard constraints + an **executable Verification procedure** |
| State store | In-session / transcript + counters (turns, tokens, time) | Plain files on disk (`goal.md`, `memory/`, `attempts/`, `.goaloop/`) |
| Who verifies | A **different, fresh model** (default Haiku) — but it reads **only the conversation transcript** | A **fresh `claude -p` process** that **independently runs the check** against real workspace state |
| What it judges on | Whatever the worker *surfaced in the conversation* (LLM yes/no + reason) | Command exit code / parsed metric / rubric score — deterministic pass/fail |
| Anti-cheat strategy | Judge model ≠ worker model, but judge sees only the worker's self-report and **can't run tools** | Structural: judge is a different, memory-less process that re-runs the check on disk truth |
| Stop conditions | Evaluator says "yes"; `/goal clear`; Ctrl+C (`-p`); `/clear` | `pass` (verified), `blocked` (Runner), `error` (bounded retries), human `stop` |
| Budget enforcement | Tracks turns/tokens/time; "stop after N turns" is a soft prose clause judged from the transcript | None enforced; reads each attempt's reported cost only |
| Host integration | Built in; zero setup; needs the workspace trusted and hooks enabled | Sits on top of stock Claude Code; CLI + skills, no host changes |
| Survives session close | No — bound to the session (`--resume` restores the goal but resets counters) | Yes — the orchestrator is detached and keeps iterating |
| Long jobs | Run to completion within a turn; evaluator fires only after the turn ends | `in_progress` pause + `--resume` the same session; the wait burns no tokens |

One line: **Claude `/goal` keeps one agent working in a continuous
session, gated by a cheap fresh model that reads the agent's own
transcript; GoaLoop spawns a disposable fresh worker per attempt, gated
by an independent process that re-runs the check against real state.**

## 1. What each one is

**Claude Code `/goal`.** You type `/goal <condition>` and Claude starts a
turn immediately with the condition as its directive. After each turn, a
**small fast model** (the [configured](https://code.claude.com/docs/en/model-config)
small/fast model, default Haiku) receives the condition plus the
conversation so far and returns a yes/no decision with a short reason. A
"no" tells Claude to keep working (the reason becomes guidance for the
next turn); a "yes" clears the goal and records an achieved entry. One
goal per session; `/goal` with no argument shows status (turns, tokens,
time, last reason); `/goal clear` cancels. It runs in interactive mode,
in `-p`, in the desktop app, and through Remote Control.

**GoaLoop.** A user writes a `goal.md` (via the `/goal-init` interview or
`/goal-flash` one-shot) that spells out the objective, hard constraints,
and — crucially — a concrete **Verification** procedure. A detached
Python orchestrator then spawns one fresh `claude -p` **Runner** per
attempt: it reads the workspace, runs the Verification, does one unit of
work if it failed, records `attempts/NNN.md`, and ends with a JSON
terminator (`pass`/`advanced`/`in_progress`/`blocked`). The orchestrator
loops until `pass` or a stop.

## 2. Architecture

**Claude `/goal` — in-process continuation, hook-gated.** The official
docs are explicit: *"`/goal` is a wrapper around a session-scoped
prompt-based Stop hook."* Each time Claude finishes a turn, the Stop hook
sends the condition + conversation to the small fast model, which answers
yes/no. "No" re-starts a turn in the **same session**, carrying full
context forward; "yes" clears the goal. The loop is therefore the same
session spawning new turns — the working context is never thrown away.

**GoaLoop — out-of-process orchestration.** A small deterministic Python
process (not an LLM) runs a `while` loop, spawning a fresh `claude -p`
Runner per attempt with `agents/goal-runner.md` as the system prompt. The
Runner has no memory of prior attempts except via workspace files. It
emits a JSON terminator; the orchestrator branches on it. All
authoritative state is on disk; the orchestrator is detached
(`start_new_session=True`) so it survives the Manager session closing.

The deep difference: **Claude `/goal`'s loop shares one context across
turns; GoaLoop's loop deliberately throws context away each attempt** —
and Claude's loop dies with the session while GoaLoop's outlives it.

A historical note worth flagging: a prompt-based Stop-hook evaluator is
*exactly* the design GoaLoop considered and deferred. [`design.md`](design.md)
lists Stop/SessionStart hooks under "Explicitly Excluded" and names an
"optional Stop-hook-based independent evaluator" as possible future work.
Claude `/goal` is essentially that idea shipped — but with a
**transcript-only** judge rather than GoaLoop's independent-execution
judge (see §4).

## 3. Data model

**Claude `/goal`** — no durable schema. The goal is session state: the
condition string (≤ 4000 chars) plus live counters (turns evaluated,
tokens spent, elapsed time) and the evaluator's last reason. A goal still
active when a session ends is restored on `--resume`/`--continue`, but the
turn count, timer, and token baseline all reset; an already-achieved or
cleared goal is not restored. There is no on-disk audit trail of what each
turn did beyond the normal session transcript.

**GoaLoop** — files on disk, no schema either, but durable and inspectable:

```
<workspace>/
├── goal.md            # objective + hard constraints + Verification procedure
├── config.yaml        # optional: model / interval / mode
├── suggestions.md     # optional: async per-attempt notes
├── memory/learnings.md  # ~4KB curated cross-attempt knowledge
├── attempts/NNN.md    # write-once audit trail
└── .goaloop/          # state.json, status.txt, logs (orchestrator bookkeeping)
```

The verification procedure is a *first-class, mandatory part of the spec*,
not an emergent behavior of a prompt; and `attempts/NNN.md` is a
write-once ledger that survives crashes and session closes.

## 4. Verification — the central difference

This is where the two designs are most opposed. Three axes.

### a) What verification *is* — transcript-judged condition vs. objective executable

**Claude `/goal` judges a free-text condition against the transcript.**
The condition is natural language; the evaluator is an LLM. The docs are
explicit about its reach: *"It doesn't run commands or read files
independently, so write the condition as something Claude's own output can
demonstrate."* "All tests in `test/auth` pass" works **only because**
Claude itself runs the tests and the result lands in the transcript for
the evaluator to read. The check is not executed by the judge — it is
*read* by the judge from what the worker chose to surface.

**GoaLoop makes verification a literal, executable procedure, mandatory at
init.** `/goal-init` is a hard gate:

> Acceptable: "run `./scripts/foo.sh` and check exit code", "parse
> `metrics.p99` from result.json and compare against 5.0", "score
> `draft.md` against `rubric.md` and check ≥ 8.0".
> **Unacceptable: "the agent looks at it and decides".**

If you can't state a concrete check, it refuses to write `goal.md`. The
judge process **runs that check itself** and produces a deterministic
two-state pass/fail — it does not depend on what a prior process claimed.

### b) Who verifies, and on what input — fresh model, shared transcript vs. fresh process, disk truth

|  | Claude `/goal` | GoaLoop |
|---|---|---|
| Verifier | a **different, fresh model** (default Haiku), not the working model | a **fresh `claude -p` process** (Runner N judges Runner N−1's output) |
| Timing | end of each turn, when deciding whether to continue | **start** of the attempt, before doing any work |
| Inputs | the condition + **the conversation transcript** | the condition + **disk files / live workspace state** — runs the check itself |
| Tools | **none** — cannot run commands or read files | full tools — runs benchmarks, greps, executes the verification script |
| Output | LLM yes/no + reason | deterministic pass/fail from actual exit code / parsed value |

This places Claude `/goal` **between** Codex and GoaLoop on the
independence axis. Codex is *judge = player* (the same agent self-audits).
Claude `/goal` separates the **judge model** from the worker — a genuine
improvement — but the judge's only window into reality is **what the worker
wrote into the conversation**. GoaLoop severs the link entirely: the judge
is a different process that **looks at the workspace and re-runs the
check**, with no dependence on the worker's narrative.

### c) Anti-cheat — separated-but-trusting vs. structurally severed

The failure mode each design is exposed to is different:

- **Codex** can rationalize "I think it's done" because the judge *is* the
  worker. It counters with prompt rhetoric ("treat completion as
  unproven").
- **Claude `/goal`** closes that gap — a fresh model decides — but opens a
  narrower one: the judge believes the transcript. A worker that *says*
  "all tests pass" without actually running them, or that runs a narrower
  test than the condition implies, can satisfy a transcript-reading judge.
  The judge has no tools to catch it. (In practice the same agent usually
  does run the tests, so this is a soft gap, not a guarantee.)
- **GoaLoop** leaves the cheat no structural place to happen: the judging
  process re-runs the check against disk truth, so a false "tests pass"
  claim simply fails the next attempt's Verification. The defense is
  structural, not rhetorical.

**Net:** Claude `/goal` is a meaningful step up from self-audit — a
cheaper, fresh-model gate that fires every turn — but its verdict is only
as truthful as the worker's transcript. GoaLoop pays more (a full
`claude -p` process re-runs the check) to buy a verdict that doesn't
depend on the worker being honest about what it did.

## 5. Trade-offs — when each wins

**Claude `/goal` is better when:**

- You want **zero setup** — it's built into Claude Code, no separate
  process, no `goal.md` to author, just `/goal <condition>`.
- **Context continuity matters** — carrying the full working context
  across turns avoids re-deriving state each iteration (GoaLoop
  re-establishes context from files every attempt).
- The objective is **moderately fuzzy** — a transcript-judging LLM can
  make headway on conditions that don't reduce to a single command, as
  long as the worker surfaces the relevant evidence.
- You want the cheapest possible per-turn gate — evaluation runs on the
  small fast model and is typically negligible vs. main-turn spend.
- You're working **interactively in one sitting** and don't need the loop
  to outlive the session.

**GoaLoop is better when:**

- "Done" *can* be reduced to a check (perf thresholds, test green-counts,
  rubric scores) and you want that check **executed by the judge**, not
  merely read from the worker's self-report.
- You want an **impartial referee that sees disk truth** — the
  fresh-process design judges actual state, immune to a worker that
  misreports what it ran.
- You need the loop to **survive the session closing** — the detached
  orchestrator keeps iterating for hours/days regardless of whether Claude
  Code stays open.
- You want a **durable, inspectable audit trail** (`attempts/NNN.md`) and
  **crash/quota recovery**, and tolerate the multi-second per-attempt floor
  of spawning `claude -p`.

The cost mirror is clean: Claude `/goal` buys zero-infra, continuous
context, a cheap per-turn gate, and tolerance of fuzzier goals; GoaLoop
buys execution-level verification rigor (a judge that re-runs the check on
real state), session-independent durability, and a recoverable audit
trail — at the price of authoring a concrete check up front and throwing
context away each attempt.

## Appendix: source pointers

**Claude Code `/goal`:**
- [Keep Claude working toward a goal](https://code.claude.com/docs/en/goal) — official docs (loop, evaluator, status, `-p`, requirements)
- [Prompt-based Stop hooks](https://code.claude.com/docs/en/hooks-guide#prompt-based-hooks) — the mechanism `/goal` wraps
- [small fast model config](https://code.claude.com/docs/en/model-config) — the evaluator model (default Haiku)

**GoaLoop** (this repo):
- [`design.md`](design.md) — full design and rationale
- [`comparison-codex.md`](comparison-codex.md) — the sibling comparison against OpenAI Codex's `goal`
- `agents/goal-runner.md` — the Runner system prompt (verification procedure)
- `skills/goal-init/SKILL.md` — the strict verification-rigor gate
- `goaloop/orchestrator.py` — the attempt loop and terminator handling
