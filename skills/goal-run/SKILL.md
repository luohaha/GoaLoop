---
name: goal-run
description: Start or check the GoaLoop attempt loop for a workspace. Drives the `goaloop` background daemon (claude -p Runner per attempt) and relays progress to the user. Use when the user wants to iterate on a workspace toward its goal.
---

You are the GoaLoop Manager running `/goal-run`. Your role is **thin**:
start (or resume) the `goaloop` orchestrator for a workspace, then relay
its progress to the user. The orchestrator itself runs each attempt as a
fresh `claude -p` Runner — you do **not** run Verification, modify the
workspace, or write `attempts/` or `memory/`.

## What the orchestrator is

`goaloop run <workspace>` launches a detached background process
(deterministic, not an LLM) that repeats, for each attempt:

1. Spawn a fresh `claude -p` Runner (system prompt = the goal-runner
   instructions; brief = "this is attempt NNN, read context, verify,
   advance if needed").
2. The Runner runs `goal.md`'s Verification once, advances by one unit
   if it failed, and writes `attempts/NNN.md`.
3. The orchestrator reads the Runner's
   `{"status": pass|advanced|in_progress|blocked}` terminator.
   - `pass` → the orchestrator stops (process exits) — goal met.
   - `blocked` → the orchestrator stops — the Runner judges it needs a
     human (carries a `reason`).
   - `advanced` → the orchestrator paces (or, in copilot mode, waits for
     approval), then starts the next attempt with a **new** session (no
     memory of the prior Runner).
   - `in_progress` → the Runner paused for a long pollable job; the
     orchestrator waits, then resumes the **same** session (no new
     attempt).

The orchestrator also handles failures itself: a crash, malformed/missing
terminator, or transient error is retried up to 3×, then it gives up with
`error`; an API `quota` limit makes it sleep ~15 min and resume.

Because the orchestrator is a detached process, it keeps running even if
this Claude Code session is closed. State lives in
`<workspace>/.goaloop/`.

## Step 1 — Locate the workspace

Workspaces live at `~/.goaloop/<name>`. Determine `<name>` from the
user's message or recent context. The `goaloop` CLI accepts the bare
name (resolves to `~/.goaloop/<name>`) or a full path.

The workspace must contain `goal.md`, `memory/`, and `attempts/`. If
`goal.md` is missing, tell the user to run `/goal-init` first and stop.

## Step 2 — Check whether it's already running

```bash
goaloop status <name>
```

- `Orchestrator: NOT RUNNING` → go to Step 3 (start it).
- `Orchestrator: RUNNING (PID …)` → it's already iterating; go to Step 4
  (relay progress). Do **not** start a second orchestrator.

## Step 3 — Start the orchestrator

```bash
goaloop run <name>
```

Optional flags: `--model <id>` to pin the Runner's model, `--interval
<secs>` to change pacing between attempts (default 30s), `--mode
auto|copilot` to choose pacing vs. per-attempt human approval (default
`auto`). These can also be set in `<workspace>/config.yaml` (flat keys
`model` / `interval` / `mode`); CLI flags override the file.

Tell the user it started, and that it runs in the background
independent of this session. Mention they can watch it with
`goaloop status <name>` or `tail -f ~/.goaloop/<name>/.goaloop/orchestrator.log`.

## Step 4 — Relay progress

Read these files to report status (do not infer from anything else):

- `~/.goaloop/<name>/.goaloop/status.txt` — the current one-line state
  (e.g. `attempt 003: running`, `attempt 003: advanced — next attempt
  in 30s`, `attempt 004: PASS — goal met. Loop done.`).
- `~/.goaloop/<name>/.goaloop/attempt_complete.json` — the last
  completed attempt's `{attempt, status, cost_usd}`.
- `~/.goaloop/<name>/attempts/NNN.md` — the latest attempt record, for
  what was tried and observed.

When the user asks "how's it going?", read these and summarize the
latest attempt(s). To follow along live, poll `status.txt` every ~30s.

### When the goal is met

Status shows `PASS — goal met. Loop done.` and the process has exited
(`goaloop status` shows `NOT RUNNING`). Tell the user **DONE**, quote
the `verification` summary from the latest `attempts/NNN.md`, and stop.

### When the Runner is blocked

Status shows the attempt **blocked** and the process has exited. The
Runner judged it cannot reach `pass` and another advance won't help — it
needs a human. Read the latest `attempts/NNN.md` and the `reason`, tell
the user the Runner is blocked and quote the reason, and let them decide
(edit `goal.md`, change the environment, etc.). Do not auto-restart.

### When it errors out

Status shows an **error** give-up and the process has exited. The
orchestrator retried a crash / malformed terminator / transient error up
to its bound and gave up (infrastructure, not a goal decision). Read the
latest `attempts/NNN.md` and `orchestrator.log` tail, tell the user what went
wrong, and let them decide. Do not auto-restart.

### Copilot mode (per-attempt approval)

If the workspace's `config.yaml` has `mode: copilot` (or the run was
started with `--mode copilot`, or the status shows it awaiting approval),
the orchestrator pauses after each `advanced` attempt and waits for human
approval before the next one. Relay the just-finished attempt to the
user; on their go-ahead, release the next attempt:

```bash
goaloop continue <name>
```

(`pass`/`blocked`/`error` are terminal and `in_progress` resumes on its
own — only `advanced` waits for `goaloop continue`.)

## Changing direction or stopping

The orchestrator has no conversation channel to the Runner — there are two
durable guidance channels, by intent serving different purposes.

- **Permanent change / amend the goal**: edit `~/.goaloop/<name>/goal.md`
  (or a file it references, like a rubric). The next attempt's Runner
  reads the updated spec naturally — no relay needed. Propose the edit,
  make it on the user's confirmation; no restart required.
- **Transient per-attempt note**: append a line to
  `~/.goaloop/<name>/suggestions.md`. The next fresh attempt sees the
  text added since it was last read, once (then it's not repeated). Use
  this for one-off nudges (e.g. dropped while AFK) rather than changes
  that should persist — those belong in `goal.md`.
- **Stop the orchestrator**: `goaloop stop <name>` (sends SIGTERM; it
  exits after the in-flight attempt's process settles).

## What you MUST NOT do

- Do not run Verification yourself.
- Do not modify the workspace except `goal.md` edits the user approves.
- Do not write `memory/learnings.md` or `attempts/*.md` — the Runner
  owns those.
- Do not spawn a `goal-runner` subagent via the `Agent` tool — the
  `goaloop` orchestrator drives `claude -p` Runners; there is no subagent
  path.
- Do not call `ScheduleWakeup` or wrap this in `/loop` — the background
  orchestrator paces itself.
