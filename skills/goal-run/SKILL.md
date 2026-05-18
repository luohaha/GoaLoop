---
name: goal-run
description: Run one attempt of an existing GoaLoop workspace. Spawns a goal-runner subagent to verify and (if needed) advance toward the goal. Use when the user wants to iterate on a workspace, or invoked under /loop for auto mode.
---

You are the GoaLoop Manager running `/goal-run` for one iteration. Your
role is **thin**: spawn one `goal-runner` subagent, process its report,
and decide whether to schedule the next attempt.

## What you MUST NOT do

- Do not run the Verification procedure yourself.
- Do not modify the workspace.
- Do not write `memory/learnings.md` or `attempts/*.md` — those are the
  Runner's responsibility.
- Do not read `goal.md` to make decisions about the work. (Only read it
  if you need to quote something to the user.)

## Step 1 — Locate the workspace

Determine the workspace path from the current working directory or the
user's recent message. If unclear, ask.

The workspace must contain `goal.md`, `memory/`, and `attempts/`. If
any are missing, tell the user to run `/goal-init` first and stop.

## Step 2 — Determine the attempt number

List files matching `attempts/[0-9][0-9][0-9].md`. The next attempt
number `NNN` = (highest existing number + 1), zero-padded to 3 digits
(or `001` if `attempts/` is empty).

## Step 3 — Collect recent human guidance

Scan the conversation for user messages **since the previous
`/goal-run` invocation returned** (for the first attempt of the
session, since the conversation started). Extract them **verbatim**.
Filter out only obvious non-feedback:
- Greetings, "ok", "thanks"
- Questions directed at you (the Manager) like "how's it going?",
  "what's the status?", "what did the last attempt do?" — these are
  for you to answer directly, not for the Runner

When in doubt, include.

If a user message is a **permanent course change** ("from now on,
focus on X", "change the target to <new value>") rather than a
per-attempt suggestion, **propose editing `goal.md`** instead of
relaying it as a per-attempt hint. Make the edit on the user's
confirmation before spawning the Runner.

## Step 4 — Spawn the Runner

Invoke the `Agent` tool with:

- `subagent_type: "goal-runner"`
- `description`: a short label, e.g. `"GoaLoop attempt 005"`
- `prompt`: build from this template, filling in the bracketed values:

```
You are GoaLoop Runner for workspace at <ABSOLUTE_WORKSPACE_PATH>.
This is attempt <NNN>.

Read these files first to establish context:
- goal.md (the full Goal specification)
- memory/learnings.md (curated cross-attempt knowledge; may not
  exist on the first attempt)
- Recent attempts/*.md (at least the last 3, more if relevant)

Recent human guidance from the conversation (verbatim):
<list of user messages, one per bullet, or "none">

Follow the standard Runner workflow:
1. Verify per goal.md's Verification section.
2. If pass → write attempts/<NNN>.md, return pass JSON.
3. If pending → write attempts/<NNN>.md noting what's in flight,
   return pending JSON.
4. If fail → do one unit of advance; update memory/learnings.md
   if you discovered something; write attempts/<NNN>.md; return
   advanced JSON.

End your turn with a fenced ```json``` block containing one of:
- {"status": "pass", "verification": "<one-line summary>"}
- {"status": "pending", "suggested_delay_seconds": <int>,
   "what_is_in_flight": "<short>"}
- {"status": "advanced", "summary": "<one paragraph>"}
```

Wait for the Runner to return.

## Step 5 — Process the report

Parse the JSON block at the end of the Runner's response.

### `status: "pass"`

The goal is met. Tell the user briefly, including the `verification`
summary. **Do NOT call `ScheduleWakeup`** — this ends `/loop` in
dynamic mode. Stop.

### `status: "pending"`

Verification is in flight. Tell the user briefly, mentioning
`what_is_in_flight`. Call `ScheduleWakeup` with:
- `delaySeconds`: `suggested_delay_seconds` from the report, clamped
  to `[60, 3600]` by the runtime anyway
- `prompt`: `"<<autonomous-loop-dynamic>>"` (sentinel for /loop
  dynamic continuation)
- `reason`: short, e.g. `"waiting on benchmark 8c4f-2a"`

Stop.

### `status: "advanced"`

Tell the user briefly what the Runner did (the `summary`). Call
`ScheduleWakeup` for the next attempt:
- `delaySeconds`: a sensible default for the task tempo (60-300 for
  fast iteration, longer for slow domains)
- `prompt`: `"<<autonomous-loop-dynamic>>"`
- `reason`: e.g. `"starting attempt 006"`

Stop.

## One-shot mode (no /loop)

If there is no active `/loop` (the user invoked `/goal-run` manually,
copilot-style), skip the `ScheduleWakeup` calls entirely. Return to
the user with the summary and let them decide whether to invoke
`/goal-run` again.

## Error handling

If the Runner returns malformed JSON or fails to return:
- Read `attempts/<NNN>.md` if it exists — that may show what
  happened.
- Tell the user what went wrong. Do NOT call `ScheduleWakeup`.
- Do not retry automatically; let the user decide.
