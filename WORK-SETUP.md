# WORK-SETUP — installing and updating the kit on a .NET/C# machine

Instructions written so a Claude Code session on the target machine can apply
them directly. Paste-ready prompt blocks are at the bottom.

## Preflight

```bash
python3 --version    # the hook command hardcodes python3
dotnet --version     # the .NET gate shells dotnet build/test
claude --version
```

Version note: the default kit needs nothing newer than basic hook support
(PostToolUse/Stop command hooks). Only the async-Stop opt-in (see README
"Advanced") requires a Claude Code version whose docs list `asyncRewake`.

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
  bootstrap added it). Personal hooks go in `settings.local.json`. For
  gate.py, either add it to the tracked hooks directory (team-visible) or
  exclude it by exact path: `echo ".claude/hooks/gate.py" >> .git/info/exclude`
  — a directory-wide exclude does nothing for already-tracked files.

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

## Smoke test (run in the repo)

Exercises both real dotnet paths without waiting for an organic edit:

```bash
echo '{"cwd":"'"$PWD"'"}' | python3 .claude/hooks/gate.py --stop; echo "exit=$?"
echo '{"cwd":"'"$PWD"'","tool_input":{"file_path":"X.cs"}}' | python3 .claude/hooks/gate.py; echo "exit=$?"
```

Expect `exit=0` on a green repo; a failure prints the gate's reason. A
`systemMessage` JSON line means the gate skipped — read it, it says why and
how to fix it.

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
