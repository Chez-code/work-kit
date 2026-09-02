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
  overwrites existing files

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
(uv-managed venv preferred); a `.sln`/`.csproj` repo gets `dotnet build` after
each edit and `dotnet build` + `dotnet test` at Stop. For any other stack, swap
the commands in `gate.py` — the hook wiring in `settings.json` is
stack-agnostic. `.pre-commit-config.yaml` and `ci.yml` are Python-specific
templates; translate or skip them elsewhere.

In a shared repo, consider putting the hooks block in
`.claude/settings.local.json` (untracked) rather than committing
`settings.json`, so you're not imposing hooks on collaborators.
