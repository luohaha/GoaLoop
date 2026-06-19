# GoaLoop vs. Codex `goal` — competitive analysis

Both tools answer the same question: *how do you let an LLM agent keep
working toward a target across many turns/attempts until it's actually
done?* They reach almost opposite answers. Codex's `goal` feature is an
**in-process, continuation-based** loop with self-audited completion;
GoaLoop is an **out-of-process, fresh-attempt** loop with an externally
verified gate.

This document compares the two — what each is, how they're built, and
where they meaningfully differ — with verification as the centerpiece,
since that's where the designs diverge most.

> Reference points: Codex's feature lives in `codex-rs/ext/goal/`
> (Rust, compiled into the agent) plus `codex-rs/state/...goals.rs`
> (SQLite) and `codex-rs/prompts/.../goals/*.md` (steering prompts).
> GoaLoop is the Python package + skills + `agents/goal-runner.md` in
> this repo; see [`design.md`](design.md).

## TL;DR

| | **Codex `goal`** | **GoaLoop** |
|---|---|---|
| Form factor | In-process Rust extension, compiled into the agent | Out-of-process Python orchestrator + `claude -p` subprocesses |
| "Loop" mechanism | Same session auto-continues a new **turn** when idle (shared context) | Each **attempt** is a brand-new `claude -p` process (no shared context) |
| Goal expression | One free-text `objective` + optional token budget | Structured `goal.md`: objective + hard constraints + an **executable Verification procedure** |
| State store | SQLite (`thread_goals` table, one goal per thread) | Plain files on disk (`goal.md`, `memory/`, `attempts/`, `.goaloop/`) |
| Verification | **Self-audit** — same agent that did the work judges completion via prompt | **External gate** — a fresh process runs a literal check; deterministic pass/fail |
| Anti-cheat strategy | Prompt rhetoric ("treat completion as unproven…") | Structural: judge is a different, memory-less process using an external criterion |
| Stop conditions | `complete`/`blocked` (model-decided), `budget_limited` (auto), user pause | `pass` (verified), `blocked` (Runner), `error` (bounded retries), human `stop` |
| Budget enforcement | Hard token budget → auto `budget_limited` status | None enforced; reads each attempt's reported cost only |
| Host integration | Built into Codex itself; requires changing Rust | Sits on top of stock Claude Code; skills + hooks + Python, no host changes |
| Long jobs | Continue within turns | `in_progress`/`ScheduleWakeup` pause, then `--resume` the same session |

One line: **Codex makes the agent its own tireless project manager;
GoaLoop makes the system an impartial referee over disposable workers.**

## 1. What each one is

**Codex `goal`.** A user (or developer instruction) calls a `create_goal`
tool with an `objective` and an optional `token_budget`. From then on,
whenever the thread goes idle, Codex *automatically starts a new turn* to
keep pursuing the goal — no human nudge required. The agent decides it's
done by calling `update_goal(status="complete")`, or gives up with
`update_goal(status="blocked")`. Three tools total: `create_goal`,
`get_goal`, `update_goal`.

**GoaLoop.** A user writes a `goal.md` (via the `/goal-init` interview or
`/goal-flash` one-shot) that spells out the objective, hard constraints,
and — crucially — a concrete **Verification** procedure. A detached
Python orchestrator then spawns one fresh `claude -p` **Runner** per
attempt: it reads the workspace, runs the Verification, does one unit of
work if it failed, records `attempts/NNN.md`, and ends with a JSON
terminator (`pass`/`advanced`/`in_progress`/`blocked`). The orchestrator
loops until `pass` or a stop.

## 2. Architecture

**Codex — in-process continuation.** `GoalExtension` implements a set of
lifecycle traits (`ThreadLifecycleContributor`, `TurnLifecycleContributor`,
`TokenUsageContributor`, `ToolLifecycleContributor`). Three coordinated
mechanisms make the loop:

1. **Continuation** — `on_thread_idle` triggers `continue_if_idle()`,
   which reads the active goal, injects a continuation steering prompt,
   and calls `try_start_turn_if_idle()`. This *is* the loop: the same
   session keeps spawning new turns, carrying full context forward.
2. **Accounting** — `on_token_usage` accumulates tokens and wall-clock
   per turn into SQLite. When `tokens_used >= token_budget`, a SQL rule
   flips status to `budget_limited`.
3. **Steering** — three template prompts are injected as hidden context:
   `continuation.md`, `budget_limit.md`, `objective_updated.md`.

**GoaLoop — out-of-process orchestration.** A small deterministic Python
process (not an LLM) runs a `while` loop, spawning a fresh `claude -p`
Runner per attempt with `agents/goal-runner.md` as the system prompt. The
Runner has no memory of prior attempts except via workspace files. It
emits a JSON terminator; the orchestrator branches on it. All authoritative
state is on disk; the orchestrator is detached (`start_new_session=True`)
so it survives the Manager session closing.

The deep difference: **Codex's loop shares one context across iterations;
GoaLoop's loop deliberately throws context away each iteration.**

## 3. Data model

**Codex** — `ThreadGoal` in SQLite (`thread_goals`, one row per thread):

```rust
pub struct ThreadGoal {
    thread_id, goal_id, objective,
    status,              // Active|Paused|Blocked|UsageLimited|BudgetLimited|Complete
    token_budget: Option<i64>,
    tokens_used: i64, time_used_seconds: i64,
    created_at, updated_at,
}
```

The `objective` is free text; there is no field for "how to verify". The
status enum carries the lifecycle, and `token_budget`/`tokens_used` are
the only hard, machine-checked constraint.

**GoaLoop** — files on disk, no schema:

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
not an emergent behavior of the prompt.

## 4. Verification — the central difference

This is where the two designs are most opposed. Three axes:

### a) What verification *is* — subjective self-audit vs. objective executable

**Codex has no programmatic verification.** Its `update_goal` handler only
validates that the status is `complete` or `blocked`, does token
accounting, and writes the DB row. It does **not** check whether the
objective was actually achieved — if the model says complete, it's
complete. The `objective` is free text with no command, no parse rule, no
pass/fail convention. "Verification" is entirely the natural-language
**"Completion audit"** section of `continuation.md`, an instruction to the
model to be honest with itself.

**GoaLoop makes verification a literal, executable procedure, mandatory at
init.** `/goal-init` is a hard gate:

> Acceptable: "run `./scripts/foo.sh` and check exit code", "parse
> `metrics.p99` from result.json and compare against 5.0", "score
> `draft.md` against `rubric.md` and check ≥ 8.0".
> **Unacceptable: "the agent looks at it and decides".**

If you can't state a concrete check, it refuses to write `goal.md`. The
result is a deterministic two-state pass/fail (`goal-runner.md §2`: "the
**only authoritative check**, not your sense that it 'looks done'").

### b) Who verifies, and when — same context vs. fresh process

|  | Codex | GoaLoop |
|---|---|---|
| Verifier | **the same agent that did the work** (same session, full context) | **a fresh `claude -p` process** (Runner N judges Runner N−1's output) |
| Timing | end of turn, when deciding to mark complete | **start** of the attempt, before doing any work |
| Inputs | current worktree + remembered conversation | disk files only — **no prior-attempt context** |
| Output | model's internal judgment → `update_goal` | runs the external procedure → deterministic pass/fail |

Codex is **judge = player**: the context that wrote the code, holding the
memory of "I just did this", decides whether it's good enough — inherently
prone to self-justification. GoaLoop is **judge ≠ player, physically
isolated**: the process judging the state is a clean one with zero
emotional investment in the prior change and no ability to rationalize "I
think it should work now".

### c) Anti-cheat — rhetoric vs. structure

**Codex leans on prompt rhetoric** to counteract the weakness of
self-audit (`continuation.md`, written very forcefully):

- "treat completion as **unproven**"
- "The audit must **prove** completion, not merely fail to find obvious
  remaining work"
- "Do not rely on **intent, partial progress, memory of earlier work, or a
  plausible final answer**" (explicitly disqualifying memory — precisely
  because it's the same context)
- "Do not mark complete merely because the budget is nearly exhausted"
- `blocked` requires the same blocker for **3 consecutive turns** — but the
  model self-counts; there's no programmatic counter.

**GoaLoop leans on structure**, so it needs far less rhetoric:

- The verification procedure is external and fixed in `goal.md`; the model
  can't redefine "success" into something easier because the judgment
  doesn't pass through its subjectivity.
- The judge is a different, memory-less process — the self-justification
  path is physically severed.
- "Run any long-running step **to completion** before judging" — no
  faking it via timeout; long jobs are spanned by pause/resume.
- Even LLM-as-judge is structured: the Runner reads a **fixed rubric** +
  artifact, scores each dimension, writes the verdict where Verification
  says — still a clean process, with a rubric that can't be loosened
  mid-attempt.

**Net:** Codex treats verification as a discipline the agent imposes on
itself, backed by a strongly worded prompt but ultimately self-reported
and trust-based. GoaLoop treats verification as an objective gate the
system enforces — the goal must carry an executable check, and the judge
is a memory-less new process scoring the old process's output. GoaLoop
doesn't rely on "please don't cheat" rhetoric so much as leave cheating
**no structural place to happen**.

## 5. Trade-offs — when each wins

**Codex `goal` is better when:**

- The objective is fuzzy or hard to reduce to a command — self-audit can
  still make headway where no executable check exists.
- You want zero setup and tight integration — it's built into the agent,
  no separate process, no `goal.md` to author.
- Context continuity matters — carrying the full working context across
  turns avoids re-deriving state each iteration.
- You want a hard token budget enforced automatically.

**GoaLoop is better when:**

- "Done" *can* be reduced to a check (perf thresholds, test green-counts,
  rubric scores) and you want that check to be load-bearing and
  cheat-resistant.
- You value an impartial referee — the fresh-process design gives
  arm's-length judging without nested evaluator infrastructure.
- You can't or won't modify the host agent — GoaLoop rides on stock
  Claude Code.
- You want a durable, inspectable audit trail on disk and crash recovery,
  and tolerate the multi-second per-attempt floor of spawning `claude -p`.

The cost mirror is clean: Codex buys zero-infra, continuous context, and
tolerance of vague goals; GoaLoop buys verification rigor (structural
defense against self-deception), host non-intrusion, and recoverability —
at the price of forcing you to express "what success looks like" as a
concrete check up front.

## Appendix: source pointers

**Codex** (`~/codex/codex-rs/`):
- `ext/goal/src/spec.rs` — the three tool definitions (`create_goal`/`get_goal`/`update_goal`)
- `ext/goal/src/tool.rs` — tool handlers (note `handle_update` validates status only)
- `ext/goal/src/runtime.rs` — `continue_if_idle()` continuation loop
- `ext/goal/src/extension.rs` — lifecycle trait wiring
- `state/src/model/thread_goal.rs` — `ThreadGoal` struct + status enum
- `state/src/runtime/goals.rs` — SQLite persistence + budget auto-limit
- `prompts/templates/goals/{continuation,budget_limit,objective_updated}.md` — steering prompts

**GoaLoop** (this repo):
- [`design.md`](design.md) — full design and rationale
- `agents/goal-runner.md` — the Runner system prompt (verification procedure)
- `skills/goal-init/SKILL.md` — the strict verification-rigor gate
- `goaloop/orchestrator.py` — the attempt loop and terminator handling
- `goaloop/adapter.py` — the `claude -p` subprocess adapter
