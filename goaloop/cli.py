"""CLI for GoaLoop: `goaloop run|status|stop <workspace>`.

`run` starts the Orchestrator. By default it detaches into the background
(like auto-perf-opt's daemon) so it keeps running independent of the Claude
Code session that launched it; `--foreground` runs it inline. State, logs,
and the PID file live under `<workspace>/.goaloop/`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import load_config
from .orchestrator import Orchestrator


def _resolve_workspace(name_or_path: str) -> Path:
    """Accept either a workspace name (→ ~/.goaloop/<name>) or a path."""
    p = Path(name_or_path).expanduser()
    if p.is_dir() and (p / "goal.md").exists():
        return p.resolve()
    candidate = (Path.home() / ".goaloop" / name_or_path)
    if candidate.is_dir():
        return candidate.resolve()
    return p.resolve()  # let the caller report the missing goal.md


def _state_dir(ws: Path) -> Path:
    return ws / ".goaloop"


def _pid_path(ws: Path) -> Path:
    return _state_dir(ws) / "pipeline.pid"


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _running_pid(ws: Path) -> int | None:
    pp = _pid_path(ws)
    if pp.exists():
        try:
            pid = int(pp.read_text().strip())
        except (ValueError, OSError):
            return None
        if _is_running(pid):
            return pid
        pp.unlink(missing_ok=True)  # stale
    return None


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _terminate(pid: int) -> None:
    """SIGTERM the orchestrator AND its `claude -p` child.

    A backgrounded orchestrator is its own session/group leader
    (`start_new_session=True` in `_run_background`), so signaling the whole
    process group reaches the in-flight Runner subprocess too. Signaling only
    the orchestrator pid leaves that child orphaned — it keeps running and can
    race a later restart on the same session. For a foreground run (which
    shares the caller's process group) fall back to the bare pid so we don't
    take the caller down with it.
    """
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    try:
        if pgid == pid:  # backgrounded run → group leader; take the group
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


# ---- commands --------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    ws = _resolve_workspace(args.workspace)
    if not (ws / "goal.md").exists():
        print(f"No goal.md at {ws} — run /goal-init first.", file=sys.stderr)
        return 1

    if (pid := _running_pid(ws)) is not None:
        print(f"Orchestrator already running (PID {pid}). Use `goaloop stop {args.workspace}` first.",
              file=sys.stderr)
        return 1

    if args.foreground:
        return _run_foreground(ws, args)
    return _run_background(ws, args)


def _run_foreground(ws: Path, args: argparse.Namespace) -> int:
    # Resolve effective settings: CLI flags override config.yaml override defaults.
    cfg = load_config(ws)
    model = args.model or cfg.model
    interval = args.interval if args.interval is not None else cfg.interval
    mode = args.mode or cfg.mode
    max_attempts = args.max_attempts if args.max_attempts is not None else cfg.max_attempts
    max_cost = args.max_cost if args.max_cost is not None else cfg.max_cost_usd

    state = _state_dir(ws)
    state.mkdir(parents=True, exist_ok=True)
    _pid_path(ws).write_text(str(os.getpid()))

    log_fh = open(state / "orchestrator.log", "a", buffering=1)

    def log(msg: str) -> None:
        line = f"{_ts()} {msg}"
        print(line, file=log_fh, flush=True)

    def _sigterm(signum, frame):  # noqa: ANN001
        log("[orchestrator] received SIGTERM — stopping")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm)

    try:
        Orchestrator(ws, model=model, interval=interval, mode=mode,
                 max_attempts=max_attempts, max_cost_usd=max_cost, log=log).run()
        return 0
    finally:
        _pid_path(ws).unlink(missing_ok=True)
        log_fh.close()


def _run_background(ws: Path, args: argparse.Namespace) -> int:
    state = _state_dir(ws)
    state.mkdir(parents=True, exist_ok=True)
    # Forward only the flags the user actually passed; the foreground process
    # re-resolves against config.yaml so there's a single resolution point.
    cmd = [sys.executable, "-m", "goaloop", "run", str(ws), "--foreground"]
    if args.interval is not None:
        cmd += ["--interval", str(args.interval)]
    if args.model:
        cmd += ["--model", args.model]
    if args.mode:
        cmd += ["--mode", args.mode]
    if args.max_attempts is not None:
        cmd += ["--max-attempts", str(args.max_attempts)]
    if args.max_cost is not None:
        cmd += ["--max-cost", str(args.max_cost)]

    boot_log = open(state / "boot.log", "w")
    proc = subprocess.Popen(
        cmd, stdout=boot_log, stderr=boot_log,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    time.sleep(1)
    if proc.poll() is not None:
        print(f"Orchestrator failed to start (exit {proc.returncode}). See {state / 'boot.log'}",
              file=sys.stderr)
        return 1

    print(f"GoaLoop started in background (PID {proc.pid})")
    print(f"  workspace: {ws}")
    print(f"  log:       {state / 'orchestrator.log'}")
    print(f"  status:    goaloop status {args.workspace}")
    print(f"  stop:      goaloop stop {args.workspace}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws = _resolve_workspace(args.workspace)
    pid = _running_pid(ws)
    print(f"Orchestrator: {'RUNNING (PID ' + str(pid) + ')' if pid else 'NOT RUNNING'}")

    status_path = _state_dir(ws) / "status.txt"
    if status_path.exists():
        print(f"Status: {status_path.read_text().strip()}")

    attempts = ws / "attempts"
    if attempts.is_dir():
        files = sorted(p.name for p in attempts.glob("[0-9][0-9][0-9].md"))
        print(f"Attempts recorded: {len(files)}" + (f" (latest {files[-1]})" if files else ""))

    # Cumulative spend (tracked since the cost cap was added). Reading the
    # checkpoint keeps this live even mid-attempt.
    cp_path = _state_dir(ws) / "state.json"
    if cp_path.exists():
        import json
        try:
            total = json.loads(cp_path.read_text()).get("total_cost_usd")
        except (json.JSONDecodeError, OSError):
            total = None
        if total:
            print(f"Cumulative cost: ${total:.2f}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    ws = _resolve_workspace(args.workspace)
    pid = _running_pid(ws)
    if pid is None:
        print("Not running.")
        return 0
    print(f"Sending SIGTERM to orchestrator (PID {pid})…")
    _terminate(pid)
    print("Stop signal sent; the orchestrator and its Runner subprocess will exit shortly.")
    return 0


def cmd_continue(args: argparse.Namespace) -> int:
    """Approve the next attempt in copilot mode (writes continue.json)."""
    ws = _resolve_workspace(args.workspace)
    if _running_pid(ws) is None:
        print("Orchestrator is not running — nothing to approve.", file=sys.stderr)
        return 1
    state = _state_dir(ws)
    state.mkdir(parents=True, exist_ok=True)
    (state / "continue.json").write_text("{}")
    print("Approved — the orchestrator will start the next attempt.")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Deploy the bundled skills and Runner agent into ~/.claude.

    Makes `/goal-init`, `/goal-run`, `/goal-flash` and the `goal-runner`
    subagent available to every Claude Code session. The assets ship inside
    the installed package (`resources/`), so this works from a `uv tool
    install` / `pip install` with no source checkout.
    """
    resources = Path(__file__).resolve().parent / "resources"
    claude = Path.home() / ".claude"

    copied: list[str] = []
    skipped: list[str] = []

    def deploy(src: Path, dst: Path) -> None:
        if dst.exists() and not args.force:
            skipped.append(str(dst))
            return
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied.append(str(dst))

    skills_src = resources / "skills"
    if skills_src.is_dir():
        for skill in sorted(p for p in skills_src.iterdir() if p.is_dir()):
            deploy(skill, claude / "skills" / skill.name)

    agents_src = resources / "agents"
    if agents_src.is_dir():
        for agent in sorted(agents_src.glob("*.md")):
            deploy(agent, claude / "agents" / agent.name)

    for path in copied:
        print(f"installed  {path}")
    for path in skipped:
        print(f"exists     {path}  (use --force to overwrite)")
    if not copied and not skipped:
        print("Nothing to install — no bundled resources found.", file=sys.stderr)
        return 1
    if copied:
        print("\nDone. Open Claude Code and type /goal-init to verify.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="goaloop", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="start (or background) the orchestrator")
    p_run.add_argument("workspace", help="workspace name (~/.goaloop/<name>) or path")
    p_run.add_argument("--foreground", "-f", action="store_true", help="run inline, not detached")
    p_run.add_argument("--model", default=None,
                       help="model for claude -p (overrides config.yaml)")
    p_run.add_argument("--interval", type=int, default=None,
                       help="seconds between successful attempts "
                            "(overrides config.yaml; default 30)")
    p_run.add_argument("--mode", choices=("auto", "copilot"), default=None,
                       help="auto (default) or copilot, pause for approval each "
                            "attempt (overrides config.yaml)")
    p_run.add_argument("--max-attempts", type=int, default=None,
                       help="stop after this many attempts (overrides "
                            "config.yaml; default unlimited)")
    p_run.add_argument("--max-cost", type=float, default=None, dest="max_cost",
                       help="stop once cumulative claude -p cost (USD) reaches "
                            "this (overrides config.yaml; default unlimited)")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="show orchestrator status and attempt count")
    p_status.add_argument("workspace")
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="stop a running background orchestrator")
    p_stop.add_argument("workspace")
    p_stop.set_defaults(func=cmd_stop)

    p_continue = sub.add_parser("continue", help="approve next attempt (copilot mode)")
    p_continue.add_argument("workspace")
    p_continue.set_defaults(func=cmd_continue)

    p_install = sub.add_parser(
        "install", help="deploy bundled skills + agent into ~/.claude")
    p_install.add_argument("--force", action="store_true",
                           help="overwrite existing skills/agents")
    p_install.set_defaults(func=cmd_install)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
