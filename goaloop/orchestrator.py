"""The GoaLoop Orchestrator — the background process that drives attempts.

Runs the attempt loop: spawns a fresh `claude -p` Runner over a workspace
each attempt until the Runner's Verification passes, the Runner reports it's
blocked, the human stops it, or unrecoverable errors are exhausted. The
Orchestrator is deliberately dumb (Ralph-loop spirit): all authoritative
state is the workspace on disk (`goal.md`, `memory/learnings.md`,
`attempts/NNN.md`). It keeps only a tiny checkpoint so a crashed/quota-paused
attempt can resume the same session instead of restarting from scratch.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Callable

from .adapter import ClaudeAdapter, QuotaExhausted, TransientError

# Consecutive non-recoverable failures (malformed terminator, missing
# attempt record, generic crash) before the loop gives up. Quota/transient
# pauses are external and do NOT count toward this.
MAX_CONSECUTIVE_FAILURES = 3

# Cool-down after an API quota hit. Anthropic limits reset on ~hourly
# boundaries, so retrying every ~15 min recovers without hammering.
QUOTA_RETRY_SECS = 900

# Short backoff before resuming a session after a transient network error.
TRANSIENT_RETRY_SECS = 10
TRANSIENT_MAX_RETRIES = 3

# When a Runner pauses mid-attempt (`in_progress`) to wait out a long
# job it would otherwise poll in a live turn, it tells us how long to
# wait. Cap it so a too-optimistic hint can't starve forward progress;
# fall back to this if the hint is missing/invalid. The process exits
# during the wait (zero tokens), then we --resume the same session.
IN_PROGRESS_MAX_SECS = 7200
IN_PROGRESS_FALLBACK_SECS = 300

# Prompt sent on every resume (in_progress pause, or crash/transient/quota
# interruption) instead of the full brief — the session already holds the
# brief + all context, so re-sending would just re-bill input tokens. One
# word is enough: it's non-empty (all `--resume` requires) and the session
# transcript tells the Runner where it left off.
_CONTINUE_PROMPT = "Continue."

_ATTEMPT_RE = re.compile(r"^(\d{3})\.md$")
# Last JSON object in the Runner's text that carries a "status" field.
_STATUS_RE = re.compile(r"\{[^{}]*\"status\"[^{}]*\}")


class Orchestrator:
    """Runs attempts until the Runner reports `pass`/`blocked`, errors are
    exhausted, or the human stops it."""

    def __init__(
        self,
        workspace: Path,
        model: str | None = None,
        interval: int = 30,
        mode: str = "auto",
        log: Callable[[str], None] = print,
    ):
        self.ws = workspace.resolve()
        self.model = model
        self.interval = interval  # pacing between successful `advanced` attempts
        self.mode = mode  # "auto" | "copilot" (pause for approval each attempt)
        self.log = log

        self.state_dir = self.ws / ".goaloop"
        self.attempts_dir = self.ws / "attempts"
        self.checkpoint_path = self.state_dir / "state.json"
        self.status_path = self.state_dir / "status.txt"
        self.complete_path = self.state_dir / "attempt_complete.json"
        self.continue_path = self.state_dir / "continue.json"
        # Async human feedback channel (optional). The human appends notes to
        # suggestions.md; we inject anything past the cursor as NEW into the
        # next fresh attempt's brief, then advance the cursor.
        self.suggestions_path = self.ws / "suggestions.md"
        self.cursor_path = self.state_dir / "suggestions.cursor"

        self.adapter = ClaudeAdapter(
            cwd=str(self.ws),
            system_prompt=_runner_system_prompt(),
            model=model,
            log=log,
        )

    # ---- workspace state helpers -------------------------------------

    def _next_attempt_number(self) -> int:
        """Highest NNN in attempts/ + 1 (recomputed from disk every loop).

        Because the Runner writes `attempts/NNN.md` only at the end of a
        completed attempt, a crash mid-attempt leaves the number unchanged
        and the next pass naturally retries the same number. State in files.
        """
        highest = 0
        if self.attempts_dir.is_dir():
            for p in self.attempts_dir.iterdir():
                m = _ATTEMPT_RE.match(p.name)
                if m:
                    highest = max(highest, int(m.group(1)))
        return highest + 1

    def _goal_mtime(self) -> float | None:
        """mtime of goal.md (or None if missing).

        Travels with the active session in the checkpoint so a restart can
        tell whether the goal was edited since the session started — a resumed
        session only gets "Continue." and never re-reads goal.md, so resuming
        across a goal change would silently run the OLD goal.
        """
        try:
            return (self.ws / "goal.md").stat().st_mtime
        except OSError:
            return None

    def _load_checkpoint(self) -> dict:
        if self.checkpoint_path.exists():
            try:
                return json.loads(self.checkpoint_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_checkpoint(self, **kw) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path.write_text(json.dumps(kw, indent=2))

    def _set_status(self, msg: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(msg + "\n")
        self.log(f"[orchestrator] {msg}")

    def _mark_complete(self, attempt: int, status: str, cost: float | None) -> None:
        self.complete_path.write_text(json.dumps(
            {"attempt": attempt, "status": status, "cost_usd": cost}, indent=2,
        ))

    def _end_error(self, attempt: int, reason: str) -> None:
        """Terminal give-up — the loop's `error` counterpart to `pass`.

        Used when an unrecoverable situation has exhausted its bounded
        retries (repeated crashes, malformed output, or transient API
        errors). Exits the loop cleanly with a clear `error` status and
        clears the active session so the next `goaloop run` starts fresh
        rather than resuming the broken one. (Quota is NOT routed here — it
        is an external clock the loop waits out indefinitely.)
        """
        self._set_status(f"attempt {attempt:03d}: ERROR — {reason}. Loop ended.")
        self._mark_complete(attempt, "error", None)
        self._save_checkpoint()  # clear active session

    def _suggestions_section(self) -> str:
        """Build the NEW-since-cursor block from suggestions.md, then advance
        the cursor (these notes are now delivered into a session).

        Returns "" when there's no suggestions.md or nothing new. Only NEW
        text is injected — older notes stay in the file for the human; the
        Runner can read it directly if it wants history. goal.md remains the
        channel for permanent/structural guidance; suggestions.md is for
        transient per-attempt notes (e.g. left while AFK).
        """
        if not self.suggestions_path.exists():
            return ""
        content = self.suggestions_path.read_text()
        if not content.strip():
            return ""
        cursor = 0
        if self.cursor_path.exists():
            try:
                cursor = int(self.cursor_path.read_text().strip())
            except ValueError:
                cursor = 0
        cursor = max(0, min(cursor, len(content)))
        new = content[cursor:].strip()
        if not new:
            return ""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(str(len(content)))  # delivered — advance
        return f"\n## Human guidance (NEW — address this attempt)\n\n{new}\n"

    def _wait_for_continue(self, n: int) -> None:
        """Copilot mode: block until the human approves the next attempt.

        Polls for `.goaloop/continue.json` (written by `goaloop continue`).
        The process stays alive while waiting; SIGTERM still stops it.
        """
        self.continue_path.unlink(missing_ok=True)  # ignore any stale signal
        self._set_status(
            f"attempt {n:03d}: advanced — awaiting approval (copilot mode). "
            f"Run `goaloop continue <name>` to start the next attempt."
        )
        while not self.continue_path.exists():
            time.sleep(5)
        self.continue_path.unlink(missing_ok=True)
        self.log("[orchestrator] approval received — continuing")

    def _build_brief(self, n: int) -> str:
        guidance = self._suggestions_section()
        return f"""Workspace: {self.ws}
This is attempt {n:03d}; write your attempt record to attempts/{n:03d}.md.
{guidance}
Run one attempt per your system-prompt workflow, then end your final message
with a single line that is exactly one of these JSON objects:
  {{"status": "pass", "verification": "<one-line summary>"}}
  {{"status": "advanced", "summary": "<one paragraph>"}}
  {{"status": "in_progress", "wait_secs": <int>, "note": "<what you're waiting on>"}}
  {{"status": "blocked", "reason": "<why you're stuck; what a human must resolve>"}}
"""

    # ---- the loop ----------------------------------------------------

    def run(self) -> None:
        cp = self._load_checkpoint()
        # If a prior process died mid-attempt, resume that session — UNLESS
        # goal.md was edited since the session started. A resumed session only
        # gets "Continue." and never re-reads goal.md, so resuming across a
        # goal change would silently keep running the OLD goal (it did, once).
        # When the goal moved, drop the resume and start a fresh attempt that
        # reads the new goal.
        resume_session: str | None = cp.get("active_session_id")
        active_goal_mtime: float | None = cp.get("goal_mtime")
        if resume_session and active_goal_mtime != self._goal_mtime():
            self.log(
                f"[orchestrator] goal.md changed since session "
                f"{resume_session[:8]} started — starting fresh, not resuming"
            )
            resume_session = None
            active_goal_mtime = None
        consecutive_failures = 0

        while True:
            n = self._next_attempt_number()

            if resume_session:
                session_id, resume = resume_session, True
                prompt = _CONTINUE_PROMPT  # session already holds the brief + context
                self.log(f"[orchestrator] attempt {n:03d}: resuming session {session_id[:8]}")
            else:
                session_id, resume = str(uuid.uuid4()), False
                active_goal_mtime = self._goal_mtime()  # pin goal version to this session
                prompt = self._build_brief(n)
            # Persist the active session BEFORE the call so a crash leaves a
            # recoverable breadcrumb. goal_mtime travels with it so a restart
            # can detect a goal edit and refuse to resume a now-stale session.
            self._save_checkpoint(
                active_session_id=session_id, attempt=n, goal_mtime=active_goal_mtime,
            )
            self._set_status(f"attempt {n:03d}: running")

            try:
                result = self.adapter.run(prompt, session_id, resume)
            except QuotaExhausted as e:
                self._set_status(
                    f"attempt {n:03d}: quota hit — sleeping "
                    f"{QUOTA_RETRY_SECS // 60} min, then resuming. ({e})"
                )
                self._mark_complete(n, "quota_paused", None)
                time.sleep(QUOTA_RETRY_SECS)
                resume_session = session_id
                continue
            except TransientError as e:
                resume_session = e.session_id
                retries = cp.get("transient_retries", 0) + 1
                cp["transient_retries"] = retries
                if retries > TRANSIENT_MAX_RETRIES:
                    self._end_error(n, f"transient API errors exhausted "
                                       f"({TRANSIENT_MAX_RETRIES} retries)")
                    return
                self._set_status(
                    f"attempt {n:03d}: transient error (retry {retries}) — "
                    f"resuming in {TRANSIENT_RETRY_SECS}s"
                )
                time.sleep(TRANSIENT_RETRY_SECS)
                continue
            except Exception as e:  # noqa: BLE001 — record and bound retries
                consecutive_failures += 1
                self._set_status(f"attempt {n:03d}: FAILED ({e})")
                self._mark_complete(n, "error", None)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self._end_error(n, f"{consecutive_failures} consecutive failures "
                                       f"(last: {e})")
                    return
                resume_session = session_id
                continue

            cp["transient_retries"] = 0
            term = _parse_terminator(result.text)
            status = term.get("status") if term else None

            if status == "pass":
                self._set_status(f"attempt {n:03d}: PASS — goal met. Loop done.")
                self._mark_complete(n, "pass", result.cost_usd)
                self._save_checkpoint()  # clear active session
                return

            # blocked — the Runner judges it cannot reach pass AND another
            # advance won't help (stuck, needs human). Terminal, like pass,
            # but a non-success outcome. Distinct from `error`: blocked is the
            # Runner's judgment, error is an infra failure the loop detected.
            if status == "blocked":
                reason = str(term.get("reason") or term.get("note")
                             or "(no reason given)").strip()
                self._set_status(
                    f"attempt {n:03d}: BLOCKED — {reason}. Loop ended (needs human)."
                )
                self._mark_complete(n, "blocked", result.cost_usd)
                self._save_checkpoint()  # clear active session
                return

            # in_progress — the Runner deliberately paused to wait out a long
            # job. Keep the attempt number AND the session; exit during the
            # wait (zero tokens), then --resume the same session to check on
            # it. This does NOT count as a failure or an advance.
            #
            # Two ways the Runner can signal this: the explicit
            # {"status":"in_progress"} terminator, OR a ScheduleWakeup tool
            # call — the way it naturally pauses in the interactive harness.
            # Honoring the tool call means a Runner that kicks off a long job
            # and schedules a wakeup but forgets the terminator line is still
            # paused + resumed, not killed as a "malformed" attempt (the
            # failure mode that ends an otherwise-healthy run).
            wake_secs = result.requested_resume_secs
            if status == "in_progress" or (status is None and wake_secs):
                if status == "in_progress":
                    hint = term.get("wait_secs")
                    if not (isinstance(hint, (int, float)) and hint > 0):
                        hint = wake_secs  # fall back to the ScheduleWakeup delay
                    note = str(term.get("note", "")).strip()
                else:
                    hint = wake_secs
                    note = "paused via ScheduleWakeup (no terminator line)"
                wait = (min(int(hint), IN_PROGRESS_MAX_SECS)
                        if isinstance(hint, (int, float)) and hint > 0
                        else IN_PROGRESS_FALLBACK_SECS)
                self._mark_complete(n, "in_progress", result.cost_usd)
                self._set_status(
                    f"attempt {n:03d}: in_progress — waiting {wait}s before "
                    f"resuming same session." + (f" ({note})" if note else "")
                )
                consecutive_failures = 0
                time.sleep(wait)
                resume_session = session_id
                continue

            # advanced — verify the Runner actually recorded the attempt.
            if not (self.attempts_dir / f"{n:03d}.md").exists() or status != "advanced":
                consecutive_failures += 1
                reason = "no terminator" if status is None else (
                    "missing attempt record" if status == "advanced" else f"status={status}"
                )
                self._set_status(f"attempt {n:03d}: malformed ({reason})")
                self._mark_complete(n, "no_output", result.cost_usd)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self._end_error(n, f"{consecutive_failures} consecutive malformed "
                                       f"attempts (last: {reason})")
                    return
                resume_session = session_id
                continue

            # Clean advance: next attempt starts fresh (no memory of this one).
            consecutive_failures = 0
            resume_session = None
            self._save_checkpoint()  # clear active session
            self._mark_complete(n, "advanced", result.cost_usd)
            if self.mode == "copilot":
                self._wait_for_continue(n)  # block for human approval
            else:
                self._set_status(f"attempt {n:03d}: advanced — next attempt in {self.interval}s")
                time.sleep(self.interval)


_VALID_STATUSES = ("pass", "advanced", "in_progress", "blocked")


def _parse_terminator(text: str) -> dict | None:
    """Return the Runner's terminator object (with `status` + any fields).

    Prefer the last standalone JSON line (what the brief asks for, robust to
    braces inside the summary text); fall back to scanning for any flat
    `{...status...}` object if the Runner wrapped it differently. Returns the
    whole dict so callers can read `wait_secs` etc., or None if absent.
    """
    for line in reversed((text or "").splitlines()):
        line = line.strip().strip("`").strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("status") in _VALID_STATUSES:
            return obj

    for chunk in reversed(_STATUS_RE.findall(text or "")):
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("status") in _VALID_STATUSES:
            return obj
    return None


def _runner_system_prompt() -> str:
    """The Runner instructions, used as --append-system-prompt.

    Single source of truth is `agents/goal-runner.md` in the repo, shipped
    inside the package at `resources/agents/goal-runner.md` (a symlink in the
    source tree, a real copy in the installed wheel) so an installed `goaloop`
    is self-contained. Its YAML frontmatter is stripped. Override with
    GOALOOP_RUNNER_PROMPT to point elsewhere.
    """
    import os

    override = os.environ.get("GOALOOP_RUNNER_PROMPT")
    path = Path(override) if override else (
        Path(__file__).resolve().parent / "resources" / "agents" / "goal-runner.md"
    )
    text = path.read_text()
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:]
    return text.strip()
