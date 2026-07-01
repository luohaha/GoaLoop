"""Per-workspace configuration (`<workspace>/config.yaml`).

Optional and tiny by design: a flat file of scalar keys, parsed without a
YAML dependency (GoaLoop stays stdlib-only). CLI flags override config
values, which override these defaults. Anything domain-specific stays out
of here — it belongs in `goal.md`.

Recognized keys:
  model        : model id passed to `claude -p` (default: CLI default / none)
  interval     : seconds to pace between successful attempts (default 30)
  mode         : "auto" (default) | "copilot" (pause for approval each attempt)
  max_attempts : stop after this many attempts (default: unlimited)
  max_cost_usd : stop once cumulative `claude -p` cost reaches this (default: unlimited)
  job_cleanup_pattern : substring matched against process cmdlines; if set, `stop`
                 reaps (and `run` clears stray) processes whose cmdline contains it.
                 Use when the Runner launches long jobs detached (setsid/nohup) that
                 escape the orchestrator's process group and would otherwise survive
                 `stop` and pile up across restarts (default: unset / disabled).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_INTERVAL = 30
VALID_MODES = ("auto", "copilot")


@dataclass
class Config:
    model: str | None = None
    interval: int = DEFAULT_INTERVAL
    mode: str = "auto"
    max_attempts: int | None = None
    max_cost_usd: float | None = None
    job_cleanup_pattern: str | None = None


def _parse_flat_yaml(text: str) -> dict[str, str]:
    """Parse a flat `key: value` file. Ignores comments and blank lines.

    Deliberately not a real YAML parser — config.yaml here is only ever a
    handful of scalar keys, and avoiding the dependency keeps install
    trivial. Nested structures are not supported (and not needed).
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        key, val = line.split(":", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def load_config(workspace: Path) -> Config:
    """Load `<workspace>/config.yaml` if present, else return defaults."""
    cfg = Config()
    path = workspace / "config.yaml"
    if not path.exists():
        return cfg

    raw = _parse_flat_yaml(path.read_text())
    if raw.get("model"):
        cfg.model = raw["model"]
    if raw.get("interval"):
        try:
            cfg.interval = int(raw["interval"])
        except ValueError:
            pass
    mode = raw.get("mode", "").lower()
    if mode in VALID_MODES:
        cfg.mode = mode
    if raw.get("max_attempts"):
        try:
            cfg.max_attempts = int(raw["max_attempts"])
        except ValueError:
            pass
    if raw.get("max_cost_usd"):
        try:
            cfg.max_cost_usd = float(raw["max_cost_usd"])
        except ValueError:
            pass
    if raw.get("job_cleanup_pattern"):
        cfg.job_cleanup_pattern = raw["job_cleanup_pattern"]
    return cfg
