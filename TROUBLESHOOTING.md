# TROUBLESHOOTING — diagnosing and operating the harness

Two audiences: the operator sitting in front of Claude Code, and a Claude
session asked to debug the gate. Both start from the same mental model.

## 1. Mental model (read this first)

Five hooks, three scripts, two exit codes, one log.

| Hook | Fires when | Python repo | .NET repo | Rust repo |
|---|---|---|---|---|
| Edit gate (`PostToolUse`, matcher `Write\|Edit\|MultiEdit`, `gate.py`) | after every file write by a tool | `.py` only: clear stale `.pyc`, `ruff check --fix --output-format concise`, `pytest -x -q` | `.cs/.csproj/.sln/.slnx` only: `dotnet build <target>` | `.rs`, `Cargo.toml`, `Cargo.lock` only: `rbox cargo check --workspace --all-targets` |
| Stop gate (`Stop`, `gate.py --stop`) | when Claude tries to declare the task done | full `pytest -q` | `dotnet build <target>` then `dotnet test --no-build <target>` | `cargo fmt --check`, `cargo clippy -D warnings`, then `cargo test`, all through rbox |
| Bash guard (`PostToolUse`, matcher `Bash`, `bash_guard.py`) | after every Bash command | if the command wrote/moved/deleted a source file inside the repo: log a `bash` event, re-run the edit gate on that file, report | same | same |
| Prompt reset (`UserPromptSubmit`, `gate.py --new-prompt`) | on every prompt you send | delete `.claude/gate-state.json`, log a `prompt` event, print nothing | same | same |
| Policy guard (`PreToolUse`, `policy_guard.py`) | before every Bash command and file write | nothing | nothing | refuses `rbox allow` / `urgent` / `export` / `net allow`, typing into other terminals, and touching rbox's own folders |

A repo can have more than one lane. The edited file picks the lane; Stop runs
every lane the repo has, inside one `GATE_TIMEOUT` budget. A repo counts as
Rust when it has a `Cargo.toml` at the root or one level down; cargo runs in
that folder. Python is the lane of any repo that is not .NET and does not have
a `Cargo.toml` at its root, or has one together with a `pyproject.toml`.

Every gate run past the extension check appends a `start` and an `end`
line to `.claude/gate-log.jsonl`; `.claude/bin/gate-watch.py` tails it in a
tmux pane (`prefix + W`) and `--stats` summarises it.

- **Exit 0** = proceed. **Exit 2** = blocked; stderr is fed back to Claude as
  the thing to fix. **Exit 1** = the gate itself crashed (a traceback) — that is
  a bug in the gate, not a verdict on the code, and it does NOT block.
- **Skips are visible.** When the gate cannot run (no `dotnet` on PATH, zero or
  several solutions, no rbox or no sandbox on the machine) it prints a
  `systemMessage` JSON line on stdout and exits 0. If you see `[gate] ... no
  verification is running` or `... are NOT running`, nothing is being checked.
- **Waiting is not blocking.** In a Rust repo, crates that a person has not
  approved yet stop the build (rbox exit 5). The gate does not block on that,
  because Claude cannot fix it: it prints `Rust checks did not run: N crates
  await approval`, logs a `pending` verdict, and the watcher shows
  `UNVERIFIED` on every line until a run in which check, clippy or test really
  executed. Formatting is still enforced meanwhile. Approve with `rbox allow`
  in your own terminal.
- **Once per stop.** The Stop gate blocks the first stop attempt; the second
  consecutive attempt is let through (`stop_hook_active` anti-loop guard). A red
  suite can therefore still be handed back — the last `[STOP GATE FAILED]` text
  is the truth, not Claude's "done".
- **Bash edits are invisible to the edit gate.** `sed -i`, heredocs, scripts:
  the Bash guard catches the common shapes and re-runs the gate on the file,
  but only for paths it can resolve inside the repo. Anything else is under
  the Stop gate alone.
- **Loop detector.** Consecutive blocks whose set of failure keys is
  unchanged are strikes; at three (`GATE_LOOP_STRIKES`) the gate emits
  `[LOOP DETECTED]` instead of the failure text and tells Claude to stop.
  Any user prompt resets the count. A released Stop (second consecutive
  stop) runs the gate and logs `released` with `result: green|block` but
  never blocks and never counts a strike.
- **Knobs** (environment, set as a prefix in the hook command string):
  `GATE_TIMEOUT` whole-gate budget in seconds (default 540, must stay below the
  hook `timeout`, 600), `GATE_DOTNET_TARGET` the solution/project to build when
  auto-detection is ambiguous, `GATE_LOOP_STRIKES` strikes before LOOP
  (default 3), `GATE_RBOX` the path to rbox when it is not at
  `~/.local/share/rustbox/bin/rbox`, `GATE_RUST_DIR` the one folder to run
  cargo in when several folders one level down hold a `Cargo.toml`.

## 2. The diagnostic ladder

Work top-down. Each rung is cheap and rules out the rungs above it.

### Rung 1 — is the hook wired at all?

```bash
cd /path/to/repo                      # Claude must be launched from the repo root
python3 -m json.tool .claude/settings.local.json >/dev/null && echo JSON_OK
grep -n 'gate.py\|bash_guard.py' .claude/settings.local.json .claude/settings.json 2>/dev/null
ls -l .claude/hooks/gate.py .claude/hooks/gate_log.py .claude/hooks/bash_guard.py .claude/bin/gate-watch.py
```

Five hook entries are expected: policy_guard.py (PreToolUse), gate.py
(PostToolUse), bash_guard.py (PostToolUse Bash), gate.py --new-prompt
(UserPromptSubmit), gate.py --stop (Stop). gate.py imports gate_log.py from
its own directory; if that file is missing every gate run crashes with
`ModuleNotFoundError` (exit 1, nothing blocked). `sync.sh` restores it, and
adds any kit file a newer release introduced. The policy guard's hook command
does nothing when its file is absent, so a half-updated repo cannot block
every tool call.

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
# Edit path (swap the extension to match your stack: X.cs / x.py / src/main.rs)
echo '{"cwd":"'"$PWD"'","tool_input":{"file_path":"X.cs"}}' | python3 .claude/hooks/gate.py; echo "exit=$?"
# Bash guard (expect a systemMessage JSON line and exit 0 on a green repo)
echo '{"cwd":"'"$PWD"'","tool_input":{"command":"sed -i s/a/b/ X.cs"}}' | python3 .claude/hooks/bash_guard.py; echo "exit=$?"
# Prompt reset (expect NO output at all and exit 0)
echo '{}' | python3 .claude/hooks/gate.py --new-prompt; echo "exit=$?"
# What got logged
tail -n 5 .claude/gate-log.jsonl
```

Read the result by exit code:

| exit | meaning | next step |
|---|---|---|
| 0, no output | passed (or non-code extension, which is silent) | Rung 3 if you expected a block |
| 0, `systemMessage` line | gate skipped, or Rust is waiting for an approval; it says which | do what the message says (PATH, `GATE_DOTNET_TARGET`, `rustbox/install.sh`, `rbox allow`) |
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
| Watcher pane shows nothing / `waiting for .claude/gate-log.jsonl` | pane opened from a directory that is not the repo root, or no gate has run yet | open the pane (`prefix + W`) from the pane where `claude` runs in the repo root; make one `.cs`/`.py` edit |
| `LOOP` fired but the failure looked different each time | the failure keys matched even though the text moved (same test name, same error code) | read `keys` in the log line; if they are genuinely the same failure, it was a loop; send a prompt to reset |
| `LOOP` never fires on an obvious loop | the failure keys change every run (fallback key on unmatched output) | look at `keys` in the log; if they are `raw:` keys that differ, capture the output and add a pattern to `gate_log.py` |
| tmux status bar stuck red | the watcher was killed without restoring | restart the watcher: it restores from `.claude/gate-watch.status-style`; or `tmux set -g status-style "$(cat .claude/gate-watch.status-style)"` |
| Guard warns on a scratch file | the path resolves inside the repo and has a source extension | expected; paths under /tmp or outside the repo are ignored, anything inside is a source change |
| Guard says `target not found` | Python lane, relative path after a `cd` the guard could not follow, or the file really is gone | harmless: nothing was verified for it; ask Claude to use Edit |
| `ruff: error: unrecognized ... --output-format` | ruff older than 2023 pinned in the project | upgrade ruff, or drop the two `--output-format concise` tokens in gate.py (the full-format fallback still extracts keys) |
| Every gate run exits 1 with `ModuleNotFoundError: gate_log` | `gate_log.py` missing next to `gate.py` | `sync.sh` the repo |
| Rust repo: `rbox is not installed` on every edit | rbox was never installed on this machine | `rustbox/install.sh` from the kit checkout; `rbox doctor` |
| Rust repo: `rbox cannot run here` | no bubblewrap, sandboxes restricted by the distro, or no toolchain | `rbox doctor` names the cause and the one sudo line a person runs |
| Rust repo: `Rust checks did not run: N crates await approval` | new or changed crate files that nobody has approved on this machine | `rbox pending` to see them; a person runs `rbox allow` in their own terminal |
| Rust repo: watcher shows `UNVERIFIED` long after an approval | no gate run has compiled anything since | make one `.rs` edit or stop once; the line clears when check, clippy or test runs |
| Rust repo: blocked with `the dependencies changed since the crates were last downloaded` | `Cargo.lock` changed (a pull, a checkout, a hand edit) | `rbox sync` (downloads only); Claude may run it |
| Rust repo: `there is no Cargo.lock yet` | fresh clone of a library, or a new project made without `rbox new` | `rbox sync` |
| Rust repo: first gate run after adding crates times out | cold build of every dependency inside the 540 s budget | `rbox warm` once, outside the gate |
| Rust repo: `cargo` typed by Claude fails with a namespace or uid-map error, the gate is fine | Claude Code's own sandbox blocks nested sandboxes | list `rbox` and `cargo` under `sandbox.excludedCommands` in `~/.claude/settings.json` |
| Rust repo: a test fails with `Read-only file system` | the project is read-only inside the box | write under `OUT_DIR`, `CARGO_TARGET_TMPDIR` or `/tmp` |
| Rust repo: a test cannot reach the network | nothing in the box has a network unless asked | mark it `#[ignore]`; run with `rbox --net cargo test -- --ignored` after a person lists the host (`rbox net allow`) |
| `[POLICY GUARD] Refused` | Claude tried one of the operator-only rbox commands, a way around them, or a write to rbox's downloaded crate sources | expected; do the step yourself if you want it done |
| Rust in a subfolder: `N folders with a Cargo.toml one level down` | more than three Rust folders and no way to guess which matters | prefix the hook command with `GATE_RUST_DIR=<folder>` |
| `rbox --net cargo test` runs no doc tests | with `--net` only finished test programs are run; nothing is compiled with the network | run doc tests without `--net` |
| `rbox allow` refuses everything with `crates.io could not be reached` | approving needs a fresh answer about each crate | try again online; `rbox pending` still lists from the last download |
| `[POLICY GUARD] Refused: rbox's downloaded crate sources are read-only` | a command mentioned rbox's cache and did more than read, or mixed a read with another step | read with `cat`, `grep`, `rg`, `ls`, `find`, `head`; run other steps as a separate command |
| `rbox: cannot-run: ... holds more than 2,000,000 files outside its caches` | the project, or a `--rw` folder, is too large to search for secret files, and rbox will not search only part of it | move bulk data out, or thin it; rbox warns from 500,000 files on |
| `--rw ...: that is inside ..., which is hidden in the box` | the folder is in a cache folder, `.git` or `.claude`, which the box covers with an empty folder | choose another folder for the program's output |
| `install.sh` stops with `it would switch this machine from Rust A to Rust B` | the kit's pinned Rust changed and a session ran the installer | a person runs `rustbox/install.sh` in their own terminal |
| `another cargo wins` from `rbox doctor` | rustup or a distro cargo comes before `~/.local/bin` on PATH | put `~/.local/bin` first, or remove the other cargo; it builds outside the box |

### Rung 3b — the kit's own tests

From the kit checkout, not the target repo:

```bash
cd ~/work-kit
python3 -m unittest discover tests     # key extraction, state file, guard matcher
tests/smoke.sh                         # end-to-end with a fake dotnet and a fake rbox; no SDK needed
python3 tests/rust/test_online.py      # Rust only: real cargo, real crates (needs rbox installed)
rbox selftest                          # Rust only: the box itself, with a hostile crate
```

Both green means the scripts are fine and the problem is wiring or
environment in the target repo (rungs 1, 2, 5).

### Rung 4 — isolate in a scratch repo

If the symptom survives rungs 1–3, reproduce it small:

```bash
mkdir -p /tmp/gate-scratch && cd /tmp/gate-scratch && git init -q
~/work-kit/bootstrap.sh .
# Python: uv init -q && mkdir tests && printf 'def test_x():\n    assert False\n' > tests/test_x.py
# .NET:   dotnet new xunit -o Tests -n Tests
# Rust:   run `rbox new scratch` instead of the mkdir, then git init and bootstrap inside it
claude          # ask for one trivial edit, watch Ctrl+O for the gate line
```

A deliberately failing test must produce a `[EDIT GATE FAILED]` line on the
next `.py`/`.cs` edit and a `[STOP GATE FAILED]` when Claude tries to stop. If
the scratch repo behaves and the real one does not, diff the two
`.claude/` directories — the difference is the bug.

### Rung 5 — no dotnet (or no rbox) on this machine

Put a recording shim first on PATH and read what the gate would have run. For
the Rust lane the shim is named by `GATE_RBOX` instead of found on PATH:
`GATE_RBOX=/tmp/shim/rbox`, a script that logs `$*` and exits 0 (green),
101 (a compile error), 3, 4 or 5 (rbox's refused, cannot-run, waiting).

```bash
mkdir -p /tmp/shim && printf '#!/bin/sh\necho "dotnet $*" >> /tmp/shim/calls.log\nexit 0\n' > /tmp/shim/dotnet && chmod +x /tmp/shim/dotnet
PATH=/tmp/shim:$PATH python3 .claude/hooks/gate.py --stop <<< '{"cwd":"'"$PWD"'"}'; cat /tmp/shim/calls.log
```

Change `exit 0` to `exit 1` to test the blocking path; add `sleep 5` with a
short `GATE_TIMEOUT=3` to test the timeout path.

### What the Bash guard does not see

The guard parses the command string with a shell tokenizer and looks for
known shapes. It is a net under the Edit-tool path, not a parser of bash.
Shapes it does not catch, so only the Stop gate stands under them:

- `find ... -exec sed -i ...`, `xargs sed -i`, and `for f in ...; do sed -i "$f"; done`
- `rm -rf src/` on a directory (only file targets with a source extension count)
- `git checkout -- file`, `git stash`, `git restore`, `patch`, and any tool
  that rewrites files as a side effect
- scripts run from a file (`bash fix.sh`, `python3 fix.py`) whose body
  does the writing
- paths built from variables: `"$DIR/x.py"` is not expanded, so it either
  misses (outside the repo) or warns spuriously with `target not found`
  (Python lane) or a harmless extra build (.NET lane)

And two harmless false positives: a heredoc body is tokenized like the
command, so a `;` or a command word inside it can produce a spurious match;
and `sed -i ... > /dev/null` still warns on the sed. Both cost one extra
gate run at most.

If the log shows Claude reaching for one of these repeatedly, the fix is
the working agreement in CLAUDE.md, not a bigger parser.

## 3. Operating the harness well

- **Launch from the repo root.** The hook command uses the project directory;
  a session started elsewhere may load different settings.
- **Fill CLAUDE.md before the first real task.** Locked specs, build order,
  and the Commands section for your stack. The template's "If verification
  fails" section stays in.
- **Keep the watcher pane open.** `prefix + W`. Red means act, yellow means
  glance, plain means carry on. After a week, `gate-watch.py --stats
  --since 7d` is your baseline.
- **Trust the gate output, not the summary.** When Claude says done, the last
  Stop-gate line is the verdict. A red Stop followed by a stop that went
  through means the work is unfinished.
- **Prefer Write/Edit for source changes.** Ask Claude to use the file tools
  rather than shell rewrites; the Bash guard re-runs the gate on what it
  can see, but it is a net under the path, not the path.
- **Use /rewind when the watcher shows LOOP.** Restore to before the bad
  edit and ask for a different approach rather than a fourth retry. Your
  next prompt resets the strike count.
- **Do not disable hooks to get unstuck.** Fix the cause via the ladder. If a
  gate is genuinely wrong, `GATE_TIMEOUT` and `GATE_DOTNET_TARGET` are the
  knobs; the example async Stop file is the escape hatch for slow suites.
- **Rust: approve crates yourself, and promptly.** While crates wait, nothing
  in Rust is verified and the watcher says so on every line. Read the
  `rbox allow` table rather than typing yes at it: the rows that run code at
  build time and the recently published ones are the ones that matter.
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
