#!/usr/bin/env python3
"""Policy guard for Claude Code hooks (PreToolUse). Stdlib only.

In a Rust repo that uses rbox, some things are the operator's alone:
approving a crate (`rbox allow`), pulling a version past the cooldown
(`rbox urgent`), copying a built program out of the box (`rbox export`),
and allowing a network host (`rbox net allow`). rbox itself refuses them
without a real terminal. This hook refuses the ways around that before they
run, and keeps Claude out of rbox's own folders.

It is a guard rail, not a wall: Claude runs as the same user, so a
determined process could still reach those files. The point is that it
cannot happen casually or by accident. The `permissions.deny` rules in
settings.json cover the same ground and hold in every permission mode.

Active only when the repo has a Cargo.toml at its root (the same test as the
gate's Rust lane); everywhere else it exits 0 at once.

Refused (exit 2, the reason on stderr goes back to Claude):
- Bash: `rbox allow`, `rbox urgent`, `rbox export`, `rbox net allow`
- Bash: typing into another terminal or window (tmux send-keys and
  paste-buffer, screen -X, xdotool, ydotool, wtype)
- Bash: faking a terminal around rbox (script, expect, unbuffer, socat)
- Bash: touching rbox's state folder, its toolchain, a build-output folder
  under its cache, or switching it to a test world
- Write/Edit/MultiEdit/NotebookEdit: any file inside rbox's own folders

Reading the downloaded crate sources under ~/.cache/rustbox/*/vendor is
fine, and so are `rbox pending`, `rbox sync`, `rbox status` and cargo.

Exit 0 -> proceed.  Exit 2 -> refused.  A crash exits 1, which does not block.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before the sibling imports

import json
import os
import re
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_log  # noqa: E402
from bash_guard import _segments, _strip_prefix, _tokens  # noqa: E402

HUMAN_ONLY = {"allow", "urgent", "export"}
KEYSTROKE_TOOLS = {"xdotool", "ydotool", "wtype"}
FAKE_TERMINAL_TOOLS = {"script", "expect", "unbuffer", "socat"}
OWN_FOLDERS = (".local/state/rustbox", ".local/share/rustbox", ".cache/rustbox")
ASK = "Only the user can do this, from their own terminal. Tell them what you need and why."

_STATE = re.compile(r"\.local/state/rustbox")
_TOOLCHAIN = re.compile(r"\.local/share/rustbox/toolchains")
_BUILD_OUTPUT = re.compile(r"\.cache/rustbox/[^\s/'\"]+/target")
_TEST_WORLD = re.compile(r"\bRBOX_TEST_BASE\b")


def is_rbox(word: str) -> bool:
    return os.path.basename(word) in ("rbox", "rbox.py")


def rbox_action(seg: list[str]) -> str | None:
    """The human-only rbox action this simple command asks for, if any."""
    if not seg or not is_rbox(seg[0]):
        return None
    words = seg[1:]
    while words and words[0] in ("--net", "--env"):
        words = words[2:] if words[0] == "--env" else words[1:]
    if not words:
        return None
    if words[0] in HUMAN_ONLY:
        return words[0]
    if words[0] == "net" and len(words) > 1 and words[1] == "allow":
        return "net allow"
    return None


WRAPPERS = {
    "sh", "bash", "zsh", "dash", "eval", "watch", "script", "expect", "tmux", "screen", "nohup", "setsid",
    "timeout", "xargs", "su", "nice", "ionice", "stdbuf", "unbuffer", "parallel", "socat", "systemd-run",
}


def check_command(command: str, depth: int = 0) -> str | None:
    """Why this Bash command is refused, or None."""
    mentions_rbox = bool(re.search(r"\brbox\b", command))
    for seg in _segments(_tokens(command)):
        seg = _strip_prefix(seg)
        if not seg:
            continue
        if len(seg) == 1 and " " in seg[0].strip() and depth < 3:
            # the rest of a quoted command after an operator: `bash -c "cd x && rbox allow"`
            inner = check_command(seg[0], depth + 1)
            if inner:
                return inner
            continue
        name = os.path.basename(seg[0])
        # rbox anywhere in the simple command, so `timeout 5 rbox allow`,
        # `env -i rbox allow` and `find . -exec rbox allow` are all seen
        for i, word in enumerate(seg):
            if is_rbox(word):
                action = rbox_action(seg[i:])
                if action:
                    return f"`rbox {action}` is the user's decision"
        if name == "tmux" and any(word in ("send-keys", "send", "paste-buffer", "pasteb") for word in seg[1:]):
            return "typing into another terminal pane"
        if name == "screen" and "-X" in seg[1:]:
            return "typing into another terminal session"
        if name in KEYSTROKE_TOOLS:
            return f"`{name}` types into other windows"
        if name in FAKE_TERMINAL_TOOLS and mentions_rbox:
            return f"`{name}` around rbox fakes a terminal"
        if name in WRAPPERS and depth < 3:
            # a quoted command handed to a shell or wrapper: `sh -c "rbox allow"`
            for word in seg[1:]:
                if " " in word:
                    inner = check_command(word, depth + 1)
                    if inner:
                        return inner
    if _STATE.search(command):
        return "that is where rbox keeps the crates the user approved"
    if _TOOLCHAIN.search(command):
        return "the Rust toolchain is only run through rbox (use `cargo`, it is boxed)"
    if _BUILD_OUTPUT.search(command):
        return "build output is only run or copied through rbox (`rbox run`, or the user's `rbox export`)"
    if _TEST_WORLD.search(command) and mentions_rbox:
        return "a test world has its own approvals, separate from this machine's"
    if mentions_rbox and re.search(r"\bCLAUDE(CODE|_CODE_\w+)\b", command):
        return "hiding the Claude session from rbox"
    return None


def check_path(file_path: str, home: Path) -> str | None:
    """Why editing this file is refused, or None."""
    if not file_path:
        return None
    try:
        resolved = Path(os.path.realpath(os.path.expanduser(file_path)))
    except OSError:
        return None
    for folder in OWN_FOLDERS:
        own = home / folder
        if resolved == own or own in resolved.parents:
            return "that is one of rbox's own folders"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        return 0
    root = gate_log.repo_root(payload)
    if not (root / "Cargo.toml").is_file():
        return 0

    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    if tool == "Bash":
        subject = str(tool_input.get("command") or "")
        reason = check_command(subject)
    else:
        subject = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        reason = check_path(subject, Path(os.path.expanduser("~")).resolve())
    if not reason:
        return 0

    fields = {"kind": "policy", "phase": "end", "verdict": "block", "reason": reason, "command": subject[:120]}
    fields.update(gate_log.session_fields(payload))
    gate_log.append_event(root, **fields)
    sys.stderr.write(f"[POLICY GUARD] Refused: {reason}. {ASK}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
