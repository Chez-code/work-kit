"""Unit tests for rustbox/rbox.py. Stdlib only, no Rust toolchain and no network needed.

Run from the kit checkout:  python3 -m unittest discover tests
These cover the decisions rbox makes before anything runs: which box a command
gets, what the box may see, and when a build must wait for a person. The
end-to-end behaviour (real cargo, real crates) is in tests/rust/.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rustbox"))

import rbox  # noqa: E402

# Options that exist in bubblewrap 0.8.0 (Debian 12). rbox must not emit anything newer.
BWRAP_0_8 = {
    "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session",
    "--share-net", "--tmpfs", "--proc", "--dev", "--dir", "--symlink", "--chdir",
    "--bind", "--ro-bind", "--ro-bind-try", "--ro-bind-data",
}


class World:
    """A throwaway home, project and rbox dirs for one test."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.dirs = rbox.Dirs({"RBOX_TEST_BASE": str(base / "world")})
        self.dirs.home = base / "home"
        self.dirs.share = self.dirs.home / ".local" / "share" / "rustbox"
        self.toolchain = self.dirs.share / "toolchains" / "1.100.0"
        (self.toolchain / "bin").mkdir(parents=True)
        self.root = base / "home" / "projects" / "app"
        (self.root / "src").mkdir(parents=True)
        (self.root / "Cargo.toml").write_text("[package]\nname = 'app'\n")
        self.project = rbox.Project(self.dirs, self.root)
        self.boxes: list = []

    def close(self) -> None:
        for box in self.boxes:
            for fd in box.fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.tmp.cleanup()

    def build(self, cwd: Path | None = None, extra_env: dict | None = None) -> rbox.Box:
        box = rbox.build_box(self.dirs, "/usr/bin/bwrap", self.toolchain, self.project, cwd or self.root, extra_env)
        self.boxes.append(box)
        return box

    def fetch(self, network: bool, cwd: Path | None = None) -> rbox.Box:
        box = rbox.fetch_box(self.dirs, "/usr/bin/bwrap", self.toolchain, self.root, cwd or self.root, network)
        self.boxes.append(box)
        return box


def pairs(opts: list, flag: str) -> list:
    """The arguments that follow each occurrence of a two-argument bwrap option."""
    return [(opts[i + 1], opts[i + 2]) for i, o in enumerate(opts) if o == flag and i + 2 < len(opts)]


def singles(opts: list, flag: str) -> list:
    return [opts[i + 1] for i, o in enumerate(opts) if o == flag and i + 1 < len(opts)]


class Humans(unittest.TestCase):
    def test_person_at_a_terminal(self):
        self.assertEqual(rbox.human_problems({}, True, True, True, []), [])

    def test_no_terminal(self):
        self.assertIn("no real terminal", rbox.human_problems({}, False, True, True, []))
        self.assertIn("no real terminal", rbox.human_problems({}, True, True, False, []))

    def test_claude_marker_in_own_environment(self):
        for marker in rbox.CLAUDE_MARKERS:
            problems = rbox.human_problems({marker: "1"}, True, True, True, [])
            self.assertEqual(len(problems), 1, marker)
            self.assertIn("Claude session", problems[0])

    def test_claude_marker_in_a_parent_even_with_a_terminal(self):
        # what `env -u CLAUDECODE script -c "rbox allow"` looks like
        problems = rbox.human_problems({}, True, True, True, ["CLAUDECODE"])
        self.assertEqual(len(problems), 1)
        self.assertIn("parent process", problems[0])

    def test_this_test_process_is_not_treated_as_human_under_claude(self):
        if not any(os.environ.get(m) for m in rbox.CLAUDE_MARKERS):
            self.skipTest("not running under a Claude session")
        with self.assertRaises(rbox.Stop) as caught:
            rbox.require_human("allow")
        self.assertEqual(caught.exception.code, rbox.EXIT_REFUSED)


class CargoArguments(unittest.TestCase):
    def test_subcommand_after_leading_flags(self):
        self.assertEqual(rbox.split_cargo(["-q", "--color", "never", "check", "--workspace"]),
                         (["-q", "--color", "never"], "check", ["--workspace"]))

    def test_no_subcommand(self):
        self.assertEqual(rbox.split_cargo([]), ([], None, []))
        self.assertEqual(rbox.split_cargo(["--version"])[1], "--version")

    def test_toolchain_override_refused(self):
        with self.assertRaises(rbox.Stop):
            rbox.split_cargo(["+nightly", "build"])

    def test_config_before_the_subcommand_refused(self):
        with self.assertRaises(rbox.Stop):
            rbox.split_cargo(["--config", "x=1", "build"])

    def test_rule_changing_flags_refused(self):
        for rest in (["--config", "a=1"], ["--config=a=1"], ["-Zunstable-options"], ["-Z", "x"],
                     ["--target-dir", "/tmp/x"], ["--target-dir=/tmp/x"]):
            with self.assertRaises(rbox.Stop, msg=str(rest)):
                rbox.check_flags("build", [], rest)

    def test_flags_after_dashdash_belong_to_the_program(self):
        rbox.check_flags("run", [], ["--", "--config", "mine.toml", "-Z"])
        rbox.check_flags("rustc", [], ["--", "-C", "opt-level=3"])

    def test_specific_refusals(self):
        with self.assertRaises(rbox.Stop):
            rbox.check_flags("clippy", [], ["--fix"])
        with self.assertRaises(rbox.Stop):
            rbox.check_flags("doc", [], ["--open"])
        with self.assertRaises(rbox.Stop):
            rbox.check_flags("add", [], ["x", "--git", "https://example.invalid/x"])
        rbox.check_flags("clippy", [], ["--", "-D", "warnings"])

    def test_lock_flags_are_stripped_only_before_dashdash(self):
        self.assertEqual(rbox.strip_lock_flags(["--locked", "-p", "x", "--offline", "--", "--locked"]),
                         ["-p", "x", "--", "--locked"])

    def test_every_command_has_exactly_one_home(self):
        groups = [rbox.BUILD_CMDS, rbox.FETCH_NET_CMDS, rbox.SYNC_CMDS, rbox.FETCH_LOCAL_CMDS, set(rbox.REFUSED_CMDS)]
        for i, a in enumerate(groups):
            for b in groups[i + 1:]:
                self.assertEqual(a & b, set())
        for name in ("metadata", "fetch", "generate-lockfile", "tree", "locate-project", "fmt", "vendor"):
            self.assertTrue(any(name in g for g in groups), name)
        self.assertTrue(rbox.NET_OK_CMDS <= rbox.BUILD_CMDS)
        for name in ("build", "check", "clippy", "doc"):
            self.assertNotIn(name, rbox.NET_OK_CMDS)

    def test_manifest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "a").mkdir()
            (root / "a" / "Cargo.toml").write_text("")
            self.assertEqual(rbox.manifest_arg(["--manifest-path", "a/Cargo.toml"], root), root / "a" / "Cargo.toml")
            self.assertEqual(rbox.manifest_arg(["--manifest-path=a/Cargo.toml"], root), root / "a" / "Cargo.toml")
            self.assertIsNone(rbox.manifest_arg(["--", "--manifest-path", "a/Cargo.toml"], root))
            with self.assertRaises(rbox.Stop):
                rbox.manifest_arg(["--manifest-path", "a/missing.toml"], root)


class Boxes(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)

    def test_only_old_bubblewrap_options(self):
        for box in (self.w.build(), self.w.fetch(True), self.w.fetch(False)):
            used = {o for o in box.opts if o.startswith("--")}
            self.assertEqual(used - BWRAP_0_8, set())

    def test_every_box_is_isolated(self):
        for box in (self.w.build(), self.w.fetch(True), self.w.fetch(False)):
            for flag in ("--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session"):
                self.assertIn(flag, box.opts)

    def test_build_box_has_no_network_and_a_read_only_project(self):
        box = self.w.build()
        self.assertNotIn("--share-net", box.opts)
        self.assertIn((str(self.w.root), str(self.w.root)), pairs(box.opts, "--ro-bind"))
        self.assertNotIn((str(self.w.root), str(self.w.root)), pairs(box.opts, "--bind"))
        self.assertIn((str(self.w.project.vendor), "/rbox/vendor"), pairs(box.opts, "--ro-bind"))
        self.assertIn((str(self.w.project.target), "/rbox/target"), pairs(box.opts, "--bind"))
        self.assertEqual(box.env["CARGO_NET_OFFLINE"], "true")
        self.assertEqual(box.env["CARGO_TARGET_DIR"], "/rbox/target")

    def test_nothing_of_rbox_is_created_in_the_project(self):
        before = sorted(p.name for p in self.w.root.iterdir())
        self.w.build()
        self.w.fetch(True)
        self.assertEqual(sorted(p.name for p in self.w.root.iterdir()), before)
        self.assertNotIn(self.w.root, self.w.project.vendor.parents)
        self.assertNotIn(self.w.root, self.w.project.target.parents)

    def test_environment_is_built_not_inherited(self):
        os.environ["RBOX_UNITTEST_SECRET"] = "hunter2"
        self.addCleanup(os.environ.pop, "RBOX_UNITTEST_SECRET", None)
        for box in (self.w.build(), self.w.fetch(True)):
            self.assertNotIn("RBOX_UNITTEST_SECRET", box.env)
            self.assertNotIn("hunter2", " ".join(box.env.values()))
            self.assertNotIn("hunter2", " ".join(box.opts))
            for marker in rbox.CLAUDE_MARKERS:
                self.assertNotIn(marker, box.env)
            self.assertEqual(box.env["HOME"], "/rbox/home")

    def test_named_variables_do_pass(self):
        box = self.w.build(extra_env={"API_KEY": "abc"})
        self.assertEqual(box.env["API_KEY"], "abc")

    def test_fetch_box_runs_no_host_programs(self):
        box = self.w.fetch(True)
        self.assertNotIn(("/usr", "/usr"), pairs(box.opts, "--ro-bind"))
        self.assertNotIn(("/usr/bin", "/usr/bin"), pairs(box.opts, "--ro-bind"))
        self.assertEqual(box.env["PATH"], "/rbox/toolchain/bin")
        self.assertEqual(box.env["RUSTC_WRAPPER"], "")
        self.assertEqual(box.env["RUSTC_WORKSPACE_WRAPPER"], "")

    def test_fetch_box_network_only_when_asked(self):
        self.assertIn("--share-net", self.w.fetch(True).opts)
        offline = self.w.fetch(False)
        self.assertNotIn("--share-net", offline.opts)
        self.assertEqual(offline.env["CARGO_NET_OFFLINE"], "true")

    def test_fetch_box_pins_the_cooldown(self):
        box = self.w.fetch(True)
        self.assertEqual(box.env["CARGO_RESOLVER_INCOMPATIBLE_PUBLISH_AGE"], "deny")
        self.assertEqual(box.env["CARGO_REGISTRY_GLOBAL_MIN_PUBLISH_AGE"], "7 days")
        pins = rbox.cooldown_pins()
        self.assertIn('registry.global-min-publish-age="7 days"', pins)
        self.assertIn('registries.crates-io.min-publish-age="7 days"', pins)
        self.assertIn('resolver.incompatible-publish-age="deny"', pins)

    def test_fetch_box_hides_cargo_config_at_every_level(self):
        member = self.w.root / "crates" / "m"
        (member / ".cargo").mkdir(parents=True)
        (member / ".cargo" / "config.toml").write_text("")
        (self.w.root / ".cargo").mkdir()
        (self.w.root / ".cargo" / "config").write_text("")
        box = self.w.fetch(True, cwd=member)
        hidden = singles(box.opts, "--tmpfs")
        self.assertIn(str(self.w.root / ".cargo"), hidden)
        self.assertIn(str(member / ".cargo"), hidden)

    def test_build_box_keeps_project_config_but_pins_the_source(self):
        (self.w.root / ".cargo").mkdir()
        box = self.w.build()
        self.assertNotIn(str(self.w.root / ".cargo"), singles(box.opts, "--tmpfs"))
        self.assertIn('source.crates-io.replace-with="rbox-vendor"', rbox.SOURCE_PINS)
        self.assertIn('source.rbox-vendor.directory="/rbox/vendor"', rbox.SOURCE_PINS)

    def test_cargo_config_symlink_refused(self):
        (self.w.root / "elsewhere").mkdir()
        os.symlink(self.w.root / "elsewhere", self.w.root / ".cargo")
        with self.assertRaises(rbox.Stop):
            self.w.fetch(True)

    def test_secrets_are_hidden_in_both_boxes(self):
        (self.w.root / ".git" / "hooks").mkdir(parents=True)
        (self.w.root / ".claude").mkdir()
        (self.w.root / ".env").write_text("K=1")
        (self.w.root / ".mcp.json").write_text("{}")
        (self.w.root / "deep" / "er").mkdir(parents=True)
        (self.w.root / "deep" / "er" / ".env.production").write_text("K=2")
        (self.w.root / "target" / "x").mkdir(parents=True)
        (self.w.root / "target" / "x" / ".env").write_text("not walked")
        for box in (self.w.build(), self.w.fetch(True)):
            tmpfs = singles(box.opts, "--tmpfs")
            null = [dest for src, dest in pairs(box.opts, "--ro-bind") if src == "/dev/null"]
            self.assertIn(str(self.w.root / ".git"), tmpfs)
            self.assertIn(str(self.w.root / ".claude"), tmpfs)
            self.assertIn(str(self.w.root / ".env"), null)
            self.assertIn(str(self.w.root / ".mcp.json"), null)
            self.assertIn(str(self.w.root / "deep" / "er" / ".env.production"), null)
            self.assertNotIn(str(self.w.root / "target" / "x" / ".env"), null)

    def test_git_worktree_pointer_file_is_left_alone(self):
        (self.w.root / ".git").write_text("gitdir: /elsewhere\n")
        box = self.w.build()
        self.assertNotIn(str(self.w.root / ".git"), singles(box.opts, "--tmpfs"))

    def test_options_travel_by_file_descriptor_not_on_the_command_line(self):
        box = self.w.build(extra_env={"API_KEY": "abc"})
        argv = box.argv(["/rbox/toolchain/bin/cargo", "check"])
        self.assertEqual(argv[1], "--args")
        self.assertEqual(argv[3:], ["--", "/rbox/toolchain/bin/cargo", "check"])
        self.assertNotIn("abc", " ".join(argv))


class Roots(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)

    def test_home_and_above_are_not_projects(self):
        for bad in (self.w.dirs.home, self.w.dirs.home.parent, Path("/")):
            with self.assertRaises(rbox.Stop, msg=str(bad)):
                rbox.check_root(self.w.dirs, bad)

    def test_rbox_folders_are_not_projects(self):
        for own in (self.w.dirs.share, self.w.dirs.cache / "abc" / "vendor", self.w.dirs.state):
            with self.assertRaises(rbox.Stop, msg=str(own)):
                rbox.check_root(self.w.dirs, own)

    def test_a_normal_project_is_fine(self):
        rbox.check_root(self.w.dirs, self.w.root)

    def test_scope_is_the_topmost_safe_manifest(self):
        member = self.w.root / "crates" / "m"
        member.mkdir(parents=True)
        (member / "Cargo.toml").write_text("")
        self.assertEqual(rbox.discovery_scope(self.w.dirs, member), self.w.root)

    def test_a_stray_manifest_in_home_does_not_widen_the_scope(self):
        (self.w.dirs.home / "Cargo.toml").write_text("")
        self.assertEqual(rbox.discovery_scope(self.w.dirs, self.w.root), self.w.root)

    def test_no_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rbox.Stop):
                rbox.discovery_scope(self.w.dirs, Path(tmp).resolve())

    def test_two_clones_get_separate_cache(self):
        other = rbox.Project(self.w.dirs, self.w.root.parent / "app-clone")
        self.assertNotEqual(other.key, self.w.project.key)
        self.assertNotEqual(other.target, self.w.project.target)


def write_meta(world: World, rows: list) -> None:
    world.project.lock.write_text("# lock\n")
    world.project.state.mkdir(parents=True, exist_ok=True)
    (world.project.state / "meta.json").write_text(
        json.dumps({"lock": rbox.sha256_file(world.project.lock), "rows": rows}))


def crate(name: str, version: str, checksum: str = "", **more) -> dict:
    row = {"name": name, "version": version, "source": rbox.CRATES_IO, "checksum": checksum or (name[0] * 64),
           "build_script": False, "proc_macro": False, "native": False, "via": ""}
    row.update(more)
    return row


class Approvals(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)

    def stop_code(self) -> int:
        with self.assertRaises(rbox.Stop) as caught:
            rbox.require_approved(self.w.dirs, self.w.project)
        return caught.exception.code

    def test_no_lockfile(self):
        self.assertEqual(self.stop_code(), rbox.EXIT_REFUSED)

    def test_lockfile_changed_since_download(self):
        write_meta(self.w, [])
        self.w.project.lock.write_text("# lock, edited\n")
        self.assertEqual(self.stop_code(), rbox.EXIT_REFUSED)

    def test_new_crate_waits_for_a_person(self):
        write_meta(self.w, [crate("serde", "1.0.229")])
        with self.assertRaises(rbox.Stop) as caught:
            rbox.require_approved(self.w.dirs, self.w.project)
        self.assertEqual(caught.exception.code, rbox.EXIT_WAITING)
        self.assertIn("serde 1.0.229", caught.exception.message)

    def test_confirmed_crate_builds(self):
        write_meta(self.w, [crate("serde", "1.0.229")])
        rbox.add_confirmed(self.w.dirs, [("serde", "1.0.229", "s" * 64)])
        rbox.require_approved(self.w.dirs, self.w.project)

    def test_a_version_change_asks_again(self):
        rbox.add_confirmed(self.w.dirs, [("serde", "1.0.229", "s" * 64)])
        write_meta(self.w, [crate("serde", "1.0.230", "t" * 64)])
        self.assertEqual(self.stop_code(), rbox.EXIT_WAITING)

    def test_same_version_different_file_asks_again(self):
        rbox.add_confirmed(self.w.dirs, [("serde", "1.0.229", "s" * 64)])
        write_meta(self.w, [crate("serde", "1.0.229", "f" * 64)])
        self.assertEqual(self.stop_code(), rbox.EXIT_WAITING)

    def test_approval_is_machine_wide(self):
        rbox.add_confirmed(self.w.dirs, [("serde", "1.0.229", "s" * 64)])
        other = rbox.Project(self.w.dirs, self.w.root.parent / "second")
        other.root.mkdir()
        other.lock.write_text("# other\n")
        other.state.mkdir(parents=True)
        (other.state / "meta.json").write_text(
            json.dumps({"lock": rbox.sha256_file(other.lock), "rows": [crate("serde", "1.0.229")]}))
        rbox.require_approved(self.w.dirs, other)

    def test_nothing_in_the_repo_can_approve(self):
        # the confirmed set lives outside any project
        self.assertNotIn(self.w.root, self.w.dirs.confirmed.parents)
        write_meta(self.w, [crate("serde", "1.0.229")])
        (self.w.root / "deps.allow").write_text("serde 1.0.229 " + "s" * 64 + "\n")
        (self.w.root / "confirmed").write_text("serde 1.0.229 " + "s" * 64 + "\n")
        self.assertEqual(self.stop_code(), rbox.EXIT_WAITING)

    def test_git_or_other_registry_is_refused_not_pending(self):
        row = crate("evil", "0.1.0")
        row["source"] = "git+https://example.invalid/evil?rev=abc"
        write_meta(self.w, [row])
        self.assertEqual(self.stop_code(), rbox.EXIT_REFUSED)

    def test_missing_download_is_refused(self):
        row = crate("serde", "1.0.229")
        row["checksum"] = ""
        write_meta(self.w, [row])
        self.assertEqual(self.stop_code(), rbox.EXIT_REFUSED)

    def test_local_stand_in_waits_until_confirmed_for_this_project(self):
        row = {"name": "serde", "version": "1.0.229", "source": None, "path": "third_party/serde", "patched": True}
        write_meta(self.w, [row])
        self.assertEqual(self.stop_code(), rbox.EXIT_WAITING)
        rbox.add_pairs(self.w.project.state / "local-ok", [("serde", "third_party/serde")], "# h\n")
        rbox.require_approved(self.w.dirs, self.w.project)

    def describe(self, rows: list, index: dict) -> tuple:
        write_meta(self.w, rows)
        (self.w.project.state / "index.json").write_text(json.dumps(index))
        original = rbox.lookup_index
        rbox.lookup_index = lambda pairs_: {f"{n} {v}": {"unknown": "offline in test"} for n, v in pairs_}
        self.addCleanup(setattr, rbox, "lookup_index", original)
        state = rbox.approval_state(self.w.dirs, self.w.project)
        return rbox.describe_pending(self.w.dirs, self.w.project, state)

    @staticmethod
    def published(days: float) -> str:
        import datetime
        when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_too_new_is_refused(self):
        ok, refused = self.describe([crate("fresh", "2.0.0")], {"fresh 2.0.0": {"pubtime": self.published(2)}})
        self.assertEqual(ok, [])
        self.assertIn("too new", refused[0]["why"])
        self.assertIn("rbox urgent fresh@2.0.0", refused[0]["why"])

    def test_old_enough_is_approvable(self):
        ok, refused = self.describe([crate("calm", "1.0.0")], {"calm 1.0.0": {"pubtime": self.published(8)}})
        self.assertEqual((len(ok), refused), (1, []))
        self.assertFalse(ok[0]["by_name"])

    def test_removed_from_the_index_is_refused(self):
        ok, refused = self.describe([crate("gone", "0.3.10")], {"gone 0.3.10": {"missing": True}})
        self.assertEqual(ok, [])
        self.assertIn("not in the crates.io index", refused[0]["why"])

    def test_unknown_date_is_refused(self):
        ok, refused = self.describe([crate("dark", "1.0.0")], {})
        self.assertEqual(ok, [])
        self.assertIn("could not be checked", refused[0]["why"])

    def test_young_code_running_crate_needs_its_name_typed(self):
        ok, _ = self.describe(
            [crate("mac", "1.0.0", proc_macro=True), crate("lib", "1.0.0"), crate("old", "1.0.0", build_script=True)],
            {"mac 1.0.0": {"pubtime": self.published(12)}, "lib 1.0.0": {"pubtime": self.published(12)},
             "old 1.0.0": {"pubtime": self.published(400)}})
        by = {i["name"]: i["by_name"] for i in ok}
        self.assertEqual(by, {"mac": True, "lib": False, "old": False})

    def test_urgent_override_lets_a_young_version_through_by_name(self):
        rbox.add_pairs(self.w.dirs.urgent, [("fix", "1.2.4")], "# h\n")
        ok, refused = self.describe([crate("fix", "1.2.4")], {"fix 1.2.4": {"pubtime": self.published(1)}})
        self.assertEqual(refused, [])
        self.assertTrue(ok[0]["by_name"] and ok[0]["urgent"])

    def test_version_change_is_shown_as_a_change(self):
        rbox.add_confirmed(self.w.dirs, [("serde", "1.0.228", "a" * 64)])
        ok, _ = self.describe([crate("serde", "1.0.229")], {"serde 1.0.229": {"pubtime": self.published(60)}})
        self.assertEqual(ok[0]["was"], "1.0.228")
        self.assertIn("1.0.228 -> 1.0.229", rbox.crate_line(ok[0])[1])

    def test_table_groups_code_running_crates_first(self):
        ok, refused = self.describe(
            [crate("plain", "1.0.0", via="top"), crate("builds", "1.0.0", build_script=True), crate("fresh", "9.0.0")],
            {"plain 1.0.0": {"pubtime": self.published(90)}, "builds 1.0.0": {"pubtime": self.published(90)},
             "fresh 9.0.0": {"pubtime": self.published(1)}})
        out = io.StringIO()
        rbox.show_pending(ok, refused, [{"name": "serde", "path": "third_party/serde", "patched": True}], out)
        text = out.getvalue()
        self.assertLess(text.index("RUNS CODE AT BUILD TIME (1)"), text.index("OTHER (1)"))
        self.assertIn("build script", text)
        self.assertIn("via top", text)
        self.assertIn("LOCAL CODE STANDING IN FOR A CRATE (1)", text)
        self.assertIn("CANNOT BE APPROVED TODAY (1)", text)

    def test_interactive_approval_writes_only_what_was_confirmed(self):
        rows = [crate("mac", "1.0.0", proc_macro=True), crate("lib", "1.0.0"), crate("young", "1.0.0", build_script=True)]
        index = {"mac 1.0.0": {"pubtime": self.published(90)}, "lib 1.0.0": {"pubtime": self.published(90)},
                 "young 1.0.0": {"pubtime": self.published(10)}}
        write_meta(self.w, rows)
        (self.w.project.state / "index.json").write_text(json.dumps(index))
        answers = iter(["", "yes", "no"])  # skip the by-name crate, approve code-running, decline the rest
        saved = (rbox.ask, rbox.require_human, rbox.find_bwrap, rbox.find_project, rbox.Dirs.toolchain)
        rbox.ask = lambda prompt: next(answers)
        rbox.require_human = lambda what: None
        rbox.find_bwrap = lambda: "/usr/bin/bwrap"
        rbox.find_project = lambda *a, **k: self.w.project
        rbox.Dirs.toolchain = lambda self_: self.w.toolchain

        def restore():
            rbox.ask, rbox.require_human, rbox.find_bwrap, rbox.find_project, rbox.Dirs.toolchain = saved

        self.addCleanup(restore)
        real_open = open

        def fake_open(path, *args, **kwargs):
            return io.StringIO() if path == "/dev/tty" else real_open(path, *args, **kwargs)

        rbox.open = fake_open
        self.addCleanup(lambda: delattr(rbox, "open"))
        code = rbox.cmd_allow(self.w.dirs, [])
        self.assertEqual(code, rbox.EXIT_WAITING)
        confirmed = {key[0] for key in rbox.load_confirmed(self.w.dirs)}
        self.assertEqual(confirmed, {"mac"})

    def test_allow_takes_no_arguments_so_listing_is_a_separate_command(self):
        # `rbox pending` lists and never approves; `rbox allow` only approves. That keeps a
        # deny rule on `rbox allow` from also blocking the harmless listing.
        with self.assertRaises(rbox.Stop):
            rbox.cmd_allow(self.w.dirs, ["--show"])
        with self.assertRaises(rbox.Stop):
            rbox.cmd_allow(self.w.dirs, ["serde"])


class SmallThings(unittest.TestCase):
    def test_index_paths(self):
        self.assertEqual(rbox.index_path("a"), "1/a")
        self.assertEqual(rbox.index_path("ab"), "2/ab")
        self.assertEqual(rbox.index_path("abc"), "3/a/abc")
        self.assertEqual(rbox.index_path("Cfg-If"), "cf/g-/cfg-if")
        self.assertEqual(rbox.index_path("serde_json"), "se/rd/serde_json")

    def test_age_text(self):
        self.assertEqual(rbox.age_text(None), "publish date unknown")
        self.assertEqual(rbox.age_text(0.5), "published 12 hours ago")
        self.assertEqual(rbox.age_text(41.7), "published 41 days ago")
        self.assertEqual(rbox.age_text(250), "published 8 months ago")
        self.assertEqual(rbox.age_text(1200), "published 3 years ago")

    def test_toolchain_pin_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            pin = Path(tmp) / "toolchain.sums"
            sha = "a" * 64
            pin.write_text(f"# c\nversion 1.100.0\nx86_64-unknown-linux-gnu {sha} https://static.rust-lang.org/dist/x.tar.xz\n"
                           f"aarch64-unknown-linux-gnu {sha} https://static.rust-lang.org/dist/y.tar.xz\n")
            version, parts = rbox.read_sums(pin, "x86_64-unknown-linux-gnu")
            self.assertEqual((version, parts), ("1.100.0", [(sha, "https://static.rust-lang.org/dist/x.tar.xz")]))
            pin.write_text(f"version 1.100.0\nx86_64-unknown-linux-gnu {sha} https://evil.example/x.tar.xz\n")
            with self.assertRaises(rbox.Stop):
                rbox.read_sums(pin, "x86_64-unknown-linux-gnu")
            pin.write_text("version 1.100.0\n")
            with self.assertRaises(rbox.Stop):
                rbox.read_sums(pin, "x86_64-unknown-linux-gnu")

    def test_shipped_pin_file_is_well_formed(self):
        pin = Path(rbox.__file__).resolve().parent / "toolchain.sums"
        for triple in ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"):
            version, parts = rbox.read_sums(pin, triple)
            self.assertEqual(len(parts), 5)
            names = " ".join(url for _, url in parts)
            for part in ("rustc-", "cargo-", "rust-std-", "clippy-", "rustfmt-"):
                self.assertIn(part + version, names)

    def test_test_world_moves_state_and_cache_only(self):
        real = rbox.Dirs({})
        world = rbox.Dirs({"RBOX_TEST_BASE": "/tmp/somewhere"})
        self.assertTrue(world.test_world and not real.test_world)
        self.assertEqual(world.share, real.share)
        self.assertNotEqual(world.confirmed, real.confirmed)

    def test_dirs_ignore_xdg_overrides(self):
        os.environ["XDG_STATE_HOME"] = "/tmp/attacker-state"
        self.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
        self.assertNotIn("attacker", str(rbox.Dirs(dict(os.environ)).confirmed))


if __name__ == "__main__":
    unittest.main()
