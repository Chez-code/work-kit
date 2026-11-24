#!/usr/bin/env bash
# Compatibility dry run: install rbox and run its offline tests inside clean
# Debian containers, to catch differences in bubblewrap, Python and gcc
# versions before a real machine does.
# Usage: tests/rust/docker.sh [image ...]      (default: debian:12 debian:13)
#
# This checks userland only: the container shares the host's kernel. It pulls
# the images, and runs them with relaxed confinement because bubblewrap needs
# to create namespaces inside the container. The Rust toolchain parts are
# taken from this machine's download cache (~/.cache/rustbox/dl), so run
# rustbox/install.sh here first.
set -euo pipefail

KIT="$(cd "$(dirname "$0")/../.." && pwd)"
DL="$HOME/.cache/rustbox/dl"
[ -d "$DL" ] || { echo "no download cache at $DL: run rustbox/install.sh first"; exit 1; }
IMAGES=("$@"); [ ${#IMAGES[@]} -gt 0 ] || IMAGES=(debian:12 debian:13)

INSIDE='
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq python3 bubblewrap gcc libc6-dev git xz-utils ca-certificates procps >/dev/null 2>&1
useradd -m dev
su dev -c "
  set -e
  export PATH=\$HOME/.local/bin:\$PATH
  mkdir -p ~/.cache/rustbox/dl && cp /dl-src/*.tar.xz ~/.cache/rustbox/dl/
  cp -r /kit ~/work-kit && cd ~/work-kit
  echo \"== \$(. /etc/os-release; echo \$PRETTY_NAME): python \$(python3 -V | cut -d\\  -f2), \$(bwrap --version), gcc \$(gcc -dumpversion)\"
  ./rustbox/install.sh | tail -12
  echo == selftest; rbox selftest | tail -2
  echo == unit tests; python3 -m unittest discover tests 2>&1 | tail -3
  echo == smoke; tests/smoke.sh | tail -1
  echo == mutation; python3 tests/rust/mutation.py
"
'
STATUS=0
for image in "${IMAGES[@]}"; do
  echo "############ $image"
  docker run --rm \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined \
    -v "$KIT:/kit:ro" -v "$DL:/dl-src:ro" "$image" bash -c "$INSIDE" || { echo "FAILED in $image"; STATUS=1; }
done
exit $STATUS
