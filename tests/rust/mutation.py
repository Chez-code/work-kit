#!/usr/bin/env python3
"""Mutation check for `rbox selftest`: weaken the box on purpose, one way at a
time, and confirm the self-test notices each time. A self-test that still
passes with a weakened box would prove nothing.

Needs the installed toolchain (run rustbox/install.sh first). Run from the kit
checkout:  python3 tests/rust/mutation.py
"""

import contextlib
import io
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "rustbox"))
import rbox  # noqa: E402

REAL_BUILD_BOX = rbox.build_box
DIRS = rbox.Dirs(dict(os.environ))


class Everything(set):
    """A network list that claims to contain every host."""

    def __contains__(self, item) -> bool:
        return True

    def __bool__(self) -> bool:
        return True

    def __iter__(self):
        return iter(["(everything)"])


def selftest_with_open_proxy() -> tuple:
    real = rbox.load_net_allow
    rbox.load_net_allow = lambda project: Everything()
    try:
        return selftest_with(lambda box, project: None)
    finally:
        rbox.load_net_allow = real


def selftest_with(mutate) -> tuple:
    """(exit code, names of failed probes) with the build box changed by `mutate`."""

    def weakened(dirs, bwrap, toolchain, project, cwd, extra_env=None):
        box = REAL_BUILD_BOX(dirs, bwrap, toolchain, project, cwd, extra_env)
        mutate(box, project)
        return box

    rbox.build_box = weakened
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = rbox.cmd_selftest(DIRS, [])
    except rbox.Stop as stop:
        return stop.code, ["(refused before running: " + stop.message + ")"]
    finally:
        rbox.build_box = REAL_BUILD_BOX
    failed = [line.split(None, 1)[1] for line in out.getvalue().splitlines() if line.strip().startswith("FAILED")]
    return code, failed


def leak_environment(box, project):
    # what inheriting the caller's environment would look like
    box.env["RBOX_SELFTEST_CANARY"] = os.environ["RBOX_SELFTEST_CANARY"]
    box.env["CLAUDECODE"] = "1"


def allow_nested(box, project):
    box.opts = [o for o in box.opts if o != "--disable-userns"]


CASES = [
    ("inherit the caller's environment", leak_environment, "read the canary variable (/proc/1/environ)"),
    ("share the machine's network", lambda b, p: b.add("--share-net"), "connect to a service on this machine (TCP)"),
    ("bind the home folder", lambda b, p: b.add("--bind", str(DIRS.home), str(DIRS.home)), "read a file in your home folder"),
    ("bind /run", lambda b, p: b.add("--ro-bind", "/run", "/run"), "connect to socket"),
    ("make the project writable", lambda b, p: b.add("--bind", str(p.root), str(p.root)), "write into src/"),
    ("allow nested sandboxes", allow_nested, "create a nested sandbox"),
]


def main() -> int:
    try:
        DIRS.toolchain()
    except rbox.Stop as stop:
        print("skip: " + stop.message)
        return 0
    code, failed = selftest_with(lambda box, project: None)
    print(f"unchanged box: exit {code}, {len(failed)} probes failed")
    bad = code != 0
    for label, mutate, must_catch in CASES:
        code, failed = selftest_with(mutate)
        caught = code != 0 and any(must_catch in name for name in failed)
        print(f"{'caught  ' if caught else 'MISSED  '} {label}: exit {code}, {len(failed)} probes failed")
        bad = bad or not caught
    code, failed = selftest_with_open_proxy()
    caught = code != 0 and any("unlisted service on this machine through the proxy" in name for name in failed)
    print(f"{'caught  ' if caught else 'MISSED  '} a proxy that lets every host through: exit {code}, {len(failed)} probes failed")
    bad = bad or not caught
    print("mutation check: FAILED" if bad else "mutation check: every weakening was caught")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
