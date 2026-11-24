#!/usr/bin/python3 -I
"""Run the kit's rbox in a separate test world.

    tests/rust/world.py BASE ARGS...     is     rbox ARGS...

with the crate sources, build output, approvals and network lists kept under
BASE instead of this machine's real ones. The toolchain is still the
installed one.

This exists for the kit's tests only. The installed rbox has no option and no
environment variable that does this: a world is chosen here, in code, so that
nothing a build or a session can set will point the real tool at other
approvals.
"""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "rustbox"))

import rbox  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write(__doc__)
        sys.exit(2)
    sys.exit(rbox.cli(["rbox", *sys.argv[2:]], rbox.Dirs(test_base=Path(sys.argv[1]))))
