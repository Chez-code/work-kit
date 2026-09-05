#!/usr/bin/env bash
# Update kit-managed files in a repo that was bootstrapped earlier.
# Usage: ./sync.sh /path/to/repo
# Safe by construction: a changed file is replaced and the old copy is kept
# as <file>.prev (the file may be git-excluded, so git cannot recover it —
# the .prev is the only safety net). Never deletes, idempotent.
set -euo pipefail

TARGET="${1:?usage: ./sync.sh /path/to/repo}"
KIT="$(cd "$(dirname "$0")/kit" && pwd)"

[ -d "$TARGET" ] || { echo "not a directory: $TARGET"; exit 1; }

sync_file() { # sync_file <relpath>
  src="$KIT/$1"; dst="$TARGET/$1"
  if [ ! -e "$dst" ]; then
    echo "absent, skipped: $1"
    return
  fi
  if cmp -s "$src" "$dst"; then
    echo "up to date:      $1"
  else
    cp "$dst" "$dst.prev"
    cp "$src" "$dst"
    echo "updated:         $1  (old copy kept as $1.prev)"
    echo "                 review with: diff \"$dst.prev\" \"$dst\""
    echo "                 then delete the .prev — it's yours to remove"
  fi
}

sync_file .claude/hooks/gate.py
sync_file .claude/settings.json

if [ -e "$TARGET/.claude/settings.local.json" ]; then
  echo ""
  echo "note: .claude/settings.local.json is never touched by sync. If the kit's"
  echo "      settings changed (see any settings.json.prev diff above), apply the"
  echo "      same change there by hand."
fi
