# GoaLoop vs. pi `goal` extensions — competitive analysis

Both tools answer the same question: *how do you let an LLM agent keep
working toward a target across many turns/attempts until it's actually
done?* The interesting comparison here is with **[pi](https://github.com/earendil-works/pi)**,
a minimal, self-extensible coding agent, where "goal" is not native but
added by community **extensions** — and those extensions span almost the
entire design spectrum, from a faithful Codex clone to the most
GoaLoop-like competitor seen so far.

This document focuses on **`pi-goal-x`**, the strongest of the variants:
it ships an **independent completion auditor** — a separate pi agent that
inspects the real workspace with tools. That makes it the closest sibling
to GoaLoop's externally-verified gate, and the most instructive contrast.

> Reference points: pi is the agent toolkit at
> [github.com/earendil-works/pi](https://github.com/earendil-works/pi);
> goal is added by extensions, chiefly
> [`pi-goal-x`](https://pi.dev/packages/pi-goal-x) (independent auditor +
> verification contracts), with
> [`code-yeongyu/pi-goal`](https://github.com/code-yeongyu/pi-goal) and
> [`Michaelliv/pi-goal`](https://github.com/Michaelliv/pi-goal) as the
> Codex-style variants. GoaLoop is the Python package + skills +
> `agents/goal-runner.md` in this repo; see [`design.md`](design.md). See
> also the sibling comparisons against
> [Codex](comparison-codex.md) and [Claude Code `/goal`](comparison-claude-goal.md).

## A note on "pi's goal": there isn't one — there are several

`goal` is not a built-in pi feature; it's an extension, and at least three
exist with materially different verification stances:

| Extension | Verification stance | One line |
|---|---|---|
| `code-yeongyu/pi-goal` | judge = player | A faithful **Codex** port: three tools (`create_goal`/`update_goal`/`get_goal`), a hidden continuation prompt, token budget → `budgetLimited`. The agent self-completes via `update_goal({status:"complete"})`; **no independent auditor.** |
| `Michaelliv/pi-goal` | judge = player (evidence-disciplined) | Same-session self-audit, but the model is *"instructed to audit completion against real evidence before calling `update_goal`"* — still the same agent reading the transcript, **not an independent agent.** |
| **`pi-goal-x`** | **judge ≠ player (independent auditor)** | Verification contracts + an **independent completion auditor** (a separate pi agent that inspects the workspace with tools) + task lists/subtasks + multiple goals (`.pi/goals/` + `/goal-focus`). |

The first two are, in verification terms, "Codex on pi" — the existing
[`comparison-codex.md`](comparison-codex.md) covers that design. The rest
of this document is about **`pi-goal-x`**, because it's the one that
actually moves off the self-audit position.

## TL;DR (GoaLoop vs. `pi-goal-x`)

| | **pi-goal-x** | **GoaLoop** |
|---|---|---|
| Form factor | In-process extension to the pi agent | Out-of-process Python orchestrator + `claude -p` subprocesses |
| "Loop" mechanism | Same session auto-continues a new **turn** (shared context); empty-turn guard stops chat loops | Each **attempt** is a brand-new `claude -p` process (no shared context) |
| Goal expression | Free-text objective + optional **verification contracts** (plain-text requirements) + task lists/subtasks | Structured `goal.md`: objective + hard constraints + an **executable Verification procedure** |
| Independent verification | **Yes — but once, at claimed completion**: `complete_goal` spawns a separate pi agent that inspects the workspace | **Yes — every attempt**: a fresh Runner verifies before doing any work (Runner N judges Runner N−1) |
| Verifier's powers | A separate pi agent with **read-only tools** (`read`, `grep`, `find`, `ls`, `bash`); a **semantic** approve/reject | A fresh `claude -p` Runner that **runs the human-authored check**; deterministic pass/fail where possible |
| Executor context | Continuous — full context carried across turns | Disposable — context thrown away each attempt (anti-cheat by amnesia) |
| State store | `.pi/goals/` under the session; in-memory auditor session | Files on disk (`goal.md`, `memory/`, `attempts/`, `.goaloop/`) — durable audit trail |
| Budget enforcement | Token budget (default ~200k) → `budgetLimited` | None enforced; reads each attempt's reported cost only |
| Survives session close | No — in-process, tied to the session | Yes — the orchestrator is detached and keeps iterating |
| Verification optional? | Yes — contracts can be disabled (`disableContracts` / `PI_GOAL_DISABLE_CONTRACTS=1`) | No — `/goal-init` refuses to write `goal.md` without a concrete check |

One line: **`pi-goal-x` makes the independent auditor a final quality
inspector — invoked once when the agent claims done; GoaLoop makes the
same kind of independent verification the entrance gate of every attempt,
with a disposable executor and the whole loop running out-of-process where
it outlives the session.**

## 1. What each one is

**pi + `pi-goal-x`.** pi is a minimal, self-extensible coding agent (agent
loop + tools + TUI/CLI). `pi-goal-x` adds a `/goal` command and goal tools
so the same pi session keeps taking turns toward an objective. You can
attach **verification contracts** (plain-text requirements like "Run
`npm test`, zero failures") to a goal or task, and break work into task
lists with subtasks. When the agent calls `complete_goal`, the extension
spawns an **independent auditor** (a separate pi agent) to inspect the
workspace and approve or reject before the goal is archived. Multiple
goals live in `.pi/goals/`; `/goal-focus` switches the active one.

**GoaLoop.** A user writes a `goal.md` (via the `/goal-init` interview or
`/goal-flash` one-shot) that spells out the objective, hard constraints,
and — crucially — a concrete **Verification** procedure. A detached
Python orchestrator then spawns one fresh `claude -p` **Runner** per
attempt: it reads the workspace, runs the Verification, does one unit of
work if it failed, records `attempts/NNN.md`, and ends with a JSON
terminator (`pass`/`advanced`/`in_progress`/`blocked`). The orchestrator
loops until `pass` or a stop.

## 2. Architecture

**pi-goal-x — in-process continuation, episodic independent audit.** The
main loop is the same session auto-continuing turns with full context
carried forward (a hidden continuation prompt re-queues while the goal is
`active`; an empty-turn guard stops pure-chat loops). The independence is
*episodic*: only at the claimed-completion boundary does `complete_goal`
spawn a separate agent. From the docs: *"Before archiving the goal,
`complete_goal` starts a separate pi agent in an isolated in-memory
session."* That auditor inspects the workspace with read-only tools and
ends its report with `<approved/>` or `<disapproved/>`.

**GoaLoop — out-of-process orchestration, per-attempt independent verify.**
A deterministic Python process (not an LLM) runs a `while` loop, spawning a
fresh `claude -p` Runner per attempt. The Runner has no memory of prior
attempts except via workspace files, and it runs Verification **first
thing, every attempt**. All authoritative state is on disk; the
orchestrator is detached (`start_new_session=True`) so it survives the
session that launched it.

Two structural differences fall out of this:

- **Where independence lives.** pi-goal-x's executor is one continuous
  context; independence is bolted on as a final gate. GoaLoop's executor is
  a new process every attempt, so independence is intrinsic — the verifier
  *is* the next worker, judging the previous one's output with no shared
  memory.
- **What survives.** pi-goal-x is an in-process extension; its loop and its
  in-memory auditor die with the session. GoaLoop's loop is a detached
  process with an on-disk `attempts/NNN.md` ledger and crash/quota recovery.

## 3. Data model

**pi-goal-x** — goals persisted under `.pi/goals/` (multiple open goals,
one focused per session via `/goal-focus`), each carrying the objective,
optional verification contracts, and a task list with subtasks. The
auditor runs in an ephemeral in-memory session and leaves no durable record
of its own beyond the approve/reject outcome.

**GoaLoop** — files on disk, durable and inspectable:

```
<workspace>/
├── goal.md            # objective + hard constraints + Verification procedure
├── config.yaml        # optional: model / interval / mode
├── suggestions.md     # optional: async per-attempt notes
├── memory/learnings.md  # ~4KB curated cross-attempt knowledge
├── attempts/NNN.md    # write-once audit trail
└── .goaloop/          # state.json, status.txt, logs (orchestrator bookkeeping)
```

Both support decomposition, but differently: pi-goal-x has first-class
task lists/subtasks inside one goal; GoaLoop treats each attempt as the
unit and leaves sub-decomposition to the Runner's own working style.

## 4. Verification — the central difference

This is the closest any of the surveyed tools gets to GoaLoop, so the
divergence is narrower and more interesting. Three axes.

### a) When independent verification happens — once vs. every attempt

**pi-goal-x audits at the boundary.** The independent auditor fires when
the agent calls `complete_goal`. Throughout the run, the executor
self-audits in its own continuous context; the impartial check is the
*final* gate before archiving. A goal that the executor keeps believing is
"on track" is not independently re-checked until it claims done.

**GoaLoop verifies at the start of every attempt.** Verification runs
first-thing in each Runner, before any work. There is no "self-audit during
the loop, independent check at the end" split — *every* iteration is gated
by a fresh process. A dishonest or over-optimistic "I advanced" is caught
by the very next attempt's Verification, not deferred to a completion
boundary that might never arrive.

### b) What the verifier judges on — semantic LLM vs. executable check

|  | pi-goal-x auditor | GoaLoop Runner verify |
|---|---|---|
| Verifier | a separate pi agent (its own session) | a fresh `claude -p` process |
| Tools | **read-only**: `read`, `grep`, `find`, `ls`, `bash` | full tools — runs the verification script / benchmark / test |
| Criterion | objective + optional plain-text **contracts**; *"semantic, not a paperwork checklist"* | a human-authored **executable** procedure, fixed at init |
| Output | LLM judgment → `<approved/>` / `<disapproved/>` | deterministic pass/fail (exit code / parsed metric), LLM-as-judge only with a fixed rubric |
| Mandatory? | No — contracts can be disabled | Yes — no concrete check, no `goal.md` |

Both verifiers can *touch the real workspace* (this is the big step beyond
Claude `/goal`, whose evaluator reads only the transcript). The remaining
gap: pi-goal-x's auditor is fundamentally a **semantic LLM decision** over
what it observes, while GoaLoop's primary gate is a **deterministic check
the human wrote** and which the framework refuses to run without. GoaLoop
falls back to LLM-as-judge only for genuinely qualitative goals, and even
then against a fixed rubric — never as the default.

### c) Anti-cheat — final inspector vs. structural severance

- **pi-goal-x** severs judge from player *at completion*: the executor
  can't self-declare done past the auditor. But for the entire run up to
  that point, the continuous-context executor decides its own progress, and
  the auditor's verdict is a semantic LLM call that a determined executor
  could in principle satisfy with a plausible-looking workspace. Contracts
  are optional and can be turned off.
- **GoaLoop** severs judge from player *at every step*: each attempt is a
  memoryless process re-running a fixed, often deterministic check on disk
  truth. There is no continuous context to accumulate self-justification in,
  and the check isn't an LLM's opinion unless the goal is inherently
  qualitative. The verification is non-optional by construction.

**Net:** `pi-goal-x` is the first surveyed design to give the verifier real
tools and real workspace access — a genuine independent auditor, not a
transcript reader. GoaLoop pushes the same idea further on three fronts:
the independent check runs *every attempt* rather than once, the default
criterion is a *deterministic human-authored* check rather than a semantic
LLM verdict, and the check is *mandatory* rather than optional.

## 5. Trade-offs — when each wins

**pi-goal-x is better when:**

- You're already in the **pi ecosystem** and want goals as a native-feeling
  extension with task lists, subtasks, and multiple concurrent goals.
- **Context continuity matters** — the continuous-session executor avoids
  re-deriving state each iteration (GoaLoop re-reads files every attempt).
- The objective is **moderately fuzzy** — a semantic auditor with workspace
  access can approve outcomes that don't reduce to a single command, while
  still being more than a transcript reader.
- You want a **hard token budget** enforced automatically (`budgetLimited`).
- You want decomposition *inside* one goal (subtasks) rather than across
  separate attempts.

**GoaLoop is better when:**

- "Done" *can* be reduced to a check and you want that check **executed by
  an independent process on every iteration**, not just once at the end.
- You want the **strongest structural anti-cheat** — disposable
  memoryless executor + a deterministic, mandatory gate — over a semantic
  final inspector.
- You need the loop to **survive the session closing** — the detached
  orchestrator iterates for hours/days regardless of any open front-end.
- You want a **durable, inspectable audit trail** (`attempts/NNN.md`) and
  **crash/quota recovery**, and tolerate the multi-second per-attempt floor
  of spawning `claude -p`.

The cost mirror: `pi-goal-x` buys ecosystem integration, continuous
context, in-goal task decomposition, an automatic budget, and a real
(if episodic, semantic) independent auditor; GoaLoop buys per-attempt
independent verification, a deterministic and mandatory gate,
session-independent durability, and a recoverable audit trail — at the
price of authoring a concrete check up front and throwing context away
each attempt.

## Appendix: source pointers

**pi + goal extensions:**
- [earendil-works/pi](https://github.com/earendil-works/pi) — the pi agent toolkit (host)
- [pi-goal-x](https://pi.dev/packages/pi-goal-x) — independent auditor, verification contracts, task lists, multi-goal
- [code-yeongyu/pi-goal](https://github.com/code-yeongyu/pi-goal) — Codex-style port (self-completion, no auditor)
- [Michaelliv/pi-goal](https://github.com/Michaelliv/pi-goal) — same-session self-audit with evidence discipline

**GoaLoop** (this repo):
- [`design.md`](design.md) — full design and rationale
- [`comparison-codex.md`](comparison-codex.md) — sibling comparison vs. OpenAI Codex's `goal`
- [`comparison-claude-goal.md`](comparison-claude-goal.md) — sibling comparison vs. Claude Code's `/goal`
- `agents/goal-runner.md` — the Runner system prompt (verification procedure)
- `goaloop/orchestrator.py` — the attempt loop and terminator handling
