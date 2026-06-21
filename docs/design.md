# GoaLoop — Design Document

> **One-sentence definition.** GoaLoop is a goal-driven multi-attempt
> iteration framework: a thin Manager (the user's Claude Code session,
> driven by skills) launches a small background orchestrator, the
> orchestrator runs each attempt as a fresh `claude -p` Runner that executes
> the verification procedure spelled out in `goal.md` plus one unit of work
> toward the goal, and the orchestrator terminates when verification passes
> or the human stops it.

## Status

Implemented (v0.1). The runtime is the `goaloop` Python package
(`goaloop/`): a `claude -p` adapter, the attempt loop, and a
`run`/`status`/`stop`/`continue` CLI. This document is the canonical
reference for the architecture.

> **Note on the runtime pivot.** An earlier draft of this design ran the
> Runner as a Claude Code *subagent* (spawned via the `Agent` tool from the
> Manager session), specifically to avoid `claude -p` under the belief that
> `claude -p` always bills at API rates. That belief was wrong: `claude -p`
> authenticated with a Claude Code subscription is subscription-covered, the
> same as the interactive session. v0.1 therefore uses `claude -p` Runners
> driven by a detached orchestrator — which restores the true "fresh process
> per iteration, all state on disk" Ralph-loop shape and lets the
> orchestrator survive the Manager session closing. The sections below
> describe this runtime.

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

Existing implementations of this pattern tend to fall into one trap:

- **Domain-specific frameworks** that bake assumptions into the core (the
  artifact is a GitHub PR, isolation is a git worktree, there is a "deploy"
  phase between commit and verify, iterations have a fixed-cadence
  multi-phase state machine). These work well for the domain they were
  built for but resist generalization without invasive plugin layers.

GoaLoop avoids that trap. Each Runner is a fresh `claude -p` process — and
when `claude -p` is authenticated with a Claude Code subscription it is
subscription-covered, so the orchestrator does not incur per-token API
rates. The
framework provides only the conventions needed for the iteration pattern
itself — goal specification, manager–runner role split, verification
protocol — and makes no domain assumptions.

## Philosophy

Four guiding principles, each constraining what GoaLoop will and will not do.

### 1. Manager–Runner split

GoaLoop has three roles with a clean boundary (the Manager and the
orchestrator are distinct processes; the Runner is spawned per attempt).

- **Manager** is the user's main CC session, driven by the two GoaLoop
  skills. It is thin: it starts/stops the `goaloop` orchestrator, reads the
  orchestrator's status files, and talks to the user. It does not verify,
  does not modify the workspace (except `goal.md` edits the user approves),
  does not curate memory.
- **Orchestrator** is a small detached process (`goaloop run`) that paces
  attempts and reacts to each Runner's terminator. It is deterministic — not
  an LLM. It holds no authoritative state — only a tiny checkpoint (the
  active session id) so a crashed or quota-paused attempt can resume rather
  than restart.
- **Runner** is a fresh `claude -p` process the orchestrator spawns per
  attempt. It does one complete attempt: read context, run the Verification
  procedure, do one unit of work if needed, update memory files, write a
  per-attempt record, end its turn with a status terminator.

Each Runner invocation is a fresh `claude -p` session — no memory of prior
runs, context only via workspace files. This is the genuine "fresh process
per iteration" property, and on a Claude Code subscription it does not pay
API rates. Because the orchestrator is detached, it keeps iterating even if
the Manager session closes.

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

2. **Each Runner is a fresh `claude -p` session, and Verification runs once
   at the start of each attempt.** The verdict produced in attempt N is
   therefore formed by Runner N reading the state that Runner N−1 left
   behind, with no shared context to Runner N−1's reasoning. The "judge" is
   always a fresh process looking at the workspace and the immutable
   criteria — never the same process that produced the artifact.

The arm's-length-referee property emerges from the time-series structure
of attempts, not from a parallel referee process. Runner N judges Runner
N−1's work because Runner N runs first-thing-in-the-attempt and has no
memory of Runner N−1. This holds uniformly for every Verification type
(shell threshold, boolean, LLM-as-judge): no nested agent is required
even for judge-style criteria — the Runner reads the rubric itself, scores
the artifact directly, and the structural independence comes from being a
different Runner than the one that wrote the artifact.

The load-bearing requirement on the human is to write a concrete
Verification procedure at `/goal-init` time. If a goal cannot be expressed
that way, GoaLoop refuses to initialize the workspace — that refusal is
itself useful product feedback to the human.

### 3. Honest about what the framework can enforce

GoaLoop is a thin layer of conventions on top of `claude -p`. What it can
and cannot do shifted slightly with the `claude -p` runtime:

- It **can** stop the orchestrator programmatically (`goaloop stop` sends SIGTERM)
  and **can** read each attempt's reported `total_cost_usd` from the
  stream-json `result` event (logged per attempt). It still does **not**
  *enforce* a token/cost budget — knowing the cost is not the same as
  capping it mid-stream — so no `max_tokens` appears in `goal.md`.
- It cannot sandbox the Runner from the user's workspace (`claude -p` runs
  with `--dangerously-skip-permissions` in the workspace).
- It cannot verify in real time that the Runner ran the procedure honestly;
  the next Runner's fresh-context verification catches dishonesty, but not
  instantly.

The design accepts these limits rather than papering over them. Budget
enforcement and attempt counters do not appear in `goal.md`. The
orchestrator does keep one operational safety valve — it gives up after
bounded retries of *malformed* or failing attempts (crashes, missing
terminators; `MAX_CONSECUTIVE_FAILURES`) — but that is a guard against a
broken Runner, not a goal-level termination condition. The real termination
paths remain: (a) Verification passes and the orchestrator exits, (b) the
Runner judges itself `blocked`, (c) the orchestrator gives up with `error`,
(d) the human stops it.

### 4. Ralph loop spirit: dumb loop, state in files

Geoffrey Huntley's Ralph loop pattern — `while :; do claude -p < PROMPT.md;
done` — works because the loop is dumb, the prompt is fixed, and all state
lives on disk. GoaLoop *is* a Ralph loop, with thin structure added:

| Ralph (`claude -p`) | GoaLoop |
|---|---|
| `while :; do claude -p < PROMPT.md` | `goaloop run` orchestrator, one `claude -p` per attempt |
| `PROMPT.md` (fixed instruction) | `agents/goal-runner.md` as `--append-system-prompt` + a per-attempt brief |
| Subprocess per iteration | Fresh `claude -p` session per attempt |
| `PLAN.md` + scattered state files | `goal.md` + `memory/learnings.md` + `attempts/NNN.md` |
| `grep DONE PLAN.md` to terminate | orchestrator reads the Runner's `{"status":"pass"}` terminator and exits |
| No evaluator (or `grep` as evaluator) | Runner executes `goal.md`'s Verification procedure |
| API billing | Subscription (subscription-authenticated `claude -p`) |

## Architecture

### Core concepts

**Goal**
The objective + hard constraints + a literal verification procedure that
will be executed to decide whether the objective and constraints are
satisfied. Captured in `goal.md`. Mutable mid-run — editing `goal.md` takes
effect on the next `/goal-run` invocation.

**Workspace**
A directory that contains one goal-driven task instance. It lives at
`~/.goaloop/<workspace_name>`, so the workspace name is its identifier and
its path is derived from that name. Default fully private:
nothing crosses workspace boundaries unless the user explicitly arranges
sharing outside GoaLoop. Multiple workspaces can run concurrently in separate
Claude Code sessions; external resource conflicts (shared clusters, shared
GPUs) are not GoaLoop's concern.

**Manager**
The user's main CC session, driven by the `/goal-init` and
`/goal-run` skills. Starts/stops the orchestrator, reads its status files,
talks to the user. Holds no authoritative state itself — the workspace files
are the source of truth.

**Orchestrator**
The `goaloop` background process (`goaloop run <workspace>`). Deterministic,
not an LLM. For each attempt it mints a session id, spawns one `claude -p`
Runner, parses the Runner's terminator, and branches: exit (on `pass` or
`blocked`), pace and start the next attempt with a fresh session (on
`advanced`), or wait then resume the same session (on `in_progress`). It
persists only a small checkpoint (`.goaloop/state.json` — the active session
id) so a crashed or quota-paused attempt resumes the same session instead of
restarting. On transient network errors it resumes the same session (bounded
retries); on an API quota hit it sleeps for a cool-down and resumes
indefinitely; on an unrecoverable give-up it exits with `error`.

**Runner**
A fresh `claude -p` process spawned per attempt, with `agents/goal-runner.md`
as its appended system prompt. Reads the workspace, runs Verification,
optionally does one unit of advance, updates `learnings.md`, writes
`attempts/NNN.md`, and ends its turn with a `{"status":
pass|advanced|in_progress|blocked}` terminator. Fresh context every NEW
attempt — no memory of prior runs except via workspace files.

**Verification**
The procedure defined in `goal.md`'s Verification section. The Runner
executes it within its turn; the result is one of two states: `pass` or
`fail`. (Verification is two-state; the Runner's *terminators* are four —
see below.) Long-running steps (benchmarks, training) are waited on inside
the Runner — either synchronously, or by pausing the attempt with
`in_progress` and resuming after a timed wait so the wait itself burns no
tokens.

### Workspace layout

```
<workspace>/
├── goal.md            # Objective + Hard Constraints + Verification spec
├── config.yaml        # Optional: model / interval / mode (see below)
├── suggestions.md     # Optional: async per-attempt human notes (see below)
├── memory/
│   └── learnings.md   # Run-level curated knowledge; Runner self-maintains, ~4KB cap
├── attempts/
│   ├── 001.md         # Per-attempt factual record, written by the Runner of that attempt
│   ├── 002.md
│   └── ...            # Append-only: each file is written once, never modified
└── .goaloop/          # Orchestrator-private state (not part of the goal record)
    ├── state.json     # Checkpoint: active session id + cumulative counters/cost for resume
    ├── status.txt     # Current one-line orchestrator status (read by /goal-run)
    ├── attempt_complete.json  # Last completed attempt's {attempt, status, cost_usd, total_cost_usd}
    ├── suggestions.delivered.md  # Archive of consumed suggestions.md notes, stamped per attempt
    ├── continue.json  # copilot-mode approval token (written by `goaloop continue`)
    ├── orchestrator.log       # Per-attempt log: Runner messages, tool calls, results
    └── pipeline.pid   # PID of the running orchestrator (for status/stop)
```

The `goal.md` + `memory/` + `attempts/` triple is the goal record GoaLoop
requires; `config.yaml` and `suggestions.md` are optional human inputs, and
`.goaloop/` is the orchestrator's own bookkeeping and can be deleted
between runs without losing the audit trail. Anything else (scripts the
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
Prefer: a shell command + how to parse its output + pass/fail rule.
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

#### Verification two-state semantics, attempt status model

Verification itself returns one of two states — `pass` or `fail`. But the
Runner ends its turn with one of **four** status terminators (an LLM
judgment), and the orchestrator can additionally derive two outcomes
deterministically when the Runner doesn't cleanly return a terminator. The
full model:

| Status | Source | Carries | Terminal? | Orchestrator response |
|---|---|---|---|---|
| **pass** | Runner | `verification` summary | Yes (success) | Exit; `status.txt` records PASS; `/goal-run` tells the user DONE |
| **advanced** | Runner | `summary` | No | Runner did ONE unit of work and wrote `attempts/NNN.md`; orchestrator paces (or, in copilot mode, waits for approval), then starts the NEXT attempt with a FRESH session |
| **in_progress** | Runner | `wait_secs` | No | Runner paused mid-attempt to wait out a long pollable job; orchestrator exits the process during the wait (zero tokens), sleeps, then `--resume`s the SAME session. Attempt number does not advance |
| **blocked** | Runner | `reason` | Yes (needs human) | Runner judges it cannot reach `pass` and another advance won't help (stuck, needs a human); orchestrator exits |
| **error** | Orchestrator | — | Yes (infra give-up) | Unrecoverable after bounded retries — a crash, a malformed/missing terminator, or a transient network error each retried up to 3× (`MAX_CONSECUTIVE_FAILURES` / `TRANSIENT_MAX_RETRIES`); then exit, clearing the session |
| **quota** | Orchestrator | — | No | API rate/quota limit hit; orchestrator sleeps ~15 min and resumes the SAME session, INDEFINITELY (an external clock, not a give-up) |

**`blocked` vs. `error`** is the key distinction. `blocked` is the *Runner's*
judgment (an LLM deciding the goal is unreachable without human help).
`error` is the *orchestrator's* deterministic detection (regex on claude's
result text for transient/quota, `try/except` for crashes, terminator-parse
failure for malformed output) after bounded retries. The first means "the
task needs you"; the second means "the infrastructure gave up".

The convention for signaling `pass` / `fail` from a shell-based verification
is, e.g., exit code `0` / non-zero, or a JSON status field — the choice is
the user's, recorded in `goal.md`'s Verification section. (That `pass`/`fail`
result is internal to Verification; the Runner maps it to one of the four
*terminators* above.)

Long-running verification (benchmarks, training, integration tests measured
in minutes to hours) is run **to completion within one attempt**: the Runner
kicks off the work and either waits for it synchronously, or — for a job it
can poll cheaply — returns `in_progress` with a `wait_secs` so the
orchestrator can drop the process during the wait and `--resume` the same
session afterward. Either way one attempt contains one complete Verification:
there is no async "in flight" state that splits a run across *attempts*, and
the verification script does not need to be re-entrant.

#### Session semantics

A fresh `claude -p` session is minted for every NEW attempt — this is the
anti-cheat property (Runner N has no memory of Runner N−1). Same-session
`--resume` happens only in two cases:

- **`in_progress` pause** — a clean resume after a timed wait, prompted with
  `Continue.`
- **Interruption** (crash / transient error / quota / malformed terminator)
  — resumed with a longer prompt telling the Runner to ignore the
  interruption and not restart its work.

A tiny checkpoint at `.goaloop/state.json` holds the active session id for
crash recovery; on a give-up (`error`) the session is cleared.

### The skills

#### `/goal-init`

An interactive interview that produces a valid `goal.md` and the workspace
directory. The interview is strict: if the user cannot articulate the
verification procedure concretely, the skill refuses to write the file and
asks for clarification. This is by design — the framework's load-bearing
guarantee is the rigor of `goal.md`'s Verification section.

Interview script (each step waits for the user's answer before proceeding):

1. **Workspace name.** "What should I call this workspace? It will live at
   `~/.goaloop/<name>`." (path is derived from the name, never asked)
2. **Objective.** "In one sentence, what are you trying to achieve? Make it
   quantitative if possible."
3. **Hard Constraints.** "What absolutely cannot change or degrade while
   pursuing this objective?" (zero or more)
4. **Verification of objective.** "How will we know the objective is met?
   Give me a command I can run, or a state I can observe. If I can't grep
   the answer or compare a number, we need to refine the objective. How
   does the procedure signal pass / fail?"
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

Starts (or checks) the orchestrator and relays its progress. The skill body
instructs the Manager to perform, in order:

1. **Locate the workspace** at `~/.goaloop/<name>`; require `goal.md`,
   `memory/`, `attempts/` (else send the user to `/goal-init`).
2. **Check liveness.** `goaloop status <name>`. If already RUNNING, skip to
   relay; do not start a second orchestrator.
3. **Start the orchestrator.** `goaloop run <name>` (detached background
   process). Optional `--model` / `--interval` / `--mode`.
4. **Relay progress.** Read `.goaloop/status.txt`,
   `.goaloop/attempt_complete.json`, and the latest `attempts/NNN.md` to
   summarize for the user. On PASS (orchestrator exited), tell the user DONE
   and quote the verification summary. On `blocked`, quote the Runner's
   reason (needs human). On `error`, surface the infra give-up. In copilot
   mode, after each `advanced` attempt relay it and approve the next with
   `goaloop continue <name>` on the user's go-ahead.

The orchestrator, not the Manager, performs the per-attempt mechanics:

1. **Count attempts.** Highest `NNN` in `attempts/` + 1 — recomputed from
   disk each turn, so a crash that didn't write `attempts/NNN.md` retries
   the same number.
2. **Pick a session.** Resume the checkpointed session if the prior process
   died mid-attempt; otherwise mint a fresh uuid (the normal case — a fresh
   Runner with no memory of the last).
3. **Spawn the Runner.** `claude -p <brief> --append-system-prompt
   goal-runner.md --output-format stream-json --session-id <uuid>
   --dangerously-skip-permissions`. The brief carries the workspace path,
   the attempt number, the read-context + terminator instructions, and —
   on a fresh attempt — any NEW `suggestions.md` text since the cursor.
4. **Parse the terminator** (`{"status": pass|advanced|in_progress|blocked}`)
   and branch: `pass`/`blocked` → exit; `advanced` (and `attempts/NNN.md`
   exists) → pace (or, in copilot mode, wait for approval), then next
   attempt; `in_progress` → drop the process, wait `wait_secs`, resume the
   same session. A crash, missing/malformed terminator, transient error, or
   quota limit is handled by the orchestrator (retry / wait / give up with
   `error`) rather than by this branch.

The Manager is otherwise passive: it does not read `goal.md` to make
decisions (only to quote to the user), does not run Verification, does not
modify the workspace, does not update `learnings.md`.

#### `/goal-flash`

A fast path that collapses `/goal-init` + `/goal-run` into one shot, for
tasks the user can state in a sentence — where the seven-question interview
is overkill. Instead of interviewing, it **infers** a complete `goal.md`
from the short description (workspace name auto-named, Hard Constraints
defaulted to `None`, Environment & Tools reverse-engineered from the
verification command, Initial Context omitted unless useful), shows it to
the user, and starts the orchestrator immediately — no question-at-a-time,
no per-section confirmation.

The one invariant it does **not** relax is verification rigor (Philosophy
§2): the only thing flash may not infer is a fabricated check. If a concrete
verification cannot be derived from the description, flash refuses and sends
the user to `/goal-init` (or asks that single question) rather than writing
a placeholder. This keeps the load-bearing guarantee intact while removing
the friction for already-clear tasks.

Because the goal was inferred rather than interviewed, `goal.md`'s
mutability mid-run (the steering wheel — see Human guidance protocol) is the
correction channel: flash surfaces the inferred Verification and any
assumptions so the user can catch a wrong inference fast and edit `goal.md`,
or `goaloop stop`. Progress relay after start is identical to `/goal-run`.

The Runner, on its end, follows this fixed workflow:

1. **Load context.** Read `goal.md` in full. Read `memory/learnings.md`.
   Read recent `attempts/*.md` (last few; Runner's judgment of how far
   back).
2. **Verify.** Execute the Verification procedure from `goal.md`.
3. **Branch on result:**
   - **pass** → Write `attempts/NNN.md` recording the verification result;
     do not modify `learnings.md`; return `{ status: "pass", verification:
     ... }`.
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
7. **Terminate.** End the turn with `{"status": "advanced", "summary": ...}`
   (or `{"status": "pass", ...}` on the pass branch; `in_progress` with
   `wait_secs` to pause for a long pollable job; `blocked` with a `reason` if
   stuck and needing a human). The orchestrator parses this line.

Each attempt runs Verification **exactly once** (in the Runner). There is no
separate pre-check / post-check. The "did the previous attempt help?"
question is answered by the next attempt's Verification.

#### Human guidance protocol

The orchestrator runs detached: there is no live conversation channel from
the Manager into a running Runner, and the Manager does not compose a fresh
brief each turn (the orchestrator does, from a fixed template). There are
**two** durable channels, by intent serving different purposes.

**`goal.md` (and the files it references) — permanent / structural.**

To steer the run durably, the human edits `goal.md` — refine the Objective,
add or relax a Hard Constraint, or add a note in the `Initial Context`
section ("from now on focus on the publish phase, stop optimizing
compaction"). The next attempt's Runner reads the updated spec naturally;
nothing needs to be relayed, and the change applies whether or not the
Manager session is open. The Manager's job is to **propose and make the edit
on the user's confirmation** — not to carry the guidance itself.

This is also why `goal.md` is mutable mid-run: it is the steering wheel, not
just the starting configuration.

**`suggestions.md` — transient / per-attempt.**

For a one-off note that does not belong in the goal spec (e.g. something left
while AFK — "try lock granularity next"), the human appends to
`<workspace>/suggestions.md`. The file is a **mailbox, not a log**: whatever it
holds is undelivered. On each FRESH attempt the orchestrator atomically
*claims* its contents (renaming it aside), injects them into the Runner's brief
as a "Human guidance (NEW)" section, archives them to
`.goaloop/suggestions.delivered.md`, and clears the file — so each note reaches
exactly one attempt. The atomic rename is what makes this race-free: a note
appended while a claim is in flight lands in either that batch or a fresh file
the next attempt picks up — never lost, never delivered twice, with no byte
cursor to drift when the human edits or deletes earlier notes. Use `goal.md`
for changes that should persist; use `suggestions.md` for transient nudges.

**The Manager distinguishes messages for itself vs. goal edits.**

| User message | Manager's response |
|---|---|
| "How is it going?" | Answer directly by reading `.goaloop/status.txt` + latest `attempts/NNN.md` |
| "Try lock granularity instead next time" | A transient nudge → append to `suggestions.md` (next attempt sees it once); a permanent change → propose editing `goal.md`'s Initial Context |
| "Stop the orchestrator" | `goaloop stop <name>`; confirm it stopped |
| "Change the target to P99 < 3s" | Propose editing `goal.md`'s Objective; edit on confirmation |
| "Hmm" / "OK" / chit-chat | Ignore |

#### `attempts/NNN.md` structure

Written by the Runner of attempt N. Approximately 30 lines or less. Fixed
sections so future Runners can scan quickly:

```markdown
# Attempt NNN

## Verification result
- status: pass | fail
- key metrics: <e.g. P99=5.4s, memory_delta=+3%>
- raw output excerpt: <a few lines of the verification's actual output>

## What I did
<One paragraph: the approach this attempt tried.>

## Workspace changes
- <file 1>: <one-line summary>
- <file 2>: ...
- or "no changes" (pure investigation)

## Observations
<Key observations, anomalies, things noticed but not acted on.>

## Suggested next direction
<If applicable, what the next Runner should consider; or "n/a".>
```

### How to run

The user runs `goaloop run <name>` (directly or via `/goal-run`), which
launches the detached orchestrator. It runs each attempt as a fresh
`claude -p` Runner and exits on `pass`. Because it is its own process, it
keeps iterating regardless of whether the Claude Code session that launched
it stays open.

There are two modes, selected by `--mode` or `config.yaml`:

- **`auto`** (default) — after each `advanced` attempt the orchestrator paces
  itself by `interval` (default 30s) and starts the next attempt.
- **`copilot`** — after each `advanced` attempt the orchestrator PAUSES and
  waits for human approval before the next attempt instead of pacing.
  Approve with `goaloop continue <name>` (which writes
  `.goaloop/continue.json`). Only `advanced` pauses: `pass`/`blocked`/`error`
  are terminal (no wait), and `in_progress` is an intra-attempt resume (no
  wait).

#### Configuration (`config.yaml`)

Optional, per-workspace, at `<workspace>/config.yaml`. Flat keys:

| Key | Meaning | Default |
|---|---|---|
| `model` | model id passed to `claude -p` | (CLI default) |
| `interval` | seconds between successful attempts (auto mode) | `30` |
| `mode` | `auto` or `copilot` | `auto` |

Precedence: CLI flags (`--model` / `--interval` / `--mode`) override
`config.yaml`, which overrides the built-in defaults.

The orchestrator terminates when:

- The Runner reports `pass` and the orchestrator process exits naturally.
- The Runner reports `blocked` — it judges the goal unreachable without a
  human; the orchestrator exits.
- The orchestrator gives up with `error` after bounded retries of malformed /
  failing attempts (a broken-Runner guard, not a goal condition).
- The human runs `goaloop stop <name>` (SIGTERM).

(A `quota` limit is not a termination — the orchestrator sleeps and resumes
indefinitely.)

Human pacing during a run is achieved by:

- **Reading status** via `/goal-run` (or `goaloop status`, or
  `tail -f .goaloop/orchestrator.log`) — the orchestrator writes `status.txt` and
  `attempt_complete.json` each attempt.
- **Editing `goal.md`** (permanent) or **appending to `suggestions.md`**
  (transient, per-attempt) to steer — the next attempt's Runner picks it up
  (see Human guidance protocol).
- **`goaloop continue`** to release the next attempt in copilot mode.
- **`goaloop stop`** to halt earlier than `pass`.

> **Important.** The orchestrator drives itself — it is a `while` loop in
> `goaloop run`, not a `ScheduleWakeup` chain and not wrapped in `/loop`.
> `/goal-run` only starts/checks it; the Manager does not schedule attempts.

### No loop nesting

GoaLoop uses exactly one loop: the `goaloop run` orchestrator's attempt loop.
The Manager skill body MUST NOT invoke `/loop` or `ScheduleWakeup`, and the
Runner (a `claude -p` session) MUST NOT either — if a Runner emitted
`ScheduleWakeup` it would have no effect on the orchestrator, which paces
attempts itself. Long-running verification is handled within one attempt
(the Runner waits, or returns `in_progress` for the orchestrator to time the
wait), not by any wakeup mechanism — see the attempt status model above.

### Termination

Four terminal states:

1. **pass — goal met.** Runner returns `pass`; the orchestrator exits.
   Workspace contains the converged state, full audit trail in `attempts/`.
2. **blocked — needs human.** Runner judges it cannot reach `pass` and
   another advance won't help; it returns `blocked` with a `reason` and the
   orchestrator exits. (Runner judgment — an LLM decision.)
3. **error — infra give-up.** The orchestrator gives up after bounded retries
   of *malformed* / failing attempts (crashes, missing terminators, transient
   errors), so a broken Runner doesn't spin forever. (Orchestrator's
   deterministic detection.)
4. **Human stops.** `goaloop stop <name>` sends SIGTERM; the orchestrator
   exits.

A `quota` limit is NOT terminal — the orchestrator sleeps (~15 min) and
resumes the same session indefinitely, since it's an external clock.

There is no "budget exhausted", no "max attempts", no "convergence detected"
goal termination. If progress stalls (the Runner keeps reporting `advanced`
without converging), the human notices — by reading `attempts/` or asking
the Manager — and intervenes.

## Explicitly Excluded

The following were considered and deliberately not built. Each appears here
with the reason so future contributors don't reintroduce them by reflex.

> **Implemented after v0.1 (now included).** Two features once listed here as
> excluded have since been built: `suggestions.md` (a transient per-attempt
> human → agent channel, NEW-since-cursor injection — see Human guidance
> protocol) and copilot mode (the `--mode copilot` human-review pause,
> released with `goaloop continue` — see How to run). They are documented
> above and are no longer exclusions.

| Feature | Why excluded |
|---|---|
| Independent referee evaluator (separate from Runner) | Inter-attempt judging — Runner N (fresh `claude -p` session) judging the state Runner N−1 left behind — provides the same arm's-length-referee property at zero infrastructure cost; see Philosophy §2 |
| Multiple agent roles beyond Manager–Runner (planner / critic / judge) | Manager + Runner already covers orchestration vs. execution; further role splits add coordination overhead and rarely pay off |
| Subagent Runner (via the `Agent` tool) | The earlier design's runtime, dropped in v0.1: it required the Manager session to stay open and self-schedule, and tied each attempt to the Manager's context lifetime. A detached `claude -p` orchestrator is closer to the Ralph-loop ideal and survives the session closing — for the same subscription cost. See the runtime-pivot note under Status. |
| `verdict.log` append-only history | `attempts/NNN.md` already records each verification result; one less moving piece |
| Hooks (Stop / SessionStart / PreToolUse / PostToolUse) | The orchestrator + skills are sufficient; hooks would have been infrastructure for an independent evaluator we are not building |
| Pareto multi-objective | "Main objective + hard constraints" covers the realistic cases without ambiguity |
| Budget enforcement (`max_tokens`, `max_iterations`, etc.) | GoaLoop has no mechanism to enforce these; promising them would be a lie |
| Workspace isolation primitives (`snapshot`, `rollback`) | Trial-and-rollback inside one Runner attempt is the Runner's responsibility (`git stash`, copy directories, whatever fits); the framework only sees the end of each attempt |
| Cross-workspace sharing (learnings / skills / templates) | v1 keeps workspaces fully independent; cross-workspace patterns can be added when concrete demand appears |
| External resource locks (shared clusters, GPUs) | Outside GoaLoop's scope; if needed, the user's verification scripts coordinate via whatever mechanism fits their environment |
| Custom monitor TUI | `.goaloop/orchestrator.log` (`tail -f`) plus `goaloop status` / `/goal-run` cover live monitoring; a bespoke TUI isn't worth the maintenance |
| `events.jsonl` event stream | `orchestrator.log` already captures per-attempt Runner messages, tool calls, and results; one less format |
| `questions.md` (agent → human async channel) | The Runner records open questions in `attempts/NNN.md` / `learnings.md`; the human reads them via `/goal-run` |
| `DONE` marker file | Not needed — the orchestrator exiting on `pass` and `status.txt` recording PASS is the signal; `/goal-run` relays "DONE" to the user |
| Nested `/loop` invocations | GoaLoop's single loop is the `goaloop run` orchestrator; `/goal-run` neither wraps `/loop` nor schedules wakeups |
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
  parse JSON, compare. The script blocks until the benchmark completes.
- Verification (constraint): run `./scripts/memory_delta.sh baseline current`.
- Environment & Tools: SSH config, scripts location, database source path,
  GitHub CLI for PRs, `jq`.

Test: can a Runner in `/goal-run` plausibly do a benchmark-analyze-fix
attempt within this spec, without GoaLoop needing domain-specific features?
The "deploy" step lives inside the verification scripts (the human's
responsibility); the Runner's work is investigation + code change. Each
attempt runs the benchmark to completion inside the Runner, then either
reports `pass` or does the next code change and reports `advanced`.

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
— the Runner does the judging itself, in its own `claude -p` context. The
arm's-length property comes from Runner N (a fresh `claude -p` session)
judging the draft that Runner N−1 produced; no nested agent or separate
evaluator subsystem is required. This is the same mechanism that gives
Scenario A its anti-cheat property, applied to a qualitative criterion.

Both scenarios fall out naturally on the implemented runtime — no
domain-specific features and no awkward workarounds.

## Open questions deferred to implementation

Small decisions not load-bearing on the architecture, settled while writing
the skills and the Runner system prompt (recorded here for context):

- Exact wording of the `/goal-init` interview prompts.
- Exact wording of the `/goal-run` Manager skill body.
- Exact system prompt for the Runner (`agents/goal-runner.md`, used as
  `--append-system-prompt`).
- What guidance to give the Runner about when `learnings.md` entries should
  be added vs. merged vs. pruned.
- How many recent `attempts/*.md` files the Runner should read by default
  (last N, or all under a size budget).
- How robustly the orchestrator should parse the Runner's terminator (v0.1:
  last standalone JSON line, with a flat-`{...status...}` regex fallback).

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
