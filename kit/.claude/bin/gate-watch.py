#!/usr/bin/env python3
"""gate-watch: the operator's pane. Stdlib only.

Tails .claude/gate-log.jsonl (written by gate.py and bash_guard.py) and prints
one line per gate event, colored by what it means for the operator:

  plain   green edits and stops, PROMPT (strikes reset), released-and-green
  yellow  BLOCK, bash WARN, green with nothing run, heuristic warnings
  red     LOOP, SKIP, CRASH, KILLED, RELEASED still failing

Yellow and red ring the bell. Red also flashes a tmux message and turns the
status bar red until the next green Stop or the next prompt. The original
status-style is saved to .claude/gate-watch.status-style before it is
changed and restored from that file, so a watcher that dies while red does
not learn "red" as the baseline on restart.

Usage:
  gate-watch.py                 tail from the current end of the log
  gate-watch.py --from-start    replay the whole log first
  gate-watch.py --no-tmux       never touch tmux (bell only)
  gate-watch.py --stats [--since 7d|24h|N]   baseline numbers (N = days)

Run it from the repo root (a tmux pane opened with prefix+W does that), or
set CLAUDE_PROJECT_DIR.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOG_REL = Path(".claude") / "gate-log.jsonl"
STYLE_REL = Path(".claude") / "gate-watch.status-style"
KILL_AFTER = 610.0  # seconds; the hook timeout is 600
POLL = 0.5

RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def repo_root() -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env) if env else Path.cwd()


def parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def base(path: str | None) -> str:
    if not path:
        return ""
    return os.path.basename(path.rstrip("/")) or path


def ran_summary(ran: list | None) -> str:
    names: list[str] = []
    for cmd in ran or []:
        toks = str(cmd).split()
        if not toks:
            continue
        if base(toks[0]) == "dotnet" and len(toks) > 1:
            names.append(toks[1])
        elif "ruff" in toks:
            names.append("ruff")
        elif "pytest" in toks:
            names.append("pytest")
        else:
            names.append(base(toks[0]))
    return ", ".join(names) if names else "nothing ran"


# ------------------------------------------------------------------ tmux


class Tmux:
    def __init__(self, root: Path, enabled: bool) -> None:
        self.root = root
        self.enabled = enabled and bool(os.environ.get("TMUX")) and bool(shutil.which("tmux"))
        self.red = False

    def _run(self, *args: str) -> str:
        try:
            proc = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout.strip()

    def alert(self, text: str) -> None:
        if self.enabled:
            self._run("display-message", "-d", "4000", text[:200])

    def repaint_red(self) -> None:
        """Watcher restarted while the bar was red: keep it red until a restore."""
        if self.enabled and (self.root / STYLE_REL).exists():
            self._run("set", "-g", "status-style", "bg=red,fg=white")
            self.red = True

    def go_red(self) -> None:
        if not self.enabled or self.red:
            return
        style_file = self.root / STYLE_REL
        if not style_file.exists():
            baseline = self._run("show", "-gv", "status-style") or "default"
            try:
                style_file.parent.mkdir(parents=True, exist_ok=True)
                style_file.write_text(baseline + "\n", encoding="utf-8")
            except OSError:
                return  # cannot save the baseline: do not change the bar
        self._run("set", "-g", "status-style", "bg=red,fg=white")
        self.red = True

    def restore(self) -> None:
        if not self.enabled:
            return
        style_file = self.root / STYLE_REL
        if not style_file.exists():
            self.red = False
            return
        try:
            baseline = style_file.read_text(encoding="utf-8").strip() or "default"
        except OSError:
            baseline = "default"
        self._run("set", "-g", "status-style", baseline)
        try:
            style_file.unlink()
        except OSError:
            pass
        self.red = False


# --------------------------------------------------------------- watcher


class Watcher:
    def __init__(self, root: Path, tmux: Tmux, color: bool) -> None:
        self.root = root
        self.tmux = tmux
        self.color = color
        self.pending: dict[str, dict] = {}
        self.history: dict[str, list[dict]] = defaultdict(list)  # session -> edit ends since prompt
        self.last_verdict: dict[str, str] = {}
        self.keys: dict[str, list[str]] = {}
        tmux.repaint_red()  # a previous watcher may have died red; stay red until a restore

    # -- output

    def _emit(self, text: str, level: str = "plain") -> None:
        if level == "red":
            line = f"{RED}{text}{RESET}" if self.color else text
            sys.stdout.write(line + "\a\n")
            self.tmux.alert(text)
            self.tmux.go_red()
        elif level == "yellow":
            line = f"{YELLOW}{text}{RESET}" if self.color else text
            sys.stdout.write(line + "\a\n")
        else:
            sys.stdout.write(text + "\n")
        sys.stdout.flush()

    @staticmethod
    def _fmt(ts: str, kind: str, file: str, verdict: str, dur: str, detail: str, sub: bool) -> str:
        tag = " [sub]" if sub else ""
        return f"{ts[11:19]} {kind:<6} {base(file):<18} {verdict:<24} {dur:>5}  {detail}{tag}".rstrip()

    # -- events

    def handle(self, ev: dict) -> None:
        kind = ev.get("kind")
        session = str(ev.get("session") or "")
        sub = bool(ev.get("agent"))
        ts = str(ev.get("ts") or "")

        if kind == "prompt":
            self.history[session] = []
            self.last_verdict[session] = ""
            self.keys[session] = []
            self._emit(self._fmt(ts, "PROMPT", "", "", "", "strikes reset", sub))
            self.tmux.restore()
            return

        if ev.get("phase") == "start":
            run = ev.get("run")
            if run:
                self.pending[str(run)] = ev
            return

        if kind == "bash":
            reason = str(ev.get("reason") or "")
            cmd = str(ev.get("command") or "")[:40]
            verdict = str(ev.get("verdict") or "warn")
            if verdict == "block":
                self._emit(self._fmt(ts, "BASH", str(ev.get("file") or ""), "BLOCK", "", f"refused: {cmd}", sub), "yellow")
            else:
                detail = f"{reason}: {cmd}   gate: {ev.get('gate', '?')}"
                self._emit(self._fmt(ts, "BASH", str(ev.get("file") or ""), "WARN", "", detail, sub), "yellow")
            return

        if kind not in ("edit", "stop"):
            return

        run = str(ev.get("run") or "")
        self.pending.pop(run, None)
        verdict = str(ev.get("verdict") or "")
        dur = f"{float(ev.get('duration') or 0):.0f}s"
        keys = ev.get("keys") or []
        first_key = str(keys[0])[:44] if keys else ""
        more = f" (+{len(keys) - 1})" if len(keys) > 1 else ""
        strike = ev.get("strike")
        file = str(ev.get("file") or "")
        label = kind.upper()

        if verdict == "green":
            summary = ran_summary(ev.get("ran"))
            if summary == "nothing ran":
                self._emit(self._fmt(ts, label, file, "green", dur, "nothing ran", sub), "yellow")
            else:
                self._emit(self._fmt(ts, label, file, "green", dur, summary if kind == "stop" else "", sub))
            if kind == "stop":
                self.tmux.restore()
        elif verdict == "block":
            self._emit(self._fmt(ts, label, file, "BLOCK", dur, f"{first_key}{more}   strike {strike}", sub), "yellow")
        elif verdict == "loop":
            self._emit(self._fmt(ts, label, file, "LOOP", dur, f"{first_key}{more}   strike {strike}", sub), "red")
        elif verdict == "skip":
            self._emit(self._fmt(ts, label, file, "SKIP", dur, str(ev.get("reason") or "")[:60], sub), "red")
        elif verdict == "crash":
            self._emit(self._fmt(ts, label, file, "CRASH", dur, str(ev.get("reason") or "")[:60], sub), "red")
        elif verdict == "released":
            if ev.get("result") == "green":
                self._emit(self._fmt(ts, label, file, "released green", dur, ran_summary(ev.get("ran")), sub))
                self.tmux.restore()
            else:
                self._emit(self._fmt(ts, label, file, "RELEASED still failing", dur, f"{first_key}{more}", sub), "red")
        else:
            self._emit(self._fmt(ts, label, file, verdict, dur, "", sub))

        if kind == "edit":
            self._heuristics(session, ev, verdict, keys, file)
        if verdict in ("block", "loop"):
            self.keys[session] = [str(k) for k in keys]
        elif verdict == "green":
            self.keys[session] = []
        self.last_verdict[session] = verdict

    def _heuristics(self, session: str, ev: dict, verdict: str, keys: list, file: str) -> None:
        prev_verdict = self.last_verdict.get(session, "")
        prev_keys = self.keys.get(session, [])

        # editing the failing test while red
        if prev_verdict in ("block", "loop") and file:
            fname = base(file)
            hit = False
            for k in prev_keys:
                k = str(k)
                if "::" in k and base(k.split("::", 1)[0]) == fname:
                    hit = True
                elif fname.endswith(".cs") and "Test" in fname:
                    hit = True
            if hit:
                self._emit(f"{'':8} WARN   editing the failing test {fname}", "yellow")

        hist = self.history[session]
        hist.append(ev)
        if len(hist) < 3:
            return
        a, b, c = hist[-3], hist[-2], hist[-1]
        deltas = [x.get("delta") for x in (a, b, c)]
        if all(isinstance(d, int) for d in deltas):
            absd = [abs(d) for d in deltas]
            if all(x.get("verdict") in ("block", "loop") for x in (a, b, c)) and absd[0] > absd[1] > absd[2]:
                self._emit(f"{'':8} WARN   shrinking edits: {absd[0]}, {absd[1]}, {absd[2]} chars", "yellow")
            same_file = a.get("file") == b.get("file") == c.get("file") and a.get("file")
            if same_file and all(d != 0 for d in deltas):
                alternating = deltas[0] * deltas[1] < 0 and deltas[1] * deltas[2] < 0
                close = max(absd) <= 1.2 * min(absd)
                if alternating and close:
                    self._emit(f"{'':8} WARN   oscillating on {base(str(a.get('file')))}", "yellow")

    def check_killed(self) -> None:
        now = datetime.now().astimezone()
        for run, ev in list(self.pending.items()):
            started = parse_ts(str(ev.get("ts") or ""))
            if started is None:
                self.pending.pop(run, None)
                continue
            if (now - started).total_seconds() > KILL_AFTER:
                self.pending.pop(run, None)
                self._emit(
                    self._fmt(str(ev.get("ts")), str(ev.get("kind", "")).upper(), str(ev.get("file") or ""),
                              "KILLED", "", "hook timed out, nothing verified", bool(ev.get("agent"))),
                    "red",
                )


def tail(root: Path, watcher: Watcher, from_start: bool) -> None:
    path = root / LOG_REL
    announced = False
    while not path.exists():
        if not announced:
            sys.stdout.write(f"waiting for {LOG_REL} in {root} ...\n")
            sys.stdout.flush()
            announced = True
        time.sleep(1.0)
    sys.stdout.write(f"gate-watch: {path}\n")
    sys.stdout.flush()
    f = open(path, encoding="utf-8")
    if not from_start:
        f.seek(0, os.SEEK_END)
    pos = f.tell()
    while True:
        line = f.readline()
        if line:
            pos = f.tell()
            line = line.strip()
            if line:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    watcher.handle(ev)
            continue
        watcher.check_killed()
        try:
            if path.stat().st_size < pos:  # truncated: reopen
                f.close()
                f = open(path, encoding="utf-8")
                pos = 0
        except OSError:
            pass
        time.sleep(POLL)


# ----------------------------------------------------------------- stats


def parse_since(text: str | None) -> timedelta | None:
    if not text:
        return None
    m = re.fullmatch(r"(\d+)([dhm]?)", text.strip())
    if not m:
        raise SystemExit(f"--since: expected 7d, 24h, 30m or N (days), got {text!r}")
    n, unit = int(m.group(1)), m.group(2)
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n), "": timedelta(days=n)}[unit]


def stats(root: Path, since: timedelta | None) -> int:
    path = root / LOG_REL
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    events.append(ev)
    except OSError:
        print(f"no log at {path}")
        return 1
    if since is not None:
        cutoff = datetime.now().astimezone() - since
        events = [e for e in events if (parse_ts(str(e.get("ts") or "")) or cutoff) >= cutoff]

    ends = [e for e in events if e.get("phase") == "end"]
    starts = {str(e.get("run")): e for e in events if e.get("phase") == "start" and e.get("run")}
    ended_runs = {str(e.get("run")) for e in ends if e.get("run")}
    now = datetime.now().astimezone()
    killed = 0
    for run, s in starts.items():
        if run in ended_runs:
            continue
        t = parse_ts(str(s.get("ts") or ""))
        if t and (now - t).total_seconds() > KILL_AFTER:
            killed += 1

    sessions = {str(e.get("session")) for e in events if e.get("session")}
    prompts = [e for e in ends if e.get("kind") == "prompt"]
    edits = [e for e in ends if e.get("kind") == "edit"]
    stops = [e for e in ends if e.get("kind") == "stop"]
    gated = [e for e in edits + stops if e.get("verdict") in ("green", "block", "loop")]
    blocks = [e for e in gated if e.get("verdict") in ("block", "loop")]
    loops = [e for e in ends if e.get("verdict") == "loop"]
    skips = [e for e in ends if e.get("verdict") == "skip"]
    crashes = [e for e in ends if e.get("verdict") == "crash"]
    bash = [e for e in ends if e.get("kind") == "bash" and e.get("verdict") == "warn"]
    bash_rerun = [e for e in bash if e.get("gate") in ("green", "block", "loop", "crashed", "timed out")]
    bash_failed = [e for e in bash if e.get("gate") in ("block", "loop")]
    nothing_ran = [e for e in edits + stops if e.get("verdict") == "green" and not e.get("ran")]
    released = [e for e in stops if e.get("verdict") == "released"]
    released_red = [e for e in released if e.get("result") == "block"]
    stop_green = [e for e in stops if e.get("verdict") == "green"]
    stop_red = [e for e in stops if e.get("verdict") in ("block", "loop")]

    per_session = defaultdict(int)
    for e in edits:
        per_session[str(e.get("session"))] += 1
    avg_edits = (len(edits) / len(per_session)) if per_session else 0.0
    max_edits = max(per_session.values()) if per_session else 0
    per_prompt = (len(edits) / len(prompts)) if prompts else 0.0

    # block -> next green in the same session
    fix_times: list[float] = []
    by_session: dict[str, list[dict]] = defaultdict(list)
    for e in gated:
        by_session[str(e.get("session"))].append(e)
    for seq in by_session.values():
        seq.sort(key=lambda e: str(e.get("ts")))
        open_block: datetime | None = None
        for e in seq:
            t = parse_ts(str(e.get("ts") or ""))
            if t is None:
                continue
            if e.get("verdict") in ("block", "loop"):
                if open_block is None:
                    open_block = t
            elif e.get("verdict") == "green" and open_block is not None:
                fix_times.append((t - open_block).total_seconds())
                open_block = None
    avg_fix = (sum(fix_times) / len(fix_times)) if fix_times else 0.0

    def pct(n: int, d: int) -> str:
        return f"{(100.0 * n / d):.0f}%" if d else "-"

    def mmss(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s:02d}s"

    window = f"since {since}" if since is not None else "whole log"
    print(f"gate-stats ({window}) — {path}")
    print(f"{'Sessions':<20}{len(sessions)}")
    print(f"{'Prompts':<20}{len(prompts)}")
    print(f"{'Edits':<20}{len(edits):<6} (avg {avg_edits:.1f} per session, max {max_edits}, {per_prompt:.1f} per prompt)")
    print(f"{'Blocks':<20}{len(blocks):<6} ({pct(len(blocks), len(gated))})   avg time to green after block: {mmss(avg_fix)}")
    print(f"{'Loops':<20}{len(loops)}")
    print(f"{'Bash edits':<20}{len(bash):<6} ({len(bash_rerun)} gate re-runs, {len(bash_failed)} failed)")
    print(f"{'Green, nothing ran':<20}{len(nothing_ran)}")
    print(f"{'Released stops':<20}{len(released):<6} ({len(released_red)} still failing)")
    print(f"{'Skips':<20}{len(skips)}")
    print(f"{'Crashes':<20}{len(crashes)}")
    print(f"{'Killed':<20}{killed}")
    print(f"{'Stop gates':<20}{len(stops):<6} green {len(stop_green)}, block {len(stop_red)}, released {len(released)}")
    return 0


# ------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description="tail the gate log; --stats for the baseline")
    ap.add_argument("--from-start", action="store_true", help="replay the whole log first")
    ap.add_argument("--no-tmux", action="store_true", help="never touch tmux")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--stats", action="store_true", help="print baseline numbers and exit")
    ap.add_argument("--since", default=None, help="7d, 24h, 30m or N days (stats only)")
    args = ap.parse_args()

    root = repo_root()
    if args.stats:
        return stats(root, parse_since(args.since))

    tmux = Tmux(root, enabled=not args.no_tmux)
    color = (not args.no_color) and sys.stdout.isatty()
    watcher = Watcher(root, tmux, color)

    def on_exit(signum, frame):  # noqa: ARG001
        tmux.restore()
        sys.stdout.write("\n")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)
    try:
        tail(root, watcher, args.from_start)
    finally:
        tmux.restore()
    return 0


if __name__ == "__main__":
    sys.exit(main())
