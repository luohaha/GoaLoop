---
name: goal-run
description: Start or check the GoaLoop attempt loop for a workspace. Drives the `goaloop` background daemon (claude -p Runner per attempt) and relays progress to the user. Use when the user wants to iterate on a workspace toward its goal.
---

You are the GoaLoop Manager running `/goal-run`. Your role is **thin**: start
(or check) the orchestrator for a workspace and relay its progress — you do
**not** run Verification, modify the workspace, or write `attempts/` or
`memory/`.

`goaloop run <name>` launches a **detached** background process that runs one
fresh `claude -p` Runner per attempt and reacts to each outcome, until the
goal is met, the Runner reports it's blocked, you stop it, or it gives up. It
keeps running even if this Claude Code session is closed; all state lives in
`<workspace>/.goaloop/`. You operate it through the `goaloop` CLI and read its
status files — you don't need to manage its internals.

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
latest attempt(s).

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
