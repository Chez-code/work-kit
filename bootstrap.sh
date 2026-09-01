#!/usr/bin/env bash
# Retrofit the Claude Code verification workflow into an existing repo.
# Usage: ./bootstrap.sh /path/to/repo
# Idempotent: never overwrites existing files; prints what it skipped.
set -euo pipefail

TARGET="${1:?usage: ./bootstrap.sh /path/to/repo}"
KIT="$(cd "$(dirname "$0")/kit" && pwd)"

[ -d "$TARGET" ] || { echo "not a directory: $TARGET"; exit 1; }

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
copy .pre-commit-config.yaml
copy .github/workflows/ci.yml

if [ ! -e "$TARGET/CLAUDE.md" ]; then
  cp "$KIT/CLAUDE.md.template" "$TARGET/CLAUDE.md"
  echo "installed:     CLAUDE.md (from template — fill in the placeholders)"
else
  echo "skip (exists): CLAUDE.md"
fi

echo ""
echo "Next steps in $TARGET:"
echo "  1. Edit CLAUDE.md — fill placeholders, write your [LOCKED] specs"
echo "  2. pre-commit install && pre-commit autoupdate"
echo "  3. Launch 'claude' and confirm hooks fire (Ctrl+O for verbose)"
