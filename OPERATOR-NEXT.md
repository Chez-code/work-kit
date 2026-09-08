# OPERATOR-NEXT — the loop with the automation (design preview)

Status, 2026-09-07: release 1 is BUILT. Sections 0 (watcher pane), 3
(Bash guard, gate log, loop detector), 5 and 7 (`--stats`) describe what is
now in the kit; OPERATOR.md is the operating reference for those. Sections
1, 2, 4 and 6 (`/spec-check`, the reviewer loop, the review pane) are still
design: nothing under those names exists yet. Decisions taken during the
build that differ from the first draft are marked "as built".

The three gates stay. What changes is that the scripts now watch the things
OPERATOR.md told you to watch by eye, and a cold reviewer does the
back-and-forth you did by hand between two terminals.

## The operator's loop, revised

```
 0. Setup     tmux: builder pane | watcher pane | review pane
 1. Spec      brainstorm -> CLAUDE.md -> /spec-check -> commit
 2. Plan      plan mode -> plan file -> /review plan -> you approve
 3. Execute   Claude edits; gate + Bash guard + loop detector; watcher alerts
 4. Verdict   Stop gate -> /review build -> you approve
 5. Anchor    commit the green state
 6. Update    /spec-check -> commit
```

What is still yours: the brainstorm, every approve button, every commit, and
the decision to rewind. What the scripts now do: keep the log, catch Bash
edits, count strikes, run the reviewer rounds, and tap you on the shoulder.

## 0. Setup: three panes

One tmux window per repo, three panes. The kit's `tmux.conf` gets a
keybinding that opens this layout.

```
+---------------------------------+-------------------------------+
|                                 |  gate-watch                   |
|  claude  (builder)              |  12:01 EDIT  Orders.cs  green |
|                                 |  12:03 EDIT  Orders.cs  green |
|                                 |  12:04 STOP  green  38s       |
|                                 +-------------------------------+
|                                 |  review tail                  |
|                                 |  docs/reviews/phase-1/        |
|                                 |    round-1.md  CHANGES (3)    |
|                                 |    round-2.md  APPROVED       |
+---------------------------------+-------------------------------+
```

- **Builder pane**: the `claude` session. Same as today.
- **Watcher pane**: `gate-watch`, a script that tails `.claude/gate-log.jsonl`
  and prints one line per gate event. Quiet when things are green. Rings the
  bell and flashes the tmux status bar on anything that needs you.
- **Review pane**: tails `docs/reviews/`. This replaces your second Claude
  terminal. The reviewer's notes land here as files, so you read them the
  same way you read the reviewer chat today. (Design; release 2.)

As built: `prefix + W` opens only the watcher pane, to the right of the
pane you press it in. The review pane comes with the reviewer loop.

You never type into the watcher or review panes. They are read-only.

## 1. Spec: brainstorm, write, check, commit

Steps 1 to 3 are unchanged: plan mode, talk it through, tell Claude to write
CLAUDE.md with the tags and the phases.

Step 4 was "read the file yourself". It is now `/spec-check`. Claude reads
CLAUDE.md and walks you through it one tag at a time:

```
/spec-check

CLAUDE.md: 4 LOCKED, 3 DEFAULT, 2 OPEN, 0 placeholders left.

[LOCKED] All monetary amounts are Decimal.
  keep / downgrade to DEFAULT / reword / drop ?
> keep

[LOCKED] No live API keys; testnet or read-only only.
> keep

[LOCKED] Every adapter raises a typed error, never returns None.
> downgrade

[OPEN] Retry policy on transient errors.
  keep open / decide now / move to deferrals ?
> move to deferrals
  Where does it land instead?  > scheduler phase
  Why not now?                 > adapter should stay dumb

Build order: 3 phases, each with a definition of done.  ok / edit ?
> ok

Commands section: dotnet build / dotnet test.  ok / edit ?
> ok

Rewrote CLAUDE.md. Diff:
  -- [LOCKED] Every adapter raises a typed error...
  ++ [DEFAULT] Every adapter raises a typed error...
  ++ - retry/backoff on transient errors -> scheduler phase; adapter stays dumb
Commit it?  yes / no
```

You are still the one defending every line. The command just makes sure you
look at each one instead of trusting the file was written right.

Backstop for the day you skip it: the Stop gate refuses to go green while
CLAUDE.md still contains a template placeholder such as `<PROJECT NAME>`.

## 2. Plan: plan mode, then the reviewer loop

Plan mode as before. One change at the end: instead of approving the plan in
the plan-mode dialog, say "write the plan to `docs/plans/phase-1.md`", leave
plan mode, and run:

```
/review plan docs/plans/phase-1.md
```

What happens, in order:

1. A reviewer subagent starts with a fresh context. It gets the plan,
   CLAUDE.md and read-only access to the repo. It does not get the builder's
   conversation, which is the point: it is as cold as your second terminal.
2. It writes `docs/reviews/phase-1/round-1.md`. The review pane shows it the
   moment it lands. Every round file ends with one line: `VERDICT: APPROVED`
   or `VERDICT: CHANGES` followed by the numbered findings.
3. The builder reads the findings, revises the plan file, and appends a
   "Response to round 1" section to the plan saying what changed for each
   finding and what it pushed back on.
4. It pauses and asks you:

```
Round 1: CHANGES (3 findings). Plan revised.
  1. Phase 1 DoD does not say which tests prove it     -> added test names
  2. Step 4 edits Orders.cs and Billing.cs together    -> split into 4a/4b
  3. Retry logic in adapter conflicts with deferral    -> removed from plan

  [1] Send back to the reviewer
  [2] Approve the plan as revised
  [3] I will edit the plan file, then re-run
  [4] Auto: keep looping up to 3 rounds, pause when APPROVED or at the cap
  [5] Stop
```

Option 1 is your current "give the revised plan to the reviewer" step.
Option 4 is the loop you asked for. Option 2 is the human approval that the
reviewer never gets to give on your behalf.

While it loops you watch the review pane. Each round is a new file; nothing
is overwritten. If a round says something you disagree with, hit `Esc`, edit
the plan yourself, and pick option 3.

When you approve, the command records `APPROVED by operator, round N` at the
bottom of the plan file and commits the plan and the review folder together.
The plan is now a fixed point like CLAUDE.md.

## 3. Execute: what the scripts watch now

You still type "build phase 1 per docs/plans/phase-1.md" and watch it go.
The difference is what fires without you.

### Bash-edit guard

As built (warn mode, decided in the open questions): a PostToolUse hook on
Bash. The command has already run, so instead of blocking, the guard re-runs
the gate on the touched file and reports:

```
[bash guard] Bash edit to src/Orders.cs (in-place edit), gate re-run: green
```

Claude also gets a note that the change was made outside the gate and that
Edit is the right tool. If the re-run fails, Claude gets the gate's failure
text and fixes it as usual. The watcher shows `BASH ... WARN ... gate: green`.
Block mode exists (`--block` as a PreToolUse entry, see OPERATOR.md) for
when the log shows it is needed.

### The gate log

Every gate run writes one line to `.claude/gate-log.jsonl`. The watcher pane
turns those into:

```
12:01:14 EDIT   Orders.cs          green                       4s
12:01:52 EDIT   Orders.cs          BLOCK                       6s  CS0103: The name 'total' does not exist in t   strike 1
12:02:20 EDIT   Orders.cs          green                       4s
12:03:05 EDIT   OrdersTests.cs     green                       5s
12:04:40 STOP                      green                      38s  build, test
```

As built: a gate that crashes logs `CRASH`; a start line with no end line
after 610 s prints `KILLED` (the harness timed the hook out). A `[gate]`
skip prints as `SKIP` with the reason. A Stop that passed without running
anything prints `nothing ran` in yellow. All of the red ones ring the bell,
because all of them mean nothing is being verified.

### Loop detector

The gate keeps the last few failures in a small state file. On the third
consecutive block naming the same test or compiler error, it stops feeding
the failure back and instead blocks with:

```
[LOOP DETECTED] CS0103 'total' has failed 3 edits in a row
(12:01, 12:05, 12:08). Do not attempt another fix. Stop and tell the
operator what you tried and why each attempt failed.
```

Claude writes the summary and stops. The watcher pane prints `LOOP` in red,
rings the bell, and the tmux status bar turns red until the next green Stop
or your next prompt. Your move is the OPERATOR.md rewind procedure, now with
the summary already written for you: `Esc Esc`, pick the prompt before the
failed approach, restore code and conversation, add the constraint, resend.

As built: the strike key is the whole set of failures, so fixing four of
five compile errors is progress, not a strike. Every prompt you send resets
the count (a UserPromptSubmit hook), so a loop means "three identical blocks
with no operator intervention", and a rewind starts clean.

The other loop signals map like this:

| Signal in OPERATOR.md          | Now                                        |
|--------------------------------|--------------------------------------------|
| 1. Same failure, third time    | Automatic. Gate blocks, watcher alerts.    |
| 2. Oscillation                 | Watcher warns on add/remove/add on one file (size heuristic). |
| 3. Shrinking edits             | Watcher warns on three shrinking blocked edits. |
| 4. Editing the test instead    | Watcher warns when a failing test's file is edited while red. |
| 5. Proposing to disable gate   | Still yours. Read the transcript.          |
| 6. Switching to Bash           | Guard warns and re-runs the gate; watcher shows it. |

Warnings are yellow lines with a bell. They do not block Claude. Signal 4 is
a warning rather than a block because sometimes the test really is wrong.

### What you do during execution

Mostly nothing. Watch the watcher pane, not the transcript. Open the
transcript (`Ctrl+O`) only when the watcher shows something red or when the
count of edits for this task is well past your baseline from `/gate-stats`.

## 4. Verdict: Stop gate, then the build review

Claude says done. The Stop gate runs. The watcher pane shows `STOP green`
or `STOP BLOCK` with the failure. Same rules as OPERATOR.md: commit on the
gate text, not on the summary. As built, the second consecutive stop (which
the gate cannot block) still runs the suite and shows `released green` or
`RELEASED still failing`, so a red suite handed back is visible.

Then, on a green Stop:

```
/review build docs/plans/phase-1.md
```

Same loop as the plan review, pointed at the diff. The reviewer gets the
plan, the diff since the plan was approved, and read-only repo access. It
checks that the build did what the plan said, nothing more and nothing
less, and that the [LOCKED] specs held. Rounds land in
`docs/reviews/phase-1/build-round-N.md`. The builder fixes, the gate
verifies each fix, and the same five-option menu comes back to you.

Approve, and the phase is done. The command writes the closing note into
the plan file and stops. It does not commit for you.

## 5. Anchor: commit

Unchanged. The watcher pane's last line should read `STOP green` before you
commit. If the last line is a `SKIP` or `CRASH`, nothing was verified and
you fix that first.

## 6. Update: spec check again, then clear

`/spec-check` again. It now also lists anything the phase's review rounds
flagged as a decision, and offers to move it into the tags or the
deferrals. Commit. `/clear`. Next phase.

## 7. Weekly: the baseline

```
python3 .claude/bin/gate-watch.py --stats --since 7d

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

As built: a script flag rather than a slash command, so it works outside a
Claude session. Review-round counts arrive with the reviewer release.

That is the baseline OPERATOR.md told you to build by keeping the
transcript open for a day. Now it is a number you can compare a session
against.

## What stays manual, on purpose

- The brainstorm. No command can decide what the system is.
- Every approve: spec, plan, build. The reviewer's APPROVED is a claim from
  a model, same as the builder's "done".
- Every commit.
- The rewind. The detector tells you when; it does not choose the prompt to
  go back to.
- Reading the transcript when something is red.

## Open questions for the build plan

Release 1 answers: tmux optional (bell-only without it); Bash guard warn
mode with gate re-run; loop threshold 3 (`GATE_LOOP_STRIKES`), reset on
every prompt. Still open for release 2:

- Does the work machine run tmux? If not, alerts fall back to the terminal
  bell and the watcher runs in a second window.
- Where do `docs/plans/` and `docs/reviews/` live: committed to the repo, or
  excluded like the harness? Committed is better for a team; excluded keeps
  the repo clean on a work codebase you do not own.
- Auto-loop cap: three rounds, or configurable per command?
- Bash guard: DECIDED, warn only for now. Flip to block later if the log
  shows Bash edits happening often enough to matter.
- Loop detector threshold: three, as OPERATOR.md says, or two for the
  Stop gate since it only blocks once?
