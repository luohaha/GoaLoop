# GoaLoop — Design Document

> **One-sentence definition.** GoaLoop is a goal-driven multi-attempt
> iteration framework that runs on a Claude Code subscription session: the
> user's main session acts as a thin Manager that orchestrates fresh-context
> Runner subagents, each Runner executes the verification procedure spelled
> out in `goal.md` and one unit of work toward the goal, and the loop
> terminates when verification passes or the human stops it.

## Status

Design phase. No implementation yet. This document is the canonical reference
for the architecture before any code is written.

## Motivation

Many software engineering tasks share the same shape: a human-defined
target, a way to measure whether it has been met, and repeated agent-driven
modifications to a workspace until the target is met or the human gives up.
Examples:

- Performance optimization (reduce P99 latency below X)
- Flaky test reduction (until N consecutive green runs)
- Build-time optimization (under X minutes)
- ML hyperparameter or model tuning (until eval metric above threshold)
- Writing iteration (until reviewer or rubric passes)
- Cost optimization (until cloud bill below target)
- Security hardening (until scan finds zero criticals)

LLM agents are well-suited to this work — they can investigate, modify code,
run experiments, learn from results, and iterate. The bottleneck is rarely
the agent's capability; it is the surrounding structure: how to express the
goal, how to verify it objectively, how to accumulate knowledge across
attempts, how to terminate cleanly.

Existing implementations of this pattern tend to fall into two traps:

1. **Subprocess-based agents** (one process per iteration, driven by an
   orchestrator). This pattern works but costs full API rates per token
   spent, since each subprocess is a separate non-interactive session not
   covered by Claude Code's subscription. Long iteration loops become
   expensive.

2. **Domain-specific frameworks** that bake assumptions into the core (the
   artifact is a GitHub PR, isolation is a git worktree, there is a "deploy"
   phase between commit and verify, iterations have a fixed-cadence
   multi-phase state machine). These work well for the domain they were
   built for but resist generalization without invasive plugin layers.

GoaLoop avoids both traps. The runtime is the user's Claude Code interactive
session plus subagent calls — entirely subscription-covered, no subprocess
overhead. The framework provides only the conventions needed for the
iteration pattern itself — goal specification, manager–runner role split,
verification protocol — and makes no domain assumptions.

## Philosophy

Four guiding principles, each constraining what GoaLoop will and will not do.

### 1. Manager–Runner split, both on subscription

GoaLoop has exactly two roles, and both run in the user's Claude Code
session — neither is a `claude -p` subprocess.

- **Manager** is the user's main CC session, driven by the two GoaLoop
  skills. It is a thin orchestrator: it spawns Runner subagents, receives
  their reports, talks to the user, and decides when to schedule the next
  iteration. It does not verify, does not modify the workspace, does not
  curate memory.
- **Runner** is a subagent (spawned via the `Agent` tool with a custom
  `goal-runner` agent type) that does one complete attempt: read context,
  run the Verification procedure, do one unit of work if needed, update
  memory files, write a per-attempt record, return a report.

Each Runner invocation is a fresh subagent — no memory of prior runs. This
restores the "fresh session per iteration" property without paying API
rates. The Runner is bounded by its own subagent context budget, which keeps
each attempt focused.

### 2. Verification rigor over runtime referee

A natural approach to anti-cheating in iteration frameworks is to make the
evaluator a separate component (a referee subagent or subprocess) that the
working agent cannot influence. The intuition is correct — the player
should not be the referee — but the implementation cost is an entire
evaluator subsystem: a separate execution context, a protocol for invoking
it, a way to gate its output from agent tampering, and a story for what to
do when the evaluator itself fails.

GoaLoop achieves the same property without an evaluator subsystem, through
the combination of two design choices already mandated elsewhere:

1. **Verification criteria are written into `goal.md` at init time and not
   modified by Runners.** Whether the criterion is a shell exit code, a
   numeric threshold, or a rubric for LLM-as-judge scoring, it lives in
   `goal.md` (or files `goal.md` references, like `rubric.md`), which is
   the human's authored input. Runners read these criteria; they do not
   author them.

2. **Each Runner is a fresh subagent, and Verification runs once at the
   start of each attempt.** The verdict produced in attempt N is therefore
   formed by Runner N reading the state that Runner N−1 left behind, with
   no shared context to Runner N−1's reasoning. The "judge" is always a
   fresh agent looking at the workspace and the immutable criteria — never
   the same agent that produced the artifact.

The arm's-length-referee property emerges from the time-series structure
of attempts, not from a parallel referee process. Runner N judges Runner
N−1's work because Runner N runs first-thing-in-the-attempt and has no
memory of Runner N−1. This holds uniformly for every Verification type
(shell threshold, boolean, LLM-as-judge): no nested subagent is required
even for judge-style criteria — the Runner reads the rubric itself, scores
the artifact directly, and the structural independence comes from being a
different Runner than the one that wrote the artifact.

The load-bearing requirement on the human is to write a concrete
Verification procedure at `/goal-init` time. If a goal cannot be expressed
that way, GoaLoop refuses to initialize the workspace — that refusal is
itself useful product feedback to the human.

### 3. Honest about what the framework can enforce

GoaLoop is a thin layer of conventions on top of Claude Code. It cannot:

- Count tokens (not exposed to skills).
- Stop `/loop` programmatically (only `Esc` / session close / 7-day expiry).
- Sandbox the agent from the user's workspace.
- Verify that the Runner ran the verification procedure honestly (next
  Runner's verification catches it, but not in real time).

The design accepts these limits rather than papering over them. Budget
enforcement, attempt counters as termination conditions, and "max
consecutive failures" do not appear in `goal.md` — promising them would be a
lie. The two real termination paths are: (a) Verification passes and the
Manager does not schedule the next wakeup, (b) the human stops the loop.

### 4. Ralph loop spirit: dumb loop, state in files

Geoffrey Huntley's Ralph loop pattern — `while :; do claude -p < PROMPT.md;
done` — works because the loop is dumb, the prompt is fixed, and all state
lives on disk. GoaLoop preserves the spirit while changing the runtime:

| Ralph (`claude -p`) | GoaLoop (skills + subagent) |
|---|---|
| `while :; do claude -p < PROMPT.md` | `/loop /goal-run` (dynamic mode) |
| `PROMPT.md` (fixed instruction) | `/goal-run` skill body |
| Subprocess per iteration | Fresh subagent per iteration |
| `PLAN.md` + scattered state files | `goal.md` + `memory/learnings.md` + `attempts/NNN.md` |
| `grep DONE PLAN.md` to terminate | Manager omits `ScheduleWakeup` after Runner reports pass |
| No evaluator (or `grep` as evaluator) | Runner executes `goal.md`'s Verification procedure |
| API billing | Subscription |

## Architecture

### Core concepts

**Goal**
The objective + hard constraints + a literal verification procedure that
will be executed to decide whether the objective and constraints are
satisfied. Captured in `goal.md`. Mutable mid-run — editing `goal.md` takes
effect on the next `/goal-run` invocation.

**Workspace**
A directory that contains one goal-driven task instance. Its path is its
identifier; there is no global "workspace name". Default fully private:
nothing crosses workspace boundaries unless the user explicitly arranges
sharing outside GoaLoop. Multiple workspaces can run concurrently in separate
Claude Code sessions; external resource conflicts (shared clusters, shared
GPUs) are not GoaLoop's concern.

**Manager**
The user's main CC session, driven by the `/goal-init` and
`/goal-run` skills. Spawns Runner subagents, receives their reports,
talks to the user, decides whether to schedule the next iteration. Holds no
authoritative state itself — the workspace files are the source of truth.

**Runner**
A subagent (custom `goal-runner` agent type) spawned per attempt. Reads
the workspace, runs Verification, optionally does one unit of advance,
updates `learnings.md`, writes `attempts/NNN.md`, returns a structured
report to the Manager. Fresh context every invocation — no memory of prior
runs except via workspace files.

**Verification**
The procedure defined in `goal.md`'s Verification section. The Runner
executes it; the result is one of three states: `pass`, `fail`, or
`pending` (verification is in flight, e.g. a long-running benchmark started
but not yet complete).

### Workspace layout

```
<workspace>/
├── goal.md            # Objective + Hard Constraints + Verification spec
├── memory/
│   └── learnings.md   # Run-level curated knowledge; Runner self-maintains, ~4KB cap
└── attempts/
    ├── 001.md         # Per-attempt factual record, written by the Runner of that attempt
    ├── 002.md
    └── ...            # Append-only: each file is written once, never modified
```

That is the entire structure GoaLoop requires. Anything else (scripts the
verification uses, source code being modified, datasets, etc.) lives wherever
the user already keeps it — `goal.md`'s `Environment & Tools` section points
to those locations.

Two memory tiers, by deliberate design:

| File | Nature | Writer | Purpose |
|---|---|---|---|
| `attempts/NNN.md` | Factual, write-once, never modified | Runner of attempt N | Raw record: what was tried, what was observed, verification result |
| `memory/learnings.md` | Curated, mutable, ~4KB cap | Runner (any attempt) | Distilled cross-attempt knowledge: validated patterns, ruled-out hypotheses |

`attempts/` is the "ledger". `learnings.md` is the "textbook". A Runner
arriving fresh reads the latest few `attempts/NNN.md` for recent context
and `learnings.md` for accumulated wisdom.

### `goal.md` specification

`goal.md` is plain Markdown — no YAML frontmatter. It MUST contain these
sections in this order:

```markdown
# Goal

## Objective
<One paragraph. Quantitative wherever possible. State the desired
end-state, not the method.>

## Hard Constraints
<Zero or more bulleted constraints that MUST NOT be violated even if the
objective is met. Quantitative where possible.>
- ...
- ...

## Verification

### How to verify the objective
<The concrete procedure that decides whether the objective is met.
Prefer: a shell command + how to parse its output + pass/fail/pending rule.
Acceptable: a sequence of commands + expected observations.
NOT acceptable: "the Runner decides whether it looks good".>

### How to verify each constraint
<For each hard constraint above, the concrete check.>
- <constraint 1>: <check>
- <constraint 2>: <check>

### Environment & Tools
<Everything the verification procedure needs to run.>
- CLI tools required (e.g. gh, jq, ssh, kubectl)
- Credentials and their locations (SSH config, API token paths)
- External system access (clusters, APIs, databases)
- File paths (scripts, source code, datasets)
- Preconditions (services that must be running, dependencies installed)
```

Optional trailing section:

```markdown
## Initial Context
<Anything the Runner should know on first invocation that isn't already
implied by the verification spec — e.g. relevant source code locations,
past experiments, domain background.>
```

Notably absent: no `Budget`, no `max_iterations`, no `max_tokens`. GoaLoop
cannot enforce these (see Philosophy §3); listing them would be misleading.

#### Verification three-state semantics

Verification is **not** binary. The procedure returns one of three states:

| State | Meaning | Manager response |
|---|---|---|
| **pass** | Objective met AND no hard constraint violated | Stop: don't schedule next wakeup, tell user DONE |
| **fail** | Objective not met OR some constraint violated | Runner does an advance, then this attempt ends |
| **pending** | Verification is in flight (async, e.g. long benchmark started but not yet complete) | Manager schedules a wakeup to check again, no advance |

The three-state shape is what makes long verification work without
restructuring the loop. The convention for signaling each state from a
shell-based verification is, e.g., exit codes `0` / `1` / `2`, or a JSON
status field — the choice is the user's, recorded in `goal.md`'s
Verification section.

For long-running verification (benchmarks, training, integration tests
measured in hours), the user's verification script must be **re-entrant**:
on first call it kicks off the async work and records a handle (e.g. a
`.benchmark-handle` file), returns `pending`; on subsequent calls it
queries the handle, returns `pending` if still running, `pass`/`fail` once
complete and parses the result. GoaLoop does not provide this pattern as a
primitive — it is the verification author's responsibility, identical to
the pattern any async system requires.

### The two skills

#### `/goal-init`

An interactive interview that produces a valid `goal.md` and the workspace
directory. The interview is strict: if the user cannot articulate the
verification procedure concretely, the skill refuses to write the file and
asks for clarification. This is by design — the framework's load-bearing
guarantee is the rigor of `goal.md`'s Verification section.

Interview script (each step waits for the user's answer before proceeding):

1. **Workspace location.** "Where should this workspace live? (path)"
2. **Objective.** "In one sentence, what are you trying to achieve? Make it
   quantitative if possible."
3. **Hard Constraints.** "What absolutely cannot change or degrade while
   pursuing this objective?" (zero or more)
4. **Verification of objective.** "How will we know the objective is met?
   Give me a command I can run, or a state I can observe. If I can't grep
   the answer or compare a number, we need to refine the objective. How
   does the procedure signal pass / fail / pending?"
5. **Verification of each constraint.** For each constraint from step 3:
   "How will we check this constraint?"
6. **Environment & Tools.** "What does running these verification steps
   require — CLI tools, credentials, external system access, file paths,
   preconditions?"
7. **Initial Context (optional).** "Anything else the Runner should know on
   its first invocation?"

The skill then writes `<workspace>/goal.md` and creates
`<workspace>/memory/` and `<workspace>/attempts/`. `learnings.md` may be
omitted at init (Runner creates it on first write). Output: the path to the
workspace and a one-line confirmation.

#### `/goal-run`

A single iteration: the Manager spawns one Runner subagent and processes its
report. The skill body instructs the Manager to perform, in order:

1. **Count attempts.** Find the highest `NNN` in `<workspace>/attempts/`
   (or 0 if empty). The next Runner is attempt `NNN+1`.
2. **Compose brief.** Build a self-contained brief for the Runner that
   includes:
   - The workspace path
   - The attempt number (`NNN+1`)
   - Recent human guidance from the conversation, **verbatim** — see the
     Human guidance protocol below (or "none")
   - Instruction to read `goal.md`, `memory/learnings.md`, and recent
     `attempts/*.md` for context
   - The expected return shape (see below)
3. **Spawn Runner.** Invoke the `Agent` tool with `subagent_type:
   "goal-runner"` and the brief as prompt. Wait for return.
4. **Process report.** Parse the Runner's structured report. One of:
   - `status: pass` → tell the user "DONE", show the verification output
     and key metrics; do **not** call `ScheduleWakeup`; loop ends.
   - `status: pending` → tell the user briefly that verification is in
     flight; call `ScheduleWakeup` with a delay appropriate to the
     verification's expected completion time (taken from the Runner's
     report or a sensible default).
   - `status: advanced` → tell the user briefly what the Runner did; call
     `ScheduleWakeup` for the next attempt.
5. **(Implicit)** Memory and per-attempt files have already been written by
   the Runner; Manager does not touch them.

The Manager is otherwise passive: it does not read `goal.md` (except
optionally to quote relevant parts to the user), does not run Verification,
does not modify the workspace, does not update `learnings.md`.

The Runner, on its end, follows this fixed workflow:

1. **Load context.** Read `goal.md` in full. Read `memory/learnings.md`.
   Read recent `attempts/*.md` (last few; Runner's judgment of how far
   back).
2. **Verify.** Execute the Verification procedure from `goal.md`.
3. **Branch on result:**
   - **pass** → Write `attempts/NNN.md` recording the verification result;
     do not modify `learnings.md`; return `{ status: "pass", verification:
     ... }`.
   - **pending** → Write `attempts/NNN.md` recording what was started (e.g.
     handle file path, expected completion time, what's being measured);
     do not advance; return `{ status: "pending", suggested_delay: ... }`.
   - **fail** → Continue to step 4.
4. **Advance.** Decide what to try based on `goal.md`, `learnings.md`, and
   prior `attempts/`. Do one meaningful unit of work toward the objective:
   investigate, modify the workspace, run experiments. Local sanity checks
   (does my code compile, does my unit test pass) are part of the Runner's
   internal work and are not the same as Verification.
5. **Update memory.** If this attempt revealed something worth carrying
   forward, update `memory/learnings.md` (add / merge / prune to stay under
   ~4KB).
6. **Record attempt.** Write `attempts/NNN.md` following the structure
   below.
7. **Return.** Return `{ status: "advanced", summary: ... }`.

Each `/goal-run` invocation runs Verification **exactly once** (in the
Runner). There is no separate pre-check / post-check. The "did the previous
attempt help?" question is answered by the next attempt's Verification.

#### Human guidance protocol

The Manager–Runner split breaks the direct human↔Runner channel: the human
talks to the Manager (their own CC session), and only the Manager can reach
the Runner via the brief. The protocol for how human guidance flows through:

**Primary channel: the conversation, relayed verbatim.**

When the Manager composes the brief (step 2 above), it populates the
"Human guidance" section by:

1. Identifying the conversation window since the previous `/goal-run`
   invocation returned (or since the session started, for the first attempt).
2. Extracting user messages from that window.
3. Including them in the brief **verbatim**, not summarized.
4. Filtering out only obvious non-feedback (e.g. greetings, `how's it going?`
   directed at the Manager itself). When in doubt, include.

The Manager is a transparent relay, not a summarizer. Summarization is
work, and Manager is supposed to be thin; the Runner can filter noise.

**Permanent guidance goes in `goal.md`.**

If the human's message is a lasting course change ("from now on, focus on
the publish phase, stop trying to optimize compaction"), it is not an
ephemeral suggestion — it is a Goal amendment. The Manager should propose
editing `goal.md` (typically the Initial Context section, or refining
Objective / Constraints if applicable), then make the edit on the user's
confirmation. Subsequent Runners read the updated `goal.md` naturally; no
relay needed.

**The Manager distinguishes messages for itself vs. for the Runner.**

| User message | Manager's response |
|---|---|
| "How is it going?" | Manager answers directly by reading `attempts/`; not relayed |
| "Try lock granularity instead next time" | Relayed to next brief |
| "Stop the loop" | Manager omits `ScheduleWakeup`; tells user the loop has stopped |
| "Change the target to P99 < 3s" | Manager proposes editing `goal.md`; not relayed (goal change is structural, not a per-attempt suggestion) |
| "Hmm" / "OK" / chit-chat | Filtered |

The Manager uses common sense and biases toward inclusion when ambiguous.

**No `suggestions.md` file.**

The conversation is already the persistent record (it lives in the CC
session) and `goal.md` covers cross-session permanence. Introducing a
`suggestions.md` would create a third truth source for human input that can
conflict with both the conversation and `goal.md`. v1 deliberately omits
it; if real usage shows users need an async file-based channel (e.g. for
leaving notes while AFK), it can be added in a later version.

#### `attempts/NNN.md` structure

Written by the Runner of attempt N. Approximately 30 lines or less. Fixed
sections so future Runners can scan quickly:

```markdown
# Attempt NNN

## Verification result
- status: pass | fail | pending
- key metrics: <e.g. P99=5.4s, memory_delta=+3%>
- raw output excerpt: <a few lines of the verification's actual output>

## What I did
<One paragraph: the approach this attempt tried.>

## Workspace changes
- <file 1>: <one-line summary>
- <file 2>: ...
- or "no changes" (pending, or pure investigation)

## Observations
<Key observations, anomalies, things noticed but not acted on.>

## Suggested next direction
<If applicable, what the next Runner should consider; or "n/a".>
```

### How to run

v0.1 has a single mode: the user invokes `/loop /goal-run` (dynamic,
no interval), and the loop self-paces until the Runner reports `pass`
or the user stops it.

There is intentionally no separate "copilot" / "one-shot" mode in
v0.1. The Manager skill always calls `ScheduleWakeup` on `advanced`
or `pending`, because it has no reliable way to detect whether the
current invocation came from `/loop` or from a bare `/goal-run`. The
honest design choice is to commit to the auto-paced shape and let the
human pace by other means (described below) rather than fake a
copilot mode that the implementation cannot guarantee.

The loop terminates when:

- The Runner reports `pass`, the Manager does not schedule the next
  wakeup, and the loop ends naturally.
- The user presses `Esc` / closes the session.
- The 7-day auto-expiry on scheduled tasks fires.

Human pacing during a run is achieved by:

- **Reading the relayed report** after each attempt. The Manager
  summarizes the Runner's return to the user.
- **Dropping verbatim suggestions** in the conversation, which the
  Manager relays into the next Runner's brief.
- **Editing `goal.md`** mid-run to amend the target or constraints.
  The next Runner picks up the new spec.
- **Pressing Esc** to stop earlier than `pass`.

> **Important.** Use dynamic mode (`/loop /goal-run`), NOT interval
> mode (`/loop 5m /goal-run`). Interval-mode loops cannot be ended by
> the invoked skill — only by the user. Dynamic mode lets the Manager
> end the loop by simply not scheduling the next wakeup once the
> Runner reports `pass`.

### Loop nesting is not allowed

GoaLoop relies on a single outer `/loop /goal-run` in dynamic mode. The
Manager skill body MUST NOT invoke another `/loop` internally, and the
Runner subagent MUST NOT invoke `/loop` at all. Nested `/loop` behavior is
not documented in Claude Code and is not part of GoaLoop's contract.
Sub-task polling and long-running verification are handled by the outer
loop's `ScheduleWakeup` and the Verification three-state semantics
described above.

### Termination

Two real paths:

1. **Goal met.** Runner returns `pass`; Manager does not call
   `ScheduleWakeup`; loop ends naturally. Workspace contains the converged
   state, full audit trail in `attempts/`.
2. **Human stops.** User presses `Esc`, closes the session, or stops `/loop`.

There is no "budget exhausted", no "max attempts", no "convergence detected"
termination from the framework. If progress stalls, the human notices (by
reading `attempts/` or asking the Manager) and intervenes — there is no
daemon trying to intervene on their behalf.

## Explicitly Excluded

The following were considered and deliberately not built. Each appears here
with the reason so future contributors don't reintroduce them by reflex.

| Feature | Why excluded |
|---|---|
| Independent referee evaluator (separate from Runner) | Inter-attempt judging — Runner N (fresh subagent) judging the state Runner N−1 left behind — provides the same arm's-length-referee property at zero infrastructure cost; see Philosophy §2 |
| Multiple agent roles beyond Manager–Runner (planner / critic / judge) | Manager + Runner already covers orchestration vs. execution; further role splits add coordination overhead and rarely pay off |
| `claude -p` subprocess Runner | API billing defeats the subscription economics that motivate the rewrite; subagent provides the same fresh-context property |
| `verdict.log` append-only history | `attempts/NNN.md` already records each verification result; one less moving piece |
| Hooks (Stop / SessionStart / PreToolUse / PostToolUse) | Skills + subagent are sufficient; hooks would have been infrastructure for an independent evaluator we are not building |
| Pareto multi-objective | "Main objective + hard constraints" covers the realistic cases without ambiguity |
| Budget enforcement (`max_tokens`, `max_iterations`, etc.) | GoaLoop has no mechanism to enforce these; promising them would be a lie |
| Workspace isolation primitives (`snapshot`, `rollback`) | Trial-and-rollback inside one Runner attempt is the Runner's responsibility (`git stash`, copy directories, whatever fits); the framework only sees the end of each attempt |
| Cross-workspace sharing (learnings / skills / templates) | v1 keeps workspaces fully independent; cross-workspace patterns can be added when concrete demand appears |
| External resource locks (shared clusters, GPUs) | Outside GoaLoop's scope; if needed, the user's verification scripts coordinate via whatever mechanism fits their environment |
| Custom monitor TUI | Claude Code is the UI; conversation history shows what's happening |
| `events.jsonl` event stream | Same reason as monitor TUI |
| `suggestions.md` (human → agent async channel) | User speaks to the Manager in the conversation; Manager passes recent feedback into the Runner's brief |
| `questions.md` (agent → human async channel) | Manager talks to the human directly in the conversation |
| `DONE` marker file | Not needed — Manager omitting `ScheduleWakeup` ends the loop; "DONE" in the Manager's message to the user is the human-visible signal |
| Auto-pause / human-review pause skill | `/loop` in dynamic mode auto-pauses between attempts; user reads the Manager's relay of the Runner's report and decides |
| Nested `/loop` invocations | Nested loop behavior is not documented in Claude Code; GoaLoop relies only on a single outer `/loop /goal-run` |
| Manager-side verification or memory curation | Runner is the sole writer to `learnings.md` and `attempts/`; keeps a single authoritative writer per file |
| Pre-check / post-check duplication | One Verification per attempt; the next attempt's Verification serves as the prior attempt's post-mortem |

## Acid-test scenarios

The design should be validated against two concrete scenarios before any
implementation. If either scenario forces an awkward workaround, the design
needs revision; if both feel natural, the design is ready to implement.

### Scenario A: Database performance optimization

A `goal.md` for "reduce P99 ingest latency below 5s on the realtime
benchmark, without increasing memory by more than 10%":

- Objective: quantitative threshold on P99.
- Hard constraint: memory ceiling.
- Verification (objective): SSH to bench cluster, run `./scripts/measure_p99.sh`,
  parse JSON, compare. Returns `pending` while benchmark is running.
- Verification (constraint): run `./scripts/memory_delta.sh baseline current`.
- Environment & Tools: SSH config, scripts location, database source path,
  GitHub CLI for PRs, `jq`.

Test: can a Runner in `/goal-run` plausibly do a benchmark-analyze-fix
attempt within this spec, without GoaLoop needing domain-specific features?
The "deploy" step lives inside the verification scripts (the human's
responsibility); the Runner's work is investigation + code change. The
benchmark's hours-long run becomes a `pending` loop of attempts that mostly
just check status, then a single `fail` attempt that does the next code
change.

### Scenario B: Writing iteration (autoresearch-style)

A `goal.md` for "draft a 1500-word RFC explaining concept X, until an LLM
judge rates it ≥ 8/10 on a clarity rubric":

- Objective: judge score threshold.
- Hard constraint: length between 1200 and 1800 words.
- Verification (objective): the Runner reads `rubric.md` (immutable
  during the run) and `draft.md` (the current state), scores the draft
  against each rubric dimension in its own context, writes the verdict
  as JSON to `last-verdict.json`. Pass when `score >= 8.0`.
- Verification (constraint): `wc -w draft.md` in range.
- Environment & Tools: the rubric document, the draft file.

Test: does the framework accommodate LLM-as-judge verification without
needing any framework support for "judge evaluators"? The answer is yes
— the Runner does the judging itself, in its own subagent context. The
arm's-length property comes from Runner N (fresh subagent) judging the
draft that Runner N−1 produced; no nested subagent or separate evaluator
subsystem is required. This is the same mechanism that gives Scenario A
its anti-cheat property, applied to a qualitative criterion.

If both scenarios pass the acid test, GoaLoop is ready to implement.

## Open questions deferred to implementation

Small decisions not load-bearing on the architecture, to settle while writing
the skills and the Runner agent definition:

- Exact wording of the `/goal-init` interview prompts.
- Exact wording of the `/goal-run` Manager skill body.
- Exact system prompt for the `goal-runner` subagent type.
- What guidance to give the Runner about when `learnings.md` entries should
  be added vs. merged vs. pruned.
- How many recent `attempts/*.md` files the Runner should read by default
  (last N, or all under a size budget).
- Whether the Manager should fall back to reading `attempts/` if the
  Runner's report parses oddly (defensive vs. trust the contract).

These will be settled by writing the skills and observing what reads
naturally; they do not require architectural decisions.

## Future work (post-v1)

Possible additions if real usage exposes a need, not commitments:

- An optional Stop-hook-based independent evaluator for users who need
  stronger anti-cheating than `goal.md` rigor plus fresh-context Runner
  provides.
- A cross-workspace shared-skills mechanism if multiple workspaces start
  repeating the same verification utilities.
- A "templates" library of common `goal.md` shapes (perf, flaky-test,
  writing) to bootstrap `/goal-init`.
- Support for `goal.md` to point at an external verification script file
  rather than inlining the procedure, for very long verification specs.
- Parallel Runner spawning within one Manager iteration, if a goal
  naturally decomposes into independent sub-investigations.

None of these are needed for v1.
