# TROUBLESHOOTING — diagnosing and operating the harness

Two audiences: the operator sitting in front of Claude Code, and a Claude
session asked to debug the gate. Both start from the same mental model.

## 1. Mental model (read this first)

Two hooks, one script, two exit codes.

| Hook | Fires when | Python repo | .NET repo |
|---|---|---|---|
| Edit gate (`PostToolUse`, matcher `Write\|Edit\|MultiEdit`) | after every file write by a tool | `.py` only: clear stale `.pyc`, `ruff check --fix`, `pytest -x -q` | `.cs/.csproj/.sln/.slnx` only: `dotnet build <target>` |
| Stop gate (`Stop`) | when Claude tries to declare the task done | full `pytest -q` | `dotnet build <target>` then `dotnet test --no-build <target>` |

- **Exit 0** = proceed. **Exit 2** = blocked; stderr is fed back to Claude as
  the thing to fix. **Exit 1** = the gate itself crashed (a traceback) — that is
  a bug in the gate, not a verdict on the code, and it does NOT block.
- **Skips are visible.** When the gate cannot run (no `dotnet` on PATH, zero or
  several solutions) it prints a `systemMessage` JSON line on stdout and exits 0.
  If you see `[gate] ... no verification is running`, nothing is being checked.
- **Once per stop.** The Stop gate blocks the first stop attempt; the second
  consecutive attempt is let through (`stop_hook_active` anti-loop guard). A red
  suite can therefore still be handed back — the last `[STOP GATE FAILED]` text
  is the truth, not Claude's "done".
- **Bash edits are invisible to the edit gate.** `sed -i`, heredocs, scripts:
  only the Stop gate stands under those.
- **Knobs** (environment, set as a prefix in the hook command string):
  `GATE_TIMEOUT` whole-gate budget in seconds (default 540, must stay below the
  hook `timeout`, 600), `GATE_DOTNET_TARGET` the solution/project to build when
  auto-detection is ambiguous.

## 2. The diagnostic ladder

Work top-down. Each rung is cheap and rules out the rungs above it.

### Rung 1 — is the hook wired at all?

```bash
cd /path/to/repo                      # Claude must be launched from the repo root
python3 -m json.tool .claude/settings.local.json >/dev/null && echo JSON_OK
grep -n gate.py .claude/settings.local.json .claude/settings.json 2>/dev/null
ls -l .claude/hooks/gate.py
```

- A `Settings Error` banner at Claude startup = the settings file is not strict
  JSON (a `//` comment or trailing comma). Fix the file; nothing else works
  until it parses.
- In a running session, `/hooks` lists what Claude Code actually loaded. If
  gate.py is not in the list, the file it lives in is not being read (wrong
  file, wrong directory, launched from a subdirectory of a non-git folder).
- Press **Ctrl+O** for verbose mode. Every hook run shows up there with its
  exit code and output. If an edit produces no hook line at all, go back to
  the JSON and `/hooks` checks.

### Rung 2 — run the gate by hand

This removes Claude Code from the picture entirely.

```bash
# Stop path
echo '{"cwd":"'"$PWD"'"}' | python3 .claude/hooks/gate.py --stop; echo "exit=$?"
# Edit path (swap the extension to match your stack: X.cs / x.py)
echo '{"cwd":"'"$PWD"'","tool_input":{"file_path":"X.cs"}}' | python3 .claude/hooks/gate.py; echo "exit=$?"
```

Read the result by exit code:

| exit | meaning | next step |
|---|---|---|
| 0, no output | passed (or non-code extension, which is silent) | Rung 3 if you expected a block |
| 0, `systemMessage` line | gate skipped, says why | do what the message says (PATH, `GATE_DOTNET_TARGET`) |
| 2 | blocked; stderr shows the failing command's last 40 lines | run that command by hand for the full output |
| 1, traceback | gate bug | capture the traceback; that is the bug report |

### Rung 3 — symptom table

| Symptom | Likely cause | Check / fix |
|---|---|---|
| .NET repo, every edit passes instantly | `dotnet` not on PATH for the hook, or target ambiguous | hand-run shows the skip message; fix PATH or set `GATE_DOTNET_TARGET` |
| .NET repo, edit gate blocks on every edit | the build is red independently of the edit | `dotnet build <target>` by hand; the gate only shows the tail |
| "timed out after Ns (GATE_TIMEOUT budget)" | suite/build slower than the budget | raise `GATE_TIMEOUT` and the hook `timeout` together, keep ~60 s margin; or opt into the async Stop example |
| Claude says done, tests are red | second-stop pass-through (by design) | read the last `[STOP GATE FAILED]`; tell Claude to fix it; the next stop is gated again |
| Red test passed the edit gate | edit made via Bash, or a same-second same-size edit on an old gate.py | prefer Write/Edit; `sync.sh` to the current gate |
| Python repo, "no pytest runner found" | neither `uv` nor `pytest` on PATH | `uv sync` in the repo, or install pytest |
| Gate runs ruff but never pytest | no `tests/` directory at the repo root | create it; the gate only runs pytest when it exists |
| Hook fires in a subagent unexpectedly | expected — hooks run inside subagents too | nothing to fix |
| Every non-code edit prints a skip message | gate.py older than 8d78f1d | `sync.sh` the repo |
| Wrong Python/ruff version used | `uv` present but no `pyproject.toml`, so PATH tools are used | add `pyproject.toml` or accept PATH tools |

### Rung 4 — isolate in a scratch repo

If the symptom survives rungs 1–3, reproduce it small:

```bash
mkdir -p /tmp/gate-scratch && cd /tmp/gate-scratch && git init -q
~/work-kit/bootstrap.sh .
# Python: uv init -q && mkdir tests && printf 'def test_x():\n    assert False\n' > tests/test_x.py
# .NET:   dotnet new xunit -o Tests -n Tests
claude          # ask for one trivial edit, watch Ctrl+O for the gate line
```

A deliberately failing test must produce a `[EDIT GATE FAILED]` line on the
next `.py`/`.cs` edit and a `[STOP GATE FAILED]` when Claude tries to stop. If
the scratch repo behaves and the real one does not, diff the two
`.claude/` directories — the difference is the bug.

### Rung 5 — no dotnet on this machine

Put a recording shim first on PATH and read what the gate would have run:

```bash
mkdir -p /tmp/shim && printf '#!/bin/sh\necho "dotnet $*" >> /tmp/shim/calls.log\nexit 0\n' > /tmp/shim/dotnet && chmod +x /tmp/shim/dotnet
PATH=/tmp/shim:$PATH python3 .claude/hooks/gate.py --stop <<< '{"cwd":"'"$PWD"'"}'; cat /tmp/shim/calls.log
```

Change `exit 0` to `exit 1` to test the blocking path; add `sleep 5` with a
short `GATE_TIMEOUT=3` to test the timeout path.

## 3. Operating the harness well

- **Launch from the repo root.** The hook command uses the project directory;
  a session started elsewhere may load different settings.
- **Fill CLAUDE.md before the first real task.** Locked specs, build order,
  and the Commands section for your stack. The template's "If verification
  fails" section stays in.
- **Keep Ctrl+O on for the first day.** Watching gate lines go by is how you
  learn what a green session looks like; after that you only need it when
  something feels off.
- **Trust the gate output, not the summary.** When Claude says done, the last
  Stop-gate line is the verdict. A red Stop followed by a stop that went
  through means the work is unfinished.
- **Prefer Write/Edit for source changes.** Ask Claude to use the file tools
  rather than shell rewrites; Bash edits bypass the edit gate.
- **Use /rewind when Claude loops on one failure.** Restore to before the bad
  edit and ask for a different approach rather than a fourth retry.
- **Do not disable hooks to get unstuck.** Fix the cause via the ladder. If a
  gate is genuinely wrong, `GATE_TIMEOUT` and `GATE_DOTNET_TARGET` are the
  knobs; the example async Stop file is the escape hatch for slow suites.
- **Keep the edit-time check fast.** Python: `-x` fails fast by design. .NET:
  point `GATE_DOTNET_TARGET` at the project you are working in if the whole
  solution builds slowly; the Stop gate can still target the full solution
  through a second hook entry if you want both.
- **Update ritual.** `git -C ~/work-kit pull && ~/work-kit/sync.sh <repo>`,
  read the `.prev` diff, delete the `.prev`. If hooks live in
  `settings.local.json`, apply any settings change there by hand.
- **When reporting a problem to a Claude session,** paste three things: the
  hand-run smoke-test output with exit code, the verbose-mode hook line from
  the session, and the settings file that holds the hooks. That is enough to
  diagnose almost everything in rung 3 in one turn.
