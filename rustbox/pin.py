#!/usr/bin/env python3
"""Maintainer helper: write rustbox/toolchain.sums for a Rust release.

    python3 rustbox/pin.py 1.101.0          # prints what it would write
    python3 rustbox/pin.py 1.101.0 --write  # replaces rustbox/toolchain.sums

It downloads the release's channel manifest and its detached signature,
checks the signature with gpg against the Rust release key (the fingerprint
below is the only thing trusted from this file), and takes the download URL
and sha256 of each part from the manifest. install.sh then only ever checks
hashes, so machines that install rbox need neither gpg nor this script.

Run it on the maintainer's machine only: it needs Python 3.11+ (tomllib) and
gpg. Bumping the pin is a deliberate act: read the release notes first, run
the kit's tests against the new toolchain, then commit toolchain.sums.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

RELEASE_KEY = "108F66205EAEB0AAA8DD5E1C85AB96E6FA1BE5FE"  # Rust Language (Tag and Release Signing Key)
DIST = "https://static.rust-lang.org/dist/"
KEY_URL = "https://static.rust-lang.org/rust-key.gpg.ascii"
TRIPLES = ("aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu")
PARTS = ("rustc", "cargo", "rust-std", "clippy-preview", "rustfmt-preview")


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "rbox-pin"}), timeout=120) as r:
        return r.read()


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--write"]
    if len(args) != 1:
        print(__doc__)
        return 2
    version = args[0]
    try:
        import tomllib
    except ImportError:
        print("pin.py needs Python 3.11 or newer (tomllib)")
        return 2
    name = f"channel-rust-{version}.toml"
    manifest = fetch(DIST + name)
    signature = fetch(DIST + name + ".asc")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "manifest").write_bytes(manifest)
        (home / "manifest.asc").write_bytes(signature)
        (home / "key").write_bytes(fetch(KEY_URL))
        gpg = ["gpg", "--batch", "--quiet", "--homedir", str(home)]
        subprocess.run([*gpg, "--import", str(home / "key")], check=True, capture_output=True)
        check = subprocess.run([*gpg, "--status-fd", "1", "--verify", str(home / "manifest.asc"), str(home / "manifest")],
                               capture_output=True, text=True)
    valid = [line.split() for line in check.stdout.splitlines() if "VALIDSIG" in line]
    # VALIDSIG <signing key fpr> ... <primary key fpr>: the primary key must be the release key
    if check.returncode != 0 or not valid or valid[0][-1] != RELEASE_KEY:
        print("the manifest's signature did NOT verify against the Rust release key. Nothing written.")
        print(check.stdout + check.stderr)
        return 1
    data = tomllib.loads(manifest.decode())
    lines = [
        "# Rust toolchain pin for rbox. Written by rustbox/pin.py from the release manifest,",
        "# after checking the manifest's signature against the Rust release key",
        f"# (fingerprint {RELEASE_KEY}).",
        f"# manifest: {DIST}{name}",
        f"# manifest sha256: {hashlib.sha256(manifest).hexdigest()}",
        "# Lines: <machine triple> <sha256> <url>. Install verifies every hash.",
        f"version {version}",
    ]
    for triple in TRIPLES:
        for part in PARTS:
            target = data["pkg"][part]["target"][triple]
            if not target.get("available"):
                print(f"{part} is not available for {triple} in {version}. Nothing written.")
                return 1
            lines.append(f"{triple} {target['xz_hash']} {target['xz_url']}")
    text = "\n".join(lines) + "\n"
    if "--write" in sys.argv:
        (Path(__file__).resolve().parent / "toolchain.sums").write_text(text, encoding="utf-8")
        print(f"signature ok ({data['pkg']['rust']['version']}); wrote rustbox/toolchain.sums")
    else:
        print(text, end="")
        print(f"# signature ok ({data['pkg']['rust']['version']}); re-run with --write to replace rustbox/toolchain.sums")
    return 0


if __name__ == "__main__":
    sys.exit(main())
