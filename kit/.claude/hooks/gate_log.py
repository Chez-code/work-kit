#!/usr/bin/env python3
"""Shared helpers for the work-kit hooks: event log, failure keys, loop state.

Imported by gate.py and bash_guard.py, which live in the same directory.
Stdlib only. Nothing here may raise into the caller: the gate's verdict must
never depend on the log or the state file, so every I/O path swallows
OSError and every parse path degrades to "empty".

The log (`.claude/gate-log.jsonl`) is a plain file inside a work repo. It
holds paths, the first 120 characters of guarded Bash commands, verdicts,
durations and failure keys. It never holds prompt text or tool output
beyond the keys.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

LOG_REL = Path(".claude") / "gate-log.jsonl"
STATE_REL = Path(".claude") / "gate-state.json"

KEY_MAX = 80  # characters kept from a matched failure line


# ---------------------------------------------------------------- basics


def repo_root(payload: dict) -> Path:
    """CLAUDE_PROJECT_DIR when set, else the payload cwd, else the process cwd.

    Claude's Bash shell keeps its working directory between calls, so the
    payload cwd can be a subdirectory after a `cd`. The env var is what the
    hook command strings already rely on to find these scripts.
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    cwd = payload.get("cwd")
    if cwd:
        return Path(cwd)
    return Path.cwd()


def now_iso() -> str:
    """Local time with the UTC offset, so `--since` survives a DST change."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_run_id() -> str:
    return uuid.uuid4().hex


def session_fields(payload: dict) -> dict:
    fields = {"session": str(payload.get("session_id") or "")[:8]}
    agent = payload.get("agent_id")
    if agent:
        fields["agent"] = str(agent)
    return fields


def edit_delta(tool_input: dict) -> int | None:
    """Size of a file-tool edit: Edit = new - old, Write = content, MultiEdit = sum."""
    if not tool_input:
        return None
    if "edits" in tool_input and isinstance(tool_input["edits"], list):
        total = 0
        for e in tool_input["edits"]:
            if isinstance(e, dict):
                total += len(str(e.get("new_string", ""))) - len(str(e.get("old_string", "")))
        return total
    if "content" in tool_input:
        return len(str(tool_input["content"]))
    if "new_string" in tool_input or "old_string" in tool_input:
        return len(str(tool_input.get("new_string", ""))) - len(str(tool_input.get("old_string", "")))
    return None


# ------------------------------------------------------------------- log


def append_event(root: Path, **fields) -> None:
    """Append one JSON line. One write() call so concurrent hooks do not interleave."""
    event = {"ts": now_iso()}
    event.update({k: v for k, v in fields.items() if v is not None})
    try:
        path = root / LOG_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(root: Path) -> list[dict]:
    """Every parseable line, in file order. Bad lines are skipped."""
    events: list[dict] = []
    try:
        with open(root / LOG_REL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
    except OSError:
        pass
    return events


# ---------------------------------------------------------- failure keys

_PYTEST_FAILED = re.compile(r"^FAILED (\S+)")
_PYTEST_FAILED_VERBOSE = re.compile(r"^(\S+::\S+) FAILED\b")
_PYTEST_ERROR = re.compile(r"^ERROR (\S+)")
_RUFF_CONCISE = re.compile(r"^(\S+?):\d+:\d+: ([A-Z]+\d{3,4})\b")
_RUFF_FULL_CODE = re.compile(r"^([A-Z]+\d{3,4})\b")
_RUFF_FULL_LOC = re.compile(r"-->\s*(\S+?):\d+:\d+")
_DOTNET_BUILD = re.compile(r"error ((?:CS|MSB|NU|NETSDK)\d+): (.+?)(?:\s\[|$)")
_DOTNET_TEST = re.compile(r"^\s*Failed (\S+)")
_TIMEOUT = re.compile(r"timed out after \d+s")
_ERRORISH = re.compile(r"error|fail", re.IGNORECASE)


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text[:KEY_MAX]


def _fallback_key(text: str) -> str:
    """Last error-ish line, else the last line; digits stripped so timings do not churn."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "raw:"
    chosen = next((ln for ln in reversed(lines) if _ERRORISH.search(ln)), lines[-1])
    return "raw:" + _cut(re.sub(r"\d", "", chosen))


def failure_keys(text: str) -> list[str]:
    """Every failure the gate output names, deduplicated, in order of appearance.

    The loop detector compares the SET of keys between runs: an unchanged set
    is a strike, a changed set is progress even if one error persists.
    """
    keys: list[str] = []
    seen: set[str] = set()

    def add(key: str) -> None:
        if key and key not in seen:
            seen.add(key)
            keys.append(key)

    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        m = _PYTEST_FAILED.match(line) or _PYTEST_FAILED_VERBOSE.match(line)
        if m:
            add(_cut(m.group(1)))
            continue
        m = _PYTEST_ERROR.match(line)
        if m:
            add("error:" + _cut(m.group(1)))
            continue
        m = _RUFF_CONCISE.match(line)
        if m:
            add(f"ruff:{m.group(2)}:{_cut(m.group(1))}")
            continue
        m = _DOTNET_BUILD.search(line)
        if m:
            add(_cut(f"{m.group(1)}: {m.group(2)}"))
            continue
        m = _DOTNET_TEST.match(line)
        if m:
            add(_cut(m.group(1)))
            continue
        if _TIMEOUT.search(line):
            add("timeout")
            continue
        m = _RUFF_FULL_CODE.match(line)
        if m:
            for follow in lines[i + 1 : i + 4]:
                loc = _RUFF_FULL_LOC.search(follow)
                if loc:
                    add(f"ruff:{m.group(1)}:{_cut(loc.group(1))}")
                    break
    if not keys:
        add(_fallback_key(text))
    return keys


# ----------------------------------------------------------- loop state


def load_state(root: Path) -> dict:
    """{"keys": [...], "strike": int, "times": [...]}; anything unreadable is empty."""
    empty = {"keys": [], "strike": 0, "times": []}
    try:
        with open(root / STATE_REL, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return empty
    if not isinstance(obj, dict):
        return empty
    keys = obj.get("keys")
    strike = obj.get("strike")
    times = obj.get("times")
    if not isinstance(keys, list) or not isinstance(strike, int) or not isinstance(times, list):
        return empty
    return {"keys": [str(k) for k in keys], "strike": strike, "times": [str(t) for t in times]}


def save_state(root: Path, state: dict) -> None:
    """Temp file in the same directory, then os.replace: readers never see a torn file."""
    path = root / STATE_REL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".gate-state.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except OSError:
        pass


def clear_state(root: Path) -> None:
    try:
        os.unlink(root / STATE_REL)
    except FileNotFoundError:
        pass
    except OSError:
        pass
