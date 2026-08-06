"""Worker-agent abstraction and provider registry.

The orchestrator depends only on :class:`AgentAdapter`.  Provider modules
translate their CLI's command line, event stream, session lifecycle, and
recoverable failures into this small common contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Protocol, TextIO


# Recoverable provider error families shared by the built-in CLI adapters.
QUOTA_RE = re.compile(
    r"hit your (?:\w+ )?limit|(?:session|usage) limit|rate limit|"
    r"quota exceeded|too many requests|overloaded|"
    r"resets \d{1,2}(?::\d{2})?\s*[ap]m",
    re.IGNORECASE,
)
TRANSIENT_RE = re.compile(
    r"ECONNRESET|ETIMEDOUT|ENETUNREACH|EAI_AGAIN|socket hang up|"
    r"fetch failed|connection reset|connection refused|bad gateway|"
    r"service unavailable|gateway timeout|\b50[234]\b",
    re.IGNORECASE,
)


class QuotaExhausted(Exception):
    """The provider's quota / rate limit was hit; the loop waits and retries."""


class TransientError(Exception):
    """A resumable provider or network error."""

    def __init__(self, message: str, session_id: str | None):
        self.session_id = session_id
        super().__init__(message)


@dataclass
class AgentResult:
    """Provider-neutral result from one headless agent turn."""

    text: str
    session_id: str
    # Not every CLI reports monetary cost.  ``None`` means unavailable.
    cost_usd: float | None = None
    # Optional provider/tool hint used for long-running in-attempt pauses.
    requested_resume_secs: int | None = None


SessionStarted = Callable[[str], None]


class AgentAdapter(Protocol):
    """Contract implemented by every headless worker-agent provider."""

    provider: str
    reports_cost: bool

    def allocate_session_id(self) -> str | None:
        """Return a new session id, or None when the CLI assigns it at start."""

    def run(
        self,
        prompt: str,
        session_id: str | None = None,
        resume: bool = False,
        stderr: TextIO | int | None = None,
        on_session_started: SessionStarted | None = None,
    ) -> AgentResult:
        """Run one turn and return a provider-neutral result."""


AgentFactory = Callable[..., AgentAdapter]
_REGISTRY: dict[str, AgentFactory] = {}
_BUILTINS_LOADED = False


def register_agent(name: str, factory: AgentFactory, *, replace: bool = False) -> None:
    """Register a provider factory.

    This is intentionally tiny: adding another provider only requires an
    adapter implementing ``AgentAdapter`` and one registration call.
    """

    key = name.strip().lower()
    if not key:
        raise ValueError("agent provider name cannot be empty")
    if key in _REGISTRY and not replace:
        raise ValueError(f"agent provider already registered: {key}")
    _REGISTRY[key] = factory


def _load_builtin_agents() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return

    # Imported lazily so provider-specific dependencies and module setup do
    # not leak into callers that only need the common types.
    from .adapter import ClaudeAdapter
    from .codex_adapter import CodexAdapter

    _REGISTRY.setdefault("claude", ClaudeAdapter)
    _REGISTRY.setdefault("codex", CodexAdapter)
    _BUILTINS_LOADED = True


def available_agents() -> tuple[str, ...]:
    """Return registered provider names, including GoaLoop's built-ins."""

    _load_builtin_agents()
    return tuple(sorted(_REGISTRY))


def create_agent(name: str, **kwargs) -> AgentAdapter:
    """Create a registered provider adapter."""

    _load_builtin_agents()
    key = name.strip().lower()
    try:
        factory = _REGISTRY[key]
    except KeyError as e:
        supported = ", ".join(sorted(_REGISTRY))
        raise ValueError(
            f"unsupported agent provider {name!r}; choose one of: {supported}"
        ) from e
    return factory(**kwargs)
