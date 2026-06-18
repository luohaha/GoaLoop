"""Thin wrapper around `claude -p` (Claude Code non-interactive mode).

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
import subprocess
import uuid
from dataclasses import dataclass
from typing import Callable, TextIO

# Result text that means the account hit its API quota / rate limit. The
# loop responds by sleeping for a cool-down and resuming the same session.
_QUOTA_RE = re.compile(
    r"hit your limit|rate limit|quota exceeded|too many requests|"
    r"overloaded|resets \d+[ap]m",
    re.IGNORECASE,
)

# Transient network / upstream hiccups. The loop resumes the same session
# (so in-flight work like a long verification survives) after a short wait.
_TRANSIENT_RE = re.compile(
    r"ECONNRESET|ETIMEDOUT|ENETUNREACH|EAI_AGAIN|socket hang up|"
    r"fetch failed|connection reset|connection refused|bad gateway|"
    r"service unavailable|gateway timeout|\b50[234]\b",
    re.IGNORECASE,
)


class QuotaExhausted(Exception):
    """API quota / rate limit hit; the loop waits and resumes."""


class TransientError(Exception):
    """Resumable network error. `session_id` is the session to `--resume`."""

    def __init__(self, message: str, session_id: str):
        self.session_id = session_id
        super().__init__(message)


@dataclass
class ClaudeResult:
    text: str
    session_id: str
    cost_usd: float | None = None


class ClaudeAdapter:
    """Runs a single `claude -p` turn and returns its final result text.

    `system_prompt` is appended to Claude Code's default system prompt
    (`--append-system-prompt`) — GoaLoop passes the Runner instructions
    here. `cwd` is where the Runner operates (the workspace).
    """

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
    ) -> ClaudeResult:
        """Execute one turn. Returns the result text + cost.

        `session_id=None` mints a fresh uuid (the normal per-attempt case);
        pass one with `resume=True` to continue an interrupted session.
        `stderr` is where the child's stderr goes (a log file handle, or
        `subprocess.DEVNULL`); routing it away from a PIPE avoids the
        pipe-buffer deadlock without needing a drain thread.

        Raises QuotaExhausted / TransientError for the two recoverable
        families, RuntimeError for a hard non-zero exit with no result.
        """
        if session_id is None:
            session_id = str(uuid.uuid4())
        args = self._build_args(prompt, session_id, resume)
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # drop nested-session guard

        self.log(f"[runner] claude -p (session={session_id[:8]}, resume={resume})")

        proc = subprocess.Popen(
            args,
            cwd=self.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=stderr if stderr is not None else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        result_text, cost = self._parse_stream(proc)
        self._wait_for_exit(proc)

        if proc.returncode and not result_text:
            raise RuntimeError(f"claude -p exited {proc.returncode} with no result")

        if result_text and _QUOTA_RE.search(result_text):
            raise QuotaExhausted(result_text.strip()[:300])
        if result_text and _TRANSIENT_RE.search(result_text):
            raise TransientError(result_text.strip()[:300], session_id)

        return ClaudeResult(text=result_text, session_id=session_id, cost_usd=cost)

    def _parse_stream(self, proc: subprocess.Popen) -> tuple[str, float | None]:
        """Read stream-json JSONL, log progress, return (result_text, cost)."""
        result_text = ""
        cost: float | None = None
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
                                self.log(f"[runner] {_clip(text, 500)}")
                        elif block.get("type") == "tool_use":
                            self.log(f"[runner] tool_use: {block.get('name', '?')}")
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
        return result_text, cost

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
