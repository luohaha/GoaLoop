"""GoaLoop — a goal-driven multi-attempt iteration loop.

The package is a lean orchestrator: it loops fresh headless Claude or Codex
workers over a workspace's ``goal.md`` until Verification passes or the human
stops it. All durable state lives in the workspace on disk.
"""

__version__ = "0.1.0"
