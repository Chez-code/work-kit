# OPERATOR — the human half of the harness

The harness is three gates, not two. The edit gate and the Stop gate are
scripts. The third gate is you. This guide describes what the operator does at
each step of a task, what signals to watch, and which controls to reach for.
`TROUBLESHOOTING.md` covers what to do when a gate misbehaves; this file covers
how to run a session well when everything works.

## The operator's loop

```
 1. Spec      brainstorm in chat -> CLAUDE.md written and committed
 2. Plan      plan mode for anything touching more than one file
 3. Execute   Claude edits; the edit gate runs; you watch the signals
 4. Verdict   Claude says done; the Stop gate rules; you read the verdict
 5. Anchor    commit the green state
 6. Update    CLAUDE.md gets what you learned (locked specs, deferrals)
```

Steps 1, 4, 5 and 6 are yours. The scripts cannot do them.

## 1. Spec: brainstorm first, then write CLAUDE.md

CLAUDE.md is loaded into every session in that repo. It is the only thing
that survives between sessions besides the code, so the spec lives there.

The workflow that works:

1. Launch `claude` in the repo. Press `Shift+Tab` until the mode indicator
   shows plan mode. In plan mode Claude reads and proposes but does not edit,
   which makes it safe for open-ended brainstorming.
2. Talk it through: what the system is, who it serves, what "done" means, what
   must never change. Push back, change your mind, ask for alternatives.
3. When you have it, say: "Write this into CLAUDE.md using the template's
   sections. Mark the non-negotiables [LOCKED], the current choices [DEFAULT],
   and the undecided points [OPEN]. Put the build order in phases with a
   definition of done for each." Leave plan mode (`Shift+Tab`) so it can
   write the file.
4. Read the file yourself. A [LOCKED] tag you did not mean is a constraint
   you will fight later. Fix the wording until every line is something you
   would defend.
5. Commit it. `git add CLAUDE.md && git commit -m "Project brief"`. The spec
   is now a fixed point that survives any rewind or session reset.

After each phase, come back to it: move [OPEN] items to [DEFAULT] or [LOCKED],
add anything you deliberately deferred to the "Deliberate deferrals" section,
and correct the Commands section if the real commands changed. Ask Claude to
make the edit and review the diff before committing.

## 2. Plan: use plan mode for multi-file work

The template's working agreements already say this. In practice: for any task
that will touch more than one file, ask for a plan first, read it, correct it,
then approve. The gates verify code; the plan is where you verify intent.

## 3. Execute: what to watch and how

### The transcript viewer (`Ctrl+O`)

`Ctrl+O` toggles the transcript viewer. It shows the full conversation
including every tool call Claude makes and the feedback the hooks sent back.
The normal view shows summaries; the transcript shows the raw sequence.

What a gate looks like in there:

- **Green edit gate**: nothing. The gate exits 0 with no output. You see the
  edit and then Claude's next action.
- **Blocked edit gate**: a block of text starting `[EDIT GATE FAILED] Fix
  before proceeding.` followed by the last 40 lines of the failing command.
  Claude's next action should be a fix to the reported problem.
- **Green Stop gate**: Claude's final message with no gate text after it.
- **Blocked Stop gate**: `[STOP GATE FAILED]` after Claude tried to finish,
  then Claude continues working.
- **Gate crash**: a line reading `<hook name> hook error` with
  `Failed with non-blocking status code` and the first line of a traceback.
  That is a bug in the gate, not in your code. It did not block anything.
- **Gate skipped**: a system message line beginning `[gate]` saying why. Skips
  mean nothing is being verified until you fix the cause.

### Why keep it open on the first day

You are learning the baseline. After a day you know how many edits a normal
task takes, what a fix-after-block looks like, how long the Stop gate takes on
your suite, and what "Claude said done and the gate agreed" looks like.
Without that baseline you cannot tell a quiet session from a broken one,
because a gate that never fires and a gate that always passes look identical
from the summary view.

After the first day you open it in three situations: Claude says done and
something feels off; a task took far more edits than usual; you want to
confirm the Stop gate actually ran before you commit. The debug log
(`/debug` shows the path) has everything else, including exit-0 hook output.

### The file tools, and how to ask for them

Claude Code edits files two ways. The file tools, called `Read`, `Edit` and
`Write`, are what the edit gate watches. Shell commands run through `Bash`,
such as `sed -i`, `perl -pi`, `cat > file <<EOF`, `echo >> file` or a script
that rewrites source, are not watched by the edit gate. Only the Stop gate
stands under them, and the Stop gate blocks once per stop.

The gate does not care which one Claude used; it only fires on the tool
events it can see. So the operator steers Claude toward the visible path.

Standing instruction, in CLAUDE.md working agreements:

```markdown
- Make source changes with the Edit/Write file tools, never with sed, perl,
  heredocs or scripts through Bash. The verification gate only sees file
  tool edits.
```

One-off instruction when you see it happen: "Use the Edit tool for that
change, not sed. The gate did not see that edit." Claude will redo it.

Hard stop, if you want one: a permission deny rule in `settings.local.json`
refuses the common shell rewrites outright.

```json
"permissions": { "deny": ["Bash(sed -i*)", "Bash(perl -pi*)"] }
```

How you spot a Bash edit in the transcript: the tool call is `Bash` with a
command string, not `Edit` with a file path and a diff.

## 4. Verdict: reading the Stop gate

When Claude says the task is done, the Stop gate has already run. The
verdict is in the transcript, and only there.

- No `[STOP GATE FAILED]` text after the final message: green. Proceed to
  commit.
- `[STOP GATE FAILED]` followed by more work, then a second "done": the second
  stop was not gated. The gate lets go on the second consecutive stop so it
  cannot loop forever. Read the failure text. If Claude fixed what it named,
  run the suite yourself once (`! dotnet test` or `! uv run pytest -q` in the
  prompt runs it in the session) before you trust it. If it did not, say so:
  "The Stop gate reported X. It is not fixed. Fix it." The next stop is gated
  again.
- Skip message at Stop: nothing was verified. Fix the cause before
  committing anything.

The rule: Claude's summary is a claim; the gate text is the evidence. Commit
on evidence.

## 5. Anchor: commit green states

Every green Stop is a checkpoint worth keeping. Small commits, one logical
change each, as the working agreements say. Two reasons beyond hygiene:

- `/rewind` restores file-tool edits only. Shell-made changes and subagent
  edits are outside its reach. Git covers those.
- A committed green state is the point you rewind to when the next approach
  fails, whether by `/rewind` or by `git checkout`.

## 6. Recovery: /rewind in detail

### What it is

Claude Code snapshots the files it edits before each prompt you send. The
rewind menu lets you jump back to any of those points. It is session-level
undo, separate from git.

Open it with `/rewind`, or press `Esc` twice with an empty prompt. If there is
text in the prompt, double `Esc` clears the text instead; press `Up` to get it
back, then try again with the prompt empty.

The menu lists every prompt you sent this session. Pick one, then choose:

- **Restore code and conversation**: files and chat both go back to that
  point. This is the one you want when an approach failed.
- **Restore conversation**: chat goes back, files stay as they are now. Use
  when the code is fine but the conversation went somewhere useless.
- **Restore code**: files go back, chat stays. Use when you want Claude to
  remember what it tried but start from clean files.
- **Summarize from here** / **Summarize up to here**: compress part of the
  conversation to free context. Not a rewind; files do not change.
- **Never mind**: close the menu.

After a restore, the prompt you selected is put back in the input field so
you can edit it before re-sending. That is where you add the constraint that
the failed approach taught you.

### What it does not restore

- Files changed by Bash commands. `rm`, `mv`, `cp`, `sed -i`, scripts.
- Edits made by subagents that ran in the background.
- Changes made outside Claude Code, or by another session.
- Symlinked or hard-linked files. The menu warns
  `Restored the code, but skipped N files` if it hit any.

For all of those, git is the undo. This is another reason to commit green
states and to steer Claude to the file tools.

Checkpoints persist with the session, so `/rewind` still works after
`/resume`. They are kept for the 100 most recent prompts and for about 30
days after the session last saved one.

### When to use it: recognising a loop

A loop is Claude trying to fix the same failure repeatedly without making
progress. The gate feeds the failure back each time, so the loop shows up as
repeated `[EDIT GATE FAILED]` blocks. The signals, in rough order of how early
they appear:

1. **Same test name, third time.** The failure text names the same test or
   the same compiler error in three consecutive gate blocks. Two is a normal
   fix-and-adjust. Three is a loop.
2. **Oscillation.** The fix adds something, the next fix removes it, the next
   adds it back. Claude is trying to satisfy two constraints it has not
   noticed conflict.
3. **Shrinking edits.** Each fix is smaller than the last and changes less.
   Claude is out of ideas and is nudging.
4. **Editing the test instead of the code.** The failing test gets modified,
   weakened, skipped or deleted. Unless the test was actually wrong, this is
   the loop escaping sideways.
5. **Proposing to disable the gate.** Any suggestion to skip the hook,
   comment out the test, or run with the check off.
6. **Switching to Bash for the edit.** Often unconscious, but it takes the
   edit out of the gate's sight.

Signals 4 through 6 are worth interrupting immediately (`Esc` stops Claude
mid-turn). Signals 1 through 3 are the "three strikes" rule: on the third
block for the same failure, rewind.

### How to rewind out of a loop

1. `Esc` to interrupt if Claude is mid-turn.
2. `Esc` `Esc` (empty prompt) or `/rewind`.
3. Pick the prompt just before the failed approach started. Usually that is
   the prompt where you asked for the feature, not the fixes after it.
4. **Restore code and conversation.**
5. The original prompt is back in the input. Add what you now know:
   "The previous attempt did X and looped on test Y because Z. Do not do X.
   Consider W instead." Then send it.

If the loop involved Bash edits, run `git status` first. Anything the rewind
could not restore needs `git checkout -- <file>` before the retry.

If you are unsure whether to rewind or push on, rewinding is cheap and the
failed attempt is still in the transcript. Pushing on past three strikes
mostly produces a fourth.

## 7. Pointing GATE_DOTNET_TARGET at a project

### Why

The gate resolves a single solution or project automatically. When it finds
several, it skips and tells you. Even when it finds one, building the whole
solution after every `.cs` edit can be slow on a large codebase. The override
fixes both: it names exactly what to build, and it can name a smaller target
for the edit gate than for the Stop gate.

### Where

In the hook command string in `.claude/settings.local.json`, as a prefix. The
path is relative to the repo root, because the gate runs from there. Two
entries, two different targets:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "GATE_DOTNET_TARGET=src/Orders/Orders.csproj python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/gate.py\"",
            "timeout": 600
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "GATE_DOTNET_TARGET=src/App.sln python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/gate.py\" --stop",
            "timeout": 600
          }
        ]
      }
    ]
  }
}
```

Read it as: after every edit, build only the Orders project; when Claude says
done, build and test the whole solution. Change the edit-gate target when you
move to a different project in the same solution.

### How to pick the target

- Edit gate: the `.csproj` you are currently working in. `dotnet build` on a
  project also builds the projects it references, so dependencies are covered.
- Stop gate: the `.sln` or `.slnx` that contains the test projects. If the
  tests live in a separate solution, point the Stop gate at that one.
- Check the choice by hand once: `dotnet build src/Orders/Orders.csproj` from
  the repo root should succeed, and `dotnet test src/App.sln --no-build`
  should find tests. If the test run reports zero tests, the target does not
  include a test project.

### Verify it took

Run the smoke test from WORK-SETUP with the same prefix:

```bash
GATE_DOTNET_TARGET=src/App.sln python3 .claude/hooks/gate.py --stop <<< '{"cwd":"'"$PWD"'"}'; echo "exit=$?"
```

No skip message and `exit=0` on a green solution means the target resolved.
In a session, `/hooks` shows the command string Claude Code loaded, prefix
included.

## 8. A note on what the operator does not do

- Does not disable hooks to get past a block. The block is the harness
  working. Diagnose with `TROUBLESHOOTING.md`.
- Does not accept "done" without the gate verdict.
- Does not let [LOCKED] specs be worked around. If a task conflicts with one,
  the spec changes deliberately or the task does.
- Does not push through a loop. Three strikes, rewind.
