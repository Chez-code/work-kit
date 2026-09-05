# work-kit

A small, portable dev-environment kit: a tmux config plus a verification
harness for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview)
that makes quality gates (lint, tests) fire automatically inside the agent
loop — so the agent cannot hand you code that hasn't survived them.

## Contents

- `tmux.conf` — tmux config: mouse on, 100k history, tpm with
  sensible/resurrect/continuum (sessions survive reboots)
- `kit/` — the Claude Code harness, dropped into any repo:
  - `.claude/settings.json` — two hooks: **PostToolUse** (after every
    Write/Edit: ruff-fix the touched file, run fast tests) and **Stop** (full
    test suite before the agent may declare a task done)
  - `.claude/hooks/gate.py` — the gate script both hooks call; stdlib-only,
    exit 2 feeds the failure output straight back to the agent to fix
  - `.pre-commit-config.yaml` — commit-time gate (ruff + mypy)
  - `.github/workflows/ci.yml` — the same gates in CI (GitHub Actions)
  - `CLAUDE.md.template` — project brief template: mission, [LOCKED] specs,
    build order, working agreements
- `bootstrap.sh` — copies the kit into a target repo; idempotent, never
  overwrites existing files; skips the Python-only pre-commit/CI templates
  when the target is a .NET repo
- `sync.sh` — updates kit-managed files in an already-bootstrapped repo;
  every replaced file is kept as `<file>.prev` for review
- `WORK-SETUP.md` — step-by-step install/update instructions for a .NET/C#
  machine, written so a Claude Code session there can apply them directly

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
target (the message shows how). For any other stack, swap the commands in
`gate.py` — the hook wiring in `settings.json` is stack-agnostic.
`.pre-commit-config.yaml` and `ci.yml` are Python-specific templates
(bootstrap skips them on .NET repos automatically).

Two knobs via environment: `GATE_DOTNET_TARGET` (above) and `GATE_TIMEOUT` —
the whole-gate time budget in seconds (default 540). Keep it below the
hook-level `timeout` in settings (600), so a hung build/test is reported as a
blocking gate failure instead of being silently killed by the harness.

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
