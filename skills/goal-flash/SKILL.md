---
name: goal-flash
description: One-shot init + run for a task you can state in a sentence. Infers a complete goal.md from a short description (no interview), then starts the orchestrator. Use when the task is clear enough that the full /goal-init interview would be overkill.
---

You are running `/goal-flash` for GoaLoop. This is the **fast path**: the
user hands you a task they can state in a sentence or two, and you turn it
into a running loop in one shot — infer a complete `goal.md`, write it, show
it, and start the orchestrator. No question-at-a-time interview.

Use this instead of `/goal-init` when the task is already clear. If it is
NOT — see the hard gate below — fall back to `/goal-init`.

## The one hard gate: verification must be concrete

Everything in `goal.md` can be inferred EXCEPT the load-bearing part: a
concrete Verification procedure (a command + how to parse it + a pass/fail
rule, or an equally concrete observable). This is the framework's only
non-negotiable — the whole anti-cheat property rests on it.

So before writing anything:

- If you can derive a concrete verification from the description (explicit,
  or unambiguously implied — e.g. "make the repo's tests pass" → run the
  test command, exit 0), proceed.
- If you **cannot** — the task names no measurable end-state and you'd have
  to invent the check — do NOT fabricate a placeholder. Tell the user this
  task isn't flash-able as stated, name the one missing piece (how to verify
  it), and suggest `/goal-init` (or ask just that single question). Never
  weaken verification rigor to keep the fast path fast.

Everything else (constraints, environment, initial context) you infer and
default without asking.

## Step 1 — Resolve the workspace

Workspaces live at `~/.goaloop/<name>`. Derive a short kebab-case `<name>`
from the task description (the path is never asked). If
`~/.goaloop/<name>/goal.md` already exists, stop and tell the user — pick a
different name or use `/goal-run` on the existing one. `<workspace>` below
means the resolved path.

## Step 2 — Infer the full goal.md

From the one-sentence description, fill every section by reasoning — do not
ask:

- **Objective** — restate as a quantitative end-state where possible.
- **Hard Constraints** — only what the description actually implies;
  otherwise `None`.
- **Verification → objective** — the concrete check from the hard gate
  above (command + parsing + pass/fail convention).
- **Verification → constraints** — a concrete check per constraint, or
  "No constraints to verify".
- **Environment & Tools** — reverse-engineered from the verification
  command (the tools/paths/credentials it needs). If something is genuinely
  required but unknown, note it as an assumption in this section rather than
  blocking.
- **Initial Context** — omit unless the description carries background worth
  passing to the first Runner.

Use the exact section structure from `/goal-init` / design.md:

```markdown
# Goal

## Objective
<inferred>

## Hard Constraints
- <inferred, or "None">

## Verification

### How to verify the objective
<concrete command + parsing + pass/fail>

### How to verify each constraint
- <constraint>: <check>
(or "No constraints to verify")

### Environment & Tools
- <inferred bullets>

## Initial Context
<only if useful; omit the whole section otherwise>
```

## Step 3 — Write it and show it

```bash
mkdir -p <workspace>/memory <workspace>/attempts
# write <workspace>/goal.md
```

Then show the user the full `goal.md` you wrote, and call out the inferred
**Verification** and any **assumptions** explicitly — those are what they're
most likely to want to correct. You are not waiting for sign-off (Step 4
follows immediately); you are giving them what they need to catch a wrong
inference fast.

## Step 4 — Start the orchestrator

Start it right away — that's the point of `/goal-flash`:

```bash
goaloop run <name>
```

(Optional `--model` / `--interval` / `--mode`, same as `/goal-run`.) Tell
the user it's running detached and how to steer, since you inferred the goal
rather than interviewing for it:

- It runs in the background independent of this session; watch with
  `goaloop status <name>` or `tail -f <workspace>/.goaloop/orchestrator.log`.
- **The inferred goal.md is the steering wheel and is mutable mid-run.** If
  the inference was off, edit `<workspace>/goal.md` (esp. Verification) — the
  next attempt picks it up. For a transient nudge, drop a note file the next
  attempt will read — `suggestions/<next-attempt>.md`, named per the convention
  `/goal-run` describes (check `goaloop status` for the round). Halt with
  `goaloop stop <name>`.

From here, progress relay is identical to `/goal-run` — read
`.goaloop/status.txt`, `.goaloop/attempt_complete.json`, and the latest
`attempts/NNN.md`, and report PASS / blocked / error as that skill describes.
In particular, to auto-relay each completed round without polling, arm the
persistent **Monitor** described in `/goal-run` Step 4 ("Auto-relay each
completed round") rather than a fast `ScheduleWakeup`/`/loop` cadence.

## What you MUST NOT do

- Do not fabricate a verification to avoid asking. If it isn't concrete,
  bail to `/goal-init`.
- Do not run a question-at-a-time interview — that's `/goal-init`. The only
  thing you may ask is the single verification question when the gate fails.
- Once running, do not run Verification, modify the workspace (except
  `goal.md` edits the user approves), or write `memory/` or `attempts/` —
  the Runner owns those (same boundary as `/goal-run`).
