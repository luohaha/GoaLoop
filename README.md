# GoaLoop

> A goal-driven multi-attempt iteration framework that runs on a
> Claude Code subscription session.

GoaLoop turns "iterate until the target is met" into a small, sharp
protocol on top of Claude Code. You write a `goal.md` that spells out
what "done" looks like and how to verify it. GoaLoop then runs a thin
**Manager** in your main CC session that spawns fresh-context
**Runner** subagents per attempt, until the verification passes or
you stop the loop.

The framework is two skill files and one subagent definition. There is
no daemon, no subprocess pool, no TUI, no Python orchestrator.

## Why

Many software engineering tasks share the same shape — define a
target, iterate, verify, repeat:

- Performance optimization
- Flaky test reduction
- Build-time optimization
- ML hyperparameter or model tuning
- Writing iteration (until a rubric passes)
- Cost optimization
- Security hardening

Existing tools for this pattern either run agents as subprocesses
(paying API rates per token) or bake in domain assumptions like
"artifact = GitHub PR" and "isolation = git worktree". GoaLoop does
neither. The runtime is your Claude Code subscription session plus
subagent calls; the framework makes no domain assumptions.

See [`docs/design.md`](docs/design.md) for the full design and
rationale.

## Architecture in one picture

```
Your Claude Code session (Manager)
  │
  │  /goal-init  → interviews you, writes goal.md
  │  /goal-run   → does one attempt:
  │     │
  │     ▼
  │   Spawn goal-runner subagent (fresh context per attempt)
  │     │
  │     │  reads goal.md, learnings.md, recent attempts/
  │     │  runs the Verification procedure
  │     │  if fail → advances workspace by one unit
  │     │  writes attempts/NNN.md; optionally updates learnings.md
  │     │  returns JSON report
  │     ▼
  │   Manager: pass → stop; pending/advanced → ScheduleWakeup("/goal-run")
  │
  └── /goal-run self-schedules its next attempt — no /loop wrapper
```

## Install

GoaLoop is two skill files plus one custom subagent type. Two ways
to install:

**Option A — Local to one project (recommended for trying it out):**

```bash
git clone <repo-url> ~/GoaLoop
cd ~/your-working-project
mkdir -p .claude
ln -s ~/GoaLoop/skills .claude/skills
ln -s ~/GoaLoop/agents .claude/agents
```

**Option B — Globally for all CC sessions:**

```bash
git clone <repo-url> ~/GoaLoop
mkdir -p ~/.claude/skills ~/.claude/agents
cp -r ~/GoaLoop/skills/* ~/.claude/skills/
cp -r ~/GoaLoop/agents/* ~/.claude/agents/
```

Verify by opening Claude Code and typing `/goal-init` — it should be
recognized.

## Quickstart

```
> /goal-init
```

Claude interviews you with seven questions: workspace path, objective,
hard constraints, how to verify the objective (concretely!), how to
verify each constraint, what environment/tools the verification needs,
and any initial context.

The interview is **strict** about getting concrete verification — if
you can't articulate a real check, GoaLoop refuses to write the
`goal.md`. That refusal is the point: a goal you can't verify is a
goal you can't reach.

When the interview completes, your workspace looks like:

```
<workspace>/
├── goal.md           # the spec — edit it mid-run if you want
├── memory/           # Runner-curated knowledge accumulates here
└── attempts/         # one file per attempt, write-once audit trail
```

Then start it:

```
> /goal-run                  # self-paces until pass — no /loop needed
```

## Running

GoaLoop runs as a single self-driven loop. There is no manual one-shot
mode in v0.1, and no outer `/loop` wrapper: on `advanced` or `pending`
`/goal-run` schedules its own next attempt via
`ScheduleWakeup(prompt: "/goal-run")`, and on `pass` it omits the
wakeup so the loop ends. You type `/goal-run` once and it keeps going.

Invocation:

```
> /goal-run
```

The loop terminates when:

- The Runner reports `pass`, the Manager therefore omits the next
  `ScheduleWakeup`, and the loop ends naturally.
- You press `Esc` (or close the session).
- The 7-day auto-expiry on scheduled wakeups fires.

You stay in control throughout: read what the Runner did after each
attempt (relayed by the Manager), drop suggestions into the
conversation (verbatim-relayed to the next Runner), or edit `goal.md`
to amend the target. To stop sooner than `pass`, press Esc.

> ⚠️ Don't wrap `/goal-run` in `/loop`. The skill already self-schedules
> via `ScheduleWakeup`; an interval-mode `/loop 5m /goal-run` could not
> be ended by the skill anyway. Just run `/goal-run`.

## Workspace contents

After running, the workspace looks like:

```
<workspace>/
├── goal.md
├── memory/
│   └── learnings.md      # ~4KB cap; Runner curates this
└── attempts/
    ├── 001.md            # one Markdown file per attempt
    ├── 002.md
    └── ...
```

- **`goal.md`** is the authoritative spec. Edit it mid-run to change
  the target or constraints — the next attempt picks it up.
- **`memory/learnings.md`** is the Runner's "textbook" — validated
  approaches, ruled-out hypotheses, surprising observations.
- **`attempts/NNN.md`** is the audit trail. Each Runner writes one
  and never modifies others. ~30 lines each.

## Design highlights

- **Verification is load-bearing.** The `goal.md` Verification section
  is a literal command/procedure, written by you at init time. The
  Runner executes it; never makes up a judgment.
- **Three-state verification.** `pass` / `fail` / `pending` —
  long-running checks (benchmarks, training) return `pending` while
  in flight, and the Manager schedules a wake-up to check back.
- **Anti-cheat by time.** Each Runner is a fresh subagent. The Runner
  in attempt N judges what attempt N−1 left behind, with no shared
  context. Even for LLM-as-judge verification, no nested subagent is
  needed — the time separation gives you arm's-length judging.
- **Honest about what the framework can enforce.** No budget caps, no
  forced attempt limits, no `goal.md` keys for things GoaLoop can't
  actually enforce. The only real termination paths are "Verification
  passes" and "human stops the loop".

## When NOT to use GoaLoop

- When you can't articulate a concrete verification procedure. If
  "what success looks like" is purely a human judgment call, the
  framework's load-bearing assumption breaks. Use direct conversation
  with Claude instead.
- When the iteration unit is sub-second. Manager+Runner per attempt
  has a few-second floor.
- When you need parallel exploration across independent hypotheses.
  GoaLoop runs one Runner at a time per Manager session. Multiple
  Claude Code sessions can run multiple workspaces in parallel, but
  there's no built-in coordination.

## Status

Pre-1.0. The design is documented in [`docs/design.md`](docs/design.md);
the skills and agent are implemented but not yet battle-tested across
diverse domains.

## License

Apache License 2.0. See [LICENSE](LICENSE).
