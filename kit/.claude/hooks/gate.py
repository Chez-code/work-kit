#!/usr/bin/env python3
"""Verification gate for Claude Code hooks. Stdlib only.

Auto-detects the stack:
- Python (pyproject.toml): PostToolUse ruff-fixes the touched .py file and runs
  fast tests; Stop runs the full suite.
- .NET (.sln/.slnx/.csproj at the root or one level down): PostToolUse builds
  after each .cs edit (tests are too slow mid-session); Stop builds, then runs
  `dotnet test --no-build`. NOTE: --no-build against a target with no test
  projects exits 0 — a green Stop means "build ok + any tests that exist
  passed", not "tests exist"; add a test project early. The `ran` field in
  the log says what actually executed.

Modes (argv):
- (none)        edit gate, PostToolUse on Write|Edit|MultiEdit, or invoked by
                bash_guard.py with a synthesized payload (tool_name "Bash").
- --stop        Stop gate. With stop_hook_active set (second consecutive
                stop) it still runs everything, returns 0 regardless, and
                logs the verdict as `released` so the watcher can tell a
                real fix from a red suite handed back.
- --new-prompt  UserPromptSubmit: clear the loop state, log a `prompt`
                event, print NOTHING (exit-0 stdout would be injected into
                Claude's context every turn).

Loop detector: consecutive blocks whose failure-key SET is unchanged count
as strikes (state in .claude/gate-state.json). At GATE_LOOP_STRIKES (default
3) the gate stops feeding the failure back and tells Claude to stop and
report to the operator. Any new user prompt resets the count.

Every gate run that gets past the extension check writes a start line and
exactly one end line to .claude/gate-log.jsonl (see gate_log.py).

Environment:
- CLAUDE_PROJECT_DIR: repo root (fallback: payload cwd, then process cwd).
- GATE_DOTNET_TARGET: explicit .sln/.slnx/.csproj to build when auto-detection
  finds zero or several candidates (multi-solution monorepos).
- GATE_TIMEOUT: total time budget in seconds for ALL subprocesses in one gate
  run combined (default 540). Keep it below the hook-level timeout (600 in
  settings.json): a hang must be caught HERE, as a blocking failure, before
  the harness kills the hook — a harness kill is non-blocking and would let
  the hang pass silently.
- GATE_LOOP_STRIKES: strikes before [LOOP DETECTED] fires (default 3).

Exit 0  -> proceed.
Exit 2  -> blocked; stderr is fed back to Claude so it fixes its own mess.

stdout discipline: on exit 0, Claude Code parses hook stdout as JSON. The skip
paths print exactly one systemMessage object; nothing else may EVER write to
stdout, or that message silently stops rendering.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # no __pycache__ in the target repo (before the sibling import)

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_log  # noqa: E402

MAX_LINES = 40  # keep feedback concise — Claude needs the failure, not the novel
DOTNET_EXTS = (".cs", ".csproj", ".sln", ".slnx")
PYTHON_EXTS = (".py",)

DOTNET_HINT = (
    "Set GATE_DOTNET_TARGET to the solution/project to build. The form that "
    "cannot fail is an env prefix in the hook command itself, e.g. "
    '"command": "GATE_DOTNET_TARGET=src/App.sln python3 '
    '\\"$CLAUDE_PROJECT_DIR/.claude/hooks/gate.py\\"" '
    "(the settings env block also works)."
)


def is_dotnet(root: Path) -> bool:
    """A solution or project file at (or one level under) the root marks a .NET repo."""
    if os.environ.get("GATE_DOTNET_TARGET"):
        return True
    for pattern in ("*.sln", "*.slnx", "*/*.sln", "*/*.slnx", "*.csproj", "*/*.csproj"):
        if any(root.glob(pattern)):
            return True
    return False


def find_dotnet_target(root: Path) -> tuple[str | None, str]:
    """Resolve the single build target, or return (None, why-skipped)."""
    override = os.environ.get("GATE_DOTNET_TARGET")
    if override:
        return override, ""
    solutions = sorted(
        str(p) for pat in ("*.sln", "*.slnx", "*/*.sln", "*/*.slnx") for p in root.glob(pat)
    )
    if len(solutions) == 1:
        return solutions[0], ""
    if len(solutions) > 1:
        return None, f"{len(solutions)} solution files found"
    projects = sorted(str(p) for pat in ("*.csproj", "*/*.csproj") for p in root.glob(pat))
    if len(projects) == 1:
        return projects[0], ""
    if len(projects) > 1:
        return None, f"no solution and {len(projects)} .csproj files found"
    return None, "no .sln/.slnx/.csproj found at root or one level down"


def skip_visibly(message: str) -> int:
    """Skip the gate, but say so where the user can see it.

    stderr on exit 0 only shows in verbose mode; the systemMessage JSON on
    stdout renders in the normal transcript, so a silently-disabled gate
    cannot go unnoticed for weeks.
    """
    sys.stderr.write(message + "\n")
    print(json.dumps({"systemMessage": message}))
    return 0


def clear_stale_pyc(source: Path) -> None:
    """Drop every cached compilation of the file that was just edited.

    Python validates .pyc files by source mtime + size. An edit that keeps
    the size and lands in the same second (exactly the one-character fixes a
    hook sees the instant they happen) leaves a stale but valid-looking .pyc,
    and pytest then silently runs the PREVIOUS version of the test.
    PYTHONDONTWRITEBYTECODE does not help — caches are still read. Deleting
    the edited file's cache entries is the only reliable guard.
    """
    pycache = source.parent / "__pycache__"
    if pycache.is_dir():
        for pyc in pycache.glob(source.stem + ".*.pyc"):
            try:
                pyc.unlink()
            except OSError:
                pass


def uv_available(root: Path) -> bool:
    """Project tools (ruff, pytest) live in the uv-managed venv, not on PATH."""
    return bool(shutil.which("uv")) and (root / "pyproject.toml").is_file()


def sh(cmd: list[str], cwd: Path, deadline: float, ran: list[str]) -> tuple[int, str]:
    """Run cmd with whatever remains of the whole-gate time budget; record it in `ran`."""
    ran.append(" ".join(cmd).replace(str(cwd) + os.sep, ""))  # repo-relative in the log
    remaining = max(1.0, deadline - time.monotonic())
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=remaining)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {remaining:.0f}s (GATE_TIMEOUT budget): {' '.join(cmd)}"
    out = (proc.stdout + proc.stderr).strip()
    lines = out.splitlines()
    if len(lines) > MAX_LINES:
        out = "\n".join(lines[-MAX_LINES:])
    return proc.returncode, out


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _relative(root: Path, path: str | None) -> str | None:
    """Repo-relative form for the log when the path is absolute and inside the root."""
    if not path:
        return path
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return path


class Run:
    """One gate run: owns the log base fields and writes the single end line."""

    def __init__(self, root: Path, payload: dict, kind: str) -> None:
        self.root = root
        self.started = time.monotonic()
        self.ran: list[str] = []
        self.base = {"kind": kind, "run": gate_log.new_run_id()}
        self.base.update(gate_log.session_fields(payload))
        if kind == "edit":
            tool_input = payload.get("tool_input") or {}
            self.base["tool"] = payload.get("tool_name")
            self.base["file"] = _relative(root, tool_input.get("file_path"))
            self.base["delta"] = gate_log.edit_delta(tool_input)
        gate_log.append_event(root, phase="start", **self.base)

    def end(self, verdict: str, **extra) -> None:
        duration = round(time.monotonic() - self.started, 1)
        gate_log.append_event(
            self.root, phase="end", verdict=verdict, ran=self.ran, duration=duration, **extra, **self.base
        )

    def skip(self, message: str) -> int:
        self.end("skip", reason=message)
        return skip_visibly(message)


def judge(root: Path, run: Run, failures: list[str], stop_mode: bool, released: bool) -> int:
    """Turn the collected failures into a verdict, a log line and an exit code."""
    if not failures:
        if released:
            run.end("released", result="green")
        else:
            gate_log.clear_state(root)
            run.end("green")
        return 0

    text = "\n\n".join(failures)
    keys = gate_log.failure_keys(text)
    if released:
        # Second consecutive stop: the harness would loop if we blocked again.
        # Verified anyway so the watcher can show "released, still failing".
        run.end("released", result="block", keys=keys)
        return 0

    state = gate_log.load_state(root)
    if state["strike"] and set(keys) == set(state["keys"]):
        strike = state["strike"] + 1
        times = state["times"]
    else:
        strike = 1
        times = []
    times.append(gate_log.now_iso())
    gate_log.save_state(root, {"keys": keys, "strike": strike, "times": times})

    threshold = max(1, _env_int("GATE_LOOP_STRIKES", 3))
    if strike >= threshold:
        first = keys[0]
        more = f" (+{len(keys) - 1} more)" if len(keys) > 1 else ""
        if stop_mode and strike > threshold:
            message = (
                f"[LOOP DETECTED] still failing on {first}{more} (strike {strike}). "
                "Report to the operator; do not retry."
            )
        else:
            when = ", ".join(t[11:19] for t in times)
            message = (
                f"[LOOP DETECTED] {first}{more} has failed {strike} edits in a row ({when}). "
                "Do not attempt another fix. Stop and tell the operator what you tried "
                "and why each attempt failed."
            )
        sys.stderr.write(message + "\n")
        run.end("loop", keys=keys, strike=strike)
        return 2

    label = "STOP GATE" if stop_mode else "EDIT GATE"
    sys.stderr.write(f"[{label} FAILED] Fix before proceeding.\n" + text + "\n")
    run.end("block", keys=keys, strike=strike)
    return 2


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    root = gate_log.repo_root(payload)

    if "--new-prompt" in sys.argv:
        # Operator intervened: a loop is "N blocks with no prompt in between".
        gate_log.clear_state(root)
        gate_log.append_event(root, kind="prompt", phase="end", **gate_log.session_fields(payload))
        return 0

    stop_mode = "--stop" in sys.argv
    released = bool(stop_mode and payload.get("stop_hook_active"))
    dotnet = is_dotnet(root)

    file_path = ""
    if not stop_mode:
        # Extension check FIRST and silent: a README edit is not logged and
        # never emits the skip systemMessage, in both lanes.
        file_path = (payload.get("tool_input") or {}).get("file_path", "") or ""
        if not file_path.endswith(DOTNET_EXTS if dotnet else PYTHON_EXTS):
            return 0

    run = Run(root, payload, "stop" if stop_mode else "edit")
    try:
        return gate(root, run, payload, stop_mode, released, dotnet, file_path)
    except Exception as exc:  # log the crash, keep the traceback and exit 1 as before
        run.end("crash", reason=f"{type(exc).__name__}: {exc}"[:200])
        raise


def gate(
    root: Path, run: Run, payload: dict, stop_mode: bool, released: bool, dotnet: bool, file_path: str
) -> int:
    deadline = time.monotonic() + _env_float("GATE_TIMEOUT", 540.0)
    failures: list[str] = []

    if dotnet:
        if not shutil.which("dotnet"):
            return run.skip("[gate] .NET repo but no dotnet on PATH — gate is NOT running")
        target, reason = find_dotnet_target(root)
        if target is None:
            return run.skip(
                f"[gate] .NET gate skipped ({reason}) — no verification is running. {DOTNET_HINT}"
            )
        code, out = sh(["dotnet", "build", target, "--nologo", "-v", "q"], root, deadline, run.ran)
        if code != 0:
            failures.append(f"dotnet build:\n{out}")
        elif stop_mode:
            code, out = sh(
                ["dotnet", "test", target, "--no-build", "--nologo", "-v", "q"], root, deadline, run.ran
            )
            if code != 0:
                failures.append(f"dotnet test:\n{out}")
    else:
        if not stop_mode:
            # Payloads carry absolute paths in practice; resolve against the
            # repo root so the cache lookup stays correct if that changes.
            edited = Path(file_path)
            clear_stale_pyc(edited if edited.is_absolute() else root / edited)
            if uv_available(root):
                ruff_cmd = ["uv", "run", "ruff"]
            else:
                ruff_cmd = ["ruff"] if shutil.which("ruff") else []
            if ruff_cmd:
                # concise: one `path:line:col: CODE msg` line per finding, which
                # is what gate_log.failure_keys parses regardless of ruff config.
                code, out = sh(
                    [*ruff_cmd, "check", "--fix", "--output-format", "concise", file_path],
                    root,
                    deadline,
                    run.ran,
                )
                if code != 0:
                    failures.append(f"ruff:\n{out}")

        if (root / "tests").is_dir():
            # Mirror the ruff branch: prefer the uv-managed venv, fall back to a
            # pytest on PATH, and if neither exists degrade to a clean skip rather
            # than shelling `python3 -m pytest` — which on a uv-only machine either
            # errors confusingly or runs the wrong global pytest.
            if uv_available(root):
                pytest_cmd = ["uv", "run", "pytest", "-q"]
            elif shutil.which("pytest"):
                pytest_cmd = ["pytest", "-q"]
            else:
                pytest_cmd = []
            if pytest_cmd:
                if not stop_mode:
                    pytest_cmd.append("-x")  # fail fast mid-session; full run at Stop
                code, out = sh(pytest_cmd, root, deadline, run.ran)
                if code != 0:
                    failures.append(f"pytest:\n{out}")
            else:
                sys.stderr.write("[gate] no pytest runner (uv or pytest) found — skipping tests\n")

    return judge(root, run, failures, stop_mode, released)


if __name__ == "__main__":
    sys.exit(main())
