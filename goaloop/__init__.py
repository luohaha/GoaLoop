"""GoaLoop — a goal-driven multi-attempt iteration loop driven by `claude -p`.

The package is a lean orchestrator: it loops a fresh `claude -p` session
(the Runner) over a workspace's `goal.md` until the Runner's Verification
passes or the human stops it. All durable state lives in the workspace on
disk; the loop holds nothing authoritative.
"""

__version__ = "0.1.0"
