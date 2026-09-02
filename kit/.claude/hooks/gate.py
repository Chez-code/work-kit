#!/usr/bin/env python3
"""Verification gate for Claude Code hooks. Stdlib only.

Auto-detects the stack:
- Python (pyproject.toml): PostToolUse ruff-fixes the touched .py file and runs
  fast tests; Stop runs the full suite.
- .NET (.sln/.csproj): PostToolUse builds after each .cs edit (tests are too
  slow mid-session); Stop builds and runs the full test suite.

Exit 0  -> proceed.
Exit 2  -> blocked; stderr is fed back to Claude so it fixes its own mess.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

MAX_LINES = 40  # keep feedback concise — Claude needs the failure, not the novel


def is_dotnet(root: Path) -> bool:
    """A solution or project file at (or one level under) the root marks a .NET repo."""
    for pattern in ("*.sln", "*.csproj", "*/*.csproj"):
        if any(root.glob(pattern)):
            return True
    return False


def uv_available(root: Path) -> bool:
    """Project tools (ruff, pytest) live in the uv-managed venv, not on PATH."""
    return bool(shutil.which("uv")) and (root / "pyproject.toml").is_file()


def sh(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
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

    failures: list[str] = []

    if is_dotnet(root):
        if not shutil.which("dotnet"):
            sys.stderr.write("[gate] .NET repo but no dotnet on PATH — skipping\n")
            return 0
        if not stop_mode:
            file_path = (payload.get("tool_input") or {}).get("file_path", "")
            if not file_path.endswith((".cs", ".csproj", ".sln")):
                return 0
        code, out = sh(["dotnet", "build", "--nologo", "-v", "q"], root)
        if code != 0:
            failures.append(f"dotnet build:\n{out}")
        elif stop_mode:
            code, out = sh(["dotnet", "test", "--nologo", "-v", "q"], root)
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
                code, out = sh([*ruff_cmd, "check", "--fix", file_path], root)
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
                code, out = sh(pytest_cmd, root)
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
