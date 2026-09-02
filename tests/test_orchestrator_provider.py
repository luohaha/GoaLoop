"""Integration-level checks for provider selection and session checkpointing."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from goaloop.agent import AgentResult, ProviderError
from goaloop.cli import main
from goaloop.orchestrator import Orchestrator


CODEX_THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"


class PassingCodexLikeAgent:
    provider = "codex"
    reports_cost = False

    def __init__(self):
        self.calls: list[tuple[str | None, bool]] = []

    def allocate_session_id(self):
        return None

    def run(
        self,
        prompt,
        session_id=None,
        resume=False,
        stderr=None,
        on_session_started=None,
    ):
        self.calls.append((session_id, resume))
        if on_session_started:
            on_session_started(CODEX_THREAD)
        return AgentResult(
            text='{"status": "pass", "verification": "ok"}',
            session_id=CODEX_THREAD,
        )


class RejectedCodexLikeAgent:
    provider = "codex"
    reports_cost = False

    def __init__(self):
        self.calls = 0

    def allocate_session_id(self):
        return None

    def run(
        self,
        prompt,
        session_id=None,
        resume=False,
        stderr=None,
        on_session_started=None,
    ):
        self.calls += 1
        if on_session_started:
            on_session_started(CODEX_THREAD)
        raise ProviderError("codex exec failed: cyber_policy")


class OrchestratorProviderTest(unittest.TestCase):
    def _workspace(self, root: str) -> Path:
        ws = Path(root)
        (ws / "goal.md").write_text("# Goal\n\n## Verification\nRun tests.\n")
        return ws

    def test_provider_assigned_session_is_checkpointed_then_cleared_on_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(tmp)
            fake = PassingCodexLikeAgent()
            with patch("goaloop.orchestrator.create_agent", return_value=fake):
                Orchestrator(ws, agent="codex", log=lambda _: None).run()

            self.assertEqual(fake.calls, [(None, False)])
            state = json.loads((ws / ".goaloop" / "state.json").read_text())
            self.assertEqual(state["active_agent"], "codex")
            self.assertIsNone(state["active_session_id"])
            complete = json.loads(
                (ws / ".goaloop" / "attempt_complete.json").read_text()
            )
            self.assertEqual(complete["status"], "pass")

    def test_checkpoint_from_other_provider_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(tmp)
            state_dir = ws / ".goaloop"
            state_dir.mkdir()
            (state_dir / "state.json").write_text(
                json.dumps(
                    {
                        "active_agent": "claude",
                        "active_session_id": "old-claude-session",
                        "attempt": 1,
                        "goal_mtime": (ws / "goal.md").stat().st_mtime,
                    }
                )
            )

            logs: list[str] = []
            fake = PassingCodexLikeAgent()
            with patch("goaloop.orchestrator.create_agent", return_value=fake):
                Orchestrator(ws, agent="codex", log=logs.append).run()

            self.assertEqual(fake.calls, [(None, False)])
            self.assertTrue(any("worker agent changed" in line for line in logs))

    def test_provider_failure_ends_immediately_with_original_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(tmp)
            fake = RejectedCodexLikeAgent()
            with patch("goaloop.orchestrator.create_agent", return_value=fake):
                Orchestrator(ws, agent="codex", log=lambda _: None).run()

            self.assertEqual(fake.calls, 1)
            status = (ws / ".goaloop" / "status.txt").read_text()
            self.assertIn("codex exec failed: cyber_policy", status)
            self.assertNotIn("malformed", status)
            state = json.loads((ws / ".goaloop" / "state.json").read_text())
            self.assertIsNone(state["active_session_id"])
            complete = json.loads(
                (ws / ".goaloop" / "attempt_complete.json").read_text()
            )
            self.assertEqual(complete["status"], "error")

    def test_unenforceable_cost_cap_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(tmp)
            logs: list[str] = []
            fake = PassingCodexLikeAgent()
            with patch("goaloop.orchestrator.create_agent", return_value=fake):
                Orchestrator(
                    ws, agent="codex", max_cost_usd=1.0, log=logs.append
                ).run()

            self.assertTrue(
                any("max_cost_usd cannot be enforced" in line for line in logs)
            )

    def test_cli_agent_flag_reaches_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(tmp)
            with patch("goaloop.cli.Orchestrator") as orchestrator:
                code = main(["run", str(ws), "--foreground", "--agent", "codex"])

            self.assertEqual(code, 0)
            _, kwargs = orchestrator.call_args
            self.assertEqual(kwargs["agent"], "codex")
            orchestrator.return_value.run.assert_called_once_with()

    def test_unknown_configured_agent_fails_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(tmp)
            (ws / "config.yaml").write_text("agent: imaginary\n")
            stderr = io.StringIO()
            with (
                patch("goaloop.cli.Orchestrator") as orchestrator,
                redirect_stderr(stderr),
            ):
                code = main(["run", str(ws), "--foreground"])

            self.assertEqual(code, 1)
            self.assertIn("Unsupported agent provider", stderr.getvalue())
            orchestrator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
