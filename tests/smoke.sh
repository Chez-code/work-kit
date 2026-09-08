#!/usr/bin/env bash
# End-to-end smoke test for the kit's hooks and watcher. No dotnet needed: a
# recording shim (TROUBLESHOOTING rung 5) stands in for it. Run from anywhere:
#   tests/smoke.sh
# Exits non-zero on the first failed assertion and says which one.
set -euo pipefail

KIT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${SMOKE_DIR:-$(mktemp -d)}"
REPO="$WORK/repo"
SHIM="$WORK/shim"
LOG="$REPO/.claude/gate-log.jsonl"
STATE="$REPO/.claude/gate-state.json"
PASS=0
# The work dir is removed on success unless SMOKE_KEEP=1 (or SMOKE_DIR was given).
cleanup() { [ "${SMOKE_KEEP:-0}" = 1 ] || [ -n "${SMOKE_DIR:-}" ] || rm -rf "$WORK"; }

say()  { printf '\n== %s\n' "$*"; }
ok()   { PASS=$((PASS + 1)); printf '   ok  %s\n' "$*"; }
fail() { printf '   FAIL %s\n' "$*" >&2; exit 1; }
assert_eq()  { [ "$1" = "$2" ] || fail "$3: expected '$2', got '$1'"; ok "$3"; }
assert_has() { grep -q -- "$2" <<<"$1" || fail "$3: missing '$2' in: $1"; ok "$3"; }
assert_not() { grep -q -- "$2" <<<"$1" && fail "$3: unexpected '$2'"; ok "$3"; }
last_log()   { tail -n 1 "$LOG"; }
log_count()  { grep -c -- "$1" "$LOG" || true; }

shim() { # shim <exit code> [stdout line]  — what `dotnet` will do
  printf '#!/bin/sh\necho "dotnet $*" >> "%s/calls.log"\n' "$SHIM" > "$SHIM/dotnet"
  [ -n "${2:-}" ] && printf 'echo "%s"\n' "$2" >> "$SHIM/dotnet"
  printf 'exit %s\n' "$1" >> "$SHIM/dotnet"
  chmod +x "$SHIM/dotnet"
}
shim_test_fails() { # build ok, test fails
  cat > "$SHIM/dotnet" <<'SH'
#!/bin/sh
case "$1" in
  test) echo "  Failed Orders.Tests.TotalTest [12 ms]"; exit 1 ;;
  *) exit 0 ;;
esac
SH
  chmod +x "$SHIM/dotnet"
}

GATE="python3 $REPO/.claude/hooks/gate.py"
GUARD="python3 $REPO/.claude/hooks/bash_guard.py"
WATCH="python3 $REPO/.claude/bin/gate-watch.py"
SESSION='"session_id":"smoke-session-1"'
EDIT_PAYLOAD='{"cwd":"'"$REPO"'",'"$SESSION"',"tool_name":"Edit","tool_input":{"file_path":"src/Orders.cs","old_string":"abc","new_string":"abcdef"}}'
STOP_PAYLOAD='{"cwd":"'"$REPO"'",'"$SESSION"'}'
RELEASED_PAYLOAD='{"cwd":"'"$REPO"'",'"$SESSION"',"stop_hook_active":true}'
guard_payload() { printf '{"cwd":"%s",%s,"tool_input":{"command":"%s"}}' "$REPO" "$SESSION" "$1"; }

run_gate()  { set +e; OUT="$(echo "$1" | $GATE "${@:2}" 2>"$WORK/err")"; RC=$?; set -e; ERR="$(cat "$WORK/err")"; }
run_guard() { set +e; OUT="$(echo "$1" | $GUARD "${@:2}" 2>"$WORK/err")"; RC=$?; set -e; ERR="$(cat "$WORK/err")"; }

# ------------------------------------------------------------------ setup
say "setup in $WORK"
mkdir -p "$REPO/src" "$SHIM"
( cd "$REPO" && git init -q && touch App.sln && echo 'class A {}' > src/Orders.cs && echo '# readme' > README.md )
"$KIT/bootstrap.sh" "$REPO" > "$WORK/bootstrap1.txt"
export PATH="$SHIM:$PATH"
export CLAUDE_PROJECT_DIR="$REPO"
[ -f "$REPO/.claude/hooks/gate_log.py" ] || fail "bootstrap did not install gate_log.py"
[ -f "$REPO/.claude/bin/gate-watch.py" ] || fail "bootstrap did not install gate-watch.py"
ok "bootstrap installed the runtime files"

# ------------------------------------------------------------- 1. green edit
say "1. green edit"
shim 0
run_gate "$EDIT_PAYLOAD"
assert_eq "$RC" 0 "green edit exits 0"
assert_eq "$(log_count '"phase": "start", "kind": "edit"')" 1 "one edit start line"
assert_has "$(last_log)" '"verdict": "green"' "edit end is green"
assert_has "$(last_log)" '"ran": \["dotnet build App.sln' "ran lists the build, repo-relative"
RUN_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["run"])' "$(last_log)")"
assert_eq "$(log_count "$RUN_ID")" 2 "start and end share one run id"

# ------------------------------------------------------- 2. loop detector
say "2. three strikes then LOOP"
shim 1 "src/Orders.cs(12,5): error CS0103: The name 'total' does not exist in the current context [App.csproj]"
run_gate "$EDIT_PAYLOAD"; assert_eq "$RC" 2 "block 1 exits 2"; assert_has "$(last_log)" '"strike": 1' "strike 1"
run_gate "$EDIT_PAYLOAD"; assert_eq "$RC" 2 "block 2 exits 2"; assert_has "$(last_log)" '"strike": 2' "strike 2"
assert_has "$ERR" "EDIT GATE FAILED" "block text is the normal failure"
run_gate "$EDIT_PAYLOAD"; assert_eq "$RC" 2 "block 3 exits 2"
assert_has "$(last_log)" '"verdict": "loop"' "third is verdict loop"
assert_has "$ERR" "LOOP DETECTED" "third stderr says LOOP DETECTED"
assert_has "$ERR" "CS0103" "loop message names the key"
run_gate "$STOP_PAYLOAD" --stop; assert_eq "$RC" 2 "stop after loop exits 2"
assert_has "$ERR" "still failing on" "stop uses the short loop form"
assert_has "$(last_log)" '"strike": 4' "stop counted as strike 4"

# ------------------------------------------------------ 3. prompt resets
say "3. new prompt resets strikes, silently"
run_gate "{$SESSION}" --new-prompt
assert_eq "$RC" 0 "--new-prompt exits 0"
assert_eq "$OUT" "" "--new-prompt prints nothing on stdout"
assert_eq "$ERR" "" "--new-prompt prints nothing on stderr"
[ ! -e "$STATE" ] || fail "state file still present after --new-prompt"; ok "state file cleared"
assert_has "$(last_log)" '"kind": "prompt"' "prompt line logged"
assert_not "$(last_log)" "prompt_text" "prompt text never logged"
run_gate "$EDIT_PAYLOAD"; assert_has "$(last_log)" '"strike": 1' "strike restarts at 1 after prompt"

# ------------------------------------------------ 4. changed set = progress
say "4. a changed failure set is not a strike"
shim 1 "a.cs(1,1): error CS0001: one [p.csproj]
b.cs(1,1): error CS0002: two [p.csproj]"
run_gate "$EDIT_PAYLOAD"; assert_has "$(last_log)" '"strike": 1' "two errors: strike 1"
assert_has "$(last_log)" 'CS0002' "both keys recorded"
shim 1 "a.cs(1,1): error CS0001: one [p.csproj]"
run_gate "$EDIT_PAYLOAD"; assert_has "$(last_log)" '"strike": 1' "one error left: strike 1 again (progress)"

# ---------------------------------------------------------- 5. back to green
say "5. green clears the state"
shim 0
run_gate "$EDIT_PAYLOAD"; assert_eq "$RC" 0 "green again"
[ ! -e "$STATE" ] || fail "state file survives a green"; ok "state file gone on green"

# ------------------------------------------------------------- 6. stop gate
say "6. stop gate: green, block, released"
run_gate "$STOP_PAYLOAD" --stop; assert_eq "$RC" 0 "stop green exits 0"
assert_has "$(last_log)" '"kind": "stop"' "stop logged"
assert_has "$(last_log)" 'dotnet test App.sln' "ran has the test run"
shim_test_fails
run_gate "$STOP_PAYLOAD" --stop; assert_eq "$RC" 2 "stop with failing test exits 2"
assert_has "$(last_log)" '"keys": \["Orders.Tests.TotalTest"\]' "key is the failing test name"
STRIKE_BEFORE="$(cat "$STATE")"
run_gate "$RELEASED_PAYLOAD" --stop; assert_eq "$RC" 0 "released stop exits 0 even when red"
assert_has "$(last_log)" '"verdict": "released"' "released verdict logged"
assert_has "$(last_log)" '"result": "block"' "released result is block"
assert_eq "$(cat "$STATE")" "$STRIKE_BEFORE" "released stop leaves the strike state alone"
shim 0
run_gate "$RELEASED_PAYLOAD" --stop; assert_eq "$RC" 0 "released green exits 0"
assert_has "$(last_log)" '"result": "green"' "released result is green"

# ------------------------------------------------------------ 7. bash guard
say "7. bash guard"
run_guard "$(guard_payload 'sed -i s/a/b/ src/Orders.cs')"
assert_eq "$RC" 0 "warn mode exits 0 on green re-run"
assert_has "$OUT" '"systemMessage"' "systemMessage present"
assert_has "$OUT" 'gate re-run: green' "message says green"
assert_has "$OUT" '"additionalContext"' "additionalContext present"
assert_has "$(last_log)" '"kind": "bash"' "bash warn logged"
assert_has "$(last_log)" '"gate": "green"' "bash line carries the gate outcome"
assert_has "$(grep '"tool": "Bash"' "$LOG" | tail -n 1)" '"verdict": "green"' "edit pair with tool Bash logged"
shim 1 "a.cs(1,1): error CS0001: one [p.csproj]"
run_guard "$(guard_payload 'sed -i s/a/b/ src/Orders.cs')"
assert_eq "$RC" 2 "warn mode exits 2 when the re-run fails"
assert_has "$ERR" "\[bash guard\]" "stderr has the guard prefix"
assert_has "$ERR" "EDIT GATE FAILED" "stderr has the gate text"
shim 0
run_guard "$(guard_payload 'sed -i s/a/b/ /tmp/x.cs')"
assert_eq "$RC" 0 "tmp target: exit 0"; assert_eq "$OUT" "" "tmp target: no output"
BEFORE="$(wc -l < "$LOG")"
run_guard "$(guard_payload 'ls -la')"
assert_eq "$OUT$ERR" "" "ls: nothing printed"
assert_eq "$(wc -l < "$LOG")" "$BEFORE" "ls: nothing logged"
run_guard "$(guard_payload 'cat > src/Orders.cs <<EOF')" --block
assert_eq "$RC" 2 "--block exits 2"; assert_has "$ERR" "BASH GUARD\] Blocked" "--block message"
echo 'class B {}' > "$REPO/src/Billing.cs"
BEFORE="$(log_count '"tool": "Bash"')"
run_guard "$(guard_payload 'sed -i s/a/b/ src/Orders.cs src/Billing.cs')"
assert_eq "$(( $(log_count '"tool": "Bash"') - BEFORE ))" 2 "two .cs targets: exactly one edit pair (2 lines)"
BEFORE="$(log_count '"tool": "Bash"')"
run_guard "$(guard_payload 'cd src && sed -i s/a/b/ Orders.cs')"
assert_eq "$(( $(log_count '"tool": "Bash"') - BEFORE ))" 2 "cd + relative target still gets a build"
run_guard "$(guard_payload 'rm src/Billing.cs')"
assert_has "$(last_log)" '"reason": "delete source"' "rm is a delete source warn"
assert_not "$OUT" "Edit or Write tool" "rm context does not tell Claude to use Edit"
assert_has "$OUT" "removed src/Billing.cs" "rm context says removed"
run_guard "$(guard_payload 'rm src/Orders.cs')" --block
assert_eq "$RC" 0 "--block lets rm through (no file-tool equivalent)"
assert_eq "$OUT$ERR" "" "--block on rm prints nothing"
run_guard "$(guard_payload 'mv src/Orders.cs src/Old.cs')" --block
assert_eq "$RC" 0 "--block lets mv through"
run_guard "$(guard_payload 'rm src/Orders.cs; sed -i s/a/b/ src/Billing.cs')" --block
assert_eq "$RC" 2 "--block still refuses a sed in the same command"
assert_has "$(last_log)" '"reason": "in-place edit"' "block reason lists only the blockable shape"
# root from CLAUDE_PROJECT_DIR even when payload cwd is a subdirectory
SUBCWD='{"cwd":"'"$REPO/src"'",'"$SESSION"',"tool_name":"Edit","tool_input":{"file_path":"src/Orders.cs"}}'
run_gate "$SUBCWD"; assert_eq "$RC" 0 "gate with cwd=src/: exit 0"
assert_not "$OUT" "gate skipped" "gate with cwd=src/: no SKIP (root from CLAUDE_PROJECT_DIR)"
run_guard "$(printf '{"cwd":"%s",%s,"tool_input":{"command":"sed -i s/a/b/ src/Orders.cs"}}' "$REPO/src" "$SESSION")"
assert_has "$OUT" 'gate re-run: green' "guard with cwd=src/: resolves inside the repo"

# ---------------------------------------------------------- 8. non-code edit
say "8. non-code edit is silent and unlogged"
BEFORE="$(wc -l < "$LOG")"
run_gate '{"cwd":"'"$REPO"'","tool_name":"Edit","tool_input":{"file_path":"README.md"}}'
assert_eq "$RC" 0 "README edit exits 0"; assert_eq "$OUT$ERR" "" "README edit prints nothing"
assert_eq "$(wc -l < "$LOG")" "$BEFORE" "README edit not logged"

# ---------------------------------------------------------------- 9. watcher
say "9. watcher replay and stats"
( cd "$REPO" && timeout 2 $WATCH --from-start --no-tmux --no-color > "$WORK/watch.txt" || true )
W="$(cat "$WORK/watch.txt")"
for word in "green" "BLOCK" "LOOP" "WARN" "RELEASED still failing" "released green" "PROMPT" "strike 3"; do
  assert_has "$W" "$word" "watcher prints '$word'"
done
S="$(cd "$REPO" && $WATCH --stats)"
assert_has "$S" "Sessions            1" "stats: one session"
assert_has "$S" "Prompts             1" "stats: one prompt"
assert_has "$S" "Loops               2" "stats: two loops (edit + stop)"
assert_has "$S" "Released stops      2      (1 still failing)" "stats: released split"
S7="$(cd "$REPO" && $WATCH --stats --since 7d)"
assert_has "$S7" "since 7 days" "stats --since 7d parses"

# --------------------------------------------------- 10. bootstrap idempotent
say "10. bootstrap exclude entries and no pycache"
"$KIT/bootstrap.sh" "$REPO" > "$WORK/bootstrap2.txt"
EXCL="$(git -C "$REPO" rev-parse --git-path info/exclude)"
case "$EXCL" in /*) ;; *) EXCL="$REPO/$EXCL" ;; esac
for entry in .claude/gate-log.jsonl .claude/gate-state.json .claude/gate-watch.status-style; do
  assert_eq "$(grep -cxF "$entry" "$EXCL")" 1 "exclude has $entry exactly once"
done
assert_has "$(git -C "$REPO" status --porcelain)" "" "git status runs"
assert_not "$(git -C "$REPO" status --porcelain)" "gate-log.jsonl" "log is not untracked"
[ ! -d "$REPO/.claude/hooks/__pycache__" ] || fail "__pycache__ appeared in the target repo"; ok "no __pycache__"

# ------------------------------------------------------------- 11. tmux.conf
say "11. tmux binding"
assert_has "$(grep 'bind W' "$KIT/tmux.conf")" "python3 .claude/bin/gate-watch.py" "binding text has the space"
if command -v tmux >/dev/null; then
  TMPCONF="$WORK/tmux.conf"
  grep -v '^run ' "$KIT/tmux.conf" > "$TMPCONF"   # skip the tpm bootstrap line
  KEYS="$(tmux -f "$TMPCONF" -L smoke-$$ start-server \; list-keys 2>&1 || true)"
  tmux -L smoke-$$ kill-server 2>/dev/null || true
  assert_has "$KEYS" "gate-watch" "tmux loads the binding"
else
  echo "   skip tmux not installed"
fi

printf '\nALL OK  (%d assertions)  work dir: %s\n' "$PASS" "$WORK"
cleanup
