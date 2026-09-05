#!/usr/bin/env python3
"""Verification gate for Claude Code hooks. Stdlib only.

Auto-detects the stack:
- Python (pyproject.toml): PostToolUse ruff-fixes the touched .py file and runs
  fast tests; Stop runs the full suite.
- .NET (.sln/.slnx/.csproj at the root or one level down): PostToolUse builds
  after each .cs edit (tests are too slow mid-session); Stop builds, then runs
  `dotnet test --no-build`. NOTE: --no-build against a target with no test
  projects exits 0 — a green Stop means "build ok + any tests that exist
  passed", not "tests exist"; add a test project early.

Environment:
- GATE_DOTNET_TARGET: explicit .sln/.slnx/.csproj to build when auto-detection
  finds zero or several candidates (multi-solution monorepos).
- GATE_TIMEOUT: total time budget in seconds for ALL subprocesses in one gate
  run combined (default 540). Keep it below the hook-level timeout (600 in
  settings.json): a hang must be caught HERE, as a blocking failure, before
  the harness kills the hook — a harness kill is non-blocking and would let
  the hang pass silently.

Exit 0  -> proceed.
Exit 2  -> blocked; stderr is fed back to Claude so it fixes its own mess.

stdout discipline: on exit 0, Claude Code parses hook stdout as JSON. The skip
paths print exactly one systemMessage object; nothing else may EVER write to
stdout, or that message silently stops rendering.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_LINES = 40  # keep feedback concise — Claude needs the failure, not the novel

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


def uv_available(root: Path) -> bool:
    """Project tools (ruff, pytest) live in the uv-managed venv, not on PATH."""
    return bool(shutil.which("uv")) and (root / "pyproject.toml").is_file()


def sh(cmd: list[str], cwd: Path, deadline: float) -> tuple[int, str]:
    """Run cmd with whatever remains of the whole-gate time budget."""
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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}

    root = Path(payload.get("cwd") or Path.cwd())
    stop_mode = "--stop" in sys.argv

    # Stop hooks must not loop forever: if we already blocked once, let go.
    if stop_mode and payload.get("stop_hook_active"):
        return 0

    try:
        budget = float(os.environ.get("GATE_TIMEOUT", "540"))
    except ValueError:
        budget = 540.0
    deadline = time.monotonic() + budget

    failures: list[str] = []

    if is_dotnet(root):
        if not stop_mode:
            # Extension check FIRST: a non-code edit must return silently, like
            # the Python lane — never emit the skip systemMessage for a README.
            file_path = (payload.get("tool_input") or {}).get("file_path", "")
            if not file_path.endswith((".cs", ".csproj", ".sln", ".slnx")):
                return 0
        if not shutil.which("dotnet"):
            return skip_visibly("[gate] .NET repo but no dotnet on PATH — gate is NOT running")
        target, reason = find_dotnet_target(root)
        if target is None:
            return skip_visibly(
                f"[gate] .NET gate skipped ({reason}) — no verification is running. {DOTNET_HINT}"
            )
        code, out = sh(["dotnet", "build", target, "--nologo", "-v", "q"], root, deadline)
        if code != 0:
            failures.append(f"dotnet build:\n{out}")
        elif stop_mode:
            code, out = sh(
                ["dotnet", "test", target, "--no-build", "--nologo", "-v", "q"], root, deadline
            )
            if code != 0:
                failures.append(f"dotnet test:\n{out}")
    else:
        if not stop_mode:
            file_path = (payload.get("tool_input") or {}).get("file_path", "")
            if not file_path.endswith(".py"):
                return 0  # only gate Python edits
            if uv_available(root):
                ruff_cmd = ["uv", "run", "ruff"]
            else:
                ruff_cmd = ["ruff"] if shutil.which("ruff") else []
            if ruff_cmd:
                code, out = sh([*ruff_cmd, "check", "--fix", file_path], root, deadline)
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
                code, out = sh(pytest_cmd, root, deadline)
                if code != 0:
                    failures.append(f"pytest:\n{out}")
            else:
                sys.stderr.write("[gate] no pytest runner (uv or pytest) found — skipping tests\n")

    if failures:
        label = "STOP GATE" if stop_mode else "EDIT GATE"
        sys.stderr.write(f"[{label} FAILED] Fix before proceeding.\n" + "\n\n".join(failures))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
