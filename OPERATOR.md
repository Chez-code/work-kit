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

### The watcher pane (`prefix + W`)

Launch `claude` inside tmux from the repo root, then press `prefix + W`. A
pane opens on the right running `.claude/bin/gate-watch.py`, which tails
the gate log (`.claude/gate-log.jsonl`) and prints one line per gate event:

```
12:01:14 EDIT   Orders.cs          green                       4s
12:01:52 EDIT   Orders.cs          BLOCK                       6s  CS0103: The name 'total' does not exist in t   strike 1
12:08:10 EDIT   Orders.cs          LOOP                        5s  CS0103: The name 'total' does not exist in t   strike 3
12:09:00 BASH   Orders.cs          WARN                            in-place edit: sed -i s/a/b/ src/Orders.cs   gate: green
12:10:22 STOP                      green                      38s  build, test
12:11:05 STOP                      green                       0s  nothing ran
12:12:40 STOP                      released green             36s  build, test
12:13:10 STOP                      RELEASED still failing     40s  Orders.Tests.TotalTest
12:15:00 PROMPT                                                    strikes reset
13:00:01 STOP                      SKIP                        0s  [gate] .NET gate skipped (2 solution files found)
```

Three colors, three levels of attention:

- **Plain**: `green`, `released green`, `PROMPT`. Nothing to do.
- **Yellow, bell**: `BLOCK`, a `BASH WARN`, a green that says `nothing ran`,
  a heuristic `WARN` line. Glance at it.
- **Red, bell, tmux message, status bar turns red**: `LOOP`, `SKIP`,
  `CRASH`, `KILLED`, `RELEASED still failing`. Act on it.

Line by line:

- `BLOCK ... strike N`: the edit gate failed and fed the failure back.
  Strike N is how many consecutive blocks named the same set of failures.
  One or two is a normal fix-and-adjust.
- `LOOP`: third strike. The gate did not feed the failure back; it told
  Claude to stop and report what it tried. Your move is section 6.
- `BASH ... WARN ... gate: X`: Claude changed a source file through Bash.
  The guard re-ran the gate on that file and X is the result: `green`,
  `block`, `loop`, `not gated` (an extension the gate ignores, like `.md`)
  or `target not found` (Python lane, the path did not exist).
- `green ... nothing ran`: the Stop gate passed without running anything.
  Usually a Python repo with no `tests/` directory or no test project in
  the solution. Green here means nothing was verified.
- `released green` / `RELEASED still failing`: the second consecutive stop,
  which the gate lets through by design. It still ran the suite; the
  second form means Claude handed back a red suite. Read section 4.
- `PROMPT ... strikes reset`: you sent a prompt. The loop counter starts
  over, and the status bar goes back to normal.
- `SKIP`, `CRASH`, `KILLED`: nothing was verified. Skip says why (PATH,
  ambiguous target). Crash is a bug in the gate. Killed means the hook hit
  the 600 s harness timeout, which is a hang in the build or suite.
- Indented `WARN` lines are heuristics from the edit sizes: `shrinking
  edits` (three blocked edits, each smaller), `oscillating on <file>`
  (add, remove, add), `editing the failing test <file>` (a test named in
  the failure was edited while red). They are hints, not verdicts.

`[sub]` on a line means a subagent produced it. The status bar returns to
its saved style on the next green Stop or your next prompt; `Ctrl+C` in
the pane restores it too.

### The transcript viewer (`Ctrl+O`): the fallback

`Ctrl+O` toggles the transcript viewer. It shows the full conversation
including every tool call Claude makes and the feedback the hooks sent back.
The normal view shows summaries; the transcript shows the raw sequence.
Open it when the watcher shows red, or when Claude's summary and the
watcher disagree.

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
- **Loop detected**: `[LOOP DETECTED] <failure> has failed 3 edits in a row
  (...). Do not attempt another fix. Stop and tell the operator ...`. Claude's
  next message should be a report, not another edit.
- **Bash guard**: a system message `[bash guard] Bash edit to <file>
  (<what>), gate re-run: <result>`, or on a failed re-run the gate's own
  `[EDIT GATE FAILED]` text prefixed with `[bash guard]`.

### The baseline

The watcher log is the baseline. After a week, run:

```bash
python3 .claude/bin/gate-watch.py --stats --since 7d
```

```
Sessions            9
Prompts             41
Edits               212    (avg 23.6 per session, max 61, 5.2 per prompt)
Blocks              31     (15%)   avg time to green after block: 1m 40s
Loops               2
Bash edits          4      (4 gate re-runs, 1 failed)
Green, nothing ran  0
Released stops      3      (1 still failing)
Skips               1
Crashes             0
Killed              0
Stop gates          14     green 12, block 2, released 3
```

Those numbers tell you what a normal session looks like, so a quiet session
and a broken one no longer look the same. A session whose edit count is far
past the average, or any non-zero in the bottom half, is the cue to open the
transcript.

### The file tools, and the Bash guard

Claude Code edits files two ways. The file tools, called `Read`, `Edit` and
`Write`, are what the edit gate watches. Shell commands run through `Bash`,
such as `sed -i`, `perl -pi`, `cat > file <<EOF`, `echo >> file`, `tee`,
`rm`, `mv` or a script that rewrites source, are not watched by the edit
gate.

The Bash guard (`bash_guard.py`, a PostToolUse hook on Bash) closes most of
that gap. When a Bash command matches one of those shapes against a source
file inside the repo, the guard:

1. logs a `bash` event (the watcher shows `BASH ... WARN`),
2. runs the gate on the touched file (`edit` lines with tool `Bash`), and
3. tells both of you: a system message for the operator, and a note to
   Claude that the change was made outside the gate and that the Edit tool
   is the right path. If the re-run fails, Claude gets the gate's failure
   text and fixes it as usual.

The guard is a net, not the path. It warns, it does not block, and it cannot
verify a path it cannot resolve (`target not found`) or an extension the
gate ignores (`not gated`). So the operator still steers Claude toward the
visible path. Standing instruction, in CLAUDE.md working agreements:

```markdown
- Make source changes with the Edit/Write file tools, never with sed, perl,
  heredocs or scripts through Bash. The verification gate only sees file
  tool edits.
```

One-off instruction when the watcher shows a run of `BASH WARN` lines: "Use
the Edit tool for source changes, not sed. The gate did not see that edit."

Hard stop, if you want one: add the guard as a PreToolUse hook with
`--block` in `settings.local.json`. It then refuses the command before it
runs, with a one-line reason Claude can act on.

```json
"PreToolUse": [
  { "matcher": "Bash", "hooks": [ { "type": "command",
    "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/bash_guard.py\" --block",
    "timeout": 10 } ] }
]
```

Keep the PostToolUse entry as well: block mode refuses only the shapes that
have an Edit/Write equivalent (sed, redirects, tee, truncate, inline
scripts). Deletes, moves and copies have no file tool, so they go through
and the PostToolUse entry verifies them afterwards.

What the log holds: paths, verdicts, durations, failure keys (a test name or
a compiler error line), and the first 120 characters of a guarded Bash
command. It never holds prompt text or build output. It is excluded from
git by bootstrap.

## 4. Verdict: reading the Stop gate

When Claude says the task is done, the Stop gate has already run. The
verdict is in the transcript, and only there.

- No `[STOP GATE FAILED]` text after the final message: green. Proceed to
  commit.
- `[STOP GATE FAILED]` followed by more work, then a second "done": the second
  stop was not blocked. The gate lets go on the second consecutive stop so it
  cannot loop forever, but it still runs the suite and logs the result. The
  watcher shows `released green` when Claude's fix was real and `RELEASED
  still failing` in red when it was not. On the red form say so: "The Stop
  gate reported X. It is not fixed. Fix it." The next stop is gated again.
  Without the watcher, run the suite yourself once (`! dotnet test` or
  `! uv run pytest -q` in the prompt runs it in the session).
- Skip message at Stop: nothing was verified. Fix the cause before
  committing anything.

The rule: Claude's summary is a claim; the gate text is the evidence. Commit
on evidence.

## 5. Anchor: commit green states

Every green Stop is a checkpoint worth keeping. The watcher's last `STOP`
line should read `green` with something in the `ran` column, not `nothing
ran`, `SKIP` or `RELEASED still failing`. Small commits, one logical change
each, as the working agreements say. Two reasons beyond hygiene:

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
they appear, and who watches for each:

1. **Same failure, third time.** Automatic. The gate compares the set of
   failures between consecutive blocks; three identical sets is `LOOP`. The
   gate stops feeding the failure back and tells Claude to stop and report.
   A changed set (four errors became one) is progress, not a strike. Your
   next prompt resets the count, so after a rewind Claude starts at zero.
2. **Oscillation.** Watcher heuristic (`oscillating on <file>`): three edits
   to one file whose sizes go add, remove, add. Claude is trying to satisfy
   two constraints it has not noticed conflict.
3. **Shrinking edits.** Watcher heuristic (`shrinking edits`): three blocked
   edits, each smaller than the last. Claude is out of ideas and is nudging.
4. **Editing the test instead of the code.** Watcher heuristic (`editing the
   failing test`). Unless the test was actually wrong, this is the loop
   escaping sideways.
5. **Proposing to disable the gate.** Yours. Any suggestion to skip the hook,
   comment out the test, or run with the check off.
6. **Switching to Bash for the edit.** Guard. Shows as `BASH WARN`; a run of
   them while red is the loop escaping sideways.

Signals 4 through 6 are worth interrupting immediately (`Esc` stops Claude
mid-turn). Signal 1 interrupts itself; when you see `LOOP`, rewind.

To clear the strike count by hand (say, after you fixed the cause outside
Claude), send any prompt, or delete `.claude/gate-state.json`.

### How to rewind out of a loop

1. `Esc` to interrupt if Claude is mid-turn.
2. `Esc` `Esc` (empty prompt) or `/rewind`.
3. Pick the prompt just before the failed approach started. Usually that is
   the prompt where you asked for the feature, not the fixes after it. The
   `LOOP` line in the watcher carries the times of the three strikes, which
   places it in the prompt list.
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
- Does not accept "done" without the gate verdict. `released green` in the
  watcher is a verdict; Claude's summary is not.
- Does not let [LOCKED] specs be worked around. If a task conflicts with one,
  the spec changes deliberately or the task does.
- Does not push through a loop. Three strikes, rewind.
