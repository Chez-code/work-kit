#!/usr/bin/env bash
# Retrofit the Claude Code verification workflow into an existing repo.
# Usage: ./bootstrap.sh /path/to/repo
# Idempotent: never overwrites existing files; prints what it skipped.
set -euo pipefail

TARGET="${1:?usage: ./bootstrap.sh /path/to/repo}"
KIT="$(cd "$(dirname "$0")/kit" && pwd)"

[ -d "$TARGET" ] || { echo "not a directory: $TARGET"; exit 1; }

# .NET repo? (solution/project file at the root or one level down — same rule
# as gate.py's detector). The pre-commit and CI templates are Python-only and
# would land as live, failing config in a .NET repo, so they get skipped.
is_dotnet() {
  compgen -G "$TARGET/*.sln"      >/dev/null 2>&1 || \
  compgen -G "$TARGET/*.slnx"     >/dev/null 2>&1 || \
  compgen -G "$TARGET/*/*.sln"    >/dev/null 2>&1 || \
  compgen -G "$TARGET/*/*.slnx"   >/dev/null 2>&1 || \
  compgen -G "$TARGET/*.csproj"   >/dev/null 2>&1 || \
  compgen -G "$TARGET/*/*.csproj" >/dev/null 2>&1
}
DOTNET=0
is_dotnet && DOTNET=1

copy() { # copy <relpath>
  src="$KIT/$1"; dst="$TARGET/$1"
  if [ -e "$dst" ]; then
    echo "skip (exists): $1"
  else
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    echo "installed:     $1"
  fi
}

copy .claude/settings.json
copy .claude/hooks/gate.py
copy .claude/hooks/gate_log.py
copy .claude/hooks/bash_guard.py
copy .claude/bin/gate-watch.py
if [ "$DOTNET" = 1 ]; then
  echo "skip (Python-only, .NET repo): .pre-commit-config.yaml"
  echo "skip (Python-only, .NET repo): .github/workflows/ci.yml"
else
  copy .pre-commit-config.yaml
  copy .github/workflows/ci.yml
fi

# Runtime files the hooks write (event log, loop state, saved tmux style) must
# not show up as untracked. `git rev-parse --git-path` finds the right exclude
# file in worktrees and submodules too. Idempotent: each line is added once.
if EXCLUDE="$(git -C "$TARGET" rev-parse --git-path info/exclude 2>/dev/null)"; then
  case "$EXCLUDE" in /*) ;; *) EXCLUDE="$TARGET/$EXCLUDE" ;; esac
  mkdir -p "$(dirname "$EXCLUDE")"
  touch "$EXCLUDE"
  for entry in .claude/gate-log.jsonl .claude/gate-state.json .claude/gate-watch.status-style; do
    if grep -qxF "$entry" "$EXCLUDE"; then
      echo "excluded already: $entry"
    else
      echo "$entry" >> "$EXCLUDE"
      echo "excluded:      $entry  (in $(basename "$(dirname "$EXCLUDE")")/exclude)"
    fi
  done
else
  echo "note: not a git repo — add .claude/gate-log.jsonl, .claude/gate-state.json"
  echo "      and .claude/gate-watch.status-style to your ignore rules by hand"
fi

if [ ! -e "$TARGET/CLAUDE.md" ]; then
  cp "$KIT/CLAUDE.md.template" "$TARGET/CLAUDE.md"
  echo "installed:     CLAUDE.md (from template — fill in the placeholders)"
else
  echo "skip (exists): CLAUDE.md"
fi

echo ""
echo "Next steps in $TARGET:"
echo "  1. Edit CLAUDE.md — fill placeholders, write your [LOCKED] specs"
if [ "$DOTNET" = 1 ]; then
  echo "  2. Launch 'claude' inside tmux; prefix + W opens the gate watcher pane"
else
  echo "  2. pre-commit install && pre-commit autoupdate"
  echo "  3. Launch 'claude' inside tmux; prefix + W opens the gate watcher pane"
fi
