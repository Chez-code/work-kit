#!/usr/bin/env python3
"""Bash guard for Claude Code hooks. Stdlib only.

The edit gate only fires on Write/Edit/MultiEdit tool events. A `sed -i`, a
heredoc, a `tee`, an `rm` of a source file through Bash is invisible to it.
This hook watches Bash commands for those shapes.

Warn mode (default; PostToolUse, matcher "Bash"): the command already ran.
The guard logs a `bash` event, runs gate.py on the touched file(s) so the
change is verified after all, and reports the result:
  - every gate run green  -> exit 0, one JSON line with a systemMessage for
    the operator and additionalContext for Claude (nothing is blocked)
  - a gate run blocked    -> exit 2, the gate's own failure text on stderr,
    which PostToolUse shows to Claude as the thing to fix
  - gate crashed / budget -> exit 0, systemMessage says so

Block mode (`--block`; meant for a PreToolUse entry): refuse the command
outright with exit 2 and a one-line reason. Not wired by default.

Target rule: a path counts only if it has a source extension AND resolves
inside the repo root. Scratch files under /tmp and the like are ignored.
In the Python lane a target must also exist (ruff needs the file); in the
.NET lane the build is repo-wide so existence is not checked.

Time budget: one deadline for the whole guard (GATE_TIMEOUT, default 540),
the remainder handed to each gate subprocess as its own GATE_TIMEOUT, so two
targets cannot add up past the hook timeout.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before the sibling imports

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_log  # noqa: E402
from gate import DOTNET_EXTS, PYTHON_EXTS, is_dotnet  # noqa: E402

SOURCE_EXTS = (
    ".py", ".cs", ".csproj", ".sln", ".slnx", ".md", ".json", ".toml", ".yaml", ".yml",
    ".cfg", ".ini", ".txt", ".ts", ".js", ".sh",
)
SEPARATORS = {"&&", "||", ";", "|", "&"}
# Shapes that block mode refuses: each has an Edit/Write equivalent Claude can
# use instead. Deletes and moves have no file-tool equivalent, so block mode
# lets them through and the PostToolUse (warn) entry verifies them afterwards.
BLOCKABLE = {"in-place edit", "redirect into source", "tee into source", "truncate source", "script write"}
_REDIRECT = re.compile(r"^(\d*)(>>|>\||&>|>)(.*)$")
_SCRIPT_WRITE = re.compile(r"""open\s*\(.*['"](w|a|wb|ab|w\+|a\+)['"]""")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# A bare `&` splits a segment, but not one touching a `>`: `&>` and `2>&1`
# must stay whole so the redirect regex can read them.
_SEP_SPLIT = re.compile(r"(&&|\|\||;|\||(?<!>)&(?!>))")


def _tokens(command: str) -> list[str]:
    """shlex tokens, with list/pipe operators split off the words they touch.

    shlex keeps `src/x.cs;` or `a&&b` as one token; the guard needs `;` and
    `&&` as separate tokens to see the second command. Splitting inside a
    quoted string (`-m 'x; y'`) only produces an extra harmless segment.
    """
    try:
        raw = shlex.split(command, posix=True)
    except ValueError:
        raw = command.split()
    out: list[str] = []
    for tok in raw:
        if tok in SEPARATORS:
            out.append(tok)
            continue
        out.extend(part for part in _SEP_SPLIT.split(tok) if part)
    return out


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a pipeline/list into simple commands at && || ; | &."""
    segs: list[list[str]] = [[]]
    for tok in tokens:
        if tok in SEPARATORS:
            segs.append([])
        else:
            segs[-1].append(tok)
    return [s for s in segs if s]


def _strip_prefix(seg: list[str]) -> list[str]:
    """Drop leading VAR=value assignments and sudo/env wrappers."""
    i = 0
    while i < len(seg) and (_ENV_ASSIGN.match(seg[i]) or seg[i] in ("sudo", "env", "command")):
        i += 1
    return seg[i:]


def _positionals(seg: list[str], takes_arg: set[str]) -> tuple[list[str], set[str]]:
    """Non-flag arguments after the command name, and the set of flags seen.

    `takes_arg` names flags whose next token is their argument (skipped).
    """
    flags: set[str] = set()
    pos: list[str] = []
    skip = False
    for tok in seg[1:]:
        if skip:
            skip = False
            continue
        if tok == "--":
            continue
        if tok.startswith("-") and len(tok) > 1:
            flags.add(tok)
            if tok in takes_arg:
                skip = True
            continue
        pos.append(tok)
    return pos, flags


def _short_flag_has(flags: set[str], letter: str) -> bool:
    return any(f.startswith("-") and not f.startswith("--") and letter in f[1:] for f in flags)


class Guard:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def is_target(self, path: str, cwd: Path) -> str | None:
        """Absolute path when `path` is a source file inside the repo, else None."""
        if not path or path == "/dev/null" or path.startswith("-"):
            return None
        if not path.endswith(SOURCE_EXTS):
            return None
        try:
            resolved = Path(os.path.realpath(str(cwd / path) if not os.path.isabs(path) else path))
        except OSError:
            return None
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return None
        return str(resolved)

    def find(self, command: str) -> list[tuple[str, str | None]]:
        """[(label, absolute target or None)] for every risky shape in the command."""
        found: list[tuple[str, str | None]] = []
        cwd = self.root
        for seg in _segments(_tokens(command)):
            seg = _strip_prefix(seg)
            if not seg:
                continue
            cmd = os.path.basename(seg[0])

            if cmd == "cd":
                cwd = (cwd / seg[1]) if len(seg) > 1 and not seg[1].startswith("-") else self.root
                continue

            # redirects apply to any command in the segment, and a bare
            # `> file.py` (truncation) has the operator as its first word
            for idx, tok in enumerate(seg):
                m = _REDIRECT.match(tok)
                if not m:
                    continue
                target = m.group(3)
                if not target:
                    target = seg[idx + 1] if idx + 1 < len(seg) else ""
                t = self.is_target(target, cwd)
                if t:
                    found.append(("redirect into source", t))

            if cmd == "sed":
                pos, flags = _positionals(seg, {"-e", "-f", "--expression", "--file"})
                inplace = _short_flag_has(flags, "i") or any(f.startswith("--in-place") for f in flags)
                if inplace:
                    if not any(f in ("-e", "-f", "--expression", "--file") or f.startswith("--expression=") or f.startswith("--file=") for f in flags):
                        pos = pos[1:]  # first positional is the script
                    for p in pos:
                        t = self.is_target(p, cwd)
                        if t:
                            found.append(("in-place edit", t))
            elif cmd == "perl":
                pos, flags = _positionals(seg, {"-e", "-E"})
                inplace = _short_flag_has(flags, "i")
                if inplace:
                    if not any(f in ("-e", "-E") for f in flags):
                        pos = pos[1:]  # script file
                    for p in pos:
                        t = self.is_target(p, cwd)
                        if t:
                            found.append(("in-place edit", t))
            elif cmd == "tee":
                pos, _ = _positionals(seg, set())
                for p in pos:
                    t = self.is_target(p, cwd)
                    if t:
                        found.append(("tee into source", t))
            elif cmd == "truncate":
                pos, _ = _positionals(seg, {"-s", "--size", "-r", "--reference"})
                for p in pos:
                    t = self.is_target(p, cwd)
                    if t:
                        found.append(("truncate source", t))
            elif cmd in ("mv", "cp"):
                pos, _ = _positionals(seg, {"-t", "--target-directory"})
                if pos:
                    t = self.is_target(pos[-1], cwd)
                    if t:
                        found.append(("file move/copy", t))
            elif cmd == "rm":
                pos, _ = _positionals(seg, set())
                for p in pos:
                    t = self.is_target(p, cwd)
                    if t:
                        found.append(("delete source", t))
            elif cmd in ("python", "python3", "ruby", "node"):
                if ("-c" in seg or "-e" in seg or "-" in seg) and _SCRIPT_WRITE.search(command):
                    found.append(("script write", None))
        # dedupe, keep order
        seen: set[tuple[str, str | None]] = set()
        out: list[tuple[str, str | None]] = []
        for item in found:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out


def _rel(root: Path, path: str | None) -> str:
    if path is None:
        return "?"
    try:
        return str(Path(path).relative_to(root.resolve()))
    except ValueError:
        return path


def run_gate(root: Path, payload: dict, target: str, deadline: float) -> tuple[int, str]:
    """Invoke gate.py on one path with the remaining budget. Returns (exit code, stderr)."""
    remaining = max(1.0, deadline - time.monotonic())
    synthetic = {
        "cwd": str(root),
        "session_id": payload.get("session_id"),
        "agent_id": payload.get("agent_id"),
        "tool_name": "Bash",
        "tool_input": {"file_path": target},
    }
    env = dict(os.environ)
    env["GATE_TIMEOUT"] = str(int(remaining))
    env["CLAUDE_PROJECT_DIR"] = str(root)
    gate_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate.py")
    try:
        proc = subprocess.run(
            [sys.executable, gate_py],
            input=json.dumps(synthetic),
            capture_output=True,
            text=True,
            cwd=str(root),
            env=env,
            timeout=remaining + 5,
        )
    except subprocess.TimeoutExpired:
        return 124, "gate re-run timed out"
    except OSError as exc:
        return 1, f"could not run gate.py: {exc}"
    return proc.returncode, proc.stderr


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    root = gate_log.repo_root(payload)
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not command:
        return 0

    matches = Guard(root).find(command)
    if not matches:
        return 0

    labels = []
    for label, _ in matches:
        if label not in labels:
            labels.append(label)
    files = [p for _, p in matches if p]
    first_rel = _rel(root, files[0] if files else None)
    short_cmd = command[:120]
    base = {"kind": "bash", "phase": "end", "command": short_cmd, "file": first_rel}
    base.update(gate_log.session_fields(payload))

    if "--block" in sys.argv:
        # PreToolUse: the command has NOT run yet. Refuse only the shapes that
        # have an Edit/Write equivalent; anything else (rm, mv, cp) proceeds and
        # the PostToolUse entry warns and re-runs the gate after it.
        blockable = [label for label in labels if label in BLOCKABLE]
        if not blockable:
            return 0
        gate_log.append_event(root, verdict="block", reason=", ".join(blockable), **base)
        sys.stderr.write(
            f"[BASH GUARD] Blocked: `{short_cmd}`. Source changes go through the Edit or "
            "Write tool so the gate can see them. Make this change with Edit instead.\n"
        )
        return 2

    deadline = time.monotonic() + _budget()
    dotnet = is_dotnet(root)

    # Decide which targets get a gate re-run.
    to_run: list[str] = []
    notes: list[str] = []
    if dotnet:
        buildable = [p for p in files if p.endswith(DOTNET_EXTS)]
        if buildable:
            to_run = [buildable[0]]  # the build is repo-wide: once is enough
        others = [p for p in files if not p.endswith(DOTNET_EXTS)]
        if others and not buildable:
            notes.append("not gated")
    else:
        for p in files:
            if p.endswith(PYTHON_EXTS):
                if os.path.exists(p):
                    to_run.append(p)
                else:
                    notes.append("target not found")
            else:
                notes.append("not gated")
    if not files:
        notes.append("target unknown")

    outcome = "not gated"
    failed_text = ""
    if to_run:
        outcome = "green"
        for target in to_run:
            code, err = run_gate(root, payload, target, deadline)
            if code == 2:
                outcome = "loop" if "[LOOP DETECTED]" in err else "block"
                failed_text = err
                break
            if code != 0:
                outcome = "timed out" if code == 124 else "crashed"
                failed_text = err
                break
    elif "target not found" in notes:
        outcome = "target not found"

    reason = ", ".join(labels)
    if notes and outcome in ("not gated", "target not found"):
        reason += ", " + ", ".join(dict.fromkeys(notes))
    gate_log.append_event(root, verdict="warn", reason=reason, gate=outcome, **base)

    if outcome in ("block", "loop"):
        sys.stderr.write(f"[bash guard] the Bash edit to {first_rel} failed the gate:\n{failed_text}")
        return 2

    what = f"Bash edit to {first_rel} ({labels[0]})"
    if labels[0] in BLOCKABLE:
        context = (
            f"The Bash command you just ran changed {first_rel} ({labels[0]}). "
            f"The edit gate does not see Bash changes, so the guard ran it for you: {outcome}. "
            "Use the Edit or Write tool for source changes so they are verified directly."
        )
    else:
        # delete / move / copy: there is no file tool for these, so no "use Edit" advice
        context = (
            f"The Bash command you just ran {'removed' if labels[0] == 'delete source' else 'moved or copied'} "
            f"{first_rel} ({labels[0]}). The edit gate does not see Bash changes, so the guard ran "
            f"it for you: {outcome}."
        )
    print(
        json.dumps(
            {
                "systemMessage": f"[bash guard] {what}, gate re-run: {outcome}",
                "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context},
            }
        )
    )
    return 0


def _budget() -> float:
    try:
        return float(os.environ.get("GATE_TIMEOUT", "540"))
    except ValueError:
        return 540.0


if __name__ == "__main__":
    sys.exit(main())
