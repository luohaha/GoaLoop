"""Claude provider for the worker-agent abstraction.

Distilled from auto-perf-opt's ClaudeAdapter, keeping only what GoaLoop's
lean loop needs: spawn the process, parse the stream-json events for a
result + cost, and classify the two error families the loop reacts to
(quota vs. transient network). Everything auto-perf-opt's adapter does for
robustness at scale (binary-missing retry, oversized-line draining, async
stderr races) is dropped — GoaLoop runs one Runner at a time and can keep
the parsing synchronous.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import Callable, TextIO

from .agent import (
    AgentResult,
    QUOTA_RE as _QUOTA_RE,
    QuotaExhausted,
    SessionStarted,
    TRANSIENT_RE as _TRANSIENT_RE,
    TransientError,
)

# High-precision Claude Code usage-limit notice, used to scan the Runner's
# ASSISTANT text (not just result/stderr). When a turn is aborted mid-flight
# by the session/usage limit, Claude Code emits "You've hit your session limit
# · resets 3:10pm" as an assistant text block and the `result` field comes back
# empty — so a quota scan over result+stderr alone misses it and the loop
# misreads a recoverable pause as a "malformed" attempt. We can't scan
# assistant text with the broad _QUOTA_RE, because words like "overloaded" or
# "too many requests" legitimately appear in a Runner's own analysis; these two
# phrasings are specific to Claude Code's limit notice and won't.
_USAGE_LIMIT_RE = re.compile(
    r"hit your (?:\w+ )?limit|resets \d{1,2}(?::\d{2})?\s*[ap]m",
    re.IGNORECASE,
)


def _classify_streams(
    result_text: str, stderr_text: str, assistant_text: str, session_id: str
) -> Exception | None:
    """Map the child's output to a recoverable-error exception, or None.

    `result_text` + `stderr_text` get the full quota/transient patterns (these
    streams carry real API errors verbatim). `assistant_text` is scanned ONLY
    for the high-precision usage-limit notice (_USAGE_LIMIT_RE) — the one error
    Claude Code surfaces there rather than in `result` — so a broad scan of
    free-form analysis text can't misfire into a false quota/transient pause.
    """
    primary = "\n".join(s for s in (result_text, stderr_text) if s)
    if primary and _QUOTA_RE.search(primary):
        return QuotaExhausted(primary.strip()[:300])
    if primary and _TRANSIENT_RE.search(primary):
        return TransientError(primary.strip()[:300], session_id)
    if assistant_text and _USAGE_LIMIT_RE.search(assistant_text):
        # Surface the matched line, not the whole (possibly long) analysis.
        line = next(
            (ln for ln in assistant_text.splitlines() if _USAGE_LIMIT_RE.search(ln)),
            assistant_text,
        )
        return QuotaExhausted(line.strip()[:300])
    return None


# Backwards-compatible name for callers that imported the old concrete result.
ClaudeResult = AgentResult


class ClaudeAdapter:
    """Runs a single `claude -p` turn and returns its final result text.

    `system_prompt` is appended to Claude Code's default system prompt
    (`--append-system-prompt`) — GoaLoop passes the Runner instructions
    here. `cwd` is where the Runner operates (the workspace).
    """

    provider = "claude"
    reports_cost = True

    def __init__(
        self,
        cwd: str,
        system_prompt: str | None = None,
        model: str | None = None,
        log: Callable[[str], None] = print,
    ):
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.model = model
        self.log = log

    def allocate_session_id(self) -> str:
        """Claude accepts a caller-provided UUID before the process starts."""

        return str(uuid.uuid4())

    def _build_args(self, prompt: str, session_id: str, resume: bool) -> list[str]:
        args = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if self.model:
            args += ["--model", self.model]
        if self.system_prompt:
            args += ["--append-system-prompt", self.system_prompt]
        args += (["--resume", session_id] if resume else ["--session-id", session_id])
        return args

    def run(
        self,
        prompt: str,
        session_id: str | None = None,
        resume: bool = False,
        stderr: TextIO | int | None = None,
        on_session_started: SessionStarted | None = None,
    ) -> AgentResult:
        """Execute one turn. Returns the result text + cost.

        `session_id=None` mints a fresh uuid (the normal per-attempt case);
        pass one with `resume=True` to continue an interrupted session.
        `stderr`: where the child's stderr goes. Default (None) captures it to
        a temp file so quota / network errors that surface ONLY on stderr (or
        as a non-zero exit with empty stdout) can still be classified; pass an
        explicit handle (log file / DEVNULL) to override. Either way it's a
        file, not a PIPE — no drain thread, no deadlock.

        Raises QuotaExhausted / TransientError for the two recoverable
        families, RuntimeError for a hard non-zero exit with no result.
        """
        if session_id is None:
            session_id = self.allocate_session_id()
        if on_session_started is not None:
            on_session_started(session_id)
        args = self._build_args(prompt, session_id, resume)
        # Resolve `claude` to an absolute path so a quirky PATH at spawn time
        # doesn't cause a spurious FileNotFoundError, and re-resolve every turn
        # so an in-place auto-update is picked up. If it can't be found, treat
        # that as transient (the binary is briefly absent while Claude Code
        # auto-updates in place) — a backoff retry recovers; a genuinely missing
        # binary just exhausts the transient budget with a clear message,
        # instead of instantly burning the generic 3-strike failure budget.
        resolved = shutil.which(args[0])
        if resolved:
            args[0] = resolved
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # drop nested-session guard

        self.log(f"[runner] claude -p (session={session_id[:8]}, resume={resume})")

        # Capture stderr ourselves (to a temp file, not a PIPE) when the caller
        # didn't supply a destination, so it can feed error classification.
        capture = tempfile.TemporaryFile(mode="w+") if stderr is None else None
        child_stderr = capture if capture is not None else stderr

        try:
            proc = subprocess.Popen(
                args,
                cwd=self.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=child_stderr,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            raise TransientError(
                f"claude executable not found ({args[0]!r}) — likely a transient "
                f"auto-update; will back off and retry: {e}", session_id) from e

        result_text, cost, resume_secs, assistant_text = self._parse_stream(proc)
        self._wait_for_exit(proc)

        stderr_text = ""
        if capture is not None:
            try:
                capture.seek(0)
                stderr_text = capture.read()
            finally:
                capture.close()

        # Classify across ALL streams. Real quota / network errors from
        # `claude -p` often land ONLY on stderr, or (for the session/usage
        # limit) ONLY in an assistant text block with an empty `result` — so
        # checking result_text alone would misread them as a hard failure and
        # burn the loop's failure budget instead of waiting them out. This runs
        # BEFORE the empty-result RuntimeError for that reason.
        err = _classify_streams(result_text, stderr_text, assistant_text, session_id)
        if err is not None:
            raise err

        if proc.returncode and not result_text:
            detail = stderr_text.strip()[:200]
            raise RuntimeError(
                f"claude -p exited {proc.returncode} with no result"
                + (f": {detail}" if detail else "")
            )

        return AgentResult(
            text=result_text, session_id=session_id, cost_usd=cost,
            requested_resume_secs=resume_secs,
        )

    def _parse_stream(
        self, proc: subprocess.Popen
    ) -> tuple[str, float | None, int | None, str]:
        """Read stream-json JSONL, log progress.

        Returns (result_text, cost, requested_resume_secs, assistant_text).
        `requested_resume_secs` is the `delaySeconds` of the final ScheduleWakeup
        tool call this turn (or None) — the orchestrator uses it to treat a
        tool-call pause as in_progress. `assistant_text` is every assistant text
        block joined, so error classification can catch a usage-limit notice
        that Claude Code emits there (with an empty `result`) on an aborted turn.
        """
        result_text = ""
        cost: float | None = None
        requested_resume_secs: int | None = None
        assistant_chunks: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")

            if etype == "assistant":
                msg = event.get("message", {})
                if isinstance(msg, dict):
                    for block in msg.get("content", []):
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                assistant_chunks.append(text)
                                self.log(f"[runner] {_clip(text, 500)}")
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "?")
                            self.log(f"[runner] tool_use: {name}")
                            # A ScheduleWakeup tool call is the Runner pausing
                            # the way it does in the interactive harness. Last
                            # one wins. Surfacing its delay lets the loop pause
                            # + resume even when the Runner skipped the
                            # {"status":"in_progress"} terminator line.
                            if name == "ScheduleWakeup":
                                delay = (block.get("input") or {}).get("delaySeconds")
                                if isinstance(delay, (int, float)) and delay > 0:
                                    requested_resume_secs = int(delay)
            elif etype == "result":
                result_text = event.get("result", "") or ""
                cost = event.get("total_cost_usd", event.get("cost_usd"))
                self.log(f"[runner] result (cost=${cost})")
                # The result event is the last one we care about. Stop reading
                # here: `claude -p` has been observed to keep the child (and
                # its stdout) alive after emitting `result` while tool-call
                # side effects settle, which would otherwise block the stdout
                # iterator indefinitely on a long, tool-heavy attempt.
                break
        return result_text, cost, requested_resume_secs, "\n".join(assistant_chunks)

    @staticmethod
    def _wait_for_exit(proc: subprocess.Popen, timeout: float = 10.0) -> None:
        """Wait for the process to exit after the result event; kill if it hangs."""
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"… (+{len(text) - n} chars)"
