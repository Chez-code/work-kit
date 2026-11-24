# work-kit

A small, portable dev-environment kit: a tmux config plus a verification
harness for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview)
that makes quality gates (lint, tests) fire automatically inside the agent
loop — so the agent cannot hand you code that hasn't survived them. For Rust
it also brings `rbox`, a sandbox for `cargo`, so that code from crates.io
cannot read your files or reach the network while it builds, tests or runs.

## Contents

- `tmux.conf` — tmux config: mouse on, 100k history, tpm with
  sensible/resurrect/continuum (sessions survive reboots); `prefix + W`
  opens the gate watcher pane
- `kit/` — the Claude Code harness, dropped into any repo:
  - `.claude/settings.json` — five hooks: **PreToolUse** (the policy guard),
    **PostToolUse** on Write/Edit (ruff-fix the touched file, run fast
    tests), **PostToolUse** on Bash (the Bash guard), **UserPromptSubmit**
    (reset the loop counter) and **Stop** (full test suite before the agent
    may declare a task done); plus `permissions.deny` rules for the things
    only the operator may do with rbox
  - `.claude/hooks/gate.py` — the gate script; stdlib-only, exit 2 feeds the
    failure output straight back to the agent to fix. Logs every run to
    `.claude/gate-log.jsonl` and stops feeding a failure back after three
    identical blocks in a row (`[LOOP DETECTED]`, see OPERATOR.md)
  - `.claude/hooks/bash_guard.py` — watches Bash for `sed -i`, heredocs,
    `tee`, `rm` and friends on source files, warns, and re-runs the gate on
    the touched file so the change is verified anyway
  - `.claude/hooks/gate_log.py` — shared log / failure-key / loop-state code
  - `.claude/hooks/policy_guard.py` — in Rust repos, refuses the ways around
    rbox's person-only commands (approving a crate, exporting a program,
    allowing a network host) before they run
  - `.claude/bin/gate-watch.py` — the operator's tmux pane: tails the log,
    colors events, rings the bell on anything red; `--stats` for a baseline
  - `.pre-commit-config.yaml` — commit-time gate (ruff + mypy)
  - `.github/workflows/ci.yml` — the same gates in CI (GitHub Actions)
  - `CLAUDE.md.template` — project brief template: mission, [LOCKED] specs,
    build order, working agreements
- `rustbox/` — the Rust box, installed once per machine by
  `rustbox/install.sh` (no sudo, home folder only):
  - `rbox.py` — runs `cargo` inside bubblewrap. Build-type commands get no
    network and a read-only project; download-type commands run nothing but
    cargo. Every crate file has to be approved by a person before it can be
    built, and `--net` reaches only listed hosts
  - `toolchain.sums` — the pinned Rust release, by sha256 (`pin.py` rewrites
    it from the signed release manifest)
  - `selftest/` — a hostile crate that `rbox selftest` builds, tests and runs
    to prove the box holds on this machine
- `bootstrap.sh` — copies the kit into a target repo; idempotent, never
  overwrites existing files; installs the Python-only pre-commit/CI
  templates only where the Python lane runs
- `sync.sh` — updates kit-managed files in an already-bootstrapped repo;
  every replaced file is kept as `<file>.prev` for review
- `WORK-SETUP.md` — step-by-step install/update instructions for a .NET/C#
  machine, written so a Claude Code session there can apply them directly
- `RUST-SETUP.md` — the same for Rust: installing rbox, approving crates,
  the network list, what the gate does in a Rust repo
- `RUST-STACK.md` — why the box exists (dated incidents), the rules it
  enforces, and what to build a Rust service on
- `OPERATOR.md` — the human half of the harness: the operator's loop, what
  to watch in the transcript, file tools vs shell edits, `/rewind` and loop
  detection, per-hook `GATE_DOTNET_TARGET`
- `TROUBLESHOOTING.md` — diagnostic ladder and symptom table for when a gate
  misbehaves
- `OPERATOR-NEXT.md` — design for the next operator automation: what shipped
  (log, guard, loop detector, watcher) and what is still design (spec
  check, reviewer loop)
- `tests/` — kit self-tests: `python3 -m unittest discover tests` and
  `tests/smoke.sh` (end-to-end with a fake `dotnet` and a fake `rbox`, no SDK
  or toolchain needed); `tests/rust/` drives the real Rust toolchain against
  real crates and weakens the box on purpose to prove the self-test can fail

## Install

tmux:

```bash
cp tmux.conf ~/.tmux.conf
git clone https://github.com/tmux-plugins/tpm ~/.tmux/plugins/tpm
tmux    # then prefix + I to install plugins
```

Harness, per repo:

```bash
./bootstrap.sh /path/to/repo
cd /path/to/repo
# 1. Fill in CLAUDE.md placeholders
# 2. pre-commit install && pre-commit autoupdate
# 3. Launch `claude` and confirm the hooks fire
```

## Adapting to your stack

`gate.py` auto-detects the stack: a `pyproject.toml` repo gets ruff + pytest
(uv-managed venv preferred); a repo with a `.sln`/`.slnx`/`.csproj` (root or
one level down) gets `dotnet build` after each edit and `dotnet build` +
`dotnet test --no-build` at Stop. Multi-solution repos: the gate skips with a
visible message rather than guess — set `GATE_DOTNET_TARGET` to pin the
target (the message shows how). A repo with a `Cargo.toml` at the root gets
`cargo check` after each edit and `cargo fmt --check`, `cargo clippy` and
`cargo test` at Stop, all through rbox (see RUST-SETUP.md). A repo can have
more than one lane: the edited file picks it, and Stop runs them all. For any
other stack, swap the commands in `gate.py` — the hook wiring in
`settings.json` is stack-agnostic. `.pre-commit-config.yaml` and `ci.yml` are
Python-specific templates (bootstrap installs them only where the Python lane
runs).

Knobs via environment: `GATE_DOTNET_TARGET` (above), `GATE_TIMEOUT` —
the whole-gate time budget in seconds, shared by every lane of a run (default
540; keep it below the hook-level `timeout` in settings (600), so a hung
build/test is reported as a blocking gate failure instead of being silently
killed by the harness) — `GATE_LOOP_STRIKES`, the number of identical
consecutive blocks before the gate tells Claude to stop and report (default
3), and `GATE_RBOX`, the path to rbox when it is not in its default place.

In a shared repo, consider putting the hooks block in
`.claude/settings.local.json` (untracked) rather than committing
`settings.json`, so you're not imposing hooks on collaborators.

## Advanced: async Stop gate

By default the Stop gate blocks until the full suite finishes — that blocking
is the harness's core guarantee. For a slow suite you can trade the guarantee
for speed: `kit/.claude/settings.async-stop.example.json` (a reference file —
bootstrap and sync never install it) shows a Stop entry with
`"asyncRewake": true`. The suite then runs in the background; Claude can
declare done before it finishes but is woken with the failure output if it
goes red. Notes: hook timeouts are NOT enforced on async hooks, and this
needs a Claude Code version whose docs list `asyncRewake`. To opt in, MERGE
that hooks block into your repo's `.claude/settings.local.json` — don't copy
the file over it, since Claude Code accumulates permission allow rules there
that a copy would wipe.
