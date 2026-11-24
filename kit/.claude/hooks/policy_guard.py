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

Active only in a repo with Rust in it: a Cargo.toml at the root or one level
down, the same test as the gate's Rust lane. Everywhere else it exits 0 at
once.

Refused (exit 2, the reason on stderr goes back to Claude):
- Bash: `rbox allow`, `rbox urgent`, `rbox export`, `rbox shell`, `rbox net allow`
- Bash: typing into another terminal or window (tmux send-keys and
  paste-buffer, screen -X, xdotool, ydotool, wtype)
- Bash: faking a terminal around rbox (script, expect, unbuffer, socat)
- Bash: touching rbox's state folder, its toolchain or its pin, a build-output
  folder under its cache, or running the kit's test-world wrapper
- Bash: anything but a look, in a command that mentions rbox's cache. The
  downloaded crate sources there are what the user approved. They can be
  read (cat, grep, rg, ls, find, head, wc, jq ...). A command that names the
  cache and also runs something else (rm, cp, tee, a shell, python, xargs
  with any of those, a redirect into the cache) is refused as a whole, so
  that no loop, variable or pipe has to be followed to see where a write
  would land. Run other steps as a separate command.
- Write/Edit/MultiEdit/NotebookEdit: any file inside rbox's own folders

Reading the downloaded crate sources under ~/.cache/rustbox/*/vendor is
fine, and so are `rbox pending`, `rbox sync`, `rbox status`, `rbox clean` and
cargo.

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
from bash_guard import _REDIRECT, _segments, _strip_prefix, _tokens  # noqa: E402

try:
    from gate import is_rust  # noqa: E402
except ImportError:  # gate.py from an older kit release (half-synced repo)
    def is_rust(root: Path) -> bool:
        return (root / "Cargo.toml").is_file()

HUMAN_ONLY = {"allow", "urgent", "export", "shell"}
KEYSTROKE_TOOLS = {"xdotool", "ydotool", "wtype"}
FAKE_TERMINAL_TOOLS = {"script", "expect", "unbuffer", "socat"}
OWN_FOLDERS = (".local/state/rustbox", ".local/share/rustbox", ".cache/rustbox")
ASK = "Only the user can do this, from their own terminal. Tell them what you need and why."

_STATE = re.compile(r"\.local/state/rustbox")
_TOOLCHAIN = re.compile(r"\.local/share/rustbox/toolchain")  # the toolchains, the pin file, the version in use
_CACHE = re.compile(r"(\.cache|XDG_CACHE_HOME\}?)/rustbox(?![\w-])")
# In a command that mentions rbox's cache, every simple command has to be one of these.
LOOK_ONLY = {
    "cat", "ls", "head", "tail", "grep", "egrep", "fgrep", "rg", "find", "wc", "file", "stat", "du", "diff", "cmp",
    "tree", "bat", "sed", "awk", "jq", "cut", "sort", "uniq", "nl", "tr", "column", "strings",
    "sha256sum", "sha1sum", "md5sum", "b2sum", "realpath", "readlink", "basename", "dirname",
    "test", "[", "[[", "cd", "pwd", "echo", "printf", "read", "true", "false", ":", "exit",
}
COMPANIONS = {"rbox", "cargo"}  # fine beside a look, as long as they are not pointed at the cache themselves
SHELL_OPENERS = {"do", "then", "else", "elif", "if", "while", "until", "!", "time", "{", "("}  # dropped from the front
SHELL_ONLY = {"for", "select", "done", "fi", "esac", "}", ")"}  # these run nothing themselves
XARGS_TAKES_VALUE = {"-n", "-L", "-P", "-I", "-d", "-a", "-s", "-E"}
UNIQ_TAKES_VALUE = {"-f", "-s", "-w"}
FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls", "-fprint0"}
_INLINE_WRITE = re.compile(r">\s*[^\s>]*(\.cache|XDG_CACHE_HOME\}?)/rustbox")
_SED_WRITE = re.compile(r"(^|[;{}/\s\d$])[we]\s+\S")
_AWK_WRITE = re.compile(r"print[^;}]*(>|\|)|system\s*\(|\|\s*getline")
READ_ONLY = "rbox's downloaded crate sources are read-only"
READ_ONLY_ADVICE = (
    "A command that mentions rbox's cache may only look (cat, grep, rg, ls, find, head, wc, jq); "
    "run other steps as a separate command. If a crate needs changing, tell the user."
)
_BUILD_OUTPUT = re.compile(r"\.cache/rustbox/[^\s/'\"]+/target")
_TEST_WORLD = re.compile(r"rust/world\.py")


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


def short_flag(word: str, letter: str) -> bool:
    return word.startswith("-") and not word.startswith("--") and letter in word[1:]


def more_than_a_look(name: str, words: list[str]) -> bool:
    """True when one of the look-only tools was asked to write a file or run a program."""
    if name == "sed":
        return any(w.startswith("--in-place") or short_flag(w, "i") or (not w.startswith("-") and _SED_WRITE.search(w))
                   for w in words)
    if name == "find":
        return any(w in FIND_ACTIONS for w in words)
    if name == "sort":
        return any(w.startswith("--output") or short_flag(w, "o") for w in words)
    if name == "uniq":
        files, i = 0, 0
        while i < len(words):
            if words[i] in UNIQ_TAKES_VALUE:
                i += 1
            elif not words[i].startswith("-"):
                files += 1
            i += 1
        return files > 1  # the second file name is where uniq writes
    if name == "awk":
        return "inplace" in words or any(_AWK_WRITE.search(w) for w in words)
    if name == "tree":
        return "-o" in words
    if name == "rg":
        return any(w.startswith("--pre") for w in words)
    if name == "file":
        return any(w in ("-C", "--compile") for w in words)
    return False


def simple_commands(command: str) -> list[list[str]]:
    """Every simple command in a shell line, as a list of words.

    Split at ; & && || | and newlines, and at command substitutions and
    subshells, whose commands are listed on their own. Quotes are honoured:
    an operator inside quotes is text (`grep 'a|b'` is one command), except
    that `$(` and backticks still start a command inside double quotes, as
    they do in the shell. `&>`, `>&` and `2>&1` are redirects and stay in
    their word.
    """
    commands: list[list[str]] = []
    words: list[str] = []
    word: list[str] | None = None  # None: no word in progress
    quote = ""
    stack: list[tuple] = []  # what was being read when a `$(`, `(` or backtick opened

    def end_word() -> None:
        nonlocal word
        if word is not None:
            words.append("".join(word))
            word = None

    def end_command() -> None:
        nonlocal words
        end_word()
        if words:
            commands.append(words)
        words = []

    def open_inner(kind: str) -> None:
        nonlocal words, word, quote
        stack.append((kind, words, word, quote))
        words, word, quote = [], None, ""

    def close_inner() -> None:
        nonlocal words, word, quote
        end_command()
        if stack:
            _, words, word, quote = stack.pop()

    i, size = 0, len(command)
    while i < size:
        char = command[i]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                word.append(char)
            i += 1
        elif char == "\\" and i + 1 < size:
            word = [] if word is None else word
            word.append(command[i + 1])
            i += 2
        elif char == "`":
            if stack and stack[-1][0] == "`":
                close_inner()
            else:
                open_inner("`")
            i += 1
        elif command.startswith("$(", i):
            open_inner("(")
            i += 2
        elif quote == '"':
            if char == '"':
                quote = ""
            else:
                word.append(char)
            i += 1
        elif char in "'\"":
            quote = char
            word = [] if word is None else word
            i += 1
        elif char in " \t":
            end_word()
            i += 1
        elif char == "#" and word is None:
            while i < size and command[i] != "\n":  # a comment
                i += 1
        elif char in ";|\n":
            end_command()
            i += 1
        elif char == "&":
            before = command[i - 1] if i else ""
            after = command[i + 1] if i + 1 < size else ""
            if before == ">" or after == ">":
                word = [] if word is None else word
                word.append(char)
            else:
                end_command()
            i += 1
        elif char == "(":
            end_word()
            open_inner("(")
            i += 1
        elif char == ")":
            close_inner()
            i += 1
        else:
            word = [] if word is None else word
            word.append(char)
            i += 1
    while stack:
        close_inner()
    end_command()
    return commands


def cache_violation(command: str) -> str | None:
    """Why this command, which mentions rbox's cache, is more than a look at it.

    The whole command is judged, not only the part that names the cache: a
    path found there can reach a later command through a pipe, a loop or a
    variable, and following those is guesswork. So every simple command in it
    has to be a look.
    """
    segments = []
    for seg in simple_commands(command):
        seg = _strip_prefix(seg)
        while seg and seg[0] in SHELL_OPENERS:
            seg = _strip_prefix(seg[1:])
        if seg and os.path.basename(seg[0]) == "xargs":
            seg = seg[1:]
            while seg and seg[0].startswith("-"):
                seg = seg[2:] if seg[0] in XARGS_TAKES_VALUE else seg[1:]
        if seg and seg[0] not in SHELL_ONLY:
            segments.append(seg)
    moved = any(os.path.basename(seg[0]) == "cd" for seg in segments)
    for seg in segments:
        name = os.path.basename(seg[0])
        if name in COMPANIONS and not any(_CACHE.search(word) for word in seg[1:]):
            continue
        if name not in LOOK_ONLY:
            return f"{READ_ONLY}, and `{name}` is more than a look"
        if more_than_a_look(name, seg[1:]):
            return f"{READ_ONLY}, and this `{name}` writes a file or runs a program"
        for i, word in enumerate(seg):
            if _INLINE_WRITE.search(word):
                return f"{READ_ONLY}, and this writes into them"
            match = _REDIRECT.match(word)
            if not match:
                continue
            target = match.group(3) or (seg[i + 1] if i + 1 < len(seg) else "")
            if target.startswith("&") or target == "/dev/null":
                continue
            # after a `cd`, or through a variable, there is no telling where a relative target lands
            if _CACHE.search(target) or "$" in target or (moved and not target.startswith(("/", "~"))):
                return f"{READ_ONLY}, and this redirect may write into them"
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
    if _CACHE.search(command):
        reason = cache_violation(command)
        if reason:
            return reason
    if _TEST_WORLD.search(command):
        return "the kit's test world has its own approvals, separate from this machine's"
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
    if not is_rust(root):
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
    advice = READ_ONLY_ADVICE if reason.startswith(READ_ONLY) else ASK
    sys.stderr.write(f"[POLICY GUARD] Refused: {reason}. {advice}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
