---
name: goal-init
description: Initialize a new GoaLoop workspace by interviewing the user and writing a goal.md. Use when the user wants to start a new goal-driven iteration task.
---

You are running `/goal-init` for GoaLoop. Interview the user and produce
a valid `goal.md` plus the supporting directory structure.

## Workspace location

Workspaces always live under `~/.goaloop/<workspace_name>`. You never
ask for a path — only for the workspace name. Once you know the name,
the working directory is fixed at `~/.goaloop/<workspace_name>`.
Everywhere below, `<workspace>` means that resolved path.

## Outcome

When you finish, the workspace directory contains:
- `<workspace>/goal.md` — the Goal specification
- `<workspace>/memory/` — empty directory (Runner creates `learnings.md`
  on first write)
- `<workspace>/attempts/` — empty directory

## Interview rules

- **Ask one question at a time.** Wait for the user's answer before
  moving on. Do not batch.
- **Be strict on verification.** If the user can't give a concrete
  procedure to check the objective or a constraint, do NOT fill in
  placeholder language. Stop, explain why we need this, and offer to
  refine the objective until it's verifiable. Acceptable:
  "run `./scripts/foo.sh` and check exit code", "parse `metrics.p99`
  from result.json and compare against 5.0", "score `draft.md`
  against `rubric.md` and check ≥ 8.0". Unacceptable: "the agent
  looks at it and decides".
- Quote the user's answers back briefly to confirm understanding,
  especially after steps 4 and 5.

## Interview script

Ask these in order, one at a time:

1. **Workspace name.** "What should I call this workspace? Give me a
   short name (the project name works well). The workspace will live at
   `~/.goaloop/<name>`." Resolve `<workspace>` to that path. If it
   already exists, it must not already contain a `goal.md` — if it
   does, stop and tell the user.

2. **Objective.** "In one sentence, what is the end state you want to
   reach? Make it quantitative if at all possible — a number to hit, a
   test to pass, a score threshold."

3. **Hard Constraints.** "What absolutely cannot change or degrade
   while pursuing this objective? List as many as apply, or say
   'none'."

4. **Verification of objective.** "How will we know the objective is
   met? Give me a concrete check — a command to run plus how to
   interpret its output. If the check is long-running (benchmark,
   training, integration test taking hours), tell me how it signals
   **pending** vs done — typically a re-entrant script that returns
   exit code 2 while still running and exit 0/1 when complete."

5. **Verification of each constraint.** For each constraint from step
   3: "How will we check this constraint?"

6. **Environment & Tools.** "What does running the verification require?
   - CLI tools (ssh, jq, kubectl, mdformat, …)?
   - Credentials and where they live (SSH config, API tokens, …)?
   - External system access (clusters, APIs, databases, …)?
   - File paths (scripts, source code, datasets, rubrics, …)?
   - Preconditions (services running, dependencies installed, …)?"

7. **Initial Context (optional).** "Anything else the Runner should
   know on its first invocation? Background, recent findings, source
   code locations, prior experience? Or 'none'."

## Writing goal.md

After the interview, assemble `<workspace>/goal.md` with this exact
section structure:

```markdown
# Goal

## Objective
<step 2>

## Hard Constraints
- <step 3 item 1>
- <step 3 item 2>
(or write "None" on its own line if step 3 was empty)

## Verification

### How to verify the objective
<step 4 — be specific about the command, the parsing, and the
pass/fail/pending convention>

### How to verify each constraint
- <constraint 1>: <check from step 5>
- ...
(or "No constraints to verify" if step 3 was empty)

### Environment & Tools
<step 6, as a bulleted list>

## Initial Context
<step 7>
(omit this whole section if step 7 was 'none')
```

Then create directories and write the file:

```bash
mkdir -p <workspace>/memory <workspace>/attempts
# write goal.md
```

## Confirmation

After writing, tell the user:
- The workspace path
- That `goal.md` was written (and show its content for review)
- That they can now run `/goal-run`, which iterates automatically —
  it self-schedules each next attempt until the goal is met (press
  Esc to stop early); no `/loop` wrapper needed

Encourage them to read and tweak `goal.md` before kicking off — it's
the load-bearing artifact of the whole framework.
