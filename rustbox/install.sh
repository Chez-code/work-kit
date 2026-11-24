#!/usr/bin/env bash
# Install rbox on this machine: the tool, the pinned Rust toolchain, and the
# `rbox` and `cargo` links in ~/.local/bin.
# Usage: rustbox/install.sh          (run it again after `git pull` to update)
#
# Touches only your home folder and never uses sudo:
#   ~/.local/share/rustbox/   the tool, the toolchain, the self-test crate
#   ~/.cache/rustbox/         downloads, crate sources, build output
#   ~/.local/state/rustbox/   the crates you approved
#   ~/.local/bin/rbox, ~/.local/bin/cargo   links to the tool
# To remove rbox, delete those.
set -euo pipefail

KIT="$(cd "$(dirname "$0")" && pwd)"
SHARE="$HOME/.local/share/rustbox"
BIN="$HOME/.local/bin"

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "rbox needs /usr/bin/python3, version 3.9 or newer."
  echo "A person with sudo runs:  sudo apt install python3"
  exit 4
fi
if [ ! -x /usr/bin/python3 ]; then
  echo "rbox starts with /usr/bin/python3, which is missing here (python3 is at $(command -v python3))."
  exit 4
fi

mkdir -p "$SHARE/bin" "$BIN"

if [ -f "$SHARE/bin/rbox" ] && ! cmp -s "$KIT/rbox.py" "$SHARE/bin/rbox"; then
  echo "updated:   $SHARE/bin/rbox"
elif [ ! -f "$SHARE/bin/rbox" ]; then
  echo "installed: $SHARE/bin/rbox"
else
  echo "unchanged: $SHARE/bin/rbox"
fi
install -m 755 "$KIT/rbox.py" "$SHARE/bin/rbox"
install -m 644 "$KIT/toolchain.sums" "$SHARE/toolchain.sums"
if [ -d "$KIT/selftest" ]; then
  rm -rf "$SHARE/selftest"
  cp -r "$KIT/selftest" "$SHARE/selftest"
fi

for name in rbox cargo; do
  link="$BIN/$name"
  if [ -L "$link" ]; then
    if [ "$(readlink -f "$link")" != "$(readlink -f "$SHARE/bin/rbox")" ]; then
      echo "REFUSED: $link is a link to something else ($(readlink "$link")). Remove it and run this again."
      exit 3
    fi
  elif [ -e "$link" ]; then
    echo "REFUSED: $link already exists and is not rbox's. Move it away and run this again."
    exit 3
  fi
  ln -sfn "$SHARE/bin/rbox" "$link"
done
echo "linked:    $BIN/rbox and $BIN/cargo"

"$SHARE/bin/rbox" toolchain install "$SHARE/toolchain.sums"

echo ""
echo "Checking this machine:"
"$SHARE/bin/rbox" doctor
