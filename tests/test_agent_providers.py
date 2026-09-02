"""Provider-contract tests for Claude and headless Codex workers."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from goaloop.adapter import ClaudeAdapter, ClaudeResult
from goaloop.agent import (
    AgentResult,
    ProviderError,
    QuotaExhausted,
    TransientError,
    available_agents,
    create_agent,
)
from goaloop.codex_adapter import CodexAdapter
from goaloop.config import load_config


THREAD_ID = "0199a213-81c0-7800-8aa1-bbab2a035a53"


class FakeProcess:
    def __init__(self, events: list[dict] | None = None, returncode: int = 0):
        lines = [json.dumps(event) for event in (events or [])]
        self.stdout = io.StringIO("\n".join(lines) + ("\n" if lines else ""))
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):  # noqa: ANN001
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def successful_events(text: str) -> list[dict]:
    return [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {"type": "turn.started"},
        {
            "type": "item.started",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "python -m unittest",
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "item_2", "type": "agent_message", "text": text},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    ]


class RegistryTest(unittest.TestCase):
    def test_builtins_are_registered(self):
        self.assertEqual(available_agents(), ("claude", "codex"))

    def test_factory_selects_provider(self):
        common = {"cwd": "/tmp", "system_prompt": "runner", "log": lambda _: None}
        self.assertIsInstance(create_agent("claude", **common), ClaudeAdapter)
        self.assertIsInstance(create_agent("codex", **common), CodexAdapter)

    def test_unknown_provider_lists_choices(self):
        with self.assertRaisesRegex(ValueError, "claude, codex"):
            create_agent("other", cwd="/tmp")

    def test_old_claude_result_name_is_compatible(self):
        self.assertIs(ClaudeResult, AgentResult)


class ConfigTest(unittest.TestCase):
    def test_agent_defaults_to_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_config(Path(tmp)).agent, "claude")

    def test_agent_can_select_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "config.yaml").write_text("agent: codex\nmodel: test-model\n")
            cfg = load_config(Path(tmp))
            self.assertEqual(cfg.agent, "codex")
            self.assertEqual(cfg.model, "test-model")

    def test_unknown_agent_is_preserved_for_cli_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "config.yaml").write_text("agent: imaginary\n")
            self.assertEqual(load_config(Path(tmp)).agent, "imaginary")


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.logs: list[str] = []
        self.adapter = CodexAdapter(
            cwd="/workspace",
            system_prompt="RUNNER PROTOCOL",
            model="test-model",
            log=self.logs.append,
        )

    def _run_with(self, process: FakeProcess, *args, **kwargs):
        with (
            patch("goaloop.codex_adapter.shutil.which", return_value="/usr/bin/codex"),
            patch("goaloop.codex_adapter.subprocess.Popen", return_value=process) as popen,
        ):
            result = self.adapter.run(*args, **kwargs)
        return result, popen

    def test_initial_turn_combines_runner_prompt_and_checkpoints_thread(self):
        final = '{"status": "pass", "verification": "tests pass"}'
        started: list[str] = []
        result, popen = self._run_with(
            FakeProcess(successful_events(final)),
            "attempt brief",
            on_session_started=started.append,
        )

        self.assertEqual(result.text, final)
        self.assertEqual(result.session_id, THREAD_ID)
        self.assertIsNone(result.cost_usd)
        self.assertEqual(started, [THREAD_ID])

        command = popen.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/codex", "exec"])
        self.assertIn("--json", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertNotIn("resume", command)
        self.assertEqual(command[-2:], ["test-model", "RUNNER PROTOCOL\n\n---\n\nattempt brief"])
        self.assertEqual(popen.call_args.kwargs["cwd"], "/workspace")
        self.assertNotIn("CODEX_THREAD_ID", popen.call_args.kwargs["env"])

    def test_resume_uses_codex_exec_resume_without_repeating_system_prompt(self):
        final = '{"status": "advanced", "summary": "made progress"}'
        result, popen = self._run_with(
            FakeProcess(successful_events(final)),
            "Continue.",
            THREAD_ID,
            True,
        )

        self.assertEqual(result.session_id, THREAD_ID)
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/codex", "exec", "resume"])
        self.assertEqual(command[-2:], [THREAD_ID, "Continue."])
        self.assertNotIn("RUNNER PROTOCOL", command)

    def test_resume_requires_session_id(self):
        with self.assertRaisesRegex(ValueError, "requires a session id"):
            self.adapter.run("Continue.", None, True)

    def test_quota_error_event_is_recoverable(self):
        events = [
            {"type": "thread.started", "thread_id": THREAD_ID},
            {"type": "error", "message": "rate limit exceeded"},
        ]
        with self.assertRaises(QuotaExhausted):
            self._run_with(FakeProcess(events, returncode=1), "brief")

    def test_transient_error_preserves_codex_thread_id(self):
        events = [
            {"type": "thread.started", "thread_id": THREAD_ID},
            {"type": "turn.failed", "error": {"message": "fetch failed: ECONNRESET"}},
        ]
        with self.assertRaises(TransientError) as ctx:
            self._run_with(FakeProcess(events, returncode=1), "brief")
        self.assertEqual(ctx.exception.session_id, THREAD_ID)

    def test_provider_failure_is_not_masked_by_intermediate_message(self):
        events = [
            {"type": "thread.started", "thread_id": THREAD_ID},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "I am still investigating the failure.",
                },
            },
            {
                "type": "turn.failed",
                "error": {
                    "message": "content was flagged for cybersecurity risk",
                    "codex_error_info": "cyber_policy",
                },
            },
        ]
        with self.assertRaises(ProviderError) as ctx:
            self._run_with(FakeProcess(events), "brief")
        self.assertIn("cyber_policy", str(ctx.exception))

    def test_nonzero_exit_is_not_masked_by_intermediate_message(self):
        events = [
            {"type": "thread.started", "thread_id": THREAD_ID},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Working on it."},
            },
        ]
        with self.assertRaisesRegex(RuntimeError, "codex exec exited 1"):
            self._run_with(FakeProcess(events, returncode=1), "brief")

    def test_missing_thread_started_is_hard_failure(self):
        events = [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            }
        ]
        with self.assertRaisesRegex(RuntimeError, "no thread.started"):
            self._run_with(FakeProcess(events), "brief")


if __name__ == "__main__":
    unittest.main()
