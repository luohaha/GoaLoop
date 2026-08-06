---
name: goal-flash
description: Start a GoaLoop goal-driven iteration loop from a one-line task. Infers a complete goal.md (objective, constraints, and a concrete verification procedure) from a short description, writes it, starts the background orchestrator, and relays progress. The single entry point for GoaLoop.
---

You are running `/goal-flash` for GoaLoop — the single entry point. The user
hands you a task they can state in a sentence or two, and you turn it into a
running loop in one shot: infer a complete `goal.md`, write it, show it, start
the orchestrator, and relay progress until the goal is met.

GoaLoop is a detached background loop that runs one fresh headless **Runner**
per attempt against the `goal.md` you write. The worker provider is Claude
(`claude -p`, default) or Codex (`codex exec`). Each Runner reads context, runs
the Verification, advances one unit of work if it fails, and records the
attempt — until Verification passes, the Runner reports it's blocked, or you
stop it. It keeps running even if this Claude Code session closes; all state
lives in `<workspace>/.goaloop/`.

Your role is **thin**: produce a good `goal.md`, start the orchestrator, and
relay its status. You do **not** run Verification, modify the workspace once it's
running (except `goal.md` edits the user approves), or write `attempts/` or
`memory/` — the Runner owns those.

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
  to invent the check — do NOT fabricate a placeholder. Stop and ask the user
  the single question you're missing: how should the objective be verified
  concretely? Acceptable answers: "run `./scripts/foo.sh` and check exit
  code", "parse `metrics.p99` from result.json, pass if ≤ 5.0", "score
  `draft.md` against `rubric.md`, pass if ≥ 8.0". Unacceptable: "the agent
  looks at it and decides". Once you have a concrete answer, proceed. Never
  weaken verification rigor to keep the fast path fast — a goal you can't
  verify is a goal you can't reach.

Everything else (constraints, environment, initial context) you infer and
default without asking.

## Step 1 — Resolve the workspace

Workspaces live at `~/.goaloop/<name>`. Derive a short kebab-case `<name>`
from the task description (the path is never asked). If
`~/.goaloop/<name>/goal.md` already exists, stop and tell the user — pick a
different name, or (if they meant to resume that one) just start/relay it with
Steps 4–5 instead of overwriting. `<workspace>` below means the resolved path.

## Step 2 — Infer the full goal.md

From the one-sentence description, fill every section by reasoning — do not
ask (except the verification question from the hard gate, if it fires):

- **Objective** — restate as a quantitative end-state where possible.
- **Hard Constraints** — only what the description actually implies;
  otherwise `None`.
- **Verification → objective** — the concrete check from the hard gate
  above (command + parsing + pass/fail convention). If the check is
  long-running (benchmark, training, integration test), that's fine — the
  Runner waits for it to finish within one attempt; just make sure the
  command blocks until the result is ready and then signals pass vs fail.
- **Verification → constraints** — a concrete check per constraint, or
  "No constraints to verify".
- **Environment & Tools** — reverse-engineered from the verification
  command (the tools/paths/credentials it needs). If something is genuinely
  required but unknown, note it as an assumption in this section rather than
  blocking.
- **Initial Context** — omit unless the description carries background worth
  passing to the first Runner.

Use this exact section structure:

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

Optional flags: `--agent claude|codex` to select the worker (default Claude),
`--model <id>` to pin its model, `--interval <secs>` to change pacing between
attempts (default 30s), and `--mode auto|copilot` to choose self-pacing vs.
per-attempt human approval (default `auto`). These can also be set in
`<workspace>/config.yaml` (flat keys `agent` / `model` / `interval` / `mode`);
CLI flags override the file.

Tell the user it's running detached and how to steer, since you inferred the
goal rather than interviewing for it:

- It runs in the background independent of this session; watch with
  `goaloop status <name>` or `tail -f <workspace>/.goaloop/orchestrator.log`.
- **The inferred goal.md is the steering wheel and is mutable mid-run.** If
  the inference was off, edit `<workspace>/goal.md` (esp. Verification) — the
  next attempt picks it up. For a transient nudge, run
  `goaloop suggest <name> "your note"` (or `--file <path>`) — it writes the
  correctly-numbered `suggestions/NNN.md` for you (handling the running-attempt
  and retry cases), so a mis-numbered note can't silently sit unread. Halt with
  `goaloop stop <name>`.

## Step 5 — Relay progress

Read these files to report status (do not infer from anything else):

- `<workspace>/.goaloop/status.txt` — the current one-line state (e.g.
  `attempt 003: running`, `attempt 003: advanced — next attempt in 30s`,
  `attempt 004: PASS — goal met. Loop done.`).
- `<workspace>/.goaloop/attempt_complete.json` — the last completed attempt's
  `{attempt, status, cost_usd}`.
- `<workspace>/attempts/NNN.md` — the latest attempt record, for what was
  tried and observed.

When the user asks "how's it going?", read these and summarize the latest
attempt(s).

### Auto-relay each completed round (don't poll)

Rounds take minutes to hours, so don't sit in a polling loop or a
`ScheduleWakeup`/`/loop` cadence — that re-reads your whole context every tick
for nothing. Instead arm a **persistent Monitor** on the workspace's own
signals; it fires a `<task-notification>` that wakes you the instant a round
lands, at ~zero context cost until then:

```bash
ws=~/.goaloop/<name>
prev=$(ls -1 "$ws/attempts/" 2>/dev/null | grep -c '\.md$' || echo 0)
while true; do
  cur=$(ls -1 "$ws/attempts/" 2>/dev/null | grep -c '\.md$' || echo 0)
  if [ "$cur" -gt "$prev" ]; then
    echo "ROUND_COMPLETE: attempts/ now has $cur record(s)"; prev=$cur
  fi
  pgrep -f "goaloop run.*<name>" >/dev/null 2>&1 || { echo "ORCHESTRATOR_STOPPED"; break; }
  sleep 60
done
```

Run it with the Monitor tool, `persistent: true`. A new `attempts/NNN.md` is a
clean round boundary — an `in_progress`/quota pause writes none, so the Monitor
only fires on a genuinely completed attempt (or when the orchestrator exits:
`pass`/`blocked`/`error`/stopped). On each fire, read the new `NNN.md` and relay
**concisely** (status + key metrics), then handle the terminal cases below.

Notes:
- This only works while *this* interactive session stays alive to receive the
  notification — it's not for a fire-and-exit invocation.
- Honor opt-out: if the user only wanted a one-off status, or said don't notify,
  skip the Monitor and just answer on demand with `goaloop status` + the files
  above. If they later say "stop notifying," `TaskStop` the Monitor.
- A long `ScheduleWakeup` (≥20 min) is fine as a *fallback* heartbeat if you want
  belt-and-suspenders, but the Monitor is the primary signal — don't poll fast.

Most statuses are transient — `running`, `advanced — next attempt in …`,
`in_progress — waiting …`, and `quota hit — sleeping …` all mean **still
working**; just relay that and summarize the latest attempt. The cases below
are the ones where you act.

### When the goal is met

Status shows `PASS — goal met. Loop done.` and the process has exited
(`goaloop status` shows `NOT RUNNING`). Tell the user **DONE**, quote the
`verification` summary from the latest `attempts/NNN.md`, and stop.

### When the Runner is blocked

Status shows the attempt **blocked** and the process has exited. The Runner
judged it cannot reach `pass` and another advance won't help — it needs a
human. Read the latest `attempts/NNN.md` and the `reason`, tell the user the
Runner is blocked and quote the reason, and let them decide (edit `goal.md`,
change the environment, etc.). Do not auto-restart.

### When it errors out

Status shows an **error** give-up and the process has exited. The orchestrator
retried a crash / malformed terminator / transient error up to its bound and
gave up (infrastructure, not a goal decision). Read the latest `attempts/NNN.md`
and the `orchestrator.log` tail, tell the user what went wrong, and let them
decide. Do not auto-restart.

### Copilot mode (per-attempt approval)

If the run was started with `--mode copilot` (or `config.yaml` has
`mode: copilot`, or the status shows it awaiting approval), the orchestrator
pauses after each `advanced` attempt and waits for human approval before the
next one. Relay the just-finished attempt to the user; on their go-ahead,
release the next attempt:

```bash
goaloop continue <name>
```

(`pass`/`blocked`/`error` are terminal and `in_progress` resumes on its own —
only `advanced` waits for `goaloop continue`.)

## Changing direction or stopping

The orchestrator has no conversation channel to the Runner — there are two
durable guidance channels, by intent serving different purposes.

- **Permanent change / amend the goal**: edit `~/.goaloop/<name>/goal.md`
  (or a file it references, like a rubric). The next attempt's Runner reads
  the updated spec naturally — no relay needed. Propose the edit, make it on
  the user's confirmation; no restart required.
- **Transient per-attempt note**: the Runner of attempt NNN reads
  `~/.goaloop/<name>/suggestions/NNN.md` at the start of that attempt.
  **Simplest — let the CLI number it: `goaloop suggest <name> "your note"`**
  (or `--file <path>`) writes the correctly-numbered file(s) for you, including
  the running-attempt/retry case, so a mis-numbered note can't silently sit
  unread. Use this for one-off nudges (e.g. dropped while AFK); `goal.md` is
  for changes that should persist. (If the loop has already moved past the
  round you targeted, the note just sits unread — re-point it at the new next
  attempt.)
- **Stop the orchestrator**: `goaloop stop <name>` (sends SIGTERM; it exits
  after the in-flight attempt's process settles).

## What you MUST NOT do

- Do not fabricate a verification to avoid asking. If it isn't concrete, stop
  and ask the single verification question (the hard gate above).
- Do not run a question-at-a-time interview. The only thing you may ask is
  that single verification question when the gate fails — everything else is
  inferred.
- Once running, do not run Verification, modify the workspace (except
  `goal.md` edits the user approves), or write `memory/` or `attempts/` — the
  Runner owns those.
- Do not spawn a `goal-runner` subagent via the `Agent` tool — the `goaloop`
  orchestrator drives the configured headless Runner; there is no subagent path.
- Do not call `ScheduleWakeup` fast or wrap this in `/loop` — the background
  orchestrator paces itself; use the persistent Monitor above to auto-relay.
