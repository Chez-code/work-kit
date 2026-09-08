# WORK-SETUP — installing and updating the kit on a .NET/C# machine

Instructions written so a Claude Code session on the target machine can apply
them directly. Paste-ready prompt blocks are at the bottom.

## Preflight

```bash
python3 --version    # the hook command hardcodes python3
dotnet --version     # the .NET gate shells dotnet build/test
claude --version
```

Version note: the default kit needs PostToolUse, UserPromptSubmit and Stop
command hooks, and `hookSpecificOutput.additionalContext` on PostToolUse
(all present in Claude Code 2.x). Only the async-Stop opt-in (see README
"Advanced") requires a version whose docs list `asyncRewake`. `tmux` is
optional: without it the watcher runs in any second terminal and alerts are
bell-only.

## Fresh install

```bash
git clone https://github.com/Chez-code/work-kit ~/work-kit

# tmux (optional — only if you do not already have a ~/.tmux.conf)
[ -f ~/.tmux.conf ] || cp ~/work-kit/tmux.conf ~/.tmux.conf
[ -d ~/.tmux/plugins/tpm ] || git clone https://github.com/tmux-plugins/tpm ~/.tmux/plugins/tpm

# harness into a repo
~/work-kit/bootstrap.sh /path/to/repo
cd /path/to/repo
```

Then move the hooks out of the tracked settings file:

- If `.claude/settings.local.json` does **not** exist:
  `mv .claude/settings.json .claude/settings.local.json`
- If it already exists (Claude Code accumulates permission allow rules
  there): **merge** the `"hooks"` block from `.claude/settings.json` into it,
  then delete `.claude/settings.json`. Never overwrite the local file — a
  copy wipes those accumulated permissions.

A hand-created `settings.local.json` is NOT auto-ignored by git — the exclude
step is manual and required:

```bash
echo ".claude/settings.local.json" >> .git/info/exclude
```

### What goes into the repo's git

- **Repo with no tracked `.claude/`** (fresh project): commit `CLAUDE.md` —
  it's project spec and benefits everyone. Exclude the whole harness
  directory: `echo ".claude/" >> .git/info/exclude`. Note gate.py then lives
  only in this checkout; `sync.sh`'s `.prev` rule is its only safety net.
- **Repo whose team already commits its own `.claude/`**: leave the team's
  `settings.json` alone (skip the mv entirely — delete the kit-copied one if
  bootstrap added it). Personal hooks go in `settings.local.json`. For the
  scripts, either add them to the tracked directories (team-visible) or
  exclude them by exact path — a directory-wide exclude does nothing for
  already-tracked files:

  ```bash
  for f in .claude/hooks/gate.py .claude/hooks/gate_log.py .claude/hooks/bash_guard.py .claude/bin/gate-watch.py; do
    echo "$f" >> .git/info/exclude
  done
  ```

  bootstrap already added the three runtime files (`gate-log.jsonl`,
  `gate-state.json`, `gate-watch.status-style`) to the exclude file; check
  with `git status` after the first gate run.

### CLAUDE.md on a .NET repo

The template's Commands section lists uv/ruff/mypy. Replace it with:

```markdown
## Commands
- Build: `dotnet build`
- Test: `dotnet test`
```

(bootstrap already skips the Python-only pre-commit/CI files on .NET repos.)

### No gate adaptation needed

`gate.py` auto-detects .NET (`.sln`/`.slnx`/`.csproj` at the root or one
level down): build after each `.cs` edit, build + `dotnet test --no-build` at
Stop. If the repo has several solutions, the gate skips with a visible
message — set `GATE_DOTNET_TARGET` as the message describes to pin the
target.

## Updating (after new kit commits)

```bash
git -C ~/work-kit pull
~/work-kit/sync.sh /path/to/repo
```

`sync.sh` overwrites only kit-managed files and keeps every replaced file as
`<file>.prev` — review the diff, then delete the `.prev`.

**One-time step for the timeout-race release**: `sync.sh` never touches
`settings.local.json`, so if your hooks live there, change any
`"timeout": 300` fields in its hook entries to `600` by hand. Leaving them at
300 means the harness kills a hung gate before the gate can report it — the
hang then passes silently.

**One-time step for the watcher release**: two new hook entries. If your
hooks live in `settings.local.json`, merge these into it by hand (the
kit's `settings.json` shows them in place):

```json
"PostToolUse": [
  { "...": "the existing Write|Edit|MultiEdit entry stays" },
  { "matcher": "Bash", "hooks": [ { "type": "command",
    "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/bash_guard.py\"", "timeout": 600 } ] }
],
"UserPromptSubmit": [
  { "hooks": [ { "type": "command",
    "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/gate.py\" --new-prompt", "timeout": 10 } ] }
]
```

Then run `sync.sh` (it installs `gate_log.py`, `bash_guard.py` and
`bin/gate-watch.py` if bootstrap put them there; on a repo bootstrapped
before this release, copy them once: `cp ~/work-kit/kit/.claude/hooks/{gate_log,bash_guard}.py .claude/hooks/ && mkdir -p .claude/bin && cp ~/work-kit/kit/.claude/bin/gate-watch.py .claude/bin/`),
and append the three runtime files to the exclude file as bootstrap would.

## Smoke test (run in the repo)

Exercises both real dotnet paths without waiting for an organic edit:

```bash
echo '{"cwd":"'"$PWD"'"}' | python3 .claude/hooks/gate.py --stop; echo "exit=$?"
echo '{"cwd":"'"$PWD"'","tool_input":{"file_path":"X.cs"}}' | python3 .claude/hooks/gate.py; echo "exit=$?"
```

Expect `exit=0` on a green repo; a failure prints the gate's reason. A
`systemMessage` JSON line means the gate skipped — read it, it says why and
how to fix it.

Then the guard, the prompt reset and the log:

```bash
echo '{"cwd":"'"$PWD"'","tool_input":{"command":"sed -i s/a/b/ X.cs"}}' | python3 .claude/hooks/bash_guard.py; echo "exit=$?"
echo '{}' | python3 .claude/hooks/gate.py --new-prompt; echo "exit=$? (no other output expected)"
tail -n 4 .claude/gate-log.jsonl
python3 .claude/bin/gate-watch.py --stats
git status --short    # gate-log.jsonl must NOT appear
```

The kit's own tests run from the kit checkout, not the repo:

```bash
cd ~/work-kit && python3 -m unittest discover tests && tests/smoke.sh
```

**Capture real dotnet output for the kit test** (once per machine): the
failure-key patterns for `dotnet build` and `dotnet test` ship with the
documented formats. Confirm them against the real thing by breaking a
build on purpose, then paste the lines into `DOTNET_BUILD_SAMPLE` and
`DOTNET_TEST_SAMPLE` in `~/work-kit/tests/test_gate_log.py` and re-run the
unittest:

```bash
dotnet build -v q 2>&1 | grep -m1 ' error '
dotnet test -v q 2>&1 | grep -m1 'Failed '
```

Finally, in tmux: launch `claude` from the repo root, press `prefix + W`,
make one `.cs` edit, and watch the `EDIT ... green` line appear.

## Paste-ready prompts for the machine's Claude

Fresh install:

> Read ~/work-kit/WORK-SETUP.md and apply the fresh-install steps to
> <repo path>. This repo [has / does not have] a tracked .claude/ directory —
> follow the matching git branch of the instructions. Finish by running the
> smoke test and showing me both exit codes.

Update:

> Read ~/work-kit/WORK-SETUP.md and apply the update steps to <repo path>:
> pull the kit, run sync.sh, show me any .prev diffs, apply the one-time
> settings.local.json timeout change if present, then run the smoke test.
